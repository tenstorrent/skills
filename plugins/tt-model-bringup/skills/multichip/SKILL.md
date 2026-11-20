---
name: multichip
description: Parallelize single-chip TTNN code across the available multi-chip hardware. Use when parallelizing a model or module, choosing a tensor/mesh parallel strategy, comparing to a single-chip TTNN baseline, and leaving correctness plus performance evidence.
---

# Parallelizing TTNN code to use multiple chips in a device mesh

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

Read `tech_reports/LLMs/llms.md`, especially section 3.3 (multi-device). Read
[multi-chip and CCL](../ttnn-knowledge/topics/multichip.md) before choosing the strategy: reference code, the
dense 1D TP pattern, Galaxy and 2D planning, distributed norms, validation and runtime setup. For MoE models also
read [MoE](../ttnn-knowledge/topics/moe.md).

Then read the single-chip baseline code and its reports. Identify layer kinds, tested shapes, sequence limits,
layouts, precision, cache behaviour, trace behaviour and latency.

## Plan

Choose a strategy for the hardware. For 1D meshes up to 8 chips, start with 1D tensor parallelism. For
Galaxy-class meshes, make a model-specific 2D plan; dense models may use 2D matmuls. The goal is the fastest
practical multichip implementation, not the most convenient one: apply as much compute and memory bandwidth as
possible while minimizing the data movement this adds. Treat the residual stream and module interfaces as
performance contracts and minimize collective time, even when that changes choices made in the single-chip
decoder.

For MoE models, the routed decode pipeline is part of the mesh plan: routing representation, replicated or TP
axis, expert mapping, sparse expert weight placement, score-weighted reduce path, and whether expert replication
across all layers is affordable. Expert placement (tensor-parallel, expert-parallel, replicated groups or a
hybrid) is a measured choice. Keep the gate-selected active-expert path; dense all-expert execution is not the
final path unless evidence shows it is faster in decode. Accept expert replication only if `memory_capacity_plan.json`
proves enough DRAM for all model layers plus the full KV cache at the target maximum sequence length, not just the
current decoder layer. If replication does not fit, document the alternative chosen and record the rejected
strategy and its evidence.

Before coding the final path, make a table of how each tensor, program config and shard spec changes under the
scheme. An optimized single-chip baseline's configs are sized for full tensors; most rows divide by the TP factor
along a mesh dimension, some need implicit or explicit padding.

Recompute the context-length contract: tensor parallelism changes per-device KV-cache and weight memory. Update
`models/autoports/<model>/doc/context_contract.json` with the target mesh, per-device KV-cache bytes, weight
bytes, reserved trace/activation buffer estimate and the largest supported context. Keep the HF-advertised context
unless a hard physical device limit prevents it from fitting or running and evidence proves the largest feasible
context. Convenience, runtime, profiling cost or implementation bugs are not valid reasons to reduce it.

Preserve valid logical sequence lengths. Padding of weights, activations, cache pages, CCL dimensions or prefill
chunks stays internal, masked or sliced at documented boundaries. A single-chip decoder that accepted arbitrary
valid prompt lengths does not become a multichip decoder that only accepts multiples of an internal chunk, tile,
block, page or trace size.

## Layer interface

The next stage stacks decoders, so each decoder's input and output formats match each other; they need not match
the single-chip decoder's. Chip-sharded activations at the decoder boundary are fine and often better. Adapt the
test code to provide that sharding, provided the conversion happens once at the model's entry and exit, not at
every layer. With several decoder layer kinds, their input and output shardings and dtypes are compatible. The
single-chip ops and configs may change too (for example sharded and replicated norm variants exist): nothing in
the baseline is fixed.

Before hand-writing an attention, MLP, expert or residual path, compare the model's dataflow with the reusable
modules in the multichip topic. If a common module cannot be used directly, write down the exact contract mismatch
and reproduce the relevant optimization family in model-local code. A first import, shape or helper API error is
not enough to discard a topology-critical optimization; adapt shapes, layouts, padding, weight packing or the
residual contract and retry, or use `$autofix`.

For row-parallel output projections and other "local result then collective" boundaries, make a topology table
before coding the final path, with at least: local matmul plus all-reduce or full gather; reduce-scatter plus
delayed gather; fused all-gather-matmul where the next matmul consumes gathered input; fused matmul plus
reduce-scatter or all-reduce where supported; and a residual-sharded variant. For each row record the residual
layout before and after, the next consumer, expected collective bytes, activation/CCL dtype, persistent-buffer plan
and why it should or should not win. Fused CCL ops have hung or returned wrong values on 4-chip Blackhole meshes,
and repaired versions often measured slower than the separate ops; measure the unfused path first and keep the
faster correct one ([multi-chip and CCL](../ttnn-knowledge/topics/multichip.md)).

Do not make the replicated residual the only measured contract. `reduce-scatter -> all-gather` back to back is
correct but mostly recreates the communication a fused path avoids. If a candidate produces a sharded residual,
adapt the next norm, residual add, attention input, MLP input or test fixture to consume it directly. A standalone
decoder test may gather at the test boundary to compare with the reference, but that gather is not inside the
measured layer unless the stacked model would pay it at every layer.

RMSNorm must be correct; a distributed norm is not mandatory. A replicated stream with a local norm is acceptable
if it keeps the decoder chain's layout and measures faster.

## Correctness And Performance

Choose the topology for performance before polishing correctness: time spent proving a design that adds needless
communication is usually lost. The chosen path still meets every correctness requirement below. Validate against
the single-chip TTNN baseline with identical synthetic or real weights, inputs, page tables and positions.

RMSNorm, KV-cache layout, head ownership, bias placement, padding and slicing, and collective axes are first-class
correctness questions. If a failure involves subtle mesh layout, CCL, semaphore, cache or routed-expert behaviour
and component comparisons are not enough, use `$autofix`; it runs `$autodebug` if needed and verifies or refutes
each proposed bug before keeping a fix.

Async CCLs and semaphores are subtle: read the relevant docs and reuse the CCL/semaphore helpers of
`tt_transformers`, `gpt_oss` or `deepseek_v3` where they fit. Bind static mode choices at construction and keep
runtime forwards straight-line; measured prefill and decode have no `ttnn.from_torch`, `ttnn.to_torch`, host
reads or writes, or tensor allocation after trace capture. A bias applied before an all-reduce is proven not to be
applied once per TP shard unless intended.

Use `$optimize` and `tt-perf-report` before finishing this stage. The final multichip decoder uses the hardware
well within the limits of available TTNN ops.

## Evidence To Leave

Done means all of these are true and recorded:

- Which single-chip baseline was used and why.
- Target mesh shape, hardware, and chosen parallelization strategy for both weights and activations with calculated evidence showing why this is optimal for the model and hardware vs other sensible options.
- Updated context contract for the multichip target mesh, including any hard-physical-limit context reduction.
- Evidence that valid non-aligned logical sequence lengths still work after multichip sharding and padding.
- Table of tensor shapes, configs and shard specs along with how these are mesh-sharded, how that affects the per-device shapes used and any implicit or explicit padding that is necessary as a result.
- Multi-chip prefill and decode PCC against the single-chip TTNN baseline.
- Paged KV-cache behavior on the target mesh.
- Warmed trace replay for decode on the target mesh.
- Determinism or stress coverage appropriate to the implementation risk.
- Runtime fallback audit remains clean.
- Watcher-clean run (`export TT_METAL_WATCHER=10`). If it has a false positive explain why it's a false positive.
- Warmed single-chip baseline and multi-chip latency, speedup, and efficiency.
- `tt-perf-report` output with the main communication, DRAM, compute, and data-movement findings.

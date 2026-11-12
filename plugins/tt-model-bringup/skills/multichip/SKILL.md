---
name: multichip
description: Parallelize single-chip TTNN code across the available multi-chip hardware. Use when parallelizing a model or module, choosing a tensor/mesh parallel strategy, comparing to a single-chip TTNN baseline, and leaving correctness plus performance evidence.
---

# Parallelizing TTNN code to use multiple chips in a device mesh

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


Read and follow `tech_reports/LLMs/llms.md` particularly section 3.3 Multi-Device. At the time of writing of this skill, that section did not include EP (expert parallelism) or MoE model specifics but is otherwise an excellent guide.

Then carefully read your single-chip baseline code and any reports/documentation it comes with. Identify layer kinds, tested shapes, sequence limits, layouts, precision, cache behavior, trace behavior, and latency.

Choose a strategy for the hardware. For 1D meshes up to 8 chips, start with 1D tensor parallelism. For Galaxy-class meshes, make a model-specific 2D plan. For dense models this may use 2D matmuls. For MoE models, make the routed decode pipeline part of the mesh plan: choose the routing representation, replicated or TP axis, expert mapping, sparse expert weight placement, score-weighted reduce path, and whether the model can afford expert replication across all layers. Choose EP, TP or hybrid and the batched-decode path with the decision rule in "MoE And Expert Replication" below. If your single-chip code has already been optimized its program configs and shard specs will all be configured for the full weight/tensor sizes. Create a table before coding the final path that shows how each tensor, program config, and shard spec changes under your parallelism scheme. Most rows divide by the TP factor along a mesh dimension. Some rows need implicit or explicit padding.

Recompute the context-length contract when moving to multiple chips. Tensor parallelism changes per-device KV-cache and weight memory. Update `models/autoports/<model>/doc/context_contract.json` with the target mesh, per-device KV-cache bytes, weight bytes, reserved trace/activation buffer estimate, and the largest supported context. Keep the HF-advertised context unless a hard physical device limit prevents it from fitting or running and evidence proves the largest feasible context. Convenience, runtime, profiling cost, or implementation bugs are not valid reasons to reduce advertised capability.

Preserve valid logical sequence lengths while changing sharding. If the multichip path pads weights, activations, cache pages, CCL dimensions, or prefill chunks, keep that padding internal and mask/slice at documented boundaries. Do not turn a single-chip decoder that accepted arbitrary valid prompt lengths into a multichip decoder that only accepts lengths divisible by the internal chunk, tile, block, page, or trace size.

A good multichip design needs to keep two objectives in mind: apply as much compute and memory bandwidth to the workload as possible, and minimize the extra data movement this creates. In practice this means deciding which weight tensors to fracture across which mesh dimensions, and how activations move between modules. The second decision is similar to within-chip sharding in the decoder optimization stage. Treat the residual stream and module interfaces as performance contracts. Minimize collective time, even when that requires changing choices made in the single-chip decoder. The goal is the fastest practical multichip implementation, not the most convenient one.

The next step will stack decoders together, so we should ensure that the decoder's output and input formats are the same as each other, but they do NOT need to be the same as the single-chip's inputs/outputs. Clearly given our goal, it's absolutely fine and in many cases desirable for the multichip implementation to maintain chip-sharded activations on its inputs and outputs, something the single-chip version cannot do. The right thing to do in this case is to modify the test code itself to provide the correct sharding for the multi-chip decoder as in the final model as long as this will only need to be performed once at the entry/exit to the entire model and not at the boundary to every layer. If there are multiple decoder layer types it's probably important that their input+output shardings and datatypes are compatible with each other, however! You may need to make other changes to the single-chip implementation's ops and configurations as well - for example we have both sharded and replicated variants of norms available. Think everything through and take nothing for granted as fixed or unalterable.

Before committing to a hand-written attention, MLP, expert, or residual path, compare the model's dataflow to the reusable modules listed below. If a common module cannot be used directly, write down the exact contract mismatch and still reproduce the relevant optimization family in model-local code. A first import error, shape error, or helper API mismatch is not enough to discard a topology-critical optimization; adapt shapes, layouts, padding, weight packing, or the residual contract and retry, or use `$autofix`.

For row-parallel output projections and other "local result then collective" boundaries, make a small topology table before coding the final path. Include at least: local matmul plus all-reduce/full gather, reduce-scatter plus delayed gather, fused all-gather-matmul where the next matmul consumes gathered input, fused matmul plus reduce-scatter/all-reduce where supported, and a residual-sharded variant. For each row record the residual layout before/after, the next op that consumes it, expected collective bytes, activation/CCL dtype, persistent-buffer plan, and why it should or should not win.

Do not make the current replicated residual contract the only measured contract. A path that does `reduce-scatter -> all-gather` immediately may be correct, but it mostly recreates the communication the fused path was meant to avoid. If the candidate produces a sharded or fractured residual, adapt the next norm, residual add, attention input, MLP input, or test fixture so that layout can be consumed directly. For standalone decoder tests, it is fine to gather only at the test boundary to compare against the reference, but do not include that boundary gather inside the measured decoder layer unless the full stacked model would also pay it at every layer.

The typical advice is "get correctness before chasing speed" but do not let this mislead you: our only acceptable goal is an extremely high-performance implementation. Pursuing the correctness of a design that clearly introduces unnecessary communication patterns may not be a good use of time. To evaluate correctness we recommend comparing multi-chip TTNN output to the single-chip TTNN baseline with identical synthetic or real weights, inputs, page tables, and positions. This isolates sharding and collective bugs from HF-vs-TTNN numerical differences.

Treat RMSNorm, KV-cache layout, head ownership, bias placement, padding/slicing, and collective axes as first-class correctness questions. Most multi-chip bugs live there. If the failure involves subtle mesh layout, CCL, semaphore, cache, or routed-expert behavior and ordinary component comparisons are not enough, use `$autofix`; it will run `$autodebug` if needed, then verify or refute each proposed bug before keeping any fix.

Async CCLs and semaphores are subtle. Read the relevant docs and consider reusing the CCL/semaphore helper classes that `tt_transformers`, `gpt_oss`, and `deepseek_v3` use.

Use `$optimize` and `tt-perf-report` before finishing this stage. The final multichip decoder should use the hardware well within the limits of available TTNN ops.

## Evidence To Leave

Done means all of these are true and recorded:

- Which single-chip baseline was used and why.
- Target mesh shape, hardware, and chosen parallelization strategy for both weights and activtations with calculated evidence showing why this is optimal for the model and hardware vs other sensible options.
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

# Multichip Parallelization Knowledge

This reference is a compact map of tt-metal model code worth reading before adding multi-chip decoder execution.

## Primary Style Recommendation

Prefer the GPT-OSS structured approach and the TTTv2 modules, which now live in [tenstorrent/tt-transformers](https://github.com/tenstorrent/tt-transformers) (moved out of tt-metal `models/common/modules`):

- `models/demos/gpt_oss/config.py`: `ModeConfig`, `MeshConfig`, semantic mapper helpers such as `column_parallel`, `row_parallel`, `sequence_parallel`, and CCL helper methods.
- `models/demos/gpt_oss/tt/ccl.py`: small generic `CCLManager` with ping-pong semaphores instead of model-specific CCL control flow.
- `models/demos/gpt_oss/tests/test_factory.py`: target mesh setup, fabric config, and hardware-shape parametrization.
- `tt-transformers:src/tt_transformers/modules/attention/attention_1d.py`: 1D attention with local heads, paged KV cache, optional fused all-gather plus WO matmul, and reduce-scatter output.
- `tt-transformers:src/tt_transformers/modules/mlp/mlp_1d.py`: 1D MLP with W1/W3 output sharding, W2 input sharding, and reduce-scatter output.
- `tt-transformers:src/tt_transformers/modules/rmsnorm/rmsnorm_1d.py` and `rmsnorm_2d.py`: reusable RMSNorm paths, including distributed stats.
- `models/common/tt_ccl.py`: common CCL semaphores, topology detection, and link-count helpers.

Use these as the first implementation references. If they do not fit the target model, write model-local code in the same structured style: config dataclasses, explicit mesh helpers, setup-time weight conversion, and straight-line runtime forwards.

Do not treat "does not fit" as a stopping point. The important reusable knowledge is the contract: local head ownership, sharded residuals, distributed norms, fused all-gather plus output projection, reduce-scatter outputs, persistent CCL buffers, and setup-time weight transformation. If the target model needs custom math, preserve these performance contracts where the math allows it.

## Galaxy As Evidence, Not Template

`models/demos/llama3_70b_galaxy/` is highly optimized and useful for understanding where collectives belong, but it is too model-specific to copy into this skill's default path.

Useful lessons:

- Attention splits Q/K/V work across devices, runs SDPA on local heads, gathers or concatenates heads before WO, then reduces output to restore residual layout.
- MLP performs W1/W3 work on sharded intermediate chunks, gathers when W2 needs the full intermediate, then reduces output.
- The residual layout after each sublayer is a first-class performance contract.

Avoid copying:

- custom `llama_rs_*` ops as a baseline;
- 32-chip asserts;
- prefetcher-specific control flow;
- environment-variable topology switches as the normal user interface;
- model-specific hardcoded memory configs where generic config helpers can express the same decision.

Read these files when targeting Galaxy:

- `models/demos/llama3_70b_galaxy/tt/model_config.py`
- `models/demos/llama3_70b_galaxy/tt/llama_attention.py`
- `models/demos/llama3_70b_galaxy/tt/llama_mlp.py`
- `models/demos/llama3_70b_galaxy/tt/llama_ccl.py`
- `models/demos/llama3_70b_galaxy/tt/distributed_norm.py`

## Dense 1D Tensor Parallel Default

For 1D meshes up to 8 devices, start with the common TP pattern used in LLM implementations:

- WQKV: column/output sharding, so each device owns local Q/K/V heads.
- KV cache: per-device local KV-head cache, paged if the baseline is paged.
- SDPA: local to the local heads.
- WO: row/input sharding over concatenated local heads, then reduce-scatter or all-reduce to restore residual layout.
- W1/W3: column/output sharding over intermediate dimension.
- W2: row/input sharding over intermediate dimension, then reduce-scatter or all-reduce to restore residual layout.

The output of each decoder layer must match the input layout contract. Avoid a design that requires an extra gather or reshard between stacked decoder layers unless evidence shows it is faster overall.

Do not pick all-reduce simply because it gives a convenient replicated tensor. For each row-parallel output, compare replicated residual and sharded residual contracts. A replicated contract can simplify correctness, but a sharded contract can remove or shrink collectives across the full stack. The correct choice is the fastest measured whole-layer path that can be stacked without an extra boundary conversion.

When a sharded residual contract is hard to wire through the full layer, build the smallest shape-faithful probe that includes the producing op and the next consuming op. For example, do not stop at "fused matmul plus reduce-scatter runs" or "fused matmul plus reduce-scatter followed by immediate all-gather is slow"; test whether the next norm, residual add, attention, or MLP boundary can consume the reduced layout. Use `$autofix` if the first layout/API attempt fails.

## RMSNorm Correctness

The distributed RMSNorm primitive pair is:

1. `ttnn.rms_norm_pre_all_gather`
2. `ttnn.experimental.all_gather_async` or `ttnn.all_gather` for the stats
3. `ttnn.rms_norm_post_all_gather`

Use this when hidden activations are sharded across the normalized dimension and local RMSNorm would compute incorrect statistics. The reusable implementations to read are:

- `tt-transformers:src/tt_transformers/modules/rmsnorm/rmsnorm_1d.py` `_prefill_1d_distributed`
- `tt-transformers:src/tt_transformers/modules/rmsnorm/rmsnorm_2d.py` `decode_forward`
- `tt-transformers:src/tt_transformers/modules/rmsnorm/rmsnorm_2d.py` `prefill_forward`
- `models/demos/deepseek_v3/tt/rms_norm/distributed_rms_norm.py::DistributedRMSNorm`
- `models/ttt_compat/tt/ccl.py::tt_distributed_rmsnorm`
- `models/ttt_compat/tt/ccl.py::tt_sharded_distributed_rmsnorm`

Do not phrase distributed RMSNorm as always mandatory. Correct RMSNorm is mandatory. A faster replicated-activation stream plus local RMSNorm is acceptable if it preserves the decoder chain layout and improves measured performance.

## MoE And Expert Replication

Keep the gate-selected active-expert path from the single-chip baseline. Do not run every expert densely as the final path unless measurement proves it faster.

### Expert layout decision rule

Compute these from the HF config and write them in `mesh_plan.md`: expert count `E`, top-k `k`, expert intermediate `I`, TP width per chip `I/TP` in 32-wide tiles, and the decode batch `B` the server will run.

- Many narrow experts (`I/TP` below about 8 tiles, or `E >= 64`): keep whole experts per chip (expert parallel, EP). TP slices that narrow give few-core matmuls and poor DRAM streaming. Batch-1 load-balance arguments are not a reason to reject EP.
- Few wide experts (Mixtral or GPT-OSS shapes): TP of each active expert is fine.
- Hybrid is fine: EP for prefill plus indexed TP (`sparse_matmul` with per-token `indices`) at batch 1.

These thresholds are defaults. Record the computed values and the measured result if you depart from them.

### Batched decode

At `B > 1`, compute the expected union of selected experts per layer, `u = 1 - (1 - k/E)^B` (the fraction of all experts any row selects). When `u` exceeds about 25%, do not use a union-masked `sparse_matmul` over all experts: it computes every touched expert for every row and adds full `[E, B, H]` mask and weight ops. Route each token to its own experts instead:

`ttnn.experimental.deepseek_prefill` dispatch -> `moe_fused_swiglu` -> combine -> `post_combine_reduce`

These ops work on a 1x4 Blackhole mesh with a single-device dispatch group since tt-metal #60445. The same path serves prefill. Keep a separate indexed batch-1 path and trace when it is faster for a lone request.

### Reference implementations (QB2, 1x4 Blackhole)

- `models/demos/blackhole/qwen36/tt/moe/`: EP, `sparse_matmul` decode/prefill, `reduce_scatter` into the hidden-sharded residual.
- `models/demos/gemma4_26b_a4b_qb2/tt/multichip_decoder.py`: hybrid EP prefill plus indexed TP decode.
- `models/autoports/aleph_alpha_kolibri_1_bf16/tt/multichip_decoder.py`: EP weights, token-grouped dispatch / fused SwiGLU / combine for prefill and batched decode, indexed batch-1 path.
- `models/demos/gpt_oss/tt/experts/` and `models/demos/gpt_oss/tt/topk.py`: TP active experts for few wide experts. `models/demos/gpt_oss/tt/experts_throughput/` is the Galaxy 4x8 variant; accept expert replication only if `memory_capacity_plan.json` proves DRAM for all layers plus full KV cache.

Evidence: on Kolibri-1 (384 experts, top-6, I=512) moving from TP plus union-masked decode to EP plus grouped decode took 1 user on the 32-slot server from 114 to 57 ms per token; on K2-MoVA EP decode took a 32-user step from 62 to 41 ms.

### Prefill chunk and router precision

- Default the MoE prefill chunk to 2048 tokens and align the prefill buckets to it. Do not pad a 1024 chunk into a 2048 bucket.
- Compute router logits in fp32; BF16 is fine after top-k. Do not chase bit-exact top-k tie order against Torch: accept top-1 flips that occur only on near-ties and record them.

### Correctness and evidence

Treat expert mapping, routing-weight layout, shared-expert placement, expert weight layout, memory configs, semaphores and preallocated buffers, and the final residual layout as correctness contracts. Validate the whole router -> expert compute -> score weighting -> expert reduce -> collective sequence, not the gate and experts separately.

Stage 5 must report decode at batch 1 and at the largest batch the server will run, with realistic, diverse routing (real prompts, not the same token in every row), plus the MoE share of the decode step. Record the rejected layouts with their evidence.

## 2D Mesh Planning

On Galaxy 4x8, do not blindly flatten to TP=8. Make an explicit 2D plan:

- Choose which axis owns TP, expert parallelism, sequence parallelism, or replication.
- Identify which collectives cross rows and which cross columns.
- Estimate activation, weight, expert, and KV-cache memory with all layers loaded.
- Compare communication volume against the 1D alternative.
- Record why the selected strategy should improve single-user latency.

GPT-OSS `MeshConfig` is the preferred shape for representing this plan. Galaxy Llama is useful for validating where reductions and gathers happen in an optimized 2D decoder, not for copying bespoke ops.

## Validation Heuristics

Compare multi-chip output to the single-chip TTNN baseline first. This removes HF-vs-TTNN numerical differences from the bug hunt. If multi-chip PCC is close to the minimum threshold, split the decoder into component comparisons:

- input RMSNorm;
- QKV projection;
- RoPE;
- SDPA output;
- WO projection and reduction;
- post-attention residual;
- post-attention RMSNorm;
- router/top-k for MoE;
- active expert outputs;
- W2/down projection and reduction;
- final residual.

Check layouts and collectives before changing precision. Most multi-chip bringup bugs are wrong sharding, wrong gather/reduce axis, bad padding/slicing, repeated bias application after all-reduce, wrong local head count, wrong local KV-cache shape, or mismatched input/output residual layout.

## Runtime Gotchas

- Before opening any TTNN mesh that will run CCL, configure the fabric that matches the intended mesh/topology; prefer repo pytest `device_params`, or call `ttnn.set_fabric_config(...)` before `ttnn.open_mesh_device(...)`.
    - For example, for 8-chip Wormhole/T3K 1D TP the expected setup is `FABRIC_1D_RING` before mesh open and `ttnn.Topology.Ring` for CCL ops.
- Treat CCL failures from raw `open_mesh_device` as setup evidence, not hardware evidence, until the same case fails with the correct fabric config and matching CCL topology.
- Reset all devices after any failed run that uses multiple chips.
- Bind static mode choices at construction time when possible; keep runtime forward paths straight-line.
- Avoid `ttnn.from_torch`, `ttnn.to_torch`, host reads, host writes, or tensor allocation after trace capture inside measured prefill/decode paths.
- Use source-built watcher separately from profiler runs.
- Make CCL semaphore ownership explicit; reuse generic CCL managers unless the target model already has a better local abstraction.
- Pad CCL-sensitive hidden dimensions deliberately and slice at a documented boundary.
- If a bias is applied before an all-reduce, prove it is not applied once per TP shard unless that is intended.

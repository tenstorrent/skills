---
name: functional-decoder
description: Bring up a functionally correct TTNN implementation of HuggingFace transformer decoder layers under the model-specific autoport directory. Use when producing a functional_decoder.py implementation, reading the HF decoder architecture, validating paged prefill/decode against the HF reference, and leaving compact correctness and performance evidence.
---

# Functional Decoder Bringup

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

## Mission Context

If this skill is used as part of `$model-bringup`, follow that skill's mission, workspace, and reporting
contract. This stage turns the HF decoder layer, or each layer kind, into complete, tested TTNN code on a single
1x1 device mesh. "Functional" means complete and ready to ship, not a prototype: every later stage builds on it.

## Your Part

Implement `models/autoports/<model>/tt/functional_decoder.py` with a `FunctionalDecoder` that subclasses
`models.common.lightweightmodule.LightweightModule` and loads real weights:

```python
class FunctionalDecoder(LightweightModule):
    @classmethod
    def from_state_dict(cls, state_dict, *, hf_config, layer_idx, mesh_device, **kwargs): ...

    def prefill_forward(self, ...): ...
    def decode_forward(self, ...): ...
```

`<model>` is the HF model id lowercased with every non-alphanumeric character replaced by an underscore
(`org/Model-1.2B-Instruct` becomes `org_model_1_2b_instruct`); gates find the directory by this name, so add no
other qualifiers. Choose keyword-friendly forward signatures that fit the model and record them in the README.

## How To Approach It

Understand the HF model first: load `AutoConfig`, read the installed `configuration_*.py` and `modeling_*.py`,
and trace the decoder line by line (attention, RoPE, cache API, MLP or MoE, residual order, norms, activation,
biases, head shapes, layer-kind differences). Read the matching working implementations in
[reference code](../ttnn-knowledge/topics/reference-code.md) before designing.

Implement correctness first: BF16, tile layout and DRAM are fine while proving semantics. Weight conversion,
dtype choice, `ttnn.as_tensor` and cache construction belong in `from_state_dict` or setup helpers; runtime
prefill and decode have no hidden `torch`, `from_torch`, `to_torch` or host fallback. Defaults and HF-reference
practice: [correctness](../ttnn-knowledge/topics/correctness.md).

For MoE models, validate the real router and the gate-selected experts end to end, not only gate and experts
separately. On non-Galaxy hardware start from the GPT-OSS active-expert pattern (top-k scores as a sparsity
tensor, `ttnn.sparse_matmul` for gate/up/down, score weighting, reduce over experts); not from Galaxy-only fused
paths without evidence they run on the target. MoE behaviour by batch: [MoE](../ttnn-knowledge/topics/moe.md).

When PCC is low, narrow it down: split into components, check HF parity, raise fidelity, simplify the shape. PCC
near 1 can still hide a uniform gain error or a flipped discrete choice: [numerics](../ttnn-knowledge/topics/numerics.md).
If narrowing stalls, use `$autofix`. If the cause is a tt-metal bug, keep a minimal reproducer, then work around
it in the model (`scope` `release`) or fix the op with a test (`scope` `open`; see `model-bringup`), and record
the evidence.

## Context, Length And Batch Contract

Derive the capability contract from the HF config and code before writing final tests: context length, cache and
position semantics, layer kinds, mode switches such as sliding vs full attention, and the prefill/decode
behaviour later stages must preserve.

- The target context is the full value the HF config advertises (usually `max_position_embeddings`). Do not
  create a smaller config or hide a smaller implementation behind a smaller `max_model_len`.
- Reduce it only for a hard physical device limit (for example device DRAM for weights, KV and persistent
  buffers). Record the byte calculation or failed probe, the largest feasible value and the setting that uses it.
  Runtime, profiling or watcher cost is not capacity evidence.
- Prefill and decode tests include the full supported length, or the largest feasible one with that evidence.
- Public prefill accepts any valid logical length up to the supported context. Pad or chunk internally, mask
  padded tokens, slice outputs back; no `seq_len % chunk == 0` checks on the public path unless the HF model
  itself has the restriction. Tests include non-aligned lengths, and window boundaries for sliding-window models.
- Prefill commonly uses `(1, batch, seq_len, hidden)` and decode `(1, 1, batch, hidden)`. Start at batch 1 but
  hard-code it nowhere (masks, page tables, positions, cache indexing, residuals, outputs). After batch 1 passes,
  test up to the largest batch in `batch_sizes` (default 32) when hardware allows; otherwise record the largest
  tested batch and the limit.
- Full and sliding attention that differ only by mask or window share one parameterized implementation and tests.

## Implement Chunked Prefill

Implement chunked prefill now: a bounded physical chunk reuses working shapes for any logical length, traces
well, and keeps L1 use manageable. Read how existing code does it, without copying its restrictions:

- [TT-Transformers generator](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_transformers/tt/generator.py): tokens sliced relative to the new input, cache pages and RoPE/attention positions absolute; its divisibility restrictions are internal, not API.
- [TT-Transformers attention](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_transformers/tt/attention.py): chunk-local page tables drive cache writes; `chunked_scaled_dot_product_attention` reads the accumulated cache with a causal offset. It rejects sliding-window attention.
- [Gemma4 generator](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/demos/gemma4/tt/generator.py): last-token indices, intermediate chunks, per-layer page-table routing and sliding-cache tails; its traced-chunk path excludes bounded sliding KV.

Requirements:
- Keep chunk start, valid token count and padded physical length distinct. Absolute position is the cached prefix
  length plus the offset in the new input. Prior K/V is preserved, new K/V goes to the request's own pages, each
  query sees the valid prefix and the causal current chunk, and padding never reaches attention or live cache.
- Every intermediate chunk fills its K/V. Generation logits come from the last logical token, not a padded row;
  all logical outputs are kept when a caller asks for them. The generator orchestrates; the decoder exposes the cache and position inputs it needs.
- One prefill call can carry several rows with different lengths and starts, with independent cache writes,
  positions and last-token selection. A per-row loop is the functional baseline; running dense work once across
  rows is the later target.
- Test increasing lengths on one loaded instance (below, at and above a chunk boundary; several chunks plus a
  tail; then a short request again) against HF and an unchunked control, including decode after the fill. Keep
  fresh requests and explicit continuation apart, and change tokens and page ownership to expose stale state.
- Test continuation at the starts that a prefix-cache hit, a chunk continuation and a preemption resume produce:
  the smallest block, the least common multiple of the block sizes, a tile-unaligned start, the sliding window and
  one past it, and the last token. Compare the layer's contribution (output minus residual input) per row with the
  full-prefill run; whole-output PCC hides attention errors behind the residual. A negative control that drops
  the prefix history must fail the same check. The full-model and serving stages reuse this path
  ([prefill features](../vllm-integration/references/prefill-features.md)); known causes of mismatches at
  unaligned starts: [prefill and KV](../ttnn-knowledge/topics/prefill-kv.md).

For tracing later: keep host chunk planning separate from device execution, and prepare every physical shape and
op path (first chunk, continuation, tails) before the first capture (`$tt-enable-tracing`). Refresh positions, valid
lengths and page tables before replay, and restore cache state after warmup and capture. A new logical length
inside a prepared bucket reuses its trace. A fixed-input replay passing does not prove increasing lengths work.

If you find strong evidence that a non-chunked prefill is faster and still trace-safe with interleaved prefill
and decode of varied lengths, present it before switching.

## Paged KV Cache

Paged prefill and paged decode are the final path. Read the paged KV basics and failure modes in
[prefill and KV](../ttnn-knowledge/topics/prefill-kv.md) before writing the cache tests (fill and update dtype
contracts, tensor current positions, page-table permutations, read windows past the row's end, the decode
query grid).

## Trace And Profiling

- The final decode pass is traced; if capture or replay gets tricky, use `$tt-enable-tracing` rather than accepting
  eager decode. Measure decode PCC from replay output.
- Allocation after capture is a defect: run repeated replay with `TT_METAL_TRACE_ALLOC_TRACKING=1` over every
  captured path and resolve each unsafe allocation per the checkout's
  `tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`. Common causes:
  [tracing](../ttnn-knowledge/topics/tracing.md).
- Measure warmed prefill and decode separately with signposts and `tt-perf-report`
  ([profiling](../ttnn-knowledge/topics/profiling.md)).

## Fallback Audit

- Runtime prefill and decode call no torch, `ttnn.from_torch` or `ttnn.to_torch`. Allowed in tests: setup inside
  `from_state_dict`, input construction, the final PCC comparison, and a named prefill-to-decode boundary.
- Fallback hidden in helpers counts; read wrappers as well as the layer file. The host-side last-token extraction
  in `models/tt_transformers/tt/model.py` is a production compromise, not a pattern to copy.
- Device commands follow `tt-device-usage`. If a wheel-installed `ttnn` hits SFPI/JIT mismatches, use a
  source-built tt-metal whose headers and compiler match.

## Code Quality

Explicit model contracts over permissive fallbacks; invalid config values fail rather than being patched; no
generality for shapes the model does not have; no debug fallbacks or shims in the final code without a reason.

## Evidence To Leave

`PCC >= 0.995` is the default bar for prefill and decode unless the model gives a concrete reason for another.
Done means all of these are recorded:

- HF-vs-TTNN prefill and decode PCC and performance for each layer kind, and a real-weight test that passes the
  bar for every layer kind.
- Warmed prefill and traced decode performance with `tt-perf-report` output.
- Paged KV-cache behaviour, including page-table and current-position handling.
- A capability-contract table (claim, evidence, remaining risk) for context length, cache behaviour, layer kinds
  and mode switches, and `models/autoports/<model>/doc/context_contract.json` with the HF-advertised context,
  the supported context, the largest tested prefill and decode contexts, and any reduction with its evidence.
- Length coverage: a small smoke length, a length exactly at a tile, page or chunk boundary, one just across it,
  and a long non-divisible length near the supported context (a reduced layer harness is fine for the long one).
- Determinism for repeated identical inputs.
- No torch or host calls inside a single prefill or decode pass.
- A watcher-clean run with `TT_METAL_WATCHER=10`; if it cannot run, the exact environment failure and replacement
  evidence. A suspected false positive needs evidence; never skip asserts or weaken the test.

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

If this skill is used as part of `$model-bringup`, follow that skill's mission, workspace, and reporting contract. This stage turns the HF decoder layer (or multiple layers if there are different kinds in the HF model) into complete, working, tested TTNN code. It's called "functional" because it should be functionally complete and ready to ship - this is not a minimal prototype, it's the heart of everything else that follows. You should bring up the decoder on a single 1x1 device mesh. No need to use multiple devices at this stage.

## Your Part

Implement:

```text
models/autoports/<model>/tt/functional_decoder.py
```

Derive `<model>` from the HF model id: lowercase it and replace every non-alphanumeric character with an underscore, for example `org/Model-1.2B-Instruct` becomes `org_model_1_2b_instruct`. Downstream verification gates and tooling resolve the autoport directory from the HF model id, so do not add extra qualifiers such as hardware or experiment names.

with a model-appropriate `FunctionalDecoder` that subclasses `models.common.lightweightmodule.LightweightModule` and exposes a real weight-loading boundary:

```python
class FunctionalDecoder(LightweightModule):
    @classmethod
    def from_state_dict(cls, state_dict, *, hf_config, layer_idx, mesh_device, **kwargs): ...

    def prefill_forward(self, ...): ...
    def decode_forward(self, ...): ...
```

Implement forward signatures that fit the model. Keep them keyword-friendly. Record the signatures in the README.

## How To Approach It

Start by understanding the HF model. Load `AutoConfig`, inspect the installed `configuration_*.py` and `modeling_*.py`, and identify the decoder layer, attention, RoPE, cache API, MLP/MoE path, residual order, norm behavior, activation, bias flags, head shapes, and layer-kind differences. Trace through the model's execution line by line to make sure you understand it.

Before writing final tests, derive the model capability contract from the HF config and model code. For decoder-layer bringup this includes the target context/sequence length, cache and position semantics, layer kinds, mode switches such as sliding vs full attention, and the externally visible prefill/decode behavior later stages must preserve. Small shapes are useful for debugging, but stage completion requires evidence for the advertised contract or a hard physical device limit reason for the largest feasible reduction.

Support logical sequence lengths, not only friendly aligned lengths. If the implementation chunks, pads, or tiles prefill internally, public prefill inputs should still accept any valid `seq_len <= supported_context` unless the HF model itself has a real semantic restriction. Round or pad internally, mask padded tokens, and slice outputs back to the logical length. Do not leave checks such as `seq_len % 1024 == 0` on the public model path just because the current chunking helper wants that shape.

Implement correctness first. BF16, tile layout, and DRAM memory are fine while proving semantics. Move weight conversion, reshaping, dtype selection, `ttnn.as_tensor`, and cache construction into `from_state_dict` or setup helpers. Keep runtime prefill/decode free of hidden `torch`, `from_torch`, `to_torch`, or host fallback except explicit test boundaries.

For MoE models, validate the real router/gate and active experts end-to-end. Component tests for gate or experts are useful diagnostics, but the decoder result should include the gate-selected expert path a real model run would use. For non-Galaxy bringup, use the GPT-OSS active-expert pattern as the default: router/top-k scores as a sparsity tensor, `ttnn.sparse_matmul` for gate/up/down projections, score weighting, and a model-appropriate reduce over experts. Do not start from Galaxy-only fused MoE paths unless you have direct evidence that the target hardware and ops support them.

When PCC is low, debug it. Split the decoder into components, check HF parity, raise fidelity where useful, simplify the failing shape, and keep narrowing until the cause is understood. If a bug is tricky enough that ordinary narrowing stalls, use `$autofix`; it will run `$autodebug` if needed, then verify or refute each proposed bug before keeping any fix. If the cause is a tt-metal bug, make a reproducer or an on-branch workaround and record the evidence.

## Implement Chunked Prefill

Implement chunked prefill during functional bringup. A bounded physical chunk size can reuse working shapes while preserving arbitrary logical prompt lengths, it plays nicely with tracing and makes it easier to optimize L1 usage.

Read existing implementations before designing the path:

- [TT-Transformers generator](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_transformers/tt/generator.py): `prefill_forward_single_user_text` slices tokens relative to the new input, but selects cache pages and sets RoPE/attention positions using absolute offsets. Its helpers operate on padded physical lengths; do not copy their divisibility restrictions into the public API.
- [TT-Transformers attention](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/tt_transformers/tt/attention.py): chunk-local page tables drive cache writes, while `chunked_scaled_dot_product_attention` reads the accumulated cache with a causal chunk offset. This path explicitly rejects sliding-window attention.
- [Gemma4 generator](https://github.com/tenstorrent/tt-metal/blob/fc7777ceca4a2360667dbb193b28fedcccc89866/models/demos/gemma4/tt/generator.py): true last-token indices, intermediate-chunk handling, per-layer page-table routing, and bounded sliding-cache tails need model-specific treatment. Its traced-chunk path excludes bounded sliding KV. Inspect the target revision and layer contracts rather than transplanting these exceptions.

- Keep chunk start, valid token count, and padded physical length distinct. For continuation, absolute position is cached-prefix length plus the offset in the new input. Preserve prior K/V, write new K/V to the intended request's pages, and let each query attend to the valid prefix and causal current chunk. Padding must not become visible to attention or overwrite live cache entries.
- Fill the required K/V for every intermediate chunk. Select generation logits from the final logical token, not a padded row; preserve all logical outputs when the layer or readiness caller requests them. The generator owns orchestration; the decoder exposes the cache/position inputs it needs.
- Exercise increasing lengths on the same loaded instance, including just below/at/above a chunk boundary and multiple chunks plus a tail, then reuse a short request. Compare with HF and a feasible unchunked control, including follow-on decode that consumes the filled cache. Distinguish fresh requests, which reset request state, from explicit prefix continuation. Change tokens and page ownership to expose stale state; check each supported layer kind and sliding-window boundary.
- Test explicit continuation at the starts a vLLM prefix-cache hit, a chunk continuation, and a preemption resume produce: the smallest block, the least common multiple of the block sizes, a tile-unaligned start, the sliding window and one past it, and the last token. At the layer level compare the layer's contribution (output minus residual input) per row over a window of resumed rows against the full-prefill run; whole-output PCC on one layer hides attention errors behind the residual passthrough (a no-history control still scored 0.9997 on it). Add a negative control that drops the prefix history or zeroes the sliding-window snapshot and confirm it fails the same per-row gate; a control that still agrees means the test cannot see a broken resume. The full-model stage reuses this path for `prefill_logits_continuation`, and the serving stages depend on it for prefix caching; see [prefill features](../vllm-integration/references/prefill-features.md).
  Mismatches at unaligned starts have come from the rotary op's per-core-group compute config, SDPA chunk partition by start alignment, and CCL accumulation order by tile-row phase: [prefill and KV](../ttnn-knowledge/topics/prefill-kv.md).
- Let one prefill call carry several rows with different lengths and starts. Keep per-row cache writes, positions, and last-token selection independent, and plan for running the dense work once across rows; the per-row full-decoder loop is a functional baseline, not the optimized target.

Keep later tracing straightforward: separate host chunk planning from device execution, and follow `$tt-enable-tracing` for stable buffers and input refresh. Prepare/compile all supported physical shapes and op paths before recording the first trace; the first chunk, continuation chunks, and tails may need different traces or an eager path, whose persistent setup must also precede capture. Tensor-valued offsets only permit reuse when the op supports them and the remaining program signature is unchanged. Refresh positions, valid lengths and page tables before replay, and restore cache state after warmup/capture. A new logical length within a prepared physical bucket must reuse its trace, not trigger recapture. A passing fixed-input replay does not prove increasing-length requests work.

If after working on a chunked prefill implementation you have strong evidence that it would still produce a more performant model whilst still remaining trace-safe when interleaving prefill and decode with a variety of sequence lengths, then present that evidence clearly before proceeding with the alternative path.

## Evidence To Leave

Use `PCC >= 0.995` as the default acceptance bar for prefill and decode unless the model gives a concrete reason to choose a different bar. Done means all of these are true and recorded:

- HF-vs-TTNN prefill and decode PCC and performance for each meaningful layer kind.
- Prefill and decode on-device performance measured from warmed runs (using traced execution for decode) - these should have perf report outputs to prove it.
- Paged KV-cache behavior, including page table and current-position handling.
- A short capability-contract evidence table: claim, evidence, and remaining risk for the advertised context/sequence length, cache behavior, layer kinds, and mode switches.
- Full advertised prefill/decode sequence length, or the largest feasible value when a hard physical device limit prevents the advertised value from fitting or running.
- Sequence-length coverage for valid non-aligned logical lengths: at least one small smoke length, one length exactly at a relevant tile/page/chunk boundary, one just across a boundary, and one long non-divisible length near the supported context. Use a reduced layer-level harness if the long test would otherwise be too slow, but do not treat aligned-only coverage as complete.
- `models/autoports/<model>/doc/context_contract.json` records the HF-advertised context, the current supported context, the largest context tested in prefill and decode, and any hard-physical-limit reduction with byte/probe evidence.
- Determinism for repeated identical inputs tested.
- Evidence there are no torch or host calls required within a single prefill or decode pass - everything runs and stays on device.
- Watcher-clean run with `TT_METAL_WATCHER=10`. If the command cannot run in the environment, record the exact environment failure and replacement evidence; do not skip asserts or weaken the test.


# Decoder Bringup Knowledge

The knowledge for this stage lives in [`ttnn-knowledge`](../ttnn-knowledge/SKILL.md); each step below says which topic to read when.

## Repo Paths Worth Reading

Before designing a module, read the working implementations listed in [reference code](../ttnn-knowledge/topics/reference-code.md) that match the model's attention, MLP or MoE, norms and RoPE.

## TTNN Correctness Defaults And HF Reference

Before writing the layer and its tests, read [correctness](../ttnn-knowledge/topics/correctness.md): TTNN defaults while proving semantics, layer-only HF references, real-weight statistics and synthetic weights. You must include a test with real weights and show it passes PCC >= 0.995 for each of the layer kinds.

## Paged KV Cache Checks

Use paged prefill and paged decode as the final path. Before writing the cache tests, read the paged KV basics and the failure mechanisms in [prefill and KV](../ttnn-knowledge/topics/prefill-kv.md): fill and update dtype contracts, tensor current positions, page-table permutations, read windows past the row's end, and the decode query grid.

## Prefill And Decode Shapes

- Prefill commonly uses `(1, batch, seq_len, hidden)` style tensor shapes; decode commonly uses `(1, seq_len=1, batch, hidden)`.
- Use batch 1 as the first smoke/performance target, but do not hard-code batch 1 into masks, page tables, current positions, cache indexing, residual shapes, or output handling.
- After batch-1 PCC passes, add at least one batch >1 prefill/decode correctness test. Test up to the largest batch in `batch_sizes` (default 32) when the target hardware, memory, and harness allow it. If batch 32 cannot run, record the largest tested batch and the hard limit.
- The target sequence/context length is the full value advertised by the HF config, usually `max_position_embeddings` or the model's equivalent field. Do not create a smaller model config. Do not hide a smaller implementation behind a smaller `max_model_len`.
- Do not reduce the advertised model capability to make bringup, tests, profiling, or serving easier. A reduction is acceptable only when a hard physical device limit prevents the advertised capability from fitting or running, such as device DRAM capacity for weights + KV/cache/state + required persistent buffers. If reduced, record the byte calculation or failed capacity probe, the largest feasible supported value, and the exact construction/serving setting that uses it.
- Decode tests must include the full supported context length unless measured KV-cache DRAM capacity forces a reduction. If reduced, record the attempted full-length or capacity-probe command, log, failure signature or byte calculation, and largest feasible tested context.
- Prefill tests must include the full supported sequence length unless measured L1/DRAM capacity forces a reduction. If reduced, record the attempted full-length or capacity-probe command, log, failure signature or byte calculation, and largest feasible tested sequence.
- Prefill and decode tests must also include valid logical lengths that are not divisible by the implementation's preferred chunk, tile, block, page, or trace length. A TTNN kernel may require padded physical shapes internally; the decoder API should own that padding and masking rather than rejecting the logical length.
- Do not accept "tractability", profiling/watcher cost, runtime, or small synthetic proof scope as sequence-capacity evidence.
- Sliding-window models should include lengths around window boundaries, not only tiny smoke shapes.
- If a model has both full and sliding attention but identical decoder computation except mask/window configuration, use one parameterized implementation and tests for both modes.

## Trace And Profiling

- The final decode pass must use traced TTNN execution. If trace capture, replay, stale inputs, event synchronization, or trace-safe input setup becomes tricky, use `$tt-enable-tracing` before accepting an eager-only decode path.
- Decode PCC should be measured from replay output, not only from an uncaptured forward pass.
- Tensor allocation after trace capture is a red flag. Before accepting the traced decoder, run representative repeated replay with `TT_METAL_TRACE_ALLOC_TRACKING=1`, cover every captured control-flow path, and resolve every unsafe allocation under the target tt-metal checkout's `tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`. Common causes: [tracing](../ttnn-knowledge/topics/tracing.md).
- Measure warmed prefill and decode separately, with signposts, and report them with `tt-perf-report`; how, and the watcher run that is part of done, are in [profiling](../ttnn-knowledge/topics/profiling.md).

## Fallback And Watcher Audit

- Runtime prefill/decode should not call torch, `ttnn.from_torch`, or `ttnn.to_torch` except at explicit boundaries.
- Setup-time conversion inside `from_state_dict`, input construction, final PCC comparison, and a named temporary prefill-to-decode boundary are allowed in the test harness.
- Host fallback hidden in helpers is still fallback; inspect wrappers as well as the layer file.
- `models/tt_transformers/tt/model.py` contains host-side last-token extraction patterns for batched prefill. Treat that as an existing production compromise, not as a pattern to copy silently.
- Run watcher-enabled tests. A clean watcher run is part of done; a suspected false positive needs evidence.
- Run TT hardware-facing commands one at a time: resets, `tt-smi`, import/open-device probes, Tracy/profile runs, and tests that touch the device.
- If wheel-installed `ttnn` hits SFPI/JIT mismatches, prefer a source-built tt-metal environment whose headers and compiler match.

## Code Quality Defaults

- Prefer explicit model contracts over permissive fallback logic.
- Do not silently infer or patch invalid config values in internal paths; fail directly.
- Do not add fake generality for one supported HF config shape.
- Keep debug-only fallback, logging, and compatibility shims out of final bringup code unless they are explicitly justified.

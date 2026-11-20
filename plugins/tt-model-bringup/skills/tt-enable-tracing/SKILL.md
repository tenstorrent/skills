---
name: tt-enable-tracing
description: "Enable or debug TTNN trace capture and replay for models, decoders, generators, and multi-chip TTNN code. Use when eliminating TTNN dispatch overhead, implementing traced prefill/decode, making full-model generation trace-safe, or investigating trace failures such as unsupported writes, reads, event synchronization, stale inputs, or bad replay correctness."
---

# TT Enable Tracing

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

Use this skill when adding TTNN trace capture/replay or when a traced path fails. It states what a traced path
must satisfy and what evidence it leaves. Read [tracing](../ttnn-knowledge/topics/tracing.md) before
writing capture code: how capture works, a trace-safe structure, program-cache signatures, the generator split,
what stays outside capture, multi-chip traces, debugging and a symptom table. Before accepting a capture/replay
path or exempting shared buffers, read `tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`
in the target tt-metal checkout ([upstream](https://github.com/tenstorrent/tt-metal/blob/main/tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md)).

The readiness harness's teacher-forcing decode always uses traced decode. An eager teacher-forcing pass is not
evidence for optimized full-model or datatype-sweep performance; fix capture/replay first.

## Program-Cache Warmup

Every op in the traced region is compiled with its exact capture signature before the first capture
([program-cache signatures](../ttnn-knowledge/topics/tracing.md#program-cache-signatures)). Warmup covers the
whole generator, not only the trace about to be captured, because compilation elsewhere can leave persistent
allocations a live trace overwrites. Two phases:

1. **Prepare all supported variants without capture.** Compile the physical prefill/chunk buckets, decode
   variants, sampling modes and applicable post-processing or vision paths; allocate their persistent inputs,
   outputs where supported, CCL resources and helper state. Include eager paths that run between replays.
   Deduplicate by actual program signature, not by each request's logical length tuple.
2. **Record from prepared state.** Then record traces without interleaving new compilation or unrelated
   persistent allocations. A prepared variant may defer its first capture to save trace-region space; that is not
   recapture.

The generator owns this ordering for standalone and serving entrypoints, during explicit setup or coordinated
first use; do not depend solely on vLLM warmup callbacks. Repeated setup reuses prepared state. Restore request,
KV and sampling RNG state that warmup modified; a late decode compile with mock inputs must not overwrite a real
request's prefilled cache. Under vLLM, warm over the served cache allocation, not a test-sized pool. If warmup
mutates persistent trace inputs, reset them to the intended capture state just before capture. An unexpected
capture-time miss is diagnosed and its signature stabilized; repeated production warmup or recapture is not the
fix.

## Trace Lifetime And Reuse

During normal serving, each captured trace lives as long as the configured model and device. Supported requests
do not invalidate, retire, evict or recapture traces. If request handling needs invalidation for correctness,
that is a preparation, trace-selection or buffer-lifetime defect to fix before accepting the serving path.

Capture at most once per distinct execution signature: the captured program, physical layout and stable
buffer/resource bindings, not a request ID or cache-key label. Separate prepared buffer slots can need separate
traces. A different signature selects a different trace; it does not invalidate existing ones. Changes to tokens,
positions, logical prompt lengths, page mappings or scheduler slots refresh persistent inputs or select a prepared
variant. Bound the variants with supported physical buckets and runtime tensor inputs; do not impose new
logical-length restrictions to make warmup finite. A deferred first capture of a prepared variant preserves every
existing trace.

Switching between supported modes selects the matching cached trace: greedy, then sampled, then greedy reuses the
first greedy trace. A new logical prefill length does not retire decode or sampling traces to make room for late
persistent allocations; prepare those allocations before capture.

Captured backing storage and execution resources stay valid throughout serving; replacing them during request
handling is not a reason to rebuild traces. Never replay an unsafe trace to meet the reuse requirement; keep the
allocation checks on and the gate failing until the cause is fixed. Measured recapture latency does not turn a
correctness workaround into an accepted design.

Explicit unload or reconfiguration, server teardown or device recovery ends the affected traces' lifetime: stop
using them before releasing their resources, and prepare and capture again before resuming. These transitions are
not a request-time fallback. A deliberately bounded trace cache is a memory/latency tradeoff, not a correctness fix
or an exception to this requirement; if the target service needs one, raise it with the owner instead of weakening
the gate.

Exact prompt lengths, chunk start positions, batch composition and row order can change without changing the
captured program, and a changed request key does not trigger invalidation. Chunked prefill needs explicit coverage:
first chunks, continuation chunks at different starts, partial final chunks, prefix-cache hits resuming at the
cached length, multi-row prefill buckets with changed row counts and segment lengths, and new combinations and
orders of known lengths while other requests decode. None may retire prefill, decode or sampling traces, even when
prefill runs eagerly. Run the [mixed-shape serving check](../vllm-integration/SKILL.md#mixed-shape-trace-reuse-check)
on the same server. Removing retirement alone is not a safe fix; keep the allocation checks.

## Decode Loop And Token Feedback

Do not trace the high-level generator method unless it is proven trace-safe; split it as in
[generator split](../ttnn-knowledge/topics/tracing.md#generator-split). Host writes, reads, synchronization and
mode decisions stay outside capture
([list](../ttnn-knowledge/topics/tracing.md#what-stays-outside-capture)).

Page-table and position tensors are trace inputs: bind persistent device tensors before capture and replay over
them. Token feedback stays in the traced device path; do not read a sampled token to host and rebuild the next
input. The steady-state step is trace replay plus the minimum caller-visible readback; tokens, current positions,
RoPE indices, masks and page tables are not rebuilt on the host every token. Host refresh belongs at request
boundaries, explicit reset or real scheduler-owned input changes; per-token host refresh is an incomplete tracing
implementation. A traced loop slower than the decoder-stack lower bound is fixed before kernels are retuned.

## Canonical Split Sampling

After preparing all model and sampling variants, token-out traced decode uses two cooperating traces:

1. The model decode trace, up to sampler-ready logits.
2. The chosen common sampling implementation (or a correct generator-owned trace wrapper around it) for the active
   mode. Compare `models/common/sampling/` and `models/common/modules/sampling/sampling_1d.py` against the
   model's state, seed, topology, trace and logprob requirements before choosing.
3. The sampler is called with `tt_out_tok=<persistent decode token input tensor>`, so the sampled token lands in
   the tensor the next decode replay reads.
4. With a fixed-step decode loop, current-position/RoPE state advances on device inside the trace; a completed
   trace uses no host-originated position refresh per token.
5. Page-table inputs refresh only when the page table changes; test unchanged and changed tables. The unchanged
   case does no per-token page-table copies after setup.
6. Greedy decode keeps the sampled token on device; benchmark the available on-device greedy strategies on the
   target mesh. Force-argmax is a candidate, not the default.
7. Read the chosen sampler's trace lifetime before counting replays: a sampler that releases its traces when its
   force-argmax state flips breaks the mode-switch rule, and explicit per-request seeds can take an eager path.
   Count traced sampling steps from the sampler's own traced condition, not the generator's intent.

The reference is `models/tt_transformers/tt/generator.py`: capture decode once, bind the model to the persistent
inputs replay refreshes, trace the sampler path, pass `tt_out_tok` pointing at the decode token input. Untraced
sampling hidden inside the model trace is not the canonical path.

Split greedy is semantically greedy. A slower generic `top_k=32` or top-p path does not prove split greedy is slow.
If `top_k=1` split sampling fails on a non-tile inner dimension, fix the sampler-ready shape (preserve or pad the
top-k representation), or keep a minimal repro and leave the stage incomplete. For vocab-sharded logits use local
top-k candidates and greedy params rather than a physical per-shard `top_k=1`
([LM head and sampling](../ttnn-knowledge/topics/perf-advice.md#lm-head-and-sampling)). If `ArgMaxDeviceOperation`,
full-vocab all-gather, generic `TopKDeviceOperation` or another sampling op dominates greedy token-out decode, the
LM-head/sampling boundary is still wrong; fix it before lower-level decoder optimizations or marking tracing done.

For vLLM decode serving, mirror the production split: bind persistent token, current-position, RoPE, page-table and
KV-cache tensors before capture, warm the same mode, capture a device-only decode, and replay with
`ttnn.execute_trace(..., blocking=False)` only when the caller implements the async read/host-processing split.
If the sampler consumes transformed logits, capture the model output in that sampler-ready form so replay returns
the same device tensor. Reuse the full-model stage's common sampling path; no host argmax or full-logits readback
in a `sample_on_device_mode=all` path.

Do not collect Tracy, `tt-perf-report` or `TT_METAL_DEVICE_PROFILER` metrics from a live vLLM server or serving
adapter. vLLM-stage tracing evidence is functional and serving-level: capture/replay succeeds, stale-input tests
pass, on-device sampling is wired, async split behaviour is correct, qualitative and sampling checks pass, and
`run_vllm_server` benchmark JSON records TTFT/ITL/throughput. Earlier non-serving profiles give device context.

Async readback is separate from scheduler overlap. If the caller builds token IDs, current positions or request
lengths from host scheduler tables, the input refresh for step N+1 waits for sampled token N to be applied, unless
a separate test proves the next token/position path is entirely device-owned and cannot be overwritten by stale
host state.

## Trace Allocation Safety Gate

Before accepting a new or changed traced path, run its representative repeated-replay test in a fresh process
with tracking enabled before TTNN is imported, for example from the target checkout:

```bash
TT_METAL_TRACE_ALLOC_TRACKING=1 TT_METAL_TRACE_ALLOC_SKIP_PROGRAM_CACHE=0 python your_replay_test.py
```

For serving, set the environment in the server and worker processes before startup, not only in the client. End
every tracked workload with a decode replay (or read `num_program_cache_entries()` after each prefill), because
the tracker checks only before a replay ([tracker mechanics](../ttnn-knowledge/topics/tracing.md#allocation-tracker-mechanics)).
Exercise every trace variant and the real replay order, including alternating traces that share persistent inputs
or outputs. Program-cache allocations stay in the acceptance check: `TT_METAL_TRACE_ALLOC_SKIP_PROGRAM_CACHE=1` is
a diagnostic filter, and a run that needs it is not passing evidence. Run tracked tests with `pytest --timeout 1800`
or higher. Keep the positive control's output at `doc/<stage>/trace_alloc_tracking/positive_control.log`.

Fix late compilation and unintended persistent allocations first. For deliberately shared buffers that remain,
use `trace_allocation_tracker.acknowledge_corruptible(tensor)`, only for exact tensors whose lifetimes have been
reviewed. For a known output created during capture, identify it after capture and acknowledge it before replay,
rather than exempting the capture window:

```python
from ttnn.tools import trace_allocation_tracker

# The exact output identified after capture, with its lifetime verified below.
trace_allocation_tracker.acknowledge_corruptible(trace_output)
```

Acknowledgment does not prevent corruption; it removes the backing allocation from validation. Acknowledging a
view exempts the shared backing buffer, so identify its other aliases and consumers too. Prove the lifetime against
every live trace in their actual replay order: refresh an input after any replay that can clobber it and before its
next read; consume or preserve an output before another trace can overwrite it. Trace B overwriting its own output
does not prove that output survives an intervening replay of trace A.

Use `trace_allocation_tracker.corruptible_allocation_scope` only when exact-tensor acknowledgment is impossible,
around the narrowest set of required allocation calls, with unrelated work moved out. Never wrap a whole capture,
forward pass or warmup: the scope exempts every allocation inside it, including implicit program-cache and helper
allocations, against all active traces. Identify those allocations, verify each lifetime as above, and explain why
exact acknowledgment cannot be used. If you cannot isolate and justify them, fix the allocation path or report the
blocker instead of widening the scope.

Trace IDs are scoped by the active sub-device manager: capture, end, execute and release each trace with the
intended manager active, and cover manager changes in the replay test when the implementation uses them. A clean
tracker run does not replace warmup, cache-key, updated-input, output-correctness or token-feedback checks. Collect
performance numbers separately with tracking and diagnostics off, recording the settings of each run. Causes seen
in earlier bringups: [tracing](../ttnn-knowledge/topics/tracing.md).

## Required Replay Tests

- Before accepting any reduced input-refresh scheme: a replay test of two decode steps with different token and
  current-position values that inspects the exact persistent trace inputs and asserts the outputs changed. If
  page-table refresh is skipped, cover unchanged and changed page tables.
- Before accepting token-out decode: a feedback test proving the token sampled by replay N is the input of replay
  N+1. Teacher forcing can pass while feedback is stale or host-reconstructed.
- Benchmarks record whether `ttnn.execute_trace` is blocking. Blocking replay can be a correctness probe; a
  production generator or vLLM async path uses nonblocking replay with a read/output-processing split when the
  caller can consume it.

Debugging steps and the symptom table are in [tracing](../ttnn-knowledge/topics/tracing.md#debugging-trace-failures).
When output matches a listed symptom, run its focused experiment before any precision or model-quality theory. If
still stuck after isolating the failing block, use `$autofix`; it verifies or refutes each proposed root cause
with focused experiments before keeping a fix.

## Evidence To Leave

Leave compact evidence that the traced path is real:

- Correctness before and after tracing against the same reference.
- Repeated replay determinism across several executions.
- The exact tracking-enabled invocation and environment, trace variants and replay ordering covered, and a clean allocation-check result with program-cache allocations included. The tracker is silent when clean, so include one positive control in the same environment (a deliberately live device buffer at replay) and show that it raised; a clean run without that control does not prove the gate was active. List each acknowledged backing allocation, its aliases/consumers, and its lifetime invariant against every live trace. For each allocation scope, explain why exact-tensor acknowledgment is not possible and identify the allocation calls it encloses.
- Warmup coverage and ordering: supported physical buckets/modes and persistent setup completed before the first capture, plus restoration of request/KV and RNG state.
- Cross-request reuse evidence on the same generator/server: interleave prefill and decode, change logical lengths within prepared buckets, resume prefix-cached requests at nonzero start positions, submit multi-row prefill bursts, and alternate supported sampling modes before returning to an earlier signature. Map cache keys to actual execution signatures and trace IDs. Show at most one capture per signature and zero invalidations, retirements, or evictions during the workload. Include traces captured before the workload. Do not reset counters or restart the generator to hide recapture. Separate teardown releases from request handling. Include any deferred first-capture cost in request latency.
- Updated-input replay test proving outputs change when trace inputs are refreshed.
- For vLLM decode: stale-input validation for token/current-position/page-table refresh, explicit async-overlap setting and proof if enabled, on-device sampling trace evidence, and a passing server smoke run with decode trace enabled.
- Split-sampling evidence for token-out decode: internal sampling trace enabled, `tt_out_tok` wired to the persistent decode token input, and greedy benchmarks using the fastest correct on-device sampling strategy measured for this mesh.
- No host fallback in the captured path.
- Warmed trace replay timing, with prefill and decode measured separately where applicable.
- Host-work counters for the replay loop: trace replay count, token refresh count, current-position/RoPE refresh count, page-table refresh count, synchronizations/readbacks, and whether positions/tokens are advanced on device.
- `tt-perf-report` or Tracy evidence for the traced region, except vLLM serving stages where profiler collection is intentionally skipped and replaced by serving benchmark plus trace-contract evidence.
- Clear note of any remaining untraced boundary outside token-out decode. Token-out decode has no host sampling or full-logits readback.

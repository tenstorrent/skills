---
name: vllm-integration
description: Add vLLM serving integration for an existing TTNN full model under the model-specific autoport directory. Use after full-model when producing tt/generator_vllm.py, registering the model with the TT vLLM plugin, running the vLLM readiness server path, and reporting serving-path correctness and performance.
---

# vLLM Integration

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.

## Mission Context

As part of `$model-bringup`, follow its mission, workspace and reporting contract. This stage makes a working TTNN
full model and generator usable through the shared vLLM serving path. The full model belongs to `$full-model`;
this stage owns the adapter, plugin registration, serving checks and serving-path performance evidence. Plugin,
sampler and eval-harness behaviour seen in earlier bringups: [serving and evals](../ttnn-knowledge/topics/serving-evals.md).

## Your Part

Starting from `models/autoports/<model>/`, add `tt/generator_vllm.py`. `tt/model.py` and `tt/generator.py` already
exist and pass generator-level readiness; if not, use `$full-model` first. Small, evidence-backed changes to
`model.py` or `generator.py` are in scope when the adapter exposes a real contract gap, but this stage does not
become full-model bringup again. Fix a serving gap the model owns in the model or generator, even when a plugin
change would be a smaller diff. Changes to the TT vLLM plugin or to vLLM follow
[plugin changes](references/plugin-changes.md).

- **Precision.** Before writing adapter code, load the datatype-sweep selection and confirm the generator
  constructs that exact policy: weight groups, activation/residual dtype, CCL dtype, KV-cache dtype, compute
  fidelities and layer exceptions. Serving a reduced-speed model does not satisfy completion.
- **Context.** Load `models/autoports/<model>/doc/context_contract.json` and serve the recorded supported context
  (default: the HF-advertised context). Do not lower `--max-model-len`, model config context, benchmark context, API
  context or any other advertised capability to work around a model bug. A smaller value is valid only when the
  contract records evidence that a hard physical device limit prevents the advertised value and that the selected
  value is the largest feasible.
- **Prompt lengths.** Any valid prompt length up to the supported context works. The adapter does not require a
  multiple of an internal prefill chunk, tile, block, page or trace size; when the model pads or chunks internally,
  the adapter passes the logical length through to masks, positions, cache fill and output slicing. Include a direct
  OpenAI-compatible request or targeted runner check with a non-aligned length, not only the default 128-token
  benchmark.
- **Batches.** The optimization target is the target workload in [`agentic-serving`](../agentic-serving/SKILL.md);
  the primary single-user benchmark is the README headline and a regression row. The adapter does not assume
  `max_num_seqs=1` or batch 1 in cache allocation, page tables, scheduler inputs, sampling, async decode or output
  formatting. Validate and record one serving configuration per batch in `batch_sizes`
  (`bringup/agentic_evals/inputs.json`, default 1, 8, 16, 32); tune batch 1 and the largest, the others should
  work. Record any batch the device cannot hold, with the hard limit: later stages choose among validated batches,
  so an unrecorded drop silently changes which batch a run can use.
- **Decode buckets.** Declare the prepared decode buckets in the adapter's `tt_supported_decode_batch_sizes` and
  document the key in the serving contract; without it the plugin pads every decode step to `max_num_seqs`.

## Serving dependency

Follow [serving stack setup](references/serving-stack.md): the standalone TT plugin and the upstream vLLM version
its current instructions recommend, reusing the pair `$full-model` recorded for its serving contract check. Keep the
pair and its provenance in the run record.

Before writing adapter code, read the pinned plugin's model interface guide (currently `docs/MODEL_INTERFACE.md`)
and the documents it links, and load `doc/full_model/serving_contract.json` (if missing, write it first as
`$full-model` describes; the gate requires it). Declare only `model_capabilities` keys the pinned plugin documents,
each value only with evidence that the model meets it.

## vLLM Adapter

`tt/generator_vllm.py` delegates to the generator's low-level methods; adapter-only code is vLLM interface
translation and does not duplicate logic in `tt/model.py` or `tt/generator.py`. Read
`tech_reports/LLMs/vLLM_integration.md`, `models/tt_transformers/tt/generator_vllm.py`, and the model's
`prefill_forward` and `decode_forward`.

**Cache ownership.** Keep the generator's two modes: standalone readiness, where the generator allocates and resets
the cache; and vLLM, where `allocate_vllm_kv_cache` creates the cache and the adapter passes that exact cache through
the low-level API. vLLM owns the attention KV cache; constant-size recurrent or linear-attention state (conv
windows, SSM or gated-delta state) may live in the model across decode steps instead of as a vLLM cache. Route
sliding-window and full-attention layers through vLLM's hybrid attention infrastructure (per-layer KV-cache specs
and block tables), not one uniform attention type.

**Traced decode.** Prompt lengths, page tables, decode positions, batch dimensions, trace-side state and on-device
sampling are explicit inputs. Serving decode drives the generator's traced decode path, not an eager fallback; use
`$tt-enable-tracing` for capture, replay, trace-safe inputs and replay correctness. Before accepting the adapter,
run representative repeated serving replay with `TT_METAL_TRACE_ALLOC_TRACKING=1` set in the server/worker
environment before TTNN is imported, alternating the trace keys and request patterns that share persistent inputs
and outputs, and resolve every unsafe allocation per the checkout's
`tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`.

**Warmup.** Use the generator-owned two-phase warmup and trace lifetime rule from `$tt-enable-tracing`; vLLM
callbacks are not the only place warmup is enforced. In vLLM mode warmup and capture run over the served cache
allocation, not a generator-owned or test-sized pool, because paged-attention program keys include the cache shape
([tracing](../ttnn-knowledge/topics/tracing.md)). Run the [mixed-shape trace reuse check](#mixed-shape-trace-reuse-check)
on the same server.

**Sampling.** Traced serving decode reuses the full-model generator's canonical split-sampling path and replays with
`ttnn.execute_trace(..., blocking=False)`. For `sample_on_device_mode=all` there is no new sampling strategy, host
greedy/top-1 argmax, full-logits readback, generic top-k fallback for greedy, or Python token readback/writeback
loop. If the generator lacks split sampling, fix `$full-model`; do not patch sampling in the adapter. Refresh token,
current-position and page-table inputs only on real scheduler state changes (no full page-table copy per token when
unchanged), and prove the changed and unchanged cases with stale-input tests.

### Async decode

Implement the async split before advertising it: `decode_forward(..., read_from_device=False)` returns device
tensors, `read_decode_output(..., async_read=True)` does the minimal deferred read, and
`process_decode_output_host(...)` formats on the host. Set `supports_async_decode=True` only after this path meets
the plugin's expectations with decode trace enabled and stale-token/current-position tests pass. That declaration
is the only gate on overlap: do not add a plugin capability, a plugin-read environment variable or an
`--additional-config` option for it. Overlap without proof can silently corrupt generation.

- vLLM may submit step N+1 before sampled token N reaches host scheduler state. Reuse the device token and position
  state for steady decode and advance it exactly once per emitted token, including across transitions between the
  model's traces. Adapter state derived from position (page-table redirects, position tables) comes from
  device-owned or generator-mirrored positions; the plugin's `start_pos` argument is advisory on steady steps.
- The plugin overlaps decode only under vLLM `--async-scheduling`, which the packaged runner does not pass: add it
  through `--additional-server-args` and record it. Prove the overlapped path with a served greedy-determinism
  check: the same prompt repeated under concurrent load returns identical tokens every run.
- `reset_batch=False` does not prove the page table is unchanged. Read the pinned plugin's state updates and
  decode-reload contract; do not keep a workaround only because an old fork needed it.
- Before enabling overlap, verify the pinned vLLM revision refreshes device page tables when active requests gain
  blocks. A reset that reloads host tokens or positions first drains pending decode results; a page-only refresh
  needs evidence that it preserves token and position state; empty new-block lists do not force a refresh every
  token. When the adapter misses the plugin's documented decode-reload contract, fix the adapter; when the plugin
  violates its own contract, fix it only with a complete [plugin changes](references/plugin-changes.md) record.
  Until then keep overlap disabled and do not report the async path as complete.
- Validate allocator-driven page growth across several page boundaries with several distinct concurrent requests
  and a trustworthy non-overlapped control, covering request reordering, pending tokens and unchanged-table steady
  decode. Forcing `reset_batch=True`, changing only an unused page or checking only for degenerate text does not
  establish page-table correctness.

## Prefix Caching

Prefix caching is required unless one of the two documented exclusions in
[prefill features](references/prefill-features.md) applies: `block_output`, or `recurrent_state` for models whose
recurrent or linear-attention state cannot be rebuilt from KV blocks. An excluded model records the exclusion in its
serving contract row and skips the resume check and the `prefix_cache` stage; a `recurrent_state` model implements
session continuation instead (same document). Otherwise declare `model_capabilities["supports_prefix_caching"] =
True` and serve with `--enable-prefix-caching` once the generator meets that document's continuation contract: a
cache hit arrives as `start_pos` equal to the cached length over the request's own page table, the same path chunked
prefill and preemption resume use. Prove it twice: the generator-level resume check (`run_prefill_resume_check`,
with its negative controls failing) and the served `prefix_cache` readiness stage (hit counters from `/metrics`,
warm/cold TTFT, first-token agreement, shared-prefix hit rate), with the sampling and qualitative stages run against
the same caching-enabled server.

Declaring `False` is accepted only for those two exclusions, named in the serving contract row. A pinned plugin that
vetoes or breaks caching for a model that meets the contract is a `plugin_owned` row, not a reason to declare
`False`. Do not add a capability key, a plugin-read environment variable or an `--additional-config` option to gate
it. Check reuse on multi-turn traffic as [`agentic-serving`](../agentic-serving/SKILL.md) describes.

## Batched Prefill

The plugin submits every admitted request in one `prefill_forward` call. Run the dense work once for all short rows
instead of looping the full decoder per row; [prefill features](references/prefill-features.md) gives the
equal-padded and packed-row designs, the warmup rule and the gates. Prove batched equals solo at the generator
level, then run the `prefill_burst` readiness stage with batched prefill off and on and record the TTFT ratio.
Stage 9 reports the burst ratio as advisory; Stage 10 requires it, with single-user latency unchanged.

## Minimum-Surface Bring-Up Loop

Do not debug vLLM by relaunching the complete all-layer model. First make the adapter work on the smallest
representative target: the same generator, `generator_vllm.py`, plugin registration, cache and page-table shapes,
terminal norm, LM head, sampling and trace behaviour, with one real layer of each layer kind. On it, make server
launch, trace capture and replay, the async split, vLLM cache ownership, on-device sampling, token/position/page-table
refresh and stale-input tests pass, then a multi-request smoke test after batch 1 works. The reduced target is an
inner-loop tool only, never reported as the model's accuracy or performance; final accuracy, qualitative, sampling
and benchmark evidence come from the all-layer model.

Run checks smallest first. Rerun a failing pytest, prompt, request or benchmark shape directly against a live
server instead of the whole suite after every edit; use `--sampling-profile smoke` or one targeted request first,
then the full profile once fixed.

## Short Request-Lifecycle Check

Extend the serving smoke run with a short sequence derived from the affected shape and memory paths. On the live
server: complete prefill and decode to warm traces and collectives, submit a previously unseen valid shape, then
repeat known shapes, including the boundary and tail cases of changed paths. A fresh-process single request cannot
show behaviour after decode allocations or across requests.

Record request completion and available device-memory and cache counters before and after. Repeats should
stabilize; per-shape growth needs an ownership and capacity explanation consistent with the advertised input range.
Without counters, inspect allocation and cache ownership and record the limit of the evidence; successful requests
alone do not prove stable memory use. Aim for minutes of extra requests inside the existing smoke run, not a
separate all-layer startup or soak; expand only for unexplained growth, errors or uncovered paths, using an isolated
op or layer repro. A time budget that ends before the required checks complete is incomplete evidence, not a pass;
this check does not replace the final all-layer serving evidence or the acceptance gates.

### Mixed-shape trace reuse check

Part of Stage 9 serving evidence; repeat it after Stage 10 changes the trace or scheduler path. Use the live server
and the request-lifecycle check above.

1. Complete normal generator-wide preparation first, with no separate warmup for each measured request signature.
2. Send unseen valid logical lengths within prepared physical buckets (for example 129, 130 and 131 tokens, which
   expose exact-length keys when the model supports them).
3. Exercise each supported chunked-prefill path with first chunks, changed continuation start positions and partial
   final chunks. Scheduler coverage needs concurrent arrivals and prompts that cross its token budget while other
   requests decode.
4. Repeat known lengths in different batch combinations and row orders, with supported sampling-mode switches before
   returning to an earlier trace key.
5. Record actual request lengths, chunk lengths, start positions and active decode work; a configured chunking flag
   does not prove the workload chunked.
6. Collect trace IDs, capture and retirement counts, and compilation/capture times for prefill, decode and
   sampling, mapping cache keys to execution signatures, with a baseline of traces captured before the workload.
   The generator owns capture, so add generator or model event instrumentation where HTTP results cannot show these
   events; plugin instrumentation follows [plugin changes](references/plugin-changes.md). Missing counters are
   incomplete evidence, not zero recaptures.
7. Report request completion, correctness, TTFT p50/p95/p99, decode pauses and completed-request throughput,
   including first encounters with the measured lengths and chunk offsets; this is separate from fixed-shape warmed
   performance.

Apply the [trace lifetime rule](../tt-enable-tracing/SKILL.md#trace-lifetime-and-reuse): at most one capture per
prepared execution signature and zero invalidations, retirements, evictions or recaptures through the workload,
keeping traces captured before it and deferred first captures during it. Do not restart the generator or reset
counters between requests; record shutdown releases separately. Apply the allocation-safety gate from
`$tt-enable-tracing` and measure performance separately with tracking off. Keep commands, workload, trace events and
results under `readiness_vllm/`, and check for stale inputs and request contamination as well as recapture. The
packaged runner does not yet automate this workload; use a targeted integration test. Fixed-shape benchmark results
or successful HTTP responses alone do not satisfy it.

## Plugin Registration

Register the model through the pinned plugin's documented extension mechanism
([serving stack setup](references/serving-stack.md)). Verify the resolved class is this experiment's adapter,
including in engine subprocesses; an existing built-in architecture must not silently select a reference model. Do
not add a built-in registration to the plugin source when a runtime mechanism can select the adapter
([plugin changes](references/plugin-changes.md)).

## vLLM Server Integration Test

```bash
python -m readiness_check.run_vllm_server \
  --model-dir models/autoports/<model_name> \
  --hf-model <hf-model-id-or-local-path> \
  --mesh-device <N150|N300|P300x2|T3K|TG> \
  --max-num-seqs <int> \
  --max-model-len <int> \
  --sampling-profile <full|smoke> \
  --tt-config '{"trace_region_size": <bytes>, "fabric_config": <fabric mode>}'
```

The runner owns server launch, health polling, check execution and shutdown. Under `<model_dir>/readiness_vllm/` it
writes `server.log`, `sampling_tests.log`, `vllm_qualitative_outputs.json`, primary single-user raw
`vllm_result.json`, primary normalized `vllm_benchmark.json`, `vllm_benchmark.log`, and by default the secondary CI
serving-burst `vllm_ci_serving_result.json`, `vllm_ci_serving_benchmark.json` and `vllm_ci_serving_benchmark.log`.
The gates read only that root; `--output-dir` sends an attached or tracked run elsewhere so it does not overwrite
them. `--additional-config` takes a `{"tt": ...}` JSON object (verify against the selected upstream CLI and plugin
config reader; it accepts `P300x2` for a Blackhole QuietBox 2, 1 x 4 chips). Do not patch vLLM to accept the
obsolete `--plugin-config` flag or wrap the runner to change its mesh choice; after changing the vLLM revision, check
its argument parser and TT config reader before serving. Use typed runner flags for `--max-model-len` and
`--tt-config`; `--additional-server-args` (split with `shlex`, so quote JSON inside it) is for uncommon flags.

`--stages` accepts `serve`, `sampling`, `qualitative`, `benchmark`, `prefix_cache` and `prefill_burst`; the default
is `serve,sampling,qualitative,benchmark`, and the last two are required for prefix-cache and batched-prefill
evidence. Pass `--enable-prefix-caching` on launch, or `--server-log` when attaching to a hand-launched server, so the
run summary records the prefix-caching state. Set `VLLM_SERVER_DEV_MODE=1` in the readiness server environment so
`/reset_prefix_cache` exists for the `prefix_cache` stage's cold references and long-resume comparison.

Hold a server open while iterating, then attach checks from another shell:

```bash
python -m readiness_check.run_vllm_server \
  --stages serve \
  --model-dir ... --hf-model ... --mesh-device ... [--max-model-len ...] [--tt-config ...]

python -m readiness_check.run_vllm_server \
  --stages sampling \
  --max-num-seqs <int> \
  --sampling-profile <full|smoke> \
  --server-url http://localhost:8000 \
  --model-dir ... --hf-model ...
```

The `sampling` stage, like `serve`, rejects a plugin source layout the runner does not accept (a bundled or nested
plugin checkout), even against an external server. On such a layout, run the plugin's sampling suite by hand with the
runner's exact pytest command, record the command in the work log, put the result file where the gate expects it,
and attach the other stages with `--server-url` and `--server-log`.

Check stages:

- `sampling`: the canonical TT plugin pytest suite against the live server; `full` runs all of it, `smoke` a small
  sanity subset for slow loops.
- `qualitative`: greedy and sampled completions for `${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/vllm_prompts.txt`;
  use `$qualitative-check` so prompts use the HF-declared format, compare with controls, and judge coherence, topic,
  repetition, gibberish and wrong-language drift.
- `benchmark`: the primary single-user profile (128-token input, 128-token output, one prompt, `--max-concurrency 1`,
  `ignore_eos`, percentile metrics `ttft,tpot,itl,e2el`, completed-prompt gate) in `vllm_benchmark.json`: the
  headline decode t/s/u and the single-user regression row. By default also the vLLM-nightly-shaped CI
  serving-burst profile (100/100, 32 prompts, no explicit `--max-concurrency`, `ignore_eos`, same metrics) in
  `vllm_ci_serving_benchmark.json`, for nightly parity, serving capacity and larger-batch coverage, never the
  headline (burst admission and chunked prefill affect TPOT). Both run greedy (`--temperature 0.0`);
  `--benchmark-use-server-generation-config` reproduces nightly/server-generation-config behaviour, and its numbers
  are labelled sampled/default-generation-config.
- `prefix_cache`: one ~2300-token prompt cold, warm, warm, a prefix-cache reset, repeat; prefix-cache counters from
  `/metrics`, TTFT, first-token logprob and greedy output, the shared-prefix `vllm bench serve` workload, and
  `prefix_cache_check.json` with the verdict defined in [prefill features](references/prefill-features.md). Every
  run also writes `vllm_run_summary.json`.
- `prefill_burst`: 32 x 128-token prompts at concurrency 32 so the scheduler forms multi-row prefill steps, written
  to `vllm_prefill_burst_benchmark.json`; `--prefill-burst-serial-result` passes the same workload measured with
  batched prefill off, for the TTFT ratios the gate reads.

The runner enforces on-device sampling (`sample_on_device_mode: all`); the full-model stage already provides traced
token-out split sampling, or work returns to `$full-model`. Shared tests that need host sampling get an explicit
compatibility mode whose metrics are labelled separately; the optimized serving path stays on device.

**Served sampling defaults.** The served default recipe stays on the device sampler. vLLM applies the checkpoint's
`generation_config.json` defaults to every request that does not override them; compare its `top_k`, `top_p` and
`temperature` with the declared `max_device_top_k` and the device sampler's real candidate width
([serving and evals](../ttnn-knowledge/topics/serving-evals.md#plugin-and-vllm)). If the default recipe would route to
the host sampler, either raise the device ceiling with a probe that proves the wider draw, or serve with
`--override-generation-config` and record it as an operator decision in the work log. During the sampling and burst
stages, scan sampled token ids for out-of-vocabulary values and count host-sampled steps by logging a per-step
sampling-mode line from the adapter during the run and summarizing the server log afterwards. A measured path that
silently fell back to host sampling is not the optimized path.

`--max-num-seqs` goes to both server launch and sampling pytest (`--tt-max-num-seqs`); leave it at 1 only for the
primary single-user benchmark or a focused debugging run. Final serving evidence covers every batch in
`batch_sizes` unless hardware or memory prevents it.

Final evidence uses `--sampling-profile full`; debug with `smoke` and targeted node ids first. If the full profile is
impractical for an MoE model, record the status as `smoke-gated`; that does not satisfy the full sampling gate unless
the project owner explicitly accepts that coverage.

### Sampling failures

- When determinism tests fail, check that the model's logits for a prompt are reproducible across runs and batch
  positions, standalone and through vLLM.
- Classify reproducibility-only failures separately when they are the only failures (typically
  `test_top1_is_greedy`, `test_topk`, `test_uniform_seed_deterministic`, `test_specific_seed_reproducible`,
  `test_same_seeds_reproduce_across_batches`, `test_*_mixed_batch`, `test_mixed_params_batch`). They need no more
  serving-correctness work only when correctness, logprobs, crash-free serving and qualitative output all pass.
  Correctness failures, missing or wrong logprobs, crashes and gibberish remain required work.
- A seeded request that changes sampler mid-request (a host-only parameter such as `top_k` above `max_device_top_k`
  joining its batch) is a routing defect: record it with the admission pattern as evidence, not as a model defect.
- When a plugin test fails because the device sampler lacks a request feature (logprobs, penalties, a top-k range),
  fix the generator sampling path that `$full-model` owns. A documented `model_capabilities` declaration that routes
  such requests to host sampling is acceptable only for a real support boundary of this model recorded in
  `doc/full_model/serving_contract.json`. Do not add a routing capability or weaken a plugin test. Warm the
  penalties path with one counted sampling pass before capture
  ([serving and evals](../ttnn-knowledge/topics/serving-evals.md#plugin-and-vllm)).

## No Tracy Or Perf-Report In vLLM Stages

Do not collect Tracy, `tt-perf-report` or `TT_METAL_DEVICE_PROFILER` metrics from vLLM integration or optimized-vLLM
serving runs: no profiler env vars on the live server, no `python -m tracy` around `run_vllm_server`, no
serving-adapter profile just for a profiler table, no `ttnn.ReadDeviceProfiler(mesh)` in stage closure. This pattern
has wedged boards ([tt-device-usage](../tt-device-usage/SKILL.md#profiler-runs-that-wedged-a-t3k)). vLLM evidence is
what `run_vllm_server` produces: sampling results, qualitative outputs, degenerate-output checks, server logs, the
primary single-user `vllm bench serve` JSON (TTFT, TPOT, ITL, aggregate output throughput) and the secondary CI
serving-burst JSON. For device-op context use full-model or reduced non-serving profiles from earlier stages; if
they are missing, record the gap, do not recreate them here.

Optimized-vLLM optimizes with same-harness before/after primary single-user and CI serving-burst metrics plus
contract checks: async split, nonblocking trace replay, stale-input coverage, on-device sampling, no host greedy
argmax, no full-logits readback, no unnecessary page-table/token/position refresh.

Compare decode serving changes on the exact same runner, prompt/output lengths, `max_num_seqs`, model length, mesh,
TT config and sampling mode. Report raw `median_ttft_ms`/`p99_ttft_ms`, `mean_tpot_ms`/`p99_tpot_ms`,
`median_itl_ms`/`p99_itl_ms`, `output_throughput` and TPOT-derived decode t/s/u (`1000 / mean_tpot_ms`) before and
after. Compare 128/128/1 only with 128/128/1 and 100/100/32 only with 100/100/32.

Keep teacher-forcing and serving performance separate: teacher forcing is a lower bound for the decoder and
generator; serving adds orchestration, sampling, token feedback, request handling and readback. If serving is much
slower, remove serving-specific overhead (fallback sampling, stale-input refreshes, per-token page-table copies,
blocking replay, synchronizations, readbacks, adapter-side reconstruction) before retuning the decoder.

## Failures And Recovery

- After a vLLM crash, stop this run's leftover `EngineCore` or `vllm.entrypoints` processes before retrying; they can
  hold chip locks after `tt-smi -r`. Never stop processes another job owns.
- If a profiler run was started by mistake and fails, do not escalate into repeated watcher, profiler or reset
  attempts: follow [tt-device-usage](../tt-device-usage/SKILL.md#profiler-runs-that-wedged-a-t3k) (stop profiling,
  keep logs, mark the evidence `hardware-profiler-limited`, run its recovery before treating the stage as stopped;
  request an operator reboot only after bounded recovery fails or needs authority this agent lacks).
- Transient CCL or fabric link errors right after a failed multi-device run get a device reset and one retry before
  they count as hardware evidence, while `tt-smi` stays responsive and the profiler pattern above is absent.
- Failed serving gates, missing artifacts, sampling or qualitative failures, and `$stage-review` `more-work-needed`
  findings are stage work, not stop reasons. Fix an obvious cause directly and rerun the failing gate. If the first
  fix does not close it, or the failure crosses the adapter, generator, cache ownership, scheduler inputs, trace
  inputs, sampling or registration, use `$autofix` (it runs `$autodebug` as needed and verifies or refutes each
  proposed bug); plugin fixes it proposes follow [plugin changes](references/plugin-changes.md). Terminal-stop only
  after `$autofix` has tried and failed, an external dependency is unavailable, or a required plugin change awaits
  human authorization with its proposal recorded.

Record the working server invocation in the work log: `--max-model-len`, `--tt-config`, workload config and env vars
that mattered.

## Output-Quality Verdicts Need A Control

Before calling a serving-output problem a model-quality limitation, produce matching prompt-correct evidence with
`$qualitative-check` from HF, or at least from the full-model stage on comparable prompts. Serving output materially
worse than that control is a serving regression in scope here; stale trace inputs, sampler state and cache/position
handling are the usual causes, not the checkpoint. If the shared runner cannot send the correct prompt format, fix it
or add a targeted prompt-correct request before judging.

After qualitative collection, run:

```bash
python "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
  --hf-model <hf-model-id> --missing-artifacts critical --scope vllm
```

Mechanical degeneracy (doubled tokens, single-token collapse) is never a model property; the runner-side gate runs
the same check. A tokenizer without a chat template means a base model: say so and judge outputs as continuations,
so chat-style prompts do not mask serving bugs with poor text.

## Evidence To Leave

Done means all of these are true and recorded:

- Full-model generator readiness baseline used before adding vLLM.
- Selected datatype policy loaded by serving, including KV-cache and CCL dtype.
- Adapter class, the low-level generator methods it delegates to, and the KV-cache ownership contract.
- Plugin registration path and architecture name.
- Exact successful `run_vllm_server` invocation.
- Served max context, matching `doc/context_contract.json`, with any hard-physical-limit reduction evidence.
- Non-aligned prompt-length evidence through serving: a valid length not divisible by internal chunk/page/block
  alignment succeeds without capping or truncating the advertised context.
- Served batch/concurrency coverage, including every validated batch in `batch_sizes` and any hard-physical-limit
  reduction evidence.
- Capability flags with evidence: no unproven `supports_async_decode=True`, on-device sampling verified for the
  measured mode, every declared `model_capabilities` key documented by the pinned plugin.
- Prefix caching: `supports_prefix_caching=True` with `readiness/prefill_resume_check.json` (controls failed),
  `readiness_vllm/prefix_cache_check.json` (status pass), `readiness_vllm/vllm_run_summary.json` showing the server
  enabled it, and sampling and qualitative verdicts from the caching-enabled server; or a serving-contract row naming
  a documented exclusion; or a `plugin_owned` row with its justification record when the pinned plugin blocks it.
- Batched prefill: batched-equals-solo generator evidence, the warmup bucket list with program-cache counts, and
  `readiness_vllm/vllm_prefill_burst_benchmark.json` with its `serial_reference` ratios.
- Plugin and vLLM checkout state: unchanged from the pinned commits, or a complete
  `doc/vllm_integration/plugin_changes.md` entry for each local change ([plugin changes](references/plugin-changes.md)).
- Serving uses the full-model split-sampling contract: internal sampling trace, `tt_out_tok` feedback into the
  persistent decode token input, greedy benchmarks on the fastest correct on-device sampling strategy measured for
  this mesh, and stale-token/current-position smoke coverage.
- Logit-determinism evidence through vLLM: run-to-run and cross-batch-position reproducibility, compared with the
  standalone baseline.
- Sampling test results, with reproducibility-only failures separated from real failures.
- `$qualitative-check` artifacts: greedy and sampled serving-output verdict, prompt-format metadata, rendered prompts
  or token ids, and matching HF/full-model controls.
- Primary single-user benchmark workload config, temperature/generation-config mode, and whether it used the default
  128/128/1 shape or explicit overrides.
- Primary serving-path TTFT median/P99, TPOT mean/P99, ITL median/P99, aggregate output throughput and TPOT-derived
  decode t/s/u from `vllm_benchmark.json`.
- Secondary CI serving-burst workload config and metrics from `vllm_ci_serving_benchmark.json` when comparing to
  vLLM nightly or serving capacity.
- Watcher or device-reset notes if relevant to serving stability.

## Preferred Outputs

```text
models/autoports/<model>/tt/generator_vllm.py
models/autoports/<model>/doc/vllm_integration/work_log.md
models/autoports/<model>/doc/vllm_integration/README.md
```

The README leads with vLLM sampling status, qualitative verdict, and primary single-user 128/128/1 TTFT median, TPOT
mean, ITL median, aggregate output throughput and TPOT-derived decode t/s/u. CI serving-burst 100/100/32 metrics go
in a secondary section. The workload config sits next to every number.

## Useful References

| Topic | Path |
|---|---|
| vLLM integration guide | `tech_reports/LLMs/vLLM_integration.md` |
| vLLM server integration runner | `${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/run_vllm_server.py` |
| vLLM qualitative prompts | `${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/vllm_prompts.txt` |
| Generator contract | `${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/contract.py` |
| tt_transformers generator | `models/tt_transformers/tt/generator.py` |
| Thin vLLM adapter | `models/tt_transformers/tt/generator_vllm.py` |
| vLLM model registration | Standalone plugin registration; see [stack setup](references/serving-stack.md) |
| Plugin model interface and capabilities | Pinned plugin checkout, currently `docs/MODEL_INTERFACE.md` and the documents it links |
| Plugin and vLLM changes | [plugin changes](references/plugin-changes.md) |
| Prefix caching and batched prefill | [prefill features](references/prefill-features.md) |
| Resumed-prefill check | `${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/run_prefill_resume_check.py` |
| Plugin, sampler and harness lessons | [serving and evals](../ttnn-knowledge/topics/serving-evals.md) |

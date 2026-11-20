---
name: full-model
description: Build a complete TTNN autoregressive model and generator from a HuggingFace model plus one or more working TTNN block or decoder implementations. Use when assembling embeddings, block stack, final norm, logits, KV-cache handling, and generation into a full model, validating end-to-end behavior, and reporting full-model accuracy plus generator-level performance. Repo-specific file contracts belong in a project orchestration skill such as model-bringup.
---

# Full Model

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


This skill starts from working TTNN block or decoder code and turns it into a complete autoregressive model that can run end-to-end on real weights. The output should be a model wrapper plus a generator appropriate to the project. If a project-specific orchestration skill defines filenames, class names, readiness runners, or artifact locations, follow that contract there; this skill describes the general engineering work.

The full model stage does not implement the vLLM adapter, but it must design the model and generator so a later serving adapter can drive the same low-level prefill/decode path without duplicating model logic. Treat the generator as a serving component, not only as a demo loop: external callers may own scheduling, cache/page-table state, prompt lengths, positions, sampling parameters, fixed request slots, inactive rows, and mixed-length prompts.

## What To Build

Implement the model-specific pieces around the working block stack:

- token embeddings;
- positional handling such as RoPE, ALiBi, or model-specific position state;
- block or decoder stack, including multiple layer kinds when present;
- KV-cache allocation, reset, page-table or position handling, and repeated decode reuse;
- final norm and LM head, including tied embeddings when the HF config uses them;
- logits and a canonical split-sampling token-out path using the common sampling implementation that best fits the model;
- full on-device traced decode from token in to sampled token out, implemented as model decode trace plus a traced sampling path.
- a generator that owns high-level token-in/token-out generation and exposes a clean low-level prefill/decode API for external callers.

Use the strongest correct implementation available as your block stack. If there are several candidates, choose the one with the best evidence for the target mesh and explain the choice.

## Capability And Context Contract

The full model must support the context length advertised by the HF config. Start from `models/autoports/<model>/doc/context_contract.json`, then recompute it for the full layer stack. Include all loaded weights, the full-layer KV cache, page tables, trace buffers, persistent CCL buffers, and other long-lived tensors that consume device DRAM. Byte arithmetic is a plan, not the measurement: after the model loads, record the device memory view (`ttnn.get_memory_view` or the runtime's equivalent) and derive the KV budget from the measured free DRAM.

Also preserve the rest of the advertised model capability contract established by earlier stages: cache/state semantics, layer kinds, mode switches, and the externally visible generation behavior. Do not reduce the advertised model capability to make bringup, tests, profiling, or serving easier. A reduction is acceptable only when a hard physical device limit prevents the advertised capability from fitting or running, such as device DRAM capacity for weights + KV/cache/state + required persistent buffers. If reduced, record the byte calculation or failed capacity probe, the largest feasible supported value, and the exact construction/serving setting that uses it.

Prompt length is a logical API input. The full model and generator must accept valid prompt lengths up to the supported context, including lengths that are not divisible by internal prefill chunk, tile, block, page, or trace sizes. If an op path needs aligned physical shapes, pad or chunk internally, mask padded tokens, and slice returned logits/output state back to the logical prompt length. Do not expose assertions such as `seq_len % chunk_size == 0` unless the HF model itself has that semantic restriction.

Batch handling is part of the capability contract. Optimize for batch 1 and the largest batch in `batch_sizes` (default 1, 8, 16, 32), the target workload in [`agentic-serving`](../agentic-serving/SKILL.md); the other batches should work too. Do not hard-code batch 1 into the model, generator, cache, page tables, position handling, sampling, or output formatting. Support larger batches through the same low-level API and test up to the largest batch in `batch_sizes` (default 1, 8, 16, 32) when the target hardware, memory, and harness allow it. If it cannot run, record the largest tested batch and the hard physical limit.

## Serving Contract Check

Find serving gaps before the generator, sampler, and datatype policy are optimized and frozen. Before you choose the sampler and fix the low-level generator API, resolve and pin the serving stack as [serving stack setup](../vllm-integration/references/serving-stack.md) describes. This check only reads the pinned plugin checkout. It does not install the stack or launch a server, and it is not vLLM integration work.

Read the pinned plugin's model interface guide (currently `docs/MODEL_INTERFACE.md`) and every document it links for setup, execution, lifecycle, capabilities, decode reload, and other extension contracts that the model could use. Inspect the pinned plugin source where those documents do not say which adapter calls or declarations the plugin uses. Read its device test suite (`tests/tt`). When the pinned plugin has no interface guide, read its model-runner and sampling source and `tests/tt` instead, and list every file read in `documents_read`; the gate checks that each `supported` row's evidence path exists under the model directory. Then write `models/autoports/<model>/doc/full_model/serving_contract.json`:

```json
{
  "plugin": {"repository": "tenstorrent/vllm-tt-plugin", "commit": "<40-character SHA>"},
  "vllm": {"version": "<recommended release or ref>"},
  "documents_read": ["docs/MODEL_INTERFACE.md", "docs/MODEL_SETUP.md"],
  "rows": [
    {"item": "logprobs", "kind": "request_feature", "status": "supported", "evidence": "<test or artifact path>"},
    {"item": "supports_prefix_caching", "kind": "capability", "status": "supported",
     "evidence": "readiness/prefill_resume_check.json"},
    {"item": "batched_prefill", "kind": "call", "status": "supported", "evidence": "<batched-equals-solo test>"},
    {"item": "supports_device_penalties", "kind": "capability", "status": "not_supported",
     "declaration": "<key and value, or documented default>", "reason": "<why the model does not implement it>"}
  ]
}
```

Prefix caching and batched prefill are required rows. `supports_prefix_caching` may be `not_supported` only for an exclusion named in [prefill features](../vllm-integration/references/prefill-features.md) (`block_output` or `recurrent_state`), and the `reason` must name it; the gate rejects any other reason. A `recurrent_state` model still reuses prefixes through session continuation: its row also names the `session_continuation` evidence (or the `template_breaks_extension` proof), which the serving stages build.

Add one row for each adapter call, `model_capabilities` declaration, and request feature that the plugin calls or tests, such as sampling parameters, seeds, logprobs, penalties, chunked prefill, prefix caching, and async decode. `kind` is `call`, `capability`, or `request_feature`. `status` is one of:

- `supported`: the generator provides it; `evidence` names the test or artifact;
- `not_supported`: an existing documented declaration or its documented default states it; `declaration` names it, and `reason` explains the effect on requests and why the model does not implement it;
- `plugin_owned`: the [plugin changes](../vllm-integration/references/plugin-changes.md) rules assign the gap to the plugin; `record` is the justification record path relative to the model directory;
- `missing`: the plugin test suite or the serving goal requires it, and the generator does not provide it.

Missing rows are full-model work, and the stage gate rejects them. Do not plan to close a row with a new plugin capability, environment variable, or `--additional-config` option. A gap that is smaller to close in the plugin is still a model gap when the model owns the behavior.

## How To Approach It

Read the HF reference model and the reports of the working TTNN blocks first. Understand the whole autoregressive
path: input ids, embeddings, masks, position ids, cache layout, block calls, logits, sampling, token feedback.
Setup work (weight conversion, dtypes, layouts, cache construction, tokenizer) stays out of the measured prefill
and decode.

Debug on a reduced full model, not the full stack. After one or two expensive all-layer failures, build a probe
that keeps one real layer of each kind with real tensor, cache and page-table shapes, the real final norm, LM head
and sampling, and a short prompt and generation, running in a couple of minutes. Use it to localize wrapper,
trace, cache, page-table, LM-head and sampling bugs; final evidence still comes from the all-layer model. If the
probe cannot show a failure, record why before spending more full-stack runs.

### Traced decode and sampling

The full model owns sampling, and is complete only when token-out decode uses the canonical split-sampling
contract:

- Before designing it, read both common sampling implementations, `models/common/sampling/` and
  `models/common/modules/sampling/sampling_1d.py`, and compare them with the model: state ownership, tracing,
  mesh support, sampling parameters, seeds, logprobs, penalties, `tt_out_tok`, top-k and padding expectations.
  Choose the better fit and record the choice and the rejected alternative. Write a custom sampler only after
  naming the contract neither provides.
- The model's decode trace produces sampler-ready logits without host readback; sampling is traced (or wrapped in
  a correct trace by the generator); `tt_out_tok` points at the persistent decode token input, so each sampled token
  is the next input on device.
- Position and RoPE state advance on device inside the trace; page-table inputs refresh only when the page table
  changes.
- Greedy stays on device on the fastest correct sampling path for the mesh; force-argmax is optional and is
  benchmarked against the normal top-k/top-p path when terminal sampling is material. The same path serves top-k
  and top-p sampling: no greedy-only path left for vLLM to finish.
- No host argmax, full-logits readback, untraced sampling inside the model trace, or Python token loop. If the
  contract does not work, the stage stays failing until it does.
- Shared tests that need host sampling get an explicit compatibility mode, kept separate from the measured and
  served path.

Build decode around persistent device state, following `$tt-enable-tracing`: prepare every supported prefill,
decode and sampling variant before the first capture and restore the KV, request and RNG state warmup changed;
allocate token, position/RoPE, page-table, KV, sampler, output and CCL buffers before capture; retain traces per
its lifetime rule. Between replays there is no per-token host work: no mask construction, cache reset,
synchronization, blocking replay or feedback readback. If TTNN or the runtime blocks one of these, keep the stage
incomplete until it is fixed or reduced to the smallest repro. Before accepting the trace, run repeated token-out
replay with `TT_METAL_TRACE_ALLOC_TRACKING=1` over every captured trace key in generator order and resolve each
unsafe allocation per the checkout's `tech_reports/AdvancedPerformanceOptimizationsForModels/TraceCorrectness.md`
(common causes: [tracing](../ttnn-knowledge/topics/tracing.md)). Eager decode is for debugging only, not evidence.

### Generator API

Expose two levels: low-level prefill and decode where the caller manages cache, page tables, positions, prompt
lengths and batch state; and high-level generation that owns tokenizer and setup and loops deterministically over
the low-level methods. The standard entry point is `build_generator(model_dir, mesh_device, **kwargs)`, returning a
generator implementing `readiness_check.contract.Generator` from this plugin:

```python
def prefill_forward(tokens, *, page_table, kv_cache, prompt_lens, start_pos=None, ...): ...
def decode_forward(tokens, start_pos, *, page_table, kv_cache, ...): ...
def prefill_logits(prompt_token_ids): ...  # high-level, owns cache/page-table setup
def prefill_logits_continuation(prompt_token_ids, start, *, positions=None, prefill_prefix=True): ...
def generate(prompt_token_ids, max_new_tokens, *, next_input=None, enable_trace=True, **kwargs): ...
```

- `prefill_forward` takes several rows, each with an absolute `start_pos` and `prompt_lens` end, and computes
  `tokens[i, start_pos[i]:prompt_lens[i]]` at absolute cache and RoPE positions. That one path serves prefix-cache
  hits, chunked continuations and preemption resumes
  ([prefill features](../vllm-integration/references/prefill-features.md)). `prefill_logits_continuation` is the
  driver the resumed-prefill check calls.
- `enable_trace` is an explicit keyword on `generate` (the teacher-forcing runner needs traced decode); `generate`
  is a thin loop over the low-level methods.
- Cache ownership is explicit: standalone generation may own its cache, serving passes an allocated cache and page
  table; support both unless the project says otherwise.
- Keep the decoder's inter-layer layout unless a measured change is faster end to end. If the residual stays
  sharded across devices, do not gather every layer for the wrapper's convenience; localize any final gather to the
  terminal path and measure it.

Use `$multichip` and `$optimize` for the pieces added around the decoder stack, matching its sharding. Profile the
reduced variant (one real layer of each kind, real shapes, sharding, dtypes, cache, terminal path and tracing),
never the all-layer stack with Tracy or the device profiler; final evidence includes its `tt-perf-report`.

## Validation

Compare with the HF reference on real weights, using the project's readiness harness where one exists. Evidence:

- prefill logits or token prediction quality, and teacher-forced decode quality across positions;
- free-running generation with several prompts and the longest feasible length (at least 64–128 tokens when
  runtime allows), reviewed for coherence: teacher forcing overrides token feedback and cannot see feedback bugs.
  Then run

  ```bash
  python "${TT_MODEL_BRINGUP_ROOT}/runtime/readiness_check/check_degenerate_output.py" \
    --hf-model <hf-model-id> --missing-artifacts critical --scope autoregressive
  ```

  and include its verdict. Doubled tokens or single-token collapse are decode-loop bugs, never model properties;
  the stage gate runs the same check.
- paged KV behaviour, prompt lengths including a non-aligned length near a chunk or page boundary and a long
  non-divisible one near the supported context, sequence limits;
- deterministic greedy output and logits across repeated runs and batch positions;
- watcher, fallback and runtime-integrity checks for the environment.

Start with a smoke check on the reduced probe, then the all-layer gate. While debugging, rerun only the failing
item; after the fix, rerun the smoke check, then the full gate for final evidence.

Batch coverage: batch 1 is the latency row and the model must still handle batch dimensions; add batch > 1 tests
for prefill, decode, cache and page-table indexing, token feedback and output, up to the largest batch in
`batch_sizes` when hardware allows. Two further tests are required because serving depends on them: resumed
prefill through `prefill_logits_continuation` (run `run_prefill_resume_check`, output under `readiness/`), and one
`prefill_forward` call carrying rows of different lengths whose outputs equal the single-row results. A prefill
kernel chosen by chunk length can fail the resumed check with a correct cache, because continuation and full
prefill then compute the prefix differently; rule that out first.

For instruction and chat models, the main readiness reference is the DeepSeek AIME24 prompt set rendered with the
HF chat template:

```bash
python -m readiness_check.generate \
  --hf-model <hf-model-id> \
  --prompt-source aime24 \
  --chat-template \
  --gen-len 100 \
  --top-k 100 \
  --output <model_dir>/readiness_aime24_chat.refpt
```

Raw book text is extra stress coverage, not the main gate for an instruct model with a usable chat template. Use
`$qualitative-check` for prompt-based checks, prompt-format evidence and HF controls. Generate the reference fresh
unless metadata under the current autoport directory proves the same model id and revision, tokenizer, prompt
source, chat-template flag, generation length, top-k and command.

Run the shared qualitative prompt suite through HF and the TT generator as soon as the model generates text
(`$qualitative-check`), keeping the script and outputs under `doc/full_model/qualitative/` so later stages rerun
the same prompts.

Add a split-sampling trace test: replay two or more decode steps with different tokens and positions; assert the
persistent inputs changed as expected and the sampled token of step N is the input of step N+1 with no host
reconstruction; cover changed and unchanged page tables; show no per-token host rebuild of tokens, positions, RoPE
indices, masks or page tables; alternate greedy and sampled parameters if trace ids are cached by sampling mode.

When accuracy is poor, check the new wrapper first (embeddings, final norm, LM head and tied weights, positions,
masks, cache indexing, prompt lengths, page tables, sampling); use `$autofix` when the causal chain is unclear;
go back to decoder precision only when evidence points there. A failure that follows a dtype, cache dtype,
fidelity or CCL payload change is a precision-policy question until controls isolate a defect; how to run those
controls is in [numerics](../ttnn-knowledge/topics/numerics.md). For paged decode, validate the cache and page-table allocation
against the attention op's rounded read window; the over-allocation control is in [prefill and KV](../ttnn-knowledge/topics/prefill-kv.md).

## Performance

Measure warmed full-model prefill and decode end to end from the host, including generator overhead, cache
management, final norm, LM head, logits and sampling, unless the project asks for device-only timing. Use
`$optimize` for on-device work and read section 4.5 of `tech_reports/LLMs/llms.md` for host-side advice.

Report: prefill time to first token at a representative length; trace-verified decode tokens/s/user over a
representative window; traced decode latency; any material host/device gap or setup overhead. When they differ,
report both teacher-forcing decode (no sampling or feedback; comparable to readiness and PERF-style numbers) and
token-out decode (final norm, LM head, sampling, feedback, required readback; comparable to generation and
serving), and name the boundary in every comparison. `readiness_check.run_teacher_forcing` drives
`generate(..., next_input=..., enable_trace=True)`, so it measures token-out; a logits-only number needs its own
labelled traced no-sampling run.

Host steps between decode iterations are bugs to remove, not terms to report: count trace replays, token, position
and RoPE refreshes, page-table refreshes, synchronizations and readbacks, and drive steady-state refreshes to zero
except where the caller's API needs a readback.

Measure batch-1 TTFT and decode with the vLLM primary single-user shape (prompt 128, generate 128 unless the
project says otherwise), separately from the accuracy workload, and record the shape beside every number; later stages compare it with `vllm_benchmark.json`.
Larger batches prove capability; they do not replace the batch-1 latency row.

For greedy decode, sampler work must not dominate token-out time; if argmax, a full-vocab all-gather or generic
top-k does, fix the LM-head and sampling contract before tuning dtypes, CCL or the LM head. Compare against the
decoder-stack lower bound (each optimized layer kind's decode latency times its count) and close the full-model-only
costs (final norm, LM head, sampling, trace replay, input and page-table refreshes, synchronizations, readback,
disabled CCL or persistent-buffer options). If the lower bound says the target is reachable and the full model misses it, the
optimized-full-model stage closes that gap; if the lower bound already misses the target, return to decoder,
multichip or precision work rather than generator code. If performance regresses badly from block-level numbers,
inspect data movement between embeddings, blocks, final norm, LM head and sampling before changing precision.

## Evidence To Leave

Done means all of these are true and recorded:

- HF model id or local checkpoint, hardware, mesh, branch/commit, and environment;
- selected block/decoder implementation and why;
- model wrapper and generator contracts;
- serving contract: pinned plugin and vLLM pair, and `doc/full_model/serving_contract.json` with no missing rows;
- resumed prefill: `readiness/prefill_resume_check.json` from `run_prefill_resume_check` with every resume start passing and both negative controls failing, or the serving-contract exclusion that explains its absence;
- batched prefill: batched-equals-solo evidence for mixed lengths and row orders, the warmup bucket list, and program-cache entry counts before and after the workload;
- carried-forward decoder contract: weight/activation/KV/CCL dtype policy, tensor-group exceptions, and residual layout;
- state-dict mapping, tied-embedding behavior if relevant, and real-weight loading behavior;
- KV-cache, page-table or position handling, prompt lengths, and repeated decode reuse;
- context contract: HF-advertised context, full-model supported context, and any hard-physical-limit reduction evidence;
- batch contract: batch-1 regression row, target-workload row, largest batch tested up to the largest in `batch_sizes`, and any hard-physical-limit reduction evidence;
- full-model accuracy and `$qualitative-check` evidence, including prompt-format metadata, rendered prompt artifacts, HF controls, and the shared qualitative prompt suite run through the TT generator;
- split-sampling trace evidence: model trace to logits, internal sampling trace, `tt_out_tok` feedback into the persistent decode token input, current-position coherence, and page-table refresh coverage;
- determinism or repeated-run coverage appropriate to the implementation risk, including logit reproducibility across runs and batch positions;
- watcher/fallback/runtime-integrity status;
- full-model prefill plus separate teacher-forcing and token-out decode performance when those paths differ;
- layer-stack lower-bound accounting and the full-model-only costs;
- trace-loop host-work counters, including token/current-position/RoPE/page-table refreshes and sync/readback counts;
- sequence limits and remaining risks;
- tt-perf-report output for the reduced profiling variant with one real layer of each unique layer kind.

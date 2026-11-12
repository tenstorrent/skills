# Eval readiness under load: long answers, long context, chunk size

Evals run 32 users that each write thousands of reasoning tokens, on prompts that reach tens of thousands of tokens. A server that passes the 128/128 benchmarks can still fail them: sampling falls back to the host under the eval settings, KV memory runs out, a long context loops or slows down sharply, or the eval does not finish inside the CI time cap. These probes find that on the served chip path, before CI.

All probes attach to a running server (`run_vllm_server --stages serve`, or the stage's own launch) and write `doc/eval_readiness/<probe>.json` through the shared result writer. The stage gate `scripts/check_eval_readiness.py` reads them. Pass the server log with `--server-log`: the probes read engine errors, trace captures, `sample_on_device_mode` and `max_num_batched_tokens` from it.

| Probe | Stage | Result | Command |
|---|---|---|---|
| P5 accuracy | 9 vllm | `long_context_accuracy` | `python -m readiness_check.run_long_context --part accuracy` |
| Chunk-size sweep | 10 | raw files under `long_context_timing/` | `python -m readiness_check.run_long_context --part chunk` |
| P5 timing | 10 | `long_context_timing` | `python -m readiness_check.run_long_context --part timing` |
| P4 long answers + health | 10 | `long_answer_load` | `python -m readiness_check.run_long_answer_load` |

Common arguments: `--server-url http://localhost:<port> --model-dir models/autoports/<model> --server-log <server log>`. Run them from the tt-metal checkout with the plugin `runtime/` directory on `PYTHONPATH`. They use only the Python standard library and vLLM's `/tokenize`, `/metrics`, `/health` and `/v1/models` endpoints.

## Stage 10 order

1. Chunk-size sweep (below). It needs a server restart per value, so do it first.
2. Serve the picked `max_num_batched_tokens` for everything that follows.
3. P5 timing, which also reads the sweep files.
4. P4 with the eval profile.
5. `check_eval_readiness.py --stage optimized-vllm` (the stage check script runs it).

## Chunk-size sweep

The prefill chunk size (`--max-num-batched-tokens`) decides how long a long prompt blocks the users who are decoding, and how fast the prompt itself is read. Chunked prefill stays on; this sweep picks the size.

Restart the server at three values aligned with the prepared prefill trace buckets, by default 512, 2048 and 4096, and run once per value:

```bash
python -m readiness_check.run_long_context --part chunk --server-url http://localhost:8000 \
  --model-dir models/autoports/<model> --server-log <server log>
```

Each run starts `--decode-users` (default 31) short prompts that keep decoding, then sends a 4K and a 32K prompt (`--chunk-prompts`) and records their TTFT and the worst inter-token gap the decoding users saw while each prompt was read. It writes `doc/eval_readiness/long_context_timing/chunk_<value>.json`; the value comes from the server log or `--max-num-batched-tokens`. P5 timing then picks the largest value whose worst gap is at or below `--itl-bound-ms` (default 1000 ms) and flags, as advisory, a sweep with fewer than three values or a served value that differs from the pick. Start MoE models at 2048.

Evidence: on one MoE model, 512 to 2048 cut the 32-user 4K-prompt TTFT from 41.6 s to 5.75 s and a full benchmark from 2 h 12 min to 58 min.

## P5 long context

Accuracy (stage 9): a needle fact ("the secret passphrase for the lighthouse keeper is ...") is placed at depths 0.1, 0.5 and 0.9 of long natural text, the readiness book `models/ttt_compat/tests/tale-of-two-cities.txt.bz2` in the tt-metal checkout (older checkouts keep it under the former tt_transformers tests folder; or pass `--text-file`). Lengths are about 8K, 32K and 128K tokens, clamped to the served `max_model_len` minus the answer budget, 64 tokens of template slack and a 2% length tolerance; prompt length is counted with the served chat template through `/tokenize`, and the text is trimmed until the prompt is at most the target. An HTTP 400 for context length is a `probe_setup` failure (critical, a probe problem), not a recall miss. Questions go through the chat template at temperature 0 with thinking off (`--thinking` to turn it on). `recall_<length>` fails when an answer does not contain the needle; `served_on_device_sampling` (advisory) records the server's sampling mode, because the check must run on the chip path. Evidence: one model's 34K recall looped on the chip sampling path and passed on the host path, and that blocked its stage 10 for days.

Timing (stage 10): `--part timing` measures single-user decode ms/token at 1K, 8K, 32K and 128K (32 output tokens, `ignore_eos`), and TTFT for 127- against 128-token prompts.

- `tpot_cliff` (advisory): TPOT more than 2x between adjacent lengths. Evidence: one model took 13 ms at 32K, 229 ms at 64K and 1.75 s at 512K; 8 of 10 agentic tasks then timed out, and the fix (an existing fast kernel) took a week of CI runs.
- `ttft_127_vs_128` (advisory): ratio above 1.5. Evidence: one model took 556 ms for a non-power-of-two prompt and 190 ms for an aligned one, because the unaligned prompt ran several prefill passes.
- `timing_requests_completed` (critical): any timing request failed.

Usual fixes:

- Recall miss: run the same prompt on the HF model (the control rule) and on the host-sampling path. A miss only on the chip path points at long-context attention, RoPE scaling, the KV cache dtype, or the device sampler at long positions. A miss the HF model shares is a limitation (`original_model: "same"`).
- Cliff: find the length where the attention or cache-read path changes kernel or program config; use the existing long-sequence kernel or chunked SDPA; record it if no fast path exists.
- Slow unaligned TTFT: pad the prompt to the prepared bucket inside one prefill pass instead of splitting it.

## P4 long answers with 32 users

```bash
python -m readiness_check.run_long_answer_load --server-url http://localhost:8000 \
  --model-dir models/autoports/<model> --server-log <server log> --hf-model <org/name> \
  [--profile lm_eval_reasoning] [--profiles-file <your copy>] --theoretical-tps <per-user roofline at 32 users> [--eval-plan <plan json>]
```

The default profile is `lm_eval_reasoning` (the lm-eval reasoning settings). Profiles come from `runtime/readiness_check/eval_request_profiles.json`, which ships with the plugin: do not edit the installed copy. To change or add a profile, copy the file into the model's `doc/eval_readiness/` and pass `--profiles-file <copy>`. Profiles resolve through `eval_request_profiles.get_profile`, so `--profile model_card` reads the checkpoint's `generation_config.json` (pass `--hf-model`; P5 takes the same flags). A profile with `thinking: null` leaves the chat template's thinking default alone. Requests use the bundled reasoning questions (`load_prompts.json`) through the chat template with thinking on, up to 6,000 tokens each; `ignore_eos` is set only when the profile sets it.

The probe first runs a short burst with the eval profile and one with the plain profile (greedy, no seed), same users and length, back to back. The step-time ratio is median decode TPOT (eval) over median TPOT (plain), the same definition as P2. Then it runs the long load while polling `/metrics`, and records per-user and aggregate tok/s per 1,000-token answer bucket, running and waiting requests, peak KV usage and preemptions. Raw data goes to `doc/eval_readiness/long_answer_load/`.

| Check | Severity | Fails when | Usual fix |
|---|---|---|---|
| `host_sampling_under_load` | critical | eval/plain step ratio > 1.15 | declare the profile's top_k, seed or penalties on the device sampler (one model moved to top_k 64: 243 to 62 ms per step) |
| `requests_completed` | critical | any request failed | read the server log around the failure |
| `engine_errors` | critical | fatal marker or ERROR line in the server log during the load, or `/health` not 200 | as above |
| `slots_released` | critical | running or waiting not back to 0 after the load | a request leak in the adapter or a stuck sampler state |
| `kv_back_to_idle` | critical | KV usage not back to its idle value | blocks held by a finished request |
| `no_new_trace_captures` | critical | capture lines in the server log during the load | a program compiled after warmup; see the mixed-shape trace reuse check |
| `preemptions` | critical, waivable | more than 1 preemption while the KV pool fits users x (prompt + answer) | pool size (`gpu_memory_utilization`, `num_gpu_blocks`), scheduler budget; advisory when the pool does not fit |
| `functional_floor` | critical | per-user tok/s in the first answer bucket below the floor | host sampling, per-row loops, a slow MoE layout |
| `context_slowdown` | advisory | last-bucket aggregate below half the first | long-context decode path |
| `answer_length` | advisory | median answer below half of `--max-tokens` | harder prompts or an `ignore_eos` profile |
| `eval_forecast` | advisory | an eval or the total over 80% of the CI cap | run the suggested subset or output cap |

Floor: the FUNCTIONAL bar is single-user decode at 10% or more of the theoretical roofline ($optimize, "Theoretical roofline"). Decode is weight-bandwidth bound, so a batch should not drop each user below 10% of the theoretical per-user rate at that batch. Pass `--theoretical-tps` as the roofline at 32 users (for MoE, count the experts 32 users touch), or the floor itself with `--functional-floor-tps`. Without either, the check is an advisory reminder.

Trace captures: the probe counts server-log lines that match `--trace-capture-regex` (default: `begin_trace_capture`, "recapture", "capture ... trace"). Log one line per capture from the generator, as the mixed-shape check asks; a log with no capture lines at all is reported as missing evidence, not as zero captures.

Forecast: `--eval-plan` (default `runtime/readiness_check/eval_plan_defaults.json`) lists each eval's questions, repeats and typical answer tokens, and the CI caps (18 h per job, 12 h nightly; pick with `--ci-cap`). The bundled numbers are planning defaults; replace them with the model's release eval plan and the answer lengths stage 11 measures. Hours = questions x answer tokens / aggregate tok/s, integrated over the measured buckets (lengths past the last bucket reuse its speed and are marked extrapolated, so the forecast is optimistic there). For an eval over 80% of the cap the probe suggests the largest question subset and the largest output cap that fit; a cap below the typical answer length produces blank answers, so prefer the subset. Evidence: one MoE model managed 165 tok/s with 32 users; its first CI eval run was cancelled at the 18 h cap with nothing saved, and the fourth run, sized by hand (GPQA cut to 80 questions), passed in 11 h 24 min.

Slow but correct is advisory: record the forecast and the suggested sizes in the README and move on. A slow MoE layout is fixed in the multichip stages, not here.

## Batched prefill (optional)

Measure the burst ratio and record it; the gate reports it as advisory. Adopt batched prefill only when it does not slow decode or cost trace retention; see [prefill features](prefill-features.md#batched-prefill).

## Time box and limitations

Spend at most 2 hours on one probe's failures. Then write the finding to `doc/eval_readiness/limitations.json` (`probe`, `check`, `reason`, `evidence` paths, `original_model`, `fix_hours`) and move on: the gate turns a waivable critical check into an advisory one when the HF model behaves the same (`original_model: "same"`) or the 2-hour box was spent. Failed requests where every request fails are not waivable. Rerun the probe after each fix; the result file must come from the final served configuration.

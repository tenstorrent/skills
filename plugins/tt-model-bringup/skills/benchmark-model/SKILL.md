---
name: benchmark-model
description: Report a text model's accuracy on fixed lm-eval subsets and vLLM serving performance at single-user plus user-selected workload profiles. Use after optimized-vLLM for the final model-bringup report or to benchmark a supported model.
---

# Benchmark a model through vLLM

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Produce an end-of-bringup report with benchmark scores, published references where available, and serving performance. The person who launched the bringup judges whether the results are good enough.

Use upstream EleutherAI lm-evaluation-harness for accuracy and `vllm bench serve` for performance. Attach to the working Stage 10 server with its selected precision policy. The complete client stage must finish in **less than one hour**. Its clock starts when `benchmark_stage run` is invoked, before the first server-selection hook, and includes verification, inference, every server launch or reload it performs, compilation, warmups, phase collection, scoring and reporting. Record setup completed before invocation separately, including installation, downloads and the already-running server's startup.

## Select and run benchmarks

Read [the run contract](references/run-contract.md) for setup, profiles, commands and report inputs. Aim for two or three applicable generative benchmarks: MMLU-Pro, GPQA Diamond, IFEval or GSM8K-CoT. Prefer tasks with published figures for the exact model; a missing reference does not prevent running or completing the stage. Another upstream generative task is suitable when the client can execute and score it correctly.

Select the upstream prompt/scorer variant and the model's generation settings before running. A benchmark name alone does not identify a recipe. Use the model's native HF chat template exactly once, with structured chat messages, and record its tokenizer revision, thinking mode, generation parameters and answer parsing. Preserve normal benchmark prompt lengths, few-shot examples and EOS behavior. Allow sufficient output budget for the intended reasoning mode.

Reuse the packaged common question subsets. A new model does not require a new subset. Use `prepare --reuse-manifest` to select a different recipe over the same documents. For a new benchmark, freeze documents before observing scores; retain IDs, content hashes and population counts. Never select questions to improve agreement with a published score.

Run accuracy at the user-selected concurrency. If none was selected, use concurrency 1. Use the
shared request pool when the selected tasks have identical generation settings. Check raw responses
for template, transport and scoring integration errors and repair those errors. Keep upstream
scores and all questions in the denominator, including token-limited answers. Report truncation
counts and relevant protocol differences. Low scores are results to present, not a reason to block
completion or keep optimizing the model.

## Measure performance

Always measure the single-user profile and add only the input length, output length and concurrency
profiles requested by the user. Use the best validated settings from optimized-vLLM for each
profile. Each must use its corresponding decode trace and cache configuration. Keep model,
precision, hardware and full context capacity unchanged. Warm each server configuration, then
measure repeated requests. Disable prefix caching, use distinct prompts, request greedy decoding
and ignore EOS for performance. Verify actual token counts.

Prepare full-phase accounting before starting the timed run. Use or implement the model's collector and configure the required `roofline_command` from the run contract. Enable lightweight host timing at the actual prefill/decode completion boundaries and derive work from the model's executed shapes, precision and parallelism. Do not enable the live device profiler or add synchronization that changes the serving path. The runner checks the collector on each running server and collects its evidence before switching profiles.

The report labels each workload, concurrent requests and server slots separately. Headline
performance includes TTFT, TPOT, per-user decode tokens/s, aggregate output tokens/s, prefill FLOP
roofline percentage and decode DRAM bandwidth roofline percentage. Both roofline estimates are
required for every requested profile. Their denominators cover complete elapsed phases, including
host work and gaps, rather than matmul duration. If timing or work accounting is missing, preserve
the measured results and repair the collection; the stage is incomplete. No minimum roofline
percentage or accuracy score is required.

Use the run contract's server-control hook to prepare and record the selected accuracy server and
each requested performance profile. Switching and warmup count toward the stage budget. Preserve
ITL, end-to-end latency, percentiles, request throughput, completion counts and wall time.

## Diagnose Slow Benchmarks And Evals

Before a long run, use a small representative subset to estimate wall time from measured TTFT,
TPOT/tokens-per-user, input/output lengths, concurrency and sample count. For high input length,
output length, batch or concurrency cases, passing is not sufficient: compare host-observed TTFT
and token cadence with device prefill/decode intervals and check whether an unexamined optimization
could materially shorten the run.

If device time explains the wall time, return to the relevant prefill, decode, sampling or
multi-device optimization. If it does not, measure queue wait, request scheduling, trace
compilation/selection, synchronization, HTTP/client overhead, serialization, retries, timeouts and
eval-framework or task-environment setup. Fix the dominant overhead and repeat the small probe
before committing the stage budget to the full workload. Keep first-use and warmed measurements
separate, and do not shorten valid inputs or outputs to make the run finish.

## Deliver the report

The runner writes the single final report at `doc/benchmark/run/REPORT.md`, with results and reference links at the top and supporting measurements below. Keep `identity.json` and `RUN_NOTES.md` in `doc/benchmark/`; the runner retains its configuration, manifest, raw outputs and summary under `run/`.

For a bringup, verify that the server imports the generated `models/autoports/<model>` implementation with all configured layers. Record model/tokenizer revisions, precision, source commits, server command and hardware. Preserve `doc/context_contract.json`. Stock implementations are suitable for standalone benchmarks, but do not establish the autoport's results.

Complete the stage when the selected benchmarks and both performance rows, including full-phase prefill FLOP and decode DRAM accounting, have valid, complete evidence, the report is generated, implementation/context checks pass, and elapsed client time is under one hour. No accuracy acceptance review is required. Use $autofix for execution or integration failures; retain failed-run evidence. Stop only processes owned by this stage and follow the enclosing run's reservation policy.

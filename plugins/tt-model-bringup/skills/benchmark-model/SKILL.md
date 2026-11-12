---
name: benchmark-model
description: Report a text model's accuracy on fixed lm-eval subsets and 4K-input vLLM serving performance at decode batch sizes 1, 8, 16 and 32. Use after optimized-vLLM for the final model-bringup report or to benchmark a supported model.
---

# Benchmark a model through vLLM

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Produce an end-of-bringup report with benchmark scores, published references where available, and serving performance. The person who launched the bringup judges whether the results are good enough.

Use upstream EleutherAI lm-evaluation-harness for accuracy and `vllm bench serve` for performance. Use the Stage 10 serving configs with their selected precision policy. The runner keeps two clocks:

- **Client clock**: at most **30 minutes per configured batch size**. It starts when `benchmark_stage run` is invoked and covers verification, accuracy, measured requests, phase collection, scoring and reporting.
- **Setup clock**: server launches and reloads, compilation and warmups the runner performs. It has its own bound (`setup_budget_seconds`, default 30 minutes per measured profile).

Record setup completed before invocation separately, including installation, downloads and the already-running server's startup. See [the run contract](references/run-contract.md#clocks-sample-counts-and-attempts) for sample sizing and attempts. You may run up to **two fresh, fully timed attempts** without asking; do not stop to ask for permission before the second.

## Select and run benchmarks

Read [the run contract](references/run-contract.md) for setup, profiles, commands and report inputs. Aim for two or three applicable generative benchmarks: MMLU-Pro, GPQA Diamond, IFEval or GSM8K-CoT. Prefer tasks with published figures for the exact model; a missing reference does not prevent running or completing the stage. Another upstream generative task is suitable when the client can execute and score it correctly.

Select the upstream prompt/scorer variant and the model's generation settings before running. A benchmark name alone does not identify a recipe. Use the model's native HF chat template exactly once, with structured chat messages, and record its tokenizer revision, thinking mode, generation parameters and answer parsing. Preserve normal benchmark prompt lengths, few-shot examples and EOS behavior. Allow sufficient output budget for the intended reasoning mode.

Reuse the packaged common question subsets. A new model does not require a new subset. Use `prepare --reuse-manifest` to select a different recipe over the same documents. For a new benchmark, freeze documents before observing scores; retain IDs, content hashes and population counts. Never select questions to improve agreement with a published score.

Run accuracy on the largest measured batch: its server and the same number of concurrent HTTP requests. Keep the Stage 9/10 prefix-caching setting for evals; the runner records it. Use the shared request pool when the selected tasks have identical generation settings. Check raw responses for template, transport and scoring integration errors and repair those errors. Keep upstream scores and all questions in the denominator, including token-limited answers. Report truncation counts and relevant protocol differences. Low scores are results to present, not a reason to block completion or keep optimizing the model.

Read the **answer statistics** the runner adds to the report for each task: the share of answers the scorer could extract (per filter, target at least 95%), the share blank at the output cap (target at most 5%), answer tokens (median, p90, max) and a recommended output cap. Low extraction or many blank answers usually point to the protocol, not the model. Usual fixes: use the model's few-shot or answer-format recipe, report the flexible filter beside the strict one, raise the output cap toward the recommendation, or check the reasoning parser. Examples: 9 of 40 GPQA answers blank at a 32K cap; MMLU-Pro 44.6 → 65.4 after a few-shot fix; GSM8K strict 0/10 vs flexible 7/10 on the same answers.

## Measure performance

Measure one profile per decode batch size `b` in `batch_sizes` (default **1, 8, 16, 32**). Each profile uses the Stage 10 config for that batch, with server `--max-num-seqs b` and `b` concurrent requests, **4096 input tokens** and **128 output tokens**, so the batch is filled rather than padded. If Stage 10 could not validate a batch size, list it in `skipped_batch_sizes` with the reason; the report shows it as not measured. Keep model, precision, hardware and full context capacity unchanged. Warm each server, then measure repeated requests with distinct prompts, greedy decoding and ignored EOS. Perf rows may run with prefix caching off; record the setting either way. Verify actual token counts.

On the largest server the runner adds two rows at concurrency 1: **128/128**, the FUNCTIONAL reference, and **32K in / 128 out** (or the max context if smaller). They show what a lone user pays on the server CI uses; a model can decode at 16 ms/token on a 1-slot server and 114 ms/token on its 32-slot server.

Prepare full-phase accounting before starting the timed run. Use or implement the model's collector and configure the required `roofline_command` from the run contract. Enable lightweight host timing at the actual prefill/decode completion boundaries and derive work from the model's executed shapes, precision and parallelism. Do not enable the live device profiler or add synchronization that changes the serving path. The runner checks the collector on each running server and collects its evidence before switching profiles.

The report labels the profile, concurrent requests and server slots separately. Headline performance includes TTFT, TPOT, per-user decode tokens/s, aggregate output tokens/s, prefill FLOP roofline percentage and decode DRAM bandwidth roofline percentage. **Both roofline estimates are required for every batch profile.** Their denominators cover complete elapsed phases, including host work and gaps, rather than matmul duration. If timing or work accounting is missing, preserve the measured results and repair the collection; the stage is incomplete. No minimum roofline percentage or accuracy score is required.

Use the run contract's server-control hook. The runner prepares the largest server first, runs accuracy and its profile there, then switches to each smaller batch, largest first. Preserve ITL, end-to-end latency, percentiles, request throughput, completion counts and wall time in the report details.

## Deliver the report

The runner writes the single final report at `doc/benchmark/run/REPORT.md`, with results and reference links at the top and supporting measurements below. Keep `identity.json` and `RUN_NOTES.md` in `doc/benchmark/`; the runner retains its configuration, manifest, raw outputs and summary under `run/`.

For a bringup, verify that the server imports the generated `models/autoports/<model>` implementation with all configured layers. Record model/tokenizer revisions, precision, source commits, server command and hardware. Preserve `doc/context_contract.json`. Stock implementations are suitable for standalone benchmarks, but do not establish the autoport's results.

Complete the stage when the selected benchmarks, their answer statistics, every batch profile with full-phase prefill FLOP and decode DRAM accounting, and both lone-user rows have valid, complete evidence, the report is generated, implementation/context checks pass, and both clocks are within their bounds. The checker exits 1 (advisory) for extraction below 95% or blank-at-cap above 5%, and 2 (critical) for missing or stale statistics. No accuracy acceptance review is required. Use $autofix for execution or integration failures; retain failed-run evidence. Stop only processes owned by this stage and follow the enclosing run's reservation policy.

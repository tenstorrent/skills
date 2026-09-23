---
name: benchmark-model
description: Report a text model's accuracy on fixed lm-eval subsets and 4K-input vLLM serving performance at concurrency 1 and 32. Use after optimized-vLLM for the final model-bringup report or to benchmark a supported model.
---

# Benchmark a model through vLLM

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Produce an end-of-bringup report with benchmark scores, published references where available, and serving performance. The person who launched the bringup judges whether the results are good enough.

Use upstream EleutherAI lm-evaluation-harness for accuracy and `vllm bench serve` for performance. Attach to the working Stage 10 server with its selected precision policy. The complete client stage must finish in **less than one hour**, including dataset verification, accuracy requests, performance warmups/runs, scoring and reporting. Record installation, downloads, model loading and trace compilation separately.

## Select and run benchmarks

Read [the run contract](references/run-contract.md) for setup, profiles, commands and report inputs. Aim for two or three applicable generative benchmarks: MMLU-Pro, GPQA Diamond, IFEval or GSM8K-CoT. Prefer tasks with published figures for the exact model; a missing reference does not prevent running or completing the stage. Another upstream generative task is suitable when the client can execute and score it correctly.

Select the upstream prompt/scorer variant and the model's generation settings before running. A benchmark name alone does not identify a recipe. Use the model's native HF chat template exactly once, with structured chat messages, and record its tokenizer revision, thinking mode, generation parameters and answer parsing. Preserve normal benchmark prompt lengths, few-shot examples and EOS behavior. Allow sufficient output budget for the intended reasoning mode.

Reuse the packaged common question subsets. A new model does not require a new subset. Use `prepare --reuse-manifest` to select a different recipe over the same documents. For a new benchmark, freeze documents before observing scores; retain IDs, content hashes and population counts. Never select questions to improve agreement with a published score.

Run accuracy at **32 concurrent HTTP requests**. Use the shared request pool when the selected tasks have identical generation settings. Check raw responses for template, transport and scoring integration errors and repair those errors. Keep upstream scores and all questions in the denominator, including token-limited answers. Report truncation counts and relevant protocol differences. Low scores are results to present, not a reason to block completion or keep optimizing the model.

## Measure performance

Use **4096 input tokens** and **128 output tokens**, at concurrency **1 and 32**. Warm both shapes, then measure repeated requests under the same precision policy as accuracy. Disable prefix caching, use distinct prompts, request greedy decoding and ignore EOS for performance. Verify actual token counts.

Headline performance includes TTFT, TPOT, per-user decode tokens/s, aggregate output tokens/s, prefill FLOP roofline percentage and decode DRAM bandwidth roofline percentage. Compute roofline estimates using the complete elapsed prefill/decode phases, including host work and gaps. Use measured server phase timing and model/hardware accounting as described in the run contract; show unavailable values when those inputs cannot be obtained. HTTP concurrency does not establish physical device batch size. Preserve ITL, end-to-end latency, percentiles, request throughput, completion counts and wall time in the report details.

## Deliver the report

The runner writes the single final report at `doc/benchmark/run/REPORT.md`, with results and reference links at the top and supporting measurements below. Keep `identity.json` and `RUN_NOTES.md` in `doc/benchmark/`; the runner retains its configuration, manifest, raw outputs and summary under `run/`.

For a bringup, verify that the server imports the generated `models/autoports/<model>` implementation with all configured layers. Record model/tokenizer revisions, precision, source commits, server command and hardware. Preserve `doc/context_contract.json`. Stock implementations are suitable for standalone benchmarks, but do not establish the autoport's results.

Complete the stage when the selected benchmarks and both performance rows have valid, complete evidence, the report is generated, implementation/context checks pass, and elapsed client time is under one hour. No accuracy acceptance review is required. Use $autofix for execution or integration failures; retain failed-run evidence. Stop only processes owned by this stage and follow the enclosing run's reservation policy.

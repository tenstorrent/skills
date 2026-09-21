---
name: benchmark-model
description: Evaluate a brought-up text model through vLLM using fixed lm-eval subsets and measure 4K-input serving performance at concurrency 1 and 32. Use after optimized-vLLM for the final model-bringup benchmark stage or to calibrate its subsets on supported models.
---

# Benchmark a model through vLLM

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). This replaces the TTI release stage. Use upstream EleutherAI lm-evaluation-harness for accuracy and `vllm bench serve` for performance. Keep native model formatting and upstream scoring; do not introduce a model-family prompt wrapper.

The target is a complete stage in **less than one hour**, attached to the working Stage 10 server. Record cold installation, download, model-load and trace-compilation time separately. Include task preparation/verification, all accuracy requests, performance warmups/runs, scoring and reporting in stage wall time. A timeout is incomplete evidence, not a pass.

## Select and freeze

Read [the run contract](references/run-contract.md) for client setup, commands, profiles and evidence. Check the publisher’s evaluation recipe before selecting the upstream task variant. A benchmark name alone does not identify the prompt or scorer. Choose two or three supported generative tasks with published figures for this exact model: MMLU-Pro, GPQA Diamond, IFEval, or GSM8K-CoT for smaller/older models. These task names identify different protocols; GPQA main is not Diamond. Code-only models may require a separately supported code benchmark; report lack of coverage instead of substituting unrelated published scores.

Freeze the common subset manifest before observing scores. Use the same task documents for every model. Record document hashes, full population, actual sample count, harness version and subset hash. Recheck dataset content before each run. When using a different upstream recipe over the same benchmark documents, preserve the frozen IDs with `prepare --reuse-manifest`. If official per-question reference results exist, compare their subset and full scores directly. Do not select seeds/items to make published numbers agree. If a sample is too noisy, expand its deterministic hash ordering and repeat calibration.

Run accuracy at **32 concurrent HTTP requests**, with normal benchmark prompt lengths and normal EOS behavior. When selected tasks have identical generation settings, use the run contract's shared request pool to avoid waiting on a separate long tail for each benchmark. Do not pad accuracy inputs to 4K or shorten them to fit. Preserve the benchmark's few-shot examples, extraction rules and scoring. Use structured chat messages so the server applies the HF-declared template once. Record tokenizer revision/template hash, system prompt, generation parameters, thinking mode and answer parsing. Reasoning budgets must be sufficient for the selected published protocol; a shortened budget must not be presented as equivalent.

## Prove what ran

During model bringup, serve the generated `models/autoports/<model>` implementation and the precision policy selected by datatype-sweep/optimized-vLLM. Stock `models/tt_transformers` or `models/common` is valid only for an explicitly requested calibration, not as final evidence for an autoport. Save the launch command, source commits, effective configuration, imported generator module/file, full-layer count, model revision, hardware and health-check evidence. Preserve `doc/context_contract.json`; repair valid unaligned-input failures in the implementation.

Use accuracy mode for calibration when requested. Report performance under that same policy; do not silently switch precision between accuracy and timing.

## Measure and inspect

Measure performance with **4096 input tokens**, **128 output tokens** by default, at concurrency **1 and 32**. Use the same output length for both rows. Warm both shapes, then measure repeated requests. Disable prefix caching, use distinct prompts/seeds, request greedy decoding and ignore EOS only for performance. Verify actual server token counts. Report TTFT, TPOT, ITL, end-to-end latency, per-user decode tokens/s derived from TPOT, aggregate output tokens/s, request throughput, percentiles, completion counts and wall time. Label concurrency as the serving workload; continuous batching does not guarantee a constant physical batch on every step.

Inspect raw generated answers as well as aggregate scores. Report API failures, missing final answers, finish reasons and truncation rates. Fix parser/template integration errors before interpreting quality. If an upstream extraction rule disagrees with an explicit final choice, preserve the upstream score and document the affected answers separately. Do not hand-correct the headline score or silently redefine the benchmark after seeing results; a different extraction rule is a different protocol. Any truncation requires a documented assessment or rerun with a sufficient budget; do not remove failed questions from the denominator.

Compare each subset result with the exact published full-set metric. Report the score difference, sample uncertainty, protocol differences and source URL. Agreement on a few control models is a useful sanity check, not proof of equivalence for every model or sensitivity to every implementation error. Keep per-task results rather than hiding disagreement in an average. Use later reruns of this frozen subset to establish regression baselines.

## Finish

Write `doc/benchmark/REPORT.md`, `RUN_NOTES.md`, the frozen manifest, run configuration, compact machine-readable results, raw client outputs and an evidence-based accuracy review. Distinguish execution success from acceptable accuracy. The stage is complete only when required requests and both performance rows pass, runtime is under one hour, the implementation/context match, and unexplained quality failures are resolved. Use $autofix for model/client failures and $stage-review for final review. Preserve evidence for failures; never mark a partial run complete. Stop only processes owned by this stage, and follow the enclosing run's reservation ownership policy.

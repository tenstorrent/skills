# Benchmark stage calibration

Stage 11 is the final model-bringup stage. It evaluates accuracy with fixed subsets
of standard lm-evaluation-harness tasks and measures vLLM serving performance.

See [benchmark-model](../plugins/tt-model-bringup/skills/benchmark-model/SKILL.md)
and its [run contract](../plugins/tt-model-bringup/skills/benchmark-model/references/run-contract.md)
for installation, commands, evidence requirements and the frozen profiles.
The stage attaches to the working optimized-vLLM server, runs accuracy at
concurrency 32, and measures 4096-input/128-output-token serving at concurrency 1
and 32. The one-hour budget includes task verification, inference, scoring,
server profile switching, performance warmups and reporting; initial environment setup
and model startup are separate. The single-user row uses a one-slot server; the
32-user row uses a 32-slot server. Both use the same precision and full context
capacity. The calibration measurements below used one 32-slot server for both
concurrency settings. They do not measure the dedicated single-user profile or
establish the two-profile stage runtime.

The packaged profiles cover MMLU-Pro, GSM8K and IFEval for non-reasoning models,
and MMLU-Pro plus GPQA Diamond for reasoning evaluations. Select the upstream task
variants and generation settings that match the model's published evaluation
recipe. The calibrations below establish measured coverage, runtime and comparison
limits for these profiles.

## T3K calibration, 21 September 2026

The same content-identified documents were used across three full tt_transformers
models in accuracy mode: 280/12,032 MMLU-Pro, 256/1,319 GSM8K and 256/541 IFEval.
MMLU sampling is proportional across subjects. Subsets use a deterministic hash
order independent of scores. These are subset measurements, not full-dataset scores.

Scores are **measured subset / published full-set percent**.

| Model | Chips | Complete stage | MMLU-Pro | GSM8K | IFEval |
|---|---:|---:|---:|---:|---:|
| Llama 3.1 8B Instruct | 8 | 9m12s | 46.07 / 48.3 | 86.72 / 84.5 | 83.54 / 80.4 |
| Llama 3.2 3B Instruct | 8 | 6m48s | 32.73 / — | 70.31 / 77.7 | 78.74 / 77.4 |
| Qwen2.5 7B Instruct | 4 | 25m41s | 41.07 / 56.3 | 46.48 / 91.6 | 60.94 / 71.2 |

Llama uses Meta-aligned upstream tasks, subject-macro MMLU, strict GSM extraction,
and the mean of four IFEval metrics. Qwen uses the generic upstream tasks, pooled
MMLU, flexible GSM extraction and strict-prompt IFEval. Its exact published prompt
recipe was not established. Do not compare different aggregations across rows.
Sources: [Meta 3.1](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct),
[Meta 3.2](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct),
[Qwen2.5](https://qwenlm.github.io/blog/qwen2.5-llm/).

Meta's public per-question results allow a direct subset-difficulty check. The
frozen subset scores differ from full-set scores by -4.28 pp on Llama 3.1 MMLU,
-0.86/-0.76 pp on the two GSM controls, and +2.19/+3.32 pp on IFEval. The MMLU
subset is somewhat harder and IFEval somewhat easier. This supports a coarse
bringup check, not a universal two-point threshold. Rescoring saved reference
outputs with the same parser leaves a 5.47 pp GSM deficit for Llama 3.2.
Sources: [3.1 eval records](https://huggingface.co/datasets/meta-llama/Llama-3.1-8B-Instruct-evals),
[3.2 eval records](https://huggingface.co/datasets/meta-llama/Llama-3.2-3B-Instruct-evals).

| Model | Output tokens/s, concurrency 1 | Output tokens/s, concurrency 32 |
|---|---:|---:|
| Llama 3.1 8B | 41.2 | 215.3 |
| Llama 3.2 3B | 57.3 | 327.9 |
| Qwen2.5 7B | 19.0 | 116.7 |

Every warmup and measured performance request returned 4096 input and 128 output
tokens. Median inter-token latency increased 16–21% at concurrency 32; mean TPOT
rose substantially more under mixed prefill/decode continuous batching. These are
serving measurements, not fixed physical-batch kernel benchmarks.

Runtime is acceptable on all controls; accuracy is not uniformly accepted. Qwen's
gaps and Llama 3.2's GSM gap remain unresolved. A separately selected 32-question
Llama 3.1 replay also changed three answers between identical concurrency-32
phases, all changing correctness. That selected diagnostic is not a benchmark
score; phase order confounds concurrency with server history and no backend cause
was established. Execution success must remain separate from accuracy acceptance.

Calibration stack: tt-metal v0.79.0 (`de546d3b146758714d900f11b218c8f9c805f410`),
vllm-tt-plugin `1799d6ed2780f8ef05c166fca7c40fee555bedb0`, vLLM 0.26.0 and
lm-eval 0.4.13. A KV-capacity-only pool adjustment and larger trace allocation were
needed; model math and kernels were unchanged. The packaged generic and Meta
profiles record evaluation-document and few-shot hashes, full-population identity
and source manifest identity. Calibration coverage is limited to dense text models.

## QB2 Llama 3.1 8B calibration

The calibration used `models/demos/llama31_8b_qb2` at
`8da445c5cf7fe6e0369e744079ea9260ed78fb04` with the
[RMSNorm prefill-tail fix](https://github.com/tenstorrent/tt-metal/pull/57336).
All 792 accuracy requests and both performance rows completed in **9m19s**.
The 1,214-token regression case passed with HF PCC 0.998396 for prefill and
0.998542 for decode.

| Benchmark | Samples / full | QB2 subset % | Published full % | Sampling-only 95% interval |
|---|---:|---:|---:|---:|
| MMLU-Pro, subject macro | 280 / 12,032 | 45.71 | 48.3 | 39.87–51.56 |
| GSM8K, strict extraction | 256 / 1,319 | 83.20 | 84.5 | 78.99–87.06 |
| IFEval, mean of four | 256 / 541 | 80.99 | 80.4 | 77.77–84.05 |

The QB2 and T3K controls use the same Meta profile and generation limits.
Published figures are from the [Meta model card](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct).
Aggregate agreement supports a coarse bringup check. It does not establish output
quality on every question: 38 MMLU answers exhausted the 1,024-token cap and scored
zero; repetitive answers also occur. T3K counts were similar, but a backend
cause was not established. The intervals describe subset sampling only.

| Serving metric, 4096 input / 128 output tokens | Concurrency 1 | Concurrency 32 |
|---|---:|---:|
| Aggregate output tokens/s | 86.94 | 388.94 |
| Mean time to first token, ms | 248.88 | 7,448.47 |
| Mean time per output token, ms | 9.63 | 17.55 |
| Median inter-token latency, ms | 9.66 | 16.05 |
| p95 inter-token latency, ms | 9.76 | 17.99 |
| Median end-to-end latency, ms | 1,472.85 | 9,445.35 |

The concurrency-32 run includes one 31.56-second request and a 9.97-second
inter-token gap. The raw client's peak-concurrency field is incorrect (63);
reconstructing request intervals confirms 32. The table preserves its effect on
aggregate throughput and mean latency.

QB2 uses four Blackhole devices across two P300 cards. All 32 layers ran with the
specialized implementation's shipped fixed precision policy; it has no generic
accuracy-mode switch. Native CI build `9b04e73a4ee2a30c2ee5a8694d63d4450234984d`,
companion vllm-tt-plugin `ce08904469f1ce1b524008b350cbb856c79d3a61`, vLLM 0.26.0
and lm-eval 0.4.13 were used. Setup, loading and initial compilation are outside
the client-stage timer.


## QB2 Gemma 4 31B calibration

The calibration used the [Gemma PR 56765](https://github.com/tenstorrent/tt-metal/pull/56765)
implementation at `dcfa5e2da087432c337a70ac92f8eeeb3bb97f55`, with all 60 layers
and its shipped precision policy, on the same four-device QB2 and native serving
stack as Llama. Accuracy uses the native thinking template, temperature 1,
top-p 0.95, top-k 20 and a 32,768-token cap. The device-supported top-k 20 differs
from Google's general top-k 64 recommendation. The exact publisher recipe is not
fully specified.

The calibration used the question selection in `ci-v1-reasoning.json`: 280 MMLU
questions and 128 GPQA Diamond questions. It completed the stage in **51m02s**
(3,062.25 seconds), including verification, shared accuracy, scoring, performance warmups, both
performance rows and generated reporting.

| Completed timed subset | Samples / full | Measured % | Published full % | Sampling-only 95% interval |
|---|---:|---:|---:|---:|
| MMLU-Pro, pooled | 280 / 12,032 | 86.79 | 85.2 | 82.90–90.32 |
| GPQA Diamond, upstream flexible extraction | 128 / 198 | 84.38 | 84.3 | 80.65–88.10 |

References: [Google model card](https://ai.google.dev/gemma/docs/core/model_card_4).

All 408 responses exactly match the corresponding full-GPQA control's final content,
reasoning, token usage and finish reason, with distinct response IDs. The subset
contains one capped empty answer in each task, both repetitive failures.
The sampling intervals exclude protocol, stochastic and implementation uncertainty;
aggregate agreement does not resolve those output defects.

### Full-GPQA control and subset difficulty

`ci-v1-reasoning-full.json` contains all 198 GPQA Diamond questions. This profile
completed its 478 accuracy requests in **58m37s**, but **exceeded the 60-minute
stage deadline during concurrency-32 performance warmup**.

| Benchmark | Samples / full | Measured % | Published full % |
|---|---:|---:|---:|
| MMLU-Pro, pooled | 280 / 12,032 | 86.79 | 85.2 |
| GPQA Diamond, upstream flexible extraction | 198 / 198 | 81.82 | 84.3 |

The 128-question subset uses a deterministic hash order frozen before score
inspection. Within the full-GPQA control it scores 108/128 (84.38%), **2.56 percentage
points above** the full 81.82%. In a second TT control it scores 113/128 (88.28%),
**3.94 points above** the full 84.34%. These within-run comparisons use TT outputs,
not independent reference inference. They indicate an easier subset; account for
this bias when comparing its score with the published 84.3%.

Three full-run responses reach the cap with repetitive degenerate reasoning and
no final answer: one MMLU and two GPQA. All remain scored as failures. The upstream
GPQA flexible extractor also takes the last parenthesized uppercase letter, which
can select a rejected option or a chemical stereochemistry marker. Raw review
found three explicitly correct answers lost to this rule, plus one label error
that does not change correctness. **The headline retains the upstream 162/198
score**, without manual corrections. Its separate strict metric is zero because
it requires an answer format that the prompt does not request; it is not the
published-score comparison. Neither the cause of repetitive outputs nor exact
publisher equivalence is established.

### Protocol and serving performance

Gemma's calibrated task selection is MMLU-Pro and GPQA Diamond. IFEval is available
for a separate diagnostic; Google's reported figure does not identify which of
its four aggregations was used. Both selected tasks use `until: []` to preserve
native reasoning, with generation ending at native EOS or the declared token cap.
GPQA uses upstream prompts/scorers with the client's deterministic seed-0 choice
ordering. One shared pool of 32 requests avoids separate idle tails between tasks.


| Gemma serving metric, 32-slot server, 4096 input / 128 output tokens | Concurrency 1 | Concurrency 32 |
|---|---:|---:|
| Aggregate output tokens/s | 23.24 | 170.28 |
| Mean time to first token, ms | 679.11 | 18,476.77 |
| Mean time per output token, ms | 38.02 | 43.90 |
| Decode tokens/s/user, from mean TPOT | 26.30 | 22.78 |
| Median inter-token latency, ms | 38.04 | 44.32 |
| p95 inter-token latency, ms | 38.48 | 45.50 |
| Median end-to-end latency, ms | 5,504.60 | 24,044.41 |

Every warmup and measured request returned exactly 4096 input and 128 output
tokens: 8 measured requests at concurrency 1 and 96 at concurrency 32, with 1/32
warmup requests respectively. The same server and fixed precision policy were
used for accuracy and performance. Prefix caching remained disabled. Concurrency
32 raises aggregate throughput 7.33×, while mean TPOT increases 15.5%; prefill and
admission contribute to much higher TTFT. These are serving workloads, not fixed
physical-batch kernel timings.

The three-task Meta/generic profiles and the two-task 280-MMLU/128-GPQA reasoning
profile completed within an hour on these controls. Applying a profile to another
model requires checking its published recipe and reviewing raw output failures
alongside aggregate scores. The final stage reports measured results and available
references; the bringup owner decides whether the model meets their accuracy needs.

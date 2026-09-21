# Model bringup benchmark stage

Stage 11 replaces TTI release with pinned upstream lm-evaluation-harness tasks and
vLLM serving performance. TTI remains an optional standalone `tti-release` skill;
its artifact checker now lives beside the skill. The multigoal sequence still has
11 stages.

See [benchmark-model](../plugins/tt-model-bringup/skills/benchmark-model/SKILL.md)
and its [run contract](../plugins/tt-model-bringup/skills/benchmark-model/references/run-contract.md)
for installation, commands, evidence requirements and the two frozen profiles.
The stage attaches to the working optimized-vLLM server, runs accuracy at
concurrency 32, and measures 4096-input/128-output-token serving at concurrency 1
and 32. The one-hour budget includes task verification, inference, scoring,
performance warmups and reporting; environment setup and model startup are separate.

## Initial calibration, 21 September 2026

The same content-identified documents were used across three full tt_transformers
models in accuracy mode: 280/12,032 MMLU-Pro, 256/1,319 GSM8K and 256/541 IFEval.
MMLU sampling is proportional across subjects. Counts expanded deterministically
after a runtime pilot; questions were not selected to improve score agreement.
These are subset measurements, not new full-dataset scores.

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

Meta's public per-question results allow a direct subset-difficulty check. Original
subset scores differ from full-set scores by -4.28 pp on Llama 3.1 MMLU,
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
serving measurements, not fixed physical-batch kernel benchmarks. No MoE was tested.

Runtime is acceptable on all controls; accuracy is not uniformly accepted. Qwen's
gaps and Llama 3.2's GSM gap remain unresolved. A separately selected 32-question
Llama 3.1 replay also changed three answers between identical concurrency-32
phases, all changing correctness. That selected diagnostic is not a benchmark
score; phase order confounds concurrency with server history and no backend cause
was established. Execution success must remain separate from accuracy acceptance.

Calibration stack: tt-metal v0.79.0 (`de546d3b146758714d900f11b218c8f9c805f410`),
vllm-tt-plugin `1799d6ed2780f8ef05c166fca7c40fee555bedb0`, vLLM 0.26.0 and
lm-eval 0.4.13. A KV-capacity-only pool adjustment and larger trace allocation were
needed; model math and kernels were unchanged. Generic and Meta profile manifests
are packaged with the stage. GPQA, MoE, reasoning and multimodal profiles still
require separate calibration.

## QB2 Llama 3.1 8B calibration

The requested specialized `models/demos/llama31_8b_qb2` implementation completed
all 792 accuracy requests and both performance rows in **9m19s**, after a narrow
local model fix. Unmodified main (`8da445c5cf7fe6e0369e744079ea9260ed78fb04`)
reproducibly fails a 1,214-token prompt with an RMSNorm L1 allocation collision.
Routing tails above 128 rows through its existing interleaved normalization path
passed the isolated regression (HF PCC 0.998396 prefill, 0.998542 decode) and the
full serving run. These results describe that patched model; the skills PR does
not change tt-metal.

| Benchmark | Samples / full | QB2 subset % | Published full % | Sampling-only 95% interval |
|---|---:|---:|---:|---:|
| MMLU-Pro, subject macro | 280 / 12,032 | 45.71 | 48.3 | 39.87–51.56 |
| GSM8K, strict extraction | 256 / 1,319 | 83.20 | 84.5 | 78.99–87.06 |
| IFEval, mean of four | 256 / 541 | 80.99 | 80.4 | 77.77–84.05 |

The same Meta profile and generation limits used in the T3K control were retained.
Published figures are from the [Meta model card](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct).
Aggregate agreement supports a coarse bringup check. It does not establish output
quality on every question: 38 MMLU answers exhausted the 1,024-token cap and scored
zero; repetitive answers also occur. Prior T3K counts were similar, but a backend
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
aggregate throughput and mean latency. A 32-sized tile does not make these
continuous-batching serving latencies equal between concurrency settings.

QB2 uses four Blackhole devices across two P300 cards. All 32 layers ran with the
specialized implementation's shipped fixed precision policy; it has no generic
accuracy-mode switch. Native CI build `9b04e73a4ee2a30c2ee5a8694d63d4450234984d`,
companion vllm-tt-plugin `ce08904469f1ce1b524008b350cbb856c79d3a61`, vLLM 0.26.0
and lm-eval 0.4.13 were used. Setup, loading and initial compilation are outside
the client-stage timer. Gemma 4 31B QB2 calibration remains in progress.

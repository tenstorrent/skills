# Benchmark stage calibration

Stage 11 is the final model-bringup stage. It evaluates accuracy with fixed subsets
of standard lm-evaluation-harness tasks and measures vLLM serving performance.
The report presents results and published references; the bringup owner decides
whether the model meets their needs.

See [benchmark-model](../plugins/tt-model-bringup/skills/benchmark-model/SKILL.md)
and its [run contract](../plugins/tt-model-bringup/skills/benchmark-model/references/run-contract.md)
for installation, commands, evidence requirements and frozen profiles.
Accuracy runs at concurrency 32. Performance uses 4096 input and 128 output tokens
with two server profiles: one slot for one user, and 32 slots for 32 users.
Both profiles preserve precision, checkpoint, hardware and full context capacity.
The one-hour stage budget includes task verification, inference, scoring, server
profile switching, performance warmups and reporting. Initial environment setup
and model startup are separate. Completion requires full-phase prefill FLOP and
decode DRAM accounting for both profiles; the collector runs before each server
is stopped.

## QB2 validation, 23 September 2026

Both models completed the stage on a four-chip Blackhole QB2. Each run generated
new answers for the fixed subsets and measured both server profiles. The audit
checked document identities, response counts, scoring inputs, performance token
lengths and observed server configurations. These measurements used client
version 0.1.10 and did not capture phase accounting. They validate the accuracy
and serving profiles; a hardware run including required phase collection remains
to be measured.

| Model | Accuracy subset coverage | Complete stage | One-slot reload, included |
|---|---|---:|---:|
| Llama 3.1 8B Instruct | MMLU-Pro 280/12,032; GSM8K 256/1,319; IFEval 256/541 | **10m45s** | 86.6s |
| Gemma 4 31B IT | MMLU-Pro 280/12,032; GPQA Diamond 128/198 | **56m18s** | 230.6s |

### Serving performance

All warmup and measured requests returned exactly 4096 input and 128 output
tokens. Each profile used one warmup wave, followed by eight measured requests
for one user or 96 for 32 users. Prefix caching was disabled.

| Model | Profile | Concurrent requests | Server slots | Mean TTFT ms | Mean TPOT ms | Decode tokens/s/user | Aggregate output tokens/s |
|---|---|---:|---:|---:|---:|---:|---:|
| Llama 3.1 8B Instruct | Single user | 1 | 1 | 240.66 | 8.41 | 118.84 | 97.75 |
| Llama 3.1 8B Instruct | 32 users | 32 | 32 | 7250.63 | 16.00 | 62.49 | 406.29 |
| Gemma 4 31B IT | Single user | 1 | 1 | 584.02 | 22.77 | 43.92 | 36.83 |
| Gemma 4 31B IT | 32 users | 32 | 32 | 18090.30 | 39.42 | 25.37 | 177.33 |

Decode tokens/s/user is 1,000 divided by mean time per output token in milliseconds.
Aggregate throughput includes prompt processing and request admission. HTTP
concurrency does not imply a fixed physical device batch.
Full-phase prefill FLOP and decode DRAM roofline percentages are unavailable:
these runs did not capture the required phase timing and matching work accounting.
The generated reports show these fields as —.

Separate 128-input/128-output-token checks compare the one-slot configuration with
short-context implementation measurements. Each used one warmup and eight measured
requests after the complete stage; they are outside its timer.

| Model | Current 4K-input single user | Current 128-input single user | Earlier short-context measurement |
|---|---:|---:|---:|
| Llama 3.1 8B Instruct | 118.84 | 130.59 | [130.72](https://github.com/tenstorrent/tt-metal/actions/runs/35722211339) |
| Gemma 4 31B IT | 43.92 | 46.45 | [40.37](https://github.com/tenstorrent/tt-metal/blob/dcfa5e2da087432c337a70ac92f8eeeb3bb97f55/models/demos/gemma4_31b_qb2/README.md#measured-serving) |

All values in this comparison are decode tokens/s/user. The Gemma reference uses
128 input and 128 output tokens on a one-slot server. The Llama CI reference uses
29–106-token inputs with variable output lengths and one request at a time on a
32-slot server; it is a throughput sanity check, not a matched 128/128 comparison.
The references also ran on different hosts. Input lengths, server capacity and
measurement protocols must be considered when comparing performance figures.

### Accuracy subsets

The question selection is fixed by content hashes, independent of scores.
Published figures cover full datasets; these measurements cover only the counts
shown below. No full-dataset run is included in this validation.

| Model | Benchmark | Samples / full | Measured % | Published full % | Difference pp |
|---|---|---:|---:|---:|---:|
| Llama 3.1 8B Instruct | MMLU-Pro, subject macro | 280 / 12,032 | 45.71 | [48.30](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) | -2.59 |
| Llama 3.1 8B Instruct | GSM8K, strict extraction | 256 / 1,319 | 83.20 | [84.50](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) | -1.30 |
| Llama 3.1 8B Instruct | IFEval, mean of four | 256 / 541 | 81.15 | [80.40](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct) | +0.75 |
| Gemma 4 31B IT | MMLU-Pro, pooled | 280 / 12,032 | 86.79 | [85.20](https://ai.google.dev/gemma/docs/core/model_card_4) | +1.59 |
| Gemma 4 31B IT | GPQA Diamond, flexible extraction | 128 / 198 | 84.38 | [84.30](https://ai.google.dev/gemma/docs/core/model_card_4) | +0.08 |

Llama uses the Meta-aligned upstream tasks with its native chat template and greedy
generation. Token limits are 1,024 for MMLU-Pro/GSM8K and 3,840 for IFEval.
All capped answers remain in the scores: 38 MMLU, three GSM8K and five IFEval.
IFEval's upstream loose scorer uses an unseeded language detector, so its score
can vary slightly even when generated answers are identical.

Gemma uses the native thinking template, temperature 1, top-p 0.95, top-k 20,
`until: []` and a 32,768-token cap. Top-k 20 is supported by the device sampler;
Google's general recommendation is 64. Exact publisher prompt, budget and
aggregation equivalence is not established. Both tasks share one 32-request pool
and use upstream prompts/scorers with deterministic seed-0 GPQA choice ordering.

Gemma retained one capped answer with an empty final response in each task.
All 408 regenerated answers, including reasoning and token usage, match the
previous subset run.

### Implementations and environment

| Model | Source | Layers | Context capacity |
|---|---|---:|---:|
| Llama | `models/demos/llama31_8b_qb2` at `8da445c5cf7fe6e0369e744079ea9260ed78fb04`, with the [RMSNorm prefill-tail fix](https://github.com/tenstorrent/tt-metal/pull/57336) | 32 | 131,072 |
| Gemma | [`models/demos/gemma4_31b_qb2`](https://github.com/tenstorrent/tt-metal/tree/dcfa5e2da087432c337a70ac92f8eeeb3bb97f55/models/demos/gemma4_31b_qb2) at `dcfa5e2da087432c337a70ac92f8eeeb3bb97f55` | 60 | 262,144 |

Both specialized implementations use their shipped fixed precision policies and
four-way tensor parallelism across two P300 cards. Native TTNN build:
`9b04e73a4ee2a30c2ee5a8694d63d4450234984d`; vllm-tt-plugin:
`ce08904469f1ce1b524008b350cbb856c79d3a61`; vLLM 0.26.0; lm-eval 0.4.13.
The client is `tt-model-bringup` 0.1.10 at skills commit
`ddb14590393e7dffe533bc9c5451bd2c963c7390`.

## Subset-difficulty controls

### GPQA Diamond

A separate 21 September Gemma control evaluated all 198 GPQA Diamond questions.
Its score was **162/198 (81.82%)**. The fixed 128-question subset scored
**108/128 (84.38%)** on the same outputs, **2.56 percentage points higher**.
A second TT control scored 88.28% on the subset and 84.34% on the full set,
a **3.94-point difference**. These are within-run TT comparisons, not independent
reference inference, and indicate an easier subset.

The full-GPQA profile completed its 478 accuracy requests in 58m37s and exceeded
the one-hour stage deadline during performance warmup. Its capped, repetitive
responses remained scored as failures. Upstream flexible extraction takes the
last parenthesized uppercase letter; raw review found three explicitly correct
answers lost to this rule. Scores retain the upstream extraction without manual
corrections.

### T3K and published per-question controls, 21 September 2026

Three full tt_transformers models ran in accuracy mode with the same
content-identified 280 MMLU-Pro, 256 GSM8K and 256 IFEval documents. Scores below
are **measured subset / published full-set percent**. These earlier runs used one
32-slot server for both performance concurrency settings, so their runtimes do
not validate the dedicated one-slot server switch.

| Model | Chips | Stage wall time | MMLU-Pro | GSM8K | IFEval |
|---|---:|---:|---:|---:|---:|
| Llama 3.1 8B Instruct | 8 | 9m12s | 46.07 / 48.3 | 86.72 / 84.5 | 83.54 / 80.4 |
| Llama 3.2 3B Instruct | 8 | 6m48s | 32.73 / — | 70.31 / 77.7 | 78.74 / 77.4 |
| Qwen2.5 7B Instruct | 4 | 25m41s | 41.07 / 56.3 | 46.48 / 91.6 | 60.94 / 71.2 |

Llama uses subject-macro MMLU, strict GSM extraction and the mean of four IFEval
metrics. Qwen uses generic upstream tasks, pooled MMLU, flexible GSM extraction
and strict-prompt IFEval; its exact published prompt recipe was not established.
Different aggregations are not directly comparable. References:
[Meta 3.1](https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct),
[Meta 3.2](https://huggingface.co/meta-llama/Llama-3.2-3B-Instruct),
[Qwen2.5](https://qwenlm.github.io/blog/qwen2.5-llm/).

Meta's public per-question records allow direct subset-difficulty checks. The
frozen subset differs from the full set by -4.28 pp on Llama 3.1 MMLU,
-0.86/-0.76 pp on the two GSM controls, and +2.19/+3.32 pp on IFEval. Thus MMLU
is somewhat harder and IFEval somewhat easier for these controls. Rescoring saved
reference outputs with the same parser leaves a 5.47 pp GSM deficit for Llama 3.2.
Sources: [3.1 eval records](https://huggingface.co/datasets/meta-llama/Llama-3.1-8B-Instruct-evals),
[3.2 eval records](https://huggingface.co/datasets/meta-llama/Llama-3.2-3B-Instruct-evals).

Qwen's score gaps and Llama 3.2's GSM gap remain unresolved. A separately selected
32-question Llama 3.1 diagnostic changed three answers between identical
concurrency-32 phases, all changing correctness. Phase order confounds concurrency
with server history; no backend cause was established. This diagnostic is not a
benchmark score.

T3K stack: tt-metal v0.79.0 (`de546d3b146758714d900f11b218c8f9c805f410`),
vllm-tt-plugin `1799d6ed2780f8ef05c166fca7c40fee555bedb0`, vLLM 0.26.0 and
lm-eval 0.4.13. A KV-capacity-only pool adjustment and larger trace allocation were
needed; model math and kernels were unchanged. Calibration coverage is limited
to dense text models.

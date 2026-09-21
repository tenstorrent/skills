# Benchmark stage run contract

Client dependencies are separate from the TT server environment. Install explicitly into a client venv:

```bash
uv pip install --python "$EVAL_PYTHON" 'lm-eval[api,ifeval]==0.4.13' 'transformers<5'
"$EVAL_PYTHON" -m nltk.downloader punkt_tab
export PYTHONPATH="$TT_MODEL_BRINGUP_ROOT/runtime${PYTHONPATH:+:$PYTHONPATH}"
```

Dataset access uses the operator's existing Hugging Face credentials. Keep credentials out of copied commands and results. Dataset downloads and client provisioning are setup, performed before the stage; dataset verification within the runner counts toward stage time.

Before launching, print `benchmark_stage.__file__` with the selected client Python
from the actual launch directory and verify that it is inside this plugin's
`runtime/benchmark_stage`. Python's working directory can shadow `PYTHONPATH` with
an older copied package; do not assume exporting the path selects the intended code.

The CI profile contains 280 MMLU-Pro questions (proportional subject allocation), 256 GSM8K-CoT questions and 256 IFEval prompts. Its original measured scope is non-reasoning dense controls; retain the calibration report's score and protocol limitations. For the Gemma 4 QB2 reasoning calibration, use the separate frozen profile described below. GPQA Diamond can replace GSM8K when that is the model's published evaluation. Neither profile has been calibrated on MoE models.

For `gpqa_diamond_cot_zeroshot`, the client preserves the upstream prompt and scorer but makes answer-choice shuffling deterministic with a private seed-0 RNG and recomputes that transform. Upstream 0.4.13 uses a global RNG whose state is absent from the dataset transform cache key. The manifest records this processing policy and evaluation rejects a different policy. Freeze a new GPQA manifest with this client; do not reuse a manifest prepared with the upstream cache-dependent shuffle.

Freeze once, before observing scores:

```bash
"$EVAL_PYTHON" -m benchmark_stage prepare \
  --tasks mmlu_pro,gsm8k_cot,ifeval --counts 280,256,256 \
  --output "$BENCHMARK_ROOT/subset"
```

For the pilot tasks, prefer the packaged `runtime/benchmark_stage/profiles/ci-v1.json`
manifest. Its `reused_manifest_sha256` retains the original calibration manifest identity; the later freeze adds few-shot hashes without changing any evaluation questions. `prepare` creates a new candidate profile. A benchmark name alone is
not an exact recipe: for example, upstream `gsm8k_cot_llama` documents Meta's
published prompt, while `gsm8k_cot` is the generic recipe. Choose the publisher's
supported recipe for that model; do not apply a Llama-specific recipe to other
families by default. It stores task-local indices,
content hashes, few-shot hashes and full-population hashes. When expanding a profile,
verify every prior per-task index remains selected; subject apportionment may round
differently at a new total. Use the resulting manifest unchanged on each model. Dataset revision drift fails closed. Archive the manifest with the release evidence; do not recompute indices for an already published profile.

To select an upstream recipe variant without changing the questions, use
`prepare --reuse-manifest`. It requires identical evaluation-document populations
and sample counts, carries the source manifest hash, and freezes the new recipe's
few-shot examples independently:

```bash
"$EVAL_PYTHON" -m benchmark_stage prepare \
  --tasks mmlu_pro_llama,gsm8k_cot_llama,ifeval --counts 280,256,256 \
  --reuse-manifest "$BENCHMARK_ROOT/subset/manifest.json" \
  --output "$BENCHMARK_ROOT/subset-meta"
```

Use the new manifest and exact variant task name in the run configuration. Native
chat serialization remains the checkpoint's own template. This option does not
claim that every other part of the publisher's protocol matches. If the publisher
provides per-question evaluation records, compare their full-set and selected-set
scores directly as an additional subset-difficulty check; never use those records
to choose better-matching questions.

The packaged `profiles/ci-v1-meta.json` contains the same questions with the
Meta-aligned recipes already frozen. For Llama 3.1/3.2 calibration, use
`mmlu_pro_llama`, `gsm8k_cot_llama`, and `ifeval`, with respective generation caps
1024, 1024, and 3840 at temperature zero, as recorded in Meta's official evaluation
records. Select `subject_macro:exact_match,strict_match` for Meta MMLU-Pro,
`exact_match,strict-match` as the predeclared primary GSM metric, and
`ifeval_mean_four`. Preserve flexible GSM extraction as a secondary result. Neither
GSM filter exactly reproduces Meta's numeric normalization; compare against saved
official outputs rescored with the same filter when available.

The generic example below is a different protocol. Do not use its results as
publisher-equivalent just because the task names refer to the same benchmarks.

Create a run configuration JSON with the following fields:

```json
{
  "model": "organization/model",
  "base_url": "http://127.0.0.1:8000",
  "manifest": "/operator-selected/subset/manifest.json",
  "tasks": ["mmlu_pro", "gsm8k_cot", "ifeval"],
  "vllm_cli": "/operator-selected/serve-env/bin/vllm",
  "budget_seconds": 3600,
  "output_tokens": 128,
  "generation": {
    "mmlu_pro": {"max_gen_toks": 4096, "temperature": 0},
    "gsm8k_cot": {"max_gen_toks": 4096, "temperature": 0},
    "ifeval": {"max_gen_toks": 4096, "temperature": 0}
  }
}
```

Then run:

```bash
"$EVAL_PYTHON" -m benchmark_stage run \
  --config "$BENCHMARK_ROOT/run_config.json" \
  --output "$MODEL_DIR/doc/benchmark/run"
```

The output directory must be new. Each task runs in a child process with concurrency 32. Existing response caches are not used. The HTTP timeout allows an individual long reasoning answer up to one hour; the parent runner still terminates the whole stage at its remaining wall-clock budget. Accuracy budgets come from the upstream task, with a 2048-token backend default where upstream omits one; override explicitly when the model's intended evaluation needs a larger budget. `generation` forwards upstream-supported options including `chat_template_kwargs`. Record all overrides and compare them with the reference protocol. For reasoning models, inspect upstream textual stop strings before the full run: MMLU-Pro's `Question:` can occur inside native reasoning. When it stops reasoning before a final answer, record an explicit `until: []` override to rely on native EOS and the declared token cap; preserve the failed attempt and rerun all frozen questions. Verify the API returns the final answer separately from reasoning.

Use the upstream vLLM 0.26 performance client (the calibration uses its empty
build with vllm-tt-plugin). This is a client requirement; do not replace a working
Stage 10 server simply to obtain the benchmark CLI. Older fork clients do not consume server prompt-token
usage, and random-range-ratio semantics changed. The runner uses ratio 0 for fixed
lengths and checks each returned input/output token count.

The watchdog covers client subprocesses and writes a failed summary if any command fails or the deadline expires. Performance uses the explicitly selected vLLM client CLI; it may be in a separate
client environment when the working server uses an older fork. Check its supported
flags and server usage-token reporting before expensive accuracy runs. The server remains owned by the caller. For a larger generation budget or another profile, change the configuration and rerun into a new directory; never edit a completed result to pretend the original run passed.

Evidence alongside `run/`:

- `manifest.json`: identical to the manifest used for evaluation.
- `identity.json`: model ID, actual implementation path and imported generator, model/tokenizer revisions, precision policy, full-layer count, server configuration/command, source commits, device topology and prefix-cache setting.
- `REPORT.md`: per-task samples/full size, upstream metric names, subset/full reference scores, deltas, uncertainty, protocol comparability and observed failures; performance rows and elapsed time.
- `RUN_NOTES.md`: environment, exact commands, setup versus timed-stage wall times, recovery and artifact locations.
- `accuracy_review.json`: verdict (`pass` or `fail`) plus one assessment per task with exact reference metric, source, score, subset score, observed delta and explanation. A successful harness process alone is not an accuracy pass.

References: [lm-evaluation-harness v0.4.13](https://github.com/EleutherAI/lm-evaluation-harness/tree/v0.4.13), [vLLM benchmark CLI](https://docs.vllm.ai/en/latest/benchmarking/cli/). The serving API's native chat template and model-specific generation protocol take precedence over a generic family preset.

Identity field names checked by the gate: `model`, `implementation`, `generator_module`,
`model_revision`, `tokenizer_revision`, `precision`, `layer_count`,
`configured_layer_count`, `source_commits`, `hardware`, `server_command`,
`prefix_caching`. Both layer counts must match. Preserve the imported file and
server log alongside this record. For each accuracy review, `reference_metric`
is the exact upstream metric key; `subject_macro:<metric>` computes an unweighted mean of the explicitly listed child-task scores, needed for Meta MMLU-Pro even though the upstream group itself micro-averages; `ifeval_mean_four` selects Meta’s published
mean of the four IFEval metrics. Scores and deltas are percentage points. Any
length-limited response requires an explicit `truncation_assessment` or a rerun.


A valid reasoning response that reaches its token limit without a final answer
remains in the scored denominator as an empty answer. The client preserves the
raw response, never grades hidden reasoning, and records
`empty_final_length_responses`. The evidence gate reconciles that count and
requires the same explicit truncation assessment as other length-limited outputs.
Empty answers with a normal stop or malformed API responses still fail the run.

For long reasoning runs, set `accuracy_execution` to `shared` when all selected
tasks use identical generation overrides. The client makes one upstream
`simple_evaluate` call with one pool of 32 requests, so a long answer in one task
does not leave the other tasks waiting with idle serving slots. Keep separate
scores, sample IDs and raw responses for every task. Shared accuracy durations
refer to the same wall-clock interval and must not be added together.

Provide the same complete `generation` dictionary for each selected task,
including `do_sample`, `until`, token budget and native thinking settings. The
client checks the effective upstream generation dictionaries before sending any
request; different settings require the default `sequential` mode. Upstream's API
backend discards `do_sample` and uses `temperature` for sampling, but the explicit
flag prevents different task defaults from splitting its request pool. Do not
change a publisher's generation protocol merely to make tasks share a pool.

Shared mode requires unique requests with one generation per question. It writes
`request_links.jsonl` beside each raw transcript. The gate reconciles each link's
request hash, dataset ID, response ID and scored final answer. Duplicate or
ambiguous requests fail before inference. A deadline still fails the complete
stage; partial raw transcripts are diagnostic evidence only.


## Gemma 4 QB2 reasoning profile

`profiles/ci-v1-reasoning.json` preserves the same 280 MMLU-Pro and 256 IFEval
questions and adds all 198 GPQA Diamond questions. Its manifest hash is
`83203b75b253a2dff70c9bac96c257fe784981a714c767b61f15fdefb707598b`.
The full GPQA set was frozen before inference: a 64-question candidate was
6.28 percentage points easier than the full set in a historical result, so the
candidate was expanded without searching for a better-matching seed.

The timed Gemma candidate selects only `mmlu_pro` and
`gpqa_diamond_cot_zeroshot`, with `accuracy_execution: "shared"`. IFEval remains
available in the manifest for a separate diagnostic; it is not part of that timed
candidate. Google's reported IFEval figure does not identify which of its four
aggregations was used.

Use the following identical generation dictionary for both selected tasks when
reproducing this calibration:

```json
{
  "max_gen_toks": 32768,
  "temperature": 1,
  "top_p": 0.95,
  "top_k": 20,
  "chat_template_kwargs": {"enable_thinking": true},
  "until": [],
  "do_sample": true
}
```

This uses the Gemma PR's device-supported sampler. Top-k 20 differs from Google's
general top-k 64 recommendation; it is a recorded calibration protocol, not a
universal default or an exact reproduction of the published recipe. Preserve
native reasoning/final-answer separation. Compare `exact_match,custom-extract`
for pooled MMLU-Pro with 85.2%, and `exact_match,flexible-extract` for GPQA Diamond
with 84.3%, from the [Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4).
The final measured runtime, scores and limitations belong in the calibration
report; the existence of a frozen profile alone does not establish a passing run.

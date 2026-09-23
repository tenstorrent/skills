# Benchmark stage run contract

## Client setup

Use a client environment separate from the working TT server:

```bash
uv pip install --python "$EVAL_PYTHON" 'lm-eval[api,ifeval]==0.4.13' 'transformers<5'
"$EVAL_PYTHON" -m nltk.downloader punkt_tab
export PYTHONPATH="$TT_MODEL_BRINGUP_ROOT/runtime${PYTHONPATH:+:$PYTHONPATH}"
```

Use the operator's existing Hugging Face credentials for datasets. Downloads and
client provisioning happen before the timed stage. Dataset verification is timed.
Check `benchmark_stage.__file__` from the launch directory: it must resolve inside
this plugin's `runtime/benchmark_stage`, since the working directory can shadow
`PYTHONPATH`.

For performance, select a working upstream **vLLM 0.26** client with
`vllm bench serve --help`. Set `vllm_cli` to its executable. An older TT server can
stay running while a separate client sends requests. If no compatible client is
installed, use a dedicated venv and the [vLLM installation instructions](https://docs.vllm.ai/en/v0.26.0/getting_started/installation/gpu/).
For a host without GPU build tools, the documented `VLLM_TARGET_DEVICE=empty`
source installation skips accelerator binaries; this client sends HTTP requests
and does not run the model. Pin the source to `v0.26.0` and verify the benchmark
CLI starts before running accuracy. The runner uses `--random-range-ratio 0` and
validates each server-reported input/output token count.

## Select questions and protocol

Choose tasks for the model's intended use and prefer published references for its
exact checkpoint. Missing reference figures are allowed. The packaged manifests
under `runtime/benchmark_stage/profiles/` contain:

| Manifest | Available tasks and sample counts |
|---|---|
| `ci-v1.json` | MMLU-Pro 280; GSM8K-CoT 256; IFEval 256 |
| `ci-v1-meta.json` | Same questions, with Meta-specific MMLU-Pro/GSM8K recipes |
| `ci-v1-reasoning.json` | MMLU-Pro 280; GPQA Diamond 128; IFEval 256 |
| `ci-v1-reasoning-full.json` | MMLU-Pro 280; all 198 GPQA Diamond questions; IFEval 256 |

Select only the tasks to run in the configuration. Profile names describe the
available tasks, not generation settings for a model family. Determine chat
formatting, thinking mode, sampling and output budgets from the exact model's
published protocol or intended use. Runtime depends on model speed and generated
length. The full GPQA profile is available for larger evaluations; it is not
necessary for the one-hour stage.

Reuse the same documents on each model. For a new benchmark, freeze a manifest
before observing scores:

```bash
"$EVAL_PYTHON" -m benchmark_stage prepare \
  --tasks mmlu_pro,gsm8k_cot,ifeval --counts 280,256,256 \
  --output "$BENCHMARK_ROOT/subset"
```

To choose another upstream recipe over the same documents, preserve question IDs
and counts with `--reuse-manifest`. This example selects Meta-specific recipes;
use them only when appropriate to the model's protocol:

```bash
"$EVAL_PYTHON" -m benchmark_stage prepare \
  --tasks mmlu_pro_llama,gsm8k_cot_llama,ifeval --counts 280,256,256 \
  --reuse-manifest "$TT_MODEL_BRINGUP_ROOT/runtime/benchmark_stage/profiles/ci-v1.json" \
  --output "$BENCHMARK_ROOT/subset-meta"
```

The manifest freezes document populations, selected content and few-shot examples.
Changed dataset content fails verification. Archive the manifest used by the run.
Do not change questions to obtain closer agreement with published figures.

For `gpqa_diamond_cot_zeroshot`, the client preserves upstream prompts and scoring
and makes choice shuffling deterministic with a private seed-0 RNG. It recomputes
the transform because upstream 0.4.13 caches a shuffle whose RNG state is absent
from the cache key. Use the packaged GPQA manifest or prepare one with this client.

## Run configuration

This model-neutral example uses greedy, non-thinking generation. Set the budgets
and sampling options for the model you are benchmarking:

```json
{
  "model": "organization/model",
  "base_url": "http://127.0.0.1:8000",
  "manifest": "/operator-selected/subset/manifest.json",
  "tasks": ["mmlu_pro", "gsm8k_cot", "ifeval"],
  "vllm_cli": "/operator-selected/client-env/bin/vllm",
  "budget_seconds": 3600,
  "output_tokens": 128,
  "generation": {
    "mmlu_pro": {"max_gen_toks": 4096, "temperature": 0},
    "gsm8k_cot": {"max_gen_toks": 4096, "temperature": 0},
    "ifeval": {"max_gen_toks": 4096, "temperature": 0}
  }
}
```

The `generation` overrides also accept upstream options such as
`chat_template_kwargs`. Check textual stop strings for reasoning models: a
benchmark's `Question:` stop can occur within reasoning. If it cuts off native
reasoning, explicitly use `until: []` and retain the declared token cap and EOS.
Record the setting as a protocol difference. The API must expose final-answer
content separately from reasoning.

Optionally add `metrics` to select headline upstream metric keys and `references`
to attach published figures. Scores are percentages. This schema example uses an
illustrative score and URL; replace them with the exact model's source:

```json
{
  "metrics": {"gsm8k_cot": ["exact_match,strict-match"]},
  "references": {
    "gsm8k_cot": {
      "exact_match,strict-match": {
        "score": 75.0,
        "source_url": "https://example.org/model-card",
        "protocol_notes": "Describe any known difference in prompt, scoring or generation settings."
      }
    }
  }
}
```

With no explicit `metrics`, the report uses reference metric keys when present,
or all upstream score keys otherwise. Raw results always retain all metrics.
`subject_macro:<metric>` computes the unweighted mean of the frozen child tasks;
`ifeval_mean_four` computes the mean of IFEval's four accuracy metrics. Select
these only when they match the reference aggregation. Unknown metrics, invalid
scores or references without source URLs are errors; an absent reference is not.
The report calculates score differences directly. No accuracy verdict is required.

Run into a new directory:

```bash
"$EVAL_PYTHON" -m benchmark_stage run \
  --config "$BENCHMARK_ROOT/run_config.json" \
  --output "$MODEL_DIR/doc/benchmark/run"
```

The runner freezes a copy of the configuration and manifest. Accuracy uses fresh
requests at concurrency 32. A watchdog terminates owned client subprocesses when
the one-hour budget expires and writes a failed summary. Preserve that evidence;
use a new output directory for a rerun. The server remains owned by the caller.

Set `accuracy_execution` to `shared` when every task has the same complete
`generation` dictionary. One request pool avoids separate long tails per task.
Include `do_sample`, `until`, output budget and native thinking settings explicitly;
the client also checks effective upstream defaults. Do not change the intended
protocol to make tasks share a pool. Shared mode retains separate task scores and
request links; its per-task wall times refer to the same interval.

A reasoning response that exhausts its token budget without a final answer is
scored as empty and stays in the denominator. Raw reasoning is retained but never
graded as the final answer. Truncation and empty-final counts appear in the report.
Malformed responses or empty answers with a normal stop fail the run. Preserve
upstream extraction results, including apparent scorer mistakes; document a
protocol limitation without hand-correcting scores.

## Full-phase roofline accounting

The performance table includes estimated prefill FLOP utilization and decode DRAM
bandwidth utilization. For each phase:

`percent = 100 × modeled work / (elapsed phase seconds × hardware peak rate)`

Use useful prefill FLOPs for the actual prompts and a dtype/fidelity-appropriate
peak FLOP/s over all participating chips. Count decode DRAM bytes for weights at
their stored dtypes, KV reads/writes and other material traffic across the actual
steps. Account for tensor/data parallelism, replication, batch sharing and active
MoE experts. Cite the accounting method and hardware peak source.

Time the entire warmed phase at the host boundary, including dispatch, all ops,
communication, sampling/readback and intervening host gaps. For chunked prefill,
include every chunk. Sum non-overlapping phase intervals on one common timeline;
never sum overlapping request latencies or per-device times. Work and elapsed
time must cover the same requests/steps on the same chips. A matmul-only duration
is not a valid denominator. If prefill/decode overlap cannot be separated, record
the missing phase accounting rather than infer it from HTTP concurrency.

The serving client does not supply these server timings or model byte counts.
Use the implementation's timing logs or add lightweight host timing, following
the serving skill's profiler restrictions. An optional `roofline_command` array
in the run configuration invokes an implementation-specific collector after the
performance runs, with `--run-dir <output>` appended. It runs within the stage
budget and writes `roofline.json` and its supporting artifacts in that directory.
For example: `["/client/bin/python", "/model/tools/collect_roofline.py"]`.

The collector output maps concurrency (`"1"`, `"32"`) to entries with:

- `performance_sha256`: SHA-256 of the corresponding `perf-b1.json` or `perf-b32.json` file bytes, binding accounting to this run.
- `prefill`: `flops`, `seconds`, `peak_flops_per_second`.
- `decode`: `dram_bytes`, `seconds`, `peak_dram_bytes_per_second`.

Each phase also records `timing_scope: "full_phase_wall_time"`, `timing_method`,
`work_method`, `peak_source` and `evidence` (a relative path to a retained timing
and accounting artifact). Rates use FLOP/s or bytes/s, not TFLOP/s or GB/s. The
report computes percentages and links the inputs. Omit an unavailable phase;
missing accounting is shown as — and does not block the benchmark report. Record
why it is unavailable in `RUN_NOTES.md`. A collector that fails or writes invalid
accounting fails the run, rather than publishing a misleading percentage.

## Report and retained evidence

```text
doc/benchmark/
  identity.json                 authored from the running server
  RUN_NOTES.md                  commands, setup time, protocol details and limitations
  run/
    REPORT.md                   generated final report: scores, references and performance
    run_config.json             frozen configuration
    manifest.json               frozen subset, with full populations and content hashes
    summary.json                execution status, timing and normalized results
    <task>/                     upstream results, scored samples and raw responses
    perf-b{1,32}*.json           raw performance measurements and warmups
    roofline.json               optional server phase accounting and source artifacts
```

No report or manifest copying is needed. The checker reads the generated report
and manifest under `run/`. It verifies workload identity, complete responses,
scored documents, token counts, timing and reference metric validity. It does not
judge the model's accuracy.

`identity.json` uses these fields (replace illustrative values with observed ones):

```json
{
  "model": "organization/model",
  "implementation": "models/autoports/model",
  "generator_module": "models.autoports.model.generator",
  "model_revision": "checkpoint-commit",
  "tokenizer_revision": "tokenizer-commit",
  "precision": "the server's selected precision policy",
  "layer_count": 32,
  "configured_layer_count": 32,
  "source_commits": {"tt-metal": "commit", "vllm": "commit"},
  "hardware": "observed chip topology",
  "server_command": ["the", "actual", "launch", "command"],
  "prefix_caching": false
}
```

Both layer counts must match. Keep the imported module's file path and server log
with the identity evidence. The standalone skill also supports stock models;
the final bringup checker requires the target autoport implementation.

References: [lm-evaluation-harness v0.4.13](https://github.com/EleutherAI/lm-evaluation-harness/tree/v0.4.13),
[vLLM benchmark CLI](https://docs.vllm.ai/en/v0.26.0/benchmarking/cli/).

---
name: benchmark-model
description: Measure a text model's vLLM serving performance over the configured batch sizes and input lengths, with roofline accounting, and write the serving rates goal 12 reads (stage 11). Optionally score fixed lm-eval subsets when benchmarking a model on its own.
---

# Benchmark a model through vLLM

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Attach to the working Stage 10 server with its selected precision policy and measure it with `vllm bench serve`. In the pipeline, stage 11 is benchmarks only: the run config sets `tasks: []` and goal 12 runs the evals.

The pipeline stage finishes within `benchmark_budget_hours` from `bringup/agentic_evals/inputs.json` (default 3), from the first measurement through reporting, including every server launch, reload, compilation and warmup. The runner (`benchmark_stage run`) keeps its own `budget_seconds`, at most 30 minutes per configured batch. Record setup done before the clock separately, including installation, downloads and the supplied server's startup. Read [the run contract](references/run-contract.md) for setup, profiles, commands and report inputs.

## Measure performance

Measure every supported batch size — `batch_sizes` from `bringup/agentic_evals/inputs.json`, default 1, 8, 16, 32, minus any stage 10 could not validate — at input lengths **4096, `typical_context_tokens`, `working_set_tokens` and at least 28K**, each with **128 output tokens**. Write a predicted rate for each row before measuring it, from stage 10's measurements or the roofline accounting. The runner measures the 4096-token profiles with roofline accounting; measure the longer rows with the same client and the same server configuration per batch. The run config's `concurrencies` is that list: request concurrency is set equal to the batch under test so the device batch is actually filled, which is the only way the profile measures that batch rather than a padded one. Set `--max-num-seqs` to the same value. Use the configuration optimized-vLLM validated for that batch; batch 1 stays the single-user regression row. Each profile must use its corresponding decode trace and cache configuration. Keep model, precision, hardware and full context capacity unchanged. Warm each server configuration, then measure repeated requests. Record the prefix-caching setting, use distinct prompts, request greedy decoding and ignore EOS for performance. Verify actual token counts.

Prepare full-phase accounting before starting the timed run. Use or implement the model's collector and configure the required `roofline_command` from the run contract. Enable lightweight host timing at the actual prefill/decode completion boundaries and derive work from the model's executed shapes, precision and parallelism. Do not enable the live device profiler or add synchronization that changes the serving path. The runner checks the collector on each running server and collects its evidence before switching profiles.

The report labels the profile, concurrent requests and server slots separately. Headline performance includes TTFT, TPOT, per-user decode tokens/s, aggregate output tokens/s, prefill FLOP roofline percentage and decode DRAM bandwidth roofline percentage. **Both roofline estimates are required for every profile.** Their denominators cover complete elapsed phases, including host work and gaps, rather than matmul duration. If timing or work accounting is missing, preserve the measured results and repair the collection; the stage is incomplete. No minimum roofline percentage is required.

Use the run contract's server-control hook to prepare and record the largest configured server first, then step down through the remaining batches. Switching and warmup count toward the stage budget. Preserve ITL, end-to-end latency, percentiles, request throughput, completion counts and wall time in the report details.

## Release benchmark matrix

Choose the benchmark matrix this model's release will run, from the matrices of similar models
already in tt-inference-server: same family or architecture, similar size, same device. Keep the
input and output lengths and concurrencies they use that this model supports, within its validated
batches and context; drop the rest and add none they do not run. Write it to
`doc/benchmark/release_benchmark_matrix.json` with the models it was taken from and, per row, an
estimated time from the profiles measured above. Measure every row on the eval route and record
each in `serving_rates.json` `release_rows`.

## Serving rates for goal 12

Write `doc/benchmark/serving_rates.json`:
the server config, `validated_batches`, `rows` (per batch and `input_tokens`: `prefill_tok_s`,
`decode_tok_s_user`, `ttft_s`, `step_ms`, `predicted`, and a `cause` for a row below its predicted
range), `release_rows` with `route: "eval"`, and `release_source`. Goal 12 reads these rates and
does not re-measure them; `check_agentic_serving.py --stage benchmark` gates the file.

## Accuracy (standalone only)

When this skill benchmarks a model outside the pipeline, it may also score fixed lm-eval subsets, as
the run contract's accuracy section describes: the model's native chat template once, the packaged
frozen subsets, accuracy once at the largest batch, every question in the denominator, truncations
reported, and no accuracy verdict.

## Diagnose slow benchmarks and evals

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

Complete the stage when the selected benchmarks and every performance row, including full-phase prefill FLOP and decode DRAM accounting, have valid, complete evidence, the report is generated, implementation/context checks pass, and elapsed client time is inside the budget. No accuracy acceptance review is required. Use $autofix for execution or integration failures; retain failed-run evidence. Stop only processes owned by this stage and follow the enclosing run's reservation policy.

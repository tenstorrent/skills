---
name: agentic-serving
description: Serve a bringup for the agentic workload it will see (many concurrent multi-turn conversations with long shared prefixes), keep device memory accounted for, measure optimisations soundly, and run the agentic evals (goal 12). Use whenever a stage chooses layouts, precision, memory reserves, batch, cache policy or serving configuration, for stage 11's serving rates, and for goal 12.
---

# Agentic serving

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Stages 06–10 record what this skill
asks for in `models/autoports/<model>/doc/agentic_serving.json`; `scripts/check_agentic_serving.py`
gates it. Missing evidence is critical. A measured shortfall with a recorded reason is advisory.

## Target workload

Agents resend long conversations with short appends and wait on each reply; several share one engine.
Throughput then depends on prefix-cache reuse, how many conversations the KV pool holds, per-step fixed
costs and long-context accuracy, none of which a 128/128 benchmark shows. Record `workload`:
`target_concurrency`, `working_set_tokens` (live conversation per agent), `typical_context_tokens` and
`source`. Estimate them from the intended evals until goal 12 measures them. Batch 1 stays a regression row.

## Decode batches

Decode runs on `batch_sizes` from `bringup/agentic_evals/inputs.json` (default 1, 8, 16, 32). Optimise and
measure against the set, batch 1 first, then the largest batch the device holds. Record any batch the device
cannot hold and why; live users pad up to the next supported batch.

## Memory

Weights, KV pool, per-request reserves and trace region share DRAM. From stage 06 keep `ledger`: per
weight family its bytes and `copies` (a second copy needs a `copy_reason`), reserves against measured peak,
KV bytes per token and `pool_tokens`. Size the pool from free DRAM measured after weights, reserves and
trace region (`pool_sizing: measured`); a fixed budget needs `pool_sizing_reason`. The pool should hold
`target_concurrency × working_set_tokens`, or record `pool_shortfall_reason`. For hybrid models record
bytes per cache ID by layer group, allocated against used.

## Serving invariants

Check on replayed or synthetic multi-turn traffic at the target concurrency, and record under `serving`
in stages 09 and 10:

- `prefix_caching` on, with a hit rule for every layer kind, or `false` with a `reason`.
- `hit_fraction_of_ceiling` (cached ÷ tokens a previous turn sent) at least 0.9 unless `pool_bound`.
- `trace_captures_after_warmup` 0; no host work per step that scales with steps or pool size.
- `growing_contexts`: `target_concurrency` interleaved conversations growing to `working_set_tokens`,
  from recorded histories (`scripts/agent_replay.py`) when they exist. Record `preemptions`, prefilled
  against final context tokens, and `late_hit_fraction` over the last quarter of requests (≥ 0.9).

Stage 10 also records, without a pass bar, `prefill_by_length` (prefill rate at 4096,
`typical_context_tokens` and `working_set_tokens`, and decode rate during a long prefill) and
`host_contention` (decode at the largest batch on an idle host and with every host CPU busy).
Stage 08 checks precision on long agent prefixes and teacher-forced and natural text (datatype-sweep), not
only short top-k agreement.

## Measurement method

Every optimisation measurement (stages 07–10 and goal 12's speed work):

- builds control and change from the same image and toolchain, and records both SHAs;
- writes a predicted range first; a miss is explained before the next change;
- layers changes as cumulative arms (A0, A0+1, A0+1+2 …). Low-risk changes may share an arm when the
  result lands in range; a miss splits it, least certain first. Prefer runtime switches to restarts.
  An arm that has decided its question is stopped;
- goes cheapest first: arithmetic on data already held, a CPU test, a per-chip op or layer benchmark, a
  short replay, and only then a serving arm or a scored run;
- judges speed on replayed agent trajectories at the agent counts goal 12 uses (recorded histories when
  they exist; before any goal-12 run, stage 10's growing-context traffic), beside one fixed synthetic suite
  (short, context ≥ 28K, 1/4/8/32 users). A gain that does not show on replay is not adopted;
- gates a change meant to be lossless on identical greedy and seeded outputs over a fixed prompt panel
  (feature on vs off, cache on vs off); a numerics change reports PCC against the reference;
- checks the loaded config (dtype, export, flags, versions) against what the measurement claims before
  any sweep or comparison;
- probes a suspected mechanism (compiles, memory fit, placement, scheduling) directly, testing the assumed
  cause with a control, before building a fix;
- logs each autonomous choice: question / choice / why / cost of the alternative.

Before changing anything, look at what a run did over time: per model call, generation, prefill and
waiting, and everything outside the server (tool commands, container round trips). The share of agent
time spent generating bounds what any serving change can save.

## Stage 11: serving rates

Stage 11 is benchmarks only. It measures, on the route goal 12 will use and with the sampling settings
its harness sends, a batch × input-length grid
(each validated batch, and above batch 1 also at request concurrency 1; inputs of 4096,
`typical_context_tokens`, `working_set_tokens`, ≥ 28K, and the longest context the evals reach: their prompt
plus output cap, at most the served max context, where the pool holds the batch) plus every benchmark row
the release spec lists for this model. Goal 12 interpolates between lengths and concurrencies and
extrapolates past the longest length, so a row at the top of the range removes the largest error (the v4
Gemma grid stopped at 32K while GPQA calls reached 127K, and its full-batch B32 rows priced a server with a
few requests in flight far below its real speed). Every row completes; a row the budget leaves
unmeasured goes in `missing_rows` (batch, input_tokens, reason) and the stage completes. Each row is compared with its
predicted rate; a slowdown beyond the prediction's range is explained. It writes
`doc/benchmark/serving_rates.json`: the server config, `validated_batches`, the `workload` lengths the grid
used, and per batch, concurrency and input length
`prefill_tok_s`, `decode_tok_s_user`, `ttft_s` and `step_ms`. Goal 12 reads these rates and does not
re-measure them, except the rows it uses after a precision fix (Failure analysis 3).

## Goal 12: agentic evals

Goal 12 shows that the served model scores like its reference on agentic evals, at a speed the measured
rates explain. It runs on the local machine; no CI is needed.

### Inputs

`bringup/agentic_evals/inputs.json`; every field is optional and overrides the default.

| Field | Default |
|---|---|
| `entry` | `autoport` (stages 01–11 ran), or `existing` (see Entry paths) |
| `mode` | `quick` (phase 1, no task containers), `agentic` (phases 1–2; needs Docker, RAM and disk for task containers), `extended` (agentic plus the extended phase). Each mode includes the one before. Default `agentic` |
| `phase1` | `{"suite": "r1_gpqa_diamond", "tasks": 10}` |
| `phase2` | `{"suite": "swe_bench_verified", "tasks": 10}` |
| `extended_suites` | `["r1_gpqa_diamond", "swe_bench_verified"]`; add `terminal_bench_2_1` to run it |
| `task_lists` | the fixed lists in Task lists |
| `references` | per suite; see References |
| `ok_band` | the reference lies inside the score's 95% interval, or the score is above it |
| `explain_tolerance` | 0.25 (measured wall at most 25% above the explained time) |
| `stage_budget_hours` | quick 12; agentic 24 (phase 1 up to `phase1_hours`, phase 2 the rest); extended +24 |
| `phase1_hours` | 12; lower it with a shorter `stage_budget_hours` so phase 2 still runs |
| `run_timeout_hours` | 12 per run; extended lists target ≈ 10 h |
| `network` | per task `allow_internet` (Terminal-Bench); SWE agent in main's `no-network` mode with `allow_agent_endpoint` |
| `limits` | agent and request limits and output cap from the harness reference; runs start there |
| `decisions` | operator answers to a blocked goal's question, e.g. a reference to use |
| `triage_model` | the model for reply-triage sub-agents (Failure analysis); default the cheapest one the Codex install offers |

Before stage 9 the operator proves the setup: Docker (or the harness's container runtime), gated dataset
credentials, and host RAM and disk for task containers. A mode or machine change
mid-run repeats stage 10's agent-load checks.

### Entry paths

- **`autoport`**: a new model through stages 01–11. Limits and release rows come from the closest model
  in tt-inference-server's model specs with a published reference; record which.
- **`existing`**: the model already has a tt-inference-server model spec. Start at stage 11 with that
  spec's serving config, limits and release rows; stages 06–10 evidence is not required, and the gate
  skips the `agentic_serving.json` checks.

### References

Per suite, first match wins: `references` in inputs; the harness's reference for this model; the model
card's published score for this exact model and suite; a floor the operator sets in inputs. Without any, the
phase cannot pass: ask the operator. Record the source next to each score.

### Phases

- **Phase 1 (`quick`):** `phase1`, scored on main's harness on this machine, within stage hours 0 to `phase1_hours`
  (target run < 5 h).
- **Phase 2 (`agentic`):** `phase2`, the fixed diverse SWE-bench list, same rule. It starts once phase 1
  has passed, or has its list run, every failure analysed and its fix attempts recorded, and at the latest
  at `phase1_hours`; it has the rest of `stage_budget_hours`. A phase that cannot pass early does not hold
  the machine to the boundary (v4 Gemma finished GPQA analysis at hour 4.7 and SWE waited until hour 8).
  Plan both from the start.
- A phase **passes** when its score is within `ok_band` of the reference and its wall time is explained.
  Explained hours replay the run on the rates: each model call takes uncached input ÷ prefill rate + output
  tokens ÷ decode rate per user on the run's server batch with the requests then in flight, with rates
  interpolated between the measured concurrencies and input lengths (extrapolated past the longest, and
  then labelled approximate); each
  task adds its measured time outside the server; tasks start on the agents in the observed order; the
  explained wall is when the last one ends (one long task sets it). `scripts/explain_hours.py --rates
  doc/benchmark/serving_rates.json --calls <calls.json> --server-batch <max-num-seqs>` computes it from the
  per-call accounting; record its output with the run. Measured wall at most `explain_tolerance` above it passes;
  slower, find the gap. Faster means the rates are pessimistic for this traffic: note it, it does not hold
  the phase (v4 Gemma SWE ran 3.96 h against 11.70 h explained with its band met). A run whose calls cannot be priced at all records `explained_reason` instead; that is reported, not blocking.
- **A mode is done when all its phases pass**: release ready for that mode.
- **Not passed yet:** a phase whose time ends below its band or with unexplained time is a result. Keep its
  score, interval, reference and per-task analysis as they are, record the reason, and move on; never
  discard it or call it failing. If phase 2 started after `phase1_hours`, REPORT.md says so plainly: the setup or
  the text was not tested right. Phase timing is reported, not gated.
- **Extended (+24 h, after agentic passes):** estimate each full suite (198, 500, 89) from the phase runs'
  tokens per task, the rates and the measured tail; do speed work by the Measurement method; then run per
  suite in `extended_suites` the largest list whose projected run fits `run_timeout_hours` with margin
  (≈ 10 h), scored by the same rules. Lists grow by adding tasks to the phase lists, so results nest.

A score is the first attempt of every task in the list. Use n = 10 for what it is: it finds gross defects,
not small regressions; report the interval.

### Server size

For each run, in order: measure device memory free at launch (per device, before the KV pool is
allocated; the smallest device) and size the pool from it; set agents = min(pool tokens ÷ the suite's
working set per agent, task sandboxes the host can run, tasks in the list); serve on the smallest
validated batch that holds them. Record `server_size` with the run (Outputs). A pool below half the
measured free memory, or more agents than the pool holds at their working set, needs a recorded `reason`;
the gate is critical without one. Where this leaves a choice, take the fastest projected finish (waves ×
per-task time at that batch's per-user rate, plus the tail). Agents never exceed `max_num_seqs`. Start
from the best-known config in `learned.json` for this model or its closest model; a projection more than
2× the best-known time for that list size is a config defect, not a size limit.
Phase lists always run: the phase's time bounds them, and a long first estimate is expected for a new
model. Admit an extended list only when its projection from this run's measured per-task times fits
`run_timeout_hours`. Record measured tokens and hours per task in `learned.json` (`task_times`) so the next
model's estimates start from them.

### Settings

- **Limits:** runs start at the harness reference's agent and request limits and output cap for this model
  (or the closest model with a published reference); never rewrite release values. No task fails on a limit
  unless it loops or stalls (Failure analysis 2). Record per task the limits it finished under, and the
  reference's limits beside the score.
- **Network:** a task may use what it declares (`allow_internet`). A trajectory that fetched its own
  solution (the upstream fix, a later release, the resolving discussion) is a failure and stays in the
  denominator; report how many. Never void a list.
- **Task resources** come from each task's definition, never a blanket value.
- **Reasoning:** start at the reference's setting (medium when unknown).
  Record the setting with each run and the reference's own setting with the reference.
- **Harness and route** are main's, unchanged; only a precision fix (Failure analysis 3) changes precision. A real harness defect is fixed on a generic branch named for
  the defect and listed in `learned.json`.

### Task lists

Lists are fixed per suite so models compare. GPQA Diamond: 10 questions spread across physics, chemistry
and biology. SWE-bench Verified: 10 tasks diverse by repository, difficulty and patch size, starting from
main's CI nightly five; never a prefix of a sorted list. Terminal-Bench: spread across categories. Record
each list and its rule in `results.json` before any score on it.

### Failure analysis

This is iteration on the phase's failing tasks inside its time, never growth of the list.

1. Every failed or timed-out task gets a row: cause (wrong answer or patch, anomaly, loop to the cap,
   malformed tool call, cut off while progressing, stalled, infrastructure, fetched solution) and evidence
   (trajectory lines, tokens, time), from the triage below. A degenerate reply is `anomaly`, never `wrong`
   or `loop_cap` (that is an agent repeating coherent actions).
2. A task that hit the agent or request limit or the output cap fails on it only if its trajectory loops
   (`loop_cap`) or stalls (`stalled`), proven as the triage below says. One cut off while progressing is
   rerun at ≥ 2× that limit, within `run_timeout_hours`, until it finishes, loops or stalls; that result counts.
3. An anomaly recorded in stage 11's `doc/benchmark/anomaly_check.json` starts this procedure early; it is still
   decided on the real eval replies. Each anomaly, and a phase below its band, gets a fix attempt by $datatype-sweep's Anomalies section
   (reproducible test on the model runner, full precision, bisect, re-measure, rerun), inside the phase's
   time, recorded in `anomalies`. A precision fix is the one allowed precision change; a speed loss is
   accepted.
4. Possible, not required: one try at another reasoning setting (thinking on or off, an effort the server
   offers) for a task that looped, in the same wave; the whole list is rerun at it only if it solved a failed
   task, not for time alone; the phase takes the run that scores best within its time.
5. Reruns should take about 20% of the phase's time at most.
6. Any other single-task rerun changes the score only when the first failure was infrastructure (verifier
   setup, image pull); otherwise it is reported beside the score.

Triage: script, then sub-agent, then the coordinator.

- **Script** (measures, never judges). As each run or wave finishes, `scripts/reply_digest.py --log <server
  log> --accounting <the run's per-call accounting> --out doc/agentic_evals/current/<run>/digest` reads the
  model's own replies from the server's response log (harness files lose them: lm_eval keeps only the final
  answer, mini-swe-agent drops capped replies) and writes per task one line per model call (tokens, finish,
  tail compression and distinct characters, action, repeats) plus the tail of each flagged or capped reply.
  `flag` marks a degenerate tail (thresholds in the script). Every capped or flagged reply must map to a task.
- **Sub-agent** per failed, capped or flagged task, started as that task finishes, in parallel while the list
  runs: fresh, no prior context, low effort, model `triage_model`; input the task statement and its digest.
  It returns JSON `{task, verdict, first_bad_call, evidence, confidence}`, verdict one of `progressing`,
  `looping_actions`, `repeating_tokens`, `anomaly`, `stalled`, `coherent_but_wrong`, `infra`, with evidence as
  call ranges or quotes; save it as `digest/<task>.triage.json`.
- **Coordinator** (this agent) decides causes, reruns and the anomaly procedure. A `flag` is anomaly evidence by
  itself (cause `anomaly`, or a reason in the run's `flag_reasons`). For a task that hit a limit, only
  `looping_actions`, `repeating_tokens`, `stalled` or `anomaly` with high confidence and cited calls proves it
  did not just need more room; otherwise rerun it at a higher limit.

Before the first eval run (stage 11's end or stage 12's start), send one short request through the served
route and run `reply_digest.py --check-log --log <server log> --id <its id> --record
doc/agentic_evals/logging_check.json`: the log must hold its body, token counts and finish reason. If it fails,
fix response logging (a flag set to `1` where the server parses `true` once cost a run three hours) before any
eval run.

How reruns run. A task here is one eval task (one SWE instance, one GPQA question). Failed tasks are rerun
together as one harness run per setting (the list run's command with only the failed task IDs), all started
at once on the server already running; each task is still its own harness agent and container and is
analysed on its own. Concurrency is bounded by the server's agent slots and host memory for task containers.
Request-level changes (output cap up to the server's max context, agent or request timeouts, seed, reasoning
setting) share one wave; server-level changes (precision config, cap above max context, KV pool, batch)
need a restart: a new wave. A task alone at batch 1 is only a control for a load question (timeout under
load, preemption or KV eviction, batch-dependent numerics), after the wave reproduced the failure. Each run
records implementation commit, precision config id and server settings in `runs.json`; scores at different
configs are never mixed.

Name what each cause would need to fix and whether it is the model, the serving stack or the harness.

### Runner, usage and sub-agents

The multigoal runner starts and checks stages; it does not watch eval runs. Start every list run and
rerun wave with `scripts/eval_runs.py start --model-dir <model> --id <run> -- <harness command>`: the run
survives the tool call, and its command, log, pid and start go to `runs.json`. Wait with `eval_runs.py wait
--model-dir <model> --minutes <n>`: it blocks without model calls and records end, hours, exit code and
`conclusion`. Each look in a short turn resends the whole context (Gemma's v4 stage-12 thread sent 392M
input tokens), and agent-built supervisors have crashed mid-run; use waits of tens of minutes, and do
other analysis between them. Score the run from the harness output and mark it `collected` in
`results.json`. When the stage ends (and after each remediation round) the runner counts the stage's own Codex
tokens and hours from its CODEX_HOME and writes them to REPORT.md (`## Stage usage`) and
`stage_usage.json`; leave both as written. Sub-agents run at low effort for profiling, log triage and
tests; the coordinator decides. Sub-agents append findings to `learned.json` and read it before starting.

### Outputs

In `doc/agentic_evals/`:

- `results.json`: per run `phase`, `suite`, `tasks` (names, list rule), `config` (`pool_tokens`, `agents`,
  `batch`, `max_num_seqs`, `server_size` (`free_gb` per device measured at launch, `pool_gb` per device,
  `working_set_tokens` per agent, `reason`), limits and their source, network mode, `reasoning`, SHAs), `score` (`solved`, `n`, `interval_95`),
  `reference` (`value`, `source`, `reasoning`), `band_pass`, `predicted_hours`, `explained_hours`,
  `measured_hours`, `explained_reason` when there is no number, `collected`, and `failures`: one row per failed task with `task`, `cause` (`wrong`,
  `anomaly`, `loop_cap`, `malformed_tool_call`, `cutoff_progressing`, `stalled`, `infrastructure`, `fetched_solution`),
  `evidence` and `rerun` (`tried`: setting, `limits`, result; `counted`); `flag_reasons` and `unmapped_reason`
  when needed (Triage); `digest` when not `current/<run>/digest`; `config.precision` (the
  precision config file served); `anomalies` (datatype-sweep, Anomalies); plus
  `phases`: per phase `run`, `start_hour`, `pass`, and `reason` when not passed.
- `runs.json`: the run records `eval_runs.py` writes (one per list run or wave).
- `logging_check.json` (before the first run) and `current/<run>/digest/` (`summary.json`, `<task>.json`,
  `<task>.triage.json`).
- `learned.json`: `best_config` per suite (the config, list size and hours of the best valid run),
  `task_times` (per suite and task: tokens and hours measured), `harness_facts` and `generic_fixes`, for the
  next model.
- `REPORT.md`: each phase passed or not passed yet (with the reason), scores with intervals, reasoning
  settings and limits against the references', hours predicted against measured, a late phase 2 start if any, the per-task
  analysis, then what remains.

Update `workload` in `agentic_serving.json` with the measured values.

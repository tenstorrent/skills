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
Stage 08 checks precision on long agent prefixes and free-running replies, not only short top-k agreement.

## Measurement method

Every optimisation measurement (stages 07–10 and goal 12's speed work):

- builds control and change from the same image and toolchain, and records both SHAs;
- writes a predicted range first; a miss is explained before the next change;
- layers changes as cumulative arms (A0, A0+1, A0+1+2 …). Low-risk changes may share an arm when the
  result lands in range; a miss splits it, least certain first. Prefer runtime switches to restarts.
  An arm that has decided its question is stopped;
- goes cheapest first: arithmetic on data already held, a CPU test, a per-chip op or layer benchmark, a
  short replay, and only then a serving arm or a scored run;
- judges speed on replayed real agent trajectories at the agent counts goal 12 uses, beside one fixed
  synthetic suite (short, context ≥ 28K, 1/4/8/32 users). A gain that does not show on replay is not adopted;
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
(each validated batch; inputs of 4096, `typical_context_tokens`, `working_set_tokens` and ≥ 28K) plus every
benchmark row the release spec lists for this model. Every row completes; a row the budget leaves
unmeasured goes in `missing_rows` (batch, input_tokens, reason) and the stage completes. Each row is compared with its
predicted rate; a slowdown beyond the prediction's range is explained. It writes
`doc/benchmark/serving_rates.json`: the server config, `validated_batches`, and per batch and input length
`prefill_tok_s`, `decode_tok_s_user`, `ttft_s` and `step_ms`. Goal 12 reads these rates and does not
re-measure them.

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
| `explain_tolerance` | 0.25 (measured wall within ±25% of the explained time) |
| `stage_budget_hours` | quick 12; agentic 24 (phase 1 up to `phase1_hours`, phase 2 the rest); extended +24 |
| `phase1_hours` | 12; lower it with a shorter `stage_budget_hours` so phase 2 still runs |
| `run_timeout_hours` | 12 per run; extended lists target ≈ 10 h |
| `usage_budget` | the Codex points this stage may use; the runner reads the account at start |
| `network` | per task `allow_internet` (Terminal-Bench); SWE agent in main's `no-network` mode with `allow_agent_endpoint` |
| `limits` | agent and request limits and output cap from the harness reference; runs start there |
| `decisions` | operator answers to a blocked goal's question, e.g. a reference to use |

Before stage 9 the operator proves the setup: Docker (or the harness's container runtime), gated dataset
credentials, host RAM and disk for task containers, and the usage budget. A mode or machine change
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
- **Phase 2 (`agentic`):** `phase2`, the fixed diverse SWE-bench list, same rule. It starts when phase 1
  passes or at `phase1_hours`, whichever comes first, and has the rest of the 24 h. Plan both from the start.
- A phase **passes** when its score is within `ok_band` of the reference and its wall time is explained.
  Explained hours replay the run on the rates: each model call takes uncached input ÷ prefill rate + output
  tokens ÷ decode rate per user, from the `serving_rates.json` row at the batch covering the requests active
  then and the call's final context (the next row up); each task adds its measured time outside the server;
  tasks start on the agents in the observed order; the explained wall is when the last one ends (one long
  task sets it). Measured wall within `explain_tolerance` of that passes; otherwise find the gap.
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
Admit a run only when its projection from measured times fits `run_timeout_hours`.

### Settings

- **Limits:** runs start at the harness reference's agent and request limits and output cap for this model
  (or the closest model with a published reference); never rewrite release values. No task fails on a limit
  unless it loops or stalls (Failure analysis 2). Record per task the limits it finished under, and the
  reference's limits beside the score.
- **Network:** a task may use what it declares (`allow_internet`). A trajectory that fetched its own
  solution (the upstream fix, a later release, the resolving discussion) is a failure and stays in the
  denominator; report how many. Never void a list.
- **Task resources** come from each task's definition, never a blanket value.
- **Reasoning:** start at the reference's setting (medium when unknown); Failure analysis probes others.
  Record the setting with each run and the reference's own setting with the reference.
- **Harness and route** are main's, unchanged. A real harness defect is fixed on a generic branch named for
  the defect and listed in `learned.json`.

### Task lists

Lists are fixed per suite so models compare. GPQA Diamond: 10 questions spread across physics, chemistry
and biology. SWE-bench Verified: 10 tasks diverse by repository, difficulty and patch size, starting from
main's CI nightly five; never a prefix of a sorted list. Terminal-Bench: spread across categories. Record
each list and its rule in `results.json` before any score on it.

### Failure analysis

This is iteration on the phase's failing tasks inside its time, never growth of the list.

1. Every failed or timed-out task gets a row: cause (wrong answer or patch, loop to the cap, malformed tool
   call, cut off while progressing, stalled, infrastructure, fetched solution) and evidence (trajectory
   lines, tokens, time).
2. A task that hit the agent or request limit or the output cap fails on it only if its trajectory loops
   (`loop_cap`) or stalls (`stalled`). One cut off while progressing is rerun alone at ≥ 2× that limit,
   within `run_timeout_hours`, until it finishes, loops or stalls; that result counts.
3. For tasks that looped or hit a limit, probe other settings (thinking on or off, the efforts the server
   offers) on each task alone, in parallel where the server allows. One probe per task is enough. Record
   each attempt in `rerun.tried` (setting, limits, result, tokens, time).
4. Rerun the whole list at a new setting only if a probe solved a failed task, not for time alone; the
   phase takes the run that scores best within its time.
5. Limit reruns and probes together should take about 20% of the phase's time at most.
6. Any other single-task rerun changes the score only when the first failure was infrastructure (verifier
   setup, image pull); otherwise it is reported beside the score.

Name what each cause would need to fix and whether it is the model, the serving stack or the harness.

### Runner, usage and sub-agents

The multigoal runner, not the agent, waits on runs: a run registers its id and collect command in
`runs.json`; the runner collects and scores it, appends its record (start, end, hours, conclusion) and wakes
the agent, even if the agent has stopped. With nothing to decide, the agent ends its turn instead of
polling. The runner tracks Codex usage against `usage_budget` and, when the projection says the budget ends
first, tells the agent to finish the current phase's report. Sub-agents run at low effort for profiling,
log triage and tests; the coordinator decides. Sub-agents append findings to `learned.json` and read it
before starting.

### Outputs

In `doc/agentic_evals/`:

- `results.json`: per run `phase`, `suite`, `tasks` (names, list rule), `config` (`pool_tokens`, `agents`,
  `batch`, `max_num_seqs`, `server_size` (`free_gb` per device measured at launch, `pool_gb` per device,
  `working_set_tokens` per agent, `reason`), limits and their source, network mode, `reasoning`, SHAs), `score` (`solved`, `n`, `interval_95`),
  `reference` (`value`, `source`, `reasoning`), `band_pass`, `predicted_hours`, `explained_hours`,
  `measured_hours`, `collected`, and `failures`: one row per failed task with `task`, `cause` (`wrong`,
  `loop_cap`, `malformed_tool_call`, `cutoff_progressing`, `stalled`, `infrastructure`, `fetched_solution`),
  `evidence` and `rerun` (`tried`: setting, `limits`, result; `counted`); plus
  `phases`: per phase `run`, `start_hour`, `pass`, and `reason` when not passed.
- `runs.json`: the runner's append-only run records.
- `learned.json`: `best_config` per suite (the config, list size and hours of the best valid run),
  `harness_facts` and `generic_fixes`, for the next model.
- `REPORT.md`: each phase passed or not passed yet (with the reason), scores with intervals, reasoning
  settings and limits against the references', hours predicted against measured, a late phase 2 start if any, the per-task
  analysis, then what remains.

Update `workload` in `agentic_serving.json` with the measured values.

---
name: agentic-serving
description: Optimize a bringup for the agentic serving workload it will actually see (many concurrent multi-turn conversations with long shared prefixes), keep device memory accounted for, and run budgeted agentic evals (goal 12). Use whenever a stage chooses layouts, precision, memory reserves, batch size, cache policy or serving configuration, and for goal 12.
---

# Agentic serving

Follow [model-bringup startup](../model-bringup/SKILL.md#startup). Stages record what this skill
asks for in `models/autoports/<model>/doc/agentic_serving.json`; `scripts/check_agentic_serving.py`
gates it. Missing evidence is critical. A measured shortfall is critical unless it carries a reason
(`copy_reason`, `pool_shortfall_reason`, `serving.reason`, `trace_reason`), which makes it advisory.
Goal 12 must price every such shortfall in suite hours before it ranks its work.

## Target workload

Agents resend long conversations with short appends and wait on each reply; several share one
engine. Throughput then depends on prefix-cache reuse, how many conversations the KV pool holds,
per-step fixed costs, and long-context accuracy, none of which a 128/128/1 benchmark shows.

Record `workload`: `target_concurrency`, `working_set_tokens` (live conversation per agent),
`typical_context_tokens`, and `source`. Without measurements, estimate them from the intended evals
and say so; goal 12 replaces them with measured values. Every stage that reports performance reports
the target workload next to batch 1, which stays as a regression row.

## Decode batches

Decode runs on a set of device batches: `batch_sizes` from `bringup/agentic_evals/inputs.json`,
default 1, 8, 16 and 32. Every stage that optimizes or measures decode works against that set, not
one shape. Layouts, sharding and traces chosen for batch 1 alone are often wrong for the largest
batch, and by the serving stages they are already fixed.

Work through the set in order of importance: batch 1 first, then the largest batch the device
holds, then any in between. Batch 1 stays the regression row. Record any batch the device cannot hold and why — active users pad up to the next
supported batch, so an omitted batch is served by the one above it.

## Memory

Decode on large models is usually weight-read bound, so extra rows are cheap in time and expensive
in KV memory. Weights, KV pool, per-request reserves and trace region compete for the same DRAM.
From stage 06 on, keep `ledger`: per weight family its bytes and `copies` (a second copy needs a
measured `copy_reason`), reserves against their measured peak use, KV bytes per token, and
`pool_tokens`. The pool should hold `target_concurrency × working_set_tokens`; otherwise record a
`pool_shortfall_reason`. Reconcile the ledger with allocator readings and explain any gap.

Size the pool from free DRAM measured after weights, reserves and trace region, and record
`pool_sizing: measured`. A fixed token budget is `pool_sizing: fixed` and needs a
`pool_sizing_reason`. For hybrid models record bytes per cache ID by group, allocated against used:
an ID that gets a page in every group but lives in one wastes the rest.

## Serving invariants

Check these on replayed or synthetic multi-turn traffic at the target concurrency, not only on
fixed-shape benchmarks. Record them under `serving` in stages 09 and 10.

- `prefix_caching`: enabled for serving, with a hit rule for every layer kind (full attention,
  sliding window, recurrent state). Otherwise `false` with a `reason`.
- `hit_fraction_of_ceiling`: cached tokens over the tokens a previous turn already sent. It should be
  at least 0.9 unless live conversations exceeded the pool (`pool_bound: true`). Look at which
  prefixes are lost and why: by time away, by previous reply length, and by pool pressure.
- `trace_captures_after_warmup`: captures, retirements and evictions per request, all 0.
- No host work per decode step that scales with steps or pool size.
- Start-up stays small relative to an eval suite.

Precision is part of this. Short-prompt top-k agreement does not show long-context or free-running
failures (malformed tool calls, loops to the length cap). Stage 08 also checks agent-shaped long
prefixes and free-running replies before choosing.

## Look at the whole run

Before changing anything, draw what a run did over time: device activity, what requests waited on,
memory and cache fill. Then ask where time goes, what sits idle, what work repeats, and how the end
differs from the middle. Name a mechanism, predict the effect of a change, and check it on the next
run.

For an agentic run, split each model call's wall time into generation, prefill and waiting for it,
and everything outside the server: tool commands, container round trips, scheduling. Under real
agent load also read prefilled tokens against the agents' final context tokens, decode rate while
long prompts prefill, and the engine's host time per step against task-container CPU.

## Goal 12: agentic evals

Optional operator inputs come from `bringup/agentic_evals/inputs.json` in the target checkout:

- evals: default `r1_gpqa_diamond`, `swe_bench_verified` and `terminal_bench_2_1`;
- `target`: where the stage ends, see [Stage order](#stage-order). `nightly` stops once the
  nightly task lists score; `full_load` (default) adds one run per suite that fills a server;
  `full` goes on to the full suites;
- `task_lists`, optional: task names per eval and rung, replacing the lists
  [Task lists](#task-lists) would choose;
- `batch_sizes`, see [Decode batches](#decode-batches): stage 10 validates a tuned configuration
  per batch and stage 11 measures a profile for each surviving one;
- `release_spec`, optional: where the release values come from. Without it, find the release
  configuration for each eval yourself, in the release workflow and the serving harness;
- `stage_budget_hours` (default 24), `effort_budget_hours` (default 16) and `suite_budget_hours`
  (default 8). The stage budget covers survey to release dispatch. Rung runs draw on the suite
  budget, not the effort budget; each release suite runs to completion within `suite_budget_hours`;
- `permissions`: standing decisions the stage would otherwise stop to ask for, such as
  `release_route_repairs` (may the stage publish fixes to the release route) and
  `account_fallback`. Ask for a missing one once, at the start, in `blocked.json`;
- reference scores per eval. Without one, look for a published score for this model; when there is
  none, the suite grades NA and the report says so;
- the eval harness: default the tt-inference-server agentic runner on its main branch;
- `compute`.

`compute` is the hardware the operator grants, on top of the local device, which stays available
for the quick checks the earlier stages ran there. Without a grant, local is all there is. With
one, `max_parallel` is how many runs may be in flight: a ceiling to fill when the plan needs it,
not a quota to spend. Re-plan when the grant changes.

A grant dispatches runs one of two ways. `commands` takes operator-supplied `submit`, `status` and
`collect` shell commands and is the general form. `kind: "github_actions"` dispatches a workflow in
a CI repository, which needs the implementation pushed where the runners can fetch it; see
[CI compute](references/ci-compute.md) for the input shape, the publish contract and the polling
loop. Either way, record the grant you actually used in `doc/agentic_evals/compute.json` so a
reader can tell a thin result from a small grant.


### Harness

Run the harness and the release route as they are on main. Do not write a stage-owned sandbox,
wrapper, task selector or network policy. Validity comes from the trajectory audit: a task
whose trajectory fetched its fix or reached the network is quarantined, not scored.

A real harness defect is fixed on a generic branch named for the defect, not the model, and listed
in `learned.json` so the next model finds it.

### Models new to the harness

When tt-inference-server has no entry for the model, add one before the preflight: the model and
its serving settings, and per eval its nightly task list, limits and reference. Find where each
lives in the current harness. This entry is the release spec that `serving_config.json` describes.

### Eval settings

Find the release values for the time limits and output cap before the first run, and record their
source and values in the preflight.

- Agent time limit, request time limit and output cap: never the reason a score is lost. The survey
  estimates what the model needs; runs correct it. When tasks are cut off while still making
  progress, raise the limit and run those tasks again. A task that loops or repeats may end at the
  output cap or time limit; it counts as a failure, and raising the limit does not fix it. Loops
  are a problem of their own: record them and rank finding their cause as an effort. Never go below
  the release values, and report the limits each score used.
- Task CPU and memory come from each task's definition. Never override them with a blanket value.

Record the limits in `serving_config.json`, with the release values as `release_limits`.

### Batch and agents

Record `planned_clients` for every run before it starts: how many agents it runs at once.

- **Agents** never exceed `max_num_seqs`, the agents the pool holds (pool tokens ÷ working set per
  agent, measured by the replay), or the number of task sandboxes the host can run at once (Docker networks, CPU, disk). Extra agents queue
  and slow every agent.
- **Batch**: serve each run on the validated batch that finishes its agents soonest. Per model
  call, time is output tokens ÷ that batch's measured per-user rate plus the call's time outside
  generation, which the smoke measures. A batch above the agents pays its full step for empty rows;
  one below makes agents queue. That is usually the smallest validated batch at or above
  `planned_clients`; the gate expects it unless `batch_reason` says otherwise.
- Predict with the measured share of agent time spent generating, not the rate ratio alone: when
  most of an agent's time is tool work, a faster batch saves little.
- Choose only among the batches the model supports: `validated_batches` from stage 10. When a
  validated batch has no measured per-user rate, measure it with a short serving run before
  choosing it, or choose among the measured ones and say so.
- The adapter serves one batch per server. A run on another batch needs its own server: restart
  for it rather than serve a small run on the largest batch.

Record the batch and `planned_clients` of each run in `serving_config.json`.

### Task lists

Each eval runs a growing series of task lists, one per rung. Choose each list before any score on
it is seen, and record the tasks, the rule and the source in `cohorts.json`.

Where a list comes from, first match wins, per eval and rung:

1. `task_lists` in `inputs.json`;
2. the model's own lists in tt-inference-server, for the nightly rung (its `ci-nightly` list);
3. the default rule below.

**Default rule.** Spread each list across the eval's strata (repositories for SWE-bench,
categories for Terminal-Bench), make each rung's list contain the one before it, and record the
rule so another run picks the same tasks. Never take a prefix of a sorted list.

| Rung | GPQA Diamond | SWE-bench Verified | Terminal-Bench | Size set by |
|---|---|---|---|---|
| smoke | 2 | 2 | 2 | fixed |
| nightly | 10 | 5 | 10 | fixed, the sizes main runs nightly |
| full load | agents per server | agents per server | agents per server | the agent, from [Batch and agents](#batch-and-agents) |
| larger | — | multiples of full load, or 100 to project 500 | multiples of full load | the agent, from the target and the budget |
| full suite | 198 | 500 | 89 | fixed |

**Read every score with its error.** Report the 95% interval, about ±2·√(p(1−p)/n), and say when a
list is too small to separate the model from its reference.

**Aggregates, not single tasks.** Per-task durations do not repeat between runs. Conclude from
score, suite wall and total agent time.

**Shape follows the claim.** Concurrency needs enough simultaneous conversations to load the pool
and scheduler, precision and long context need long free-running replies, caching needs repeated
multi-turn traffic. Replaying real agent histories shows capacity; short synthetic
tests do not.

### Stage order

Test size grows one rung at a time, so each problem a larger run could hit shows on the smallest
list that reveals it. A suite moves up a rung when it scored validly on the current one and the
time measured there projects the next rung inside the budget left. The stage ends at its `target`,
or earlier on a measured stop, and always holds the last rung it banked. Dispatch on CI from the
first hour, in parallel up to `max_parallel`, one suite per runner.

1. **Survey and preflight, about 2 h.** Answer every check and calculation in
   [the survey](references/survey.md) on the stage-11 data and one smoke run at release limits.
   The preflight proves the release values and their source, the registry, disk and admission
   (tasks × declared task storage plus the admission reserve against free disk; concurrent trials
   against the host limit), the release route, and an isolated 2-task smoke with a trajectory
   audit. For a model new to the harness, it first adds the entries above. Write `survey.json` and
   `preflight.json`, rank the problems found as [Ranking](#ranking) says, and write the first go or
   stop estimate per eval to `estimate.json`.
2. **Smoke rung: 2 tasks per suite** on the stock harness and route. Each suite must produce a valid
   scored trajectory. A suite that cannot becomes the first problem in the ranking.
3. **Nightly rung.** The first deliverable: the lists the release grades, scored against the
   references. Record `functional_at`.
4. **Full-load rung**, for target `full_load` or `full`: one run per suite that fills a server. It
   measures suite time, agent capacity and the slow tail at real load; its measured time
   replaces the estimate for larger rungs.
5. **Efforts, `effort_budget_hours`, hard stop**, ranked against the last banked rung. They start
   once the smoke rung is banked and run while rungs are on CI; a rung never waits for an effort.
6. **Combine, about 2 h.** Re-check the current best, then one scored probe.
7. **Larger rungs and full suites**, for target `full`, through the same workflow every earlier run
   used, split on task boundaries so each dispatch finishes inside the grant's per-run timeout. An
   infrastructure failure is re-dispatched; the release coordinator must allow that.

A measured stop is a valid result at any rung. Report it with `functional_at` and the estimate
that decided it.

### Ranking

Rank in two tiers.

1. **Validity first.** A problem that keeps a suite from producing a valid or representative score
   records `unblocks: <eval>` and ranks above every speed problem, cheapest first.
2. **Then speed**, by suite hours saved, summed over the evals, per device hour. Price savings at
   the suite sizes the release runs, not at sample size. Agents per server is always a candidate,
   priced against the pool and the measured call times.

For each problem estimate the device hours it needs, local and CI; agent time is not counted. When
a number cannot be calculated, guess and mark it. Name how the effort will confirm success without
the full suites, and the value that counts. Adjust where judgement says otherwise and record why.
Rank every problem, even past the budget; problems found later, by efforts, rungs or the
coordinator's own look at the run, join the ranking the same way. Re-rank after each effort and
each rung from the checks and calculations it touched.

### Efforts

An effort owns one problem and one branch. The coordinator sets its goal, its confirmation and a
device-hour time box with room over the estimate, and gives all three to the sub-agent, which plans
how to reach the goal in `runs/<effort>/plan.md` and revises the plan as it learns. The box bounds
the runs it chooses; it is not a target. Stop once the question is settled. At the box, the effort
records what it learned and re-estimates the device hours left and the saving; the coordinator
decides against the current ranking whether it continues.

- **Fastest test first.** Prefer a short test to a scored sample. The replay test
  (`scripts/agent_replay.py`) answers capacity and caching questions in minutes. Estimate a
  sample's CI time before dispatching and size it to what it has to separate; one run may test
  several changes when its results separate what each contributed.
- **Accepting a change.** A paired exactness check plus the confirmation accepts an exact change. A
  change that alters outputs needs a scored pair against the current best.
- **Change and problem are separate.** A real gain that misses its target is kept; the problem
  stays open.
- **Merge as you go.** Later efforts build on the current best. An effort that started from an
  older best confirms again after rebasing.
- **Re-ranks do not interrupt running efforts**, unless a merge already solved their problem.
- **Evidence is written once**, by the effort, when it ends. No sub-agent maintains or audits the
  evidence while the stage runs; one closure review at the end, boxed at 1 h.

The coordinator holds the current best, the slot ledger and the budget, chooses which efforts run
together, and reads wide where efforts read deep, as [Look at the whole run](#look-at-the-whole-run)
asks. Every dispatched run registers with it at launch. Charge an effort for the device jobs it
ran, by the slots they occupied; a server that stays up between jobs is not effort time. At each
effort boundary the coordinator re-reads `inputs.json` and `steering.md` (operator prose, if
present) and logs what it applied to `steering.log`. When blocked, it writes the reason to
`blocked.json` at once.

### Sharing the local device

Parallel efforts reach the local device only through one queue in `doc/agentic_evals/device_queue/`,
which runs one job at a time as [tt-device-usage](../tt-device-usage/SKILL.md) requires and logs
each job's owner, start, end and exit status. Keep local jobs short and send long runs to CI. After
a hang or crash, reset and check the device before the next job.

### Outputs

Write these to `doc/agentic_evals/`:

- `survey.json`: `checks`, `calculations` and `shortfalls`, as the survey reference names them
- `preflight.json`: `release_values` (with their source), `registry`, `disk`, `admission`,
  `release_route` and `isolation_smoke`, each with `ok`
- `cohorts.json`: per eval `source` (`inputs`, `harness` or `default_rule`), `rule`, and `rungs`
  mapping each rung run to its task names
- `efforts.json`: `efforts`, each with `problem`, `owner`, `goal`, `plan`, `predicted`
  (`saving_hours` per eval, `device_hours`, `basis`: `measured`, `calculated` or `guess`),
  `unblocks` for a validity problem, `confirm` (`method`, `success`), `box_hours`, `re_estimate`
  once past the box, `status` (`done`, `running`, or `not_run` with a `reason`), and when done
  `used_device_hours`, `change` (`kept`, `dropped`, `none`), `problem_status` (`solved`, `open`),
  `confirmed` and `evidence`; and `rankings`, each with `after`, `order` (`problem`,
  `saving_hours`, `device_hours`, `unblocks`) and a `reason` when it departs from the two tiers
- `plan.json`, with the `target`
- `runs/<arm>/` (manifest and collected results)
- `estimate.json`: per eval `decision` (`go` or `stop`), `reason` and `functional_at`, the size of
  the largest task list that completed with valid scores
- `full/<eval>/results.json` with `actual_hours` next to the prediction
- `serving_config.json`: the measured `max_concurrency`, `prefix_caching`, `max_num_seqs`,
  `validated_batches`, per run in `batch_by_eval` its `batch` and `planned_clients`, the agent and
  request time limits and output cap, and `release_limits`
- `compute.json`: the grant as used — `max_parallel`, peak runs in flight, and for a CI grant the
  published refs and every dispatched run id, so each result traces back to a run
- `learned.json`: what the next model's stage 12 should not rediscover: `harness_facts`
  (host and harness limits found, each with its evidence), `suite_timings` (per run `eval`,
  `tasks`, `agents`, `batch`, `wall_hours`, `agent_hours`, `timeouts`, `score`) and
  `generic_fixes` (branch, SHA and what each fixes).
- `REPORT.md`, leading with `functional_at` and the score and interval per suite, then each
  effort's predictions next to its results

Update `workload` with the measured values.

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

## Goal 12: agentic evals

Optional operator inputs come from `bringup/agentic_evals/inputs.json` in the target checkout:

- evals: default `r1_gpqa_diamond`, `swe_bench_verified` and `terminal_bench_2_1`; the default
  subset is 10 tasks each;
- `batch_sizes`, see [Decode batches](#decode-batches): stage 10 validates a tuned configuration
  per batch and stage 11 measures a profile for each surviving one;
- `release_spec`, optional: where the release values come from. Without it, find the release
  configuration for each eval yourself, in the release workflow and the serving harness;
- `stage_budget_hours` (default 24), `effort_budget_hours` (default 16) and `suite_budget_hours`
  (default 8). The stage budget covers survey to release dispatch; each release suite then runs
  to completion within `suite_budget_hours`;
- reference scores;
- the eval harness: default the tt-inference-server agentic runner;
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

### Eval settings

Find the release values for the time limits and output cap before the first run, and record their
source and values in the preflight.

- Agent time limit, request time limit and output cap: never the reason a score is lost. The survey
  estimates what the model needs; runs correct it. When tasks are cut off while still making
  progress, raise the limit and run those tasks again. A task that loops or repeats may end at the
  output cap or time limit; that is a model failure and counts as one, not a limit to raise. Never
  go below the release values, and report the limits each score used.
- Batch: run the full evals on the largest validated batch, with enough agents to fill it, unless a
  recorded reason says otherwise. Samples and tests use the largest batch that works and suits the
  test. Record the batch each eval used.

Record the limits and batches used in `serving_config.json`, with the release values as `release_limits`.

### Stage order

1. **Survey, about 2 h.** Answer every check and calculation in [the survey](references/survey.md)
   on the stage-11 data and one smoke run at release limits, and run the preflight: release values,
   registry, disk, the release route the final run will use, and an isolated 2-task smoke with a
   trajectory audit. Write `survey.json` and `preflight.json`.
2. **Rank, about 0.5 h.** Turn each problem the survey found into an effort in `efforts.json`, as
   [Ranking](#ranking) describes. Write the first go or stop estimate per eval to `estimate.json`.
3. **Efforts, `effort_budget_hours`, hard stop.** Run efforts in parallel, each owned by one
   sub-agent, as [Efforts](#efforts) describes.
4. **Combine, about 2 h.** Re-check the current best, then one scored probe at release limits.
5. **Release.** Run the suites with a go estimate through the same workflow every earlier run used,
   split on task boundaries so each dispatch finishes inside the grant's per-run timeout. An
   infrastructure failure is re-dispatched; the release coordinator must allow that.

A measured stop is a valid result at any step. Report it with the estimate that decided it.

### Ranking

For each problem, estimate the suite hours it saves per eval and the device hours it needs, local
and CI; agent time is not counted. When a number cannot be calculated, guess and mark it. Name how
the effort will confirm success without the full suites, and the value that counts.

Order by suite hours saved, summed over the evals, per device hour, then adjust where judgement says
otherwise and record why. Rank every problem, even past the budget. Re-rank after each effort from
the checks and calculations the change touched; the full suites run only at release.

### Efforts

An effort owns one problem and one branch. The coordinator sets its goal, its confirmation and a
device-hour time box with room over the estimate, and gives all three to the sub-agent, which plans
how to reach the goal in `runs/<effort>/plan.md` and revises the plan as it learns. The box bounds
the runs it chooses; it is not a target. Stop once the question is settled. At the box, the effort
records what it learned and re-estimates the device hours left and the saving; the coordinator
decides against the current ranking whether it continues.

- **Fastest test first.** Prefer a short test to a scored sample; size samples as
  [Sizing a sample](#sizing-a-sample) says.
- **Accepting a change.** A paired exactness check plus the confirmation accepts an exact change. A
  change that alters outputs needs a scored pair against the current best.
- **Change and problem are separate.** A real gain that misses its target is kept; the problem
  stays open.
- **Merge as you go.** Later efforts build on the current best. An effort that started from an
  older best confirms again after rebasing.
- **Re-ranks do not interrupt running efforts**, unless a merge already solved their problem.

The coordinator holds the current best, the slot ledger and the budget, chooses which efforts run
together, and reads wide where efforts read deep, as [Look at the whole run](#look-at-the-whole-run)
asks. Every dispatched run registers with it at launch. At each effort boundary it re-reads
`inputs.json` and logs what it applied to `steering.log`. When blocked, it writes the reason to
`blocked.json` at once.

### Sharing the local device

Parallel efforts reach the local device only through one queue in `doc/agentic_evals/device_queue/`,
which runs one job at a time as [tt-device-usage](../tt-device-usage/SKILL.md) requires and logs
each job's owner, start, end and exit status. Keep local jobs short and send long runs to CI. After
a hang or crash, reset and check the device before the next job.

### Sizing a sample

Estimate a sample's CI time before dispatching, and size it to what it has to separate. Too small
to beat the noise decides nothing; a full suite to settle what a sample answers costs more.

Shape follows the claim: concurrency needs enough simultaneous conversations to load the pool and
scheduler, precision and long context need long free-running replies, caching needs repeated
multi-turn traffic. A count valid for one is meaningless for another.

Write these to `doc/agentic_evals/`:

- `survey.json`: `checks`, `calculations` and `shortfalls`, as the survey reference names them
- `preflight.json`: `release_values` (with their source), `registry`, `disk`, `release_route`
  and `isolation_smoke`, each with `ok`
- `efforts.json`: `efforts`, each with `problem`, `owner`, `goal`, `plan`, `predicted`
  (`saving_hours` per eval, `device_hours`, `basis`: `measured`, `calculated` or `guess`),
  `confirm` (`method`, `success`), `box_hours`, `re_estimate` once past the box, `status`
  (`done`, `running`, or `not_run` with a `reason`), and when done `used_device_hours`, `change`
  (`kept`, `dropped`, `none`),
  `problem_status` (`solved`, `open`), `confirmed` and `evidence`; and `rankings`, each with
  `after`, `order` (`problem`, `saving_hours`, `device_hours`) and a `reason` when it departs from
  saving per device hour
- `plan.json`
- `runs/<arm>/` (manifest and collected results)
- `estimate.json`: per eval `decision` (`go` or `stop`) and `reason`
- `full/<eval>/results.json` with `actual_hours` next to the prediction
- `serving_config.json`: the measured `max_concurrency`, `prefix_caching`, `max_num_seqs`, the batch
  each eval used, the agent and request time limits and output cap, and `release_limits`
- `compute.json`: the grant as used — `max_parallel`, peak runs in flight, and for a CI grant the
  published refs and every dispatched run id, so each result traces back to a run
- `REPORT.md`, with each effort's predictions next to its results

Update `workload` with the measured values.

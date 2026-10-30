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

Weight the effort: batch 1 first, then the largest, then the ones between. Batch 1 stays the
regression row. Record any batch the device cannot hold and why — active users pad up to the next
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

- evals: default GPQA Diamond, SWE-bench Verified and Terminal-Bench, named and versioned as the
  current release spec names them; the default subset is 10 tasks each;
- `batch_sizes`, see [Decode batches](#decode-batches): stage 10 validates a tuned configuration
  per batch and stage 11 measures a profile for each surviving one;
- `planned_clients` per eval: how many agents the release runs at once;
- `stage_budget_hours` (default 24), `effort_budget_hours` (default 16) and `suite_budget_hours`
  (default 8);
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

### Settings the release fixes

Some settings are not tuning arms. Read them from the release spec before the first run:

- Eval names and versions. A stale default in a skill or inputs file does not override the spec.
- Agent time limit, request time limit and output cap: never below the release values. Raise the
  output cap while any answer hits it; a capped answer measures the cap, not the model.
- Batch: serve each eval on the smallest validated batch at or above its `planned_clients`. A larger
  batch pads every step and prefill to its full shape. A server may hold several batches; record
  the one each eval used.

Record them in `serving_config.json` next to `release_limits`, the values the spec sets.

### Stage order

1. **Survey, about 2 h.** Answer every check and calculation in [the survey](references/survey.md)
   on the stage-11 data and one smoke run at release limits, and run the preflight: registry,
   disk, the release route itself, and an isolated 2-task smoke with a trajectory audit. The
   release route is the workflow the final run will use; prove it here, not at the end. Write
   `survey.json` and `preflight.json`.
2. **Rank, about 0.5 h.** Turn every problem the survey found into an effort in `efforts.json`,
   as [Ranking](#ranking) describes, even when their estimates add up to more than the budget.
   Write the first go or stop estimate per eval to `estimate.json`.
3. **Efforts, `effort_budget_hours`, hard stop.** Run efforts in parallel, each owned by one
   sub-agent, as [Efforts](#efforts) describes. Re-estimate and re-rank after each effort.
4. **Combine, about 2 h.** Re-check the current best, then one scored probe at release limits.
5. **Release.** Run the suites with a go estimate through the same workflow every earlier run used,
   split on task boundaries so each dispatch finishes inside the grant's per-run timeout. An
   infrastructure failure is re-dispatched; the release coordinator must allow that.

A measured stop is a valid result at any step. Report it with the estimate that decided it.

### Ranking

Before an effort starts, write down what it should gain and what it should cost:

- **Saving:** suite hours saved per eval in the plan, for example SWE-bench and Terminal-Bench,
  and their sum.
- **Work:** hours the effort should take.
- **Confirmation:** how the effort will show it worked without the full suites: a short test, a
  calculation from the survey, or a small task subset. Name it and the value that counts as success.
- **Basis** of each number: `measured`, `calculated` or `guess`.

An estimate that cannot be calculated is guessed from the nearest evidence (a similar change, a
stage-11 measurement, a short test) and marked `guess`; it never blocks the ranking. A guess is
replaced by a measured value when one exists.

Order by total suite hours saved per hour of work. Then read the order as a whole: a calculation
can be wrong, and the coordinator may know better. Move any effort it judges misplaced, and record
the reason with that ranking. Rank every problem, even past the budget: efforts below the line run
when earlier ones finish early. When nothing worth doing is left, go to Combine early; the time left
shortens the release.

Re-rank whenever an effort ends: re-run the survey checks and calculations the merged change
touches, update every open problem's saving against the new current best, and append the new
order to `rankings` in `efforts.json`. Do not run the full suites to re-estimate; they run in
Release.

### Efforts

An effort owns one problem: one owner, one branch, one device or slot set, and a time box of about
twice its work estimate.

- **Fastest test first.** Look for a test of minutes in the survey's short tests before any scored
  sample. Use a scored sample only when the question is about scores, completion or harness
  behaviour, and size it as [Sizing a sample](#sizing-a-sample) says.
- **Accepting a change.** A paired exactness check plus the effort's confirmation is enough for a
  change that keeps tokens exact. A change that alters outputs needs a scored pair against the
  current best.
- **Two outcomes.** Record whether the change is `kept`, `dropped` or `none`, and separately whether
  the problem is `solved` or `open`. A real gain that does not solve the problem is kept, and the
  problem returns to the ranking with its new numbers.
- **Merge as you go.** A kept change joins the current best; efforts that start later build on it.
  An effort that started from an older best rebases and repeats its confirmation before merging.
- **Running efforts are not interrupted** by a re-rank. The exception is an effort whose problem a
  merge has already solved: it stops and records what it learned.
- **At the time box,** the effort re-estimates its remaining work and saving, and the coordinator
  decides with the current ranking whether it continues.

The coordinator holds the current best, the slot ledger and the budget, and chooses which efforts
run together and on which lanes. Efforts that touch the same files or the same memory run in turn
or share a branch, and each device has one owner. It reads wide where an effort reads deep, as
[Look at the whole run](#look-at-the-whole-run) asks, and starts the next ranked effort whenever a
lane frees. Every dispatched run registers with the coordinator at launch. At each effort boundary
it re-reads `inputs.json`, applies operator changes and records them in `steering.log`. When it is
blocked, it writes the reason to `blocked.json` at once instead of waiting for the next resume.

### Sizing a sample

Estimate a sample's CI time before dispatching, and size it to what it has to separate. Too small
to beat the noise decides nothing; a full suite to settle what a sample answers costs more.

Shape follows the claim: concurrency needs enough simultaneous conversations to load the pool and
scheduler, precision and long context need long free-running replies, caching needs repeated
multi-turn traffic. A count valid for one is meaningless for another.

Write these to `doc/agentic_evals/`:

- `survey.json`: `checks`, `calculations` and `shortfalls`, as the survey reference names them
- `preflight.json`: `registry`, `disk`, `release_route` and `isolation_smoke`, each with `ok`
- `efforts.json`: `efforts`, per effort `problem`, `owner`, `predicted` (`saving_hours` per eval,
  `work_hours`, `basis`), `confirm` (`method`, `success`), `box_hours`, `status` (`done`,
  `running` or `not_run` with a `reason`), and when done `used_hours`, `change`, `problem_status`,
  `confirmed` (the measured result) and `evidence`; and `rankings`, one entry per ranking with
  `after`, `order` (problem, `saving_hours`, `work_hours`) and `reason` when the order is not by
  saving per hour
- `plan.json`
- `runs/<arm>/` (manifest and collected results)
- `estimate.json`: per eval `decision` (`go` or `stop`) and `reason`
- `full/<eval>/results.json` with `actual_hours` next to the prediction
- `serving_config.json`: the measured `max_concurrency`, `prefix_caching`, `max_num_seqs`, the batch
  each eval used, the agent and request time limits and output cap, and `release_limits`
- `compute.json`: the grant as used — `max_parallel`, peak runs in flight, and for a CI grant the
  published refs and every dispatched run id, so each result traces back to a run
- `REPORT.md`, with each effort's predicted saving and work hours next to its confirmed result and
  hours used, and how estimates compared with results

Update `workload` with the measured values.

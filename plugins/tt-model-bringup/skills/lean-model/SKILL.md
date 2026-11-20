---
name: lean-model
description: Bring a Hugging Face model to a correct, fast TTNN full model on the target mesh in one goal, led by a coordinator with subagents, judged by outputs and tests rather than by a fixed stage order. Use for the lean pipeline's model goal, and for speedup work on an existing model.
---

# Lean model

The outcome and its tests are fixed; the path is yours. What follows is what earlier bringups learned, the good
and the bad, offered as defaults. Where your measurements say otherwise, follow them and write down why.

Load the references when you reach them, not up front: [ops and kernels](references/ops.md),
[mixture of experts](references/moe.md), [one or two machines](references/machines.md).

## Outcome

- `models/autoports/<model>/tt/`: the full model and the `full-model` generator contract: chunked prefill,
  traced decode with on-device split sampling, paged KV, fixed slots, every batch in `batch_sizes`, the
  advertised context (or a hard physical limit with evidence).
- The `reference-accuracy` suite with verdict match at full precision (the implementation is right). The served
  precision may differ from HF by design; it is chosen on the reference tiers as a speed and accuracy tradeoff.
- `doc/model/model.json` and `REPORT.md`: mode, targets, floors, measured, precision, KV pool, ops added or
  changed, tier commands, the perf ledger (change, before, after, tier result), hours and tokens spent, and
  what was not done.

## What tends to work

- **Design for the target before the first kernel.** The parallel layout and how many cores each step uses
  decide speed far more than tuning; a wrong structure is not recovered by leaf work. Derive per-batch targets
  from a roofline (bytes and FLOPs per step against bandwidth and peak) and pick the layout that can reach them.
  Start from the closest model in tt-metal and its fastest kernels where the architecture matches, and check
  each borrowed piece against the reference rather than assuming it fits.
- **Whole and thin before deep.** Get the whole model correct end to end early, at any speed. Then add what
  changes the design: batching with fixed slots, chunked prefill, long context, paged KV, request state. Optimise
  after those exist; work done before them is often redone.
- **Measure from the first run, tune once correct.** Record each module's time against its floor from day one;
  that ranks the work. Do not tune a module that is not yet correct; layout and parallel-structure choices are
  design, not tuning, and are made in the plan.
- **The reference decides numerics.** Build the CPU captures and corpus early, in the background. Every
  precision or kernel change is judged on the reference tiers, not on a few prompts.
- **Touch the end early.** Once the model is correct, drive it with a small request loop over the generator
  (several users, mixed lengths, arrivals mid-decode) and a few eval questions. This is a smoke test, not the vLLM
  adapter, which belongs to the serve goal. Hangs under real traffic, memory exhaustion and routing or state bugs show up there long
  before unit tests catch them.

A workable order: plan (no device) → correct skeleton at B1 → design-shaping features → speed and precision →
hardening. Overlap them wherever the work is independent.

## Tests

| Tier | When | What |
|---|---|---|
| T0 | every change | the changed module against HF captures (default PCC ≥ 0.999 at bf16; a lower bar is written down with its reason) and its op unit tests |
| T1 | every landing | full model B1: reference quick tier; a few greedy prompts; batched equals alone |
| T2 | milestones | reference full and long tiers; every batch; longest context; perf against targets; the request-loop smoke |

Keep T1 near ten minutes so it runs often. Every device run has a timeout. Never loosen a check or delete one
to pass; a numerics change moves the baseline only after T2.

What the tiers must contain, because these bugs pass weaker checks:
- **Batched equals alone:** run the largest batch with mixed prompts and compare each user with the same prompt run
  alone over at least 1,000 generated tokens: identical greedy tokens, and logits bitwise where the ops allow. A
  PCC near 1 is not enough: a difference of 1e-5 can flip a routing or top-k choice many layers later. Name any op
  that is not batch-invariant with its largest difference.
- **Enough positions:** a teacher-forced comparison needs enough positions to resolve the difference it decides
  on; to tell top-1 apart by 0.5 points, use several thousand positions, not a few dozen.
- **Warmup covers every shape:** prefill lengths 1, 2, an odd length, chunk size − 1, chunk size and chunk size + 1,
  and the longest context, at every batch. After warmup, the program-cache count does not change across a
  mixed-length request loop; a change means something compiles per request.
- **Host time per step** is measured beside device time in every perf result. Host work that grows with batch,
  context or table size shows up only there.
- **Model memory map:** keep one map of persistent on-chip and DRAM allocations per module; T2 runs every module
  together at the largest batch and longest context, where allocations of different modules collide.

## Working in parallel

One coordinator plans, briefs, lands and reports, and never blocks on a wait. A subagent gets one deliverable,
the files it owns, the ones it must not touch, its device slot, its tests and how to report. Good units: the
CPU reference, the op inventory, one module or layer kind each, the integration skeleton, later one perf hotspot
each. Run only as many live agents as the coordinator can review: a report should be reviewed and landed or
answered within about an hour, or the next agent waits. Landing: batch reports, cherry-pick onto head, run T1
once, announce the new head; agents rebase.

From the first correct skeleton on, one agent always owns the top item of the perf ranking, and every milestone
report gives measured and floor per batch. Speed work is never parked until the end.

Experiment where a measurement can decide. At most about a fifth of the budget goes to throwaway prototypes on side branches (another
layout, a fused op, a different parallel scheme), run side by side and judged only by measurement. Land the
winner, drop the rest. Scope risky work as "measure and report before building", bugs as "root cause before fix".

## Modes (`inputs.json` `mode`)

`port` (default, a model close to one in tt-metal), `fresh` (a new architecture: spec, captures and per-module
ports first), `speedup` (an existing correct model: start at the perf loop, gated by the reference suite),
`quick` (existing ops only, perf within 2× of target, to reach serving and evals soon).

## Devices and budget

Every device job runs under a lock with a timeout; an unclean exit resets the board and checks it before the
next job (`tt-device-usage`). Set this up before the first device run, not after the first incident. When two
machines disagree on a numeric result, compare firmware and driver versions and rerun on the other machine before
debugging the model. `budget_hours` in `model.json` bounds the goal: use it to get closer to the targets, and when it ends, report
as is. A missed target is reported with its reason; it is not a reason to stop before the budget is spent.
Commit summaries, not raw output (`model-bringup`). One `stage-review`, at the end.

---
name: lean-model
description: Bring a Hugging Face model to a correct, fast TTNN full model on the target mesh in one goal, led by a coordinator with subagents, judged by outputs and tests rather than by a fixed stage order. Use for the lean pipeline's model goal, and for speedup work on an existing model.
---

# Lean model

The outcome and its tests are fixed; the path is yours. What follows is what earlier bringups learned, offered
as defaults. Where your measurements say otherwise, follow them and write down why.

Load the references when you reach them, not up front: [ops and kernels](references/ops.md),
[mixture of experts](references/moe.md), [one or two machines](references/machines.md).

## Outcome

- `models/autoports/<model>/tt/`: the full model and the `full-model` generator contract: chunked prefill,
  traced decode with on-device split sampling, paged KV, fixed slots, every batch in `batch_sizes`, the
  advertised context (or a hard physical limit with evidence).
- The `reference-accuracy` suite with verdict match; the served precision chosen on its tiers.
- `doc/model/model.json` and `REPORT.md`: mode, targets, floors, measured, precision, KV pool, ops added or
  changed, tier commands, and the perf ledger (change, before, after, tier result).

## What tends to work

- **Design for the target before the first kernel.** The parallel layout and how many cores each step uses
  decide speed far more than tuning; a wrong structure is not recovered by leaf work. Derive per-batch targets
  from a roofline (bytes and FLOPs per step against bandwidth and peak) and pick the layout that can reach them.
  Start from the closest model in tt-metal and its fastest kernels.
- **Whole and thin before deep.** Get the whole model correct end to end early, at any speed. Then add what
  changes the design: batching with fixed slots, chunked prefill, long context, paged KV, request state. Optimise
  after those exist; work done before them is often redone.
- **Measure from the first run, tune once correct.** Record each module's time against its floor from day one;
  that ranks the work. Do not tune a module that is not yet correct.
- **The reference decides numerics.** Build the CPU captures and corpus early, in the background. Every
  precision or kernel change is judged on the reference tiers, not on a few prompts.
- **Touch the end early.** Once the model is correct, push a few requests through a minimal server and a few
  eval questions. Hangs under real traffic, memory exhaustion and routing or state bugs show up there long
  before unit tests catch them.

A workable order: plan (no device) → correct skeleton at B1 → design-shaping features → speed and precision →
hardening. Overlap them wherever the work is independent.

## Tests

| Tier | When | What |
|---|---|---|
| T0 | every change | the changed module against HF captures (PCC ≥ 0.999 at bf16) and its op unit tests |
| T1 | every landing | full model B1: reference quick tier; a few greedy prompts; batched equals alone |
| T2 | milestones | reference full and long tiers; every batch; longest context; perf against targets; a served smoke |

Keep T1 near ten minutes so it runs often. Every device run has a timeout. Never loosen a check or delete one
to pass; a numerics change moves the baseline only after T2.

## Working in parallel

One coordinator plans, briefs, lands and reports, and never blocks on a wait. A subagent gets one deliverable,
the files it owns, the ones it must not touch, its device slot, its tests and how to report. Good units: the
CPU reference, the op inventory, one module or layer kind each, the integration skeleton, later one perf hotspot
each. The coordinator's review is the bottleneck, so a handful of live agents is usually the useful limit.
Landing: cherry-pick onto head, run T1, announce the new head; agents rebase.

Experiment freely. Up to about a fifth of the budget can go to throwaway prototypes on side branches (another
layout, a fused op, a different parallel scheme), run side by side and judged only by measurement. Land the
winner, drop the rest. Scope risky work as "measure and report before building", bugs as "root cause before fix".

## Modes (`inputs.json` `mode`)

`port` (default, a model close to one in tt-metal), `fresh` (a new architecture: spec, captures and per-module
ports first), `speedup` (an existing correct model: start at the perf loop, gated by the reference suite),
`quick` (existing ops only, perf within 2× of target, to reach serving and evals soon).

## Devices and budget

Every device job runs under a lock with a timeout; an unclean exit resets the board and checks it before the
next job (`tt-device-usage`). `budget_hours` in `model.json` bounds the goal; when it ends, report as is.
Commit summaries, not raw output (`model-bringup`). One `stage-review`, at the end.

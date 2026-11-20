---
name: lean-model
description: Bring a Hugging Face model to a correct, fast TTNN full model on the target mesh in one goal, led by a coordinator with subagents, judged by outputs and tests rather than by a fixed stage order. Use for the lean pipeline's model goal, and for speedup work on an existing model.
---

# Lean model

The outcome and its tests are fixed; the path is yours. What follows is what earlier bringups learned, the good
and the bad, offered as defaults. Where your measurements say otherwise, follow them and write down why.

Load the references when you reach them, not up front: [ops and kernels](references/ops.md),
[one or two machines](references/machines.md); for routed experts, [MoE](../ttnn-knowledge/topics/moe.md).

## Knowledge

What earlier bringups learned about TTNN, by topic, is in [`ttnn-knowledge`](../ttnn-knowledge/SKILL.md): memory,
numerics, prefill and KV, tracing, multi-chip and CCL, fusion, MoE, performance and profiling, matmul and
precision, serving and evals, reference code. Read a topic when the work reaches it. The generator and context
contract the outcome refers to is defined in [full-model](../full-model/SKILL.md); its stage procedure does not apply
here.

## Outcome

- `models/autoports/<model>/tt/`: the full model and the `full-model` generator contract: chunked prefill,
  traced decode with on-device split sampling, paged KV, fixed slots, every batch in `batch_sizes`, the
  advertised context (or a hard physical limit with evidence).
- The `reference-accuracy` suite with verdict match at full precision (the implementation is right). The served
  precision may differ from HF by design; it is chosen on the reference tiers as a speed and accuracy tradeoff.
- `doc/model/model.json` and `REPORT.md`: mode, targets, floors, measured, precision, KV pool, ops added or
  changed, tier commands, the perf ledger (change, before, after, tier result), hours and tokens spent, and
  what was not done; for a mixture-of-experts model, `moe` with experts, top-k, layout, distinct experts per
  step and decode against its floor at batch 1 and the largest batch.

## What tends to work

- **Design for the target before the first kernel.** The parallel layout and how many cores each step uses
  decide speed far more than tuning; a wrong structure is not recovered by leaf work. Derive per-batch targets
  from a roofline (bytes and FLOPs per step against bandwidth and peak) and pick the layout that can reach them.
  Similar models in tt-metal are worth reading while planning; anything reused from them is checked against the
  reference rather than assumed to fit.
- **Whole and thin before deep.** Get the whole model correct end to end early, at any speed. Then add what
  changes the design: batching with fixed slots, chunked prefill, long context, paged KV, request state. Optimise
  after those exist; work done before them is often redone.
- **Measure from the first run, tune once correct.** Record each module's time against its floor from day one;
  that ranks the work. Do not tune a module that is not yet correct; layout and parallel-structure choices are
  design, not tuning, and are made in the plan.
- **The reference decides numerics.** Build the CPU captures and corpus early, in the background. Every
  precision or kernel change is judged on the reference tiers, not on a few prompts.
- **Touch the end early.** Once the model is correct, drive it with a small request loop over the generator
  (several users, mixed lengths, arrivals mid-decode) and a few eval questions. This is a smoke test, not the
  vLLM adapter, which belongs to the serve goal. Hangs under real traffic, memory exhaustion and routing or state
  bugs show up there long before unit tests catch them.

A workable order: plan (no device) → correct skeleton at B1 → design-shaping features → speed and precision →
hardening. Overlap them wherever the work is independent.

## Heads-ups

Things that have cost earlier bringups time. They are not requirements; use them where they apply.

- Speed work tends to get crowded out by correctness and serving fixes. Keeping someone on the top perf item
  once the model is correct has helped.
- PCC close to 1 can still hide a small difference that flips a discrete choice (expert routing, top-k) later in
  the model. Comparing batched against alone on generated tokens, not only PCC, has caught this.
- A teacher-forced comparison on a few dozen positions cannot separate small top-1 differences; size it to the
  decision.
- Some bugs showed up only with real traffic: kernels compiling per request after warmup, host work per step,
  allocations of different modules colliding, unusual prompt lengths (very short, at chunk edges).
- Running several agents on one board without the `tt-device-usage` rules has hung boards and lost hosts.
- Firmware differences between machines have looked like model bugs.

## Tests

| Tier | When | What |
|---|---|---|
| T0 | every change | the changed module against HF captures (default PCC ≥ 0.999 at bf16; a lower bar is written down with its reason) and its op unit tests |
| T1 | every landing | full model B1: reference quick tier; a few greedy prompts; batched equals alone |
| T2 | milestones | reference full and long tiers; every batch; longest context; perf against targets; the request-loop smoke |

Keep T1 near ten minutes so it runs often. Every device run has a timeout. Never loosen a check or delete one
to pass; a numerics change moves the baseline only after T2.

## Working in parallel

One coordinator plans, briefs, lands and reports, and never blocks on a wait. A subagent gets one deliverable,
the files it owns, the ones it must not touch, its device slot, its tests and how to report. Good units: the
CPU reference, the op inventory, one module or layer kind each, the integration skeleton, later one perf hotspot
each. The coordinator's review is the bottleneck, so a handful of live agents is usually the useful limit.
Landing: cherry-pick onto head, run T1, announce the new head; agents rebase.

Experiment where a measurement can decide. A share of the budget, which you choose and record, can go to
throwaway prototypes on side branches (another layout, a fused op, a different parallel scheme), run side by side
and judged only by measurement. Land the
winner, drop the rest. Scope risky work as "measure and report before building", bugs as "root cause before fix".

## Modes (`inputs.json` `mode`)

`port` (default, a model close to one in tt-metal), `fresh` (a new architecture: spec, captures and per-module
ports first), `speedup` (an existing correct model: start at the perf loop, gated by the reference suite),
`quick` (existing ops only, speed secondary, to reach serving and evals soon).

## Devices and budget

Before the first device run, record in `model.json` whether bounded resets and multi-chip health checks are
authorized on the run's machines; without it, the first unclean exit stops the goal until someone answers. Every
device job runs under a lock with a timeout, and after an unclean exit the board is reset and checked before the
next job (`tt-device-usage`).

`budget_hours` in `model.json` bounds the goal: use it to get closer to the targets, and when it ends, report as
is. A missed target is reported with its reason; it is not a reason to stop before the budget is spent.

Commit summaries, not raw output, and when something took long to understand and is solved, add a few lines to
`bringup/lessons.md` (`model-bringup`). One `stage-review`, at the end.

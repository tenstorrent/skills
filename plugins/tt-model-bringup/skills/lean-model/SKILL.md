---
name: lean-model
description: Bring a Hugging Face model to a correct, fast TTNN full model on the target mesh in one goal, led by a coordinator with subagents, judged by outputs and tests rather than by a fixed stage order. Use for the lean pipeline's model goal, including making an existing correct model faster.
---

# Lean model

The outcome and its gate are fixed; how to get there is yours. Below is what earlier bringups learned, good and
bad. Use it where it fits this run.

Read when the work reaches them: [ops and kernels](references/ops.md); [MoE](../ttnn-knowledge/topics/moe.md) for
routed experts; TTNN knowledge by topic in [`ttnn-knowledge`](../ttnn-knowledge/SKILL.md). The generator and
context contract is defined in [full-model](../full-model/SKILL.md); its stage procedure does not apply here.

## Outcome

- `models/autoports/<model>/tt/`: the full model and the `full-model` generator contract: chunked prefill,
  traced decode with on-device split sampling, paged KV, fixed slots, every batch in `batch_sizes`, the
  advertised context (or a hard physical limit with evidence).
- The `reference-accuracy` suite with verdict match at full precision. The served precision may differ from HF
  by design; it is chosen on the reference tiers.
- `doc/model/model.json` and `REPORT.md`: scope, `started_at`, `budget_hours`, `hours_used`,
  `reset_authorization`, targets and floors per batch, measured, precision, KV pool, tier commands, the perf
  ledger (change, before, after, test result), ops added or changed, and what was not done. For a
  mixture-of-experts model, `moe`: experts, top-k, layout, and distinct experts per step and decode against its
  floor at batch 1 and the largest batch.

## What earlier bringups learned

- The parallel layout and how many cores each step uses decided speed more than later tuning did. A roofline
  (bytes and FLOPs per step against bandwidth and peak) gave targets to plan the layout against.
- Optimisation done before batching, chunked prefill, long context and paged KV existed was often redone.
- Similar models in tt-metal helped planning; pieces reused from them sometimes did not fit and were caught only
  against the reference.
- A CPU reference built early, in the background, let precision and kernel changes be judged on it rather than on
  a few prompts.
- A small request loop over the generator (several users, mixed lengths, arrivals mid-decode) found bugs long
  before unit tests did: hangs under real traffic, memory exhaustion, per-request compiles after warmup, host
  work per step, colliding allocations, very short prompts and chunk edges.
- Speed work got crowded out by correctness and serving fixes.
- PCC close to 1 hid small differences that flipped routing or top-k later in the model; batched against alone
  on generated tokens caught them. A teacher-forced comparison on a few dozen positions could not separate small
  top-1 differences.
- Devices: agents sharing a board without the `tt-device-usage` rules hung boards and lost hosts; firmware
  differences between machines looked like model bugs; with several agents per machine the device lock became
  the bottleneck; concurrent C++ rebuilds of one tree broke each other; the CPU reference can take much of the
  host's memory.

## Tests

Record in `model.json` `tiers` the commands for a per-change check (T0), a quick full-model check on every
landing (T1), and a full check at milestones (T2: the reference full and long tiers, every batch, the longest
context, perf against targets). What else goes in each is your call. Every device run has a timeout. Never
loosen or delete a check to pass; a numerics change moves the baseline only after T2.

## Working in parallel

One coordinator; subagents as you see fit, each with its own files, tests and device access. Every landing passes
T1. Throwaway prototypes on side branches, judged by measurement, are welcome.

## Run settings

- Where to start (a similar tt-metal model, an existing implementation of this one, or from scratch) is your
  call; write down why.
- `scope` in `inputs.json` (`model-bringup`): `contained` keeps every change inside the model folder and
  `bringup/`; `open` lets the model carry its own versions of ops and shared code. [ops](references/ops.md) has
  the options under each.
- `budget_hours` in `inputs.json` (default 24, wall clock). Copy it and `started_at` (UTC) into `model.json` at
  startup so the time left survives compaction and resumes. T2, the review and the report fit inside it. A missed
  target is reported with its reason; it is not a reason to stop early.
- `reset_authorization` goes in `model.json` before the first device run, from the run's brief; if nobody has
  said, ask then. Device jobs follow `tt-device-usage`.
- Commit summaries, not raw output. When something took long to understand and is solved, add a few lines to
  `bringup/lessons.md` (`model-bringup`). One `stage-review`, at the end.

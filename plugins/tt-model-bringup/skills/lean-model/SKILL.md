---
name: lean-model
description: Bring a Hugging Face model to a correct, fast TTNN full model on the target mesh in one goal, led by a coordinator with subagents, judged by outputs and tests rather than by a fixed stage order. Use for the lean pipeline's model goal, and for speedup work on an existing model.
---

# Lean model

One goal replaces the strict stages 1–8: a correct, fast full model with its reference suite, precision choice
and performance ledger. How to get there is the coordinator's choice. The outputs and tests below are not.

## Outcome

- `models/autoports/<model>/tt/`: the full model and the standard generator contract (`full-model` skill's
  generator section): chunked prefill, traced decode with on-device split sampling, paged KV, fixed slots,
  every batch in `batch_sizes`, the advertised context (or a hard physical limit with evidence).
- The `reference-accuracy` suite and its verdict: full precision matches HF; the served precision is the
  baseline every later change is gated on.
- `doc/model/model.json` and `REPORT.md`: mode, targets, floors, measured numbers, precision, KV pool,
  tiers, and the perf ledger (one row per change: what, before, after, accuracy tier result).

## Plan first (an hour, no device)

Read the HF config and code; write SPEC.md (every op's exact rule, shapes, dtypes, layer kinds). Find the
closest model already in tt-metal and start from its architecture and fastest kernels, not from a simple
version meant to be sped up. Set targets from a roofline: weight and KV bytes per step over DRAM bandwidth,
FLOPs over peak, per batch; a target is a multiple of its floor. Budget memory: weights, KV pool (largest
that fits), reserves, trace region. Map every op to an existing ttnn op at model shapes; list gaps.

## Modes (`inputs.json` `mode`)

- `port` (default): a model close to one in tt-metal; reuse its layout and ops.
- `fresh`: a new architecture; spec, reference captures and per-module work come first.
- `speedup`: an existing correct model; start at the perf loop with the reference suite as the gate.
- `quick`: correctness and serving first, existing ops only, perf to within 2× of target.

## Coordinator and subagents

One coordinator plans, briefs, lands and reports; it never blocks on a wait. Subagents get one deliverable
each, own named files, work in their own worktree and branch, and report in one line plus a notes file.
Run independent work in parallel: reference captures on CPU, op inventory, per-module ports, later one agent
per perf hotspot. Two competing designs may run side by side when a measurement decides between them. About
four live subagents is the useful limit: the coordinator's review is the bottleneck. Scope risky work as
"measure and report before building"; bugs as "root cause before fix"; time-box both.

Landing: the coordinator cherry-picks onto head, runs T1, fast-forwards, announces the head; agents rebase.

## Tests (the gates)

| Tier | When | What |
|---|---|---|
| T0 | every change | the changed module against HF captures (PCC ≥ 0.999 at bf16) and its op unit tests |
| T1 | every landing | full model B1: reference quick tier; short greedy prompts; batched equals alone |
| T2 | milestones | reference full and long tiers; every batch; longest context; perf sweep vs targets |

Never loosen a check or delete a check line to pass. A numerics change moves the baseline only after T2.

## Ops and performance

Correct with existing ttnn ops first. Then profile a traced step, rank ops by time lost against their floor,
and fix the top item: config, then fusion, then a new op. A generic fix goes into the ttnn op with unit tests;
a model-specific fusion is a model-local `ttnn.generic_op`. Every new path keeps a switch back. Precision is
chosen inside this loop on the reference tiers (`datatype-sweep` rules), not in a separate stage.

## Devices and budget

Every device job runs under a lock with a timeout; an unclean exit resets the board and checks it before the
next job (`tt-device-usage`). One goal budget in hours, written in `model.json`; when it ends, report as is.
Commit summaries, not raw output (`model-bringup`). One `stage-review` at the end, not per step.

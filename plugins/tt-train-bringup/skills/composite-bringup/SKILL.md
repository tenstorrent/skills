---
name: composite-bringup
description: Implement the full training model in tt-train (ttml autograd) on the plan's chips (one chip, or the plan's TP/DP layout when the model does not fit one), correctness first, using existing kernels wherever the plan's kernel inventory has them and composed ttml ops elsewhere, and build the PCC harness that compares the forward output and every gradient against torch. Use as the second phase of train-bringup.
---

# Composite bring-up

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup)
and read `doc/plan.md` from the plan phase.

## What to build

Implement every layer kind and the full model in ttml autograd under
`tt-train/sources/ttml/ttml/models/<model>/`, with a training example under
`tt-train/sources/examples/<model>_train/` that runs real steps (data, loss, optimiser) on the
plan's chips (one chip, or the TP/DP layout of the plan's multi-chip fit section).
Follow the kernel inventory: call an existing ttnn or ttml kernel wherever one exists, compose from
ttml ops everywhere else. Reshapes, permutes, concats and slices are allowed in this phase; they are
what the loop removes later. Load HF weights through a documented loader. Apply the freeze map from
day one: every backward op takes per-input `requires_grad` flags and allocates no gradient for a frozen
input.

Keep one layout contract from the start: activations cross module boundaries as `[B, 1, T, W]` in
tile layout with heads merged on the last dimension. A module that needs another layout converts
internally so the cost is visible to its owner. Putting a head axis on dimension −2 pads it to 32
rows and every following op streams the padding; avoid it where an existing kernel allows, and
record it where it cannot be avoided yet.

## PCC harness

Build the harness in this phase; every later verification reuses it. Per module and per layer kind,
at real shapes and the real dtype policy, compare against a torch fp64 reference fed with
bf16-rounded inputs: the forward output and **every** gradient (inputs and trainable parameters).
Report PCC and relative error per tensor; PCC above 0.999 can hide a systematic bias, so both are
required. Then run N training steps of the full model and compare the loss trajectory against the
torch reference within a stated tolerance. Put the harness under `tt-train/tests/python/` so it runs
as an ordinary test.

When the plan's layout has tensor or data parallelism, the harness also runs every module at the
per-rank shard shapes (the shapes the kernels actually see; a single-chip-only harness let the
first TP chain of the reference bring-up crash on shapes it had never tested), and the loss match
runs at that layout with the loss gathered on the host. The loss script fails loudly: a missing
loss log, fewer than N steps, or a NaN is a failed gate, never an empty file to read past later.

Never shrink hidden size, head counts, sequence length or weight shapes to make evidence easier to
collect; small shapes are for debugging only.

## Baseline measurement

Before leaving this phase, take the first measurement so the loop has a baseline: a cooled MFU run
without the profiler on the full model on the plan's chips, telemetry sampler attached (wall
ms/step, MFU against the nominal peak with the plan's FLOP count, per-chip settled clock and the
critical chip next to it) and Tracy runs at four doubling depths of whole blocks (the smallest
repeating group of layer kinds) with real shapes, fitted and projected to the full depth as
described in the
[optimisation loop's measurement protocol](../optimization-loop/SKILL.md#measurement-protocol).
Never profile the full layer stack. Measure the host/dispatch gap here (wall-clock minus summed
device time, with Tracy's cycles converted to the delivered clock first); the plan does not guess
it. Attribute the top three ttnn ops to their compiled kernel source, DEST sync mode and % of peak
(the plan's section 1 entries, now confirmed from the profile) so the loop's first iteration has
its candidate. Create `doc/optimization/README.md` and `doc/perf_analysis.md` in the
[loop's document format](../optimization-loop/SKILL.md#document-format): the result table with one
row (composite baseline), the first ranked cost table at projected full depth, and the four raw
Tracy measurements with their fit, residuals and projection in the evidence directory.

## Gate

PCC harness passes for every module and layer kind on the forward and every gradient at real shapes,
including the per-rank shard shapes when the plan's layout is multi-chip. Loss over N steps matches
torch within tolerance at the plan's layout. Full model runs training steps on the plan's chips
within the plan's memory budget. `doc/perf_analysis.md` exists with the baseline, the measured
host/dispatch gap and the top-op kernel attribution. `doc/composite/README.md`
and `work_log.md` record commands, PCC and relative-error tables, perf, limitations and artefacts.
`stage-review` returns `clean-pass`.

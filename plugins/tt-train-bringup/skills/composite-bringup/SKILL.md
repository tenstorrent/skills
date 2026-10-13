---
name: composite-bringup
description: Implement the full training model in tt-train (ttml autograd) on one chip, correctness first, using existing kernels wherever the plan's kernel inventory has them and composed ttml ops elsewhere, and build the PCC harness that compares the forward output and every gradient against torch. Use as the second phase of train-bringup.
---

# Composite bring-up

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup)
and read `doc/plan.md` from the plan phase.

## What to build

Implement every layer kind and the full model in ttml autograd under
`tt-train/sources/ttml/ttml/models/<model>/`, with a training example under
`tt-train/sources/examples/<model>_train/` that runs real steps (data, loss, optimiser) on one chip.
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

Never shrink hidden size, head counts, sequence length or weight shapes to make evidence easier to
collect; small shapes are for debugging only.

## Baseline measurement

Before leaving this phase, take the first measurement so the loop has a baseline: an MFU run without
the profiler (ms/step, MFU computed with the plan's FLOP count) and a Tracy run on a reduced model
with one real layer of each kind. Create `doc/perf_analysis.md` with the MFU history table (one row:
composite baseline) and the first ranked cost table from the Tracy CSV.

## Gate

PCC harness passes for every module and layer kind on the forward and every gradient at real shapes.
Loss over N steps matches torch within tolerance. Full model runs training steps on one chip within
the plan's memory budget. `doc/perf_analysis.md` exists with the baseline. `doc/composite/README.md`
and `work_log.md` record commands, PCC and relative-error tables, perf, limitations and artefacts.
`stage-review` returns `clean-pass`.

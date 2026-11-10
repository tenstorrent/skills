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
Follow the kernel inventory's selected training paths: use compatible existing fused kernels and
program configs, compose from ttml ops where support is missing, and document why a fallback is
needed. Load HF weights through a documented loader. Apply the freeze map from day one: per-input
`requires_grad` must suppress the gradient computation, allocation and reduction, not merely discard
its result. Preserve input gradients through frozen modules wherever earlier trainable parameters
need them.

Keep one layout contract from the start: activations cross module boundaries as `[B, 1, T, W]` in
tile layout with heads merged on the last dimension. A module that needs another layout converts
internally so the cost is visible to its owner. Putting a head axis on dimension −2 pads it to 32
rows and every following op streams the padding; avoid it where an existing kernel allows, and
record it where it cannot be avoided yet.

## Mesh code rules

On a multi-chip layout (the plan's `mesh` block) the model follows these rules from the first
commit; each is a numerics bug that a single-chip test never shows:

- LoRA adapters are sharded with their base weight. A column-parallel base (q, k, v, gate, up)
  shards adapter B on its output dimension and replicates A; a row-parallel base (o, down)
  shards adapter A on its input dimension and replicates B. The adapter product then lands in the
  same shard as the base output and needs no extra collective.
- DP syncs trainable gradients only: `ttml.sync_gradients` (or its equivalent) runs over the
  parameters with `requires_grad`, on the `dp` axis only, after the backward and before the
  optimiser. Frozen weights never get a gradient to sync. TP-sharded gradients are already
  per-shard correct and are not reduced over `tp`.
- The seed is the same on every rank, so replicated parameters (adapter A, norms) start
  identical; check it once by comparing a replicated parameter across ranks after init.
- Data is sharded from the global batch in reference order: the global batch is the batch the
  torch reference trains on, and DP rank `i` takes its `i`-th contiguous slice. The continuation
  goldens are made at the global batch and stay valid for any DP size.
- The loss is the mean over the global batch: the mean of per-rank token-weighted losses over the
  total number of loss tokens, not a mean of per-rank means when ranks carry different token
  counts. With the gradient averaged over `dp` the update equals the single-replica one.

## Before accepting the first baseline

Correctness-first includes removing avoidable work using existing capabilities. During implementation
and the baseline profile below, check these properties and record the evidence in `doc/composite/README.md`:

- The executed backward agrees with the freeze map: no unused base-weight, norm-parameter or
  embedding gradients, and all required activation and adapter gradients remain. Check op emission
  as well as gradient values; a discarded dW can leave numerical tests passing.
- Constant preparation happens once when valid: frozen embedding untilize, weight packing or
  transposition, and position caches. Do not cache data that changes with optimiser updates or
  inputs; include persistent copies in the memory budget.
- Large matmuls use the selected orientation and explicit supported program configs where needed,
  in both forward and backward at the actual shard shapes. Confirm those paths execute; a config
  keyed only on the global shape can silently miss every TP call.
- Explain each large matmul, collective, layout conversion and host synchronisation in a step.
  Remove redundant conversions, repeated constant work and unnecessary host reads using existing
  paths. Retain required communication, data/loss work and correctness checks. Necessary layout
  ops are allowed; record their reason and measured cost for the loop.

Fix violations before measuring and accepting the composite baseline. There is no universal minimum initial MFU.
New optimisation kernels and exhaustive config sweeps belong to the loop after the baseline uses
the planned capabilities and passes the forward, gradient and loss checks.

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
Record actual compute cores, participating chips, useful FLOPs and nominal peak with the baseline.
Exclude compilation and warmup from timing, and inspect per-step time and loaded clock trends;
if they are still settling, extend the run to report a settled window or label the result transient.
Keep cold, warm, random-init and real-weight measurements distinct when comparing runs.

## Gate

PCC harness passes for every module and layer kind on the forward and every gradient at real shapes,
including the per-rank shard shapes when the plan's layout is multi-chip. Loss over N steps matches
torch within tolerance at the plan's layout. Full model runs training steps on the plan's chips
within the plan's memory budget. The first-baseline checks above have evidence, with no frozen
gradient work or redundant constant preparation deferred to the loop. `doc/perf_analysis.md` exists
with the baseline, the measured host/dispatch gap and the top-op kernel attribution. `doc/composite/README.md`
and `work_log.md` record commands, PCC and relative-error tables, perf, limitations and artefacts.
`stage-review` returns `clean-pass`.

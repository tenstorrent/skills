---
name: train-plan
description: Plan a tt-train training bring-up before writing device code. Produces the kernel inventory (existing ttnn/ttml forward and backward per reference op, with the compiled kernel source and % of peak of the top ops), the freeze map (which parameters and inputs need gradients), the roofline target (wall ms/step and MFU at the nominal peak of the authorised chips), the memory budget (weights, optimiser state, activations, recompute policy) and the hardware ceiling (clock under load, power limits, critical chip, multi-chip fit). Use as the first phase of train-bringup.
---

# Plan

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup).

No model code runs on the device in this phase; the only device use is the hardware
characterisation of section 5. The five artefacts below are what the later phases build and
measure against. Produce them from the reference torch model (`modeling_*.py`, the HF config), the
training recipe and the target chips, and write them to `doc/plan.md` under the model directory.

Copy the user's hard constraints (precision, normally HiFi4 with fp32 accumulation; batch; forbidden
modes and runs) verbatim into a `## Hard constraints` block at the top of `doc/plan.md`. Every later
phase reads it before any benchmark; a forbidden mode is never run "just to compare".

## 1. Kernel inventory

Walk the reference model's forward and backward op by op (projections, attention or linear
attention, activations, norms, embeddings, loss, optimiser). For every op record: whether an existing
ttnn or ttml kernel implements the forward, whether a backward exists, the layout and dtype it
accepts, and what is missing. Search `ttnn/cpp/ttnn/operations/**`, the Python golden functions in
`ttnn/ttnn/operations/*.py`, `tt-train/sources/ttml/ops/`, `tt-train/sources/ttml/metal/ops/`, and
any inference implementation of the same model under `models/`. Inference bring-ups leave forward
kernels behind; those are free, and only their backwards need to be written. A forward that already
exists in ttnn and a backward that is "two convs with a flag" is a different cost from a new fused op.

For the three most expensive ops of the step (from the roofline below, normally the projection
matmuls and attention) name the kernel that will actually be compiled: the program factory and
kernel source files, including any fork a ttml path selects (a `_metal2` variant found on day 35 of
the reference bring-up belonged on this line on day 1), the DEST sync mode (half sync caps
fp32-accumulate throughput), the core grid on the model's shapes, and the expected fraction of the
FPU peak. These three ops are the loop's first candidates; "ttnn matmul, exists" is not an entry.
When the layout of section 5 is multi-chip, list the per-rank shard shapes next to the full shapes;
the composite harness and the loop's kernels work on the shard shapes.

## 2. Freeze map

For every parameter and every module input, record whether a gradient is needed. Full fine-tuning:
everything trains, so every weight gradient matmul is useful math. LoRA or partial fine-tuning:
frozen weights, norm gammas, embedding tables and conv taps never need a gradient tensor allocated,
written or reduced. The map feeds `requires_grad` flags into every backward op in the composite
phase and is the detector for the wasted class in the loop.

## 3. Roofline target

For every useful op per layer kind: `max(FLOPs / peak FLOP/s, bytes / DRAM bandwidth)` on the target
chips, counting forward, backward (dX and dW) and recompute if the memory budget forces it. Sum per
layer, scale by layer counts, add the fixed costs (embedding, LM head, loss, optimiser) and, for a
multi-chip layout, the collective bytes per step at the measured link bandwidth. The result is the
achievable ms/step and MFU. Record the useful FLOPs per step (`6 × trainable params × tokens` plus
attention terms) so MFU runs can be computed the same way later.

The target is one wall-clock ms/step and one MFU figure against the nominal peak of the authorised
chips (for example 1350 MHz, 148.5 TFLOP/s per chip; the box's sum for a TP run), the same number
the loop's MFU runs report. It is never cycle time, never a single-chip number for a model that
needs the box, and never re-based to the delivered clock. The delivered clock from section 5 is
written next to it as the margin: "target 45 % at 1350 MHz; the 125 W cap delivers ~1.1 GHz, so the
ceiling on this board is ~38 % unless the limit is raised". Do not budget a host or dispatch gap by
assumption; the composite phase measures it (wall-clock minus summed device time at the delivered
clock). This number is the exit condition of the optimisation loop.

## 4. Memory budget

Weights at their stored dtype, gradients, optimiser state (fp32 AdamW is 8 bytes per trainable
parameter), saved activations at the target sequence length and batch, plus workspace, against the
chip's DRAM. Decide from this the recompute policy (none, per block, per sub-module), whether the
optimiser state can be fp32, and whether the full model fits on one chip at the target sequence
length at all. Recompute multiplies every forward cost in the loop, so it must be fixed here.

## 5. Hardware ceiling

Characterise the authorised chips on day 1, before any model code, with the plugin's scripts
(`scripts/tt_clock_ladder.py` with `scripts/tt_telemetry_sampler.py` in a child process, summarised
by `scripts/tt_telemetry_summary.py`; see `tt-device-usage`, "Telemetry And Clock"). Record in the
plan, with the table or figure attached:

- the per-chip settled AICLK under idle, a few-core matmul, a third of the grid and the full grid,
  all at the recipe's fidelity; the clock that holds through the full-grid phase is the delivered
  clock, and the TDP at which it drops is the power cap;
- the limits (`tt-smi` TDP, TDC, thermal, board power), the firmware bundle version, the throttler
  masks seen, and the critical chip (lowest clock or highest current under load; it sets the step);
- whether the limit can be raised: which bundle or board setting it needs and the exact command.
  Firmware-state commands (TDP or clock limits) are user-run: write the invocation into the plan,
  ask the hardware owners once on day 1, and continue with the measured ceiling meanwhile;
- the multi-chip fit: from the memory budget, whether the model fits one chip at the target
  sequence length and batch. If not, the TP/DP layout the box allows, the user authorisation it
  needs, the collective bytes per step and their floor at the measured link bandwidth. The result
  metric for a model that needs the box is the full-model run over the box; no single-chip subset
  number stands in for it.

Under a power cap, bytes moved, cores spinning and DRAM traffic cost clock on every chip; the loop
chooses kernel variants by cooled wall ms with telemetry attached, not by cycles. Say so in the plan
so the composite phase's layout choices already favour fewer bytes and fewer active cores.

## Gate

`doc/plan.md` contains the hard-constraints block and the five artefacts with numbers, the exact
reference commit and HF revision, the recipe, the chips and their firmware bundle. The roofline
target is a single wall ms/step and MFU figure at the nominal peak with its derivation and the
delivered-clock margin; the top three ops name their compiled kernel source and % of peak; the
multi-chip fit decision is explicit. `stage-review` returns `clean-pass`.

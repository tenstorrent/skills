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

Also inspect existing training implementations and shared modules in the repository for reuse,
including packed projections, native GQA, fused activations and frozen-parameter paths. Record the
selected implementation and source revision, and check its backward and shard-shape support; an
inference graph alone does not supply autograd. Prefer compatible existing fused kernels over
decomposing them into primitive ops. Record a concrete correctness, shape or layout reason for a
fallback. Reuse is part of the first implementation, not deferred optimisation.

For the selected paths, specify forward and backward layouts at module boundaries, logical versus
padded shapes, and which constant transformations happen once at load time. Include the matmul
orientation and program config at the actual sequence, batch and per-rank shapes. Explain which
dominant shapes are compute-bound or bandwidth-bound; another model's initial MFU is not a target.

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
Check that each backward actually skips the corresponding kernel, allocation and collective;
discarding a computed gradient is not a skip. Frozen weights can still require input gradients
to reach earlier trainable parameters: prune each gradient independently, never detach an entire
frozen module just because its parameters do not train.

## 3. Roofline target

For every useful op per layer kind: `max(FLOPs / peak FLOP/s, bytes / DRAM bandwidth)` on the target
chips, counting forward, backward terms required by the freeze map and recompute if the memory
budget forces it. Sum per layer, scale by layer counts, add the fixed costs (embedding, LM head,
loss, optimiser) and, for a
multi-chip layout, the collective bytes per step at the measured link bandwidth. The result is the
achievable ms/step and MFU. Derive useful FLOPs from the reference forward and required backward:
a dense projection contributes `2 × M × N × K` for its forward and for each required dX or dW
product. Include attention and adapter work. For LoRA, include the frozen base's forward and
required dX, exclude its dW, and include adapter gradients; `6 × trainable params × tokens` misses
most of this work. The `6 × params × tokens` shortcut is valid only where the actual full-training
graph supports it. Budget recompute in execution time but exclude repeated recompute FLOPs from
the useful-work MFU numerator. Record the counting convention and derivation for later runs.

For a distributed step count useful work over the global batch once: TP shards partition that work,
while DP replicas process distinct samples. Derive the nominal denominator from the actual compute
grid and participating chips at the documented precision/fidelity convention. Record these alongside
batch, sequence, TP/DP and useful FLOPs; never copy a previous box's core count or multiply TP work
by the chip count again. Cross-box comparisons report wall time and both denominators explicitly.

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
chip's DRAM. Include graph-retained activations, padding, temporary layout buffers, collective
buffers and any additional stored weight orientations at their peak lifetime, per rank. Decide
from this the recompute policy (none, per block, per sub-module), whether the
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

### Layout choice on an authorised box

When the user authorises a box (`authorised_hardware`, or the shapes the pipeline lists as
candidates), the layout is chosen by cost on that box, even if the model fits one chip: an
authorised box is the result configuration, and a model that fits one chip can still be faster
as DP over four. Cost every layout on every allowed shape that passed its mesh smoke
(`tt-device-usage`, "Mesh Setup"): DP, TP, and DP x TP where the shape has two axes larger than
one. For each (shape, layout) record, in one table:

- per-rank memory: weights (sharded or replicated), gradients, optimiser state, saved activations
  at the per-rank batch, collective buffers, against one chip's DRAM; a layout that does not fit
  is listed with its overshoot, not dropped silently;
- compute roofline per rank: the useful FLOPs each rank executes (TP partitions a layer's work,
  DP partitions the batch) at one chip's nominal peak, and the per-rank shard shapes of the top
  three ops, since a TP shard can fall below the shape a kernel runs well at;
- collective bytes per step: DP all-reduces trainable gradients only (LoRA adapters are tiny,
  full fine-tuning is the whole model); TP all-reduces or all-gathers activations in the forward
  and their gradients in the backward, twice per block for a Megatron split; at the measured
  link bandwidth per axis (the smoke's `--bandwidth-mib` figure until the plan measures better),
  with whether it can overlap with compute;
- the resulting step time and MFU over the box's nominal peak (chips x per-chip peak).

The plan picks the fastest layout that fits, says why in one line, and names the runner-up with
its number. Choosing a layout without the others costed, or costing only one shape, is a plan
defect. The chosen shape, axis names (`dp`, `tp`) and layout go into a `mesh` block of
`doc/plan.md` with the global batch (per-rank batch x DP size) the continuations will use.

Under a power cap, bytes moved, cores spinning and DRAM traffic cost clock on every chip; the loop
chooses kernel variants by cooled wall ms with telemetry attached, not by cycles. Say so in the plan
so the composite phase's layout choices already favour fewer bytes and fewer active cores.

## Gate

`doc/plan.md` contains the hard-constraints block and the five artefacts with numbers, the exact
reference commit and HF revision, the recipe, the chips and their firmware bundle. The roofline
target is a single wall ms/step and MFU figure at the nominal peak with its derivation and the
delivered-clock margin; the top three ops name their compiled kernel source and % of peak; the
multi-chip fit decision is explicit; on an authorised box every allowed shape and layout is costed
and the fastest that fits is chosen. The inventory selects reusable training paths, layouts and
one-time constant preparation for the first baseline; the useful-FLOP derivation follows the
freeze map and the denominator uses the actual compute grid. `stage-review` returns `clean-pass`.

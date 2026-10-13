---
name: train-plan
description: Plan a tt-train training bring-up before writing device code. Produces the kernel inventory (existing ttnn/ttml forward and backward per reference op), the freeze map (which parameters and inputs need gradients), the roofline target (achievable ms/step and MFU on one chip) and the memory budget (weights, optimiser state, activations, recompute policy). Use as the first phase of train-bringup.
---

# Plan

Before using this skill, follow the startup in [the train-bringup entrypoint](../train-bringup/SKILL.md#startup).

Nothing on the device is written in this phase. The four artefacts below are what the later phases
build and measure against. Produce them from the reference torch model (`modeling_*.py`, the HF
config), the training recipe and the target chip, and write them to `doc/plan.md` under the model
directory.

## 1. Kernel inventory

Walk the reference model's forward and backward op by op (projections, attention or linear
attention, activations, norms, embeddings, loss, optimiser). For every op record: whether an existing
ttnn or ttml kernel implements the forward, whether a backward exists, the layout and dtype it
accepts, and what is missing. Search `ttnn/cpp/ttnn/operations/**`, the Python golden functions in
`ttnn/ttnn/operations/*.py`, `tt-train/sources/ttml/ops/`, `tt-train/sources/ttml/metal/ops/`, and
any inference implementation of the same model under `models/`. Inference bring-ups leave forward
kernels behind; those are free, and only their backwards need to be written. A forward that already
exists in ttnn and a backward that is "two convs with a flag" is a different cost from a new fused op.

## 2. Freeze map

For every parameter and every module input, record whether a gradient is needed. Full fine-tuning:
everything trains, so every weight gradient matmul is useful math. LoRA or partial fine-tuning:
frozen weights, norm gammas, embedding tables and conv taps never need a gradient tensor allocated,
written or reduced. The map feeds `requires_grad` flags into every backward op in the composite
phase and is the detector for the wasted class in the loop.

## 3. Roofline target

For every useful op per layer kind: `max(FLOPs / peak FLOP/s, bytes / DRAM bandwidth)` on the target
chip, counting forward, backward (dX and dW) and recompute if the memory budget forces it. Sum per
layer, scale by layer counts, add the fixed costs (embedding, LM head, loss, optimiser). The result
is the achievable ms/step and MFU. Record the useful FLOPs per step (`6 × trainable params × tokens`
plus attention terms) so MFU runs can be computed the same way later. This number is the exit
condition of the optimisation loop.

## 4. Memory budget

Weights at their stored dtype, gradients, optimiser state (fp32 AdamW is 8 bytes per trainable
parameter), saved activations at the target sequence length and batch, plus workspace, against the
chip's DRAM. Decide from this the recompute policy (none, per block, per sub-module), whether the
optimiser state can be fp32, and whether the full model fits on one chip at the target sequence
length at all. Recompute multiplies every forward cost in the loop, so it must be fixed here.

## Gate

`doc/plan.md` contains the four artefacts with numbers, the exact reference commit and HF revision,
the recipe, and the chip. The roofline target is a single ms/step and MFU figure with its derivation.
`stage-review` returns `clean-pass`.

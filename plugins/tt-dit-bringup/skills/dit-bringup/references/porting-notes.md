# Porting notes

Lessons from earlier tt_dit ports that apply to most new ones. `tt-dit-add-model` has the full
method; these are the points that most often decide how long a port takes.

## Start from the closest in-tree model

- Pick the in-tree model whose architecture is closest (see `../../../shared/reference-models.md`)
  and copy its file layout, pipeline, and test structure.
- Reuse its blocks, attention, patch embedding, linear layers, and VAE where they fit. Write new
  modules only for what is genuinely different.
- When the new model needs different behaviour from an existing module, **subclass it** in the
  model's directory and override `forward()`, delegating to `super().forward()` for the
  standard case. Do not change the shared module for one model.
- Write the model summary for stage 1 early: what matches the closest model, what differs, and
  how each difference is handled. It is the fastest way for a reviewer to check the port.

## Make the reference runnable on CPU

- The source model may assume CUDA (flash attention, fused CUDA kernels, `.cuda()` calls).
  Patch these with CPU equivalents before importing the model's modules, keep the patches in
  one file in the test directory, and list them in the stage 1 summary.
- Check that the patched reference still matches the original on a small input where the
  original can run, or say in the summary that this was not possible.

## Weights

- No weights yet: use the same seeded random weights on both sides so tests still compare like
  with like. Re-run stages 2–4 when real weights arrive; random weights can hide key-mapping
  errors and do not exercise realistic value ranges.
- State-dict mapping is usually most of the port. Common conversions:
  - fuse separate Q, K, V projections into one QKV weight (and K, V into KV for cross-attention);
  - rename norms and feed-forward layers to the in-tree names (the numbering often differs);
  - reshape modulation parameters to the layout the in-tree block expects.
- Load strictly so missing and unexpected keys fail loudly.

## Host or device

Small, shape-only computations (positional or Fourier embeddings, RoPE tables, timestep
embeddings) can run on the host once and be uploaded. Anything per step and large (attention,
feed-forward, matmuls, norms, VAE convolutions) runs on device.

## Fused ops have shape contracts

Fused tt_dit and ttnn ops often assume a particular broadcast. Common case: fused
norm-with-modulation and fused gated-residual paths assume one modulation or gate vector per
batch, broadcast over tokens. A model with per-token modulation needs the unfused path (plain
norm, then `x * (1 + scale) + shift`, then `ttnn.addcmul` for the gate) until the fused op
supports it. Check the current op's validation code for the constraint rather than relying on
this note, and record it in `../../../shared/known-issues.md` if it is new.

## Sharding

`from_torch` with `mesh_axes` maps tensor dimensions to mesh axes; use `...` to replicate
leading dimensions. Weights gathered from an interleaved tensor-parallel layout are often
already in the right order; check before re-interleaving.

## Leave notes for the next port

At the end of each stage, add anything model-neutral that would have saved time to
`../../../shared/known-issues.md` or this file. Keep closed-source model details out.

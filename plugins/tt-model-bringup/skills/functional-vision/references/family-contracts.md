# Vision family contracts

Select the contract from architecture and outputs, not model branding.

## Classification

- Raw output: class logits.
- Metrics: top-1 and top-5.
- Risks: branch concatenation, patch positions, relative bias.

## Detection

- Raw outputs: class scores and box parameters.
- Metric: dataset mAP.
- Keep initial anchors, decode and NMS host-side.
- Hungarian matching and focal loss remain training-only.
- Deformable attention requires an explicit operator gate.

## Segmentation

- Raw outputs: dense logits or panoptic component maps.
- Metrics: mIoU, Dice or panoptic quality.
- Keep initial resizing and panoptic grouping host-side.
- Validate odd spatial sizes and skip alignment.

## BEV perception

- Raw outputs: depth, BEV features, boxes or vectors.
- Metrics: NDS, mAP or task-specific map quality.
- Keep calibration metadata preparation host-side initially.
- Validate projection geometry and voxel pooling separately.

## Temporal planning

- Raw outputs: maps, tracks, trajectories and task heads.
- Metrics: per-head accuracy, displacement and collision.
- Validate empty state, next-frame state and scene reset.
- Preserve ordered dataset sequences.

## Diffusion planning

- Raw outputs: candidate trajectories and scores.
- Metrics: planning score, diversity and collision.
- Fix seeds, schedules and iteration counts.
- Compare every denoising iteration.

## Multimodal generation

- Vision workflow owns encoder outputs and projector embeddings.
- Text workflow owns decoder, KV-cache, sampling and generation.
- vLLM is optional only after full multimodal correctness.

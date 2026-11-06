---
name: model-classification
description: Classify a model as text, vision, multimodal, or unsupported before TTNN bring-up, then write a pinned model contract that selects the correct workflow without inspecting forbidden target implementations.
---

# Model classification

Classify the upstream model before selecting bring-up stages. Do not infer the category from a
directory name or from an existing TTNN implementation.

## Inputs

- Upstream repository and immutable revision.
- Weight source and immutable revision.
- Intended task and acceptance metric.
- Target checkout and fresh output directory.
- Allowed reference roots and forbidden read roots.
- Requested profile, topology and serving mode.

## Procedure

1. Read only the authorized upstream source and documentation.
2. Identify modalities, state, raw outputs and task metrics.
3. Classify the model:
   - `text`: autoregressive or text-only generation.
   - `vision`: image, detection, segmentation, BEV or planning.
   - `multimodal`: coupled vision and language generation.
   - `other`: no supported workflow fits.
4. Select the task family from the contract reference.
5. Record preprocessing and postprocessing boundaries.
6. Record custom operators and dynamic-shape risks.
7. Write `MODEL_DIR/doc/model_contract.json`.
8. Validate it with `scripts/check_model_contract.py`.

Read [the model contract](../model-bringup/references/model-contract.md) before writing the file.

## Routing

- `text`: use the existing eleven-stage text workflow.
- `vision`: use `$functional-vision` and CV prompts.
- `multimodal`: split vision and text ownership explicitly.
- `other`: stop and report the missing workflow.

For multimodal models, `$functional-vision` owns the vision encoder and projector. Existing text
skills own the decoder, generation, KV-cache and optional vLLM work. Do not treat a VLM as pure CV.

## Access boundary

Forbidden read roots remain forbidden throughout the run. A write root inside a forbidden parent
does not authorize reading sibling files. Read only files created by the current run inside its
fresh write root. Record every upstream file used in `doc/reference/source_manifest.json`.

## Completion

- The contract validator exits zero.
- Every source revision is immutable.
- Weight artifacts are read and matched to SHA-256.
- Preprocessing, postprocessing and risks are explicit.
- Every output and metric has a numeric acceptance rule.
- Raw outputs and metrics are explicit.
- State reset and sequence semantics are explicit.
- The selected workflow matches the category.
- No device work has started.

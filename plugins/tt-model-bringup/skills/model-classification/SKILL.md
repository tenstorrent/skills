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
- Explicit configuration-scope choice from the user.
- Target checkout and fresh output directory.
- Allowed reference roots and forbidden read roots.
- Requested profile, topology and serving mode.
- Licenses for every source, weight, checkpoint, and dataset.

## Procedure

1. Read only the authorized upstream source and documentation.
2. Identify modalities, state, raw outputs and task metrics.
3. Classify the model:
   - `text`: autoregressive or text-only generation.
   - `vision`: image, detection, segmentation, BEV or planning.
   - `multimodal`: coupled vision and language generation.
   - `other`: no supported workflow fits.
4. Ask whether the port is pinned or generic.
5. For `pinned`, record exactly one named configuration.
6. For `generic`, record every initially supported configuration.
7. Never infer configuration scope from available checkpoints.
8. Select the task family from the contract reference.
9. Record preprocessing and postprocessing boundaries.
10. Record custom operators and dynamic-shape risks.
11. Record every external asset and its license policy.
12. Mark unknown commercial or redistribution terms explicitly.
13. Write `MODEL_DIR/doc/model_contract.json` using schema two.
14. Validate it with `scripts/check_model_contract.py`.

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
- Configuration scope records an explicit user choice.
- Generic scope names at least two supported configurations.
- Every external asset has licensing and redistribution status.
- Raw outputs and metrics are explicit.
- State reset and sequence semantics are explicit.
- The selected workflow matches the category.
- No device work has started.

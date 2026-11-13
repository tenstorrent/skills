---
name: functional-vision
description: Bring up functionally correct single-chip TTNN inference for computer-vision models and vision subgraphs, with upstream-only references, intermediate parity, task metrics, and no mandatory optimization or serving work.
---

# Functional vision bring-up

Implement correct TTNN inference for a validated vision contract. This skill does not optimize,
parallelize, serve or release the model.

## Prerequisites

1. Follow [model-bringup startup](../model-bringup/SKILL.md#startup).
2. Validate `MODEL_DIR/doc/model_contract.json`.
3. Read [family contracts](references/family-contracts.md).
4. Confirm the fresh output and forbidden read roots.
5. Use `$tt-device-usage` before any device action.

## Functional workflow

1. Reproduce upstream preprocessing and reference inference.
2. Instantiate every contract-supported PyTorch configuration.
3. Capture each instantiated model's printed Torch structure.
4. Capture deterministic module-level PyTorch goldens.
5. Inventory shapes, layouts and unsupported operators.
6. Define host preprocessing and postprocessing boundaries.
7. Implement TTNN modules with intermediate parity tests.
8. Assemble the complete functional inference graph.
9. Compare every declared raw output.
10. Validate state reset and sequence behavior when required.
11. Run the contract's task metric.
12. Run `$stage-review` and repair every finding.

## Required boundaries

- Default to batch one and single-chip.
- Derive layers, channels, shapes, and queries from configuration.
- Never hardcode values from one selected checkpoint.
- Generic ports must execute every declared configuration.
- Preserve advertised input shapes and output semantics.
- Keep training losses outside inference bring-up.
- Keep initial decode, NMS and grouping host-side.
- Keep initial dataset metrics host-side.
- Never hide unsupported operations behind Torch fallbacks.
- Never call `ttnn.from_torch` inside measured inference.
- Never call `ttnn.to_torch` before declared outputs.
- Do not start fusion, datatype sweeps or optimization.
- Do not start multichip or serving work.

For temporal models, validate an empty-state frame, a stateful next frame and a scene reset. For
iterative models, fix seeds and iteration counts, then compare each iteration. For multimodal
models, stop after the vision encoder and projector handoff contract; text generation belongs to
the text workflow.

## Evidence

Before final review, read [the generated README guide](references/generated-readme.md).

Write at the model root:

- `MODEL_DIR/README.md`: package map, runtime flow, tests, evidence and artifact boundaries.

Write under `MODEL_DIR/doc/functional_vision/`:

- `README.md`: commands, results, limitations and artifact paths.
- `work_log.md`: concise stage decisions and failures.
- `parity.json`: declared intermediate and raw-output comparisons.
- `task_eval.json`: dataset, metric protocol and results.
- `fallback_audit.txt`: measured-path host fallback audit.

The model contract, golden manifest and `doc/reference/source_manifest.json` remain under
`MODEL_DIR/doc/`. Store generated PyTorch model structures under `MODEL_DIR/doc/reference/`.
Do not copy
weights, datasets, private logs or large tensors into Git.

## Completion

- Every required module has reference parity.
- Every declared configuration passes full-model parity.
- Full raw outputs meet contract thresholds.
- Stateful gates pass when applicable.
- Task metrics meet the contract for every configuration.
- Runtime fallback audit is clean.
- Repeated inference is deterministic when expected.
- Root README matches the final generated package.
- Root README discloses every external asset's license status.
- Root README links every supported Torch model structure.
- Every README link resolves to an existing path.
- `$stage-review` returns `clean-pass`.
- Local stage-owned changes are committed but never pushed.

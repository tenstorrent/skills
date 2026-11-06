# Generated autoport README

## Purpose

- Create `MODEL_DIR/README.md` before functional workflow completion.
- Treat it as package navigation, not stage history.
- Update it when later stages change package structure.

## Required content

- State model, task, scope, and supported execution profile.
- Separate completed, optional, and excluded capabilities.
- Map top-level sources, directories, and public entrypoints.
- Explain preprocessing, device inference, state, and postprocessing.
- Document public inputs, outputs, shapes, and state semantics.
- Map tests to behaviors they validate.
- Link contracts, manifests, metrics, and review evidence.
- Explain generated artifact locations and regeneration commands.
- Identify common edit locations for developers.

## Accuracy rules

- Derive every claim from the final generated tree.
- Link only paths that exist.
- Use relative links within `MODEL_DIR`.
- Distinguish host operations from TTNN operations.
- Distinguish current results from historical diagnostics.
- Mention optional stages only after their completion.
- Never expose weights, datasets, credentials, or private logs.
- Never duplicate complete work logs or large metrics.

## Validation

- Verify every linked local path exists.
- Run `git diff --check` on the README.
- Include the README in final stage review.
- Record README completion in the work log.

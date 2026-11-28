# Generated autoport README

## Purpose

- Create `MODEL_DIR/README.md` before functional workflow completion.
- Treat it as package navigation, not stage history.
- Update it when later stages change package structure.

## Required content

- State model, task, scope, and supported execution profile.
- State pinned or generic configuration scope.
- List every supported configuration by its exact name.
- Separate completed, optional, and excluded capabilities.
- Map top-level sources, directories, and public entrypoints.
- Explain preprocessing, device inference, state, and postprocessing.
- Document public inputs, outputs, shapes, and state semantics.
- Map tests to behaviors they validate.
- Link contracts, manifests, metrics, and review evidence.
- Add a `Licensed Assets` section from the contract.
- List sources, weights, checkpoints, and evaluation datasets.
- Show license, commercial-use, and redistribution status.
- Highlight restricted, noncommercial, or unknown terms.
- State whether each asset is included by this package.
- Link authoritative license terms when available.
- Add a `PyTorch Model Structure` section.
- Name the upstream revision and instantiated configuration.
- Show the exact structure-generation command.
- Link complete generated structure files under `doc/reference/`.
- Include only a concise structure excerpt inline.
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
- Describe external assets without copying their contents.
- Report license terms; never invent legal conclusions.
- Mark unresolved license terms as `unknown`.
- Never duplicate complete work logs or large metrics.

## Torch structure rules

- Generate structures using `print(model)` or equivalent `repr`.
- Instantiate models through the pinned upstream reference.
- Use each exact contract configuration.
- Load weights only when model construction requires them.
- Write `torch_model_structure_<config>.txt` for each configuration.
- Sanitize configuration names for safe artifact filenames.
- Generic ports require one structure file per configuration.
- Do not require optional packages such as `torchinfo`.
- Do not handwrite or normalize generated model structures.

## Validation

- Verify every linked local path exists.
- Verify structure files match contract configurations.
- Verify README asset rows match contract assets.
- Run `git diff --check` on the README.
- Include the README in final stage review.
- Record README completion in the work log.

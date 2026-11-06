# Model contract

Write new contracts with `schema_version: 2`. Validate before implementation:

```bash
python "$TT_MODEL_BRINGUP_ROOT/scripts/check_model_contract.py" \
  MODEL_DIR/doc/model_contract.json --print-plan
```

## Required objects

- `model`: `name`, `category`, `task`, source and weight identities.
- `model.config_scope`: explicit pinned or generic configuration choice.
- `execution`: `profile`, `topology`, `serving`, `state`, multichip reason.
- `paths`: reference, weights, target, write and forbidden roots.
- `validation`: boundaries, risks, outputs, metrics and acceptance thresholds.
- `assets`: external sources, weights, checkpoints, and datasets.

## Values

- Categories: `text`, `vision`, `multimodal`, `other`.
- Profiles: `functional`, `optimized`, `release`.
- Topologies: `single-chip`, `auto`, `multichip`.
- Serving: `none`, `task-api`, `vllm`.
- State: `stateless`, `temporal`, `iterative`, `kv-cache`.

Pure vision cannot use `vllm`. Functional work requires `serving=none`. Text and multimodal models
own KV-cache state. Source revision must be a full Git commit.

Ask the user to select configuration scope before implementation. `pinned` names exactly one
configuration. `generic` names at least two initially supported configurations. Set
`selection_source` to `explicit-user-choice`; never derive scope from the selected checkpoint.

Each asset records `kind`, `name`, `source`, `license`, optional `license_url`, `commercial_use`,
`redistribution`, `included_in_package`, and `notes`. Commercial and redistribution policy values
are `allowed`, `restricted`, or `unknown`. Report uncertainty explicitly. Do not interpret license
terms beyond their authoritative source.

## Example

```json
{
  "schema_version": 2,
  "model": {
    "name": "example-vision-model",
    "category": "vision",
    "task": "classification",
    "source_repository": "https://github.com/example/model",
    "source_revision": "<40-character-commit>",
    "weights": "example/model",
    "weights_revision": "<immutable-weight-version>",
    "weights_digest": "sha256:<64-character-digest>",
    "weights_file": "model.pth",
    "config_scope": {
      "mode": "generic",
      "configs": ["small", "base"],
      "selection_source": "explicit-user-choice"
    }
  },
  "execution": {
    "profile": "functional",
    "topology": "single-chip",
    "serving": "none",
    "state": "stateless"
  },
  "paths": {
    "reference_root": "<upstream-checkout>",
    "weights_root": "<downloaded-weights-directory>",
    "target_dir": "models/autoports/example/run-1",
    "write_root": "models/autoports/example/run-1",
    "forbidden_read_roots": ["models/experimental/example"]
  },
  "validation": {
    "reference_framework": "pytorch",
    "raw_outputs": ["logits"],
    "metrics": ["top-1", "top-5"],
    "dataset": "ImageNet-1K",
    "preprocessing": ["resize", "crop", "normalize"],
    "postprocessing": ["argmax logits"],
    "risks": ["residual downsample shape"],
    "acceptance": {
      "logits": {"metric": "pcc", "operator": ">=", "value": 0.99, "unit": "ratio"},
      "top-1": {"metric": "absolute_delta", "operator": "<=", "value": 0.01, "unit": "fraction"},
      "top-5": {"metric": "absolute_delta", "operator": "<=", "value": 0.01, "unit": "fraction"}
    }
  },
  "assets": [
    {
      "kind": "source",
      "name": "example-model source",
      "source": "https://github.com/example/model",
      "license": "Apache-2.0",
      "license_url": "https://github.com/example/model/blob/main/LICENSE",
      "commercial_use": "allowed",
      "redistribution": "allowed",
      "included_in_package": false,
      "notes": []
    },
    {
      "kind": "weights",
      "name": "example pretrained weights",
      "source": "example/model",
      "license": "unknown",
      "license_url": null,
      "commercial_use": "unknown",
      "redistribution": "unknown",
      "included_in_package": false,
      "notes": ["Resolve terms before redistribution"]
    },
    {
      "kind": "dataset",
      "name": "ImageNet-1K",
      "source": "https://www.image-net.org/",
      "license": "ImageNet terms of access",
      "license_url": "https://www.image-net.org/download.php",
      "commercial_use": "restricted",
      "redistribution": "restricted",
      "included_in_package": false,
      "notes": ["Dataset is not redistributed"]
    }
  ]
}
```

For sharded weights, omit `weights_file` and use `manifest:<repository-relative-json>`. The manifest
contains an `artifacts` list; each row has only `path` and a 64-character `sha256`, relative to
`weights_root`. The checker reads every artifact and verifies its digest. It rejects unknown keys,
stale reference checkouts, source-origin mismatches and unpinned source commits.

Acceptance metrics use `pcc`, `cosine_similarity`, error, delta, `exact`, or `task_score` rules.
Higher-is-better metrics require `>=`; errors and deltas require `<=`; every rule names its unit.
Multichip topology requires `execution.multichip_reason`.

Schema one remains accepted only for resuming existing runs. New runs must use schema two and its
configuration-scope and asset disclosures.

The write root may sit inside a forbidden parent. That permits only current-run files under the
fresh write root. It never permits sibling reads.

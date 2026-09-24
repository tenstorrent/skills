# Golden test manifest and execution

Stage 0 writes `models/autoports/<model>/tests/golden/manifest.json`. The packaged
`scripts/check_golden_tests.py` executes its selected pytest node IDs itself after
each stage goal completes, before existing stage gates. Exit 2 blocks progression
and enters the runner's normal remediation loop. `--no-checks` explicitly bypasses
gates and is not acceptance evidence.

## Manifest v1

The compact example below shows one layer type, workload and case. Populate
`stages` for **every stage 0–11**, each mapping **all case IDs** to explicit,
distinct, collectable pytest node IDs. Paths are relative to the target checkout.
Do not put generated tensors in the plugin or commit them to the model repository.

```json
{
  "schema_version": 1,
  "num_layers": 2,
  "layer_types": {"full_attention": [0, 1]},
  "workloads": {"short": {"batch": 1, "isl": 128, "osl": 32}},
  "cases": {
    "full_short": {
      "layer_type": "full_attention", "layer_index": 0,
      "workload": "short", "decode_steps": 31, "state_tensors": ["k", "v"]
    }
  },
  "pcc_threshold": 0.995,
  "artifacts": {"bringup/references/org_model/golden/full_short.pt": "<sha256>"},
  "stages": {
    "0": {"tests": {"full_short": "models/autoports/org_model/tests/golden/test_parity.py::test_cpu[full_short]"}},
    "1": {"tests": {"full_short": "models/autoports/org_model/tests/golden/test_parity.py::test_functional[full_short]"}}
  },
  "negative_controls": {
    "wrong_output": "models/autoports/org_model/tests/golden/test_controls.py::test_wrong_output",
    "wrong_state": "models/autoports/org_model/tests/golden/test_controls.py::test_wrong_state",
    "wrong_position": "models/autoports/org_model/tests/golden/test_controls.py::test_wrong_position",
    "missing_golden": "models/autoports/org_model/tests/golden/test_controls.py::test_missing_golden",
    "bad_digest": "models/autoports/org_model/tests/golden/test_controls.py::test_bad_digest"
  }
}
```

Also record requirements path/digest and full workload list/selection rationale;
reference model, code, weights and tokenizer revisions, corpus/sample, token IDs,
chat-template/generation settings, tensor shapes/dtypes, cache valid-position/layout
metadata and any explicit model acceptance contract. Hash **every** saved input,
output and state artifact. The gate verifies hashes before and after testing.
An exception below 0.995 needs `acceptance_contract` pointing to the supplied contract;
stage review must verify it, not treat that field's existence as permission.

Layer indices must partition `0..num_layers-1`. Every layer type is tested at every
selected workload. Shared K/V belongs to its actual owner; consumers must exercise
that sharing. Recurrent types name their real state tensors instead of `k`/`v`.
Record exactly how OSL maps to decode calls: usually prefill predicts output token 1
and `OSL-1` decode calls produce the remainder. An OSL=1 workload still needs a
separate one-step decode probe, labelled as such rather than claiming it was required
generation. Never shrink a long requested trajectory to this diagnostic probe.

## Tests and metrics

Each mapped test runs both phases and uses pytest's `record_property` fixture:

```python
record_property("golden_metrics", json.dumps({
    "prefill_output": prefill_pcc,
    "decode_output": per_step_decode_pcc,
    "prefill_k": prefill_k_pcc, "prefill_v": prefill_v_pcc,
    "decode_k": final_k_pcc, "decode_v": final_v_pcc,
}))
```

For recurrent states replace k/v with the manifest's state tensor names. Decode
output is a list with exactly `decode_steps` entries; all other metrics are scalar.
Assert shapes, finite tensors and the fixed PCC threshold inside tests too. For
constant tensors use the declared equality/tolerance check, reporting 1 only when
it passes, 0 otherwise. Do not use `nan_to_num` or a blanket perfect-score fallback.

Stage 0 uses a thin CPU adapter around the original PyTorch layers to replay
captured layer inputs against goldens from the original full-model run. Do not
reimplement the reference mathematics. This validates fixture extraction and the
semantic interface, not a decoder skeleton or an independent model oracle.
Keep layer analysis/source pointers and the adapter's logical input/output,
mask/position and state semantics in the existing manifest and README; no separate
architecture specification is needed. Stage 1 owns decoder structure. Layer tests
replay captured layer inputs; full-model tests replay the recorded token trajectory.
Stage 1–5 tests call the actual
corresponding decoder; 6–8 exercise full-model outputs and selected layer state;
9–11 exercise the actual serving adapter's numerical path. Keep test interfaces
fixed, but implement future backend adapters in their owning stages. Unimplemented
adapters fail explicitly; Stage 0 runs only the CPU cases and negative controls.

Controls pass only when deliberate corruption is rejected. `wrong_state` checks
K and V independently (or every recurrent state tensor); `wrong_position` exercises
an incorrect/no-op decode update, not just a damaged serialized tensor. Use
temporary copies for cache-file controls, leaving canonical artifacts unchanged.

The gate runs one isolated pytest session with no parent conftest or automatic
third-party plugins. Define needed fixtures in `tests/golden/conftest.py`, using
lazy device imports; do not stub `ttnn` globally. Tests that need independent fabric
or device lifetimes must manage that isolation explicitly. It checks exact collected
and executed IDs, rejects skip/xfail/setup/teardown failures and checks every metric.
Runner-owned JSON/logs include the command, exit code, source and artifact hashes;
each attempt has a fresh evidence directory. A prewritten success report is not used.

For manual verification run from the target checkout:

```bash
python "$TT_MODEL_BRINGUP_ROOT/scripts/check_golden_tests.py" \
  --model-dir models/autoports/org_model --stage 0
```

The gate verifies execution and numeric evidence, not that arbitrary test code is
honest. Independent stage review still checks reference independence, real inputs,
adapter behavior, selection coverage and any test/manifest corrections.

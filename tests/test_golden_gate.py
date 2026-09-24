"""Host-only regressions for execution/coverage enforcement; no model/device claims."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup/scripts/check_golden_tests.py"
spec = importlib.util.spec_from_file_location("golden_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.fixture
def suite(tmp_path):
    model = tmp_path / "models/autoports/example"
    tests = model / "tests/golden"
    tests.mkdir(parents=True)
    tensor = tmp_path / "bringup/references/example/golden/cache.pt"
    tensor.parent.mkdir(parents=True)
    tensor.write_bytes(b"host-test artifact, not model weights")
    relative = tests.relative_to(tmp_path).as_posix()
    metrics = {"prefill_output": .999, "decode_output": [.998, .997],
               "prefill_k": .999, "prefill_v": .999, "decode_k": .999, "decode_v": .999}
    test_file = tests / "test_parity.py"
    test_file.write_text('''import json
import pytest

def test_parity(record_property):
    record_property("golden_metrics", json.dumps(METRICS))

@pytest.mark.parametrize("control", CONTROLS)
def test_control(control):
    assert control
'''.replace("METRICS", repr(metrics)).replace("CONTROLS", repr(sorted(gate.CONTROLS))))
    node = f"{relative}/test_parity.py::test_parity"
    data = {"schema_version": 1, "num_layers": 2, "layer_types": {"full": [0, 1]},
            "workloads": {"small": {"batch": 1, "isl": 32, "osl": 3}},
            "cases": {"full_small": {"layer_type": "full", "layer_index": 0,
                                      "workload": "small", "decode_steps": 2, "state_tensors": ["k", "v"]}},
            "pcc_threshold": .995, "artifacts": {str(tensor.relative_to(tmp_path)): gate.digest(tensor)},
            "stages": {str(i): {"tests": {"full_small": node}} for i in range(12)},
            "negative_controls": {name: f"{relative}/test_parity.py::test_control[{name}]" for name in gate.CONTROLS}}
    manifest = tests / "manifest.json"
    manifest.write_text(json.dumps(data))
    return tmp_path, model, test_file, tensor, manifest, data, metrics


def test_gate_executes_cpu_cases_and_controls_with_fresh_evidence(suite, monkeypatch):
    repo, model, _, _, _, _, _ = suite
    # Parent conftest must not contaminate CPU-only validation.
    (repo / "conftest.py").write_text('raise RuntimeError("parent TTNN fixture imported")')
    monkeypatch.chdir(repo)
    evidence = repo / "evidence"
    gate.run(model, 0, evidence)
    report = json.loads((evidence / "pytest.json").read_text())
    assert report["collected"] == 6
    assert all(t["calls"] == 1 for t in report["tests"].values())
    assert (evidence / "provenance.json").is_file()
    with pytest.raises(FileExistsError):
        gate.run(model, 0, evidence)


@pytest.mark.parametrize("replacement", [
    'pytest.skip("missing hardware")',
    'pytest.xfail("bad output")',
    'assert False, "mismatch"',
    'raise RuntimeError("adapter missing")',
])
def test_gate_rejects_nonpassing_test_calls(suite, monkeypatch, replacement):
    repo, model, source, _, _, _, _ = suite
    source.write_text(source.read_text().replace('    record_property(', f'    {replacement}\n    record_property('))
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError):
        gate.run(model, 1, repo / "evidence")


def test_gate_rejects_uncollected_case(suite, monkeypatch):
    repo, model, source, _, _, _, _ = suite
    source.write_text(source.read_text().replace("def test_parity(", "def not_a_test("))
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="tests failed"):
        gate.run(model, 1, repo / "evidence")


@pytest.mark.parametrize("change", ["missing_metric", "low_k", "nan", "short_decode", "missing_case", "setup_error"])
def test_numeric_and_execution_evidence_is_checked(suite, change):
    _, _, _, _, _, data, metrics = suite
    node = data["stages"]["1"]["tests"]["full_small"]
    report = {"exit_code": 0, "collected": 1,
              "tests": {node: {"passed": True, "calls": 1, "metrics": copy.deepcopy(metrics)}}}
    values = report["tests"][node]["metrics"]
    if change == "missing_metric":
        del values["prefill_output"]
    elif change == "low_k":
        values["decode_k"] = .8
    elif change == "nan":
        values["decode_v"] = float("nan")
    elif change == "short_decode":
        values["decode_output"] = [.999]
    elif change == "missing_case":
        report["tests"] = {}
    else:
        report["tests"][node]["passed"] = False
    with pytest.raises(ValueError):
        gate.validate_results(data, 1, report)


@pytest.mark.parametrize("change", ["layer_gap", "workload_gap", "missing_stage", "lower_threshold", "short_trajectory", "no_controls"])
def test_manifest_cannot_silently_drop_coverage(suite, change):
    data = copy.deepcopy(suite[5])
    if change == "layer_gap":
        data["num_layers"] = 3
    elif change == "workload_gap":
        data["workloads"]["long"] = {"batch": 1, "isl": 128, "osl": 33}
    elif change == "missing_stage":
        data["stages"].pop("7")
    elif change == "lower_threshold":
        data["pcc_threshold"] = .9
    elif change == "short_trajectory":
        data["cases"]["full_small"]["decode_steps"] = 1
    else:
        data["negative_controls"].pop("wrong_position")
    with pytest.raises(ValueError):
        gate.validate_manifest(data, 0)


def test_cached_artifacts_are_checked_before_execution(suite, monkeypatch):
    repo, model, _, tensor, _, _, _ = suite
    tensor.write_bytes(b"changed")
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="digest mismatch"):
        gate.run(model, 1, repo / "evidence")
    assert not (repo / "evidence").exists()


def test_gate_detects_test_rewriting_manifest(suite, monkeypatch):
    repo, model, source, _, manifest, _, _ = suite
    source.write_text(source.read_text().replace('    record_property(',
        f'    from pathlib import Path\n    Path({str(manifest)!r}).write_text("{{}}")\n    record_property('))
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="changed during verification"):
        gate.run(model, 1, repo / "evidence")


def test_cli_missing_manifest_is_critical(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--model-dir", "missing", "--stage", "0"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 2
    assert "Golden gate failed" in result.stderr

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "tt-model-bringup"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = load("check_eval_readiness", PLUGIN / "scripts" / "check_eval_readiness.py")
writer = load("eval_readiness_result", PLUGIN / "runtime" / "readiness_check" / "eval_readiness_result.py")


def write(model_dir, probe, rows):
    return writer.write_result(model_dir, probe, rows, summary_lines=["s"], command=["x"])


def test_every_stage_probe_missing_is_critical(tmp_path):
    for stage in gate.STAGE_PROBES:
        assert gate.main(["--model-dir", str(tmp_path), "--stage", stage]) == gate.EXIT_CRITICAL


def test_pass_advisory_and_critical(tmp_path):
    write(tmp_path, "long_answer_load", [writer.check("a", True)])
    write(tmp_path, "long_context_timing", [writer.check("cliff", False, severity="advisory", detail="2.4x")])
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "optimized-vllm"]) == gate.EXIT_ADVISORY
    write(tmp_path, "long_answer_load", [writer.check("host_sampling", False, detail="host steps 40%")])
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "optimized-vllm"]) == gate.EXIT_CRITICAL


def test_skip_needs_reason_and_counts_as_pass(tmp_path):
    write(tmp_path, "precision_reasoning", [writer.check("hf_slice", None, detail="HF does not fit in host RAM")])
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_PASS
    path = writer.result_path(tmp_path, "precision_reasoning")
    body = json.loads(path.read_text())
    body["checks"][0]["detail"] = ""
    path.write_text(json.dumps(body))
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_CRITICAL


@pytest.mark.parametrize(
    "item, expected",
    [
        ({"original_model": "same", "fix_hours": 0}, 1),
        ({"original_model": "better", "fix_hours": 2.5}, 1),
        ({"original_model": "better", "fix_hours": 0.5}, 2),
        ({"original_model": "same", "fix_hours": 0, "evidence": ["missing.json"]}, 2),
        ({"original_model": "same", "fix_hours": 0, "reason": ""}, 2),
    ],
)
def test_limitation_waivers(tmp_path, item, expected):
    write(tmp_path, "precision_reasoning", [writer.check("loss_vs_reference", False, detail="worse than noise")])
    (tmp_path / "evidence.json").write_text("{}")
    entry = {"probe": "precision_reasoning", "check": "loss_vs_reference", "reason": "BFP4 required to fit",
             "evidence": ["evidence.json"]}
    entry.update(item)
    (tmp_path / "doc/eval_readiness/limitations.json").write_text(json.dumps({"items": [entry]}))
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == expected


def test_unwaivable_check_stays_critical(tmp_path):
    write(tmp_path, "precision_reasoning", [writer.check("x", False, waivable=False, detail="d")])
    (tmp_path / "e.json").write_text("{}")
    (tmp_path / "doc/eval_readiness/limitations.json").write_text(json.dumps({"items": [
        {"probe": "precision_reasoning", "check": "x", "reason": "r", "evidence": ["e.json"],
         "original_model": "same"}]}))
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_CRITICAL


def test_wrong_probe_name_and_schema_are_critical(tmp_path):
    path = write(tmp_path, "precision_reasoning", [writer.check("x", True)])
    body = json.loads(path.read_text())
    body["probe"] = "other"
    path.write_text(json.dumps(body))
    assert gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"]) == gate.EXIT_CRITICAL


def test_output_is_short(tmp_path, capsys):
    write(tmp_path, "precision_reasoning", [writer.check(f"c{i}", False, detail="d") for i in range(100)])
    gate.main(["--model-dir", str(tmp_path), "--stage", "datatype-sweep"])
    assert len(capsys.readouterr().out.splitlines()) <= 40

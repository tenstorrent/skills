#!/usr/bin/env python3
"""Execute the golden baseline and reject missing, stale or failing evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid


CONTROLS = {"wrong_output", "wrong_state", "wrong_position", "missing_golden", "bad_digest"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def child(root, relative):
    path = (root / relative).resolve()
    require(not Path(relative).is_absolute() and path.is_relative_to(root), f"Path escapes {root}: {relative}")
    return path


def validate_manifest(data, stage):
    require(data.get("schema_version") == 1, "Unsupported golden manifest schema")
    kinds, workloads, cases = data["layer_types"], data["workloads"], data["cases"]
    require(kinds and 1 <= len(workloads) <= 10 and cases, "Empty inventory or invalid workload count")
    require(type(data["num_layers"]) is int and data["num_layers"] > 0, "Invalid layer count")
    for indices in kinds.values():
        require(indices and all(type(i) is int and i >= 0 for i in indices), "Invalid layer indices")
    indices = [i for group in kinds.values() for i in group]
    require(sorted(indices) == list(range(data["num_layers"])), "Layer inventory does not cover the stack exactly")
    for workload in workloads.values():
        require(type(workload["batch"]) is int and workload["batch"] == 1 and all(type(workload[k]) is int and workload[k] > 0 for k in ("isl", "osl")), "Invalid batch-one workload")
    coverage = set()
    for case in cases.values():
        kind, workload = case["layer_type"], case["workload"]
        require(kind in kinds and workload in workloads, "Unknown case layer type/workload")
        require(case["layer_index"] in kinds[kind], "Case index has the wrong layer type")
        require(type(case["decode_steps"]) is int and case["decode_steps"] > 0, "No decode steps")
        require(case["decode_steps"] >= max(1, workloads[workload]["osl"] - 1), "Decode trajectory shorter than requested OSL")
        states = case["state_tensors"]
        require(states and len(states) == len(set(states)) and all(isinstance(s, str) and s for s in states), "Missing/duplicate state tensors")
        coverage.add((kind, workload))
    require(coverage == {(k, w) for k in kinds for w in workloads}, "Missing layer-type/workload coverage")
    require(set(data["stages"]) == {str(i) for i in range(12)}, "Stage mapping must cover 0–11")
    for plan in data["stages"].values():
        require(set(plan["tests"]) == set(cases), "Stage test mapping omits cases")
    threshold = data["pcc_threshold"]
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 < threshold <= 1, "Invalid PCC threshold")
    require(threshold >= .995 or data.get("acceptance_contract"), "Lower threshold needs an explicit model acceptance contract")
    require(data["artifacts"], "No cached artifacts")
    tests = data["stages"][str(stage)]["tests"]
    controls = data.get("negative_controls", {}) if stage == 0 else {}
    if stage == 0:
        require(set(controls) == CONTROLS, "Stage 0 sensitivity controls are incomplete")
    nodes = list(tests.values()) + list(controls.values())
    require(len(nodes) == len(set(nodes)), "Each required case/control needs a distinct pytest node ID")
    return tests, nodes


def validate_results(data, stage, report):
    tests, nodes = validate_manifest(data, stage)
    require(report["exit_code"] == 0, "Pytest did not exit successfully")
    require(report["collected"] == len(nodes) and set(report["tests"]) == set(nodes), "Collected/executed cases differ from the manifest")
    for node in nodes:
        result = report["tests"][node]
        require(result["passed"] and result["calls"] == 1, f"Missing, skipped, xfailed or failing test: {node}")
    for case_id, node in tests.items():
        case, metrics = data["cases"][case_id], report["tests"][node]["metrics"]
        expected = {"prefill_output", "decode_output"} | {f"{phase}_{s}" for phase in ("prefill", "decode") for s in case["state_tensors"]}
        require(isinstance(metrics, dict) and set(metrics) == expected, f"Missing output/state metrics: {node}")
        for name, value in metrics.items():
            values = value if name == "decode_output" else [value]
            require(isinstance(values, list) and len(values) == (case["decode_steps"] if name == "decode_output" else 1), f"Incomplete decode trajectory: {node}")
            require(all(type(v) in (int, float) and math.isfinite(v) and data["pcc_threshold"] <= v <= 1 for v in values), f"PCC below threshold or invalid: {node}: {name}")


def run(model, stage, evidence):
    repo = Path.cwd().resolve()
    model = model.resolve()
    require(model.is_relative_to(repo), "Model must be inside the target checkout")
    golden = model / "tests/golden"
    manifest = golden / "manifest.json"
    data = json.loads(manifest.read_text())
    _, nodes = validate_manifest(data, stage)
    for node in nodes:
        require(isinstance(node, str) and "::" in node, "Use explicit pytest test node IDs")
        path = child(repo, node.split("::", 1)[0])
        require(path.is_relative_to(golden) and path.is_file(), f"Test outside golden suite: {node}")
    artifacts = {child(repo, path): sha for path, sha in data["artifacts"].items()}
    for path, sha in artifacts.items():
        require(digest(path) == sha, f"Golden digest mismatch: {path}")
    sources = set(model.rglob("*.py")) | {manifest}
    before = {str(path): digest(path) for path in sources}
    evidence.mkdir(parents=True, exist_ok=False)
    report_path = evidence / "pytest.json"
    env = os.environ.copy()
    env["TT_GOLDEN_REPORT"] = str(report_path)
    env["TT_GOLDEN_STAGE"] = str(stage)
    env["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    command = [sys.executable, "-m", "pytest", "-p", "golden_pytest", "--tb=short",
               "-c", os.devnull, "--rootdir", str(repo), "--confcutdir", str(golden), *nodes]
    # The evidence directory is new for each attempt; old reports cannot satisfy this gate.
    result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (evidence / "pytest.log").write_text(result.stdout)
    (evidence / "provenance.json").write_text(json.dumps({"stage": stage, "command": command,
        "source_sha256": before, "artifacts": data["artifacts"], "exit_code": result.returncode}, indent=2) + "\n")
    require(result.returncode == 0 and report_path.is_file(), f"Golden tests failed; see {evidence}")
    require(sources == set(model.rglob("*.py")) | {manifest} and all(digest(Path(p)) == sha for p, sha in before.items()), "Tests/source/manifest changed during verification")
    require(all(digest(path) == sha for path, sha in artifacts.items()), "Goldens changed during verification")
    validate_results(data, stage, json.loads(report_path.read_text()))
    print(f"Stage {stage} golden tests passed; evidence: {evidence}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--stage", type=int, choices=range(12), required=True)
    args = parser.parse_args()
    root = Path(os.environ.get("MULTIGOAL_LOG_DIR", "bringup/artifacts/golden-checks")).resolve()
    try:
        run(args.model_dir, args.stage, root / f"golden-{args.stage}-{uuid.uuid4().hex}")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Golden gate failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

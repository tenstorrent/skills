"""Packaging and script behaviour for the tt-model-op-analysis plugin."""

from __future__ import annotations

import csv
import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "tt-model-op-analysis"
SCRIPTS = PLUGIN / "scripts"
SKILL_NAMES = ("static-op-analysis", "measured-op-analysis")


def test_plugin_has_both_skills():
    for name in SKILL_NAMES:
        assert (PLUGIN / "skills" / name / "SKILL.md").is_file()


def test_manifest_versions_match():
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    assert claude["version"] == codex["version"] == "0.1.0"


def run_script(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args],
                          capture_output=True, text=True)


def write_rows(path: pathlib.Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def op_row(i: int, launches_p150: int, quasar_port: str = "⚠️", quasar_ev: str = "test_x.py") -> dict:
    return {
        "id": str(i), "stage": "encoder", "ttnn_api": f"ttnn.op{i}", "op_code": f"Op{i}DeviceOperation",
        "device_op": f"prim::op{i}", "program_factory": f"Op{i}Factory", "call_site": "model.py",
        "evidence": "op.cpp", "confidence": "verified", "grid_dependency": "",
        "p150:shapes": "(1,32)", "p150:launches": str(launches_p150), "p150:status": "✅",
        "quasar:as_written": "❌", "quasar:port": quasar_port, "quasar:evidence": quasar_ev,
    }


def make_static_run(tmp_path: pathlib.Path, op_rows: list[dict], trace_rows: list[dict]) -> pathlib.Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"kind": "static", "targets": ["p150", "quasar"]}))
    write_rows(run / "op_table.csv", op_rows)
    write_rows(run / "call_trace.csv", trace_rows)
    return run


def trace_row(i: int, repeats: int, per_repeat: int) -> dict:
    return {"id": str(i), "profile": "p150", "stage": "encoder", "ops": "op1, op2",
            "repeats": str(repeats), "launches_per_repeat": str(per_repeat), "notes": ""}


def test_valid_static_run_passes(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 12), op_row(2, 1)], [trace_row(1, 12, 1), trace_row(2, 1, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "OK"


def test_off_by_one_id_fails(tmp_path):
    rows = [op_row(1, 12), op_row(2, 1)]
    rows[1]["id"] = "3"
    run = make_static_run(tmp_path, rows, [trace_row(1, 12, 1), trace_row(2, 1, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 1
    assert "op_table.csv row 2 has id 3" in result.stdout


def test_launch_total_mismatch_fails(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 12), op_row(2, 1)], [trace_row(1, 12, 1), trace_row(2, 2, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 1
    assert "p150: op_table launches 13 != call_trace launches 14" in result.stdout


def test_missing_column_fails(tmp_path):
    rows = [op_row(1, 1)]
    del rows[0]["evidence"]
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 1
    assert "op_table.csv missing column evidence" in result.stdout


def test_quasar_check_without_evidence_fails(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1, quasar_port="✅", quasar_ev="")], [trace_row(1, 1, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 1
    assert "op_table.csv row 1: ✅ in quasar:port without quasar:evidence" in result.stdout


def test_unknown_status_fails(tmp_path):
    rows = [op_row(1, 1)]
    rows[0]["p150:status"] = "ok"
    run = make_static_run(tmp_path, rows, [trace_row(1, 1, 1)])
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 1
    assert "op_table.csv row 1: p150:status 'ok' not in" in result.stdout


TENSOR_FIELDS = ["W_PAD[LOGICAL]", "Z_PAD[LOGICAL]", "Y_PAD[LOGICAL]", "X_PAD[LOGICAL]",
                 "LAYOUT", "DATATYPE", "MEMORY"]
OPS_HEADER = (["OP CODE", "OP TYPE", "ATTRIBUTES", "CORE COUNT", "HOST START TS", "HOST END TS",
               "HOST DURATION [ns]", "DEVICE KERNEL DURATION [ns]"]
              + [f"INPUT_{i}_{f}" for i in range(2) for f in TENSOR_FIELDS]
              + [f"OUTPUT_0_{f}" for f in TENSOR_FIELDS])
TENSOR_A = ["1[1]", "10[10]", "32[4]", "64[64]", "TILE", "BFLOAT16", "DEV_1_L1_HEIGHT_SHARDED"]
NO_TENSOR = [""] * len(TENSOR_FIELDS)


def ops_csv(path: pathlib.Path, rows: list[list]) -> pathlib.Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(OPS_HEADER)
        writer.writerows(rows)
    return path


def dev(code, start, end, cores, kernel_ns, host_ns=100):
    return [code, "tt_dnn_device", "{'program_config': 'MatmulMultiCoreReuseMultiCastProgramConfig'}",
            cores, start, end, host_ns, kernel_ns] + TENSOR_A + NO_TENSOR + TENSOR_A


def host(code, start, end, host_ns):
    return [code, "python_fallback", "", "", start, end, host_ns, ""] + NO_TENSOR * 3


def sign(name, ts):
    return [name, "signpost", "", "", ts, "", "", ""] + NO_TENSOR * 3


def read(path):
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def test_warm_window_uses_second_iteration_only(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [
        dev("MatmulDeviceOperation", 0, 10, 120, 5_000_000),
        sign("start", 20),
        dev("MatmulDeviceOperation", 1_000_000, 2_000_000, 120, 1_000_000),
        host("TorchPermute", 2_000_000, 3_000_000, 1_000_000),
        dev("SoftmaxDeviceOperation", 3_000_000, 5_000_000, 64, 500_000),
        sign("end", 6_000_000),
    ])
    out = tmp_path / "out"
    result = run_script("tracy_report.py", str(src), str(out), "--start", "start", "--end", "end")
    assert result.returncode == 0, result.stdout + result.stderr
    measured = read(out / "measured_ops.csv")
    assert [r["op_code"] for r in measured] == ["MatmulDeviceOperation", "TorchPermute", "SoftmaxDeviceOperation"]
    fb = read(out / "host_fallback.csv")[0]
    assert fb["window"] == "warm (signposts start..end)"
    assert (fb["device_ops"], fb["host_ops"]) == ("2", "1")
    assert fb["device_op_time_ms"] == "1.500"
    assert fb["host_op_time_ms"] == "1.000"
    assert fb["total_time_ms"] == "4.000"
    assert fb["host_pct_of_total"] == "25.00"
    assert "threshold" not in (out / "host_fallback.csv").read_text()


def test_inputs_outputs_from_expanded_columns(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    row = read(out / "measured_ops.csv")[0]
    tensor = "1[1]x10[10]x32[4]x64[64] TILE BFLOAT16 DEV_1_L1_HEIGHT_SHARDED"
    assert row["inputs"] == tensor
    assert row["outputs"] == tensor


def test_no_signposts_is_cold_whole_csv(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1_000_000, 8, 1_000_000)])
    out = tmp_path / "out"
    result = run_script("tracy_report.py", str(src), str(out))
    assert result.returncode == 0, result.stderr
    assert read(out / "host_fallback.csv")[0]["window"] == "cold (no signposts)"
    assert len(read(out / "measured_ops.csv")) == 1


def test_missing_named_signpost_fails(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    result = run_script("tracy_report.py", str(src), str(tmp_path / "out"), "--start", "start", "--end", "end")
    assert result.returncode == 2
    assert "ERROR: signpost 'start' not found" in result.stdout


def test_footprint_max_cores_and_unmeasured_dram(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [
        dev("MatmulDeviceOperation", 0, 1, 64, 1), dev("MatmulDeviceOperation", 1, 2, 120, 1),
    ])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    fp = read(out / "footprint.csv")
    assert fp == [{"op_code": "MatmulDeviceOperation", "launches": "2", "max_core_count": "120",
                   "peak_dram_mb": "not measured"}]


def test_footprint_uses_given_peak_dram(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 64, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out), "--peak-dram-mb", "198").returncode == 0
    assert read(out / "footprint.csv")[0]["peak_dram_mb"] == "198.0"


MEASURED_FIELDS = ["id", "op_code", "op_type", "attributes", "inputs", "outputs",
                   "core_count", "device_kernel_ns", "host_ns"]


def static_table(path, rows):
    base = {"stage": "s", "ttnn_api": "a", "device_op": "d", "call_site": "", "evidence": "e",
            "confidence": "verified", "grid_dependency": "", "p150:shapes": "", "p150:status": "✅"}
    write_rows(path, [{"id": str(i), **base, **r} for i, r in enumerate(rows, 1)])
    return path


def measured_table(path, rows):
    write_rows(path, [{"id": str(i), "op_code": c, "op_type": "tt_dnn_device", "attributes": a,
                       "inputs": "", "outputs": "", "core_count": "1", "device_kernel_ns": "1", "host_ns": "1"}
                      for i, (c, a) in enumerate(rows, 1)], fieldnames=MEASURED_FIELDS)
    return path


def run_diff(tmp_path, static_rows, measured_rows):
    s = static_table(tmp_path / "op_table.csv", static_rows)
    m = measured_table(tmp_path / "measured_ops.csv", measured_rows)
    out = tmp_path / "diff.csv"
    result = run_script("diff_reports.py", "--static", str(s), "--measured", str(m),
                        "--profile", "p150", "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    return read(out)


def test_diff_categories(tmp_path):
    rows = run_diff(tmp_path,
        [{"op_code": "MatmulDeviceOperation", "program_factory": "MatmulMultiCoreReuseMcast2DProgramFactory (cfg)", "p150:launches": "2"},
         {"op_code": "SoftmaxDeviceOperation", "program_factory": "SoftmaxShardedProgramFactory", "p150:launches": "1"},
         {"op_code": "ReshardDeviceOperation", "program_factory": "ReshardGenericFactory", "p150:launches": "1"}],
        [("MatmulDeviceOperation", "MatmulMultiCoreReuseMcast2DProgramFactory"),
         ("SoftmaxDeviceOperation", "other"), ("SoftmaxDeviceOperation", "other"),
         ("TilizeDeviceOperation", "")])
    got = {(r["category"], r["op_code"]) for r in rows}
    assert got == {
        ("count differs", "MatmulDeviceOperation"),
        ("count differs", "SoftmaxDeviceOperation"),
        ("missing in measured", "ReshardDeviceOperation"),
        ("extra in measured", "TilizeDeviceOperation"),
    }


def test_static_row_without_op_code_reported(tmp_path):
    rows = run_diff(tmp_path, [{"op_code": "", "program_factory": "X", "p150:launches": "1"}], [])
    assert [(r["category"], r["static_ids"]) for r in rows] == [("unmatched static row", "1")]


def test_identical_tables_have_no_diff(tmp_path):
    rows = run_diff(tmp_path,
        [{"op_code": "MatmulDeviceOperation", "program_factory": "Mcast2D", "p150:launches": "1"}],
        [("MatmulDeviceOperation", "uses Mcast2D")])
    assert rows == []


def cmp_rows(**overrides):
    row = {"id": "1", "stage": "enc", "ttnn_api": "ttnn.linear", "device_op": "prim::matmul",
           "program_factory": "Mcast2D", "p150:launches": "12", "p150:status": "✅"}
    row.update(overrides)
    return row


def run_compare(tmp_path, old, new):
    write_rows(tmp_path / "old.csv", old)
    write_rows(tmp_path / "new.csv", new)
    out = tmp_path / "changes.csv"
    result = run_script("compare_runs.py", "--old", str(tmp_path / "old.csv"),
                        "--new", str(tmp_path / "new.csv"), "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    return read(out)


def test_reordered_rows_no_change(tmp_path):
    a = cmp_rows(id="1")
    b = cmp_rows(id="2", ttnn_api="ttnn.add", device_op="prim::binary_ng", program_factory="BinaryNg")
    assert run_compare(tmp_path, [a, b], [dict(b, id="1"), dict(a, id="2")]) == []


def test_duplicate_keys_reordered_no_change(tmp_path):
    a = cmp_rows(id="1", **{"p150:shapes": "64"})
    b = cmp_rows(id="2", **{"p150:shapes": "128"})
    assert run_compare(tmp_path, [a, b], [dict(b, id="1"), dict(a, id="2")]) == []


def test_missing_key_column_fails(tmp_path):
    write_rows(tmp_path / "old.csv", [{"id": "1", "profile": "p150", "stage": "s"}])
    write_rows(tmp_path / "new.csv", [{"id": "1", "profile": "p150", "stage": "s"}])
    result = run_script("compare_runs.py", "--old", str(tmp_path / "old.csv"), "--new", str(tmp_path / "new.csv"),
                        "--out", str(tmp_path / "changes.csv"))
    assert result.returncode == 2
    assert "ERROR: key column ttnn_api missing" in result.stdout


def test_added_removed_changed(tmp_path):
    old = [cmp_rows(), cmp_rows(ttnn_api="ttnn.add", device_op="prim::binary_ng", program_factory="BinaryNg")]
    new = [cmp_rows(**{"p150:launches": "13"}),
           cmp_rows(ttnn_api="ttnn.softmax", device_op="prim::softmax", program_factory="Sharded")]
    rows = run_compare(tmp_path, old, new)
    got = {(r["change"], r["column"], r["old"], r["new"]) for r in rows}
    assert ("changed", "p150:launches", "12", "13") in got
    assert any(r["change"] == "added" and "ttnn.softmax" in r["key"] for r in rows)
    assert any(r["change"] == "removed" and "ttnn.add" in r["key"] for r in rows)


REFERENCES = PLUGIN / "references"
LINE_CITATION = re.compile(r"\.(?:cpp|hpp|h|py|rst|sh)\s*:\s*\d+")
PLUGIN_ROOT_REF = re.compile(r"<plugin-root>/((?:references|scripts)/[\w./-]+)")


def _unfenced_lines(path):
    fenced = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and not line.lstrip().startswith(">"):
            yield n, line


def test_references_cite_no_line_numbers():
    paths = sorted(REFERENCES.glob("*.md"))
    assert paths
    for path in paths:
        for n, line in _unfenced_lines(path):
            assert not LINE_CITATION.search(line), f"{path.name}:{n} cites a source line number"


def test_plugin_root_links_resolve():
    files = list(PLUGIN.glob("skills/*/SKILL.md")) + list(REFERENCES.glob("*.md"))
    seen = 0
    for path in files:
        for rel in PLUGIN_ROOT_REF.findall(path.read_text(encoding="utf-8")):
            seen += 1
            assert (PLUGIN / rel).is_file(), f"{path}: {rel} does not exist"
    assert seen


def test_schemas_doc_lists_every_column():
    sys.path.insert(0, str(SCRIPTS))
    import compare_runs
    import diff_reports
    import opa_schema
    import tracy_report
    doc = (REFERENCES / "schemas.md").read_text(encoding="utf-8")
    names = (set(opa_schema.OP_TABLE_BASE) | set(opa_schema.CALL_TRACE_COLUMNS)
             | set(tracy_report.MEASURED_COLUMNS) | set(tracy_report.HOST_FALLBACK_COLUMNS)
             | set(tracy_report.FOOTPRINT_COLUMNS) | set(diff_reports.DIFF_COLUMNS)
             | set(compare_runs.CHANGE_COLUMNS))
    missing = sorted(n for n in names if f"`{n}`" not in doc)
    assert not missing, f"schemas.md does not document {missing}"


def test_skills_are_short_and_name_their_gates():
    static = (PLUGIN / "skills" / "static-op-analysis" / "SKILL.md").read_text(encoding="utf-8")
    measured = (PLUGIN / "skills" / "measured-op-analysis" / "SKILL.md").read_text(encoding="utf-8")
    for text in (static, measured):
        assert len(text.splitlines()) <= 120
        assert "Body written in Task 7" not in text
        assert "<plugin-root>/references/run-setup.md" in text
    assert "<plugin-root>/scripts/validate_static.py" in static
    assert "<plugin-root>/scripts/tracy_report.py" in measured
    assert "<plugin-root>/scripts/diff_reports.py" in measured
    for text in (static, measured):
        assert "`<plugin-root>` is the `tt-model-op-analysis` plugin directory" in text
    assert "Only for a warm window" in measured


def test_root_readme_installs_plugin_on_both_hosts():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "codex plugin add tt-model-op-analysis@tenstorrent-skills" in readme
    assert "/plugin install tt-model-op-analysis@tenstorrent-skills" in readme


def test_validator_accepts_bom_csv(tmp_path):
    run = make_static_run(tmp_path, [op_row(1, 1)], [trace_row(1, 1, 1)])
    for name in ("op_table.csv", "call_trace.csv"):
        path = run / name
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    result = run_script("validate_static.py", str(run))
    assert result.returncode == 0, result.stdout


def test_written_csvs_carry_bom_for_spreadsheet_apps(tmp_path):
    src = ops_csv(tmp_path / "ops.csv", [dev("MatmulDeviceOperation", 0, 1, 8, 1)])
    out = tmp_path / "out"
    assert run_script("tracy_report.py", str(src), str(out)).returncode == 0
    for name in ("measured_ops.csv", "host_fallback.csv", "footprint.csv"):
        assert (out / name).read_bytes().startswith(b"\xef\xbb\xbf"), name

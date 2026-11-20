# The review-fix round of the code benches (benchfix.py, bench v2 stage 4) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there). Add new fix-round tests here.
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _bf_score(criteria, correctness, notes=""):
    return {"scores": {"criteria": criteria, "correctness": correctness}, "notes": notes}


def _bf_arms():
    return {"opus": {"provider": "claude", "model": "opus", "effort": "medium"},
            "glm": {"provider": "chat", "model": "glm", "effort": "high"},
            "orig": {"provider": "original", "model": "original", "effort": ""}}


def _bf_graded(*items):
    # blind_review's result: {"scored": {item: (label, arm, score)}}
    return {"scored": {f"i{n}": it for n, it in enumerate(items)}}


def _bf_first(tmp_path=None):
    from ttp import benchdaily, benchfix
    tasks = {"T-1": {"project": "p"}, "T-2": {"project": "p"}}
    rows = {("T-1", 0, "opus"): {"outcome": "done"}, ("T-1", 0, "glm"): {"outcome": "flagged"},
            ("T-2", 0, "opus"): {"outcome": "done"}, ("T-2", 0, "glm"): {"outcome": "timeout"}}
    graded = _bf_graded(("T-1", "opus", _bf_score(4, 4)), ("T-1", "glm", _bf_score(2, 3, "bad edge")),
                        ("T-2", "opus", _bf_score(3, 2, "off by one")), ("T-2", "glm", _bf_score(1, 1)),
                        ("T-1", "orig", _bf_score(4, 4)))
    return benchfix.first_cells(graded, rows, tasks, _bf_arms(), 1, "orig", benchdaily.landed), tasks


def test_fix_findings_are_the_reviewers_notes_and_name_the_scores_below_the_bar():
    from ttp import benchfix
    assert benchfix.findings(_bf_score(2, 4, "handle empty input")) == \
        "Scores below the bar: criteria 2/4.\n\nhandle empty input"
    assert benchfix.findings(_bf_score(4, 4, "")) == ""
    assert benchfix.findings(_bf_score(1, 2)) == "Scores below the bar: criteria 1/4, correctness 2/4."


def test_fix_round_picks_only_usable_first_tries_that_did_not_land_and_never_the_original():
    from ttp import benchfix
    cells, _ = _bf_first()
    assert [(c["task"], c["arm"], c["landed"]) for c in cells] == \
        [("T-1", "glm", False), ("T-1", "opus", True), ("T-2", "glm", False), ("T-2", "opus", False)]
    need = benchfix.needing_fix(cells)
    assert [(c["task"], c["arm"]) for c in need] == [("T-2", "opus")], \
        "a landed cell, a cheat-flagged one and a timed-out one are not fix candidates"
    assert need[0]["findings"].startswith("Scores below the bar: correctness 2/4.") and "off by one" in need[0]["findings"]
    assert benchfix.enabled(None) and benchfix.enabled({"bench": {"fix": {"enabled": True}}})
    assert not benchfix.enabled({"bench": {"fix": {"enabled": False}}}) \
        and not benchfix.enabled({"bench": {"fix": {"enabled": "off"}}})


def test_fix_run_fixes_runs_each_cell_once_with_the_diff_and_findings_and_skips_past_the_runaway_guard(tmp_path):
    from ttp import benchdaily, benchfix
    cells, tasks = _bf_first()
    for t, a in (("T-2", "opus"), ("T-1", "glm")):
        d = tmp_path / "cells" / f"{t}-r0-{a}"
        d.mkdir(parents=True)
        (d / "diff.patch").write_text(f"diff of {t} {a}\n")
    for t in tasks.values():
        t.update(task="x", source_root=str(tmp_path), state=str(tmp_path))
    tasks["T-1"]["task"], tasks["T-2"]["task"] = "T-1", "T-2"
    seen = []

    def run_cell(bench_dir, out, task, cell, arm, opts, cfg):
        seen.append((task["task"], cell["arm"], cell["fix"]))
        return {"task": cell["task"], "rep": cell["rep"], "arm": cell["arm"], "outcome": "done", "cost_usd": 3.0,
                "wall_s": 120}
    need = [c for c in cells if c["task"] == "T-2" and c["arm"] == "opus"] + \
           [{**c, "findings": "x"} for c in cells if c["task"] == "T-1" and c["arm"] == "glm"]
    got = benchfix.run_fixes(tmp_path, tmp_path, tasks, _bf_arms(), need, {}, {}, run_cell, benchdaily.read_rows,
                             benchdaily.append_row, {"claude": 1, "chat": 1}, None, 0.0, benchdaily.is_claude)
    assert sorted(seen) == [("T-1", "glm", {"diff": "diff of T-1 glm\n", "findings": "x"}),
                            ("T-2", "opus", {"diff": "diff of T-2 opus\n", "findings": need[0]["findings"]})]
    assert set(got["rows"]) == {("T-1", 0, "glm"), ("T-2", 0, "opus")} and got["skipped"] == []
    assert got["rows"][("T-2", 0, "opus")]["first"] == {"criteria": 3, "correctness": 2}
    seen.clear()
    benchfix.run_fixes(tmp_path, tmp_path, tasks, _bf_arms(), need, {}, {}, run_cell, benchdaily.read_rows,
                       benchdaily.append_row, None, None, 0.0, benchdaily.is_claude)
    assert seen == [], "a fix with a row is not run again"
    # a guard already reached leaves a Claude fix out, never a GLM one
    (tmp_path / benchfix.FILE).unlink()
    got = benchfix.run_fixes(tmp_path, tmp_path, tasks, _bf_arms(), need, {}, {}, run_cell, benchdaily.read_rows,
                             benchdaily.append_row, {"claude": 1, "chat": 1}, 5.0, 9.0, benchdaily.is_claude)
    assert [s["arm"] for s in got["skipped"]] == ["opus"] and "runaway guard" in got["skipped"][0]["why"]
    assert set(got["rows"]) == {("T-1", 0, "glm")}


def test_fix_report_counts_landed_first_try_and_after_one_fix_and_states_the_paired_verdicts():
    from ttp import benchdaily, benchfix
    cells, _ = _bf_first()
    need = benchfix.needing_fix(cells)
    fixrows = {("T-2", 0, "opus"): {"outcome": "done", "cost_usd": 1.5, "wall_s": 90}}
    regraded = {"scored": {"i0": ("T-2", "opus", _bf_score(4, 3))}}
    rows = benchfix.cell_rows(cells, fixrows, regraded, benchdaily.landed)
    by = {(r["task"], r["arm"]): r for r in rows}
    assert by[("T-2", "opus")]["landed_first"] is False and by[("T-2", "opus")]["landed_fixed"] is True
    assert by[("T-1", "opus")]["landed_first"] and by[("T-1", "opus")]["landed_fixed"] and not by[("T-1", "opus")]["fix_ran"]
    assert by[("T-1", "glm")]["landed_fixed"] is False and by[("T-1", "glm")]["fix_ran"] is False
    assert by[("T-2", "opus")]["fix_usd"] == 1.5 and by[("T-2", "opus")]["fix_minutes"] == 1.5 and len(need) == 1
    # a fix whose regrade still fails stays failed
    rows2 = benchfix.cell_rows(cells, fixrows, {"scored": {"i0": ("T-2", "opus", _bf_score(4, 2))}}, benchdaily.landed)
    assert {(r["task"], r["arm"]): r for r in rows2}[("T-2", "opus")]["landed_fixed"] is False
    summ = benchfix.summary(rows)
    assert summ["models"]["claude:opus:medium"]["first"]["passed"] == 1
    assert summ["models"]["claude:opus:medium"]["fixed"]["passed"] == 2 and summ["models"]["claude:opus:medium"]["rescued"] == 1
    assert summ["models"]["chat:glm:high"]["fixed"]["passed"] == 0 and summ["models"]["chat:glm:high"]["fixes"] == 0
    text = benchfix.table_md(summ)
    assert "landed first try" in text and "landed after one fix" in text and "claude:opus:medium" in text
    assert "first try:" in text and "after one fix:" in text, "each pair gets a verdict on both measures"
    assert any(w in text for w in ("cannot separate", "beats")), text


def test_fix_regrade_batch_holds_only_the_arms_whose_fix_ran_with_the_fixed_diff(tmp_path):
    from ttp import benchfix
    for a, diff in (("opus", "first try + fix\n"), ("glm", "")):
        d = tmp_path / "cells" / f"T-2-r0-{a}-fix"
        d.mkdir(parents=True)
        (d / "diff.patch").write_text(diff)
    fixrows = {("T-2", 0, "opus"): {"outcome": "done", "session_id": "s1"},
               ("T-2", 0, "glm"): {"outcome": "error"}}

    def blind_cells(out, tasks, arms, reps):
        return {"cells": [{"task": "T-2", "arms": {"opus": {"diff": "first"}, "glm": {"diff": "first"}}}]}
    got = benchfix.regrade_build(fixrows, 1, blind_cells)(tmp_path, {"T-2": {}}, _bf_arms())
    assert [list(c["arms"]) for c in got["cells"]] == [["opus"]]
    side = got["cells"][0]["arms"]["opus"]
    assert side["diff"] == "first try + fix\n" and side["run_id"] == "T-2-r0-opus-fix" and side["session_id"] == "s1"
    assert benchfix.regrade_build({}, 1, blind_cells)(tmp_path, {"T-2": {}}, _bf_arms()) == {"cells": []}


def test_fix_cell_gets_the_first_try_committed_so_its_final_diff_holds_both(tmp_path):
    from ttp import bench, benchrun
    ws = tmp_path / "ws"
    ws.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@x", *a],
                                  capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    (ws / "a.txt").write_text("one\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    first = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n one\n+two\n"
    assert benchrun.apply_first_try(ws, first) == ""
    assert (ws / "a.txt").read_text() == "one\ntwo\n" and g("rev-parse", "HEAD~1").stdout.strip() == base
    assert g("status", "--porcelain").stdout == "", "committed, so the agent's own edits are the only change after it"
    assert benchrun.apply_first_try(ws, "") == "", "an empty first try starts from the base"
    assert "does not apply" in benchrun.apply_first_try(ws, first.replace("one", "zzz"))
    assert benchrun.NO_FINDINGS and "reviewer" in benchrun.FIX_HEADER
    assert bench is not None


def test_fix_round_flag_is_parsed_and_the_day_runs_it_unless_the_config_turns_it_off():
    from ttp import benchfix, cli
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is not None:
        ns = p.parse_args(["bench", "daily", "--day", "2026-11-17", "--fix-round"])
        assert getattr(ns, "fix_round", False) is True
    assert benchfix.enabled({}) and not benchfix.enabled({"bench": {"fix": {"enabled": False}}})

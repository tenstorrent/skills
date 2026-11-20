# The not-run check of the benches that grade cells (benchnotrun.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new not-run tests here.
from __future__ import annotations

import threading
from pathlib import Path


def _bn_ended(calls=5, launched=True, fault="", failed=0, server_calls=0):
    return {"launched": launched, "rc": 0, "bridge": {"usage": {"calls": calls}, "last_fault": fault,
                                                      "server": {"calls": server_calls, "failed_calls": failed}}}


def _bn_row(outcome="done", **kw):
    return {"task": "T-1", "rep": 0, "arm": "glm", "outcome": outcome, "input_tokens": 100, "output_tokens": 10,
            "provider": "chat", "model": "glm", "effort": "high", **kw}


def test_not_run_zero_model_calls_is_not_run_and_a_missing_count_with_no_tokens_is_zero():
    from ttp import benchnotrun
    row = _bn_row()
    assert benchnotrun.apply(row, _bn_ended(calls=0), "diff")
    assert row["outcome"] == "not_run" and row["passed"] is None and "zero model calls" in row["not_run_reason"]
    row = _bn_row(input_tokens=0, output_tokens=0)
    ended = _bn_ended()
    ended["bridge"]["usage"] = {}
    assert benchnotrun.apply(row, ended, "diff"), "no count and no token usage is zero calls"
    row = _bn_row()   # no count, but tokens were used: a model ran
    assert not benchnotrun.apply(row, ended, "diff") and row["outcome"] == "done"


def test_not_run_a_cell_that_never_launched_is_not_run():
    from ttp import benchnotrun
    row = _bn_row(outcome="fail")
    ended = _bn_ended(calls=9, launched=False)
    ended["launch_error"] = "endpoint not set"
    assert benchnotrun.apply(row, ended, "diff")
    assert row["outcome"] == "not_run" and row["not_run_reason"].startswith("never launched: endpoint not set")


def test_not_run_a_cell_the_server_broke_is_not_run_but_only_when_it_left_no_diff():
    from ttp import benchnotrun
    row = _bn_row()
    assert benchnotrun.apply(row, _bn_ended(calls=3, fault="malformed", server_calls=3, failed=1), "")
    assert "server broke it" in row["not_run_reason"] and "malformed" in row["not_run_reason"]
    row = _bn_row()
    assert benchnotrun.apply(row, _bn_ended(calls=2, server_calls=2, failed=2), b""), "every server call failed"
    row = _bn_row()
    assert not benchnotrun.apply(row, _bn_ended(calls=3, fault="malformed", server_calls=3, failed=1), "real diff"), \
        "a cell that left work is the model's"
    assert row["outcome"] == "done"


def test_not_run_a_model_that_worked_and_left_no_diff_failed_and_a_normal_cell_stays_done():
    from ttp import benchnotrun
    for outcome in ("done", "fail"):
        row = _bn_row(outcome)
        assert not benchnotrun.apply(row, _bn_ended(calls=40), ""), "calls > 0 and an empty diff is a failure"
        assert row["outcome"] == outcome and not benchnotrun.is_not_run(row)
    row = _bn_row()
    assert not benchnotrun.apply(row, _bn_ended(calls=40), "diff --git a b") and row["outcome"] == "done"
    # a dry run, and a cell the harness or the server already marked, are left as they are
    for r in (_bn_row(dry_run=True), _bn_row("error"), _bn_row("server"), _bn_row("isolation_failed")):
        before = dict(r)
        assert not benchnotrun.apply(r, _bn_ended(calls=0), "") and r == before


def test_not_run_counts_the_server_cut_cells_with_the_not_run_ones_per_arm():
    from ttp import benchnotrun
    rows = [_bn_row("not_run"), _bn_row("server", arm="opus"), _bn_row("done"), _bn_row("not_run", task="T-2")]
    assert benchnotrun.tally(rows) == {"glm": 2, "opus": 1}
    assert benchnotrun.is_not_run(rows[1]) and not benchnotrun.is_not_run(rows[2]) and not benchnotrun.is_not_run(None)


def test_not_run_canary_stops_an_arm_whose_first_cell_was_not_run_and_clears_one_that_ran():
    from ttp import benchnotrun
    c = benchnotrun.Canary()
    assert c.before("glm") == "first"
    c.after("glm", _bn_row("not_run", not_run_reason="zero model calls"))
    assert c.before("glm") == "stop" and "glm" in c.stopped
    assert "T-1" in c.stopped["glm"] and "zero model calls" in c.stopped["glm"] and "no other cell" in c.stopped["glm"]
    assert c.before("opus") == "first"
    c.after("opus", _bn_row("fail"))   # a model that worked and failed clears the arm
    assert c.before("opus") == "go" and "opus" not in c.stopped
    t = benchnotrun.Canary({"orig"})
    assert t.before("orig") == "go", "an arm with a row from an earlier run is trusted"


def test_not_run_canary_holds_the_arms_other_cells_until_its_first_cell_ends():
    from ttp import benchnotrun
    c = benchnotrun.Canary()
    assert c.before("glm") == "first"
    got = []
    t = threading.Thread(target=lambda: got.append(c.before("glm")))
    t.start()
    t.join(0.3)
    assert t.is_alive() and not got, "the second cell waits for the first"
    c.after("glm", _bn_row("not_run"))
    t.join(5)
    assert got == ["stop"]


def _bn_task(tmp_path, tid="T-1"):
    return {"task": tid, "source_root": str(tmp_path), "state": str(tmp_path), "repeat": False}


def test_not_run_run_task_launches_no_more_cells_of_an_arm_whose_first_cell_was_not_run(tmp_path, monkeypatch):
    from ttp import benchdaily, benchnotrun, benchrun
    launched = []

    def run_cell(bench_dir, out, task, cell, arm, opts, cfg):
        launched.append((task["task"], cell["arm"]))
        return {"task": task["task"], "rep": 0, "arm": cell["arm"],
                "outcome": "not_run" if cell["arm"] == "glm" else "done", "not_run_reason": "zero model calls"}
    monkeypatch.setattr(benchrun, "run_cell", run_cell)
    arms = {"glm": {"provider": "chat"}, "opus": {"provider": "claude"}}
    canary = benchnotrun.Canary()
    lock = threading.Lock()
    for tid in ("T-1", "T-2", "T-3"):
        benchdaily.run_task(tmp_path, tmp_path, _bn_task(tmp_path, tid), arms, {"canary": canary}, {}, None, lock)
    assert launched == [("T-1", "glm"), ("T-1", "opus"), ("T-2", "opus"), ("T-3", "opus")], \
        "glm stops after its first cell was not run; opus runs on"
    assert list(canary.stopped) == ["glm"]
    rows = benchdaily.read_rows(tmp_path / "results.jsonl")
    assert [k for k, r in rows.items() if r["outcome"] == "not_run"] == [("T-1", 0, "glm")]
    # a resume runs the not-run cell again and leaves a done one
    launched.clear()
    benchdaily.run_task(tmp_path, tmp_path, _bn_task(tmp_path, "T-1"), arms, {"canary": benchnotrun.Canary()}, {}, None, lock)
    assert launched == [("T-1", "glm")]


def test_not_run_day_record_and_table_show_the_count_outside_every_rate(tmp_path):
    from ttp import benchdaily
    rows = {("T-1", 0, "glm"): _bn_row("not_run", not_run_reason="zero model calls"),
            ("T-2", 0, "glm"): _bn_row("not_run", task="T-2", not_run_reason="never launched: x")}
    tasks = {"T-1": {"project": "p"}, "T-2": {"project": "p"}}
    for r in rows.values():
        benchdaily.append_row(tmp_path / "results.jsonl", r, threading.Lock())
    rec = benchdaily.day_record("2026-11-17", tasks, {"glm": {"provider": "chat", "model": "glm", "effort": "high"}},
                                tmp_path, {"scored": {}}, [], [])
    assert [(x["task"], x["why"]) for x in rec["not_run"]] == [("T-1", "zero model calls"), ("T-2", "never launched: x")]
    assert rec["cells"] == [] and rec["not_run"][0]["model"] == "chat:glm:high"
    root = tmp_path / "bench"
    (root / "days").mkdir(parents=True)
    rec["cells"] = [{"project": "p", "task": "T-9", "arm": "glm", "model": "chat:glm:high", "claude": False, "landed": True,
                     "criteria": 4, "correctness": 4, "cost_usd": 1.0, "minutes": 5.0, "stopped": False}]
    (root / "days" / "2026-11-17.json").write_text(__import__("json").dumps(rec))
    cum = benchdaily.cumulative(root)
    a = cum["models"]["chat:glm:high"]
    assert a["n"] == 1 and a["landed"] == 1 and a["not_run"] == 2, "not-run cells are not in N or the landed rate"
    text = benchdaily.table_md(cum)
    assert "| not run |" in text and "| chat:glm:high | 1 | 1/1 |" in text and "| 0 | 2 |" in text.replace("| 60-min stops", "")
    assert "## Not run" in text and "zero model calls" in text
    # a model with only not-run cells still gets its row, N = 0
    (root / "days" / "2026-11-18.json").write_text(__import__("json").dumps(
        {"day": "2026-11-18", "cells": [], "not_run": [{"project": "p", "task": "T-3", "arm": "x", "model": "claude:opus:medium",
                                                       "why": "zero model calls"}]}))
    cum = benchdaily.cumulative(root)
    assert cum["models"]["claude:opus:medium"]["n"] == 0 and cum["models"]["claude:opus:medium"]["not_run"] == 1
    assert benchdaily.table_md(cum)


def test_not_run_arms_are_left_out_of_the_blind_batch_and_the_replay_and_an_empty_answer_still_fails(tmp_path):
    from ttp import benchdaily, benchreplay
    for a, outcome in (("glm", "not_run"), ("opus", "done")):
        keep = tmp_path / "cells" / f"Q-1-r0-{a}"
        keep.mkdir(parents=True)
        benchdaily.append_row(tmp_path / "results.jsonl", {"task": "Q-1", "rep": 0, "arm": a, "outcome": outcome}, threading.Lock())
    tasks = {"Q-1": {"spec": "why?"}}
    arms = {"glm": {"provider": "chat"}, "opus": {"provider": "claude"}}
    got = benchreplay.answer_cells({"Q-1": {"established": "because"}})(tmp_path, tasks, arms)
    assert [list(c["arms"]) for c in got["cells"]] == [["opus"]]
    assert got["cells"][0]["arms"]["opus"]["diff"] == "(no answer was written)", \
        "a model that worked and wrote nothing is graded, not skipped"
    rec = benchreplay.table(tmp_path, {"Q-1": {"project": "p", "source_task": 1, "class": "analysis"}}, arms,
                            {"scored": {}}, [], [])
    assert rec["not_run"]["per_arm"] == {"glm": 1}
    text = benchreplay.table_md({**rec, "arms_def": {}}, {"glm": "glm high", "opus": "opus medium"})
    assert "not run" in text and "glm high" in text

# Context packs and the original-change arm of the code benches (benchpack.py, bench v2) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new pack and original-arm tests here.
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class _BpDb:
    def __init__(self):
        self.c = sqlite3.connect(":memory:")
        self.c.row_factory = sqlite3.Row
        self.c.executescript(
            "create table runs(id integer primary key, task int, role text, dir text, session_id text);"
            "create table tasks(id integer primary key, title text, status text, result text, spec text);"
            "create table messages(id integer primary key, ts real, direction text, text text);")

    def q(self, sql, params=()):
        return self.c.execute(sql, params).fetchall()


_BP_PROMPT = (
    "FORKED FROM the coordinator session\nnoise\n\n# BINDING RESTRICTIONS\n- never push to main\n\n"
    "# You are a worker\n\n## Progress\nwrite ttp note lines\n\n## Handoff\nwrite result.json\n\n"
    "## Budget and time\n$5\n\n# CHARTER\nkeep it generic\n\n## Policies\n- be kind\n\n# YOUR TASK\n\nfix the parser\n"
    "Acceptance: the parser accepts empty input\n")


def _bp_run(tmp_path, db, task_id, prompt=_BP_PROMPT, lines=()):
    d = tmp_path / f"run{task_id}"
    d.mkdir()
    (d / "prompt.md").write_text(prompt)
    (d / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    db.c.execute("insert into runs values(?,?,?,?,?)", (task_id * 10, task_id, "worker", str(d), "s"))
    return d


def _bp_bash(i, cmd, result):
    return [{"message": {"content": [{"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": cmd}}]}},
            {"message": {"content": [{"type": "tool_result", "tool_use_id": f"u{i}", "content": result}]}}]


def test_pack_strip_prompt_keeps_what_the_worker_read_and_cuts_the_harness_operating_rules():
    from ttp import benchpack
    got = benchpack.strip_prompt(_BP_PROMPT)
    assert got.startswith("# BINDING RESTRICTIONS") and "FORKED FROM" not in got
    for gone in ("Progress", "Handoff", "Budget and time", "ttp note lines", "result.json", "$5"):
        assert gone not in got
    for kept in ("never push to main", "# CHARTER", "be kind", "# YOUR TASK", "fix the parser", "Acceptance: the parser"):
        assert kept in got


_BP_DELIVERY_PROMPT = (
    "# BINDING RESTRICTIONS\n- Push only to branch x; never `git push` to main; open no pull requests with gh pr.\n\n"
    "# You are a worker\n\n## Landing on the project's branch\n\nland with `ttp push` after `git fetch origin`\n\n"
    "## Pull requests\n\nopen a draft with `gh pr create`\n\n"
    "# YOUR TASK\n\nfix the parser\n- run the tests\n- then `git push` the branch\n- keep the diff small\n")


def test_pack_strip_drops_delivery_steps_but_keeps_the_restrictions_as_written():
    from ttp import benchpack
    got = benchpack.strip_prompt(_BP_DELIVERY_PROMPT)
    assert not benchpack.DELIVERY.search(got.split("# CHARTER")[0].split("\n\n", 1)[1]), got
    for gone in ("Landing on the project", "Pull requests", "ttp push", "gh pr create", "git fetch origin", "then `git push`"):
        assert gone not in got
    for kept in ("never `git push` to main", "open no pull requests with gh pr", "fix the parser", "run the tests",
                 "keep the diff small"):
        assert kept in got


def test_pack_attach_rebuilds_an_older_pack_at_a_run_boundary_and_keep_pins_the_one_a_cell_ran_on(tmp_path):
    from ttp import benchpack
    db = _BpDb()
    _bp_run(tmp_path, db, 41, _BP_DELIVERY_PROMPT)
    bench = tmp_path / "bench"
    task = {"task": "P-41", "source_task": 41, "title": "t", "spec": "s", "head": ""}
    here = bench / "P-41"
    here.mkdir(parents=True)
    old = "# Context pack\n\nland with `ttp push` and git fetch\n"
    (here / "pack.md").write_text(old)
    (here / "pack.json").write_text(json.dumps({"class": benchpack.CODE, "why": "", "sources": {},
                                                  "sha256": benchpack._sha(old)}))
    pinned = benchpack.attach(task, db, bench, {"P-41": benchpack._sha(old)})
    assert pinned["pack"]["sha256"] == benchpack._sha(old) and "ttp push" in benchpack.read(pinned), \
        "cells already run keep their pack: arms never mix packs inside one result set"
    fresh = benchpack.attach(task, db, bench)
    assert fresh["pack"]["version"] == benchpack.PACK_VERSION and fresh["pack"]["sha256"] != benchpack._sha(old)
    assert "ttp push" not in benchpack.read(fresh) and "fix the parser" in benchpack.read(fresh)
    assert (here / "pack.md").read_text() == old, "the older file stays for cells still running on it"
    again = benchpack.attach(task, db, bench)
    assert again["pack"]["sha256"] == fresh["pack"]["sha256"], "rebuilt once"
    assert benchpack.attach(task, db, bench, {"P-41": "0" * 64})["pack"]["sha256"] == fresh["pack"]["sha256"]


def test_pack_sha_is_recorded_per_cell_and_pack_keep_reads_it_from_the_rows(tmp_path):
    from ttp import benchdaily, benchpack, benchrun
    db = _BpDb()
    _bp_run(tmp_path, db, 61)
    t = benchpack.attach({"task": "P-61", "source_task": 61, "title": "t", "spec": "s", "head": ""}, db, tmp_path / "b")
    row = benchrun.pack_fields(t)
    assert row["pack_sha256"] == t["pack"]["sha256"] and row["pack_version"] == benchpack.PACK_VERSION
    assert benchrun.pack_fields({"task": "x"}) == {}
    assert benchdaily.pack_keep({("P-61", 0, "a"): row, ("P-62", 0, "a"): {"outcome": "pass"}}) == \
        {"P-61": t["pack"]["sha256"]}


def test_pack_refs_finds_earlier_tasks_and_user_messages_and_a_fix_titles_review():
    from ttp import benchpack
    spec = "see #8715 and user msg #3107, msgs #3085, #3088 and #8800 fix; also #9000 and issue &#123"
    assert benchpack.refs(spec, "Fix review #8722: x", 8733) == ([8722, 8715], [3107, 3085, 3088, 8800])
    assert benchpack.refs("none here", "plain", 5) == ([], [])


def test_pack_snapshot_keeps_live_reads_redacted_and_names_remote_commands(tmp_path):
    from ttp import benchpack
    out = tmp_path / "output.jsonl"
    lines = (_bp_bash(1, "tail -n 3 state/logs/daemon.log", "line one\nline two") + _bp_bash(2, "ls src", "a.py")
             + _bp_bash(3, "ssh box uptime", "up 3 days"))
    out.write_text("".join(json.dumps(x) + "\n" for x in lines))
    items, remote = benchpack.snapshot(out)
    assert [i["label"] for i in items] == ["tail -n 3 state/logs/daemon.log"] and "line two" in items[0]["text"]
    assert remote == ["ssh box uptime"]


def test_pack_classes_code_bench_live_only_and_invalid(tmp_path):
    from ttp import benchpack
    db = _BpDb()
    db.c.execute("insert into tasks values(8001,'Earlier','done',?,'')", (json.dumps({"summary": "earlier result text"}),))
    db.c.execute("insert into messages values(3001,1700000000,'in','the user said do it')")
    _bp_run(tmp_path, db, 9011, lines=_bp_bash(1, "cat state/logs/x.log", "log body"))
    task = {"source_task": 9011, "title": "Do it", "spec": "per #8001 and user msg #3001\nAcceptance: works", "head": "abcdef0123456"}
    got = benchpack.build_pack(db, task)
    assert got["class"] == benchpack.CODE and got["sha256"] == benchpack._sha(got["text"])
    for need in ("fix the parser", "earlier result text", "the user said do it", "log body", "Context pack"):
        assert need in got["text"]
    assert got["sources"]["tasks"] == [8001] and got["sources"]["messages"] == [3001] and got["sources"]["snapshot"] == 1
    # an original run that ran on another machine cannot be replayed in a cell
    _bp_run(tmp_path, db, 12, lines=_bp_bash(1, "ssh box pytest", "ok"))
    live = benchpack.build_pack(db, {**task, "source_task": 12})
    assert live["class"] == benchpack.LIVE_ONLY and "another machine" in live["why"] and live["text"] == ""
    # no recorded prompt, or a pack that names the fix commit
    assert benchpack.build_pack(db, {**task, "source_task": 13})["class"] == benchpack.INVALID
    _bp_run(tmp_path, db, 14, prompt=_BP_PROMPT + "the fix is abcdef012\n")
    leak = benchpack.build_pack(db, {**task, "source_task": 14})
    assert leak["class"] == benchpack.INVALID and "fix commit" in leak["why"]


def test_pack_fix_review_task_carries_the_reviews_findings(tmp_path):
    from ttp import benchpack
    db = _BpDb()
    db.c.execute("insert into tasks values(20,'Review #19: x','done',?,'')", (json.dumps({"summary": "FINDING: off by one"}),))
    _bp_run(tmp_path, db, 21)
    got = benchpack.build_pack(db, {"source_task": 21, "title": "Fix review #20: x", "spec": "fix it", "head": ""})
    assert "## Review findings to fix" in got["text"] and "FINDING: off by one" in got["text"]
    assert got["sources"]["review_findings"] is True


def test_pack_attach_pins_the_hash_and_a_changed_pack_is_refused(tmp_path):
    import pytest
    from ttp import benchpack
    db = _BpDb()
    _bp_run(tmp_path, db, 31)
    bench = tmp_path / "bench"
    t = benchpack.attach({"task": "P-31", "source_task": 31, "title": "t", "spec": "s", "head": ""}, db, bench)
    assert t["pack_class"] == benchpack.CODE and Path(t["pack"]["path"]).parent == bench / "P-31"
    text = benchpack.read(t)
    assert "fix the parser" in text and benchpack.frame(text, "PROMPT").endswith("---\n\nPROMPT")
    again = benchpack.attach({"task": "P-31", "source_task": 31, "title": "t", "spec": "s", "head": ""}, db, bench)
    assert again["pack"]["sha256"] == t["pack"]["sha256"], "built once, reused"
    Path(t["pack"]["path"]).write_text(text + "tampered")
    with pytest.raises(benchpack.PackError):
        benchpack.read(t)
    assert benchpack.read({"task": "x"}) == "" and benchpack.frame("", "PROMPT") == "PROMPT"


def test_pack_screen_keeps_code_bench_tasks_and_lists_the_rest_with_why(tmp_path):
    from ttp import benchpack
    db = _BpDb()
    _bp_run(tmp_path, db, 41)
    tasks = {"P-41": {"task": "P-41", "project": "P", "source_task": 41, "title": "a", "spec": "s", "head": ""},
             "P-42": {"task": "P-42", "project": "P", "source_task": 42, "title": "b", "spec": "s", "head": ""}}
    kept, left = benchpack.screen(tasks, {"P": db}, tmp_path / "bench")
    assert list(kept) == ["P-41"] and "pack" in kept["P-41"]
    assert [(x["task"], x["class"]) for x in left] == [("P-42", benchpack.INVALID)] and left[0]["why"]


def test_pack_original_arm_is_the_landed_diff_and_a_failing_original_drops_its_task(tmp_path):
    import subprocess
    from ttp import benchdaily, benchpack
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*a):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=n", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (repo / "m.py").write_text("x = 1\n")
    git("add", "."); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "m.py").write_text("x = 2\n")
    git("commit", "-qam", "change")
    head = git("rev-parse", "HEAD")
    out = tmp_path / "out"
    out.mkdir()
    tasks = {"P-1": {"task": "P-1", "repo": str(repo), "base": base, "head": head}}
    assert benchpack.add_original(out, tasks) == ["P-1"]
    assert benchpack.add_original(out, tasks) == [], "idempotent"
    diff = (out / "cells" / "P-1-r0-original" / "diff.patch").read_text()
    assert "-x = 1" in diff and "+x = 2" in diff
    rows = benchdaily.read_rows(out / "results.jsonl")
    assert rows[("P-1", 0, "original")]["cost_usd"] == 0.0
    cells = [{"task": "P-1", "project": "P", "arm": "original", "landed": True, "criteria": 4, "correctness": 4},
             {"task": "P-1", "project": "P", "arm": "a", "landed": False, "criteria": 1, "correctness": 1},
             {"task": "P-2", "project": "P", "arm": "original", "landed": False, "criteria": 2, "correctness": 1},
             {"task": "P-2", "project": "P", "arm": "a", "landed": True, "criteria": 4, "correctness": 4}]
    kept, gone, rate = benchpack.split_original(cells)
    assert [(c["task"], c["arm"]) for c in kept] == [("P-1", "a")]
    assert [g["task"] for g in gone] == ["P-2"] and "original change did not pass" in gone[0]["why"]
    assert rate == {"n": 2, "passed": 1, "per_task": {"P-1": True, "P-2": False}}


def test_pack_the_cell_prompt_carries_the_pack_for_every_arm_and_a_bad_pack_is_an_error_row(tmp_path):
    from ttp import benchpack, benchrun
    db = _BpDb()
    _bp_run(tmp_path, db, 51)
    t = benchpack.attach({"task": "P-51", "source_task": 51, "title": "t", "spec": "s", "head": ""}, db, tmp_path / "b")
    text = benchrun.benchpack.frame(benchpack.read(t), benchrun.prompt("do s", {}, 0))
    assert text.startswith("# Context pack") and "do s" in text
    Path(t["pack"]["path"]).write_text("changed")
    row = benchrun.run_cell(tmp_path / "b", tmp_path / "o", {**t, "repo": None}, {"task": "P-51", "rep": 0, "arm": "a"},
                            {"provider": "claude", "model": "m", "effort": "high"}, {"isolate": False, "dry_run": True}, {})
    assert row["outcome"] == "error" and "context pack" in row["why"][0]


def test_pack_the_table_reports_the_original_pass_rate_the_dropped_and_the_not_code_bench_tasks(tmp_path):
    from ttp import benchdaily
    root = tmp_path / "daily"
    (root / "days").mkdir(parents=True)
    cell = {"project": "A", "task": "A-1", "source_task": 1, "arm": "son", "model": "claude:s:high", "claude": True,
            "criteria": 4, "correctness": 4, "tests": 0, "scope": 0, "landed": True, "cost_usd": 0.5, "minutes": 3,
            "stopped": False, "outcome": "done"}
    rec = {"day": "2026-11-16", "cells": [cell], "original": {"n": 3, "passed": 2, "per_task": {"A-1": True, "A-2": False, "A-3": True}},
           "dropped_original": [{"task": "A-2", "project": "A", "why": "the original change did not pass the blind review"}],
           "not_code_bench": [{"project": "B", "task": "B-9", "class": "live-only", "why": "2 of 3 acceptance lines need the live system"}]}
    (root / "days" / "2026-11-16.json").write_text(json.dumps(rec))
    cum = benchdaily.write_table(root)
    assert cum["original"]["passed"] == 2 and cum["dropped_original"][0]["day"] == "2026-11-16"
    md = (root / "table.md").read_text()
    assert "## Original change" in md and "2/3" in md
    assert "## Dropped: the original change did not pass" in md and "A A-2" in md
    assert "## Not in the code bench" in md and "B B-9: live-only: 2 of 3" in md

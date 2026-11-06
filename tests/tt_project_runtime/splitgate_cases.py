# Area case file for tests/test_tt_project_runtime.py: splitgate (chains.split_lines at a code hand-off).
from __future__ import annotations


def _again(env, p, tid, result, name="big2.py", lines=10):
    """A second run of re-queued task `tid`: one more commit on its branch, then the hand-off `result`."""
    from ttp import worktree
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    path, br = worktree.ensure(p, p.db.task(tid))
    (path / name).write_text("".join(f"more {i}\n" for i in range(lines)))
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}-2"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return path, br, p.db.q("SELECT * FROM tasks WHERE kind='review' AND depends_on=?", (json.dumps([tid]),))


def test_an_oversized_code_change_goes_back_once_for_a_split_with_its_work_kept(env):
    """A code hand-off over chains.split_lines non-test lines is re-queued once before any review: the
    same task, branch and worktree, a split order appended to its spec, a fresh budget. Its next hand-off
    over the limit is reviewed (no loop), and the review spec names the size."""
    from ttp import splitgate
    p = make(env)
    def before(tid, head):
        p.db.update_task(tid, spent_usd=2.0)
    tid, branch, done, reviews = _finish_code(env, p, "big", {"big.py": 260, "tests/test_big.py": 500},
                                              before=before)
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["branch"] == branch and not reviews
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == 1
    assert "Split order" in t["spec"] and "260 non-test lines" in t["spec"] and f"landed:#{tid}" in t["spec"]
    assert f"git branch {branch}-full" in t["spec"] and splitgate.FIELD in t["spec"]
    assert splitgate.returned(t) and "big works" in t["result"]
    base = p.config()["budget"]["task_default_usd"]["standard"]
    assert abs(t["budget_usd"] - (2.0 + base)) < 0.01
    assert "Returned once to its worker for a split" in done["text"] and done["status"] == "handled"
    assert p.db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (splitgate.EVENT, tid))
    # The work stays: the re-run's worktree is on the same branch with the change.
    path, br, reviews = _again(env, p, tid, {"status": "done", "summary": "still big"})
    assert br == branch and (path / "big.py").exists()
    t = p.db.task(tid)
    assert t["status"] == "done" and len(reviews) == 1 and t["spec"].count("Split order") == 1
    assert "Size: 270 non-test lines" in reviews[0]["spec"] and "went back once" in reviews[0]["spec"]


def test_a_change_under_the_limit_or_with_the_gate_off_is_reviewed_as_before(env):
    """Tests, docs and changesets do not count; at or under the limit, or with split_lines 0, the review is
    queued at once and its spec has no size line."""
    p = make(env)
    tid, _, _, reviews = _finish_code(env, p, "small", {"app.py": 200, "tests/test_app.py": 400,
                                                         "docs/guide.md": 300, ".changeset/x.md": 50})
    assert p.db.task(tid)["status"] == "done" and len(reviews) == 1 and "Size:" not in reviews[0]["spec"]
    p.set_config("chains.split_lines", 0)
    tid, _, _, reviews = _finish_code(env, p, "off", {"app.py": 900})
    assert p.db.task(tid)["status"] == "done" and len(reviews) == 1 and "Size:" not in reviews[0]["spec"]


def test_a_recorded_reason_an_urgent_task_or_a_split_part_passes_with_a_size_note(env):
    """A hand-off with `no_split` (also carried by on_pass), a priority 1 task and a split part's own hand-off
    are reviewed at once; the first two note the size and why it was not split."""
    from ttp import checksettle, splitgate
    p = make(env)
    tid, _, _, reviews = _finish_code(env, p, "reasoned", {"big.py": 300}, result={
        "status": "done", "summary": "one table rewrite", "no_split": "one schema change, all or nothing"})
    assert p.db.task(tid)["status"] == "done" and len(reviews) == 1
    assert "Size: 300 non-test lines" in reviews[0]["spec"] and "all or nothing" in reviews[0]["spec"]
    tid, _, _, reviews = _finish_code(env, p, "hot", {"big.py": 300},
                                      before=lambda tid, head: p.db.update_task(tid, priority=1))
    assert p.db.task(tid)["status"] == "done" and len(reviews) == 1
    assert "Size: 300 non-test lines" in reviews[0]["spec"] and "urgent" in reviews[0]["spec"]
    tid, _, _, reviews = _finish_code(env, p, "code: part 2 of parser", {"big.py": 300})
    assert p.db.task(tid)["status"] == "done" and len(reviews) == 1 and not splitgate.returned(p.db.task(tid))
    assert splitgate.FIELD in checksettle.KEYS


def test_split_gate_decisions_and_bypasses(env):
    """splitgate.check: kind, part titles, continuation labels, blank or non-string reasons and junk config."""
    from ttp import splitgate
    cfg = {"chains": {"split_lines": 100}}
    big = {"a.py": 101}
    code = {"id": 1, "kind": "code", "title": "t", "spec": "s", "labels": "[]"}
    assert splitgate.check(cfg, code, big) == ("return", 101, 100)
    assert splitgate.check(cfg, code, {"a.py": 100}) is None and splitgate.check(cfg, code, None) is None
    assert splitgate.check(cfg, code, {"a.py": None, "test_a.py": 900}) is None
    assert splitgate.check(cfg, dict(code, kind="work"), big) is None
    assert splitgate.check(cfg, dict(code, title="Code: Part 3 of x"), big) is None
    assert splitgate.check(cfg, dict(code, labels='["continues:7"]'), big) is None
    for junk in ("", "   ", 5, None, ["x"]):
        assert splitgate.check(cfg, code, big, {"no_split": junk})[0] == "return"
    assert splitgate.check(cfg, code, big, {}, {"no_split": "why"})[0].endswith("why")
    assert splitgate.check(cfg, dict(code, labels='["split_order:1"]'), big)[0].startswith("it went back")
    assert splitgate.check(cfg, dict(code, labels="not json"), big)[0] == "return"
    assert splitgate.check(cfg, dict(code, spec="urgent: prod down"), big)[0].startswith("urgent")
    assert splitgate.check({"chains": {"split_lines": "junk"}}, code, big) is None

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
    p.set_config("chains.split_lines", 200)   # opt-in since 11-12 (default off)
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
    p.set_config("chains.split_lines", 200)   # opt-in since 11-12 (default off)
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


def test_a_needs_review_hand_off_is_returned_for_a_split_only_when_a_review_would_follow(env):
    """A needs_review hand-off of a big change that gets no auto review (not urgent, owes nothing) reaches the
    coordinator as it is: status review, no split order, its request kept. An owed one (an open obligation's
    owner) gets a review at once, so it goes back for a split first."""
    from ttp import obligations, splitgate
    p = make(env)
    p.set_config("chains.split_lines", 200)   # opt-in since 11-12 (default off)
    ask = {"status": "needs_review", "summary": "pick the schema", "question": "which schema?"}
    tid, _, done, reviews = _finish_code(env, p, "asks", {"big.py": 300}, result=ask)
    t = p.db.task(tid)
    assert t["status"] == "review" and not reviews and not splitgate.returned(t)
    assert splitgate.LABEL not in (t["labels"] or "") and "Split order" not in t["spec"]
    assert done is None or "Returned once" not in done["text"]
    assert not p.db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (splitgate.EVENT, tid))
    tid, _, _, reviews = _finish_code(env, p, "owed", {"big.py": 300}, result=ask,
                                      before=lambda tid, head: obligations.add(p, "a promised fix", owners=[tid]))
    t = p.db.task(tid)
    assert t["status"] == "queued" and splitgate.returned(t) and not reviews
    assert "do not list them again" in t["spec"]


def _other_base(p, name="other", lines=900):
    """Branch `name` off the repo's head carrying `lines` lines the push branch `work` lacks, standing as the
    remote's default branch (origin/HEAD); `work` is the push branch at the repo's head."""
    root = p.root
    head = _git_out(root, "rev-parse", "HEAD").strip()
    _git_out(root, "branch", "-f", "work", head)
    _git_out(root, "checkout", "-qb", name)
    (root / "vendored.py").write_text("".join(f"v {i}\n" for i in range(lines)))
    _git_out(root, "add", "vendored.py")
    _git_out(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "vendored")
    _git_out(root, "checkout", "-q", head)
    _git_out(root, "update-ref", f"refs/remotes/origin/{name}", name)
    _git_out(root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{name}")
    p.set_config("delivery.push_branch", "work")


def test_the_split_size_counts_only_the_changes_own_lines_from_its_own_base(env):
    """A branch the worker based on another branch (the remote's default) is measured from that base, not
    from the push branch: a small change there is reviewed at once, its huge gap to the push branch aside."""
    from ttp import splitgate, worktree
    p = make(env)
    _other_base(p)
    def rebase(path):
        _git_out(path, "reset", "-q", "--hard", "other")
    tid, branch, _, reviews = _finish_code(env, p, "small on other", {"small.py": 20}, edit=rebase)
    t = p.db.task(tid)
    assert worktree.diff_lines(p, [branch], base="work")["vendored.py"] == 900
    assert worktree.own_base(p, t, branch) == ("origin/other", "origin/other")
    assert t["status"] == "done" and not splitgate.returned(t) and len(reviews) == 1
    assert "Size:" not in reviews[0]["spec"]


def test_a_large_change_on_the_push_branch_still_goes_back_and_the_order_names_its_base(env):
    """A branch on the push branch over the limit still goes back once; the order, the event and the hand-off
    line name what the size was measured against, as does the size line of its next review."""
    from ttp import splitgate
    p = make(env)
    p.set_config("chains.split_lines", 200)   # opt-in since 11-12 (default off)
    _other_base(p)
    tid, _, done, reviews = _finish_code(env, p, "big on work", {"big.py": 300})
    t = p.db.task(tid)
    assert t["status"] == "queued" and splitgate.returned(t) and not reviews
    assert "300 non-test lines (measured against the commit its worktree started from, " in t["spec"]
    assert "(measured against the commit its worktree started from, " in done["text"]
    ev = p.db.one("SELECT data FROM events WHERE kind=? AND task=?", (splitgate.EVENT, tid))
    assert json.loads(ev["data"])["base"].startswith("the commit its worktree started from")
    _, _, reviews = _again(env, p, tid, {"status": "done", "summary": "still big"})
    assert "Size: 310 non-test lines (measured against " in reviews[0]["spec"]
    assert splitgate.against("") == "" and "Size: 101 non-test lines (measured against main)" in \
        splitgate.review_line({"chains": {"split_lines": 100}}, {"id": 1, "kind": "code", "title": "t",
                                                                   "labels": "[]"}, {"a.py": 101}, base="main")

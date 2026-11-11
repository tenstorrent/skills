# Review continuation-owner tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add tests here for the daemon's live recheck of a reviewed task's
# cancel and continuation owner before it queues a review, a fix or a re-review (Daemon._stale_source,
# Daemon._continuation_owner).
from __future__ import annotations


def _owner_project(env):
    p = make(env)
    _with_origin(env, clone=False)
    p.set_config("budget.max_tasks_per_24h", 1000)
    return p


def _owner_unreviewed(env, p, title):
    """A finished code task whose daemon review was cancelled, so nothing reviews it: (id, branch)."""
    tid, branch, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 20})
    p.db.update_task(rev["id"], status="cancelled")
    return tid, branch


def _owner_reviews_of(p, tid):
    return [t for t in p.db.q("SELECT * FROM tasks WHERE kind='review' AND status!='cancelled'")
            if t["title"].startswith(f"Review #{tid}:")]


def test_a_task_cancelled_before_or_during_its_review_enqueue_gets_no_review(env, monkeypatch):
    """(a) A code task cancelled after its run finished gets no review from the sweep's call, and one
    cancelled while _auto_review reads its diff (between the first read and the add) gets none either:
    the recheck in the adding transaction wins. Not cancelled, it gets exactly one."""
    from ttp import worktree
    from ttp.daemon import Daemon
    p = _owner_project(env)
    d = Daemon(p.base)
    tid, _ = _owner_unreviewed(env, p, "early")
    p.db.update_task(tid, status="cancelled")
    assert d._auto_review(p.db.task(tid), "made it") is None and not _owner_reviews_of(p, tid)
    # Cancelled mid-way: the first check passed, the diff read cancels it, the add must not happen.
    tid, _ = _owner_unreviewed(env, p, "racing")
    real = worktree.diff_lines
    def cancel_then_read(proj, branches, *a, **k):
        p.db.update_task(tid, status="cancelled")
        return real(proj, branches, *a, **k)
    monkeypatch.setattr(worktree, "diff_lines", cancel_then_read)
    assert d._auto_review(p.db.task(tid), "made it") is None and not _owner_reviews_of(p, tid)
    monkeypatch.setattr(worktree, "diff_lines", real)
    # (d) The normal path: one review, and a second call returns that one.
    tid, _ = _owner_unreviewed(env, p, "normal")
    rid, added = d._auto_review(p.db.task(tid), "made it")
    assert added and [r["id"] for r in _owner_reviews_of(p, tid)] == [rid]
    assert d._auto_review(p.db.task(tid), "made it") == (rid, False)


def test_an_open_continuation_owns_the_review_and_a_continuer_on_another_branch_is_reviewed_on_its_own(env):
    """(b) While another open task continues a done code task, the done one gets no review of its own (the
    in-review sweep raises no 'cannot queue its review' event for it either); once it is cancelled and a
    continuation on a different branch finishes, that continuation is reviewed on its own branch and the
    cancelled task still gets nothing. A continuer that only waits on the task landing owns nothing."""
    from ttp.daemon import REVIEW_UNQUEUED, Daemon
    p = _owner_project(env)
    d = Daemon(p.base)
    a, abranch = _owner_unreviewed(env, p, "first")
    b = p.db.add_task("continue first", "carry on", kind="code", origin="coordinator", labels=[f"continues:{a}"])
    assert d._stale_source(p.db.task(a)) == f"open task #{b} continues it"
    assert d._auto_review(p.db.task(a), "made it") is None and not _owner_reviews_of(p, a)
    p.db.update_task(a, status="review")
    d._sweep_review(p.db.task(a), {}, [], {}, None, {}, {}, 0, time.time() + 10)
    assert not p.db.one("SELECT 1 FROM events WHERE kind=? AND task=?", (REVIEW_UNQUEUED, a))
    # A deploy check that waits on #a landing carries none of its work: #a is reviewed.
    p.db.update_task(b, status="cancelled")
    w = p.db.add_task("deploy first", "check it live", kind="code", origin="coordinator",
                      labels=[f"continues:{a}", f"start_when:ttp landed --task {a}"])
    assert d._continuation_owner(p.db.task(a)) is None and d._stale_source(p.db.task(a)) == ""
    p.db.update_task(w, status="cancelled")
    # #a cancelled, then continued on a branch of its own.
    p.db.update_task(a, status="cancelled")
    c, cbranch, _, reviews = _finish_code(env, p, "redo first", {"redo.py": 20}, labels=[f"continues:{a}"])
    assert cbranch != abranch and len(reviews) == 1, reviews
    assert f"branch {cbranch}," in reviews[0]["spec"] and f"branch {abranch}," not in reviews[0]["spec"]
    assert d._auto_review(p.db.task(a), "made it") is None and not _owner_reviews_of(p, a)


def _owner_failed_cancelled(env, p, title):
    """A code task whose daemon review failed, the code task then cancelled: (code id, branch, review)."""
    cid, branch, _, (rev,) = _finish_code(env, p, title, {f"{title}.py": 20})
    p.db.update_task(rev["id"], status="failed")
    p.db.update_task(cid, status="cancelled")
    return cid, branch, p.db.task(rev["id"])


def _owner_descendants(p, rev):
    return p.db.q("SELECT id, title FROM tasks WHERE title LIKE ? OR title LIKE ?",
                  (f"Fix review #{rev['id']}:%", f"Re-review #{rev['id']}:%"))


def test_a_failed_review_of_a_cancelled_task_hands_its_findings_to_the_continuing_owner(env):
    """(c) A failed review whose code task was cancelled and then continued queues no 'Fix review' or
    're-review' for the cancelled task: its findings go into the continuing owner's spec, once, and its
    run gets them as an update. With no continuer, nothing is queued and the coordinator is told why."""
    from ttp.daemon import Daemon
    p = _owner_project(env)
    d = Daemon(p.base)
    fups = [{"title": "off by one", "spec": "the loop skips the last row"}]
    cid, _, rev = _owner_failed_cancelled(env, p, "doomed")
    owner = p.db.add_task("redo doomed", "start over", kind="code", origin="coordinator",
                          labels=[f"continues:{cid}"])
    run_dir = env["tmp"] / f"owner-run-{owner}"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,status,dir,started) VALUES(?,?,?,?,?)",
           (owner, "worker", "running", str(run_dir), time.time()))
    assert d._fix_failed_review(rev, fups, "one bug") is None
    assert not _owner_descendants(p, rev) and d._nofix == ""
    spec = p.db.task(owner)["spec"]
    assert spec.startswith("start over") and f"Findings of review #{rev['id']} " in spec
    assert "the loop skips the last row" in spec and "one bug" in spec
    assert "the loop skips the last row" in (run_dir / "steer.md").read_text()
    # Called again (a restart re-handles the hand-off): nothing queued, the findings are not repeated.
    assert d._fix_failed_review(p.db.task(rev["id"]), fups, "one bug") is None
    assert not _owner_descendants(p, rev) and p.db.task(owner)["spec"] == spec
    # The owner's own finished work still gets its own review: the failed one is its prior round.
    p.db.update_task(owner, status="cancelled")
    o2, obranch, _, reviews = _finish_code(env, p, "redo doomed again", {"again.py": 20}, labels=[f"continues:{cid}"])
    assert len(reviews) == 1 and f"branch {obranch}," in reviews[0]["spec"]
    assert f'"continues:{rev["id"]}"' in reviews[0]["labels"] and reviews[0]["tier"] == "standard"
    # Cancelled with no continuer: no fix either, and why is left for the coordinator.
    cid2, _, rev2 = _owner_failed_cancelled(env, p, "abandoned")
    assert d._fix_failed_review(rev2, fups, "one bug") is None
    assert not _owner_descendants(p, rev2) and f"#{cid2}" in d._nofix and "cancelled" in d._nofix


def test_a_failed_review_with_no_cancel_still_queues_one_fix_and_one_re_review_per_round(env):
    """(d) With no cancel and no other continuer, a failed review gets exactly one fix and one re-review
    (a second call queues no second pair), and a continuer that only waits on the change landing does not
    take the findings: the fix is still queued, since the change cannot land without it."""
    from ttp.daemon import Daemon
    p = _owner_project(env)
    d = Daemon(p.base)
    fups = [{"title": "bug", "spec": "fix it"}]
    cid, _, _, (rev,) = _finish_code(env, p, "plain", {"plain.py": 20})
    p.db.update_task(rev["id"], status="failed")
    fid, rid, _ = d._fix_failed_review(p.db.task(rev["id"]), fups, "bug")
    assert fid and rid and len(_owner_descendants(p, rev)) == 2
    assert d._fix_failed_review(p.db.task(rev["id"]), fups, "bug") is None and len(_owner_descendants(p, rev)) == 2
    cid, _, _, (rev,) = _finish_code(env, p, "deployed", {"deployed.py": 20})
    p.db.update_task(rev["id"], status="failed")
    w = p.db.add_task("deploy it", "check it live", kind="code", origin="coordinator",
                      labels=[f"continues:{cid}", f"start_when:landed:#{cid}"])
    out = d._fix_failed_review(p.db.task(rev["id"]), fups, "bug")
    assert out and len(_owner_descendants(p, rev)) == 2 and "fix it" not in p.db.task(w)["spec"]


def test_an_open_continuation_in_the_project_root_owns_nothing_so_the_done_task_is_reviewed(env):
    """A continuation that gets no worktree of its own (a work task while worktree.kinds lists no
    'work') runs in the project root, never on the done task's branch: its review would not cover
    that work, so the done task gets its own review. Once work tasks get worktrees, it owns the work."""
    from ttp.daemon import Daemon
    p = _owner_project(env)
    d = Daemon(p.base)
    a, _ = _owner_unreviewed(env, p, "rooted")
    w = p.db.add_task("carry rooted on", "s", kind="work", origin="coordinator", labels=[f"continues:{a}"])
    assert d._continuation_owner(p.db.task(a)) is None and d._stale_source(p.db.task(a)) == ""
    rid, added = d._auto_review(p.db.task(a), "made it")
    assert added and [r["id"] for r in _owner_reviews_of(p, a)] == [rid]
    p.set_config("worktree.kinds", ["work"])
    assert d._continuation_owner(p.db.task(a))["id"] == w


def test_o424_a_done_code_task_whose_review_enqueue_timed_out_gets_its_review_from_the_backstop(env, monkeypatch):
    """Replay of o424 (fix tasks #7413, #7418): the hand-off's diff read hit an expired git deadline
    ('timed out after 0 seconds'), so no review was queued and the task ended done. The model-free
    backstop (sweep_unreviewed) queues exactly one review on a later pass."""
    import subprocess
    from ttp import worktree
    from ttp import daemon as dm
    p = _owner_project(env)
    real = worktree.diff_lines
    def expired(*a, **k):
        raise subprocess.TimeoutExpired(["git", "rev-parse", "--verify", "--quiet", "x^{commit}"], 0)
    monkeypatch.setattr(worktree, "diff_lines", expired)
    tid, branch, _, reviews = _finish_code(env, p, "timed out", {"timed_out.py": 20})
    assert not reviews and p.db.task(tid)["status"] == "done"
    monkeypatch.setattr(worktree, "diff_lines", real)
    when = time.time() - 600
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, tid))
    p.db.x("UPDATE runs SET ended=? WHERE task=?", (when, tid))
    d = dm.Daemon(p.base)
    assert d.sweep_unreviewed() == [tid]
    assert len(_owner_reviews_of(p, tid)) == 1 and branch in _owner_reviews_of(p, tid)[0]["spec"]
    assert d.sweep_unreviewed(force=True) == []

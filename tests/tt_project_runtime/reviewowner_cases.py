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


def test_7144_a_failed_review_read_is_retried_first_by_the_sweep_outside_its_cap_and_age_window(env, monkeypatch):
    """Replay of #7144: the hand-off's diff read timed out ('timed out after 0 seconds'), so no review was
    queued, and a backlog behind review.unreviewed_per_pass left it unreviewed for 14 h. The failed read
    is recorded (KV_REVIEW_RETRY); the sweep retries it first, counted against no cap, even past
    review.unreviewed_max_age_h; the mark clears once settled, a read that fails again records it again,
    and an unreadable mark never stops the sweep."""
    import subprocess
    from ttp import worktree
    from ttp import daemon as dm
    p = _owner_project(env)
    p.set_config("review.unreviewed_per_pass", 1)
    # The review's own read: a timed-out split read defers the review first (reviewsweep_cases).
    p.set_config("chains.split_lines", 0)
    older, _ = _owner_unreviewed(env, p, "older backlog")
    real = worktree.diff_lines
    def expired(*a, **k):
        raise subprocess.TimeoutExpired(["git", "rev-list", "-n1", "x", "--not", "y"], 0)
    monkeypatch.setattr(worktree, "diff_lines", expired)
    tid, branch, _, reviews = _finish_code(env, p, "read failed", {"read_failed.py": 20})
    assert not reviews and list(p.db.kv(dm.KV_REVIEW_RETRY)) == [str(tid)]
    # The sweep's own read fails too: the mark stays for the next pass, nothing is queued for it.
    _owner_aged(p, older, time.time() - 600)
    _owner_aged(p, tid, time.time() - 100 * 3600)   # past the 72 h window
    d = dm.Daemon(p.base)
    assert tid not in d.sweep_unreviewed(force=True) and list(p.db.kv(dm.KV_REVIEW_RETRY)) == [str(tid)]
    monkeypatch.setattr(worktree, "diff_lines", real)
    newer, _ = _owner_unreviewed(env, p, "newer backlog")
    _owner_aged(p, newer, time.time() - 600)
    got = d.sweep_unreviewed(force=True)
    assert got[0] == tid and len(got) == 2, got   # the retry first, plus one routine review under the cap
    assert len(_owner_reviews_of(p, tid)) == 1 and branch in _owner_reviews_of(p, tid)[0]["spec"]
    assert p.db.kv(dm.KV_REVIEW_RETRY) == {}
    assert d.sweep_unreviewed(force=True) == [newer if older in got else older]
    # A mark for a missing task or with an unreadable time is dropped or restarted, never a failed sweep.
    lines = []
    monkeypatch.setattr(dm, "log", lambda proj, msg: lines.append(msg))
    p.db.set_kv(dm.KV_REVIEW_RETRY, {"999999": "garbage", "x": 1, str(older): "garbage"})
    assert d.sweep_unreviewed(force=True) == [] and p.db.kv(dm.KV_REVIEW_RETRY) == {}
    assert [ln for ln in lines if "review read retry dropped: no such task" in ln], lines
    assert not [ln for ln in lines if "unreviewed sweep failed" in ln], lines


def _owner_aged(p, tid, when):
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (when, tid))
    p.db.x("UPDATE runs SET ended=? WHERE task=?", (when, tid))


def test_the_sweep_ignores_a_root_continuer_and_logs_an_urgent_tasks_skip_once_per_reason(env, monkeypatch):
    """The unreviewed sweep's 'an open task continues it' skip follows _continuation_owner: a continuer that
    works in the project root holds no review. An urgent task it leaves alone logs why once per reason, again
    only when the reason changes; a routine task's skip logs nothing."""
    from ttp import daemon as dm
    p = _owner_project(env)
    lines = []
    monkeypatch.setattr(dm, "log", lambda proj, msg: lines.append(msg))
    a, _ = _owner_unreviewed(env, p, "urgent swept")
    p.db.update_task(a, priority=1)
    _owner_aged(p, a, time.time() - 600)
    c = p.db.add_task("carry it", "s", kind="code", origin="coordinator", labels=[f"continues:{a}"])
    d = dm.Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == [] and not _owner_reviews_of(p, a)
    held = [ln for ln in lines if f"task {a}:" in ln and "an open task continues it" in ln]
    assert len(held) == 1, lines
    assert d.sweep_unreviewed(force=True) == []
    assert [ln for ln in lines if f"task {a}:" in ln and "no review queued" in ln] == held
    # A routine task held the same way logs nothing.
    b, _ = _owner_unreviewed(env, p, "routine swept")
    _owner_aged(p, b, time.time() - 600)
    p.db.add_task("carry b", "s", kind="code", origin="coordinator", labels=[f"continues:{b}"])
    assert d.sweep_unreviewed(force=True) == [] and not [ln for ln in lines if f"task {b}:" in ln and "no review queued" in ln]
    # The code continuer becomes a root-working one: it holds nothing, the urgent task is reviewed.
    p.db.update_task(c, kind="work")
    assert d.sweep_unreviewed(force=True) == [a] and len(_owner_reviews_of(p, a)) == 1


def test_a_done_task_whose_continuer_was_cancelled_late_is_still_reviewed(env):
    """A done code task older than review.unreviewed_max_age_h whose continuer held its review until it was
    cancelled within that window gets its review: its age runs from the continuer's cancel. A continuer
    cancelled long ago, or a task with no continuer, leaves an old task out of the window as before."""
    from ttp.daemon import Daemon
    p = _owner_project(env)
    p.set_config("review.unreviewed_max_age_h", 2)
    old = time.time() - 5 * 3600
    a, _ = _owner_unreviewed(env, p, "held long")
    _owner_aged(p, a, old)
    c = p.db.add_task("carry held", "s", kind="code", origin="coordinator", labels=[f"continues:{a}"])
    b, _ = _owner_unreviewed(env, p, "cancelled early")
    _owner_aged(p, b, old)
    e = p.db.add_task("carry early", "s", kind="code", origin="coordinator", labels=[f"continues:{b}"])
    n, _ = _owner_unreviewed(env, p, "no continuer")
    _owner_aged(p, n, old)
    p.db.update_task(c, status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 600, c))
    p.db.update_task(e, status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (old, e))
    d = Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == [a]
    assert len(_owner_reviews_of(p, a)) == 1 and not _owner_reviews_of(p, b) and not _owner_reviews_of(p, n)


def test_a_second_failed_review_with_an_open_fix_gets_its_own_fix_and_re_review(env):
    """An open fix of an earlier failed review (review_fix:) or of a PR (pr_fix: on another stack) continues
    the code task, but owns no later failed review's findings: that fix has its own re-review, so the later
    review gets its own fix and re-review and moves its dependents. A plain open continuer still takes the
    findings (test (c) above)."""
    from ttp.daemon import Daemon
    p = _owner_project(env)
    p.set_config("review.auto_fix_rounds", 2)   # rounds count per change (6617f325d): allow this second one
    d = Daemon(p.base)
    fups = [{"title": "bug", "spec": "fix it"}]
    cid, branch, _, (rev,) = _finish_code(env, p, "twice", {"twice.py": 20})
    p.db.update_task(rev["id"], status="failed")
    fid, rrid, _ = d._fix_failed_review(p.db.task(rev["id"]), fups, "bug")
    assert d._continuation_owner(p.db.task(cid))["id"] == fid
    assert d._continuation_owner(p.db.task(cid), fixes=False) is None
    # A second review of the same change fails while the first fix is still open, with a task waiting on it.
    rev2 = p.db.add_task(f"Review #{cid}: twice again", f"Review branch {branch}.", kind="review",
                         origin="coordinator", depends_on=[cid], labels=[f"auto_review:{cid}"])
    dep = p.db.add_task("after twice", "s", kind="work", origin="coordinator", depends_on=[rev2])
    p.db.update_task(rev2, status="failed")
    out = d._fix_failed_review(p.db.task(rev2), [{"title": "other", "spec": "the second finding"}], "other")
    assert out and out[0] != fid and dep in out[2], out
    assert "the second finding" not in p.db.task(fid)["spec"]
    assert f'"review_fix:{rev2}"' in p.db.task(out[0])["labels"]

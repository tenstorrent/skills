# The review sweep (Daemon.sweep_reviews) of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers (make, _unreviewed, _ran, _sweep, _daemon_reviews),
# fixtures and imports come from that file, and pytest collects these tests under its name. Add tests of
# code tasks left in review with no review, and of the review_unqueued event, here.
from __future__ import annotations


def _unqueued(p, tid=None):
    q = "SELECT * FROM events WHERE kind='review_unqueued'"
    return p.db.q(q + " AND task=?", (tid,)) if tid else p.db.q(q)


def test_a_task_left_in_review_gets_exactly_one_review_with_its_follow_ups_and_a_second_pass_adds_none(env):
    from ttp.daemon import Daemon
    p = make(env)
    tid, branch, head = _unreviewed(env, p, "left fix", status="review", age=1900, result={
        "status": "needs_review", "summary": "left fix works", "followups": [{"title": "tidy later", "spec": "s"}]})
    d = Daemon(p.base)
    _sweep(d)
    (rev,) = _daemon_reviews(p, tid)
    assert rev["title"] == f"Review #{tid}: left fix" and f"branch {branch}, head {head[:12]}" in rev["spec"]
    assert "tidy later" in rev["spec"] and "left fix works" in rev["spec"]
    _sweep(d)
    _sweep(Daemon(p.base))
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1 and not _unqueued(p)


def test_a_task_in_review_with_no_run_on_record_is_swept_from_when_it_entered_review(env):
    """A task set to review with no run row (by hand, or a run row lost to a crash) was skipped for good."""
    from ttp.daemon import Daemon
    p = make(env)
    tid, branch, _ = _unreviewed(env, p, "no run", status="review", age=1900)
    p.db.x("DELETE FROM runs WHERE task=?", (tid,))
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time() - 1900, f"task:{tid}", "task_review", "normal", "handed off", "handled", tid))
    _sweep(Daemon(p.base))
    assert len(_daemon_reviews(p, tid)) == 1
    # Entered review just now: it waits review.sweep_after_s like a task with a run.
    fresh, _, _ = _unreviewed(env, p, "fresh", status="review", age=0)
    p.db.x("DELETE FROM runs WHERE task=?", (fresh,))
    _sweep(Daemon(p.base))
    assert not _daemon_reviews(p, fresh)


def test_a_task_whose_review_is_open_done_or_failed_gets_no_second_review_or_event(env):
    from ttp.daemon import Daemon
    p = make(env)
    for status in ("queued", "running", "done", "failed"):
        tid, _, _ = _unreviewed(env, p, f"has {status}", status="review", age=1900)
        rid = p.db.add_task(f"Review #{tid}: has {status}", "review it", kind="review", origin="coordinator",
                            depends_on=[tid])
        p.db.update_task(rid, status=status)
        _sweep(Daemon(p.base))
        assert len(p.db.q("SELECT id FROM tasks WHERE kind='review' AND depends_on=?", (json.dumps([tid]),))) == 1, \
            status
        assert not _unqueued(p, tid), status


def test_a_task_with_an_unreadable_head_raises_one_coordinator_event_and_gets_no_review(env):
    from ttp.daemon import Daemon
    p = make(env)
    tid, _, _ = _unreviewed(env, p, "gone branch", status="review", age=1900)
    p.db.update_task(tid, branch="ttp/no-such-branch")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 1900, tid))
    for _ in range(3):
        _sweep(Daemon(p.base))
    assert not _daemon_reviews(p, tid)
    (ev,) = _unqueued(p, tid)
    assert ev["status"] == "queued" and ev["source"] == "daemon" and ev["severity"] == "normal"
    assert f"#{tid} gone branch" in ev["text"] and "cannot queue its review" in ev["text"]
    assert not p.db.q("SELECT id FROM events WHERE task=? AND severity='high'", (tid,)), "never a user alert"
    from ttp import coordinator
    assert coordinator.EFFORT_EVENT_TRIGGERS.get("review_unqueued") == "stuck"


def test_no_review_step_raises_the_event_and_a_full_review_cap_waits_silently(env):
    from ttp.daemon import Daemon
    p = make(env)
    tid, _, _ = _unreviewed(env, p, "capped", status="review", age=1900)
    p.set_config("coordinator.max_review_tasks_per_day", 0)
    _sweep(Daemon(p.base))
    assert not _daemon_reviews(p, tid) and not _unqueued(p), "a full cap is a wait, not a fault"
    p.set_config("coordinator.max_review_tasks_per_day", 40)
    p.set_config("review.auto", False)
    _sweep(Daemon(p.base))
    (ev,) = _unqueued(p, tid)
    assert "review.auto is off or delivery has no review step" in ev["text"] and not _daemon_reviews(p, tid)


def test_a_branchless_needs_review_hand_off_of_a_change_in_another_repo_is_raised_once_per_hand_off(env):
    """The root cause seen in the field: a code task worked in another repository's clone, so no branch
    is on record, and its needs_review hand-off named the branch and head only in its summary, with no
    PR and no `published` change. _auto_review returns None for it, and the sweep used to drop that
    silently on every pass, so the task sat in review with no review and no one told."""
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("fix in another repo", "spec", kind="code", origin="coordinator")
    p.db.update_task(tid, status="review", result=json.dumps({
        "status": "needs_review", "summary": "Commit abc123 on branch fix/x of the other repo; gate passed."}))
    _ran(p, tid, time.time() - 1900)
    d = Daemon(p.base)
    for _ in range(3):
        _sweep(d)
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'")
    (ev,) = _unqueued(p, tid)
    assert "no branch on record" in ev["text"] and "`published`" in ev["text"]
    # A later hand-off (a new run) that still names nothing reviewable is raised again, once.
    _ran(p, tid, time.time() - 1800)
    _sweep(d)
    _sweep(d)
    assert len(_unqueued(p, tid)) == 2
    # Once it hands off its change as `published`, the sweep queues its review there instead.
    p.db.update_task(tid, result=json.dumps({
        "status": "needs_review", "summary": "ok",
        "published": {"repo": "acme/widgets", "branch": "fix/x", "head": "ab" * 20}}))
    _sweep(d)
    assert len(_daemon_reviews(p, tid)) == 1 and len(_unqueued(p, tid)) == 2


def test_one_task_s_error_skips_only_that_task_in_the_review_sweep(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    bad, _, _ = _unreviewed(env, p, "breaks", status="review", age=1900)
    good, _, _ = _unreviewed(env, p, "fine", status="review", age=1900)
    real = Daemon._landed_elsewhere

    def landed(self, t):
        if t["id"] == bad:
            raise RuntimeError("boom")
        return real(self, t)
    monkeypatch.setattr(Daemon, "_landed_elsewhere", landed)
    _sweep(Daemon(p.base))
    assert len(_daemon_reviews(p, good)) == 1 and not _daemon_reviews(p, bad)
    assert "review sweep of task %d failed" % bad in (p.logs / "daemon.log").read_text()

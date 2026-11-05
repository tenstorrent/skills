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


def test_a_branchless_hand_off_s_top_level_branch_and_head_queue_exactly_one_review_of_that_head(env):
    """A code task in a project root that is no git repository has no branch on record: its hand-off's
    top-level `branch` + `head` (+ optional `repo`) are taken as its `published` change. A branch with
    no head, a malformed head, or a branch and sha named only in the summary queue nothing."""
    from ttp.daemon import Daemon
    from ttp import extchange
    p = make(env)
    head = "c0ffee" * 6 + "abcd"
    bad = [{"branch": "fix/y"}, {"branch": "fix/y", "head": "xyz1234"}, {"branch": "fix/y", "head": "ab" * 32},
           {"branch": "fix/y", "head": "abc12"}, {"head": head}, {"branch": "a..b", "head": head},
           {"branch": "fix/y", "head": head, "repo": "not a repo"},
           {"summary": f"Pushed {head} on branch fix/y of example/other."}]
    tids = []
    for i, extra in enumerate(bad):
        tid = p.db.add_task(f"branchless {i}", "spec", kind="code", origin="coordinator")
        p.db.update_task(tid, status="review", result=json.dumps({"status": "done", "summary": "ok", **extra}))
        _ran(p, tid, time.time() - 1900)
        tids.append(tid)
    good = p.db.add_task("branchless good", "spec", kind="code", origin="coordinator")
    p.db.update_task(good, status="review", result=json.dumps({
        "status": "done", "summary": "ok", "branch": "fix/y", "head": head.upper()}))
    _ran(p, good, time.time() - 1900)
    named = p.db.add_task("branchless with repo", "spec", kind="code", origin="coordinator")
    p.db.update_task(named, status="review", result=json.dumps({
        "status": "done", "summary": "ok", "repo": "example/other", "branch": "feat/z", "head": head[:12]}))
    _ran(p, named, time.time() - 1900)
    d = Daemon(p.base)
    _sweep(d)
    _sweep(d)
    for tid in tids:
        assert not _daemon_reviews(p, tid) and len(_unqueued(p, tid)) == 1, tid
    (rev,) = _daemon_reviews(p, good)
    assert f"branch fix/y, head {head[:12]}" in rev["spec"] and "repo " not in rev["spec"].split("\n")[0]
    assert extchange.HEAD_LABEL + head in rev["labels"]
    assert "names no repository" in rev["spec"] and not _unqueued(p, good)
    (rev2,) = _daemon_reviews(p, named)
    assert "repo example/other, branch feat/z" in rev2["spec"] and extchange.REVIEW_ONLY in rev2["spec"]
    # A task with a branch on record never takes the top-level fields: its own branch is its change.
    other = p.db.add_task("has a branch", "spec", kind="code", origin="coordinator")
    p.db.update_task(other, branch="ttp/none-such", result=json.dumps({
        "status": "done", "summary": "ok", "branch": "fix/y", "head": head}))
    assert d._external_change(p.db.task(other)) is None


def test_a_question_hop_with_a_branch_commit_gets_one_review_at_hand_off_and_one_without_a_diff_none(env):
    """#6127: a `continue:` hop kept its parent's kind 'question', committed on its branch and sat in
    review with no review queued. A done task of any kind but review or harness whose branch changes
    anything gets exactly one review at its hand-off, with no coordinator turn; one that changes nothing
    gets none, and the review sweep raises nothing for it."""
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)   # a repo with no remote never pushes: review only
    parent = p.db.add_task("look into caps", "spec", kind="question", origin="coordinator")
    tid, branch, done, reviews = _finish_code(env, p, "continue: caps line", {"caps.py": 5}, kind="question",
                                              labels=[f"continues:{parent}"])
    (rev,) = reviews
    assert rev["title"] == f"Review #{tid}: continue: caps line" and rev["origin"] == "daemon"
    assert f"branch {branch}, head " in rev["spec"] and "spec of continue: caps line" in rev["spec"]
    assert done["status"] == "handled", "a routine hand-off still starts a coordinator turn"
    assert f"Review #{rev['id']} queued by the daemon" in done["text"]
    # One head gets one review: asking again (a sweep, a repeated hand-off) names the same one.
    d = Daemon(p.base)
    assert d._auto_review(p.db.task(tid), "again") == (rev["id"], False)
    p.db.update_task(tid, status="review")
    _sweep(d)
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1
    # A question (or work) task whose branch changes nothing gets no review.
    for kind in ("question", "work"):
        q, _, qdone, none = _finish_code(env, p, f"{kind} only", {}, kind=kind)
        assert not none and "queued by the daemon" not in qdone["text"]
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1 and not _unqueued(p)


def test_the_sweep_reviews_a_non_code_task_in_review_with_a_diff_once_and_skips_one_without(env):
    from ttp.daemon import Daemon
    p = make(env)
    tid, branch, head = _unreviewed(env, p, "work hop", kind="work", status="review", age=1900, result={
        "status": "needs_review", "summary": "work hop committed"})
    bare = p.db.add_task("plain question", "spec", kind="question", origin="coordinator")
    p.db.update_task(bare, status="review", result=json.dumps({"status": "needs_review", "summary": "answer"}))
    _ran(p, bare, time.time() - 1900)
    d = Daemon(p.base)
    _sweep(d)
    (rev,) = _daemon_reviews(p, tid)
    assert f"branch {branch}, head {head[:12]}" in rev["spec"]
    assert not _daemon_reviews(p, bare) and not _unqueued(p)
    _sweep(Daemon(p.base))
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1 and not _unqueued(p)


def test_a_review_or_harness_task_s_own_commits_are_never_a_change_to_review(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    for kind, want in (("review", False), ("harness", False), ("code", True)):
        tid, _, _ = _unreviewed(env, p, f"{kind} commits", kind=kind)
        assert d._code_change(p.db.task(tid)) is want, kind
    tid = p.db.add_task("no branch", "spec", kind="question", origin="coordinator")
    assert d._code_change(p.db.task(tid)) is False

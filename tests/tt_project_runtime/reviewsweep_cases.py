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



def test_a_hand_off_s_branch_in_the_project_s_own_git_is_no_external_change_and_is_recorded(env):
    """A code task whose branch record was lost names its branch and head at the top level of its
    hand-off: both are in the project's own git, so its review is a normal one (pushable, no no_push or
    'not in this project's git' wording) and the branch is recorded on the task again."""
    from ttp.daemon import Daemon
    from ttp import extchange, worktree
    p = make(env)
    tid = p.db.add_task("lost its branch", "spec", kind="code", origin="coordinator")
    path, own = worktree.ensure(p, p.db.task(tid))
    head = _commit_file_in(path, "fix.txt")
    p.db.update_task(tid, branch=None, status="review", result=json.dumps({
        "status": "done", "summary": "ok", "branch": own, "head": head}))
    _ran(p, tid, time.time() - 1900)
    d = Daemon(p.base)
    assert d._external_change(p.db.task(tid)) is None and p.db.task(tid)["branch"] == own
    p.db.update_task(tid, branch=None)
    _sweep(d)
    (rev,) = _daemon_reviews(p, tid)
    assert extchange.NO_PUSH_LINE not in rev["spec"] and extchange.REVIEW_ONLY not in rev["spec"]
    assert "not in this project's git" not in rev["spec"] and "external repository" not in rev["spec"]
    assert f"branch {own}" in rev["spec"] and p.db.task(tid)["branch"] == own


def test_a_root_git_config_saying_bare_is_healed_and_dispatch_keeps_a_recorded_branch(env):
    """A project root whose .git says core.bare=true (written outside the harness) made code tasks start in
    the root with no worktree, and dispatch then cleared their recorded branch: _workdir_for sets it back
    to false and gives the task its worktree on its branch."""
    from ttp.daemon import Daemon
    from ttp import worktree
    p = make(env)
    tid = p.db.add_task("keeps its branch", "spec", kind="code", origin="coordinator")
    path, own = worktree.ensure(p, p.db.task(tid))
    p.db.update_task(tid, branch=own)
    _git_out(p.root, "config", "core.bare", "true")
    assert not worktree.is_git(p.root)
    assert Daemon(p.base)._workdir_for(p.db.task(tid)) == (str(path), own)
    assert worktree.is_git(p.root) and _git_out(p.root, "config", "core.bare") == "false"
    assert "core.bare=true" in (p.logs / "daemon.log").read_text()


def test_a_work_task_with_a_branch_commit_gets_one_review_at_hand_off_and_one_without_a_diff_none(env):
    """A done work task whose branch changes anything gets exactly one review at its hand-off, as a code
    task does; one that changes nothing gets none, and the review sweep adds or raises nothing."""
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)   # a repo with no remote never pushes: review only
    tid, branch, done, reviews = _finish_code(env, p, "measure caps", {"caps.py": 5}, kind="work")
    (rev,) = reviews
    assert rev["title"] == f"Review #{tid}: measure caps" and rev["origin"] == "daemon"
    assert f"branch {branch}, head " in rev["spec"] and "spec of measure caps" in rev["spec"]
    assert f"Review #{rev['id']} queued by the daemon" in done["text"]
    # One head gets one review: asking again (a sweep, a repeated hand-off) names the same one.
    d = Daemon(p.base)
    assert d._auto_review(p.db.task(tid), "again") == (rev["id"], False)
    p.db.update_task(tid, status="review")
    _sweep(d)
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1
    _, _, wdone, none = _finish_code(env, p, "work only", {}, kind="work")
    assert not none and "queued by the daemon" not in wdone["text"]
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1 and not _unqueued(p)


def test_a_question_or_plan_task_with_a_branch_commit_gets_no_review_the_documented_gap(env):
    """Only code and work tasks are reviewed by the daemon: a question or plan task that committed (also a
    `continue:` hop that kept such a kind) gets none at hand-off or from the sweep; its branch reaches the
    coordinator as a local_only event (guide.md)."""
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    for kind in ("question", "plan"):
        parent = p.db.add_task(f"look into {kind}", "spec", kind=kind, origin="coordinator")
        tid, _, done, none = _finish_code(env, p, f"continue: {kind} line", {f"{kind}.py": 5}, kind=kind,
                                          labels=[f"continues:{parent}"])
        assert not none and "queued by the daemon" not in done["text"]
        assert Daemon(p.base)._code_change(p.db.task(tid)) is False
        p.db.update_task(tid, status="review")
        _ran(p, tid, time.time() - 1900)
    _sweep(Daemon(p.base))
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'")


def test_a_work_hop_that_hands_off_another_continue_hop_is_a_split_with_no_review(env):
    """#6378: the split gate keyed on kind 'code' while the review gate took work tasks, so a work hop that
    left its change unfinished got a review of a half-done head. _code_change gates both: the head is a
    split, no review is queued, and the next hop takes the branch over."""
    from ttp import chaincap
    p = make(env)
    _with_origin(env, clone=False)
    parent = p.db.add_task("caps work", "spec", kind="work", origin="coordinator")
    tid, branch, done, reviews = _finish_code(
        env, p, "continue: caps line", {"caps.py": 5}, kind="work", labels=[f"continues:{parent}"],
        result={"status": "done", "summary": "half done", "followups": [
            {"title": "continue: caps line part 2", "spec": "Finish the caps line on this branch, then hand off."}]})
    head = _git_out(p.root, "rev-parse", branch).strip()
    assert chaincap.split_head(p.db.task(tid)) == head and not reviews
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'") and f"Split at head {head[:12]}" in done["text"]


def test_code_change_is_any_code_task_and_a_work_task_with_a_diff_only(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    for kind, want in (("review", False), ("harness", False), ("question", False), ("plan", False),
                       ("code", True), ("work", True)):
        tid, _, _ = _unreviewed(env, p, f"{kind} commits", kind=kind)
        assert d._code_change(p.db.task(tid)) is want, kind
    for kind, want in (("code", True), ("work", False)):   # no branch: a code task is still one, as before
        tid = p.db.add_task(f"{kind} no branch", "spec", kind=kind, origin="coordinator")
        assert d._code_change(p.db.task(tid)) is want, kind


def test_a_finish_whose_diff_read_timed_out_gets_its_review_on_the_next_tick(env, monkeypatch):
    """A hand-off whose git read is cut off (the step budget, a slow disk: TimeoutExpired) queues no review
    then, but the next tick's retry_unread_reviews does, not the unreviewed sweep hours later. A read that
    times out again keeps it for the tick after; once queued, it is not retried."""
    import subprocess
    from ttp import worktree
    from ttp.daemon import Daemon, KV_REVIEW_UNREAD
    p = make(env)
    _with_origin(env, clone=False)
    real, cut = worktree.diff_lines, {"n": 1}

    def slow(*a, **k):
        if cut["n"]:
            cut["n"] -= 1
            raise subprocess.TimeoutExpired(["git", "diff"], 0)
        return real(*a, **k)
    monkeypatch.setattr(worktree, "diff_lines", slow)
    tid, _, done, reviews = _finish_code(env, p, "cut read", {"app.py": 5})
    assert not reviews and "queued by the daemon" not in done["text"]
    assert p.db.kv(KV_REVIEW_UNREAD) == [tid]
    d = Daemon(p.base)
    cut["n"] = 1   # timed out again: kept for the next tick
    assert d.retry_unread_reviews() == [] and p.db.kv(KV_REVIEW_UNREAD) == [tid]
    (rid,) = d.retry_unread_reviews()
    assert p.db.task(rid)["title"] == f"Review #{tid}: cut read" and p.db.kv(KV_REVIEW_UNREAD) == []
    assert d.retry_unread_reviews() == []
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 1


def test_a_finish_with_its_round_slice_or_git_budget_spent_defers_its_review_to_the_next_tick(env):
    # #7690: a finish's last round (one that never aborts) past its strict slice gave _auto_review's
    # rev-parse a 0 s timeout. It still runs no git under the lock past the slice: the read is not started
    # (gitmemo.Spent, never 0 s), the task is kept unread and the next tick queues its review; so does a
    # transaction whose whole git budget is spent.
    from ttp import daemon as dm, gitmemo, gitreplay
    p = make(env)
    sliced = _unreviewed(env, p, "slice spent", age=5)[0]
    later = _unreviewed(env, p, "budget spent", age=5)[0]
    d = dm.Daemon(p.base)
    gitreplay.clear()
    with gitmemo.bounded(gitmemo.TX_GIT_S, gitmemo.TX_GIT_S), gitmemo.attempt(), \
            gitmemo.strict(0.0, abortable=False):
        assert d._auto_review(p.db.task(sliced), "slice spent works", add=True) is None
    gitreplay.clear()
    with gitmemo.bounded(gitmemo.TX_GIT_S, 0.0):
        time.sleep(0.01)
        assert d._auto_review(p.db.task(later), "budget spent works", add=True) is None
    assert p.db.kv(dm.KV_REVIEW_UNREAD) == sorted([sliced, later]) and not _daemon_reviews(p, sliced)
    assert "after 0 seconds" not in (p.logs / "daemon.log").read_text()
    assert len(d.retry_unread_reviews()) == 2, "the next tick queues both"
    assert len(_daemon_reviews(p, sliced)) == 1 and len(_daemon_reviews(p, later)) == 1


def test_a_finish_with_the_review_cap_full_gets_its_review_once_the_cap_allows_one(env, monkeypatch):
    """A finish whose review the rolling review cap holds marks the task review-pending (KV_REVIEW_UNREAD):
    while the cap stays full the next tick keeps it without a git read, and the first tick the cap allows
    queues its review, not the unreviewed sweep hours later."""
    from ttp import coordinator, worktree
    from ttp.daemon import Daemon, KV_REVIEW_UNREAD
    p = make(env)
    _with_origin(env, clone=False)
    real, full = coordinator.next_task_slot, {"on": True}
    monkeypatch.setattr(coordinator, "next_task_slot", lambda db, cap, now=None, review=False:
                        time.time() + 60 if review and full["on"] else real(db, cap, now, review))
    tid, _, done, reviews = _finish_code(env, p, "capped fix", {"app.py": 5})
    assert not reviews and p.db.kv(KV_REVIEW_UNREAD) == [tid]
    reads, diff = [], worktree.diff_lines
    monkeypatch.setattr(worktree, "diff_lines", lambda *a, **k: reads.append(1) or diff(*a, **k))
    d = Daemon(p.base)
    assert d.retry_unread_reviews() == [] and p.db.kv(KV_REVIEW_UNREAD) == [tid] and not reads
    full["on"] = False
    (rid,) = d.retry_unread_reviews()
    assert p.db.task(rid)["title"] == f"Review #{tid}: capped fix" and p.db.kv(KV_REVIEW_UNREAD) == []


def test_a_finish_reads_git_for_its_review_under_its_own_short_cap(env, monkeypatch):
    """The finish's review read runs under gitmemo.FINISH_REVIEW_GIT_S, not the finish transaction's whole
    budget: a slow read leaves the review to the next tick."""
    import sys
    from ttp import gitmemo, worktree
    from ttp.daemon import KV_REVIEW_UNREAD
    p = make(env)
    _with_origin(env, clone=False)
    caps, diff = [], worktree.diff_lines

    def read(*a, **k):   # the review's own reads only: the split gate's memoized read runs before it
        f, mine = sys._getframe(1), False
        while f is not None and not mine:
            f, mine = f.f_back, f.f_code.co_name == "_auto_review"
        if mine:
            caps.append(gitmemo.timeout(None, read=True))
        return diff(*a, **k)
    monkeypatch.setattr(worktree, "diff_lines", read)
    tid, _, _, reviews = _finish_code(env, p, "short read", {"app.py": 5})
    assert reviews and caps and all(c is not None and c <= gitmemo.FINISH_REVIEW_GIT_S for c in caps), caps
    assert gitmemo.FINISH_REVIEW_GIT_S < gitmemo.TX_GIT_S and not p.db.kv(KV_REVIEW_UNREAD)


def test_the_unreviewed_sweep_reads_every_head_in_one_git_call(env, monkeypatch):
    """sweep_unreviewed reads its tasks' branch heads in one batched cat-file (worktree.branch_heads),
    never one rev-parse per task, and still finds each task unreviewed."""
    import sys
    from ttp import daemon as dm, worktree
    p = make(env)
    tids = [_unreviewed(env, p, f"batched {n}")[0] for n in range(3)]
    calls, git, batches, heads = [], worktree._git, [], worktree.branch_heads
    monkeypatch.setattr(worktree, "_git", lambda *a, **k: (sys._getframe(1).f_code.co_name == "_unreviewed_skip"
                                                           and calls.append(a)) or git(*a, **k))
    monkeypatch.setattr(worktree, "branch_heads", lambda pr, b: batches.append(b) or heads(pr, b))
    d = dm.Daemon(p.base)
    d.cfg.setdefault("review", {})["unreviewed_per_pass"] = 5
    queued = []   # _auto_review reads its own head: only the sweep's own reads are counted
    monkeypatch.setattr(d, "_auto_review", lambda t, *a, **k: queued.append(t["id"]))
    d.sweep_unreviewed()
    assert sorted(queued) == tids
    assert len(batches) == 1 and sorted(batches[0]) == sorted(p.db.task(t)["branch"] for t in tids)
    assert not calls, "the sweep read a head on its own"

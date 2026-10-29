# Review-hop priority tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new chainprio.py tests (the priority of daemon-queued Review, Fix review
# and Re-review tasks) here.
from __future__ import annotations


def _failed_chain(env, p, priority):
    """A code task at `priority`, its daemon review failed, and the fix and re-review the daemon queued.
    Returns (code id, review, fix, re-review)."""
    from ttp.daemon import Daemon
    p.set_config("budget.max_tasks_per_24h", 1000)
    cid, _, _, (rev,) = _finish_code(env, p, f"p{priority} change", {"app.py": 20},
                                     before=lambda tid, head: p.db.update_task(tid, priority=priority))
    p.db.update_task(rev["id"], status="failed")
    fid, rid, _ = Daemon(p.base)._fix_failed_review(p.db.task(rev["id"]), [{"title": "bug", "spec": "fix it"}], "bug")
    return cid, p.db.task(rev["id"]), p.db.task(fid), p.db.task(rid)


def test_a_priority_1_change_keeps_priority_1_through_review_fix_and_re_review(env):
    """An urgent code task's daemon review, the fix of that failed review and the re-review all start
    at priority 1, so none waits behind routine work; a second round (the re-review fails too) and a
    hop whose own covered task is routine stay at the chain root's priority 1."""
    from ttp import chainprio
    from ttp.daemon import Daemon
    p = make(env)
    _with_origin(env, clone=False)
    cid, rev, fix, rr = _failed_chain(env, p, 1)
    assert [rev["priority"], fix["priority"], rr["priority"]] == [1, 1, 1], (rev, fix, rr)
    assert rev["title"].startswith(f"Review #{cid}:") and fix["title"].startswith(f"Fix review #{rev['id']}:")
    assert rr["title"].startswith(f"Re-review #{rev['id']}:")
    # The fix ran at the default priority (a task added by hand): the root still makes its hops urgent.
    p.db.update_task(fix["id"], priority=3, status="done", branch=p.db.task(cid)["branch"])
    p.db.update_task(rr["id"], status="failed")
    out = Daemon(p.base)._fix_failed_review(p.db.task(rr["id"]), [{"title": "bug 2", "spec": "fix it"}], "bug 2")
    assert out, "no second-round fix queued"
    assert p.db.task(out[0])["priority"] == 1 and p.db.task(out[1])["priority"] == 1
    assert chainprio.of(p.db, p.db.task(fix["id"])) == 1


def test_a_routine_chain_stays_routine_and_follows_its_parent_unless_set_explicitly(env):
    """A priority 3 code task's review, fix and re-review stay at 3 (not raised, not the old fixed 2).
    Raising the code task with task_update moves its open hops to 1; one the coordinator set a
    priority on keeps it when the parent moves again, and the hops after it take its urgency."""
    from ttp import chainprio
    from ttp import coordinator as coord
    p = make(env)
    _with_origin(env, clone=False)
    cid, rev, fix, rr = _failed_chain(env, p, 3)
    assert [rev["priority"], fix["priority"], rr["priority"]] == [3, 3, 3], (rev, fix, rr)
    assert coord.apply(p, [{"type": "task_update", "id": cid, "priority": 1}]) == []
    assert [p.db.task(t)["priority"] for t in (fix["id"], rr["id"])] == [1, 1]
    assert p.db.task(rev["id"])["priority"] == 3, "a closed review is history: it does not move"
    assert coord.apply(p, [{"type": "task_update", "id": rr["id"], "priority": 2}]) == []
    assert chainprio.SET_LABEL in json.loads(p.db.task(rr["id"])["labels"])
    assert coord.apply(p, [{"type": "task_update", "id": cid, "priority": 3}]) == []
    assert p.db.task(fix["id"])["priority"] == 3 and p.db.task(rr["id"])["priority"] == 2
    # A hop set explicitly passes its urgency on: the fix after an urgent-set review is urgent.
    assert coord.apply(p, [{"type": "task_update", "id": fix["id"], "priority": 1}]) == []
    assert chainprio.of(p.db, p.db.task(fix["id"])) == 1


def test_chain_priority_ignores_unrelated_chains_and_bad_labels(env):
    """follow moves only the changed task's own chain; hops with unreadable or dangling labels keep
    their priority and the computation never loops on a label cycle."""
    from ttp import chainprio
    p = make(env)
    a = p.db.add_task("a", "s", kind="code", origin="coordinator", priority=3)
    b = p.db.add_task("b", "s", kind="code", origin="coordinator", priority=3)
    ra = p.db.add_task(f"Review #{a}: a", "s", kind="review", origin="daemon", priority=3,
                       labels=[f"auto_review:{a}"], depends_on=[a])
    rb = p.db.add_task(f"Review #{b}: b", "s", kind="review", origin="daemon", priority=3,
                       labels=[f"auto_review:{b}"], depends_on=[b])
    junk = p.db.add_task("Re-review #999: x", "s", kind="review", origin="daemon", priority=2,
                         labels=["continues:999"])
    p.db.x("UPDATE tasks SET labels='not json' WHERE id=?", (rb,))
    p.db.update_task(a, priority=1)
    assert [i for i, _, _ in chainprio.follow(p.db, a)] == [ra]
    assert p.db.task(rb)["priority"] == 3 and p.db.task(junk)["priority"] == 2
    # Two hops that name each other (a corrupt record) still give an answer.
    x = p.db.add_task("Fix review #1: x", "s", kind="code", origin="daemon", priority=2)
    y = p.db.add_task("Re-review #1: y", "s", kind="review", origin="daemon", priority=3,
                      labels=[f"auto_review:{x}"])
    p.db.update_task(x, labels=[f"continues:{y}"])
    assert chainprio.of(p.db, p.db.task(x)) in (2, 3)

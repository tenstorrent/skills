# Urgent-lane tests (ttp/urgent.py: what makes a task urgent, its reviews, device queue and status marks)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file, and pytest collects these tests under its
# name. Its dispatch, push-queue and lock-waiter tests are in dispatch_cases, push_cases and lock_cases.
from __future__ import annotations


def test_a_task_is_urgent_at_priority_one_or_with_an_urgent_spec_line_only():
    from ttp import urgent
    assert urgent.is_urgent({"priority": 1}) and urgent.is_urgent({"priority": 0})
    assert urgent.is_urgent({"priority": 3, "spec": "Do it.\n  URGENT: the user is waiting \n"})
    assert urgent.why({"priority": 3, "spec": "Do it.\nurgent: the user is waiting \n"}) == "the user is waiting"
    assert urgent.why({"priority": 1, "spec": "s"}) == "priority 1"
    for t in ({"priority": 2}, {"priority": None}, {"priority": "x"}, {}, None,
              {"priority": 3, "spec": "this is not urgent: really"},   # not at the line's start
              {"priority": 3, "spec": "urgent:\nnext line"},           # no reason given
              {"priority": 3, "spec": "urgent:   \n"},
              {"priority": 3, "spec": "urgently: soon"}):
        assert not urgent.is_urgent(t), t
        assert urgent.why(t or {}) == "", t
    assert urgent.extra_slots({}) == 1 and urgent.extra_slots({"budget": {"urgent_extra_slots": 0}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": -2}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": "x"}}) == 1


def test_an_urgent_code_task_gets_its_review_at_priority_one_on_a_needs_review_hand_off_too(env):
    p = make(env)
    _with_origin(env, clone=False)
    hot = lambda tid, head: p.db.update_task(tid, priority=1)   # noqa: E731
    tid, _, _, reviews = _finish_code(env, p, "hot fix", {"a.py": 5}, before=hot,
                                      result={"status": "needs_review", "summary": "ok"})
    assert [(r["priority"], r["origin"]) for r in reviews] == [(1, "daemon")], "no review queued at once"
    tid, _, _, reviews = _finish_code(env, p, "hot done", {"b.py": 5}, before=hot)
    assert [r["priority"] for r in reviews] == [1]
    tid, _, _, reviews = _finish_code(env, p, "routine", {"c.py": 5}, result={"status": "needs_review", "summary": "ok"})
    assert reviews == [], "a routine needs_review hand-off still waits for the coordinator or the sweep"
    tid, _, _, reviews = _finish_code(env, p, "routine done", {"d.py": 5})
    assert [r["priority"] for r in reviews] == [2]


def test_the_review_sweeps_take_an_urgent_task_at_once_and_past_the_cap(env):
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("review.unreviewed_per_pass", 1)
    old = [_unreviewed(env, p, f"routine {i}")[0] for i in range(2)]
    hot = _unreviewed(env, p, "hot", spec="Fix it.\nurgent: the user is waiting")[0]
    d = Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == [hot, old[0]], "the cap counts routine tasks only"
    assert [r["priority"] for r in _daemon_reviews(p, hot)] == [1]
    assert [r["priority"] for r in _daemon_reviews(p, old[0])] == [2]
    # Left in review by a hand-off (follow-ups leave the review to the coordinator): no sweep_after_s wait.
    tid, _, _, reviews = _finish_code(env, p, "noted", {"n.py": 5}, result={
        "status": "needs_review", "summary": "ok", "followups": [{"title": "later", "spec": "more"}]})
    assert not reviews
    p.db.update_task(tid, priority=1)
    _ran(p, tid, time.time() - 60)
    _sweep(Daemon(p.base))
    assert [r["priority"] for r in _daemon_reviews(p, tid)] == [1], "an urgent task waited for sweep_after_s"


def test_an_urgent_task_in_review_ten_minutes_raises_one_review_stall(env):
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    hot = p.db.add_task("hot", "s\nurgent: the user is waiting", kind="code", origin="coordinator")
    routine = p.db.add_task("routine", "s", kind="code", origin="coordinator")
    for t in (hot, routine):
        p.db.update_task(t, status="review")
    since = p.db.review_since()[hot]
    d = Daemon(p.base)
    stalls = lambda: p.db.q("SELECT * FROM events WHERE kind='review_stall' ORDER BY id")   # noqa: E731
    d._raise_review_stalls(since + 599)
    assert stalls() == []
    d._raise_review_stalls(since + 601)
    (ev,) = stalls()
    assert ev["task"] == hot and "is urgent (the user is waiting)" in ev["text"] and "no review of it is open" in ev["text"]
    d._raise_review_stalls(since + 3600)
    assert len(stalls()) == 1, "one event per review stint"
    p.set_config("coordinator.review_stall_s", 0)
    p.db.x("DELETE FROM events WHERE kind='review_stall'")
    Daemon(p.base)._raise_review_stalls(since + 3600)
    assert stalls() == [], "review_stall_s 0 turns it off for urgent tasks too"


def test_the_device_runner_takes_an_urgent_job_before_older_routine_ones(tmp_path):
    from ttp import devq
    d = tmp_path / "rq"
    devq._dirs(d)
    devq._write(d / "config.json", json.dumps(devq.settings({})))
    jobs = [("j1", {}), ("j2", {"urgent": "yes"}), ("j3", None), ("j4", {"urgent": True}), ("j5", {"urgent": True})]
    for i, (job, extra) in enumerate(jobs):
        body = "{not json" if extra is None else json.dumps({"id": job, "cmd": "true", **extra})
        devq._write(d / "queue" / f"{i:020d}-{job}.json", body)
    r = devq.Runner(d)
    assert r.next_job() == "j4", "the oldest urgent job first"
    assert r.next_job() == "j4", "a running job is never passed over"
    (d / "running" / "j4.json").unlink()
    assert r.next_job() == "j5"
    (d / "running" / "j5.json").unlink()
    assert r.next_job() == "j1", "then arrival order; only a true `urgent` counts"


def test_status_and_the_web_payload_mark_an_urgent_task_with_its_wait(env):
    from ttp import cli, web
    p = make(env)
    hot = p.db.add_task("hot fix", "Fix it.\nurgent: the user is waiting", kind="code", origin="coordinator")
    p1 = p.db.add_task("p1 fix", "s", kind="code", origin="coordinator", priority=1)
    routine = p.db.add_task("routine fix", "s", kind="code", origin="coordinator")
    now = time.time()
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 12 * 60 - 5, hot))
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 75 * 60 - 5, p1))
    out = cli.status_text(p)
    assert f"#{hot} queued (urgent, waited 12m): hot fix — urgent: the user is waiting" in out, out
    assert f"#{p1} queued (urgent, waited 1h15m): p1 fix — urgent: priority 1" in out, out
    assert f"#{routine} queued (urgent" not in out
    rows = {t["id"]: t for t in web.state_payload(p, p.db)["tasks"]}
    assert rows[hot]["urgent"] == "the user is waiting" and 12 * 60 <= rows[hot]["waited_s"] < 13 * 60
    assert rows[routine]["urgent"] is None and rows[routine]["waited_s"] is None
    assert all("spec" not in t for t in rows.values()), "the payload never carries the spec"

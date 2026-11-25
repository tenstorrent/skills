# Lock yield tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file and dispatch_cases.py
# (_slot_setup, _slot_tasks), which sorts before it. Add new tests of Daemon.check_lock_yield and the
# urgent_started wake here.
from __future__ import annotations


def _hold_lock(p, d, monkeypatch, res, task, run, project_root=None, job_rc=None):
    """Take a `ttp lock` slot of `res` as task `task`'s run `run` would (its label names both), or as a
    person at a shell when task is None. Returns the open slot file: close it to release."""
    from ttp import hold, locks, shared
    for k in ("TTP_RUN_ID", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    extra = {}
    if task is not None:
        extra = {"run_id": str(run), "task_id": str(task), "project_root": str(project_root or p.base),
                 **({"job_rc": str(job_rc)} if job_rc else {})}
    who = hold.who(task) if task is not None else "someone at a shell"
    f = locks.try_take(d._slot_paths(res), shared.holder(p, res, who, d.cfg), "soak", extra=extra)
    assert f is not None
    return f


def _yield_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='lock_yield_asked' ORDER BY id")


def test_an_urgent_order_waiting_on_a_lock_asks_its_holder_to_yield_in_one_tick_then_stops_it_after_the_grace(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """A soak of the project's own holds the box lock while a user-ordered urgent task waits in
    `ttp lock` for it. One tick asks the holder to yield (a yield file and the reservation, which makes the
    holder's next batch wait), logged once; once the grace passes the holder is stopped, requeued with its work
    and attempts kept, and resumes later; the request clears once the urgent task no longer waits."""
    from ttp import eventhold, hold, locks, preempt, shared
    p, d, drain, settle = _slot_setup(env, monkeypatch, tmp_path, slots=3)
    f = None
    try:
        soak = p.db.add_task("soak on the box", "s", kind="work", tier="light", origin="coordinator", priority=3,
                             labels=["resource:box"])
        d.dispatch()
        soak_run = p.db.one("SELECT * FROM runs WHERE task=? AND status='running'", (soak,))
        f = _hold_lock(p, d, monkeypatch, "box", soak, soak_run["id"])
        hot = p.db.add_task("reset the box", "s\nurgent: the user ordered it now", kind="work", tier="light",
                            origin="user", labels=["resource:box"])
        d.dispatch()
        assert hot in _slot_tasks(p), "dispatched on the tick it is added"
        started = p.db.q("SELECT * FROM events WHERE kind='urgent_started' AND task=?", (hot,))
        assert len(started) == 1 and eventhold.hot(p.db, started, time.time()), "its start wakes the coordinator"
        hot_run = p.db.one("SELECT * FROM runs WHERE task=? AND status='running'", (hot,))
        locks.record_wait(pathlib.Path(hot_run["dir"]), "w1", time.time(), None, locks=["box"])
        d.cfg.setdefault("budget", {})["urgent_yield_grace_s"] = 600
        d.check_lock_yield()
        ask = json.loads(preempt.yield_path(d._slot_paths("box")[0].parent, "box").read_text())
        assert ask["for_task"] == hot and ask["holders"] == [f"#{soak}"]
        mine = shared.holder(p, "box", hold.who(hot), d.cfg)
        assert locks.reserved_by(d._reserve_path("box")) == mine, "the lock goes to the urgent task first"
        ev = _yield_events(p)
        assert len(ev) == 1 and f"#{soak}" in ev[0]["text"] and f"#{hot}" in ev[0]["text"] and "box" in ev[0]["text"]
        assert not (pathlib.Path(soak_run["dir"]) / "STOP").exists(), "no stop inside the grace"
        d.check_lock_yield()
        assert len(_yield_events(p)) == 1, "logged once per holder"
        before = p.db.task(soak)["attempts"]
        d.cfg["budget"]["urgent_yield_grace_s"] = 0
        d.check_lock_yield()
        assert (pathlib.Path(soak_run["dir"]) / "STOP").read_text() == preempt.STOP_TEXT, "stopped after the grace"
        stop = p.db.q("SELECT * FROM events WHERE kind='task_preempted' AND task=?", (soak,))
        assert len(stop) == 1 and "box" in stop[0]["text"] and f"#{hot}" in stop[0]["text"]
        deadline = time.monotonic() + 30
        while p.db.task(soak)["status"] == "running" and time.monotonic() < deadline:
            d.reap_runs()
            time.sleep(0.05)
        t = p.db.task(soak)
        assert t["status"] == "queued" and t["attempts"] == before, "requeued with no attempt spent"
        assert t["blocked_reason"] == "preempted for user-ordered urgent work; resumes"
        f.close()
        f = None
        locks.record_wait(pathlib.Path(hot_run["dir"]), "w1", time.time() - 1, time.time())
        d.check_lock_yield()
        assert not preempt.yield_path(d._slot_paths("box")[0].parent, "box").exists(), "the request clears"
        assert locks.reserved_by(d._reserve_path("box")) is None
        d.dispatch()
        assert soak in _slot_tasks(p), "the preempted task resumes"
    finally:
        if f is not None:
            f.close()
        drain(d)
        assert reap_tmp_processes(tmp_path) == [], "a stand-in run outlived the test"


def test_a_queued_urgent_task_takes_the_reservation_over_an_older_waiter_but_never_stops_a_person_or_another_project(
        env, monkeypatch, tmp_path):
    """A queued `exclusive:` urgent task held by the lock: the reservation moves to it from an older routine
    waiter. A person's hold (no task) and another project's run get the yield request only, never a stop."""
    from ttp import hold, locks, preempt, shared
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.cfg.setdefault("budget", {})["urgent_yield_grace_s"] = 0
    older = p.db.add_task("older routine waiter", "s", kind="work", tier="light", origin="coordinator",
                          labels=["exclusive:box"])
    locks.reserve(d._reserve_path("box"), shared.holder(p, "box", hold.who(older), d.cfg))
    f = _hold_lock(p, d, monkeypatch, "box", None, None)
    try:
        hot = p.db.add_task("reset the box", "s\nurgent: the user ordered it now", kind="work", tier="light",
                            origin="user", labels=["exclusive:box"])
        d.check_lock_yield()
        assert locks.reserved_by(d._reserve_path("box")) == shared.holder(p, "box", hold.who(hot), d.cfg)
        ev = _yield_events(p)
        assert len(ev) == 1 and "someone at a shell" in ev[0]["text"]
        assert not p.db.q("SELECT id FROM events WHERE kind='task_preempted'"), "a person's hold is never stopped"
    finally:
        f.close()
    other = p.db.add_task("same id, another project", "s", kind="work", tier="light", origin="coordinator")
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (other,))
    run = p.db.x("INSERT INTO runs(task, role, provider, status, started, dir) VALUES(?,?,?,?,?,?)",
                 (other, "worker", "claude", "running", time.time(), str(tmp_path / "other-run")))
    (tmp_path / "other-run").mkdir()
    f = _hold_lock(p, d, monkeypatch, "box", other, run, project_root=tmp_path / "elsewhere" / ".tt-project")
    try:
        d.check_lock_yield()
        d.check_lock_yield()
        assert not (tmp_path / "other-run" / "STOP").exists(), "another project's work is never stopped"
        assert len(_yield_events(p)) == 2, "it gets the request, logged once"
    finally:
        f.close()


def test_no_yield_on_a_paused_resource_or_for_routine_priority_one_work(env, monkeypatch, tmp_path):
    """A user's pause of the resource holds the urgent task as before: nobody is asked to yield. A routine
    priority-1 task with no `urgent:` line never preempts."""
    from ttp import coordinator as coord, locks
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.cfg.setdefault("budget", {})["urgent_yield_grace_s"] = 0
    f = _hold_lock(p, d, monkeypatch, "box", None, None)
    try:
        p.db.add_task("routine p1", "s", kind="work", tier="light", origin="coordinator", priority=1,
                      labels=["exclusive:box"])
        d.check_lock_yield()
        assert not _yield_events(p) and locks.reserved_by(d._reserve_path("box")) is None
        coord.pause_resource(p, "box", True, reason="maintenance", by="user")
        p.db.add_task("reset the box", "s\nurgent: the user ordered it now", kind="work", tier="light",
                      origin="user", labels=["exclusive:box"])
        d.check_lock_yield()
        assert not _yield_events(p) and locks.reserved_by(d._reserve_path("box")) is None
    finally:
        f.close()


def test_a_detached_soak_holding_the_lock_is_ended_after_the_grace_and_its_task_left_to_its_wait(
        env, monkeypatch, tmp_path):
    """The usual soak runs as `ttp detach --lock`: its run handed off `waiting`, so the lock's holder is the job,
    not a running run. After the grace the job is ended (jobstop.end) and logged; its task keeps its wait."""
    from ttp import jobstop
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.cfg.setdefault("budget", {})["urgent_yield_grace_s"] = 0
    soak = p.db.add_task("soak on the box", "s", kind="work", tier="light", origin="coordinator", priority=3)
    p.db.x("UPDATE tasks SET status='waiting' WHERE id=?", (soak,))
    run = p.db.x("INSERT INTO runs(task, role, provider, status, started, dir) VALUES(?,?,?,?,?,?)",
                 (soak, "worker", "claude", "done", time.time(), str(tmp_path / "soak-run")))
    rc = tmp_path / "soak-run" / "soak.rc"
    ended = []
    monkeypatch.setattr(jobstop, "end", lambda p_, rc_, why, task=None, reason="", fields=None: ended.append(
        (str(rc_), task, why)) or {"text": "stopped"})
    f = _hold_lock(p, d, monkeypatch, "box", soak, run, job_rc=rc)
    try:
        hot = p.db.add_task("reset the box", "s\nurgent: the user ordered it now", kind="work", tier="light",
                            origin="user", labels=["exclusive:box"])
        d.check_lock_yield()
        assert ended == [(str(rc), soak, f"yields box to user-ordered urgent task #{hot}")]
        stop = p.db.q("SELECT * FROM events WHERE kind='task_preempted' AND task=?", (soak,))
        assert len(stop) == 1 and "detached job soak" in stop[0]["text"] and f"#{hot}" in stop[0]["text"]
        assert p.db.task(soak)["status"] == "waiting", "its own wait wakes it"
    finally:
        f.close()

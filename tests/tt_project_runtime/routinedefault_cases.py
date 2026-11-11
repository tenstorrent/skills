# Routine default tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. Add new tests of the
# daemon's model-free routine defaults (routinedefault.py: own machine-change renewals, run caps, probe
# stall re-checks) here.
from __future__ import annotations


def _digest_once(p, since):
    from ttp import routinedefault as rd
    first = rd.digest_lines(p.db, since)
    p.db.set_kv("last_coordinator_turn", time.time() + 1)
    return first, rd.digest_lines(p.db, float(p.db.kv("last_coordinator_turn")))


def test_an_own_expiring_machine_change_with_an_open_task_is_renewed_twice_then_pages(env):
    """Task #8431: an own entry about to expire whose task is still open is renewed by its own span, at most
    RENEW_MAX times, recorded and listed once in the next digest; the third expiry is overdue (pages).
    A done task, another project's entry, an --auto-undo entry and the switch off are never renewed,
    and no undo ever runs."""
    from ttp import machine_ledger as ml
    from ttp import machines as mm
    from ttp import routinedefault as rd
    p = make(env)
    mm.add("box-a", tags="device")
    now = time.time()
    t0 = now - 7200
    open_t = p.db.add_task("soak", "s", kind="work", tier="light", origin="user")
    done_t = p.db.add_task("done one", "s", kind="work", tier="light", origin="user")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (done_t,))
    mark = p.base / "undo-ran"
    mine = ml.add_change("box-a", "raised fan floor", f"touch {mark}", p.name, expires=now + 60, task=open_t, now=t0)
    over = ml.add_change("box-a", "pinned clocks", "clk reset", p.name, expires=now + 60, task=done_t, now=t0)
    other = ml.add_change("box-a", "their tweak", "undo it", "other", expires=now + 60, task=open_t, now=t0)
    auto = ml.add_change("box-a", "auto one", "clk reset", p.name, expires=now + 60, task=open_t, now=t0,
                         auto_undo=True)
    ml.Probes(p, log=lambda m: None).tick(now)
    got = {c["id"]: c for c in ml.changes()}
    assert got[mine["id"]]["expires"] == pytest.approx(now + 60 + 7200 + 60)
    assert len(got[mine["id"]]["auto_renewed"]) == 1 and "still queued" in got[mine["id"]]["auto_renewed"][0]["why"]
    for c in (over, other, auto):
        assert "auto_renewed" not in got[c["id"]], c["what"]
    assert not mark.exists(), "a renewal never runs an undo"
    first, again = _digest_once(p, now - 1)
    assert "Routine defaults applied" in first[0] and f"machine-change:{mine['id']}" in first[1]
    assert again == [], "listed once"
    later = got[mine["id"]]["expires"]
    ml.Probes(p, log=lambda m: None).tick(later)
    third = [c for c in ml.changes() if c["id"] == mine["id"]][0]
    assert len(third["auto_renewed"]) == rd.RENEW_MAX
    ml.Probes(p, log=lambda m: None).tick(third["expires"] + 1)
    last = [c for c in ml.changes() if c["id"] == mine["id"]][0]
    assert len(last["auto_renewed"]) == rd.RENEW_MAX and ml.overdue(last, third["expires"] + 1).startswith("expired")
    assert not last.get("closed") and not mark.exists(), "past its renewals it pages; its undo waits for the coordinator"
    p.set_config("coordinator.routine_defaults", False)
    nxt = ml.add_change("box-a", "off switch", "fanctl reset", p.name, expires=now + 60, task=open_t, now=t0)
    ml.Probes(p, log=lambda m: None).tick(now)
    assert "auto_renewed" not in [c for c in ml.changes() if c["id"] == nxt["id"]][0]


def test_a_task_at_its_run_cap_is_held_quietly_once_and_wakes_when_it_comes_back(env):
    """Task #8431: a task's first run cap blocks it with no queued event (no wake), recorded and listed in
    the next digest; after a requeue, the same task at its cap again queues the event. Off: it queues."""
    from ttp import dispatchguard, runcap
    from ttp import routinedefault as rd
    p = make(env)
    tid = p.db.add_task("loops", "s", kind="work", tier="light", origin="user")
    _ended_runs(p, tid, 4, cost=0.1, handoff={"status": "waiting", "summary": "tried a flag"})
    assert "max_task_runs 4" in runcap.check(p, p.config(), p.db.task(tid))
    [ev] = p.db.q("SELECT * FROM events WHERE kind=?", (runcap.RUN_CAP_EVENT,))
    assert ev["status"] == "handled" and p.db.task(tid)["status"] == "blocked"
    [rec] = rd.entries(p.db)
    assert rec["kind"] == "run_cap" and rec["key"] == f"#{tid}" and "tried a flag" in rec["what"]
    assert "run_cap" in rd.audit_line(p.db, 0)
    time.sleep(0.01)
    dispatchguard.record_requeue(p.db, tid, "blocked", "coordinator")
    p.db.update_task(tid, status="queued")
    time.sleep(0.01)
    _ended_runs(p, tid, 4, cost=0.1, handoff={"status": "waiting"})
    assert runcap.check(p, p.config(), p.db.task(tid))
    evs = p.db.q("SELECT status FROM events WHERE kind=? ORDER BY id", (runcap.RUN_CAP_EVENT,))
    assert [e["status"] for e in evs] == ["handled", "queued"], "the same task back wakes the coordinator"
    q = p.db.add_task("other loop", "s", kind="work", tier="light", origin="user")
    p.set_config("coordinator.routine_defaults", False)
    _ended_runs(p, q, 4, cost=0.1, handoff={"status": "waiting"})
    assert runcap.check(p, p.config(), p.db.task(q))
    assert p.db.one("SELECT status FROM events WHERE task=?", (q,))["status"] == "queued", "off: it wakes"


def test_a_new_probe_stall_reruns_its_probe_and_wakes_only_if_it_still_says_not_yet(env):
    """Task #8431: a new probe stall re-runs its probe first: still running is judged later, a pass ends it
    with a record and no event, a not yet raises the event as before, and the same stall back wakes."""
    from ttp import progress
    from ttp import routinedefault as rd
    p = make(env)
    now = time.time()
    tid = p.db.add_task("waits", "s", kind="work", tier="light", origin="user")
    stall = {"kind": "stall", "key": f"probe:#{tid}", "task": tid, "what": f"task #{tid} waits: its probe said "
             "not yet 10 times", "since": now - 9000, "last": now, "n": 10, "reason": "not yet"}

    def check(rc):
        st = {"raised": {}, "history": {}}
        due = progress._recheck(p.db, st, [(stall, "new")], lambda t: "test -e x || exit 1",
                                lambda probe, key: rc, now)
        return due, st
    due, st = check(None)
    assert due == [] and st["raised"] == {}, "still running: judged on a later pass"
    due, st = check(1)
    assert len(due) == 1, "still not yet: it wakes"
    due, st = check(0)
    assert due == [] and [r["kind"] for r in rd.entries(p.db)] == ["stall"]
    due, st = check(0)
    assert len(due) == 1, "the same stall back wakes the coordinator"
    p.set_config("coordinator.routine_defaults", False)
    seen = []
    progress.check(p.db, p.config(), p.base, now, current_probe=lambda t: None,
                   recheck=lambda probe, key: seen.append(probe) or 0)
    assert seen == [], "off: no re-run"

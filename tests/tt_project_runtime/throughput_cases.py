# Throughput review (throughput.py, `ttp throughput`) and the speed-complaint trigger (slowcomplaint.py).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it.
from __future__ import annotations

from pathlib import Path


def _tp_task(p, title, created, prio=3, **kw):
    tid = p.db.add_task(title, "s", priority=prio, origin="user", **{k: v for k, v in kw.items() if k != "labels"})
    p.db.x("UPDATE tasks SET created=?, labels=? WHERE id=?", (created, json.dumps(kw.get("labels") or []), tid))
    return tid


def _tp_run(p, task, started, ended=None, role="worker"):
    return p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)",
                  (task, role, started, ended, "done" if ended else "running"))


def _tp_busy_fixture(p, now, slots=2):
    """`slots` runs filling every worker slot from now-3h on, a priority 1 task that waited 40 min behind
    them, two plain tasks queued 2 h ago and still waiting, and one task that started at once."""
    p.set_config("budget.max_parallel_workers", slots)
    for i in range(slots):
        _tp_run(p, _tp_task(p, f"long {i}", now - 3 * 3600), now - 3 * 3600, None)
    urgent = _tp_task(p, "urgent fix", now - 2 * 3600, prio=1)
    _tp_run(p, urgent, now - 2 * 3600 + 2400, now - 3600)
    waiting = [_tp_task(p, f"queued {i}", now - 2 * 3600) for i in range(2)]
    quick = _tp_task(p, "quick", now - 7 * 3600)
    _tp_run(p, quick, now - 7 * 3600 + 5, now - 6 * 3600)
    # Finished, as finish_run leaves them: a queued task with an ended run is a re-queue wait.
    p.db.x("UPDATE tasks SET status='done' WHERE id IN (?,?)", (urgent, quick))
    return urgent, waiting


def _tp_dispatch_hold(p, monkeypatch, task, at, label, kind="busy"):
    """The dispatcher's own hold record at `at`: running holders of `label` until Daemon._resource_hold
    names `task` held, then Daemon._log_resource_wait writes the dispatch_hold event measure() reads.
    The holders are removed after, so only the record stays."""
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    holders, rec = [], None
    while len(holders) < 4:
        holders.append(p.db.add_task(f"holder {len(holders)}", "s", origin="user"))
        p.db.x("UPDATE tasks SET status='running', labels=? WHERE id=?", (json.dumps([label]), holders[-1]))
        rec = d._resource_hold(p.db.task(task))
        if rec:
            break
    assert rec and rec["kind"] == kind, rec
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: at)
        d._log_resource_wait(p.db.task(task), rec)
    p.db.x(f"DELETE FROM tasks WHERE id IN ({','.join('?' * len(holders))})", holders)
    return rec


def _tp_lane(rep, name):
    return next(ln for ln in rep["lanes"] if ln["lane"] == name)


def test_throughput_measures_waits_per_lane_from_task_run_and_push_records(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    urgent, waiting = _tp_busy_fixture(p, now)
    # A landed:# chain, a device task, a review queued 40 min after its task's run ended, a push row
    # whose batch started 50 min after approval.
    _tp_task(p, "after #5", now - 5 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 5 * 3600}"])
    p.set_config("resources", {"box": 1})
    dev = _tp_task(p, "device run", now - 3600, labels=["exclusive:box"])
    _tp_dispatch_hold(p, monkeypatch, dev, now - 3500, "exclusive:box")
    code = _tp_task(p, "code change", now - 4 * 3600)
    _tp_run(p, code, now - 4 * 3600, now - 3 * 3600)
    rv = _tp_task(p, "Review #x", now - 3 * 3600 + 2400, labels=[f"auto_review:{code}"])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    _tp_run(p, rv, now - 3 * 3600 + 3000, now - 3 * 3600 + 4000, role="reviewer")
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1','m','br',?)", (now - 3600,))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, batch, created, updated) VALUES(?,?,?,?,?,?,?)",
           (code, "b", "h", "br", "b1", now - 3600 - 3000, now))
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    w = _tp_lane(rep, "worker_slots")
    assert w["limit_key"] == "budget.max_parallel_workers" and w["limit"] == 2
    assert w["waiting_now"] == 2 and w["p1_count"] == 1 and w["p1_p90_s"] == 2400 and w["slow"]
    assert w["at_limit"] == 3 and w["idle"] == 0, "every wait over threshold met all slots busy"
    assert _tp_lane(rep, "landed_chain")["count"] == 1 and _tp_lane(rep, "landed_chain")["p90_s"] >= 5 * 3600 - 1
    assert _tp_lane(rep, "resource:box")["waiting_now"] == 1 and dev
    rs, rt = _tp_lane(rep, "review_sweep"), _tp_lane(rep, "review_stall")
    assert (rs["count"], rs["p90_s"], rt["p90_s"]) == (1, 2400, 3000)
    assert _tp_lane(rep, "push_batch")["p90_s"] == 3000
    assert _tp_lane(rep, "checks_slots")["count"] == 0 and "dependency" not in {ln["lane"] for ln in rep["lanes"]}
    assert rep["lanes"][0]["slow"], "slow lanes come first"
    # A task that started before the window is outside it; the text names lanes and limits.
    assert _tp_lane(throughput.measure(p.db, p.config(), since_s=3600, now=now, gates={}), "worker_slots")["count"] == 2
    txt = throughput.text({**rep, "changes": [], "kept": []})
    assert "worker_slots SLOW" in txt and "limit budget.max_parallel_workers=2" in txt


def test_throughput_apply_raises_only_at_limit_with_headroom_bounded_by_load_and_undoes(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    _tp_busy_fixture(p, now)
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 64)
    # A spend gate below the slot count, a red gate or a busy CPU: nothing changes.
    prov = p.config()["core_provider"]
    for gates, cpu in (({prov: {"level": "yellow", "allow_new_work": True, "max_parallel": 1}}, True),
                       ({prov: {"level": "red", "allow_new_work": False, "max_parallel": 2}}, True),
                       ({}, False)):
        monkeypatch.setattr(throughput, "cpu_free", lambda cpu=cpu: (cpu, "" if cpu else "host load high"))
        rep = throughput.review(p, p.config(), do_apply=True, gates=gates, now=now)
        assert rep["changes"] == [] and p.config()["budget"]["max_parallel_workers"] == 2, rep["kept"]
        assert any(k["lane"] == "worker_slots" for k in rep["kept"])
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, trigger="t", now=now)
    (c,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    # Step from load: 3 waits at the limit, at most doubling: 2 -> 4.
    assert (c["old"], c["new"]) == (2, 4) and p.config()["budget"]["max_parallel_workers"] == 4
    assert c["undo"] == f"ttp throughput --undo {c['id']}" and "while all 2 slots ran" in c["why"]
    assert "budget.max_parallel_workers 2->4" in throughput.status_line(p.db)
    # Never above the host's share of cores.
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 6)
    assert throughput.load_size(3, 10, 3) == 3 and throughput.load_size(2, 10, 3) == 3
    assert throughput.load_size(4, 1, 100) == 5 and throughput.load_size(4, 50, 100) == 8
    # Undo: back to the old value, once; not over a later hand edit.
    assert throughput.undo(p, c["id"]).endswith("back to 2") and p.config()["budget"]["max_parallel_workers"] == 2
    assert "already undone" in throughput.undo(p, c["id"])
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    (c2,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    p.set_config("budget.max_parallel_workers", 9)
    assert "left as it is" in throughput.undo(p, c2["id"]) and p.config()["budget"]["max_parallel_workers"] == 9
    assert "no throughput change" in throughput.undo(p, 999)
    # A second review lists the earlier changes.
    assert {e["id"] for e in throughput.review(p, p.config(), gates={}, now=now)["earlier_changes"]} >= {c["id"], c2["id"]}


def test_throughput_worker_raise_moves_no_spend_limit(env, monkeypatch):
    from ttp import budget as bud, throughput
    p = make(env)
    now = time.time()
    _tp_busy_fixture(p, now)
    prov = p.config()["core_provider"]
    assert not p.config()["budget"].get("hourly_floor_usd"), "the floor is sized from the worker count"
    # Four failed runs at once in the last hour: the waste cap is sized per worker, up to the slot count.
    for i in range(4):
        t = _tp_task(p, f"failing {i}", now - 1800)
        p.db.x("INSERT INTO runs(task, role, provider, started, ended, status, cost_usd) VALUES(?,?,?,?,?,?,?)",
               (t, "worker", prov, now - 1800, now - 600, "failed", 1.0))
    before = bud.evaluate(p.db, p.config(), prov, [], now=now).numbers
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    monkeypatch.setattr(throughput.os, "cpu_count", lambda: 64)
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    (c,) = [c for c in rep["changes"] if c["key"] == "budget.max_parallel_workers"]
    assert c["new"] > c["old"] == 2 and p.config()["budget"]["max_parallel_workers"] == c["new"]
    after = bud.evaluate(p.db, p.config(), prov, [], now=now).numbers
    assert (after["hourly_ceiling"], after["waste_limit"]) == (before["hourly_ceiling"], before["waste_limit"])
    assert throughput.spend_workers(p.db, c["new"]) == 2
    # A hand edit after the raise is the user's own count; an undo ends the pin; old records stay pinned.
    assert throughput.spend_workers(p.db, 7) == 7
    p.db.set_kv(throughput.KV, [{**r, "ts": now - 30 * 86400} for r in p.db.kv(throughput.KV)])
    throughput.apply(p, p.config(), {"lanes": [], "thresholds_s": {"p1": 600, "other": 1800}}, now=now)
    assert throughput.spend_workers(p.db, c["new"]) == 2
    throughput.undo(p, c["id"])
    assert throughput.spend_workers(p.db, 2) == 2 and throughput.spend_workers(p.db, c["new"]) == c["new"]


def test_throughput_dependency_waits_are_their_own_lane_and_raise_no_workers(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("budget.max_parallel_workers", 1)
    _tp_run(p, _tp_task(p, "long", now - 3 * 3600), now - 3 * 3600, None)
    # A code task held by a deferral and a priority 1 review queued behind it for 2 h.
    code = _tp_task(p, "code", now - 2 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 2 * 3600}"])
    rv = _tp_task(p, "Review", now - 2 * 3600, prio=1, depends_on=[code])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    # A task whose dependency was done 1 h ago and that started a minute later waited a minute.
    dep = _tp_task(p, "dep", now - 4 * 3600)
    _tp_run(p, dep, now - 4 * 3600 + 5, now - 3 * 3600)
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 3600, dep))
    later = _tp_task(p, "later", now - 4 * 3600, prio=1, depends_on=[dep])
    _tp_run(p, later, now - 3600 + 60, now - 1800)
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    d = _tp_lane(rep, "dependency")
    assert (d["count"], d["waiting_now"], d["limit_key"]) == (1, 1, None) and d["p1_p90_s"] >= 2 * 3600 - 1
    w = _tp_lane(rep, "worker_slots")
    assert w["p1_count"] == 1 and w["p1_p90_s"] == 60 and w["waiting_now"] == 0 and not w["slow"]
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"] == "budget.max_parallel_workers"]


def test_throughput_measures_requeue_waits_by_reason_and_a_slow_checks_lane_gets_a_p1_followup(env, monkeypatch):
    from ttp import slowcomplaint, throughput
    p = make(env)
    now = time.time()
    checks = "waiting for ttp checks: waiting (pid 5, not running) for this project's other full checks to end"

    def requeued(title, gap_end, status, reason, wait_for=None, data=True, ended=now - 4 * 3600, kind="external"):
        # As the daemon records it: the run ends 'ok' (finish_run's process outcome), the waiting hand-off
        # is a task_waiting event and the task's status and blocked_reason.
        t = _tp_task(p, title, ended - 3600, prio=1)
        p.db.x("INSERT INTO runs(task, role, started, ended, status, note) VALUES(?,?,?,?,?,?)",
               (t, "worker", ended - 3600, ended, "ok", json.dumps({"spec_sha": "x", "pick": {}})))
        w = reason if wait_for is None else wait_for
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
               (ended + 1, f"task:{t}", "task_waiting", "low", f"#{t} {title}: {w}",
                json.dumps({"wait": kind, "why": "hand-off", "for": w} if data else {}), "handled", t))
        if gap_end:
            _tp_run(p, t, gap_end, gap_end + 600)
        p.db.x("UPDATE tasks SET status=?, blocked_reason=? WHERE id=?", (status, reason, t))
        return t

    requeued("ran again", now - 2 * 3600, "done", "", wait_for=checks)
    requeued("still queued", None, "queued", checks)
    requeued("own job", None, "waiting", "own ttp checks --detach running", kind="self")
    requeued("retry", None, "waiting", "CI on the branch")
    requeued("old done", None, "done", checks)
    # The title names checks, the wait does not: the "#<id> <title>: " prefix is not the reason.
    requeued("ttp checks lanes", now - 3 * 3600, "done", "", wait_for="CI on the branch", data=False)
    # A task that ran three days ago and waits again: a re-queue wait, not a first start for a slot.
    requeued("ran long ago", None, "queued", "", ended=now - 3 * 86400)
    # Read as its own checks, but the event Daemon.check_checks_wait raised while it waited for a slot (from
    # checklane.wait_events, at now - 3 h) shows the slot held it.
    ev = requeued("slot event", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    from ttp import checkcap, locks, testslots
    from ttp.daemon import Daemon
    ticket = locks.enqueue(checkcap.slot_dir(), testslots.suite_base(p.name, "0123456789ab"),
                           f"task #{ev} {p.name}: full checks (run 9, pid 1)", now - 4 * 3600 + 60, priority=1)
    real_time = time.time
    try:
        monkeypatch.setattr(time, "time", lambda: now - 3 * 3600)
        Daemon(p.base).check_checks_wait(every_s=0)
    finally:
        monkeypatch.setattr(time, "time", real_time)
        locks.dequeue(ticket)
    # Its episode row, and the incident's waking row (checklane.wait_wake) naming the same waiter.
    assert [r["task"] for r in p.db.q("SELECT task FROM events WHERE kind='checks_slot_wait' AND status='coalesced'")] \
        == [ev]
    assert {r["task"] for r in p.db.q("SELECT task FROM events WHERE kind='checks_slot_wait'")} == {ev}
    # Read as its own checks, but checkcap's waits log shows the slot held it.
    lg = requeued("slot log", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    (Path(p.db.path).parent / checkcap.WAITS_LOG).write_text(json.dumps(
        {"task": lg, "queued_at": now - 4 * 3600 + 60, "granted_at": now - 2 * 3600 - 60, "slot": "queue"}) + "\n")
    short = requeued("short slot", now - 2 * 3600, "done", "", wait_for="the detached ttp checks to pass")
    with open(Path(p.db.path).parent / checkcap.WAITS_LOG, "a") as fh:   # 10 of 120 min: its own job held it
        fh.write(json.dumps({"task": short, "queued_at": now - 3 * 3600, "granted_at": now - 3 * 3600 + 600}) + "\n")
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    ck = _tp_lane(rep, "checks_slots")
    assert (ck["count"], ck["waiting_now"], ck["p1_p90_s"]) == (4, 1, 4 * 3600) and ck["slow"]
    assert (_tp_lane(rep, "own_job")["count"], _tp_lane(rep, "retry_wait")["count"]) == (2, 3)
    assert _tp_lane(rep, "retry_wait")["waiting_now"] == 2
    assert "worker_slots" not in {ln["lane"] for ln in rep["lanes"] if ln["count"] and ln["lane"] != "dependency"} \
        or _tp_lane(rep, "worker_slots")["waiting_now"] == 0
    _, kept = throughput.plan(p.db, p.config(), rep)
    assert any(k["lane"] == "checks_slots" for k in kept)
    fid = slowcomplaint._followups(p.db, {**rep, "kept": kept}, [1])["opened"]
    t = p.db.one("SELECT priority, spec FROM tasks WHERE id=?", (fid,))
    assert t["priority"] == 1 and "lane checks_slots slow: 4 wait(s)" in t["spec"]


# Unequal waits so each limit wins once: the lane holds 3 h against the cap's 2 h, then the reverse.
@pytest.mark.parametrize("lane_h, cap_h, want, n", [(3, 2, "checks.slots", 2),
                                                (2, 3, "checks.test_project_max", 5)])
def test_throughput_checks_lane_names_the_limit_that_held_the_wait(env, monkeypatch, lane_h, cap_h, want, n):
    from ttp import checkcap, checklane, locks, testslots, throughput

    def writer_text(p, task, lock):
        """checklane.wait_events' text for `task` queued on the repository's lanes while #12 holds a
        lane: the repository's own lane (lane limit), or a wrapper lock's lane (lanes free: the cap)."""
        _lane_env(monkeypatch)
        monkeypatch.setenv("TTP_TASK", "12")
        hold = testslots.suite_hold(p.name, repo=_LANE_REPO, slots=1, lock=lock, what="pytest -q tests/a.py")
        monkeypatch.delenv("TTP_TASK")
        ticket = locks.enqueue(checkcap.slot_dir(), testslots.suite_base(p.name, _LANE_REPO),
                               f"task #{task} {p.name}: full checks (run 9, pid 1)", time.time() - 1000, priority=2)
        try:
            ev = [e for e in checklane.wait_events(p.name, {}) if e["task"] == task]
        finally:
            locks.dequeue(ticket)
            hold.close()
        assert len(ev) == 1 and f"#{task} has waited" in ev[0]["text"] and "#12" in ev[0]["text"], ev
        return ev[0]["text"]

    def requeued_wait(p, now, hours, lock):
        # A finished task re-queued from a checks wait: a task_waiting hand-off then the wait to now,
        # with the checks_slot_wait event the daemon writes (checklane.wait_events) inside it.
        t = _tp_task(p, "checks wait", now - (hours + 1) * 3600, prio=1)
        p.db.x("INSERT INTO runs(task, role, started, ended, status, note) VALUES(?,?,?,?,?,?)",
               (t, "worker", now - (hours + 1) * 3600, now - hours * 3600, "ok", "{}"))
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (now - hours * 3600, f"task:{t}", "task_waiting", "low",
                f"#{t} waits: the project's other full checks", "handled", t))
        text = writer_text(p, t, lock)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (now - (hours - 0.5) * 3600, "daemon", "checks_slot_wait", "low", text, "handled", t))
        p.db.x("UPDATE tasks SET status='queued', blocked_reason='waiting for the checks slot' WHERE id=?", (t,))
        return text

    p = make(env)
    now = time.time()
    p.set_config("checks.slots", 2)
    p.set_config("checks.test_project_max", 5)
    lane_text = requeued_wait(p, now, lane_h, "")
    cap_text = requeued_wait(p, now, cap_h, _WRAP_A)
    assert throughput.slot_limit(lane_text) == "checks.slots", lane_text
    assert throughput.slot_limit(cap_text) == "checks.test_project_max", cap_text
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    ck = _tp_lane(rep, "checks_slots")
    assert ck["count"] == 2
    # Both limits read through checklane from config; the lane names whichever held more of its wait time.
    assert ck["limits"] == {"checks.slots": 2, "checks.test_project_max": 5}
    assert (ck["limit_key"], ck["limit"]) == (want, n), ck
    assert "checks.slots" in ck["note"] and "checks.test_project_max" in ck["note"]
    assert "config_set" in ck["note"]


def test_throughput_keeps_push_window_and_sweep_unless_they_held_the_waits(env):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("delivery.push_batch_s", 3600)
    # A row that waited 50 min while another batch ran: the batch held it, not the window.
    code = _tp_task(p, "code change", now - 4 * 3600)
    _tp_run(p, code, now - 4 * 3600, now - 4 * 3600 + 600)
    p.db.x("INSERT INTO push_batches(id, marker, target, started, ended) VALUES('b0','m','br',?,?)",
           (now - 3 * 3600, now - 3600 - 60))
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1','m','br',?)", (now - 3600,))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, batch, created, updated) VALUES(?,?,?,?,?,?,?)",
           (code, "b", "h", "br", "b1", now - 3600 - 3000, now))
    # A review queued at finish, 40 min after the work ended (not by the sweep).
    rv = _tp_task(p, "Review", now - 4 * 3600 + 3000, labels=[f"auto_review:{code}"])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    _tp_run(p, rv, now - 4 * 3600 + 3100, now - 4 * 3600 + 3600, role="reviewer")
    p.set_config("review.sweep_after_s", 1200)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    assert _tp_lane(rep, "push_batch")["slow"] and _tp_lane(rep, "review_sweep")["slow"]
    levers = ("delivery.push_batch_s", "review.sweep_after_s")
    changes, kept = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"] in levers], changes
    why = {k["lane"]: k["why"] for k in kept}
    assert "1 of 1 row(s) waited behind a running batch" in why["push_batch"]
    assert "1 of 1 review(s) were queued at finish, not by the sweep" in why["review_sweep"]
    # The window alone held the row, and the sweep queued the review: both levers change.
    p.db.x("DELETE FROM push_batches WHERE id='b0'")
    p.db.x("INSERT INTO events(ts, source, kind, severity, text, status, task) VALUES(?,?,?,?,?,?,?)",
           (now - 4 * 3600 + 3000, "daemon", "review_swept", "normal", f"#{code} x; review #{rv} queued by the "
            "daemon.", "handled", code))
    changes, _ = throughput.plan(p.db, p.config(), throughput.measure(p.db, p.config(), now=now, gates={}))
    assert {(c["key"], c["new"]) for c in changes if c["key"] in levers} == {("delivery.push_batch_s", 1800), ("review.sweep_after_s", 600)}


def _tp_review(p, code_created, run_end, queued, started, labels=()):
    """A code task whose run ended at `run_end`, and its review queued at `queued`, first run at `started`."""
    code = _tp_task(p, "code change", code_created)
    _tp_run(p, code, code_created, run_end)
    rv = _tp_task(p, "Review", queued, labels=[f"auto_review:{code}", *labels])
    p.db.x("UPDATE tasks SET kind='review' WHERE id=?", (rv,))
    _tp_run(p, rv, started, started + 600, role="reviewer")
    return code, rv


def _tp_slot_wait(p, monkeypatch, task, queued_at, granted_at):
    """A checks slot wait of `task`, logged by checkcap's own writer as the task's checks log it."""
    from ttp import checkcap
    monkeypatch.setenv("TTP_TASK", str(task))
    checkcap.record_wait(Path(p.db.path).parent, p.name, 3, queued_at, granted_at)
    monkeypatch.delenv("TTP_TASK")


def test_throughput_review_sweep_is_not_lowered_for_waits_the_checks_held(env, monkeypatch):
    from ttp import checkcap, throughput
    p = make(env)
    now = time.time()
    state = Path(p.db.path).parent
    p.set_config("review.sweep_after_s", 1800)
    p.set_config("coordinator.review_stall_s", 14400)
    # Swept 1 h after its code task's run ended, then 2.9 h to its first run: the code task's own checks
    # and the precheck the daemon started on its head waited for a checks slot most of both spans.
    t0 = now - 6 * 3600
    code, rv = _tp_review(p, t0 - 1800, t0, t0 + 3600, t0 + 14000)
    p.db.x("INSERT INTO events(ts, source, kind, severity, text, status, task) VALUES(?,?,?,?,?,?,?)",
           (t0 + 3600, "daemon", "review_swept", "normal", f"#{code} x; review #{rv} queued by the daemon.", "handled", code))
    _tp_slot_wait(p, monkeypatch, code, t0 + 60, t0 + 3000)
    _tp_slot_wait(p, monkeypatch, code, t0 + 3700, t0 + 13500)
    # Queued at finish, held about an hour by its precheck (its checks.pid start to its checks.rc).
    t1 = now - 3 * 3600
    code2, rv2 = _tp_review(p, t1 - 1800, t1, t1 + 100, t1 + 5000)
    name = f"t{code2}-0123456789ab"
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"auto_review:{code2}", f"precheck:{name}"]), rv2))
    pc = state / "prechecks" / name
    pc.mkdir(parents=True)
    (pc / "checks.pid").write_text(json.dumps({"pid": 1, "ts": t1 + 100}))
    (pc / "checks.rc").write_text("0")
    os.utime(pc / "checks.rc", (t1 + 3600, t1 + 3600))
    levers = ("coordinator.review_stall_s", "review.sweep_after_s")
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    rs, rt = _tp_lane(rep, "review_sweep"), _tp_lane(rep, "review_stall")
    assert rs["slow"] and rt["slow"] and (rt["count"], rt["slot_held"], rt["free_count"]) == (2, 2, 0)
    assert (rs["slot_held"], rs["swept_count"]) == (1, 0), "the sweep queued it, but the checks held its wait"
    changes, kept = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"] in levers], changes
    why = {k["lane"]: k["why"] for k in kept}
    assert "0 review wait(s) not held by the checks (p90 0s), 2 held by their precheck" in why["review_stall"]
    assert "1 held by the checks" in why["review_sweep"]
    # Without the checks' records the sweep held its wait: it is lowered, the stall alarm still is not.
    (state / checkcap.WAITS_LOG).unlink()
    shutil.rmtree(pc)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    assert (_tp_lane(rep, "review_stall")["free_count"], _tp_lane(rep, "review_sweep")["swept_count"]) == (2, 1)
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert {(c["key"], c["old"], c["new"]) for c in changes if c["key"] in levers} == {("review.sweep_after_s", 1800, 900)}


def test_throughput_never_proposes_a_review_stall_change(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("coordinator.review_stall_s", 14400)
    # Live shape: one review held 4 h behind a checks slot, the free ones all under half the alarm.
    t0 = now - 10 * 3600
    code, _ = _tp_review(p, t0 - 600, t0, t0 + 60, t0 + 4 * 3600)
    _tp_slot_wait(p, monkeypatch, code, t0 + 30, t0 + 4 * 3600 - 100)
    for i, wait in enumerate((2352, 6012)):
        start = t0 + 3600 * (i + 1)
        _tp_review(p, start - 600, start, start + 60, start + wait)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    rt = _tp_lane(rep, "review_stall")
    assert rt["slow"] and (rt["count"], rt["slot_held"], rt["free_count"]) == (3, 1, 2) and rt["free_p90_s"] > 1800
    changes, kept = throughput.plan(p.db, p.config(), rep)
    assert "coordinator.review_stall_s" not in {c["key"] for c in changes}, changes
    why = {k["lane"]: k["why"] for k in kept}["review_stall"]
    assert "2 review wait(s) not held by the checks" in why and "1 held" in why and "information only" in why
    # Free waits between half the alarm and the alarm, none held: still no change.
    for i, wait in enumerate((9000, 11000)):
        start = now - 7 * 3600 + 1800 * i
        _tp_review(p, start - 600, start, start + 60, start + wait)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    rt = _tp_lane(rep, "review_stall")
    assert rt["free_count"] == 4 and 7200 < rt["free_p90_s"] < 14400
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert "coordinator.review_stall_s" not in {c["key"] for c in changes}, changes
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    assert throughput._cfg(p.config(), "coordinator.review_stall_s", 0) == 14400 and not [
        c for c in rep["changes"] if c["key"] == "coordinator.review_stall_s"]


def test_throughput_idle_slots_are_not_raised_and_resource_lanes_are_only_reported(env, monkeypatch):
    from ttp import throughput
    p = make(env)
    now = time.time()
    p.set_config("budget.max_parallel_workers", 6)
    p.set_config("resources", {"box": 1})
    _tp_task(p, "waits idle", now - 2 * 3600)       # nothing ran: slots sat idle
    _tp_dispatch_hold(p, monkeypatch, _tp_task(p, "box job", now - 2 * 3600, labels=["resource:box"]),
                      now - 2 * 3600 + 60, "resource:box")
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.review(p, p.config(), do_apply=True, gates={}, now=now)
    assert rep["changes"] == [] and _tp_lane(rep, "worker_slots")["idle"] == 1
    kept = {k["lane"]: k["why"] for k in rep["kept"]}
    assert "idle" in kept["worker_slots"] and "not measurable" in kept["resource:box"]
    assert p.config()["resources"] == {"box": 1}


def test_throughput_never_changes_spend_lock_pause_or_restriction_keys(env, monkeypatch):
    from ttp import throughput
    from ttp.coordinator import USER_SETTABLE, settable
    for key in ("budget.daily_usd", "budget.weekly_usd", "budget.global_daily_usd", "budget.reserve_pct",
                "delivery.push_queue", "review.auto", "locks.x", "resources.box", "coordinator.provider_failover_to",
                "checks.test_project_max", "spend_cap", "delivery.never_force", "restrictions"):
        assert not throughput.safe(key), key
    for key in throughput.LEVERS:
        assert throughput.safe(key) and key in USER_SETTABLE and settable(key), key
    p = make(env)
    # plan() offers only safe keys, whatever the lanes say.
    before = p.config()["budget"].get("daily_usd")
    _tp_busy_fixture(p, time.time())
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    rep = throughput.measure(p.db, p.config(), gates={})
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert changes and all(throughput.safe(c["key"]) for c in changes)
    assert p.config()["budget"].get("daily_usd") == before
    # The levers a coordinator may set are listed by `ttp config --chat-settable list`.
    r = _ttp_run(p, "config", "--chat-settable", "list")
    assert all(k in r.stdout.split() for k in throughput.LEVERS), r.stdout
    assert _ttp_run(p, "config", "--chat-settable", "review.unreviewed_per_pass", "-1").returncode == 1


def test_speed_complaint_detector_matches_complaints_not_plain_messages():
    from ttp import slowcomplaint as sc
    for text in ("This is so slow", "Why is everything stuck?", "Fix your throughput asap", "tasks sit in the queue",
                 "It's taking forever", "the review is still not done after a day", "Again nothing for hours",
                 "speed up the reviews", "I yelled at it for being slow"):
        assert sc.detect(text), text
    for text in ("Approved, go ahead", "Queue a task for the docs", "What is the status of the build?",
                 "Thanks, looks good", "Use the slowtests report", ""):
        assert not sc.detect(text), text


def test_speed_complaint_on_the_turn_start_runs_the_review_applies_and_queues_p1_followups(env, monkeypatch):
    from ttp import throughput
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    now = time.time()
    _tp_busy_fixture(p, now)
    _tp_task(p, "after #5", now - 5 * 3600, labels=["start_when:landed:#5", f"deferred_since:{now - 5 * 3600}"])
    monkeypatch.setattr(throughput, "cpu_free", lambda: (True, ""))
    d = Daemon(p.base)
    d.update_gates()
    calls = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, *a, **k: calls.append((prompt, k)) or 1)
    mid = p.db.post("in", "Everything is so slow, fix your throughput asap", chat="c1")
    later = time.time() + 600   # past the debounce
    monkeypatch.setattr(time, "time", lambda: later)
    d.maybe_coordinate()
    prompt = calls[-1][0]
    assert f"Speed complaint: measured throughput review (speed complaint #{mid}" in prompt
    assert "worker_slots SLOW" in prompt and "Changed #" in prompt and "budget.max_parallel_workers" in prompt
    # The slow landed_chain lane has no setting: reported, no follow-up task.
    assert "landed_chain SLOW" in prompt and "Not changed: landed_chain (no setting)" in prompt
    assert not p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")
    # A slow resource lane has a setting a person or task may change: one p1 follow-up.
    p.set_config("resources", {"boxA": 1})
    for i in range(2):
        _tp_dispatch_hold(p, monkeypatch, _tp_task(p, f"needs box {i}", now - 3 * 3600, prio=1,
                                                   labels=["resource:boxA"]), now - 3 * 3600 + 60, "resource:boxA")
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "Why is this so slow", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 300)
    d.maybe_coordinate()
    fu = p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")
    assert [t["priority"] for t in fu] == [1] and "resource:boxA" in fu[0]["title"] and "urgent:" in fu[0]["spec"]
    # A second complaint the same day runs it again, lists the earlier change, queues no second task.
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "Still waiting for hours again", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 900)
    d.maybe_coordinate()
    assert "Earlier (last 24 h) #" in calls[-1][0]
    assert len(p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")) == 1
    # Cancelled, its lane is not reopened on the same evidence; the model-free review still reports the lane.
    p.db.update_task(fu[0]["id"], status="cancelled")
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "still so slow", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 1200)
    d.maybe_coordinate()
    assert f"Not reopened (unchanged evidence since a closed follow-up): resource:boxA (#{fu[0]['id']})" in calls[-1][0]
    assert "- landed_chain SLOW: 1 wait(s)" in calls[-1][0] and "Not changed: landed_chain" in calls[-1][0]
    assert len(p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")) == 1
    # A plain message adds no section.
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    p.db.post("in", "Thanks, approved", chat="c1")
    monkeypatch.setattr(time, "time", lambda: later + 2400)
    d.maybe_coordinate()
    assert "Speed complaint" not in calls[-1][0]


def _tp_kept(*lanes):
    """A review's slow lanes as throughput.review returns them: (name, count, p90_s) each, none raised.
    Each names a setting: a lane with none opens no task (test_throughput_a_wait_inside_a_pause_is_a_hold_*)."""
    return {"lanes": [{"lane": n, "count": c, "median_s": p90 // 2, "p90_s": p90, "p1_p90_s": p90, "limit": None}
                      for n, c, p90 in lanes],
            "kept": [{"lane": n, "key": f"{n}.limit", "why": "not raised"} for n, _, _ in lanes]}


def test_speed_complaint_keeps_one_open_throughput_task_listing_every_slow_lane(env):
    from ttp import slowcomplaint
    p = make(env)
    now = time.time()
    fu = slowcomplaint._followups(p.db, _tp_kept(("resource:box", 3, 4000), ("retry_wait", 5, 9000)), [7], now)
    assert fu["listed"] is None and not fu["held"]
    (t,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")
    assert t["id"] == fu["opened"] and t["priority"] == 1 and t["spec"].startswith("urgent:")
    assert "resource:box, retry_wait" in t["title"]
    assert "lane resource:box slow: 3 wait(s)" in t["spec"] and "lane retry_wait slow: 5 wait(s)" in t["spec"]
    # While it is open, two more slow lanes (and the two it lists) add no task: the new ones join its spec.
    run_dir = Path(p.base) / "state" / "runs" / "77"
    run_dir.mkdir(parents=True)
    p.db.update_task(t["id"], status="running")
    p.db.x("INSERT INTO runs(task, role, started, status, dir) VALUES(?,?,?,?,?)",
           (t["id"], "worker", now, "running", str(run_dir)))
    rep = _tp_kept(("resource:box", 3, 4000), ("retry_wait", 5, 9000), ("own_job", 2, 5000), ("push_batch", 4, 3000))
    fu = slowcomplaint._followups(p.db, rep, [8], now + 60)
    assert fu["opened"] is None and fu["listed"] == (t["id"], ["own_job", "push_batch"])
    (t2,) = p.db.q("SELECT * FROM tasks WHERE title LIKE 'throughput: %'")
    assert t2["spec"].count("lane own_job slow:") == 1 and t2["spec"].count("lane resource:box slow:") == 1
    assert "lane push_batch slow: 4 wait(s)" in (run_dir / "steer.md").read_text()
    # A third complaint with the same lanes changes nothing.
    fu = slowcomplaint._followups(p.db, rep, [9], now + 120)
    assert fu == {"opened": None, "listed": None, "held": []}
    assert p.db.one("SELECT spec FROM tasks WHERE id=?", (t["id"],))["spec"] == t2["spec"]


def test_speed_complaint_does_not_reopen_a_closed_lane_on_unchanged_evidence(env):
    from ttp import slowcomplaint
    p = make(env)
    now = time.time()
    rep = _tp_kept(("checks_slots", 4, 6000), ("deferral", 2, 3000))
    tid = slowcomplaint._followups(p.db, rep, [1], now)["opened"]
    p.db.update_task(tid, status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now, tid))
    # Unchanged (or a little worse) evidence inside the cooldown: held, no task.
    for same in (rep, _tp_kept(("checks_slots", 5, 7000), ("deferral", 2, 3000))):
        fu = slowcomplaint._followups(p.db, same, [2], now + 600)
        assert fu["opened"] is None and fu["held"] == [("checks_slots", tid), ("deferral", tid)]
    assert len(p.db.q("SELECT id FROM tasks WHERE title LIKE 'throughput: %'")) == 1
    # A done task holds its lanes the same way: its window still holds the old waits.
    p.db.update_task(tid, status="done")
    assert slowcomplaint._followups(p.db, rep, [3], now + 700)["opened"] is None
    # Waits grown past FACTOR reopen that lane alone; so does a grown p90.
    grown = _tp_kept(("checks_slots", 4 * 2, 6000), ("deferral", 2, 3000))
    fu = slowcomplaint._followups(p.db, grown, [4], now + 900)
    assert fu["held"] == [("deferral", tid)] and fu["opened"]
    new = p.db.one("SELECT title, spec FROM tasks WHERE id=?", (fu["opened"],))
    assert "checks_slots" in new["title"] and "deferral" not in new["spec"]
    p.db.update_task(fu["opened"], status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now + 900, fu["opened"]))
    second = fu["opened"]
    fu = slowcomplaint._followups(p.db, _tp_kept(("checks_slots", 8, 6000), ("deferral", 2, 3000 * 2)), [5], now + 960)
    assert fu["held"] == [("checks_slots", second)]
    assert "deferral" in p.db.one("SELECT title FROM tasks WHERE id=?", (fu["opened"],))["title"]
    # After the cooldown, unchanged evidence reopens it too.
    p.db.update_task(fu["opened"], status="cancelled")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now + 960, fu["opened"]))
    later = now + 960 + slowcomplaint.COOLDOWN_S + 1
    assert slowcomplaint._followups(p.db, _tp_kept(("deferral", 2, 6000)), [6], later)["opened"]


def test_ttp_throughput_cli_prints_text_and_json(env):
    p = make(env)
    _tp_busy_fixture(p, time.time())
    r = _ttp_run(p, "throughput")
    assert "Throughput over the last 24h00m" in r.stdout and "worker_slots" in r.stdout
    data = json.loads(_ttp_run(p, "throughput", "--json", "--since", "6h").stdout)
    assert data["since_s"] == 6 * 3600 and {"lanes", "thresholds_s", "changes", "kept", "earlier_changes"} <= set(data)
    assert {"lane", "limit_key", "limit", "count", "median_s", "p90_s", "p1_p90_s", "at_limit", "idle",
            "waiting_now", "slow"} <= set(data["lanes"][0])
    assert _ttp_run(p, "throughput", "--since", "soon").returncode != 0


def test_throughput_a_wait_inside_a_pause_is_a_hold_with_no_limit_and_opens_no_task(env, monkeypatch):
    """A resource's pause (still on, or ended: coordinator._wake_pause_waiters records it) holds the
    tasks labelled with it on purpose: the hold lane, no limit, no follow-up; an unpaused one stays
    its resource's lane."""
    from ttp import coordinator, slowcomplaint, throughput
    p = make(env)
    now = time.time()
    p.set_config("resources", {"boxA": 1, "boxB": 1, "boxC": 1})
    real_time = time.time
    try:
        monkeypatch.setattr(time, "time", lambda: now - 6 * 3600)
        coordinator.pause_resource(p, "boxA", True, "maintenance", by="user")
        coordinator.pause_resource(p, "boxB", True, "firmware update", by="coordinator")
        monkeypatch.setattr(time, "time", lambda: now - 3600)
        coordinator.pause_resource(p, "boxB", False, by="coordinator")
    finally:
        monkeypatch.setattr(time, "time", real_time)
    (ev,) = p.db.q("SELECT * FROM events WHERE kind='resource_resumed'")
    assert json.loads(ev["data"])["resource"] == "boxB" and abs(json.loads(ev["data"])["since"] - (now - 6 * 3600)) < 1
    _tp_task(p, "on paused box", now - 5 * 3600, prio=1, labels=["resource:boxA"])
    b = _tp_task(p, "on resumed box", now - 5 * 3600 + 120, prio=1, labels=["resource:boxB"])
    _tp_run(p, b, now - 3600 + 60, now - 1800)
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (b,))
    _tp_dispatch_hold(p, monkeypatch, _tp_task(p, "on busy box", now - 4 * 3600, prio=1, labels=["resource:boxC"]),
                      now - 4 * 3600 + 60, "resource:boxC")
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    hold = _tp_lane(rep, "hold")
    assert (hold["count"], hold["limit"], hold["limit_key"]) == (2, None, None) and hold["slow"]
    assert not any(ln["lane"] in ("resource:boxA", "resource:boxB") and ln["count"] for ln in rep["lanes"])
    assert _tp_lane(rep, "resource:boxC")["count"] == 1
    _, kept = throughput.plan(p.db, p.config(), rep)
    assert {k["lane"] for k in kept} >= {"hold", "resource:boxC"}
    made = slowcomplaint._followups(p.db, {**rep, "kept": kept}, [1])
    opened = p.db.task(made["opened"])
    assert "resource:boxC" in opened["title"] and "lane hold " not in opened["spec"] and made["listed"] is None
    assert "hold SLOW" in throughput.text({**rep, "kept": kept})


def test_throughput_a_self_wait_or_its_own_job_is_own_job_not_its_resource(env, tmp_path, monkeypatch):
    """Re-queue waits handed off through the daemon (task_waiting data): wait=self, live work whose key
    names the task's own detached job, or a retry_when on its own results/ marker or .rc file, count as
    own_job. A lock wait counts in its resource's lane only when the dispatcher recorded a hold naming
    that resource for the task (Daemon._log_resource_wait); without one it is retry_wait."""
    from ttp import throughput
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    p.set_config("resources", {"boxA": 1})
    d, now = Daemon(p.base), time.time()
    job = tmp_path / "jobs" / "bench"
    job.parent.mkdir()
    hand = {"self": {"wait_kind": "self", "waiting_for": "boxA bench results", "retry_when": "test -e out/done"},
            "job": {"wait_kind": "external", "waiting_for": "boxA bench job",
                    "retry_when": f"ttp detach --check {job} && ttp lock --probe boxA"},
            "marker": {"waiting_for": "boxA sweep", "retry_when": "test -e results/sweep.done"},
            "rc": {"waiting_for": "boxA build", "retry_when": "test -s out/build.rc"},
            "lock": {"waiting_for": "boxA busy", "retry_when": "ttp lock --probe boxA"},
            "unheld": {"waiting_for": "boxA busy", "retry_when": "ttp lock --probe boxA"}}
    ids = {}
    for name, res in hand.items():
        tid = _tp_task(p, f"box work {name}", now - 3 * 3600, labels=["resource:boxA"])
        _tp_run(p, tid, now - 3 * 3600, now - 2 * 3600 - (300 if name == "lock" else 0))
        p.db.update_task(tid, status="running")
        run_dir = tmp_path / f"run-{tid}"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "waits",
                                                         "retry_after_s": 1800, **res}))
        d._finish_worker({"task": tid, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
        assert p.db.task(tid)["status"] == "queued"
        ids[name] = tid
    data = {r["task"]: json.loads(r["data"]) for r in p.db.q("SELECT task, data FROM events WHERE kind='task_waiting'")}
    assert data[ids["self"]]["wait"] == "self" and data[ids["job"]]["live"]["key"].startswith("job:")
    assert data[ids["lock"]]["wait"] == "external"
    # Only the "lock" wait has the dispatcher's hold record; "unheld" names the same box but has none.
    _tp_dispatch_hold(p, monkeypatch, ids["lock"], time.time(), "resource:boxA")
    rep = throughput.measure(p.db, p.config(), now=time.time() + 1, gates={})
    assert _tp_lane(rep, "own_job")["count"] == 4
    box = _tp_lane(rep, "resource:boxA")
    assert box["count"] == box["waiting_now"] == 1 and box["p90_s"] >= 2 * 3600 + 300
    assert _tp_lane(rep, "retry_wait")["count"] == 1


def test_throughput_review_sweep_of_a_run_that_ended_long_before_its_hand_off(env, tmp_path):
    """A code task whose run ended hours before its hand-off (a waiting run whose own checks then passed)
    is swept from the hand-off the daemon records, not from the run's end."""
    from ttp import throughput
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    p.set_config("review.sweep_after_s", 1200)
    d, now = Daemon(p.base), time.time()
    code, path, _ = _code_task(p, "code change", status="running")
    _commit_file(path, "change")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 6 * 3600, code))
    _tp_run(p, code, now - 6 * 3600, now - 5 * 3600)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "change made, checks pass"}))
    d._finish_worker({"task": code, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
    (rv,) = [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE kind='review'")]
    assert p.db.q("SELECT 1 FROM events WHERE task=? AND kind IN ('task_review','task_done')", (code,))
    _tp_run(p, rv, time.time() + 60, time.time() + 600, role="reviewer")
    rep = throughput.measure(p.db, p.config(), now=time.time() + 700, gates={})
    sw = _tp_lane(rep, "review_sweep")
    assert sw["count"] == 1 and sw["p90_s"] < 60 and not sw["slow"]


def _tp_checks_requeue(p, d, monkeypatch, tmp_path, title, w0, wait_s):
    """A priority 1 task whose run ended at w0 and handed off a wait for the project's other checks,
    recorded by the daemon's own Daemon._finish_worker at w0; it ran again wait_s later."""
    from ttp.providers.base import RunUsage
    t = _tp_task(p, title, w0 - 3600, prio=1)
    _tp_run(p, t, w0 - 3600, w0)
    p.db.update_task(t, status="running")
    run_dir = tmp_path / f"run-{t}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({
        "status": "waiting", "summary": "checks wait", "retry_after_s": 600, "retry_when": "test -e out/checks.done",
        "waiting_for": "ttp checks: waiting for this project's other full checks to end"}))
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: w0)
        d._finish_worker({"task": t, "note": "{}"}, RunUsage(cost_usd=0.1), "ok", run_dir)
    _tp_run(p, t, w0 + wait_s, w0 + wait_s + 600)
    p.db.update_task(t, status="done")
    return t


def _tp_checks_run(p, monkeypatch, start, seconds):
    """One finished checks run, recorded by checkwall's own writer when it ends."""
    from ttp import checkwall
    with monkeypatch.context() as m:
        m.setattr(time, "time", lambda: start + seconds)
        checkwall.record_run(Path(p.db.path).parent, seconds, True)


def test_throughput_checks_at_limit_counts_only_the_wait_before_the_grant(env, monkeypatch, tmp_path):
    """#4399's repro: cap 3, a p1 task waits 999 s for the checks, its own checks are granted at once at
    w0+5 and run 900 s beside two runs started at w0. Its waits-log record has no time before the grant,
    so the wait is not at the limit and nothing is raised. A task queued 700 s while three runs ran is."""
    from ttp import throughput
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("checks.test_project_max", 3)
    d, now = Daemon(p.base), time.time()
    w0 = now - 5 * 3600
    t = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "checks at once", w0, 999)
    for _ in range(2):
        _tp_checks_run(p, monkeypatch, w0, 1000)
    _tp_slot_wait(p, monkeypatch, t, w0 + 5, w0 + 5)
    _tp_checks_run(p, monkeypatch, w0 + 5, 900)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    ck = _tp_lane(rep, "checks_slots")
    assert (ck["limit"], ck["count"], ck["at_limit"], ck["idle"]) == (3, 1, 0, 1), ck
    changes, _ = throughput.plan(p.db, p.config(), rep)
    assert not [c for c in changes if c["key"].startswith("checks.")], changes
    # A wait with no grant record at all is not at the limit either.
    _tp_checks_requeue(p, d, monkeypatch, tmp_path, "no grant record", w0 + 60, 999)
    ck = _tp_lane(throughput.measure(p.db, p.config(), now=now, gates={}), "checks_slots")
    assert (ck["count"], ck["at_limit"]) == (2, 0), ck
    # Queued 700 s while the project's three checks ran at the cap: at the limit.
    w1 = now - 3 * 3600
    q = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "queued at the cap", w1, 999)
    for _ in range(3):
        _tp_checks_run(p, monkeypatch, w1 - 60, 1200)
    _tp_slot_wait(p, monkeypatch, q, w1, w1 + 700)
    ck = _tp_lane(throughput.measure(p.db, p.config(), now=now, gates={}), "checks_slots")
    assert (ck["count"], ck["at_limit"], ck["idle"]) == (3, 1, 2), ck
    changes, _ = throughput.plan(p.db, p.config(), throughput.measure(p.db, p.config(), now=now, gates={}))
    assert not [c for c in changes if c["key"].startswith("checks.")], changes


def test_slow_information_only_lane_opens_no_followup_on_a_speed_complaint(env, monkeypatch):
    """review_stall is information only (plan() marks its kept entry `info`): a slow one beside a speed
    complaint opens no throughput task, and the report still lists the lane."""
    from ttp import slowcomplaint, throughput
    p = make(env)
    now = time.time()
    p.set_config("coordinator.review_stall_s", 14400)
    t0 = now - 10 * 3600
    for i, wait in enumerate((6000, 9000, 11000)):
        start = t0 + 3600 * i
        _tp_review(p, start - 600, start, start + 60, start + wait)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    assert _tp_lane(rep, "review_stall")["slow"]
    _, kept = throughput.plan(p.db, p.config(), rep)
    (rs,) = [k for k in kept if k["lane"] == "review_stall"]
    assert rs.get("info") and rs.get("key"), rs
    made = slowcomplaint._followups(p.db, {**rep, "kept": kept}, [1])
    # The fixture's own worker_slots waits may open theirs; none lists review_stall.
    rows = p.db.q("SELECT id, title, spec FROM tasks WHERE title LIKE 'throughput: %'")
    assert [r["id"] for r in rows] == ([made["opened"]] if made["opened"] else []) and made["listed"] is None
    assert not [r for r in rows if "review_stall" in r["title"] or "lane review_stall " in r["spec"]], rows
    assert "review_stall SLOW" in throughput.text({**rep, "kept": kept})


def test_throughput_checks_wait_is_judged_at_the_cap_then_and_a_one_at_a_time_queue_is_serialized(
        env, monkeypatch, tmp_path):
    """#4461: the cap is 4 today, but a wait queued while 3 checks ran under the then-cap of 3 (the most
    that ran while the queue stayed non-empty) is at the limit, not idle. A wait behind runs that went one
    at a time back to back, each start and the grant within SERIAL_S of a run's end, is serialized; one
    behind fewer runs than ran at once earlier in the same busy period is idle; a peak held for less than
    SUSTAIN_S (an urgent run past the cap) does not raise the cap then."""
    from ttp import throughput
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("checks.test_project_max", 4)
    d, now = Daemon(p.base), time.time()
    w1 = now - 6 * 3600   # three runs at the then-cap of 3
    held = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "at the old cap", w1, 999)
    for s0 in (w1 - 60, w1 - 50, w1 - 40):
        _tp_checks_run(p, monkeypatch, s0, 1200)
    _tp_slot_wait(p, monkeypatch, held, w1, w1 + 700)
    w2 = now - 3 * 3600   # one run at a time: each starts 3 s after the last one ended, the grant 4 s after
    serial = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "one at a time", w2, 1300)
    _tp_checks_run(p, monkeypatch, w2 - 100, 400)
    _tp_checks_run(p, monkeypatch, w2 + 303, 500)
    _tp_slot_wait(p, monkeypatch, serial, w2, w2 + 807)
    rep = throughput.measure(p.db, p.config(), now=now, gates={})
    ck = _tp_lane(rep, "checks_slots")
    assert (ck["count"], ck["at_limit"], ck["serialized"], ck["idle"]) == (2, 1, 1, 0), ck
    assert "serialized 1" in throughput.text({**rep, "kept": [], "changes": []})
    _, kept = throughput.plan(p.db, p.config(), rep)
    assert any("1 wait(s) serialized" in k["why"] for k in kept if k["lane"] == "checks_slots"), kept
    # Two runs at once while another waiter queued, then one run while this one still waits: idle.
    w3 = now - 2 * 3600
    other = _tp_task(p, "earlier waiter", w3 - 3600)
    _tp_slot_wait(p, monkeypatch, other, w3 - 700, w3 + 50)
    idle = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "fewer than ran", w3, 999)
    _tp_checks_run(p, monkeypatch, w3 - 800, 820)
    _tp_checks_run(p, monkeypatch, w3 - 750, 1500)
    _tp_slot_wait(p, monkeypatch, idle, w3 + 40, w3 + 600)
    # Two runs at once for long, a third (an urgent run past the cap) for less than SUSTAIN_S: the cap
    # then was 2, so a wait behind two runs is at the limit.
    w4 = now - 3600
    other = _tp_task(p, "earlier waiter at the peak", w4 - 3600)
    _tp_slot_wait(p, monkeypatch, other, w4 - 800, w4 + 10)
    peak = _tp_checks_requeue(p, d, monkeypatch, tmp_path, "behind a short peak", w4, 999)
    _tp_checks_run(p, monkeypatch, w4 - 900, 1500)
    _tp_checks_run(p, monkeypatch, w4 - 850, 1500)
    _tp_checks_run(p, monkeypatch, w4 - 300, 200)
    _tp_slot_wait(p, monkeypatch, peak, w4, w4 + 500)
    ck = _tp_lane(throughput.measure(p.db, p.config(), now=now, gates={}), "checks_slots")
    assert (ck["count"], ck["at_limit"], ck["serialized"], ck["idle"]) == (4, 2, 1, 1), ck

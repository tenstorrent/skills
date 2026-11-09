# Overseer tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add new ttp.overseer and
# Daemon.check_overseer tests here.
from __future__ import annotations


def _stalled_loop(env, now):
    """A project whose loop delivers nothing while its heartbeat stays fresh: an urgent task ready with
    every slot free, a worker run and a coordinator run finished (exit.json) but never reaped, an event
    never delivered, and slow-step reports."""
    p = make(env)
    old = now - 900
    tid = p.db.add_task("urgent fix", "urgent: the user is waiting", kind="code")
    p.db.x("UPDATE tasks SET updated=?, created=? WHERE id=?", (old, old, tid))
    for role in ("worker", "coordinator"):
        rid = p.db.x("INSERT INTO runs(task, role, started, status, dir) VALUES(?,?,?,?,?)",
                     (tid if role == "worker" else None, role, old, "running", ""))
        d = p.runs / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "exit.json").write_text("{}")
        os.utime(d / "exit.json", (old, old))
    # Only the queued event is due to the coordinator; a `new` (the schema default) or handled one is not.
    for status in ("queued", "new", "new", "handled"):
        p.db.x("INSERT INTO events(ts, source, kind, text, status) VALUES(?,?,?,?,?)", (old, "daemon", "x", "e", status))
    for _ in range(3):
        p.db.x("INSERT INTO events(ts, source, kind, text, status) VALUES(?,?,?,?,?)",
               (now - 60, "daemon", "tick_step_slow", "slow", "handled"))
    (p.state / "heartbeat").write_text(json.dumps({"pid": 1}))   # fresh: the old health read "ok"
    return p


def test_the_overseer_reads_results_not_the_heartbeat_and_says_them_in_one_line(env):
    from ttp import overseer
    now = time.time()
    p = _stalled_loop(env, now)
    h = overseer.measure(p, now)
    assert h["urgent_ready"] == 1 and h["urgent_wait_s"] >= 899 and h["free_slots"] > 0
    assert (h["unreaped"], h["unapplied"], h["undelivered"], h["overruns_h"]) == (2, 1, 1, 3)
    assert h["heartbeat_s"] < 60 and h["last_dispatch"] == pytest.approx(now - 900)
    assert len(h["failing"]) == 3, h["failing"]   # urgent wait, unreaped runs, unapplied answer; not the heartbeat
    text = overseer.line(h, now)
    for want in ("last dispatch 15 min ago", "urgent ready 1 (waiting 15 min with a free slot)", "unreaped 2",
                 "unapplied answers 1", "undelivered events 1", "step overruns 3/h", "FAILING: "):
        assert want in text, text
    # A quiet, healthy project reads as such; no reading yet says so.
    p.db.x("UPDATE runs SET status='done'")
    p.db.x("UPDATE tasks SET status='done'")
    assert overseer.measure(p, now)["failing"] == []
    assert overseer.line(None) == "loop health: no overseer reading yet"


def test_a_dispatch_hold_on_urgent_work_is_no_results_failure(env, monkeypatch):
    """A paused project, a closed budget gate and a resource hold the daemon recorded each keep urgent work
    out of dispatch on purpose: the line names the hold, with no FAILING, no stacks and no incident."""
    from ttp import overseer
    now = time.time()
    dumps = []
    monkeypatch.setattr(overseer, "capture_stacks", lambda p, now, why: dumps.append(why))
    p = _stalled_loop(env, now)
    p.db.x("UPDATE runs SET status='done'")   # only the urgent wait is left to judge
    tid, core = p.db.one("SELECT id FROM tasks")["id"], p.config()["core_provider"]
    hold_ev = ("INSERT INTO events(ts, source, kind, severity, text, status, task) VALUES(?,?,?,?,?,?,?)")
    holds = (
        (lambda: p.db.set_kv("paused", True), lambda: p.db.set_kv("paused", False), "the project is paused"),
        (lambda: p.db.set_kv("gates", {core: {"level": "red", "allow_new_work": False, "max_parallel": 0}}),
         lambda: p.db.set_kv("gates", {}), "the budget gate allows no new work"),
        (lambda: p.db.x(hold_ev, (now - 600, "daemon", "dispatch_hold", "low", "lock blx: held by #9", "handled", tid)),
         lambda: p.db.x("DELETE FROM events WHERE kind='dispatch_hold'"), "lock blx: held by #9"))
    for on, off, words in holds:
        on()
        h = overseer.check(p, now)
        text = overseer.line(h, now)
        assert h["failing"] == [] and (h["urgent_ready"], h["urgent_held"]) == (1, 1), (words, h)
        assert f"urgent ready 1 (1 held: {words})" in text and "FAILING" not in text, text
        off()
    assert dumps == [] and not (p.state / overseer.INCIDENTS_FILE).exists()
    # A hold recorded before the task became ready again holds nothing now: the stalled loop still fails.
    p.db.x(hold_ev, (now - 2000, "daemon", "dispatch_hold", "low", "old hold", "handled", tid))
    assert overseer.measure(p, now)["failing"] == ["urgent work ready with a free slot for 15 min"]


def test_an_urgent_review_held_on_its_running_prechecks_is_no_results_failure(env, monkeypatch):
    """The daemon keeps a review out of dispatch while the checks it started on its head run (no rc,
    pid alive), for up to PRECHECK_HOLD_S: the overseer reads the same hold, so no FAILING and no
    incident. Once the checks ended or the hold ran out, the waiting review fails as before."""
    from ttp import overseer
    from ttp.cli import CHECKS_PID, CHECKS_RC
    from ttp.daemon import PRECHECK_HOLD_S, PRECHECKS_DIR, Daemon
    from ttp.runner import proc_start
    now = time.time()
    dumps = []
    monkeypatch.setattr(overseer, "capture_stacks", lambda p, now, why: dumps.append(why))
    p = _stalled_loop(env, now)
    p.db.x("UPDATE runs SET status='done'")   # only the urgent wait is left to judge
    tid = p.db.one("SELECT id FROM tasks")["id"]
    name = f"t{tid - 1}-0123456789ab"
    p.db.x("UPDATE tasks SET kind='review', labels=? WHERE id=?", (json.dumps([f"precheck:{name}"]), tid))
    out = p.state / PRECHECKS_DIR / name
    out.mkdir(parents=True)

    def started(ts):
        (out / CHECKS_PID).write_text(json.dumps({"pid": os.getpid(), "started": proc_start(os.getpid()), "ts": ts}))

    started(now - 600)
    assert Daemon(p.base)._precheck_holds(p.db.task(tid), now), "the daemon holds it: the same test"
    h = overseer.check(p, now)
    text = overseer.line(h, now)
    assert h["failing"] == [] and (h["urgent_ready"], h["urgent_held"]) == (1, 1), h
    assert "urgent ready 1 (1 held: its review's prechecks are running)" in text and "FAILING" not in text, text
    assert dumps == [] and not (p.state / overseer.INCIDENTS_FILE).exists()
    stalled = ["urgent work ready with a free slot for 15 min"]
    (out / CHECKS_RC).write_text("0\n")   # the checks ended: the reviewer should have started
    assert overseer.measure(p, now)["failing"] == stalled
    (out / CHECKS_RC).unlink()
    started(now - PRECHECK_HOLD_S - 1)   # still running, but the daemon no longer waits for them
    assert overseer.measure(p, now)["failing"] == stalled
    started(now - 600)
    (out / CHECKS_PID).write_text(json.dumps({"pid": os.getpid(), "started": "not this process", "ts": now - 600}))
    assert overseer.measure(p, now)["failing"] == stalled   # their process is gone


def test_the_health_line_reaches_the_digest_status_and_deployed(env, monkeypatch):
    from ttp import cli, coordinator, overseer
    now = time.time()
    p = _stalled_loop(env, now)
    monkeypatch.setattr(overseer, "capture_stacks", lambda p, now, why: None)
    overseer.check(p, now)
    assert "unapplied answers 1" in cli.status_text(p)
    parts, _ = coordinator.digest_parts(p, {}, [], [])
    assert any(k == "loop_health" and "FAILING" in body for k, body in parts)


def test_a_results_failure_captures_stacks_once_and_records_one_incident(env, monkeypatch):
    from ttp import daemon as dm, overseer, watchdog
    now = time.time()
    p = _stalled_loop(env, now)
    proc = subprocess.Popen(["sleep", "600"])
    try:
        (p.state / "daemon.pid").write_text(str(proc.pid))
        monkeypatch.setattr(dm, "_is_daemon", lambda pid: pid == proc.pid)
        monkeypatch.setattr(overseer.shutil, "which", lambda name: None)   # no py-spy: the faulthandler path
        sent = []
        monkeypatch.setattr(watchdog, "start_marker", lambda p: {"pid": proc.pid, "stacks": True})

        real_kill = os.kill

        def kill(pid, sig):   # os.kill itself is patched: only the dump request is caught
            if sig != signal.SIGUSR1:
                return real_kill(pid, sig)
            sent.append((pid, sig))
            with open(p.state / dm.STACKS_FILE, "a") as f:
                f.write("Thread 0x1 (most recent call first):\n  File daemon.py, line 1 in named_refs\n")
        monkeypatch.setattr(watchdog.os, "kill", kill)
        h = overseer.check(p, now)
        assert sent == [(proc.pid, signal.SIGUSR1)]
        stacks = pathlib.Path(h["incident"]["stacks"])
        assert stacks.parent.name == "incidents" and "named_refs" in stacks.read_text()
        # The next pass in the same failure keeps the incident and captures nothing more.
        h2 = overseer.check(p, now + 60)
        assert sent == [(proc.pid, signal.SIGUSR1)] and h2["incident"] == h["incident"]
        lines = (p.state / overseer.INCIDENTS_FILE).read_text().splitlines()
        assert len(lines) == 1 and "unreaped" in json.loads(lines[0])["why"]
        assert "incident since" in overseer.line(h2, now + 60)
        # Results good again: the incident closes.
        p.db.x("UPDATE runs SET status='done'")
        p.db.x("UPDATE tasks SET status='done'")
        assert "incident" not in overseer.check(p, now + 120)
    finally:
        proc.kill()
        proc.wait()


def test_py_spy_is_preferred_for_the_stacks_when_installed(env, monkeypatch):
    from ttp import daemon as dm, overseer
    p = make(env)
    (p.state / "daemon.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(dm, "_is_daemon", lambda pid: True)
    monkeypatch.setattr(overseer.shutil, "which", lambda name: "/opt/py-spy")
    monkeypatch.setattr(overseer.subprocess, "run", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout=f"Thread {argv[3]}: tick\n"))
    path = overseer.capture_stacks(p, time.time(), "test")
    assert "py-spy dump" in path.read_text() and str(os.getpid()) in path.read_text()


def test_the_daemon_write_lock_does_not_stall_the_overseer(env):
    from ttp import overseer
    now = time.time()
    p = _stalled_loop(env, now)
    held = sqlite3.connect(str(p.state / "project.db"), timeout=1, isolation_level=None)
    held.execute("BEGIN IMMEDIATE")   # the daemon in its write transaction, for as long as it likes
    held.execute("UPDATE kv SET ts=ts")
    try:
        t0 = time.monotonic()
        h = overseer.measure(p, now)
        assert time.monotonic() - t0 < overseer.BUSY_MS / 1000, "a reader never waits on the writer"
        assert h["unreaped"] == 2 and not h.get("error")
        with pytest.raises(sqlite3.OperationalError):   # and it cannot write
            overseer._ro_db(p.state / "project.db").conn.execute("UPDATE kv SET ts=0")
    finally:
        held.execute("ROLLBACK")
        held.close()


def test_the_loop_and_systemd_restart_a_dead_overseer(env, monkeypatch):
    from ttp import daemon as dm, overseer, service
    p = make(env)
    unit = service._overseer_text(p)
    assert "Restart=always" in unit and "-m ttp.overseer" in unit and "ttp.daemon" not in unit
    d = dm.Daemon(p.base)
    calls = []
    monkeypatch.setattr(service, "ensure_overseer", lambda p: calls.append(p.name) or "restarted its unit")
    monkeypatch.setattr(service, "installed", lambda p: None)
    now = time.time()
    assert d.check_overseer(now) is None and not calls, "no service keeps this daemon: it starts no overseer"
    monkeypatch.setattr(service, "installed", lambda p: {"kind": "systemd", "watchdog": True})
    assert d.check_overseer(now) == "restarted its unit" and calls == [p.name]   # never ran: missing
    assert d.check_overseer(now + 10) is None and len(calls) == 1, "at most once per STALE_S"
    hb = p.state / overseer.HEARTBEAT_FILE
    hb.write_text("{}")
    os.utime(hb, (now + overseer.STALE_S, now + overseer.STALE_S))
    assert d.check_overseer(now + 2 * overseer.STALE_S) is None, "a live overseer is left alone"
    os.utime(hb, (now, now))
    assert d.check_overseer(now + 2 * overseer.STALE_S) == "restarted its unit" and len(calls) == 2


def test_ensure_overseer_restarts_its_systemd_unit_or_spawns_one(env, monkeypatch):
    from ttp import service
    p = make(env)
    ran, spawned = [], []
    monkeypatch.setattr(service, "_run", lambda *a: ran.append(a) or types.SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kw: spawned.append(argv[2]))
    assert service.ensure_overseer(p) == "started a detached overseer" and "-m ttp.overseer" in spawned[0]
    service._unit(p).parent.mkdir(parents=True, exist_ok=True)
    service._unit(p).write_text(service._unit_text(p))
    assert service.ensure_overseer(p) == "restarted its unit"
    name = service._overseer_unit(p).name
    assert ("systemctl", "--user", "restart", name) in ran and service._overseer_unit(p).exists()


def test_a_look_the_coordinator_sets_replaces_the_idle_wake_and_wakes_it_once(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import looks
    from ttp.daemon import idle_wake
    p, d, clock, starts = _wake_setup(env, monkeypatch)
    assert coord.apply(p, [{"type": "look", "at": "45m", "text": "did the soak finish? it gates the merge"}]) == []
    at = clock[0] + 45 * 60
    w = idle_wake(p, p.config(), {}, clock[0])
    assert w["at"] == at and w["look"].startswith("did the soak"), w
    assert _first_turn(d, clock, starts, at - 30) is None, "woke before the look was due"
    fired = _first_turn(d, clock, starts, at + 120)
    assert fired is not None and fired >= at, (fired, at)
    assert looks.pending(p.db) == [] and p.db.kv(looks.DONE_KEY)[-1]["status"] == "woken"
    starts.clear()
    assert _first_turn(d, clock, starts, clock[0] + 1800) is None, "a settled look woke it again"
    assert idle_wake(p, p.config(), {}, clock[0]).get("look") is None, "no look: back to the idle wake"


def test_a_look_whose_check_says_nothing_changed_starts_no_turn(env, monkeypatch):
    from ttp import coordinator as coord
    from ttp import looks
    p, d, clock, starts = _wake_setup(env, monkeypatch)
    assert coord.apply(p, [{"type": "look", "at": "10m", "text": "same", "precheck": "true"},
                           {"type": "look", "at": "20m", "text": "changed", "precheck": "exit 3"}]) == []
    _first_turn(d, clock, starts, clock[0] + 15 * 60)
    assert not starts and p.db.kv(looks.DONE_KEY)[-1]["status"] == "skipped"
    assert _first_turn(d, clock, starts, clock[0] + 15 * 60) is not None, "a check that saw a change did not wake"
    assert [x["text"] for x in p.db.kv(looks.DONE_KEY) if x["status"] == "woken"] == ["changed"]
    assert coord.apply(p, [{"type": "look", "at": "1h", "text": "x"}, {"type": "look", "enabled": False}]) == []
    assert looks.pending(p.db) == []
    assert coord.apply(p, [{"type": "look", "at": "9d", "text": "x"}]), "a look past the horizon was taken"


def test_the_overseer_wakes_a_due_look_the_stuck_loop_missed_and_the_paths_never_start_two(env, monkeypatch):
    from ttp import looks
    from ttp import overseer as ov
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    clock = [time.time()]
    starts = []
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(Daemon, "start_run", lambda self, *a, **k: starts.append(clock[0]))
    p.db.set_kv("last_coordinator_turn", clock[0])
    looks.add(p.db, clock[0] + 60, "check the soak")
    # The tick loop is stuck: nothing calls maybe_coordinate; the overseer's wake runs it in-process here.
    monkeypatch.setattr(ov, "spawn_wake", lambda p: ov.wake(str(p.base)))
    clock[0] += 60 + ov.LOOK_GRACE_S - 5
    ov.check(p, clock[0])
    assert not starts, "the overseer woke inside the loop's grace"
    clock[0] += 10
    h = ov.check(p, clock[0])
    assert len(starts) == 1 and h["wake"]["rc"] == 0, h.get("wake")
    assert looks.pending(p.db) == [] and p.db.kv(looks.DONE_KEY)[-1]["status"] == "woken"
    looks.add(p.db, clock[0], "again")
    clock[0] += ov.LOOK_GRACE_S + 10
    ov.check(p, clock[0])
    assert len(starts) == 1, "woke twice within WAKE_EVERY_S"
    # The loop starting a turn at the same moment holds the start lock: the other path starts none.
    with looks.start_lock(p.state) as mine:
        assert mine
        Daemon(p.base).maybe_coordinate()
    assert len(starts) == 1, "two paths started two turns"


def _slow_daemon(p, monkeypatch, ran):
    """A Daemon on `p` whose tick steps have a 0.01 s budget, and a step that overruns it into `ran`."""
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    monkeypatch.setattr(d, "_step_budget", lambda: 0.01)

    def slow():
        ran.append(1)
        time.sleep(0.03)
    return d, slow


def _degraded_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='tick_step_degraded'")


def test_a_step_that_keeps_overrunning_sits_out_ticks_is_one_incident_and_one_ride_along_event(env, monkeypatch):
    from ttp import looks, pagesev
    from ttp import overseer as ov
    from ttp.daemon import DEGRADE_AFTER, DEGRADE_TICKS
    p = make(env)
    _no_events(p)
    ran = []
    d, slow = _slow_daemon(p, monkeypatch, ran)
    for _ in range(DEGRADE_AFTER + DEGRADE_TICKS):
        d._run_step(slow, "sweep_unreviewed")
        d._run_step(lambda: None, "quick_step")
    assert len(ran) == DEGRADE_AFTER, "the degraded step was not skipped for its ticks"
    d._run_step(slow, "sweep_unreviewed")
    assert len(ran) == DEGRADE_AFTER + 1, "the step did not come back after its ticks"
    incs = [json.loads(x) for x in (p.state / ov.INCIDENTS_FILE).read_text().splitlines()]
    assert [i["step"] for i in incs] == ["sweep_unreviewed"], incs
    assert incs[0]["overruns"]["sweep_unreviewed"] == DEGRADE_AFTER and Path(incs[0]["stacks"]).read_text()
    [ev] = _degraded_events(p)
    assert ev["severity"] == "low" and ev["status"] == "queued" and pagesev.rides_along(ev), ev
    assert looks.pending(p.db) == [], "a degraded step forced a coordinator turn"


def test_a_step_degraded_for_hours_and_across_a_restart_adds_no_look_and_one_event(env, monkeypatch):
    from ttp import looks
    from ttp.daemon import DEGRADE_AFTER, DEGRADE_TICKS
    p = make(env)
    _no_events(p)
    real, shift = time.time, [0.0]
    monkeypatch.setattr(time, "time", lambda: real() + shift[0])
    ran = []
    d, slow = _slow_daemon(p, monkeypatch, ran)
    for hour in range(3):
        if hour == 2:   # a daemon restart: a new Daemon on the same project
            d, slow = _slow_daemon(p, monkeypatch, ran)
        before = len(ran)
        for _ in range(2 * (DEGRADE_AFTER + DEGRADE_TICKS)):
            d._run_step(slow, "sweep_unreviewed")
        assert len(ran) - before < 2 * (DEGRADE_AFTER + DEGRADE_TICKS), "the step was never degraded"
        shift[0] += 3600
    assert looks.pending(p.db) == [], "a degraded step added a look that forces a coordinator turn"
    assert len(_degraded_events(p)) <= 1, "the same degrade was raised again"


def test_a_step_that_recovered_and_degrades_again_raises_one_more_event(env, monkeypatch):
    from ttp.daemon import DEGRADE_AFTER, DEGRADE_TICKS, DEGRADE_WINDOW
    p = make(env)
    _no_events(p)
    ran = []
    d, slow = _slow_daemon(p, monkeypatch, ran)
    for _ in range(DEGRADE_AFTER + DEGRADE_TICKS):
        d._run_step(slow, "blocker_sweep")
    assert len(_degraded_events(p)) == 1
    for _ in range(DEGRADE_AFTER + DEGRADE_TICKS):   # still slow: the same condition, no new event
        d._run_step(slow, "blocker_sweep")
    assert len(_degraded_events(p)) == 1
    for _ in range(DEGRADE_WINDOW):   # it recovered
        d._run_step(lambda: None, "blocker_sweep")
    for _ in range(DEGRADE_AFTER):
        d._run_step(slow, "blocker_sweep")
    assert len(_degraded_events(p)) == 2, "a new degrade after a recovery was not raised"


def test_a_slow_push_step_still_runs_every_tick(env, monkeypatch):
    from ttp.daemon import DEGRADE_AFTER, DEGRADE_TICKS
    p = make(env)
    _no_events(p)
    for name in ("tend_pushes", "reap_runs", "some_new_step"):
        ran = []
        d, slow = _slow_daemon(p, monkeypatch, ran)
        for _ in range(DEGRADE_AFTER + DEGRADE_TICKS):
            d._run_step(slow, name)
        assert len(ran) == DEGRADE_AFTER + DEGRADE_TICKS, f"slow {name} sat out ticks"
        assert not d._degraded.get(name)
    assert _degraded_events(p) == []


def test_only_observe_only_tick_steps_may_be_degraded(env):
    import ast
    import inspect
    import textwrap
    from ttp.daemon import DEGRADE_OK, Daemon
    tree = ast.parse(textwrap.dedent(inspect.getsource(Daemon.tick)))
    steps = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
             and isinstance(n.value, ast.Name) and n.value.id == "self" and callable(getattr(Daemon, n.attr, None))}
    steps |= {c.args[1].value for c in ast.walk(tree) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "_run_step"
              and len(c.args) > 1 and isinstance(c.args[1], ast.Constant)}
    assert {"tend_pushes", "dispatch", "maybe_coordinate", "reap_runs", "deliver_outbound"} <= steps, "the walk lost steps"
    assert DEGRADE_OK <= steps, f"DEGRADE_OK names no tick step: {DEGRADE_OK - steps}"
    bad = [s for s in DEGRADE_OK if s.startswith("reap_") or any(w in s for w in ("push", "meter", "dispatch", "start",
                                                                                  "deliver", "coordinat"))]
    assert not bad, f"steps the loop needs are allowed to sit out ticks: {bad}"

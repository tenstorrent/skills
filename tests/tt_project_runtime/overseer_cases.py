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
    p.db.x("INSERT INTO events(ts, source, kind, text, status) VALUES(?,?,?,?,?)", (old, "daemon", "x", "e", "new"))
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

# Cases for tests/test_tt_project_runtime.py: a daemon stalled in one tick step is restarted by its
# watchdog, and the restarted daemon never re-enters the same hang by reaping the same run again
# (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _tickhang_watchdog(p, monkeypatch, pid=424242):
    """A live 'daemon' pid whose heartbeat stopped WATCHDOG_S + 100 s ago; restarts are recorded, not run."""
    from ttp import watchdog
    restarts = []
    monkeypatch.setattr(watchdog, "_is_daemon", lambda q: q == pid)
    monkeypatch.setattr(watchdog, "_alive", lambda q: q == pid)
    monkeypatch.setattr(watchdog, "save_dump", lambda p, q, now, mins, wait: p.state / "incidents" / "dump.log")
    monkeypatch.setattr(watchdog, "_restart", lambda p, q, grace: (restarts.append(q), (True, "restarted"))[1])
    (p.state / "daemon.pid").write_text(str(pid))
    hb = p.state / "heartbeat"
    hb.write_text(json.dumps({"pid": pid, "started": time.time() - 3600}))
    since = time.time() - watchdog.WATCHDOG_S - 100
    os.utime(hb, (since, since))
    return restarts, since


def test_a_daemon_stalled_in_a_named_step_is_restarted_at_twice_the_stale_threshold(env, monkeypatch):
    from ttp import daemon as dm
    from ttp import watchdog
    p = make(env)
    assert watchdog.WATCHDOG_S == 2 * dm.HEARTBEAT_STALE_S < watchdog.settings(p)["hang_s"]
    restarts, since = _tickhang_watchdog(p, monkeypatch)
    now = time.time()
    # No stall report, one from another pid, one from before the last progress, a torn file: hang_s holds.
    for stall in (None, {"pid": 7, "step": "reap_runs", "at": now}, {"pid": 424242, "step": "reap_runs",
                  "at": since - 5}, "{torn"):
        if stall is not None:
            (p.state / dm.STALL_FILE).write_text(stall if isinstance(stall, str) else json.dumps(stall))
        assert watchdog.check(p, now=now) == "stale"
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S) == "stale", stall
        (p.state / "watchdog.json").unlink(missing_ok=True)
    assert not restarts
    # The daemon's own tick watch said it stalled in reap_runs after that progress: restarted now.
    (p.state / dm.STALL_FILE).write_text(json.dumps({"pid": 424242, "step": "reap_runs", "at": since + 300}))
    assert watchdog.check(p, now=now) == "stale"
    assert watchdog.check(p, now=now + watchdog.CONFIRM_S) == "restarted" and restarts == [424242]
    assert "stalled in tick step reap_runs" in (p.logs / "daemon.log").read_text()
    told = p.db.q("SELECT severity, text FROM messages WHERE ref='daemon-hang' AND kind='alert'")
    assert len(told) == 1 and told[0]["severity"] == "low" and "reap_runs" in told[0]["text"]
    # A repeat within the window waits twice as long.
    assert watchdog.threshold(p, watchdog.settings(p), now, True) == 2 * watchdog.WATCHDOG_S


def test_a_stalled_tick_writes_the_stall_report_the_watchdog_reads(env):
    from ttp import daemon as dm
    from ttp import watchdog
    p = make(env)
    d = dm.Daemon(p.base)
    d.cfg = p.config()
    t = time.monotonic()
    d._tick_began, d._moved, d._step_name = t, t, "reap_runs"
    assert d.check_overrun(t + 10) is None and not (p.state / dm.STALL_FILE).exists()
    before = time.time()
    assert d.check_overrun(t + dm.TICK_STALL_S + 1)["stalled"]
    rec = json.loads((p.state / dm.STALL_FILE).read_text())
    assert rec["pid"] == os.getpid() and rec["step"] == "reap_runs" and rec["at"] >= before
    assert watchdog.stalled_step(p, os.getpid(), before - 1) == "reap_runs"
    assert watchdog.stalled_step(p, os.getpid(), rec["at"] + 1) is None   # it moved since


class _CutOff(BaseException):
    """What killing the daemon mid-step looks like to the code under it: nothing below catches it."""


def _coordinator_run(p, tmp_path, actions):
    run_dir = tmp_path / "coord-run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"actions": actions, "_cost": 0.25}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    return p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
                  ("coordinator", "fake", time.time(), "running", str(run_dir), "x", "{}"))


def test_a_reap_cut_off_in_a_slow_step_is_retried_once_lean_and_applied_once(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord
    from ttp import daemon as dm
    p = make(env)
    rid = _coordinator_run(p, tmp_path, [{"type": "task_add", "title": "label the numbers", "spec": "s",
                                          "kind": "work", "tier": "light"}])
    real, seen = coord.apply, []

    def apply(*a, **kw):
        seen.append(kw.get("lean"))
        if not kw.get("lean"):
            raise _CutOff()   # the slow optional step: the watchdog kills the daemon in it
        return real(*a, **kw)
    monkeypatch.setattr(coord, "apply", apply)
    with pytest.raises(_CutOff):
        dm.Daemon(p.base).reap_runs()
    run = p.db.one("SELECT status, cost_usd FROM runs WHERE id=?", (rid,))
    assert run["status"] == "running" and not run["cost_usd"], "a cut-off reap committed part of the run's end"
    assert p.db.kv(dm.REAP_KEY)[str(rid)]["lean"] is False
    # The restarted daemon reaps it once more, lean: the turn is applied once and booked once.
    dm.Daemon(p.base).reap_runs()
    assert seen == [False, True]
    run = p.db.one("SELECT status, cost_usd FROM runs WHERE id=?", (rid,))
    assert run["status"] == "ok" and run["cost_usd"] == 0.25
    assert len(p.db.q("SELECT id FROM tasks WHERE title='label the numbers'")) == 1
    assert not p.db.kv(dm.REAP_KEY)
    assert p.db.one("SELECT id FROM messages WHERE ref=?", (f"reap-lean:{rid}",))
    assert "never finished" in (p.logs / "daemon.log").read_text()


def test_a_lean_reap_cut_off_too_quarantines_the_run_instead_of_looping(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord
    from ttp import daemon as dm
    p = make(env)
    rid = _coordinator_run(p, tmp_path, [{"type": "memory_add", "text": "x", "memory_kind": "preference"}])
    calls = []
    monkeypatch.setattr(coord, "apply", lambda *a, **kw: (calls.append(kw.get("lean")), (_ for _ in ()).throw(_CutOff()))[1])
    for _ in range(2):
        with pytest.raises(_CutOff):
            dm.Daemon(p.base).reap_runs()
    assert calls == [False, True]
    dm.Daemon(p.base).reap_runs()   # the third daemon never enters the step again
    assert calls == [False, True]
    run = p.db.one("SELECT status FROM runs WHERE id=?", (rid,))
    assert run["status"] == "failed" and not p.db.kv(dm.REAP_KEY)
    assert p.db.one("SELECT id FROM messages WHERE ref=?", (f"reap-quarantine:{rid}",))
    assert "quarantined" in (p.logs / "daemon.log").read_text()


def test_a_lean_reap_that_raises_is_quarantined_at_once_and_a_full_one_keeps_its_three_tries(env, tmp_path, monkeypatch):
    from ttp import coordinator as coord
    from ttp import daemon as dm
    p = make(env)
    rid = _coordinator_run(p, tmp_path, [{"type": "memory_add", "text": "x", "memory_kind": "preference"}])
    calls, cut = [], [True]

    def apply(*a, **kw):
        calls.append(kw.get("lean"))
        if cut:   # only the first reap is cut off
            cut.clear()
            raise _CutOff()
        raise RuntimeError("bad action")
    monkeypatch.setattr(coord, "apply", apply)
    with pytest.raises(_CutOff):
        dm.Daemon(p.base).reap_runs()
    d = dm.Daemon(p.base)
    d.reap_runs()
    assert calls == [False, True]
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.one("SELECT id FROM messages WHERE ref=?", (f"reap-quarantine:{rid}",))
    # A full reap that raises (no cut-off before it) is no hang: it keeps the old three tries.
    rid2 = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
                  ("coordinator", "fake", time.time(), "running", p.db.one("SELECT dir FROM runs WHERE id=?",
                                                                           (rid,))["dir"], "x", "{}"))
    calls.clear()
    d.reap_runs()
    d.reap_runs()
    assert calls == [False, False] and p.db.one("SELECT status FROM runs WHERE id=?", (rid2,))["status"] == "running"
    assert not p.db.kv(dm.REAP_KEY)
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid2,))["status"] == "failed"


def test_a_lean_apply_skips_the_restriction_checks_and_notes_it(env):
    """The same narrowing that queues a restriction_tools task applied lean: the charter changes the
    same, no tool scan runs, and the next digest's notes say the checks were skipped."""
    from ttp import coordinator as coord
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on a shared rack at night")
    act = {"type": "charter_update", "section": "Restrictions", "quote": _GUARD_SOAK + " (user msg #7)",
           "text": "Run the soak test on a shared rack only at night.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids, lean=True) == []
    assert "only at night" in p.charter_path.read_text()
    assert tasks() == [] and not p.db.q("SELECT id FROM events WHERE kind='restriction_tools'")
    assert any("restriction checks skipped" in n for n in p.db.kv(coord.NOTES_KEY, []))

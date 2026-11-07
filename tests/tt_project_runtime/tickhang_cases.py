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


def test_a_lean_reap_of_a_coordinator_without_tool_hooks_still_runs_the_whole_wake_guard(env, tmp_path):
    """An earlier daemon cut off before the wake guard: the lean retry still puts back a charter the
    wake changed outside ttp's writers and records it; only the coordwrites report is skipped."""
    from ttp import coordtools
    from ttp import daemon as dm
    p = make(env)
    rid = _coordinator_run(p, tmp_path, [])
    before = p.charter_path.read_text()
    coordtools.wake_snapshot(tmp_path / "coord-run", coordtools.protected_paths(p))
    p.charter_path.write_text(before + "\n- loosened\n")
    p.db.set_kv(dm.REAP_KEY, {str(rid): {"pid": 1, "started": 0, "lean": False, "at": time.time()}})
    dm.Daemon(p.base).reap_runs()
    assert p.db.one("SELECT id FROM messages WHERE ref=?", (f"reap-lean:{rid}",)), "reaped lean"
    assert p.charter_path.read_text() == before
    assert p.db.q("SELECT id FROM events WHERE kind='coord_protected_write'")


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


def test_reap_runs_stops_at_its_step_budget_and_the_rest_are_reaped_once_on_later_ticks(env, tmp_path, monkeypatch):
    """Several runs ending together, each end slow, once held reap_runs (and every worker start) for 9 min:
    past STEP_BUDGET_S it stops, the rest stay running on record with their exit.json on disk, and later
    ticks, or a restarted daemon, reap each of them exactly once."""
    from ttp import daemon as dm
    p = make(env)
    rids = []
    for n in range(4):
        run_dir = tmp_path / f"coord-run-{n}"
        run_dir.mkdir()
        (run_dir / "output.jsonl").write_text(json.dumps({"actions": [
            {"type": "task_add", "title": f"slow end {n}", "spec": "s", "kind": "work", "tier": "light"}],
            "_cost": 0.1}))
        (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
        rids.append(p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
                           ("coordinator", "fake", time.time(), "running", str(run_dir), "x", "{}")))
    real, ends = dm.Daemon.finish_run, []

    def slow_finish(self, r, exit_info):
        time.sleep(0.3)   # one run's end work on a loaded host
        ends.append(r["id"])
        return real(self, r, exit_info)
    monkeypatch.setattr(dm.Daemon, "finish_run", slow_finish)
    monkeypatch.setattr(dm, "STEP_BUDGET_S", 0.2)

    def running():
        return [r["id"] for r in p.db.q("SELECT id FROM runs WHERE status='running' ORDER BY id")]
    d = dm.Daemon(p.base)
    t0 = time.monotonic()
    d._run_step(d.reap_runs)
    assert time.monotonic() - t0 < 0.9, "the step ran past its budget by more than one run's end"
    assert ends == rids[:1] and running() == rids[1:]
    d._run_step(d.reap_runs)
    assert ends == rids[:2] and running() == rids[2:], "the next tick resumes after the last one reaped"
    d = dm.Daemon(p.base)   # a restart in between loses none of the deferred ends
    for _ in range(3):
        d._run_step(d.reap_runs)
    assert ends == rids and not running()
    assert all(p.db.one("SELECT status FROM runs WHERE id=?", (i,))["status"] == "ok" for i in rids)
    for n in range(4):
        assert len(p.db.q("SELECT id FROM tasks WHERE title=?", (f"slow end {n}",))) == 1, "applied once"


def test_a_finish_warm_up_with_hung_git_ends_at_its_budget_and_the_run_is_still_reaped(env, monkeypatch):
    """A failed review's area once read git for minutes in _prefetch_git, inside reap_runs: all its git
    together now ends within gitmemo.PREFETCH_GIT_S, every spawn has a timeout, and the hand-off is
    still judged and recorded."""
    import subprocess
    from ttp import daemon as dm
    from ttp import gitmemo, reviewcap
    p = make(env)
    tid = p.db.add_task("review the change", "spec", kind="review", tier="standard", origin="coordinator")
    real_spawn, spawned = gitmemo._spawn, []

    def hung_spawn(argv, t, kw):
        if not any("hung-ref" in str(a) for a in argv):
            return real_spawn(argv, t, kw)
        spawned.append(t)
        assert t is not None, "a git spawn with no timeout"
        time.sleep(min(t, 0.2))
        raise subprocess.TimeoutExpired(argv, t)

    def slow_area(p_, cfg, review, now=None):   # one git read per failed review in its window, each hung
        for n in range(50):   # each a different read: the memo keeps a timed-out answer
            try:
                gitmemo.run(["git", "-C", str(p_.root), "rev-parse", f"hung-ref-{n}"], capture_output=True, read=True)
            except subprocess.TimeoutExpired:
                pass
        return None
    monkeypatch.setattr(gitmemo, "_spawn", hung_spawn)
    monkeypatch.setattr(reviewcap, "area", slow_area)
    monkeypatch.setattr(gitmemo, "PREFETCH_GIT_S", 0.5, raising=False)
    d = dm.Daemon(p.base)
    t0 = time.monotonic()
    d._run_step(lambda: d._prefetch_git(p.db.task(tid), {"status": "failed", "summary": "changes needed"}),
                "reap_runs")
    assert time.monotonic() - t0 < 3, "the warm-up ran past its budget"
    assert len(spawned) <= 5 and max(spawned) <= 0.5 + 0.01, spawned   # past the deadline: no new spawn
    assert "reading git before finishing stopped at its" in (p.logs / "daemon.log").read_text()
    assert tid in d._git_memo, "the finish still gets its warm-up's memo"


def test_finish_rounds_learning_hung_git_end_at_their_wall_and_the_last_round_still_applies(env, monkeypatch):
    """Each miss a finish round learned outside the lock once had TICK_GIT_S of its own, past the
    rounds' FINISH_WALL_S: one run's end could hold reap_runs for minutes. The reads it learns now end
    by the wall, every spawn has a timeout, and the final round (which never aborts) still applies."""
    import subprocess
    from ttp import daemon as dm
    from ttp import gitmemo
    p = make(env)
    tid = p.db.add_task("finish behind slow git", "spec", kind="code", tier="standard", origin="coordinator")
    real_spawn, spawned = gitmemo._spawn, []

    def hung_spawn(argv, t, kw):
        if not any("hung-ref" in str(a) for a in argv):
            return real_spawn(argv, t, kw)
        spawned.append(t)
        assert t is not None, "a git spawn with no timeout"
        time.sleep(min(t, 2.0))
        raise subprocess.TimeoutExpired(argv, t)
    monkeypatch.setattr(gitmemo, "_spawn", hung_spawn)
    monkeypatch.setattr(gitmemo, "FINISH_WALL_S", 0.5)
    d = dm.Daemon(p.base)
    t0, finals = time.monotonic(), []
    for n, rnd in enumerate(d._finish_rounds(tid)):
        with rnd:
            if not rnd.final:   # every abortable round misses a read, as a cold memo under load does
                raise gitmemo.GitMiss(["git", "-C", str(p.root), "rev-parse", f"hung-ref-{n}"], {"capture_output": True},
                                      None, True, "test miss", True)
            finals.append(n)
            d.p.db.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    assert time.monotonic() - t0 < 1.5, "the finish ran past its wall"
    assert spawned and max(spawned) <= 0.5 + 0.01, spawned
    assert len(finals) == 1 and d.p.db.task(tid)["status"] == "done", "the last round did not apply"

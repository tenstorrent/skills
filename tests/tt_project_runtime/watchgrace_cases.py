# Grace-window tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file. A command schedule's
# `grace_min` holds a keyed line until it has lasted that long, so a route that heals on its own
# raises nothing on a short blip (ttp.watchgrace).
from __future__ import annotations


def _grace_watcher(env, monkeypatch, grace_min=10):
    """A daemon whose command schedule `rt` prints `out["stdout"]`, on a clock the test moves."""
    from ttp import daemon as dm
    from ttp import screen as scr
    p = make(env)
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(scr.time, "time", lambda: clock["t"])
    monkeypatch.setattr(dm.time, "time", lambda: clock["t"])
    out = {"stdout": ""}
    monkeypatch.setattr(dm.schedrun, "run_group",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out["stdout"], ""))
    d = dm.Daemon(p.base)
    payload = {"command": "x", "grace_min": grace_min}
    return p, d, clock, out, lambda: d._run_command_watcher({"name": "rt"}, payload)


def _raised(p):
    return [dict(r) for r in p.db.q("SELECT severity, text, status FROM events WHERE kind='observation' "
                                    "ORDER BY id")]


_DOWN = json.dumps({"text": "route to the relay: unreachable", "severity": "high", "key": "relay-route"})


def test_watchgrace_a_check_that_fails_then_recovers_inside_its_grace_raises_nothing(env, monkeypatch):
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    out["stdout"] = _DOWN
    assert run() == "ok (0 observations, 1 held in their grace)"
    clock["t"] += 5 * 60
    assert run() == "ok (0 observations, 1 held in their grace)"
    clock["t"] += 3 * 60
    out["stdout"] = ""   # healed by itself before its 10 min
    assert run() == "ok (0 observations)"
    assert _raised(p) == [] and p.db.q("SELECT id FROM issues") == []
    assert not p.db.kv("watch_grace:rt")
    # The next blip counts its grace afresh: 9 min in, still nothing.
    out["stdout"] = _DOWN
    run()
    clock["t"] += 9 * 60
    run()
    assert _raised(p) == []


def test_watchgrace_a_check_failing_past_its_grace_raises_one_normal_line_then_low_repeats(env, monkeypatch):
    from ttp import cli
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    out["stdout"] = json.dumps({"text": "route to the relay: unreachable", "severity": "normal", "key": "relay-route"})
    run()
    clock["t"] += 6 * 60
    run()
    assert _raised(p) == []
    clock["t"] += 5 * 60   # 11 min: past its grace
    assert run() == "ok (1 observations)"
    [raised] = _raised(p)
    assert raised["severity"] == "normal", raised
    for _ in range(3):
        clock["t"] += 10 * 60
        assert run() == "ok (1 observations)"   # never an error run
    assert len(_raised(p)) == 1, _raised(p)   # the low repeats wake nobody
    assert p.db.kv("watch_grace:rt")["relay-route"]["raised"] == "normal"
    p.db.x("INSERT INTO schedules(name, kind, every_s, enabled, payload) VALUES(?,?,?,?,?)",
           ("rt", "command", 600, 1, json.dumps({"command": "x", "grace_min": 10})))
    assert "relay-route: down for 41 min (past its 10 min grace)" in _cli_out(cli, p, "rt")


def _cli_out(cli, p, name):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["schedules", p.name, "grace", name])
    return buf.getvalue()


def test_watchgrace_step_forget_and_status():
    from ttp import watchgrace as wg
    assert wg.grace_s({"grace_min": 2}) == 120 and wg.grace_s({}) == 0 and wg.grace_s({"grace_min": "x"}) == 0
    st = {}
    assert wg.step(st, "k", 0, 0, "high") == (wg.PASS, "high")
    assert wg.step(st, "k", 100, 600, "high") == (wg.HOLD, None)
    assert wg.status(st, 600, now=400) == ["k: retrying for 5 min (5 min of grace left)"]
    assert wg.step(st, "k", 700, 600, "high") == (wg.RAISE, "high")   # its own rating, at least normal
    assert wg.step(st, "k", 800, 600, "high") == (wg.REPEAT, "low")
    assert wg.step(st, "k", 900, 600, "critical") == (wg.RAISE, "critical")   # a real escalation
    assert wg.status(st, 600, "other") == ["other: ok"]
    assert wg.forget(st, set()) == {} and wg.forget(st, {"k"}) == st


def test_watchgrace_holds_no_unkeyed_line_and_a_clear_ends_the_clock(env, monkeypatch):
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    out["stdout"] = json.dumps({"text": "disk nearly full", "severity": "high"})
    assert run() == "ok (1 observations)"
    out["stdout"] = _DOWN
    run()
    out["stdout"] = json.dumps({"text": "cleared: relay route", "key": "relay-route"})
    run()
    assert "relay-route" not in (p.db.kv("watch_grace:rt") or {})


def test_watchgrace_schedule_set_takes_grace_min_and_zero_removes_it(env):
    from ttp import coordinator as coord
    p = make(env)
    base = {"type": "schedule_set", "name": "rt", "kind": "command", "every": "10m", "command": "true"}
    assert coord.apply(p, [{**base, "grace_min": 15}]) == []
    load = lambda: json.loads(p.db.one("SELECT payload FROM schedules WHERE name='rt'")["payload"])
    assert load()["grace_min"] == 15
    assert coord.apply(p, [{**base, "grace_min": None}]) == [] and load()["grace_min"] == 15   # left out: kept
    assert coord.apply(p, [{**base, "grace_min": 0}]) == [] and "grace_min" not in load()
    assert coord.apply(p, [{**base, "grace_min": -1}]) != []


def test_watchgrace_passes_low_keyed_lines_through_and_still_holds_a_normal_fault(env, monkeypatch):
    # A watcher that keys its routine "in sync" line at low can use grace_min for its real fault lines.
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    seen = []
    monkeypatch.setattr(d, "observe", lambda source, text, hint=None, **kw: seen.append((text, hint, kw.get("key"))))
    routine = json.dumps({"text": "relay: in sync", "severity": "low", "key": "relay-sync"})
    fault = json.dumps({"text": "relay: behind by 3 commits", "severity": "normal", "key": "relay-behind"})
    out["stdout"] = routine
    assert run() == "ok (1 observations)"
    assert seen == [("relay: in sync", "low", "relay-sync")]   # as rated, on its first run, never raised
    assert "relay-sync" not in (p.db.kv("watch_grace:rt") or {})
    out["stdout"] = routine + "\n" + fault
    clock["t"] += 5 * 60
    assert run() == "ok (1 observations, 1 held in their grace)"
    assert seen[-1] == ("relay: in sync", "low", "relay-sync")
    clock["t"] += 11 * 60   # the fault, 11 min in: past its 10 min grace
    assert run() == "ok (2 observations)"
    assert seen[-1] == ("relay: behind by 3 commits", "normal", "relay-behind")
    assert seen[-2] == ("relay: in sync", "low", "relay-sync")   # still low, never lifted to normal
    for sev in ("info", "LOW"):   # any case of a low rating passes
        from ttp import watchgrace as wg
        assert not wg.holds(sev)
    assert wg.holds("normal") and wg.holds("high") and wg.holds(None) and wg.holds("bogus")


_CANT = json.dumps({"text": "ssh to the relay timed out: check could not run", "error": True})


def _ran(p, d, status):
    """Record one run of schedule `rt` as the daemon does, and return the fix tasks it queued."""
    if not p.db.one("SELECT name FROM schedules WHERE name='rt'"):
        p.db.x("INSERT INTO schedules(name, kind, every_s, enabled, payload) VALUES(?,?,?,?,?)",
               ("rt", "command", 600, 1, json.dumps({"command": "x", "grace_min": 10})))
    d._schedule_ran({"name": "rt"}, status, None)
    return [r["id"] for r in p.db.q("SELECT id FROM tasks WHERE title LIKE 'Fix schedule rt%'")]


def test_watchgrace_could_not_run_lines_inside_the_grace_are_a_retry_and_a_good_run_resets_them(env, monkeypatch):
    from ttp import schedule as sched
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    out["stdout"] = _DOWN
    run()   # a keyed fault's clock starts
    first = p.db.kv("watch_grace:rt")["relay-route"]["first"]
    out["stdout"] = _CANT
    for _ in range(3):   # 0, 4 and 8 min: three could-not-run runs, all inside the 10 min grace
        status = run()
        assert status == "retrying (1 lines: check could not run, inside its grace)", status
        assert not sched.failing(status) and _ran(p, d, status) == []
        clock["t"] += 4 * 60
    assert _raised(p) == [] and p.db.q("SELECT id FROM issues") == []
    # the check could not run: the keyed fault's clock is kept, not restarted
    assert p.db.kv("watch_grace:rt")["relay-route"]["first"] == first
    out["stdout"] = ""   # it ran again and found nothing: both clocks end
    assert run() == "ok (0 observations)" and _ran(p, d, "ok (0 observations)") == []
    assert not p.db.kv("watch_grace:rt")
    out["stdout"] = _CANT
    clock["t"] += 60
    assert run().startswith("retrying")   # the next outage counts its grace afresh
    out["stdout"] = json.dumps({"text": "relay: in sync", "severity": "low", "key": "relay-sync"})
    run()   # a run with no error line ends the error clock too
    assert "(check could not run)" not in (p.db.kv("watch_grace:rt") or {})


def test_watchgrace_could_not_run_lines_past_the_grace_fail_the_schedule_once(env, monkeypatch):
    from ttp import cli
    from ttp import schedule as sched
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    out["stdout"] = _CANT
    statuses = []
    for _ in range(6):   # every 5 min for 25 min
        statuses.append(run())
        _ran(p, d, statuses[-1])
        clock["t"] += 5 * 60
    assert [sched.failing(x) for x in statuses] == [False, False, True, True, True, True], statuses
    assert statuses[2] == "error (1 lines: check could not run)"
    assert len(_ran(p, d, statuses[-1])) == 1   # one fix task for the stint, not one per run
    assert "(check could not run): down for" in _cli_out(cli, p, "rt")


def test_watchgrace_never_holds_a_crashed_command_or_errors_without_a_grace(env, monkeypatch):
    from ttp import daemon as dm
    p, d, clock, out, run = _grace_watcher(env, monkeypatch)
    monkeypatch.setattr(dm.schedrun, "run_group",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "Traceback: boom"))
    assert run().startswith("error: watcher command failed rc=2")   # the script, not the route
    p2, d2, clock2, out2, run2 = _grace_watcher(env, monkeypatch, grace_min=0)
    monkeypatch.setattr(dm.schedrun, "run_group",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, _CANT, ""))
    assert run2() == "error (1 lines: check could not run)"   # no grace_min: as before

# Host-load watch (hostload.py, Daemon.check_host_load) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
# Add tests of the checks-host overload observation and the per-host lines of `ttp checks --slot-status`
# and the daily review here.
from __future__ import annotations


def _overloaded_host(monkeypatch, load=(105.0, 98.0, 123.0), cores=32, cap=4):
    from ttp import checkcap
    monkeypatch.setattr(os, "getloadavg", lambda: load)
    monkeypatch.setattr(checkcap, "cores", lambda: cores)
    monkeypatch.setenv("TTP_CHECKS_HOST_MAX", str(cap))


def _host_load_issues(p, status=None):
    from ttp import hostload
    return p.db.q("SELECT * FROM issues WHERE source=? AND watch_key=?" + (" AND status=?" if status else ""),
                  (hostload.SOURCE, hostload.KEY, *([status] if status else [])))


def test_the_host_load_watch_raises_once_after_its_hold_and_clears_with_the_condition():
    from ttp import hostload
    over = {"load": (105.0, 98.0, 123.0), "cores": 32, "factor": 1.0, "threshold": 32.0, "cap": 32,
            "running": 30, "spilled": {}, "spill_hosts": []}
    w = hostload.Watch()
    assert w.step({**over, "load": (5.0, 5.0, 5.0)}, 0.0) == ("clear", None), "a restart closes a left issue once"
    assert w.step(over, 100.0) is None and w.step(over, 100.0 + hostload.HOLD_S - 1) is None, "not before the hold"
    kind, obs = w.step(over, 100.0 + hostload.HOLD_S)
    assert kind == "raise" and obs["key"] == hostload.KEY == "checks_host_overloaded" and obs["episode"] == "100"
    t = obs["text"]
    for part in ("5-minute load 98.0", "105.0/98.0/123.0", "its 32 cores", "30 check runs", "cap of 32 (checks.host_max)",
                 "delivery.checks_spill_hosts", "for 15 min"):
        assert part in t, (part, t)
    assert w.step(over, 100.0 + 2 * hostload.HOLD_S) is None, "one observation per episode"
    assert w.step({**over, "running": 10}, 3000.0) == ("clear", None), "runs well under the cap: cleared"
    assert w.step({**over, "running": 10}, 3060.0) is None, "cleared once"
    w.step(over, 4000.0)
    kind, obs = w.step(over, 4000.0 + hostload.HOLD_S)
    assert kind == "raise" and obs["episode"] == "4000", "a new episode raises again"
    # Each part of the condition: the 5-minute load (not the 1-minute) over the threshold, runs near the cap.
    assert not hostload.overloaded({**over, "load": (200.0, 31.0, 200.0)})
    assert hostload.overloaded({**over, "running": 24}) and not hostload.overloaded({**over, "running": 23})
    assert hostload.overloaded({**over, "cap": 1, "running": 1}) and not hostload.overloaded({**over, "cap": 0})
    assert not hostload.overloaded({**over, "load": None}), "an unreadable load never fires"
    named = hostload.text({**over, "spill_hosts": ["check-host-a"], "factor": 2.0, "threshold": 64.0}, 900)
    assert "(delivery.checks_spill_hosts: check-host-a) take none" in named and "2 x its 32 cores" in named, named


def test_the_daemon_raises_a_checks_host_overload_from_held_slots_and_closes_it_when_the_load_drops(env, monkeypatch):
    from ttp import checkcap, daemon, hostload
    p = make(env)
    _overloaded_host(monkeypatch)
    monkeypatch.setattr(hostload, "HOLD_S", 0.0)
    held = [checkcap.acquire(f"other{i}") for i in range(3)]   # 3 of 4: near the cap
    try:
        d = daemon.Daemon(p.base)
        assert hostload.snapshot(d.cfg)["running"] == 3
        d.check_host_load()
        open_ = _host_load_issues(p, "open")
        assert len(open_) == 1 and open_[0]["episode"], open_
        ev = p.db.q("SELECT * FROM events WHERE kind='observation' AND text LIKE 'checks host overloaded:%'")
        assert len(ev) == 1 and ev[0]["status"] == "queued" and "3 check runs here" in ev[0]["text"], ev
        d._host_load_checked = 0.0
        d.check_host_load()
        assert len(p.db.q("SELECT id FROM events WHERE text LIKE 'checks host overloaded:%'")) == 1, "once"
        monkeypatch.setattr(os, "getloadavg", lambda: (10.0, 20.0, 60.0))
        d._host_load_checked = 0.0
        d.check_host_load()
        assert not _host_load_issues(p, "open")
        closed = _host_load_issues(p, "fixed")
        assert len(closed) == 1 and closed[0]["cleared_why"] == hostload.CLEARED, closed
    finally:
        for h in held:
            h.close()


def test_no_checks_host_overload_while_a_spill_host_takes_runs_or_the_runs_are_far_under_the_cap(env, monkeypatch):
    from ttp import checkcap, checkspill, daemon, hostload
    p = make(env)
    _overloaded_host(monkeypatch)
    monkeypatch.setattr(hostload, "HOLD_S", 0.0)
    held = [checkcap.acquire(f"other{i}") for i in range(3)]
    claim = checkspill._claim("check-host-a", 1, {"project": p.name, "task": "7", "pid": os.getpid(),
                                                  "since": time.time(), "what": "pytest -q", "host": "check-host-a"})
    try:
        d = daemon.Daemon(p.base)
        d.check_host_load()
        assert not _host_load_issues(p), "a spill host takes runs: the pool is in use, no alert"
        lines = hostload.lines(d.cfg)
        assert lines[0].startswith("this host: 3 check runs (cap 4), load 1/5/15 min 105.0/98.0/123.0 on 32 cores")
        assert "check-host-a: 1 spilled check runs from this host" in lines, lines
    finally:
        claim.close()
        held.pop().close()
    d._host_load_checked = 0.0
    d.check_host_load()   # 2 of 4 runs: under the cap's NEAR share
    assert not _host_load_issues(p)
    for h in held:
        h.close()


def test_slot_status_and_the_daily_review_show_check_runs_per_host_and_the_load(env, tmp_path, monkeypatch, capsys):
    import json as _json
    from ttp import checkcap, cli, daemon
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    _overloaded_host(monkeypatch)
    held = checkcap.acquire("other")
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--slot-status"])
        assert e.value.code == 0
        out = capsys.readouterr().out
        assert "check runs per host:\n  this host: 1 check runs (cap 4), load 1/5/15 min 105.0/98.0/123.0 on 32 " \
               "cores (threshold 32)" in out, out
        with pytest.raises(SystemExit) as e:
            cli.main(["checks", "--slot-status", "--json"])
        hosts = _json.loads(capsys.readouterr().out)["hosts"]
        assert hosts["running"] == 1 and hosts["cap"] == 4 and hosts["load"] == [105.0, 98.0, 123.0], hosts
        assert hosts["overloaded"] is False, "1 of 4 runs is not near the cap"
        dm = daemon.Daemon(p.base)
        s = {"name": "daily-review", "budget_usd_day": None, "last_run": None, "description": "review"}
        assert dm._schedule_llm(s, {}) == "queued"
        spec = p.db.one("SELECT spec FROM tasks WHERE origin='schedule'")["spec"]
        assert "Check runs per host now" in spec and "- this host: 1 check runs (cap 4)" in spec, spec[-800:]
    finally:
        held.close()

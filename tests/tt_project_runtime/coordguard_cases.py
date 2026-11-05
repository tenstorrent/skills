# Coordinator session spend guard (coordguard.py, Daemon.check_coord_guard) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there): helpers, fixtures and imports come
# from that file. Add tests of the session-mode baseline, trips, holds and the status line here.
from __future__ import annotations

DAY = 86400.0


def _coord_runs(p, start, n, usd_each, step_s=600.0):
    for i in range(n):
        t = start + i * step_s
        p.db.x("INSERT INTO runs(role, started, ended, status, cost_usd) VALUES('coordinator', ?, ?, 'ok', ?)",
               (t, t + 60, usd_each))


def _session_on(p, value="on", by="coordinator"):
    from ttp import configwho
    p.set_config("coordinator.session", {**(p.raw_config().get("coordinator", {}).get("session") or {}),
                                         "mode": value}, by=configwho.by(by, via="cli"))


def _guard_project(env, now):
    """A project whose stateless coordinator ran 70 turns at $1 over the week before `now` ($10/day, $1/turn)."""
    p = make(env)
    _coord_runs(p, now - 7 * DAY + 60, 70, 1.0, step_s=7 * DAY / 70 - 1)
    return p


def test_the_session_guard_records_the_stateless_baseline_at_switch_on(env):
    from ttp import coordguard
    now = time.time()
    p = _guard_project(env, now)
    _coord_runs(p, now - 9 * DAY, 5, 50.0)   # older than the week: not in the baseline
    _session_on(p)
    got = coordguard.check(p.db, p.config(), now)
    assert got["event"] == "baseline", got
    base = p.db.kv(coordguard.BASELINE_KEY)
    assert base["turns"] == 70 and abs(base["usd_turn"] - 1.0) < 1e-6 and abs(base["usd_day"] - 10.0) < 0.1, base
    assert base["at"] <= now and now - base["at"] < 60, "switch-on is the mode's recorded set time"
    assert p.db.kv(coordguard.KEY) == {"tripped": False, "trips": []}
    assert coordguard.check(p.db, p.config(), now + 1) is None, "recorded once"
    assert p.db.kv(coordguard.BASELINE_KEY) == base


def test_the_session_guard_trips_over_its_allowance_and_stays_quiet_under_it(env):
    from ttp import coordguard
    now = time.time()
    p = _guard_project(env, now)
    _session_on(p)
    coordguard.check(p.db, p.config(), now)
    base = p.db.kv(coordguard.BASELINE_KEY)
    # Full day: the allowance is the baseline day plus the warm-up turns, never more.
    assert abs(coordguard.allowance(base, 24) - (base["usd_day"] + coordguard.WARM_TURNS * base["usd_turn"])) < 1e-6
    assert coordguard.allowance(base, 0.1) == coordguard.allowance(base, coordguard.MIN_H), "never less than an hour"
    assert coordguard.allowance(base, 2) > 2 / 24 * base["usd_day"] * 1.5, "early hours keep headroom"
    # Session spend at about the baseline rate, 12 h in: under the allowance.
    t = now + 12 * 3600
    _coord_runs(p, now + 60, 5, 1.0, step_s=7200)
    assert coordguard.check(p.db, p.config(), t) is None
    assert coordguard.tripped(p.db, t) is False and p.db.kv(coordguard.KEY)["tripped"] is False
    assert not p.db.q("SELECT id FROM issues WHERE source=?", (coordguard.SOURCE,))
    # A runaway: well past the allowance trips it, with the contract's fields.
    _coord_runs(p, t - 3000, 10, 2.0, step_s=60)
    got = coordguard.check(p.db, p.config(), t)
    g = p.db.kv(coordguard.KEY)
    assert got["event"] == "trip" and not got["hold"], got
    assert g["tripped"] is True and g["at"] == t and g["until"] == t + coordguard.PAUSE_H * 3600, g
    assert "$25.00" in g["why"] and "baseline $10.0" in g["why"], g["why"]
    assert coordguard.tripped(p.db, t + 60)
    # Configured pause_h is read; a bad value falls back to the default.
    assert coordguard.pause_h({"coordinator": {"session": {"pause_h": 2}}}) == 2.0
    assert coordguard.pause_h({"coordinator": {"session": {"pause_h": "x"}}}) == coordguard.PAUSE_H
    assert coordguard.pause_h({"coordinator": {"session": "on"}}) == coordguard.PAUSE_H


def test_the_session_guard_untrips_after_until_and_counts_only_session_spend(env):
    from ttp import coordguard
    now = time.time()
    p = _guard_project(env, now)
    _session_on(p)
    coordguard.check(p.db, p.config(), now)
    _coord_runs(p, now + 60, 20, 2.0, step_s=60)
    t = now + 3600
    assert coordguard.check(p.db, p.config(), t)["event"] == "trip"
    until = p.db.kv(coordguard.KEY)["until"]
    assert coordguard.check(p.db, p.config(), until - 1) is None and coordguard.tripped(p.db, until - 1)
    _coord_runs(p, t + 60, 30, 1.0, step_s=600)   # stateless turns while tripped: never counted after
    got = coordguard.check(p.db, p.config(), until)
    g = p.db.kv(coordguard.KEY)
    assert got["event"] == "untrip" and g["tripped"] is False and g["resumed"] == until, g
    assert not coordguard.tripped(p.db, until)
    assert coordguard.check(p.db, p.config(), until + 600) is None, "the window starts again at the resume"


def test_the_third_session_guard_trip_in_a_week_holds_until_the_mode_is_set_again(env):
    from ttp import configwho, coordguard
    now = time.time()
    p = _guard_project(env, now)
    _session_on(p)
    coordguard.check(p.db, p.config(), now)
    t = now
    for i in range(3):
        _coord_runs(p, t + 60, 20, 2.0, step_s=60)
        got = coordguard.check(p.db, p.config(), t + 3600)
        assert got["event"] == "trip" and got["hold"] == (i == 2), (i, got)
        t = p.db.kv(coordguard.KEY)["until"]
        if i < 2:
            assert coordguard.check(p.db, p.config(), t)["event"] == "untrip"
    g = p.db.kv(coordguard.KEY)
    assert g["hold"] and len(g["trips"]) == 3
    late = t + 3 * DAY
    assert coordguard.check(p.db, p.config(), late) is None, "past until: still held"
    assert coordguard.tripped(p.db, late) and p.db.kv(coordguard.KEY)["until"] > late, "until renewed for its readers"
    # A worker's set is not the coordinator's, and a set from before the trip is old: still held.
    configwho.record(p.db, "coordinator.session.mode", configwho.by("worker", task=1), now=late)
    assert coordguard.check(p.db, p.config(), late + 1) is None and coordguard.tripped(p.db, late + 1)
    configwho.record(p.db, "coordinator.session.mode", configwho.by("coordinator"), now=g["at"] - 1)
    assert coordguard.check(p.db, p.config(), late + 1) is None and coordguard.tripped(p.db, late + 1)
    configwho.record(p.db, "coordinator.session.mode", configwho.by("coordinator"), now=late + 1)
    got = coordguard.check(p.db, p.config(), late + 2)
    assert got["event"] == "release", got
    g = p.db.kv(coordguard.KEY)
    assert g["tripped"] is False and g["trips"] == [] and not coordguard.tripped(p.db, late + 3), g
    # Trips older than the week do not count toward a hold.
    p.db.set_kv(coordguard.KEY, {"tripped": False, "trips": [late - 8 * DAY, late - 7.5 * DAY], "resumed": late})
    _coord_runs(p, late + 60, 20, 2.0, step_s=60)
    got = coordguard.check(p.db, p.config(), late + 3600)
    assert got["event"] == "trip" and not got["hold"], got


def test_the_session_guard_is_inert_with_the_mode_off_or_no_baseline(env):
    from ttp import coordguard
    now = time.time()
    p = _guard_project(env, now)
    _coord_runs(p, now + 60, 50, 5.0, step_s=60)
    for mode in (None, "off", "shadow", False, "", 1.5):
        cfg = p.config()
        if mode is not None:
            cfg = {**cfg, "coordinator": {**cfg["coordinator"], "session": {"mode": mode}}}
        assert not coordguard.mode_on(cfg), mode
        assert coordguard.check(p.db, cfg, now + 3600) is None, mode
        assert p.db.kv(coordguard.KEY) is None and p.db.kv(coordguard.BASELINE_KEY) is None, mode
        assert coordguard.status_line(p.db, cfg, now) is None
    assert coordguard.mode_on({"coordinator": {"session": {"mode": "ON "}}})
    assert coordguard.mode_on({"coordinator": {"session": {"mode": True}}})
    # Switched on, tripped, then off: both keys go, so the next switch-on records a fresh baseline.
    _session_on(p)
    assert coordguard.check(p.db, p.config(), now)["event"] == "baseline"
    assert coordguard.check(p.db, p.config(), now + 3600)["event"] == "trip"
    _session_on(p, "off")
    assert coordguard.check(p.db, p.config(), now + 3700)["event"] == "off"
    assert p.db.kv(coordguard.KEY) is None and not coordguard.tripped(p.db, now + 3700)
    # No coordinator turn in the week before: nothing to compare, never trips.
    q = p
    q.db.x("DELETE FROM runs")
    _session_on(q)
    assert "nothing to compare" in coordguard.check(q.db, q.config(), now)["text"]
    _coord_runs(q, now + 60, 50, 5.0, step_s=60)
    assert coordguard.check(q.db, q.config(), now + 3600) is None and not coordguard.tripped(q.db, now + 3600)
    assert "inert" in coordguard.status_line(q.db, q.config(), now + 3600)


def test_the_daemon_session_guard_raises_an_issue_and_a_low_notify_and_closes_it_on_resume(env, monkeypatch):
    from ttp import coordguard, daemon
    from ttp.cli import status_text
    now = time.time()
    p = _guard_project(env, now)
    _session_on(p)
    d = daemon.Daemon(p.base)
    d.check_coord_guard()
    assert p.db.kv(coordguard.BASELINE_KEY)["turns"] == 70
    assert "session guard: baseline $10.00/day, $1.00/turn · ok:" in status_text(p, verbose=True)
    assert "session guard" not in status_text(p)
    _coord_runs(p, time.time() + 1, 20, 2.0, step_s=1)   # session turns since the switch-on
    d.check_coord_guard()
    assert p.db.kv(coordguard.KEY) is not None and p.db.kv(coordguard.KEY)["tripped"] is False, "once per CHECK_S"
    d._coord_guard_checked = 0.0
    d.check_coord_guard()
    g = p.db.kv(coordguard.KEY)
    assert g["tripped"] is True, g
    issues = p.db.q("SELECT * FROM issues WHERE source=? AND watch_key=? AND status='open'",
                    (coordguard.SOURCE, coordguard.KEY))
    assert len(issues) == 1, issues
    out = p.db.q("SELECT * FROM messages WHERE direction='out' AND kind='alert' AND severity='low'")
    assert any("coordinator session spent" in m["text"] for m in out), out
    assert "TRIPPED, stateless until" in status_text(p, verbose=True)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + coordguard.PAUSE_H * 3600 + 60)
    d._coord_guard_checked = 0.0
    d.check_coord_guard()
    assert p.db.kv(coordguard.KEY)["tripped"] is False
    assert not p.db.q("SELECT id FROM issues WHERE source=? AND status='open'", (coordguard.SOURCE,))

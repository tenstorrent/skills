# Event hold tests of tests/test_tt_project_runtime.py (eventhold.py: routine event-only coordinator turns
# wait until coordinator.event_hold_s after the last turn ended), which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _evhold_setup(env, monkeypatch, busy=2):
    """_batch_setup with batch_s and the wake gap off (so only the event hold can hold; the gap, which a
    free slot does not end, is in wakegap_cases.py) and a turn that just ended."""
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=busy)
    p.set_config("coordinator.batch_s", 0)
    p.set_config("coordinator.wake_gap_s", 0)
    d.cfg = p.config()
    p.db.x("INSERT INTO runs(role, started, ended, status) VALUES('coordinator', ?, ?, 'ok')",
           (clock[0] - 60, clock[0]))
    p.db.set_kv("last_coordinator_turn", clock[0] - 60)
    return p, d, clock, starts


def test_eventhold_two_routine_wakes_60s_apart_share_one_turn_after_the_hold(env, monkeypatch):
    from ttp.daemon import Daemon
    p, d, clock, starts = _evhold_setup(env, monkeypatch)
    hold = float(p.config()["coordinator"]["event_hold_s"])
    assert hold == 900
    ended = clock[0]
    clock[0] += 10
    _event(p, clock, "upstream_note")
    assert not _turn_within(d, clock, starts, 60), "a routine event started a turn inside the hold"
    _event(p, clock, "task_done")
    _event(p, clock, "followup_proposed")
    # A daemon restart keeps the hold: it runs from the last turn's end in the database.
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    monkeypatch.setattr(d, "start_run", lambda *a, **k: starts.append(clock[0]))
    assert not _turn_within(d, clock, starts, ended + hold - 5 - clock[0], step=5)
    assert _turn_within(d, clock, starts, 120), "the hold ran past event_hold_s"
    assert len(starts) == 1 and starts[0] >= ended + hold


def test_eventhold_a_user_message_starts_a_turn_at_once(env, monkeypatch):
    p, d, clock, starts = _evhold_setup(env, monkeypatch)
    clock[0] += 10
    _event(p, clock, "task_done")
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 60), "a user message waited on the event hold"


def test_eventhold_high_or_non_routine_events_start_a_turn_at_once(env, monkeypatch):
    for kind, sev in (("upstream_note", "high"), ("task_done", "critical"), ("obligation_overdue", "normal"),
                      ("ask_timeout", "normal"), ("machine_condition", "normal")):
        p, d, clock, starts = _evhold_setup(env, monkeypatch)
        clock[0] += 10
        _event(p, clock, "task_done")
        _event(p, clock, kind, sev)
        assert _turn_within(d, clock, starts, 60), f"{kind}/{sev} waited on the event hold"


def test_eventhold_a_free_slot_with_nothing_ready_starts_a_turn_at_once(env, monkeypatch):
    p, d, clock, starts = _evhold_setup(env, monkeypatch, busy=0)
    clock[0] += 10
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60), "an idle slot waited on the event hold"
    # Ready work queued for the free slot: dispatch fills it, so the routine turn waits.
    p, d, clock, starts = _evhold_setup(env, monkeypatch, busy=0)
    p.db.add_task("next up", "s", origin="coordinator")
    clock[0] += 10
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 120)


def test_eventhold_off_or_long_after_the_last_turn_does_not_hold(env, monkeypatch):
    p, d, clock, starts = _evhold_setup(env, monkeypatch)
    p.set_config("coordinator.event_hold_s", 0)
    d.cfg = p.config()
    clock[0] += 10
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60)
    p, d, clock, starts = _evhold_setup(env, monkeypatch)
    clock[0] += 400
    _event(p, clock, "task_waiting")
    assert _turn_within(d, clock, starts, 60)


def test_eventhold_until_reads_only_routine_batches():
    from ttp import eventhold
    cfg = {"coordinator": {"event_hold_s": 300}}
    ev = [{"kind": "checks_slot_wait", "severity": "low"}]
    assert eventhold.until(cfg, [], ev, 1000.0, 1100.0) == 1300.0
    assert eventhold.until(cfg, [], ev, 1000.0, 1300.0) is None
    assert eventhold.until(cfg, [{"id": 1}], ev, 1000.0, 1100.0) is None
    assert eventhold.until(cfg, [], [], 1000.0, 1100.0) is None
    assert eventhold.until(cfg, [], ev, None, 1100.0) is None
    assert eventhold.until(cfg, [], ev + [{"kind": "stall", "severity": "normal"}], 1000.0, 1100.0) is None
    assert eventhold.until(cfg, [], [{"kind": "task_done", "severity": None}], 1000.0, 1100.0) is None
    assert eventhold.until({"coordinator": {"event_hold_s": "x"}}, [], ev, 1000.0, 1100.0) == 1900.0
    assert eventhold.until({"coordinator": {"event_hold_s": -5}}, [], ev, 1000.0, 1100.0) is None


def test_eventhold_hot_marks_fresh_urgent_and_obligation_owner_ends_not_priority_alone(env, monkeypatch):
    from ttp import eventhold, obligations
    p = make(env)
    now = time.time()
    plain = p.db.add_task("plain", "s", priority=1)
    hot = p.db.add_task("hot", "urgent: the user waits\ns")
    owner = p.db.add_task("owner", "s")
    ev = lambda tid: [{"kind": "task_done", "severity": "normal", "task": tid}]
    monkeypatch.setattr(obligations, "owner_due", lambda db: {owner: now + 3600})
    assert not eventhold.hot(p.db, ev(plain), now)
    assert eventhold.hot(p.db, ev(hot), now) and eventhold.hot(p.db, ev(owner), now)
    assert not eventhold.hot(p.db, [{"kind": "task_done", "severity": "normal"}], now)
    monkeypatch.setattr(obligations, "owner_due", lambda db: 1 / 0)
    assert not eventhold.hot(p.db, ev(owner), now), "a failure holds as before"

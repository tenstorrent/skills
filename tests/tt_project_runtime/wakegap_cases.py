# Wake gap and wake triage tests of tests/test_tt_project_runtime.py (eventhold.gap_until: event-only
# batches that may wait share one coordinator turn per coordinator.wake_gap_s, a free slot included;
# waketriage.py: the watcher model folds a routine batch into the next wake), which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations

_WG_CHAT = {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1",
            "tiers": {"light": {"model": "example-org/small-model", "route": True}}}


def _wg_setup(env, monkeypatch, busy=0, chat=False):
    """A turn that just ended, batch_s off, a free worker slot by default; `chat` turns the watcher on."""
    from ttp import configwho
    p, d, clock, starts = _batch_setup(env, monkeypatch, busy=busy)
    p.set_config("coordinator.batch_s", 0)
    if chat:
        p.set_config("providers.chat", _WG_CHAT, by=configwho.by("user", via="cli"))
    d.cfg = p.config()
    p.db.x("INSERT INTO runs(role, started, ended, status) VALUES('coordinator', ?, ?, 'ok')",
           (clock[0] - 60, clock[0]))
    p.db.set_kv("last_coordinator_turn", clock[0] - 60)
    notes = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: (starts.append(clock[0]),
                                                          notes.append((k.get("note") or {}).get("events"))))
    return p, d, clock, starts, notes


def test_wakegap_routine_events_in_the_gap_share_one_wake_even_with_a_free_slot(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch, busy=0)
    ended = clock[0]
    assert float(p.config()["coordinator"]["wake_gap_s"]) == 300
    for kind in ("task_done", "task_failed", "blocker_sweep", "urgent_check_wait"):
        clock[0] += 40   # events at +40, +110, +180 and +250 s, each checked for 30 s
        _event(p, clock, kind)
        assert not _turn_within(d, clock, starts, 30), f"{kind} woke the coordinator inside the wake gap"
    assert _turn_within(d, clock, starts, 120), "the wake gap ran past wake_gap_s"
    assert len(starts) == 1 and starts[0] >= ended + 300


def test_wakegap_user_messages_and_urgent_events_wake_at_once(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    clock[0] += 10
    _event(p, clock, "task_done")
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 60), "a user message waited on the wake gap"
    for kind, sev in (("task_done", "high"), ("obligation_overdue", "normal"), ("ask_timeout", "normal"),
                      ("machine_condition", "normal"), ("promise_overdue", "normal"), ("queue_wait", "normal"),
                      ("charter_retry", "normal"), ("outside_writes", "normal")):
        p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
        clock[0] += 10
        _event(p, clock, "task_done")
        _event(p, clock, kind, sev)
        assert _turn_within(d, clock, starts, 60), f"{kind}/{sev} waited on the wake gap"


def test_wakegap_off_wakes_a_free_slot_at_once(env, monkeypatch):
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch)
    p.set_config("coordinator.wake_gap_s", 0)
    d.cfg = p.config()
    clock[0] += 10
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60)


def test_waketriage_skip_folds_the_batch_into_the_next_wake(env, monkeypatch):
    from ttp import babysit
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, chat=True)
    asked = []
    monkeypatch.setattr(babysit, "_post", lambda s, messages, timeout: (asked.append(messages), {
        "choices": [{"message": {"content": "skip"}}], "usage": {"prompt_tokens": 40, "completion_tokens": 1}})[1])
    clock[0] += 400   # past the wake gap
    _event(p, clock, "task_done")
    eid = p.db.one("SELECT MAX(id) n FROM events")["n"]
    assert not _turn_within(d, clock, starts, 300), "a watcher skip still woke the coordinator"
    assert len(asked) == 1, "the same folded batch was asked again"
    assert "task_done happened" in asked[0][1]["content"]
    assert p.db.one("SELECT COUNT(*) n FROM ledger WHERE source='babysit:coordinator_wake'")["n"] == 1
    assert p.db.one("SELECT status FROM events WHERE id=?", (eid,))["status"] == "queued"   # kept on disk
    p.db.x("INSERT INTO messages(ts,direction,chat,text,handled) VALUES(?,?,?,?,0)", (clock[0], "in", "web", "hi"))
    assert _turn_within(d, clock, starts, 60)
    assert eid in notes[-1], "the folded event was missing from the next wake's digest"
    # Left folded, the batch still wakes once its oldest event is wake_triage_max_s old.
    p, d, clock, starts, notes = _wg_setup(env, monkeypatch, chat=True)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert not _turn_within(d, clock, starts, 600)
    assert _turn_within(d, clock, starts, 1300), "a folded batch never woke the coordinator"


def test_waketriage_a_watcher_error_wakes_and_logs_the_model(env, monkeypatch, capsys):
    from ttp import babysit, daemon
    p, d, clock, starts, _ = _wg_setup(env, monkeypatch, chat=True)
    lines = []
    monkeypatch.setattr(daemon, "log", lambda proj, text, *a, **k: lines.append(text))

    def down(s, messages, timeout):
        raise babysit.Unavailable("HTTP 503")
    monkeypatch.setattr(babysit, "_post", down)
    clock[0] += 400
    _event(p, clock, "task_done")
    assert _turn_within(d, clock, starts, 60), "a watcher error held the wake"
    assert any("triage failed" in t and "example-org/small-model" in t and "HTTP 503" in t for t in lines), lines

# Screen issue cases: run by tests/test_tt_project_runtime.py (see _include_cases there).


def _hint_rig(env, monkeypatch):
    """One watcher condition on a fake clock, sent with any hint: (see(at_h, hint, **kw) -> Verdict, p, scr)."""
    p = make(env)
    from ttp import screen as scr
    cfg = p.config()
    clock = {"t": 1_800_000_000.0}
    t0 = clock["t"]
    monkeypatch.setattr(scr.time, "time", lambda: clock["t"])

    def see(at_h, hint, text="box-a: queue held (3 jobs waiting)", **kw):
        clock["t"] = t0 + at_h * 3600
        kw.setdefault("rewake_after_s", 6 * 3600)
        kw.setdefault("key", "box-a-queue")
        return scr.screen(p.db, cfg, "watcher:hw", text, hint, **kw)
    return see, p, scr


def test_a_keyed_issue_follows_a_lower_hint_down_and_stops_reminding_at_info(env, monkeypatch):
    see, p, _scr = _hint_rig(env, monkeypatch)
    assert see(0, "normal").wake
    # The watcher was fixed to report info: the open row follows it down and reminds no more.
    v = see(7, "info")
    assert not v.wake and v.severity == "info"
    row = p.db.one("SELECT severity, status, history FROM issues")
    assert (row["severity"], row["status"]) == ("info", "ignored")
    assert json.loads(row["history"])[-1]["action"] == "lowered"
    assert not see(13, "info").wake and not see(20, "info").wake
    assert len(p.db.q("SELECT id FROM issues")) == 1


def test_a_keyed_issue_lowered_but_above_the_floor_keeps_its_reminder_at_the_new_severity(env, monkeypatch):
    see, p, _scr = _hint_rig(env, monkeypatch)
    assert see(0, "high").wake
    assert not see(2, "normal").wake, "lowered inside the window: no wake"
    assert p.db.one("SELECT severity, status FROM issues") == {"severity": "normal", "status": "open"}
    v = see(7, "normal")
    assert v.wake and v.severity == "normal" and "after its last wake" in v.reason


def test_a_pinned_lower_hint_lowers_and_a_higher_hint_still_raises_and_wakes(env, monkeypatch):
    see, p, _scr = _hint_rig(env, monkeypatch)
    assert see(0, "high", pin=True).wake
    assert not see(1, "info", pin=True).wake
    assert p.db.one("SELECT severity, status FROM issues") == {"severity": "info", "status": "ignored"}
    v = see(2, "critical")
    assert v.wake and v.reason == "now critical"
    assert p.db.one("SELECT severity, status FROM issues") == {"severity": "critical", "status": "open"}


def test_an_unkeyed_issue_and_a_fixed_rows_reopen_ignore_the_lowering(env, monkeypatch):
    see, p, _scr = _hint_rig(env, monkeypatch)
    # Unkeyed: its text is its identity, so the same text at a lower hint changes nothing.
    assert see(0, "normal", key=None).wake
    v = see(7, "info", key=None)
    assert v.wake and v.severity == "normal"
    assert p.db.one("SELECT severity, status FROM issues") == {"severity": "normal", "status": "open"}
    # A fixed keyed row reopens as before: at info it is ignored, and nothing says it was lowered.
    first = see(8, "high")
    p.db.x("UPDATE issues SET status='fixed', closed=? WHERE id=?", (1_800_000_000.0 + 8 * 3600, first.issue_id))
    v = see(9, "info")
    assert not v.wake and v.reason == "regressed after fix"
    row = p.db.one("SELECT severity, status, history FROM issues WHERE id=?", (first.issue_id,))
    assert (row["severity"], row["status"]) == ("info", "ignored")
    assert [h["action"] for h in json.loads(row["history"])] == ["reopened"]

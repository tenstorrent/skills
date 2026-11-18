# Learned wake escalation (waketier.direct, escalation_cases, escalation_stats): tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

import json


class _RunsDB:
    """Just the runs query waketier.escalation_cases makes, newest first."""

    def __init__(self, rows):
        self.rows = rows

    def q(self, sql, params=()):
        return sorted(self.rows, key=lambda r: -r["id"])


def _esc_runs(task, kind, shape, reason="spec_change", first_id=1):
    """Runs of one task. shape: 'E' a light wake that escalated (a follow-up run follows it), 'L' a light wake
    followed by an ordinary run, in order; every light wake is followed by its next run."""
    rows, rid = [], first_id
    for ch in shape:
        light = {"wake": {"tier": "light", "escalated": False, "reason": reason}}
        rows.append({"id": rid, "task": task, "kind": kind, "note": json.dumps(light)})
        nxt = {"wake": {"tier": "standard", "escalated": True, "reason": "asked"}} if ch == "E" else {}
        rows.append({"id": rid + 1, "task": task, "kind": kind, "note": json.dumps(nxt)})
        rid += 2
    return rows


def _task(tier="standard", kind="code"):
    return {"id": 1, "tier": tier, "kind": kind}


def _prev(why="spec_change", **kw):
    return {"status": "waiting", "wake_tier": "light", "waiting_for": "a review", "wake_reason": why, **kw}


def test_a_kind_and_reason_that_mostly_escalates_wakes_at_the_tasks_own_tier():
    from ttp import waketier
    db = _RunsDB(_esc_runs(1, "code", "EEELEE"))
    got = waketier.direct(db, {}, _task(), _prev())
    assert got and got["tier"] == "standard" and (got["cases"], got["escalated"]) == (6, 5)
    assert waketier.direct(db, {}, _task("deep"), _prev())["tier"] == "deep"
    stats = waketier.escalation_stats(db, {})
    assert stats == [{"kind": "code", "reason": "spec_change", "cases": 6, "escalated": 5, "share": 0.83}]


def test_no_direct_wake_for_half_or_fewer_few_cases_other_kinds_or_the_off_switch():
    from ttp import waketier
    even = _RunsDB(_esc_runs(1, "code", "ELEL"))   # exactly half: not more than half
    assert waketier.direct(even, {}, _task(), _prev()) is None
    few = _RunsDB(_esc_runs(1, "code", "EEE"))   # three cases, under min_cases (4)
    assert waketier.direct(few, {}, _task(), _prev()) is None
    many = _RunsDB(_esc_runs(1, "code", "EEEEE"))
    assert waketier.direct(many, {}, _task(kind="question"), _prev()) is None   # another kind's history
    assert waketier.direct(many, {}, _task(), _prev("timer")) is None   # another wake reason's history
    off = {"waiting": {"wake_escalation": {"enabled": False}}}
    assert waketier.direct(many, off, _task(), _prev()) is None
    assert waketier.direct(many, {}, _task(), _prev()) is not None
    # last_n looks only at the newest cases: an old run of escalations does not decide a new calm one.
    old = _esc_runs(1, "code", "EEEEEE") + _esc_runs(1, "code", "LLLLLLLL", first_id=100)
    assert waketier.direct(_RunsDB(old), {}, _task(), _prev()) is None


def test_direct_never_applies_to_escalated_stale_fix_or_light_tasks_and_survives_bad_notes():
    from ttp import waketier
    db = _RunsDB(_esc_runs(1, "code", "EEEEE"))
    assert waketier.direct(db, {}, _task("light"), _prev()) is None   # already light
    assert waketier.direct(db, {}, _task(), _prev(escalated_wake=True)) is None   # already an escalated wake
    assert waketier.direct(db, {}, _task(), _prev(stale_wakes=2)) is None   # the wait was handed back unchanged
    assert waketier.direct(db, {}, _task(), {**_prev(), "status": "done"}) is None
    assert waketier.direct(db, {}, _task(), _prev(wake_tier="standard")) is None   # not a light wake to begin with
    fix = _RunsDB(_esc_runs(1, "code", "EEEEE", reason="checks_failed"))
    assert waketier.direct(fix, {}, _task(), _prev("checks_failed")) is None   # a FIX reason carries its own tier
    junk = _RunsDB([{"id": 1, "task": 1, "kind": "code", "note": "{not json"},
                    {"id": 2, "task": 1, "kind": "code", "note": None}] + _esc_runs(2, "code", "EEEEE", first_id=10))
    assert waketier.direct(junk, {}, _task(), _prev()) is not None
    bad = {"waiting": {"wake_escalation": {"last_n": "x", "share": None, "min_cases": []}}}
    assert waketier.escalation_settings(bad) == waketier.ESCALATION_DEFAULTS


class _KVDB(_RunsDB):
    def __init__(self, rows):
        super().__init__(rows)
        self.store = {}

    def kv(self, key, default=None):
        return self.store.get(key, default)

    def set_kv(self, key, value):
        self.store[key] = value


def _daemon(db, cfg=None):
    import types
    from ttp import daemon
    return daemon, types.SimpleNamespace(p=types.SimpleNamespace(db=db), cfg=cfg or {})


def test_the_wake_start_uses_the_own_tier_off_switch_restores_light_and_every_8th_runs_light(monkeypatch):
    daemon, d = _daemon(_KVDB(_esc_runs(1, "code", "EEEEEE")))
    lines = []
    monkeypatch.setattr(daemon, "log", lambda p, m: lines.append(m))
    got = daemon.Daemon._wake_direct(d, _task(), _prev())
    assert got and got["tier"] == "standard" and "escalated 6/6" in lines[-1] and "code wakes for spec_change" in lines[-1]
    tiers = [(daemon.Daemon._wake_direct(d, _task(), _prev()) or {}).get("tier") for _ in range(7)]
    assert tiers == ["standard"] * 6 + [None]   # the 8th wake of this kind and reason (1 + 7) runs light
    assert daemon.Daemon._wake_direct(d, _task(), _prev())["tier"] == "standard"
    off = {"waiting": {"wake_escalation": {"enabled": False}}}
    daemon, d = _daemon(_KVDB(_esc_runs(1, "code", "EEEEEE")), off)
    assert daemon.Daemon._wake_direct(d, _task(), _prev()) is None


def test_a_failing_direct_lookup_keeps_the_wake_light(monkeypatch):
    from ttp import waketier
    daemon, d = _daemon(_KVDB([]))
    lines = []
    monkeypatch.setattr(daemon, "log", lambda p, m: lines.append(m))
    monkeypatch.setattr(waketier, "direct", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert daemon.Daemon._wake_direct(d, _task(), _prev()) is None and "wake direct failed" in lines[-1]

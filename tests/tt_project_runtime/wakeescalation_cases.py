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
        self.scans = 0

    def q(self, sql, params=()):
        self.scans += 1
        return super().q(sql, params)

    def kv(self, key, default=None):
        return self.store.get(key, default)

    def set_kv(self, key, value):
        self.store[key] = value


def _daemon(db, cfg=None):
    import types
    from ttp import daemon
    d = types.SimpleNamespace(p=types.SimpleNamespace(db=db), cfg=cfg or {}, _wake_directs={})
    d._wake_direct = lambda t, pv: daemon.Daemon._wake_direct(d, t, pv)
    d._wake_direct_started = lambda t: daemon.Daemon._wake_direct_started(d, t)
    return daemon, d


def test_a_held_wake_decides_once_logs_once_and_only_a_started_run_counts(monkeypatch):
    db = _KVDB(_esc_runs(1, "code", "EEEEEE"))
    daemon, d = _daemon(db)
    lines = []
    monkeypatch.setattr(daemon, "log", lambda p, m: lines.append(m))
    task = {**_task(), "result": "r1"}
    for _ in range(3):   # three ticks of a start held by a full route slot
        assert d._wake_direct(task, _prev())["tier"] == "standard"
    assert db.scans == 1 and lines == [] and db.store == {}
    d._wake_direct_started(task)
    assert db.store == {"wake_direct_n:code:spec_change": 1} and len(lines) == 1
    assert "escalated 6/6" in lines[0] and "code wakes for spec_change" in lines[0]
    d._wake_direct_started(task)   # a second call for the same start counts nothing
    assert db.store == {"wake_direct_n:code:spec_change": 1} and len(lines) == 1


def test_the_8th_started_direct_wake_runs_light_even_when_earlier_ticks_were_held(monkeypatch):
    daemon, d = _daemon(_KVDB(_esc_runs(1, "code", "EEEEEE")))
    monkeypatch.setattr(daemon, "log", lambda p, m: None)
    tiers = []
    for n in range(1, 10):
        task = {**_task(), "result": f"r{n}"}   # a new wake: a new result
        for _ in range(3):   # held ticks never advance the counter
            got = d._wake_direct(task, _prev())
        tiers.append((got or {}).get("tier"))
        d._wake_direct_started(task)
    assert tiers == ["standard"] * 7 + [None, "standard"]
    assert d.p.db.store["wake_direct_n:code:spec_change"] == 9


def test_the_off_switch_restores_light_and_an_error_keeps_the_wake_light(monkeypatch):
    from ttp import waketier
    off = {"waiting": {"wake_escalation": {"enabled": False}}}
    daemon, d = _daemon(_KVDB(_esc_runs(1, "code", "EEEEEE")), off)
    assert d._wake_direct({**_task(), "result": "r"}, _prev()) is None
    daemon, d = _daemon(_KVDB([]))
    lines = []
    monkeypatch.setattr(daemon, "log", lambda p, m: lines.append(m))
    monkeypatch.setattr(waketier, "direct", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert d._wake_direct({**_task(), "result": "r"}, _prev()) is None and "wake direct failed" in lines[-1]
    d._wake_direct_started({**_task(), "result": "r"})   # nothing to count
    assert d.p.db.store == {}


def test_start_tasks_starts_a_mostly_escalating_wake_at_the_own_tier_with_direct_in_the_note(env, monkeypatch):
    from ttp import waketier
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    calls = []
    waits = {"status": "waiting", "summary": "checks run", "retry_when": "ttp checks --result /tmp/run/1",
             "waiting_for": "checks", "next_step": "push"}
    t = p.db.add_task("Copy the log", "x", kind="work", tier="standard", origin="coordinator",
                      provider="claude", budget_usd=8.0)
    p.db.update_task(t, result=json.dumps(waits))
    why = waketier.reason(waits)
    monkeypatch.setattr(waketier, "escalation_cases", lambda db, *a: calls.append(1) or [("work", why, True)] * 6)
    d.dispatch()
    ((a, k),) = started
    assert a[3] == "standard" and k["note"]["wake"]["tier"] == "standard"
    assert k["note"]["wake"]["direct"] == {"tier": "standard", "kind": "work", "reason": why,
                                           "cases": 6, "escalated": 6}
    assert len(calls) == 1 and p.db.kv(f"wake_direct_n:work:{why}") == 1
    # Off: the same wake stays light and carries no direct block.
    started.clear()
    p.set_config("waiting.wake_escalation.enabled", False)
    p.db.update_task(t, status="queued", result=json.dumps(waits))
    d = Daemon(p.base)
    d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    d.dispatch()
    ((a, k),) = started
    assert a[3] == "light" and "direct" not in k["note"]["wake"]

# Course check line for a running worker run past its estimate (course.over_estimate): tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def q(self, sql, params=()):
        return list(self.rows)


def _over_running(tid=7, kind="code", tier="standard", started=0.0):
    return {"id": tid, "title": f"task {tid}", "kind": kind, "tier": tier, "started": started}


def _hist(monkeypatch, run=None, kind=None):
    from ttp import eta
    monkeypatch.setattr(eta, "history", lambda db, now=None: {"run": run or {}, "kind": kind or {}})


def test_over_estimate_names_a_run_past_the_median_of_its_kind_and_tier(monkeypatch):
    from ttp import course
    _hist(monkeypatch, run={("code", "standard"): [600.0, 1200.0, 900.0]})
    now = 900.0 * 1.5 + 100   # median 900 s; limit 1350 s
    got = course.over_estimate(_FakeDB([_over_running(started=0.0)]), 0.5, 3, 15, now)
    assert [g["id"] for g in got] == [7] and got[0]["n"] == 3 and round(got[0]["median_min"]) == 15
    assert course.over_estimate(_FakeDB([_over_running(started=0.0)]), 0.5, 3, 15, 1300.0) == []   # still inside 50%


def test_over_estimate_needs_samples_a_floor_and_an_on_switch(monkeypatch):
    from ttp import course
    db = _FakeDB([_over_running(started=0.0)])
    _hist(monkeypatch, run={("code", "standard"): [600.0, 700.0]})
    assert course.over_estimate(db, 0.5, 3, 15, 5000.0) == []   # two samples do not measure a median
    _hist(monkeypatch, run={("code", "standard"): [60.0, 70.0, 80.0]})
    assert course.over_estimate(db, 0.5, 3, 15, 600.0) == []   # 10 min is under the 15 min floor
    assert [g["id"] for g in course.over_estimate(db, 0.5, 3, 15, 1000.0)] == [7]
    assert course.over_estimate(db, 0, 3, 15, 1e6) == []   # 0 turns the sign off


def test_over_estimate_falls_back_to_the_kind_and_skips_unmeasured_kinds(monkeypatch):
    from ttp import course
    rows = [_over_running(1, "code", "deep"), _over_running(2, "work", "standard")]
    _hist(monkeypatch, kind={"code": [100.0, 100.0, 100.0]})   # no deep-tier samples, kind samples exist
    got = course.over_estimate(_FakeDB(rows), 0.5, 3, 15, 2000.0)
    assert [g["id"] for g in got] == [1]   # the work run has no history: no line, never a guess
    _hist(monkeypatch)
    assert course.over_estimate(_FakeDB(rows), 0.5, 3, 15, 2000.0) == []
    assert course.over_estimate(_FakeDB([]), 0.5, 3, 15, 2000.0) == []

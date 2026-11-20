# Lanes on the same files line up and start early (overlapdep.py): tests of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

import contextlib
import json


class _P:
    def __init__(self, overlap=None, tasks=None, unmet=None):
        self.overlap = overlap or {}
        self.tasks = tasks or {}
        self.unmet = unmet or {}
        self.updates = []
        self.db = self

    def config(self):
        return {"overlap": self.overlap}

    def task(self, tid):
        return self.tasks.get(tid)

    def unmet_dependencies(self, ts):
        return {t["id"]: self.unmet.get(t["id"], []) for t in ts}

    def update_task(self, tid, **kw):
        self.updates.append((tid, kw))

    @contextlib.contextmanager
    def tx(self):
        yield

    def q(self, sql, params=()):
        return [t for t in self.tasks.values() if t["status"] == "queued" and t["kind"] == "code"
                and t["depends_on"] not in ("", "[]")]


def _t(tid, kind="code", status="queued", deps=(), labels=()):
    return {"id": tid, "kind": kind, "status": status, "depends_on": json.dumps(list(deps)),
            "labels": json.dumps(list(labels)), "spec": ""}


def _hit(tid, open_=True, exempt="", paths=(("a/b.py", False),)):
    h = {"id": tid, "open": open_, "paths": list(paths)}
    if exempt:
        h["exempt"] = exempt
    return h


def _no_chain(monkeypatch):
    from ttp import chaincap
    monkeypatch.setattr(chaincap, "chain", lambda db, r: [])


def test_overlapdep_auto_dependency_picks_newest_open_code_task_and_skips_bypasses(monkeypatch):
    from ttp import overlapdep
    _no_chain(monkeypatch)
    p = _P(tasks={7: _t(7, status="running"), 6: _t(6), 5: _t(5, kind="work"), 4: _t(4, status="done")})
    assert overlapdep.auto_dependency(p, "code", [_hit(7), _hit(6)], [])[0] == 7
    # bypasses: not a code task, no hits, off, already a dependency, closed or exempt hit, lineage, done/other kind
    assert overlapdep.auto_dependency(p, "work", [_hit(7)], []) is None
    assert overlapdep.auto_dependency(p, "code", [], []) is None
    assert overlapdep.auto_dependency(_P(overlap={"auto_depend": False}, tasks=p.tasks), "code", [_hit(7)], []) is None
    assert overlapdep.auto_dependency(p, "code", [_hit(7)], [7]) is None
    assert overlapdep.auto_dependency(p, "code", [_hit(7, open_=False)], []) is None
    assert overlapdep.auto_dependency(p, "code", [_hit(7, exempt="holds a device")], []) is None
    assert overlapdep.auto_dependency(p, "code", [_hit(7)], [], roots={7}) is None
    assert overlapdep.auto_dependency(p, "code", [_hit(5), _hit(4)], []) is None
    # an exempt newest hit never hides an older one that may be waited on
    assert overlapdep.auto_dependency(p, "code", [_hit(7, exempt="is blocked"), _hit(6)], [])[0] == 6


def test_overlapdep_settings_keep_defaults_for_wrong_types():
    from ttp import overlapdep
    s = overlapdep.settings(_P(overlap={"auto_depend": "no", "early_deps": 0, "split_hours": -1}))
    assert s == {"auto_depend": True, "early_deps": True, "split_hours": 2.0}
    assert overlapdep.settings(_P(overlap={"split_hours": 0.5}))["split_hours"] == 0.5


def test_overlapdep_early_start_needs_exactly_one_unmet_code_dependency_with_an_approved_head(monkeypatch):
    from ttp import lanewait, overlapdep, worktree
    monkeypatch.setattr(worktree, "branches_early", lambda p, t: True)
    monkeypatch.setattr(lanewait, "batched", lambda db, when: (9, 3, "abc") if when == "landed:#9" else None)
    mk = lambda **kw: _P(tasks={9: _t(9, status="pushing"), 8: _t(8), 20: _t(20, deps=(9,))}, **kw)
    p = mk(unmet={20: [9]})
    assert overlapdep.early_start(p, p, p.tasks[20]) == (9, 3, "abc")
    # bypasses: two unmet deps, a dep with no queue row, a dep that is not code, off, not queued, a deferred start
    p = mk(unmet={20: [9, 8]})
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    p = mk(unmet={20: [8]})
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    p = mk(unmet={20: [9]})
    p.tasks[9]["kind"] = "work"
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    p = mk(unmet={20: [9]})
    p.overlap = {"early_deps": False}
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    p = mk(unmet={20: [9]})
    p.tasks[20]["status"] = "running"
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    p = mk(unmet={20: [9]})
    p.tasks[20]["labels"] = json.dumps(["start_when:ttp landed --task 9"])
    assert overlapdep.early_start(p, p, p.tasks[20]) is None
    monkeypatch.setattr(worktree, "branches_early", lambda p, t: False)   # carries a branch: waits for the landing
    p = mk(unmet={20: [9]})
    assert overlapdep.early_start(p, p, p.tasks[20]) is None


def test_overlapdep_start_early_drops_only_that_dependency_and_labels_the_start(monkeypatch):
    from ttp import lanewait, overlapdep, worktree
    monkeypatch.setattr(worktree, "branches_early", lambda p, t: True)
    monkeypatch.setattr(lanewait, "batched", lambda db, when: (9, 3, "abc"))
    p = _P(tasks={9: _t(9, status="review"), 8: _t(8, status="done"), 20: _t(20, deps=(8, 9))}, unmet={20: [9]})
    assert overlapdep.start_early(p, p) == [20]
    tid, kw = p.updates[0]
    assert tid == 20 and kw["depends_on"] == [8]
    assert "start_from:abc" in kw["labels"] and "early_start:9:3" in kw["labels"]
    p = _P(tasks={20: _t(20, deps=(9,))}, unmet={20: [9, 8]})
    assert overlapdep.start_early(p, p) == [] and not p.updates


def test_overlapdep_split_note_is_advice_over_the_history_estimate():
    from ttp import overlapdep
    hist = {"run": {("code", "standard"): [3 * 3600.0, 4 * 3600.0, 5 * 3600.0]}, "kind": {"code": [600.0]},
            "cell": {"code": [1800.0]}}
    p = _P()
    assert "4.0 h" in overlapdep.split_note(p, "code", "standard", "do it", hist)
    # bypasses: a short kind/tier, an unknown tier falling back to the kind median, other kinds, off, no history
    assert overlapdep.split_note(p, "code", "light", "do it", {"run": {}, "kind": {"code": [600.0]}, "cell": {}}) == ""
    assert overlapdep.split_note(p, "code", "deep", "do it", hist) == ""   # kind median is short: no note
    assert overlapdep.split_note(p, "question", "standard", "do it", hist) == ""
    assert overlapdep.split_note(_P(overlap={"split_hours": 0}), "code", "standard", "x", hist) == ""
    assert overlapdep.split_note(p, "code", "standard", "x", {"run": {}, "kind": {}, "cell": {}}) == ""
    # cells times the median cell seconds beat the run median
    assert "2.5 h" in overlapdep.split_note(p, "code", "light", "cells: a, b, c, d, e", hist | {"cell": {"code": [1800.0]}})

# The independent check of a big ETA (etacheck.py): tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there).
from __future__ import annotations

import json


class _CheckDB:
    """Just the task table the checker reads and writes."""
    def __init__(self):
        self.rows = []

    def add_task(self, title, spec="", **kw):
        row = {"id": len(self.rows) + 100, "title": title, "spec": spec, "status": "queued", "result": None,
               "labels": json.dumps(kw.pop("labels", [])), **kw}
        self.rows.append(row)
        return row["id"]

    def q(self, sql, params=()):
        pat = str(params[0]).strip("%")
        return sorted((r for r in self.rows if pat in r["labels"]), key=lambda r: -r["id"])

    def task(self, tid):
        return {"id": tid, "title": f"task {tid}", "kind": "code", "status": "queued", "tier": "standard",
                "spec": "Where: a/b.py, c.md"}


class _Est:
    def task(self, tid):
        return {"why": "", "end": 1000.0, "path": [tid], "legs": [], "overrun": False}


def _cand(owners=(1, 2, 3), oid="o7"):
    return {"id": oid, "owners": list(owners), "why": f"covers {len(owners)} tasks", "end": 1000.0,
            "est": _Est(), "o": {"id": oid, "text": "ship the thing"}}


def _fake_eta(monkeypatch):
    from ttp import eta
    monkeypatch.setattr(eta, "obligation_estimate", lambda db, o, now, est: {"why": "", "end": 1000.0, "path": [1], "legs": [], "overrun": False})
    monkeypatch.setattr(eta, "field", lambda e, where, now=None: "ETA 10:00 PST (critical path #1)")


def test_etacheck_qualifies_by_task_count_or_hours_and_can_be_off():
    from ttp import etacheck
    assert etacheck.qualifies([1, 2, 3], None, 0.0, None) == "covers 3 tasks"
    assert etacheck.qualifies([1, 2], 4.5 * 3600, 0.0, None).startswith("finishes in 4.5 h")
    assert etacheck.qualifies([1, 2], 4 * 3600, 0.0, None) == "" and etacheck.qualifies([1], None, 0.0, None) == ""
    assert etacheck.qualifies([], 9 * 3600, 0.0, None) == ""
    assert etacheck.qualifies([1, 2, 3], None, 0.0, {"eta": {"enabled": False}}) == ""
    # a setting of the wrong type keeps its default instead of disabling or loosening the check
    assert etacheck.qualifies([1, 2, 3], None, 0.0, {"eta": {"min_tasks": "x", "enabled": "no"}}) == "covers 3 tasks"
    assert etacheck.qualifies([1, 2], None, 0.0, {"eta": {"min_tasks": True}}) == ""   # a bool is no count


def test_etacheck_route_is_the_review_route_and_never_max():
    from ttp import etacheck
    assert etacheck.route(None) == ({"tier": "standard"}, "")
    assert etacheck.route({"eta": {"check_route": "light"}}) == ({"tier": "light"}, "")
    for bad in ("deep", "MAX", "xhigh-everything"):   # a string route that is no review route or tier falls back
        pins, why = etacheck.route({"eta": {"check_route": bad}})
        assert pins == {"tier": "standard"} and "review route" in why
    for effort in ("max", " MAX ", "Max"):   # case and padding never get a max checker through
        pins, why = etacheck.route({"eta": {"check_route": {"tier": "standard", "model": "m", "effort": effort}}})
        assert "effort" not in pins and pins["model"] == "m" and "never runs at max" in why
    pins, why = etacheck.route({"eta": {"check_route": {"tier": " Deep ", "effort": "high", "provider": "p"}}})
    assert pins == {"tier": "standard", "provider": "p", "effort": "high"} and "light or standard" in why
    assert etacheck.route({"eta": {"check_route": {"model": "m", "effort": "high"}}})[0] == {"tier": "standard", "model": "m", "effort": "high"}


def test_etacheck_queues_one_urgent_review_per_owner_set_and_retries_a_failure_once(monkeypatch):
    from ttp import etacheck
    _fake_eta(monkeypatch)
    db = _CheckDB()
    tid, line = etacheck.queue(db, _cand(), None, 0.0, None)
    row = db.rows[0]
    assert tid == 100 and "ETA check #100 queued for o7" in line and "ETA 10:00 PST" in line
    assert row["kind"] == "review" and row["priority"] == 1 and row["tier"] == "standard" and row["origin"] == "daemon"
    assert row["spec"].startswith("urgent:") and "ship the thing" in row["spec"] and "files: a/b.py, c.md" in row["spec"]
    assert etacheck.label("o7", [3, 2, 1]) in row["labels"] and '"eta_check"' not in row["labels"]
    assert etacheck.queue(db, _cand(), None, 0.0, None) == (None, "")                  # idempotent
    assert etacheck.queue(db, _cand((1, 2, 3, 4)), None, 0.0, None)[0] == 101           # a changed owner set is checked again
    db.rows[0]["status"] = "failed"
    assert etacheck.queue(db, _cand(), None, 0.0, None)[0] == 102                      # a failed check is retried once
    db.rows[2]["status"] = "failed"
    assert etacheck.queue(db, _cand(), None, 0.0, None) == (None, "")                  # then it is the coordinator's
    assert etacheck.queue(db, _cand(oid="o8"), None, 0.0, None)[0] == 103              # another order has its own


def test_etacheck_verdict_reads_only_a_well_formed_eta_check():
    from ttp import etacheck
    good = {"result": json.dumps({"eta_check": {"verdict": "Optimistic", "proposals": ["split #5", "", "run #6 first"] + ["x"] * 9}})}
    got = etacheck.verdict(good)
    assert got["verdict"] == "optimistic" and got["proposals"][:2] == ["split #5", "run #6 first"] and len(got["proposals"]) == 5
    for bad in (None, "", "not json", json.dumps({"eta_check": "ok"}), json.dumps({"eta_check": {"verdict": "great", "proposals": "x"}})):
        assert etacheck.verdict({"result": bad}) == {"verdict": "unclear", "proposals": []}


def test_etacheck_state_shows_each_open_order_check_and_nothing_for_a_closed_one(monkeypatch):
    from ttp import etacheck, obligations
    _fake_eta(monkeypatch)
    db = _CheckDB()
    etacheck.queue(db, _cand(oid="o7"), None, 0.0, None)
    etacheck.queue(db, _cand(oid="o8"), None, 0.0, None)
    db.rows[1]["status"] = "done"
    db.rows[1]["result"] = json.dumps({"eta_check": {"verdict": "pessimistic", "proposals": ["start #4 now"]}})
    state = {"o7": {"id": "o7", "receipt": None}, "o8": {"id": "o8", "receipt": None}}
    monkeypatch.setattr(obligations, "get", lambda db, oid: state.get(oid))
    out = etacheck.state_lines(db, None, 0.0)
    assert out[0].startswith("## ETA checks") and "carry its verdict" in out[0]
    assert "- o7: preliminary ETA, checker #100 queued" in out and "- o8: checker #101 says pessimistic; shorter: start #4 now" in out
    state["o8"]["receipt"] = "done"   # a delivered order shows no check
    assert etacheck.state_lines(db, None, 0.0)[1:] == ["- o7: preliminary ETA, checker #100 queued"]
    state.clear()
    assert etacheck.state_lines(db, None, 0.0) == []


def test_etacheck_files_come_only_from_where_lines_and_are_capped():
    from ttp import etacheck
    assert etacheck.files_of("no where line mentions a/b.py") == []
    spec = "Where: a/b.py, c.md, a/b.py\nWhere: d/e/f.py (the hook), x1.py x2.py x3.py"
    assert etacheck.files_of(spec) == ["a/b.py", "c.md", "d/e/f.py", "x1.py", "x2.py", "x3.py"]
    assert etacheck.files_of(spec, 2) == ["a/b.py", "c.md"] and etacheck.files_of(spec, 0) == []

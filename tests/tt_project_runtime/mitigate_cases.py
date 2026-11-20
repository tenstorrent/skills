# Interim mitigations for live impact (runtime/ttp/mitigate.py). Run from test_tt_project_runtime.py.


def _due_h(p, hours):
    cfg = json.loads(p.config_path.read_text())
    cfg.setdefault("coordinator", {})["mitigation_due_h"] = hours
    p.config_path.write_text(json.dumps(cfg))


def _mitigations(p):
    return p.db.q("SELECT * FROM tasks WHERE labels LIKE '%\"mitigation\"%' ORDER BY id")


def test_a_hand_off_stating_live_impact_gets_a_due_mitigation_beside_its_fix(env):
    from ttp import mitigate
    p = make(env)
    # Without the structured field nothing extra is filed, whatever the free text says.
    tid, _, _ = _hand_off(env, p, {"status": "done", "summary": "the service is down because of a known defect",
                                   "followups": [{"title": "fix hold escalation", "spec": "patch it"}]})
    assert _mitigations(p) == [] and p.db.kv(mitigate.KEY) is None
    # A follow-up that carries live_impact: the fix stays a proposed follow-up, and a mitigation is queued.
    tid, _, _ = _hand_off(env, p, {"status": "done", "summary": "found it", "followups": [
        {"title": "fix hold escalation", "spec": "patch the service",
         "live_impact": "device-broker holds of one class never escalate"}]})
    (m,) = _mitigations(p)
    assert m["priority"] == 1 and m["status"] == "queued" and m["kind"] == "code" and m["parent"] == tid
    assert m["title"].startswith("mitigate: device-broker: holds of one class never escalate")
    spec = " ".join(m["spec"].split())
    for part in ("Interim mitigation, due ", "Live impact now: device-broker: holds of one class never escalate",
                 f"Known defect reported by #{tid}", "the proposed follow-up 'fix hold escalation'",
                 "least-disruptive safe workaround the charter or memory already approves",
                 "`ttp machines change add <alias>", "--undo", "`temporary_measures`"):
        assert part in spec, part
    rec = p.db.kv(mitigate.KEY)[str(m["id"])]
    assert rec["of"] == tid and rec["resource"] == "device-broker" and rec["due"] - rec["filed"] == mitigate.DUE_H * 3600
    assert p.db.one("SELECT 1 FROM events WHERE kind='followup_proposed' AND task=?", (tid,))
    assert p.db.one("SELECT status FROM events WHERE kind=? AND task=?", (mitigate.QUEUED, m["id"]))["status"] == "handled"
    # The same resource while that mitigation is open: no second one. Its own hand-off files none either.
    _hand_off(env, p, {"status": "blocked", "summary": "x", "question": "q",
                       "live_impact": {"resource": "device-broker", "what": "still degraded"}})
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(m["id"], status="running")
    run_dir = env["tmp"] / "run-m"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "restarted",
                                                     "live_impact": "other-service slow"}))
    Daemon(p.base)._finish_worker({"task": m["id"]}, Usage(cost_usd=1.0), "ok", run_dir)
    assert [t["id"] for t in _mitigations(p)] == [m["id"]]


def test_an_ask_from_a_code_task_with_live_impact_names_that_task_as_the_fix(env):
    from ttp import mitigate
    p = make(env)
    _due_h(p, 2)
    tid, _, _ = _hand_off(env, p, {"status": "blocked", "summary": "fix ready, needs a decision", "question": "which?",
                                   "live_impact": ["queue-service jobs stall after a reset", "bad"]}, kind="code")
    (m,) = _mitigations(p)
    spec = " ".join(m["spec"].split())
    assert f"Fix: #{tid} hand-off (its review, push and deploy are still ahead)" in spec
    assert f"--until-task {tid}`" in spec
    rec = p.db.kv(mitigate.KEY)[str(m["id"])]
    assert rec["fix"] == tid and rec["due"] - rec["filed"] == 2 * 3600
    # Off at 0.
    _due_h(p, 0)
    _hand_off(env, p, {"status": "done", "summary": "s", "live_impact": "another-service down"})
    assert len(_mitigations(p)) == 1


def test_an_overdue_mitigation_wakes_the_coordinator_and_a_done_fix_lists_its_measures_for_undo(env):
    from ttp import coordinator as coord, mitigate, sweep
    from ttp.cli import status_text
    p = make(env)
    fix = p.db.add_task("fix hold escalation", "patch", kind="code", tier="standard", origin="coordinator")
    tid, _, _ = _hand_off(env, p, {"status": "done", "summary": "s", "followups": [
        {"title": "Fix hold escalation", "spec": "patch", "live_impact": "svc holds stay"}]})
    # A fix task queued before the report is not taken for the one it proposes.
    p.db.update_task(fix, status="done")
    (m,) = _mitigations(p)
    rec = p.db.kv(mitigate.KEY)[str(m["id"])]
    now = rec["filed"] + 60
    # Not due yet: the sweep lists it with its due time and needs no outcome; status shows it.
    (item,) = [i for i in sweep.items(p.db, now) if i["task"] == m["id"]]
    assert item["kind"] == "mitigation" and item["ends"] == "due" and "mitigation due " in item["reason"]
    assert "OVERDUE" not in item["reason"]
    assert f"#{m['id']} queued, mitigation due " in status_text(p)
    assert mitigate.tend(p.db, p.name, now=now) == []
    # Past due and not done: one high event that wakes the coordinator for a tricky turn, once.
    late = rec["due"] + 1
    assert mitigate.tend(p.db, p.name, now=late) == [f"mitigation #{m['id']} overdue"]
    assert mitigate.tend(p.db, p.name, now=late + 600) == []
    ev = p.db.one("SELECT * FROM events WHERE kind=?", (mitigate.OVERDUE,))
    assert ev["severity"] == "high" and ev["status"] == "queued" and "OVERDUE" in ev["text"]
    assert mitigate.OVERDUE in coord.EFFORT_EVENT_TRIGGERS
    (item,) = [i for i in sweep.items(p.db, late) if i["task"] == m["id"]]
    assert item["ends"] is None and "OVERDUE" in item["reason"]
    # A blocked mitigation keeps its sweep item, with the due time in its reason.
    p.db.update_task(m["id"], status="blocked", blocked_reason="no approved workaround")
    (item,) = [i for i in sweep.items(p.db, late) if i["task"] == m["id"]]
    assert item["kind"] == "blocked" and item["reason"].startswith("mitigation due ") and "no approved" in item["reason"]
    # Done, with temporary measures; nothing to undo until the fix is done too.
    p.db.update_task(m["id"], status="done", result=json.dumps(
        {"status": "done", "summary": "escalated by hand",
         "temporary_measures": [{"what": "manual escalation timer", "undo": "remove the timer"}]}))
    assert mitigate.tend(p.db, p.name, now=late + 700) == []
    assert not any(i["task"] == m["id"] for i in sweep.items(p.db, late + 700))
    real = p.db.add_task("Fix hold escalation", "patch", kind="code", tier="standard", origin="coordinator")
    assert mitigate.tend(p.db, p.name, now=late + 800) == []
    p.db.update_task(real, status="done")
    assert mitigate.tend(p.db, p.name, now=late + 900) == [f"mitigation #{m['id']} undo listed"]
    assert mitigate.tend(p.db, p.name, now=late + 1000) == []
    ev = p.db.one("SELECT * FROM events WHERE kind=?", (mitigate.UNDO,))
    assert ev["status"] == "queued" and f"Fix #{real} " in ev["text"] and "Once it is deployed" in ev["text"]
    assert "manual escalation timer (undo: remove the timer)" in ev["text"]


def test_an_overdue_mitigation_whose_fix_is_already_done_raises_no_overdue_event(env):
    from ttp import mitigate
    p = make(env)
    tid, _, _ = _hand_off(env, p, {"status": "done", "summary": "fixed", "live_impact": "svc jobs stall"}, kind="code")
    (m,) = _mitigations(p)
    rec = p.db.kv(mitigate.KEY)[str(m["id"])]
    assert rec["fix"] == tid and p.db.task(tid)["status"] == "done"
    assert mitigate.tend(p.db, p.name, now=rec["due"] + 1) == []
    assert not p.db.q("SELECT 1 FROM events WHERE kind=?", (mitigate.OVERDUE,))
    # A fix still in flight: the overdue mitigation is raised.
    fix, _, _ = _hand_off(env, p, {"status": "blocked", "summary": "s", "question": "q",
                                   "live_impact": "other-svc slow"}, kind="code")
    (m2,) = [t for t in _mitigations(p) if t["id"] != m["id"]]
    rec2 = p.db.kv(mitigate.KEY)[str(m2["id"])]
    assert mitigate.tend(p.db, p.name, now=rec2["due"] + 1) == [f"mitigation #{m2['id']} overdue"]


def test_tend_reads_and_writes_the_mitigations_in_one_transaction(env, monkeypatch):
    from ttp import mitigate
    p = make(env)
    _hand_off(env, p, {"status": "blocked", "summary": "s", "question": "q", "live_impact": "svc down"}, kind="code")
    (m,) = _mitigations(p)
    due = p.db.kv(mitigate.KEY)[str(m["id"])]["due"]
    seen = []
    real_records, real_set = mitigate.records, p.db.set_kv
    monkeypatch.setattr(mitigate, "records", lambda db: seen.append(("read", db.conn.in_transaction)) or real_records(db))
    monkeypatch.setattr(p.db, "set_kv", lambda *a, **k: seen.append(("write", p.db.conn.in_transaction)) or real_set(*a, **k))
    assert mitigate.tend(p.db, p.name, now=due + 1) == [f"mitigation #{m['id']} overdue"]
    assert seen == [("read", True), ("write", True)]


def test_a_live_impact_with_no_fix_queued_tells_the_coordinator_to_queue_one(env):
    from ttp import mitigate
    p = make(env)
    tid, _, queued = _hand_off(env, p, {"status": "done", "summary": "saw it", "live_impact": "svc holds stay"})
    (m,) = _mitigations(p)
    ev = p.db.one("SELECT * FROM events WHERE kind=? AND task=?", (mitigate.QUEUED, m["id"]))
    assert ev["status"] == "queued" and "none queued yet" in ev["text"] and ev["text"].endswith("Queue the fix task.")
    # A code task stating it is the fix itself: nothing for the coordinator to do.
    _hand_off(env, p, {"status": "done", "summary": "s", "live_impact": "other-svc slow"}, kind="code")
    (m2,) = [t for t in _mitigations(p) if t["id"] != m["id"]]
    assert p.db.one("SELECT status FROM events WHERE kind=? AND task=?", (mitigate.QUEUED, m2["id"]))["status"] == "handled"

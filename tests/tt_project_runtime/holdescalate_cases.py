# Hold escalation tests (holdescalate.run, selfblock.raise_due) of tests/test_tt_project_runtime.py, which runs
# this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that file
# (_bl_task is blockledger_cases.py's). Add new hold-escalation tests here.
from __future__ import annotations


def _he_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='hold_escalate' ORDER BY id")


def _he_hold(p, now, minutes, reason, priority=1, title="ordered work"):
    return _bl_task(p, title, "blocked", now, priority=priority, blocked_ago=minutes * 60, reason=reason)


def test_hold_escalate_never_clears_a_safety_hold(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    held = {_he_hold(p, now, 40, r, title=r): r for r in (
        "held by R-ab12 until later", "needs your credentials to continue", "a charter Restriction forbids it",
        "waits on resource:dev-box", "paused by the user until they say so")}
    for step in (0, 600, 3600):
        he.run(p.db, {}, now + step)
    assert {p.db.task(t)["status"] for t in held} == {"blocked"}
    texts = [e["text"] for e in _he_events(p)]
    assert len(texts) == 3 and all("(safety: " in t and "proceeds" not in t for t in texts)
    assert not any("resource" in t or "paused" in t for t in texts)   # a lock or a user pause shows, no wake


def test_hold_escalate_raises_a_convenience_hold_at_five_minutes_and_clears_it_at_ten(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    t = _he_hold(p, now, 6, "waiting for the shipping window to open")
    assert [d["did"] for d in he.run(p.db, {}, now)] == ["escalated"]
    ev = _he_events(p)
    assert len(ev) == 1 and "held 6 min" in ev[0]["text"] and "proceeds at" in ev[0]["text"] and "unless you cite a safety gate" in ev[0]["text"]
    assert he.run(p.db, {}, now + 60) == [] and p.db.task(t)["status"] == "blocked"
    assert [d["did"] for d in he.run(p.db, {}, now + 301)] == ["proceeded"]
    task = p.db.task(t)
    assert task["status"] == "queued" and not task["blocked_reason"]
    assert "auto-proceeded past for the shipping window to open after 11 min (holdescalate)" in task["spec"]
    assert p.db.kv(f"holdescalate:proceeded:{t}")["minutes"] == 11
    from ttp import blockledger as bl
    assert any(r["rule"] == "auto-proceed" and r["task"] == t for r in bl.rows(p.db, now - 86400, now + 400))
    assert he.run(p.db, {}, now + 900) == [] and len(_he_events(p)) == 1   # one escalation per hold episode


def test_hold_escalate_clears_a_deferral_and_stops_on_a_safety_cite(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    d = p.db.add_task("deferred order")
    p.db.x("UPDATE tasks SET priority=1, labels=? WHERE id=?",
           (json.dumps([f"start_after:{now + 86400}", "start_why:nightly window", f"deferred_since:{now - 600}"]), d))
    held = _he_hold(p, now, 8, "waiting for the shipping window to open", title="held")
    he.run(p.db, {}, now)
    p.db.update_task(held, blocked_reason="held by R-ab12 until later")   # a safety cite added after the escalation
    he.run(p.db, {}, now + 400)
    assert p.db.task(held)["status"] == "blocked"
    task = p.db.task(d)
    assert task["status"] == "queued" and json.loads(task["labels"]) == []
    assert any(e["status"] == "queued" and f"#{d} " in e["text"] for e in _he_events(p))


def test_hold_escalate_leaves_routine_work_open_dependencies_asks_and_review_alone(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    routine = _he_hold(p, now, 90, "waiting for the shipping window to open", priority=3, title="routine")
    root = p.db.add_task("root")
    dep = _he_hold(p, now, 90, "waiting for the shipping window to open", title="dependent")
    p.db.update_task(dep, depends_on=[root])
    ask = _he_hold(p, now, 90, "ask:12", title="asked")
    rev = _bl_task(p, "in review", "review", now, priority=1, blocked_ago=5400, kind="task_review", reason="review")
    assert he.run(p.db, {}, now) == [] and he.run(p.db, {}, now + 900) == []
    assert [p.db.task(i)["status"] for i in (routine, dep, ask, rev)] == ["blocked", "blocked", "blocked", "review"]


def test_hold_escalate_config_turns_it_off_or_stops_at_the_event(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    t = _he_hold(p, now, 30, "waiting for the shipping window to open")
    assert he.run(p.db, {"hold_escalate_min": 0}, now) == [] and not _he_events(p)
    assert [d["did"] for d in he.run(p.db, {"hold_proceed_min": 0}, now)] == ["escalated"]
    assert "proceeds at" not in _he_events(p)[0]["text"]
    assert he.run(p.db, {"hold_proceed_min": 0}, now + 3600) == [] and p.db.task(t)["status"] == "blocked"


def test_hold_escalate_runs_from_the_selfblock_scan_and_is_a_stuck_event(env):
    from ttp import coordinator, selfblock
    p = make(env)
    now = time.time()
    _he_hold(p, now, 30, "waiting for the shipping window to open")
    selfblock.raise_due(p.db, now)
    assert len(_he_events(p)) == 1 and coordinator.EFFORT_EVENT_TRIGGERS["hold_escalate"] == "stuck"


def test_hold_escalate_proceeds_once_per_run_and_leaves_a_running_or_capped_task(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    t = _he_hold(p, now, 6, "task budget reached, split the change")
    he.run(p.db, {}, now)
    assert [d["did"] for d in he.run(p.db, {}, now + 301)] == ["proceeded"]
    p.db.x("UPDATE tasks SET status='blocked', blocked_reason='task budget reached, split the change' WHERE id=?", (t,))
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now + 400, "t", "task_blocked", f"#{t} ordered work is blocked: task budget reached", None, "handled", t))
    for step in (800, 1200, 2400):   # the budget gate put it back and no run started: no second proceed, no loop
        assert he.run(p.db, {}, now + step) == []
    assert p.db.task(t)["status"] == "blocked" and len(_he_events(p)) == 1
    capped = _he_hold(p, now, 6, "waiting for the shipping window to open", title="capped")
    p.db.x("UPDATE tasks SET status='failed', attempts=3, max_attempts=3 WHERE id=?", (capped,))
    running = _he_hold(p, now, 6, "waiting for the shipping window to open", title="running")
    p.db.x("UPDATE tasks SET status='running' WHERE id=?", (running,))
    assert he._proceed(p.db, p.db.task(capped), {"kind": "blocked"}, "r", 9, now) is False
    assert he._proceed(p.db, p.db.task(running), {"kind": "blocked"}, "r", 9, now) is False
    assert p.db.task(capped)["status"] == "failed" and p.db.task(running)["status"] == "running"
    assert "auto-proceeded" not in (p.db.task(running)["spec"] or "") and p.db.kv(f"holdescalate:proceeded:{running}") is None


def test_hold_escalate_defaults_are_the_project_config_defaults(monkeypatch):
    import importlib
    from ttp import holdescalate, project
    monkeypatch.setitem(project.DEFAULT_CONFIG["coordinator"], "hold_escalate_min", 9)
    monkeypatch.setitem(project.DEFAULT_CONFIG["coordinator"], "hold_proceed_min", 7)
    try:
        assert importlib.reload(holdescalate).DEFAULTS == {"hold_escalate_min": 9, "hold_proceed_min": 7}
    finally:
        monkeypatch.undo()
        importlib.reload(holdescalate)
    assert holdescalate.DEFAULTS == {k: project.DEFAULT_CONFIG["coordinator"][k] for k in holdescalate.DEFAULTS}


def test_hold_escalate_treats_a_start_when_probe_as_an_outside_wait(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    d = p.db.add_task("rebase after the merge")
    labels = [f"start_when:test -e {p.base}/merged", "start_why:rebases only after the other PR has merged into main",
              f"deferred_since:{now - 3600}"]
    p.db.x("UPDATE tasks SET priority=1, labels=? WHERE id=?", (json.dumps(labels), d))
    out = [he.run(p.db, {}, now + step) for step in (0, 600, 3600)]
    assert out[0] == [{"task": d, "rule": out[0][0]["rule"], "minutes": 60, "safety": he.OUTSIDE, "did": "shown"}]
    assert out[1] == out[2] == [] and not _he_events(p)
    assert p.db.task(d)["status"] == "queued" and json.loads(p.db.task(d)["labels"]) == labels


def test_hold_escalate_raises_one_hold_once_though_it_is_recorded_again(env):
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    cfg = {"hold_proceed_min": 0}
    t = _he_hold(p, now, 6, "waiting for the shipping window to open")
    assert [d["did"] for d in he.run(p.db, cfg, now)] == ["escalated"]
    p.db.x("UPDATE tasks SET status='waiting' WHERE id=?", (t,))   # re-recorded: a new segment, so a later hold start
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now + 30, "t", "task_waiting", f"#{t} ordered work is waiting: waiting for the shipping window to open",
            None, "handled", t))
    assert he.run(p.db, cfg, now + 400) == [] and len(_he_events(p)) == 1
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?,?,?,?,?)", (t, "worker", now + 500, now + 520, "done"))
    p.db.x("UPDATE tasks SET status='blocked' WHERE id=?", (t,))
    p.db.x("INSERT INTO events(ts,source,kind,text,data,status,task) VALUES(?,?,?,?,?,?,?)",
           (now + 900, "t", "task_blocked", f"#{t} ordered work is blocked: waiting for the shipping window to open",
            None, "handled", t))
    assert [d["did"] for d in he.run(p.db, cfg, now + 1300)] == ["escalated"]   # a run came between: a new hold


def test_a_start_after_deferral_on_outside_data_is_never_proceeded(env):
    """A start_after deferral whose why names an outside fact (data that must accumulate, a cited
    user message) is an outside wait, like a start_when probe: shown, never auto-proceeded. The
    task_update deferral check keeps the same gate through the same classifier."""
    from ttp import convgate, holdescalate as he
    from ttp.coordinator import deferral_update
    p = make(env)
    now = time.time()
    why = ("needs data that must accumulate: the after-figure is the full 24 h window, "
           "like-for-like with the 24 h before-figure (user msg #1)")
    t = p.db.add_task("compare the before and after figures")
    p.db.x("UPDATE tasks SET priority=1 WHERE id=?", (t,))
    upd, after, when, note = deferral_update(p.db, p.db.task(t), {"start_after": "23h", "why": why}, p)
    assert "kept" in note and "dropped" not in note          # the task_update check keeps the gate
    p.db.update_task(t, **upd)
    assert any(str(lb).startswith(f"start_why:{why}") for lb in json.loads(p.db.task(t)["labels"])), upd["labels"]
    assert convgate.safety_class(why, None) is not None      # one classifier serves both callers
    assert convgate.safety_class("needs data that must accumulate first") == "data that must accumulate"
    assert he._hold(p.db, p.db.task(t), now)["cls"] == convgate.safety_class(why, None)
    out = [he.run(p.db, {}, now + step) for step in (3600, 3900, 7200)]
    assert [r["did"] for rows in out for r in rows] == ["shown"] and out[0][0]["safety"] == he.OUTSIDE
    task = p.db.task(t)
    assert task["status"] == "queued" and "auto-proceeded" not in (task["spec"] or "")
    assert any(str(lb).startswith("start_after:") for lb in json.loads(task["labels"]))
    assert not _he_events(p)


def test_a_start_after_deferral_with_no_outside_reason_still_proceeds(env):
    """A start_after deferral whose why names no outside fact stays a convenience hold: it
    escalates and proceeds after hold_proceed_min, as before."""
    from ttp import convgate, holdescalate as he
    p = make(env)
    now = time.time()
    t = p.db.add_task("tidy the notes")
    p.db.x("UPDATE tasks SET priority=1, labels=? WHERE id=?",
           (json.dumps([f"start_after:{now + 8 * 3600:.0f}", "start_why:later",
                        f"deferred_since:{now - 600}"]), t))
    assert convgate.safety_class("later", None) is None
    assert he._hold(p.db, p.db.task(t), now).get("cls") is None
    assert [d["did"] for d in he.run(p.db, {}, now)] == ["escalated"]
    assert [d["did"] for d in he.run(p.db, {}, now + 301)] == ["proceeded"]
    task = p.db.task(t)
    assert "auto-proceeded" in (task["spec"] or "") and json.loads(task["labels"]) == []


def test_a_start_after_deferral_the_check_kept_is_never_proceeded_without_a_stored_why(env):
    """The deferral check records the class it kept (`start_safety`); a start_after held that way
    stays an outside wait for hold-escalate though no why was stored (an older deferral, or a why
    cut at START_WHY_CHARS before its cite)."""
    from ttp import holdescalate as he
    p = make(env)
    now = time.time()
    t = p.db.add_task("compare the before and after figures")
    p.db.x("UPDATE tasks SET priority=1, labels=? WHERE id=?",
           (json.dumps([f"start_after:{now + 8 * 3600:.0f}", "start_safety:the user's word",
                        f"deferred_since:{now - 3600}"]), t))
    assert [d["did"] for d in he.run(p.db, {}, now) + he.run(p.db, {}, now + 600)] == ["shown"]
    assert p.db.task(t)["status"] == "queued" and "auto-proceeded" not in (p.db.task(t)["spec"] or "")

# Missing-entry and promise-slack tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers come from that file and eta_cases.py (_eta_history,
# _eta_promise), which sorts before it. Add new tests of runtime/ttp/missingentry.py and
# obligations._check_slack here.
from __future__ import annotations

from types import SimpleNamespace


def test_a_missing_entry_note_wakes_the_coordinator_with_a_must_act_item_at_once(env, monkeypatch):
    """A guard refused a step the user ordered. The worker's structured note becomes one
    must-act item in the order's chat that skips every wake hold; a config_set or a task_update of the
    owner acts on it, a plain reply does not."""
    from ttp import cli, eventhold, missingentry, mustact
    p = make(env)
    _no_events(p)
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled,chat) VALUES(?,?,?,?,?,?)",
                 (time.time(), "in", "user", "reset the box now", 1, "c7"))
    t = p.db.add_task("reset the box", "s", kind="work", tier="light", origin="user")
    run = env["home"] / "run-me"
    run.mkdir(parents=True)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(t))
    cli.cmd_note(SimpleNamespace(text=f"missing-entry: guards.allow_commands | tt-smi -r | order #{mid}", to=None))
    cli.cmd_note(SimpleNamespace(text=f"missing-entry: guards.allow_commands | tt-smi -r | order #{mid}", to=None))
    cli.cmd_note(SimpleNamespace(text="plain progress", to=None))
    bot = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled,chat) VALUES(?,?,?,?,?,?)",
                 (time.time(), "out", "reply", "queued", 1, "c7"))
    cli.cmd_note(SimpleNamespace(text=f"missing-entry: guards.allow_commands | rm -rf x | order #{bot}", to=None))
    cli.cmd_note(SimpleNamespace(text="missing-entry: guards.allow_commands | rm -rf x | order #99999", to=None))
    evs = p.db.q("SELECT * FROM events WHERE kind=?", (missingentry.KIND,))
    assert len(evs) == 1, "one item per rule and entry; none for an order id naming no message from the user"
    ev = evs[0]
    assert ev["task"] == t and ev["severity"] == "high" and eventhold.immediate(dict(ev))
    it = mustact.item(ev)
    assert it["key"] == f"missing-entry-{t}" and it["chat"] == "c7" and it["tasks"] == [t]
    block = mustact.block(p.db, [ev["id"]])
    assert "## Must act now" in block and "tt-smi -r" in block and "guards.allow_commands" in block
    assert "add it in this turn" in block and "machines.allowed" in block
    assert mustact.acted(it, [{"type": "config_set", "key": "guards.allow_commands"}], {0})
    assert mustact.acted(it, [{"type": "task_update", "id": t, "status": "queued"}], {0})
    assert not mustact.acted(it, [{"type": "reply", "chat": "c7", "text": "on it"}], {0})
    assert missingentry.parse("missing-entry: `delivery.push_allowed` | true | user msg 12") == {
        "rule": "delivery.push_allowed", "entry": "true", "order": 12}
    assert missingentry.parse("no missing entry here") is None


def test_the_prompts_tell_workers_to_report_a_missing_entry_and_the_coordinator_to_add_it():
    worker = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    coord = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert 'ttp note "missing-entry: <setting or file> | <entry> |' in worker
    assert "`missing-entry-<task>`" in coord and "machines.allowed" in coord


def _slack_events(p, oid):
    return p.db.q("SELECT * FROM events WHERE kind='promise_slack' AND fingerprint LIKE ? ORDER BY id",
                  (f"obligation:{oid}:promise:slack%",))


def test_a_short_task_promised_far_out_alarms_until_it_starts(env):
    """About 2 min of work promised 40 min out, unstarted, is a must-act item; it clears once
    the task starts on its host. 10 min out, or 2 h of work promised 3 h out, is fine."""
    from ttp import eventhold, mustact, obligations as ob
    p = make(env)
    _no_events(p)
    now = time.time()
    _eta_history(p, now, kind="work", runs=(100, 120, 140))
    t = p.db.add_task("reset the box", "s", kind="work", priority=1, reply_chat="c1")
    o = _eta_promise(p, t, "c1", "I'll post the result here in 40 minutes.", now)
    ob.check(p, now + 60)
    [ev] = _slack_events(p, o["id"])
    assert ev["status"] == "queued" and eventhold.immediate(dict(ev))
    assert "40 min" in ev["text"] and "about 2 min of work" in ev["text"] and "`justify`" in ev["text"]
    assert mustact.item(ev)["key"] == o["id"] and "promise far beyond its work" in mustact.LATE.values()
    ob.check(p, now + 120)
    assert len(_slack_events(p, o["id"])) == 1, "raised once per due"
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (t, now + 150))
    ob.check(p, now + 180)
    assert [e["status"] for e in _slack_events(p, o["id"])] == ["handled"], "cleared on start"
    ob.record(p, o["id"], "receipt", "posted", None)
    near = _eta_promise(p, t, "c1", "I'll post the result here in 10 minutes.", now + 200)
    ob.check(p, now + 260)
    assert not _slack_events(p, near["id"])
    ob.record(p, near["id"], "receipt", "posted", None)
    long_ = p.db.add_task("long soak", "s", kind="code", priority=1, reply_chat="c2")
    _eta_history(p, now, kind="code", runs=(7000, 7200, 7400))
    far = _eta_promise(p, long_, "c2", "I'll post the result here in 3 hours.", now + 300)
    ob.check(p, now + 360)
    assert not _slack_events(p, far["id"])


def test_a_due_moved_twice_before_the_work_starts_alarms_and_a_justify_clears_it(env):
    from ttp import coordinator as coord, obligations as ob
    p = make(env)
    _no_events(p)
    now = time.time()
    t = p.db.add_task("long soak", "s", kind="code", priority=1, reply_chat="c2")
    _eta_history(p, now, kind="code", runs=(7000, 7200, 7400))
    o = _eta_promise(p, t, "c2", "I'll post the result here in 3 hours.", now)
    ob.set_due(p, o["id"], now + 4 * 3600, now=now + 60)
    ob.check(p, now + 90)
    assert not _slack_events(p, o["id"]), "one move is fine"
    ob.set_due(p, o["id"], now + 5 * 3600, now=now + 120)
    ob.check(p, now + 150)
    [ev] = _slack_events(p, o["id"])
    assert "moved 2 times" in ev["text"]
    assert coord.apply(p, [{"type": "obligation", "key": o["id"],
                            "justify": "safety: device hold on the box until its soak ends"}]) == []
    ob.check(p, now + 180)
    assert [e["status"] for e in _slack_events(p, o["id"])] == ["handled"], "a justify clears it"
    ob.record(p, o["id"], "receipt", "posted", None)
    asked = _eta_promise(p, t, "c2", "I'll post the result here in 3 hours.", now + 200)
    ob.set_due(p, asked["id"], now + 4 * 3600, now=now + 210, user=1)
    ob.set_due(p, asked["id"], now + 5 * 3600, now=now + 220, user=1)
    ob.check(p, now + 230)
    assert not _slack_events(p, asked["id"]), "moves the user asked for never count"

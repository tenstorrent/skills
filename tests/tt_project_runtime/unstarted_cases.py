# Known, ready work not started (ttp/unstarted.py, Daemon.check_unstarted) and the coordinator's
# parked-work nudge. tests/test_tt_project_runtime.py runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _unstarted_events(p, kind):
    return p.db.q("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))


def _ready_setup(env, monkeypatch):
    """A project with nothing else to do and a daemon; check(at) runs one ready check at `at`."""
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    d.kicks = []   # dispatch kicks (Daemon._kick_dispatch) recorded, not run
    monkeypatch.setattr(d, "dispatch", lambda: d.kicks.append(time.time()))

    def check(at):
        monkeypatch.setattr(time, "time", lambda: at)
        d._unstarted_checked = at - 60
        d.check_unstarted()
        return _unstarted_events(p, "ready_not_started")
    return p, d, check


def test_ready_not_started_after_10_min_and_2_min_at_priority_1_once_and_clears_on_start(env, monkeypatch):
    p, d, check = _ready_setup(env, monkeypatch)
    routine = p.db.add_task("routine work", "s", kind="work", tier="light", origin="coordinator", priority=3)
    urgent = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="coordinator", priority=1)
    start = time.time()
    assert check(start + 60) == []
    got = check(start + 150)
    assert [(e["task"], e["fingerprint"]) for e in got] == [(urgent, f"ready_not_started:{urgent}:0")]
    assert "worker slot free; held by" in got[0]["text"] and f"#{urgent}" in got[0]["text"]
    assert len(check(start + 540)) == 1, "priority 3 waits its 10 minutes"
    got = check(start + 610)
    assert [e["task"] for e in got] == [urgent, routine]
    assert len(check(start + 900)) == 2, "one event per stay, never raised again"
    p.db.update_task(routine, status="running")
    check(start + 960)
    ev = p.db.one("SELECT status FROM events WHERE fingerprint=?", (f"ready_not_started:{routine}:0",))
    assert ev["status"] == "handled", "it clears once the task starts"
    assert _unstarted_events(p, "ready_backlog") == [], "free slots and no gate: the plain alarm, no backlog"


def _start_run(p, d, at, title="other work"):
    """A worker run of the core provider started at `at` for a task of its own (a slot turning over)."""
    tid = p.db.add_task(title, "s", kind="work", tier="light", origin="coordinator", priority=3)
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(task, role, provider, started, status) VALUES(?,?,?,?,?)",
           (tid, "worker", d.cfg.get("core_provider", "claude"), at, "running"))
    return tid


def test_ready_not_started_reports_a_backlog_while_more_tasks_are_ready_than_free_slots(env, monkeypatch):
    """Five ready tasks, three slots, runs starting as slots free: the queue drains. The late ones raise no
    alarm; one handled backlog record gives the queue depth, live runs and free slots. Once the backlog is
    gone the plain alarm is back, from when it drained."""
    p, d, check = _ready_setup(env, monkeypatch)
    p.set_config("budget.max_parallel_workers", 3)
    d.cfg = p.config()
    d.update_gates()
    ids = [p.db.add_task(f"queued work {i}", "s", kind="work", tier="light", origin="coordinator", priority=3)
           for i in range(5)]
    start = time.time()
    check(start + 60)
    _start_run(p, d, start + 650)
    assert check(start + 700) == [], "a backlog drains: no ready-not-started alarm"
    got = _unstarted_events(p, "ready_backlog")
    assert len(got) == 1 and got[0]["status"] == "handled" and got[0]["severity"] == "low"
    core = d.cfg.get("core_provider", "claude")
    assert f"5 ready on {core} (queue depth)" in got[0]["text"] and "1 run" in got[0]["text"]
    assert "2 free slots" in got[0]["text"] and "not a stall" in got[0]["text"]
    _start_run(p, d, start + 1300)
    assert check(start + 1400) == [] and len(_unstarted_events(p, "ready_backlog")) == 1, "one per episode"
    for tid in ids[:3]:
        p.db.update_task(tid, status="done")
    p.db.x("UPDATE runs SET status='done', ended=? WHERE status='running'", (start + 1450,))
    assert check(start + 1500) == [], "drained: each wait counts from here"
    assert [e["task"] for e in check(start + 2110)] == ids[3:], "no backlog, free slots: the plain alarm"
    assert "since" not in (p.db.kv("ready_backlog") or {}).get(core, {}), "the episode ended"


def test_ready_backlog_with_free_slots_and_no_start_is_a_stall(env, monkeypatch):
    """More ready than free slots, but a free slot left unused and no run started for READY_S: the queue is
    not draining, so the plain alarm is raised, naming the backlog; a fresh start mutes it again."""
    from ttp import unstarted
    p, d, check = _ready_setup(env, monkeypatch)
    p.set_config("budget.max_parallel_workers", 3)
    d.cfg = p.config()
    d.update_gates()
    ids = [p.db.add_task(f"queued work {i}", "s", kind="work", tier="light", origin="coordinator", priority=3)
           for i in range(5)]
    start = time.time()
    _start_run(p, d, start - 100)   # live, but started before the task's window
    check(start + 60)
    got = check(start + 60 + unstarted.READY_S + 10)
    assert [e["task"] for e in got] == ids, "free slots, nothing started: a stall"
    assert "does not explain it" in got[0]["text"] and "5 ready on" in got[0]["text"]
    assert _unstarted_events(p, "ready_backlog") == []
    _start_run(p, d, start + 700)
    assert [e["status"] for e in check(start + 720)] == ["handled"] * 5, "draining again: the alarms clear"
    assert len(_unstarted_events(p, "ready_backlog")) == 1


def test_ready_not_started_names_the_backlog_a_red_spend_gate_left_until_it_drains(env, monkeypatch):
    """A red gate holds three tasks (its own alert). Once it clears, tasks still waiting while runs start
    name when it cleared and why it held, not an unexplained stall; with free slots and no start since it
    cleared, the plain alarm; once the gate cleared long ago, the plain alarm too."""
    from ttp import budget as bud
    from ttp import unstarted
    p, d, check = _ready_setup(env, monkeypatch)
    ids = [p.db.add_task(f"queued work {i}", "s", kind="work", tier="light", origin="coordinator", priority=3)
           for i in range(3)]
    green = dict(d.gates)
    d.gates = {prov: bud.Gate(provider=prov, level="red", max_parallel=0, allow_new_work=False,
                              cause="no new work at red: global daily cap reached") for prov in green}
    start = time.time()
    _start_run(p, d, start + 2990)   # before the gate cleared: no sign the queue drains after it
    assert check(start + 60) == [] and check(start + 3000) == [], "the red gate has its own alert"
    core = d.cfg.get("core_provider", "claude")
    assert (p.db.kv("ready_backlog") or {})[core]["stopped"] is True
    d.gates = green
    assert check(start + 3060) == []
    got = check(start + 3700)
    assert [e["task"] for e in got] == ids, "free slots and no start since the gate cleared: a stall"
    assert "or since the gate cleared" in got[0]["text"]
    assert _unstarted_events(p, "ready_backlog") == []
    _start_run(p, d, start + 3710)
    assert [e["status"] for e in check(start + 3720)] == ["handled"] * 3, "the queue the gate left drains"
    got = _unstarted_events(p, "ready_backlog")
    assert len(got) == 1, got
    assert f"3 ready on {core} (queue depth)" in got[0]["text"] and "4 free slots" in got[0]["text"]
    assert "the spend gate cleared at" in got[0]["text"] and "global daily cap reached" in got[0]["text"]
    later = start + 3060 + unstarted.BACKLOG_S + 60
    p.db.x("UPDATE runs SET status='done', ended=? WHERE status='running'", (later - 10,))
    check(later)
    assert "since" not in (p.db.kv("ready_backlog") or {}).get(core, {}), "the gate cleared long ago: no backlog"
    assert len(_unstarted_events(p, "ready_backlog")) == 1


def test_ready_backlog_never_hides_deadline_work(env, monkeypatch):
    """An urgent p1 task in a queue deeper than its free slots keeps its alarm: deadline work is no backlog."""
    p, d, check = _ready_setup(env, monkeypatch)
    p.set_config("budget.max_parallel_workers", 1)
    d.cfg = p.config()
    d.update_gates()
    for i in range(2):
        p.db.add_task(f"queued work {i}", "s", kind="work", tier="light", origin="coordinator", priority=3)
    hot = p.db.add_task("urgent: user-ordered fix", "urgent: the user asked for it now", kind="work",
                        tier="light", origin="user", priority=1)
    start = time.time()
    check(start + 30)
    got = check(start + 700)
    assert hot in [e["task"] for e in got], got


def test_ready_not_started_leaves_out_paused_resources_and_full_slots(env, monkeypatch):
    from ttp.db import PAUSED_RESOURCES_KEY
    p, d, check = _ready_setup(env, monkeypatch)
    p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev-x": {"reason": "hold", "by": "coordinator", "since": 1}})
    p.db.add_task("needs the paused box", "s", kind="work", tier="light", origin="coordinator",
                  priority=1, labels=["resource:dev-x"])
    start = time.time()
    assert check(start + 900) == [], "a paused resource has its own alert"
    p.db.set_kv(PAUSED_RESOURCES_KEY, {})
    p.set_config("budget.max_parallel_workers", 1)
    d.cfg = p.config()
    d.update_gates()
    busy = p.db.add_task("holds the slot", "s", kind="work", tier="light", origin="coordinator", priority=3)
    p.db.update_task(busy, status="running")
    for prov in {g for g in d.gates} | {"claude"}:
        p.db.x("INSERT INTO runs(task, role, provider, started, status) VALUES(?,?,?,?,?)",
               (busy, "worker", prov, start, "running"))
    assert check(start + 1800) == [], "every slot taken: worker_slots' lane, not this check"


def _sent_back_review(p, start):
    """A p1 review whose run ended at `start` and whose approved head then waited in the push queue."""
    tid = p.db.add_task("review a change", "s", kind="review", tier="standard", origin="coordinator", priority=1)
    p.db.x("UPDATE tasks SET created=?, status='pushing' WHERE id=?", (start - 600, tid))
    p.db.x("INSERT INTO runs(task, role, provider, started, ended, status) VALUES(?,?,?,?,?,?)",
           (tid, "reviewer", "claude", start - 300, start, "ok"))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) VALUES(?,?,?,?,?,?,?)",
           (tid, "work-branch", "abc123", "push-branch", "approved", start, start))
    return tid


def _send_back(p, tid, at, monkeypatch):
    """The push queue sends the head back with a conflict at `at` (pushq._requeue, as _settle does)."""
    from ttp import pushq
    monkeypatch.setattr(time, "time", lambda: at)
    p.db.x("UPDATE push_queue SET status='conflict', updated=? WHERE task=?", (at, tid))
    pushq._requeue(p.db, p.db.task(tid), "push conflict: work-branch conflicts with push-branch")


def test_ready_not_started_counts_a_review_the_push_queue_sent_back_from_its_requeue(env, monkeypatch):
    """Hours in 'pushing' are no wait for a worker: a review sent back by the push queue is ready from
    then, not from its last run's end, while the daemon runs and after a restart alike."""
    p, d, check = _ready_setup(env, monkeypatch)
    start = time.time()
    tid = _sent_back_review(p, start)
    assert check(start + 60) == []
    assert check(start + 10200) == [], "it waits in the push queue: not ready"
    _send_back(p, tid, start + 10230, monkeypatch)
    assert p.db.task(tid)["status"] == "queued"
    assert check(start + 10240) == [], "sent back 10 s ago: its next tick starts it"
    got = check(start + 10380)
    assert [e["task"] for e in got] == [tid] and "ready 2 min" in got[0]["text"]

    p.db.x("DELETE FROM events")   # a daemon restart: no earlier check, only the records
    other = _sent_back_review(p, start)
    _send_back(p, other, start + 10230, monkeypatch)
    d._unstarted_seen, d._unstarted_held, d._unstarted_ready = {}, set(), None
    assert check(start + 10240) == [], "the push queue's record says both came back 10 s ago"
    assert [e["task"] for e in check(start + 10380)] == [tid, other]


def test_ready_not_started_counts_a_task_back_in_the_queue_from_the_check_that_missed_it(env, monkeypatch):
    """A task back in the queue with no record of when (a wait whose probe passed, a requeue) counts from
    the previous check that did not find it ready, not from its last run's end."""
    p, d, check = _ready_setup(env, monkeypatch)
    start = time.time()
    tid = p.db.add_task("waits on a probe", "s", kind="work", tier="light", origin="coordinator", priority=1)
    p.db.x("UPDATE tasks SET created=?, status='waiting' WHERE id=?", (start - 600, tid))
    p.db.x("INSERT INTO runs(task, role, provider, started, ended, status) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "claude", start - 300, start, "ok"))
    assert check(start + 7200) == []
    p.db.x("UPDATE tasks SET status='queued' WHERE id=?", (tid,))
    assert check(start + 7260) == [], "back for at most a minute"
    assert [e["task"] for e in check(start + 7380)] == [tid]


def test_check_slot_wait_skips_a_task_the_ready_check_already_raised(env, monkeypatch):
    """Both checks watch an urgent task with a free slot: one event, whichever raised first."""
    p, d, check = _ready_setup(env, monkeypatch)
    urgent = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="coordinator", priority=1)
    monkeypatch.setattr(d, "_start_hold", lambda t, prov, ctx: "budget")   # a hold neither check leaves out
    start = time.time()
    assert len(check(start + 700)) == 1
    d._slot_wait_checked = start + 640
    d.check_slot_wait()
    assert p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'slot_wait:%'") == []
    p.db.x("DELETE FROM events WHERE kind='ready_not_started'")
    d._slot_wait_checked = start + 640
    d.check_slot_wait()
    assert len(p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'slot_wait:%'")) == 1, "without it, slot_wait raises"


def _promise(p, chat="c1", text="I'll post the bench numbers by 15:00."):
    from ttp import coordinator as coord
    from ttp import obligations as ob
    assert coord.apply(p, [{"type": "reply", "chat": chat, "text": text}]) == []
    return next(o for o in ob.items(p.db) if o.get("promise") and o["promise"]["chat"] == chat)


def test_announced_promise_with_no_owner_raised_after_10_min_naming_none_and_cleared_on_close(env):
    from ttp import obligations as ob
    from ttp import unstarted
    p = make(env)
    _no_events(p)
    o = _promise(p)
    t0 = float(o["created"])
    assert unstarted.announced(p, t0 + 540) == []
    assert unstarted.announced(p, t0 + 610) == [f"announced_not_started:{o['id']}"]
    ev = _unstarted_events(p, "announced_not_started")[0]
    assert "Owner: none" in ev["text"] and "announced 10 min ago" in ev["text"] and "no owner task" in ev["text"]
    assert unstarted.announced(p, t0 + 900) == [], "one event per promise"
    ob.record(p, o["id"], "receipt", "numbers posted", None)
    unstarted.announced(p, t0 + 960)
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev["id"],))["status"] == "handled"


def test_announced_task_priority_1_raised_after_5_min_names_its_deferral_and_clears_on_a_run(env):
    from ttp import unstarted
    p = make(env)
    _no_events(p)
    tid = p.db.add_task("send the summary", "s", kind="work", tier="light", origin="coordinator", priority=1)
    t0 = p.db.task(tid)["created"]
    p.db.update_task(tid, reply_chat="c9", not_before=t0 + 7200,
                     labels=json.dumps(["start_after:7200", "start_why:later"]))
    assert unstarted.announced(p, t0 + 240) == []
    assert unstarted.announced(p, t0 + 310) == [f"announced_not_started:t{tid}"]
    ev = _unstarted_events(p, "announced_not_started")[0]
    assert ev["task"] == tid and f"Owner: #{tid}" in ev["text"] and "start_after" in ev["text"]
    p.db.x("INSERT INTO runs(task, role, provider, started, status) VALUES(?,?,?,?,?)",
           (tid, "worker", "claude", t0 + 320, "running"))
    unstarted.announced(p, t0 + 330)
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev["id"],))["status"] == "handled"


@pytest.mark.parametrize("hold", ["ask", "paused", "dependency"])
def test_announced_owner_held_externally_raises_nothing(env, hold):
    from ttp import unstarted
    from ttp.db import PAUSED_RESOURCES_KEY
    p = make(env)
    _no_events(p)
    dep = p.db.add_task("upstream step", "s", kind="work", tier="light", origin="coordinator")
    labels = ["resource:dev-x"] if hold == "paused" else []
    tid = p.db.add_task("the announced step", "s", kind="work", tier="light", origin="coordinator",
                        labels=labels, depends_on=[dep] if hold == "dependency" else None)
    p.db.update_task(tid, reply_chat="c3")
    if hold == "ask":
        p.db.post("out", f"Should #{tid} use the new data set?", kind="ask", chat="c3")
    elif hold == "paused":
        p.db.set_kv(PAUSED_RESOURCES_KEY, {"dev-x": {"reason": "hold", "by": "user", "since": 1}})
    else:
        p.db.update_task(dep, status="running")
    t0 = p.db.task(tid)["created"]
    assert unstarted.announced(p, t0 + 1800) == []
    assert _unstarted_events(p, "announced_not_started") == []


def _pushing_owner(p, batched: bool):
    """A promise owned by an approved review in status pushing with an open push-queue row: batched in
    an unfinished batch whose marker does not exist (dead by pushq.alive), or approved."""
    from ttp import obligations as ob
    o = _promise(p, chat="c4")
    tid = p.db.add_task("Review #1: the change", "s", kind="review", tier="light", origin="coordinator")
    ob.attach(p, o["id"], [tid])
    p.db.update_task(tid, status="pushing")
    t0 = float(o["created"])
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1', ?, 'origin/x', ?)",
           (str(p.base / "no-such-marker.json"), t0))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, batch, created, updated) "
           "VALUES(?, 'ttp/t', ?, 'origin/x', ?, ?, ?, ?)",
           (tid, "a" * 40, "batched" if batched else "approved", "b1" if batched else None, t0, t0))
    return tid, t0


def test_announced_owner_pushing_in_a_live_push_queue_counts_as_started(env, monkeypatch):
    """An owner the push queue delivers is the daemon's work: no announced_not_started while its batch
    runs or it waits in a queue that is not stalled (old code: 'pushing without starting')."""
    from ttp import pushq, unstarted
    p = make(env)
    _no_events(p)
    tid, t0 = _pushing_owner(p, batched=True)
    monkeypatch.setattr(pushq, "alive", lambda marker: (True, {"phase": "checks"}))
    assert unstarted.announced(p, t0 + 1800) == []
    p.db.x("UPDATE push_queue SET status='approved', batch=NULL")
    assert unstarted.announced(p, t0 + 1900) == []
    assert _unstarted_events(p, "announced_not_started") == []
    p.db.x("DELETE FROM push_queue")   # pushing with no queue row holding it: nothing will pick it up
    assert len(unstarted.announced(p, t0 + 2000)) == 1
    assert "pushing without starting" in _unstarted_events(p, "announced_not_started")[0]["text"]


@pytest.mark.parametrize("stall", ["dead batch", "dying", "not landing"])
def test_announced_owner_pushing_in_a_dead_push_queue_pages_through_the_queue_event(env, stall):
    """A dead batch or a stalled queue holds a pushing owner: no announced_not_started (the queue is
    not alive, so the owner is not started), while the queue's own events page it."""
    from ttp import pushq, unstarted
    p = make(env)
    _no_events(p)
    tid, t0 = _pushing_owner(p, batched=stall == "dead batch")
    alerts = []
    if stall == "dead batch":
        assert pushq.holding(p.db, tid) == (False, "push batch b1 not running")
        p.db.set_kv(pushq.KV, {"deaths": pushq.DYING_AFTER - 1})
        b = p.db.one("SELECT * FROM push_batches WHERE id='b1'")
        pushq._died(p, b, {}, lambda kind, text, **k: alerts.append(kind), "died", t0 + 60)
        assert alerts == ["push_queue_dying"]
        assert p.db.one("SELECT 1 FROM events WHERE kind='push_batch_died'")
    elif stall == "dying":
        p.db.set_kv(pushq.KV, {"deaths": pushq.DYING_AFTER})
    else:
        p.db.set_kv(pushq.KV, {"landing_told": t0 + 30})
    live, words = pushq.holding(p.db, tid)
    assert not live and ("stalled" in words), words
    assert unstarted.announced(p, t0 + 1800) == []
    assert _unstarted_events(p, "announced_not_started") == []


def _notes(p):
    from ttp import coordinator as coord
    return [n for n in p.db.kv(coord.NOTES_KEY) or [] if "without start_after" in n]


@pytest.mark.parametrize("why", ["", "later", "do it after the refactor", "check the inbox later",
                                 "the userland cleanup first", "metadata tidy-up first"])
def test_parked_nudge_for_a_deferral_naming_no_external_fact_never_refuses_and_counts(env, why):
    from ttp import coordinator as coord
    from ttp import unstarted
    p = make(env)
    add = {"type": "task_add", "title": "write the report", "spec": "Write it.", "start_after": "2h"}
    assert coord.apply(p, [{**add, "why": why} if why else add]) == [], "it never refuses"
    t = p.db.one("SELECT * FROM tasks WHERE title=?", ("write the report",))
    assert t["not_before"], "the deferral still applies"
    assert len(_notes(p)) == 1 and unstarted.RULE in _notes(p)[0]
    assert p.db.kv(unstarted.NUDGE_KV)["n"] == 1


def test_parked_nudge_folds_into_the_tasks_existing_note_one_note_per_action(env):
    # A note already written for the task (a flag) keeps its place; the nudge rides in it.
    from ttp import coordinator as coord
    from ttp import unstarted
    p = make(env)
    for probe in ("test -f out/now", "test -f state/ready/box1.READY"):   # a live probe is the named fact
        assert coord.apply(p, [{"type": "task_add", "title": probe, "spec": "s", "start_when": probe}]) == []
        assert not any(unstarted.RULE in n for n in p.db.kv(coord.NOTES_KEY) or [])
    assert coord.apply(p, [{"type": "task_add", "title": "both", "spec": "s", "start_after": "2h",
                            "start_when": "test -f out/now"}]) == []
    assert _notes(p) == [] and not p.db.kv(unstarted.NUDGE_KV)
    out = ["task_add: #12 added", "task_add: #1 added; flagged: x"]
    unstarted.add_nudge(out, "task_add: #1", "task_add: #1 is deferred: rule")
    assert out == ["task_add: #12 added", "task_add: #1 added; flagged: x; is deferred: rule"]
    unstarted.add_nudge(out, "task_add: #2", "task_add: #2 is deferred: rule")
    assert out[-1] == "task_add: #2 is deferred: rule"   # no note of its own yet: a new one


@pytest.mark.parametrize("why", ["after #12 lands", "the nightly data must accumulate first",
                                 "the user answers the ask", "the shared box is reserved until then",
                                 "the release window opens at 18:00"])
def test_no_parked_nudge_when_the_why_names_an_external_fact_or_nothing_is_deferred(env, why):
    from ttp import coordinator as coord
    p = make(env)
    assert coord.apply(p, [{"type": "task_add", "title": "deferred", "spec": "s", "start_after": "2h",
                            "why": why}]) == []
    assert coord.apply(p, [{"type": "task_add", "title": "now", "spec": "s", "why": "later"}]) == []
    assert _notes(p) == []


def test_parked_nudge_when_task_update_adds_spec_to_work_deferred_over_10_min(env):
    from ttp import coordinator as coord
    from ttp import unstarted
    p = make(env)
    far = p.db.add_task("far", "s", kind="work", tier="light", origin="coordinator")
    near = p.db.add_task("near", "s", kind="work", tier="light", origin="coordinator")
    now = time.time()
    p.db.update_task(far, not_before=now + 1800)
    p.db.update_task(near, not_before=now + 300)
    assert coord.apply(p, [{"type": "task_update", "id": near, "spec": "also do the second step"},
                           {"type": "task_update", "id": far, "priority": 2}]) == []
    assert _notes(p) == []
    assert coord.apply(p, [{"type": "task_update", "id": far, "spec": "also do the second step"}]) == []
    assert [n.split(" added")[0] for n in _notes(p)] == [f"task_update: #{far}"]
    assert p.db.kv(unstarted.NUDGE_KV)["n"] == 1


def test_ready_followup_of_a_hand_off_is_announced_work_owned_by_the_task_that_takes_it_up(env):
    """A `ready:` follow-up is a ready-work signal: none taken up in 10 min names owner none; a later task
    titled as it is its owner, held externally raises nothing, and its run clears the event."""
    from ttp import unstarted
    p = make(env)
    _no_events(p)
    src = p.db.add_task("wait on the soak", "s", kind="code", tier="light", origin="coordinator", priority=3)
    fups = [{"title": "ready: write the release notes for the soak change", "spec": "x"},
            {"title": "continue: read the soak result", "spec": "y"}]
    p.db.update_task(src, status="waiting", result=json.dumps({"status": "waiting", "followups": fups}))
    t0 = time.time()
    p.db.x("INSERT INTO runs(task, role, provider, started, ended, status) VALUES(?,?,?,?,?,?)",
           (src, "worker", "claude", t0 - 300, t0, "done"))
    assert unstarted.announced(p, t0 + 540) == []
    assert unstarted.announced(p, t0 + 610) == [f"announced_not_started:f{src}.0"], "only the ready: one"
    ev = _unstarted_events(p, "announced_not_started")[0]
    assert "Owner: none" in ev["text"] and "write the release notes" in ev["text"]
    # Taken up by a task with the step as its title, parked behind a dependency on the waiting task.
    own = p.db.add_task("Write the release notes for the soak change", "s", kind="work", tier="light",
                        origin="coordinator", priority=3, depends_on=[src])
    assert "dependency" in (unstarted.external_hold(p.db, p.db.task(own), {}) or "")
    unstarted.announced(p, t0 + 620)
    assert p.db.one("SELECT status FROM events WHERE id=?", (ev["id"],))["status"] == "handled", \
        "an owner held by a task still waiting is an external hold: retired, no turn"
    # A short step text is not matched by chance in another task's spec.
    src2 = p.db.add_task("other", "s", kind="code", tier="light", origin="coordinator", priority=3)
    p.db.update_task(src2, status="done", result=json.dumps({"followups": [{"title": "Ready: bump"}]}))
    p.db.x("INSERT INTO runs(task, role, provider, started, ended, status) VALUES(?,?,?,?,?,?)",
           (src2, "worker", "claude", t0 - 300, t0, "done"))
    p.db.add_task("unrelated", "please bump the counter", kind="work", tier="light", origin="coordinator")
    assert unstarted.announced(p, t0 + 700) == [f"announced_not_started:f{src2}.0"]
    taker = p.db.add_task("bump", "s", kind="work", tier="light", origin="coordinator")
    p.db.x("INSERT INTO runs(task, role, provider, started, status) VALUES(?,?,?,?,?)",
           (taker, "worker", "claude", t0 + 710, "running"))
    unstarted.announced(p, t0 + 720)
    assert p.db.one("SELECT status FROM events WHERE fingerprint=?",
                    (f"announced_not_started:f{src2}.0",))["status"] == "handled", "its owner started"


def test_status_and_web_name_the_dependency_a_queued_task_waits_on(env):
    """A queued task held by an unfinished dependency is not ready, so no ready check names it: ttp status
    and the web app's task row say what it waits on and that task's state."""
    import shutil
    import subprocess
    from ttp.cli import status_text
    from ttp.web import health
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    first = p.db.add_task("continue the measurement", "s", kind="code", origin="coordinator")
    p.db.update_task(first, status="waiting")
    then = p.db.add_task("review the change", "s", kind="review", origin="coordinator", depends_on=[first])
    free = p.db.add_task("ready work", "s", origin="coordinator")
    h = health(p, p.db, now=time.time())
    assert [(t["id"], [(d["id"], d["status"]) for d in t["on"]]) for t in h["dep_held"]] == [(then, [(first, "waiting")])]
    line = next(ln for ln in status_text(p).splitlines() if ln.startswith(f"  #{then} "))
    assert line == f"  #{then} queued, waits on #{first} (waiting): review the change", line
    assert not [ln for ln in status_text(p).splitlines() if ln.startswith(f"  #{free} queued, waits")]
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "depWait(t) ? esc(depWait(t))" in js[js.index("function taskRow"):]
    node = shutil.which("node")
    if node:
        fn = js[js.index("const depWait ="):js.index("// A chain's metric")]
        out = subprocess.run([node, "-e", fn + "console.log(JSON.stringify([depWait({status: 'queued', waits_on: [7, null]}),"
                              "depWait({status: 'queued', waits_on: []}), depWait({status: 'running', waits_on: [7]})]))"],
                             capture_output=True, text=True, timeout=30)
        assert json.loads(out.stdout) == ["waits on #7", "", ""], out.stderr


def test_an_urgent_task_ready_with_a_free_slot_kicks_dispatch_and_routine_work_does_not(env, monkeypatch):
    """The model-free detector: a priority 1 task ready past READY_P1_S with a free slot and no hold kicks
    dispatch from the check (it does not wait for the tick's own dispatch step) and its event says so."""
    from ttp import unstarted
    p, d, check = _ready_setup(env, monkeypatch)
    routine = p.db.add_task("routine work", "s", kind="work", tier="light", origin="coordinator", priority=3)
    p1 = p.db.add_task("urgent work", "s", kind="work", tier="light", origin="coordinator", priority=1)
    start = time.time()
    assert check(start + 60) == [] and d.kicks == []
    got = check(start + unstarted.READY_P1_S + 30)
    assert [e["task"] for e in got] == [p1] and "kicked dispatch" in got[0]["text"]
    assert len(d.kicks) == 1, "one kick for the urgent task"
    p.db.update_task(p1, status="running")
    d.kicks.clear()
    assert routine not in [e["task"] for e in check(start + 300)] and d.kicks == [], "priority 3 never kicks"
    p.db.update_task(p1, status="queued")
    monkeypatch.setattr(d, "lease_held", lambda: False)
    check(start + 400)
    assert d.kicks == [], "no kick without the project lease"


def test_a_deadline_task_held_only_by_full_slots_raises_at_60_s_naming_what_fills_them(env, monkeypatch):
    """A fresh `urgent:` line or an obligation owner whose resources are free but whose worker slots
    (burst slots included) are all taken is raised after unstarted.READY_URGENT_S, naming the slots' users."""
    from ttp import unstarted, urgent
    p, d, check = _ready_setup(env, monkeypatch)
    core = d.cfg.get("core_provider", "claude")
    gate = d.gates[core]
    full = gate.max_parallel + urgent.extra_slots(d.cfg)
    for i in range(full):
        tid = p.db.add_task(f"busy {i}", "s", kind="code" if i % 2 else "review", tier="light", origin="user")
        p.db.update_task(tid, status="running")
        p.db.x("INSERT INTO runs(task, role, provider, status, started) VALUES(?,?,?,?,?)",
               (tid, "worker", core, "running", time.time()))
    hot = p.db.add_task("user deadline", "s\nurgent: the user waits on it", kind="work", tier="light",
                        origin="user", priority=1)
    plain = p.db.add_task("routine p1", "s", kind="work", tier="light", origin="coordinator", priority=1)
    start = time.time()
    assert check(start + 30) == []
    got = check(start + 30 + unstarted.READY_URGENT_S + 5)
    assert [e["task"] for e in got] == [hot], "the deadline task only; routine priority 1 is a slot wait"
    assert "every worker slot taken but its resources free" in got[0]["text"]
    assert f"they hold {full} runs (" in got[0]["text"] and " review" in got[0]["text"]
    assert d.kicks == [], "no free slot: nothing to kick"
    assert plain not in [e["task"] for e in check(start + 30 + unstarted.READY_URGENT_S + 70)]


def test_a_deadline_task_held_by_full_slots_is_raised_only_with_its_resources_free_and_slot_waits_on(
        env, monkeypatch):
    """The full-slots event says the resources are free: a start with empty slots (as _log_slot_fill
    checks) must find nothing else holding it, and coordinator.urgent_slot_wait_min 0 turns it off."""
    from ttp import unstarted
    p, d, check = _ready_setup(env, monkeypatch)
    core = d.cfg.get("core_provider", "claude")
    for i in range(d.gates[core].max_parallel + 1):
        tid = p.db.add_task(f"busy {i}", "s", kind="work", tier="light", origin="user")
        p.db.update_task(tid, status="running")
        p.db.x("INSERT INTO runs(task, role, provider, status, started) VALUES(?,?,?,?,?)",
               (tid, "worker", core, "running", time.time()))
    hot = p.db.add_task("user deadline", "s\nurgent: the user waits on it", kind="work", tier="light",
                        origin="user", priority=1)
    start = time.time()
    held = {"kind": "lock", "resource": "box-device", "holders": []}
    monkeypatch.setattr(d, "_resource_hold", lambda task, reserve=False: held)
    assert check(start + 30) == []
    assert check(start + 30 + unstarted.READY_URGENT_S + 5) == [], "its lock is busy: no free-resources claim"
    monkeypatch.setattr(d, "_resource_hold", lambda task, reserve=False: None)
    d.cfg["coordinator"]["urgent_slot_wait_min"] = 0
    assert check(start + 30 + unstarted.READY_URGENT_S + 70) == [], "slot-wait events turned off"
    d.cfg["coordinator"]["urgent_slot_wait_min"] = 10
    assert check(start + 200) == [], "a hold restarts its clock"
    assert [e["task"] for e in check(start + 200 + unstarted.READY_URGENT_S + 5)] == [hot]


def test_user_ordered_urgent_device_work_not_started_or_stuck_in_ttp_lock_raises_one_high_event_naming_the_holder(
        env, monkeypatch, tmp_path):
    """Daemon.check_urgent_start: a fresh `urgent:` task with a machine resource still queued after
    coordinator.urgent_start_wait_min (5) names what holds it; once running, an open `ttp lock` wait that
    long names the lock's other holders. One high event per task and run; routine p1, an urgent task
    without a resource and 0 (off) raise nothing."""
    import fcntl
    from ttp import db as dbmod, locks, urgent
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    assert d.cfg["coordinator"]["urgent_start_wait_min"] == urgent.START_WAIT_MIN == 5

    def add(title, spec, labels, priority=3):
        tid = p.db.add_task(title, spec, kind="work", tier="light", origin="user", priority=priority)
        p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(labels), tid))
        return tid
    hot = add("reset the box", "s\nurgent: the user ordered it", ["resource:box-a"])
    add("routine p1 on the box", "s", ["resource:box-a"], priority=1)
    add("urgent, no machine", "s\nurgent: the user ordered it", [])
    p.db.set_kv(dbmod.PAUSED_RESOURCES_KEY, {"box-a": {"reason": "held", "since": time.time(), "by": "user"}})
    start = time.time()

    def check(at):
        monkeypatch.setattr(time, "time", lambda: at)
        d._urgent_start_checked = at - 60
        d.check_urgent_start()
        return p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'urgent_%' ORDER BY id")
    assert check(start + 4 * 60) == [], "under 5 minutes"
    got = check(start + 5 * 60 + 5)
    assert [(e["task"], e["fingerprint"], e["severity"]) for e in got] == [(hot, f"urgent_start:{hot}:0", "high")]
    assert "held by pause on resource box-a" in got[0]["text"], got[0]["text"]
    assert len(check(start + 7 * 60)) == 1, "once per task and run"

    # Running, its `ttp lock` wait open: the other project's holder is named; its own hold is not.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.update_task(hot, status="running")
    rid = p.db.x("INSERT INTO runs(task, role, provider, status, started, dir) VALUES(?,?,?,?,?,?)",
                 (hot, "worker", "claude", "running", start + 8 * 60, str(run_dir)))
    locks.record_wait(run_dir, "42:1.0", start + 8 * 60, None, ["box-a"])
    slot = d._slot_paths("box-a")[0]
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps({"holder": "other #7 (run 9)", "project": "other", "task_id": "7", "run_id": "9"}))
    fd = os.open(slot, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert len(check(start + 12 * 60)) == 1, "a wait under 5 minutes"
        got = check(start + 13 * 60 + 5)
        assert got[-1]["fingerprint"] == f"urgent_lock:{hot}:{rid}" and got[-1]["severity"] == "high"
        assert "box-a: other #7 run 9" in got[-1]["text"] and "in `ttp lock`" in got[-1]["text"]
        assert len(check(start + 15 * 60)) == 2, "once per run"
        slot.write_text(json.dumps({"holder": "itself", "project": p.name, "task_id": str(hot), "run_id": str(rid)}))
        p.db.x("DELETE FROM events WHERE fingerprint LIKE 'urgent_lock:%'")
        assert len(check(start + 16 * 60)) == 1, "its own hold is no other holder"
        d.cfg["coordinator"]["urgent_start_wait_min"] = 0
        p.db.x("DELETE FROM events")
        slot.write_text(json.dumps({"holder": "other #7 (run 9)", "project": "other", "task_id": "7"}))
        assert check(start + 20 * 60) == [], "0 turns it off"
    finally:
        os.close(fd)
        locks.record_wait(run_dir, "42:1.0", start + 8 * 60, start + 20 * 60)


def test_an_expect_claim_with_no_owner_open_20_min_is_never_announced_not_started(env):
    """A claim is promise-shaped but owes no work: an expect claim still open 20 min on raises no
    announced_not_started and shows in neither the promises digest nor the sweep's items."""
    from ttp import obligations as ob, sweep, unstarted
    p = make(env)
    _no_events(p)
    now = time.time()
    out = p.db.post("out", "it is live", chat="c1", kind="reply")
    [line] = ob.record_claims(p, [(out, "c1", [{"text": "the service is live", "expect": "true",
                                                 "due": now + 3600}])], {}, now=now)
    oid = line.split()[1]
    assert ob.get(p.db, oid)["receipt"] is None
    assert unstarted.announced(p, now + 1200) == [] and _unstarted_events(p, unstarted.ANNOUNCED_KIND) == []
    assert ob.open_promises(p.db) == [] and ob.promise_lines(p.db, now + 1200) == []
    assert not [i for i in sweep.items(p.db, now + 1200) if i.get("promise") == oid]


def test_one_stuck_user_ordered_task_held_by_full_slots_gives_one_wake_across_the_three_start_checks(
        env, monkeypatch):
    """check_unstarted (READY_URGENT_S), check_urgent_start (5 min) and check_slot_wait (10 min) all see one
    user-ordered urgent task held only by full worker slots: one event per run and hold, in whatever order
    they run and also once a turn handled it. A ready_not_started retired because its hold ended does not
    count, so the next hold is raised once."""
    from ttp import obligations, unstarted, urgent
    p, d, check = _ready_setup(env, monkeypatch)
    core = d.cfg.get("core_provider", "claude")
    for i in range(d.gates[core].max_parallel + urgent.extra_slots(d.cfg)):
        tid = p.db.add_task(f"busy {i}", "s", kind="work", tier="light", origin="user")
        p.db.update_task(tid, status="running")
        p.db.x("INSERT INTO runs(task, role, provider, status, started) VALUES(?,?,?,?,?)",
               (tid, "worker", core, "running", time.time()))
    hot = p.db.add_task("reset the box", "s\nurgent: the user ordered it", kind="work", tier="light",
                        origin="user", priority=1)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["resource:box-a"]), hot))
    start = time.time()

    def run(at, *which):
        monkeypatch.setattr(time, "time", lambda: at)
        for name in which:
            setattr(d, {"unstarted": "_unstarted_checked", "urgent_start": "_urgent_start_checked",
                        "slot_wait": "_slot_wait_checked"}[name], at - 60)
            getattr(d, f"check_{name}")()
        return [(e["task"], e["fingerprint"].split(":")[0]) for e in p.db.q(
            "SELECT * FROM events WHERE task=? AND fingerprint NOT LIKE 'urgent_lock:%' ORDER BY id", (hot,))]
    every = ("slot_wait", "urgent_start", "unstarted")   # the tick's order
    assert run(start + 30, *every) == []
    one = [(hot, unstarted.READY_KIND)]
    assert run(start + 30 + unstarted.READY_URGENT_S + 5, *every) == one
    assert run(start + 6 * 60, *every) == one, "urgent_start after the deadline event: one wake"
    p.db.x("UPDATE events SET status='handled' WHERE task=?", (hot,))
    assert run(start + 11 * 60, *every) == one, "slot_wait after it, handled by a turn: still one wake"
    # Its hold ended (check_unstarted retires its event): the next hold is a new event, raised once.
    p.db.x("UPDATE events SET status='queued' WHERE task=?", (hot,))
    assert obligations._retire(p.db, start + 11 * 60, "held: resource", f"{unstarted.READY_KIND}:{hot}:0") == 1
    assert run(start + 12 * 60, *every) == [
        (hot, unstarted.READY_KIND), (hot, "slot_wait")], "a retired event is no open one"
    # Any one of the three first: the other two leave it.
    for first in every:
        p.db.x("DELETE FROM events WHERE task=?", (hot,))
        rest = [n for n in every if n != first]
        got = run(start + 13 * 60, first)
        assert len(got) == 1, (first, got)
        assert run(start + 14 * 60, *rest) == got, f"{first} first: the others raised again"


def test_an_urgent_lock_wait_names_only_the_holders_of_the_lock_it_waits_on(env, monkeypatch, tmp_path):
    """Daemon.check_urgent_start, running branch: a task with two resources whose open `ttp lock` wait is on
    the second names only that lock's holders, not the first's; a wait naming no lock (a checks lane or a
    push) names none and raises nothing. record_wait keeps the locks when the closing call leaves them out."""
    import fcntl
    from ttp import locks
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    hot = p.db.add_task("flash both boxes", "s\nurgent: the user ordered it", kind="work", tier="light",
                        origin="user", priority=3)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["resource:box-a", "resource:box-b"]), hot))
    p.db.update_task(hot, status="running")
    start = time.time()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    rid = p.db.x("INSERT INTO runs(task, role, provider, status, started, dir) VALUES(?,?,?,?,?,?)",
                 (hot, "worker", "claude", "running", start, str(run_dir)))
    fds = []
    for res, task in (("box-a", "5"), ("box-b", "7")):
        slot = d._slot_paths(res)[0]
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text(json.dumps({"holder": f"other #{task}", "project": "other", "task_id": task, "run_id": "9"}))
        fds.append(os.open(slot, os.O_RDWR))
        fcntl.flock(fds[-1], fcntl.LOCK_EX)

    def check(at):
        monkeypatch.setattr(time, "time", lambda: at)
        d._urgent_start_checked = at - 60
        d.check_urgent_start()
        return p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'urgent_lock:%' ORDER BY id")
    try:
        locks.record_wait(run_dir, "push:1:1.0", start, None)   # no lock named: not a resource wait
        assert check(start + 10 * 60) == [], "a wait naming no lock names every resource's holders"
        locks.record_wait(run_dir, "42:2.0", start + 60, None, ["box-b"])
        (ev,) = check(start + 10 * 60)
        assert ev["fingerprint"] == f"urgent_lock:{hot}:{rid}"
        assert "box-b: other #7 run 9" in ev["text"], ev["text"]
        assert "box-a" not in ev["text"] and "#5" not in ev["text"], "a holder of a lock it does not wait on"
        assert "waited 9 min" in ev["text"], "from the start of the wait on that lock"
        locks.record_wait(run_dir, "42:2.0", start + 60, start + 11 * 60)
        assert [r[2] for r in locks.wait_rows(run_dir) if r[1] is not None] == [["box-b"]], \
            "the closing record keeps its locks"
    finally:
        for fd in fds:
            os.close(fd)
        locks.record_wait(run_dir, "push:1:1.0", start, start + 20 * 60)


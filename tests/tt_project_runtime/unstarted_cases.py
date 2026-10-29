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

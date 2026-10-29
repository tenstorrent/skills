# Urgent-lane tests (ttp/urgent.py: what makes a task urgent, its reviews, device queue and status marks)
# of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases
# there): helpers, fixtures and imports come from that file, and pytest collects these tests under its
# name. Its dispatch, push-queue and lock-waiter tests are in dispatch_cases, push_cases and lock_cases.
from __future__ import annotations


def test_a_task_is_urgent_at_priority_one_or_with_an_urgent_spec_line_only():
    from ttp import urgent
    assert urgent.is_urgent({"priority": 1}) and urgent.is_urgent({"priority": 0})
    assert urgent.is_urgent({"priority": 3, "spec": "Do it.\n  URGENT: the user is waiting \n"})
    assert urgent.why({"priority": 3, "spec": "Do it.\nurgent: the user is waiting \n"}) == "the user is waiting"
    assert urgent.why({"priority": 1, "spec": "s"}) == "priority 1"
    for t in ({"priority": 2}, {"priority": None}, {"priority": "x"}, {}, None,
              {"priority": 3, "spec": "this is not urgent: really"},   # not at the line's start
              {"priority": 3, "spec": "urgent:\nnext line"},           # no reason given
              {"priority": 3, "spec": "urgent:   \n"},
              {"priority": 3, "spec": "urgently: soon"},
              {"priority": 3, "spec": "urgent: no. parallel: runs beside other work"},   # says it is not
              {"priority": 3, "spec": "Urgent: None"}, {"priority": 3, "spec": "urgent: n/a"}):
        assert not urgent.is_urgent(t), t
        assert urgent.why(t or {}) == "", t
    assert urgent.extra_slots({}) == 1 and urgent.extra_slots({"budget": {"urgent_extra_slots": 0}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": -2}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": "x"}}) == 1


def test_an_urgent_code_task_gets_its_review_at_priority_one_on_a_needs_review_hand_off_too(env):
    p = make(env)
    _with_origin(env, clone=False)
    hot = lambda tid, head: p.db.update_task(tid, priority=1)   # noqa: E731
    tid, _, _, reviews = _finish_code(env, p, "hot fix", {"a.py": 5}, before=hot,
                                      result={"status": "needs_review", "summary": "ok"})
    assert [(r["priority"], r["origin"]) for r in reviews] == [(1, "daemon")], "no review queued at once"
    tid, _, _, reviews = _finish_code(env, p, "hot done", {"b.py": 5}, before=hot)
    assert [r["priority"] for r in reviews] == [1]
    tid, _, _, reviews = _finish_code(env, p, "routine", {"c.py": 5}, result={"status": "needs_review", "summary": "ok"})
    assert reviews == [], "a routine needs_review hand-off still waits for the coordinator or the sweep"
    tid, _, _, reviews = _finish_code(env, p, "routine done", {"d.py": 5})
    assert [r["priority"] for r in reviews] == [p.db.task(tid)["priority"]], "a routine review keeps its chain's (chainprio)"


def test_the_review_sweeps_take_an_urgent_task_at_once_and_past_the_cap(env):
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("review.unreviewed_per_pass", 1)
    old = [_unreviewed(env, p, f"routine {i}")[0] for i in range(2)]
    hot = _unreviewed(env, p, "hot", spec="Fix it.\nurgent: the user is waiting")[0]
    d = Daemon(p.base)
    assert d.sweep_unreviewed(force=True) == [hot, old[0]], "the cap counts routine tasks only"
    assert [r["priority"] for r in _daemon_reviews(p, hot)] == [1]
    assert [r["priority"] for r in _daemon_reviews(p, old[0])] == [p.db.task(old[0])["priority"]]
    # Left in review by a hand-off (follow-ups leave the review to the coordinator): no sweep_after_s wait.
    tid, _, _, reviews = _finish_code(env, p, "noted", {"n.py": 5}, result={
        "status": "needs_review", "summary": "ok", "followups": [{"title": "later", "spec": "more"}]})
    assert not reviews
    p.db.update_task(tid, priority=1)
    _ran(p, tid, time.time() - 60)
    _sweep(Daemon(p.base))
    assert [r["priority"] for r in _daemon_reviews(p, tid)] == [1], "an urgent task waited for sweep_after_s"


def test_an_urgent_task_in_review_ten_minutes_raises_one_review_stall(env):
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    hot = p.db.add_task("hot", "s\nurgent: the user is waiting", kind="code", origin="coordinator")
    routine = p.db.add_task("routine", "s", kind="code", origin="coordinator")
    for t in (hot, routine):
        p.db.update_task(t, status="review")
    since = p.db.review_since()[hot]
    d = Daemon(p.base)
    stalls = lambda: p.db.q("SELECT * FROM events WHERE kind='review_stall' ORDER BY id")   # noqa: E731
    d._raise_review_stalls(since + 599)
    assert stalls() == []
    d._raise_review_stalls(since + 601)
    (ev,) = stalls()
    assert ev["task"] == hot and "is urgent (the user is waiting)" in ev["text"] and "no review of it is open" in ev["text"]
    d._raise_review_stalls(since + 3600)
    assert len(stalls()) == 1, "one event per review stint"
    p.set_config("coordinator.review_stall_s", 0)
    p.db.x("DELETE FROM events WHERE kind='review_stall'")
    Daemon(p.base)._raise_review_stalls(since + 3600)
    assert stalls() == [], "review_stall_s 0 turns it off for urgent tasks too"


def test_the_device_runner_takes_an_urgent_job_before_older_routine_ones(tmp_path):
    from ttp import devq
    d = tmp_path / "rq"
    devq._dirs(d)
    devq._write(d / "config.json", json.dumps(devq.settings({})))
    jobs = [("j1", {}), ("j2", {"urgent": "yes"}), ("j3", None), ("j4", {"urgent": True}), ("j5", {"urgent": True})]
    for i, (job, extra) in enumerate(jobs):
        body = "{not json" if extra is None else json.dumps({"id": job, "cmd": "true", **extra})
        devq._write(d / "queue" / f"{i:020d}-{job}.json", body)
    r = devq.Runner(d)
    assert r.next_job() == "j4", "the oldest urgent job first"
    assert r.next_job() == "j4", "a running job is never passed over"
    (d / "running" / "j4.json").unlink()
    assert r.next_job() == "j5"
    (d / "running" / "j5.json").unlink()
    assert r.next_job() == "j1", "then arrival order; only a true `urgent` counts"


def test_status_and_the_web_payload_mark_an_urgent_task_with_its_wait(env):
    from ttp import cli, web
    p = make(env)
    hot = p.db.add_task("hot fix", "Fix it.\nurgent: the user is waiting", kind="code", origin="coordinator")
    p1 = p.db.add_task("p1 fix", "s", kind="code", origin="coordinator", priority=1)
    routine = p.db.add_task("routine fix", "s", kind="code", origin="coordinator")
    now = time.time()
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 12 * 60 - 5, hot))
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 75 * 60 - 5, p1))
    out = cli.status_text(p)
    assert f"#{hot} queued (urgent, waited 12m): hot fix — urgent: the user is waiting" in out, out
    assert f"#{p1} queued (urgent, waited 1h15m): p1 fix — urgent: priority 1" in out, out
    assert f"#{routine} queued (urgent" not in out
    rows = {t["id"]: t for t in web.state_payload(p, p.db)["tasks"]}
    assert rows[hot]["urgent"] == "the user is waiting" and 12 * 60 <= rows[hot]["waited_s"] < 13 * 60
    assert rows[routine]["urgent"] is None and rows[routine]["waited_s"] is None
    assert all("spec" not in t for t in rows.values()), "the payload never carries the spec"


def _gate_add(p, title, priority=1, spec="s", **gate):
    """A task_add with a start gate; (its id, the apply notes)."""
    from ttp import coordinator as coord
    notes = coord.apply(p, [{"type": "task_add", "title": title, "spec": spec, "kind": "work", "tier": "light",
                             "priority": priority, **gate}]) + list(p.db.kv(coord.NOTES_KEY, []) or [])
    return p.db.one("SELECT id FROM tasks WHERE title=?", (title,))["id"], notes


def test_an_urgent_task_add_drops_a_convenience_gate_and_keeps_a_safety_one(env):
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    for title, prio, spec, gate in (
            ("p1 later", 1, "s", {"start_after": "6h"}),
            ("p1 tidy", 1, "s", {"start_after": "2h", "why": "after lunch is tidier"}),
            ("urgent line", 3, "Do it.\nurgent: the user is waiting", {"start_after": "90m"}),
            ("p1 chained", 1, "s", {"start_when": "test -e /tmp/never-there-x", "why": "nicer in order"})):
        tid, notes = _gate_add(p, title, prio, spec, **gate)
        t = p.db.task(tid)
        assert deferral(t) == {} and (t["not_before"] or 0) <= time.time(), title
        assert any(f"#{tid}" in n and "dropped" in n and "safety class" in n for n in notes), (title, notes)
    for title, gate, cls in (
            ("p1 lull", {"start_when": "ttp lock --probe dev-x", "why": "a device lull for the broker restart"},
             "shared device or machine"),
            ("p1 users go", {"start_when": "test -e /tmp/proj/user-go"}, "the user's word"),
            ("p1 spend", {"start_after": "3h", "why": "the provider window resets then"}, "spend cap or provider window"),
            ("p1 needs file", {"start_when": "test -s /tmp/proj/helper.py", "why": "needs #1's new helper module"},
             "a dependency on another task's new file or symbol")):
        tid, notes = _gate_add(p, title, **gate)
        t = p.db.task(tid)
        assert deferral(t), title
        assert f"start_safety:{cls}" in json.loads(t["labels"]), (title, t["labels"])
        assert not any("dropped" in n for n in notes), (title, notes)
    tid, notes = _gate_add(p, "p3 later", 3, **{"start_after": "6h"})
    assert deferral(p.db.task(tid)).get("after") and not any("dropped" in n for n in notes), "a routine task is unchanged"


def test_an_urgent_task_update_drops_a_convenience_gate_and_a_routine_one_keeps_it(env):
    from ttp import coordinator as coord
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    hot, _ = _gate_add(p, "hot", 1)
    cold, _ = _gate_add(p, "cold", 3)
    notes = coord.apply(p, [{"type": "task_update", "id": hot, "start_after": "4h"},
                            {"type": "task_update", "id": cold, "start_after": "4h"}]) + list(p.db.kv(coord.NOTES_KEY, []))
    assert deferral(p.db.task(hot)) == {} and any(f"#{hot}" in n and "dropped" in n for n in notes), notes
    assert deferral(p.db.task(cold)).get("after"), "a p3 task keeps its delay"
    coord.apply(p, [{"type": "task_update", "id": cold, "start_after": "4h", "why": "after the box reboot"}])
    coord.apply(p, [{"type": "task_update", "id": cold, "priority": 1}])
    assert deferral(p.db.task(cold)).get("after"), "its recorded safety class survives a raise to p1"


def _parked(p, title, priority, after_s=None, when=None, why=None):
    """A stored queued task parked on a gate, as an older harness wrote it (no start_safety label)."""
    from ttp.coordinator import defer_labels
    after = time.time() + after_s if after_s else None
    return p.db.add_task(title, "s", kind="work", tier="light", origin="coordinator", priority=priority,
                         labels=defer_labels(after, when, why), not_before=after)


def test_the_tick_sweep_releases_parked_urgent_tasks_once_and_keeps_safety_and_routine_gates(env):
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    slept = _parked(p, "slept", 1, after_s=6 * 3600)
    probed = _parked(p, "probed", 1, when="test -e /tmp/never-x", why="run after the other one")
    lull = _parked(p, "lull", 1, when="ttp lock --probe dev-x", why="a lull on the device for a restart")
    user = _parked(p, "user", 1, when="test -e /tmp/proj/user-ok")
    routine = _parked(p, "routine", 3, after_s=6 * 3600)
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    for tid in (slept, probed):
        t = p.db.task(tid)
        assert deferral(t) == {} and t["not_before"] is None, tid
    for tid in (lull, user, routine):
        assert deferral(p.db.task(tid)), tid
    ev = p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'urgent_gate_released:%' ORDER BY task")
    assert [(e["task"], e["severity"], e["status"]) for e in ev] == [(slept, "low", "handled"),
                                                                     (probed, "low", "handled")], "one low, no wake"
    d.release_urgent_gates(time.time())
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'urgent_gate_released:%'")) == 2, "once"


def test_a_ready_urgent_task_held_by_its_kept_gate_with_a_free_slot_raises_one_event(env, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    tid = _parked(p, "held hot", 1, when="ttp lock --probe dev-x", why="a device lull for a restart")
    _parked(p, "held cold", 3, when="ttp lock --probe dev-x", why="a device lull for a restart")
    dep = p.db.add_task("first", "s", kind="work", tier="light", origin="user", priority=3)
    waits = _parked(p, "waits on dep", 1, when="ttp lock --probe dev-x", why="a device lull")
    p.db.update_task(waits, depends_on=[dep])
    p.db.update_task(dep, status="blocked")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.update_gates()
    start = time.time()
    raised = lambda: p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'slot_wait:%'")   # noqa: E731
    for minutes in (0, 5, 9, 11, 20, 40):
        d._slot_wait_checked = start + minutes * 60 - 60
        monkeypatch.setattr(time, "time", lambda: start + minutes * 60)
        d.check_slot_wait()
        if minutes == 9:
            assert raised() == []
    ev = raised()
    assert [e["task"] for e in ev] == [tid], "once, only the urgent task whose dependencies are met"
    assert "start gate (shared device or machine)" in ev[0]["text"] and "worker slot is free" in ev[0]["text"]


def test_a_review_behind_its_own_suite_gate_runs_beside_it_unless_it_reads_its_output(env):
    from ttp.daemon import Daemon
    from ttp.db import dependency_ids
    p = make(env)
    _no_events(p)
    code = p.db.add_task("change", "s", kind="code", tier="light", origin="user", priority=3)
    p.db.update_task(code, status="review")
    mk = lambda title: p.db.add_task(title, "s", kind="work", tier="light", origin="user", priority=3)   # noqa: E731
    suite, other, build = mk(f"run the suite for #{code}"), mk("unrelated tests"), mk(f"build the docs for #{code}")
    add = lambda title, spec, deps: p.db.add_task(title, spec, kind="review", tier="light", origin="daemon",   # noqa: E731
                                                  priority=3, depends_on=deps)
    beside = add(f"Review #{code}: change", "check it", [code, suite])
    reads = add(f"Review #{code}: change (2)", f"compare against #{suite}'s results", [code, suite])
    foreign = add(f"Review #{code}: change (3)", "s", [code, other])
    needs = add(f"Review #{code}: change (4)", "s", [code, build])
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    assert dependency_ids(p.db.task(beside)) == [code], "runs side by side with its own suite"
    for r, dep in ((reads, suite), (foreign, other), (needs, build)):
        assert dep in dependency_ids(p.db.task(r)), r
    ev = p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'review_suite_gate:%'")
    assert [(e["task"], e["severity"]) for e in ev] == [(beside, "low")]
    d.release_urgent_gates(time.time())
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'review_suite_gate:%'")) == 1


def test_a_review_keeps_its_dependency_on_a_code_task_that_changes_its_subject(env):
    """Only a task that runs checks and changes nothing is a suite gate: a code (or harness) task, or
    one with a branch, whose title says tests or checks changes the subject, and the review waits."""
    from ttp.daemon import Daemon
    from ttp.db import dependency_ids
    p = make(env)
    _no_events(p)
    a = p.db.add_task("change", "s", kind="code", tier="light", origin="user", priority=3)
    p.db.update_task(a, status="done")
    fix = p.db.add_task(f"code: add the missing tests to #{a}'s branch", "s", kind="code", tier="light",
                        origin="user", priority=3)
    hx = p.db.add_task(f"fix the checks for #{a}", "s", kind="harness", tier="light", origin="user", priority=3)
    br = p.db.add_task(f"run the suite for #{a}", "s", kind="work", tier="light", origin="user", priority=3)
    p.db.update_task(br, branch=f"ttp/t{br}-suite")
    rs = [p.db.add_task(f"Review #{a}: change ({i})", f"Independently review the change of code task #{a}",
                        kind="review", tier="light", origin="daemon", priority=3, depends_on=[dep])
          for i, dep in enumerate((fix, hx, br))]
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    for r, dep in zip(rs, (fix, hx, br)):
        assert dep in dependency_ids(p.db.task(r)), (r, dep)
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'review_suite_gate:%'")

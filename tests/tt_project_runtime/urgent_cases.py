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
    assert urgent.extra_slots({}) == 2 and urgent.extra_slots({"budget": {"urgent_extra_slots": 0}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": -2}}) == 0
    assert urgent.extra_slots({"budget": {"urgent_extra_slots": "x"}}) == 2


def test_only_the_harness_template_merge_task_leads_the_urgent_lane():
    """urgent.leads: the harness task `ttp upgrade` queues to finish a template merge is urgent at its
    priority 2 and sorts before priority-1 work; the same title on another kind, a near title, or another
    harness task is not."""
    from ttp import release, urgent
    merge = {"id": 9, "kind": "harness", "title": release.UPGRADE_TASK_TITLE, "priority": 2, "spec": "s"}
    assert urgent.leads(merge) and urgent.is_urgent(merge) and urgent.why(merge) == "finishes a template upgrade"
    assert urgent.why({**merge, "spec": "urgent: a demo"}) == "a demo"
    for t in ({**merge, "kind": "work"}, {**merge, "kind": "code"}, {**merge, "title": release.UPGRADE_TASK_TITLE + "!"},
              {**merge, "title": release.UPGRADE_TASK_TITLE.lower()}, {**merge, "title": "Fix schedule x"},
              {**merge, "title": None}, {}, None):
        assert not urgent.leads(t), t
        assert not urgent.is_urgent(t), t
    p1 = [{"id": i, "kind": "work", "title": "p1", "priority": 1, "spec": "s"} for i in (1, 2)]
    routine = {"id": 3, "kind": "work", "title": "r", "priority": 2, "spec": "s"}
    assert [r["id"] for r in urgent.first(p1 + [routine, merge])] == [9, 1, 2, 3]


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


def test_convgate_keeps_user_cited_and_restriction_gates_before_device_words():
    """A gate citing the user's word or naming a guard, Restriction or charter rule is a safety class,
    filed under it before any device word; a plain convenience gate and a mixed landed probe still drop."""
    from ttp import convgate
    p1 = {"priority": 1, "spec": "s\nurgent: soon"}
    for why, when, cls in (
            ("the fix is in (user msg #123): no-reset-guard passes", "bin/no-reset-guard", "the user's word"),
            ("as asked (user, 2026-01-02): not on the device before it lands", None, "the user's word"),
            ("user's words (verbatim): 'not before the release'", None, "the user's word"),
            (None, "grep -q ok /tmp/x  # user msgs #5, #6", "the user's word"),
            ("the Restriction on pushes holds this", None, "charter restriction"),
            ("a charter rule: no rollout until the box drains", None, "charter restriction"),
            (None, "bin/no-warm-reset-guard >/dev/null", "charter restriction"),
            ("needs the device free", "ttp lock --probe dev-x", "shared device or machine")):
        assert convgate.safety_class(why, when) == cls, (why, when)
        assert convgate.judge(p1, None, when or "true", why) == (False, cls), (why, when)
    for why, when in (("later is tidier", "test -e /tmp/ready"), (None, "landed:#7 && test -e /tmp/ready"),
                      ("the users table is nicer sorted", None), ("message the team after", None),
                      ("wait for a lull in live traffic", None)):   # a lull is no safety class (user msg #1571)
        assert convgate.judge(p1, None, when or "true", why) == (True, None), (why, when)
    # A bare claim of the user's word cites no message of theirs: it drops, or files under a class it names.
    for why in ("waits for the user's ok on the pacing", "until the user decides the landing order",
                "the user's approval is needed to narrow the test gate"):
        assert convgate.judge(p1, None, "true", why) == (True, None), why
    assert convgate.safety_class("until the user says the device is free", None) == "shared device or machine"


def test_convgate_files_owner_orders_and_cited_message_ids_under_the_users_word():
    """A gate why citing a message id ('msg #N', '(#N)' next to user or owner) or the owner's order,
    agent or quiet window is the user's word; a shared device or machine with a hold, quiet window or
    measurement is a safety class; a plain convenience why still drops."""
    from ttp import convgate
    p1 = {"priority": 1, "spec": "s\nurgent: soon"}
    for why in ("under the owner's order (user msg #1608, via the owner's agent), the account test pool must "
                "start no check run on gx-7 while a device test or soak runs there",
                "the owner's quiet window on gx-7 ends at 00:00",   # the #6009 gate why, alias replaced
                "the owner's agent asked for this order", "per msg #1690, not before midnight",
                "the user's order (#1690) holds it until then", "as the owner said (#12): after the window"):
        assert convgate.judge(p1, 1e12, None, why) == (False, "the user's word"), why
    for why in ("a hold on the shared device until the soak ends", "the shared machine's quiet window",
                "a measurement runs on the shared device"):
        assert convgate.judge(p1, 1e12, None, why) == (False, "shared device or machine"), why
    for why in ("starts after the reviewer is free", "the owner of the table prefers it sorted",
                "a quiet window is tidier", "after item (#4) in the list"):
        assert convgate.judge(p1, 1e12, None, why) == (True, None), why


def test_a_dropped_gate_note_quotes_the_why_it_judged(env):
    from ttp.daemon import Daemon
    p = make(env)
    _no_events(p)
    tid, notes = _gate_add(p, "p1 tidy", 1, "s", start_after="2h", why="after lunch is tidier")
    assert any(f"#{tid}" in n and "dropped" in n and "'after lunch is tidier'" in n for n in notes), notes
    tid, notes = _gate_add(p, "p1 owner", 1, "s", start_after="2h", why="the owner's quiet window on gx-7 ends at 00:00")
    assert not any("dropped" in n for n in notes) and "start_safety:the user's word" in p.db.task(tid)["labels"], notes
    parked = _parked(p, "parked", 1, when="test -e /tmp/never-q", why="nicer after the standup")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    ev = p.db.q("SELECT text FROM events WHERE fingerprint LIKE ?", (f"urgent_gate_released:{parked}:%",))
    assert ev and "'nicer after the standup'" in ev[0]["text"], ev


def test_urgent_gates_citing_the_user_or_a_restriction_survive_task_add_and_the_tick_sweep(env):
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p = make(env)
    _no_events(p)
    tid, notes = _gate_add(p, "p1 cited", 1, start_when="test -e /tmp/never-y",
                           why="user msg #123: wait until no-reset-guard passes")
    assert deferral(p.db.task(tid)) and "start_safety:the user's word" in json.loads(p.db.task(tid)["labels"])
    assert not any("dropped" in n for n in notes), notes
    cited = _parked(p, "cited", 1, when="test -e /tmp/never-z", why="per user msg #9, after the guard passes")
    guard = _parked(p, "guard", 1, when="bin/no-reset-guard")
    rule = _parked(p, "rule", 1, when="test -e /tmp/never-r", why="a Restriction keeps it off until then")
    plain = _parked(p, "plain", 1, when="test -e /tmp/never-p", why="after lunch")
    chained = _parked(p, "chained", 1, when="landed:#1 && test -e /tmp/never-c")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    for t in (cited, guard, rule):
        assert deferral(p.db.task(t)), t
    for t in (plain, chained):
        assert deferral(p.db.task(t)) == {}, t


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


def _urgent_sleeper(p, probe, retry_s=3600, urgent_line=True):
    """A task asleep on a worker's `retry_after_s` of `retry_s` with `probe` as its retry_when."""
    now = time.time()
    tid = p.db.add_task("finish the step", "do it\nurgent: the user is waiting" if urgent_line else "do it",
                        kind="work", tier="standard", origin="user")
    p.db.update_task(tid, not_before=now + retry_s, result=json.dumps(
        {"status": "waiting", "summary": "step running", "waiting_for": "the step", "retry_after_s": retry_s,
         "retry_when": probe, "waiting_since": now - 60}))
    return tid


def _one_poll_later(d, tid):
    """The daemon's clock one waiting.poll_s past the probe's last run, then a pass of the probe loop."""
    from ttp import waitpoll
    d._probed[tid] = time.time() - waitpoll.poll_s(d.cfg)
    _settle_probe(d, tid)


def test_wait_polls_default_to_minutes_and_cap_only_urgent_state_waits():
    from ttp import waitpoll
    assert 60 <= waitpoll.poll_s({}) <= 120 and waitpoll.urgent_cap_s({}) == 300
    assert waitpoll.poll_s({"waiting": {"poll_s": 1}}) == waitpoll.MIN_POLL_S
    assert waitpoll.poll_s({"waiting": {"poll_s": 9999}}) == waitpoll.MAX_POLL_S
    assert waitpoll.poll_s({"waiting": {"poll_s": "x"}}) == waitpoll.POLL_S
    hot, cold = {"priority": 1}, {"priority": 3, "spec": "routine"}
    job = {"retry_when": "ttp detach --check run/job.rc"}
    assert waitpoll.capped({}, hot, job, 3600) == 300
    assert waitpoll.capped({}, {"priority": 3, "spec": "urgent: now"}, job, 3600) == 300
    assert waitpoll.capped({}, hot, job, 200) == 200, "a shorter retry stays"
    assert waitpoll.capped({}, cold, job, 3600) == 3600, "routine work keeps its backoff"
    assert waitpoll.capped({}, hot, {}, 3600) == 3600, "no probe: nothing the daemon can read"
    assert waitpoll.capped({}, hot, {"retry_when": 'test "$(date +%s)" -ge 1900000000'}, 3600) == 3600
    assert waitpoll.capped({"waiting": {"urgent_retry_cap_s": 0}}, hot, job, 3600) == 3600, "0: off"
    assert waitpoll.capped({"waiting": {"urgent_retry_cap_s": 120}}, hot, job, 3600) == 120


@pytest.mark.parametrize("urgent_line,probe,capped", [
    (True, "exit 1", True), (False, "exit 1", False),
    (True, 'test "$(date +%s)" -ge 1900000000', False)])
def test_an_urgent_waiting_hand_off_caps_a_60_min_retry(env, monkeypatch, urgent_line, probe, capped):
    """An urgent worker's hour-long retry on state the daemon can read sleeps at most the cap; routine work
    and a bare clock gate keep the worker's timer."""
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "step runs", "waiting_for": "the step", "retry_after_s": 3600,
         "retry_when": probe}))
    p = make(env)
    tid = p.db.add_task("finish the step", "do it\nurgent: the user is waiting" if urgent_line else "do it",
                        kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "queued" and p.db.task(tid)["not_before"]
                      and not p.db.q("SELECT id FROM runs WHERE status='running'"))
    left = p.db.task(tid)["not_before"] - time.time()
    assert (left <= 300 + 5) == capped and left > 200, left


def test_an_urgent_task_wakes_within_one_poll_of_its_lock_freeing_and_sleeps_model_free_until(env, tmp_path):
    from ttp import daemon as dmod
    p = make(env)
    d = dmod.Daemon(p.base)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    try:
        _wait_for_file(held, holder)
        tid = _urgent_sleeper(p, "ttp lock --probe board")
        _settle_probe(d, tid)
        assert not _ready(p, tid), "a busy lock is not yet"
        # Its capped timer runs out with the lock still busy: back to sleep for the cap, no run.
        p.db.update_task(tid, not_before=time.time() - 1)
        d.probe_waiting()
        task = p.db.task(tid)
        assert not _ready(p, tid) and 200 < task["not_before"] - time.time() <= 300 + 5
        assert "woke" not in json.loads(task["result"]) and not p.db.q("SELECT id FROM runs WHERE task=?", (tid,))
    finally:
        release.touch()
        holder.wait(timeout=30)
    d._probed[tid] = time.time() - 5
    d.probe_waiting()
    assert tid not in d._probes and not _ready(p, tid), "the next poll waits its interval"
    _one_poll_later(d, tid)
    assert _ready(p, tid), "the freed lock did not wake it within one poll"


def test_an_urgent_task_wakes_within_one_poll_of_its_detached_job_ending(env, tmp_path):
    from ttp import daemon as dmod
    p = make(env)
    d = dmod.Daemon(p.base)
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    flag = tmp_path / "end"
    r = _ttp_run(p, "detach", "job", "--", *_until(flag), env={
        "TTP_HOME": str(env["home"]), "TTP_HOST": "testhost", "TTP_RUN_DIR": str(run_dir),
        "TTP_TASK": "41", "TTP_RUN_ID": "7"})
    assert r.returncode == 0, r.stderr
    rc = (run_dir / "job.rc").resolve()
    tid = _urgent_sleeper(p, f"ttp detach --check {rc}")
    try:
        for _ in range(2):
            _one_poll_later(d, tid)
            assert not _ready(p, tid), "a running job is not yet"
        assert not p.db.q("SELECT id FROM runs WHERE task=?", (tid,)), "polls that saw no change ran no model"
    finally:
        flag.touch()
    assert _until_true(rc.exists), "the job did not end"
    _one_poll_later(d, tid)
    assert _ready(p, tid), "the ended job did not wake it within one poll"


def test_the_coordinator_and_plan_prompts_split_an_urgent_goal_into_tasks_that_start_at_once(env):
    """An urgent goal's tasks run side by side: the coordinator's role prompt and the plan template
    allow a dependency between them only where one needs another's new file or symbol."""
    from ttp.prompts import coordinator_role
    role = " ".join(coordinator_role(make(env)).split())
    assert ("Split an urgent goal into independent tasks that start at once: `depends_on` or `start_when` "
            "between them only where one needs another's new file or symbol.") in role
    plan = " ".join((RUNTIME.parent / "template" / "prompts" / "kind-plan.md").read_text().split())
    assert ("An urgent goal (priority 1 or an `urgent:` line): every task starts at once. No chain between its "
            "tasks unless one needs another's new file or symbol") in plan


def _urgent_done(env, p, title, fups, spec="", priority=3, labels=(), reply_chat=None, metrics=None, setup=None,
                 kind="harness"):
    """A `kind` task (spec `spec`, `labels`, `reply_chat`) that handed off done with `fups`; its id."""
    tid = p.db.add_task(title, spec or f"spec of {title}", kind=kind, tier="deep", origin="coordinator",
                        priority=priority, labels=list(labels))
    p.db.update_task(tid, status="running", **({"reply_chat": reply_chat} if reply_chat else {}))
    if setup:
        setup(tid)
    return _urgent_hand_off(env, p, tid, title, fups, metrics)


def _urgent_hand_off(env, p, tid, title, fups, metrics=None):
    """Running task `tid` hands off done with `fups` and `metrics`, at a cost of $1; its id."""
    import json
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": f"{title}: checked",
                                                     "metrics": metrics or {}, "followups": fups}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid


def _proposed(p, tid):
    return p.db.q("SELECT * FROM events WHERE kind='followup_proposed' AND task=? ORDER BY id", (tid,))


def test_an_urgent_tasks_next_steps_are_queued_at_once_with_what_it_carries(env):
    """An urgent task's `continue:` and `next step:` follow-ups (no progress metric needed) are queued by the
    daemon in its hand-off step: priority, the `urgent:` line, resource labels, reply_chat; the digest notice
    names them and nothing waits on the coordinator. Its upstream note keeps its own handling."""
    import json
    from ttp import chaincap
    p = make(env)
    tid = _urgent_done(env, p, "Bring the board up", [
        {"title": "continue: bring the board up, part 2", "spec": "Flash the rest."},
        {"title": "next step: rerun the sweep", "spec": "Sweep again."}],
        spec="Bring it up.\nurgent: the user is waiting on the board", priority=2,
        labels=["resource:boxq", "exclusive:boxq-dev"], reply_chat="chat-9")
    kids = p.db.q("SELECT * FROM tasks WHERE parent=? ORDER BY id", (tid,))
    assert [k["title"] for k in kids] == ["continue: bring the board up, part 2", "next step: rerun the sweep"]
    for k in kids:
        assert k["priority"] == 2 and k["status"] == "queued" and k["reply_chat"] == "chat-9", dict(k)
        assert "urgent: the user is waiting on the board" in k["spec"]
        assert {"resource:boxq", "exclusive:boxq-dev", f"follows:{tid}"} <= set(json.loads(k["labels"]))
    assert all(e["status"] == "handled" for e in _proposed(p, tid)), [dict(e) for e in _proposed(p, tid)]
    (done,) = p.db.q("SELECT * FROM events WHERE kind='task_done' AND task=?", (tid,))
    assert f"Follow-up #{kids[0]['id']} queued by the daemon (urgent: the user is waiting" in done["text"]
    assert "NOT queued" not in done["text"] and done["status"] == "handled", dict(done)
    assert any(f"#{kids[1]['id']}" in n for n in chaincap.notices(p.db))


def test_an_obligation_owners_follow_up_is_queued_and_attached_to_it(env):
    from ttp import obligations
    p = make(env)
    held = {}
    tid = _urgent_done(env, p, "Measure the link", [{"title": "continue: measure the link, part 2", "spec": "More."}],
                       setup=lambda t: held.setdefault("o", obligations.add(p, "the link numbers", owners=[t])))
    (kid,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (tid,))
    assert kid["status"] == "queued" and kid["priority"] == 3
    assert str(kid["id"]) in obligations.get(p.db, held["o"])["owners"], obligations.get(p.db, held["o"])
    (done,) = p.db.q("SELECT text FROM events WHERE kind='task_done' AND task=?", (tid,))
    assert f"queued by the daemon (owner of obligation {held['o']})" in done["text"], done["text"]


def test_a_routine_tasks_continue_stays_proposed_and_wakes_the_coordinator(env):
    """Not urgent, owning nothing, no progress metric: the old path, proposed and NOT queued."""
    p = make(env)
    tid = _urgent_done(env, p, "Tidy the docs", [{"title": "continue: tidy the docs, part 2", "spec": "More."}])
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    (ev,) = _proposed(p, tid)
    assert ev["status"] == "queued" and "NOT queued by the daemon" in ev["text"], dict(ev)


def test_a_refused_urgent_follow_up_stays_proposed_with_its_reason(env):
    """The chain caps, a pause and the setting still hold: each keeps the follow-up proposed, with the reason in
    its followup_proposed event, which wakes the coordinator."""
    p = make(env)
    spec = "Do it.\nurgent: the user is waiting"
    fup = [{"title": "continue: do it, part 2", "spec": "More."}]
    p.set_config("chains.max_usd", 0.5)
    tid = _urgent_done(env, p, "Capped work", fup, spec=spec, setup=lambda t: p.db.update_task(t, spent_usd=2.0))
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    (ev,) = _proposed(p, tid)
    assert ev["status"] == "queued" and "NOT queued by the daemon" in ev["text"] and "max_usd" in ev["text"], ev["text"]
    p.set_config("chains.max_usd", 40)
    p.db.set_kv("paused", True)
    tid = _urgent_done(env, p, "Paused work", fup, spec=spec)
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    assert "the project is paused" in _proposed(p, tid)[0]["text"]
    p.db.set_kv("paused", False)
    p.set_config("chains.urgent_followups", False)
    tid = _urgent_done(env, p, "Opted out", fup, spec=spec)
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    assert _proposed(p, tid)[0]["status"] == "queued"


def test_a_two_wide_urgent_fan_out_stays_within_max_usd_in_total(env):
    """Each queued follow-up is urgent too: chains.max_usd bounds the whole tree under the chain root (each
    unfinished task counted at its budget), not one path. The follow-ups past it stay proposed with the reason."""
    p = make(env)
    p.set_config("chains.max_usd", 4)
    tid = _urgent_done(env, p, "Wide work", [{"title": f"continue: wide work, part {k}", "spec": "More."} for k in "ab"],
                       spec="Do it.\nurgent: the user is waiting", metrics={"progress": 1},
                       setup=lambda t: p.db.update_task(t, budget_usd=1.0, spent_usd=1.0))
    for n in range(2, 40):
        (kid,) = p.db.q("SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1") or (None,)
        if kid is None:
            break
        p.db.update_task(kid["id"], status="running", spent_usd=1.0)
        _urgent_hand_off(env, p, kid["id"], kid["title"], [
            {"title": f"continue: wide work #{kid['id']}, part {k}", "spec": "More."} for k in "ab"], {"progress": n})
    spent = p.db.one("SELECT COUNT(*) n, SUM(spent_usd) usd FROM tasks")
    assert spent["usd"] <= 4 and spent["n"] <= 4, dict(spent)
    texts = [e["text"] for e in p.db.q("SELECT text FROM events WHERE kind='followup_proposed' AND status='queued'")]
    assert any(f"the follow-up tree of #{tid}" in t and "chains.max_usd is $4" in t for t in texts), texts


def test_an_urgent_split_code_task_queues_its_carrying_continue_not_a_next_step_first(env):
    """On a split code task only the continue that carries the change takes the branch over, even listed after
    a `next step:`; the next step stays proposed with the reason."""
    import json
    from ttp import chaincap
    p = make(env)
    tid = _urgent_done(env, p, "Split work", [
        {"title": "next step: measure the link", "spec": "Measure."},
        {"title": "continue: split work, part 2", "spec": "Finish the change."}], kind="code",
        spec="Do it.\nurgent: the user is waiting", labels=[f"{chaincap.SPLIT_LABEL}{'a' * 40}"])
    kids = p.db.q("SELECT * FROM tasks WHERE parent=? AND kind!='review' ORDER BY id", (tid,))
    assert [k["title"] for k in kids] == ["continue: split work, part 2"], [dict(k) for k in kids]
    assert f"continues:{tid}" in json.loads(kids[0]["labels"])
    (ev,) = [e for e in _proposed(p, tid) if "measure the link" in e["text"]]
    assert ev["status"] == "queued" and f"#{tid} is a split" in ev["text"], ev["text"]


def test_an_urgent_follow_up_of_another_kind_stays_with_the_coordinator(env):
    """auto_hop queues the parent's kind: a follow-up asking for another kind stays proposed with the reason."""
    p = make(env)
    tid = _urgent_done(env, p, "Kind work", [
        {"title": "next step: ask which board to use", "spec": "Ask.", "kind": "question"},
        {"title": "next step: rerun it", "spec": "Again."}], spec="Do it.\nurgent: the user is waiting")
    assert [k["title"] for k in p.db.q("SELECT title FROM tasks WHERE parent=?", (tid,))] == ["next step: rerun it"]
    (ev,) = [e for e in _proposed(p, tid) if "which board" in e["text"]]
    assert ev["status"] == "queued" and "its kind question is not" in ev["text"], ev["text"]


def test_an_urgent_gate_made_only_of_landed_checks_is_a_dependency_and_stays(env):
    """A start_when made only of `landed:#<id>` and `ttp landed --task <id>` terms joined by && waits on
    other tasks' landed work: an urgent task keeps it whatever its why says, at task_add, task_update and
    the tick sweep, and the note says why. A convenience probe or a mixed one still drops."""
    from ttp import convgate, coordinator as coord
    from ttp.daemon import Daemon
    from ttp.db import deferral
    p1 = {"priority": 1, "spec": "s"}
    chain = "ttp landed --task 5924 && ttp landed --task 5925 && landed:#5926 && ttp landed --task=5927"
    for when in (chain, "landed:#7", "ttp landed --task 7", " landed:#3 &&landed:#4 "):
        assert convgate.landed_only(when), when
        assert convgate.judge(p1, None, when, "the batch is in") == (False, convgate.LANDED), when
    for when in ("", "landed:#7 && test -e /tmp/x", "landed:#7 || true", "landed:#7 &&", "ttp landed --task 7; true",
                 "ttp landed --task 7 --branch b", "landed:#7 && && landed:#8", "test -e /tmp/landed:#7",
                 "ttp landed --task abc"):
        assert not convgate.landed_only(when), when
    assert convgate.judge(p1, None, "landed:#7 && test -e /tmp/x", "tidier") == (True, None)
    p = make(env)
    _no_events(p)
    first, second = (p.db.add_task(x, "s", kind="code", tier="light", origin="user", priority=3) for x in "ab")
    p.set_config("delivery.landed_probe", "test -e landed/{id}")   # how landed:#<id> is checked here
    both = f"ttp landed --task {first} && ttp landed --task {second}"
    tid, notes = _gate_add(p, "p1 after batch", 1, start_when=both, why="after the batch")
    t = p.db.task(tid)
    assert deferral(t).get("when") and f"start_safety:{convgate.LANDED}" in json.loads(t["labels"]), t["labels"]
    assert any(f"#{tid}" in n and "kept" in n and "only landed checks" in n for n in notes), notes
    assert not any("dropped" in n for n in notes), notes
    tid2, notes = _gate_add(p, "p1 tidy", 1, start_when="test -e /tmp/never-t", why="after the batch")
    assert deferral(p.db.task(tid2)) == {} and any("dropped" in n and "not only landed checks" in n for n in notes)
    hot, _ = _gate_add(p, "hot", 1)
    notes = coord.apply(p, [{"type": "task_update", "id": hot, "start_when": f"landed:#{first}", "why": "x"}]) \
        + list(p.db.kv(coord.NOTES_KEY, []))
    assert deferral(p.db.task(hot)).get("when") and any(f"#{hot}" in n and "kept" in n for n in notes), notes
    parked = _parked(p, "parked", 1, when=f"landed:#{first} && ttp landed --task {second}", why="nicer in order")
    mixed = _parked(p, "mixed", 1, when=f"landed:#{first} && test -e /tmp/never-m", why="nicer in order")
    d = Daemon(p.base)
    d.cfg = p.config()
    d.release_urgent_gates(time.time())
    assert deferral(p.db.task(parked)).get("when"), "a landed-only probe stays"
    assert deferral(p.db.task(mixed)) == {}, "a mixed probe follows the why"
    ev = p.db.q("SELECT * FROM events WHERE fingerprint LIKE 'urgent_gate_released:%'")
    assert [e["task"] for e in ev] == [mixed] and "not only landed checks" in ev[0]["text"]


def test_queue_priority_ranks_user_ordered_urgent_work_0_other_urgent_work_1_and_routine_none():
    from ttp import urgent
    now = time.time()
    ordered = {"id": 1, "priority": 3, "spec": "s\nurgent: the user ordered it", "created": now}
    stale = dict(ordered, created=now - urgent.STALE_S - 60)
    assert urgent.queue_priority(ordered, now) == urgent.ORDERED_PRIORITY == 0
    assert urgent.queue_priority(stale, now) == urgent.PRIORITY, "a stale line is urgent, not user-ordered"
    assert urgent.queue_priority({"id": 2, "priority": 1, "spec": "s"}, now) == urgent.PRIORITY
    assert urgent.queue_priority({"id": 3, "priority": 2, "spec": "s\nurgent: no"}, now) is None
    assert urgent.queue_priority(None, now) is None
    assert urgent.queue_priority({"id": 4, "kind": "harness", "title": urgent.UPGRADE_TASK_TITLE}, now) == 0


def _topup_daemon(p, monkeypatch):
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.jev = None
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k) or 0)
    return d, started


def _spent_task(p, title, priority=3, spec="s", origin="coordinator", reply_chat=None, budget=2.0):
    tid = p.db.add_task(title, spec, kind="work", tier="light", priority=priority, origin=origin,
                        reply_chat=reply_chat, budget_usd=budget)
    p.db.update_task(tid, spent_usd=budget)
    return tid


def _budget_run(p, tid, started, status="budget"):
    rid = p.db.x("INSERT INTO runs(task, role, status, started, ended, note) VALUES(?,?,?,?,?,?)",
                 (tid, "worker", status, started, started + 60, "{}"))
    return dict(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)))


def test_an_urgent_or_user_ordered_task_at_its_budget_is_topped_up_and_started_model_free(env, monkeypatch):
    """(1) An urgent task (priority 1 or an `urgent:` line) or one made for the user's order (origin
    user, a chat it answers) with its budget spent is raised by its tier's default and started in the
    same tick: one handled event on the task, one low notice, no coordinator wake. An older budget
    hold of one is released; status, the web payload and the digest show the top-up."""
    from ttp import topup
    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    hot = _spent_task(p, "urgent fix", priority=1)
    line = _spent_task(p, "urgent line", spec="Fix it.\nurgent: the user is waiting")
    ordered = _spent_task(p, "user order", origin="user")
    chat = _spent_task(p, "chat answer", reply_chat="c1")
    held = _spent_task(p, "held urgent", priority=1)
    p.db.update_task(held, status="blocked", blocked_reason="task budget exhausted")
    d.dispatch()
    ids = {k["task"]["id"] for k in started}
    assert {hot, line, ordered, chat, held} <= ids, started
    for tid in (hot, line, ordered, chat, held):
        t = p.db.task(tid)
        assert t["budget_usd"] == 4.0 and t["status"] == "running", t   # light default $2 on top of $2 spent
        (k,) = [k for k in started if k["task"]["id"] == tid]
        assert k["budget_usd"] == 2.0
        (ev,) = topup.events(p.db, tid)
        assert ev["data"]["before"] == 2.0 and ev["data"]["after"] == 4.0 and ev["data"]["n"] == 1
    assert not p.db.q("SELECT 1 FROM events WHERE kind IN (?, 'task_budget_exhausted') AND status != 'handled'",
                      (topup.KIND,))
    notes = p.db.q("SELECT text, severity FROM messages WHERE direction='out' AND text LIKE '%budget top-up%'")
    assert len(notes) == 5 and {n["severity"] for n in notes} == {"low"}
    assert topup.counts(p.db)[hot] == {"n": 1, "usd": 2.0}
    assert "budget top-up" not in "".join(topup.digest_lines(p.db, time.time() + 1))
    assert any(f"#{hot} urgent fix" in x for x in topup.digest_lines(p.db, 0))
    from ttp import web
    row = next(t for t in web.state_payload(p, p.db)["tasks"] if t["id"] == hot)
    assert row["budget_topups"] == {"n": 1, "usd": 2.0}
    # The web app renders it on the task card, in topup.words' words.
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "esc(topupWords(t.budget_topups))" in js[js.index("function taskRow"):]
    node = shutil.which("node")
    if node:
        fn = js[js.index("const topupWords ="):js.index("// A chain's metric")]
        cases = [{"n": 1, "usd": 2.0}, {"n": 2, "usd": 16}, None, {"n": 0, "usd": 0}]
        out = subprocess.run([node, "-e", fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(topupWords)))"],
                             capture_output=True, text=True, timeout=30)
        assert json.loads(out.stdout) == [topup.words(c) for c in cases], out.stderr
    # Turned off, an urgent task waits on its budget as before.
    d.cfg["budget"]["urgent_topup"] = False
    off = _spent_task(p, "urgent off", priority=1)
    d.dispatch()
    assert p.db.task(off)["status"] == "blocked" and not topup.events(p.db, off)


def test_an_urgent_run_stopped_by_its_budget_is_requeued_quietly_and_topped_up(env, monkeypatch, tmp_path):
    """(1) A run of an urgent task that its budget stopped goes back to the queue at once (no attempt,
    no delay, no coordinator wake); the next dispatch tops it up and starts it."""
    from ttp import topup
    from ttp.providers.base import RunUsage
    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    tid = _spent_task(p, "urgent fix", priority=1)
    p.db.update_task(tid, status="running")
    d._finish_worker(_budget_run(p, tid, time.time() - 120), RunUsage(cost_usd=2.0), "budget", tmp_path)
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 0 and not t["not_before"], t
    assert not p.db.q("SELECT kind FROM events WHERE task=? AND status='queued'", (tid,))
    d.dispatch()
    assert [k["task"]["id"] for k in started] == [tid] and p.db.task(tid)["budget_usd"] == 4.0
    assert len(topup.events(p.db, tid)) == 1


def test_an_urgent_run_stopped_by_the_agents_own_budget_cap_is_topped_up_and_guarded(env, monkeypatch, tmp_path):
    """(1b) Claude Code's own --max-budget-usd stop ends the run 'failed' with error_max_budget_usd, not
    'budget': it is the same budget stop. Requeued at once (no attempt, no delay, no coordinator wake),
    and two such runs across a top-up with no new commit trip the runaway guard. A routine task's
    failed run keeps its attempt and delay."""
    from ttp import topup
    from ttp.providers.base import RunUsage

    def capped():
        return RunUsage(cost_usd=2.0, error="error_max_budget_usd: Reached maximum budget",
                        extra={"subtype": "error_max_budget_usd"})

    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    monkeypatch.setattr(d, "_branch_head", lambda t: "abc123")
    now = time.time()
    tid = _spent_task(p, "urgent fix", priority=1)
    p.db.update_task(tid, status="running")
    first = _budget_run(p, tid, now - 600, "failed")
    d._finish_worker(first, capped(), "failed", tmp_path)
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 0 and not t["not_before"], t
    assert not p.db.q("SELECT kind FROM events WHERE task=? AND status='queued'", (tid,))
    assert topup.stopped_on_budget(p.db.one("SELECT * FROM runs WHERE id=?", (first["id"],)))
    topup.top_up(p.db, d.cfg, p.db.task(tid), head="abc123", now=now - 300)
    p.db.update_task(tid, status="running")
    d._finish_worker(_budget_run(p, tid, now - 200, "failed"), capped(), "failed", tmp_path)
    t = p.db.task(tid)
    assert t["status"] == "failed" and "runaway guard" in t["result"], t
    # Mixed causes (the runner's check, then the agent's cap) are the same repeat.
    mixed = _spent_task(p, "urgent mixed", priority=1)
    _budget_run(p, mixed, now - 600, "budget")
    topup.top_up(p.db, d.cfg, p.db.task(mixed), head="abc123", now=now - 300)
    p.db.update_task(mixed, status="running")
    d._finish_worker(_budget_run(p, mixed, now - 200, "failed"), capped(), "failed", tmp_path)
    assert p.db.task(mixed)["status"] == "failed"
    # A plain failure before the top-up is no budget stop: never the guard.
    plain = _spent_task(p, "urgent plain", priority=1)
    _budget_run(p, plain, now - 600, "failed")
    topup.top_up(p.db, d.cfg, p.db.task(plain), head="abc123", now=now - 300)
    p.db.update_task(plain, status="running")
    d._finish_worker(_budget_run(p, plain, now - 200, "failed"), capped(), "failed", tmp_path)
    assert p.db.task(plain)["status"] == "queued" and p.db.task(plain)["attempts"] == 0
    # A routine task's capped run: an attempt and a delay, as before.
    routine = _spent_task(p, "routine", priority=3)
    p.db.update_task(routine, status="running")
    d._finish_worker(_budget_run(p, routine, now - 100, "failed"), capped(), "failed", tmp_path)
    t = p.db.task(routine)
    assert t["status"] == "queued" and t["attempts"] == 1 and t["not_before"], t


def test_a_dollar_cap_still_stops_a_topped_up_urgent_task(env, monkeypatch):
    """(2) The top-up raises the task's budget; the dollar caps still hold it like any task."""
    import dataclasses
    from ttp import budget as bud, topup
    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    core = d.cfg.get("core_provider", "claude")
    gate = bud.evaluate(p.db, d.cfg, core, bud.plan_windows(p.db))
    d.gates[core] = dataclasses.replace(gate, regime="caps", numbers={**gate.numbers, "daily_cap": 1.5,
                                                                       "spent_24h": 0.0})
    tid = _spent_task(p, "urgent fix", priority=1)
    d.dispatch()
    t = p.db.task(tid)
    assert not started and t["status"] == "blocked" and "above the dollar cap" in t["blocked_reason"], t
    assert len(topup.events(p.db, tid)) == 1   # raised once, then held by the cap: not raised again
    d.dispatch()
    assert not started and len(topup.events(p.db, tid)) == 1
    # Under the cap but over what is left of it today: it waits queued, unstarted.
    d.gates[core] = dataclasses.replace(gate, regime="caps", numbers={**gate.numbers, "daily_cap": 10.0,
                                                                       "spent_24h": 9.5})
    p.db.update_task(tid, status="queued", blocked_reason=None)
    d.dispatch()
    assert not started and p.db.task(tid)["status"] == "queued"


def test_the_runaway_guard_fails_an_urgent_task_whose_budget_runs_repeat_across_a_top_up(env, monkeypatch, tmp_path):
    """(3) Two runs in a row stopped by their budget across a top-up, with no hand-off and no new
    commit: failed with that evidence, never topped up again nor held on budget. A new commit, a
    hand-off or a run of another kind before the top-up keeps it going."""
    from ttp import topup
    from ttp.providers.base import RunUsage
    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    head = ["abc123"]
    monkeypatch.setattr(d, "_branch_head", lambda t: head[0])
    now = time.time()

    def round_trip(tid, prev_status="budget"):
        """A run that ends `prev_status`, a top-up, then a run stopped by its budget; the task's status."""
        _budget_run(p, tid, now - 600, prev_status)
        topup.top_up(p.db, d.cfg, p.db.task(tid), head=head[0], now=now - 300)
        p.db.update_task(tid, status="running")
        d._finish_worker(_budget_run(p, tid, now - 200), RunUsage(cost_usd=2.0), "budget", tmp_path)
        return p.db.task(tid)

    stuck = _spent_task(p, "urgent stuck", priority=1)
    t = round_trip(stuck)
    assert t["status"] == "failed", t
    assert "runaway guard" in t["result"] and "no new commit (head abc123)" in t["result"]
    n = len(topup.events(p.db, stuck))
    d.dispatch()
    assert stuck not in {k["task"]["id"] for k in started} and len(topup.events(p.db, stuck)) == n
    # A new commit since the top-up is progress: it goes on.
    moved = _spent_task(p, "urgent moving", priority=1)
    _budget_run(p, moved, now - 600)
    topup.top_up(p.db, d.cfg, p.db.task(moved), head="old999", now=now - 300)
    p.db.update_task(moved, status="running")
    d._finish_worker(_budget_run(p, moved, now - 200), RunUsage(cost_usd=2.0), "budget", tmp_path)
    assert p.db.task(moved)["status"] == "queued"
    # The run before the top-up ended another way (a timeout): not the same failure twice.
    other = _spent_task(p, "urgent other", priority=1)
    assert round_trip(other, prev_status="timeout")["status"] == "queued"
    # The first budget stop has no top-up behind it: never the guard.
    first = _spent_task(p, "urgent first", priority=1)
    p.db.update_task(first, status="running")
    d._finish_worker(_budget_run(p, first, now - 100), RunUsage(cost_usd=2.0), "budget", tmp_path)
    assert p.db.task(first)["status"] == "queued"
    # A hand-off in the stopped run is no repeat.
    assert topup.runaway(p.db, p.db.task(stuck), _budget_run(p, stuck, now), head[0], handed_off=True) == ""


def test_a_routine_task_keeps_its_budget_hold_and_its_budget_stop_fails(env, monkeypatch, tmp_path):
    """(4) A task neither urgent nor ordered by the user: no top-up, the budget hold and its
    task_budget_exhausted event as before; a run its budget stopped fails."""
    from ttp import topup
    from ttp.providers.base import RunUsage
    p = make(env)
    d, started = _topup_daemon(p, monkeypatch)
    tid = _spent_task(p, "routine", priority=3, spec="Do it.\nurgent: no")
    d.dispatch()
    t = p.db.task(tid)
    assert not started and t["status"] == "blocked" and t["blocked_reason"] == "task budget exhausted"
    assert t["budget_usd"] == 2.0 and not topup.events(p.db, tid)
    assert p.db.q("SELECT 1 FROM events WHERE kind='task_budget_exhausted' AND task=?", (tid,))
    d.dispatch()   # its hold is not released for a top-up
    assert p.db.task(tid)["status"] == "blocked" and not started
    run = _spent_task(p, "routine run", priority=3)
    p.db.update_task(run, status="running")
    d._finish_worker(_budget_run(p, run, time.time() - 60), RunUsage(cost_usd=2.0), "budget", tmp_path)
    assert p.db.task(run)["status"] == "failed" and p.db.task(run)["attempts"] == 1

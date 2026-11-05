# Plan start-gate tests (plangate.py) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new plan-gate tests here, not there.
from __future__ import annotations


def _finish_plan(env, p, result, title="plan the routing work"):
    """A done plan run handing off `result`: (its id, its task_done event row)."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task(title, "plan it", kind="plan", tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "planned", **result}))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid, _plan_events(p, tid)


def _plan_events(p, tid):
    """All the task's events in one dict: its hand-off notes go in a task_notes event of their own."""
    return {"text": "\n".join(e["text"] for e in p.db.q("SELECT text FROM events WHERE task=? ORDER BY id", (tid,)))}


def test_plan_gate_parsing_takes_ids_and_a_probe_and_reports_bad_parts():
    from ttp import plangate
    assert plangate.parse(None) == (None, [])
    assert plangate.parse({}) == (None, [])
    gate, probs = plangate.parse({"depends_on": [12, "#13", "13"], "start_when": "  landed:#12 ",
                                  "why": "once the  persistent coordinator is in place"})
    assert gate == {"depends_on": [12, 13], "start_when": "landed:#12",
                    "why": "once the persistent coordinator is in place"} and probs == []
    gate, probs = plangate.parse({"depends_on": ["soon", 4]})
    assert gate["depends_on"] == [4] and "not a task id" in probs[0]
    # A probe that waits for the user's next message, or one that always passes, is no gate.
    for bad in ("test -f x; true", "landed:abc"):
        gate, probs = plangate.parse({"start_when": bad})
        assert gate is None and probs and "start_when" in probs[0], (bad, probs)
    assert plangate.parse({"start_when": "now"})[0] is None
    assert plangate.parse("after #4")[0] is None and plangate.parse("after #4")[1]


def test_a_done_plan_stores_its_gate_and_its_event_names_it(env):
    from ttp import plangate
    p = make(env)
    dep = p.db.add_task("persistent coordinator", "s", origin="user")
    tid, ev = _finish_plan(env, p, {"gate": {"depends_on": [dep, 99999], "start_when": "test -f ready",
                                             "why": "once the persistent coordinator is in place"}})
    gate = plangate.stored(p.db.task(tid))
    assert gate == {"depends_on": [dep], "start_when": "test -f ready",
                    "why": "once the persistent coordinator is in place"}
    assert f"cite `from plan #{tid}`" in ev["text"] and "#99999 names no other task" in ev["text"], ev["text"]
    # A later done hand-off with no gate clears it.
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}-2"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "replanned"}))
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    assert plangate.stored(p.db.task(tid)) is None


def test_task_add_citing_a_gated_plan_inherits_its_gate_anded_with_its_own(env):
    from ttp import coordinator as coord
    p = make(env)
    dep = p.db.add_task("persistent coordinator", "s", origin="user")
    own = p.db.add_task("routing code", "s", origin="user")
    plan, _ = _finish_plan(env, p, {"gate": {"depends_on": [dep], "start_when": "test -f ready",
                                             "why": "once the persistent coordinator is in place"}})
    assert coord.apply(p, [
        {"type": "task_add", "title": "bench", "spec": f"from plan #{plan}: run the head-to-head"},
        {"type": "task_add", "title": "injection", "spec": f"From plan #{plan}. Assess attacks.",
         "depends_on": [own], "start_when": "test -f other", "why": "after the routing code"},
        # An urgent task drops the gate's probe as a convenience gate: its why cites no user message.
        {"type": "task_add", "title": "urgent one", "spec": f"from plan #{plan}\nurgent: asked twice",
         "priority": 1}]) == []
    bench, inj, urg = (_added(p, t) for t in ("bench", "injection", "urgent one"))
    assert json.loads(p.db.task(bench)["depends_on"]) == [dep]
    assert "start_when:test -f ready" in _labels(p, bench)
    assert sorted(json.loads(p.db.task(inj)["depends_on"])) == sorted([own, dep])
    assert "start_when:test -f ready && test -f other" in _labels(p, inj), _labels(p, inj)
    why = next(lb for lb in _labels(p, inj) if lb.startswith("start_why:"))
    assert why == (f"start_why:once the persistent coordinator is in place (plan #{plan}'s start gate); "
                   f"after the routing code"), why
    assert not any(lb.startswith(("start_when:", "start_safety")) for lb in _labels(p, urg)), _labels(p, urg)
    assert json.loads(p.db.task(urg)["depends_on"]) == [dep]
    for t in (bench, inj, urg):
        assert not _ready(p, t)


def test_a_gate_citing_a_user_message_holds_an_urgent_task_too(env):
    from ttp import convgate
    from ttp import coordinator as coord
    p = make(env)
    plan, _ = _finish_plan(env, p, {"gate": {"start_when": "test -f ready",
                                             "why": "user msg #1725: only after the persistent coordinator"}})
    assert coord.apply(p, [{"type": "task_add", "title": "urgent one", "spec": f"from plan #{plan}\nurgent: now",
                            "priority": 1}]) == []
    labels = _labels(p, _added(p, "urgent one"))
    assert "start_when:test -f ready" in labels and f"start_safety:{convgate.CLASSES[0][0]}" in labels, labels
    # The plan's own name adds no class: a gate with no why is a convenience gate.
    assert convgate.safety_class(f"plan #{plan}'s start gate") is None


def test_a_landed_gate_and_a_compound_own_probe_and_together_safely(env):
    from ttp import plangate
    assert plangate.and_probes("landed:#7", "test -f a || exit 1") == "ttp landed --task 7 && (test -f a || exit 1)"
    assert plangate.and_probes("test -f a", "") == "test -f a"
    assert plangate.and_probes("test -f " + "a" * 600, "test -f " + "b" * 600) == ""


def test_a_gate_that_cannot_be_applied_whole_comes_back_as_a_note(env):
    from ttp import coordinator as coord
    from ttp import plangate
    p = make(env)
    dep = p.db.add_task("persistent coordinator", "s", origin="user")
    plan, _ = _finish_plan(env, p, {"gate": {"depends_on": [dep], "start_when": "test -f " + "r" * 600}})
    p.db.update_task(dep, status="cancelled")
    # The stored gate names a task that is gone (an older row) as well.
    gate = dict(plangate.stored(p.db.task(plan)), depends_on=[dep, 99999])
    p.db.update_task(plan, labels=[plangate.LABEL + json.dumps(gate)])
    assert coord.apply(p, [{"type": "task_add", "title": "bench", "spec": f"from plan #{plan}",
                            "start_when": "test -f " + "o" * 600}]) == []
    tid = _added(p, "bench")
    notes = " ".join(p.db.kv(coord.NOTES_KEY, []))
    assert "#99999, which names no task here" in notes, notes
    assert f"#{dep}, which is cancelled" in notes and "could not be ANDed" in notes, notes
    # The gate still holds it: the cancelled dependency stays, and the gate's probe is kept.
    assert json.loads(p.db.task(tid)["depends_on"]) == [dep]
    assert any(lb == "start_when:test -f " + "r" * 600 for lb in _labels(p, tid))


def test_an_ungated_plan_and_an_uncited_task_add_behave_as_before(env):
    from ttp import coordinator as coord
    from ttp import plangate
    p = make(env)
    plan, ev = _finish_plan(env, p, {"followups": [{"title": "a", "spec": "b"}]})
    assert plangate.stored(p.db.task(plan)) is None and "Start gate" not in ev["text"]
    gated, _ = _finish_plan(env, p, {"gate": {"start_when": "test -f ready"}}, title="another plan")
    assert coord.apply(p, [{"type": "task_add", "title": "free", "spec": f"from plan #{plan}"},
                           {"type": "task_add", "title": "other", "spec": f"see plan #{gated} for context"}]) == []
    for t in ("free", "other"):
        tid = _added(p, t)
        assert _ready(p, tid) and not any(lb.startswith("start_") for lb in _labels(p, tid))
    assert not [n for n in p.db.kv(coord.NOTES_KEY, []) if "plan #" in n]


def test_a_gate_recorded_after_tasks_were_queued_holds_them_too(env):
    """A task queued from the plan in the same batch, before the hold was recorded (task_update `gate` on
    the plan), is held on it as task_add would have; one already started is named, never touched."""
    from ttp import coordinator as coord
    from ttp import plangate
    p = make(env)
    dep = p.db.add_task("persistent coordinator", "s", origin="user")
    plan, _ = _finish_plan(env, p, {"followups": []})
    assert coord.apply(p, [
        {"type": "task_add", "title": "bench", "spec": f"from plan #{plan}: run it"},
        {"type": "task_add", "title": "routing", "spec": f"from plan #{plan}", "start_when": "test -f a"},
        {"type": "task_add", "title": "started", "spec": f"from plan #{plan}"},
        {"type": "task_update", "id": plan, "gate": {"depends_on": [dep], "why": "user msg #12: only after it"}},
        {"type": "task_add", "title": "later", "spec": f"from plan #{plan}"}]) == []
    bench, routing, later = (_added(p, t) for t in ("bench", "routing", "later"))
    for t in (bench, routing, later):
        assert dep in json.loads(p.db.task(t)["depends_on"]) and plangate.held(p.db.task(t)), t
    assert "start_when:test -f a" in _labels(p, routing)
    # Applying it again changes nothing; a started task is only named.
    started = _added(p, "started")
    p.db.update_task(started, status="running", attempts=1, depends_on=[], labels=[])
    notes = plangate.hold_queued(p, p.db, plan, plangate.stored(p.db.task(plan)))
    assert notes == [f"#{started} cites plan #{plan} but has already started, so its gate cannot hold it: "
                     f"steer or stop it if it must wait (depends_on #{dep} (user msg #12: only after it))"], notes
    assert json.loads(p.db.task(bench)["depends_on"]) == [dep]
    # The gate is a plan's only; an empty one clears it.
    probs = coord.apply(p, [{"type": "task_update", "id": dep, "gate": {"depends_on": [plan]}}])
    assert any("plan task only" in x for x in probs), probs
    assert coord.apply(p, [{"type": "task_update", "id": plan, "gate": {}}]) == []
    assert plangate.stored(p.db.task(plan)) is None


def test_a_done_plan_holds_tasks_already_queued_from_it(env):
    from ttp import plangate
    p = make(env)
    dep = p.db.add_task("persistent coordinator", "s", origin="user")
    early = p.db.add_task("early", "from plan #PLAN", origin="coordinator")
    tid, ev = _finish_plan(env, p, {})
    p.db.update_task(early, spec=f"from plan #{tid}")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}-b"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "planned",
                                                     "gate": {"depends_on": [dep]}}))
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    assert json.loads(p.db.task(early)["depends_on"]) == [dep] and plangate.held(p.db.task(early))
    ev = _plan_events(p, tid)
    assert f"#{early} cites plan #{tid}: now held on its gate" in ev["text"], ev["text"]


@pytest.mark.parametrize("how", ["plan_gate", "user_cited_why"])
def test_a_hold_the_user_ordered_raises_no_lane_wait(env, how):
    from ttp import lanewait, plangate
    p = make(env)
    holder = p.db.add_task("the holder", "s", kind="code", status="running")
    plan, _ = _finish_plan(env, p, {"gate": {"depends_on": [holder], "why": "user msg #1725: only after it"}})
    t0 = time.time() - 3 * 3600
    labels = ({"plan_gate": [f"{plangate.HELD}{plan}"],
               "user_cited_why": [f"start_when:landed:#{holder}", "start_why:user msg #1725: only after it",
                                  f"deferred_since:{t0:.0f}"]})[how]
    dep = p.db.add_task("the waiter", "s", kind="code", status="queued", labels=labels,
                        depends_on=[holder] if how == "plan_gate" else None)
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (t0, dep))
    lanewait.check(p.db, {"waiting": {"lane_wait_event_s": 600}}, time.time())
    assert not p.db.q("SELECT id FROM events WHERE kind='lane_wait'")


@pytest.mark.parametrize("gate_why", ["user msg #1725: only after it", "once it is in place"])
def test_a_held_task_still_raises_lane_wait_on_its_other_holders(env, gate_why):
    """Only the holders a user-cited gate names are skipped: the task's own dependency still counts, and
    a gate citing no user message skips nothing."""
    from ttp import lanewait, plangate
    p = make(env)
    gate_dep = p.db.add_task("the gate's work", "s", kind="code", status="running")
    sibling = p.db.add_task("a sibling", "s", kind="code", status="running")
    plan, _ = _finish_plan(env, p, {"gate": {"depends_on": [gate_dep], "why": gate_why}})
    t0 = time.time() - 3 * 3600
    t = p.db.add_task("the waiter", "s", kind="code", status="queued", labels=[f"{plangate.HELD}{plan}"],
                      depends_on=[gate_dep, sibling])
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (t0, t))
    lanewait.check(p.db, {"waiting": {"lane_wait_event_s": 600}}, time.time())
    ev = p.db.q("SELECT data FROM events WHERE kind='lane_wait'")
    want = [sibling] if gate_why.startswith("user msg") else [gate_dep, sibling]
    assert len(ev) == 1 and sorted(json.loads(ev[0]["data"])["holders"]) == want, ev
    # The gate met, its own holder still waiting: one event, not one per check.
    p.db.update_task(gate_dep, status="done")
    lanewait.check(p.db, {"waiting": {"lane_wait_event_s": 600}}, time.time())
    assert len(p.db.q("SELECT id FROM events WHERE kind='lane_wait'")) == 1


def test_a_gate_probe_naming_a_queued_task_does_not_hold_it_on_itself(env):
    from ttp import plangate
    p = make(env)
    plan, _ = _finish_plan(env, p, {})
    t = p.db.add_task("the gate's work", f"from plan #{plan}", origin="coordinator", labels=["start_when:test -f a"])
    other = p.db.add_task("another", f"from plan #{plan}", origin="coordinator")
    notes = plangate.set_gate(p, p.db, p.db.task(plan), {"start_when": f"landed:#{t}", "why": "user msg #3: after it"})
    assert any(n.startswith(f"#{t}: plan #{plan}'s gate start_when") and "itself" in n for n in notes), notes
    assert "start_when:test -f a" in _labels(p, t) and not any(f"#{t}" in lb for lb in _labels(p, t)), _labels(p, t)
    assert f"start_when:landed:#{t}" in _labels(p, other), _labels(p, other)

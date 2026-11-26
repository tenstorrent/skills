# Expected finish from the critical path (runtime/ttp/eta.py: `ttp eta`, the STATE and status fields, the
# long-promise Course check line) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there).
from __future__ import annotations


def _eta_history(p, now, kind="work", runs=(600, 1200, 1800), wait=300):
    """Done tasks of `kind` that each waited `wait` s for their first run and ran one of `runs` seconds."""
    for d in runs:
        tid = p.db.add_task(f"past {kind} {d}", "s", kind=kind, status="done")
        c = now - 86400
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (c, tid))
        p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
               (tid, c + wait, c + wait + d))


def test_eta_follows_the_dependency_chain_with_measured_medians(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    a = p.db.add_task("first step", "s", kind="work", status="running")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 400, a))
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (a, now - 300))
    b = p.db.add_task("second step", "s", kind="work", depends_on=[a])
    c = p.db.add_task("third step", "s", kind="work", depends_on=[b, a])
    ea = eta.estimate(p.db, a, now)
    assert ea["why"] is None and ea["end"] == pytest.approx(now + 900)   # median of the longer runs, less 300 s run
    eb = eta.estimate(p.db, b, now)
    assert eb["end"] == pytest.approx(now + 900 + 300 + 1200) and eb["path"] == [b, a]
    ec = eta.estimate(p.db, c, now)
    assert ec["path"] == [c, b, a] and ec["end"] == pytest.approx(eb["end"] + 300 + 1200)
    assert "critical path #%d→#%d→#%d" % (c, b, a) in eta.field(ec, p, now)
    # Medians, never worst cases: one long outlier does not move the estimate past the median.
    _eta_history(p, now, runs=(36000,))
    assert eta.estimate(p.db, b, now)["end"] < now + 2 * 3600 + 900
    # A blocked link gives no estimate rather than a guess.
    p.db.update_task(a, status="blocked")
    e = eta.estimate(p.db, c, now)
    assert e["end"] is None and "blocked" in e["why"] and "no estimate yet" in eta.field(e, p, now)


def test_eta_code_task_adds_measured_review_and_landing_and_overrun(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now, kind="code", runs=(1000,), wait=100)
    rv = p.db.add_task("Review #1", "s", kind="review", status="done")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 5000, rv))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')", (rv, now - 4900, now - 3200))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?)",
           (rv, "b", "h", "t", "pushed", now - 3000, now - 2400, "abc1234"))
    t = p.db.add_task("the fix", "s", kind="code")
    e = eta.estimate(p.db, t, now)
    assert [k for k, _ in e["legs"]] == ["queue", "run", "review", "landing"]
    assert e["end"] == pytest.approx(now + 100 + 1000 + 1800 + 600)
    # A run past every measured one is due now and says so.
    p.db.update_task(t, status="running")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (t, now - 5000))
    e = eta.estimate(p.db, t, now)
    assert e["overrun"] and e["done"] == pytest.approx(now) and "overrunning" in eta.field(e, p, now)


def test_eta_done_code_task_awaiting_its_review_is_not_landed(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now, kind="code", runs=(1000,), wait=100)
    _eta_history(p, now, kind="review", runs=(1700,), wait=100)
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (1, "b", "h", "t", "pushed", now - 3000, now - 2400))   # median landing 600
    t = p.db.add_task("the fix", "s", kind="code", status="done", reply_chat="c1")
    # Handed off, no push row yet: its queued review (which depends on it) still runs, then it lands.
    rv = p.db.add_task(f"Review #{t}: the fix", "s", kind="review", depends_on=[t], labels=[f"auto_review:{t}"])
    e = eta.estimate(p.db, t, now)
    assert e["why"] is None and e["path"] == [t, rv]
    assert [k for k, _ in e["legs"]] == ["queue", "run", "landing"]
    assert e["done"] == pytest.approx(now) and e["end"] == pytest.approx(now + 100 + 1700 + 600) and e["end"] > now
    o = _eta_promise(p, t, "c1", "I'll post here when it's live.", now)
    assert eta.obligation_estimate(p.db, o, now)["end"] > now
    # A review task of another task, or a closed review, leaves it landed (delivered another way).
    p.db.update_task(rv, status="done")
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now)
    other = p.db.add_task("other fix", "s", kind="code", status="done")
    p.db.add_task(f"Review #{other}: other", "s", kind="review", depends_on=[other])
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now)
    # An approved row means the review is over: only the landing left.
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (other, "b2", "h", "t", "queued", now, now))
    assert [k for k, _ in eta.estimate(p.db, other, now)["legs"]] == ["landing"]


def test_eta_a_finished_task_ends_the_walk_and_an_open_cycle_names_its_members(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now, kind="review", runs=(1700,), wait=100)
    # Two done code tasks that depend on each other (a fix and its continue task) and the open review of one.
    a = p.db.add_task("the fix", "s", kind="code", status="done")
    b = p.db.add_task("continue: the fix", "s", kind="code", status="done", depends_on=[a])
    p.db.x("UPDATE tasks SET depends_on=? WHERE id=?", (json.dumps([b]), a))
    rv = p.db.add_task(f"Review #{a}: the fix", "s", kind="review", depends_on=[a], labels=[f"auto_review:{a}"],
                       reply_chat="c1")
    e = eta.estimate(p.db, rv, now)
    assert e["why"] is None and e["end"] == pytest.approx(now + 100 + 1700) and e["path"] == [rv]
    o = _eta_promise(p, rv, "c1", "I'll post here once it's reviewed.", now)
    assert eta.obligation_estimate(p.db, o, now)["end"] == pytest.approx(now + 100 + 1700)
    assert eta.estimate(p.db, a, now)["why"] is None and eta.estimate(p.db, b, now)["end"] == pytest.approx(now)
    # A cycle among open tasks gives no estimate and names the tasks in it.
    x = p.db.add_task("step x", "s", kind="review")
    y = p.db.add_task("step y", "s", kind="review", depends_on=[x])
    z = p.db.add_task("step z", "s", kind="review", depends_on=[y])
    p.db.x("UPDATE tasks SET depends_on=? WHERE id=?", (json.dumps([y]), x))
    e = eta.estimate(p.db, z, now)
    assert e["end"] is None and f"dependency cycle #{y}→#{x}→#{y}" in e["why"]
    assert f"#{x}" in eta.field(eta.estimate(p.db, x, now), p, now) and "cycle" in eta.estimate(p.db, x, now)["why"]


def test_eta_no_history_says_no_estimate_yet(env):
    from ttp import eta
    p = make(env)
    t = p.db.add_task("brand new work", "s", kind="work", priority=1)
    e = eta.estimate(p.db, t)
    assert e["end"] is None and e["why"] == "no measured work run yet"
    assert eta.field(e, p).startswith("ETA: no estimate yet")
    assert eta.wake_at(e) is None
    lines = eta.state_lines(p.db, p)
    assert lines[0].startswith("## Expected finish") and f"#{t} brand new work: ETA: no estimate yet" in lines[1]


def test_eta_command_gives_a_worker_its_wake_time(env, capsys):
    from ttp import cli, eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    up = p.db.add_task("the step it waits on", "s", kind="work")
    cli.main(["eta", f"#{up}", p.name, "--json"])
    got = json.loads(capsys.readouterr().out)
    # A worker waiting on #up sets its retry or deadline wake at that step's expected finish, not hours out.
    assert got["wake_at"] == pytest.approx(eta.estimate(p.db, up, now)["end"], abs=5)
    assert got["wake_at"] - now < 3600 and got["path"] == [up]
    cli.main(["eta", str(up), p.name])
    assert "critical path" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["eta", "nope", p.name])
    worker = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    coord_md = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "`ttp eta <task>`" in worker and "never a padded worst case" in worker
    assert "Expected finish" in coord_md and "`justify`" in coord_md


def _eta_promise(p, tid, chat, text, now):
    from ttp import obligations as ob
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)", (now, "out", "reply", text, 1))
    ob.record_promises(p, [(mid, chat, text)], {tid: chat}, now=now)
    return next(o for o in ob.open_promises(p.db) if mid == o["promise"]["message"])


def test_eta_long_promise_on_urgent_work_is_flagged_and_clears(env):
    from ttp import coordinator as coord, eta, obligations as ob
    p = make(env)
    now = time.time()
    t = p.db.add_task("urgent fix", "s", kind="work", priority=1, reply_chat="c1")
    o = _eta_promise(p, t, "c1", "I'll post the result here.", now)   # no time named: the default, 6 h out
    assert o["promise"]["due"] - now > 2 * 3600
    lines, ident = eta.course_lines(p.db, p.config(), now)
    assert len(lines) == 1 and f"long promise on urgent work: {o['id']} promise" in lines[0]
    assert "ETA: no estimate yet" in lines[0] and "`justify`" in lines[0]
    assert "long promise on urgent work" in coord.digest(p, {}, [], [])
    assert "## Expected finish" in coord.digest(p, {}, [], [])
    # Justified on record: cleared for that due; a moved due needs a new justification.
    assert coord.apply(p, [{"type": "obligation", "key": o["id"], "justify": "critical path #%d run 5 h" % t}]) == []
    assert eta.course_lines(p.db, p.config(), now)[0] == []
    st = p.db.kv(ob.KV)
    st["items"][o["id"]]["promise"]["due"] += 600
    p.db.set_kv(ob.KV, st)
    assert len(eta.course_lines(p.db, p.config(), now)[0]) == 1
    # Shortened: a later promise to the same chat with an earlier due clears it.
    o2 = _eta_promise(p, t, "c1", "I'll post the result here in 30 minutes.", now + 60)
    assert o2["promise"]["due"] < o["promise"]["due"]
    assert eta.course_lines(p.db, p.config(), now + 60)[0] == []
    # Met: a receipt closes it.
    ob.record(p, o2["id"], "receipt", "posted", None)
    assert eta.course_lines(p.db, p.config(), now + 60)[0] != []
    ob.record(p, o["id"], "receipt", "posted", None)
    assert eta.course_lines(p.db, p.config(), now + 60)[0] == []


def test_eta_a_claim_on_urgent_work_is_no_long_promise_or_expected_finish_line(env):
    """A claim backed by an urgent task, due by default hours out, is no promise: no course line, no
    Expected finish line."""
    from ttp import eta, obligations as ob
    p = make(env)
    now = time.time()
    hot = p.db.add_task("hot work", "urgent: the user is waiting\nfix it", kind="work", reply_chat="c3")
    out = p.db.post("out", "the fix is running", chat="c3", kind="reply")
    [line] = ob.record_claims(p, [(out, "c3", [{"text": "the fix lands", "task": hot}])], {}, now=now)
    assert ob.get(p.db, line.split()[1])["promise"]["due"] - now > 2 * 3600
    assert eta.course_lines(p.db, p.config(), now)[0] == []
    assert not any("promise" in x for x in eta.state_lines(p.db, p, now))


def test_eta_long_promise_needs_urgent_work_and_a_long_due(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    calm = p.db.add_task("routine work", "s", kind="work", priority=3, reply_chat="c2")
    _eta_promise(p, calm, "c2", "I'll post the result here.", now)
    assert eta.course_lines(p.db, p.config(), now)[0] == []            # not urgent: no line
    hot = p.db.add_task("hot work", "urgent: the user is waiting\nfix it", kind="work", reply_chat="c3")
    _eta_promise(p, hot, "c3", "I'll post the result here in 90 minutes.", now)
    assert eta.course_lines(p.db, p.config(), now)[0] == []            # due within 2 h
    _eta_promise(p, hot, "c3", "I'll post the full report here.", now)
    assert len(eta.course_lines(p.db, p.config(), now)[0]) == 1        # the spec's urgent: line, 6 h out
    cfg = {**p.config(), "coordinator": {"course_check": {"long_promise_h": 8}}}
    assert eta.course_lines(p.db, cfg, now)[0] == []                   # a configurable threshold
    assert eta.course_lines(p.db, {"coordinator": {"course_check": {"long_promise_h": 0}}}, now)[0] == []


def _eta_gate(p, prov="claude", **g):
    p.db.set_kv("gates", {prov: {"provider": prov, "level": "red", "allow_new_work": False, "max_parallel": 0,
                                 "reasons": [], "numbers": {}, "lane": {}, **g}})


def test_eta_red_spend_gate_starts_held_work_at_its_known_reset(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    t = p.db.add_task("held urgent work", "s", kind="work", priority=1, reply_chat="c1")
    green = eta.estimate(p.db, t, now)
    assert green["end"] == pytest.approx(now + 300 + 1200)
    # Green (or yellow) gate: unchanged.
    _eta_gate(p, level="green", allow_new_work=True, max_parallel=6)
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(green["end"])
    # The account-wide daily cap reached with a known reset two hours out: no earlier than reset + median.
    reset = now + 7200
    _eta_gate(p, reasons=["global daily cap reached: $10 of $10 today across the account; new work resumes "
                          "at the reset in 2.0 h"], numbers={"global_resets_at": reset})
    e = eta.estimate(p.db, t, now)
    assert e["why"] is None and e["end"] >= reset + 1200 and e["end"] == pytest.approx(reset + 300 + 1200)
    assert e["start"] == pytest.approx(reset + 300)
    # A dependant waits for it, and the promise on it too.
    d = p.db.add_task("after it", "s", kind="work", depends_on=[t])
    assert eta.estimate(p.db, d, now)["end"] == pytest.approx(e["done"] + 300 + 1200)
    o = _eta_promise(p, t, "c1", "I'll post here when it's live.", now)
    assert eta.obligation_estimate(p.db, o, now)["end"] == pytest.approx(e["end"])
    # A project's daily cap on a budget day; a plan window at its line: their resets.
    _eta_gate(p, reasons=["cap reached: $5.00/today of $5"],
              numbers={"day_end": now + 3600, "daily_cap": 5, "weekly_cap": 0})
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 3600 + 1500)
    _eta_gate(p, regime="windows", reasons=["5h window at 97% (the project stops at 95%)"],
              numbers={"plan": [{"window": "5h", "resets_at": now + 1800}]})
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 1800 + 1500)
    # A later not_before stays the start; a running task is not held.
    p.db.x("UPDATE tasks SET not_before=? WHERE id=?", (now + 5000, t))
    e = eta.estimate(p.db, t, now)
    assert e["end"] == pytest.approx(now + 5000 + 1500)
    # Its queue leg waits it out past the plan window's reset (cell eta_not_before), never a 0m queue.
    assert [x for k, x in e["legs"] if k.startswith("queue")][0] >= 5000 - 1800, e["legs"]
    assert "deferred until" in eta.text(e, "UTC", now), eta.text(e, "UTC", now)
    r = p.db.add_task("already running", "s", kind="work", status="running")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (r, now - 100))
    assert eta.estimate(p.db, r, now)["end"] < now + 1800
    # Another provider's red gate does not hold a task on this one.
    _eta_gate(p, prov="other", reasons=["global daily cap reached: x"], numbers={"global_resets_at": reset})
    p.db.x("UPDATE tasks SET not_before=NULL WHERE id=?", (t,))
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(green["end"])
    # A task routed to that provider is held there.
    p.db.x("UPDATE tasks SET provider='other' WHERE id=?", (t,))
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(reset + 1500)
    # The core provider named by the project: a task with none runs there.
    p.db.x("UPDATE tasks SET provider=NULL WHERE id=?", (t,))
    assert eta.estimate(p.db, t, now, core="other")["end"] == pytest.approx(reset + 1500)


def test_eta_task_with_no_provider_is_held_by_the_core_providers_gate(env):
    from ttp import eta, onhost, web
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    p.set_config("core_provider", "other")
    reset = now + 7200
    _eta_gate(p, prov="other", reasons=["global daily cap reached: x"], numbers={"global_resets_at": reset})
    t = p.db.add_task("held urgent work", "s", kind="work", priority=1, reply_chat="c1",
                      labels=["resource:box1-device"])
    held = eta.estimate(p.db, t, now, core="other")
    assert held["end"] == pytest.approx(reset + 1500) and eta.estimate(p.db, t, now)["end"] < reset
    # The web task list, the queued-host-task line with no shared estimator, and a promise on it.
    (row,) = [r for r in web.state_payload(p, p.db)["tasks"] if r["id"] == t]
    assert row["eta"] == eta.field(eta._Est(p.db, time.time(), core="other").task(t), p, time.time())
    assert row["eta"].startswith("ETA ") and row["eta"] != eta.field(eta.estimate(p.db, t, time.time()), p,
                                                                       time.time())
    assert onhost.state(p, p.db.task(t), now)["line"] == onhost.queued_line(held, p, now)
    o = _eta_promise(p, t, "c1", "I'll post here when it's live.", now)
    assert eta.obligation_estimate(p.db, o, now, core="other")["end"] == pytest.approx(reset + 1500)
    # A task routed to claude is not held by the core provider's gate.
    p.db.x("UPDATE tasks SET provider='claude' WHERE id=?", (t,))
    assert eta.estimate(p.db, t, now, core="other")["end"] < reset

def test_eta_red_spend_gate_with_no_known_reset_gives_no_clock_time(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    t = p.db.add_task("held work", "s", kind="work", priority=1, reply_chat="c1")
    # A rolling 24 h global cap has no fixed reset.
    _eta_gate(p, reasons=["global daily cap reached: $10 of $10 today across the account"],
              numbers={"global_resets_at": None})
    e = eta.estimate(p.db, t, now)
    f = eta.field(e, p, now)
    assert e["end"] is None and eta.wake_at(e) is None
    assert f.startswith("ETA: no estimate yet (") and "held by the spend cap" in f and ":" not in f[len("ETA:"):]
    o = _eta_promise(p, t, "c1", "I'll post here when it's live.", now)
    assert "held by the spend cap" in eta.field(eta.obligation_estimate(p.db, o, now), p, now)
    # A daily cap on a budget day while the rolling 7 d cap is reached too: no fixed end either.
    _eta_gate(p, reasons=["cap reached: $5.00/today of $5, $40.00/7d of $40"],
              numbers={"day_end": now + 3600, "daily_cap": 5, "weekly_cap": 40, "spent_7d": 40})
    assert "held by the spend cap" in eta.estimate(p.db, t, now)["why"]
    # A malformed stored gate holds what it says it holds and never breaks the estimate.
    p.db.set_kv("gates", {"claude": {"allow_new_work": False, "reasons": None, "numbers": None}})
    assert "held by the spend cap" in eta.estimate(p.db, t, now)["why"]
    p.db.set_kv("gates", {"claude": {"allow_new_work": False, "reasons": ["cap reached: x"], "numbers": [1]}})
    assert "held by the spend cap" in eta.estimate(p.db, t, now)["why"]
    p.db.set_kv("gates", ["not", "a", "map"])
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 1500)
    # One known and one unknown reset: unknown.
    _eta_gate(p, reasons=["runaway guard: x", "global daily cap reached: y"], numbers={})
    assert eta.estimate(p.db, t, now)["end"] is None


def test_eta_coordinator_guard_red_holds_routine_work_only_and_a_held_review(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    _eta_gate(p, reasons=["coordinator turn guard: x"], lane={"max_parallel": 2, "max_tier": "standard"})
    hot = p.db.add_task("urgent", "s", kind="work", priority=1)
    asked = p.db.add_task("user asked", "s", kind="work", reply_chat="c1")
    routine = p.db.add_task("routine", "s", kind="work", priority=5)
    assert eta.estimate(p.db, hot, now)["end"] == pytest.approx(now + 1500)
    assert eta.estimate(p.db, asked, now)["end"] == pytest.approx(now + 1500)
    assert "held by the spend cap" in eta.estimate(p.db, routine, now)["why"]
    # A code task in review whose queued review task the gate holds: the review leg starts at the reset.
    _eta_history(p, now, kind="code", runs=(1000,), wait=100)
    rvd = p.db.add_task("Review #x", "s", kind="review", status="done")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 5000, rvd))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
           (rvd, now - 4900, now - 3200))
    c = p.db.add_task("the fix", "s", kind="code", status="review")
    p.db.add_task(f"Review #{c}: the fix", "s", kind="review", depends_on=[c], labels=[f"auto_review:{c}"])
    assert "held by the spend cap" in eta.estimate(p.db, c, now)["why"], "a routine review under the guard"
    p.db.set_kv("gates", {})
    before = eta.estimate(p.db, c, now)["end"]
    _eta_gate(p, reasons=["global daily cap reached: y"], numbers={"global_resets_at": now + 7200})
    e = eta.estimate(p.db, c, now)
    assert e["why"] is None and e["end"] >= now + 7200 + 1800 and e["end"] > before


def test_eta_a_task_waiting_on_another_tasks_landing_starts_after_that_landing(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now, kind="code", runs=(1000,), wait=100)
    _eta_history(p, now, kind="work", runs=(500,), wait=100)
    rv = p.db.add_task("Review #1", "s", kind="review", status="done")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 5000, rv))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')", (rv, now - 4900, now - 3300))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?)",
           (rv, "b", "h", "t", "pushed", now - 3000, now - 2400, "abc1234"))   # medians: wait 100, review 1700, landing 600
    n = p.db.add_task("the fix", "s", kind="code")
    w = p.db.add_task("deploy it", "s", kind="work", reply_chat="c1", labels=[f"start_when:landed:#{n}"])
    w2 = p.db.add_task("check it", "s", kind="work", labels=[f"start_when:ttp landed --task {n} && ttp landed --task #{rv} || exit 1"])
    other = p.db.add_task("other probe", "s", kind="work", labels=[f"start_when:ttp landed --task {n} && test -e /x"])
    own = now + 100 + 500   # a work task's own queue and run legs
    o = _eta_promise(p, w, "c1", "I'll post here when it's deployed.", now)

    def check(n_end, state):
        assert eta.estimate(p.db, n, now)["end"] == pytest.approx(n_end), state
        for t in (w, w2):
            e = eta.estimate(p.db, t, now)
            assert e["why"] is None and e["end"] == pytest.approx(max(n_end, now) + 600 if n_end > now else own), state
            assert e["path"] == ([t, n] if n_end > now else [t]), state
        assert eta.obligation_estimate(p.db, o, now)["end"] == pytest.approx(eta.estimate(p.db, w, now)["end"]), state
        assert eta.estimate(p.db, other, now)["gated"], state   # a mixed probe is a start gate: no clock time
    check(now + 100 + 1000 + 1700 + 600, "queued")   # before the fix: deploy it showed now + 600
    p.db.update_task(n, status="running")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (n, now - 400))
    check(now + 600 + 1700 + 600, "running")
    p.db.update_task(n, status="review")
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (now - 200, n))
    check(now + 1500 + 600, "review")
    p.db.update_task(n, status="pushing")
    q = p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
               (n, "b2", "h2", "t", "approved", now - 100, now - 100))
    check(now + 500, "pushing")
    p.db.update_task(n, status="done")
    p.db.x("UPDATE push_queue SET status='landed', landed_sha='abc1234' WHERE id=?", (q,))
    check(now, "landed")
    # A task waiting on its own landing names the cycle rather than guessing.
    p.db.update_task(n, status="queued", labels=[f"start_when:landed:#{w}"])
    p.db.x("DELETE FROM push_queue WHERE id=?", (q,))
    assert "dependency cycle" in eta.estimate(p.db, w, now)["why"]


def _eta_waiting(p, now, kind="work", retry_after=120, **res):
    """A task the daemon holds on a waiting hand-off (status queued, its next probe `retry_after` s out)."""
    tid = p.db.add_task(f"waiting {kind}", "s", kind=kind)
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "summary": "s", **res}), not_before=now + retry_after)
    return tid


def test_eta_outside_work_wait_has_no_measured_finish_without_a_measured_eta(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    probes = ["ttp detach --check --host box /runs/7/job", "ssh box \"squeue -h -j 12 | grep -q . && exit 1 || exit 0\"",
              "ttp ack-wait probe --from peer --hash abc", "ttp checks --result /runs/7/checks"]
    for probe in probes:
        t = _eta_waiting(p, now, retry_when=probe)
        e = eta.estimate(p.db, t, now)
        # Never 'next probe + a median run': no time at all, and it says why.
        assert e["end"] is None and "no measured finish yet" in e["why"], probe
        assert "no measured finish yet" in eta.field(e, p, now) and eta.wake_at(e) is None
    # The status 'waiting' some callers pass reads the hand-off the same way.
    w = p.db.add_task("w", "s", kind="work", status="waiting")
    p.db.update_task(w, result=json.dumps({"status": "waiting", "retry_when": probes[0]}))
    assert eta.estimate(p.db, w, now)["end"] is None
    # Its dependants, its obligations and the long-promise course check see no time either.
    after = p.db.add_task("next step", "s", kind="work", depends_on=[t])
    e = eta.estimate(p.db, after, now)
    assert e["end"] is None and e["path"] == [after, t] and "no measured finish yet" in e["why"]
    u = p.db.add_task("urgent fix", "s", kind="work", priority=1, reply_chat="c1",
                      depends_on=[t])
    o = _eta_promise(p, u, "c1", "I'll post the result here.", now)
    assert eta.obligation_estimate(p.db, o, now)["end"] is None
    lines, _ = eta.course_lines(p.db, p.config(), now)
    assert lines and "no measured finish yet" in lines[0]


def test_eta_outside_work_wait_uses_a_measured_eta_with_its_basis(env):
    from datetime import datetime, timezone
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200
    probe = "ttp detach --check --host box /runs/7/job"
    basis = "the job's own log: step 40 of 100 after 2 h"
    # Epoch or ISO with offset; a median run follows when a worker run reads the outcome.
    t = _eta_waiting(p, now, retry_when=probe, eta=now + 7200, eta_basis=basis)
    e = eta.estimate(p.db, t, now)
    assert e["end"] == pytest.approx(now + 7200 + 1200) and [k for k, _ in e["legs"]] == ["outside work", "run"]
    iso = datetime.fromtimestamp(now + 7200, timezone.utc).astimezone().isoformat()
    t2 = _eta_waiting(p, now, retry_when=probe, eta=iso, eta_basis=basis)
    assert eta.estimate(p.db, t2, now)["end"] == pytest.approx(now + 7200 + 1200, abs=1)
    # An on_pass_cmd records the end model-free: no run after the eta.
    t3 = _eta_waiting(p, now, retry_when=probe, eta=str(now + 7200), eta_basis=basis, on_pass_cmd="ttp checks --result x --passed")
    assert eta.estimate(p.db, t3, now)["end"] == pytest.approx(now + 7200)
    # Ignored: no basis, an eta already past, a naive ISO time (no offset), a non-time.
    for bad in ({"eta": now + 7200}, {"eta": now + 7200, "eta_basis": "  "}, {"eta": now - 60, "eta_basis": basis},
                {"eta": datetime.fromtimestamp(now + 7200).isoformat(), "eta_basis": basis},
                {"eta": "soon", "eta_basis": basis}, {"eta": True, "eta_basis": basis},
                {"eta": "inf", "eta_basis": basis}, {"eta": "nan", "eta_basis": basis}):
        b = _eta_waiting(p, now, retry_when=probe, **bad)
        e = eta.estimate(p.db, b, now)
        assert e["end"] is None and "no measured finish yet" in e["why"], bad


def _eta_cell_history(p, now, per_cell=100, n=6, kind="work"):
    """A done `kind` task that ran `n` cells in `n * per_cell` s of worker time."""
    names = ", ".join(f"c{i}" for i in range(n))
    tid = p.db.add_task(f"past bench {kind}", f"run it\ncells: {names}", kind=kind, status="done")
    c = now - 86400
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (c, tid))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')",
           (tid, c + 60, c + 60 + n * per_cell))


def test_eta_a_long_job_runs_its_cells_at_the_measured_per_cell_time_never_the_kind_median(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # a work run median of 1200 s: wrong for a bench
    spec = "run the bench\ncells: a, b, c, d, e, f"
    t = p.db.add_task("bench six models", spec, kind="work")
    # No per-cell time measured yet: no estimate, not the 1200 s code-style median.
    e = eta.estimate(p.db, t, now)
    assert e["end"] is None and "no estimate yet" in e["why"] and "per-cell" in e["why"]
    assert "no estimate yet" in eta.field(e, p, now)
    _eta_cell_history(p, now, per_cell=600, n=4)           # 600 s per cell
    h = eta.history(p.db, now)
    assert h["cell"] == {"work": [600.0]}
    e = eta.estimate(p.db, t, now, hist=h)
    assert e["why"] is None and [k for k, _ in e["legs"]] == ["queue", "run"]
    assert e["legs"][1][1] == pytest.approx(6 * 600)
    # Running: what is left of the cells' time, overrunning once past it.
    r = p.db.add_task("running bench", spec, kind="work", status="running")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'running')", (r, now - 1000))
    e = eta.estimate(p.db, r, now, hist=h)
    assert e["end"] == pytest.approx(now + 3600 - 1000) and not e["overrun"]
    p.db.x("UPDATE runs SET started=? WHERE task=?", (now - 5000, r))
    e = eta.estimate(p.db, r, now, hist=h)
    assert e["overrun"] and e["end"] == pytest.approx(now)
    # A `long_job:` line without cells has no per-cell basis; a descriptive cells line and a code task's
    # cells keep the usual estimate.
    lj = p.db.add_task("soak", "long_job: soak test", kind="work")
    assert "per-cell" in eta.estimate(p.db, lj, now, hist=h)["why"]
    plain = p.db.add_task("plain", "do it. cells: whatever this means in prose", kind="work")
    assert eta.estimate(p.db, plain, now, hist=h)["legs"][-1] == ("run", pytest.approx(1500))   # the kind median, the bench run included
    code = p.db.add_task("code with test cells", "cells: t1, t2", kind="code")
    assert "per-cell" not in str(eta.estimate(p.db, code, now, hist=h)["why"])   # only no code run measured


def test_eta_a_long_job_with_a_detached_runs_eta_in_its_handoff_uses_it(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    _eta_cell_history(p, now, per_cell=600, n=4)
    probe = "ttp detach --check --host box /runs/7/job"
    t = _eta_waiting(p, now, retry_when=probe, eta=now + 5400, eta_basis="the job's log: 30 of 90 cells after 30 min")
    p.db.update_task(t, spec="bench\ncells: a, b, c, d, e, f")
    e = eta.estimate(p.db, t, now)
    assert e["why"] is None and [k for k, _ in e["legs"]][0] == "outside work"
    assert e["legs"][0][1] == pytest.approx(5400)
    # Without an eta in the hand-off it has no finish at all, not the per-cell or code-task median.
    t2 = _eta_waiting(p, now, retry_when=probe)
    p.db.update_task(t2, spec="bench\ncells: a, b, c, d, e, f")
    e = eta.estimate(p.db, t2, now)
    assert e["end"] is None and "no measured finish yet" in e["why"]


def test_eta_time_and_landing_waits_keep_their_estimates(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    # A bare clock gate, a landing wait on a landed task and no retry_when at all: the next try plus the
    # usual legs. (A landing wait on a task not landed follows that task: eta_landing_gate tests.)
    gone = p.db.add_task("landed fix", "s", kind="code", status="done")
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) VALUES(?,?,?,?,?,?,?,?)",
           (gone, "b", "h", "t", "landed", now - 900, now - 600, "abc1234"))
    for res in ({"retry_when": f'test "$(date +%s)" -ge {int(now) + 600}'}, {"retry_when": f"landed:#{gone}"},
                {"retry_when": f" landed: {gone} "}, {"retry_when": f"ttp landed --task {gone} && ttp landed --task #{gone} || exit 1"},
                {}):
        t = _eta_waiting(p, now, retry_after=600, **res)
        assert eta.outside_wait(p.db.task(t), now) is None
        assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 600 + 300 + 1200), res
    # A deferred task's start_when is the deferral's, not a waiting hand-off's.
    d = p.db.add_task("deferred", "s", kind="work", labels=["start_when:ssh box true"])
    p.db.update_task(d, result=json.dumps({"status": "waiting", "retry_when": "ssh box true"}))
    assert eta.outside_wait(p.db.task(d), now) is None


def test_eta_a_start_gated_task_has_no_estimate_and_a_start_after_is_its_earliest_start(env):
    """An unmet start_when gives 'no estimate (gated: ...)' for the task, its dependants and a promise it
    alone owns, never 'queue 0m, run 2m' from now; a start_after or clock start_when is the earliest start,
    the measured run on top; an ungated task is unchanged."""
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    plain = p.db.add_task("ungated", "s", kind="work")
    assert eta.estimate(p.db, plain, now)["end"] == pytest.approx(now + 300 + 1200)
    assert "gated" not in eta.estimate(p.db, plain, now)
    g = p.db.add_task("after the box", "s", kind="work", labels=["start_when:ssh box-a true", "start_why:box-a is back up"])
    e = eta.estimate(p.db, g, now)
    assert e["end"] is None and e["gated"] and e["legs"] == []
    assert eta.field(e, "UTC", now) == f"ETA: no estimate (gated: #{g} starts when box-a is back up)"
    assert eta.text(e, "UTC", now) == eta.field(e, "UTC", now)
    w = p.db.add_task("after the demo", "s", kind="work", labels=["start_when:test -e state/user-demo-over"])
    assert eta.field(eta.estimate(p.db, w, now), "UTC", now) == \
        f"ETA: no estimate (gated: #{w} starts when `test -e state/user-demo-over` passes)"
    d = p.db.add_task("after it", "s", kind="work", depends_on=[g])
    e = eta.estimate(p.db, d, now)
    assert e["gated"] and e["path"] == [d, g] and eta.field(e, "UTC", now).startswith("ETA: no estimate (gated: #")
    o = _eta_promise(p, w, "c1", "I'll post here when the demo is over.", now)
    assert eta.obligation_estimate(p.db, o, now)["gated"]
    u = [p.db.add_task(f"urgent {i}", "urgent: x", kind="work", priority=1,
                       labels=["start_when:test -e state/user-x", "start_why:the user says go"]) for i in range(2)]
    lines = eta.state_lines(p.db, "UTC", now)
    assert f"- #{u[0]}, #{u[1]}: ETA: no estimate (gated: starts when the user says go)" in lines, lines
    assert any(x.startswith(f"- promise {o['id']}: ETA: no estimate (gated: #{w} ") for x in lines), lines
    # A start_after, or a clock start_when, is the earliest start: waited out in the queue leg, the run on top.
    a = p.db.add_task("later", "s", kind="work", labels=[f"start_after:{now + 5000}"])
    p.db.x("UPDATE tasks SET not_before=? WHERE id=?", (now + 5000, a))
    e = eta.estimate(p.db, a, now)
    assert e["deferred"] == now + 5000 and e["end"] == pytest.approx(now + 5000 + 300 + 1200) and "gated" not in e
    c = p.db.add_task("at a time", "s", kind="work", labels=[f'start_when:test "$(date +%s)" -ge {int(now) + 4000}'])
    e = eta.estimate(p.db, c, now)
    assert e["deferred"] == int(now) + 4000 and e["end"] == pytest.approx(int(now) + 4000 + 300 + 1200)


def test_eta_a_woken_outside_wait_is_due_and_keeps_the_queue_and_run_legs(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    # The daemon woke it (its hand-off says why, not_before is past): it waits only for dispatch.
    t = _eta_waiting(p, now, retry_after=-5, retry_when="ttp detach --check --host box /runs/7/job",
                     woke="probe passed", waiting_since=now - 3600)
    assert eta.outside_wait(p.db.task(t), now) is None
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 300 + 1200)


def test_eta_outside_wait_reads_a_measured_eta_from_the_task_s_progress_reports(env, monkeypatch, capsys):
    from ttp import cli, eta, progress
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200
    probe, basis = "ttp detach --check --host box /runs/7/job", "step 40 of 100 after 2 h"
    t = _eta_waiting(p, now, retry_when=probe, eta=now + 7200, eta_basis=basis, waiting_since=now - 600)

    def report(at, eta_=None, basis_=basis, task=t, **kw):
        rec = progress.record("result", "box/job", "step", env={"TTP_TASK": str(task)}, now=at,
                              eta="" if eta_ is None else str(eta_), eta_basis=basis_ if eta_ is not None else "")
        progress.append(p.state, {**rec, **kw})
    # Reports before this wait began, of another task, with no basis, past or with no eta: ignored.
    report(now - 900, now + 600)
    report(now - 60, now + 900, task=t + 1000)
    report(now - 50, now + 900, basis_=" ")
    report(now - 40, now - 10)
    report(now - 30)
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 7200 + 1200)
    # A newer report with a measured eta wins over the hand-off's, and the newest valid one over older ones.
    report(now - 20, now + 3600)
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 3600 + 1200)
    report(now - 10, now + 1800)
    report(now - 5, now - 1)                              # already past: keeps the last valid one
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 1800 + 1200)
    # A report alone gives a finish to a hand-off with none.
    t2 = _eta_waiting(p, now, retry_when=probe, waiting_since=now - 600)
    assert eta.estimate(p.db, t2, now)["end"] is None
    report(now - 1, now + 2400, task=t2)
    assert eta.estimate(p.db, t2, now)["end"] == pytest.approx(now + 2400 + 1200)
    # `ttp progress report --eta --eta-basis` stores them on the line (the task from TTP_TASK).
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_TASK", str(t2))
    cli.main(["progress", "report", "result", "step 50", "--key", "box/job", "--eta", str(now + 3000),
              "--eta-basis", basis])
    got = progress.entries(p.state)[-1]
    assert got["eta"] == str(now + 3000) and got["eta_basis"] == basis and got["task"] == str(t2)
    assert eta.estimate(p.db, t2, now)["end"] == pytest.approx(now + 3000 + 1200, abs=1)
    cli.main(["progress", "report", "result", "--key", "box/job", "--eta", "soon"])
    assert "--eta is ignored" in capsys.readouterr().err
    assert eta.estimate(p.db, t2, now)["end"] == pytest.approx(now + 3000 + 1200, abs=1)


def test_expected_finish_names_tasks_with_no_estimate_for_one_reason_on_one_line(env):
    """A stateless coordinator turn paid for one 'no estimate yet' line per urgent task, each repeating
    its title (already in Open tasks) and the same reason. Tasks sharing a reason share one line naming
    every id; a lone one keeps its own line with its title."""
    from ttp import eta
    p = make(env)
    a = p.db.add_task("first new work", "s", kind="work", priority=1)
    lines = eta.state_lines(p.db, p)
    assert f"#{a} first new work: ETA: no estimate yet (no measured work run yet)" in lines[1]
    b = p.db.add_task("second new work", "s", kind="work", priority=1)
    c = p.db.add_task("third new work", "s", kind="work", priority=1)
    lines = eta.state_lines(p.db, p)
    assert lines[1:] == [f"- #{a}, #{b}, #{c}: ETA: no estimate yet (no measured work run yet)"], lines
    assert eta.state_lines(p.db, p, skip={a, b})[1:] == [f"- #{c} third new work: ETA: no estimate yet (no measured work run yet)"]
    assert eta.state_lines(p.db, p, skip={a, b, c}) == [], "ids shown elsewhere leave no shared line naming them"


def test_eta_of_a_queued_approval_counts_the_approvals_ahead_of_it(env):
    """#6857: the eta of an approval still waiting for a batch is the batches it waits for (the approvals
    ahead of it in the queue's order, aging included, per push_batch_max) times the median batch, plus
    what is left of a running batch; with no measured batch it keeps the median landing."""
    from ttp import eta
    p = make(env)
    now = time.time()
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (999, "b", "h", "t", "pushed", now - 9000, now - 8400))   # median landing 600
    ids = {}
    for name, prio in (("hot", 1), ("mine", 3), ("a", 2), ("b", 2), ("c", 2), ("later", 3)):   # approval order
        tid = ids[name] = p.db.add_task(f"Review {name}", "s", kind="review", priority=prio, status="pushing")
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
               (tid, "b", "h", "t", "approved", now - 60, now - 60))
    e = eta.estimate(p.db, ids["mine"], now, delivery={"push_batch_max": 2})
    assert [k for k, _ in e["legs"]] == ["landing"] and e["end"] == pytest.approx(now + 600), "no batch measured yet"
    for i, d in enumerate((1200, 1800, 2400)):
        p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,after_finalized,outcome) "
               "VALUES(?,?,?,?,?,?,?,?)", (f"b{i}", "m", "t", now - 20000 + i * 5000, None, None,
                                            now - 20000 + i * 5000 + d, "pushed"))
    e = eta.estimate(p.db, ids["mine"], now, delivery={"push_batch_max": 2})
    assert e["legs"] == [("landing (4 approvals ahead)", pytest.approx(3 * 1800))], e["legs"]
    assert eta.estimate(p.db, ids["hot"], now, delivery={"push_batch_max": 2})["end"] == pytest.approx(now + 1800)
    assert eta.estimate(p.db, ids["later"], now)["legs"][0][0] == "landing (5 approvals ahead)", "default cap 8"
    # Waited two periods of push_age_s, it joins the priority-2 lane and leads it (approved before them).
    p.db.x("UPDATE push_queue SET created=? WHERE task=?", (now - 7200, ids["mine"]))
    e = eta.estimate(p.db, ids["mine"], now, delivery={"push_batch_max": 2, "push_age_s": 3600})
    assert e["legs"] == [("landing (1 approval ahead)", pytest.approx(1800))]
    # A running batch adds what is left of its median.
    p.db.x("INSERT INTO push_batches(id,marker,target,started) VALUES('live','m','t',?)", (now - 600,))
    e = eta.estimate(p.db, ids["mine"], now, delivery={"push_batch_max": 2, "push_age_s": 3600})
    assert e["end"] == pytest.approx(now + 1200 + 1800)


def _eta_gated(p, now):
    """Medians (code queue 100 run 1000, work queue 100 run 500, review 1700, landing 600), a done code task
    #n with no review yet and a work task waiting on its landing with a promise on it."""
    _eta_history(p, now, kind="code", runs=(1000,), wait=100)
    _eta_history(p, now, kind="work", runs=(500,), wait=100)
    _eta_history(p, now, kind="review", runs=(1700,), wait=100)
    rv = p.db.add_task("Review #1", "s", kind="review", status="done")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - 5000, rv))
    p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'ok')", (rv, now - 4900, now - 3300))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?)",
           (rv, "b", "h", "t", "pushed", now - 3000, now - 2400, "abc1234"))
    n = p.db.add_task("the fix", "s", kind="code", status="done")
    p.db.update_task(n, branch=f"ttp/t{n}-fix")
    w = p.db.add_task("deploy it", "s", kind="work", reply_chat="c1", labels=[f"start_when:landed:#{n}"])
    p.db.update_task(w, not_before=now + 60)   # the probe's next check: never its finish
    return n, w, _eta_promise(p, w, "c1", "I'll post here when it's deployed.", now)


def test_eta_landing_gate_with_no_live_owner_reads_after_landed_and_no_clock_time(env):
    import re
    from ttp import eta
    p = make(env)
    now = time.time()
    n, w, o = _eta_gated(p, now)
    waiting = _eta_waiting(p, now, retry_after=60, retry_when=f"landed:#{n}")   # a hand-off's landing wait too

    def after(tid, *words):
        e = eta.estimate(p.db, tid, now)
        f = eta.field(e, p, now)
        assert e["end"] is None and e["after"] == f"landed:#{n}" and eta.wake_at(e) is None, f
        assert f.startswith(f"ETA: after landed:#{n} (") and not re.search(r"\d:\d\d", f), f
        assert all(x in e["why"] for x in words), e["why"]
        return e
    # Done, never reviewed, nothing open carries it: before the fix this showed now + 60 + the run.
    for t in (w, waiting):
        after(t, f"#{n} is done but not landed")
    oe = eta.obligation_estimate(p.db, o, now)
    assert oe["after"] == f"landed:#{n}" and eta.field(oe, p, now).startswith("ETA: after landed:#")
    dep = p.db.add_task("then", "s", kind="work", depends_on=[w])   # a dependant inherits the gate
    assert eta.estimate(p.db, dep, now)["after"] == f"landed:#{n}"
    # Its review failed (and its push row failed) with no successor.
    bad = p.db.add_task(f"Review #{n}", "s", kind="review", depends_on=[n], status="failed")
    after(w, f"its last review #{bad} failed")
    p.db.update_task(bad, status="cancelled")
    after(w, f"its last review #{bad} was cancelled")
    p.db.update_task(bad, status="done")
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (bad, f"ttp/t{n}-fix", "h", "t", "check_failed", now - 700, now - 600))
    after(w, "push queue row", "check_failed")
    # Failed, cancelled or abandoned gated tasks.
    for st in ("failed", "cancelled"):
        p.db.update_task(n, status=st)
        after(w, f"#{n} {st}")
    p.db.update_task(n, status="done")
    p.db.x("INSERT INTO task_abandoned(task, why, ts) VALUES(?,?,?)", (n, "dropped", now))
    after(w, "abandoned")


def test_expected_finish_keeps_the_gate_of_urgent_tasks_sharing_one_reason(env):
    """#7511: urgent tasks sharing one reason shared a 'no estimate yet (why)' line, which dropped the
    gate they wait on (after landed:#N). Tasks share a line only per gate and reason, and it names
    the gate."""
    from ttp import eta
    p = make(env)
    now = time.time()
    n, w, _ = _eta_gated(p, now)
    w2 = p.db.add_task("deploy it too", "s", kind="work", labels=[f"start_when:landed:#{n}"])
    for t in (w, w2):
        p.db.update_task(t, priority=1)
    bad = p.db.add_task(f"Review #{n}", "s", kind="review", depends_on=[n], status="failed")
    rows = [x for x in eta.state_lines(p.db, p, now) if x.startswith("- #")]
    assert len(rows) == 1 and rows[0].startswith(f"- #{w}, #{w2}: ETA: after landed:#{n} ("), rows
    assert f"its last review #{bad} failed" in rows[0] and "no estimate yet" not in rows[0], rows


def test_eta_state_web_and_host_estimates_read_the_projects_push_batch_cap(env):
    """#6860 follow-up: every estimate built for the STATE, the Course check, the web app and the
    started-on-host line reads the project's delivery config (push_batch_max, push_age_s), so an
    approval's 'approvals ahead' count follows the project's batch cap, not the default 8."""
    from ttp import eta
    p = make(env)
    now = time.time()
    p.set_config("delivery.push_batch_max", 2)
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (999, "b", "h", "t", "pushed", now - 9000, now - 8400))
    for i in range(3):
        p.db.x("INSERT INTO push_batches(id,marker,target,started,ended,finalized,after_finalized,outcome) "
               "VALUES(?,?,?,?,?,?,?,?)", (f"b{i}", "m", "t", now - 20000 + i * 5000, None, None,
                                            now - 20000 + i * 5000 + 1800, "pushed"))
    ids = [p.db.add_task(f"Review {k}", "s", kind="review", priority=1, status="pushing") for k in range(5)]
    for tid in ids:
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
               (tid, "b", "h", "t", "approved", now - 60, now - 60))
    last = ids[-1]
    want = now + 3 * 1800   # 4 ahead at 2 per batch: its batch is the third
    assert eta.estimate(p.db, last, now, delivery=eta.delivery_of(p))["end"] == pytest.approx(want)
    assert eta.delivery_of(p.config()) == eta.delivery_of(p) and eta.delivery_of({}) is None
    capped = eta.field(eta.estimate(p.db, last, now, delivery=eta.delivery_of(p)), p, now)
    assert capped != eta.field(eta.estimate(p.db, last, now), p, now), "the default cap of 8 reads otherwise"
    line = next(x for x in eta.state_lines(p.db, p, now) if x.startswith(f"- #{last} "))
    assert line.endswith(capped), (line, capped)
    assert eta.state_lines(p.db, p, now) == eta.state_lines(p.db, p.config(), now)
    e = eta.obligation_estimate(p.db, {"id": "o1", "owners": [last]}, now, delivery=eta.delivery_of(p))
    assert e["end"] == pytest.approx(want), e


def test_eta_landing_gate_with_a_live_owner_follows_its_critical_path(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    n, w, o = _eta_gated(p, now)
    p.db.add_task(f"Review #{n}", "s", kind="review", depends_on=[n], status="failed")
    rv = p.db.add_task(f"Review #{n}", "s", kind="review", depends_on=[n])   # its re-review is queued
    e = eta.estimate(p.db, w, now)
    assert e["why"] is None and not e.get("after") and e["path"][:3] == [w, n, rv]
    # The re-review's queue and run, the landing, then the work task's own queue and run.
    assert e["end"] == pytest.approx(now + 100 + 1700 + 600 + 100 + 500)
    assert eta.obligation_estimate(p.db, o, now)["end"] == pytest.approx(e["end"])
    # An open fix continuing it (no review yet) is a live owner too.
    p.db.update_task(rv, status="cancelled")
    fix = p.db.add_task("Fix review", "s", kind="code", labels=[f"continues:{n}"])
    e = eta.estimate(p.db, w, now)
    assert not e.get("after") and "done but not landed" not in (e["why"] or ""), e
    p.db.update_task(fix, status="cancelled")
    # A live owner with no measured leg: no clock time, and the gate is named.
    p.db.update_task(n, status="queued")
    p.db.x("DELETE FROM runs WHERE task IN (SELECT id FROM tasks WHERE kind='code')")
    e = eta.estimate(p.db, w, now)
    assert e["after"] == f"landed:#{n}" and "no measured code run" in e["why"]


def test_eta_merge_gate_reads_after_the_pr_merges_or_closes(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    for probe in ("gh pr view 12 --repo o/r --json state -q .state | grep -qv OPEN",
                  "gh api repos/o/r/pulls/12 --jq .merged | grep -q true"):
        t = p.db.add_task("after the PR", "s", kind="work", labels=[f"start_when:{probe}"])
        e = eta.estimate(p.db, t, now)
        assert e["end"] is None and e["after"] == "PR #12 merges or closes", probe
        assert eta.field(e, p, now).startswith("ETA: after PR #12 merges or closes ("), probe
        w = _eta_waiting(p, now, retry_when=probe)
        assert eta.estimate(p.db, w, now)["after"] == "PR #12 merges or closes"
        # A measured eta of the merge (with its basis) is not a probe time: it is used.
        m = _eta_waiting(p, now, retry_when=probe, eta=now + 3600, eta_basis="the PR's approvals are in")
        assert eta.estimate(p.db, m, now)["end"] == pytest.approx(now + 3600 + 1200)
    # Not a merge gate: a PR's CI checks, or a probe naming no state.
    for probe in ("gh pr checks 12 --repo o/r", "gh pr view 12 --json title"):
        assert eta.merge_gate(probe) is None, probe


def test_eta_order_skips_a_cancelled_owner_for_its_live_one(env):
    from ttp import eta
    from ttp import obligations as ob
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    a = p.db.add_task("first try", "s", kind="work")
    b = p.db.add_task("second try", "s", kind="work")
    oid = ob.add(p, "ship the fix", owners=[a, b], now=now)
    p.db.update_task(a, status="cancelled")
    live = eta.estimate(p.db, b, now)
    assert live["why"] is None and live["end"] > now
    # cancelled_owner_skipped: the cancelled owner no longer hides the live one's measured ETA.
    assert eta.obligation_estimate(p.db, ob.get(p.db, oid), now)["end"] == pytest.approx(live["end"])
    # An abandoned or superseded owner is skipped the same way.
    c = p.db.add_task("third try", "s", kind="work")
    d = p.db.add_task("fourth try", "s", kind="work")
    ob.attach(p, oid, [c, d], now=now)
    p.db.update_task(c, status="done")
    p.db.update_task(d, status="done")
    p.db.x("INSERT INTO task_abandoned(task, why, ts) VALUES(?,?,?)", (c, "dropped by a re-plan", now))
    p.db.x("INSERT INTO task_superseded(task, head, sha, ts) VALUES(?,?,?,?)", (d, "h", "abc123", now))
    assert ob.live_owner_ids(p.db, ob.get(p.db, oid)) == [b]
    # A failed owner whose continuation carries the work on follows that continuation, an owner or not.
    f = p.db.add_task("failed try", "s", kind="work")
    o2 = ob.add(p, "ship the other fix", owners=[f], now=now)
    p.db.update_task(f, status="failed")
    assert eta.obligation_estimate(p.db, ob.get(p.db, o2), now)["why"] == f"#{f} failed"
    g = p.db.add_task("continue: failed try", "s", kind="work", labels=[f"continues:{f}"])
    assert ob.live_owner_ids(p.db, ob.get(p.db, o2)) == [g]
    assert eta.obligation_estimate(p.db, ob.get(p.db, o2), now)["end"] == pytest.approx(
        eta.estimate(p.db, g, now)["end"])


def test_eta_order_with_all_owners_dead_says_no_live_owner(env):
    from ttp import eta
    from ttp import obligations as ob
    p = make(env)
    now = time.time()
    _eta_history(p, now)
    a = p.db.add_task("first try", "s", kind="work", priority=1, reply_chat="c1")
    o = _eta_promise(p, a, "c1", "I'll post the result here.", now)
    b = p.db.add_task("second try", "s", kind="work", priority=1)
    ob.attach(p, o["id"], [b], now=now)
    p.db.update_task(a, status="cancelled")
    p.db.update_task(b, status="cancelled")
    o = ob.get(p.db, o["id"])
    e = eta.obligation_estimate(p.db, o, now)
    # all_owners_dead_says_no_live_owner
    assert e["end"] is None and e["why"] == "no live owner task (all cancelled or replaced)"
    assert "no live owner task" in eta.field(e, p, now)
    # Its only urgent owners are dead: no long-promise line on urgent work.
    assert not eta._urgent_owner(p.db, o)
    # history_kept: the stored owners and the ledger line still list the dead owners.
    assert {a, b} <= set(ob._owner_ids(o)) and a in o["promise"]["tasks"] and str(b) in o["owners"]
    assert any(f"#{a}" in line for line in ob.promise_lines(p.db, now))



def test_eta_a_deferred_task_s_likely_and_conservative_times_start_after_its_planned_start(env):
    """A task deferred to a start_after (spec: its likely and conservative times must both sit after
    its start, and conservative is never before likely; the correction scales only its work, never the
    wait for the planned start)."""
    from ttp import eta, etaledger
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    p.db.set_kv(etaledger.KEY, {"on": True, "factors": {"work": 3.0}, "conservative_x": 5.5, "at": now})
    start = now + 9.5 * 3600
    a = p.db.add_task("later", "s", kind="work", labels=[f"start_after:{start:.0f}"])
    p.db.x("UPDATE tasks SET not_before=? WHERE id=?", (start, a))
    e = eta.estimate(p.db, a, now)
    assert e["deferred"] == start
    assert e["end"] == pytest.approx(start + 3 * (300 + 1200)), e["end"] - start   # the wait itself unscaled
    assert e["end"] > start and e["conservative"] >= e["end"] > start and e["p90_end"] >= e["end"]
    # A waiting task held to the same planned start: same rule, whatever holds it there.
    w = p.db.add_task("waiting", "s", kind="work", status="waiting")
    p.db.update_task(w, not_before=start)
    ew = eta.estimate(p.db, w, now)
    assert ew["deferred"] == start and ew["end"] == pytest.approx(start + 3 * (300 + 1200))
    assert ew["conservative"] >= ew["end"] > start
    # An undeferred task keeps the correction over its whole time left, as before.
    b = p.db.add_task("now", "s", kind="work")
    e2 = eta.estimate(p.db, b, now)
    assert "deferred" not in e2 or not e2["deferred"]
    assert e2["end"] == pytest.approx(now + 3 * (300 + 1200)) and e2["conservative"] >= e2["end"]


def test_eta_field_dates_a_time_a_week_away():
    """A time 6 days or more away keeps its date, so a conservative time days past the likely one never
    reads as earlier than it (the weekday-only form names the nearer day)."""
    from ttp import eta
    now = 1795708800.0   # 2026-11-26 16:00 UTC
    e = {"end": now + 3600, "conservative": now + 5400, "path": [1], "why": None, "factor": 1.0, "overrun": False}
    assert "conservative 17:30 UTC" in eta.field(e, "UTC", now)
    assert "conservative Sat 04:00 UTC" in eta.field({**e, "conservative": now + 12 * 3600 + 86400}, "UTC", now)
    line = eta.field({**e, "conservative": now + 6.5 * 86400}, "UTC", now)
    assert "conservative 2026-12-03 04:00 UTC" in line, line

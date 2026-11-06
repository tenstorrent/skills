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
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (rv, "b", "h", "t", "pushed", now - 3000, now - 2400))
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
    assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 5000 + 1500)
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
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
           (rv, "b", "h", "t", "pushed", now - 3000, now - 2400))   # medians: wait 100, review 1700, landing 600
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
        assert eta.estimate(p.db, other, now)["end"] == pytest.approx(own), state   # a mixed probe is not judged
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


def test_eta_time_and_landing_waits_keep_their_estimates(env):
    from ttp import eta
    p = make(env)
    now = time.time()
    _eta_history(p, now)                                   # run median 1200, queue wait median 300
    # A bare clock gate, `landed:#N`, `ttp landed --task N` terms and no retry_when at all: the next try
    # plus the usual legs.
    for res in ({"retry_when": f'test "$(date +%s)" -ge {int(now) + 600}'}, {"retry_when": "landed:#12"},
                {"retry_when": " landed: 12 "}, {"retry_when": "ttp landed --task 12 && ttp landed --task #13 || exit 1"},
                {}):
        t = _eta_waiting(p, now, retry_after=600, **res)
        assert eta.outside_wait(p.db.task(t), now) is None
        assert eta.estimate(p.db, t, now)["end"] == pytest.approx(now + 600 + 300 + 1200), res
    # A deferred task's start_when is the deferral's, not a waiting hand-off's.
    d = p.db.add_task("deferred", "s", kind="work", labels=["start_when:ssh box true"])
    p.db.update_task(d, result=json.dumps({"status": "waiting", "retry_when": "ssh box true"}))
    assert eta.outside_wait(p.db.task(d), now) is None


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

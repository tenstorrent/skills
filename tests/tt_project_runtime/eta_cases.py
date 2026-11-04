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

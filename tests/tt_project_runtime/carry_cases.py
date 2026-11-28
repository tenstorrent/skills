# Carry-task tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there). Add new carry-task tests here, not there.
from __future__ import annotations


def _carry_task(p, title, spec="s", kind="code", **kw):
    return p.db.task(p.db.add_task(title, spec, kind=kind, origin="user", **kw))


def test_carry_is_selected_by_mode_and_by_what_makes_a_task_ordered(env):
    from ttp import carry, obligations, userorder
    p = make(env)
    plain, urgent = _carry_task(p, "plain"), _carry_task(p, "urgent", "urgent: now\nfix it")
    owner, msged = _carry_task(p, "owner"), _carry_task(p, "msged")
    asked = _carry_task(p, "asked", "urgent: now", kind="question")
    first, chatty = _carry_task(p, "first", priority=1), _carry_task(p, "chatty", reply_chat="c1")
    obligations.add_order(p, "do it", p.db.post("in", "do it", chat="c1"), [owner["id"]], action_class="change")
    mid = p.db.post("in", "do that", chat="c1")
    assert userorder.record(p.db, msged["id"], mid, ["rig"]) == ["rig"]
    cfg = lambda m=None: {"review": {"inline": m}} if m else p.config()
    sel = lambda m, *ts: [carry.selected(cfg(m), t, p.db) for t in ts]
    every = (plain, urgent, owner, msged, asked)
    assert carry.mode(p.config()) == "ordered", "the default"
    assert sel(None, *every) == [False, True, True, True, False]
    assert sel("ordered", *every) == [False, True, True, True, False]
    assert sel("all", *every) == [True, True, True, True, False], "every code task, no other kind"
    assert sel("off", *every) == [False] * 5
    assert sel("nonsense", *every) == sel("ordered", *every), "an unknown mode reads as the default"
    assert sel("ordered", first, chatty) == [True, True], "what urgent.is_urgent and urgent.user_ordered decide"
    assert not carry.selected(p.config(), None, p.db)


def test_inline_review_flag_is_off_until_a_ring_arms_it_and_a_projects_own_value_wins(env):
    from ttp import carry, rollout
    p = make(env)
    urgent = _carry_task(p, "urgent", "urgent: now")
    assert rollout.FLAGS["inline_review"]["area"] == "review" and rollout.FLAGS["inline_review"]["key"] == "review.inline"
    assert not rollout.on("inline_review", p.name), "off by default"
    assert carry.selected(p.config(), urgent, p.db) and not carry.enabled(p.config(), urgent, p.db, p)
    p.set_config("review.inline", "all")
    assert carry.enabled(p.config(), urgent, p.db, p) and not carry.enabled(p.config(), urgent, p.db), "no project: off"
    p.set_config("review.inline", "off")
    assert not carry.enabled(p.config(), urgent, p.db, p), "its own `off` still turns carrying off"


def test_advance_writes_one_phase_event_per_change_and_keeps_the_fields(env):
    import json
    from ttp import carry
    p = make(env)
    t = _carry_task(p, "c")
    events = lambda: [json.loads(e["data"]) for e in p.db.q("SELECT data FROM events WHERE kind=? ORDER BY id", (carry.EVENT,))]
    s = carry.advance(p.db, t, carry.CHANGE, head="abc")
    assert (s["phase"], s["round"], s["head"]) == ("change", 0, "abc")
    carry.advance(p.db, t["id"], carry.REVIEW, review_run=7)
    carry.advance(p.db, t["id"], carry.REVIEW)   # no change: no event
    s = carry.advance(p.db, t["id"], carry.FIX, round=1, note="findings")
    assert (s["head"], s["review_run"], s["round"], s["note"]) == ("abc", 7, 1, "findings"), "earlier fields stay"
    got = events()
    assert [(e["task"], e["from"], e["to"]) for e in got] == [(t["id"], None, "change"), (t["id"], "change", "review"),
                                                           (t["id"], "review", "fix")]
    assert all(e["ts"] > 0 for e in got)
    assert not p.db.q("SELECT id FROM events WHERE kind=? AND status='new'", (carry.EVENT,)), "a record, no wake"
    for bad in ({"phase": "nope"}, {"phase": "fix", "bogus": 1}):
        with pytest.raises(ValueError):
            carry.advance(p.db, t, **bad)
    assert len(events()) == 3 and carry.state(p.db, t["id"])["phase"] == "fix"


def test_reconcile_moves_phases_forward_to_the_push_queue_and_finished_batches(env):
    from ttp import carry
    p = make(env)
    ids = {n: _carry_task(p, n)["id"] for n in ("queued", "landed", "done", "capped", "none", "ahead")}
    for n, tid in ids.items():
        carry.advance(p.db, tid, carry.CAPPED if n == "capped" else carry.REVIEW)
    carry.advance(p.db, ids["ahead"], carry.DEPLOY)
    now = 1000.0
    p.db.x("INSERT INTO push_batches(id, marker, target, started, after_push, after_finalized) VALUES('b1','m','t',?,'ok',?)", (now, now))
    p.db.x("INSERT INTO push_batches(id, marker, target, started, after_push, after_finalized) VALUES('b2','m','t',?,'failed',?)", (now, now))
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b3','m','t',?)", (now,))
    rows = {"queued": ("batched", "b3"), "landed": ("pushed", "b2"), "done": ("landed", "b1"),
            "capped": ("approved", None), "ahead": ("approved", None)}
    for n, (status, batch) in rows.items():
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated) VALUES(?,?,?,?,?,?,?,?)",
               (ids[n], "br", "h" + n, "t", status, batch, now, now))
    moved = carry.reconcile(p.db)
    assert sorted((t, a, b) for t, a, b in moved) == sorted(
        [(ids["queued"], "review", "queued"), (ids["landed"], "review", "landed"), (ids["done"], "review", "done")])
    phase = lambda n: carry.state(p.db, ids[n])["phase"]
    assert [phase(n) for n in ("queued", "landed", "done", "capped", "none", "ahead")] == \
        ["queued", "landed", "done", "capped", "review", "deploy"], "forward only; capped is the coordinator's"
    assert carry.state(p.db, ids["done"])["batch"] == "b1" and carry.state(p.db, ids["queued"])["head"] == "hqueued"
    assert carry.reconcile(p.db) == [], "nothing moves twice"


def test_task_show_prints_the_carry_phase_only_when_a_state_exists(env, capsys):
    from ttp import carry, cli
    p = make(env)
    t = _carry_task(p, "shown")
    show = lambda: (capsys.readouterr(), cli.main(["task", p.name, "show", str(t["id"])]), capsys.readouterr().out)[2]
    assert "carry:" not in show()
    carry.advance(p.db, t, carry.FIX, round=2)
    assert "carry: fix round 2" in show().splitlines()
    assert carry.show_line(p.db, t["id"]) == "carry: fix round 2" and carry.show_line(p.db, 0) == ""


# The push queue settles a carry task (part 4): helpers.
def _pushing(p, status="pushed", batch="b1", after_push=None, n=0, **kw):
    """A carry task in `pushing` whose newest approval is one `status` row in batch `batch`."""
    from ttp import carry
    t = _carry_task(p, "carried", "urgent: now\nship it", **kw)
    p.db.update_task(t["id"], status="pushing")
    carry.advance(p.db, t["id"], carry.QUEUED)
    p.db.x("INSERT OR IGNORE INTO push_batches(id, marker, target, started, after_push) VALUES(?,?,?,?,?)",
           (batch, "m", "origin/x", 1000.0, after_push))
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?,?)",
           (t["id"], "ttp/t-c", "a" * 40, "origin/x", status, batch, 1000.0 + n, 1000.0 + n, "b" * 40))
    return p.db.task(t["id"]), p.db.one("SELECT * FROM push_batches WHERE id=?", (batch,))


def _settle_it(p, t, b, m=None):
    from ttp import pushq
    pushq._settle(p, t["id"], b, {"log": "/tmp/l", "tip": "c" * 40, **(m or {})}, 2000.0)
    return p.db.task(t["id"]), carry_phase(p, t)


def carry_phase(p, t):
    from ttp import carry
    return carry.state(p.db, t["id"])["phase"]


def _evidence(p, tid):
    from ttp import obligations
    st = obligations._load(p.db)
    return [e["text"] for it in st["items"].values() for e in (it["owners"].get(str(tid)) or {}).get("evidence") or []]


def test_a_landed_carry_task_is_done_at_once_without_a_deploy_step_and_its_order_gets_the_sha(env):
    from ttp import obligations
    p = make(env)
    t, b = _pushing(p)
    obligations.add_order(p, "ship it", p.db.post("in", "ship it", chat="c1"), [], action_class="change")
    obligations.attach(p, "o1", [t["id"]])
    got, phase = _settle_it(p, t, b)
    assert (got["status"], phase) == ("done", "done")
    assert [e for e in _evidence(p, t["id"]) if e.startswith("landed bbbbbbb")], _evidence(p, t["id"])
    assert p.db.q("SELECT id FROM events WHERE task=? AND kind='task_done'", (t["id"],))


def test_a_landed_carry_task_waits_for_its_deploy_and_the_after_push_outcome_finishes_it(env):
    from ttp import pushq
    p = make(env)
    p.set_config("delivery.after_push", "true")
    t, b = _pushing(p)
    got, phase = _settle_it(p, t, b)
    assert (got["status"], phase) == ("pushing", "deploy"), "the batch's deploy step follows"
    pushq._after(p, b, {"pushed_sha": "b" * 40, "after_push": {}}, "ok", lambda *a, **k: None, 3000.0)
    assert (p.db.task(t["id"])["status"], carry_phase(p, t)) == ("done", "done")
    pushq._after(p, b, {"pushed_sha": "b" * 40, "after_push": {}}, "ok", lambda *a, **k: None, 3001.0)
    assert len(p.db.q("SELECT id FROM events WHERE task=? AND kind='task_done'", (t["id"],))) == 1, "once"


def test_a_failed_deploy_raises_one_event_and_wakes_the_owner_only_when_the_log_names_its_files(env, monkeypatch):
    from ttp import pushq
    p = make(env)
    p.set_config("delivery.after_push", "true")
    t, b = _pushing(p)
    _settle_it(p, t, b)
    monkeypatch.setattr(pushq, "_changed", lambda p, tip, head: frozenset({"src/mine.py"}))
    monkeypatch.setattr(pushq, "_tail_of", lambda m, d=None, n=0: "boom in other.py")
    pushq._after(p, b, {"tip": "t" * 40, "after_push": {"cmd": "deploy"}, "log": "l"}, "failed",
                 lambda *a, **k: None, 3000.0)
    assert p.db.task(t["id"])["status"] == "blocked" and carry_phase(p, t) == "deploy", "not its files: no wake, no longer pushing"
    assert len(p.db.q("SELECT id FROM events WHERE task=? AND kind='carry_deploy_failed' AND status='queued'", (t["id"],))) == 1
    t2, b2 = _pushing(p, batch="b2", after_push=None)
    p.db.x("UPDATE carry_state SET phase='deploy', batch='b2' WHERE task=?", (t2["id"],))
    monkeypatch.setattr(pushq, "_tail_of", lambda m, d=None, n=0: "Traceback in src/mine.py line 3")
    pushq._after(p, b2, {"tip": "t" * 40, "after_push": {"cmd": "deploy"}, "log": "l"}, "failed", lambda *a, **k: None, 3000.0)
    got = p.db.task(t2["id"])
    assert got["status"] == "queued" and "src/mine.py" in (got["result"] or ""), "woken with the log"
    assert carry_phase(p, t2) == "deploy", "reconcile never moves it back"


def test_a_conflict_wakes_the_owner_to_rebase_in_place_and_a_third_goes_capped(env):
    p = make(env)
    t, b = _pushing(p, "conflict", max_attempts=3)
    got, phase = _settle_it(p, t, b)
    assert (got["status"], phase) == ("queued", "rebase")
    assert "rebase it in place" in got["result"] and "push conflict" in got["result"]
    for n in (1, 2, 3):   # approvals sent back as conflicts, one more each
        p.db.update_task(t["id"], status="pushing")
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated) VALUES(?,?,?,?,?,?,?,?)",
               (t["id"], "ttp/t-c", "a" * 40, "origin/x", "conflict", "b1", 1000.0 + n, 1000.0 + n))
        got, phase = _settle_it(p, p.db.task(t["id"]), b)
    assert (got["status"], phase) == ("failed", "capped")


def test_failed_push_checks_wake_the_owner_with_the_log_and_trimmed_ones_stay_queued(env, monkeypatch):
    from ttp import carry, conflictfix
    p = make(env)
    t, b = _pushing(p, "check_failed")
    got, phase = _settle_it(p, t, b, {"log": "/tmp/the-log"})
    assert (got["status"], phase) == ("queued", "checkfix")
    assert "/tmp/the-log" in got["result"] and "fix it in place" in got["result"]
    t2, b2 = _pushing(p, "check_failed", batch="b2")
    assert carry.settle_kind({"check_failed"}, {"ids": []}) is None and carry.settle_kind({"refused"}, None) is None
    monkeypatch.setattr(conflictfix, "cap_failure", lambda tail, pat=None: {"ids": ["t"], "over": []})
    got, phase = _settle_it(p, t2, b2)
    assert (got["status"], phase) == ("queued", "queued"), "a trim keeps the phase and today's trim flow"


def test_reconcile_between_approve_and_settle_lets_the_batch_end_finish_the_task(env):
    from ttp import carry, pushq
    p = make(env)
    p.set_config("delivery.after_push", "true")
    t, b = _pushing(p, "pushed")
    assert carry.reconcile(p.db) == [(t["id"], "queued", "landed")]
    pushq._after(p, b, {"pushed_sha": "b" * 40, "after_push": {}}, "skipped", lambda *a, **k: None, 3000.0)
    assert (p.db.task(t["id"])["status"], carry_phase(p, t)) == ("done", "done")


def test_a_task_without_carry_state_settles_as_a_review_and_a_carry_owner_is_its_own_reviewed_task(env):
    from ttp import push, pushq
    p = make(env)
    plain = _carry_task(p, "plain")
    p.db.update_task(plain["id"], status="pushing")
    p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('bp','m','origin/x',1.0)")
    p.db.x("INSERT INTO push_queue(task,branch,head,target,status,batch,created,updated,pushed_sha) VALUES(?,?,?,?,?,?,?,?,?)",
           (plain["id"], "ttp/t-p", "a" * 40, "origin/x", "pushed", "bp", 1.0, 1.0, "b" * 40))
    pushq._settle(p, plain["id"], p.db.one("SELECT * FROM push_batches WHERE id='bp'"), {}, 2000.0)
    got = p.db.task(plain["id"])
    assert got["status"] == "done" and "pushed" in got["result"] and "carry" not in got["result"]
    from ttp import carry
    assert push._reviewed_tasks(p, str(plain["id"])) == []
    carry.advance(p.db, plain["id"], carry.REVIEW)
    assert [r["id"] for r in push._reviewed_tasks(p, str(plain["id"]))] == [plain["id"]]


def test_carry_report_counts_scheduler_minutes_and_phase_waits(env):
    import json, time
    from ttp import carry, schedlog
    p = make(env)
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    f = schedlog.path(p)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps({"ts": iso(now - 3600 + 600 * i), "held": h}) + "\n" for i, h in
                         enumerate([{"depends_on": 2, "carry": 1}, {"depends_on": 1}, {}])))
    for ts, to in ((now - 3000, "change"), (now - 2400, "review"), (now - 1800, "queued"), (now - 1200, "landed"),
                   (now - 600, "done")):
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
               (ts, "daemon", carry.EVENT, "low", "x", json.dumps({"task": 7, "from": None, "to": to, "ts": ts}),
                "handled", 7))
    a, b = carry.report(p, 1)
    assert "30 min on depends_on" in a and "10 min as carry" in a   # 3 x 10 min and 1 x 10 min, then 1 x 10 min
    assert "carry orders done: 1, median lead time 40 min, median push-queue/landed/deploy wait 20 min" in b


def test_a_queued_carry_task_waiting_on_depends_on_is_held_as_carry(env):
    import json
    from ttp import carry, daemon as dm, schedlog
    p = make(env)
    a = _carry_task(p, "first")
    b = _carry_task(p, "second", depends_on=[a["id"]])
    c = _carry_task(p, "plain", depends_on=[a["id"]])
    carry.advance(p.db, b["id"], carry.REVIEW)
    d = dm.Daemon(p.base)
    d.cfg = p.config()
    d._log_slot_fill(0, [], {}, {"now": 1000.0}, "claude")
    line = json.loads(schedlog.path(p).read_text().splitlines()[-1])
    assert line["held"]["carry"] == 1 and line["held"]["depends_on"] == 1


def _carry_finish(env, p, title="carried"):
    p.set_config("review.inline", "all")
    _with_origin(env, clone=False)
    return _finish_code(env, p, title, {"app.py": 20})


def _carry_dispatch(p, d, monkeypatch):
    from ttp import budget as bud
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(
        (role, prompt, provider, tier, cwd, k)) or 4242)
    for prov in (d.cfg.get("core_provider", "claude"), "chat", "codex"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    d.dispatch()
    return started


def test_a_carry_tasks_done_hand_off_queues_no_review_task_but_pins_its_head_for_a_review_run(env):
    from ttp import carry
    p = make(env)
    tid, branch, done, reviews = _carry_finish(env, p)
    assert reviews == [] and not p.db.q("SELECT id FROM tasks WHERE kind='review'"), "no Review # task"
    t, s = p.db.task(tid), carry.state(p.db, tid)
    assert t["status"] == "queued" and int(t["attempts"]) == 1, "back in the queue; the author's run is its one attempt"
    head = _git_out(p.root, "rev-parse", branch).strip()
    assert (s["phase"], s["round"], s["head"], s["review_run"]) == ("review", 1, head, None)
    assert _git_out(p.root, "rev-parse", carry.pin_ref(tid, 1)).strip() == head, "the reviewed commit is pinned"
    spec = json.loads(s["note"])
    assert f"Independently review the change of code task #{tid}" in spec["spec"] and head[:12] in spec["spec"]


def test_a_non_carry_task_still_gets_its_review_task(env):
    from ttp import carry
    p = make(env)
    _with_origin(env, clone=False)
    tid, _, _, reviews = _finish_code(env, p, "plain", {"app.py": 20})
    assert len(reviews) == 1 and reviews[0]["title"] == f"Review #{tid}: plain" and carry.state(p.db, tid) is None
    assert p.db.task(tid)["status"] == "done"


def test_the_carry_review_run_is_a_fresh_reviewer_run_of_the_same_task_at_the_pinned_head(env, monkeypatch):
    from pathlib import Path
    from ttp import carry
    from ttp.daemon import Daemon
    p = make(env)
    tid, branch, _, _ = _carry_finish(env, p)
    head = carry.state(p.db, tid)["head"]
    _git_out(p.root, "update-ref", f"refs/heads/{branch}", _git_out(p.root, "rev-parse", f"{head}~1").strip())   # the branch moves on
    d = Daemon(p.base)
    (role, prompt, prov, tier, cwd, k), = _carry_dispatch(p, d, monkeypatch)
    assert role == "reviewer" and k["task"]["id"] == tid and k.get("resume") is None
    assert f"Independently review the change of code task #{tid}" in prompt
    assert _git_out(Path(cwd), "rev-parse", "HEAD").strip() == head and cwd.endswith(f"t{tid}-review"), "a worktree of its own"
    assert carry.state(p.db, tid)["review_run"] == 4242 and p.db.task(tid)["status"] == "running"
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'")
    # Its run ends: the worktree goes, no attempt is spent and no second review starts.
    run_dir = env["tmp"] / "carry-review-run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "approved"}))
    from ttp.providers.base import RunUsage
    d._finish_worker({"task": tid, "role": "reviewer"}, RunUsage(cost_usd=0.5), "ok", run_dir)
    assert not Path(cwd).exists() and int(p.db.task(tid)["attempts"]) == 1
    assert carry.state(p.db, tid)["round"] == 1 and not p.db.q("SELECT id FROM tasks WHERE kind='review'")


def test_a_task_whose_carry_state_outlives_the_flag_still_gets_its_review_task(env):
    from ttp import carry
    p = make(env)   # review.inline is not set: carry is off
    _with_origin(env, clone=False)
    tid, _, _, reviews = _finish_code(env, p, "stale", {"app.py": 20},
                                      before=lambda t, head: carry.advance(p.db, t, carry.CHANGE))
    assert carry.state(p.db, tid) is not None and not carry.enabled(p.config(), p.db.task(tid), p.db, p)
    assert [r["title"] for r in reviews] == [f"Review #{tid}: stale"]


def test_a_finished_review_task_still_spends_its_attempt(env):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    _with_origin(env, clone=False)
    _, _, _, reviews = _finish_code(env, p, "plain", {"app.py": 20})
    rid = reviews[0]["id"]
    run_dir = env["tmp"] / "plain-review-run"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "approved"}))
    before = int(p.db.task(rid)["attempts"])
    Daemon(p.base)._finish_worker({"task": rid, "role": "reviewer"}, RunUsage(cost_usd=0.5), "ok", run_dir)
    assert int(p.db.task(rid)["attempts"]) == before + 1


@pytest.mark.parametrize("ending", ["failed", "timeout", "no_handoff"])
def test_a_failing_carry_review_run_counts_an_attempt_and_stops_after_max_attempts(env, monkeypatch, ending, tmp_path):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    p = make(env)
    tid, _, _, _ = _carry_finish(env, p, ending)
    d = Daemon(p.base)
    attempts = int(p.db.task(tid)["attempts"])
    runs = 0
    while p.db.task(tid)["status"] == "queued" and runs < 10:
        _carry_dispatch(p, d, monkeypatch)
        run_dir = tmp_path / f"run{runs}"
        run_dir.mkdir()
        d._finish_worker({"task": tid, "role": "reviewer", "note": "{}"}, RunUsage(cost_usd=0.1), ending, run_dir)
        runs += 1
    t = p.db.task(tid)
    assert t["status"] != "queued" and runs == int(t["max_attempts"] or 3) - attempts, "bounded by max_attempts"
    assert int(t["attempts"]) == int(t["max_attempts"] or 3)


def _inline(env, monkeypatch):
    """A push-queue project (_pq) whose code task has an author run (session `author`) and an inline review
    due on its head, round 1: the next reviewer run of the task is that review's verdict."""
    from ttp import carry
    s = _pq(env, monkeypatch)
    s.p.set_config("review.inline", "all")
    s.head = s.head.strip()
    s.p.db.x("INSERT INTO runs(task, role, started, ended, status, session_id) VALUES(?,?,?,?,?,?)",
             (s.code, "worker", 1.0, 2.0, "ok", "author"))
    carry.begin_review(s.p, s.p.db.task(s.code), s.head, note=json.dumps(
        {"title": "t", "spec": "Independently review it.", "tier": "standard", "labels": None}))
    s.p.db.update_task(s.code, status="running", attempts=1)
    return s


def _verdict(env, s, result, session="reviewer"):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    run_dir = env["tmp"] / f"verdict-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    s.p.db.update_task(s.code, status="running")
    Daemon(s.p.base)._finish_worker({"task": s.code, "id": 9, "role": "reviewer", "note": "{}"},
                                    RunUsage(cost_usd=0.5, session_id=session), "ok", run_dir)
    return s.p.db.task(s.code), run_dir


_REVIEW_PASS = lambda s: {"status": "done", "summary": "looks good", "push": [{"branch": s.branch, "head": s.head}]}
_REVIEW_FAIL = {"status": "failed", "summary": "it breaks", "followups": [{"title": "Fix the crash", "spec": "guard the empty list"}]}


def test_an_independent_inline_pass_approves_the_push_row_for_the_owner_and_advances_the_phase(env, monkeypatch):
    from ttp import carry
    s = _inline(env, monkeypatch)
    t, _ = _verdict(env, s, _REVIEW_PASS(s))
    rows = s.p.db.q("SELECT * FROM push_queue")
    assert t["status"] == "pushing" and [(r["task"], r["run"], r["head"], r["status"]) for r in rows] == [
        (s.code, 9, s.head, "approved")]
    st = carry.state(s.p.db, s.code)
    assert (st["phase"], st["push_row"], st["round"]) == ("queued", rows[0]["id"], 1)
    assert int(t["attempts"]) == 1, "a review run is no attempt"


def test_an_inline_pass_is_refused_from_the_authors_own_session_or_a_moved_branch(env, monkeypatch):
    from ttp import carry
    s = _inline(env, monkeypatch)
    t, _ = _verdict(env, s, _REVIEW_PASS(s), session="author")
    assert t["status"] == "queued" and not s.p.db.q("SELECT * FROM push_queue") and carry.state(s.p.db, s.code)["phase"] == "review"
    assert "author's own session" in t["result"]
    _commit(s.path, "late.txt", "late\n")   # the reviewer (or someone) committed: the branch is not the reviewed head
    t, _ = _verdict(env, s, _REVIEW_PASS(s))
    assert not s.p.db.q("SELECT * FROM push_queue") and "moved during the review" in t["result"]
    assert carry.state(s.p.db, s.code)["phase"] == "capped", "the second refusal is the approval's form failing: the coordinator's"


def test_a_failed_inline_review_fixes_in_place_and_its_re_review_lists_the_earlier_findings(env, monkeypatch):
    from ttp import carry, reviewcap
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage
    s = _inline(env, monkeypatch)
    before = s.p.db.q("SELECT id FROM tasks")
    t, run_dir = _verdict(env, s, _REVIEW_FAIL)
    st = carry.state(s.p.db, s.code)
    assert (st["phase"], st["round"], t["status"], int(t["attempts"])) == ("fix", 1, "queued", 1), "the owner is woken; no attempt"
    assert "## Update (review round 1: findings)" in t["spec"] and "guard the empty list" in t["spec"]
    assert str(run_dir / "result.json") in t["spec"] and "it breaks" in t["spec"]
    assert "its review (round 1) failed" in json.loads(t["result"])["woke"]
    assert s.p.db.q("SELECT id FROM tasks") == before, "no Review or Fix task"
    assert s.code in reviewcap.failed_reviews(s.p.db, 0), "the failure still counts for the review cap"
    _commit(s.path, "fix.txt", "fixed\n")   # the author's next run fixes it and hands off
    again = env["tmp"] / "author-again"
    again.mkdir()
    (again / "result.json").write_text(json.dumps({"status": "done", "summary": "fixed"}))
    s.p.db.update_task(s.code, status="running")
    Daemon(s.p.base)._finish_worker({"task": s.code, "id": 10, "role": "worker", "note": "{}"}, RunUsage(cost_usd=1.0), "ok", again)
    st = carry.state(s.p.db, s.code)
    assert (st["phase"], st["round"]) == ("review", 2) and st["head"] != s.head
    assert "guard the empty list" in json.loads(st["note"])["spec"], "the re-review checks each earlier finding"


def test_a_second_failed_inline_review_caps_the_task_for_the_coordinator(env, monkeypatch):
    from ttp import carry
    s = _inline(env, monkeypatch)
    _verdict(env, s, _REVIEW_FAIL)
    carry.begin_review(s.p, s.p.db.task(s.code), s.head, note="{}")   # round 2 of the same head
    t, run_dir = _verdict(env, s, _REVIEW_FAIL)
    assert carry.state(s.p.db, s.code)["phase"] == "capped" and t["status"] == "review"
    ev, = s.p.db.q("SELECT * FROM events WHERE kind='carry_capped'")
    assert str(run_dir / "result.json") in ev["text"] and ev["status"] == "queued" and ev["severity"] == "high"
    assert int(t["attempts"]) == 1


def test_an_author_failure_of_a_carry_task_is_no_failed_review_for_the_cap(env, monkeypatch):
    from ttp import reviewcap
    s = _inline(env, monkeypatch)
    s.p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "task_failed", "normal", "the author timed out", "handled", s.code))
    assert s.code not in reviewcap.failed_reviews(s.p.db, 0)


def test_a_carry_pass_with_no_valid_approval_falls_back_to_the_standard_review_not_done(env, monkeypatch):
    from ttp import carry
    from ttp.daemon import Daemon
    s = _inline(env, monkeypatch)
    asked, real = [], Daemon._auto_review
    monkeypatch.setattr(Daemon, "_auto_review", lambda self, t, *a, **k: (asked.append(t["id"]), real(self, t, *a, **k))[1])
    _verdict(env, s, {"status": "done", "summary": "looks good"})   # a pass with no push list
    assert carry.state(s.p.db, s.code)["phase"] == "plain" and not s.p.db.q("SELECT * FROM push_queue")
    assert len(s.p.db.q("SELECT id FROM events WHERE kind='carry_fallback'")) == 1
    assert not carry.enabled(s.p.config(), s.p.db.task(s.code), s.p.db, s.p), "the standard flow takes the task"
    assert s.p.db.task(s.code)["status"] == "done" and asked == [s.code], "the daemon's normal review is asked for"


def test_a_carry_pass_of_a_change_that_needs_no_landing_ends_done(env, monkeypatch):
    from ttp import carry
    s = _inline(env, monkeypatch)
    s.p.db.update_task(s.code, spec=f"{s.p.db.task(s.code)['spec'] or ''}\nno_push: notes only")
    t, _ = _verdict(env, s, {"status": "done", "summary": "looks good"})
    assert carry.state(s.p.db, s.code)["phase"] == "done" and t["status"] == "done"
    assert not s.p.db.q("SELECT id FROM events WHERE kind='carry_fallback'")

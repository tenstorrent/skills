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
    import subprocess
    from ttp import pushq
    p = make(env)
    p.set_config("delivery.after_push", "true")
    t, b = _pushing(p)
    _settle_it(p, t, b)
    monkeypatch.setattr(pushq, "_git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "src/mine.py\n", ""))
    monkeypatch.setattr(pushq, "_tail_of", lambda m, d=None, n=0: "boom in other.py")
    pushq._after(p, b, {"pushed_sha": "b" * 40, "after_push": {"cmd": "deploy"}, "log": "l"}, "failed",
                 lambda *a, **k: None, 3000.0)
    assert p.db.task(t["id"])["status"] == "pushing" and carry_phase(p, t) == "deploy", "not its files: no wake"
    assert len(p.db.q("SELECT id FROM events WHERE task=? AND kind='carry_deploy_failed' AND status='queued'", (t["id"],))) == 1
    t2, b2 = _pushing(p, batch="b2", after_push=None)
    p.db.x("UPDATE carry_state SET phase='deploy', batch='b2' WHERE task=?", (t2["id"],))
    monkeypatch.setattr(pushq, "_tail_of", lambda m, d=None, n=0: "Traceback in src/mine.py line 3")
    pushq._after(p, b2, {"pushed_sha": "b" * 40, "after_push": {"cmd": "deploy"}, "log": "l"}, "failed", lambda *a, **k: None, 3000.0)
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
    assert carry.outcome({"check_failed"}, {"ids": []}) is None and carry.outcome({"refused"}, None) is None
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

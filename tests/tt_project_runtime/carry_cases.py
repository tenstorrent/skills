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

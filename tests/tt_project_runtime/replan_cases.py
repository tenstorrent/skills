# Missed-promise tests of tests/test_tt_project_runtime.py (ttp/replan.py), run in that file's namespace
# (see _include_cases there): a due move after a miss needs a plan change, a second miss is a re-plan.
from __future__ import annotations


def _rp_promise(p, tid, chat, text, now):
    """A promise the coordinator made in `chat` (default due: 6 h out), owned by task `tid`."""
    from ttp import obligations as ob
    mid = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)", (now, "out", "reply", text, 1))
    ob.record_promises(p, [(mid, chat, text)], {tid: chat}, now=now)
    return next(o for o in ob.open_promises(p.db) if mid == o["promise"]["message"])


def _rp_setup(env, monkeypatch, now=None):
    from ttp import obligations as ob
    p = make(env)
    now = time.time() if now is None else now
    tid = p.db.add_task("merge the release", "s", kind="work", priority=2, reply_chat="c1")
    other = p.db.add_task("unrelated", "s", kind="work", priority=3)
    o = _rp_promise(p, tid, "c1", "I'll post the merged result here.", now)
    late = o["promise"]["due"] + 600
    monkeypatch.setattr(time, "time", lambda: late)
    return p, ob, o["id"], tid, other, late


def test_replan_missed_due_move_is_refused_without_a_plan_change(env, monkeypatch):
    """Past its due, `obligation` key+due alone is refused, naming what counts; nothing moves. A batch with
    a non-owner update, a one-word justify or a refused plan action does not count either."""
    from ttp import coordinator as coord
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    due = ob.get(p.db, oid)["promise"]["due"]
    for extra in ([], [{"type": "task_update", "id": other, "priority": 1}],
                  [{"type": "obligation", "key": oid, "justify": "faster"}]):
        out = coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600}, *extra])
        assert any("a new time alone is refused" in n for n in out), (extra, out)
        assert ob.get(p.db, oid)["promise"]["due"] == due
    out = coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600}])
    msg = next(n for n in out if "refused" in n)
    for what in ("task_add naming", "priority, resources, depends_on, status or spec", "config_set",
                 "`justify`", "`message`", f"ttp eta {oid}"):
        assert what in msg, what
    assert ob.get(p.db, oid)["promise"]["misses"]   # the miss is on record either way
    # Before its due, a move needs nothing.
    p2, ob2, oid2, *_ = _rp_setup(env, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: ob2.get(p2.db, oid2)["promise"]["due"] - 60)
    assert coord.apply(p2, [{"type": "obligation", "key": oid2, "due": late + 7200}]) == []


def test_replan_missed_due_moves_with_each_kind_of_plan_change(env, monkeypatch):
    """Each plan change in the same batch lets the move through: an owner's task_update of a plan field, a
    task_add naming the key, a new owner, a justify naming what changed (also on the move itself)."""
    from ttp import coordinator as coord, replan
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    batches = [
        [{"type": "task_update", "id": tid, "priority": 1}],
        [{"type": "task_update", "id": tid, "spec": "new approach: rebase on main, drop the flaky gate"}],
        [{"type": "task_add", "title": "split: land the docs part", "spec": f"part of {oid}: the docs", "kind": "work"}],
        [{"type": "obligation", "key": oid, "owners": [other]}],
        [{"type": "obligation", "key": oid, "justify": "moved the test run to a free host"}],
    ]
    for n, extra in enumerate(batches):
        st = p.db.kv(ob.KV)   # put it past its due again
        st["items"][oid]["promise"]["due"] = late - 60
        p.db.set_kv(ob.KV, st)
        to = late + 3600 * (n + 1)
        out = coord.apply(p, [*extra, {"type": "obligation", "key": oid, "due": to}])
        assert out == [], (extra, out)
        pr = ob.get(p.db, oid)["promise"]
        assert pr["due"] == to and pr["moves"][-1]["plan"], extra
    st = p.db.kv(ob.KV)
    st["items"][oid]["promise"]["due"] = late - 60
    p.db.set_kv(ob.KV, st)
    assert coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 9e4,
                            "justify": "split the merge into two smaller pushes"}]) == []
    # The bottleneck: a config_set on a key the owner's block reason names counts; another key does not.
    p.db.update_task(tid, blocked_reason="waits on budget.max_parallel slots")
    o = ob.get(p.db, oid)
    assert replan.plan_change(p.db, o, [{"type": "config_set", "key": "budget.max_parallel", "value": 4}])
    assert replan.plan_change(p.db, o, [{"type": "config_set", "key": "budget.daily_usd", "value": 4}]) is None
    assert replan.plan_change(p.db, o, [{"type": "task_update", "id": tid, "priority": 1}], skip={0}) is None


def test_replan_user_asked_move_is_never_refused(env, monkeypatch):
    """A move citing the user's own message goes through past its due with no plan change; one citing a
    message the user did not write is refused."""
    from ttp import coordinator as coord, obligations as ob
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    said = p.db.post("in", "move it to tomorrow morning, no rush", chat="c1")
    ours = p.db.x("INSERT INTO messages(ts,direction,kind,text,handled) VALUES(?,?,?,?,?)", (late, "out", "reply", "x", 1))
    out = coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600, "message": ours}])
    assert any("not a message the user wrote" in n for n in out), out
    assert coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600, "message": said}]) == []
    mv = ob.get(p.db, oid)["promise"]["moves"][-1]
    assert mv["user"] == said and mv["missed"] and not mv["plan"]


def test_replan_second_miss_is_a_must_act_replan(env, monkeypatch):
    """The first miss raises the usual overdue item; after a re-planned move, the second miss of the same
    promise raises a must-act item marked re-plan, and the must-act block says so."""
    import json
    from ttp import coordinator as coord, mustact, replan
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    ob.check(p, late)
    first = p.db.q("SELECT * FROM events WHERE kind=? ORDER BY id", (ob.PROMISE_KIND,))
    assert len(first) == 1 and "Missed twice" not in first[0]["text"]
    assert "replan" not in json.loads(first[0]["data"])["must_act"]
    assert coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1},
                           {"type": "obligation", "key": oid, "due": late + 3600}]) == []
    ob.check(p, late + 3700)
    evs = p.db.q("SELECT * FROM events WHERE kind=? ORDER BY id", (ob.PROMISE_KIND,))
    assert len(evs) == 2 and "Missed twice: re-plan" in evs[1]["text"] and "Expected finish" in evs[1]["text"]
    assert json.loads(evs[1]["data"])["must_act"]["replan"] is True
    assert len(ob.get(p.db, oid)["promise"]["misses"]) == 2
    assert "(re-plan: missed twice)" in mustact.block(p.db, [evs[1]["id"]])
    assert replan.REPLAN_TEXT.split(":")[0] in evs[1]["text"]


def test_replan_reply_with_a_new_time_and_no_plan_change_is_flagged(env, monkeypatch):
    """A reply giving a new time in the chat of a missed promise is flagged when its batch changed no plan,
    not when it did; a reply in another chat is not. The move is recorded for the metric."""
    from ttp import coordinator as coord, replan
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    assert coord.apply(p, [{"type": "reply", "chat": "c2", "text": "I'll have the other thing done by 23:59."}]) == []
    assert not p.db.q("SELECT id FROM events WHERE kind=?", (replan.FLAG_KIND,))
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "text": "I'll have it merged by 23:59."}]) == []
    flags = p.db.q("SELECT * FROM events WHERE kind=?", (replan.FLAG_KIND,))
    assert len(flags) == 1 and oid in flags[0]["text"] and "what changed" in flags[0]["text"]
    assert coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1},
                           {"type": "reply", "chat": "c1", "text": "I'll have it merged by 23:58 on a free host."}]) == []
    assert len(p.db.q("SELECT id FROM events WHERE kind=?", (replan.FLAG_KIND,))) == 1
    moves = ob.get(p.db, oid)["promise"]["moves"]
    assert [bool(m["plan"]) for m in moves] == [False, True]


def test_replan_daily_review_metric(env, monkeypatch):
    """The audit's `missed promises` line counts the missed, the missed twice, the moves after a miss with
    no plan change (target 0) and the median lateness; nothing missed shows no line."""
    from datetime import datetime, timezone
    from ttp import audit, coordinator as coord, replan
    # Pinned so the miss lands at 23:47 UTC: a bare '23:59' reply would itself be a promise missed by the
    # last check, so the reply names a relative time that holds at any time of day.
    now = datetime(2026, 1, 5, 17, 37, tzinfo=timezone.utc).timestamp()
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch, now)
    assert replan.audit_line(p.db, late - 86400, late) == ""
    ob.check(p, late)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "I'll have it merged in 3 hours."}])
    assert coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1},
                           {"type": "obligation", "key": oid, "due": late + 3600}]) == []
    ob.check(p, late + 3700)
    line = replan.audit_line(p.db, late + 3700 - 86400, late + 3700)
    assert line.startswith("missed promises, 24 h:") and "1 missed, 1 missed twice or more" in line, line
    assert "1 due moves without a plan change (target 0)" in line and "median lateness 1.2 h" in line, line
    assert line in "\n".join(audit.lines(p.db, late + 3700))


def test_replan_clock_times_from_20_to_23_are_promises(env):
    """'by 22:20' is a timed promise: the clock pattern once read only its '2' and found no time."""
    from datetime import datetime, timezone
    from ttp import obligations as ob
    now = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp()
    for text, hm in (("I'll have it merged by 22:20.", (22, 20)), ("ETA 23:59.", (23, 59)),
                     ("I'll post it at 2:30 pm.", (14, 30)), ("We'll have it at 9:05.", (9, 5))):
        assert ob.commitments(text) == [text], text
        at = datetime.fromtimestamp(ob.promise_due(text, now, timezone.utc), timezone.utc)
        assert (at.hour, at.minute) == hm, text


def test_replan_prompt_rule_and_docs(env):
    coord_md = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    guide = (RUNTIME.parent / "docs" / "guide.md").read_text()
    assert "A missed promise or order is a trigger to change approach, never only to post a new time" in coord_md
    assert "### A missed promise changes the plan" in guide and "`missed promises`" in guide


def test_replan_order_resent_with_a_new_due_after_a_miss_needs_a_new_owner(env, monkeypatch):
    """Re-sending a missed order's `order` action with a later due is the same move: refused alone,
    kept with a new owner."""
    from ttp import coordinator as coord, obligations as ob
    p = make(env)
    now = time.time()
    tid = p.db.add_task("build it", "s", kind="code", priority=2, reply_chat="c1")
    other = p.db.add_task("build it another way", "s", kind="code", priority=2)
    mid = p.db.post("in", "please build it", chat="c1")
    oid = ob.add_order(p, "build it", mid, [tid], due=now + 600, action_class="change", now=now).split()[1]
    monkeypatch.setattr(time, "time", lambda: now + 1200)
    act = {"type": "obligation", "order": mid, "text": "build it", "action_class": "change", "due": now + 7200}
    out = coord.apply(p, [{**act, "owners": [tid]}])
    assert any("a new time alone is refused" in n for n in out), out
    assert ob.get(p.db, oid)["promise"]["due"] == now + 600
    assert coord.apply(p, [{**act, "owners": [other]}]) == []
    pr = ob.get(p.db, oid)["promise"]
    assert pr["due"] == now + 7200 and pr["moves"][-1]["plan"] == f"new owner #{other}"


def test_replan_user_asked_move_needs_a_newer_message_in_its_chat(env, monkeypatch):
    """`message` carries a missed move only when the user wrote it in the promise's chat after its due was
    set: an older message, the order's own message and one from another chat are refused, saying why."""
    from ttp import coordinator as coord, obligations as ob
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    due = ob.get(p.db, oid)["promise"]["due"]
    old = p.db.post("in", "hello, unrelated", chat="c1")
    elsewhere = p.db.post("in", "push it to tomorrow", chat="c2")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (late - 10 * 86400, old))
    for mid, why in ((old, "came before its current due was set"), (elsewhere, "not the promise's chat")):
        out = coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600, "message": mid}])
        assert any("a new time alone is refused" in n and why in n for n in out), (mid, out)
        assert ob.get(p.db, oid)["promise"]["due"] == due
    newer = p.db.post("in", "move it to tomorrow morning, no rush", chat="c1")
    assert coord.apply(p, [{"type": "obligation", "key": oid, "due": late + 3600, "message": newer}]) == []
    assert ob.get(p.db, oid)["promise"]["moves"][-1]["user"] == newer
    # An order's own message came before the due it set.
    now = time.time()
    t2 = p.db.add_task("build it", "s", kind="code", priority=2, reply_chat="c1")
    mid = p.db.post("in", "please build it", chat="c1")
    o2 = ob.add_order(p, "build it", mid, [t2], due=now + 600, action_class="change", now=now).split()[1]
    monkeypatch.setattr(time, "time", lambda: now + 1200)
    out = coord.apply(p, [{"type": "obligation", "key": o2, "due": now + 7200, "message": mid}])
    assert any("came before its current due was set" in n for n in out), out
    assert ob.get(p.db, o2)["promise"]["due"] == now + 600


def test_replan_same_value_task_update_is_no_plan_change(env, monkeypatch):
    """A task_update setting an owner's field to the value it already has changes no plan: the missed move
    is refused; a real change in the same field lets it through."""
    from ttp import coordinator as coord
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    due = ob.get(p.db, oid)["promise"]["due"]
    spec = p.db.task(tid)["spec"]
    for same in ({"priority": 2}, {"spec": spec}, {"priority": 2, "spec": spec}):
        out = coord.apply(p, [{"type": "task_update", "id": tid, **same},
                              {"type": "obligation", "key": oid, "due": late + 3600}])
        assert any("a new time alone is refused" in n for n in out), (same, out)
        assert ob.get(p.db, oid)["promise"]["due"] == due
    assert coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1},
                           {"type": "obligation", "key": oid, "due": late + 3600}]) == []
    assert ob.get(p.db, oid)["promise"]["moves"][-1]["plan"] == f"task_update #{tid} changes priority"


def test_replan_order_resent_takes_the_batchs_plan_change(env, monkeypatch):
    """A re-sent missed order's new due follows the key+due rule: an owner's task_update in the same batch
    (which its refusal names) lets it through; nothing, or a same-value update, is refused."""
    from ttp import coordinator as coord, obligations as ob
    p = make(env)
    now = time.time()
    tid = p.db.add_task("build it", "s", kind="code", priority=2, reply_chat="c1")
    mid = p.db.post("in", "please build it", chat="c1")
    oid = ob.add_order(p, "build it", mid, [tid], due=now + 600, action_class="change", now=now).split()[1]
    monkeypatch.setattr(time, "time", lambda: now + 1200)
    act = {"type": "obligation", "order": mid, "text": "build it", "action_class": "change", "due": now + 7200}
    for extra in ([], [{"type": "task_update", "id": tid, "priority": 2}]):
        out = coord.apply(p, [*extra, act])
        assert any("a new time alone is refused" in n for n in out), (extra, out)
        assert ob.get(p.db, oid)["promise"]["due"] == now + 600
    out = coord.apply(p, [{"type": "task_update", "id": tid, "priority": 1, "spec": "new approach: split it"}, act])
    assert out == [], out
    pr = ob.get(p.db, oid)["promise"]
    assert pr["due"] == now + 7200 and pr["moves"][-1]["plan"].startswith(f"task_update #{tid} changes")


def test_replan_a_review_task_add_earlier_in_the_batch_keeps_the_batch_start_time(env, monkeypatch):
    """A review task_add's under-way auto reviews never replace the batch's start time, which the due
    moves compare owners' attach times with: an `obligation` naming an owner after it is judged, never a
    TypeError out of apply."""
    from ttp import coordinator as coord
    p, ob, oid, tid, other, late = _rp_setup(env, monkeypatch)
    review = {"type": "task_add", "kind": "review", "title": "review the merge", "spec": "review it", "tier": "light"}
    for owner in (tid, other):
        out = coord.apply(p, [{**review, "title": f"review the merge for #{owner}"},
                              {"type": "obligation", "key": oid, "owners": [owner], "due": late + 3600}])
        assert not any("TypeError" in n for n in out), out
        assert p.db.one("SELECT id FROM tasks WHERE title=?", (f"review the merge for #{owner}",))
    assert {tid, other} <= {int(t) for t in ob.get(p.db, oid)["owners"]}

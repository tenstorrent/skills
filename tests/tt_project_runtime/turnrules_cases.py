# Turn rules of tests/test_tt_project_runtime.py (runtime/ttp/turnrules.py: each user message's order or
# `no_order`), which runs this file in its own namespace (see _include_cases there): helpers, fixtures and
# imports come from that file and the other case files (_order_setup and _turn: obligations_cases.py).
from __future__ import annotations


def _tr_refused(p):
    from ttp import coordinator as coord, turnrules
    return [x for x in p.db.kv(coord.REJECTED_KEY, []) or [] if x.startswith(turnrules.PREFIX)]


def _tr_noted(p, text):
    from ttp import coordinator as coord
    return [n for n in p.db.kv(coord.NOTES_KEY, []) or [] if text in n]


def _tr_handled(p, m):
    return p.db.one("SELECT handled FROM messages WHERE id=?", (m,))["handled"]


def test_turnrules_a_reply_naming_its_message_by_order_is_applied_as_answered_in_reply(env, monkeypatch):
    """Order gap, form (a): the sample turn sent a reply to the message's chat with `order` N and no
    `no_order`, refused 'user message #N has no order and no `no_order`'. It answers N: applied with
    `no_order` "answered in reply" (one note), the message handled, nothing refused. A reply to another chat
    or naming none, one beside an obligation recording that order, or one with its own reason is left as
    sent."""
    from ttp import turnrules
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    reply = {"type": "reply", "chat": "c1", "text": "Nothing to build yet: the deliverable is the soak log.",
             "order": mid}
    for a, batch in ((dict(reply, chat="c2"), []), (dict(reply, chat=""), []),
                     (reply, [{"type": "obligation", "order": mid}]), (dict(reply, no_order="thanks only"), [])):
        assert turnrules.normal_form(p, a, batch or [a], "UTC", now) is None, a
    _turn(d, 1, [reply], [mid])
    assert _tr_refused(p) == [] and _tr_handled(p, mid) == 1
    assert _tr_noted(p, f"applied as: no_order='answered in reply' (was: none; the reply names user message #{mid}")


def test_turnrules_the_one_task_add_replying_to_the_message_s_chat_owns_its_order(env, monkeypatch):
    """Order gap, form (b): the sample turn met the message only with a task_add whose `reply_chat` is its
    chat. That task owns the order: recorded with its title as text (a change order for a work task, a read
    order for a question task), the message handled, nothing refused. Two task_adds replying there leave
    the owner unclear: refused as before."""
    from ttp import obligations as ob
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    m = p.db.post("in", "please add a retry to the upload step", chat="c1")
    add = {"type": "task_add", "title": "Add a retry to the upload step", "reply_chat": "c1", "kind": "work",
           "spec": "Wrap the upload call in a bounded retry; reply with the result."}
    _turn(d, 1, [add], [m])
    new = p.db.one("SELECT id FROM tasks WHERE title=?", (add["title"],))["id"]
    o = ob.open_order(p.db, m)
    assert _tr_refused(p) == [] and _tr_handled(p, m) == 1, p.db.kv("coordinator_rejected")
    assert o["text"] == add["title"] and o["promise"]["tasks"] == [new] and o["promise"]["class"] == "change", o
    assert _tr_noted(p, f"applied as: obligation order={m} owners=[{new}] action_class='change'")
    q = p.db.post("in", "what does the soak log say about the last run?", chat="c1")
    _turn(d, 2, [{"type": "task_add", "title": "Read the soak log", "reply_chat": "c1", "kind": "question",
                  "spec": "Read the newest soak log and answer what the last run did."}], [q])
    assert ob.open_order(p.db, q)["promise"]["class"] == "read" and _tr_refused(p) == []
    two = p.db.post("in", "build both report variants", chat="c1")
    _turn(d, 3, [{"type": "task_add", "title": f"Report variant {v}", "reply_chat": "c1", "kind": "work",
                  "spec": f"Build report variant {v} from the weekly table."} for v in ("one", "two")], [two])
    assert ob.open_order(p.db, two) is None and _tr_handled(p, two) == 0
    assert len(_tr_refused(p)) == 1 and f"#{two} has no order and no `no_order`" in _tr_refused(p)[0]


def test_turnrules_a_held_reply_answers_nothing_even_beside_a_task_replying_there(env, monkeypatch):
    """The held-reply form stays refused: a reply naming the message by `order` that the chat digest held
    never reached the user, so it answers nothing, and a task_add replying to that chat does not record the
    order in its place: 'your reply answering it was held for the chat digest and NOT delivered'."""
    from ttp import coordinator as coord, obligations as ob
    monkeypatch.setenv("TTP_TEST_CHAT_DIGEST", "on")
    p, d, now, tid, check, mid, raised = _order_setup(env, monkeypatch)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "Backlog: 12 tasks open, 3 queued; #40 done."}], turn=1)
    m = p.db.post("in", "fine, carry on", chat="c1")
    _turn(d, 2, [{"type": "reply", "chat": "c1", "text": "Backlog: 11 tasks open, 4 queued; #41 done.", "order": m},
                 {"type": "task_add", "title": "Close the stale queued tasks", "reply_chat": "c1", "kind": "work",
                  "spec": "Close the queued tasks idle for a week; reply with the new count."}], [m])
    gap = _tr_refused(p)
    assert len(gap) == 1 and f"#{m}: your reply answering it was held" in gap[0] and "NOT delivered" in gap[0], gap
    assert ob.open_order(p.db, m) is None and _tr_handled(p, m) == 0

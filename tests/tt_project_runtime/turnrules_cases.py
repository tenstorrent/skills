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


def test_turnrules_a_reply_repeating_a_cited_run_sends_only_its_new_part(env):
    """Replayed refusal 'reply not sent: run N's result went to this chat in message #M; say only what is
    new': the reply goes out without its sentences repeating message #M word for word, run N dropped from
    its cites, with a note. With nothing left, no sentence of it told apart as the repeat, or a sentence
    left still naming run N (it may carry new facts), the refusal stays."""
    from ttp import coordinator as coord
    p = make(env)
    r1, r2 = (p.db.x("INSERT INTO runs(task, role, status, started) VALUES(NULL, 'worker', 'ok', ?)", (time.time(),))
              for _ in range(2))

    def replies():
        return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND kind='reply' "
                                          "AND chat='c1' ORDER BY id")]
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "cites": [r1],
                            "text": f"Run {r1} passed. The bench is 12 ms."}]) == []
    first = p.db.one("SELECT MAX(id) id FROM messages WHERE kind='reply'")["id"]
    for text in (f"The bench is 12 ms. Run {r1} passed.", "Again: the bench stays at 12 ms.",
                 f"Run {r1} passed. Run {r1} failed again after the fix. I'm rolling back."):
        probs = coord.apply(p, [{"type": "reply", "chat": "c1", "cites": [r1], "text": text}])
        assert len(probs) == 1 and f"run {r1}'s result went to this chat in message #{first}" in probs[0], probs
    assert len(replies()) == 1
    assert coord.apply(p, [{"type": "reply", "chat": "c1", "cites": [r1, r2],
                            "text": f"Run {r1} passed.\nThe bench is 12 ms. Run {r2} found the cache fix "
                                    f"cuts it to 9 ms."}]) == []
    assert replies()[-1] == f"Run {r2} found the cache fix cuts it to 9 ms.", replies()
    assert any("sent only its new part" in n and f"run {r1}" in n for n in p.db.kv(coord.NOTES_KEY, []))
    probs = coord.apply(p, [{"type": "reply", "chat": "c1", "cites": [r2], "text": f"Run {r2}: 9 ms."}])
    assert probs and f"run {r2}'s result went to this chat" in probs[0], probs


def test_turnrules_a_claim_state_differing_only_in_case_is_read_as_the_allowed_one(env):
    """Replayed refusal "claim `state` is one of queued, running, waiting, started": a state that is an
    allowed one but for case or spaces is read as it, with a note, and still checked against the task's
    record (a false status stays refused). Another word stays refused. Replayed 'claim due: a time needs
    an absolute timestamp with a time zone offset': a due with no zone, as an ISO time or a clock time, is
    already read in the home zone; a date alone stays refused."""
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("soak the cache", "s", origin="user")

    def send(claim):
        return coord.apply(p, [{"type": "reply", "chat": "c1", "text": "The soak is queued.",
                                "claims": [{"text": "the soak is queued", "task": tid, **claim}]}])
    assert send({"state": " Queued "}) == []
    assert any("`state` ' Queued ' read as 'queued'" in n for n in p.db.kv(coord.NOTES_KEY, []))
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    probs = send({"state": "QUEUED"})
    assert probs and f"#{tid} is done in its record, not queued" in probs[0], probs
    probs = send({"state": "pending"})
    assert probs and "claim `state` is one of" in probs[0], probs
    for due in ("2030-01-02T09:00", "09:00"):
        assert send({"due": due}) == [], due
    probs = send({"due": "2030-01-02"})
    assert probs and "claim due" in probs[0], probs

# A coordinator reply announcing a charter change carries its charter_update in the same batch
# (runtime/ttp/charterpromise.py, coordinator.apply's batch order and _charter_retry_now). Run from
# test_tt_project_runtime.py.

_CP_ITEM = "Push only to branch work/x of the demo repo. Never push to main. Never open pull requests."
_CP_CHARTER = "# demo\n\n## Restrictions\n- " + _CP_ITEM + "\n  (user msg #1)\n\n## Policies\nReview first.\n"
_CP_ANNOUNCE = "On it. I'm adding the Restrictions item now, then queuing the run."


def _cp_events(p, kind="charter_unsent"):
    return p.db.q("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))


def test_a_reply_announcing_a_charter_change_without_a_charter_update_queues_an_event_once(env):
    """The reported case: the reply goes out, one queued event quotes it and asks for the change or a
    correction; the same turn applied again adds none; a notify is checked the same way."""
    from ttp import charterpromise, coordinator as coord
    p = make(env)
    p.charter_path.write_text(_CP_CHARTER)
    assert coord.apply(p, [{"type": "reply", "text": _CP_ANNOUNCE, "chat": "c1"}], turn=1) == []
    mid = p.db.one("SELECT id FROM messages WHERE direction='out' AND kind='reply' ORDER BY id DESC")["id"]
    evs = _cp_events(p)
    assert len(evs) == 1 and evs[0]["status"] == "queued" and evs[0]["source"] == "harness", evs
    text = evs[0]["text"]
    assert f"reply #{mid}" in text and "I'm adding the Restrictions item now" in text, text
    assert "Send that charter_update now, or tell the chat it is not being made" in text
    assert coord.EFFORT_EVENT_TRIGGERS[charterpromise.EVENT] == "conflict"
    coord.apply(p, [{"type": "reply", "text": _CP_ANNOUNCE, "chat": "c1"}], turn=1)
    assert len(_cp_events(p)) == len({e["fingerprint"] for e in _cp_events(p)})
    coord.apply(p, [{"type": "notify", "text": "I'll record that in the charter."}], turn=2)
    assert "notify #" in _cp_events(p)[-1]["text"]


def test_a_charter_announcement_with_its_charter_update_in_the_batch_applied_or_refused_is_not_flagged(env):
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_CP_CHARTER)
    ok = {"type": "charter_update", "section": "Notes", "text": "Box A is free to use."}
    assert coord.apply(p, [{"type": "reply", "text": "Adding it to the charter.", "chat": "c1"}, ok], turn=1) == []
    assert "Box A is free to use." in p.charter_path.read_text()
    bad = {"type": "charter_update", "section": "Notes", "text": ""}   # refused: no text and no quote
    assert coord.apply(p, [{"type": "reply", "text": "I'll add it to the charter.", "chat": "c1"}, bad], turn=2)
    assert _cp_events(p) == []


def test_the_charter_announcement_matcher_catches_first_person_present_and_future_and_skips_the_rest():
    from ttp import charterpromise as cp
    hits = [
        "I'm adding the Restrictions item.",
        "I am now adding a restriction for that.",
        "I'll record that in the charter.",
        "We will update the Policies section today.",
        "adding it to the charter",
        "On it, adding it to the charter now.",
        "Let me add a Goals item for the dashboard.",
        "I'm going to remove that restriction.",
        "Thanks. I'm also retiring the old policy item.",
        "I'm adding them to the Restrictions now.",
        "I'll go ahead and add it to the charter.",
    ]
    for t in hits:
        assert cp.announces(t), t
    skips = [
        "I added the Restrictions item yesterday.",            # past
        "I've recorded that in the charter already.",          # past
        "The charter was updated in turn 12.",                 # past, no first person
        'The Restrictions say "I\'ll add nothing to main".',  # quoting
        "The charter reads: `I'm adding the restriction`.",    # quoting
        "> I'm adding the Restrictions item\nThat is what the charter says.",   # quoted line
        "I'm not adding it to the charter.",                   # negation
        "I won't change the charter.",                         # negation
        "Should I add it to the charter?",                     # question
        "I'll add the Restrictions item?",                     # question
        "Once you confirm, I'll add the restriction.",         # conditional on the user
        "I'm adding a task to update the charter.",            # a task about it
        "I'm recording a memory about the charter.",           # memory, not the charter
        "I'll update you on the restriction.",                 # update you
        "I'm adding a retry; the box has access restrictions beyond our control for now and later.",
        "I'm adding the retry to the run script and the Restrictions stay as they are.",   # object too far
        "",
    ]
    for t in skips:
        assert not cp.announces(t), t
    assert not cp.has_update([{"type": "reply"}, None, "x"])
    assert cp.has_update([{"type": "charter_update"}])


def test_charter_updates_apply_before_task_add_and_task_update_in_the_same_batch(env, monkeypatch):
    """A task added after a charter_update in the batch's order reads the new charter: the batch lists
    the task_add and task_update first, the charter_update last."""
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_CP_CHARTER)
    tid = p.db.add_task("existing", "s", origin="user")
    seen = {}
    add, upd = p.db.add_task, p.db.update_task
    def add_task(*a, **k):
        seen["add"] = p.charter_path.read_text()
        return add(*a, **k)
    def update_task(i, **k):
        if i == tid:
            seen["update"] = p.charter_path.read_text()
        return upd(i, **k)
    monkeypatch.setattr(p.db, "add_task", add_task)
    monkeypatch.setattr(p.db, "update_task", update_task)
    item = "Box B is free to use for the run."
    acts = [{"type": "task_add", "title": "run on box B", "spec": "Do: run it on box B.", "kind": "harness"},
            {"type": "task_update", "id": tid, "priority": 1},
            {"type": "config_set", "key": "coordinator.ask_timeout_h", "value": "24"},
            {"type": "charter_update", "section": "Notes", "text": item}]
    assert coord.apply(p, acts, turn=1) == []
    assert item in seen["add"] and item in seen["update"], seen


def test_a_refused_charter_update_names_the_batch_replies_that_announced_it_in_its_charter_retry(env):
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_CP_CHARTER)
    lift = {"type": "charter_update", "section": "Policies", "text": "Workers may push to main for hotfixes."}
    said = "I'm adding the Policies item now, so hotfixes can go straight to main."
    # The reply comes after the refused charter_update in the batch: it is still named.
    assert len(coord.apply(p, [lift, {"type": "reply", "text": said, "chat": "c1"}], turn=1)) == 1
    out = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind='charter_retry' ORDER BY id")]
    assert len(out) == 1 and "immediate retry 1 of 2" in out[0], out
    assert "told the chat the change was being made: reply: \"I'm adding the Policies item now" in out[0], out[0]
    assert "tell that chat it is not made yet" in out[0]
    assert _cp_events(p) == []   # the refused charter_update backs it: no second event
    # A refusal with no announcing reply keeps its event as it was.
    coord.apply(p, [lift], turn=2)
    out = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind='charter_retry' ORDER BY id")]
    assert len(out) == 2 and "told the chat" not in out[1], out

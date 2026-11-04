# A refused Restrictions charter_update citing a user message: a filled resend for the coordinator and
# one alert in the relayer's chat (runtime/ttp/chartertemplate.py, coordinator.apply, _charter_retry_now).
# Run from test_tt_project_runtime.py.

_CT_CHARTER = "# demo\n\n## Restrictions\n- Never push to main. (user msg #1)\n\n## Policies\nReview first.\n"
_CT_SAID = "Run long jobs on box Q only at night, never during the working day."


def _ct_setup(env):
    p = make(env)
    p.charter_path.write_text(_CT_CHARTER)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    mid = p.db.post("in", _CT_SAID, chat="relay-chat", provenance="cli-legacy",
                    context="relayer note: the box is busy in the daytime")
    return p, mid


def _ct_alerts(p, kind="alert"):
    return p.db.q("SELECT * FROM messages WHERE direction='out' AND kind=? AND ref LIKE 'charter-refused:%' "
                  "ORDER BY id", (kind,))


def test_a_refused_restriction_citing_a_user_message_gets_a_filled_template_first(env):
    """The reported case: the cite sits in `text`, `source` is unset. The refusal and the charter_retry event
    start with a ready resend: same section, `source` {msg, quote} read from the chat log (the verbatim part,
    never the relayer's context), `text` without the cite; the reason follows in one line. Resent, it lands."""
    import json
    from ttp import coordinator as coord
    p, mid = _ct_setup(env)
    act = {"type": "charter_update", "section": "Restrictions",
           "text": f"- Run long jobs on box Q only at night. (user msg #{mid})"}
    problems = coord.apply(p, [act], user_turn=False)
    assert len(problems) == 1 and problems[0].startswith("charter_update: refused; resend this"), problems
    body, _, why = problems[0].partition(" -- why: ")
    filled = json.loads(body[body.index("{"):])
    assert filled["section"] == "Restrictions" and filled["text"] == "- Run long jobs on box Q only at night."
    assert json.loads(filled["source"]) == {"msg": mid, "quote": _CT_SAID}
    assert "\n" not in why and len(why) <= 200 and "needs `source`" in why and "SOURCE" not in why, why
    ev = p.db.q("SELECT text FROM events WHERE kind='charter_retry' ORDER BY id")
    assert len(ev) == 1 and ev[0]["text"].startswith("Resend this charter_update now"), ev
    assert json.loads(ev[0]["text"].split(": ", 1)[1].split("\n")[0]) == filled
    assert "Run long jobs on box Q only at night." not in p.charter_path.read_text()
    # A `source` quote that is not in the message's words is replaced by its words; a good one is kept.
    bad = coord.apply(p, [{**act, "source": json.dumps({"msg": mid, "quote": "the box is busy in the daytime"})}],
                      user_turn=False)
    assert json.loads(json.loads(bad[0][bad[0].index("{"):bad[0].index(" -- why: ")])["source"])["quote"] == _CT_SAID
    # The template is only a suggestion: resent, the gate checks it and it applies.
    resend = {k: v for k, v in filled.items()}
    assert coord.apply(p, [resend], user_turn=True, messages=[mid]) == [], resend
    assert f"- Run long jobs on box Q only at night. (user msg #{mid})" in p.charter_path.read_text()


def test_a_refused_restriction_with_no_readable_cited_message_gets_no_template(env):
    """Nothing is made up: an unknown message, a relayer's finding, the harness's own message, a message of
    only relayer context, or no cite at all: the refusal stays as it was, no template, no alert."""
    from ttp import coordinator as coord
    p, _ = _ct_setup(env)
    finding = p.db.post("in", "", chat="relay-chat", kind="finding", provenance="cli-legacy", context="Order: x y z")
    system = p.db.post("in", "run long jobs at night", chat=None, channel="system", provenance="system")
    ctx_only = p.db.post("in", "", chat="relay-chat", provenance="cli-legacy", context="run long jobs at night")
    for cite in (" (user msg #99999)", f" (user msg #{finding})", f" (user msg #{system})", f" (user msg #{ctx_only})",
                 ""):
        problems = coord.apply(p, [{"type": "charter_update", "section": "Restrictions",
                                    "text": "- Run long jobs on box Q only at night." + cite}], user_turn=False)
        assert len(problems) == 1 and "resend this" not in problems[0] and "rejected" in problems[0], (cite, problems)
    # A section other than Restrictions is never templated.
    assert coord.chartertemplate.template(p.db, {"section": "Policies", "text": "x (user msg #1)"}) is None
    assert not _ct_alerts(p)
    assert not any(e["text"].startswith("Resend this") for e in p.db.q("SELECT text FROM events"))


def test_a_refused_cited_restriction_alerts_the_relayers_chat_once_and_clears_when_it_lands(env):
    from ttp import chartertemplate
    from ttp import coordinator as coord
    p, mid = _ct_setup(env)
    other = p.db.post("in", "unrelated", chat="other-chat", provenance="cli-legacy")
    act = {"type": "charter_update", "section": "Restrictions",
           "text": f"- Run long jobs on box Q only at night. (user msg #{mid})"}
    for _ in range(3):   # the same refusal again: one alert
        coord.apply(p, [act], user_turn=False)
    sent = _ct_alerts(p)
    assert len(sent) == 1 and sent[0]["chat"] == "relay-chat" and sent[0]["severity"] == "normal", sent
    assert f"message #{mid}" in sent[0]["text"] and "Run long jobs on box Q only at night." in sent[0]["text"]
    assert "retry template" in sent[0]["text"] and f"#{mid})" not in sent[0]["text"]
    assert p.db.q("SELECT id FROM messages WHERE chat='other-chat' AND direction='out'") == [] and other
    # Another cause for the same item: one more alert; the item without its cite is the same item.
    coord.apply(p, [{**act, "text": "- Run long jobs on box Q only at night.",
                     "source": '{"msg": "x"}'}], user_turn=False)
    assert len(_ct_alerts(p)) == 1   # `source.msg` unreadable: the text has no cite, no message, no template
    coord.apply(p, [{**act, "source": '{"msg": %d, "quote": "the box is busy"}' % mid}], user_turn=False)
    assert len(_ct_alerts(p)) == 2, _ct_alerts(p)
    assert len(p.db.kv(chartertemplate.ALERTS_KEY)) == 1
    # It lands: the alert clears, told once (low) to the same chat; a later landing tells nothing more.
    filled = chartertemplate.template(p.db, act)[0]
    assert coord.apply(p, [filled], user_turn=True, messages=[mid]) == []
    done = _ct_alerts(p, "resolved")
    assert len(done) == 1 and done[0]["chat"] == "relay-chat" and done[0]["severity"] == "low", done
    assert not p.db.kv(chartertemplate.ALERTS_KEY)
    assert chartertemplate.landed(p.db, act) is None


def test_a_user_sourced_restriction_passes_the_people_rule_the_agents_words_do_not(env, monkeypatch):
    """Agent mode, a findings-only turn: an item lifting something with 'other projects' and 'tenant' in it
    falls in relay.HELD's people class. Resting on the owner's agent's words it is refused; with `source` the
    user's own verbatim words holding those words it applies (without that it would be refused). A held word
    the user did not say ('someone else's', 'force-push') still refuses, as do generic role words alone."""
    import json
    from ttp import chartertemplate, relay
    from ttp import coordinator as coord
    p, _ = _ct_setup(env)
    db = p.db
    p.set_config(relay.AGENT_KEY, True)
    said = "Other projects may use box Q at night when no tenant is on it."
    uid = db.post("in", said, chat="relay-chat", provenance="cli-legacy")
    db.x("UPDATE messages SET handled=1 WHERE id=?", (uid,))
    text = "- Other projects may use box Q at night when no tenant is on it."

    def findings_turn(act):
        fid = db.post("in", "", chat="relay-chat", kind="finding", provenance="cli-legacy", context=f"Order: {said}")
        return coord.apply(p, [act], user_turn=coord.user_turn(db, [fid]), messages=[fid])

    act = {"type": "charter_update", "section": "Restrictions", "text": text}
    agents = findings_turn({**act, "source": json.dumps({"msg": "x", "origin": "relayer", "quote": said})})
    assert agents and "falls in (a)" in agents[0], agents
    # Without the user's-word cover, the same user-sourced item would be refused by the people rule.
    users = {**act, "source": json.dumps({"msg": uid, "quote": said})}
    with monkeypatch.context() as m:
        m.setattr(chartertemplate, "user_covers", lambda *a, **k: False)
        refused = findings_turn(users)
        assert refused and "falls in (a)" in refused[0] and '"Other projects"' in refused[0], refused
    assert text not in p.charter_path.read_text()
    assert findings_turn(users) == []
    assert f"{text} (user msg #{uid})" in p.charter_path.read_text()
    # A held word the user's words lack still refuses; so does a quote too short to name a change.
    for extra in ("someone else's work", "force-push to main"):
        bad = {**act, "text": f"- Other projects may {extra} on box Q at night.",
               "source": json.dumps({"msg": uid, "quote": said}), "both_hold": True}
        out = findings_turn(bad)
        assert out and "falls in" in out[0], (extra, out)
    assert not chartertemplate.user_covers(db, json.dumps({"msg": uid, "quote": "box Q"}), text)
    # The user's words must hold the held words in the quote itself, and a ban covers no lift.
    assert not chartertemplate.user_covers(db, json.dumps({"msg": uid, "quote": "use box Q at night"}), text)
    ban = db.post("in", "Never let other projects use box Z while a tenant is on it.", chat="relay-chat",
                  provenance="cli-legacy")
    lift = {**act, "text": "- Other projects may use box Z while a tenant is on it.", "both_hold": True,
            "source": json.dumps({"msg": ban, "quote": "Never let other projects use box Z while a tenant is on it"})}
    out = findings_turn(lift)
    assert out and "falls in (a)" in out[0] and "may use box Z" not in p.charter_path.read_text(), out
    assert not chartertemplate.user_covers(db, json.dumps({"msg": uid, "origin": "relayer", "quote": said}), text)
    # Point 4's finding: the people rule reads generic role words alone, none of which names a person.
    for words in ("tenant", "other projects", "shared"):
        assert relay.held_match(f"jobs may run there for {words}")[0] == "people", words
    assert relay.held_match("jobs may run there for another project")[0] == ""

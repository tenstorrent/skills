# A plain yes to an ask, and the owner's agent's order, landing a Restrictions change (runtime yesask.py and
# the charter_update repair in coordinator.py) for tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers such as make and _SCOPED_BANS come from there and
# from coordinator_cases.py.
from __future__ import annotations


def _ask_and_yes(p, ask: str, yes: str = "yes of course. don't ask stupid questions") -> tuple[int, int]:
    """An ask in the web chat and the user's plain yes to it: (ask id, yes id)."""
    aid = p.db.post("out", ask, chat="web", kind="ask")
    return aid, p.db.post("in", yes, chat="web", channel="web", kind="user", provenance="web-session")


_YES_ASK = ("May workers power cycle host-a with `cycle_test.sh` for the cycle test? The ban on power cycling "
              "a serving host stands in the way.")
_YES_EXC = "Workers may power cycle host-a with `cycle_test.sh`."


def test_a_plain_yes_to_an_ask_lands_a_restrictions_exception_in_its_turn_and_later(env):
    """(a) The user's short verbatim yes to an ask about loosening a ban, cited as {msg, quote: the yes},
    lands: no `excepts`, so the harness repairs the lift into a scoped exception whose scope comes from
    the ask the yes answers, in the turn of the yes and in a later turn without it. A quote of the rule
    from the ask, the ask's own id as `msg`, or a cite in `text` the source does not name are form
    problems it fixes too; the turn's notes say what it adjusted. Before this, each was refused."""
    import json
    p = make(env)
    from ttp import coordinator as coord
    want = ("- Exception to \"Never power cycle a serving host.\" (Restrictions), which holds everywhere except inside "
            "this scope, where this item wins: " + _YES_EXC + " (user msg #{})")
    for k, (user, form) in enumerate(((True, "yes"), (False, "yes"), (False, "rule"), (False, "ask id"),
                                      (True, "foreign cite"))):
        p.charter_path.write_text(_SCOPED_BANS)
        aid, yid = _ask_and_yes(p, _YES_ASK)
        src = {"msg": aid if form == "ask id" else yid,
               "quote": "power cycle host-a with `cycle_test.sh`" if form in ("rule", "ask id") else "yes of course"}
        text = _YES_EXC + (f" (user msg #{aid})" if form == "foreign cite" else "")
        act = {"type": "charter_update", "section": "Restrictions", "text": text, "source": json.dumps(src)}
        err = coord.apply(p, [act], turn=1 + k, user_turn=user, messages=[yid] if user else None)
        notes = p.db.kv(coord.NOTES_KEY, []) or []
        assert err == [], (form, err)
        charter = p.charter_path.read_text()
        assert want.format(yid) in charter, (form, charter)
        assert _SCOPED_BANS.split("## Restrictions\n")[1].strip() in charter, form   # every ban stays as written
        assert any("charter_update repaired" in n for n in notes), (form, notes)


def test_agent_mode_the_owners_agent_order_lands_the_same_change(env):
    """(b) The same change ordered by the owner's agent (source origin relayer) with agent mode on lands
    as a scoped exception, cited relayer-ordered."""
    import json
    from ttp import relay
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_SCOPED_BANS)
    p.set_config(relay.AGENT_KEY, True)
    order = "power cycle host-a with cycle_test.sh for the cycle test"
    fid = p.db.post("in", "", chat="cli", kind="finding", provenance="cli-legacy", context=f"Order: {order}")
    act = {"type": "charter_update", "section": "Restrictions", "text": _YES_EXC,
           "source": json.dumps({"msg": fid, "origin": "relayer", "quote": order})}
    assert coord.apply(p, [act], turn=3, user_turn=False) == []
    assert (f"where this item wins: {_YES_EXC} (relayer-ordered, msg #{fid})") in p.charter_path.read_text()


def test_a_lift_the_users_words_do_not_cover_is_still_refused_naming_the_fix(env):
    """(c) is above; (d) a real conflict outside the user's words stays refused, and the refusal names the
    fix and why the harness could not repair it: a host the yes's ask never named, a host another item
    still bans, a general lift, and a hedged yes ("yes, but not today") or no ask at all."""
    import json
    p = make(env)
    from ttp import coordinator as coord
    for k, (text, yes, ask, why) in enumerate((
            ("Workers may power cycle host-a and host-b with `cycle_test.sh`.", None, True,
             "do not name host-b"),
            ("Workers may power cycle host-a and host-c with `cycle_test.sh`.", None, True,
             "wider than a ban another item keeps"),
            ("Workers may power cycle any serving host.", None, True, "lifts it in general"),
            (_YES_EXC, "yes, but not today", True, "contradicts the Restrictions item"),
            (_YES_EXC, None, False, "contradicts the Restrictions item"))):
        p.charter_path.write_text(_SCOPED_BANS)
        if ask:
            aid, yid = _ask_and_yes(p, _YES_ASK.replace("host-a", "host-a and host-c" if "host-c" in text
                                                         else "host-a"), *([yes] if yes else []))
        else:
            yid = p.db.post("in", "yes of course", chat="other", channel="web", kind="user", provenance="web-session")
        quote = "power cycle host-a" if yes or not ask else "yes of course"
        act = {"type": "charter_update", "section": "Restrictions", "text": text,
               "source": json.dumps({"msg": yid, "quote": quote})}
        err = coord.apply(p, [act], turn=1 + k, user_turn=True, messages=[yid])
        assert len(err) == 1 and why in err[0], (text, err)
        if "contradicts" not in why:
            assert "Not repaired as a scoped exception" in err[0] and "`excepts`" in err[0], err
        assert p.charter_path.read_text() == _SCOPED_BANS, text

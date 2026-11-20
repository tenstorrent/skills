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



_CITE_CHARTER = "# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n"


def _cite_change(p, cited, src: dict, turn: int, **kw) -> tuple[list[str], str]:
    """A Restrictions change whose text cites user msg #`cited` and whose `source` is `src`: (errors, the
    stored line, "" when none)."""
    import json
    from ttp import coordinator as coord
    act = {"type": "charter_update", "section": "Restrictions",
           "text": f"- Keep the changelog in English (user msg #{cited}).", "source": json.dumps(src)}
    err = coord.apply(p, [act], turn=turn, **kw)
    return err, next((x for x in p.charter_path.read_text().splitlines() if "changelog" in x), "")


def _user_said(p, words: str, context: str | None = None) -> int:
    return p.db.post("in", words, chat=None, channel="web", kind="user", provenance="web-session", context=context)


def test_a_text_cite_of_another_logged_user_message_is_kept_beside_the_source(env):
    """Text cites user message A, `source` names user message B, both logged user messages: the change
    applies citing both (in or out of a user turn, and with agent mode's relayer source, which the earlier
    foreign-cite swap skipped and refused), the turn's notes and CHARTER_HISTORY name the repair, and no
    charter_retry is raised. A resend of that change in a later turn lands the same way."""
    from ttp import relay
    from ttp import coordinator as coord
    for k, form in enumerate(("plain", "user turn", "agent relayer", "resend")):
        p = make(env)
        p.charter_path.write_text(_CITE_CHARTER)
        if form == "agent relayer":
            p.set_config(relay.AGENT_KEY, True)
        a = _user_said(p, "Keep the changelog in English.")
        b = _user_said(p, "Also keep release notes short.")
        src = {"msg": b, "quote": "keep release notes short", **({"origin": "relayer"} if "agent" in form else {})}
        kw = {"user_turn": True, "messages": [b]} if form == "user turn" else {}
        err, line = _cite_change(p, a, src, 2 + k, **kw)
        if form == "resend":   # as if refused before: the resend in a later turn lands
            p.charter_path.write_text(_CITE_CHARTER)
            err, line = _cite_change(p, a, src, 10 + k)
        assert err == [], (form, err)
        assert f"#{a}" in line and line.endswith(f" (user msg #{b})"), (form, line)
        assert sum(f"#{n}" in line for n in (a, b)) == 2 and _CITE_CHARTER.split("\n")[3] in p.charter_path.read_text()
        notes = p.db.kv(coord.NOTES_KEY, []) or []
        assert any("charter_update repaired" in n and f"#{a}" in n for n in notes), (form, notes)
        hist = (p.harness / coord.CHARTER_HISTORY).read_text()
        assert f"cites repaired: kept the cite of logged user message #{a}" in hist, (form, hist)
        assert not p.db.q("SELECT 1 FROM events WHERE kind='charter_retry'"), form


def test_a_text_cite_no_logged_user_message_backs_is_swapped_for_the_source(env):
    """A cite of an id not in the log, a message with only a relayer's context, a worker note, the
    coordinator's own line, or the owner's agent's finding with agent mode off is never kept as the user's:
    the source's cite replaces it (or, beside a kept one, it is dropped). A Brief or gate source still
    refuses such a cite, as before. With agent mode on, the owner's agent's finding counts as a user cite."""
    from ttp import relay
    p = make(env)
    p.charter_path.write_text(_CITE_CHARTER)
    b = _user_said(p, "Also keep release notes short.")
    src = {"msg": b, "quote": "keep release notes short"}
    ctx = _user_said(p, "", context="relayer: they asked about the changelog language")
    note = p.db.post("in", "worker: keep the changelog in English", chat=None, kind="note", provenance="worker")
    out = p.db.post("out", "Keep the changelog in English?", chat="web", kind="reply")
    finding = p.db.post("in", "", chat="cli", kind="finding", provenance="cli-legacy",
                        context="Order: write the changelog in plain words")
    for k, cited in enumerate((99999, ctx, note, out, finding)):
        p.charter_path.write_text(_CITE_CHARTER)
        err, line = _cite_change(p, cited, src, 2 + k)
        assert err == [] and f"#{cited}" not in line and line.endswith(f" (user msg #{b})"), (cited, err, line)
        p.charter_path.write_text(_CITE_CHARTER)
        err, line = _cite_change(p, cited, {"origin": "gate"}, 20 + k)
        assert len(err) == 1 and "which `source` does not name" in err[0] and not line, (cited, err)
    # Agent mode on: the owner's agent's finding is the owner's word, so its cite stays beside the source's.
    p.set_config(relay.AGENT_KEY, True)
    p.charter_path.write_text(_CITE_CHARTER)
    err, line = _cite_change(p, finding, src, 40)
    assert err == [] and f"#{finding}" in line and line.endswith(f" (user msg #{b})"), (err, line)
    # A relayer's context alone still is not, nor a note.
    for k, cited in enumerate((note, out)):
        p.charter_path.write_text(_CITE_CHARTER)
        err, line = _cite_change(p, cited, src, 41 + k)
        assert err == [] and f"#{cited}" not in line, (cited, line)
    # Beside a kept cite, an unbacked one in the same run is dropped and the logged one stays.
    p.set_config(relay.AGENT_KEY, False)
    a = _user_said(p, "Keep the changelog in English.")
    p.charter_path.write_text(_CITE_CHARTER)
    err, line = _cite_change(p, f"{a} and #99999", src, 50)
    assert err == [] and f"#{a}" in line and "99999" not in line and line.endswith(f" (user msg #{b})"), (err, line)
    # A gate source with a cite of a logged user message keeps both.
    p.charter_path.write_text(_CITE_CHARTER)
    err, line = _cite_change(p, a, {"origin": "gate"}, 51)
    assert err == [] and f"#{a}" in line and line.endswith(" (origin: harness gate)"), (err, line)


def test_a_kept_text_cite_is_not_the_users_word_for_a_loosening(env):
    """A loosening rests only on the source quote's checked words: a kept cite of another user message
    that would cover the lift does not make it pass."""
    import json
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_SCOPED_BANS)
    _aid, yid = _ask_and_yes(p, _YES_ASK)   # a yes that would cover the lift
    other = _user_said(p, "Keep release notes short.")
    act = {"type": "charter_update", "section": "Restrictions", "text": f"{_YES_EXC} (user msg #{yid})",
           "source": json.dumps({"msg": other, "quote": "keep release notes short"})}
    err = coord.apply(p, [act], turn=3)
    assert len(err) == 1 and "contradicts the Restrictions item" in err[0], err
    assert p.charter_path.read_text() == _SCOPED_BANS

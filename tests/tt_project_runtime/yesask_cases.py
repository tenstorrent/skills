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
    fix and why the harness could not repair it: a host the yes's ask never named that a power cycle may
    disturb, a general lift, and a hedged yes ("yes, but not today") or no ask at all. (A host another item
    bans with no person to guard gets the exception beside that ban too: chartercontra_cases.)"""
    import json
    p = make(env)
    from ttp import coordinator as coord
    for k, (text, yes, ask, why) in enumerate((
            ("Workers may power cycle host-a and host-b with `cycle_test.sh`.", None, True,
             "do not name host-b"),
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


_PREEMPT_BANS = ("# demo\n\n## Restrictions\n"
              "- Never preempt a running soak. (user msg #1)\n"
              "- Never open pull requests. (user msg #1)\n")


def _agent_order(p, words: str) -> int:
    """The owner's agent's order, relayed as a findings-only message with agent mode on: its id."""
    from ttp import relay
    p.set_config(relay.AGENT_KEY, True)
    return p.db.post("in", "", chat="cli", kind="finding", provenance="cli-legacy", context=f"Order: {words}")


def test_agent_mode_a_relayed_yes_naming_an_ask_lands_its_scoped_exception_on_the_first_try(env):
    """(a) Agent mode: the owner's agent relays only 'yes to ask N'. The ask it names by id supplies the
    scope the yes approves, so the lift on the project's own box goes in as a scoped exception at once,
    with no re-ask and no `excepts` form fix needed."""
    import json
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_SCOPED_BANS)
    aid = p.db.post("out", _YES_ASK, chat="web", kind="ask")
    fid = _agent_order(p, f"yes to ask {aid}, go ahead")
    act = {"type": "charter_update", "section": "Restrictions", "text": _YES_EXC,
           "source": json.dumps({"msg": fid, "origin": "relayer", "quote": f"yes to ask {aid}"})}
    assert coord.apply(p, [act], turn=1, user_turn=False) == []
    charter = p.charter_path.read_text()
    assert "Exception to \"Never power cycle a serving host.\"" in charter and _YES_EXC in charter, charter
    # A refusal in the same words is no yes: nothing lands.
    p.charter_path.write_text(_SCOPED_BANS)
    fid = _agent_order(p, f"no, not ask {aid}")
    act["source"] = json.dumps({"msg": fid, "origin": "relayer", "quote": f"no, not ask {aid}"})
    assert coord.apply(p, [act], turn=2, user_turn=False)
    assert _YES_EXC not in p.charter_path.read_text()


def test_agent_mode_preempting_the_soak_on_our_own_box_passes(env):
    """(b) The owner's agent orders a soak preempted on a box the project's own config allows: the ban
    on preempting a running soak gets a scoped exception there, first try."""
    import json
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(_PREEMPT_BANS)
    cfg = json.loads(p.config_path.read_text()) if p.config_path.exists() else {}
    cfg.setdefault("machines", {})["allowed"] = ["box-q"]
    p.config_path.write_text(json.dumps(cfg))
    order = "preempt the soak on our own box box-q"
    fid = _agent_order(p, order)
    act = {"type": "charter_update", "section": "Restrictions", "excepts": "Never preempt a running soak.",
           "text": "Workers may preempt the soak on box-q.",
           "source": json.dumps({"msg": fid, "origin": "relayer", "quote": order})}
    assert coord.apply(p, [act], turn=1, user_turn=False) == []
    assert "Exception to \"Never preempt a running soak.\"" in p.charter_path.read_text()


def test_agent_mode_the_same_order_aimed_at_another_persons_machine_or_job_is_still_refused(env):
    """(c) The same words aimed at another user's job or another team's box still refuse on the agent's
    word: harm to other people needs the user's own words."""
    import json
    from ttp import coordinator as coord
    p = make(env)
    for order in ("preempt another user's soak on box-q", "preempt the soak on another team's box box-z"):
        p.charter_path.write_text(_PREEMPT_BANS)
        fid = _agent_order(p, order)
        text = "Workers may " + order.replace("preempt", "preempt", 1) + "."
        act = {"type": "charter_update", "section": "Restrictions", "excepts": "Never preempt a running soak.",
               "text": text, "source": json.dumps({"msg": fid, "origin": "relayer", "quote": order})}
        err = coord.apply(p, [act], turn=1, user_turn=False)
        assert err, order
        assert text not in p.charter_path.read_text(), order


def test_a_refused_rule_change_never_holds_the_ordered_work(env, tmp_path, capsys):
    """(d) Work never parks on the charter: a task_add start_when that waits on it is dropped and the task
    queued now, a block on it is refused, a waiting hand-off's retry_when is dropped with a
    handoff_probe_refused event, and the charter_retry the coordinator gets tells it to queue the
    ordered work now and never re-ask the order."""
    import json
    import pytest
    from ttp import coordinator as coord, waitheal
    from ttp.daemon import Daemon
    p = make(env)
    for probe in ("grep -q 'box-q' tt-project/harness/CHARTER.md || exit 1", "ttp charter | grep -q box-q",
                  "test -n \"$(ttp memory | grep box-q)\""):
        assert waitheal.charter_wait(probe), probe
        with pytest.raises(ValueError, match="never holds ordered work"):
            coord.check_probe(probe, "waits_on when")
    out = coord.apply(p, [{"type": "task_add", "title": "preempt soak", "spec": "s", "why": "the exception lands",
                           "start_when": "grep -q 'box-q' tt-project/harness/CHARTER.md || exit 1"}])
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert out == [] and any("waited on the charter" in n and "was dropped" in n for n in notes), (out, notes)
    t = p.db.one("SELECT status, labels FROM tasks WHERE title='preempt soak'")
    assert t and t["status"] == "queued" and "CHARTER" not in (t["labels"] or ""), t
    assert waitheal.charter_wait("test -e out/job.rc") == ""
    from ttp import cli
    with pytest.raises(SystemExit):
        cli.main(["task", "demo", "add", "preempt the soak too", "--start-when",
                  "grep -q box-q harness/CHARTER.md || exit 1", "--why", "the exception lands"])
    seen = capsys.readouterr()
    assert "never holds ordered work" in seen.out + seen.err, (seen.out, seen.err)
    assert not p.db.one("SELECT id FROM tasks WHERE title='preempt the soak too'")
    d = Daemon(p.base)
    tid = p.db.add_task("preempt the soak", "s", kind="work", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="the charter exception",
                             retry_when="grep -q box-q tt-project/harness/CHARTER.md || exit 1")
    res = json.loads(t["result"])
    assert "retry_when" not in res and "CHARTER.md" in res["retry_when_refused"], res
    ev = p.db.one("SELECT text FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))
    assert ev and "waits on the charter" in ev["text"], ev
    p.charter_path.write_text(_PREEMPT_BANS)
    mid = p.db.post("in", "preempt the soak on box-q", chat="web", channel="web", kind="user",
                    provenance="web-session")
    act = {"type": "charter_update", "section": "Restrictions", "replaces": "Restrictions",
           "text": f"- Preempt soaks on box-q freely. (user msg #{mid})"}
    msg = coord._charter_retry_now(p, act, "needs the user's word", "t1")
    assert "queue the work that order covers now" in msg and "never to re-ask an order" in msg, msg


def test_a_dropped_charter_wait_names_the_charter_update_only_when_the_turn_sent_a_refused_one(env):
    """cells: charter-wait-note. A start_when on the charter is dropped either way; its note asks for the
    charter_update to be resent fixed only in a turn whose charter_update was refused, never in a turn that
    sent none or one that applied."""
    from ttp import coordinator as coord
    p = make(env)
    probe = "grep -q 'box-q' tt-project/harness/CHARTER.md || exit 1"

    def note(actions):
        coord.apply(p, actions)
        return [n for n in p.db.kv(coord.NOTES_KEY, []) if "waited on the charter" in n][-1]
    alone = note([{"type": "task_add", "title": "soak a", "spec": "s", "why": "w", "start_when": probe}])
    assert "was dropped" in alone and "charter_update" not in alone, alone
    applied = note([{"type": "charter_update", "section": "Goals", "text": "Keep the docs short."},
                    {"type": "task_add", "title": "soak b", "spec": "s", "why": "w", "start_when": probe}])
    assert "charter_update" not in applied, applied
    refused = note([{"type": "charter_update", "section": "Goals", "text": ""},   # no text: refused
                    {"type": "task_add", "title": "soak d", "spec": "s", "why": "w", "start_when": probe}])
    assert "resend the charter_update fixed" in refused, refused
    tid = p.db.add_task("soak c", "s", kind="work", tier="standard")
    upd = note([{"type": "task_update", "id": tid, "start_when": probe, "why": "w"}])
    assert f"#{tid}" in upd and "charter_update" not in upd, upd


def test_refuse_wait_moves_the_probe_aside_and_records_one_event(env):
    """cells: retry-when-helper. waitheal.refuse_wait, the one path of every refused hand-off wait (no shell
    command, the checks.running marker, the charter, an unfiled upstream note, an unset run variable): the
    probe moves to retry_when_refused, one handoff_probe_refused event names the task and why, and the rest
    of the hand-off is kept. Each daemon path's own text is checked by its own end-to-end test."""
    from ttp import waitheal
    p = make(env)
    tid = p.db.add_task("wait on it", "s", kind="work", tier="standard")
    task = p.db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    got = waitheal.refuse_wait(p.db, p, task, {"status": "waiting", "retry_when": "true", "on_pass": "x"},
                               "true", "its probe is refused", "retry_when refused: test")
    assert got == {"status": "waiting", "on_pass": "x", "retry_when_refused": "true"}, got
    ev = p.db.q("SELECT text, status FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))
    assert [e["text"] for e in ev] == [f"#{tid} wait on it: its probe is refused"] and ev[0]["status"] == "queued"
    from ttp.coordinator import EVENT_CHARS
    task["title"] = "t" * (EVENT_CHARS + 500)   # a long title: the event text keeps its cap
    waitheal.refuse_wait(p.db, p, task, {"retry_when": "true"}, "true", "its probe is refused", "x")
    ev = p.db.q("SELECT text FROM events WHERE kind='handoff_probe_refused' AND task=? ORDER BY id", (tid,))
    assert len(ev) == 2 and len(ev[1]["text"]) == EVENT_CHARS, len(ev[1]["text"])


def test_a_charter_update_with_wrong_typed_resource_settings_neither_crashes_nor_adopts_letters(env):
    """A hand-edited config whose `machines` or `shared_resources` is a plain string (or whose
    machines.allowed is one) neither crashes the charter check nor turns the string's letters into
    own resource names: the charter check reads machine_ledger's guarded lookup (configured_names)."""
    import json
    from ttp import coordinator as coord, machine_ledger
    p = make(env)
    # direct: the guards hold for every wrong-typed shape
    assert machine_ledger.configured_names({"machines": "box-q", "shared_resources": "abc", "resources": "xyz"}) == []
    assert machine_ledger.configured_names({"machines": {"allowed": "b-x"}}) == []
    # through a charter_update: a scoped exception on the order's own words still runs to its end
    cfg = json.loads(p.config_path.read_text()) if p.config_path.exists() else {}
    cfg["machines"] = "box-q"          # wrong type: a hand edit
    cfg["shared_resources"] = "box-r-device"
    p.config_path.write_text(json.dumps(cfg))
    p.charter_path.write_text(_PREEMPT_BANS)
    order = "preempt the soak on our own box box-q"
    fid = _agent_order(p, order)
    act = {"type": "charter_update", "section": "Restrictions", "excepts": "Never preempt a running soak.",
           "text": "Workers may preempt the soak on box-q.",
           "source": json.dumps({"msg": fid, "origin": "relayer", "quote": order})}
    err = coord.apply(p, [act], turn=1, user_turn=False)   # a wrong-typed config never crashes it
    assert err == [], err
    assert "Exception to \"Never preempt a running soak.\"" in p.charter_path.read_text()


def test_agent_mode_a_pr_leaving_draft_still_needs_the_users_own_yes(env):
    """(e) The owner's agent's yes is the user's word for charter changes, never for a PR leaving draft:
    pr_approve on its relayed words is refused and records no approval."""
    from ttp import coordinator as coord, prguard
    p = make(env)
    url = "https://github.com/acme/widgets/pull/7"
    _heads_seen(p, "acme/widgets#7")
    fid = _agent_order(p, f"yes, mark {url} ready")
    out = " ".join(coord.apply(p, [{"type": "pr_approve", "id": fid, "text": url, "quote": "ready"}]))
    assert "not a message from the user" in out or "does not count as the user's approval" in out, out
    assert not prguard.approved(p.db, "acme/widgets#7")

# A mention of a pending ask, request, draft or command carries its full text
# (runtime/ttp/quotefull.py: coordinator reply/notify refusal, ttp urgent-reply) — this file runs in
# tests/test_tt_project_runtime.py's namespace (see _include_cases there).
from __future__ import annotations

_MISS = "Apologies to <name> and <name>: drafts in my earlier messages, ready to send."
_DRAFTS = ("\n\n> Draft for <name>: the release notes in full, ready to send.\n"
           "> Draft for <other name>: the reply to the vendor in full, ready to send.")


def _sent(p, kind="reply"):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND kind=? ORDER BY id",
                                      (kind,))]


def test_todays_miss_is_refused_and_with_the_drafts_quoted_it_passes(env):
    from ttp import coordinator as coord
    p = make(env)
    problems = coord.apply(p, [{"type": "reply", "chat": "c1", "text": _MISS}], turn=1)
    assert len(problems) == 1 and "reply rejected" in problems[0] and "earlier messages" in problems[0], problems
    assert "never point to earlier messages" in problems[0] and _sent(p) == [], problems
    problems = coord.apply(p, [{"type": "reply", "chat": "c1", "text": _MISS + _DRAFTS}], turn=2)
    assert problems == [] and _sent(p) and "Draft for <name>" in _sent(p)[0], problems


def test_an_ask_named_by_id_is_expanded_not_refused(env):
    from ttp import coordinator as coord
    p = make(env)
    ask = p.db.post("out", "Reboot the board? Options: A) now, B) after the soak. My recommendation: B",
                    chat=None, kind="ask", severity="high")
    p.db.x("UPDATE messages SET ref='blocking:restriction' WHERE id=?", (ask,))
    problems = coord.apply(p, [{"type": "reply", "chat": "c1", "text": f"Ask {ask} is still open."}], turn=1)
    (stored,) = _sent(p)
    assert problems == [] and stored.startswith(f"Ask {ask} is still open.") and "My recommendation: B" in stored
    problems = coord.apply(p, [{"type": "notify", "severity": "normal",
                                "text": "The draft I sent is still pending."}], turn=2)
    assert len(problems) == 1 and "notify rejected" in problems[0], problems
    assert len(_sent(p)) == 1 and not _sent(p, "alert")


def test_a_plain_status_reply_passes(env):
    from ttp import coordinator as coord
    p = make(env)
    problems = coord.apply(p, [{"type": "reply", "chat": "c1",
                                "text": "All three tasks landed; budget at 40% of the daily cap."}], turn=1)
    assert problems == [] and len(_sent(p)) == 1


def test_backrefs_finds_each_back_reference_once():
    from ttp import quotefull
    assert quotefull.backrefs(_MISS) == ["the drafts named as pending, without its text", "in my earlier messages"]
    assert quotefull.backrefs("See above for the request.") == ["See above"]
    assert quotefull.backrefs("As drafted earlier, it went out.") == ["As drafted"]
    assert quotefull.backrefs("The draft I sent is ready to send.") == ["The draft I sent",
                                                                        "the draft named as pending, without its text"]
    assert quotefull.backrefs("Two requests are pending your review.") != []
    assert quotefull.backrefs("Task #12 landed. The request went out and was answered.") == []
    why = quotefull.refused(_MISS, "reply")
    assert "quote the draft/ask/request/command in full" in why and "never point to earlier" in why


def test_a_plain_line_or_list_item_after_the_phrase_is_not_its_text_and_plain_status_passes():
    from ttp import quotefull
    assert quotefull.backrefs(_MISS + "\nBudget at 40%.") != []
    assert quotefull.backrefs("Open items:\n- drafts in my earlier messages, ready to send\n- task #12 running") != []
    assert quotefull.backrefs("Open items:\n- the draft for <name>, ready to send\n- task #12 running") != []
    assert quotefull.backrefs("Draft for <name>, ready to send:\nHi <name>, the update follows.\n[Authored by AI]") == []
    assert quotefull.backrefs("A line that points back ('drafts in my earlier messages', 'see above') is refused.") == []
    assert quotefull.backrefs("Here's where we are: drafts in my earlier messages aren't sent yet.") != []
    assert quotefull.backrefs(_MISS + " Ask 12 is still open.\n\nStill open, in full:\n- ask 12: Reboot now?") != []
    assert quotefull.backrefs("No rush, the drafts for <name> and <name> are ready to send.") != []
    assert quotefull.backrefs("The draft for <name>, not sent yet, is ready to send.") != []
    for plain in ("No asks are still open.", "0 asks, 0 drafts still open.", "No asks, requests, or drafts are still open.", "Neither draft is ready to send.", "Task #14 is still open and has two questions from the vendor.",
                  "Bug #3 is still open, and the request to the vendor went out.", "I am ready to send the reminder at 9.",
                  "Drafts ready to send: none.", "The command finished. The PR is pending review.",
                  "I will ask the vendor. Task #14 is pending a worker."):
        assert quotefull.backrefs(plain) == [], plain


def test_a_reply_in_numbered_parts_passes_when_a_later_part_carries_the_drafts(env):
    from ttp import coordinator as coord
    p = make(env)
    problems = coord.apply(p, [{"type": "reply", "chat": "c1", "text": "(1/2) Two drafts, ready to send:"},
                               {"type": "reply", "chat": "c1", "text": "(2/2)" + _DRAFTS}], turn=1)
    assert problems == [] and len(_sent(p)) == 2, problems


def test_urgent_reply_refuses_a_back_reference(env, monkeypatch, capsys):
    from ttp import cli, coordtools, msgurgent
    p = make(env)
    monkeypatch.chdir(p.root)
    monkeypatch.setenv(coordtools.ROLE_ENV, "coordinator")
    now = time.time()
    p.db.post("in", "urgent: the endpoint is down", chat="c1", provenance="cli-legacy")
    msgurgent.tick(p, p.config(), now=now + 1)
    with pytest.raises(SystemExit):
        cli.main(["urgent-reply", "c1", _MISS])
    assert "urgent reply rejected" in capsys.readouterr().err and _sent(p) == []
    cli.main(["urgent-reply", "c1", "Found it: the encoder died; restarting it now."])
    assert "restarting it now" in _sent(p)[-1]


def test_an_ask_rethink_event_carries_the_whole_question():
    from ttp import askrethink
    question = "Reboot the board?\nOptions: A) now, B) after the soak. " + "Detail. " * 60 + "My recommendation: B"
    assert "My recommendation: B" in askrethink.text(7, [1], 3.0, question, "human")

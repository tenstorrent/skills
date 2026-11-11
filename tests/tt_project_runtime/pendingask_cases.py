# A reminder of what waits on the user quotes it in full (runtime/ttp/pendingask.py, user msg #2424) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

# Over 300 chars, so a clip to the old preview length would cut it.
_PA_LONG = ("Reboot the board's controller? The exact command is: <the exact command> --power cycle --target <board>. "
            "Options: A) run it now, B) wait until the soak ends, C) skip the reboot and drain the board instead. "
            "It stops the two jobs that run there now, so it waits on your word."
            "\n\nMy recommendation: B\n\nNon-blocking: in 30 min I act on my recommendation.")


def _pa_ask(p, text=_PA_LONG):
    return p.db.post("out", text, chat=None, kind="ask", severity="high")


def _pa_sent(p, kind):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND kind=? ORDER BY id", (kind,))]


def test_bare_ask_ref_expanded():
    from ttp import pendingask
    asks = [{"id": 297, "text": _PA_LONG}, {"id": 12, "text": "Vendor A or B?\n\nMy recommendation: A"}]
    got, notes = pendingask.expand("Still waiting on you for ask 297.", asks)
    assert got.startswith("Still waiting on you for ask 297.") and pendingask.HEADER in got, got
    assert got.endswith("- " + pendingask.full(asks[0])) and "<the exact command> --power cycle" in got, got
    assert "Vendor A" not in got and any("named ask 297 without its text" in n for n in notes), notes
    # 'asks 297 and #12' names both; each is appended once.
    got, _ = pendingask.expand("Asks 297 and #12 are open.", asks)
    assert got.count("- ask 297: ") == 1 and got.count("- ask 12: Vendor A or B? My recommendation: A") == 1, got
    # A text that already quotes the ask is sent as written, with no note.
    said = "ask 297: " + _PA_LONG
    assert pendingask.expand(said, asks) == (said, [])
    # The only open ask is what a bare 'waiting on you' means.
    got, _ = pendingask.expand("The reboot is waiting on you.", asks[:1])
    assert got.endswith(pendingask.full(asks[0])), got
    # Nothing about the user and no ask id: unchanged and quiet.
    assert pendingask.expand("Task #297 landed.", asks) == ("Task #297 landed.", [])


def test_unmatched_ref_flagged_not_dropped():
    from ttp import pendingask
    asks = [{"id": 1, "text": "Vendor A or B?"}, {"id": 2, "text": "Rename the cache folder?"}]
    for text in ("The rollout still waits on your yes.", "Still blocked on you: ask 40."):
        got, notes = pendingask.expand(text, asks)
        assert got == text, got
        (note,) = notes
        assert note.startswith("pendingask: your reply says the user still owes something") and "went as written" in note
    assert "(ask 40 is not open)" in pendingask.expand("Still blocked on you: ask 40.", asks)[1][0]


def test_reminder_quotes_open_ask_in_full(env):
    from ttp import coordinator as coord
    p = make(env)
    ask = _pa_ask(p)
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": f"Still waiting on you for ask {ask}."}], turn=1)
    (got,) = _pa_sent(p, "reply")
    assert got.startswith(f"Still waiting on you for ask {ask}.") and " ".join(_PA_LONG.split()) in got, got
    assert any(n.startswith("pendingask: your reply named ask") for n in p.db.kv(coord.NOTES_KEY, []))
    coord.apply(p, [{"type": "notify", "severity": "normal", "text": f"Ask {ask} is still open."}], turn=2)
    assert " ".join(_PA_LONG.split()) in _pa_sent(p, "alert")[-1]
    # Unmatched: sent as written, flagged for the next turn, never dropped.
    _pa_ask(p, "Rename the cache folder?")
    coord.apply(p, [{"type": "reply", "chat": "c1", "text": "The rollout still waits on your yes."}], turn=3)
    assert _pa_sent(p, "reply")[-1] == "The rollout still waits on your yes."
    assert any("names no open ask it could match" in n for n in p.db.kv(coord.NOTES_KEY, []))


def test_status_waiting_on_you_full_text(env):
    from ttp import audit, overview
    from ttp.cli import status_text
    from ttp.web import state_payload
    p = make(env)
    asks = [_pa_ask(p, f"Question {n}: pick a route?") for n in range(7)] + [_pa_ask(p)]
    tid = p.db.add_task("reboot the board", "s", origin="user")
    p.db.update_task(tid, status="blocked", blocked_reason="held", labels=["kept", f"waits:ask:{asks[-1]}"])
    flat = " ".join(_PA_LONG.split())
    out = status_text(p)
    # Every open ask in full, none capped away, and the task waiting on one carries its text too.
    assert f"(ask {asks[-1]}, " in out and flat in out and all(f"Question {n}: pick a route?" in out for n in range(7)), out
    assert f"reboot the board — waits on you: ask {asks[-1]}: {flat}" in out, out
    w = next(t for t in state_payload(p, p.db)["tasks"] if t["id"] == tid)["wait"]
    assert w["text"] == f"waits on you: ask {asks[-1]}: {flat}", w
    # The overview line lists each open ask in full.
    from ttp.release import runtime_version
    row = overview.project_status(p.name, {"host": "testhost", "dir": str(p.root)}, time.time(), "testhost",
                                  runtime_version(p.harness / "runtime"), (time.time() - 86400, time.time()))
    assert f"\n  needs you: ask {asks[-1]}: {flat}" in overview.line(row), overview.line(row)
    # The daily review quotes an unanswered ask whole, its recommendation included.
    review = "\n".join(audit.lines(p.db, p=p))
    assert f"ask {asks[-1]} (" in review and flat in review, review


def test_skill_rule_present():
    plugin = RUNTIME.parent
    skill = (plugin / "skills" / "tt-project" / "SKILL.md").read_text()
    assert 'quote it in full right there' in skill and 'never only "ask 297"' in skill
    role = (plugin / "template" / "prompts" / "coordinator.md").read_text()
    assert "an open one is quoted in full (options, command), never just its id" in role
    guide = (plugin / "docs" / "guide.md").read_text()
    assert "An open ask is shown in full wherever it waits on the user (user msg #2424)" in guide

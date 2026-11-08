# The user's latest words on a topic (userwords.py, `ttp said`): the digest shows them for blocked
# items, and the ask gate refuses an ask for leave the user's request already gave. Run from
# tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports come
# from that file, and pytest collects these tests under it.
from __future__ import annotations

_OLD_RULE = "Never touch the nightly report watcher on box-a1; it is fragile."
_NEW_WORD = "Fix the stale nightly report watcher on box-a1 yourself, it keeps missing reports."
_RE_ASK = "Should I fix the stale nightly report watcher on box-a1?"


def _ttp_said(*args):
    """`ttp said ...`: its exit code, 0 when it returns."""
    from ttp import cli
    try:
        cli.main(["said", *args])
    except SystemExit as e:
        return e.code or 0
    return 0


def _said_fixture(p):
    """An old rule, a newer contrary request, and noise around them."""
    db = p.db
    db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    old = db.post("in", _OLD_RULE, chat=None, kind="user", provenance="web-session")
    db.x("UPDATE messages SET ts=ts-20*86400 WHERE id=?", (old,))
    db.post("in", "Make the release notes shorter.", chat=None, kind="user", provenance="web-session")
    new = db.post("in", _NEW_WORD, chat=None, kind="user", provenance="web-session")
    db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    return old, new


def test_the_digest_shows_the_users_newest_words_on_a_blocked_task_and_nothing_without_a_match(env):
    from ttp import coordinator as coord, userwords
    p = make(env)
    base = coord.digest(p, {}, [], [])
    assert "latest words" not in base
    old, new = _said_fixture(p)
    # No blocker: the digest does not grow.
    assert len(coord.digest(p, {}, [], [])) == len(base)
    tid = p.db.add_task("Fix the nightly report watcher on box-a1", "s", origin="user")
    p.db.update_task(tid, status="blocked", blocked_reason="an older rule says never touch the watcher")
    d = coord.digest(p, {}, [], [])
    lines = [x for x in d.splitlines() if "User's latest words on this" in x]
    assert lines[0].startswith(f"- task #{tid}: User's latest words on this: #{new} ") and _NEW_WORD in lines[0], d
    assert any(f"#{old} " in x for x in lines[1:]) and len(lines) <= userwords.PER_ITEM, lines
    assert userwords.HEADER in d and "Make the release notes" not in d
    # A blocked task on another subject adds nothing.
    p.db.update_task(tid, status="queued", blocked_reason=None)
    other = p.db.add_task("Rotate the build cache keys", "s", origin="user")
    p.db.update_task(other, status="blocked", blocked_reason="needs a credential")
    assert "latest words" not in coord.digest(p, {}, [], [])
    # This turn's new message is shown whole under NEW EVENTS, never twice.
    p.db.update_task(tid, status="blocked", blocked_reason="r")
    d = coord.digest(p, {}, [], [new])
    assert f"latest words on this: #{new} " not in d and f"latest words on this: #{old} " in d
    # Unchanged since the last turn: one line.
    parts, seen = coord.digest_parts(p, {}, [], [])
    again = dict(coord.digest_parts(p, {}, [], [], seen=seen)[0])
    assert again["user_words"].startswith("## The user's latest words on blocked items: ") and "\n" not in again["user_words"]


def test_an_open_ask_or_refused_action_gets_the_users_latest_words_at_most_three(env):
    from ttp import coordinator as coord
    p = make(env)
    ids = [p.db.post("in", f"Keep the nightly report watcher on box-a1 running, version {i}.", chat=None,
                     kind="user", provenance="web-session") for i in range(5)]
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    ask = p.db.post("out", "May I stop the nightly report watcher on box-a1?", chat=None, kind="ask")
    d = coord.digest(p, {}, [], [])
    got = [x for x in d.splitlines() if x.startswith(f"- ask #{ask}: User's latest words")]
    assert [x.split("this: #")[1].split()[0] for x in got] == [str(i) for i in ids[::-1][:3]], got
    p.db.x("UPDATE messages SET handled=1 WHERE kind='ask'")
    p.db.set_kv(coord.REJECTED_KEY, ["task_add rejected: the restriction says not to change the nightly report "
                                     "watcher on box-a1"])
    assert sum(x.startswith("- refused action: User's latest") for x in coord.digest(p, {}, [], []).splitlines()) == 3


def test_contraction_fragments_match_no_unrelated_user_words(env):
    # don't, it's, I'll, shouldn't leave fragments (don, s, ll, t) that are no shared topic words.
    from ttp import coordinator as coord, userwords
    p = make(env)
    for text in ("If I don't answer within the hour, it's your call and you'll decide; don't wait for me.",
                 "A guard meant to catch loops shouldn't hold the queue; it's wrong, and they'd drop it.",
                 "Then tell the other project's coordinator it isn't done and don't let it's watcher stop."):
        p.db.post("in", text, chat=None, kind="user", provenance="web-session")
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    ask = ("Cursor sign-in for the live measurement: if you don't use it, tell me and I'll drop it. "
           "It's the last step, and it isn't something I can do.")
    p.db.post("out", ask, chat=None, kind="ask")
    rows = [(r["id"], r["ts"], r["text"], r["context"]) for r in p.db.q(userwords.USER_SQL, (0, userwords.SCAN))]
    assert len(rows) == 3 and userwords.on_topic(None, ask, rows=rows) == []
    assert "latest words" not in coord.digest(p, {}, [], [])


def test_a_short_sentence_sharing_only_generic_words_matches_no_topic(env):
    # Like the live case: a short sentence about alternative machines and accounts must not
    # match a provider-sign-in ask on the stems {account, machin, other} alone. Synthetic
    # text; never real user messages.
    from ttp import coordinator as coord
    p = make(env)
    p.db.post("in", "Try other machines and other accounts.", chat=None, kind="user", provenance="web-session")
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    ask = "Provider sign-in expired: should the evaluation move to other machines or other accounts?"
    p.db.post("out", ask, chat=None, kind="ask")
    d = coord.digest(p, {}, [], [])
    assert "latest words" not in d, d
    # A sentence that truly shares the topic's words still matches.
    p.db.post("in", "The provider sign-in expired again; keep the evaluation on its accounts after sign-in.",
              chat=None, kind="user", provenance="web-session")
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    d2 = coord.digest(p, {}, [], [])
    lines = [x for x in d2.splitlines() if "User's latest words on this" in x]
    # The matching message's best sentence is quoted; the filler message matches no topic.
    assert lines and lines[0].startswith("- ask #3: ") and "expired again" in lines[0], lines
    assert "Try other machines" not in d2, d2


def test_the_ask_gate_refuses_leave_the_users_newer_request_gave_naming_it(env):
    from ttp import reask
    p = make(env)
    old, new = _said_fixture(p)
    problems, sent = _ask(p, _RE_ASK, **_OWN_ACCESS)
    assert problems == [] and f"already settled by user msg #{new}:" in _flagged(p), problems
    assert "Fix the stale nightly report watcher on box-a1 yourself" in problems[0]
    # Other forms of asking leave: refused too.
    for ask in ("Can we fix the stale nightly report watcher on box-a1?",
                "OK to fix the stale nightly report watcher on box-a1?",
                "Do you want me to fix the stale nightly report watcher on box-a1?"):
        assert reask.settled_by(p, p.config(), ask).startswith(f"user msg #{new}:"), ask
    # A new question, another box, or no ask for leave: not settled by a plain request.
    for ask in ("Should I fix the stale nightly report watcher on box-b2?",
                "Should I fix the stale nightly report watcher on box-a1 with the old or the new cron syntax?"):
        assert reask.settled_by(p, p.config(), ask) is None, ask
    p.db.x("DELETE FROM messages WHERE id=?", (old,))   # its 'never' sentence settles any ask on the watcher
    assert reask.settled_by(p, p.config(), "The stale nightly report watcher on box-a1 needs a token to fix it; "
                                           "please create one.") is None
    # A resend saying what is new still goes out.
    problems, sent = _ask(p, _RE_ASK, **_OWN_ACCESS,
                          not_covered="the watcher now runs on a second host too, which the request did not cover")
    assert problems == [] and sent["text"].startswith(_RE_ASK), problems


def test_ttp_said_lists_the_newest_matching_user_words_read_only(env, capsys, monkeypatch):
    import json as _json
    from ttp import relay, userwords
    p = make(env)
    old, new = _said_fixture(p)
    relayed = p.db.post("in", f"stale watcher on box-a1 is fine\n{relay.MARK}\nthe nightly report watcher fix",
                        chat=None, kind="user", provenance="cli-legacy")
    sysrow = p.db.post("in", "nightly report watcher on box-a1", chat=None, kind="user", provenance="system")
    reply = p.db.post("out", "nightly report watcher on box-a1 fixed", chat=None, kind="reply")
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    assert _ttp_said("nightly", "report", "watcher", "--json") == 0
    got = _json.loads(capsys.readouterr().out)
    assert [m["id"] for m in got] == [new, old], got   # newest first; no context, system row or outbound one
    assert got[0]["quote"] == _NEW_WORD, got[0]
    assert str(got[0]["when"]).strip(), got[0]
    assert {relayed, sysrow, reply}.isdisjoint(m["id"] for m in got)
    assert _ttp_said("box-a1", "stale") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(f"#{relayed} ") and f"#{new} " in out and "found" in out
    assert _ttp_said("release", "unrelated", "-n", "1") == 0
    assert "0 messages found" in capsys.readouterr().out
    # Read-only: a database the process may not write is still read.
    con = userwords.open_ro(p.state / "project.db")
    try:
        assert [m["id"] for m in userwords.search(con, "release notes")] == [new - 1]
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM messages")
    finally:
        con.close()

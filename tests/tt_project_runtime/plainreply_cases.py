# A reply or notify that does not answer first, runs long, or leans on internal terms is flagged
# (runtime/ttp/plainreply.py, coordinator.apply, the digest's Course check, the daily audit). Run from
# test_tt_project_runtime.py.

_JARGON = ("Task 12 holds a slot in the checks lane; the gate re-runs its precheck on the next tick, and the "
           "retry_when probe wakes it after backoff.")
_PLAIN = ("Your tests start in about 20 minutes. The test queue runs 4 at a time and yours is second in line; "
          "I'll tell you when they finish.")
_PREAMBLE = "Here's the status of everything I looked at today:\n" + "- one item that went fine\n" * 60


def test_a_jargon_heavy_reply_is_flagged_once_in_the_next_course_check_and_a_plain_one_is_not(env):
    from ttp import coordinator as coord, plainreply
    p = make(env)
    assert plainreply.why(_PLAIN) == ""
    assert "internal terms" in plainreply.why(_JARGON)
    assert coord.apply(p, [{"type": "reply", "text": _PLAIN, "chat": "c1"}], turn=1) == []
    assert "course_check" not in dict(coord.digest_parts(p, {}, [], [])[0])
    assert coord.apply(p, [{"type": "reply", "text": _JARGON, "chat": "c1"}], turn=2) == []   # flag only
    mid = p.db.one("SELECT id FROM messages WHERE direction='out' AND kind='reply' ORDER BY id DESC")["id"]
    course_check = dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]
    assert course_check.count("reply not plain") == 1 and f"reply #{mid}: internal terms (" in course_check
    assert [r["msg"] for r in plainreply._records(p.db) if r["why"]] == [mid]
    coord.apply(p, [{"type": "reply", "text": "Done.", "chat": "c1"}], turn=3)   # shown: settled
    assert "course_check" not in dict(coord.digest_parts(p, {}, [], [])[0])
    coord.apply(p, [{"type": "notify", "text": _PREAMBLE}], turn=4)   # a notify too: long, no answer first
    assert "with no answer first" in dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]


def test_a_term_followed_by_its_plain_meaning_and_words_inside_others_do_not_count():
    from ttp import plainreply
    assert plainreply.jargon("The lane (the line tests wait in) is full; one slot means one test at a time.") == []
    assert plainreply.jargon("The gateway, the slotted tray and the ticket digester are fine.") == []
    assert plainreply.jargon("Two slots and three ticks.") == ["slot", "tick"]
    # A long reply that answers first passes; a heading, a preamble or a list first does not.
    long_ok = "Yes, the build passed. " + "It ran every test and all of them passed. " * 30
    assert plainreply.why(long_ok) == ""
    for first in ("# Status\n", "- item\n", "Let me explain what happened. ", "Summary of the day:\n"):
        assert "no answer first" in plainreply.why(first + long_ok), first
    assert plainreply.why("Here's the status: all green.") == ""   # short: no length flag
    assert plainreply.why("**Done.** " + long_ok) == ""   # a bold lead still answers


def test_the_soft_limit_term_list_and_threshold_come_from_config_with_defaults(env):
    from ttp import plainreply
    assert plainreply.terms_of({}) == plainreply.TERMS and "start_when" in plainreply.TERMS
    assert plainreply.SOFT_CHARS == 800 and plainreply.MAX_TERMS == 1
    cfg = {"coordinator": {"reply_terms": ["widget"], "reply_max_terms": 0, "reply_soft_chars": 50}}
    assert plainreply.why(_JARGON, cfg) == ""   # its own list replaces the default one
    assert "internal terms (widget)" in plainreply.why("The widget is ready.", cfg)
    assert "no answer first" in plainreply.why("Here's what I found:\n" + "x " * 40, cfg)
    assert plainreply.why(_JARGON, {"coordinator": {"reply_max_terms": 20}}) == ""
    assert plainreply.why(_JARGON, {"coordinator": {"reply_max_terms": "junk"}}) != ""   # a bad value: default
    # The keys are known config keys, so setting them raises no unknown-key alarm.
    from ttp import coordinator as coord
    p = make(env)
    for key, value in (("coordinator.reply_soft_chars", "1200"), ("coordinator.reply_max_terms", "2")):
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": value}]) == [], key
    assert p.config()["coordinator"]["reply_soft_chars"] == 1200


def test_the_daily_review_reports_the_median_reply_length_and_the_flagged_share(env):
    import time
    from ttp import audit, plainreply
    p = make(env)
    now = time.time()
    assert plainreply.audit_line(p.db, now - 86400) == ""
    plainreply.check(p.db, [(1, "reply", _PLAIN), (2, "reply", _JARGON), (3, "notify", "x" * 1000),
                            (4, "reply", "OK.")], now=now)
    line = plainreply.audit_line(p.db, now - 86400)
    med = sorted([len(_PLAIN), len(_JARGON), 1000, 3])
    assert line.startswith(f"replies and notifies to the user: 4, median {(med[1] + med[2]) / 2:,.0f} chars, "
                           "2 flagged (50%)"), line
    assert line in "\n".join(audit.lines(p.db, now + 1))

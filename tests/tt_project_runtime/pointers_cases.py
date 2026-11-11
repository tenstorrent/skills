# A coordinator reply or notify that points to content further on ("follow-up below", "see below",
# "as follows:") and has none is refused, naming the phrase (runtime/ttp/pointers.py, coordinator.apply).
# Run from test_tt_project_runtime.py.


def _out(p):
    return [r["text"] for r in p.db.q("SELECT text FROM messages WHERE direction='out' ORDER BY id")]


def test_a_reply_pointing_below_with_nothing_after_it_is_refused_naming_the_phrase(env):
    from ttp import coordinator as coord
    p = make(env)
    for text, phrase in (("Three tasks are open; follow-up below.", "follow-up below"),
                         ("The build is green.\nSee below", "See below"),
                         ("Two checks failed, details below:", "details below"),
                         ("Plan stands; the steps are as follows:", "as follows"),
                         ("Next I'll do the following tasks:\n[authored by AI]", "the following tasks:")):
        problems = coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}], turn=1)
        assert len(problems) == 1 and "reply rejected" in problems[0] and f"'{phrase}'" in problems[0], (text, problems)
    problems = coord.apply(p, [{"type": "notify", "text": "Box is down. Details below."}], turn=2)
    assert len(problems) == 1 and "notify rejected" in problems[0] and "'Details below'" in problems[0]
    assert _out(p) == []


def test_a_pointer_as_the_last_line_is_refused_and_a_bare_trailing_colon_is_not():
    from ttp import pointers
    assert pointers.dangling("All three landed.\n\nFollow-up below") == ["Follow-up below"]
    assert pointers.dangling("Done.\nMore on the cost below:\n\n") == ["below"]
    assert pointers.dangling("Here is what is left:") == []   # no pointer phrase: not refused
    assert pointers.dangling("ack: Project created. Brief:") == []
    assert pointers.dangling("Follow-up below. Status is green.") == ["Follow-up below"]   # same line: no section
    assert pointers.dangling("follow-up below, and again: follow-up below.") == ["follow-up below"]   # once


def test_a_pointer_answered_by_a_list_heading_paragraph_or_colon_text_passes(env):
    from ttp import coordinator as coord, pointers
    p = make(env)
    ok = ("Follow-up below.\n1. #12 lands today\n2. #13 waits on review",
          "Two things are open, follow-up below.\n\nFollow-up:\n- #12 lands today",
          "Details below.\n\n## Details\nThe cache was stale.",
          "See below.\n\nThe build failed at step 3 because the cache was stale.",
          "Follow-up below: #12 lands today.",
          "It works as follows: the queue runs four at a time.",
          "The steps are as follows:\n- build\n- test")
    for text in ok:
        assert pointers.dangling(text) == [], text
    assert coord.apply(p, [{"type": "reply", "text": ok[0], "chat": "c1"}, {"type": "notify", "text": ok[1]}],
                       turn=1) == []
    assert _out(p) == [ok[0], ok[1]]


def test_below_in_other_senses_is_no_pointer():
    from ttp import pointers
    for text in ("Spend is below the cap.", "Disk use dropped below 30 GB on the box.",
                 "Free space is below 30 GB", "The queue stays below its limit.", "Latency is below target",
                 "Results are below average.", "Temperatures below zero are fine.", "The following week is free.",
                 "It follows the plan."):
        assert pointers.dangling(text) == [], text


def test_a_reply_sent_as_numbered_parts_is_checked_as_a_whole(env):
    from ttp import coordinator as coord, pointers
    p = make(env)
    one, two = "(1/2) Three tasks are open; follow-up below.", "(2/2) Follow-up: #12 lands today."
    assert pointers.part_of(one) == (1, 2) and pointers.part_of("Part 2 of 2: x") == (2, 2)
    assert pointers.part_of("1 of 3 tasks failed.") is None
    assert coord.apply(p, [{"type": "reply", "text": one, "chat": "c1"}, {"type": "reply", "text": two, "chat": "c1"}],
                       turn=1) == []
    assert _out(p) == [one, two]
    # The later part going to another chat, or missing, answers nothing: the first part is refused.
    problems = coord.apply(p, [{"type": "reply", "text": one, "chat": "c1"}, {"type": "reply", "text": two, "chat": "c2"}],
                           turn=2)
    assert len(problems) == 1 and "'follow-up below'" in problems[0]
    assert pointers.refused(one, "reply") and not pointers.refused(one, "reply", pointers.later_parts(one, [two]))
    # A notify sent as parts too; an unnumbered later notify does not count as a part.
    assert pointers.later_parts("[1/2] Details below", ["Unrelated.", "[2/2] The cache was stale."]) \
        == ["[2/2] The cache was stale."]
    assert coord.apply(p, [{"type": "notify", "text": "Part 1/2: details below"},
                           {"type": "notify", "text": "Part 2/2: the cache was stale."}], turn=3) == []

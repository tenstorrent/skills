# A reply naming a limit, lock, queue or cap as the cause of slowness with no fix in its batch is flagged
# (runtime/ttp/causefix.py, coordinator.apply, the digest's Course check). Run from test_tt_project_runtime.py.

_LOCK_REPLY = ("Checks are slow because the per-project suite lock serializes them, so each task waits "
               "about 40 minutes for its turn.")


def _cause_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='cause_without_fix' ORDER BY id")


def test_a_reply_naming_a_cause_of_slowness_with_no_fix_is_flagged_once_in_the_next_course_check(env):
    """The suite-lock case: the reply goes out (flag only), a handled log event is written, and the next
    digest's Course check names the reply once; the turn after no longer shows it."""
    from ttp import causefix, coordinator as coord
    p = make(env)
    assert coord.apply(p, [{"type": "reply", "text": _LOCK_REPLY, "chat": "c1"}], turn=1) == []
    mid = p.db.one("SELECT id FROM messages WHERE direction='out' AND kind='reply' ORDER BY id DESC")["id"]
    evs = _cause_events(p)
    assert len(evs) == 1 and evs[0]["status"] == "handled" and f"reply #{mid}" in evs[0]["text"]
    course_check = dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]
    assert course_check.count("named a cause without a fix") == 1, course_check
    assert f"reply #{mid}: \"Checks are slow because the per-project suite lock" in course_check
    # The same turn applied again records nothing twice.
    coord.apply(p, [{"type": "reply", "text": _LOCK_REPLY, "chat": "c1"}], turn=1)
    assert len(causefix._records(p.db)) == len(_cause_events(p))
    # The next turn's batch settles it: the line was shown.
    coord.apply(p, [{"type": "reply", "text": "Noted.", "chat": "c1"}], turn=2)
    assert "course_check" not in dict(coord.digest_parts(p, {}, [], [])[0])
    # A notify is checked the same way.
    coord.apply(p, [{"type": "notify", "text": "Tasks wait for hours behind the 2-slot cap."}], turn=3)
    assert "notify #" in dict(coord.digest_parts(p, {}, [], [])[0])["course_check"]


def test_a_cause_reply_backed_by_a_fix_in_its_batch_is_not_flagged_but_a_refused_fix_does_not_count(env):
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("run the checks", "s", origin="user")
    for fix in ({"type": "task_add", "title": "checks: let tasks share the suite lock", "spec": "Do: x", "kind": "harness"},
                {"type": "task_update", "id": tid, "priority": 1},
                {"type": "config_set", "key": "coordinator.ask_timeout_h", "value": "24"}):
        assert coord.apply(p, [{"type": "reply", "text": _LOCK_REPLY, "chat": "c1"}, fix]) == [], fix
    assert _cause_events(p) == []
    # A task_update that changes none of priority, spec, resources or status is no fix.
    coord.apply(p, [{"type": "reply", "text": _LOCK_REPLY, "chat": "c1"}, {"type": "task_update", "id": tid,
                                                                             "note": "looked at it"}])
    assert len(_cause_events(p)) == 1
    # Nor is a fix the daemon refused.
    problems = coord.apply(p, [{"type": "reply", "text": _LOCK_REPLY, "chat": "c1"},
                               {"type": "task_update", "id": 99999, "priority": 1}])
    assert problems and len(_cause_events(p)) == 2


def test_a_cause_reply_saying_why_there_is_no_fix_passes_and_status_words_alone_do_not_trip_it(env):
    """'no fix because' and an old cause said to be fixed pass; plain status lines that use queue, lock or
    hours without naming a cause of slowness are not hits (the false-positive guard)."""
    from ttp import causefix, coordinator as coord
    p = make(env)
    passes = [
        "#12 waits on the device lock. No fix because the lock keeps two jobs off one chip.",
        "It is blocked by the review cap; not fixing: the cap is the user's.",
        "It waits on the lock. No fix: the lock keeps runs apart.",
        "Yesterday's slowness was the suite lock; that was fixed in 0.2.590.",   # a question about an old cause
        "#12 is queued and waiting on #11.",
        "The push queue landed #12 at 14:05 PDT.",
        "The build took 3 hours; it is done.",
        "I unlocked the branch and it is no longer blocked.",
        "The limit on spend is $40 a day. Results arrive tomorrow morning.",
    ]
    for text in passes:
        assert causefix.names_cause(text) == "", text
        coord.apply(p, [{"type": "reply", "text": text, "chat": "c1"}])
    assert _cause_events(p) == []
    hits = [_LOCK_REPLY, "Tasks wait behind the 2-slot cap.", "Yes, it was blocked by the cap.",
            "Runs are slow: the queue serializes them.",   # an old cause not said to be fixed is still a hit
            "There is no fix yet; #12 waits on the suite lock.", "#12 waits on the lock (it won’t fix itself)."]
    assert all(causefix.names_cause(t) for t in hits)

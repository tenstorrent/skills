# Self-blocking detector tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of selfblock.py and its daemon check here.
from __future__ import annotations

# Held for the user's word on the coordinator's own call: each must fire.
_SELF_BLOCKING = (
    "Keeping the test gate at the full suite: that rule changes only on the user's word",
    "Holds the landing order until the user says which change goes first",
    "Pacing stays at one review a day; only you can change it",
    "waiting for your ok on narrowing the hold scope to one box",
    "Needs the user to approve raising max_parallel_tasks to 4",
    "pending user approval for the retry policy",
    "Reset the idle box? It has no other tenant; waiting on the user's go-ahead",
)
# One of the five real gates: each must stay quiet.
_REAL_GATES = (
    "Deleting the old results directory needs the user's word",                         # (a) destroys data
    "Waits for the user: the machine is shared with other teams",                         # (a) others use it
    "Rewriting the branch history needs your ok: another developer pushes to it",        # (a) shared branch
    "Waits for the user's ok to merge PR 12",                                             # (b) a merge
    "Needs your yes to post the summary in the channel on your behalf",                   # (b) speaks as the user
    "Waiting for the user to sign in with the device code",                               # (c) credential
    "Raising the daily budget to $40 needs the user's word",                              # (c) money
    "Held on the user's word: a charter Restriction forbids pushing there",               # (d) restriction
    "The resource was paused by the user; it lifts on their word",                        # (e) the user's pause
    "delivery.allow_protected_push_branch needs the user's word",                         # a user-only setting
)


def test_selfblock_finding_fires_for_the_coordinators_own_call_and_is_quiet_for_real_gates():
    from ttp import selfblock
    for s in _SELF_BLOCKING:
        assert selfblock.finding(s), s
    for s in _REAL_GATES:
        assert selfblock.finding(s) is None, s
    # No wait on the user at all: quiet.
    assert selfblock.finding("waits for the broker queue to drain") is None
    assert selfblock.finding("In the user's words (verbatim): keep going") is None
    assert selfblock.finding("") is None
    # Said in the negative, or with curly quotes and no apostrophe: read as written.
    assert selfblock.finding("Decided the pacing myself, no longer waiting for your ok") is None
    assert selfblock.finding("A project gate is never held for the user's word") is None
    assert selfblock.finding("Held on the user’s say-so for the pacing")
    assert selfblock.finding("blocked: the users word decides the hold scope")


def test_selfblock_raises_one_event_per_finding_from_tasks_asks_and_replies(env):
    p = make(env)
    from ttp import coordinator as coord, selfblock
    db = p.db
    own = db.add_task("narrow the test gate")
    db.update_task(own, status="blocked", blocked_reason=_SELF_BLOCKING[0])
    real = db.add_task("merge it")
    db.update_task(real, status="blocked", blocked_reason=_REAL_GATES[3])
    # An irreversible ask naming no harm is the coordinator's; one naming deletion is a real gate.
    a_own = db.post("out", "Change the review pacing to two a day?", chat=None, kind="ask",
                    severity="normal", ref="blocking:irreversible")
    db.post("out", "Delete the stale run folders?", chat=None, kind="ask", severity="normal",
            ref="blocking:irreversible")
    # A user-only ask, and a task held on it, stay quiet whatever they say.
    a_user = db.post("out", "Merge PR 7?", chat=None, kind="ask", severity="high", ref="blocking:merge")
    held = db.add_task("waits on the merge")
    db.update_task(held, status="blocked", blocked_reason=f"waits for your ok on ask {a_user}",
                   labels=json.dumps([f"waits:ask:{a_user}"]))
    r_own = db.post("out", "I left the hold scope as it is: that changes only on your word.", chat=None,
                    kind="reply", severity="low")
    r_pause = db.post("out", "The queue stays paused until you say so: you asked me to pause it.", chat=None,
                      kind="reply", severity="low")   # (e): a pause the user set
    now = time.time()
    found = selfblock.raise_due(db, now)
    got = {(f["source"], f["id"]) for f in found}
    assert ("task", own) in got and ("ask", a_own) in got and ("reply", r_own) in got
    assert ("task", real) not in got and ("task", held) not in got and ("ask", a_user) not in got
    assert ("reply", r_pause) not in got and len(found) == 3
    (ev,) = db.q("SELECT * FROM events WHERE kind=?", (selfblock.KIND,))
    assert ev["status"] == "queued" and f"task #{own}" in ev["text"]
    assert "memory_add kind decision" in ev["text"] and "notify low" in ev["text"]
    assert coord.EFFORT_EVENT_TRIGGERS[selfblock.KIND] == "stuck"
    # Each finding raises once, across checks.
    assert selfblock.raise_due(db, now + 60) == []
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    # A new reason on the same task is a new finding.
    db.update_task(own, blocked_reason="Landing order stays fixed until the user decides")
    (again,) = selfblock.raise_due(db, now + 120)
    assert (again["source"], again["id"]) == ("task", own)
    # An old reply is out of the window.
    assert all(f["source"] != "reply" for f in selfblock.scan(db, now + 2 * selfblock.REPLY_WINDOW_S))


def test_selfblock_quiet_for_a_setting_hold_and_a_queued_task(env):
    p = make(env)
    from ttp import selfblock, settinghold
    db = p.db
    t = db.add_task("land on main")
    db.update_task(t, status="blocked", blocked_reason=f"{settinghold.NOTE} delivery.allow_protected_push_branch "
                                                       f"is off; waits for the user's word")
    q = db.add_task("queued")
    db.update_task(q, blocked_reason=_SELF_BLOCKING[0])   # queued: no hold
    assert selfblock.scan(db) == []


def test_selfblock_reads_a_waiting_tasks_hand_off_and_survives_a_torn_result(env):
    p = make(env)
    from ttp import selfblock
    db = p.db
    w = db.add_task("waits")
    db.update_task(w, status="waiting", result=json.dumps({"waiting_for": "the user's ok on the landing order"}))
    torn = db.add_task("torn")
    db.update_task(torn, status="waiting", result="{not json")
    signin = db.add_task("sign in")
    db.update_task(signin, status="waiting", result=json.dumps({"waiting_for": "waits for the user to sign in"}))
    assert [(f["source"], f["id"]) for f in selfblock.scan(db)] == [("task", w)]


def test_daemon_self_blocking_check_raises_and_survives_a_failure(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod, selfblock
    d = dmod.Daemon(p.base)
    t = p.db.add_task("narrow the gate")
    p.db.update_task(t, status="blocked", blocked_reason=_SELF_BLOCKING[2])
    d.check_self_blocking()
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    d._selfblock_checked = 0.0
    monkeypatch.setattr(selfblock, "raise_due", lambda *a, **k: 1 / 0)
    d.check_self_blocking()   # logged, never raised


def test_selfblock_reply_counts_only_hold_wording_not_a_bare_possessive():
    from ttp import selfblock
    for s in ("Decided: narrowed the test gate. Your word reverses it.",
              "Decided: narrowed the test gate to the changed files. Your word reverses it.",
              "Which laptop to use is your call.",
              "your decision.",
              "I can keep the hold scope at all boxes if you prefer; your decision."):
        assert selfblock.finding(s, rx=selfblock.HOLD) is None, s
    for s in ("I left the hold scope as it is: that changes only on your word.",
              "The landing order stays as is until your ok.",
              "Pacing is held for the user's word.",
              "Only you can widen the hold scope.",
              "This needs you to approve the new pacing."):
        assert selfblock.finding(s, rx=selfblock.HOLD), s
    # A PR or draft review ask is the user's (the PR flow): quiet.
    assert selfblock.finding("Waiting for your draft review of the change") is None
    assert selfblock.finding("Held for the user's PR review") is None


def test_selfblock_reply_raises_at_most_once_per_window_and_never_on_its_own_answer(env):
    p = make(env)
    from ttp import selfblock
    db = p.db
    db.post("out", "Decided: narrowed the test gate. Your word reverses it.", chat=None, kind="reply",
            severity="low")
    db.post("out", "Which laptop to use is your call.", chat=None, kind="reply", severity="low")
    now = time.time()
    assert selfblock.raise_due(db, now) == []
    r1 = db.post("out", "I left the hold scope as it is: that changes only on your word.", chat=None,
                 kind="reply", severity="low")
    (f,) = selfblock.raise_due(db, now + 1)
    assert (f["source"], f["id"]) == ("reply", r1)
    # The turn's answer, and any other holding reply, posted inside the window raise nothing,
    # neither now nor once the window has passed.
    db.post("out", "Kept the landing order: it changes only on your word.", chat=None, kind="reply", severity="low")
    assert selfblock.raise_due(db, now + 60) == []
    assert selfblock.raise_due(db, now + selfblock.REPLY_WINDOW_S + 120) == []
    assert db.one("SELECT COUNT(*) n FROM events WHERE kind=?", (selfblock.KIND,))["n"] == 1
    # A task finding is not limited by the reply window.
    t = db.add_task("pacing")
    db.update_task(t, status="blocked", blocked_reason=_SELF_BLOCKING[2])
    assert [(g["source"], g["id"]) for g in selfblock.raise_due(db, now + 120)] == [("task", t)]

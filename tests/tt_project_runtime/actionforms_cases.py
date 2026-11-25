# Coordinator action form tests (actionforms.py) of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file. Each form's own
# tests live in its area's case file (sweep: coordinator_cases, obligations, cells, configset, throughmute,
# turnrules, coordsession); add tests of the shared frame here.
from __future__ import annotations


def _af_blocked(p):
    t = p.db.add_task("blocked")
    p.db.update_task(t, status="blocked", blocked_reason="x")
    return [{"type": "sweep", "id": t, "outcome": "still_blocked", "until": "the vendor sends the signed key"}]


def test_actionforms_a_form_that_raises_leaves_the_action_as_sent(env, monkeypatch):
    """A fault in one form never stops the turn: that action meets its usual check exactly as sent."""
    from ttp import actionforms, coordinator as coord, sweep
    p = make(env)
    sent = _af_blocked(p)

    def boom(*a, **k):
        raise RuntimeError("form fault")
    monkeypatch.setattr(sweep, "normal_form", boom)
    assert actionforms.normalise(p, sent, home="UTC", now=time.time()) == (sent, [])
    out = coord.apply(p, sent)   # refused as before: no start_after, and the form that adds it failed
    assert len(out) == 1 and "needs `start_after`" in out[0], out


def test_actionforms_a_rewrite_is_tallied_as_normalised_never_as_a_refusal(env):
    """apply's rewrite is counted once, as 'normalised', apart from the refusals; the dry run counts nothing."""
    from ttp import coordinator as coord, coordprecheck, rejtally
    p = make(env)
    sent = _af_blocked(p)
    assert coordprecheck.problems(p, sent) == [] and not p.db.kv(rejtally.KEY)
    assert coord.apply(p, sent) == []
    (day,) = p.db.kv(rejtally.KEY).values()
    assert day == {rejtally.NORMALISED: [0, 1]}
    assert rejtally.lines(p.db) == ["action refusals (1d): 0 caught in turn, 0 refused after the turn; "
                                    "1 applied as a clear-meaning form (normalised)"]
    assert any("applied as: " in n for n in p.db.kv(coord.NOTES_KEY))


def test_actionforms_clock_times_and_local_dates_apply_and_pass_the_dry_run(env):
    """Regression for time forms once refused ("look: start_after '23:50': use a delay such as ...", "o755 due:
    a time needs an absolute timestamp with a time zone offset"): a clock time, one with a zone abbreviation
    and a local date and time are read in the home zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ttp import coordinator as coord, coordprecheck, obligations
    p = make(env)
    p.set_config("home_timezone", "America/Los_Angeles")
    now = time.time()
    sent = [{"type": "look", "at": "23:50", "text": "check the bench"},
            {"type": "task_add", "title": "later run", "kind": "work", "spec": "run it", "start_after": "19:20 PST",
             "why": "after the window"}]
    assert coordprecheck.problems(p, sent) == []
    assert coord.apply(p, sent) == []
    at = p.db.task(p.db.one("SELECT id FROM tasks WHERE title='later run'")["id"])["not_before"]
    assert now < at <= now + 86400 and datetime.fromtimestamp(at, ZoneInfo("America/Los_Angeles")).strftime("%H:%M") == "19:20"
    day = datetime.fromtimestamp(now + 3 * 86400, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    assert obligations._when(f"{day} 13:00", "o755 due", p) == \
        datetime.fromisoformat(f"{day}T13:00").replace(tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


def test_actionforms_malformed_fields_get_a_plain_reason_in_apply_and_the_dry_run(env):
    """Regression for 'reply: KeyError: text' and the internal errors of other malformed fields (the sampled
    'task_update: not enough values to unpack' no longer reproduces): a field with no clear meaning is refused
    with a plain reason by apply and by the dry run alike, never with exception text."""
    from ttp import coordinator as coord, coordprecheck
    p = make(env)
    t = p.db.add_task("queued one")
    for bad, why in (*(({"type": "task_update", "id": x, "priority": 2}, "needs the id of a task")
                       for x in ("abc", "", None, [t], f"##{t}")),
                     ({"type": "task_add", "title": "t", "kind": "work", "spec": "s", "depends_on": ["abc"]},
                      "depends_on must be a list of task ids"),
                     ({"type": "task_update", "id": t, "priority": 1e999}, "`priority` must be a whole number"),
                     ({"type": "task_update", "id": t, "priority": "high"}, "`priority` must be a whole number"),
                     ({"type": "task_add", "title": "t", "kind": "work", "spec": ["a", "b"]}, "`spec` must be text"),
                     ({"type": "reply", "chat": "c1"}, "needs `text`"),
                     ({"type": "reply", "chat": "c1", "text": ""}, "needs `text`")):
        probs = coordprecheck.problems(p, [bad])
        assert len(probs) == 1 and why in probs[0][1], (bad, probs)
        out = coord.apply(p, [bad])
        assert len(out) == 1 and why in out[0] and "internal harness error" not in out[0], (bad, out)
    assert coord.apply(p, [{"type": "task_update", "id": str(t), "priority": 2.0}]) == []
    assert p.db.task(t)["priority"] == 2
    # '#12' is task 12, for the id and for depends_on alike (one id reading)
    assert coord.apply(p, [{"type": "task_update", "id": f" #{t} ", "priority": 3}]) == []
    assert p.db.task(t)["priority"] == 3
    assert coord.apply(p, [{"type": "task_add", "title": "after it", "kind": "work", "spec": "s",
                            "depends_on": [f"#{t}"]}]) == []
    assert p.db.one("SELECT depends_on FROM tasks WHERE title='after it'")["depends_on"] == f"[{t}]"


def test_actionforms_start_after_in_a_foreign_zone_is_refused_alike_by_apply_and_the_dry_run(env):
    """Regression: the dry run passed a start_after of '19:20 PST' under a UTC home zone that apply refused
    ('zone PST is not the home zone's'); both now read it with the home zone, on task_add and task_update."""
    from ttp import coordinator as coord, coordprecheck
    p = make(env)
    p.set_config("home_timezone", "UTC")
    t = p.db.add_task("queued one")
    for a in ({"type": "task_add", "title": "later run", "kind": "work", "spec": "run it",
               "start_after": "19:20 PST", "why": "after the window"},
              {"type": "task_update", "id": t, "start_after": "19:20 PST", "why": "after the window"}):
        probs = coordprecheck.problems(p, [a])
        out = coord.apply(p, [a])
        assert len(probs) == 1 and len(out) == 1 and "PST" in probs[0][1] and "PST" in out[0], (a, probs, out)
        assert probs[0][1].split(": ", 1)[-1] in out[0], (probs, out)
    assert not p.db.one("SELECT id FROM tasks WHERE title='later run'") and not p.db.task(t)["not_before"]


def test_actionforms_a_type_that_is_not_text_is_refused_plainly(env):
    """Regression for a TypeError in normalise on `type: ["x"]`: the forms skip it, the dry run does not
    raise, and apply refuses it without an internal error."""
    from ttp import actionforms, coordinator as coord, coordprecheck
    p = make(env)
    sent = [{"type": ["task_update"], "id": 1}]
    assert actionforms.normalise(p, sent, home="UTC", now=time.time()) == (sent, [])
    coordprecheck.problems(p, sent)
    out = coord.apply(p, sent)
    assert len(out) == 1 and "internal harness error" not in out[0], out


def test_actionforms_rewrites_are_tallied_once_per_applied_batch(env):
    """A batch cut off before it finished and applied again is tallied once, not twice; a rewritten action
    that is then refused is not counted as normalised."""
    from ttp import coordinator as coord, rejtally
    p = make(env)
    sent = _af_blocked(p)

    def cut(i):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        coord.apply(p, sent, on_action=cut)
    assert not p.db.kv(rejtally.KEY)
    assert coord.apply(p, sent) == []
    (day,) = p.db.kv(rejtally.KEY).values()
    assert day == {rejtally.NORMALISED: [0, 1]}

# Tier floor tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new tests of tierfloor.py (conclusion runs never start light or below high
# effort) here.
from __future__ import annotations


def _floor_daemon(env, monkeypatch, jev=None):
    """A project with no other work and a daemon whose starts are recorded, not run: (p, d, started)."""
    from ttp.daemon import Daemon
    p = make(env)
    p.db.x("UPDATE tasks SET status='done'")
    d = Daemon(p.base)
    d.jev = jev
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.base), None))
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append((a, k)) or 0)
    return p, d, started


def test_tier_floor_classifies_each_conclusion_case_and_leaves_mechanical_work_alone():
    from ttp import tierfloor
    assert tierfloor.reasons({"kind": "question", "spec": "x"}) == ["kind question"]   # (a)
    assert tierfloor.reasons({"kind": "plan", "spec": "x"}) == ["kind plan"]
    assert tierfloor.reasons({"kind": "work", "spec": "x", "reply_chat": "c1"}) == ["answers a chat"]   # (b)
    for spec in ("Goal: find it.\nconclusion: the root cause of the stall", "- Conclusion: a k/N table",
                 "1. conclusion : a verdict", "**Conclusion:** the status claim"):
        assert tierfloor.reasons({"kind": "code", "spec": spec}) == ["conclusion: line"]   # (c)
    for name in ("daily-review", "weekly-summary", "spend_digest", "status-report"):   # (d)
        assert tierfloor.reasons({"kind": "work", "spec": "x", "origin": "schedule",
                                  "labels": json.dumps([name])}) == [f"schedule {name}"]
    # Mechanical work, a mention of the word mid-line, a polling schedule and a user's work task stay off.
    for t in ({"kind": "work", "spec": "Copy the log to the run folder."},
              {"kind": "code", "spec": "Rename the helper; its `conclusion:` field stays."},
              {"kind": "work", "spec": "x", "origin": "schedule", "labels": json.dumps(["poll-queue"])},
              {"kind": "work", "spec": "rerun the failed test", "origin": "user", "reply_chat": None}):
        assert tierfloor.reasons(t) == [], t
    both = tierfloor.reasons({"kind": "question", "reply_chat": "c", "spec": "conclusion: why"})
    assert both == ["kind question", "answers a chat", "conclusion: line"]


def test_tier_floor_config_takes_a_tier_and_effort_and_refuses_anything_else(env):
    from ttp import coordinator as coord, tierfloor
    assert tierfloor.floor({}) == {"tier": "standard", "effort": "high"}, "on by default"
    p = make(env)
    assert tierfloor.floor(p.config()) == {"tier": "standard", "effort": "high"}
    conv = coord.settable("coordinator.conclusion_floor")
    assert conv("deep/xhigh") == {"tier": "deep", "effort": "xhigh"}
    assert conv("standard") == {"tier": "standard", "effort": "high"}
    assert conv("off") == conv("light") == {"tier": "light", "effort": ""}
    assert conv({"tier": "standard", "effort": "medium"}) == {"tier": "standard", "effort": "medium"}
    for bad in ("huge", "standard/turbo", 3, None, ["standard"]):
        with pytest.raises(ValueError):
            conv(bad)
    # A hand-broken value falls back to the default: the floor never turns off by accident.
    assert tierfloor.floor({"coordinator": {"conclusion_floor": "nonsense"}}) == tierfloor.DEFAULT


def test_an_already_queued_light_question_is_lifted_at_dispatch_with_a_visible_line(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    q = p.db.add_task("Did the change stop the stall?", "Check the logs.", kind="question", tier="light",
                      origin="coordinator", provider="claude", budget_usd=2.0)
    keep = p.db.add_task("Why did it fail?", "Read the log.", kind="question", tier="light",
                         origin="coordinator", provider="claude", budget_usd=3.5, priority=4)
    d.dispatch()
    assert [a[3] for a, _ in started] == ["standard", "standard"]
    t = p.db.task(q)
    assert t["tier"] == "standard" and t["tier_source"] == "conclusion_floor"
    assert t["budget_usd"] == 8.0, "the light default follows the tier to the standard default"
    assert p.db.task(keep)["budget_usd"] == 3.5, "an explicit budget stays"
    assert t["spec"].endswith("[tier floor] light->standard (conclusion: kind question)")
    kw = started[0][1]
    assert kw["note"]["conclusion_floor"]["tier"] == "standard" and kw["budget_usd"] == 8.0
    assert kw["note"]["conclusion_floor"]["effort"] is None, "claude's standard tier already runs at high"
    assert kw["min_effort"] == "high", "the run's effort floor (start_run never lowers an effort)"
    from ttp.cli import show_task
    assert "conclusion_floor" in show_task(t) and "[tier floor]" in show_task(t)
    # A second start adds no second line.
    p.db.update_task(q, status="queued", tier="light")
    started.clear()
    d.dispatch()
    assert p.db.task(q)["spec"].count("[tier floor]") == 1


def test_the_floor_applies_after_jevs_pick_and_mechanical_light_work_stays_light(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch, jev=_EffortJev(0.1))
    now = time.time()
    for model, effort, cost in (("opus", "high", 3.0), ("sonnet", "low", 1.0)):
        p.db.x("INSERT INTO runs(role,provider,model,effort,status,started,ended,cost_usd) "
               "VALUES('worker','claude',?,?,'ok',?,?,?)", (model, effort, now - 3600, now - 3500, cost))
    q = p.db.add_task("Which boxes run the old broker?", "List them.", kind="question", tier="standard",
                      origin="coordinator", provider="claude", budget_usd=8.0)
    w = p.db.add_task("Copy the log", "Copy the build log to the run folder.", kind="work", tier="light",
                      origin="coordinator", provider="claude", budget_usd=2.0, priority=4)
    d.dispatch()
    tiers = [a[3] for a, _ in started]
    assert tiers == ["standard", "light"]
    qkw = started[0][1]
    assert qkw["note"]["pick"]["tier"] == "light" and qkw["note"]["pick"]["by"] == "jev"
    assert qkw["note"]["conclusion_floor"]["from"] == "light" and p.db.task(q)["tier"] == "standard"
    assert p.db.one("SELECT avoided_usd FROM jev_calls WHERE ref=?", (f"task:{q}",))["avoided_usd"] == 0
    assert "conclusion_floor" not in started[1][1]["note"] and p.db.task(w)["tier"] == "light"
    assert "[tier floor]" not in p.db.task(w)["spec"]


def test_a_budget_gate_below_the_floor_holds_a_conclusion_run_queued_and_clamps_mechanical_work(env, monkeypatch):
    from ttp import budget as bud, tierfloor
    p, d, started = _floor_daemon(env, monkeypatch)
    q = p.db.add_task("Did the fix hold?", "Read the logs.", kind="question", tier="light", origin="coordinator",
                      provider="claude", budget_usd=2.0)
    w = p.db.add_task("Copy the log", "Copy the build log to the run folder.", kind="work", tier="standard",
                      origin="coordinator", provider="claude", budget_usd=8.0, priority=4)
    for level in ("orange", "red"):
        d.gates = {"claude": bud.Gate(provider="claude", level=level, max_tier="light", max_parallel=2)}
        started.clear()
        d.dispatch()
        assert p.db.task(q)["status"] == "queued" and p.db.task(q)["attempts"] == 0
        assert p.db.task(q)["blocked_reason"] == f"{tierfloor.HELD_NOTE} standard"
        if level == "orange":   # mechanical work keeps the clamp
            assert [(a[3], k["task"]["id"]) for a, k in started] == [("light", w)]
            p.db.update_task(w, status="done")
        else:
            assert started == []
    d.gates = {"claude": bud.Gate(provider="claude", level="green", max_tier="deep", max_parallel=2)}
    started.clear()
    d.dispatch()
    assert [(a[3], k["min_effort"]) for a, k in started] == [("standard", "high")]
    assert not p.db.task(q)["blocked_reason"]
    # The floor turned off: the gate clamps it to light again.
    p.set_config("coordinator.conclusion_floor", "off")
    d.cfg = p.config()
    p.db.update_task(q, status="queued", tier="light")
    d.gates = {"claude": bud.Gate(provider="claude", level="orange", max_tier="light", max_parallel=2)}
    started.clear()
    d.dispatch()
    assert [(a[3], k["min_effort"]) for a, k in started] == [("light", None)]


def test_a_conclusion_tasks_wake_whose_wait_is_over_runs_at_the_floor_and_a_check_stays_light(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    waits = {"status": "waiting", "summary": "checks run", "retry_when": "ttp checks --result /tmp/run/1",
             "waiting_for": "checks"}
    q = p.db.add_task("Is the suite green?", "x", kind="question", tier="light", origin="coordinator",
                      provider="claude", budget_usd=2.0)
    w = p.db.add_task("Copy the log", "x", kind="work", tier="standard", origin="coordinator",
                      provider="claude", budget_usd=8.0, priority=4)
    for t in (q, w):
        p.db.update_task(t, result=json.dumps(waits))
    d.dispatch()
    got = {k["task"]["id"]: (a[3], k["min_effort"], k["note"]["wake"]["tier"]) for a, k in started}
    assert got[q] == ("standard", "high", "standard"), "the wake may write the answer: it runs at the floor"
    assert got[w] == ("light", None, "light"), "a mechanical task's wake stays a light check"
    # A stale wake (the same wait handed back) only checks it: light, whatever the task is.
    started.clear()
    p.db.update_task(q, status="queued", result=json.dumps({**waits, "stale_wakes": 2}))
    d.dispatch()
    assert [(a[3], k["min_effort"]) for a, k in started] == [("light", None)]


def test_retry_tier_up_still_applies_on_top_of_the_floor(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    q = p.db.add_task("Why does it stall?", "x", kind="question", tier="standard", origin="coordinator",
                      provider="claude", budget_usd=8.0)
    p.db.update_task(q, attempts=1, result=json.dumps({"status": "failed", "summary": "no idea"}))
    d.dispatch()
    (args, kw), = started
    assert args[3] == "deep" and kw["note"]["pick"]["by"] == "retry" and "conclusion_floor" not in kw["note"]
    # A failed light question retries a tier up to standard: the floor has nothing left to lift.
    started.clear()
    l = p.db.add_task("What changed?", "x", kind="question", tier="light", origin="coordinator",
                      provider="claude", budget_usd=2.0)
    p.db.update_task(l, attempts=1, result=json.dumps({"status": "failed", "summary": "?"}))
    d.dispatch()
    (args, kw), = started
    assert args[3] == "standard" and kw["note"]["pick"]["by"] == "retry" and "conclusion_floor" not in kw["note"]


def test_the_floor_keeps_required_pins_and_reviews_and_follows_the_config(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    pinned = p.db.add_task("What is the answer?", "x", kind="question", tier="light", origin="coordinator",
                           provider="claude", labels=["pin:required"])
    review = p.db.add_task("Review #1", "x", kind="review", tier="light", origin="daemon", provider="claude")
    from ttp import tierfloor
    assert tierfloor.lift(p.db, p.config(), p.db.task(pinned), "claude") is None
    assert tierfloor.lift(p.db, p.config(), p.db.task(review), "claude") is None
    # A project that sets its own floor: deep at xhigh; one that turns it off keeps light.
    q = p.db.add_task("Status?", "x", kind="question", tier="light", origin="coordinator", provider="claude")
    p.set_config("coordinator.conclusion_floor", {"tier": "deep", "effort": "xhigh"})
    d.cfg = p.config()
    got = tierfloor.lift(p.db, d.cfg, p.db.task(q), "claude")
    assert (got["tier"], got["effort"]) == ("deep", None), "claude's deep tier already runs above xhigh"
    assert tierfloor.note(got) == "[tier floor] light->deep (conclusion: kind question)"
    p.set_config("coordinator.conclusion_floor", "light/high")   # effort only: light stays, its effort rises
    got = tierfloor.lift(p.db, p.config(), p.db.task(q), "claude")
    assert (got["tier"], got["effort"]) == ("light", "high")
    assert tierfloor.note(got) == "[tier floor] tier light, effort low->high (conclusion: kind question)"
    p.set_config("coordinator.conclusion_floor", "off")
    assert tierfloor.lift(p.db, p.config(), p.db.task(q), "claude") is None


def test_a_conclusion_runs_effort_floor_lifts_a_low_tier_effort_as_the_provider_takes_it(env, monkeypatch):
    """A tier whose effort is below the floor (a project's own mapping) runs at the floor's effort."""
    from ttp import tierfloor
    from ttp.daemon import Daemon
    from ttp.providers import codex as codex_provider

    class Built(Exception):
        pass

    def build(self, **kw):   # what the run would start with; no agent process starts
        raise Built(kw["effort"])
    monkeypatch.setattr(codex_provider.Codex, "build", build)
    p = make(env)
    p.set_config("providers.codex.tiers.standard.effort", "medium")
    q = p.db.add_task("Why?", "x", kind="question", tier="standard", origin="coordinator", provider="codex")
    got = tierfloor.lift(p.db, p.config(), p.db.task(q), "codex")
    assert (got["tier"], got["effort"], got["effort_from"]) == ("standard", "high", "medium")
    assert tierfloor.note(got) == "[tier floor] tier standard, effort medium->high (conclusion: kind question)"
    d = Daemon(p.base)

    def launch(**kw):
        with pytest.raises(Built) as got:
            d.start_run("worker", "go", "codex", "standard", str(p.root), task=p.db.task(q), **kw)
        return str(got.value)

    assert launch() == "medium"
    assert launch(min_effort="high") == "high"
    assert launch(min_effort="low") == "medium", "a floor never lowers an effort"
    # A low effort pin (not one the user required) rises too, and the runner's pin check sees the new one.
    p.db.update_task(q, effort="low")
    assert launch() == "low" and launch(min_effort="high") == "high"
    # Claude takes no `minimal`: the floor lands on the nearest effort it takes.
    assert tierfloor.kept_effort("claude", "minimal") == "low"

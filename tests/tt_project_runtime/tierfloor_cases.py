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


def test_a_conclusion_run_held_by_the_budget_gate_makes_no_effort_pick_or_wake_gate_call(env, monkeypatch):
    # A hold placed after a paid step repeats that step every dispatch tick: the hold comes first.
    from ttp import budget as bud, tierfloor
    p, d, started = _floor_daemon(env, monkeypatch, jev=_EffortJev(0.1))
    now = time.time()
    for model, effort, cost in (("opus", "high", 3.0), ("sonnet", "low", 1.0)):
        p.db.x("INSERT INTO runs(role,provider,model,effort,status,started,ended,cost_usd) "
               "VALUES('worker','claude',?,?,'ok',?,?,?)", (model, effort, now - 3600, now - 3500, cost))
    q = p.db.add_task("Which boxes run the old broker?", "List them.", kind="question", tier="standard",
                      origin="coordinator", provider="claude", budget_usd=8.0)
    a = p.db.add_task("Is the suite green?", "x", kind="question", tier="light", origin="coordinator",
                      provider="claude", budget_usd=2.0)
    p.db.update_task(a, result=json.dumps({"status": "waiting", "summary": "checks run", "waiting_for": "checks",
                                           "retry_when": "ttp checks --result /tmp/run/1"}))
    gated = []
    real_gate = d._wake_gate
    monkeypatch.setattr(d, "_wake_gate", lambda t: gated.append(t["id"]) or real_gate(t))
    d.gates = {"claude": bud.Gate(provider="claude", level="orange", max_tier="light", max_parallel=2)}
    for _ in range(4):
        d.dispatch()
    assert started == [] and d.jev.calls == 0 and gated == []
    assert p.db.one("SELECT COUNT(*) AS n FROM jev_calls")["n"] == 0
    for t in (q, a):
        assert p.db.task(t)["status"] == "queued"
        assert p.db.task(t)["blocked_reason"] == f"{tierfloor.HELD_NOTE} standard"
    assert p.db.task(q)["tier"] == "standard"
    p.db.update_task(a, status="done")
    d.gates = {"claude": bud.Gate(provider="claude", level="green", max_tier="deep", max_parallel=2)}
    d.dispatch()
    assert [(x[3], k["min_effort"]) for x, k in started] == [("standard", "high")] and d.jev.calls == 1
    assert not p.db.task(q)["blocked_reason"]


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


def test_the_floor_keeps_required_pins_lifts_reviews_and_follows_the_config(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    pinned = p.db.add_task("What is the answer?", "x", kind="question", tier="light", origin="coordinator",
                           provider="claude", labels=["pin:required"])
    review = p.db.add_task("Review #1", "x", kind="review", tier="light", origin="daemon", provider="claude")
    from ttp import tierfloor
    assert tierfloor.lift(p.db, p.config(), p.db.task(pinned), "claude") is None
    got = tierfloor.lift(p.db, p.config(), p.db.task(review), "claude")
    assert (got["tier"], got["why"]) == ("standard", ["review verdict"]), "a review verdict never runs light"
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


def test_a_conclusion_tasks_poll_wake_keeps_the_light_tier_its_hand_off_asked_for(env, monkeypatch):
    """A hand-off's `wake_tier: light` is a poll: the conclusion floor lifts neither its tier nor its
    effort, on the probe-pass and the reboot wake, even past a budget gate capped at light. A wake that
    may write the conclusion (an escalated wake, a spec-change wake, no wake_tier asked) keeps the floor."""
    from ttp import budget as bud
    from ttp.daemon import SPEC_SEEN_KEY
    from ttp.prompts import spec_digest, worker_task
    p, d, started = _floor_daemon(env, monkeypatch)
    t = p.db.add_task("Measure the fix", "Run the benchmark.\nconclusion: did the fix help?", kind="work",
                      tier="standard", origin="coordinator", provider="claude", budget_usd=8.0)
    waits = {"status": "waiting", "summary": "benchmark runs", "retry_when": "test -e /nonexistent/done",
             "waiting_for": "the benchmark", "wake_tier": "light", SPEC_SEEN_KEY: spec_digest(p.db.task(t))}

    def wake(result, how="probe"):
        p.db.update_task(t, status="queued", result=json.dumps(result), not_before=time.time() + 3600)
        if how == "probe":
            d._probe_passed(p.db.task(t), time.time())
        else:
            p.db.update_task(t, not_before=None)
        started.clear()
        d.dispatch()
        return [(a[3], k["min_effort"], k["note"]["wake"]["tier"]) for a, k in started]

    assert wake(waits) == [("light", None, "light")], "a probe-pass wake of a light hand-off is a poll"
    kw = started[0][1]
    assert "a verdict or a device or machine action, so the work it does when the wait is over is that" in worker_task(
        p, p.db.task(t), str(p.base), None, wake=kw["note"]["wake"])
    # A reboot that cut its detached job: the daemon's own light relaunch wake is a poll too.
    cut = {**waits, "wake_tier": "light", "woke": "the host rebooted and cut short detached job x"}
    assert wake(cut, how="boot") == [("light", None, "light")]
    # A budget gate capped at light holds no poll: it runs light.
    d.gates = {"claude": bud.Gate(provider="claude", level="orange", max_tier="light", max_parallel=2)}
    assert wake(waits) == [("light", None, "light")] and not p.db.task(t)["blocked_reason"]
    d.gates = {"claude": bud.Gate(provider="claude", level="green", max_tier="deep", max_parallel=2)}
    # Wakes that may write the conclusion keep the floor.
    assert wake({**waits, "escalated_wake": True}) == [("standard", "high", "standard")]
    assert wake({k: v for k, v in waits.items() if k != "wake_tier"}) == [("standard", "high", "standard")]
    p.db.update_task(t, spec=p.db.task(t)["spec"] + "\n\n## Update\nAlso compare against the old build.")
    assert wake(waits) == [("standard", "high", "standard")], "a spec change is new work at the task's own tier"


def test_tier_floor_lifts_device_and_verdict_runs_and_leaves_mechanical_ones(env, monkeypatch):
    from ttp import machines, tierfloor
    monkeypatch.setattr(machines, "load", lambda: {"box1": {"tags": []}})
    cfg = {"device": {"locks": ["dev-lock"]}}
    dev = lambda **k: tierfloor.reasons({"kind": "work", "spec": "Copy the log.", **k}, cfg)
    for labels in (["needs_device"], ["resource:box1-device"], ["exclusive:dev-lock"], ["resource:box1"]):
        assert dev(labels=json.dumps(labels)) == ["device or machine resource"], labels
    for spec in ("Run tt-smi -glx_reset on the box.", "Power cycle the host via its BMC.",
                 "Start the 12 h soak.", "Submit the device job and report.", "Reset the board, then rerun."):
        assert tierfloor.reasons({"kind": "work", "spec": spec}) == ["device action"], spec
    for spec in ("Give a pass/fail call per model.", "Report a k/N table of the runs.",
                 "Post the cell results.", "verdict: whether the fix holds"):
        assert tierfloor.reasons({"kind": "work", "spec": spec}) == ["verdict"], spec
    assert tierfloor.reasons({"kind": "review", "spec": "x"}) == ["review verdict"]
    # Mechanical work stays off: a resource that is no machine or device, a git reset, a rerun a standard run judges,
    # and the floor's own note in the spec.
    for t in ({"kind": "work", "spec": "Copy the log.", "labels": json.dumps(["resource:build-cache"])},
              {"kind": "code", "spec": "git reset --hard the scratch branch, then rebuild."},
              {"kind": "work", "spec": "Rerun the failed tests; a standard run judges them."},
              {"kind": "work", "spec": "Copy it.\n\n[tier floor] light->standard (conclusion: verdict; device action)"}):
        assert tierfloor.reasons(t, cfg) == [], t


def test_device_verdict_and_chat_runs_start_at_the_floor_and_mechanical_light_work_stays_light(env, monkeypatch):
    p, d, started = _floor_daemon(env, monkeypatch)
    ids = [p.db.add_task("Reset the box", "Reset the board and rerun the smoke test.", tier="light",
                         origin="coordinator", provider="claude", labels=["needs_device"]),
           p.db.add_task("Cells", "Run the sweep and report the cell results.", tier="light",
                         origin="coordinator", provider="claude"),
           p.db.add_task("Answer", "Read the log and answer.", tier="light", origin="coordinator",
                         provider="claude", reply_chat="c1"),
           p.db.add_task("Copy the log", "Copy the build log to the run folder.", tier="light",
                         origin="coordinator", provider="claude")]
    d.dispatch()
    assert [a[3] for a, _ in started] == ["standard", "standard", "standard", "light"]
    whys = [kw["note"].get("conclusion_floor", {}).get("why") for _, kw in started]
    assert whys == [["device or machine resource"], ["verdict"], ["answers a chat"], None]
    for i in ids[:3]:
        assert p.db.task(i)["tier_source"] == "conclusion_floor" and "[tier floor]" in p.db.task(i)["spec"]
    assert p.db.task(ids[3])["tier"] == "light" and "[tier floor]" not in p.db.task(ids[3])["spec"]


def test_a_floor_run_never_routes_to_the_chat_model_and_a_mechanical_one_still_does(env, monkeypatch):
    p, d, _ = _floor_daemon(env, monkeypatch)
    d.cfg = dict(d.cfg, core_provider="claude", providers={**d.cfg["providers"], "chat": {
        "tiers": {"light": {"route": True, "model": "glm-x"}, "standard": {"route": True, "model": "glm-x"}}}})
    now = time.time()
    dev = p.db.task(p.db.add_task("Soak", "Start the soak.", tier="standard", origin="coordinator"))
    rev = p.db.task(p.db.add_task("Review #1", "x", kind="review", tier="standard", origin="daemon"))
    chat = p.db.task(p.db.add_task("Answer", "x", tier="standard", origin="coordinator", reply_chat="c1"))
    mech = p.db.task(p.db.add_task("Copy", "Copy the log.", tier="light", origin="coordinator"))
    for t in (dev, chat):
        assert d._route(t, "standard", None, now) is None
        assert d._floor_keeps_core(t, "standard", None) == "chat:glm-x"
    assert d._route(rev, "standard", None, now) is None, "reviews never route"
    assert d._route(mech, "light", None, now)["to"] == "chat" and d._floor_keeps_core(mech, "light", None) == ""
    # The project turned the floor off: its routing is its own choice again.
    d.cfg = dict(d.cfg, coordinator={**d.cfg["coordinator"], "conclusion_floor": "off"})
    assert d._route(dev, "standard", None, now)["to"] == "chat"


def test_a_floor_runs_cheap_pin_is_dropped_unless_the_user_required_it(env, monkeypatch):
    from ttp import tierfloor, web
    p, d, started = _floor_daemon(env, monkeypatch)
    to_chat = p.db.add_task("Verdict", "Give the pass/fail call.", tier="standard", origin="coordinator",
                            provider="chat", model="glm-x")
    light_model = p.db.add_task("Answer", "x", tier="standard", origin="coordinator", provider="claude",
                                model="sonnet", reply_chat="c1", priority=4)
    required = p.db.add_task("Pinned", "Run the device job.", tier="standard", origin="coordinator",
                             provider="claude", model="sonnet", labels=["pin:required"], priority=5)
    mech = p.db.add_task("Copy", "Copy the log.", tier="light", origin="coordinator", provider="claude",
                         model="sonnet", priority=5)
    cfg = dict(d.cfg, core_provider="claude")
    assert tierfloor.unpin(p.db, cfg, p.db.task(to_chat))["clear"] == {"provider": None, "model": None}
    assert tierfloor.unpin(p.db, cfg, p.db.task(light_model))["clear"] == {"model": None}
    assert tierfloor.unpin(p.db, cfg, p.db.task(required)) is None, "a pin the user required wins"
    assert tierfloor.unpin(p.db, cfg, p.db.task(mech)) is None, "mechanical work keeps its pin"
    d.cfg = cfg
    task, why = d._floor_unpin(p.db.task(light_model))
    assert task["model"] is None and p.db.task(light_model)["model"] is None and "light-tier model sonnet" in why
    assert "[tier floor] pin dropped" in p.db.task(light_model)["spec"]
    assert d._floor_unpin(p.db.task(light_model)) == (p.db.task(light_model), ""), "once"
    note = json.dumps({"conclusion_floor": {"tier": "standard", "from": "light", "why": ["device action"],
                                            "kept_core": "chat:glm-x", "unpinned": why}})
    assert web.run_floor(note) == ("floor light->standard (device action); kept off chat:glm-x; "
                                   f"pin dropped: {why}")
    assert web.run_floor(json.dumps({"wake": {"tier": "light"}})) is None and web.run_floor("bad") is None


def test_a_light_or_chat_routed_runs_notify_is_refused_and_a_standard_runs_is_queued(env, monkeypatch, capsys):
    """A light-tier or chat-routed worker run never posts to the user's chat: the daemon marks it at start
    (workernotify.ENV) and `ttp notify` refuses its low and normal notifies, user steps included, saying to
    hand the text off; `--severity high` still reaches the coordinator. A standard run's notify is queued."""
    from ttp import cli, workernotify
    from ttp.daemon import ESCALATE_FILE, NOTIFY_FILE, Daemon
    from ttp.providers import claude
    monkeypatch.setattr(claude.Claude, "binary", lambda self: "/usr/bin/true")   # never a real agent
    monkeypatch.setenv(workernotify.ENV, "inherited")   # a stale value from the daemon's own environment
    p = make(env)
    d = Daemon(p.base)

    def run_env(role, tier):
        tid = p.db.add_task("t", "s", kind="review" if role == "reviewer" else "work", tier=tier, origin="user")
        rid = d.start_run(role, "go", "claude", tier, str(p.root), task=p.db.task(tid))
        (p.runs / str(rid) / "STOP").touch()
        # The child's env as the runner builds it: run.json's env over the daemon's own (runner.supervise).
        return {**os.environ, **json.loads((p.runs / str(rid) / "run.json").read_text())["env"]}

    assert run_env("worker", "light")[workernotify.ENV] == "on the light tier"
    assert run_env("reviewer", "light")[workernotify.ENV] == "on the light tier"
    assert run_env("worker", "standard").get(workernotify.ENV, "").strip() == "", "never an inherited value"
    cfg = p.config()
    assert workernotify.no_post(cfg, "worker", "standard", "chat") == "routed to the chat provider"
    assert workernotify.no_post(cfg, "worker", "deep", "chat") == "routed to the chat provider"
    assert workernotify.no_post(cfg, "worker", "standard", "claude") == ""
    assert workernotify.no_post(cfg, "worker", "deep", "codex") == ""
    assert workernotify.no_post(cfg, "coordinator", "light", "claude") == "", "the coordinator's own channel"
    assert workernotify.no_post({**cfg, "core_provider": "chat"}, "worker", "standard", "chat") == "", \
        "a project whose core provider is chat posts as any core run"

    def notify(run_dir, why, *args):
        monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
        if why:
            monkeypatch.setenv(workernotify.ENV, why)
        else:
            monkeypatch.delenv(workernotify.ENV, raising=False)
        try:
            cli.main(["notify", *args])
        except SystemExit as e:
            return int(e.code or 0), capsys.readouterr()
        return 0, capsys.readouterr()

    for why in ("on the light tier", "routed to the chat provider"):
        run_dir = p.runs / why.replace(" ", "-")
        run_dir.mkdir(parents=True)
        for args in (["done: 3 files"], ["--severity", "low", "fyi"], ["--severity", "normal", "x"],
                     ["--step", "sign-in", "--check", "true", "sign in"]):
            rc, out = notify(run_dir, why, *args)
            assert rc != 0 and f"this run is {why}" in out.err and "hand-off" in out.err, (why, args, out)
        assert not (run_dir / NOTIFY_FILE).exists(), "nothing queued for the user's chat"
        rc, out = notify(run_dir, why, "--severity", "high", "the broker is down")
        assert rc == 0 and (run_dir / ESCALATE_FILE).exists(), "high still goes to the coordinator"
        assert not (run_dir / NOTIFY_FILE).exists()
    run_dir = p.runs / "standard"
    run_dir.mkdir(parents=True)
    rc, out = notify(run_dir, "", "done: 3 files")
    assert rc == 0 and "queued for the user" in out.out
    assert json.loads((run_dir / NOTIFY_FILE).read_text().splitlines()[0])["text"] == "done: 3 files"
    rc, out = notify(run_dir, "  ", "--severity", "low", "fyi")   # a blank value marks nothing
    assert rc == 0 and len((run_dir / NOTIFY_FILE).read_text().splitlines()) == 2


def test_a_worker_wake_of_a_code_task_with_a_conclusion_line_starts_at_its_class_effort_but_a_review_keeps_the_floor(env, monkeypatch):
    """B: a code task's worker_wake does not take the conclusion floor's effort (run 40: Sonnet at high);
    its first run, a review and a non-code task's wake keep it."""
    from ttp.daemon import SPEC_SEEN_KEY
    from ttp.prompts import spec_digest
    p, d, started = _floor_daemon(env, monkeypatch)
    spec = "Build it.\nconclusion: did the fix help?"
    waits = {"status": "waiting", "summary": "x", "retry_when": "test -e /nonexistent/done", "waiting_for": "x"}

    def run(kind, result=None, **kw):
        t = p.db.add_task("Measure the fix", spec, kind=kind, tier="standard", origin="coordinator",
                          provider="claude", budget_usd=8.0, **kw)
        if result is not None:
            p.db.update_task(t, status="queued", result=json.dumps({**result, SPEC_SEEN_KEY: spec_digest(p.db.task(t))}))
        started.clear()
        d.dispatch()
        return [(k["min_effort"], (k.get("note") or {}).get("wake") is not None) for a, k in started if k.get("task")["id"] == t]

    assert run("code", dict(waits, escalated_wake=True)) == [(None, True)], "a code task's wake: the class's effort"
    assert run("work", dict(waits, escalated_wake=True)) == [("high", True)], "a non-code wake keeps the floor"
    assert run("code") == [("high", False)], "a first run keeps the floor"
    assert run("review") == [("high", False)], "a review keeps the floor"

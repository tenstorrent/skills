# Run-class tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of runclass.py here.
from __future__ import annotations

_RC_CHAT = {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1",
            "tiers": {"light": {"model": "example-org/small-model"}}}


def _rc_cfg(**route):
    return {"providers": {"chat": _RC_CHAT}, **({"route": route} if route else {})}


def test_run_classes_cover_every_kind_of_run():
    from ttp import runclass
    c = runclass.classify
    assert c(None, {"role": "coordinator"}) == "coordinator"
    assert c({"kind": "code", "tier": "light", "title": "x"}, {"role": "coordinator"}) == "coordinator"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"conflict_fix": {"tier": "light"}}}) == "rebase"
    assert c({"kind": "code", "tier": "standard", "title": "x", "labels": '["rebase"]'}) == "rebase"
    assert c({"kind": "code", "tier": "standard", "title": "rebase #12 onto the branch"}) == "hard"   # never by title
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 120}}}) == "review_small"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 900}}}) == "hard"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {}}) == "hard"   # size unknown
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 120}}},
             {"route": {"review_small_lines": 100}}) == "routine"
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": {"wake": {"tier": "light"}}}) == "worker_wake"
    assert c({"kind": "code", "tier": "light", "title": "x"}) == "light"
    assert c({"kind": "question", "tier": "standard", "title": "why"}) == "status_report"
    assert c({"kind": "work", "tier": "standard", "title": "report runs per hour"}) == "status_report"
    assert c({"kind": "work", "tier": "standard", "title": "bring up a model"}) == "routine"
    assert c({"kind": "code", "tier": "standard", "title": "fix it"}) == "hard"
    assert c({"kind": "plan", "tier": "deep", "title": "plan"}) == "hard"
    # A recorded class wins; a bad one is ignored.
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": '{"run_class": "light"}'}) == "light"
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": '{"run_class": "bogus"}'}) == "hard"


def test_routine_runs_take_sonnet_at_high_effort_by_default_and_opus_keeps_hard_pinned_urgent_and_retried_work():
    from ttp import runclass
    c = runclass.classify
    for kind in ("work", "question", "harness"):
        t = {"kind": kind, "tier": "standard", "title": "tidy the notes"}
        assert c(t) == ("status_report" if kind == "question" else "routine"), kind
    review = {"kind": "review", "tier": "standard", "title": "r"}
    assert c(review, {"note": {"review_tier": {"lines": 350}}}) == "routine"
    assert c(review, {"note": {"review_tier": {"lines": 401}}}) == "hard"
    assert c(review, {"note": {"review_tier": {"lines": 450}}}, {"route": {"review_medium_lines": 500}}) == "routine"
    for t in ({"kind": "code", "tier": "standard", "title": "x"}, {"kind": "plan", "tier": "standard", "title": "p"},
              {"kind": "work", "tier": "deep", "title": "x"}, {"kind": "work", "tier": "standard", "title": "x",
                                                                "spec": "run it on the device (tt-smi reset)"}):
        assert c(t) == "hard", t
    t = {"kind": "harness", "tier": "standard", "title": "tidy", "labels": "[]"}
    # On by default: an empty config already routes routine runs to Sonnet at high effort.
    assert runclass.cheap_models({})["routine"] == {"model": "sonnet", "effort": "high"}
    dflt = runclass.core_cheap({}, t, "routine", "standard", "claude")
    assert (dflt["model"], dflt["effort"], dflt["class"]) == ("sonnet", "high", "routine")
    on = {"route": {"core_cheap": {"routine": True}}}
    own = runclass.core_cheap({"route": {"core_cheap": {"routine": {"model": "haiku", "effort": "low"}}}},
                              t, "routine", "standard", "claude")
    assert (own["model"], own["effort"]) == ("haiku", "low")
    got = runclass.core_cheap(on, t, "routine", "standard", "claude")
    assert (got["model"], got["effort"], got["class"], got["why"]) == ("sonnet", "high", "routine", "routine work")
    # Opus keeps: other tiers, pins (provider, model, effort), tier-up retries, urgent, deep asks and review fixes.
    assert runclass.core_cheap(on, t, "routine", "deep", "claude") is None
    assert runclass.core_cheap(on, t, "routine", "standard", "claude", None, {"by": "retry"}) is None
    for extra in ({"model": "opus"}, {"provider": "claude"}, {"effort": "xhigh"}, {"priority": 0},
                  {"labels": '["needs_deep"]'}, {"labels": '["user_deep"]'}, {"labels": '["review_fix:12"]'},
                  {"spec": "urgent: the user is waiting"}):
        assert runclass.core_cheap(on, {**t, **extra}, "routine", "standard", "claude") is None, extra
    # Off-switch: route.classes.routine core (or route.core_cheap.routine false).
    assert runclass.core_cheap({"route": {"core_cheap": {"routine": True}, "classes": {"routine": "core"}}}, t, "routine", "standard", "claude") is None
    assert runclass.core_cheap({"route": {"core_cheap": {"routine": False}}}, t, "routine", "standard", "claude") is None
    # It stays on core with a chat provider too (not a CHAT class).
    assert runclass.routes(_rc_cfg())["routine"] == "core"


def test_routine_quality_alarm_fires_when_routine_goes_wrong_ten_points_more_often_than_hard(env):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    cheap = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                             "class": "routine", "why": "routine work"}})

    def task(kind, status, result_status, note):
        t = p.db.add_task("t", "", kind=kind, tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status=?, result=? WHERE id=?", (status, json.dumps({"status": result_status}), t))
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet" if note else "opus", now - 600, "ok", 0.1, note))
    for i in range(6):   # hard: 1 of 6 needs review
        task("code", "done", "needs_review" if i == 0 else "done", None)
    for i in range(6):   # routine: 2 of 6 (33% vs 17%: 16 points) go wrong
        task("work", "failed" if i < 2 else "done", "failed" if i < 2 else "done", cheap)
    q = runclass.routine_quality(p.db, {}, now - 86400, now)
    assert (q["routine"]["bad"], q["routine"]["tasks"], q["hard"]["bad"], q["hard"]["tasks"]) == (2, 6, 1, 6)
    assert q["alarm"] and "route.classes.routine" in runclass.quality_text(q, 24)
    d = Daemon(p.base)
    d.watch_run_classes()
    d._run_class_checked = 0.0
    d.watch_run_classes()   # once per window
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='run_class_quality'")["n"] == 1
    # Within 10 points: quiet.
    p.db.x("UPDATE tasks SET status='done', result=? WHERE status='failed' AND id=(SELECT MAX(id) FROM tasks "
           "WHERE status='failed')", (json.dumps({"status": "done"}),))
    assert not runclass.routine_quality(p.db, {}, now - 86400, now)["alarm"]


def test_class_routes_default_to_chat_only_with_a_chat_model_and_never_move_the_coordinator():
    from ttp import runclass
    assert set(runclass.routes({}).values()) == {"core"}                 # no chat provider: nothing moves
    r = runclass.routes(_rc_cfg())
    assert {c for c, v in r.items() if v == "chat"} == set(runclass.CHAT)
    assert r["coordinator"] == r["hard"] == "core"
    r = runclass.routes(_rc_cfg(classes={"coordinator": "chat", "hard": "chat", "light": "core", "rebase": "x"}))
    assert r["coordinator"] == "core" and r["hard"] == "chat" and r["light"] == "core" and r["rebase"] == "chat"
    assert runclass.chat_model(_rc_cfg(), "standard") == "example-org/small-model"   # light's, for any tier
    assert runclass.chat_model({**_rc_cfg(), "core_provider": "chat"}, "light") == ""


def test_class_target_pins_and_kept_core_tasks_stay_on_core():
    from ttp import runclass
    cfg = _rc_cfg()
    t = {"kind": "question", "tier": "standard", "title": "q", "labels": "[]"}
    assert runclass.target(cfg, t, "standard", "status_report") == {
        "to": "chat", "model": "example-org/small-model", "rule": "class:status_report"}
    assert runclass.target(cfg, t, "standard", "hard") is None
    assert runclass.target({}, t, "standard", "status_report") is None
    assert runclass.target(cfg, {**t, "model": "pinned"}, "standard", "status_report") is None
    assert runclass.target(cfg, {**t, "provider": "claude"}, "standard", "status_report") is None
    assert runclass.target(cfg, t, "standard", "status_report", {"by": "retry"}) is None
    # Untrusted input (and the rest of route.KEEP_CORE) stays on core until its guards are live.
    assert runclass.target(cfg, {**t, "labels": '["untrusted_input"]'}, "standard", "status_report") is None
    assert runclass.target(cfg, {**t, "origin": "daemon"}, "standard", "status_report") is None
    assert runclass.target(cfg, {**t, "origin": "daemon", "kind": "review"}, "standard", "review_small")
    off = {"providers": {"chat": {**_RC_CHAT, "tiers": {"light": {"model": "m", "route": False}}}}}
    assert runclass.target(off, t, "light", "light") is None             # the user turned the tier off
    assert runclass.target(off, t, "standard", "status_report")["model"] == "m"


def test_a_question_and_a_wake_dispatch_to_the_chat_provider_by_class(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route=None)                                         # a model, no tier route
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slow", kind="question")
    assert prov == "chat" and note[runclass.KEY] == "status_report"
    assert note[route.KEY]["rule"] == "class:status_report" and note[route.KEY]["model"] == _RT_MODEL
    assert route.run_model(d.cfg, tier, note[route.KEY]) == (_RT_MODEL, "")


def test_stats_by_class_counts_runs_per_class_per_model_and_the_alarm_names_a_quiet_routed_class(env, capsys):
    from ttp import jevuse, runclass
    p = make(env)
    now = time.time()
    t = p.db.add_task("why", "", kind="question", tier="standard", priority=3, budget_usd=1.0)
    for i in range(6):
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "opus", now - 60 - i, "ok", 0.5, "{}"))
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (None, "coordinator", "claude", "opus", now - 30, "ok", 2.0, "{}"))
    for _ in range(5):
        jevuse.record(p.db, "wake_gate", {"task": t, "needed": 0.4}, 0.0, now=now - 10)
    s = runclass.by_class(p.db, _rc_cfg(), now - 3600, now)
    assert s["classes"]["status_report"] == {"claude:opus": {"runs": 6, "usd": 3.0}}
    assert s["classes"]["coordinator"]["claude:opus"]["runs"] == 1
    assert s["classes"]["wake_decision"] == {"jev": {"runs": 5, "usd": 0.0}}
    assert s["runs"] == 12 and s["share"] == round(11 / 12, 3)
    out = runclass.text(s)
    assert "status_report [chat]: claude:opus 6 (6.0/h) $3.00" in out and "coordinator [core]" in out
    assert {q["class"] for q in runclass.quiet_classes(s)} == {"status_report", "wake_decision"}
    assert "routed but none on chat: " in runclass.summary(s)
    # With no chat provider the class is still meant for a cheap core model: its Opus runs alarm, the wake
    # gate's own decisions (jev) do not. Quiet with cheap core models off, or once the class ran on chat.
    assert {q["class"] for q in runclass.quiet_classes(runclass.by_class(p.db, {}, now - 3600, now))} == {"status_report"}
    off = {"route": {"core_cheap": False}}
    assert runclass.quiet_classes(runclass.by_class(p.db, off, now - 3600, now)) == []
    jevuse.record(p.db, "wake_gate", {"task": t, "needed": 0.0, "model": "example-org/small-model"}, 0.0, now=now - 5)
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (t, "worker", "chat", "example-org/small-model", now - 5, "ok", 0.0, "{}"))
    assert runclass.quiet_classes(runclass.by_class(p.db, _rc_cfg(), now - 3600, now)) == []
    # The command.
    p.set_config("providers.chat", _RC_CHAT)
    from ttp import cli
    capsys.readouterr()
    cli.main(["stats", p.name, "--by-class", "--since", "1h"])
    out = capsys.readouterr().out
    assert "status_report [chat]" in out and "chat:example-org/small-model 1" in out, out


def test_deep_plan_and_device_runs_stay_hard_and_on_core_whatever_their_title_or_wake():
    from ttp import runclass
    cfg = _rc_cfg()
    wake = {"note": {"wake": {"tier": "light"}}}
    cases = [
        ({"kind": "code", "tier": "deep", "title": "push queue: stop the rebase step dropping commits"}, None),
        ({"kind": "code", "tier": "deep", "title": "x", "labels": '["rebase"]'}, None),
        ({"kind": "work", "tier": "standard", "title": "check the box", "spec": "power cycle the box, then soak 2 h"}, wake),
        ({"kind": "plan", "tier": "deep", "title": "plan"}, wake),
        ({"kind": "plan", "tier": "standard", "title": "plan"}, wake),
        ({"kind": "code", "tier": "standard", "title": "x"}, {"note": {"wake": {}, "tier": "deep"}}),
        ({"kind": "review", "tier": "deep", "title": "r"}, {"note": {"conflict_fix": {"tier": "light"}}}),
    ]
    for task, run in cases:
        assert runclass.classify(task, run, cfg) == "hard", task
        assert runclass.target(cfg, task, str(task["tier"]), runclass.classify(task, run, cfg)) is None, task
    # A push-conflict fix review below deep stays a rebase and routes to chat.
    r = {"kind": "review", "tier": "standard", "title": "r"}
    assert runclass.classify(r, {"note": {"conflict_fix": {"tier": "light"}}}, cfg) == "rebase"


def test_the_quiet_class_alarm_raises_one_coordinator_event_per_window_with_counts_and_last_error(env):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("providers.chat", _RC_CHAT)
    now = time.time()
    t = p.db.add_task("why", "", kind="question", tier="standard", priority=3, budget_usd=1.0)
    pinned = p.db.add_task("why pinned", "", kind="question", tier="standard", priority=3, budget_usd=1.0)
    p.db.x("UPDATE tasks SET provider='claude' WHERE id=?", (pinned,))
    for i in range(8):   # pinned runs never count toward the alarm
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (pinned, "worker", "claude", "opus", now - 60 - i, "ok", 0.5, "{}"))
    d = Daemon(p.base)
    events = lambda: p.db.q("SELECT text FROM events WHERE kind='run_class_quiet' AND status='queued'")
    d.watch_run_classes()
    assert events() == []
    note = '{"route": {"fallback": "timeout"}}'
    for i in range(5):
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "opus", now - 30 - i, "ok", 0.5, note))
    d._run_class_checked = 0.0
    d.watch_run_classes()
    rows = [r["text"] for r in events()]
    assert len(rows) == 1 and "status_report" in rows[0] and "5 eligible runs (13 in all)" in rows[0] \
        and "Last fallback: route fallback: timeout" in rows[0], rows
    d._run_class_checked = 0.0
    d.watch_run_classes()   # not repeated within the window
    assert len(events()) == 1
    # Quiet once the class ran on chat, and with no chat provider.
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (t, "worker", "chat", "example-org/small-model", now - 5, "ok", 0.0, "{}"))
    assert runclass.quiet_classes(runclass.by_class(p.db, _rc_cfg(), now - 3600, now + 1)) == []
    assert runclass.quiet_classes(runclass.by_class(p.db, {}, now - 3600, now + 1)) == []


def test_a_cheap_class_falls_back_to_a_cheap_claude_model_at_low_effort_and_back_to_tt_once_one_answers():
    from ttp import runclass
    t = {"kind": "question", "tier": "standard", "title": "why"}
    # No tt model configured (or none reachable): a cheap class takes Haiku or Sonnet at low effort, not Opus.
    c = runclass.core_cheap({}, t, "status_report", "standard", "claude")
    assert (c["model"], c["effort"], c["why"]) == ("sonnet", "low", "no chat model")
    assert runclass.core_cheap({}, t, "screening", "standard", "claude")["model"] == "haiku"
    assert runclass.core_cheap({}, t, "wake_decision", "standard", "claude")["model"] == "haiku"
    # The tt endpoint failed its probe (key refused, cooling): the same cheap model, naming why.
    cfg = _rc_cfg()
    fell = {"to": "chat", "fallback": "auth"}
    assert runclass.core_cheap(cfg, t, "status_report", "standard", "claude", fell)["why"] == "auth"
    # Once a tt endpoint answers with a working key the route has no fallback: the run goes to the tt model.
    ok = {"to": "chat", "fallback": "none", "model": "example-org/small-model"}
    assert runclass.core_cheap(cfg, t, "status_report", "standard", "claude", ok) is None
    # Never for the coordinator or hard work, other tiers, other providers, pinned or urgent tasks.
    for cls in ("coordinator", "hard", "light"):
        assert runclass.core_cheap({}, t, cls, "standard", "claude") is None, cls
    assert runclass.core_cheap({}, t, "status_report", "deep", "claude") is None
    assert runclass.core_cheap({}, t, "status_report", "light", "claude") is None
    assert runclass.core_cheap({}, t, "status_report", "standard", "codex") is None
    assert runclass.core_cheap({}, {**t, "model": "opus"}, "status_report", "standard", "claude") is None
    # An urgent task sets order only: its run takes the same cheap model (a wrong cheap run is retried a tier up).
    u = runclass.core_cheap({}, {**t, "priority": 0}, "status_report", "standard", "claude")
    assert (u["model"], u["effort"]) == ("sonnet", "low")
    assert runclass.core_cheap({}, t, "status_report", "standard", "claude", None, {"by": "retry"}) is None
    # Config: all off, one class off, one class's model changed, or the class kept on core.
    assert runclass.core_cheap({"route": {"core_cheap": False}}, t, "status_report", "standard", "claude") is None
    one = {"route": {"core_cheap": {"status_report": False, "rebase": {"model": "haiku", "effort": "medium"}}}}
    assert runclass.core_cheap(one, t, "status_report", "standard", "claude") is None
    assert runclass.core_cheap(one, t, "rebase", "standard", "claude")["effort"] == "medium"
    assert runclass.core_cheap({"route": {"classes": {"screening": "core"}}}, t, "screening", "standard", "claude") is None
    # The start applies it only to the provider it was chosen for.
    note = {runclass.CHEAP_KEY: c}
    assert runclass.cheap_run(note, "claude") == c and runclass.cheap_run(note, "chat") is None


def test_a_question_with_no_tt_model_dispatches_to_sonnet_low_and_to_the_tt_model_once_configured(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("core_provider", "claude")   # the cheap fallback is Claude's; make() bootstraps on "fake"
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slow", kind="question", tier="standard")
    assert prov == "claude" and note[runclass.KEY] == "status_report"
    assert note[runclass.CHEAP_KEY]["model"] == "sonnet" and note[runclass.CHEAP_KEY]["effort"] == "low"
    # A tt model appears: the next run goes there, with no edit to the class routes.
    _route_config(p, route=None)
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slower", kind="question", tier="standard")
    assert prov == "chat" and runclass.CHEAP_KEY not in note and note[route.KEY]["model"] == _RT_MODEL


def test_cheap_core_runs_keep_the_alarm_quiet_and_tt_unavailable_is_told_once(env, monkeypatch):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    t = p.db.add_task("why", "", kind="question", tier="standard", priority=3, budget_usd=1.0)
    cheap = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "low",
                                             "class": "status_report", "why": "no chat model"}})
    for i in range(6):
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet", now - 60 - i, "ok", 0.05, cheap))
    s = runclass.by_class(p.db, {}, now - 3600, now)
    assert s["cheap"] == {"status_report": 6} and runclass.quiet_classes(s) == []
    assert runclass.tt_unavailable(s) == ["status_report"]
    assert "move back by themselves" in runclass.tt_unavailable_text(s, ["status_report"])
    d = Daemon(p.base)
    told = []
    monkeypatch.setattr(d, "alert", lambda key, text, **k: told.append((key, k.get("severity"))))
    for _ in range(3):
        d._run_class_checked = 0.0
        d.watch_run_classes()
    assert told == [("tt-unavailable", "low")]
    # A tt model serves again: the flag clears, so the next outage is told again.
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (t, "worker", "chat", "example-org/small-model", now - 5, "ok", 0.0, "{}"))
    d._run_class_checked = 0.0
    d.watch_run_classes()
    assert not p.db.kv("tt_unavailable_told", "") and len(told) == 1


def test_a_cheap_class_run_starts_claude_on_its_cheap_model_at_its_effort_or_the_floors(env, monkeypatch):
    """The run note's cheap model (runclass.CHEAP_KEY) reaches the Claude command line, and a conclusion
    run's effort floor (min_effort) still lifts its low effort."""
    from ttp import runclass
    from ttp.daemon import Daemon
    from ttp.providers import claude as claude_provider

    class Built(Exception):
        pass
    real = claude_provider.Claude.build

    def build(self, **kw):   # the real command line; no agent process starts
        raise Built(real(self, **kw)[0])
    monkeypatch.setattr(claude_provider.Claude, "build", build)
    p = make(env)
    q = p.db.add_task("Why?", "x", kind="question", tier="standard", origin="coordinator")
    d = Daemon(p.base)

    def argv(model, **kw):
        note = {runclass.CHEAP_KEY: {"provider": "claude", "model": model, "effort": "low", "why": "no chat model"}}
        with pytest.raises(Built) as got:
            d.start_run("worker", "go", "claude", "standard", str(p.root), task=p.db.task(q), note=note, **kw)
        a = list(got.value.args[0])
        return a[a.index("--model") + 1], (a[a.index("--effort") + 1] if "--effort" in a else "")

    assert argv("sonnet") == ("sonnet", "low")
    assert argv("haiku", min_effort="high") == ("haiku", "high")


def test_an_urgent_task_takes_its_class_cheap_route_in_every_cheap_class_and_hard_work_keeps_core():
    from ttp import route, runclass
    cfg = _rc_cfg()
    t = {"kind": "question", "tier": "standard", "title": "why", "spec": "s"}
    urgent = [{**t, "priority": 1}, {**t, "spec": "urgent: yes\nwhy"}]
    for cls in ("wake_decision", "screening", "worker_wake", "status_report", "rebase", "review_small"):
        for u in urgent:
            assert route.kept_core(u, urgency=False) is False, (cls, u)
            assert runclass.routable(u, {runclass.KEY: cls}), (cls, u)
            assert runclass.target(cfg, u, "light", cls) is not None, (cls, u)
    # The decision (2026-11-08 23:55 PST) names those six: an urgent light-tier first run starts on core.
    for u in urgent:
        assert runclass.target(cfg, u, "light", "light") is None and not runclass.routable(u, {runclass.KEY: "light"})
        assert runclass.target(cfg, {**t, "tier": "light"}, "light", "light") is not None   # not urgent: routed
    for cls in ("wake_decision", "screening", "worker_wake", "status_report", "rebase", "review_small"):
        c = runclass.core_cheap({}, urgent[0], cls, "standard", "claude")
        assert c and c["effort"] == "low" and c["model"] in ("haiku", "sonnet"), cls
    # Urgency still keeps a non-cheap class (hard work, the coordinator) and an escalation on core.
    assert route.kept_core(urgent[0]) and route.kept_core(urgent[1])
    for cls in ("coordinator", "hard"):
        assert runclass.core_cheap({}, urgent[0], cls, "standard", "claude") is None, cls
        assert runclass.target(cfg, urgent[0], "standard", cls) is None, cls
    # A hard task (a needs_deep label) stays on core even in a cheap class.
    hard = {**t, "labels": ["needs_deep"]}
    assert route.kept_core(hard, urgency=False) and not runclass.routable(hard)


def test_an_urgent_status_report_dispatches_to_the_tt_model_and_an_urgent_hard_task_to_the_core_model(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route=None)
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slow", kind="question", priority=1)
    assert prov == "chat" and note[runclass.KEY] == "status_report", (prov, note)
    assert note[route.KEY]["model"] == _RT_MODEL
    # Urgent work of the hard class (a code task at the standard tier) keeps the core provider.
    p2 = make(env)
    _route_config(p2, route=None)
    d2 = Daemon(p2.base)
    got = _route_dispatch(p2, d2, monkeypatch, title="build it", kind="code", tier="standard", priority=1)
    assert got[0] == d2.cfg.get("core_provider", "claude")


def test_wake_keeps_effort_is_only_a_code_task_worker_wake_with_no_chat_reply():
    from ttp import tierfloor
    code = {"kind": "code"}
    assert tierfloor.wake_keeps_effort(code, "worker_wake") is True
    assert tierfloor.wake_keeps_effort(code, "status_report") is False
    assert tierfloor.wake_keeps_effort(code, "hard") is False
    assert tierfloor.wake_keeps_effort({"kind": "review"}, "worker_wake") is False
    assert tierfloor.wake_keeps_effort({"kind": "question"}, "worker_wake") is False
    assert tierfloor.wake_keeps_effort({**code, "reply_chat": "c1"}, "worker_wake") is False


def test_code_tasks_take_sonnet_by_kind_canary_then_full_and_opus_keeps_fixes_pins_and_urgent_work():
    from ttp import runclass
    t = {"id": 7, "kind": "code", "tier": "standard", "title": "add a flag", "labels": "[]"}
    assert runclass.classify(t) == "hard"   # accounting unchanged: the route is a cheap model, not a class
    assert runclass.code_kind(t) == "new"
    assert runclass.code_kind({**t, "labels": '["continues:5"]'}) == "continue"
    assert runclass.code_kind({**t, "labels": '["review_fix:9", "continues:5"]'}) == "review_fix"
    assert runclass.code_kind({**t, "tier": "deep"}) == runclass.code_kind({**t, "kind": "work"}) == ""
    # Canary: a stable share of task ids, the same answer on every run of a task.
    ids = range(1, 2001)
    picked = [i for i in ids if runclass.code_sonnet({}, {**t, "id": i})]
    assert 0.15 < len(picked) / 2000 < 0.25
    assert [i for i in ids if runclass.code_sonnet({}, {**t, "id": i})] == picked
    tid = picked[0]
    got = runclass.core_cheap({}, {**t, "id": tid}, "hard", "standard", "claude")
    assert (got["model"], got["effort"], got[runclass.CODE_KEY]) == ("sonnet", "high", "new") and "canary" in got["why"]
    other = next(i for i in ids if i not in picked)
    assert runclass.core_cheap({}, {**t, "id": other}, "hard", "standard", "claude") is None
    # Full: every task of the kind; off: none; a kind's state never moves another kind.
    full = {"new": "full"}
    assert runclass.core_cheap({}, {**t, "id": other}, "hard", "standard", "claude", None, None, full)["model"] == "sonnet"
    assert runclass.core_cheap({}, {**t, "id": tid}, "hard", "standard", "claude", None, None, {"new": "off"}) is None
    cont = {**t, "id": tid, "labels": '["continues:5"]'}
    assert runclass.core_cheap({}, cont, "hard", "standard", "claude", None, None, {"new": "off"})[runclass.CODE_KEY] == "continue"
    # Opus keeps review fixes, urgent, needs_deep, pins, tier-up retries, other tiers and device work.
    for extra in ({"labels": '["review_fix:9"]'}, {"spec": "urgent: the user is waiting"}, {"labels": '["needs_deep"]'},
                  {"model": "opus"}, {"provider": "claude"}, {"effort": "xhigh"},
                  {"spec": "run it on the device (tt-smi reset)"}):
        assert runclass.core_cheap({}, {**t, **extra}, "hard", "standard", "claude", None, None, full) is None, extra
    assert runclass.core_cheap({}, t, "hard", "standard", "claude", None, {"by": "retry"}, full) is None
    assert runclass.core_cheap({}, t, "hard", "deep", "claude", None, None, full) is None
    assert runclass.core_cheap({}, t, "hard", "standard", "codex", None, None, full) is None
    # Off-switches, a narrower kind list and the share.
    for cfg in ({"route": {"code_sonnet": False}}, {"route": {"core_cheap": False}},
                {"route": {"code_sonnet": {"kinds": ["continue"]}}}):
        assert runclass.core_cheap(cfg, t, "hard", "standard", "claude", None, None, full) is None, cfg
    assert runclass.code_sonnet({"route": {"code_sonnet": {"share": 0}}}, {**t, "id": tid}) is None
    assert runclass.code_sonnet({"route": {"code_sonnet": {"share": 1}}}, {**t, "id": other})


def test_code_quality_moves_a_kind_full_when_sonnet_matches_opus_and_back_to_opus_with_one_event(env):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()

    def task(sonnet, bad, labels="[]", review=True):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status='done', result=?, labels=? WHERE id=?",
               (json.dumps({"status": "done"}), labels, t))
        note = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                                "class": "hard", runclass.CODE_KEY: "new"}}) if sonnet else None
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet" if sonnet else "claude-opus-5-5", now - 600, "ok", 0.1, note))
        if review:   # its first review: changes asked when bad
            r = p.db.add_task(f"Review #{t}", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
            p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?",
                   ("failed" if bad else "done", json.dumps([f"auto_review:{t}"]), r))
        return t
    for i in range(10):   # Opus: 2 of 10 had changes asked
        task(False, i < 2)
    for i in range(runclass.CODE_FULL_MIN):   # Sonnet: 6 of 30 (20%: same as Opus)
        task(True, i < 6)
    task(True, False, review=False)   # no verdict yet: not counted
    q = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)
    assert (q["new"]["sonnet"]["tasks"], q["new"]["sonnet"]["bad"], q["new"]["opus"]["tasks"]) == (30, 6, 10)
    assert q["new"]["state"] == "full" and q["continue"]["state"] == "canary"
    d = Daemon(p.base)
    d.watch_run_classes()
    assert d._code_state() == {"new": "full", "continue": "canary"}
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='run_class_quality'")["n"] == 0
    # Sonnet goes wrong 10 more times: 16/40 (40%) vs 20%: back to Opus, told once.
    for _ in range(10):
        task(True, True)
    d._run_class_checked = d._code_quality_checked = 0.0
    d.watch_run_classes()
    d._run_class_checked = d._code_quality_checked = 0.0
    d.watch_run_classes()
    assert d._code_state()["new"] == "off"
    ev = p.db.q("SELECT text FROM events WHERE kind='run_class_quality'")
    assert len(ev) == 1 and "Code tasks (new)" in ev[0]["text"] and "route.code_sonnet" in ev[0]["text"]
    t = {"id": 1, "kind": "code", "tier": "standard", "title": "x", "labels": "[]"}
    assert runclass.core_cheap({}, t, "hard", "standard", "claude", None, None, d._code_state()) is None

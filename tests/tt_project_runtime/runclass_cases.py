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
             {"route": {"review_small_lines": 100}}) == "hard"
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
    for i in range(20):   # hard: 2 of 20 need review
        task("code", "done", "needs_review" if i < 2 else "done", None)
    for i in range(20):   # routine: 8 of 20 (40% vs 10%: 30 points, p<0.05) go wrong
        task("work", "failed" if i < 8 else "done", "failed" if i < 8 else "done", cheap)
    q = runclass.routine_quality(p.db, {}, now - 86400, now)
    assert (q["routine"]["bad"], q["routine"]["tasks"], q["hard"]["bad"], q["hard"]["tasks"]) == (8, 20, 2, 20)
    assert q["p"] < runclass.ALARM_P
    assert q["alarm"] and "route.classes.routine" in runclass.quality_text(q, 24)
    assert "p=" in runclass.quality_text(q, 24)
    d = Daemon(p.base)
    d.watch_run_classes()
    d._run_class_checked = 0.0
    d.watch_run_classes()   # once per window
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='run_class_quality'")["n"] == 1
    # Within 10 points: quiet.
    for _ in range(6):
        p.db.x("UPDATE tasks SET status='done', result=? WHERE status='failed' AND id=(SELECT MAX(id) FROM tasks "
               "WHERE status='failed')", (json.dumps({"status": "done"}),))
    assert not runclass.routine_quality(p.db, {}, now - 86400, now)["alarm"]


def test_routine_quality_alarm_needs_a_significant_difference_not_a_few_bad_runs(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    cheap = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                             "class": "routine", "why": "routine work"}})

    def task(kind, bad, note):
        t = p.db.add_task("t", "", kind=kind, tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status=?, result=? WHERE id=?",
               ("failed" if bad else "done", json.dumps({"status": "failed" if bad else "done"}), t))
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet" if note else "opus", now - 600, "ok", 0.1, note))
    for i in range(6):   # hard: 1 of 6; routine: 2 of 6 (16 points, but p~0.47): quiet
        task("code", i == 0, None)
        task("work", i < 2, cheap)
    q = runclass.routine_quality(p.db, {}, now - 86400, now)
    assert (q["routine"]["bad"], q["hard"]["bad"]) == (2, 1) and q["p"] > 0.4 and not q["alarm"]


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
    rv = {"route": {"classes": {"review_small": "chat"}}}
    assert runclass.target({**cfg, **rv}, {**t, "origin": "daemon", "kind": "review"}, "standard", "review_small")
    off = {"providers": {"chat": {**_RC_CHAT, "tiers": {"light": {"model": "m", "route": False}}}}}
    assert runclass.target(off, t, "light", "light") is None             # the user turned the tier off
    assert runclass.target(off, t, "standard", "status_report")["model"] == "m"


def test_a_status_report_dispatches_to_the_chat_provider_by_class(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    _route_config(p, route=None)                                         # a model, no tier route
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="write the weekly status digest", kind="work",
                                     tier="standard")
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
    assert (c["model"], c["effort"], c["why"]) == ("sonnet", "low", runclass.KEPT_CORE)
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


def test_a_question_with_no_tt_model_dispatches_to_sonnet_low_and_to_the_tt_model_once_glm_work_covers_it(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    p.set_config("core_provider", "claude")   # the cheap fallback is Claude's; make() bootstraps on "fake"
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slow", kind="question", tier="standard")
    assert prov == "claude" and note[runclass.KEY] == "status_report"
    assert note[runclass.CHEAP_KEY]["model"] == "sonnet" and note[runclass.CHEAP_KEY]["effort"] == "low"
    # A tt model appears: a question is a conclusion floor run, so the class route keeps it on core (every
    # keep-core check runs before a class route); on a project the GLM work route covers, it goes to the model.
    from ttp import configwho, glmwork
    _route_config(p, route=None)
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slower", kind="question", tier="standard")
    assert prov == "claude" and route.KEY not in note
    p.set_config("route.glm_work", {"on": True, "model": _RT_MODEL, "holdout": 0}, by=configwho.by("user", via="cli"))
    d = Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="why is it slowest", kind="question", tier="standard")
    assert prov == "chat" and runclass.CHEAP_KEY not in note and note[route.KEY]["model"] == _RT_MODEL
    assert note[route.KEY][glmwork.KEY] == "glm"


def test_cheap_core_runs_keep_the_alarm_quiet_and_tt_unavailable_is_told_once(env, monkeypatch):
    from ttp import route, runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    t = p.db.add_task("why", "", kind="question", tier="standard", priority=3, budget_usd=1.0)
    cheap = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "low",
                                             "class": "status_report", "why": "busy_wait_expired"},
                        route.KEY: {"to": "claude", "fallback": "busy_wait_expired"}})
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


def test_tt_unavailable_counts_only_cheap_runs_whose_route_failed_on_the_endpoint(env):
    """A chat-class run a keep-core rule held on Claude (a code arm, urgent or protected work, the floor: no
    route asked, or an escalation or rerun) is no sign the tt model is down; a route fault or full slots are."""
    from ttp import route, runclass
    p = make(env)
    now = time.time()
    t = p.db.add_task("why", "", kind="question", tier="standard", priority=3, budget_usd=1.0)

    def run(routed):
        note = {runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "low", "class": "status_report",
                                     "why": (routed or {}).get("fallback") or runclass.KEPT_CORE}}
        if routed:
            note[route.KEY] = routed
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet", now - 60, "ok", 0.05, json.dumps(note)))

    run(None)
    run({"to": "claude", "fallback": route.ESCALATED})
    run({"to": "claude", "fallback": "rerun"})
    s = runclass.by_class(p.db, {}, now - 3600, now)
    assert s["cheap"] == {"status_report": 3}
    assert runclass.tt_unavailable(s) == [] and s["last_error"] == {}
    for fault in ("down", "auth", "busy_wait_expired"):
        assert runclass.endpoint_fault(fault)
    run({"to": "claude", "fallback": "down"})
    s = runclass.by_class(p.db, {}, now - 3600, now)
    assert runclass.tt_unavailable(s) == ["status_report"]
    assert "route fallback: down" in runclass.tt_unavailable_text(s, ["status_report"])


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


def test_a_code_tasks_cheap_class_route_keeps_sonnet_at_high_and_other_kinds_keep_it_low():
    """The cheap classes' Sonnet low is for non-code work: a code task's wake, status, rebase and routine
    run takes the model at CODE_EFFORT_MIN (a light wake hop of a waiting code task ran Sonnet low)."""
    from ttp import runclass
    code = {"id": 3, "kind": "code", "tier": "standard", "title": "x", "labels": "[]", "spec": "s"}
    other = {**code, "kind": "work"}
    assert runclass.CODE_EFFORT_MIN == "high"
    for cls in ("worker_wake", "status_report", "rebase", runclass.ROUTINE):
        c = runclass.core_cheap({}, code, cls, "standard", "claude")
        o = runclass.core_cheap({}, other, cls, "standard", "claude")
        assert c and o and c["model"] == o["model"] == "sonnet", cls
        assert c["effort"] == "high" and o["effort"] == ("high" if cls == runclass.ROUTINE else "low"), cls
    for cls in ("wake_decision", "screening"):   # Haiku classes are not Sonnet: untouched
        assert runclass.core_cheap({}, code, cls, "standard", "claude")["effort"] == "low", cls
    for eff, want in (("minimal", "high"), ("low", "high"), ("medium", "high"), ("high", "high"),
                      ("xhigh", "xhigh"), ("max", "max"), ("", "high")):
        assert runclass.code_effort(code, "claude-sonnet-5-5", eff) == want, eff
    assert runclass.code_effort(code, "opus", "low") == "low" and runclass.code_effort(other, "sonnet", "low") == "low"
    assert runclass.code_effort(None, "sonnet", "low") == "low"


def test_a_code_run_on_sonnet_starts_at_high_by_every_route_and_a_non_code_run_keeps_its_effort(env, monkeypatch):
    """Cheap wake class, a routed model pick, a Jev or A/B effort arm, the tier's own mapping, a task pin
    and a review fix or continuation: none starts a code task's Sonnet run below high."""
    from ttp import effortab, route, runclass
    from ttp.daemon import Daemon
    from ttp.providers import claude as claude_provider

    class Built(Exception):
        pass
    real = claude_provider.Claude.build

    def build(self, **kw):   # the real command line; no agent process starts
        raise Built(real(self, **kw)[0])
    monkeypatch.setattr(claude_provider.Claude, "build", build)
    p = make(env)
    d = Daemon(p.base)

    def argv(kind="code", note=None, title="x", labels=None, **fields):
        t = p.db.add_task(title, "x", kind=kind, tier="standard", origin="coordinator", **fields)
        if labels:
            p.db.update_task(t, labels=json.dumps(labels))
        with pytest.raises(Built) as got:
            d.start_run("worker", "go", "claude", "standard", str(p.root), task=p.db.task(t), note=note)
        a = list(got.value.args[0])
        return a[a.index("--model") + 1], (a[a.index("--effort") + 1] if "--effort" in a else "")

    low = {runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "low", "why": "no chat model"}}
    assert argv(note=low) == ("sonnet", "high") and argv(kind="work", note=low) == ("sonnet", "low")
    # A note whose cheap entry predates the floor (a restart reads the stored note) is lifted at the start.
    assert argv(note={runclass.CHEAP_KEY: {**low[runclass.CHEAP_KEY], "class": "worker_wake"}}) == ("sonnet", "high")
    # A routed model pick (route.KEY with model_pick) on core: Jev's or the A/B choice of Sonnet low.
    pick = {route.KEY: {"to": "claude", "model_pick": True, "model": "sonnet", "pick_effort": "low"}}
    assert argv(note=pick) == ("sonnet", "high") and argv(kind="question", note=pick) == ("sonnet", "low")
    # Jev's effort pick on a tier whose model is Sonnet.
    p.set_config("providers.claude.tiers.standard", {"model": "sonnet", "effort": "medium"})
    d._load_config()
    assert argv() == ("sonnet", "high") and argv(kind="question") == ("sonnet", "medium")
    arm = {effortab.KEY: {"effort": "low", "arm": "low"}}
    assert argv(note=arm) == ("sonnet", "high") and argv(kind="question", note=arm) == ("sonnet", "low")
    # A task pin (a review fix or continuation inherits one) and the continuation and review-fix titles.
    assert argv(model="sonnet", effort="low") == ("sonnet", "high")
    assert argv(title="continue: x", labels=["review_fix:9"], effort="low", model="sonnet") == ("sonnet", "high")
    # Opus keeps whatever effort its route gave it.
    assert argv(model="opus", effort="low") == ("opus", "low")


def test_a_review_run_starts_on_opus_whatever_the_tier_map_or_its_sizing_and_a_review_over_the_small_size_is_hard(env, monkeypatch):
    """A re-review sized light, on a project whose light tier maps to Sonnet low, and a standard-tier review
    over the small size both start on Opus at high; the latter is of class hard (never routine) with no cheap
    pick in its run note; so does a review pinned to Sonnet."""
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
    p.set_config("providers.claude.tiers.light", {"model": "sonnet", "effort": "low"})
    d = Daemon(p.base)

    def argv(kind="review", role="reviewer", tier="light", note=None, **fields):
        t = p.db.add_task("Review #1: x", "x", kind=kind, tier=tier, origin="daemon", **fields)
        with pytest.raises(Built) as got:
            d.start_run(role, "go", "claude", tier, str(p.root), task=p.db.task(t), note=note)
        a = list(got.value.args[0])
        return a[a.index("--model") + 1], (a[a.index("--effort") + 1] if "--effort" in a else "")

    assert argv() == ("opus", "high")
    review = p.db.task(p.db.add_task("Review #2: x", "x", kind="review", tier="standard", origin="daemon"))
    note = {"review_tier": {"lines": runclass.SMALL_LINES + 50}}
    assert runclass.classify(review, {"note": note}) == "hard"
    cheap = runclass.core_cheap({**d.cfg, "core_provider": "claude"}, review, "hard", "standard", "claude")
    assert cheap is None and argv(tier="standard", note=note) == ("opus", "high")
    run_note = {**note, **({runclass.CHEAP_KEY: cheap} if cheap else {})}   # as the dispatch records it
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
           (review["id"], "reviewer", "claude", "opus", time.time() - 60, "ok", 0.1, json.dumps(run_note)))
    stats = runclass.by_class(p.db, d.cfg, time.time() - 3600)
    assert set(stats["classes"]) == {"hard"} and not stats["cheap"]
    assert argv(model="sonnet", effort="low") == ("opus", "high")
    assert argv(model="opus", effort="max") == ("opus", "max")
    assert argv(kind="question", role="worker") == ("sonnet", "low"), "other kinds keep the tier map"
    assert runclass.review_model("claude-fable-5-1", "xhigh") == ("claude-fable-5-1", "xhigh")
    assert runclass.review_model("haiku", "") == ("opus", "high")


def test_an_urgent_task_takes_its_class_cheap_route_in_every_cheap_class_and_hard_work_keeps_core():
    from ttp import route, runclass
    cfg = _rc_cfg()
    t = {"kind": "question", "tier": "standard", "title": "why", "spec": "s"}
    urgent = [{**t, "priority": 1}, {**t, "spec": "urgent: yes\nwhy"}]
    for cls in runclass.ORDER_ONLY:
        for u in urgent:
            assert route.kept_core(u, urgency=False) is False, (cls, u)
            assert runclass.routable(u, {runclass.KEY: cls}), (cls, u)
            assert runclass.target(cfg, u, "light", cls) is not None, (cls, u)
    assert runclass.ORDER_ONLY == ("wake_decision", "screening", "worker_wake", "status_report", "rebase")
    # An urgent light-tier first run starts on core, and so does an urgent review (reviews always stay on core).
    for u in urgent:
        assert runclass.target(cfg, u, "light", "light") is None and not runclass.routable(u, {runclass.KEY: "light"})
        assert not runclass.routable(u, {runclass.KEY: "review_small"})
        assert runclass.target(cfg, {**t, "tier": "light"}, "light", "light") is not None   # not urgent: routed
    for cls in ("wake_decision", "screening", "worker_wake", "status_report", "rebase"):
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
    # A cheap class is ordered by urgency, never moved to core by it (a question would stay on core as a floor run).
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="write the weekly status digest", kind="work",
                                     tier="standard", priority=1)
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


def test_code_tasks_take_sonnet_by_kind_canary_then_full_and_opus_keeps_pins_and_device_runs():
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
    # Opus keeps needs_deep, pins, tier-up retries, other tiers and device runs (by resource, not by words).
    for extra in ({"labels": '["needs_deep"]'}, {"model": "opus"}, {"provider": "claude"}, {"effort": "xhigh"},
                  {"labels": '["needs_device"]'}, {"labels": '["resource:box1-device"]'}):
        assert runclass.core_cheap({}, {**t, **extra}, "hard", "standard", "claude", None, None, full) is None, extra
    # Urgent code and a spec that only talks about devices take the route.
    for extra in ({"spec": "urgent: the user is waiting", "priority": 1},
                  {"spec": "run it on the device (tt-smi reset)"}, {"spec": "No device run in this task."}):
        assert runclass.core_cheap({}, {**t, **extra}, "hard", "standard", "claude", None, None, full), extra
    assert runclass.core_cheap({}, t, "hard", "standard", "claude", None, {"by": "retry"}, full) is None
    assert runclass.core_cheap({}, t, "hard", "deep", "claude", None, None, full) is None
    assert runclass.core_cheap({}, t, "hard", "standard", "codex", None, None, full) is None
    # Off-switches, a narrower kind list and the share.
    for cfg in ({"route": {"code_sonnet": False}}, {"route": {"core_cheap": False}},
                {"route": {"code_sonnet": {"kinds": ["continue"]}}}):
        assert runclass.core_cheap(cfg, t, "hard", "standard", "claude", None, None, full) is None, cfg
    assert runclass.code_sonnet({"route": {"code_sonnet": {"share": 0}}}, {**t, "id": tid}) is None
    assert runclass.code_sonnet({"route": {"code_sonnet": {"share": 1}}}, {**t, "id": other})


def test_review_fixes_take_the_code_sonnet_route_with_their_own_state_and_opus_keeps_late_rounds():
    from ttp import runclass
    fix = {"id": 7, "kind": "code", "tier": "standard", "title": "Fix review #9: add a flag",
           "labels": '["continues:5", "review_fix:9", "review_round:1"]'}
    assert runclass.code_kind(fix) == "review_fix" and "review_fix" in runclass.CODE_KINDS
    # Canary: the same stable share of ids as the other kinds; full: every one; off: none.
    ids = range(1, 2001)
    picked = [i for i in ids if runclass.code_sonnet({}, {**fix, "id": i})]
    assert 0.15 < len(picked) / 2000 < 0.25
    got = runclass.core_cheap({}, {**fix, "id": picked[0]}, "hard", "standard", "claude")
    assert (got["model"], got["effort"], got[runclass.CODE_KEY]) == ("sonnet", "high", "review_fix")
    full = {"review_fix": "full"}
    assert runclass.core_cheap({}, fix, "hard", "standard", "claude", None, None, full)[runclass.CODE_KEY] == "review_fix"
    # Its own state: the other kinds' states never move it, and its own never moves them.
    assert runclass.core_cheap({}, fix, "hard", "standard", "claude", None, None,
                               {"new": "full", "continue": "full", "review_fix": "off"}) is None
    new = {**fix, "id": next(i for i in ids if i not in picked), "labels": "[]", "title": "add a flag"}
    assert runclass.core_cheap({}, new, "hard", "standard", "claude", None, None, {"review_fix": "full"}) is None
    # A fix after two failed reviews stays on Opus, by its round label or by the review_fix labels it carries.
    assert runclass.review_round(fix) == 1 and runclass.review_round(new) == 0
    for labels in ('["continues:5", "review_fix:9", "review_round:2"]', '["review_fix:9", "review_fix:11"]'):
        assert runclass.review_round({**fix, "labels": labels}) == 2
        assert runclass.core_cheap({}, {**fix, "labels": labels}, "hard", "standard", "claude", None, None, full) is None
    # Opus keeps needs_deep, user_deep, pins, tier-up retries, deep and device fixes; urgent fixes take it.
    assert runclass.core_cheap({}, {**fix, "spec": "urgent: the user is waiting"}, "hard", "standard", "claude",
                               None, None, full)[runclass.CODE_KEY] == "review_fix"
    for extra in ({"model": "opus"}, {"provider": "claude"}, {"effort": "xhigh"},
                  {"labels": '["review_fix:9", "needs_device"]'},
                  {"labels": '["review_fix:9", "needs_deep"]'}, {"labels": '["review_fix:9", "user_deep"]'}):
        assert runclass.core_cheap({}, {**fix, **extra}, "hard", "standard", "claude", None, None, full) is None, extra
    assert runclass.core_cheap({}, fix, "hard", "standard", "claude", None, {"by": "retry"}, full) is None
    assert runclass.core_cheap({}, fix, "hard", "deep", "claude", None, None, full) is None
    # The daemon makes every review fix (origin daemon) from a review of this project's own diff: routed.
    # An untrusted_input label the fixed work passed on, or a PR fix (which reads PR comments), stays on Opus.
    assert runclass.core_cheap({}, {**fix, "origin": "daemon"}, "hard", "standard", "claude", None, None,
                               full)[runclass.CODE_KEY] == "review_fix"
    for labels in ('["review_fix:9", "untrusted_input"]', '["review_fix:9", "pr_fix:40"]'):
        assert runclass.core_cheap({}, {**fix, "origin": "daemon", "labels": labels}, "hard", "standard", "claude",
                                   None, None, full) is None, labels
    assert runclass.core_cheap({}, {**new, "origin": "daemon"}, "hard", "standard", "claude", None, None,
                               {"new": "full"}) is None   # other daemon-made code work stays on Opus
    # The review_fix label still keeps it off the routine and chat routes.
    assert runclass.core_cheap({}, {**fix, "kind": "work"}, "routine", "standard", "claude") is None
    # Off-switches and a narrower kind list.
    for cfg in ({"route": {"code_sonnet": False}}, {"route": {"core_cheap": False}},
                {"route": {"code_sonnet": {"kinds": ["new", "continue"]}}}):
        assert runclass.core_cheap(cfg, fix, "hard", "standard", "claude", None, None, full) is None, cfg
    assert runclass.core_cheap({"route": {"code_sonnet": {"kinds": ["review_fix"]}}}, fix, "hard", "standard",
                               "claude", None, None, full)[runclass.CODE_KEY] == "review_fix"


def test_review_fix_quality_has_its_own_alarm_judged_by_the_first_run_and_its_first_re_review(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    fix_labels = '["continues:1", "review_fix:2", "review_round:1"]'

    def task(sonnet, bad, escalated=False):
        t = p.db.add_task("Fix review #2: x", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status='done', result=?, labels=? WHERE id=?",
               (json.dumps({"status": "done"}), fix_labels, t))
        note = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                                "class": "hard", runclass.CODE_KEY: "review_fix"}}) if sonnet else None
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet" if sonnet else "claude-opus-5-5", now - 900,
                "failed" if escalated else "ok", 0.1, note))
        if escalated:   # retried a tier up on Opus, which passed: still bad, on the side it started on
            p.db.x("UPDATE tasks SET tier='deep', tier_source='retry_tier_up' WHERE id=?", (t,))
            p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd) VALUES(?,?,?,?,?,?,?)",
                   (t, "worker", "claude", "claude-opus-5-5", now - 600, "ok", 0.5))
        r = p.db.add_task(f"Re-review #2: x", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?",
               ("failed" if bad else "done", json.dumps([f"auto_review:{t}", "continues:2"]), r))
    for i in range(10):   # Opus: 2 of 10 re-reviews failed
        task(False, i < 2)
    for i in range(runclass.CODE_FULL_MIN):   # Sonnet: 6 of 30, same 20%
        task(True, i < 6)
    q = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)
    v = q["review_fix"]
    assert (v["sonnet"]["tasks"], v["sonnet"]["bad"], v["opus"]["tasks"], v["opus"]["bad"]) == (30, 6, 10, 2)
    assert v["state"] == "full" and q["new"]["state"] == q["continue"]["state"] == "canary"
    for _ in range(12):   # twelve Sonnet fixes fail and pass only on an Opus retry a tier up: 18/42 (43%) vs 20%
        task(True, False, escalated=True)
    v = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["review_fix"]
    assert (v["sonnet"]["tasks"], v["sonnet"]["bad"], v["opus"]["tasks"]) == (42, 18, 10)
    assert v["p"] < runclass.ALARM_P and v["state"] == "off"
    assert "Code tasks (review_fix)" in runclass.code_quality_text("review_fix", v, 168)


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
    assert d._code_state() == {"new": "full", "continue": "canary", "review_fix": "canary"}
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


def test_worse_p_is_the_one_sided_binomial_tail_against_the_smoothed_opus_share():
    from ttp import runclass
    assert abs(runclass.worse_p(2, 5, 76, 283) - 0.41) < 0.01
    assert runclass.worse_p(5, 5, 76, 283) < 0.002
    assert runclass.worse_p(0, 5, 0, 5) == runclass.worse_p(1, 0, 0, 0) == 1.0
    assert runclass.worse_p(1, 5, 0, 5) > 0.5          # one miss against a clean small Opus sample: not significant
    assert runclass.alarm_p({}) == runclass.ALARM_P == 0.05
    assert runclass.alarm_p({"route": {"code_sonnet": {"alarm_p": 0.01}}}) == 0.01
    for bad in (True, "x", -1, 2, None):
        assert runclass.alarm_p({"route": {"code_sonnet": {"alarm_p": bad}}}) == runclass.ALARM_P, bad


def test_code_quality_needs_a_significant_difference_to_move_a_kind_off_and_recovers(env):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    note = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                            "class": "hard", runclass.CODE_KEY: "new"}})
    sonnet_ids = []

    def task(sonnet, bad):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status='done', result=?, labels='[]' WHERE id=?", (json.dumps({"status": "done"}), t))
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", "claude", "sonnet" if sonnet else "claude-opus-5-5", now - 600, "ok", 0.1,
                note if sonnet else None))
        r = p.db.add_task(f"Review #{t}", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?",
               ("failed" if bad else "done", json.dumps([f"auto_review:{t}"]), r))
        if sonnet:
            sonnet_ids.append((t, r))
    for i in range(283):   # Opus: 76 of 283 had changes asked (27%)
        task(False, i < 76)
    for i in range(5):     # Sonnet: 2 of 5 (40%): 13 points worse, but p~0.41: stays a canary
        task(True, i < 2)
    v = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["new"]
    assert (v["sonnet"]["bad"], v["sonnet"]["tasks"], v["opus"]["bad"], v["opus"]["tasks"]) == (2, 5, 76, 283)
    assert abs(v["p"] - 0.41) < 0.01 and v["state"] == "canary"
    # A kind latched off by an older rule recovers at the next hourly recompute.
    p.db.set_kv(runclass.CODE_STATE_KV, json.dumps({"new": "off", "continue": "canary", "review_fix": "canary"}))
    d = Daemon(p.base)
    d._run_class_checked = d._code_quality_checked = 0.0
    d.watch_run_classes()
    assert d._code_state()["new"] == "canary"
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='run_class_quality'")["n"] == 0
    # 5 of 5 bad (p~0.0014): off, told once with the counts, p and how to turn it back on.
    for t, r in sonnet_ids[2:]:
        p.db.x("UPDATE tasks SET status='failed' WHERE id=?", (r,))
    v = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["new"]
    assert v["sonnet"]["bad"] == 5 and v["p"] < 0.002 and v["state"] == "off"
    # alarm_p 0: never off.
    assert runclass.code_quality(p.db, {"route": {"code_sonnet": {"alarm_p": 0}}}, now - runclass.CODE_WINDOW_S,
                                 now)["new"]["state"] == "canary"
    d._run_class_checked = d._code_quality_checked = 0.0
    d.watch_run_classes()
    assert d._code_state()["new"] == "off"
    ev = p.db.q("SELECT text FROM events WHERE kind='run_class_quality'")
    assert len(ev) == 1
    for s in ("Code tasks (new)", "5/5", "76/283", "p=0.0014", '"alarm_p": 0', "route.code_sonnet` false"):
        assert s in ev[0]["text"], s
    # The sample stops being significant (3 of the 5 pass after all): back on at the next hourly check.
    for t, r in sonnet_ids[2:]:
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (r,))
    d._run_class_checked = d._code_quality_checked = 0.0
    d.watch_run_classes()
    assert d._code_state()["new"] == "canary"


def test_code_quality_counts_a_task_retried_a_tier_up_as_bad_on_the_model_it_started_on(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    sonnet_note = json.dumps({runclass.CHEAP_KEY: {"provider": "claude", "model": "sonnet", "effort": "high",
                                                   "class": "hard", runclass.CODE_KEY: "new"}})

    def task(sonnet, escalated=False, review_bad=False, review=True):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        if escalated:   # its first run failed; the retry ran a tier up on Opus and passed
            p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
                   (t, "worker", "claude", "sonnet" if sonnet else "claude-opus-5-5", now - 900, "failed", 0.1,
                    sonnet_note if sonnet else None))
            p.db.x("UPDATE tasks SET tier='deep', tier_source='retry_tier_up' WHERE id=?", (t,))
        p.db.x("UPDATE tasks SET status='done', result=? WHERE id=?", (json.dumps({"status": "done"}), t))
        if not escalated:
            p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
                   (t, "worker", "claude", "sonnet" if sonnet else "claude-opus-5-5", now - 600, "ok", 0.1,
                    sonnet_note if sonnet else None))
        else:
            p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd) VALUES(?,?,?,?,?,?,?)",
                   (t, "worker", "claude", "claude-opus-5-5", now - 600, "ok", 0.5))
        if review:
            r = p.db.add_task(f"Review #{t}", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
            p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?",
                   ("failed" if review_bad else "done", json.dumps([f"auto_review:{t}"]), r))
        return t

    task(True, escalated=True)    # Sonnet failed, Opus retry passed review: one bad Sonnet task, no Opus one
    task(False, escalated=True)   # same on Opus: one bad Opus task
    task(True, escalated=True, review=False)   # bad without a verdict: still known
    q = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["new"]
    assert (q["sonnet"]["tasks"], q["sonnet"]["bad"]) == (2, 2)
    assert (q["opus"]["tasks"], q["opus"]["bad"]) == (1, 1)
    # A light task raised to standard by a retry is no standard task: not counted on Opus.
    t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
    p.db.x("UPDATE tasks SET tier_source='retry_tier_up', status='done' WHERE id=?", (t,))
    p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd) VALUES(?,?,?,?,?,?,?)",
           (t, "worker", "claude", "claude-opus-5-5", now - 600, "failed", 0.1))
    assert runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["new"]["opus"]["tasks"] == 1
    # Escalations alone take a kind off: Opus 1 bad of 6, Sonnet 1 clean review + 4 escalations of 5.
    for i in range(5):
        task(False)
    task(True)
    for i in range(2):
        task(True, escalated=True)
    q = runclass.code_quality(p.db, {}, now - runclass.CODE_WINDOW_S, now)["new"]
    assert (q["sonnet"]["tasks"], q["sonnet"]["bad"], q["opus"]["tasks"], q["opus"]["bad"]) == (5, 4, 6, 1)
    assert q["state"] == "off"


def test_reviews_run_on_their_tiers_core_model_in_every_run_class(env):
    """A review judges work: it is neither routed to the chat provider nor put on the cheap core fallback,
    so every run of it, in every run class (a push-conflict fix's rebase run too), runs on its tier's core
    model, even when route.classes sends every class to the chat provider."""
    from ttp import daemon, runclass
    cfg = _rc_cfg()
    assert "review_small" not in runclass.CHAT and "review_small" not in runclass.CORE_CHEAP
    assert runclass.routes(cfg)["review_small"] == "core" and runclass.routes(cfg)["status_report"] == "chat"
    r = {"kind": "review", "tier": "standard", "title": "Review #1: x", "labels": "[]"}
    assert runclass.target(cfg, r, "standard", "review_small") is None
    for cls in runclass.CLASSES:   # no cheap class or cheap config moves a review off its Opus model
        assert runclass.core_cheap({}, r, cls, "standard", "claude") is None, cls
        assert runclass.core_cheap({"route": {"core_cheap": {cls: {"model": "haiku"}}}},
                                   r, cls, "standard", "claude") is None, cls
    user = {**cfg, "route": {"classes": {c: "chat" for c in runclass.CLASSES}}}
    assert runclass.routes(user)["review_small"] == "chat"
    p = make(env)
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.p, d.cfg, d._model_pick = p, user, lambda *a, **k: None
    review = p.db.task(p.db.add_task("Review #1: x", "check it", kind="review", tier="standard", priority=3))
    work = p.db.task(p.db.add_task("work task", "do it", kind="work", tier="standard", priority=3))
    assert d._route(work, "standard", None, 1.0, cls="rebase")["to"] == "chat"
    for cls in runclass.CLASSES:
        assert d._route(review, "standard", None, 1.0, cls=cls) is None, cls


def test_code_sonnet_and_core_cheap_are_project_and_account_keys_the_project_value_layers_over(env):
    from ttp import cli, runclass
    from ttp.project import layered, unknown_key_hint
    p = make(env)
    for k in ("route.code_sonnet", "route.core_cheap", "route.code_sonnet.share", "route.code_sonnet.guard_pts"):
        assert unknown_key_hint(k) is None, k
    assert unknown_key_hint("route.nope") is not None
    assert cli.main(["config", "--account", "route.code_sonnet", '{"share": 1.0, "guard_pts": 8}']) in (None, 0)
    assert cli.main(["config", "--account", "route.core_cheap", "true"]) in (None, 0)
    cfg = p.config()
    assert cfg["route"]["code_sonnet"] == {"share": 1.0, "guard_pts": 8} and cfg["route"]["core_cheap"] is True
    # The project's value layers over the account's: its keys win, the account's others stay.
    assert cli.main(["config", p.name, "route.code_sonnet", '{"guard_pts": 3}']) in (None, 0)
    cfg = p.config()
    assert cfg["route"]["code_sonnet"] == {"share": 1.0, "guard_pts": 3}
    assert runclass.guard_settings(cfg) == {"pts": 3.0}
    assert layered({"route": {"code_sonnet": False}})["route"]["code_sonnet"] is False
    # Share 1: every code task takes Sonnet; a review is no code kind and is never routed.
    for i in range(1, 30):
        t = {"id": i, "kind": "code", "tier": "standard", "title": "x", "labels": "[]"}
        assert runclass.code_sonnet(cfg, t)["model"] == runclass.CODE_MODEL["model"]
    assert runclass.code_sonnet(cfg, {"id": 1, "kind": "review", "tier": "standard", "title": "x",
                                      "labels": "[]"}) is None


def _guard_tasks(p, now, n, fails, ago=3600.0, landed=0, spent=1.0, model="opus", effort="high", note=None,
                 cost=None):
    """`n` finished code tasks `ago` seconds before `now`, the first `fails` with a failed first review and the
    first `landed` recorded as landed; each spent `spent`. Each has one worker run on `model` at `effort` (none
    when `model` is None) before its review; `cost` is that run's $ (default `spent`), `note` its note."""
    out = []
    for i in range(n):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=5.0)
        p.db.x("UPDATE tasks SET status='done', spent_usd=?, updated=? WHERE id=?", (spent, now - ago, t))
        if model:
            p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd, note) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (t, "worker", "claude", model, effort, now - ago - 600,
                                                    now - ago - 300, "done", spent if cost is None else cost,
                                                    json.dumps(note or {})))
        r = p.db.add_task(f"Review #{t}", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?",
               ("failed" if i < fails else "done", json.dumps([f"auto_review:{t}"]), r))
        if i < landed:
            p.db.x("INSERT INTO task_landings(marker,task,sha,target,ts) VALUES(?,?,?,?,?)",
                   (f"m{t}", t, f"{t:040x}", "branch", now - ago))
        out.append(t)
    return out


_GUARD_CFG = {"route": {"code_sonnet": {"share": 1.0}}}


def test_code_route_guard_compares_sonnet_high_with_opus_high_in_one_window_and_leaves_the_rest_out(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    _guard_tasks(p, now, 20, 3, model="sonnet")            # Sonnet high: 3 of 20 failed (15%)
    _guard_tasks(p, now, 24, 3, model="opus")              # Opus high: 3 of 24 (13%)
    _guard_tasks(p, now, 3, 2, model="sonnet", effort="low")   # Sonnet low: 2 of 3 failed
    _guard_tasks(p, now, 4, 3, model=None)                 # reviews no worker run traces: 3 of 4 failed
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    m, x = row["day"]["models"], row["day"]["excluded"]
    assert (m["sonnet_high"]["reviewed"], m["sonnet_high"]["review_fails"]) == (20, 3)
    assert (m["opus_high"]["reviewed"], m["opus_high"]["review_fails"]) == (24, 3)
    assert (x["sonnet_other"]["reviewed"], x["sonnet_other"]["review_fails"]) == (3, 2)
    assert (x["untraced"]["reviewed"], x["untraced"]["review_fails"]) == (4, 3)
    assert row["enough"] and row["gap_pts"] == 2.5 and not row["tripped"] and row["comparison"] == "compared"
    assert (row["day"]["reviewed"], row["day"]["review_fails"]) == (51, 11)
    line = runclass.guard_line(row)
    assert "Sonnet high review fails 3/20 (15%) vs Opus high 3/24 (12%)" in line or "3/24 (13%)" in line
    assert "Sonnet below high 3 reviewed, 2 failed" in line and "untraced (no worker run) 4 reviewed, 3 failed" in line
    # The same numbers as one blended rate would have tripped it: 11/51 against a 13% base.
    assert not runclass.guard_check(p.db, {"route": {"code_sonnet": {"guard_pts": 30}}}, now)["tripped"]


def test_code_route_guard_needs_fifteen_reviews_per_model_and_trips_only_on_a_real_gap(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    _guard_tasks(p, now, 14, 9, model="sonnet")   # 14 Sonnet high at 64%: too few to compare
    _guard_tasks(p, now, 30, 0, model="opus")
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    assert not row["enough"] and not row["tripped"] and row["gap_pts"] is None
    assert row["comparison"] == "not enough reviews to compare"
    assert "not enough reviews to compare" in runclass.guard_line(row)
    _guard_tasks(p, now, 1, 1, model="sonnet")    # 15 Sonnet high, 10 failed (67%) against 0%
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    assert row["enough"] and row["tripped"] and row["gap_pts"] == 66.7
    assert not runclass.guard_check(p.db, {"route": {"code_sonnet": {"guard_pts": 70}}}, now)["tripped"]
    assert "gap 66.7 pts (limit 5)" in runclass.guard_line(row)
    # Too few Opus high ones: no trip either, whatever Sonnet's rate is.
    p.db.x("UPDATE runs SET effort='medium' WHERE model='opus' AND id%2=0")
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    assert row["day"]["models"]["opus_high"]["reviewed"] == 15 and row["tripped"]
    p.db.x("UPDATE runs SET effort='medium' WHERE model='opus'")
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    assert not row["enough"] and not row["tripped"] and row["day"]["excluded"]["other"]["reviewed"] == 30
    # Off: `route.code_sonnet.guard` false, or the code route itself off.
    for off in ({"code_sonnet": {"guard": False}}, {"code_sonnet": False}, {"core_cheap": False}):
        assert runclass.guard_settings({"route": off}) is None
        assert runclass.guard_run(p.db, {"route": off}, now) is None
    assert p.db.kv(runclass.GUARD_DAYS_KV) is None


def test_code_route_guard_keeps_prior_days_as_context_and_counts_a_light_hop_apart(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    _guard_tasks(p, now, 20, 10, ago=3 * 86400, model="sonnet")   # a bad prior week: never the comparator
    _guard_tasks(p, now, 20, 0, ago=3 * 86400, model="opus")
    _guard_tasks(p, now, 2, 2, model="sonnet", effort="high", note={"wake": {"tier": "light"}})   # a light wake hop
    _guard_tasks(p, now, 2, 0, model="opus", effort="high", note={"run_class": "light"})
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    assert row["day"]["excluded"]["light"] == {"reviewed": 4, "review_fails": 2, "fail_rate": 0.5}
    assert row["day"]["models"]["sonnet_high"]["reviewed"] == 0 and not row["tripped"]
    assert row["prior"]["models"]["sonnet_high"]["fail_rate"] == 0.5
    assert "prior 7 days (context only): Sonnet high 10/20 (50%), Opus high 0/20 (0%)" in runclass.guard_line(row)


def test_code_route_guard_splits_the_spend_per_landed_task_by_model_and_by_role(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    # Two landed lineages: a root on Opus plus a review-fix hop on Sonnet, and a lone Sonnet root; each has a review.
    a = _guard_tasks(p, now, 1, 0, landed=1, spent=3.0, model="opus")[0]
    fix = p.db.add_task("continue: fix", "", kind="code", tier="standard", priority=3, budget_usd=5.0)
    p.db.x("UPDATE tasks SET status='done', updated=?, labels=?, parent=?, spent_usd=2 WHERE id=?",
           (now - 3600, json.dumps([f"review_fix:{a}"]), a, fix))
    p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (fix, "worker", "claude", "sonnet", "high", now - 1000, now - 900, "done", 2.0))
    b = _guard_tasks(p, now, 1, 0, landed=1, spent=1.0, model="sonnet")[0]
    for tid, cost in ((a, 1.0), (b, 0.5)):   # the reviews' own runs
        rid = p.db.one("SELECT id FROM tasks WHERE kind='review' AND labels LIKE ?", (f'%auto_review:{tid}"%',))["id"]
        p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd) "
               "VALUES(?,?,?,?,?,?,?,?,?)", (rid, "reviewer", "claude", "opus", "high", now - 100, now - 50, "done", cost))
    ab = p.db.add_task("ab", "", kind="code", tier="standard", priority=3, budget_usd=5.0)
    p.db.x("UPDATE tasks SET status='done', updated=?, labels=? WHERE id=?",
           (now - 3600, json.dumps(["device_ab:sonnet"]), ab))
    p.db.x("INSERT INTO task_landings(marker,task,sha,target,ts) VALUES(?,?,?,?,?)", ("mab", ab, "a" * 40, "branch", now))
    p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (ab, "worker", "claude", "sonnet", "high", now - 1000, now - 900, "done", 3.0))
    day = runclass.guard_check(p.db, _GUARD_CFG, now)["day"]
    assert day["landed"] == 3 and day["hops"] == 1 and day["usd"] == 10.5 and day["usd_per_landed"] == 3.5
    assert day["usd_by_role"] == {"code_worker": 1.33, "device_ab": 1.0, "reviews": 0.5, "rework": 0.67}
    assert day["usd_by_model"] == {"opus:high": 1.5, "sonnet:high": 2.0}
    line = runclass.guard_line(runclass.guard_check(p.db, _GUARD_CFG, now))
    assert "by worker model: opus:high 1.50, sonnet:high 2.00" in line
    assert "by role: code worker runs 1.33, reviews 0.50, review fixes and continuations 0.67, device A/B runs 1.00" in line \
        or "review fixes and continuations 0.67" in line


def _attrib_task(p, tmp, now, runs, head="", verdict="done", n=0):
    """One finished code task with worker `runs` ((model, effort, cost, minutes before the review, note,
    commit its result.json names)) and one first review reviewing `head` (review_stats.head); returns the id."""
    t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=5.0)
    p.db.x("UPDATE tasks SET status='done', updated=? WHERE id=?", (now - 3600, t))
    for i, (model, effort, cost, mins, note, commit) in enumerate(runs):
        d = tmp / f"run-{t}-{i}-{n}"
        d.mkdir()
        if commit is not None:
            (d / "result.json").write_text(json.dumps({"commit": commit}))
        p.db.x("INSERT INTO runs(task, role, provider, model, effort, started, ended, status, cost_usd, note, dir) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (t, "worker", "claude", model, effort, now - 3600 - mins * 60,
                                                  now - 3600 - mins * 60 + 30, "done", cost, json.dumps(note or {}),
                                                  str(d)))
    r = p.db.add_task(f"Review #{t}", "", kind="review", tier="standard", priority=3, budget_usd=1.0)
    p.db.x("UPDATE tasks SET status=?, labels=? WHERE id=?", (verdict, json.dumps([f"auto_review:{t}"]), r))
    if head:
        p.db.x("INSERT INTO review_stats(task, ts, head) VALUES(?,?,?)", (r, now - 3000, head))
    return t


def test_code_route_guard_counts_a_review_under_the_run_that_made_the_reviewed_head_commit(env, tmp_path):
    from ttp import runclass
    p = make(env)
    now = time.time()
    head = "ab12cd34" * 5
    poll = {"wake": {"tier": "light"}}
    # Opus high made the head commit; a cheaper Sonnet high poll ran later, just before the review.
    _attrib_task(p, tmp_path, now, [("opus", "high", 3.0, 30, None, head), ("sonnet", "high", 0.1, 2, poll, None)],
                 head=head, verdict="failed")
    # Same shape with an abbreviated commit on the review side (prefix match).
    _attrib_task(p, tmp_path, now, [("opus", "high", 1.0, 30, None, head[:12]), ("sonnet", "high", 0.1, 2, None, None)],
                 head=head)
    # The commit-making run is the cheaper one: head beats the costliest run.
    _attrib_task(p, tmp_path, now, [("sonnet", "high", 0.5, 30, None, head), ("opus", "high", 4.0, 20, None, "f" * 40)],
                 head=head)
    day = runclass.guard_check(p.db, _GUARD_CFG, now)["day"]
    assert (day["models"]["opus_high"]["reviewed"], day["models"]["opus_high"]["review_fails"]) == (2, 1)
    assert day["models"]["sonnet_high"]["reviewed"] == 1 and day["excluded"]["light"]["reviewed"] == 0
    assert day["attributed"] == {"head": 3, "costliest": 0}


def test_code_route_guard_falls_back_to_the_costliest_run_when_no_commit_traces_and_never_picks_a_poll(env, tmp_path, monkeypatch):
    from ttp import runclass
    p = make(env)
    now = time.time()
    poll = {"wake": {"tier": "light"}}
    # No result.json commit, no review_stats head: the costliest real run (Opus), not the last one (a Sonnet high poll).
    _attrib_task(p, tmp_path, now, [("opus", "high", 2.0, 30, None, None), ("sonnet", "high", 0.2, 2, None, None)])
    # A head the reviewer saw that no run made (or a malformed one): costliest again.
    _attrib_task(p, tmp_path, now, [("opus", "high", 2.0, 30, None, "1" * 40), ("sonnet", "high", 0.2, 2, None, "not hex")],
                 head="2" * 40, n=1)
    # A wake/poll run is never chosen while a real worker run exists, even when it cost the most and named the head.
    _attrib_task(p, tmp_path, now, [("opus", "high", 0.5, 30, None, None), ("sonnet", "high", 9.0, 2, poll, "3" * 40)],
                 head="3" * 40, n=2)
    # A poll run is picked only when it is the only run: light, not a model bucket.
    _attrib_task(p, tmp_path, now, [("sonnet", "high", 0.2, 2, poll, None)], n=3)
    # No worker run at all: untraced.
    _attrib_task(p, tmp_path, now, [], head="4" * 40, n=4)
    # A run with no folder recorded never reads a result.json from the working directory.
    p.db.x("UPDATE runs SET dir=NULL WHERE task=(SELECT MAX(task) FROM runs)")
    (tmp_path / "result.json").write_text(json.dumps({"commit": "3" * 40}))
    monkeypatch.chdir(tmp_path)
    assert runclass._run_commit({"dir": None}) == "" and runclass._run_commit({}) == ""
    day = runclass.guard_check(p.db, _GUARD_CFG, now)["day"]
    assert day["models"]["opus_high"]["reviewed"] == 3 and day["models"]["sonnet_high"]["reviewed"] == 0
    assert day["excluded"]["light"]["reviewed"] == 1 and day["excluded"]["untraced"]["reviewed"] == 1
    assert day["attributed"] == {"head": 0, "costliest": 4}


def test_a_runs_left_commit_is_its_recorded_head_else_any_commit_form_its_hand_off_names(env, tmp_path):
    from ttp import runclass
    c = "ab12cd34" * 5

    def made(body):
        d = tmp_path / f"rc-{len(list(tmp_path.iterdir()))}"
        d.mkdir()
        (d / "result.json").write_text(body if isinstance(body, str) else json.dumps(body))
        return {"dir": str(d)}
    assert runclass._run_commit({"head": c.upper(), "dir": None}) == c      # the head the daemon recorded
    assert runclass._run_commit({**made({"commit": "f" * 40}), "head": c}) == c   # it beats the hand-off
    assert runclass._run_commit({"head": "not hex", **made({"sha": c})}) == c
    assert runclass._run_commit(made({"commits": ["1111111 first", "ab12cd34 second"]})) == "ab12cd34"
    assert runclass._run_commit(made({"commit": f"{c[:9]} fix the thing"})) == c[:9]
    assert runclass._run_commit(made({"work_commit": c})) == c
    assert runclass._run_commit(made({"commits": "nothing", "commit": "no commit made"})) == ""
    assert runclass._run_commit(made("[1, 2]")) == "" and runclass._run_commit(made("{broken")) == ""


def test_the_guard_traces_a_review_by_the_head_a_run_recorded_when_its_hand_off_names_no_commit(env, tmp_path):
    from ttp import runclass
    p = make(env)
    now = time.time()
    head = "cd34ef56" * 5
    t = _attrib_task(p, tmp_path, now, [("sonnet", "high", 0.5, 30, None, None), ("opus", "high", 4.0, 20, None, None)],
                     head=head)
    # The cheaper Sonnet run left the reviewed head; the later, dearer Opus run only polled it.
    for model, h in (("sonnet", head), ("opus", head)):
        p.db.x("UPDATE runs SET head=? WHERE task=? AND model=?", (h, t, model))
    day = runclass.guard_check(p.db, _GUARD_CFG, now)["day"]
    assert day["attributed"] == {"head": 1, "costliest": 0}
    assert day["models"]["sonnet_high"]["reviewed"] == 1 and day["models"]["opus_high"]["reviewed"] == 0


def test_code_route_guard_report_says_how_many_reviews_were_found_by_head_commit_and_by_costliest_run(env, tmp_path):
    from ttp import runclass
    p = make(env)
    now = time.time()
    head = "5" * 40
    _attrib_task(p, tmp_path, now, [("opus", "high", 1.0, 30, None, head)], head=head)
    _attrib_task(p, tmp_path, now, [("opus", "high", 1.0, 30, None, None)], n=1)
    _attrib_task(p, tmp_path, now, [("sonnet", "high", 1.0, 30, None, None)], n=2)
    line = runclass.guard_line(runclass.guard_check(p.db, _GUARD_CFG, now))
    assert "worker run found by head commit for 1 reviews, by costliest run for 2" in line
    # A row stored before this attribution existed has no counts and prints no such line.
    row = runclass.guard_check(p.db, _GUARD_CFG, now)
    row["day"].pop("attributed")
    assert "found by head commit" not in runclass.guard_line(row)


def test_code_route_guard_trip_holds_code_on_opus_until_the_setting_changes_and_tells_the_coordinator_once(env, capsys):
    from ttp import cli, runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()
    p.set_config("route.code_sonnet", {"share": 1.0})
    _guard_tasks(p, now, 10, 0, ago=2 * 86400, landed=10)
    _guard_tasks(p, now, 15, 8, landed=3, model="sonnet")   # Sonnet high 53% today ...
    _guard_tasks(p, now, 15, 0, landed=3, model="opus")     # ... against Opus high 0%
    p.db.set_kv(runclass.CODE_STATE_KV, json.dumps({"new": "full"}))
    d = Daemon(p.base)
    d.watch_run_classes()
    assert d._code_state() == {k: "off" for k in runclass.CODE_KINDS}
    t = {"id": 1, "kind": "code", "tier": "standard", "title": "x", "labels": "[]"}
    assert runclass.core_cheap(d.cfg, t, "hard", "standard", "claude", None, None, d._code_state()) is None
    ev = p.db.q("SELECT severity, text FROM events WHERE kind='code_route_guard'")
    assert len(ev) == 1 and ev[0]["severity"] == "high" and "8/15 (53%)" in ev[0]["text"] \
        and "route.code_sonnet" in ev[0]["text"]
    # The hourly loop never flips it back, and the daily row is not redone within the day.
    d._run_class_checked = d._code_quality_checked = d._code_guard_checked = 0.0
    d.watch_run_classes()
    assert d._code_state()["new"] == "off"
    assert runclass.guard_run(p.db, d.cfg, now + 3600) is None
    assert len(p.db.kv(runclass.GUARD_DAYS_KV)) == 1
    # A day later still tripped: held, recorded, no second alert.
    row = runclass.guard_run(p.db, d.cfg, now + 86400)
    assert row["held"] and not row.get("new_trip")
    assert p.db.one("SELECT COUNT(*) n FROM events WHERE kind='code_route_guard'")["n"] == 1
    assert cli.main(["stats", "demo", "--code-guard"]) in (None, 0)
    out = capsys.readouterr().out
    assert "HOLDING every code kind on Opus" in out and "TRIPPED" in out
    assert cli.main(["stats", "demo", "--code-guard", "--json"]) in (None, 0)
    s = json.loads(capsys.readouterr().out)
    assert s["held"] and len(s["rows"]) == 2 and s["guard"] == {"pts": 5.0}
    # A person changes route.code_sonnet: the hold ends and the hourly states rule again.
    p.set_config("route.code_sonnet", {"share": 0.5})
    d._load_config()
    assert not runclass.guard_held(p.db, d.cfg)
    p.db.set_kv(runclass.CODE_STATE_KV, json.dumps({"new": "full"}))
    assert d._code_state()["new"] == "full"


def test_code_route_guard_never_counts_the_reviews_that_tripped_it_once_the_setting_changes(env, monkeypatch):
    """Re-armed by a new route.code_sonnet, the next day's guard judges only reviews after its trip; new bad ones
    trip it again."""
    from ttp import runclass
    p = make(env)
    now = time.time()
    clock = [now]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    _guard_tasks(p, now, 15, 8, model="sonnet")
    _guard_tasks(p, now, 15, 0, model="opus")
    assert runclass.guard_run(p.db, _GUARD_CFG, now)["new_trip"]
    cfg = {"route": {"code_sonnet": {"share": 0.5}}}   # the re-arm
    assert not runclass.guard_held(p.db, cfg)
    now += runclass.GUARD_EVERY_S
    clock[0] = now
    row = runclass.guard_run(p.db, cfg, now)
    assert not row["tripped"] and row["day"]["reviewed"] == 0, "the tripping reviews never count again"
    _guard_tasks(p, now, 15, 8, ago=600, model="sonnet")
    _guard_tasks(p, now, 15, 0, ago=600, model="opus")
    assert runclass.guard_run(p.db, cfg, now + runclass.GUARD_EVERY_S)["new_trip"], "new bad reviews trip it again"


def test_code_route_takes_jev_picked_and_urgent_code_and_opus_keeps_only_user_pins_and_outside_input():
    from ttp import runclass
    t = {"id": 7, "kind": "code", "tier": "standard", "title": "add a flag", "labels": "[]", "spec": "add it"}
    full = {"new": "full"}
    jev = {"by": "jev", "pick": "claude:opus:high", "p": 0.8}

    def cheap(task, picked=None):
        return runclass.core_cheap({}, task, "hard", "standard", "claude", None, picked, full)

    # A Jev pick is not a pin, and urgency never keeps code on Opus.
    assert cheap(t, jev)["model"] == "sonnet"
    assert cheap({**t, "priority": 1, "spec": "urgent: the user is waiting"}, jev)["model"] == "sonnet"
    assert runclass.code_route({}, {**t, "priority": 1, "labels": '["urgent"]'}, jev, full)[1] == ""
    # Only a user's pin of provider, model or effort keeps it, and outside input does too.
    for k, v in (("provider", "claude"), ("model", "opus"), ("effort", "high")):
        assert cheap({**t, k: v}, jev) is None
        assert runclass.code_route({}, {**t, k: v}, jev, full) == (None, "user_pin")
    assert runclass.code_route({}, {**t, "labels": '["untrusted_input"]'}, jev, full) == (None, "untrusted_input")
    assert runclass.code_route({}, {**t, "labels": '["needs_device"]'}, jev, full) == (None, "device_run")
    fix = {**t, "labels": '["review_fix:9", "pr_fix:9"]'}
    assert runclass.code_route({}, fix, None, {"review_fix": "full"}) == (None, "pr_fix")
    late = {**t, "labels": json.dumps(["review_fix:9", f"{runclass.ROUND_LABEL}{runclass.REVIEW_ROUND_MAX}"])}
    assert runclass.code_route({}, late, None, {"review_fix": "full"}) == (None, "review_round")


def test_code_decision_is_what_the_run_note_records_with_the_reason():
    from ttp import runclass
    t = {"id": 7, "kind": "code", "tier": "standard", "labels": "[]", "spec": "add it"}
    full = {"new": "full"}
    note = runclass.core_cheap({}, t, "hard", "standard", "claude", None, None, full)
    assert runclass.code_decision({}, t, "hard", "standard", "claude", None, full, note) == {"to": "sonnet",
                                                                                          "kind": "new"}
    assert runclass.code_decision({}, {**t, "model": "opus"}, "hard", "standard", "claude", None, full) == {
        "to": "opus", "why": "user_pin"}
    assert runclass.code_decision({}, {**t, "labels": '["untrusted_input"]'}, "hard", "standard", "claude",
                                  None, full)["why"] == "untrusted_input"
    assert runclass.code_decision({}, t, "hard", "standard", "codex", None, full) == {"to": "codex",
                                                                                     "why": "provider"}
    assert runclass.code_decision({}, t, "hard", "standard", "claude", None, full, {"model": "sonnet"},
                                  {"arm": "sonnet"})["why"] == "device_ab"
    assert runclass.code_decision({}, {**t, "kind": "work"}, "hard", "standard", "claude") is None


def test_route_share_alarm_fires_once_a_day_on_a_low_code_share_and_not_on_a_normal_one(env):
    from ttp import runclass
    from ttp.daemon import Daemon
    p = make(env)
    now = time.time()

    def runs(n, to, why=None):
        for _ in range(n):
            t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
            cr = {"to": to} if to == "sonnet" else {"to": to, "why": why}
            p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
                   (t, "worker", "claude", "sonnet" if to == "sonnet" else "claude-opus-5-5", now - 600, "ok", 0.1,
                    json.dumps({runclass.CODE_ROUTE_KEY: cr})))

    cfg = {"route": {"code_sonnet": {"share": 0.6}}}
    # Normal: 4 of 10 the route should take (CODE_KEEP reasons do not count) is over half of 60%.
    runs(4, "sonnet")
    runs(6, "opus", "canary_share")
    runs(5, "opus", "untrusted_input")
    s = runclass.code_routes(p.db, now - 86400, now + 1)
    assert (s["runs"], s["base"], s["base_sonnet"], s["why"]["untrusted_input"]) == (15, 10, 4, 5)
    assert runclass.share_alarms(p.db, cfg, now + 1) == []
    # Low: 4 of 20 is under half of 60%; the alarm names the top reasons, not the kept ones.
    runs(10, "opus", "quality_hold")
    got = runclass.share_alarms(p.db, cfg, now + 1)
    assert [a["route"] for a in got] == ["code_sonnet"] and got[0]["n"] == 20
    assert "quality_hold 10" in got[0]["text"] and "canary_share 6" in got[0]["text"]
    assert "untrusted_input" not in got[0]["text"].split("Top reasons:")[1]
    # Runs from before the decision was recorded never count toward the share.
    for _ in range(30):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd) VALUES(?,?,?,?,?,?,?)",
               (t, "worker", "claude", "claude-opus-5-5", now - 600, "ok", 0.1))
    s2 = runclass.code_routes(p.db, now - 86400, now + 1)
    assert (s2["why"]["unrecorded"], s2["base"]) == (30, 20)
    # Off it never fires.
    assert runclass.share_alarms(p.db, {"route": {"code_sonnet": False}}, now + 1) == []
    # The daemon raises one normal event per route per day.
    d = Daemon(p.base)
    d.cfg = {**d.cfg, **cfg}
    assert d.route_share_alarms(now + 1) == ["code_sonnet"]
    assert d.route_share_alarms(now + 2) == []
    ev = p.db.q("SELECT severity, source FROM events WHERE kind='route_share_low'")
    assert [(e["severity"], e["source"]) for e in ev] == [("normal", "daemon")]
    assert d.route_share_alarms(now + 1 + runclass.SHARE_WINDOW_S) == []   # runs aged out of the window
    # The stats flag prints the per-model and per-reason counts.
    assert "quality_hold 10" in runclass.code_routes_text(runclass.code_routes(p.db, now - 86400, now + 1))


def test_class_routed_and_other_provider_code_runs_are_not_misses_of_the_code_share(env):
    from ttp import runclass
    p = make(env)
    now = time.time()
    cfg = {"route": {"code_sonnet": {"share": 0.6}}}

    def run(cls, tier, provider, model, why=None, recorded=True):
        t = p.db.add_task("t", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        cr = runclass.code_decision({}, p.db.task(t), cls, tier, provider)
        if why is not None:
            cr = {"to": "opus", "why": why}
        p.db.x("INSERT INTO runs(task,role,provider,model,started,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?)",
               (t, "worker", provider, model, now - 600, "ok", 0.1,
                json.dumps({runclass.CODE_ROUTE_KEY: cr}) if recorded else None))
        return cr

    # A light-tier chat run of a code task records run_class:light; a codex run records provider. Neither counts.
    assert run("light", "light", "claude", "claude-haiku-5-5")["why"] == "run_class:light"
    for _ in range(11):
        run("light", "light", "claude", "claude-haiku-5-5")
    for _ in range(4):
        run("hard", "standard", "codex", "gpt")
    s = runclass.code_routes(p.db, now - 86400, now + 1)
    assert (s["runs"], s["base"], s["why"]["run_class:light"], s["why"]["provider"]) == (16, 0, 12, 4)
    assert runclass.share_alarms(p.db, cfg, now + 1) == []
    text = runclass.code_routes_text(s)
    assert "on another provider or run class (not counted): run_class:light 12, provider 4" in text
    # A substantive Opus code run with no recorded reason still counts against the share.
    for _ in range(10):
        run("hard", "standard", "claude", "claude-opus-5-5", why="unknown")
    got = runclass.share_alarms(p.db, cfg, now + 1)
    assert [a["route"] for a in got] == ["code_sonnet"] and got[0]["n"] == 10, got
    top, rest = got[0]["text"].split("Top reasons:")[1].split("\n", 1)
    assert "unknown 10" in top and "run_class" not in top and "provider" not in top
    assert rest.startswith("Not counted (on another provider or run class): run_class:light 12, provider 4.")


def test_a_worker_run_ends_with_the_head_its_task_branch_points_at_recorded(env, tmp_path, monkeypatch):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    code = p.db.add_task("code", "s", kind="code", tier="standard", origin="user")
    rev = p.db.add_task("Review", "s", kind="review", tier="standard", origin="user")
    monkeypatch.setattr(d, "_branch_head", lambda t: "9a8b7c6d5e" if t["id"] == code else "")
    ended = {}
    for task, role in ((code, "worker"), (rev, "worker"), (code, "coordinator")):
        p.db.update_task(task, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,?,'claude','',?,'running')",
                     (task, role, time.time() - 60))
        (p.runs / str(rid)).mkdir(parents=True)
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), {"rc": 0, "started": time.time() - 60,
                                                                       "ended": time.time()})
        ended[(task, role)] = p.db.one("SELECT head FROM runs WHERE id=?", (rid,))["head"]
    assert ended[(code, "worker")] == "9a8b7c6d5e"
    assert not ended[(rev, "worker")] and not ended[(code, "coordinator")]

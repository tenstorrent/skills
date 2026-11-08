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
    assert c({"kind": "code", "tier": "standard", "title": "rebase #12 onto the branch"}) == "rebase"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 120}}}) == "review_small"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 900}}}) == "hard"
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {}}) == "hard"   # size unknown
    assert c({"kind": "review", "tier": "standard", "title": "r"}, {"note": {"review_tier": {"lines": 120}}},
             {"route": {"review_small_lines": 100}}) == "hard"
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": {"wake": {"tier": "light"}}}) == "worker_wake"
    assert c({"kind": "code", "tier": "light", "title": "x"}) == "light"
    assert c({"kind": "question", "tier": "standard", "title": "why"}) == "status_report"
    assert c({"kind": "work", "tier": "standard", "title": "report runs per hour"}) == "status_report"
    assert c({"kind": "work", "tier": "standard", "title": "bring up a model"}) == "hard"
    assert c({"kind": "code", "tier": "standard", "title": "fix it"}) == "hard"
    assert c({"kind": "plan", "tier": "deep", "title": "plan"}) == "hard"
    # A recorded class wins; a bad one is ignored.
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": '{"run_class": "light"}'}) == "light"
    assert c({"kind": "code", "tier": "standard", "title": "x"}, {"note": '{"run_class": "bogus"}'}) == "hard"


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
    # Quiet otherwise: no chat provider (nothing routed), or the routed class ran on chat.
    assert runclass.quiet_classes(runclass.by_class(p.db, {}, now - 3600, now)) == []
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

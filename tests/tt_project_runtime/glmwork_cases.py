# GLM work route tests (glmwork.py, the daemon's _glm_route and the class-route ordering) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new glmwork tests here.
from __future__ import annotations

_GW_MODEL = "example-org/work-model"


def _gw_cfg(**over):
    return {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}},
            "route": {"glm_work": {"on": True, "model": _GW_MODEL, **over}}}


def _gw_task(p, spec="tidy the helper and add a test", kind="work", **kw):
    t = p.db.add_task(f"{kind} task", spec, kind=kind, tier="standard", priority=3, budget_usd=1.0, **kw)
    return p.db.task(t)


def _gw_daemon(p, cfg):
    from ttp import daemon
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.p, d.cfg = p, cfg
    d._model_pick = lambda *a, **k: None
    return d


def test_glm_work_is_off_by_default_and_on_with_a_model_and_its_defaults():
    from ttp import glmwork
    assert glmwork.settings({}) is None
    assert glmwork.settings({"route": {"glm_work": {"on": True}}}) is None   # no model
    assert glmwork.settings({"route": {"glm_work": {"on": False, "model": _GW_MODEL}}}) is None
    s = glmwork.settings(_gw_cfg())
    assert (s["provider"], s["model"], s["effort"], s["harness"]) == ("chat", _GW_MODEL, "max", "mini-swe-agent")
    assert (s["holdout"], s["holdout_held"], s["kinds"]) == (0.10, 0.05, ("code", "work", "question", "harness"))
    assert s["fallback"] == {"provider": "claude", "model": "sonnet", "effort": "high"}
    # the code arm's model stands in for an unset work model
    cs = {"route": {"glm_work": {"on": True}, "code_sonnet": {"glm": {"model": "example-org/code-model"}}}}
    assert glmwork.settings(cs)["model"] == "example-org/code-model"


def test_glm_work_ring_gate_keeps_a_projects_own_setting_and_is_off_without_the_flag(env):
    from ttp import glmwork
    raw = {"route": {"glm_work": {"on": False, "model": _GW_MODEL}}}
    cfg = {"route": {"glm_work": {"on": False, "model": _GW_MODEL}}}
    assert glmwork.ring_gate(cfg, "alpha", raw) is cfg and not glmwork.covered(cfg)   # its own word wins
    cfg = {"route": {"glm_work": {"model": _GW_MODEL}}}
    assert not glmwork.covered(glmwork.ring_gate(cfg, "alpha", {}))   # no ring: off


def test_a_glm_work_rollback_leaves_the_device_route_on_where_glm_device_covers_the_project(env):
    from ttp import deviceplan, glmwork, rollout
    p = make(env)
    rollout.set_ring("alpha", "0")
    rollout.set_waves("glm_work", ["alpha"])
    rollout.set_level("glm_work", rollout.AUTO, "t")
    rollout.set_level("glm_device", 0, "t")
    base = lambda: {"route": {"glm_work": {"model": _GW_MODEL, "holdout": 0}}}   # noqa: E731
    gate = lambda: glmwork.ring_gate(base(), "alpha", {})   # noqa: E731
    dev = _gw_task(p, "Rebuild the driver and run the box test.", labels=["resource:box-a-device"])
    plain = _gw_task(p)
    cfg = gate()
    assert glmwork.settings(cfg) and not glmwork.device_only(cfg) and deviceplan.on(cfg)
    assert glmwork.keeps_core(p.db, cfg, plain, "standard") == ""
    assert rollout.stop_project("glm_work", "alpha", "worse")
    cfg = gate()
    assert glmwork.device_only(cfg) and deviceplan.on(cfg) and glmwork.settings(cfg, device=True)["model"] == _GW_MODEL
    assert glmwork.keeps_core(p.db, cfg, dev, "standard") == "", "the device task keeps the GLM route"
    assert not glmwork.covered(cfg) and glmwork.settings(cfg) is None, "only the device path reads device_only"
    got = _gw_daemon(p, cfg)._route(dev, "standard", None, 1.0, cls="routine")
    assert got and got[glmwork.KEY] == "glm"
    chat = {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1", "routes": [{"model": "m-rule"}]}
    cfg["providers"] = {"chat": chat}
    off = lambda c: _gw_daemon(p, c)._route(plain, "standard", None, 1.0, cls="routine")   # noqa: E731
    without = {**cfg, "route": {k: v for k, v in cfg["route"].items() if k != "glm_work"}}
    assert off(cfg) and off(cfg) == off(without), "a plain run routes as it did before the rollback (its rule route)"
    rollout.stop_project("glm_device", "alpha", "device harm")   # glm_device's own stop still turns the route off
    cfg = gate()
    assert not glmwork.covered(cfg) and not deviceplan.on(cfg) and glmwork.settings(cfg, device=True) is None
    rollout.arm("glm_device", project="alpha")
    own = glmwork.ring_gate(base(), "alpha", {"route": {"glm_work": {"on": False}}})
    assert not glmwork.device_only(own), "a project's own glm_work setting still wins"


def test_glm_work_keeps_every_protected_run_on_core(env):
    """Coordinator, plan, review, verdict, deep, pins, outside text, retries, GLM bounces, person-facing, design
    or optimization, device and off-list kinds stay on core; a plain work, question, harness or code run may go."""
    from ttp import glmwork
    p = make(env)
    cfg = _gw_cfg()
    kc = lambda t, tier="standard", cls=None, picked=None: glmwork.keeps_core(p.db, cfg, t, tier, cls, picked)
    for kind in ("work", "question", "harness", "code"):
        assert kc(_gw_task(p, kind=kind)) == "", kind
    assert kc(_gw_task(p), cls="coordinator") == "coordinator"
    assert kc(_gw_task(p, kind="plan")) == "plan"
    assert kc(_gw_task(p, kind="review")) == "review"
    assert kc(_gw_task(p, "Say whether the fix holds and return a verdict: pass or fail.")) == ""   # only a review keeps one
    assert kc(_gw_task(p), tier="deep") == "deep"
    assert kc(_gw_task(p, labels=["user_deep"])) == "deep"
    assert kc(_gw_task(p, provider="claude")) == "pin"
    assert kc(_gw_task(p, model="opus")) == "pin"
    assert kc(_gw_task(p, labels=["untrusted_input"])) == "untrusted"
    assert kc(_gw_task(p), picked={"by": "retry"}) == "retry"
    assert kc(_gw_task(p, reply_chat="chat-1")) == "person"
    assert kc(_gw_task(p, "Post a comment on the GitHub issue saying it is fixed.")) == "person"
    assert kc(_gw_task(p, "Optimize the scheduler's queue scan.")) == "design"
    assert kc(_gw_task(p, "Redesign the push queue.")) == "design"
    assert kc(_gw_task(p, "Propose a plan for the new cache layer.")) == "design"
    assert kc(_gw_task(p, labels=["needs_device"])) == "device"
    assert kc(_gw_task(p, "Run the soak on the box.")) == "device"
    assert kc(_gw_task(p, kind="ops")).startswith("kind ")
    assert kc(_gw_task(p, "Fix it. urgent: the user waits")) == ""   # urgent goes to GLM unless keep_urgent
    assert glmwork.keeps_core(p.db, _gw_cfg(keep_urgent=True), _gw_task(p, "Fix it.\nurgent: the user waits"),
                              "standard") == "urgent"


def test_glm_work_bounce_keeps_the_task_on_core(env, monkeypatch):
    from ttp import codearm, glmwork
    p = make(env)
    t = _gw_task(p, kind="code")
    monkeypatch.setattr(codearm, "bounced", lambda db, tid: tid == t["id"])
    assert glmwork.keeps_core(p.db, _gw_cfg(), t, "standard") == "bounced"


def test_glm_work_holdout_coin_is_seeded_recorded_and_drops_to_the_held_share_once_quality_holds(env):
    from ttp import glmwork
    p = make(env)
    cfg = _gw_cfg()
    side = [glmwork.coin(i, 0.10) for i in range(4000)]
    assert side == [glmwork.coin(i, 0.10) for i in range(4000)]   # seeded: the same side each time
    assert 0.07 < sum(side) / 4000 < 0.13
    assert 0.03 < sum(glmwork.coin(i, 0.05) for i in range(4000)) / 4000 < 0.07
    assert glmwork.holdout(p.db, cfg) == 0.10
    assert glmwork.mark_held(p.db, {"state": "ok", "why": "quality holds"}, now=5.0)
    assert not glmwork.mark_held(p.db, {"state": "ok", "why": "again"}, now=6.0)   # unchanged
    assert glmwork.holdout(p.db, cfg) == 0.05
    assert glmwork.mark_held(p.db, {"state": "worse", "why": "pass rate down"}, now=7.0)
    assert glmwork.holdout(p.db, cfg) == 0.10
    t = _gw_task(p)
    assert glmwork.arm(p.db, cfg, t, peek=True) and glmwork.recorded(p.db, t["id"]) is None   # peek: not kept
    a = glmwork.arm(p.db, cfg, t)
    assert a["arm"] in ("glm", "holdout") and glmwork.recorded(p.db, t["id"])["arm"] == a["arm"]
    assert p.db.kv(f"{glmwork.ARM_KV}{t['id']}")["share"] == 0.10   # the arm is logged per task
    every = _gw_cfg(holdout=1.0)
    h = _gw_task(p)
    assert glmwork.arm(p.db, every, h)["arm"] == "holdout"


def test_glm_work_routes_work_question_harness_runs_to_the_chat_model_and_keeps_plans_and_reviews_on_core(env):
    from ttp import glmwork
    p = make(env)
    cfg = _gw_cfg(holdout=0)
    d = _gw_daemon(p, cfg)
    for kind in ("work", "question", "harness"):
        t = _gw_task(p, kind=kind)
        got = d._route(t, "standard", None, 1.0, cls="routine")
        assert got and got["to"] == "chat" and got["model"] == _GW_MODEL and got[glmwork.KEY] == "glm", kind
        assert got["pick_effort"] == "max" and got["fallback"] == "none", kind
        assert d._route(t, "deep", None, 1.0, cls="hard") is None, kind
    for kind in ("plan", "review"):
        assert d._route(_gw_task(p, kind=kind), "standard", None, 1.0, cls="routine") is None, kind
    assert d._route(_gw_task(p, reply_chat="chat-1"), "standard", None, 1.0) is None
    held = _gw_daemon(p, _gw_cfg(holdout=1.0))
    assert held._route(_gw_task(p), "standard", None, 1.0, cls="routine") is None   # the Claude holdout


def test_glm_work_takes_code_tasks_through_the_guarded_code_arm(env):
    from ttp import codearm, glmwork, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")   # a live arm needs the policy list
    cfg = _gw_cfg(holdout=0)
    s = codearm.settings(cfg)
    assert s and s["glm_work"] and s["share"] == 1.0 and s["model"] == _GW_MODEL and s["harness"] == "mini-swe-agent"
    t = _gw_task(p, kind="code")
    assert codearm.choose(p.db, cfg, t, "standard")["run"] == "glm"
    got = _gw_daemon(p, cfg)._route(t, "standard", None, 1.0, cls="routine")
    assert got and got["to"] == "chat" and got[codearm.KEY] == "glm" and got[glmwork.KEY] == "glm"
    person = _gw_task(p, "Fix it, then post a comment on the GitHub issue.", kind="code")
    assert codearm.choose(p.db, cfg, person, "standard")["run"] == "claude"


def test_glm_device_routes_device_work_by_its_need_whatever_its_kind(env):
    """A task that needs a device takes the GLM device route on a glm_device project, filed as code or as work;
    off glm_device, a review naming a device, and code with no device need route as before."""
    from ttp import codearm, deviceplan, glmwork, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")
    dev, off = _gw_cfg(holdout=0, device=True), _gw_cfg(holdout=0)
    box = ["resource:box-a-device"]
    route = lambda cfg, t: _gw_daemon(p, cfg)._route(t, "standard", None, 1.0, cls="routine")   # noqa: E731
    code = _gw_task(p, "Rebuild the driver and run the box test.", kind="code", labels=box)
    assert deviceplan.device(code, dev) and deviceplan.on(dev) and not deviceplan.on(off)
    assert codearm.stays(p.db, dev, code, "standard") == ""
    got = route(dev, code)
    assert got and got["to"] == "chat" and got[codearm.KEY] == "glm" and got[glmwork.KEY] == "glm"
    assert codearm.stays(p.db, off, code, "standard") == "device" and route(off, code) is None
    work = _gw_task(p, "Rebuild the driver and run the box test.", labels=box)
    got = route(dev, work)
    assert got and got[glmwork.KEY] == "glm" and codearm.KEY not in got and route(off, work) is None
    review = _gw_task(p, "Review the box test change.", kind="review", labels=box)
    assert route(dev, review) is None and glmwork.keeps_core(p.db, dev, review, "standard") == "review"
    plain = _gw_task(p, kind="code")
    for cfg in (dev, off):
        assert codearm.stays(p.db, cfg, plain, "standard") == "" and route(cfg, plain)[codearm.KEY] == "glm"


def test_glm_work_fallback_names_sonnet_for_endpoint_refusals_only(env):
    from ttp import glmwork
    cfg = _gw_cfg()
    for why in ("down", "rate_limit", "busy_wait_expired", "slots", "model_missing"):
        assert glmwork.fallback(cfg, why) == {"provider": "claude", "model": "sonnet", "effort": "high"}, why
    for why in ("none", "rerun", "escalated"):
        assert glmwork.fallback(cfg, why) is None, why
    assert glmwork.fallback({}, "down") is None
    own = _gw_cfg(fallback={"model": "haiku"})
    assert glmwork.fallback(own, "down")["model"] == "haiku"


def test_glm_work_logs_starts_fallbacks_and_waits_per_hour_and_prunes_old_hours(env):
    from ttp import glmwork
    p = make(env)
    now = 1_000_000_000.0
    glmwork.log_hour(p.db, "none", 2.0, now)
    glmwork.log_hour(p.db, "rate_limit", 30.0, now)
    glmwork.log_hour(p.db, "rate_limit", None, now)
    h = glmwork.hours(p.db)[0]
    assert (h["starts"], h["fallbacks"], h["wait_s"]) == (3, {"rate_limit": 2}, 32.0)
    glmwork.log_hour(p.db, "none", 0, now + 60 * 3600)
    assert len(glmwork.hours(p.db)) == 1   # the hour 60 h back is pruned


def test_a_class_route_runs_every_keep_core_check_first(env, monkeypatch):
    """A run class that routes to chat moves a plain work run there, while a conclusion, deep-tier, device or
    question run stays on core: the class route returns only after the floor and protected checks."""
    from ttp import runclass
    p = make(env)
    d = _gw_daemon(p, {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}}})
    d._code_arm = lambda *a, **k: None
    monkeypatch.setattr(runclass, "target", lambda *a, **k: {"to": "chat", "model": _GW_MODEL, "rule": None})
    d._route_entry = lambda task, tier, name, model, now, rule=None, cls=None, core_only=False, **k: (
        None if core_only else {"to": name, "model": model})
    route = lambda t, tier="standard": d._route(t, tier, None, 1.0, cls="routine")   # noqa: E731
    assert route(_gw_task(p)) == {"to": "chat", "model": _GW_MODEL}
    assert route(_gw_task(p, spec="tidy the helper and add a test\nconclusion: the helper is tidy")) is None
    assert route(_gw_task(p), tier="deep") is None
    assert route(_gw_task(p, spec="reset the device and rerun the hang on the board")) is None
    assert route(_gw_task(p, kind="question")) is None


def test_rank_exclude_keeps_a_model_off_every_ranking_and_stand_in_list(env):
    from ttp import modeldemote, modelrank
    cfg = {"providers": {"chat": {"model_ranking": ["example-org/big", "example-org/small", "example-org/mid"],
                                  "class_ranking": {"routine": ["example-org/small", "example-org/mid"]},
                                  "rank_exclude": ["example-org/small"]}}}
    assert modelrank.excluded(cfg) == {"example-org/small"}
    assert modelrank.ranking(cfg) == ["example-org/big", "example-org/mid"]
    assert modeldemote.class_ranking(cfg, "routine") == ["example-org/mid"]
    p = make(env)
    assert "example-org/small" not in modelrank.standins(p.db, cfg, "example-org/big", 1.0)
    assert modelrank.excluded({"providers": {"chat": {"rank_exclude": 5}}}) == set()   # malformed: none


def test_a_glm_work_start_the_endpoint_refuses_runs_on_sonnet_high_and_its_note_names_it(env, monkeypatch):
    """#1924: no silent substitution: the fallback's model is in the run note and counted for the hour."""
    from ttp import configwho, glmwork, route, runclass
    from ttp import daemon as dmod
    p = make(env)
    p.set_config("core_provider", "claude")
    _route_config(p, route=None)
    p.set_config("route.glm_work", {"on": True, "model": _RT_MODEL, "holdout": 0}, by=configwho.by("user", via="cli"))
    monkeypatch.setattr(dmod.route, "cooling", lambda db, name, now: "rate_limit")
    d = dmod.Daemon(p.base)
    prov, tier, note = _route_dispatch(p, d, monkeypatch, title="sum the logs", kind="work", tier="standard")
    assert prov == "claude" and note[route.KEY]["fallback"] == "rate_limit"
    assert note[route.KEY]["fallback_model"] == "sonnet"
    assert note[runclass.CHEAP_KEY]["model"] == "sonnet" and note[runclass.CHEAP_KEY]["effort"] == "high"
    assert glmwork.hours(p.db)[0]["fallbacks"] == {"rate_limit": 1}


def test_a_glm_hand_off_asking_for_opus_or_deep_bounces_fresh_to_opus_with_its_summary_and_the_switch_restores_the_old_way():
    """Lever 8: a GLM run on either route whose hand-off has needs_opus, or needs_deep on any end
    but done, is a bounce (requeued fresh on Opus, no attempt); route.glm_work.bounce false keeps the old way."""
    import inspect
    from ttp import daemon, glmwork
    on, off = _gw_cfg(), _gw_cfg(bounce=False)
    assert glmwork.bounce_on(on) and glmwork.bounce_on({}) and not glmwork.bounce_on(off)
    deep = {"status": "failed", "needs_deep": "the race needs a trace I cannot read", "summary": "found two writers"}
    assert glmwork.bounce_reason(deep, on) == "needs deep thinking: the race needs a trace I cannot read"
    assert glmwork.bounce_reason({**deep, "status": "done"}, on) == ""   # done work stands
    opus = {"status": "blocked", "needs_opus": "the spec leaves the default open", "summary": "read both"}
    assert glmwork.bounce_reason(opus, on) == "the spec leaves the default open"
    assert glmwork.bounce_reason({"status": "blocked", "summary": "waiting on a lock"}, on) == ""
    assert glmwork.bounce_reason(deep, off) == "" and glmwork.bounce_reason(opus, off) == ""
    assert glmwork.bounce_reason(None, on) == "" and glmwork.bounce_reason("x", on) == ""
    c = glmwork.contract()
    assert "needs_opus" in c and "needs_deep" in c and "restarts fresh on Opus" in c and "ttp push" in c
    fin = inspect.getsource(daemon.Daemon.finish_run)
    assert 'glmwork.bounce_reason(_read_result(run_dir / RESULT_FILE), self.cfg)' in fin
    assert '"glm" in (routed.get(codearm.KEY), routed.get(glmwork.KEY))' in fin
    assert "its hand-off said: " in fin   # the bounced run's own summary reaches the fresh run's prompt
    assert "glmwork.contract()" in inspect.getsource(daemon.Daemon)
    assert "ttp slop --diff" in glmwork.size_check() and "glmwork.size_check()" in inspect.getsource(daemon.Daemon)


def _gw_run(p, task, status):
    p.db.x("INSERT INTO runs(task,role,dir,status) VALUES(?,'worker','d',?)", (task["id"], status))


def test_glm_work_retry_after_an_infrastructure_failure_stays_on_glm_but_one_after_a_finished_attempt_goes_to_core(env):
    from ttp import glmwork
    p = make(env)
    cfg = _gw_cfg()
    retry = {"by": "retry"}
    for end in ("failed", "timeout", "stalled", "lost", "request_error", "killed"):
        t = _gw_task(p)
        _gw_run(p, t, end)
        assert glmwork.keeps_core(p.db, cfg, p.db.task(t["id"]), "standard", None, retry) == "", end
    done = _gw_task(p)
    _gw_run(p, done, "ok")   # ended clean with no hand-off: a finished attempt
    assert glmwork.keeps_core(p.db, cfg, p.db.task(done["id"]), "standard", None, retry) == "retry"
    handed = _gw_task(p)
    _gw_run(p, handed, "failed")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", ('{"status": "failed"}', handed["id"]))
    assert glmwork.keeps_core(p.db, cfg, p.db.task(handed["id"]), "standard", None, retry) == "retry"   # GLM finished and failed


def test_glm_work_only_an_instruction_to_a_person_keeps_a_run_on_core(env):
    from ttp import glmwork
    p = make(env)
    cfg = _gw_cfg()
    kc = lambda spec: glmwork.keeps_core(p.db, cfg, _gw_task(p, spec), "standard")
    for spec in ("Fix the comment handler so github threads render.",
                 "The code posts a message to slack on failure; fix its retry.",
                 "Refactor ttp notify output.", "Add a test for the Jira client's reply parsing."):
        assert kc(spec) == "", spec
    for spec in ("Then post a summary to the Slack channel.", "Please reply to the user with the numbers.",
                 "Run gh pr comment with the summary.", "Notify the user in chat when it lands.",
                 "Daily report. Read-only. The coordinator posts your result in chat; do not post to any chat "
                 "yourself.\nReturn: a one-line verdict first, then a small table.\n\nconclusion: the report text."):
        assert kc(spec) == "person", spec


def test_glm_work_a_timeout_or_crash_fallback_does_not_pin_the_next_run_to_core_but_a_bounce_does(env):
    import json
    from ttp import glmwork, route
    p = make(env)
    for cause, skipped in (("timeout", True), ("failed", True), ("no_handoff", False), (route.BOUNCE, False)):
        t = _gw_task(p)
        note = json.dumps({route.KEY: {"to": "chat", "fallback": cause}})
        p.db.x("INSERT INTO runs(task,role,dir,status,provider,note) VALUES(?,'worker','d','failed','chat',?)",
               (t["id"], note))
        assert route.fell_back_before(p.db, t["id"]) is not None, cause
        assert (route.fell_back_before(p.db, t["id"], glmwork.INFRA_CAUSES) is None) is skipped, cause


def test_a_run_that_started_on_the_glm_work_route_is_not_policy_bounced_after_a_rollback_took_the_route():
    import inspect
    from ttp import daemon
    fin = inspect.getsource(daemon.Daemon.finish_run)
    gate = fin[fin.index("bounce = self._policy_bounce(r)") - 400:fin.index("bounce = self._policy_bounce(r)")]
    # the run's own note names the route it took; the config at its end (the route rolled back mid-run) does not
    assert 'routed.get(glmwork.KEY) != "glm"' in gate and "not glmwork.settings(self.cfg)" in gate

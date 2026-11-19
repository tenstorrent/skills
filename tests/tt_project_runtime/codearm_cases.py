# Code route arm tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of codearm.py here.
from __future__ import annotations

_CA_GLM = {"provider": "chat", "model": "example-org/code-model", "effort": "max", "harness": "claude-code"}


def _ca_cfg(**over):
    return {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}},
            "route": {"code_sonnet": {"share": 1.0, "glm_share": 1.0, "glm": dict(_CA_GLM), **over}}}


def _ca_task(p, spec="tidy the helper and add a test", **kw):
    t = p.db.add_task("tidy the helper", spec, kind="code", tier="standard", priority=3, budget_usd=1.0, **kw)
    return p.db.task(t)


def test_code_arm_is_off_by_default_and_needs_a_share_and_a_model(monkeypatch):
    from ttp import codearm
    monkeypatch.setattr(codearm, "HARNESSES", ("claude-code",))   # a stand-in guarded harness
    assert codearm.settings({}) is None
    assert codearm.settings({"route": {"code_sonnet": {"glm_share": 0.5}}}) is None   # no model
    assert codearm.settings({"route": {"code_sonnet": {"glm": dict(_CA_GLM)}}}) is None   # no share
    assert codearm.settings({"route": {"code_sonnet": False, "glm_share": 1.0}}) is None
    s = codearm.settings(_ca_cfg(glm_share=0.5, glm_cap={"start": 9, "min": 2, "max": 6}))
    assert (s["share"], s["model"], s["harness"], s["cap"]) == (0.5, _CA_GLM["model"], "claude-code",
                                                                {"start": 6, "min": 2, "max": 6})


def test_code_arm_coin_is_seeded_and_follows_the_share():
    from ttp import codearm
    assert all(codearm.coin(i, 0.5, "s") == codearm.coin(i, 0.5, "s") for i in range(50))
    hits = sum(codearm.coin(i, 0.5) for i in range(2000))
    assert 850 < hits < 1150
    assert not any(codearm.coin(i, 0.0) for i in range(200)) and all(codearm.coin(i, 1.0) for i in range(200))
    assert [codearm.coin(i, 0.5, "a") for i in range(40)] != [codearm.coin(i, 0.5, "b") for i in range(40)]


def test_code_arm_keeps_resources_conclusions_forces_pins_devices_and_judgment_on_claude(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg()
    assert codearm.stays(p.db, cfg, _ca_task(p), "standard") == ""
    t = _ca_task(p)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["resource:box1"]), t["id"]))
    assert codearm.stays(p.db, cfg, p.db.task(t["id"]), "standard") == "resources"
    assert codearm.stays(p.db, cfg, _ca_task(p, "fix it\nconclusion: the verdict"), "standard") != ""
    assert codearm.stays(p.db, cfg, _ca_task(p, "fix it\ncode_arm: claude"), "standard") == "forced_claude"
    for spec in ("decide how to batch. There are no measurements that settle it. Weigh speed against reverts.",
                 "pick a lock design; the trade-off is latency", "choose between per-repo and per-area batches"):
        assert codearm.stays(p.db, cfg, _ca_task(p, spec), "standard") == "judgment", spec
    assert codearm.stays(p.db, cfg, _ca_task(p, "drop the stale lock after use; the spec decided the design"), "standard") == ""
    assert codearm.stays(p.db, cfg, _ca_task(p, model="opus"), "standard") == "user_pin"
    assert codearm.stays(p.db, cfg, _ca_task(p, "optimize the kernel's throughput"), "standard") != ""
    assert codearm.stays(p.db, cfg, _ca_task(p), "deep") == "tier"
    rv = p.db.task(p.db.add_task("Review #1: x", "", kind="review", tier="standard", priority=3, budget_usd=1.0))
    assert codearm.stays(p.db, cfg, rv, "standard") == "not_code"
    assert codearm.choose(p.db, cfg, rv, "standard") is None   # reviews and other judgment kinds never take it
    assert codearm.stays(p.db, _ca_cfg(glm={**_CA_GLM, "harness": "codex"}), _ca_task(p),
                         "standard") == "harness_unsupported"   # Codex joins once its command layer is guarded


def test_code_arm_is_recorded_once_and_follow_ups_keep_their_lineage_arm(env):
    from ttp import chaincap, codearm
    p = make(env)
    cfg = _ca_cfg()
    t = _ca_task(p)
    got = codearm.choose(p.db, cfg, t, "standard")
    assert (got["arm"], got["run"]) == ("glm", "glm")
    assert codearm.recorded(p.db, t["id"])["arm"] == "glm"
    # A later share change never re-deals a recorded task.
    assert codearm.choose(p.db, _ca_cfg(glm_share=0.0001, glm_seed="x"), t, "standard")["arm"] == "glm"
    fix = p.db.add_task("Fix review #9: tidy", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"{chaincap.FOLLOWS}{t['id']}"]), fix))
    got = codearm.choose(p.db, _ca_cfg(glm_share=0.0001), p.db.task(fix), "standard")
    assert got["arm"] == "glm" and "lineage" in got["why"]
    peek = _ca_task(p)
    assert codearm.choose(p.db, cfg, peek, "standard", peek=True)["arm"] == "glm"
    assert codearm.recorded(p.db, peek["id"]) is None
    assert codearm.show_line(codearm.recorded(p.db, t["id"])).startswith("code route arm: glm (coin at share 1)")


def test_needs_opus_bounce_reason_and_route_line():
    from ttp import codearm, route
    assert codearm.bounce_reason({"status": "blocked", "needs_opus": "a design choice"}) == "a design choice"
    assert codearm.bounce_reason({"status": "blocked", "summary": "needs Opus: talks to a person"}) == "talks to a person"
    assert codearm.bounce_reason({"status": "needs_opus", "summary": "root cause unclear"}) == "root cause unclear"
    assert codearm.bounce_reason({"status": "blocked", "summary": "waits on #12"}) == ""
    assert codearm.bounce_reason({"status": "done", "needs_opus": "x"}) == ""
    assert codearm.bounce_reason(None) == ""
    assert route.BOUNCE == codearm.BOUNCE
    assert '"needs_opus"' in codearm.contract() and "never run `ttp push`" in codearm.contract()
    assert "do not decide it yourself" in codearm.contract()


def test_a_bounce_keeps_the_task_on_opus_after(env):
    from ttp import codearm, route, runclass
    p = make(env)
    cfg = _ca_cfg()
    t = _ca_task(p)
    assert codearm.choose(p.db, cfg, t, "standard")["run"] == "glm"
    note = {route.KEY: {"to": "chat", "model": _CA_GLM["model"], "tier": "standard", codearm.KEY: "glm",
                        "fallback": route.BOUNCE, "bounce": "a design choice"}}
    p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
           (t["id"], "worker", "chat", _CA_GLM["model"], time.time() - 60, time.time(), "fallback", 0.0,
            json.dumps(note)))
    assert codearm.bounced(p.db, t["id"]) == "a design choice"
    got = codearm.choose(p.db, cfg, t, "standard")
    assert (got["arm"], got["run"]) == ("glm", "opus") and got["why"].startswith("needs Opus:")
    assert codearm.code_state({}, got) == {"arm": "opus"}
    assert runclass.code_route(cfg, t, None, codearm.code_state({}, got)) == (None, "needs_opus")


def test_code_arm_day_table_guard_and_review_lines(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg(glm_share=0.5, glm_guard_min=2)
    now = time.time()

    def arm_task(arm, land):
        t = _ca_task(p)
        p.db.set_kv(f"{codearm.ARM_KV}{t['id']}", {"arm": arm, "why": "coin", "seed": "", "ts": now})
        p.db.x("UPDATE tasks SET status=? WHERE id=?", ("done" if land else "failed", t["id"]))
        if land:
            p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) "
                   "VALUES(?,?,?,?,?,?,?,?)", (t["id"], f"b{t['id']}", "h", "main", "landed", now, now, "abc"))
        bridge = {"calls": 4, "in": 100, "cached": 900, "out": 50, "waits": 1, "refusals": 0, "wait_s": 2.0}
        p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
               (t["id"], "worker", "chat", "m", now - 60, now - 30, "ok", 0.0,
                json.dumps({codearm.KEY: {"arm": arm, "bridge": bridge}})))
    for _ in range(2):
        arm_task("sonnet", True)
        arm_task("glm", False)
    tab = codearm.day_table(p.db, cfg, now - 86400, now + 1)
    g, s = tab["arms"]["glm"], tab["arms"]["sonnet"]
    assert (g["tasks"], g["landed"], g["decided"], g["one_round_rate"]) == (2, 0, 2, 0.0)
    assert (s["tasks"], s["landed"], s["one_round_rate"]) == (2, 2, 1.0)
    assert (g["in"], g["cached"], g["out"], g["waits"]) == (200, 1800, 100, 2)
    lines = codearm.table_lines(tab)
    assert any(ln.startswith("- glm: 2 tasks") and "200 uncached + 1,800 cached" in ln for ln in lines)
    row = codearm.guard_run(p.db, cfg, now + 1)
    assert row["tripped"] and row["new_trip"] and "points below" in row["why"]
    assert codearm.held(p.db, cfg)
    assert codearm.guard_run(p.db, cfg, now + 2) is None   # once a day
    t = _ca_task(p)
    assert codearm.choose(p.db, cfg, t, "standard", peek=True)["run"] in ("claude", "sonnet")   # held: no glm run
    assert any("switched back" in ln for ln in codearm.review_lines(p.db, cfg, now + 1))
    assert not codearm.held(p.db, _ca_cfg(glm_share=0.4))   # a new setting lifts the hold
    assert codearm.review_lines(p.db, {}, now) == []


def test_code_arm_cap_moves_with_refusals_and_waits(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg()
    assert codearm.cap(p.db, cfg) == 4 and codearm.cap(p.db, {}) == 0
    now = time.time()

    def run(refusals, wait_s):
        p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
               (None, "worker", "chat", "m", now - 120, now - 60, "ok", 0.0, json.dumps(
                   {codearm.KEY: {"bridge": {"calls": 10, "refusals": refusals, "wait_s": wait_s}}})))
    run(0, 5.0)   # no refusals, 0.5 s a call: up one
    got = codearm.cap_tick(p.db, cfg, now)
    assert (got["cap"], got["was"]) == (5, 4) and "no refusals" in got["why"]
    assert codearm.cap_tick(p.db, cfg, now + 10) is None   # hourly
    p.db.x("UPDATE runs SET ended=?", (now + 3600,))
    run(2, 5.0)
    p.db.x("UPDATE runs SET ended=? WHERE id=(SELECT MAX(id) FROM runs)", (now + 3600,))
    got = codearm.cap_tick(p.db, cfg, now + 3700)   # refusals: down one
    assert (got["cap"], got["was"]) == (4, 5) and "2 refusals" in got["why"]
    assert codearm.cap(p.db, cfg) == 4
    assert not codearm.full(p.db, cfg)


def test_code_arm_cost_prices_cached_tokens_at_the_cache_read_rate():
    from ttp import codearm
    cfg = {"pricing": {"chat": {"default": [1.0, 0.1, 4.0]}}}
    assert codearm.cost(cfg, "chat", "m", {"in": 1_000_000, "cached": 1_000_000, "out": 1_000_000}) == 5.1
    assert codearm.cost({}, "chat", "m", {"in": 1}) is None


def test_task_show_prints_the_code_arm(env, monkeypatch, capsys):
    from ttp import cli, codearm
    p = make(env)
    t = _ca_task(p)
    p.db.set_kv(f"{codearm.ARM_KV}{t['id']}", {"arm": "sonnet", "why": "coin at share 0.5", "seed": "", "ts": 1.0})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.delenv("TTP_TASK", raising=False)
    capsys.readouterr()
    cli.main(["task", "show", str(t["id"])])
    assert "code route arm: sonnet (coin at share 0.5)" in capsys.readouterr().out
    cli.main(["task", "show", str(t["id"]), "--json"])
    assert json.loads(capsys.readouterr().out)[codearm.KEY]["arm"] == "sonnet"


def _ca_run(p, task_id, result, role="worker"):
    """A finished run of the task whose run dir holds `result` as its result.json; returns the run dir."""
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,ended,status) VALUES(?,?,?,?,?,?)",
                 (task_id, role, "chat", time.time() - 60, time.time(), "done"))
    d = p.runs / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(result))
    return d


def test_handed_back_reads_the_runs_result_and_the_latest_worker_run_counts(env):
    from ttp import codearm
    p = make(env)
    t = _ca_task(p)["id"]
    d = _ca_run(p, t, {"status": "blocked", "needs_opus": "a design choice"})
    assert codearm.handed_back(d) == "a design choice"
    assert codearm.handed_back(d / "missing") == "" and codearm.handed_back(None) == ""
    (d / "result.json").write_text("not json")
    assert codearm.handed_back(d) == ""
    (d / "result.json").write_text(json.dumps({"status": "blocked", "needs_opus": "a design choice"}))
    assert codearm.task_handed_back(p, t) == "a design choice"
    _ca_run(p, t, {"status": "done", "summary": "did the work on Opus"})   # requeued on Claude: it may push
    assert codearm.task_handed_back(p, t) == ""
    assert codearm.task_handed_back(p, 987654) == "" and codearm.task_handed_back(p, "x") == ""


def test_ttp_push_from_a_run_that_handed_back_is_refused_before_it_touches_anything(env, monkeypatch, capsys):
    from ttp import cli, push
    p = make(env)
    t = _ca_task(p)["id"]
    d = _ca_run(p, t, {"status": "blocked", "needs_opus": "talks to a person"})
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(d))
    monkeypatch.setattr(push, "run", lambda *a, **k: pytest.fail("a hand-back reached the push"))
    monkeypatch.setattr(push, "detach", lambda *a, **k: pytest.fail("a hand-back reached the push"))
    for argv in (["push"], ["push", "--detach"], ["push", "--own"]):
        capsys.readouterr()
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == push.REFUSED
        err = capsys.readouterr().err
        assert "handed its task back to Opus" in err and "talks to a person" in err


def test_the_arm_owns_every_run_of_a_code_task_light_wakes_and_the_tier_route_included(env):
    """With the arm on, a claude-arm task's runs never take the old tier route to the chat provider, and a glm
    task's light wakes go to its arm; a tier-up run of a glm task and a task without an arm are as before."""
    from ttp import codearm, daemon, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")   # a live arm needs the project's policy list
    cfg = {**_ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"}), "providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1",
                                                "tiers": {"light": "example-org/light-model"}}},
           "route": {**_ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"})["route"], "classes": {"light": {"to": "chat", "model": "example-org/light-model"}}}}
    d = daemon.Daemon.__new__(daemon.Daemon)
    d.p, d.cfg = p, cfg
    d._model_pick = lambda *a, **k: None
    glm, forced = _ca_task(p), _ca_task(p, "tidy it\ncode_arm: claude")
    for t in (glm, forced):
        codearm.choose(p.db, cfg, t, "standard")
    assert codearm.recorded(p.db, forced["id"])["arm"] == "claude"
    for tier in ("standard", "light"):   # the old tier route sent a light wake to chat
        assert d._route(forced, tier, None, 1.0, cls="light") is None, tier
    for tier in ("standard", "light"):
        got = d._route(glm, tier, None, 1.0, cls="light")
        assert got and got["to"] == "chat" and got["model"] == _CA_GLM["model"] and got[codearm.KEY] == "glm", tier
    assert codearm.choose(p.db, cfg, glm, "light")["run"] == "glm"
    assert codearm.choose(p.db, cfg, glm, "deep")["run"] == "claude"   # a tier-up run stays on Claude
    assert d._route(glm, "deep", None, 1.0, cls="hard") is None
    off = {k: v for k, v in cfg.items() if k != "route"} | {"route": {"classes": cfg["route"]["classes"]}}
    assert codearm.choose(p.db, off, glm, "light") is None   # arm off: the old route applies


def test_a_live_share_on_a_harness_that_is_not_guarded_is_zero_and_logged_once(env, monkeypatch):
    from ttp import codearm
    monkeypatch.setattr(codearm, "HARNESSES", ())   # the shipped default: no guarded harness yet
    p = make(env)
    cfg = _ca_cfg()
    assert codearm.settings(cfg) is None and "claude-code" in codearm.refusal(cfg)
    assert codearm.choose(p.db, cfg, _ca_task(p), "standard") is None   # the task keeps its normal route
    logged: list = []
    assert codearm.log_refusal(p.db, cfg, logged.append) and len(logged) == 1
    codearm.log_refusal(p.db, cfg, logged.append)
    assert len(logged) == 1   # once per setting
    cfg2 = _ca_cfg(glm_share=0.4)
    codearm.log_refusal(p.db, cfg2, logged.append)
    assert len(logged) == 2   # a changed setting logs again
    assert codearm.refusal({}) == "" and codearm.refusal(_ca_cfg(glm_share=0)) == ""   # off: nothing to refuse
    monkeypatch.setattr(codearm, "HARNESSES", ("claude-code",))
    assert codearm.refusal(cfg) == "" and codearm.settings(cfg)["share"] == 1.0   # a guarded harness runs live


# The side-effect guard of a GLM-arm run (glmguard.py): model-free, in the CLI, the hook and the harness's command layer.
_GG_RUN_ENV = {"TTP_CODE_ARM_GUARD": "1"}


def _gg_run(tmp_path):
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    return wt, rd


def test_glm_guard_is_on_for_an_arm_run_by_its_variable_or_its_marker_and_off_for_other_runs(tmp_path):
    from ttp import codearm, glmguard
    glm = {codearm.KEY: {"arm": "glm", "run": "glm"}}
    assert glmguard.env_for(glm) == _GG_RUN_ENV
    for note in (None, {}, {codearm.KEY: {"arm": "glm", "run": "opus"}}, {codearm.KEY: {"arm": "sonnet", "run": "sonnet"}}):
        assert glmguard.env_for(note) == {}, note
    wt, rd = _gg_run(tmp_path)
    assert not glmguard.active({}) and not glmguard.active({"TTP_RUN_DIR": str(rd)})
    assert glmguard.active(_GG_RUN_ENV)
    glmguard.mark(rd)   # a run that unsets its variable is still guarded
    assert glmguard.active({"TTP_RUN_DIR": str(rd)})


@_pytest.mark.parametrize("cmd,action,refused", [
    ("push", "", False), ("checks", "", False), ("lock", "", False), ("detach", "", False), ("task", "show", False),
    ("task", "list", False), ("note", "", False), ("say", "", True), ("ask", "", True), ("notify", "", True),
    ("upstream", "", True), ("task", "add", True), ("task", "steer-me", True), ("obligation", "", True),
    ("memory", "", True), ("config", "", True), ("secret", "", True), ("issues", "", True)])
def test_glm_guard_cli_allows_checks_and_push_and_refuses_everything_that_reaches_someone(cmd, action, refused):
    from ttp import glmguard
    assert bool(glmguard.cli_refusal(cmd, action)) is refused, (cmd, action)


def test_glm_guard_cli_gate_ends_the_run_as_a_bounce_to_opus_and_a_normal_run_is_untouched(env, monkeypatch, tmp_path, capsys):
    import json
    from ttp import cli, codearm, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.delenv("TTP_CODE_ARM_GUARD", raising=False)
    cli.glm_gate(type("A", (), {"cmd": "notify", "action": ""})())   # not a GLM-arm run: nothing happens
    assert not (rd / glmguard.HIT).exists()
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    cli.glm_gate(type("A", (), {"cmd": "checks", "action": ""})())   # an allowed command passes
    with _pytest.raises(SystemExit) as e:
        cli.glm_gate(type("A", (), {"cmd": "notify", "action": ""})())
    assert e.value.code == 2 and "GLM arm" in capsys.readouterr().err
    res = json.loads((rd / "result.json").read_text())
    assert codearm.bounce_reason(res).startswith("guard: ") and res["status"] == "blocked"
    assert glmguard.hit_reason(rd).startswith("`ttp notify`")


@_pytest.mark.parametrize("argv", [["checks"], ["checks", "--dry-run"], ["checks", "--fresh", "--", "true"],
                                    ["checks", "--result", "x"]])
def test_glm_guard_lets_a_glm_run_run_its_checks_in_every_form(argv, env, monkeypatch, tmp_path):
    """cell guard_checks: `ttp checks` stores the commands after -- as a.cmd, which once hid its name."""
    from ttp import cli, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    ran = []
    monkeypatch.setattr(cli, "cmd_checks", lambda a: ran.append(a))
    cli.main(argv)
    assert len(ran) == 1 and not (rd / glmguard.HIT).exists(), argv
    monkeypatch.chdir(wt)
    assert glmguard.shell_refusal(f"cd {wt} && ttp {' '.join(argv)} 2>&1 | tail -20", str(wt), str(rd),
                                  {"TTP_PROJECT": str(p.base), **_GG_RUN_ENV}) is None


class _GatesPassed(BaseException):
    pass


@_pytest.mark.parametrize("name", sorted(__import__("ttp.glmguard", fromlist=["x"]).SUBS_ALLOWED))
def test_glm_guard_passes_every_allowed_command_with_and_without_arguments(name, env, monkeypatch, tmp_path):
    """cell guard_checks: `checks`, `clip`, `lock`, `detach` and `pool-*` keep the commands after -- in a.cmd."""
    from ttp import cli, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    monkeypatch.setattr(cli, "_recover_setup", lambda: (_ for _ in ()).throw(_GatesPassed()))
    parsed = 0
    for argv in ([name], [name, "show", "1"], [name, "live"], [name, "--", "true"], [name, "x", "--", "true"], [name, "x", "--", "sh", "-c", "true"]):
        try:
            cli.main(argv)
        except _GatesPassed:
            parsed += 1
        except SystemExit as e:
            assert e.code != 2 or not (rd / glmguard.HIT).exists(), (argv, glmguard.hit_reason(rd))
    assert parsed and not (rd / glmguard.HIT).exists(), (name, glmguard.hit_reason(rd))


def _gg_env(env, monkeypatch, tmp_path):
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    return p, wt, rd


def test_a_glm_runs_plain_note_goes_only_to_its_own_task_log(env, monkeypatch, tmp_path):
    """cells: glm_note_plain, glm_note_log_only (user msg #3512: ttp note goes only to the task's own log)."""
    from ttp import cli, glmguard, upstream
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    before = p.db.one("SELECT COUNT(*) n FROM messages")["n"]
    monkeypatch.setattr(upstream, "maybe_fallback", lambda *a, **k: _pytest.fail("a GLM note sent notes on"))
    monkeypatch.setattr(cli, "_note_to", lambda *a, **k: _pytest.fail("a GLM note reached another project"))
    cli.main(["note", "milestone: built the parser"])
    assert "milestone: built the parser" in (rd / "progress.md").read_text()
    assert not (rd / glmguard.HIT).exists() and not (rd / "result.json").exists()
    assert p.db.one("SELECT COUNT(*) n FROM messages")["n"] == before, "no chat outbox row"
    assert sorted(x.name for x in rd.iterdir()) == ["progress.md"], "no notify, escalation or peer note file"
    assert glmguard.shell_refusal('ttp note "update: ack" 2>&1', str(wt), str(rd), {"TTP_PROJECT": str(p.base),
                                                                                 **_GG_RUN_ENV}) is None


@_pytest.mark.parametrize("argv,route", [
    (["note", "--to", "other", "hi"], "--to"), (["note", "--to=other", "hi"], "--to"),
    (["note", "--reply", "abc123", "hi"], "--reply"), (["note", "--rep", "abc123", "hi"], "--reply"),
    (["note", "--to", "other", "--urgent", "hi"], "--to"), (["note", "--confirms", "abc123", "works"], "--confirms"),
    (["note", "--severity", "high", "hi"], "--severity"), (["note", "--status", "7"], "--status")])
def test_glm_guard_refuses_every_route_from_a_note_to_a_person_or_another_project(argv, route, env, monkeypatch,
                                                                                tmp_path, capsys):
    """cells: glm_note_to_refused, glm_note_routes_refused"""
    from ttp import cli, glmguard
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    with _pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2 and f"`ttp note {route}`" in capsys.readouterr().err
    assert glmguard.hit_reason(rd).startswith(f"`ttp note {route}`")
    words = " ".join(f"'{w}'" if " " in w else w for w in argv)
    assert glmguard.shell_refusal(f"ttp {words}", str(wt), str(rd), {"TTP_PROJECT": str(p.base), **_GG_RUN_ENV})
    assert set(glmguard.NOTE_ROUTES) >= {"--" + x.dest for x in _note_options() if x.dest != "text"}, \
        "a new `ttp note` flag is refused on the GLM arm until it is judged"


def _note_options():
    import argparse
    from ttp import cli
    seen = {}
    real = argparse.ArgumentParser.parse_args

    def grab(self, *a, **k):
        seen.update(self._subparsers._group_actions[0].choices)
        raise SystemExit(0)
    try:
        argparse.ArgumentParser.parse_args = grab
        cli.main(["note", "x"])
    except SystemExit:
        pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return [x for x in seen["note"]._actions if x.dest != "help"]


def test_glm_guard_refuses_notify(env, monkeypatch, tmp_path):
    """cells: glm_notify_refused"""
    from ttp import cli, glmguard
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    with _pytest.raises(SystemExit):
        cli.main(["notify", "done"])
    assert glmguard.hit_reason(rd).startswith("`ttp notify`") and not (rd / "notify.jsonl").exists()


def test_the_glm_prompt_names_only_commands_its_guard_allows_and_says_where_notes_go():
    """cells: prompt_commands_allowed, glm_prompt_note_rule: the prompt's list is built from the guard's own."""
    import re
    from ttp import codearm, glmguard
    text = codearm.contract()
    told, never = text.split("Never use", 1)
    named = set(re.findall(r"`ttp ([a-z][\w-]*)", told))
    assert named >= glmguard.SUBS_ALLOWED - {"advise"} and "note" in named   # advise: told only when on
    for cmd in named:
        assert glmguard.cli_refusal(cmd, "show") is None, cmd
    assert "goes only to your task's own log" in told and "coordinator decides what reaches a chat" in told
    assert "`ttp notify`" in never and "`ttp note --to`" in never and "--reply" in never and "refused" in never
    assert glmguard.cli_refusal("notify") and glmguard.cli_refusal("note", "", ["--to"])
    assert glmguard.cli_refusal("note", "", ["--reply"])


@_pytest.mark.parametrize("argv,name", [(["note", "--to", "other", "hi"], "note --to"), (["notify", "hi"], "notify"),
                                         (["task", "add", "x"], "task add"), (["say", "hi"], "say")])
def test_glm_guard_refusal_names_the_command_it_refused(argv, name, env, monkeypatch, tmp_path, capsys):
    from ttp import cli, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    with _pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2 and f"`ttp {name}`" in capsys.readouterr().err
    assert glmguard.hit_reason(rd).startswith(f"`ttp {name}`")
    assert glmguard.cli_refusal("", "").startswith("a ttp call naming no command")


def test_glm_guard_keeps_a_hand_off_the_run_wrote_before_it_was_stopped(tmp_path):
    import json
    from ttp import glmguard
    _wt, rd = _gg_run(tmp_path)
    (rd / "result.json").write_text(json.dumps({"status": "done", "summary": "built"}))
    glmguard.hit(rd, "`ttp note` is not allowed")
    assert json.loads((rd / glmguard.KEPT).read_text())["status"] == "done"
    assert json.loads((rd / "result.json").read_text())["needs_opus"].startswith("guard: ")
    glmguard.hit(rd, "second")   # the first kept hand-off stays
    assert json.loads((rd / glmguard.KEPT).read_text())["status"] == "done"
    glmguard.hit("", "nothing")   # no run folder: refuses without raising


@_pytest.mark.parametrize("cmd", [
    "ttp note --to other 'hi'", "ttp ask 'ok?'", "ttp notify 'x'", "ttp some-project note hi", "ttp other-project checks", "ttp task add t s",
    "ttp upstream 'x'", "git push origin HEAD", "git -C . push --force", "gh pr comment 1 --body x",
    "gh issue create -t x", "gh api repos/o/r/issues -f title=x", "curl -X POST https://x.invalid -d a=b",
    "curl --data a=b https://x.invalid", "ssh host true", "mail -s x a@b", "sqlite3 $TTP_PROJECT/state/project.db 'x'",
    "unset TTP_CODE_ARM_GUARD; ttp notify x", "TTP_CODE_ARM_GUARD=0 ttp notify x", "env -i ttp notify x",
    "rm $TTP_RUN_DIR/glm_guard.on", "echo x > /etc/hosts-copy", "cd /tmp/elsewhere-x && echo x > ~/note.txt",
    "bash -c 'ttp notify x'", "bash -c 'ttp note --to other x'", "ttp note --reply abc x",
    "cat > ~/.tt-project/machine-workers.json"])
def test_glm_guard_refuses_a_shell_command_that_reaches_a_person_a_project_or_something_outside_the_worktree(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", [
    "ttp checks", "ttp push", "ttp proj checks --detach", "ttp note 'update: ack'", "bash -c 'ttp note x'", "git status", "git commit -am x", "git diff HEAD~1",
    "python3 -m pytest -q tests", "echo hi > out.txt", "cat a | grep b > $TTP_RUN_DIR/notes.txt", "gh pr view 1",
    "gh api repos/o/r/pulls/1", "curl -s https://x.invalid/doc", "ttp task show 5", "ttp lock acquire x -- true",
    "ls /tmp", "sed -i s/a/b/ src/mod.py"])
def test_glm_guard_allows_the_commands_a_build_needs(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", [
    "ttp checks 2>&1 | tail -3", "ttp checks >$TTP_RUN_DIR/checks.log 2>&1", "ttp 2>&1 checks", "ttp task show 5 2>/dev/null",
    "ttp push 2>&1 | tail -5"])
def test_glm_guard_reads_a_ttp_call_without_its_redirections(cmd, tmp_path):
    """cell guard_redirect: the fd digit of `2>&1` is no word of the call."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", ["ttp notify hi 2>&1", "ttp 2>/dev/null notify hi", "ttp note --to x hi 2>&1", "ttp remove x >/dev/null"])
def test_glm_guard_still_refuses_a_redirected_ttp_call_it_would_refuse_bare(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", [
    "echo TTP_RUN_DIR=$TTP_RUN_DIR", 'echo "TTP_TASK=$TTP_TASK run=$TTP_RUN_ID"', "printenv TTP_PROJECT",
    "grep TTP_TASK= notes.txt", "ttp paths"])
def test_glm_guard_allows_reading_the_runs_identity_and_ttp_paths(cmd, tmp_path):
    """cells guard_echo_env, guard_paths."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", [
    "TTP_RUN_DIR=/tmp/x ttp push", "export TTP_TASK=3", "env TTP_PROJECT=x ttp push", "FOO=1 TTP_RUN_ID=2 ttp push",
    "true; TTP_TASK=1 ttp push", "bash -c 'TTP_TASK=2 ttp push'", "eval TTP_TASK=2", "sudo env TTP_TASK=1 ttp push",
    "read TTP_TASK <<< 3", "printf -v TTP_TASK 3", "declare -x TTP_RUN_DIR=/x", "(TTP_TASK=1 ttp push)",
    "echo $(TTP_TASK=1 ttp push)", "if true; then TTP_TASK=1; fi", "TTP_TASK+=1", "echo x && TTP_CODE_ARM_GUARD= ttp push"])
def test_glm_guard_refuses_every_real_edit_of_the_runs_identity(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


def test_glm_guard_refuses_a_file_write_outside_the_worktree_and_any_connector_tool(tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    env_ = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}

    def ask(tool, args):
        return glmguard.tool_refusal({"tool_name": tool, "tool_input": args, "cwd": str(wt)}, env_)
    assert ask("Write", {"file_path": str(wt / "a.py"), "content": "x"}) is None
    assert ask("Edit", {"file_path": "sub/a.py"}) is None
    assert ask("Write", {"file_path": str(rd / "notes.md")}) is None
    assert "outside its worktree" in ask("Write", {"file_path": str(tmp_path / "other" / "a.py")})
    assert "outside its worktree" in ask("Edit", {"file_path": "../escape.py"})
    assert ask("mcp__slack__post_message", {"text": "x"}) and ask("Read", {"file_path": "/etc/hostname"}) is None
    assert ask("Bash", {"command": "ttp notify hi"}) and ask("Bash", {"command": "ttp checks"}) is None
    assert ask("Bash", {"command": "ttp note hi"}) is None and ask("Bash", {"command": "ttp note --to x hi"})


def test_glm_guard_hook_denies_a_refused_call_and_ends_the_run_as_a_bounce(monkeypatch, tmp_path):
    import json
    from ttp import codearm, hook
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ttp note --to other hi"}, "cwd": str(wt)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "goes back to Opus" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert codearm.bounce_reason(json.loads((rd / "result.json").read_text())).startswith("guard: ")
    monkeypatch.delenv("TTP_CODE_ARM_GUARD")
    (rd / "glm_guard.json").unlink(), (rd / "result.json").unlink()
    hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ttp note --to other hi"}, "cwd": str(wt)})
    assert not (rd / "result.json").exists()   # not a GLM-arm run: the guard does not apply


def test_glm_guard_hit_is_read_as_a_bounce_by_push_and_the_runner_watches_for_it(tmp_path):
    import inspect
    from ttp import codearm, glmguard, runner
    _wt, rd = _gg_run(tmp_path)
    assert glmguard.hit_reason(rd) == ""
    glmguard.hit(rd, "`ttp say` is not allowed")
    assert glmguard.hit_reason(rd) == "`ttp say` is not allowed"
    assert codearm.handed_back(rd).startswith("guard: ")   # ttp push refuses and the push queue skips its head
    assert "glmguard.hit_reason(run_dir)" in inspect.getsource(runner)


# The arm's live harness (glmharness.py, glmenv.py): mini-swe-agent with the guard in its command layer.
def test_the_shipped_guarded_harness_is_mini_swe_agent_and_claude_code_stays_dry_run_only():
    from ttp import codearm, glmharness
    assert codearm.HARNESSES == (glmharness.HARNESS,) == ("mini-swe-agent",)
    live = _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"})
    assert codearm.refusal(live) == "" and codearm.settings(live)["harness"] == "mini-swe-agent"
    assert codearm.harness(live) == "mini-swe-agent" and codearm.harness({}) == "claude-code"
    why = codearm.refusal(_ca_cfg())   # claude-code with a live share
    assert "claude-code" in why and "mini-swe-agent" in why and codearm.settings(_ca_cfg()) is None
    assert "codex" in codearm.refusal(_ca_cfg(glm={**_CA_GLM, "harness": "codex"}))


def test_glmenv_refuses_what_the_guard_refuses_and_a_secret_read_and_allows_ordinary_work(tmp_path):
    from ttp import glmenv
    wt, rd = _gg_run(tmp_path)
    env_ = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}

    def ask(cmd):
        return glmenv.refusal(cmd, str(wt), str(rd), env_)
    assert ask("python3 -m pytest -q tests && git status") is None
    assert ask("ttp checks") is None
    assert ask("ttp note --to other hi") and ask("git push origin HEAD") and ask("echo x > /etc/hosts-copy")
    assert ask("cat " + "~/." + "ssh/" + "id_" + "rsa")   # a stored private key


def test_the_guarded_environment_ends_a_run_on_a_refused_command_and_runs_the_rest(tmp_path, monkeypatch):
    import json
    from ttp import codearm, glmenv
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    ran: list = []

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            ran.append(action["command"])
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()   # the guard sits before the base in the MRO
    assert env.execute({"command": "ls"}, str(wt))["returncode"] == 0 and ran == ["ls"]
    with pytest.raises(glmenv.Submitted) as ei:
        env.execute({"command": "ttp note --to other hi"}, str(wt))
    assert ran == ["ls"]   # the refused command never ran
    assert ei.value.messages[0]["extra"]["exit_status"] == "GuardRefused"
    assert codearm.bounce_reason(json.loads((rd / "result.json").read_text())).startswith("guard: ")


def test_glmharness_writes_a_guarded_mini_run_with_the_prompt_on_stdin_and_the_bridge_as_its_model(tmp_path):
    from ttp import glmenv, glmharness
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("do the task")
    (rd / "system.md").write_text("You are a worker.")
    spec = {"argv": ["claude"], "stdin": "{json}", "model": "example-org/code-model", "budget_usd": None}
    inst = {"kind": "mini-swe-agent", "version": "2.4.6", "bin": "/opt/harness/bin/mini"}
    got = glmharness.apply(spec, rd, {}, "/opt/rt", budget_usd=3.0, install_fn=lambda v: inst)
    assert got == "" and spec["argv"][0] == "/bin/sh" and "stdin" not in spec
    assert spec["openai_env"] is True and spec["budget_usd"] == 3.0
    assert spec["harness"] == {"kind": "mini-swe-agent", "version": "2.4.6"}
    prompt = (rd / "prompt.md").read_text()
    assert prompt.startswith("You are a worker.") and "do the task" in prompt
    conf = (rd / "mini.yaml").read_text()
    assert glmenv.CLASS_PATH in conf and "timeout: 900" in conf
    sh = (rd / "harness.sh").read_text()
    assert "openai/example-org/code-model" in sh and "PYTHONPATH=/opt/rt" in sh and "/opt/harness/bin/mini" in sh


def test_a_harness_that_cannot_be_installed_hands_the_task_back_to_opus_not_a_failed_attempt(tmp_path):
    import json
    from ttp import codearm, glmharness
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("x")
    spec = {"argv": ["claude"], "model": "m"}

    def boom(v):
        raise RuntimeError("pip is not available")
    why = glmharness.apply(spec, rd, {}, "/opt/rt", install_fn=boom)
    assert why.startswith("harness unavailable") and spec["argv"] == ["/bin/sh", "-c", "exit 0"]
    got = json.loads((rd / "result.json").read_text())
    assert codearm.bounce_reason(got).startswith("guard: harness unavailable")


def test_a_mini_run_is_costed_from_the_bridge_with_cached_tokens_at_the_cache_read_rate():
    import json
    from ttp import glmharness

    class Prov:
        model = "example-org/code-model"

        def price(self):
            return (1.0, 0.1, 4.0)   # $/M: input, cached input, output

    class Bridge:
        def stats(self):
            return {"usage": {"calls": 3, "input_tokens": 1_000_000, "output_tokens": 500_000,
                              "cache_read_input_tokens": 2_000_000}}

    class NoCalls:
        def stats(self):
            return {}
    assert abs(glmharness.cost_so_far(Bridge(), Prov()) - (1.0 + 0.2 + 2.0)) < 1e-6   # cached at 0.1, not 1.0
    line = json.loads(glmharness.result_line(Bridge().stats(), Prov()))
    assert line["type"] == "result" and abs(line["total_cost_usd"] - 3.2) < 1e-6
    assert line["usage"]["cache_read_input_tokens"] == 2_000_000 and line["usage"]["input_tokens"] == 1_000_000
    assert glmharness.result_line({"usage": {}}, Prov()) == "" and glmharness.cost_so_far(NoCalls(), Prov()) is None


def test_the_daemon_wires_the_live_harness_in_and_the_runner_costs_it():
    import inspect
    from ttp import daemon, runner
    src = inspect.getsource(daemon)
    assert "glmharness.apply(spec" in src and "codearm.harness(self.cfg) == glmharness.HARNESS" in src
    rsrc = inspect.getsource(runner)
    assert "glmharness.cost_so_far" in rsrc and "glmharness.write_result" in rsrc


def test_config_and_doctor_say_why_a_live_share_on_claude_code_is_zero():
    import inspect
    from ttp import cli
    assert "codearm.refusal(p.config()" in inspect.getsource(cli.cmd_config)
    assert "codearm.refusal(p.config()" in inspect.getsource(cli.cmd_doctor)


# The policy file list (policyfiles.py): the code that decides what needs an audit record, review, approval, budget or push.
_PF_LIST = ["src/rt/coordfix.py", "src/rt/daemon.py", "src/rt/*guard.py", "docs/policy/**", "gate.py"]


def test_policy_list_loads_comments_and_globs_and_a_missing_list_is_empty(tmp_path):
    from ttp import policyfiles
    assert policyfiles.load(tmp_path) == []
    (tmp_path / policyfiles.FILE).write_text("# a comment\n\nsrc/a.py  # why\n/lead/b.py\nsrc/**/c.py\n")
    got = policyfiles.load(tmp_path)
    assert got == ["src/a.py", "lead/b.py", "src/**/c.py"]
    assert policyfiles.matches(got, "src/a.py") and policyfiles.matches(got, "src/x/y/c.py")
    assert policyfiles.matches(got, "src/c.py") and not policyfiles.matches(got, "other/src/a.py")
    assert policyfiles.matches(["gate.py"], "deep/dir/gate.py")   # no slash: any depth
    assert not policyfiles.matches(["src/*.py"], "src/sub/a.py") and policyfiles.matches(["x.py"], "./x.py")
    (tmp_path / policyfiles.FILE).write_text("# only comments\n")
    assert policyfiles.load(tmp_path) == []


def test_a_spec_about_a_policy_file_or_term_is_policy_class_and_replay_14s_case_is():
    from ttp import policyfiles
    t = lambda title, spec="": {"title": title, "spec": spec}   # noqa: E731
    # replay 14: a spec about one function of a listed module
    assert "coordfix" in policyfiles.task_reason(_PF_LIST, t("rewrite coordfix._python_reads", "classify the writes"))
    assert policyfiles.task_reason(_PF_LIST, t("x", "Where: src/rt/daemon.py, finish_run"))
    assert policyfiles.task_reason(_PF_LIST, t("x", "edit rt/daemon.py"))   # a path tail
    assert policyfiles.task_reason(_PF_LIST, t("x", "the file docs/policy/review.md changes"))   # a glob
    assert policyfiles.task_reason(_PF_LIST, t("x", "touch gate.py"))
    for term in ("add an audit record for each refusal", "tighten the review gate", "the budget cap check",
                 "change the push queue batching", "the ask gate wording", "a charter gate for tags"):
        assert policyfiles.task_reason([], t("x", term)).startswith("policy term"), term
    # ordinary specs, including the boilerplate every spec carries, are not policy
    assert not policyfiles.task_reason(_PF_LIST, t("tidy the helper", "rename a variable in util.py; land with `ttp push`; "
                                                                       "the approved head is pushed. See Daemon docs."))
    assert not policyfiles.task_reason(_PF_LIST, t("x", "uses os.path.join and json.dumps in helpers.py"))


def test_a_policy_class_task_never_takes_the_coin_and_shows_opus_with_the_reason(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"})
    ok = _ca_task(p)
    assert codearm.choose(p.db, cfg, ok, "standard", policy=_PF_LIST)["arm"] == "glm"
    bad = _ca_task(p, "rewrite coordfix._python_reads so a write is never labelled a read")
    got = codearm.choose(p.db, cfg, bad, "standard", policy=_PF_LIST)
    assert got["arm"] == "claude" and got["run"] == "claude" and got["why"].startswith("policy: names coordfix._python_reads")
    assert codearm.recorded(p.db, bad["id"])["why"].startswith("policy:")   # recorded once, read by every run
    assert codearm.show_line(codearm.recorded(p.db, bad["id"])).startswith("code route arm: opus (policy: ")
    assert codearm.show_line({"arm": "claude", "why": "judgment"}) == "code route arm: claude (judgment)"
    # a task that gets a policy spec after its coin keeps no GLM run
    later = _ca_task(p)
    assert codearm.choose(p.db, cfg, later, "standard", policy=_PF_LIST)["run"] == "glm"
    p.db.x("UPDATE tasks SET spec=? WHERE id=?", ("now also change the review gate", later["id"]))
    again = codearm.choose(p.db, cfg, p.db.task(later["id"]), "standard", policy=_PF_LIST)
    assert again["arm"] == "glm" and again["run"] == "claude" and "policy term" in again["why"]


def test_a_project_without_a_policy_list_cannot_run_the_arm_live_and_logs_it_once(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"})
    assert codearm.settings(cfg, []) is None and codearm.settings(cfg, ["a.py"]) is not None
    assert codearm.settings(cfg) is not None   # not checked: the daemon and the CLI always pass the list
    why = codearm.refusal(cfg, [])
    assert "no policy file list" in why and "policy_files.txt" in why and codearm.refusal(cfg, ["a.py"]) == ""
    assert codearm.refusal(_ca_cfg(glm_share=0.0), []) == ""   # nothing live, nothing to refuse
    logged: list = []
    assert codearm.log_refusal(p.db, cfg, logged.append, []) == why and len(logged) == 1
    codearm.log_refusal(p.db, cfg, logged.append, [])
    assert len(logged) == 1
    assert codearm.choose(p.db, cfg, _ca_task(p), "standard", policy=[]) is None   # every task keeps its route


def test_a_diff_that_touches_a_policy_file_is_refused_with_its_tip_kept_and_the_branch_reset(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from ttp import policyfiles, worktree

    def git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (repo / "src" / "rt").mkdir(parents=True)
    (repo / "src/rt/coordfix.py").write_text("a = 1\n")
    (repo / "src/rt/util.py").write_text("b = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", f"{worktree.FORK_POINT_REF}7", base)
    git("checkout", "-qb", "ttp/t7-x")
    (repo / "src/rt/coordfix.py").write_text("a = 2\n")
    (repo / "src/rt/util.py").write_text("b = 2\n")
    git("commit", "-qam", "glm change")
    tip = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    p = SimpleNamespace(root=repo, worktrees=tmp_path / "no-worktrees")
    task = {"id": 7, "branch": "ttp/t7-x"}
    changed = {"src/rt/util.py": 2, "src/rt/coordfix.py": 2}
    assert policyfiles.bounce_diff(p, task, ["src/rt/other.py"], changed) == ""   # none listed: no refusal, no reset
    assert git("rev-parse", "ttp/t7-x") == tip
    why = policyfiles.bounce_diff(p, task, _PF_LIST, changed)
    assert why == "policy files touched: src/rt/coordfix.py"
    assert git("rev-parse", f"{policyfiles.EVIDENCE_REF}7") == tip   # the GLM tip is kept as evidence
    assert git("rev-parse", "ttp/t7-x") == base   # the Opus run starts from the fork point, not the GLM commits
    assert policyfiles.bounce_diff(p, task, _PF_LIST, None) == ""


def test_the_daemon_bounces_a_glm_run_that_touched_a_policy_file_and_the_next_run_is_opus(env, monkeypatch):
    import inspect
    from types import SimpleNamespace
    from ttp import daemon, policyfiles, worktree
    src = inspect.getsource(daemon.Daemon.finish_run)
    assert 'routed.get(codearm.KEY) == "glm"' in src and "self._policy_bounce(r)" in src
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/rt/coordfix.py\n")
    t = _ca_task(p)
    p.db.x("UPDATE tasks SET branch='ttp/t9-x' WHERE id=?", (t["id"],))
    seen: list = []
    monkeypatch.setattr(policyfiles, "task_diff", lambda pp, task, br: (pp.root, {"src/rt/coordfix.py": 3, "README.md": 1}, ""))
    monkeypatch.setattr(policyfiles, "bounce_diff", lambda pp, task, pats, ch: seen.append((pats, ch)) or "policy files touched: src/rt/coordfix.py")
    monkeypatch.setattr(daemon, "log", lambda pp, m: seen.append(m))
    me = SimpleNamespace(p=p)
    got = daemon.Daemon._policy_bounce(me, {"id": 1, "task": t["id"]})
    assert got == "policy files touched: src/rt/coordfix.py" and seen[0][0] == ["src/rt/coordfix.py"]
    monkeypatch.setattr(policyfiles, "task_diff", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no git")))
    got = daemon.Daemon._policy_bounce(me, {"id": 1, "task": t["id"]})   # unreadable: a loud bounce, never a pass
    assert got.startswith("policy diff not readable") and "no git" in got
    assert p.db.one("SELECT severity FROM events WHERE kind='policy_diff_unread'")["severity"] == "high"
    (p.harness / policyfiles.FILE).unlink()
    assert daemon.Daemon._policy_bounce(me, {"id": 1, "task": t["id"]}) == ""


def test_the_policy_check_reads_the_diff_in_the_tasks_own_code_repo_and_a_missing_branch_is_loud(env, monkeypatch, tmp_path):
    """cell policy_bounce_code_repo: the task's code lives in a repository other than the project root (its
    worktree is a linked worktree of that repository); the project root has no such branch."""
    import subprocess
    from types import SimpleNamespace
    from ttp import daemon, policyfiles, worktree

    def git(repo, *a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/rt/coordfix.py\n")
    t = _ca_task(p)
    code = tmp_path / "code-repo"
    (code / "src/rt").mkdir(parents=True)
    git(code, "init", "-q", "-b", "main")
    git(code, "config", "user.email", "t@example.invalid")
    git(code, "config", "user.name", "t")
    (code / "src/rt/coordfix.py").write_text("a = 1\n")
    git(code, "add", "-A")
    git(code, "commit", "-qm", "base")
    branch = f"ttp/t{t['id']}-x"
    wt = p.worktrees / f"t{t['id']}"
    git(code, "worktree", "add", "-q", "-b", branch, str(wt))
    git(code, "update-ref", f"{worktree.FORK_POINT_REF}{t['id']}", git(code, "rev-parse", "HEAD"))
    (wt / "src/rt/coordfix.py").write_text("a = 2\n")
    git(wt, "commit", "-qam", "glm change")
    tip = git(wt, "rev-parse", "HEAD")
    p.db.x("UPDATE tasks SET branch=? WHERE id=?", (branch, t["id"]))
    monkeypatch.setattr(daemon, "log", lambda pp, m: None)
    me = SimpleNamespace(p=p)
    assert daemon.Daemon._policy_bounce(me, {"id": 1, "task": t["id"]}) == "policy files touched: src/rt/coordfix.py"
    assert git(code, "rev-parse", f"{policyfiles.EVIDENCE_REF}{t['id']}") == tip   # evidence kept in the code repo
    p.db.x("UPDATE tasks SET branch='ttp/gone' WHERE id=?", (t["id"],))
    got = daemon.Daemon._policy_bounce(me, {"id": 2, "task": t["id"]})
    assert "does not exist" in got and "ttp/gone" in got
    assert p.db.one("SELECT 1 FROM events WHERE kind='policy_diff_unread' AND fingerprint='policy_diff_unread:2'")


# The tool-output digest of the mini-swe-agent GLM harness (glmenv.digest): server capacity, never a hidden failure.
def _digest_output(n=600, fail_at=(137, 402)):
    lines = [f"collecting item {i} ok" for i in range(n)]
    for i in fail_at:
        lines[i] = f"FAILED tests/test_x.py::test_{i} - AssertionError: {i}"
    return "\n".join(lines) + "\n3 failed, 597 passed"


def test_digest_keeps_every_failing_line_and_a_tail_and_saves_the_whole_output(tmp_path):
    from ttp import glmenv
    out = _digest_output()
    assert len(out) > glmenv.DIGEST_BYTES
    got = glmenv.digest(out, 1, tmp_path / glmenv.SAVE_DIR)
    assert len(got) < len(out) / 3
    assert "138: FAILED tests/test_x.py::test_137" in got and "403: FAILED tests/test_x.py::test_402" in got
    assert "3 failed, 597 passed" in got and "collecting item 300 ok" not in got   # the tail stays, the middle goes
    assert "lines omitted" in got
    saved = sorted((tmp_path / glmenv.SAVE_DIR).glob("*.txt"))
    assert [f.name for f in saved] == ["0001.txt"] and saved[0].read_text() == out and str(saved[0]) in got
    glmenv.digest(out, 1, tmp_path / glmenv.SAVE_DIR)
    assert (tmp_path / glmenv.SAVE_DIR / "0002.txt").is_file()


def test_digest_leaves_short_output_alone_and_never_hides_when_it_cannot_save(tmp_path):
    from ttp import glmenv
    short = "ok\n" * 100
    assert glmenv.digest(short, 0, tmp_path / "d") == short and not (tmp_path / "d").exists()
    out = _digest_output()
    assert glmenv.digest(out, 1, "") == out   # no run folder: whole
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert glmenv.digest(out, 1, blocker / "sub") == out   # cannot be saved: whole
    # every line that looks like a failure survives, in a big output of failures too
    many = "\n".join(f"Traceback line {i}" if i % 2 else f"filler {i}" for i in range(2000))
    got = glmenv.digest(many, 1, tmp_path / "e")
    assert all(f"{i + 1}: Traceback line {i}" in got for i in range(1, 2000, 2)) or got == many


def test_digest_is_off_unless_set_and_the_guarded_environment_applies_it_to_the_output(tmp_path, monkeypatch):
    from ttp import glmenv, glmharness
    assert not glmharness.digest_on({}) and not glmharness.digest_on(_ca_cfg())
    assert glmharness.digest_on(_ca_cfg(glm={**_CA_GLM, "digest": True})) and not glmharness.digest_on(_ca_cfg(glm={**_CA_GLM, "digest": "yes"}))
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    big = _digest_output()

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            return {"output": big, "returncode": 1}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    monkeypatch.delenv(glmenv.DIGEST_ENV, raising=False)
    assert env.execute({"command": "ls"}, str(wt))["output"] == big
    monkeypatch.setenv(glmenv.DIGEST_ENV, "1")
    got = env.execute({"command": "ls"}, str(wt))
    assert got["returncode"] == 1 and len(got["output"]) < len(big) and "138: FAILED" in got["output"]
    assert (rd / glmenv.SAVE_DIR / "0001.txt").read_text() == big


def test_the_launch_script_sets_the_digest_flag_only_when_asked_and_a_result_line_carries_context_per_call(tmp_path):
    import json
    from ttp import glmenv, glmharness
    inst = {"kind": "mini-swe-agent", "version": "2.4.6", "bin": "/opt/harness/bin/mini"}
    glmharness.files(tmp_path, inst, "m", "/opt/rt")
    assert glmenv.DIGEST_ENV not in (tmp_path / "harness.sh").read_text()
    glmharness.files(tmp_path, inst, "m", "/opt/rt", digest=True)
    assert f"{glmenv.DIGEST_ENV}=1" in (tmp_path / "harness.sh").read_text()
    usage = {"calls": 4, "input_tokens": 100_000, "cache_read_input_tokens": 220_000, "output_tokens": 1000}
    assert glmharness.context_per_call(usage) == 80_000 and glmharness.context_per_call({}) == 0

    class Prov:
        model = "m"

        def price(self):
            return (1.0, 0.1, 4.0)
    line = json.loads(glmharness.result_line({"usage": usage}, Prov()))
    assert line["ttp_calls"] == 4 and line["ttp_context_tokens_per_call"] == 80_000


def test_glm_code_ring_flag_gates_the_coin_flip_and_the_projects_own_share_wins(env, capsys):
    from ttp import codearm

    def cfg(**over):
        c = _ca_cfg(**over)
        if "glm_share" not in over:
            c["route"]["code_sonnet"].pop("glm_share")
        return c

    def share(raw, c):
        return codearm.ring_gate(c, "demo", raw)["route"]["code_sonnet"]["glm_share"]

    own = {"route": {"code_sonnet": {"glm_share": 0.25}}}
    zero = {"route": {"code_sonnet": {"glm_share": 0}}}
    # Flag off: no coin flip without the project's own share, even with one from the account settings.
    assert share({}, cfg()) == 0 and codearm.settings(codearm.ring_gate(cfg(), "demo", {})) is None
    assert share({}, cfg(glm_share=0.3)) == 0
    assert share(own, cfg(glm_share=0.25)) == 0.25   # the project's own value wins
    # Ring 0 on: the shipped share, or the account's; the project's own value still wins, 0 included.
    assert _roll(capsys, "ring", "demo", "0")[0] == 0
    assert _roll(capsys, "set", "glm_code", "0")[0] == 0
    assert codearm.settings(codearm.ring_gate(cfg(), "demo", {}))["share"] == codearm.RING_SHARE == 0.5
    assert share({}, cfg(glm_share=0.3)) == 0.3
    assert share(own, cfg(glm_share=0.25)) == 0.25
    assert codearm.settings(codearm.ring_gate(cfg(glm_share=0), "demo", zero)) is None
    nomodel = cfg()
    nomodel["route"]["code_sonnet"].pop("glm")
    assert codearm.settings(codearm.ring_gate(nomodel, "demo", {})) is None   # no model: still no arm
    assert share({}, cfg()) == 0.5 and codearm.ring_gate(cfg(), "other", {})["route"]["code_sonnet"]["glm_share"] == 0
    # The project's settings, as the daemon and the CLI read them, carry the gated share.
    p = make(env)
    raw = json.loads(p.config_path.read_text())
    raw.setdefault("route", {})["code_sonnet"] = {"share": 1.0, "glm": dict(_CA_GLM)}
    p.config_path.write_text(json.dumps(raw))
    assert p.config()["route"]["code_sonnet"]["glm_share"] == 0.5
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d._load_config()
    assert d.cfg["route"]["code_sonnet"]["glm_share"] == 0.5

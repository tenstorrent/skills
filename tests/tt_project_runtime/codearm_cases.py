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
    assert codearm.stays(p.db, _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"}), _ca_task(p),
                         "standard") == "harness_unsupported"


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
    from ttp import codearm, daemon
    p = make(env)
    cfg = {**_ca_cfg(), "providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1",
                                                "tiers": {"light": "example-org/light-model"}}},
           "route": {**_ca_cfg()["route"], "classes": {"light": {"to": "chat", "model": "example-org/light-model"}}}}
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
    ("task", "list", False), ("note", "", True), ("say", "", True), ("ask", "", True), ("notify", "", True),
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
    cli.glm_gate(type("A", (), {"cmd": "note", "action": ""})())   # not a GLM-arm run: nothing happens
    assert not (rd / glmguard.HIT).exists()
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    cli.glm_gate(type("A", (), {"cmd": "checks", "action": ""})())   # an allowed command passes
    with _pytest.raises(SystemExit) as e:
        cli.glm_gate(type("A", (), {"cmd": "note", "action": ""})())
    assert e.value.code == 2 and "GLM arm" in capsys.readouterr().err
    res = json.loads((rd / "result.json").read_text())
    assert codearm.bounce_reason(res).startswith("guard: ") and res["status"] == "blocked"
    assert glmguard.hit_reason(rd).startswith("`ttp note`")


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
    "ttp note --to other 'hi'", "ttp ask 'ok?'", "ttp notify 'x'", "ttp ttproject note hi", "ttp other-project checks", "ttp task add t s",
    "ttp upstream 'x'", "git push origin HEAD", "git -C . push --force", "gh pr comment 1 --body x",
    "gh issue create -t x", "gh api repos/o/r/issues -f title=x", "curl -X POST https://x.invalid -d a=b",
    "curl --data a=b https://x.invalid", "ssh host true", "mail -s x a@b", "sqlite3 $TTP_PROJECT/state/project.db 'x'",
    "unset TTP_CODE_ARM_GUARD; ttp note x", "TTP_CODE_ARM_GUARD=0 ttp note x", "env -i ttp note x",
    "rm $TTP_RUN_DIR/glm_guard.on", "echo x > /etc/hosts-copy", "cd /tmp/elsewhere-x && echo x > ~/note.txt",
    "bash -c 'ttp note x'", "cat > ~/.tt-project/machine-workers.json"])
def test_glm_guard_refuses_a_shell_command_that_reaches_a_person_a_project_or_something_outside_the_worktree(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", [
    "ttp checks", "ttp push", "ttp proj checks --detach", "git status", "git commit -am x", "git diff HEAD~1",
    "python3 -m pytest -q tests", "echo hi > out.txt", "cat a | grep b > $TTP_RUN_DIR/notes.txt", "gh pr view 1",
    "gh api repos/o/r/pulls/1", "curl -s https://x.invalid/doc", "ttp task show 5", "ttp lock acquire x -- true",
    "ls /tmp", "sed -i s/a/b/ src/mod.py"])
def test_glm_guard_allows_the_commands_a_build_needs(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


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
    assert ask("Bash", {"command": "ttp note hi"}) and ask("Bash", {"command": "ttp checks"}) is None


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

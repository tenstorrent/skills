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


def test_code_arm_is_off_by_default_and_needs_a_share_and_a_model():
    from ttp import codearm
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

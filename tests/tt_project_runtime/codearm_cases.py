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


def test_code_arm_keeps_resources_chat_conclusions_forces_pins_devices_and_judgment_on_claude(env):
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg()
    assert codearm.stays(p.db, cfg, _ca_task(p), "standard") == ""
    t = _ca_task(p)
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["resource:box1"]), t["id"]))
    assert codearm.stays(p.db, cfg, p.db.task(t["id"]), "standard") == "resources"
    # A conclusion line alone keeps nothing (the coordinator reads the hand-off); one a chat reads stays.
    assert codearm.stays(p.db, cfg, _ca_task(p, "fix it\nconclusion: the verdict"), "standard") == ""
    assert codearm.stays(p.db, cfg, _ca_task(p, "fix it\nconclusion: the verdict", reply_chat="c1"), "standard") != ""
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
    assert got["arm"] == "glm" and got["why"] == f"coin at share 1 (follows #{t['id']})"   # the root's rule
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



def test_a_glm_guarded_run_that_fell_back_to_core_and_asks_for_opus_bounces_once_to_opus(env, tmp_path):
    """A run routed to GLM whose endpoint fell back to a cheaper core model before it started still runs under
    the GLM guard; its guard's `needs_opus` hand-back is a bounce (requeued on the core provider at Opus), not a
    blocked task, and only once."""
    import inspect
    from ttp import codearm, daemon, glmguard, handback, route, runclass
    p = make(env)
    cfg = _ca_cfg()
    t = _ca_task(p)
    routed = {"tier": "standard", "to": "chat", "model": _CA_GLM["model"], codearm.KEY: "glm", "glm_work": "glm",
              "fallback": "quota", "fallback_model": "sonnet"}
    run = {"id": 7, "task": t["id"], "role": "worker", "provider": "claude"}
    res = {"status": "blocked", "needs_opus": "guard: refusal 4 in one run: write the path literally",
           "summary": "needs Opus: guard: refusal 4 in one run: write the path literally"}
    assert handback.offroute_bounce(p.db, cfg, run, routed, tmp_path, res) == ""   # no GLM guard on the run
    (tmp_path / glmguard.RUN_MARK).write_text("{}")
    why = handback.offroute_bounce(p.db, cfg, run, routed, tmp_path, res)
    assert why == "guard: refusal 4 in one run: write the path literally"
    assert handback.offroute_bounce(p.db, cfg, {**run, "provider": "chat"}, routed, tmp_path, res) == ""   # on route: the daemon's own check
    assert handback.offroute_bounce(p.db, cfg, run, {**routed, codearm.KEY: "", "glm_work": ""}, tmp_path, res) == ""
    assert handback.offroute_bounce(p.db, cfg, run, routed, tmp_path, {"status": "blocked", "summary": "a lock"}) == ""
    assert handback.offroute_bounce(p.db, cfg, {**run, "role": "coordinator"}, routed, tmp_path, res) == ""
    src = inspect.getsource(daemon.Daemon.finish_run)
    assert "handback.offroute_bounce(db, self.cfg, r, routed, run_dir," in src
    # the daemon records it as it records an on-route bounce: the next run is Opus on the core provider
    note = {route.KEY: {**routed, "fallback": route.BOUNCE, "bounce": why}}
    p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
           (t["id"], "worker", "claude", "sonnet", time.time() - 60, time.time(), "fallback", 0.0, json.dumps(note)))
    assert codearm.bounced(p.db, t["id"]) == why
    got = codearm.choose(p.db, cfg, t, "standard")
    assert got["run"] == "opus" and runclass.code_route(cfg, t, None, codearm.code_state({}, got)) == (None, "needs_opus")
    assert handback.offroute_bounce(p.db, cfg, run, routed, tmp_path, res) == ""   # once: its hand-off then stands

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


def test_code_arm_guard_report_lines_show_review_fails_bounces_cost_and_a_small_sample_warning(env):
    """Beside the switch-back verdict, per arm over its window: first-review fail rate, bounces and $ per task, and
    a warning for a compared arm under min_tasks decided tasks. The verdict itself is the guard's alone."""
    from ttp import codearm, route
    p = make(env)
    cfg = _ca_cfg(glm_share=0.5, glm_guard_min=3)
    now = time.time()

    def arm_task(arm, review, bounce, usd):
        t = _ca_task(p)
        p.db.set_kv(f"{codearm.ARM_KV}{t['id']}", {"arm": arm, "why": "coin", "seed": "", "ts": now})
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (t["id"],))
        p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) "
               "VALUES(?,?,?,?,?,?,?,?)", (t["id"], f"b{t['id']}", "h", "main", "landed", now, now, "abc"))
        r = p.db.add_task(f"review #{t['id']}", "s", kind="review", origin="daemon", labels=[f"auto_review:{t['id']}"])
        p.db.x("UPDATE tasks SET status=? WHERE id=?", (review, r))
        note = {codearm.KEY: {"arm": arm}, route.KEY: {"fallback": route.BOUNCE} if bounce else {}}
        p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
               (t["id"], "worker", "chat", "m", now - 60, now - 30, "ok", usd, json.dumps(note)))
    for review, bounce in (("failed", True), ("done", True), ("done", False)):
        arm_task("glm", review, bounce, 5.0)
    for _ in range(2):
        arm_task("sonnet", "done", False, 4.0)
    row = codearm.guard_check(p.db, cfg, now + 1)
    assert (row["enough"], row["gap_pts"], row["tripped"]) == (False, None, False)   # sonnet is under min_tasks
    lines = codearm.guard_report_lines(row)
    assert lines[0] == f"- switch-back guard, 7 days to {time.strftime('%m-%d %H:%M', time.localtime(now + 1))}: not tripped"
    assert "  - glm: first-review fails 33% of 3, 0.67 bounces per task, $5.00 per task" in lines
    assert "  - sonnet: first-review fails 0% of 2, 0 bounces per task, $4.00 per task" in lines
    assert ("  - small sample: sonnet has 2 decided tasks, under the guard's 3, so their landing rates are not "
            "compared") in lines
    assert not any("small sample: glm" in ln for ln in lines)
    assert "(GLM 5 points above Sonnet" in codearm.guard_report_lines(dict(row, gap_pts=-5.0))[0]
    assert codearm.guard_report_lines(dict(row, gap_pts=30.0, tripped=True, why="w"))[0].endswith(": tripped: w")
    assert codearm.guard_report_lines(row)[1:] == codearm.guard_text(row).split("\n")[2:]
    codearm.guard_run(p.db, cfg, now + 1)
    assert codearm.review_lines(p.db, cfg, now + 2)[-len(lines):] == codearm.guard_report_lines(
        codearm.guard_check(p.db, cfg, now + 1))   # the daily review shows the last stored verdict


def test_code_arm_switch_back_never_counts_the_tasks_that_tripped_it_once_the_setting_changes(env, monkeypatch):
    """Re-armed by a new route.code_sonnet, the guard judges only tasks dealt after its trip; new bad ones trip it
    again."""
    from ttp import codearm
    p = make(env)
    cfg = _ca_cfg(glm_share=0.5, glm_guard_min=2)
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def tasks():
        for arm, land in (("sonnet", True), ("glm", False)) * 2:
            t = _ca_task(p)
            p.db.set_kv(f"{codearm.ARM_KV}{t['id']}", {"arm": arm, "why": "coin", "seed": "", "ts": clock[0]})
            p.db.x("UPDATE tasks SET status=? WHERE id=?", ("done" if land else "failed", t["id"]))
            if land:
                p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated,landed_sha) "
                       "VALUES(?,?,?,?,?,?,?,?)", (t["id"], f"b{t['id']}", "h", "main", "landed", clock[0], clock[0],
                                                   "abc"))
    tasks()
    clock[0] += 60
    assert codearm.guard_run(p.db, cfg, clock[0])["new_trip"]
    cfg = _ca_cfg(glm_share=0.4, glm_guard_min=2)   # the re-arm
    assert not codearm.held(p.db, cfg)
    clock[0] += codearm.GUARD_EVERY_S
    row = codearm.guard_run(p.db, cfg, clock[0])
    assert not row["tripped"] and row["table"]["arms"]["glm"]["tasks"] == 0, "the tripping tasks never count again"
    tasks()
    clock[0] += codearm.GUARD_EVERY_S
    assert codearm.guard_run(p.db, cfg, clock[0])["new_trip"], "new bad tasks after the re-arm trip it again"


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


def _gg_run(tmp_path, arm=True):
    """A worktree and a run folder; `arm`: the run is on the GLM arm as the daemon launched it (its marker)."""
    from ttp import glmguard
    wt, rd = tmp_path / "wt", tmp_path / "run"
    wt.mkdir(), rd.mkdir()
    if arm:
        glmguard.mark(rd)
    return wt, rd


def test_glm_guard_is_on_for_an_arm_run_by_its_variable_or_its_marker_and_off_for_other_runs(tmp_path):
    from ttp import codearm, glmguard
    glm = {codearm.KEY: {"arm": "glm", "run": "glm"}}
    assert glmguard.arm_env(glm) == _GG_RUN_ENV
    for note in (None, {}, {codearm.KEY: {"arm": "glm", "run": "opus"}}, {codearm.KEY: {"arm": "sonnet", "run": "sonnet"}}):
        assert glmguard.arm_env(note) == {}, note
    wt, rd = _gg_run(tmp_path, arm=False)
    assert not glmguard.active({}) and not glmguard.active({"TTP_RUN_DIR": str(rd)})
    assert glmguard.active(_GG_RUN_ENV)
    glmguard.mark(rd)   # a run that unsets its variable is still guarded
    assert glmguard.active({"TTP_RUN_DIR": str(rd)})


@_pytest.mark.parametrize("cmd,action,refused", [
    ("push", "", False), ("checks", "", False), ("lock", "", False), ("detach", "", False), ("task", "show", False),
    ("task", "list", False), ("note", "", False), ("say", "", True), ("ask", "", True), ("notify", "", True),
    ("upstream", "", True), ("land", "", True), ("express", "", True), ("remove", "", True), ("machines", "set", True),
    ("task", "add", False), ("obligation", "", False), ("memory", "", False), ("config", "", False), ("slop", "", False)])
def test_glm_guard_cli_allows_checks_and_push_and_refuses_everything_that_reaches_someone(cmd, action, refused):
    from ttp import glmguard
    assert bool(glmguard.cli_refusal(cmd, action)) is refused, (cmd, action)


def test_glm_guard_cli_gate_ends_the_run_as_a_bounce_to_opus_and_a_normal_run_is_untouched(env, monkeypatch, tmp_path, capsys):
    import json
    from ttp import cli, codearm, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path, arm=False)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.delenv("TTP_CODE_ARM_GUARD", raising=False)
    cli.glm_gate(type("A", (), {"cmd": "notify", "action": ""})())   # not a GLM-arm run: nothing happens
    assert not (rd / glmguard.HIT).exists()
    glmguard.mark(rd)
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    cli.glm_gate(type("A", (), {"cmd": "checks", "action": ""})())   # an allowed command passes
    with _pytest.raises(SystemExit) as e:
        cli.glm_gate(type("A", (), {"cmd": "land", "action": ""})())
    assert e.value.code == 2 and "GLM arm" in capsys.readouterr().err
    res = json.loads((rd / "result.json").read_text())
    assert codearm.bounce_reason(res).startswith("guard: ") and res["status"] == "blocked"
    assert glmguard.hit_reason(rd).startswith("`ttp land`")


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


_GG_READS = [
    "ttp machines list", "ttp machines list --json", "ttp machines status", "ttp machines activity",
    "ttp machines change list", "ttp machines change list x --all --json", "ttp machines outage", "ttp machines outage x",
    "ttp machines outage list", "ttp machines outage --json", "ttp status", "ttp status --json --verbose", "ttp occupancy",
    "ttp occupancy --list", "ttp stats", "ttp hostcheck", "ttp checks queue", "ttp task proj show 5", "ttp proj machines change list", "ttp machines change list 2>&1 | tail -5",
    "ttp ci --branch b", "ttp ci --repo o/r --sha abc123 --workflow w", "ttp ci --run 5 --no-hang"]   # the CI probe only reads
_GG_WRITES = [
    "ttp machines change add x 'what' --undo true", "ttp machines change edit 3 --what y", "ttp machines change close 3",
    "ttp machines set x --tag a", "ttp machines add x", "ttp machines remove x", "ttp machines push", "ttp machines tag-note a 'text'",
    "ttp machines outage x 'why' --expires 1d", "ttp machines outage x --clear", "ttp machines outage x why", "ttp machines outage --by p",
    "ttp machines outage --owner o x", "ttp machines outage --cl x", "ttp machines frobnicate", "ttp machines show x", "ttp machines", "ttp machines change",
    "ttp machines change frobnicate", "ttp note --to p hi", "ttp notify hi", "ttp land 5", "ttp push --land"]


@_pytest.mark.parametrize("cmd", _GG_READS)
def test_glm_guard_allows_the_read_only_ttp_queries(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", _GG_WRITES)
def test_glm_guard_still_refuses_every_ttp_write_and_an_unknown_machines_subcommand(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", ['echo "$(ttp land 5)"', 'echo "`ttp machines set x`"', 'x="a $(git push) b"',
                                 'echo "$(gh pr comment 1 -b hi)"', 'echo "ok `ssh h \'ls > /srv/x\'`"', 'echo "$(echo "$(ttp notify hi)")"',
                                 'echo "it\'s $(ttp note --to p hi)"', 'echo "$(printf ")"; ttp notify hi)"',
                                 'echo "$(echo "\\")"; ttp notify hi)"'])
def test_glm_guard_checks_a_substitution_inside_double_quotes_like_an_unquoted_one(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", ["echo '$(ttp task add t s)'", "echo '`ttp notify hi`'", 'echo "$(ttp task proj show 5)"',
                                 'echo "\\$(ttp notify hi)"', 'echo "`ttp machines list`"'])
def test_glm_guard_leaves_single_quoted_text_escaped_dollars_and_quoted_reads_alone(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


def test_glm_guard_device_task_is_a_device_lock_or_a_machine_tagged_device(monkeypatch):
    from ttp import glmguard, machines
    monkeypatch.setattr(machines, "load", lambda: {"box-a": {"tags": ["device"]}, "cpu-1": {"tags": ["cpu", "x86"]}})
    cfg = {"device": {"locks": ["chip"]}}
    lab = lambda *names: {"labels": json.dumps(list(names))}  # noqa: E731
    for task in (lab("resource:box-a"), lab("exclusive:box-a"), lab("read:box-a"), lab("resource:x-device"),
                 lab("read:cpu-1", "resource:box-a"), {"labels": '["needs_device"]'}):
        assert glmguard.device_task(task, cfg), task
    assert glmguard.device_task(lab("resource:chip"), cfg) and not glmguard.device_task(lab("resource:chip"))
    for task in (lab("resource:cpu-1"), lab("resource:build-cache"), lab("read:cpu-1", "exclusive:build-cache"),
                 {"labels": "[]"}, {}, None):
        assert not glmguard.device_task(task, cfg), task

    def broken():
        raise ValueError("unreadable")
    monkeypatch.setattr(machines, "load", broken)   # an unreadable list keeps the stricter device guard
    assert glmguard.device_task(lab("resource:cpu-1"), cfg) and not glmguard.device_task(lab(), cfg)
    from ttp import effort   # no resource names, or only blank ones, is never device work
    assert effort.device_resources([], cfg, True) == [] and effort.device_resources(["", "  "], cfg, True) == []


@_pytest.mark.parametrize("write", ["ttp machines change add x y", "ttp machines set x", "ttp note --to p hi", "ttp land 5"])
@_pytest.mark.parametrize("chain", ["ttp machines list; {w}", "ttp machines list && {w}", "ttp machines list | {w}",
                                    "ttp machines list || {w}", "echo $({w})", "echo `{w}`", "ttp status & {w}",
                                    "ttp machines change list\n{w}", "bash -c 'ttp machines list; {w}'"])
def test_glm_guard_refuses_a_chain_with_a_write_after_a_read(chain, write, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    cmd = chain.replace("{w}", write).replace("\\n", "\n")
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("argv,refused", [
    (["machines", "list"], False), (["machines", "status"], False), (["machines", "activity"], False),
    (["machines", "change", "list"], False), (["machines", "outage"], False), (["machines", "outage", "x"], False),
    (["machines", "outage", "x", "--json"], False), (["status"], False), (["occupancy"], False),
    (["machines", "change", "add", "x", "w", "--undo", "true"], True), (["machines", "change", "close", "3"], True),
    (["machines", "change", "edit", "3", "--what", "w"], True), (["machines", "set", "x"], True), (["machines", "add", "x"], True),
    (["machines", "remove", "x"], True), (["machines", "outage", "x", "why", "--expires", "1d"], True),
    (["machines", "outage", "x", "--clear"], True), (["machines", "outage", "x", "--by", "p"], True),
    (["machines", "tag-note", "t", "text"], True)])
def test_glm_guard_cli_gate_reads_a_machines_call_from_its_parsed_arguments(argv, refused, env, monkeypatch, tmp_path):
    from ttp import cli, glmguard
    p = make(env)
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    monkeypatch.setattr(cli, "_recover_setup", lambda: (_ for _ in ()).throw(_GatesPassed()))
    try:
        cli.main(argv)
        passed = True
    except _GatesPassed:
        passed = True
    except SystemExit as e:
        passed = not (e.code == 2 and (rd / glmguard.HIT).exists())
    assert passed is not refused, (argv, glmguard.hit_reason(rd))


class _GatesPassed(BaseException):
    pass


@_pytest.mark.parametrize("name", ["checks", "lock", "detach", "task", "config", "slop"])
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
    """cells: glm_note_plain, glm_note_log_only (ttp note goes only to the task's own log)."""
    from ttp import cli, glmguard, upstream
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    before = p.db.one("SELECT COUNT(*) n FROM messages")["n"]
    monkeypatch.setattr(upstream, "maybe_fallback", lambda *a, **k: _pytest.fail("a GLM note sent notes on"))
    monkeypatch.setattr(cli, "_note_to", lambda *a, **k: _pytest.fail("a GLM note reached another project"))
    cli.main(["note", "milestone: built the parser"])
    assert "milestone: built the parser" in (rd / "progress.md").read_text()
    assert not (rd / glmguard.HIT).exists() and not (rd / "result.json").exists()
    assert p.db.one("SELECT COUNT(*) n FROM messages")["n"] == before, "no chat outbox row"
    assert sorted(x.name for x in rd.iterdir() if x.name != glmguard.MARK) == ["progress.md"], "no notify, escalation or peer note file"
    assert glmguard.shell_refusal('ttp note "update: ack" 2>&1', str(wt), str(rd), {"TTP_PROJECT": str(p.base),
                                                                                 **_GG_RUN_ENV}) is None


@_pytest.mark.parametrize("argv,route", [
    (["note", "--to", "other", "hi"], "--to"), (["note", "--to=other", "hi"], "--to"),
    (["note", "--reply", "abc123", "hi"], "--reply"), (["note", "--rep", "abc123", "hi"], "--reply"),
    (["note", "--to", "other", "--urgent", "hi"], "--to"), (["note", "--confirms", "abc123", "works"], "--confirms"),
    (["note", "--severity", "high", "hi"], "--severity")])
def test_glm_guard_refuses_every_route_from_a_note_to_a_person_or_another_project(argv, route, env, monkeypatch,
                                                                                tmp_path, capsys):
    """cells: glm_note_to_refused, glm_note_routes_refused: a note to someone becomes a draft, other routes bounce."""
    from ttp import cli, glmguard
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    with _pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2 and f"`ttp note {route}`" in capsys.readouterr().err
    if route in glmguard.OUT_NOTE:
        assert [d["channel"] for d in glmguard.drafts(rd)] == ["note"] and not (rd / glmguard.HIT).exists()
    else:
        assert glmguard.hit_reason(rd).startswith(f"`ttp note {route}`")
    words = " ".join(f"'{w}'" if " " in w else w for w in argv)
    assert glmguard.shell_refusal(f"ttp {words}", str(wt), str(rd), {"TTP_PROJECT": str(p.base), **_GG_RUN_ENV})
    assert set(glmguard.NOTE_ROUTES) | {"--status"} >= {"--" + x.dest for x in _note_options() if x.dest != "text"}, \
        "a new `ttp note` flag is refused on the GLM arm until it is judged"


def _note_options(sub="note"):
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
    return [x for x in seen[sub]._actions if x.dest != "help"]


def test_glm_guard_turns_notify_into_a_draft_and_sends_nothing(env, monkeypatch, tmp_path):
    """cells: glm_notify_refused"""
    from ttp import cli, glmguard
    p, wt, rd = _gg_env(env, monkeypatch, tmp_path)
    with _pytest.raises(SystemExit):
        cli.main(["notify", "done"])
    assert [(d["channel"], d["text"]) for d in glmguard.drafts(rd)] == [("notify", "done")]
    assert not (rd / glmguard.HIT).exists() and not (rd / "notify.jsonl").exists()


def test_the_glm_prompt_deny_list_is_the_rule_table_with_each_allowed_way():
    """cells: prompt_denylist, glm_prompt_note_rule: the deny list is built from the guard's rule table, every rule has
    an allowed way, and the list names what the guard refuses and nothing it allows."""
    from ttp import glmguard
    text = glmguard.deny_list()
    assert all(r[4] for r in glmguard.RULES), "every rule has an allowed way"
    assert [ln for ln in text.splitlines() if ln.startswith("- ")] == [
        f"- Never {r[3]}: {r[4]}." + (" (harmful)" if r[1] == glmguard.HARMFUL else "") for r in glmguard.RULES if r[3]]
    assert 8 <= text.count("\n- ") <= 11 and "goes to your task's own log" in text
    for name in ("notify", "say", "upstream", "land", "express"):
        assert f"`ttp {name}`" in text and glmguard.cli_refusal(name), name
    assert "`ask`" in text and glmguard.cli_refusal("ask"), "ask is a post kind, not a ttp subcommand"
    assert "`ttp note --to`" in text and glmguard.cli_refusal("note", "", ["--to"])
    assert glmguard.cli_refusal("note", "", ["--reply"]) and "`ttp machines` writes" in text
    for name in ("checks", "lock", "detach", "push", "landed", "task", "slop", "config"):
        assert glmguard.cli_refusal(name, "show") is None, name


@_pytest.mark.parametrize("argv,name", [(["note", "--to", "other", "hi"], "note --to"), (["notify", "hi"], "notify"),
                                         (["land", "x"], "land"), (["say", "hi"], "say")])
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
    assert glmguard.hit_reason(rd).startswith(f"`ttp {name}`") or glmguard.drafts(rd)


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
    "ttp note --to other 'hi'", "ttp ask 'ok?'", "ttp notify 'x'", "ttp land 5", "ttp p express 5 --why x",
    "ttp push --land", "ttp remove p", "ttp machines change add box 'w' --undo true",
    "ttp upstream 'x'", "git push origin HEAD", "git -C . push --force", "gh pr comment 1 --body x",
    "gh issue create -t x", "gh api repos/o/r/issues -f title=x", "curl -X POST https://x.invalid -d a=b",
    "curl --data a=b https://x.invalid", "ssh host 'ls > /srv/x'", "mail -s x a@b", "sqlite3 $TTP_PROJECT/state/project.db 'x'",
    "unset TTP_CODE_ARM_GUARD; ttp notify x", "TTP_CODE_ARM_GUARD=0 ttp notify x", "env -i ttp notify x",
    "rm $TTP_RUN_DIR/glm_guard.on", "echo x > /etc/hosts-copy", "cd /srv/elsewhere-x && echo x > note.txt",
    "bash -c 'ttp notify x'", "bash -c 'ttp note --to other x'", "ttp note --reply abc x",
    "cat > ~/.tt-project/machine-workers.json", "ssh host 'echo hi >> ~/.bashrc'", "ssh host 'run.sh > $HOME/out.log'",
    "ssh host 'cd ~/wt && make > b.log'", "sed -i 's/a/b/w /etc/x-copy' src/mod.py", "cd /srv/elsewhere-x && sed -i s/a/b/ rel.py"])
def test_glm_guard_refuses_a_shell_command_that_reaches_a_person_a_project_or_something_outside_the_worktree(cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


@_pytest.mark.parametrize("cmd", [
    "ttp checks", "ttp push", "ttp proj checks --detach", "ttp note 'update: ack'", "bash -c 'ttp note x'", "git status", "git commit -am x", "git diff HEAD~1",
    "python3 -m pytest -q tests", "echo hi > out.txt", "cat a | grep b > $TTP_RUN_DIR/notes.txt", "gh pr view 1",
    "gh api repos/o/r/pulls/1", "curl -s https://x.invalid/doc", "ttp task p show 1", "ttp task proj list", "ttp task show 5", "ttp task list",
    "ttp task --limit 5 list", "ttp lock acquire x -- true",
    "ls /tmp", "sed -i s/a/b/ src/mod.py", "ssh host 'mkdir -p out && make > out/b.log'",
    "sed -i 's/def _fmt(v) -> str:/def _rate_str(v) -> str:/; s/_fmt(/_rate_str(/g' src/mod.py",
    "sed -i -e 's|a|/b|' -e 's,/c,d,' src/mod.py", "sed -Ei.bak 's/x/\\/y/' src/mod.py", "sed -i '/stage-task.sh/d' src/mod.py",
    "sed -i -f $TTP_RUN_DIR/fix.sed src/mod.py",
    "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('src/mod.py')\ns = p.read_text()\n"
    "p.write_text(s.replace('old = 1', 'new = 1'))\nprint('ok')\nEOF",
    "python3 - <<'EOF'\nimport re\nt = open('tests/t_cases.py').read()\nt = re.sub(r'x', 'y', t)\n"
    "open('tests/t_cases.py', 'w').write(t)\nEOF"])
def test_glm_guard_allows_the_commands_a_build_needs(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", [
    "ttp checks 2>&1 | tail -3", "ttp checks >$TTP_RUN_DIR/checks.log 2>&1", "ttp 2>&1 checks", "ttp task proj show 5 2>/dev/null",
    "ttp push 2>&1 | tail -5"])
def test_glm_guard_reads_a_ttp_call_without_its_redirections(cmd, tmp_path):
    """cell guard_redirect: the fd digit of `2>&1` is no word of the call."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", [
    "cd /work/repo && ls >/dev/null 2>&1", "cd /work/repo && git log -1 2>&1 | tail -2", "cd /work/repo && ls 1>&2",
    "cd /work/repo && ls >&2", "cd /work/repo && ls 2>&-", "cd /work/repo && ls 2>& 1", "cd /work/repo && cat <&3"])
def test_glm_guard_reads_a_copied_or_closed_fd_as_no_file_after_a_cd_outside_the_worktree(cmd, tmp_path):
    """`2>&1`, `>&2`, `2>&-`: no file is written, so no target resolves under the cd (once /work/repo/1)."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}) is None, cmd


@_pytest.mark.parametrize("cmd", [
    "cd /work/repo && echo x > f", "cd /work/repo && echo x >> f 2>&1", "cd /work/repo && echo x >&f",
    "cd /work/repo && ls 2>err.log", "ls &>/srv/x", "ls 2>&1 >/srv/x", "ls >&/srv/x"])
def test_glm_guard_still_refuses_a_redirect_to_a_file_outside_the_worktree(cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), {"TTP_PROJECT": str(tmp_path / "proj"), **_GG_RUN_ENV}), cmd


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


@pytest.mark.away_from_tmp
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


@pytest.mark.away_from_tmp
def test_glm_guard_keeps_the_folder_a_run_started_in_as_a_write_root_after_its_shell_cd_elsewhere(tmp_path):
    """A run that started in its worktree and `cd`ed to a sibling folder still edits its worktree (an Edit there
    was refused as outside it); a write outside both stays refused, and the daemon marks the start folder."""
    import inspect, json
    from ttp import daemon, glmguard
    wt, rd = _gg_run(tmp_path)
    (tmp_path / "sib").mkdir()
    glmguard.mark_run(rd, False, cwd=str(wt))
    assert json.loads((rd / glmguard.RUN_MARK).read_text())["cwd"] == str(wt)
    env_ = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}

    def edit(path):
        return glmguard.tool_refusal({"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": str(tmp_path / "sib")}, env_)
    assert edit(str(wt / "src" / "m.py")) is None
    assert "outside its worktree" in edit(str(tmp_path / "other" / "m.py"))
    assert "plan=plan, cwd=str(cwd)," in inspect.getsource(daemon)


@_pytest.mark.parametrize("cmd", [
    "git diff > $TMPDIR/daemon_diff.txt", "cp src/a.py ${TMPDIR}/a.orig.py", 'echo x > "$TMP/x"', "tee $TEMP/y < a.txt",
    "mkdir -p $TMPDIR/sub && cp a $TMPDIR/sub/", "echo x > {scratch}/z"])
def test_glm_guard_allows_writes_to_the_runs_own_scratch_folder(cmd, tmp_path):
    """cell scratch_allowed: $TMPDIR (TMP, TEMP) is the run folder's scratch folder, by name or by path."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    scratch = glmguard.scratch_env(rd)
    tmp = scratch["TMPDIR"]
    env_ = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **scratch, **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd.replace("{scratch}", tmp), str(wt), str(rd), env_) is None, cmd
    assert glmguard.tool_refusal({"tool_name": "Bash", "tool_input": {"command": cmd.replace("{scratch}", tmp)},
                                  "cwd": str(wt)}, env_) is None, cmd


def test_glm_guard_lets_a_run_write_new_temp_entries_and_keeps_links_globs_and_records_outside(tmp_path):
    """A new scratch dir or file under /tmp and a temp file of the account's own harm nobody; a glob, an unread name,
    a link out, a link the command makes, the record and deleting the project's own state logs stay refused."""
    import os
    import tempfile
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path, arm=False)
    proj = tmp_path / "proj"
    (proj / "tt-project" / "state" / "logs").mkdir(parents=True)
    e = {"TTP_PROJECT": str(proj), "TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    ask = lambda c, env=e: glmguard.shell_refusal(c, str(wt), str(rd), env)
    u, taken = f"/tmp/ttp-gg-{os.getpid()}-{tmp_path.name}", ""
    try:
        for c in (f"mkdir -p {u}-metal && tar -xf dist/a.tar -C {u}-metal", f"rg -n x src > {u}.out 2>&1",
                  f"mkdir -p {u}-rc && cd {u}-rc && python3 - <<'PY'\nprint(1)\nPY",
                  f'TMPDIR=/tmp; echo x > "$TMPDIR/{u[5:]}.sh"'):
            assert ask(c) is None, c
        assert not os.listdir(rd), "a check writes nothing"
        fd, taken = tempfile.mkstemp(dir="/tmp")
        os.close(fd)
        os.symlink("/etc", f"{u}-ln")
        os.symlink(taken, f"{u}-lt")
        for c in (f"echo x > {taken}", f"rm {taken}", f"echo x > {u}-lt"):
            assert ask(c) is None, c
        assert ask(f"echo x > {u}-dev", {**e, glmguard.RUN_ENV: glmguard.DEVICE}) is None, "a device run's too"
        assert not glmguard.writes_outside(f"echo x > {u}-new", str(wt), str(rd), e, inline=True), "the post-run scan too"
        for c in ("rm -rf /tmp/*", "echo x > /tmp/$X/y", f"echo x > {u}-ln/f",
                  f"ln -s ~/.bashrc {u}-l2", f"cp -s ~/.bashrc {u}-l3", f"cp -as ~/.bashrc {u}-l4",
                  f"rm {proj}/tt-project/state/logs/changes-pre-expiry.json"):
            assert "outside its worktree" in (ask(c) or ""), c
    finally:
        for p in (f"{u}-ln", f"{u}-lt", taken):
            if os.path.islink(p) or os.path.isfile(p):
                os.unlink(p)


def test_glm_guard_checks_write_nothing_and_the_live_hook_lets_every_runs_temp_writes_through(monkeypatch, tmp_path):
    """A check (shell_refusal, tool_refusal, writes_outside) writes nothing into the run folder it reads, and a new
    /tmp entry passes for every run."""
    import os
    from ttp import glmguard, hook
    wt, rd = _gg_run(tmp_path, arm=False)
    rd2 = tmp_path / "run2"
    rd2.mkdir()
    u = f"/tmp/ttp-gg-{os.getpid()}-{tmp_path.name}-own"
    e = {"TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    bash = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(wt)}
    assert glmguard.shell_refusal(f"echo hi > {u}", str(wt), str(rd), {}) is None
    assert glmguard.shell_refusal(f"echo hi > {u}", str(wt), str(rd), e) is None
    assert glmguard.tool_refusal(bash(f"mkdir {u}"), e) is None and not glmguard.writes_outside(f"echo x > {u}", str(wt), str(rd), e)
    assert not os.listdir(rd), "a dry check creates no file in the run folder"
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    pre = lambda c: (hook.pre_tool_use(bash(c))[0] or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    try:
        assert pre(f"mkdir -p {u} && echo x > {u}/a") != "deny"
        os.mkdir(u)
        assert pre("ttp land 5") == "deny"
        monkeypatch.setenv("TTP_RUN_DIR", str(rd2))
        assert pre(f"echo y > {u}/c") != "deny", "another run's write there harms no one either"
    finally:
        if os.path.isdir(u):
            os.rmdir(u)


def test_glm_guard_lets_a_plain_reader_name_the_guards_files_of_any_run(tmp_path):
    """Replayed stop: reading glm_guard.json and glm_refusals.jsonl, in a for loop and in another run's folder."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    runs = tmp_path / "runs"
    e = {"TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    for c in ("cat glm_guard.json", "grep x $TTP_RUN_DIR/glm_guard.json", "cat $TTP_RUN_DIR/glm_guard.json 2>&1",
              f'for d in 11 12; do echo "=== run $d ==="; cat {runs}/$d/glm_guard.json 2>&1; '
              f'cat {runs}/$d/glm_refusals.jsonl 2>&1 | tail -3; done'):
        assert glmguard.shell_refusal(c, str(wt), str(rd), e) is None, c
    for c in ('echo x > "$TTP_RUN_DIR/glm_guard.on"', f"for d in 11; do rm {runs}/$d/glm_guard.json; done",
              "echo x >> $TTP_RUN_DIR/glm_refusals.jsonl", "git log -S glm_guard.json",
              'for f in $TTP_RUN_DIR/glm_guard.json; do rm "$f"; done', "ls $TTP_RUN_DIR/glm_guard.json | xargs rm -f",
              "F=$TTP_RUN_DIR/glm_guard.json; rm $F", "rm $(ls $TTP_RUN_DIR/glm_guard.json)",
              "uniq /dev/null $TTP_RUN_DIR/glm_guard.on", "uniq $TTP_RUN_DIR/glm_guard.json $TTP_RUN_DIR/run.json",
              "sort -o $TTP_RUN_DIR/glm_guard.on /dev/null", "cat glm_guard.json | less -o $TTP_RUN_DIR/run.json",
              "rg --pre rm x $TTP_RUN_DIR/glm_guard.on", "rg --pre=rm x $TTP_RUN_DIR/glm_guard.json"):
        assert glmguard.shell_refusal(c, str(wt), str(rd), e), c


def test_the_readers_that_may_name_the_guards_files_have_no_output_file_form():
    """Nor a command-running form: `rg --pre`, `find -exec` and `xargs` run a command on the file."""
    from ttp import glmguard, hook
    assert glmguard.GUARD_READERS <= hook.READERS | {"jq", "ls"}   # jq and ls write no file and run no command
    assert not glmguard.GUARD_READERS & {"uniq", "sort", "tee", "dd", "cp", "less", "more", "sed", "awk", "rg",
                                         "find", "xargs"}


@_pytest.mark.parametrize("cmd", [
    "git diff > /tmp/daemon_diff.txt", "cp src/a.py /tmp/a.orig.py", "echo x > /tmp/x", "TMPDIR=/tmp; cp a $TMPDIR/x",
    "export TMPDIR=/var/tmp && echo x > $TMPDIR/y"])
@pytest.mark.away_from_tmp
def test_glm_guard_lets_a_run_write_to_tmp_and_to_a_tmpdir_the_command_set_itself(cmd, tmp_path, monkeypatch):
    """cell scratch_tmp_open: a new file in /tmp or /var/tmp harms nobody; /tmp itself,
    a glob there, an unset TMP and an escape out of the scratch folder stay refused."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    tmp = glmguard.scratch_env(rd)["TMPDIR"]
    env_ = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), "TMPDIR": tmp, **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), env_) is None, cmd
    other = {**env_, "TMPDIR": "/tmp", "TMP": "", "TEMP": "/var/tmp"}   # a run whose TMPDIR is not its own
    assert not any(glmguard.shell_refusal(c, str(wt), str(rd), other) for c in ("cp a $TMPDIR/x", "tee ${TEMP}/x"))
    for c in ("echo > $TMP/x", "rm -rf /tmp", "rm -rf /tmp/*", "echo x > $TMPDIR/../../../escape"):
        assert "outside its worktree" in (glmguard.shell_refusal(c, str(wt), str(rd), env_ if "escape" in c else other)
                                          or ""), c


@pytest.mark.away_from_tmp
def test_glm_guard_reads_a_naming_that_keeps_tmpdir_as_no_set(tmp_path, monkeypatch):
    """cell scratch_keep: echoing `TMPDIR=$TMPDIR`, `TMPDIR="${TMPDIR:-x}"` and a Python read leave the run's own
    scratch folder its own; a set, an unset or a TMPDIR outside the run stays refused."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    tmp = glmguard.scratch_env(rd)["TMPDIR"]
    env_ = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), "TMPDIR": tmp, **_GG_RUN_ENV}
    ask = lambda c, env=env_: glmguard.shell_refusal(c, str(wt), str(rd), env)
    repro = 'ls "$TMPDIR"; echo "TMPDIR=$TMPDIR"; mkdir -p "$TMPDIR" && echo ok'
    for c in (repro, 'export TMPDIR="${TMPDIR:-/x}"; echo a > "$TMPDIR/a"',
              'mktemp -d "${TMPDIR:-/tmp}/rev.XXXXXX"', "python3 -c 'import os; print(os.environ[\"TMPDIR\"])' > $TMPDIR/o"):
        assert ask(c) is None, c
    for c in ('TMPDIR=/srv; echo "TMPDIR=$TMPDIR"; mkdir -p "$TMPDIR/x"', 'unset TMPDIR; echo "TMPDIR=$TMPDIR" > $TMPDIR/o',
              'TMPDIR=/etc; echo x > "${TMPDIR:-/x}/passwd"',
              "python3 -c 'import os; os.environ[\"TMPDIR\"] = \"/srv\"' > $TMPDIR/o"):
        assert "outside its worktree" in (ask(c) or ""), c
    for out in ("/srv", str(tmp_path / "home")):   # the run's TMPDIR is not its own
        assert "outside its worktree" in (ask(repro.replace("mkdir -p \"$TMPDIR\"", "mkdir -p \"$TMPDIR/x\""),
                                              {**env_, "TMPDIR": out}) or ""), out

def test_a_glm_runs_scratch_folder_is_in_its_run_folder_or_its_own_short_temp_dir_and_its_prompt_names_it(tmp_path):
    """cell scratch_env: the daemon exports it for every run under the GLM guards; a Write there is allowed."""
    import inspect
    from ttp import daemon, glmguard
    wt, rd = _gg_run(tmp_path)
    env_ = glmguard.scratch_env(rd)
    assert env_ == dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(rd / "tmp")) and (rd / "tmp").is_dir()
    assert glmguard.scratch_env(rd, {"TMPDIR": "/tmp"}) == env_   # a TMPDIR that is not its own is replaced
    run = {"TTP_RUN_DIR": str(rd), **env_, **_GG_RUN_ENV}
    assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": str(rd / "tmp" / "a.txt")},
                                  "cwd": str(wt)}, run) is None
    assert "outside its worktree" in glmguard.tool_refusal(
        {"tool_name": "Write", "tool_input": {"file_path": "/srv/a.txt"}, "cwd": str(wt)}, run)
    assert "tmp_env.update(glmguard.scratch_env(run_dir, tmp_env))" in inspect.getsource(daemon)


def test_a_glm_code_or_harness_run_gets_the_hand_off_line_and_every_glm_run_the_scratch_line(env, monkeypatch):
    from ttp import daemon, glmguard
    p = make(env)
    _route_config(p)
    d = daemon.Daemon(p.base)
    got = {}
    for kind in ("code", "harness", "work"):
        prompts = []
        prov, _tier, _note = _route_dispatch(p, d, monkeypatch, prompts, title=f"glm {kind}", kind=kind)
        assert prov == "chat", kind
        got[kind] = prompts[-1]
    assert all(glmguard.SCRATCH_LINE in pr and glmguard.deny_list() in pr for pr in got.values())
    assert [k for k, pr in got.items() if glmguard.HANDOFF_LINE in pr] == ["code", "harness"]
    prompts = []
    prov, _tier, _note = _route_dispatch(p, d, monkeypatch, prompts, title="core code", kind="code", provider="claude")
    assert prov != "chat" and glmguard.SCRATCH_LINE not in prompts[-1] and glmguard.HANDOFF_LINE not in prompts[-1]
    assert glmguard.deny_list() not in prompts[-1]


def test_glm_guard_hook_denies_a_refused_call_and_ends_the_run_as_a_bounce(monkeypatch, tmp_path):
    import json
    from ttp import codearm, glmguard, hook
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ttp land 5"}, "cwd": str(wt)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "goes back to Opus" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert codearm.bounce_reason(json.loads((rd / "result.json").read_text())).startswith("guard: ")
    monkeypatch.delenv("TTP_CODE_ARM_GUARD")
    (rd / "glm_guard.json").unlink(), (rd / "result.json").unlink(), (rd / glmguard.MARK).unlink()
    hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ttp task add x"}, "cwd": str(wt)})
    assert not (rd / "result.json").exists()   # not a GLM-arm run: the guard does not apply


def test_glm_guard_redirects_a_refusal_with_its_allowed_way_and_hands_back_on_the_third(monkeypatch, tmp_path):
    """cells: guard_redirect, guard_third_hands_back: a redirect shows the reason and the allowed way and the run
    goes on; the third refusal in one run hands it back."""
    from ttp import glmguard, hook
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")

    def pre(c):
        out = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": str(wt)})[0]
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", c
        return out["hookSpecificOutput"]["permissionDecisionReason"]
    why = pre("sqlite3 state/" + "project" + ".db 'select 1'")
    assert "`ttp task <project> show <id>`" in why and "goes back to Opus" not in why
    assert "$TMPDIR" in pre("echo x > /srv/notes.md")
    assert not glmguard.hit_reason(rd) and not (rd / "result.json").exists(), "the run goes on"
    assert "goes back to Opus" in pre("ttp upstream 'fix the parser'")
    assert glmguard.hit_reason(rd).startswith("refusal 3 in one run")
    assert [(r["rule"], r["back"]) for r in glmguard.refusals(rd)] == [
        ("db_read", False), ("scratch", False), ("upstream", True)]


def test_glm_guard_run_records_skip_bad_lines_and_an_unset_run_dir(tmp_path):
    """cells: guard_records_read: refusals() and drafts() read the run folder with the shared jsonl reader, which
    skips undecodable, unparsable and non-object lines; refusals() of an unset run folder is empty."""
    from ttp import glmguard
    for name, read in ((glmguard.REFUSALS, glmguard.refusals), (glmguard.DRAFTS, glmguard.drafts)):
        (tmp_path / name).write_bytes(b'{"rule": "a"}\n\xff\xfe not json\n[1]\n{"rule": "b"}\n')
        assert [r["rule"] for r in read(tmp_path)] == ["a", "b"]
    assert glmguard.refusals("") == [] and glmguard.refusals(None) == []


def test_glm_guard_hands_back_a_harmful_attempt_at_once_and_off_restores_the_first_refusal(monkeypatch, tmp_path):
    """cells: guard_harmful_hands_back, guard_redirect_off"""
    from ttp import glmguard
    _wt, rd = _gg_run(tmp_path)
    text = glmguard.refused(rd, "`ttp upstream` is not allowed", cfg={})
    assert "`upstream: ...`" in text and not glmguard.hit_reason(rd)
    text = glmguard.refused(rd, "a run on the GLM arm never `git push`", cfg={})
    assert "goes back to Opus" in text and glmguard.hit_reason(rd).endswith("never `git push`")
    off = tmp_path / "off"
    off.mkdir()
    glmguard.mark(off)
    assert "goes back to Opus" in glmguard.refused(off, "`ttp upstream` is not allowed",
                                                   cfg={"route": {"glm_refusals": {"redirect": False}}})
    assert glmguard.hit_reason(off)


def test_a_glm_run_skips_its_specs_publish_step_and_any_git_push_dry_run_included_hands_back(tmp_path):
    """cells: dryrun-deploy-push-stays-harmful, realpush-still-harmful, glm-prompt-skips-publish: replayed from a
    GLM device run that dry-ran a push of its unreviewed branch onto a deploy branch; the prompt says the review
    publishes, so a spec's publish step is skipped, and a real `git push` still hands the task back (a dry run
    publishes nothing: test_glm_guard_runs_dev_redirects_dry_pushes_reader_expansions_and_env_i_of_a_plain_program)."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    line = next(ln for ln in glmguard.deny_list().splitlines() if "`git push`" in ln)
    assert "skip every publish, push or `published` step" in line and line.endswith("(harmful)")
    push = "git push origin ttp/t1-fix:refs/heads/deploy"
    for cmd in (push, "ssh box-a 'git -C wt push origin x'",
                'grep -rn "push --own\\|own_push" harness/bin/ttp 2>/dev/null | head -3; '
                "git push origin ttp/t1-fix:refs/heads/deploy 2>&1 | head -5"):
        why = glmguard.shell_refusal(cmd, str(wt), str(rd), env={})
        assert glmguard.classify(why)[:2] == ("publish", glmguard.HARMFUL), cmd
    text = glmguard.refused(rd, glmguard.shell_refusal(push, str(wt), str(rd), env={}), cfg={})
    assert "goes back to Opus" in text and glmguard.hit_reason(rd).endswith("never `git push`")


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


def test_the_guarded_environment_lets_a_run_command_s_temp_write_through_with_no_temp_entry_to_keep(tmp_path, monkeypatch):
    """The mini-swe-agent command layer is that provider's live hook: it judges each command as hook.pre_tool_use does."""
    import os
    from ttp import glmenv, glmguard
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    u = f"/tmp/ttp-gg-{os.getpid()}-{tmp_path.name}-mini"
    assert glmenv.refusal(f"echo x > {u}", str(wt), str(rd)) is None
    assert env.execute({"command": f"echo x > {u}-a"}, str(wt))["returncode"] == 0
    assert env.execute({"command": f"echo x > {u}-b && sqlite3 state/project.db 'select 1'"}, str(wt))["returncode"] == 2


def test_the_guarded_environment_ends_a_run_on_a_refused_command_and_runs_the_rest(tmp_path, monkeypatch):
    import json
    from ttp import codearm, glmenv, glmguard
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
    # An outward post is not a refusal: it is kept as a draft for an Opus step and the run goes on.
    got = env.execute({"command": "ttp note --to other hi"}, str(wt))
    assert got["returncode"] == 2 and got["output"].startswith("not sent: ") and ran == ["ls"]
    assert (rd / glmguard.DRAFTS).exists() and not (rd / "result.json").exists()
    got = env.execute({"command": "echo x > /srv/notes.md"}, str(wt))   # a redirect: the run goes on
    assert got["returncode"] == 2 and "$TMPDIR" in got["output"] and not (rd / "result.json").exists()
    with pytest.raises(glmenv.Submitted) as ei:
        env.execute({"command": "git push origin HEAD"}, str(wt))
    assert ran == ["ls"]   # the refused command never ran
    assert ei.value.messages[0]["extra"]["exit_status"] == "GuardRefused"
    assert codearm.bounce_reason(json.loads((rd / "result.json").read_text())).startswith("guard: ")


def test_the_guarded_environment_hands_a_coordinator_update_to_the_model_at_its_next_command(tmp_path, monkeypatch):
    """mini-swe-agent has no tool hook, so its command layer is the update hook: an update the coordinator appends
    to steer.md mid-run follows the next command's output (a redirect's too), marked read as the Claude hook marks
    it, once; the run then counts as hooked, so `ttp note` leaves updates to it and the daemon reads its offset."""
    import json
    from ttp import cli, glmenv, hook
    wt, rd = _gg_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_CODE_ARM_GUARD", "1")
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.delenv("TTP_DETACHED", raising=False)
    (rd / "run.json").write_text(json.dumps({"harness": {"kind": "mini-swe-agent", "version": "2.4.6"}}))

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    assert not hook.steer_hooked(rd, "chat"), "before its first command a mini run is not counted as hooked"
    assert env.execute({"command": "ls"}, str(wt)) == {"output": "ok", "returncode": 0}, "no update: output as is"
    (rd / "steer.md").write_text("\n## Update Thu 10:06 PST\nbuild only the CPU target\n")
    got = env.execute({"command": "ls"}, str(wt))
    assert got["returncode"] == 0 and got["output"].startswith("ok\n\nUpdate for your task #7 from the project coordinator")
    assert got["output"].endswith("build only the CPU target")
    size = (rd / "steer.md").stat().st_size
    assert (rd / hook.OFFSET_FILE).read_text() == str(size) and (rd / hook.READS_FILE).read_text().endswith(f" {size}\n")
    assert hook.steer_hooked(rd, "chat") and not cli.print_update(), "`ttp note` leaves updates to the command layer"
    assert env.execute({"command": "ls"}, str(wt))["output"] == "ok", "handed over once"
    with open(rd / "steer.md", "a") as f:
        f.write("\n## Update Thu 10:20 PST\nkeep notes in the worktree\n")
    got = env.execute({"command": "echo x > /srv/notes.md"}, str(wt))   # a redirect carries it too
    assert got["returncode"] == 2 and "$TMPDIR" in got["output"] and got["output"].endswith("keep notes in the worktree")
    assert "build only" not in got["output"] and hook.unread_update(rd)[0] == ""


def test_glmharness_writes_a_guarded_mini_run_with_the_prompt_on_stdin_and_the_bridge_as_its_model(tmp_path):
    from ttp import glmenv, glmharness
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("do the task")
    (rd / "system.md").write_text("You are a worker.")
    spec = {"argv": ["claude"], "stdin": "{json}", "model": "example-org/code-model", "budget_usd": None,
            "bridge": {"endpoint": "placeholder"}}
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


def test_a_glm_run_gets_a_batched_task_message_in_place_of_minis_step_by_step_one_unless_turned_off(tmp_path):
    import json
    from ttp import glmharness

    def template(rd):
        lines = [x for x in (rd / "mini.yaml").read_text().splitlines() if x.startswith("  instance_template: ")]
        return json.loads(lines[0].split(": ", 1)[1]) if lines else None
    inst = {"kind": "mini-swe-agent", "version": "2.4.6", "bin": "/opt/harness/bin/mini"}
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("do the task")
    glmharness.apply({"model": "m", "bridge": {"endpoint": "e"}}, rd, {}, "/opt/rt", install_fn=lambda v: inst)
    t = template(rd)
    assert t is not None and t.startswith("{{task}}") and t.count("{{") == 1   # mini renders it with jinja
    assert "Batch" in t and glmharness.SUBMIT in t and "reproduce script" in t
    off = {"route": {"code_sonnet": {"glm": {"batch_steps": False}}}}
    glmharness.apply({"model": "m", "bridge": {"endpoint": "e"}}, rd, off, "/opt/rt", install_fn=lambda v: inst)
    assert template(rd) is None and "step_limit: 0" in (rd / "mini.yaml").read_text()


def test_a_harness_that_cannot_be_installed_hands_the_task_back_to_opus_not_a_failed_attempt(tmp_path):
    import json
    from ttp import codearm, glmharness
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("x")
    spec = {"argv": ["claude"], "model": "m", "bridge": {"endpoint": "e"}}

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


def test_glmfb_a_spec_with_no_bridge_endpoint_keeps_its_own_harness(tmp_path):
    from ttp import glmharness
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "prompt.md").write_text("do the task")
    for spec in ({"argv": ["claude", "-p"], "stdin": "input.jsonl", "model": "sonnet"},   # fell back to core
                 {"argv": ["claude", "-p"], "model": "m", "bridge": {"endpoint": ""}}):   # no endpoint set
        was = json.loads(json.dumps(spec))
        assert glmharness.apply(spec, rd, {}, "/opt/rt", install_fn=lambda v: 1 / 0) == "" and spec == was
    assert sorted(x.name for x in rd.iterdir()) == ["prompt.md"]


def _glmfb_project(env, chat=None):
    """A project whose glm_code arm runs every code task on mini-swe-agent; `chat`: its providers.chat."""
    from ttp import configwho, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")   # a live arm needs the project's policy list
    p.set_config("core_provider", "claude")
    cfg, user = _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"}), configwho.by("user", via="cli")
    p.set_config("providers.chat", cfg["providers"]["chat"] if chat is None else chat, by=user)
    p.set_config("route.code_sonnet", cfg["route"]["code_sonnet"], by=user)
    return p


def _glmfb_launch(env, monkeypatch, chat=None, cool=""):
    """Dispatch one arm task through the real start_run, no runner started (`cool`: the chat endpoint's
    cool-down); returns (its run row, its run.json)."""
    from ttp import budget as bud
    from ttp import daemon as dmod
    from ttp import glmharness
    p = _glmfb_project(env, chat)
    if cool:
        monkeypatch.setattr(dmod.route, "cooling", lambda db, name, now: cool)

    class Proc:   # never a real runner
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    monkeypatch.setattr(glmharness, "install", lambda version="", root=None: {
        "kind": "mini-swe-agent", "version": "2.4.6", "bin": "/opt/harness/bin/mini"})
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(d, "_workdir_for", lambda t: (str(p.root), None))
    for prov in ("claude", "chat"):
        d.gates[prov] = bud.Gate(prov, regime="windows", max_parallel=6)
    tid = _ca_task(p)["id"]
    d.dispatch()
    run = p.db.one("SELECT * FROM runs WHERE task=? AND role='worker'", (tid,))
    return run, json.loads((p.runs / str(run["id"]) / "run.json").read_text())


@_pytest.mark.parametrize("case", ["rate_limit", "quota", "no endpoint"])
def test_glmfb_a_glm_code_run_the_endpoint_cannot_take_starts_on_the_claude_cli_not_mini(env, monkeypatch, case):
    """The arm (on mini-swe-agent) takes a code task while the chat endpoint is limited, out of quota or has no
    endpoint set: its run starts on the claude CLI, never on mini with no model to call (litellm's
    "Missing credentials", or exec failing on a prompt past the argument limit)."""
    from ttp import codearm
    run, spec = _glmfb_launch(env, monkeypatch, cool="" if case == "no endpoint" else case,
                              chat={"base_url": "https://chat.example.invalid/v1"} if case == "no endpoint" else None)
    assert json.loads(run["note"])[codearm.KEY]["run"] == "glm", "the arm took it"
    assert pathlib.Path(spec["argv"][0]).name == "claude" and "harness" not in spec and not spec.get("openai_env")
    assert not (pathlib.Path(run["dir"]) / "harness.sh").exists()
    if case != "no endpoint":
        assert run["provider"] == spec["provider"] == "claude" and "sonnet" in spec["model"] and "bridge" not in spec
    else:   # the CLI on the chat bridge, which refuses to start: a free fallback (the test below)
        assert run["provider"] == "chat" and spec["bridge"]["endpoint"] == ""


def test_glmfb_a_glm_code_run_the_endpoint_can_take_still_starts_on_mini_over_the_bridge(env, monkeypatch):
    run, spec = _glmfb_launch(env, monkeypatch)
    assert run["provider"] == spec["provider"] == "chat" and spec["bridge"]["endpoint"] == "placeholder"
    assert spec["argv"] == ["/bin/sh", str(pathlib.Path(run["dir"]) / "harness.sh")] and spec["openai_env"] is True
    assert spec["harness"] == {"kind": "mini-swe-agent", "version": "2.4.6"}


@_pytest.mark.parametrize("exit_info,cause", [
    ({"rc": 1}, "failed"),   # mini exits before its first step (litellm: "Missing credentials"), handing off nothing
    ({"rc": None, "launched": False,
      "launch_error": "could not start the agent: chat bridge: providers.chat.endpoint is not set"}, "error"),
])
def test_glmfb_a_mini_run_that_fails_at_start_without_endpoint_credentials_is_a_free_route_fallback(
        env, monkeypatch, exit_info, cause):
    """Such a start failure is the route's existing fallback end (daemon.FREE_ENDS): requeued at its tier with no
    attempt spent, and its next run starts on the core provider."""
    from ttp import codearm, daemon, policyfiles, route
    p = _glmfb_project(env)
    (p.harness / policyfiles.FILE).unlink()   # no policy check: it would bounce this task, which has no branch here
    d = daemon.Daemon(p.base)
    monkeypatch.setattr(d, "observe", lambda *a, **k: None)
    tid = _ca_task(p)["id"]
    routed = {"tier": "standard", "to": "chat", "model": _CA_GLM["model"], "fallback": "none", codearm.KEY: "glm"}
    run, task = _route_finish(p, d, tid, {**exit_info, "bridge": {"last_fault": "", "faults": {}}}, routed=routed)
    assert run["status"] == "fallback" and run["status"] in daemon.FREE_ENDS
    assert task["status"] == "queued" and int(task["attempts"] or 0) == 0 and task["tier"] == "standard"
    assert route.of(run["note"])["fallback"] == cause
    if cause == "error":   # the endpoint cools: other tasks start on core too
        assert route.cooling(p.db, "chat", time.time()) == "error"
    else:
        assert route.fell_back_before(p.db, tid) == run["id"]


def test_config_and_doctor_say_why_a_live_share_on_claude_code_is_zero():
    import inspect
    from ttp import cli
    assert "config_warnings(p, a.key)" in inspect.getsource(cli.cmd_config)
    assert "codearm.refusal(p.config()" in inspect.getsource(cli.config_warnings)
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
    p = SimpleNamespace(root=repo, worktrees=tmp_path / "no-worktrees", db=None)   # no code_arm line: the db is never read
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


# Every run on the chat model: outward posts become drafts for an Opus step; a device run stops at a failed gate.
def _glm_run_env(monkeypatch, tmp_path, flag="1"):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path, arm=bool(flag))
    monkeypatch.setenv("TTP_RUN_DIR", str(rd))
    monkeypatch.setenv("TTP_PROJECT", str(tmp_path / "proj"))
    monkeypatch.delenv("TTP_CODE_ARM_GUARD", raising=False)
    for var in glmguard.SCRATCH_VARS:   # a checks run's own short TMPDIR holds the test HOME: not this run's scratch
        monkeypatch.delenv(var, raising=False)
    if flag:
        monkeypatch.setenv(glmguard.RUN_ENV, flag)
    else:
        monkeypatch.delenv(glmguard.RUN_ENV, raising=False)
    return wt, rd


@_pytest.mark.parametrize("tool,tool_input,channel", [
    ("Bash", {"command": "ttp notify 'build is green'"}, "notify"),
    ("mcp__slack__post_message", {"channel": "C1", "text": "build is green"}, "mcp__slack__post_message")])
def test_a_glm_work_runs_outward_post_is_refused_and_recorded_as_a_draft(tool, tool_input, channel, monkeypatch, tmp_path):
    from ttp import glmguard, hook
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": tool_input, "cwd": str(wt)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "recorded as a draft" in out["hookSpecificOutput"]["permissionDecisionReason"]
    got = glmguard.drafts(rd)
    assert [(d["channel"], d["text"]) for d in got] == [(channel, "build is green")]
    assert not (rd / glmguard.HIT).exists(), "the run goes on"
    hook.pre_tool_use({"tool_name": tool, "tool_input": tool_input, "cwd": str(wt)})
    assert len(glmguard.drafts(rd)) == 1, "a retried post is one draft"


def test_a_run_off_the_chat_model_posts_without_a_draft(monkeypatch, tmp_path):
    from ttp import glmguard, hook
    wt, rd = _glm_run_env(monkeypatch, tmp_path, flag="")
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ttp notify hi"}, "cwd": str(wt)})
    assert out is None and glmguard.drafts(rd) == [] and glmguard.glm_run() is None


def test_the_daemon_hands_a_runs_drafts_to_the_coordinator_once_as_one_event(env, tmp_path):
    import json
    from ttp import daemon, glmguard
    p = make(env)
    rd = tmp_path / "77"
    rd.mkdir()
    glmguard.drafted(rd, {"channel": "notify", "target": "user", "text": "build is green", "why": "w", "command": "c"})
    assert daemon.relay_glm_drafts(p, {"id": 5}, rd) == 1
    ev = p.db.one("SELECT * FROM events WHERE kind=?", (daemon.GLM_DRAFTS_KIND,))
    assert ev["task"] == 5 and "none was sent" in ev["text"] and "AI authorship tag" in ev["text"]
    assert json.loads(ev["text"].split("\n", 1)[1])["text"] == "build is green"
    assert daemon.relay_glm_drafts(p, {"id": 5}, rd) == 0
    assert daemon.GLM_DRAFTS_KIND not in __import__("ttp.coordroute", fromlist=["x"]).ROUTINE_KINDS


@_pytest.mark.parametrize("cmd,device,stops", [
    ("ttp checks", True, True), ("ttp lock blx -- tt-smi -r", True, True), ("./run_gate.sh --fast", True, True),
    ("ls /missing", True, False), ("ttp checks", False, False)])
def test_a_device_glm_run_stops_at_its_first_failed_gate_command_only(cmd, device, stops, monkeypatch, tmp_path):
    import json
    from ttp import glmguard, hook
    wt, rd = _glm_run_env(monkeypatch, tmp_path, glmguard.DEVICE if device else "1")
    glmguard.mark_run(rd, device, glmguard.gate_lines("Goal: x\ncheck: `./run_gate.sh --fast`\n"))
    out, _ = hook.post_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt),
                                 "tool_response": {"exit_code": 1, "stdout": "FAILED: 3 tests"}})
    assert (rd / glmguard.HIT).exists() is stops, cmd
    if stops:
        hit = json.loads((rd / glmguard.HIT).read_text())
        assert hit["kind"] == glmguard.GATE and "FAILED: 3 tests" in hit["output"]
        assert json.loads((rd / "result.json").read_text())["needs_opus"].startswith("gate: ")
        assert "goes back to Opus" in json.dumps(out)


def _glm_route_notes():
    """One run note per route that can send a run to the chat model, in the shapes the daemon writes."""
    from ttp import codearm, coordroute, glmwork, modelpick, route
    to = {"to": route.TO, "model": "glm"}
    return {
        "code_arm": {codearm.KEY: {"arm": "glm", "run": "glm"}, route.KEY: {**to, "tier": "standard"}},
        "glm_work": {route.KEY: {**to, "tier": "standard", glmwork.KEY: "glm"}},
        "coordinator_lane": {coordroute.LANE: "routine"},
        "coordinator_shadow": {coordroute.SHADOW: {"of": 1}},
        "tier_route": {route.KEY: {**to, "tier": "light", "rule": 0}},
        "model_pick": {route.KEY: {**to, "tier": "standard", "pick": True}, modelpick.KEY: {"pick": "glm"}},
    }


@_pytest.mark.parametrize("name", sorted(_glm_route_notes()))
def test_every_route_to_the_chat_model_starts_under_the_outward_guard(name):
    """Each route's note alone turns the guard on, so a launch on any provider is covered; a new route
    shape goes in _glm_route_notes."""
    import inspect
    from ttp import daemon, glmguard, route
    note = _glm_route_notes()[name]
    assert glmguard.run_env("claude", note) == {glmguard.RUN_ENV: "1"}, name
    assert glmguard.run_env("claude", note, device=True) == {glmguard.RUN_ENV: glmguard.DEVICE}, name
    assert glmguard.run_env(route.TO, None) == {glmguard.RUN_ENV: "1"}   # a bench launched as a task
    assert glmguard.run_env("claude", None) == {} and glmguard.run_env("claude", {route.KEY: {"to": "claude"}}) == {}
    src = inspect.getsource(daemon)
    assert "glmguard.run_env(provider, note, glmguard.device_task(task, self.cfg))" in src and "**glm_env," in src
    assert "relay_glm_drafts(self.p, task, run_dir)" in src


@_pytest.mark.parametrize("name", sorted(_glm_route_notes()))
def test_every_route_to_the_chat_model_gets_the_scratch_redirect_in_the_tool_hook(name, monkeypatch, tmp_path):
    """A run marked only as a chat-model run (no arm variable) hits the same write check: a report's
    `ttp said ... > /srv/x.json` (a new file outside the home folder and /tmp) is redirected to the run's scratch
    folder, never let through."""
    import json
    from ttp import glmguard, hook
    flag = glmguard.run_env("claude", _glm_route_notes()[name])[glmguard.RUN_ENV]
    wt, rd = _glm_run_env(monkeypatch, tmp_path, flag)
    glmguard.mark_run(rd, False, cwd=str(wt))
    cmd = "ttp said baseline --json --days 5 -n 30 > /srv/said_baseline.json 2>&1"
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt)})
    why = json.dumps(out)
    assert "deny" in why and "/srv/said_baseline.json" in why, name
    assert not (rd / glmguard.HIT).exists(), "a scratch file outside its folders is a redirect: the run goes on"


def test_glm_share_counts_the_guards_fires_per_day(env, tmp_path):
    from ttp import glmguard, glmshare
    rd = tmp_path / "r"
    rd.mkdir()
    glmguard.drafted(rd, {"channel": "notify", "target": "user", "text": "a"})
    glmguard.drafted(rd, {"channel": "notify", "target": "user", "text": "b"})
    glmguard.hit(rd, "`ttp checks` failed", glmguard.GATE, "out")
    assert glmshare.guard_fires(rd) == {"outward_drafts": 2, "gate_stops": 1, "guard_bounces": 0,
                                        "guard_redirects": 0}
    r2 = tmp_path / "r2"
    r2.mkdir()
    glmguard.mark(r2)
    glmguard.refused(r2, "`ttp upstream` is not allowed", cfg={})
    assert glmshare.guard_fires(r2)["guard_redirects"] == 1 and not glmshare.guard_fires(r2)["guard_bounces"], \
        "a redirect is counted apart: it is no bounce for the rollout judges"
    glmguard.refused(r2, "a run on the GLM arm never `git push`", cfg={})
    assert glmshare.guard_fires(r2)["guard_bounces"] == 1 and glmshare.guard_fires(r2)["guard_redirects"] == 1
    p = make(env)
    row = {"id": 1, "task": 1, "role": "worker", "provider": "chat", "model": "glm", "started": 1.7e9,
           "ended": 1.7e9, "note": None, "dir": str(rd)}
    day = glmshare.bad_output(p, [row])[0]
    assert (day["outward_drafts"], day["gate_stops"], day["runs"]) == (2, 1, 0)
    assert "outward drafts 2, gate stops 1" in glmshare.bad_lines([day])[0]


def _ua_cfgs():
    covered = {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}},
               "route": {"glm_work": {"on": True, "model": "example-org/work-model", "holdout": 0.999}}}
    return covered, _ca_cfg(glm={**_CA_GLM, "harness": "mini-swe-agent"}, glm_share=0.0001)


def test_a_code_task_the_user_ordered_onto_the_arm_runs_there_though_it_names_a_policy_file(env):
    from ttp import codearm, glmwork
    p = make(env)
    mid = p.db.post("in", "Run the delete batches on the cheaper code arm.", chat="c1")
    for cfg in _ua_cfgs():   # the holdout coin (covered) and the share coin (not) would both keep it off
        ordered = _ca_task(p, f"delete the dead helpers in src/rt/daemon.py\ncode_arm: user msg #{mid}")
        got = codearm.choose(p.db, cfg, ordered, "standard", policy=_PF_LIST)
        assert (got["arm"], got["run"], got["why"]) == ("glm", "glm", f"user msg #{mid}")
        assert codearm.show_line(codearm.recorded(p.db, ordered["id"])) == f"code route arm: glm (user msg #{mid})"
        assert codearm.choose(p.db, cfg, p.db.task(ordered["id"]), "standard", policy=_PF_LIST)["run"] == "glm"
        plain = _ca_task(p, "delete the dead helpers in src/rt/daemon.py")
        got = codearm.choose(p.db, cfg, plain, "standard", policy=_PF_LIST)
        if glmwork.settings(cfg):   # covered: no policy-file stay (GLM writes those files too)
            assert not got["why"].startswith("policy:")
        else:
            assert got["run"] == "claude" and got["why"].startswith("policy: names src/rt/daemon.py")
        pinned = _ca_task(p, f"delete the dead helpers\ncode_arm: user msg #{mid}", provider="claude")
        assert codearm.choose(p.db, cfg, pinned, "standard", policy=_PF_LIST)["run"] == "claude"   # other stays hold


def test_a_code_arm_user_line_from_a_follow_up_or_citing_no_user_message_is_ignored(env):
    from ttp import chaincap, codearm
    p = make(env)
    mid = p.db.post("in", "Run the delete batches on the cheaper code arm.", chat="c1")
    out = p.db.post("out", "On it.", chat="c1")
    cfg = _ua_cfgs()[1]
    spec = "delete the dead helpers in src/rt/daemon.py\ncode_arm: user msg #{}"
    for task, why in ((_ca_task(p, spec.format(mid), origin="daemon"), "task made by daemon"),
                      (_ca_task(p, spec.format(99999)), "#99999 is no user message"),
                      (_ca_task(p, spec.format(out)), f"#{out} is no user message")):
        mid_, ignored = codearm.user_arm(p.db, task)
        assert mid_ == 0 and ignored.startswith(f"code_arm line ignored: {why}")
        got = codearm.choose(p.db, cfg, task, "standard", policy=_PF_LIST)
        assert got["run"] == "claude" and why in got["why"]   # a policy reason, or a daemon task's own stay
    # a worker's follow-up queued by the daemon (auto hop) drops the line, so it never reaches the new task
    parent = _ca_task(p, spec.format(mid))
    hop = chaincap.hop_spec(p, parent, {"title": "next batch", "spec": spec.format(mid) + "\nthen run the tests"})
    assert "code_arm" not in hop and "then run the tests" in hop
    assert codearm.user_arm(p.db, parent)[0] == mid


def test_a_user_ordered_task_is_not_bounced_for_touching_a_policy_file(env, tmp_path):
    from types import SimpleNamespace
    from ttp import policyfiles
    p = make(env)
    mid = p.db.post("in", "Run the delete batches on the cheaper code arm.", chat="c1")
    pp = SimpleNamespace(db=p.db, root=tmp_path, worktrees=tmp_path / "no-worktrees")
    changed = {"src/rt/daemon.py": 4}
    ordered = _ca_task(p, f"delete the dead helpers\ncode_arm: user msg #{mid}")
    assert policyfiles.bounce_diff(pp, ordered, _PF_LIST, changed) == ""
    plain = _ca_task(p, "delete the dead helpers")
    assert policyfiles.bounce_diff(pp, plain, _PF_LIST, changed) == "policy files touched: src/rt/daemon.py"
    worker = _ca_task(p, f"delete the dead helpers\ncode_arm: user msg #{mid}", origin="daemon")
    assert policyfiles.bounce_diff(pp, worker, _PF_LIST, changed) == "policy files touched: src/rt/daemon.py"


def test_a_project_the_glm_work_route_covers_has_no_policy_file_stay_or_refusal_but_others_keep_both(env):
    """GLM writes policy files too on a covered project: no `policy:` stay, an empty policy list is no refusal and
    no share 0; a project outside the work route keeps all three."""
    from ttp import codearm
    p = make(env)
    covered, plain = _ua_cfgs()
    named = _ca_task(p, "rewrite coordfix._python_reads so a write is never labelled a read")
    assert codearm.stays(p.db, covered, named, "standard", policy=_PF_LIST) == ""
    assert codearm.stays(p.db, plain, named, "standard", policy=_PF_LIST).startswith("policy: names coordfix")
    assert codearm.settings(covered, policy=[]) and not codearm.settings(plain, policy=[])
    assert codearm.refusal(covered, policy=[]) == "" and "policy file list" in codearm.refusal(plain, policy=[])
    covered["route"]["glm_work"]["harness"] = "codex"
    assert codearm.settings(covered) is None   # an unguarded harness still turns it off



def test_a_covered_follow_up_of_a_claude_arm_change_is_decided_afresh_except_in_the_holdout(env):
    from ttp import chaincap, codearm
    p = make(env)
    covered, plain = _ua_cfgs()
    covered["route"]["glm_work"]["holdout"] = 0
    def follow(root, cfg):
        fix = p.db.add_task("Fix review #9: tidy", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"{chaincap.FOLLOWS}{root['id']}"]), fix))
        return codearm.choose(p.db, cfg, p.db.task(fix), "standard", policy=_PF_LIST)
    kept = _ca_task(p)   # its parent stayed on Claude for a rule since removed
    p.db.set_kv(f"{codearm.ARM_KV}{kept['id']}", {"arm": "claude", "why": "conclusion", "seed": "s", "ts": 1.0})
    assert follow(kept, covered)["run"] == "glm"
    held = _ca_task(p)   # the Claude holdout keeps its whole lineage there
    p.db.set_kv(f"{codearm.ARM_KV}{held['id']}", {"arm": "claude", "why": "holdout: seeded coin", "seed": "s", "ts": 1.0})
    got = follow(held, covered)
    assert got["arm"] == "claude" and got["why"] == f"holdout: seeded coin (follows #{held['id']})"
    again = _ca_task(p)   # outside the work route a lineage always keeps its parent's arm
    p.db.set_kv(f"{codearm.ARM_KV}{again['id']}", {"arm": "claude", "why": "conclusion", "seed": "s", "ts": 1.0})
    assert follow(again, plain)["arm"] == "claude"


def test_a_review_of_a_change_touching_a_listed_file_names_it_on_a_covered_project_only(env):
    from ttp import configwho, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")
    p.set_config("core_provider", "claude")
    _, _, _, (rev,) = _finish_code(env, p, "plain", {"src/other.py": 3})
    assert "policy files" not in rev["spec"]
    _, _, _, (rev,) = _finish_code(env, p, "listed", {"src/policy_module.py": 3})
    assert "policy files" not in rev["spec"]   # not covered: the old pin keeps such a task on Claude, no note
    p.set_config("route.glm_work", {"on": True, "model": "example-org/GLM-9", "holdout": 0}, by=configwho.by("user", via="cli"))
    _, _, _, (rev,) = _finish_code(env, p, "listed again", {"src/policy_module.py": 3})
    assert "touches policy files" in rev["spec"] and "src/policy_module.py" in rev["spec"] and "line by line" in rev["spec"]
    _, _, _, (rev,) = _finish_code(env, p, "plain again", {"src/other.py": 3})
    assert "touches policy files" not in rev["spec"]


# The denylist: only the harmful kinds measured on real runs are refused; everything else runs.
_GG_DENIED = [
    "rm -rf /srv/x", "ssh box-a rm -rf /srv/x", "curl -X POST -d a=b https://x.invalid",
    "ttp land 5", "ttp express 5", "ttp push --land", "ttp p express 5 --why x", "git push", "ttp remove p",
    "ttp machines change add box-a 'w' --undo true", "ttp machines set box-a --tag a", "ttp notify x",
    "bash <<'EOF'\nttp notify x\nEOF", 'echo "<<EOF"; ttp notify x',
    "sudo systemctl restart svc-a", "ssh box-a sudo -n systemctl stop svc-a", "service svc-a start",
    "bash -c 'systemctl --user enable svc-a'", "ssh box-a bash -s <<'EOF'\nls\nsudo systemctl restart svc-a\nEOF",
    "bash -s <<EOF\nsystemctl --now mask svc-a\nEOF"]
_GG_ALLOWED = [
    "echo hi > $TMPDIR/a", "echo hi > /tmp/a", "echo hi > out.txt", "ssh box-a mkdir -p out", "curl --unix-socket /run/b.sock -X POST http://l/x",
    "ttp machines change show 3", "ttp machines --help", "python3 <<'EOF'\nprint('`ttp notify x`')\nEOF",
    "grep -o x f", "ssh -o BatchMode=yes box-a ls", "journalctl -o cat -n 5", "ttp lock b -- ssh -o x box-a ls",
    "ttp note --status 5", "ttp slop", "ttp memory --search notify", "ttp task add t s", "ttp obligation progress o1 x",
    "systemctl status svc-a", "ssh box-a systemctl is-active svc-a", "service --status-all",
    "ttp quarantine still 5"]


@_pytest.mark.parametrize("cmd", _GG_DENIED)
def test_glm_guard_refuses_each_kind_on_the_denylist(cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {**_GG_RUN_ENV, "TMPDIR": str(rd / "tmp")}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


@_pytest.mark.parametrize("cmd", _GG_ALLOWED)
def test_glm_guard_runs_everything_off_the_denylist(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {**_GG_RUN_ENV, "TMPDIR": str(rd / "tmp")}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e) is None, cmd


# A name the guard protects, in a quoted heredoc body only written to a file, opens nothing; anywhere else in the
# command's text (an unquoted heredoc body runs its `$(...)` and backticks) it is refused, quoted or not.
_GG_MENTION_ALLOWED = [
    "cat >> tests/x.py <<'EOF'\np = state / \"project.db\"\nEOF",   # test code that names the database file
    "cat > t.py <<'EOF'\nenv = dict(TTP_RUN_DIR='x', TTP_TASK='3')\nEOF",
    "cat > notes.md <<'EOF'\nthe guard writes glm_guard.json and reads machine-workers.json\nEOF",
    "cat > tests/t.py <<'EOF'\nenv = {'TTP_CODE_ARM_GUARD': '1'}\nEOF"]
_GG_MENTION_REFUSED = [
    "sqlite3 $TTP_PROJECT/state/project.db 'x'", "grep x project.db", "grep -f project.db x",
    "cat project.db | grep x", "cd state && sqlite3 project.db .tables", "git log -S glm_guard.json",
    "python3 -c 'open(\"glm_guard.on\", \"w\")'", "find . -name glm_guard.on -delete", "rm ../9/glm_guard.json",
    "python3 - <<'EOF'\nimport sqlite3\nsqlite3.connect('/x/project.db')\nEOF",
    "bash <<'EOF'\nsqlite3 project.db .tables\nEOF",
    "cat > s.sh <<'EOF'\nsqlite3 project.db .x\nEOF\nbash s.sh",
    "echo > \"$TTP_RUN_DIR/glm_guard.on\"", "printf '' > \"$TTP_RUN_DIR/glm_guard.json\"",
    "cat /dev/null > \"$TTP_RUN_DIR/glm_guard.on\"", "cat > \"$TTP_RUN_DIR/glm_guard.on\" <<'EOF'\noff\nEOF",
    "echo x >> 'project.db'", "cat \"$TTP_PROJECT/state/project.db\"",
    "TTP_CODE_ARM_GUARD=10 ls", "TTP_CODE_ARM_GUARD=1.5 ls", "unset TTP_CODE_ARM_GUARD", "export TTP_CODE_ARM_GUARD=1 TTP_RUN_DIR=x",
    "TTP_CODE_ARM_GUARD=1 TTP_RUN_DIR=x ls", "bash -c 'unset TTP_RUN_DIR'", "TTP_CODE_ARM_GUARD=0 ttp notify x",
    "cat > t.py <<EOF\n$(sqlite3 $TTP_PROJECT/state/project.db \"delete from tasks\")\nEOF",
    "cat > t.py <<EOF\n$(rm $TTP_RUN_DIR/glm_guard.on)\nEOF", "echo x > t.txt <<EOF\n`unset TTP_RUN_DIR`\nEOF",
    "TTP_CODE_ARM_GUARD=1\"0\" ls", "TTP_CODE_ARM_GUARD=1 ls", "export TTP_CODE_ARM_GUARD=1",
    "TTP_CODE_ARM_GUARD=1 python3 -m pytest -q tests/x.py", "bash <<'EOF'\nrm $TTP_RUN_DIR/glm_guard.on\nEOF",
    # a python script's one-line literal names the file it opens, a comment beside it or not
    "python3 - <<'EOF'\n# read the tasks\nimport sqlite3\nc = sqlite3.connect(\"/x/project.db\")  # by hand\nEOF",
    "python3 - <<'EOF'\nimport sqlite3\nsqlite3.connect(\"\"\"/x/project.db\"\"\")\nEOF",
    "python3 - <<'EOF'\nDB = '/x/state/' + 'project.db'\nprint(open(DB, 'rb').read(16))\nEOF",
    "python3 - <<'EOF'\nimport json\njson.load(open('/h/.tt-project/machine-workers.json'))\nEOF",
    "python3 - <<'EOF'\nx = '''\nproject.db\nEOF",   # a script tokenize cannot read stays whole
    "python3 - <<EOF\n# project.db\nEOF", "node - <<'EOF'\n// project.db\nEOF",
    # a multi-line literal the script runs is code
    "python3 - <<'EOF'\nimport os\nos.system(\'\'\'\nsqlite3 /x/project.db \"select 1\"\n\'\'\')\nEOF",
    "python3 - <<'EOF'\nexec(\'\'\'\nimport sqlite3\nsqlite3.connect(\"/x/project.db\")\n\'\'\')\nEOF",
    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run([\"bash\", \"-c\", \"\"\"\ncat project.db\n\"\"\"])\nEOF"]
# The code a python script writes into a file (a multi-line literal) and its comments are text no call reads as a path
_GG_PY_TEXT_ALLOWED = [
    "python3 - <<'EOF'\np = 'src/n.py'\ns = open(p).read()\ns = s.replace(\"x = 1\", '''    db = DB(p.state / \"project.db\")\n"
    "    try:\n        rows = db.q(\"SELECT 1\")''')\nopen(p, \"w\").write(s)\nEOF",
    "python3 - <<'PYEOF'\np = 'src/r.py'\ns = open(p).read()\nold = '''def f(t):\n    \"\"\"Reads the database.\"\"\"\n"
    "    conn = connect_ro(t.state / \"project.db\")'''\nassert old in s\nPYEOF",
    "python3 - <<'EOF'\n# the events table is in project.db, which a run may not open: read the reports\nimport glob\n"
    "print(glob.glob('reports/*'))\nEOF"]


@_pytest.mark.parametrize("cmd", _GG_MENTION_ALLOWED)
def test_glm_guard_allows_a_protected_name_in_a_heredoc_body_only_written_to_a_file(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e) is None, cmd


@_pytest.mark.parametrize("cmd", _GG_PY_TEXT_ALLOWED)
def test_glm_guard_allows_a_protected_name_in_a_python_scripts_comment_or_multi_line_literal(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e) is None, cmd


@_pytest.mark.parametrize("cmd", _GG_MENTION_REFUSED)
def test_glm_guard_refuses_a_protected_name_anywhere_else_in_the_command(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


@_pytest.mark.parametrize("cmd", [
    "TMPDIR=/srv; echo hi > $TMPDIR/out", "export \"TMPDIR=/srv\"; echo hi > $TMPDIR/out",
    "unset TMPDIR; echo hi > $TMPDIR/out", "python3 -c 'import os' TMP=/srv; echo hi > $TMP/out",
    "export 'TMPDIR'=/srv; echo hi > $TMPDIR/x", "declare -x \"TMPDIR\"=/srv; echo hi > $TMPDIR/x",
    "read 'TMPDIR' <<< /srv; echo hi > $TMPDIR/x"])
def test_glm_guard_refuses_a_write_through_a_tmpdir_the_command_sets_outside_tmp(cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


# Words only in text are no command: a grep pattern, a commit message (heredoc, -m) naming a service change or a
# reset, an Opus plan request naming device reads, and printed text
_GG_TEXT_ONLY = {
    "grep": 'cd {wt} && grep -rn "SERVICE_RE\\|systemctl restart" tests/ | head -20; ls tests/ | head -50',
    "commit_heredoc": "cd {wt} && git add -A && git commit -q -F - <<'EOF'\nguard: read systemctl start/stop/restart"
                      " as a service change\n\nA plan step may run it.\nEOF\ngit log --oneline -2",
    "commit_m": 'cd {wt} && git commit -q -m "configwho: blame the newest change\n\nrows with a reset and an unknown'
                ' setter named \\`ttp ask\\`." && git log --oneline -3',
    "advise_plan": 'ttp advise --plan "one broker job on box-a that reads the chips from /sys/class/tenstorrent, then'
                   ' systemctl restart svc-a" > "$TTP_RUN_DIR/advise.out" 2>&1 &\necho "advise pid $!"',
    "rg": "rg -n 'tt-flash|systemctl stop' src/",
    "echo": "echo 'then systemctl restart svc-a and tt-flash'",
    "printf": 'printf "%s\\n" "reboot the box"',
}


@pytest.mark.parametrize("case", sorted(_GG_TEXT_ONLY))
def test_glm_guard_lets_service_and_device_words_in_quoted_text_run(case, tmp_path):
    from ttp import deviceplan, glmguard
    wt, rd = _gg_run(tmp_path)
    cmd = _GG_TEXT_ONLY[case].format(wt=wt)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e) is None
    assert not deviceplan.risky(cmd)


@pytest.mark.parametrize("cmd", [
    "sh -c 'systemctl restart svc-a'", "eval 'systemctl restart svc-a'", "echo 'systemctl restart svc-a' > r.sh; sh r.sh",
    "printf '%s\\n' 'systemctl restart svc-a' |& sh", "cat <<'EOF' | sh\nsystemctl restart svc-a\nEOF",
    "git commit -m \"$(systemctl restart svc-a)\"", "rg --pre 'systemctl restart svc-a' x .",
    "echo x; tt-smi -r 0", "git commit -m 'x' && tt-flash fw.bin", "grep -q x f || reboot",
    'echo x > "$TTP_RUN_DIR/glm_guard.json"',
])
def test_glm_guard_still_refuses_a_real_service_reset_or_guard_step_beside_quoted_text(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


@pytest.mark.parametrize("cmd", [
    'ttp advise --plan "check it; for each chip read it" > "$TTP_RUN_DIR/advise.out" 2>&1 &',
    'echo x > "$TTP_RUN_DIR/notes/a.txt"',
    'cp a "$TTP_RUN_DIR/b.txt"',
])
def test_glm_guard_lets_a_run_write_scratch_files_in_its_own_run_folder(cmd, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e) is None
    assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": f"{rd}/notes/b.md"},
                                  "cwd": str(wt)}, e) is None


@pytest.mark.parametrize("name", ["run.json", "glm_run.json", "glm_guard.json", "glm_guard.on", "refusals.jsonl",
                                  "glm_refusals.jsonl"])
def test_glm_guard_refuses_writes_to_the_run_folders_own_records_and_hands_them_back(name, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    for why in (glmguard.shell_refusal(f'cp a "$TTP_RUN_DIR/{name}"', str(wt), str(rd), e),
                glmguard.shell_refusal(f"ttp note hi > {rd}/{name} 2>&1 &", str(wt), str(rd), e),
                glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": f"{rd}/{name}"},
                                       "cwd": str(wt)}, e)):
        assert why and glmguard.classify(why)[1] == glmguard.HARMFUL, why


# A program name, a variable's name or a `sleep` time built from an expansion: the variable holds `SEC`, the loop
# body sleeps on it, and the script is written by an unquoted heredoc inside `bash -c`
_GG_HIDDEN_SLEEP = ("ttp lock box-a -- bash -c '\nset -e\nmkdir -p /tmp/ttp-1\nS=S; I=2\n"
                    "cat > /tmp/ttp-1/hb.sh <<EOF\n#!/bin/bash\nfor d in /sys/class/tenstorrent/*; do\n"
                    "  v1=\\$(cat \"\\$d/tt_heartbeat\" 2>/dev/null || echo unreadable)\n    \\${${S}EC} \\$I\n"
                    "  v2=\\$(cat \"\\$d/tt_heartbeat\" 2>/dev/null || echo unreadable)\ndone\nEOF\n"
                    "chmod +x /tmp/ttp-1/hb.sh\n' 2>&1")
# Quoted text naming an expansion runs none: a table row, a commit message, a grep pattern, printed text, python code
# and a script written for later (a quoted heredoc written to a file and run in a later call passes: an accepted gap)
_GG_HIDDEN_TEXT = {
    "table": "cat > notes.md <<'EOF'\n| step | how |\n| a | `ttp push` |\nEOF",
    "commit_m": "git commit -q -m 'docs: name the step (`ttp push`) plainly'",
    "commit_heredoc": "git commit -F - <<'EOF'\nguard: run the suite (`pytest -q`) first\nEOF",
    "grep": "grep -n '; $(' file.py",
    "echo": "echo 'use; $(date)'",
    "python": "python3 - <<'EOF'\nimport os\nx = 'a | `b`'\nprint(os.path.expandvars(\"$HOME\"))\nEOF",
    "script": "cat > s.sh <<'EOF'\n#!/bin/bash\nsleep \"$DELAY\"\nEOF",
}


@pytest.mark.parametrize("name", sorted(_GG_HIDDEN_TEXT))
def test_glm_guard_reads_a_hidden_program_name_only_in_command_words(name, monkeypatch, tmp_path):
    """cells: guard_hidden_text"""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", str(rd / "tmp"))
    assert glmguard.shell_refusal(_GG_HIDDEN_TEXT[name], str(wt), str(rd)) is None


def test_a_glm_run_may_not_hide_a_program_name_or_sleep_time_and_it_is_a_redirect(monkeypatch, tmp_path):
    """cells: guard_hidden_redirect"""
    import json
    from ttp import glmguard, hook
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    glmguard.mark_run(rd, False, cwd=str(wt))
    for cmd in ('"$TMPDIR/venv/bin/python" -m pytest -q', "ls ${TMPDIR:-x}/b", "for f in *; do echo $f; done",
                "sleep 2", "x=$(date); echo $x", "cat > a.md <<'EOF'\n`ttp push` lands it\neval the result\nEOF"):
        assert glmguard.shell_refusal(cmd, str(wt), str(rd)) is None, cmd
    for cmd in ("S=sleep; $S 2", "sleep $I", "x; `printf sleep` 2", "x && $(printf sleep) 2", 'eval "$x"',
                "touch ${!n}", "S=sl; ${S}eep 2", "x && sl${E} 2", _GG_HIDDEN_SLEEP):
        why = glmguard.shell_refusal(cmd, str(wt), str(rd)) or ""
        # the refusal names the plain literal form to resend
        assert why.startswith(glmguard.HIDDEN_WHY) and why.endswith(glmguard.HIDDEN_HINT), cmd
        assert "write the target path literally in the command's own words" in why, cmd
    assert glmguard.classify(glmguard.HIDDEN_WHY)[:2] == ("expansion", glmguard.REDIRECTED)
    call = lambda: json.dumps(hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": _GG_HIDDEN_SLEEP},
                                                 "cwd": str(wt)})[0])
    # a repeat is a redirect too, and an unrelated earlier refusal never turns one into a hand-back: only the
    # third refusal in one run (glm_refusals.hand_back_after) does
    (rd / hook.REFUSALS_FILE).write_text('{"kind": "worker_role", "command": "sleep 2"}\n')
    for _ in range(2):
        assert glmguard.HIDDEN_WHY in call() and not (rd / glmguard.HIT).exists()
    assert "back to Opus" in call() and (rd / glmguard.HIT).exists()


@pytest.mark.parametrize("cmd", ["echo 'tt-smi -r 0' | bash", "printf 'tt-flash fw' | sh", "echo 'tt-smi -r 0' | zsh",
                                 "echo -e 'ls\\ntt-smi -r 0' |& dash", "printf '%s\\n' 'tt-flash fw' | bash -s"])
def test_glm_guard_reads_text_piped_into_a_shell_as_commands(cmd, monkeypatch, tmp_path):
    """cells: guard_piped_shell"""
    from ttp import deviceplan, glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", str(rd / "tmp"))
    assert deviceplan.risky(cmd), cmd
    assert glmguard.shell_refusal(cmd, str(wt), str(rd)), cmd
    assert glmguard.shell_refusal(cmd.replace("tt-smi -r 0", "ls").replace("tt-flash fw", "ls"), str(wt), str(rd)) \
        is None


@pytest.mark.parametrize("rest", ["", " | bash", "; echo $BASH_COMMAND", "; history | tail -1 | sh", '; eval "$_"'])
def test_a_ttp_message_is_data_only_as_pathguard_decides(rest):
    """cells: guard_message_data"""
    from ttp import hook, pathguard
    cmd = 'ttp note "systemctl restart x"' + rest
    assert ("systemctl" in hook._split(cmd)[0]) == ("systemctl" in pathguard._blank_messages(cmd)) == bool(rest)


def test_glm_guard_lets_a_run_read_a_rollout_and_refuses_changing_one(monkeypatch, tmp_path):
    """cells: guard_rollout_read"""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", str(rd / "tmp"))
    home = '"$TMPDIR/smoke/home"'
    cmd = (f"cd {wt} && mkdir -p {home}/.tt-project && cat > {home}/.tt-project/settings.json <<'EOF'\n"
           '{"rollout": {"flags": {"a": 1}}}\nEOF\n'
           f"cd {wt} && TTP_HOME={home} PYTHONPATH=$PWD/runtime python3 -m ttp rollout status; echo \"rc=$?\"; "
           f"TTP_HOME={home} ttp rollout status --json | head -20")
    assert glmguard.shell_refusal(cmd, str(wt), str(rd)) is None
    assert glmguard.cli_refusal("rollout", "status") is None and glmguard.cli_refusal("rollout") is None
    assert glmguard.cli_refusal("rollout", "arm", ["--help"]) is None
    assert glmguard.cli_refusal("remove", "", ["-h"]) is None and glmguard.cli_refusal("remove", "", ["--help"]) is None
    assert glmguard.cli_refusal("remove", "", ["demo"]), "a remove that deletes stays refused"
    for act in ("arm", "set", "waves", "rollback"):
        why = glmguard.cli_refusal("rollout", act)
        assert why, act
        assert glmguard.shell_refusal(f"ttp rollout {act} x 1", str(wt), str(rd)), act


def test_glm_guard_lets_python_code_in_a_quoted_heredoc_name_the_run_variables(monkeypatch, tmp_path):
    """cells: guard_python_body"""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", str(rd / "tmp"))
    cmd = (f"cd {wt} && python3 - <<'EOF'\np = 'tests/test_x.py'\ns = open(p).read()\n"
           's = s.replace("x = 1", """env = {**os.environ, "TTP_RUN_DIR": str(d), "TTP_PROJECT": str(p), '
           '"TTP_RUN_ID": "dbg", "TTP_TASK": "1"}""")\nopen(p, "w").write(s)\nEOF')
    assert glmguard.shell_refusal(cmd, str(wt), str(rd)) is None
    for bad in ("export TTP_RUN_DIR=/x", f'echo x > "{rd}/glm_guard.json"'):
        assert (glmguard.shell_refusal(bad, str(wt), str(rd)) or "").startswith(glmguard.GUARD_WHY), bad
    # the run folder stays writable: a note or a scratch file there is no record of the guard's
    for ok in (f'echo "step 1 done" >> "{rd}/notes.md"', f"cat > {rd}/scratch.txt <<'EOF'\nx\nEOF"):
        assert glmguard.shell_refusal(ok, str(wt), str(rd)) is None, ok


def test_glm_guard_lets_commands_read_or_name_the_guard_files_and_refuses_their_edits(monkeypatch, tmp_path):
    """cells: guard_file_reads. A `cd` before plain readers, jq, ls, and inline python the run-time judge watches
    (replayed stops: `cd <run>; head glm_guard.json`, a loop of `python3 -c` json reads) read the guard's files;
    every edit of them stays refused."""
    from ttp import glmguard, pyaudit
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    (rd / "glm_guard.json").write_text("{}")
    other = tmp_path / "runs" / "9"
    py = f'python3 -c "import json; print(json.load(open(\'{other}/glm_guard.json\')))"'
    reads = [f"cd {rd.parent}; head -c 500 {rd.name}/run.json; echo; head -c 400 {rd.name}/glm_guard.json",
             f"cd {rd} && grep -n on glm_guard.json 2>/dev/null | head -5 | cut -c1-40; tail -1 glm_guard.on",
             f"jq .why {rd}/glm_guard.json", f"wc -l {other}/glm_guard.json", "echo glm_guard.on",
             f"cd {rd}; cat glm_guard.json | head -c 600; ls ../ | grep -v x | head",
             "python3 -m pytest -q tests/x.py -k 'glm_guard or glmenv'", f"for r in 9; do echo $r; {py}; done"]
    edits = [f"cd {rd}; rm glm_guard.on", f"cd {rd} && echo off > glm_guard.on", f"cd {rd}; cat x >> glm_guard.json",
             f"echo x | tee {rd}/glm_guard.on", f"sed -i 1d {rd}/glm_guard.json", f"cp /dev/null {rd}/glm_guard.on",
             f"mv x {rd}/glm_guard.json", f"truncate -s0 {rd}/glm_guard.on", f"ln -sf /dev/null {rd}/glm_guard.on",
             "echo glm_guard.on | xargs rm", 'cd "$(rm glm_guard.on)"', "F=glm_guard.on; cd x; rm $F",
             f"sed -n 1p {rd}/glm_guard.json", f"sed -n 'w {rd}/glm_guard.on' x", f"jq . x > {rd}/glm_guard.json",
             f"{py}; rm {rd}/glm_guard.on", f'python3 -c "print(\'{rd}/glm_guard.on\')" | xargs rm',
             f'python3 -c "print(\'rm {rd}/glm_guard.on\')" | bash', f'f=$(python3 -c "print(\'{rd}/glm_guard.on\')"); rm "$f"',
             f'bash -c "$(python3 -c "print(\'rm {rd}/glm_guard.on\')")"', f'python3 -B -c "print(1)"; rm {rd}/glm_guard.on',
             f'python3 -c "open(\'{rd}/glm_guard.on\', \'w\')"']
    for c in reads[:-1]:
        assert glmguard.shell_refusal(c, str(wt), str(rd)) is None, c
    for c in edits:
        assert (glmguard.shell_refusal(c, str(wt), str(rd)) or "").startswith(glmguard.GUARD_WHY), c
    assert glmguard.shell_refusal(reads[-1], str(wt), str(rd)) == glmguard.GUARD_WHY   # no run-time judge: refused
    _audited_env(monkeypatch, tmp_path)
    for c in reads:   # the judge loads: the python read is judged as it runs
        assert glmguard.shell_refusal(c, str(wt), str(rd)) is None, c
    for c in edits[:-1]:
        assert (glmguard.shell_refusal(c, str(wt), str(rd)) or "").startswith(glmguard.GUARD_WHY), c
    assert glmguard.shell_refusal(reads[-1].replace("python3 -c", "PYTHONPATH= python3 -c"), str(wt), str(rd))
    # what the judge refuses as the python runs: a write to the run's guard record
    assert (pyaudit.glm_write(str(rd / "glm_guard.on"), {"run_dir": str(rd), "roots": [str(wt), str(rd)]})
            or "").startswith(glmguard.GUARD_WHY)


@pytest.mark.parametrize("cmd, ok", [
    ("TTP_PROJECT={p} python3 bin/ttp paths 2>&1 | tail -3", True), ('TTP_PROJECT="{p}" ttp paths', True),
    ("cd x && TTP_PROJECT='{p}' ttp paths", True), ("TTP_PROJECT={p}x ttp paths", False),
    ("TTP_PROJECT= ttp paths", False), ("TTP_PROJECT={p} TTP_TASK=1 ttp paths", False),
    ("TTP_PROJECT={p}$X ttp paths", False), ("TTP_RUN_DIR={p} ttp paths", False)])
def test_glm_guard_lets_a_command_restate_an_identity_it_already_has_and_refuses_any_other_value(cmd, ok, tmp_path):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    p = str(tmp_path / "proj")
    why = glmguard.shell_refusal(cmd.format(p=p), str(wt), str(rd), {"TTP_PROJECT": p, **_GG_RUN_ENV})
    assert (why is None) == ok, why
    assert ok or glmguard.classify(why)[1] == glmguard.HARMFUL


@_pytest.mark.away_from_tmp
def test_glm_guard_lets_a_run_write_in_the_work_folders_its_spec_names_and_no_project_folder(tmp_path, monkeypatch):
    from ttp import glmenv, glmguard
    home = tmp_path / "home"
    tp = home / ".tt-project"
    bench, proj, scratch = tp / "model-bench" / "set", home / "a" / "b" / "proj", tp / "scratch" / "sp"
    harness = [tp / "lib" / "0.2.1" / "runtime" / "ttp", tp / "locks" / "a", tp / "checks-env" / "a",
               tp / "model-bench" / "harness" / "a"]
    for d in (bench, proj / "tt-project" / "state", proj / "tt-project" / "harness", scratch / "tt-project" / "state",
              home / "w" / "bench" / "set", home / "p" / "tt-project" / "x", home / "z" / "tt-project" / "state",
              home / "z" / "y" / "x", *harness):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    base = tmp_path / "base"
    spec = ("Extend ~/.tt-project/model-bench/set/run.py; not ~/w/bench/set/run.py, ~/a/b/proj (it holds a "
            "project), ~/.tt-project/scratch/sp (a scratch project) or ~/.tt-project/locks/a.")
    assert glmguard.spec_dirs(spec, base) == [str(bench)]
    assert glmguard.spec_dirs(spec, bench.parent / "base") == [str(bench)]   # it holds this project's base
    for no_root in ("~/a/b/proj", "~/.tt-project/scratch/sp", "~/w/bench/set", "~/p/tt-project/x", "~/z/y/x",
                    "~/.tt-project/lib/0.2.1/runtime/ttp", "~/.tt-project/locks/a", "~/.tt-project/checks-env/a",
                    "~/.tt-project/model-bench/harness/a", "~/w/none/at/all"):
        assert glmguard.spec_dirs(f"Work in {no_root}", base) == [], no_root
    wt, rd = proj / "tt-project" / "worktrees" / "t1", proj / "tt-project" / "state" / "runs" / "1"
    wt.mkdir(parents=True), rd.mkdir(parents=True)
    glmguard.mark_run(rd, False, cwd=str(wt), dirs=[str(bench), str(proj)])   # a root that holds a project, as a wider guard grants
    e = {"TTP_PROJECT": str(tmp_path / "proj"), "TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    assert glmguard.shell_refusal(f"cd {bench} && cp run.py run.py.bak", str(wt), str(rd), e) is None
    assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": f"{bench}/run.py"},
                                  "cwd": str(wt)}, e) is None
    for d in (tp / "locks" / "a", scratch):   # the harness's own folder, a project's folder
        assert glmguard.shell_refusal(f"cp {wt}/a {d}/a", str(wt), str(rd), e), d
    assert glmguard.shell_refusal(f"cp {wt}/a {home}/w/bench/set/a", str(wt), str(rd), e) is None   # a home write
    for path in (f"{proj}/tt-project/state/x.json", f"{proj}/tt-project/harness/CHARTER.md",
                 f"{scratch}/tt-project/state/x.json"):
        for cmd in (f"echo x > {path}", f"echo x >> {path}"):
            why = glmguard.shell_refusal(cmd, str(wt), str(rd), e)
            assert why and glmguard.classify(why)[:2] == ("records", glmguard.HARMFUL), cmd   # a project's records
            assert glmenv.refusal(cmd, str(wt), str(rd), e) == why, cmd                      # the arm's own layer asks the same
        assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(wt)}, e), path
    state = proj / "tt-project" / "state"   # a `cd` into a project's records never makes them the run's own
    assert glmguard.shell_refusal(f"cd {state} && echo x > x.json", str(wt), str(rd), e)
    assert glmguard.shell_refusal("echo x > x.json", str(state), str(rd), e)
    assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": f"{state}/x.json"},
                                  "cwd": str(state)}, e)
    # the run's own worktree and run folder stay writable, though both sit in a project's tt-project folder
    for ok in (f"echo x > {wt}/a.py", f"echo x > {rd}/notes.md", "cat > " + str(rd) + "/scratch.txt <<'EOF'\nx\nEOF"):
        assert glmguard.shell_refusal(ok, str(wt), str(rd), e) is None, ok
    assert glmguard.tool_refusal({"tool_name": "Write", "tool_input": {"file_path": f"{rd}/notes/b.md"},
                                  "cwd": str(wt)}, e) is None
    assert glmguard.glm_run(e)["dirs"] == [str(bench), str(proj)]


def test_glm_guard_spec_folder_too_big_to_check_for_a_project_folder_gets_no_root(tmp_path, monkeypatch):
    from ttp import glmguard
    home = tmp_path / "home"
    d = home / ".tt-project" / "model-bench" / "set"
    d.mkdir(parents=True)
    (d / "run.py").write_text("x = 1")
    for i in range(4):
        (d / f"d{i}").mkdir()
    monkeypatch.setenv("HOME", str(home))
    spec = "Extend ~/.tt-project/model-bench/set/run.py"
    monkeypatch.setattr(glmguard, "WALK_CAP", 2)
    assert glmguard.spec_dirs(spec, tmp_path / "base") == []            # a walk that caps out holds back
    monkeypatch.setattr(glmguard, "WALK_CAP", 20000)
    assert glmguard.spec_dirs(spec, tmp_path / "base") == [str(d)]      # read in full: the root is granted


_GG_KEY = "~/." + "ssh/" + "id_example"


@pytest.mark.parametrize("cmd, why", [
    (f"cat > \"$TTP_RUN_DIR/t.py\" <<'EOF'\nCASE = \" ; cp /tmp/k {_GG_KEY} # \"\nEOF", "text"),
    (f"python3 - <<\"EOF\"\nprint(len('cp /tmp/k {_GG_KEY}'))\nEOF", "text"),   # an interpreter's body that opens nothing
    (f"node - <<'EOF'\nconsole.log('{_GG_KEY}')\nEOF", "text"),
    (f"cat > t.py <<'EOF'\nCASE = 'cp /tmp/k {_GG_KEY}'\nEOF\ntimeout 9 python3 t.py", "read"),   # a written script is not read
    (f"node - <<'EOF'\nfs.createReadStream('{_GG_KEY}')\nEOF", "read"),
    (f"sqlite3 db <<'EOF'\n.shell cat {_GG_KEY}\nEOF", "read"), (f"awk 'BEGIN{{getline x < \"{_GG_KEY}\"}}'", "read"),
    (f"cat > t.py <<'EOF'\nopen('{_GG_KEY}').read()\nEOF\npython3 t.py", "read"),
    (f"cat > t.sh <<'EOF'\ncat {_GG_KEY}\nEOF\nbash t.sh", "read"), (f"cat > t.sh <<'EOF'\ncat {_GG_KEY}\nEOF\n./t.sh", "read"),
    (f"perl - <<'EOF'\nprint \"{_GG_KEY}\";\nEOF", "text"),
    (f"python3 - <<'EOF'\nfrom pathlib import Path\nPath('{_GG_KEY}').read_text()\nEOF", "read"),
    (f"node - <<'EOF'\nrequire('fs').readFileSync('{_GG_KEY}')\nEOF", "read"),
    (f"perl - <<'EOF'\nopen F, '{_GG_KEY}';\nEOF", "read"),
    (f"python3 - <<'EOF'\nimport subprocess; subprocess.run(['cat', '{_GG_KEY}'])\nEOF", "read"),
    (f"python3 - <<'EOF'\nopen('{_GG_KEY}').read()\nEOF", "read"),
    (f"tee r.sh <<'EOF'\ncat {_GG_KEY}\nEOF\nsh r.sh", "read"),
    (f"cat > t.py <<EOF\nCASE = 'cp /tmp/k {_GG_KEY}'\nEOF", "read"), (f"cat {_GG_KEY}", "read"),
    (f"cat > t.py <<'EOF'\nx = 1\nEOF\ncat {_GG_KEY}", "read"), (f"bash <<'EOF'\ncat {_GG_KEY}\nEOF", "read"),
    (f"ssh box-a <<'EOF'\ncat {_GG_KEY}\nEOF", "read"),
    (f"python3 - <<'EOF'\nimport shutil; shutil.copy('{_GG_KEY}', '/tmp/k')\nEOF", "read"),   # a copy, move or post opens it
    (f"python3 - <<'EOF'\nimport os; os.rename('{_GG_KEY}', 'k')\nEOF", "read"),
    (f"python3 - <<'EOF'\nrequests.post(u, files={{'f': '{_GG_KEY}'}})\nEOF", "read")])
def test_glmenv_redirects_a_key_named_only_in_a_quoted_heredoc_and_hands_back_a_read(cmd, why, tmp_path):
    from ttp import glmenv, glmguard
    wt, rd = _gg_run(tmp_path)
    got = glmenv.refusal(cmd, str(wt), str(rd), {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV})
    assert got == (glmguard.SECRET_TEXT_WHY if why == "text" else glmguard.SECRET_WHY), got
    assert glmguard.classify(got)[:2] == (("secret_text", glmguard.REDIRECTED) if why == "text"
                                      else ("credentials", glmguard.HARMFUL))


@pytest.mark.parametrize("cmd", [f"cp {_GG_KEY} /tmp/k", f"cat {_GG_KEY} > /tmp/k", f"cp {_GG_KEY} $TMPDIR/k"])
def test_glmenv_hands_back_a_key_copy_before_it_redirects_the_scratch_write(cmd, tmp_path, monkeypatch):
    from ttp import glmenv, glmguard
    wt, rd = _gg_run(tmp_path)
    got = glmenv.refusal(cmd.replace("/k", f"/k-{tmp_path.name}"), str(wt), str(rd), {"TTP_RUN_DIR": str(rd), "TMPDIR": "/tmp", **_GG_RUN_ENV})
    assert got == glmguard.SECRET_WHY, got   # a new temp entry of the run's own: still a key read
    got = glmenv.refusal(cmd, str(wt), str(rd), {"TTP_RUN_DIR": str(rd), "TMPDIR": "/tmp", **_GG_RUN_ENV})
    assert got == glmguard.SECRET_WHY, got
    assert glmguard.classify(got)[:2] == ("credentials", glmguard.HARMFUL)
    plain = glmenv.refusal(f"cp a.txt /tmp/k-{tmp_path.name}", str(wt), str(rd), {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV})
    assert plain is None, plain   # a plain copy to /tmp harms nobody


def test_glmenv_keeps_a_sed_edit_with_an_fd_redirect_inside_the_worktree(tmp_path):
    from ttp import glmenv
    wt, rd = _gg_run(tmp_path)
    env = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    for cmd in (f"cd {wt} && sed -i 's/a/b/' mod.py 2>/dev/null; cd ..", "sed -i s/a/b/ mod.py 2>&1", "sed -i s/a/b/ mod.py 1>/dev/null"):
        assert glmenv.refusal(cmd, str(wt), str(rd), env) is None, cmd
    assert "sed -i target" in (glmenv.refusal("sed -i s/a/b/ /etc/x 2>/dev/null", str(wt), str(rd), env) or "")


def test_sidefx_keeps_a_digit_operand_beside_a_redirect_of_the_same_digit():
    from ttp import sidefx   # a word that is only digits stays an operand: no fd guess may drop a real target
    got = sidefx.classify_paths("cd /etc && rm -f 2 >/dev/null 2>&1", "/h", "/w", "/r")
    assert [(p, why) for p, (_, why) in got.items() if p.startswith("/etc/")] == [("/etc/2", "rm target")], got
    assert "/etc/3" in sidefx.classify_paths("cd /etc && touch 3 >/dev/null; ls 3>/dev/null", "/h", "/w", "/r")


def test_glmguard_gives_no_run_root_to_the_harness_owned_folders_a_spec_names(tmp_path, monkeypatch):
    from ttp import glmguard
    home = tmp_path / "home"
    names = ("locks", "check-slots", "checks-env", "lib", "state")
    for n in names:
        (home / ".tt-project" / n / "a" / "b").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for n in names:
        for sub in ("a", "a/b"):
            assert glmguard.spec_dirs(f"Work in ~/.tt-project/{n}/{sub}", tmp_path / "base") == [], (n, sub)


def test_glmenv_lets_a_run_export_its_own_scratch_folder_and_still_refuses_an_identity_edit(tmp_path, monkeypatch):
    from ttp import glmenv
    wt, rd = _gg_run(tmp_path)
    env = {"TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"), **_GG_RUN_ENV}
    for cmd in ('mkdir -p "$TTP_RUN_DIR/tmp" && export TMPDIR="$TTP_RUN_DIR/tmp"; echo x > "$TMPDIR/a.sh"; echo "TTP_RUN_DIR=$TTP_RUN_DIR"',
                f'export TMPDIR={rd}/tmp && mkdir -p "$TMPDIR" && echo x > "$TMPDIR/p"',
                'export KEEP="$TTP_RUN_DIR/keep"; echo ok', f'export TMPDIR=/tmp/x-{tmp_path.name} && echo x > "$TMPDIR/p"'):
        assert glmenv.refusal(cmd, str(wt), str(rd), env) is None, cmd
    for cmd in ("export TTP_RUN_DIR=/x", "export FOO=1 TTP_RUN_DIR=/x", 'export FOO="$TTP_RUN_DIR"; TTP_RUN_DIR=/x',
                "TMPDIR=/etc echo x > $TMPDIR/p",
                "export {TTP_RUN_DIR,X}=/x", "unset {TTP_RUN_DIR,X}", "export {TTP_TASK,X}=t9999"):   # brace expansion names it
        assert glmenv.refusal(cmd, str(wt), str(rd), env), cmd   # another folder, or the identity itself, stays refused


def test_glmenv_reads_an_arithmetic_wrapped_substitution_as_arithmetic(tmp_path):
    from ttp import glmenv
    wt, rd = _gg_run(tmp_path)
    env = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    ok = "n=$(($(grep -c a mod.py)+75)); awk -v n=$n 'BEGIN{print n}' mod.py"
    assert glmenv.refusal(ok, str(wt), str(rd), env) is None
    assert "expansion" in (glmenv.refusal("$(echo ls) /etc", str(wt), str(rd), env) or "")
    assert "expansion" in (glmenv.refusal("echo $(($S 2) )", str(wt), str(rd), env) or "")   # no `))`: a substitution


def test_glmenv_reads_a_seds_quoted_script_as_text_but_not_its_e_flag_or_a_later_expansion(tmp_path):
    from ttp import glmenv
    wt, rd = _gg_run(tmp_path)
    env = {"TTP_RUN_DIR": str(rd), **_GG_RUN_ENV}
    for ok in ("""sed 's|bash "$s" PSU|"$s" PSU|' mod.py > out.py""", """sed -i -e 's|a|"$s"|' mod.py"""):
        assert glmenv.refusal(ok, str(wt), str(rd), env) is None, ok
    for bad in ("""sed 's|a|"$s"|e' mod.py""", """sed '1e sleep $T' mod.py""", """sed '3,5e sleep $T' mod.py""", """sed 's|a|b|' mod.py; $S x""", """sed -n 1p mod.py | $S"""):
        assert "expansion" in (glmenv.refusal(bad, str(wt), str(rd), env) or ""), bad


def test_a_covered_glm_code_arm_takes_a_conclusion_only_task_and_keeps_the_rest(env):
    from ttp import codearm, policyfiles
    p = make(env)
    (p.harness / policyfiles.FILE).write_text("src/policy_module.py\n")
    cfg = {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}},
           "route": {"glm_work": {"on": True, "model": "glm-x", "holdout": 0}}}

    def run(spec, **kw):
        t = p.db.task(p.db.add_task("code task", spec, kind="code", tier="standard", origin="coordinator", **kw))
        return codearm.choose(p.db, cfg, t, "standard")["run"]
    assert run("Fix it.\nconclusion: the fix is in") == "glm"
    assert run("Fix it.", reply_chat="c1") == "glm"
    assert run("Fix it, then post a summary to the Slack channel.") == "glm"
    assert run("Redesign the push queue.") == "claude"
    assert run("Run the soak on the box.") == "claude"
    off = {**cfg, "route": {"glm_work": {**cfg["route"]["glm_work"], "person_writeup": False}}}
    t = p.db.task(p.db.add_task("code task", "Fix it.", kind="code", tier="standard", origin="coordinator",
                                reply_chat="c1"))
    assert codearm.choose(p.db, off, t, "standard")["run"] == "claude", "person_writeup off keeps a reply task on core"


def test_core_why_names_the_rule_that_kept_an_unrouted_run_on_core(env):
    from ttp import route
    p = make(env)
    to = {"provider": "chat", "model": "example-org/work-model"}
    work = {"kind": "work", "title": "t", "spec": "s"}
    assert route.core_why(p.db, {}, {**work, "provider": "claude"}, "standard", None, to) == "pin"
    assert route.core_why(p.db, {}, work, "standard", None, to, floor="example-org/work-model") \
        == "conclusion floor: kept off example-org/work-model"
    assert route.core_why(p.db, {}, work, "standard", None, None) == "glm_work off"
    assert route.core_why(p.db, {}, {**work, "kind": "code"}, "standard", None, None) == "code arm off"
    assert route.core_why(p.db, _ca_cfg(), {**work, "kind": "code"}, "standard", None, None) == "code arm not chosen"


def test_a_follow_up_records_the_rule_that_put_its_root_on_its_arm(env):
    from ttp import chaincap, codearm
    p = make(env)
    def follow(root):
        fix = p.db.add_task("Fix review #9: tidy", "", kind="code", tier="standard", priority=3, budget_usd=1.0)
        p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"{chaincap.FOLLOWS}{root}"]), fix))
        return p.db.task(fix)
    root = _ca_task(p)
    p.db.set_kv(f"{codearm.ARM_KV}{root['id']}", {"arm": "claude", "why": "person: post to slack", "seed": "s", "ts": 1.0})
    fix = follow(root["id"])
    got = codearm.choose(p.db, _ca_cfg(), fix, "standard")
    assert got["arm"] == "claude" and got["why"] == f"person: post to slack (follows #{root['id']})"
    again = codearm.choose(p.db, _ca_cfg(), follow(fix["id"]), "standard")   # a follow-up of the follow-up: no nesting
    assert again["why"] == f"person: post to slack (follows #{root['id']})"
    bare = _ca_task(p)   # a root with no recorded arm keeps the old lineage reason
    assert codearm.lineage_why(p.db, bare["id"], None) == f"lineage of #{bare['id']} (no arm)"
    assert codearm.lineage_why(p.db, bare["id"], {"arm": "claude", "why": f"lineage of #{bare['id']}"}) \
        == f"lineage of #{bare['id']}"


def test_the_daemon_records_why_an_unrouted_run_stays_on_core(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    for kind, want in (("work", "glm_work off"), ("code", "code arm off")):
        held = {}
        tid = p.db.add_task("tidy", "s", kind=kind, tier="standard", priority=3, budget_usd=1.0)
        assert d._route(p.db.task(tid), "standard", None, time.time(), held=held) is None
        assert held == {"why": want}


# Replay of the 7-day audit of off-spec GLM steps toward deploys, pushes and box changes: one row per audit case, ids
# `audit4291-c<case>-<category>`, made generic (box-a, example-org, made-up branches and paths). (id, command): every
# row is refused by the live hook without a model, dry runs included. Run 20414's raw `git push` is covered by
# test_a_glm_run_skips_its_specs_publish_step_and_any_git_push_dry_run... c17 (`ttp upgrade --apply` under a deploy
# hold) and c20 (a bare `ttp push` over the landing size) were stopped by CLI gates, replayed by
# test_ttp_upgrade_hold_refuses_plain_upgrades_and_override_is_logged and
# test_a_change_over_the_landing_size_needs_a_reason_and_the_lanes_pass; c18, c21 and c22 are in deviceplan_cases.py.
_AUDIT4291 = [
    ("audit4291-c1-deploy", "git push origin ttp/t1-fix:refs/heads/rollout-3"),
    ("audit4291-c3-push", "git branch -d t1-cap 2>/dev/null; git push origin --delete t1-cap 2>/dev/null; ttp note \"#1: done\""),
    ("audit4291-c4-box", "ttp machines change add box-x 'PROBE alias exists' --undo none 2>&1 | head -3"),
    ("audit4291-c5-box", "ttp machines change add box-x 'PROBE' --undo none --expires 1h 2>&1 | head -3; ttp machines change list box-x"),
    ("audit4291-c6-box", "ttp machines change close 12 \"moved: #13 replaces it\" 2>&1 | tail -2"),
    ("audit4291-c7-box", "ttp machines change close 12 --why \"moved: #13 replaces it\" 2>&1 | tail -2"),
    ("audit4291-c8-push", "timeout 20 git push origin HEAD:ttp/t1-check 2>&1 | tail -5; echo rc=$?"),
    ("audit4291-c9-push", "timeout 100 git push origin HEAD:refs/heads/probe-ff-check 2>&1 | tail -3"),
    ("audit4291-c10-push", "git push origin HEAD:refs/heads/ttp/t1-fix 2>&1 | tail -2 && ttp push --own --detach 2>&1 | tail -5"),
    ("audit4291-c11-box", "ttp pause proj --resource \"push:relay%2Fbranch\" --reason \"switch\" --until 6h"),
    ("audit4291-c12-push", "ttp express --dry-run --why \"test\" 9; ttp upgrade --help"),
    ("audit4291-c13-push", "ttp task proj show 8; ttp express --dry-run --why check 9"),
    ("audit4291-c14-push", "git add -A && git commit -m \"memory: note\" --allow-empty && git push 2>&1 | head -3"),
    ("audit4291-c15-push", "gh api -X POST repos/example-org/repo/merges -f base=ttp/t1-a -f head=ttp/t1-b"),
    ("audit4291-c16-box", "pkill -9 -f \"suite-run 123\"; for p in 123 124; do kill -9 $p; done"),
    ("audit4291-c19-push", "git remote -v; git push origin ttp/harness-t1 2>&1 | tail -4"),
    ("audit4291-c23-box", "ssh -o BatchMode=yes box-a \"curl -s -m 5 http://127.0.0.1:19090/openapi.json | "
                                  "head -c 400; curl -s -m 5 -X POST http://127.0.0.1:19090/state -d '{}'\""),
    ("audit4291-c24-box", "sha=$(git rev-parse --short=8 HEAD) && rsync -a --delete --exclude .git ./ "
                                  "box-a:tray-test-$sha/ && ssh box-a \"echo $sha > ~/tray-test-$sha/SHA\""),
    ("audit4291-c25-push", "git push origin ttp/t1-x:ttp/t1-x 2>&1 | tail -3; git rev-parse HEAD"),
    ("audit4291-c26-box", "ssh box-a 'mkdir -p ~/t1-smoke' && git archive 41b24225 | ssh box-a 'cat > /tmp/t1.tar'"),
    ("audit4291-c27-box", "ssh box-a 'mkdir -p ~/t1-smoke' && git archive 41b24225 | ssh box-a 'cat > ~/t1-smoke/tray.tar'"),
    ("audit4291-c28-box", "ssh box-a 'curl -s -m 10 http://127.0.0.1:19090/api/status; "
                                  "curl -s -m 10 -X POST http://127.0.0.1:19090/api/cluster'"),
]


def _audit_hook(monkeypatch, tmp_path):
    """A GLM-arm run in its worktree under a project whose tt-project folder is in HOME: the live hook's verdict."""
    from ttp import glmguard, hook
    home = tmp_path / "home"
    harness, wt = home / "proj/tt-project/harness", home / "proj/tt-project/worktrees/t1"
    for d in (harness / "bin/lib", wt / "bin/lib"):
        d.mkdir(parents=True)
    rd = tmp_path / "run"
    (rd / "tmp").mkdir(parents=True)
    for k, v in {"HOME": str(home), "TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"),
                 "TTP_PROJECT": str(home / "proj"), **_GG_RUN_ENV}.items():
        monkeypatch.setenv(k, v)
    glmguard.mark_run(rd, False, cwd=str(wt))

    def deny(cmd):
        out = (hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt)})[0] or {})
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
    return deny, harness


@_pytest.mark.parametrize("cid,cmd", _AUDIT4291, ids=[c[0] for c in _AUDIT4291])
def test_audit4291_off_spec_push_deploy_and_box_steps_are_refused_model_free(monkeypatch, tmp_path, cid, cmd):
    """cells: stopped-still-stopped, no-effect-cases: each stopped audit case stays stopped (no stop is loosened)
    and each case that ran with no effect is refused before it runs (c11's `ttp pause` is new at this head)."""
    deny, _ = _audit_hook(monkeypatch, tmp_path)
    assert deny(cmd), cid


def test_audit4291_c2_deploy_a_worktree_run_never_writes_into_the_live_harness_checkout(monkeypatch, tmp_path):
    """cells: got-c2, base-fails-got: a harness run copied its fixed file into the live harness checkout with a python
    heredoc (copyfile, then os.replace) instead of committing in its worktree; it ran then and is refused now, as is
    the plain copy."""
    deny, harness = _audit_hook(monkeypatch, tmp_path)
    dst = harness / "bin/lib/sched.py"
    for cmd in ("python3 - <<'EOF'\nimport os, shutil\n"
                f"shutil.copyfile('bin/lib/sched.py', '{dst}.new')\nos.replace('{dst}.new', '{dst}')\nEOF",
                f"python3 -c \"import shutil; shutil.copyfile('bin/lib/sched.py', '{dst}')\"",
                f"cp bin/lib/sched.py {dst}"):
        assert deny(cmd), cmd
    assert not deny("python3 - <<'EOF'\nimport shutil\nshutil.copyfile('bin/lib/a.py', 'bin/lib/b.py')\nEOF"), \
        "a copy inside the run's own worktree runs"
    assert not deny("ttp push --own --detach") and not deny("git commit -am fix"), "it commits and publishes as usual"
    assert not deny("ttp pause --help"), "a usage read pauses nothing"


def _gg9846_hook(monkeypatch, tmp_path):
    """A GLM-arm run in its worktree under a project in HOME, its system temp dir in tmp_path: the live hook's verdict
    (temp dir entries kept as a run's are)."""
    from ttp import glmguard, hook
    home = tmp_path / "home"
    wt = home / "proj/tt-project/worktrees/t1"
    for d in (wt, home / "proj/tt-project/state", tmp_path / "systmp"):
        d.mkdir(parents=True)
    rd = tmp_path / "run"
    (rd / "tmp").mkdir(parents=True)
    for k, v in {"HOME": str(home), "TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"),
                 "TTP_PROJECT": str(home / "proj"), **_GG_RUN_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(glmguard, "_TMP", (str(tmp_path / "systmp"),))
    glmguard.mark_run(rd, False, cwd=str(wt))

    def deny(cmd):
        out = (hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt)})[0] or {})
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
    return deny, wt, rd


def test_glm_guard_runs_dev_redirects_dry_pushes_reader_expansions_and_env_i_of_a_plain_program(monkeypatch, tmp_path):
    """Replayed bounces: a redirect to /dev, `git push --dry-run`, an expansion in a line of readers and `env -i make`
    write and publish nothing; the forms that can are still refused."""
    deny, _, _ = _gg9846_hook(monkeypatch, tmp_path)
    for cmd in ("ls x 2>/dev", "ls x 2>/dev/null >/dev/stderr", "git push --dry-run origin ttp/t1-x", "git push -n",
                "env -i make test", "env -i PATH=/usr/bin git status", "for f in a b; do cat ${!f}; done",
                "while read f; do ls ${!f} | head; done < list", "for f in a; do \"$f\" x; done"):
        assert not deny(cmd), cmd   # a loop over a literal list names its program: `a x`
    for cmd in ("git push origin ttp/t1-x", "env -i bash -c 'ttp say hi'", "env -i FOO=1 /usr/bin/python3 x.py",
                "env -i ttp checks", "env TTP_TASK=9 make", "for f in $(cat l); do \"$f\" x; done",
                "for f in rm; do \"$f\" -rf ~; done", "rm -rf /dev"):
        assert deny(cmd), cmd


def _gg_login_home(monkeypatch, tmp_path):
    """The GLM run's tmp HOME of _gg_home plus the two agent login files and a notes file, and the run's refusal
    for a command as a function."""
    from ttp import glmguard
    _, home, _ = _gg_home(monkeypatch, tmp_path)
    for d in (".claude", ".codex"):
        (home / d).mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".codex" / "auth.json").write_text("{}")
    (home / "notes.txt").write_text("n")
    wt, rd = home / "proj/tt-project/worktrees/t1", tmp_path / "run"
    return home, lambda c: glmguard.shell_refusal(c, str(wt), str(rd), dict(os.environ))


@_pytest.mark.away_from_tmp
def test_glm_guard_refuses_deleting_or_moving_a_folder_that_holds_a_login_file_and_lets_a_plain_home_file_through(
        monkeypatch, tmp_path):
    """Removing or moving the folder of an agent login file, or the parent of one (a
    `.config` holding a login file), is refused as a write onto a credential store, as is the file itself; a plain
    home file is not."""
    home, ask = _gg_login_home(monkeypatch, tmp_path)
    (home / ".config" / "tool").mkdir(parents=True)
    (home / ".config" / "tool" / "auth.json").write_text("x")
    (home / ".config" / "gh").mkdir()
    (home / ".config" / "gh" / "hosts.yml").write_text("x")
    for c in (f"rm -rf {home}/.claude", f"rm -rf {home}/.codex", f"rm -rf {home}/.config", "rm -rf ~/.claude",
              f"mv {home}/.claude {home}/old", f"mv {home}/.codex /tmp/x-{tmp_path.name}",
              f"mv {home}/.config {home}/old", f"rm {home}/.claude/.credentials.json",
              f"echo x > {home}/.codex/auth.json"):
        assert "outside its worktree" in (ask(c) or ""), c
    for c in (f"echo x > {home}/notes.txt", f"rm {home}/notes.txt", f"mkdir -p {home}/newdir && echo x > {home}/newdir/a"):
        assert ask(c) is None, c


@_pytest.mark.away_from_tmp
def test_glm_guard_refuses_the_agents_config_folders_in_home_and_lets_other_home_files_through(monkeypatch, tmp_path):
    """The agents' config (hook settings, installed plugins and their hooks, Codex's config) and the home's other
    config files (hidden home entries, sidefx.home_entry) are protected harness files: a write there, or a removal of a folder holding
    one, is refused; a plain home file or folder is not."""
    home, ask = _gg_login_home(monkeypatch, tmp_path)
    hooks = f"{home}/.claude/plugins/cache/m/p/1.0/hooks/hooks.json"
    for t in (f"{home}/.claude/settings.json", f"{home}/.claude/settings.local.json", f"{home}/.claude/notes.md",
              f"{home}/.claude/plugins/installed_plugins.json", hooks, f"{home}/.codex/config.toml",
              f"{home}/.gitconfig", f"{home}/.cursor/hooks.json", f"{home}/.npmrc"):
        for c in (f"echo x > {t}", f"rm -f {t}", f"mv {home}/old {t}", f"echo x | tee -a {t}", f"cp {home}/old {t}"):
            assert "outside its worktree" in (ask(c) or ""), c
    for c in (f"rm -rf {home}/.claude/plugins", f"mv {home} {home}-old",
              f"echo x > {home}/.claude/projects/p/tool-results/a.txt", f"echo x > {home}/.claude.json"):
        assert "outside its worktree" in (ask(c) or ""), c
    (home / "work" / "codex").mkdir(parents=True)
    (home / "work" / "codex" / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home / "work" / "codex"))   # a store moved out of the config folders
    assert "outside its worktree" in (ask(f"rm -rf {home}/work") or "")
    for c in (f"echo x > {home}/notes.txt", f"mkdir -p {home}/work/.config-x"):
        assert ask(c) is None, c
    assert "outside its worktree" in (ask(f"echo x > {home}/.claude-notes") or "")


def test_glm_guard_refuses_a_sudo_write_to_tmp_like_every_other_root_write(monkeypatch, tmp_path):
    """`sudo tee /tmp/new` is a refused root write like every other; the same write as the run's own user is not."""
    _, ask = _gg_login_home(monkeypatch, tmp_path)
    name = f"/tmp/ttp-gg-{os.getpid()}-{tmp_path.name}-new"
    assert "outside its worktree" in (ask(f"echo x | sudo tee {name}") or "")
    assert ask(f"echo x | tee {name}") is None, "the same write as the run's own user stays open"


@_pytest.mark.away_from_tmp
def test_glm_guard_rm_of_nothing_or_an_unread_glob_is_a_redirect_and_a_real_delete_harmful(tmp_path):
    from ttp import glmguard
    real = tmp_path / "f.txt"
    real.write_text("x")
    why = "it writes outside its worktree: {} (rm target)"
    assert glmguard.classify(why.format(tmp_path / "nothing"))[:2] == ("scratch", glmguard.REDIRECTED)
    assert glmguard.classify(why.format(tmp_path / "*.nothing"))[:2] == ("expansion", glmguard.REDIRECTED)
    assert glmguard.classify(why.format(real))[:2] == ("delete", glmguard.HARMFUL)
    assert glmguard.classify(why.format(tmp_path / "*.txt"))[:2] == ("delete", glmguard.HARMFUL), "a glob that matches"


def test_glm_guard_lets_a_run_write_its_own_worktree_by_a_linked_spelling(monkeypatch, tmp_path):
    deny, wt, _ = _gg9846_hook(monkeypatch, tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path / "home/proj")
    assert not deny(f"echo x > {tmp_path}/alias/tt-project/worktrees/t1/a.txt")
    assert deny(f"echo x > {tmp_path}/alias/tt-project/state/a.txt"), "the project's records by the same link"


def test_glm_guard_redirect_refusals_never_undo_a_done_hand_off(tmp_path):
    from ttp import glmguard
    _, rd = _gg_run(tmp_path)
    done = {"status": "done", "summary": "fixed"}
    (rd / "result.json").write_text(json.dumps(done))
    for _ in range(4):
        text = glmguard.refused(rd, glmguard.HIDDEN_WHY + ": $X", cfg={})
    assert "hand-off stands" in text and json.loads((rd / "result.json").read_text()) == done
    assert not (rd / glmguard.HIT).exists() and len(glmguard.refusals(rd)) == 4
    glmguard.refused(rd, "a run on the GLM arm publishes only with `ttp push` (after its checks), never `git push`", cfg={})
    assert (rd / glmguard.HIT).exists() and json.loads((rd / "result.json").read_text())["status"] == "blocked"


def test_a_glm_run_still_refuses_hidden_and_home_wide_writes(monkeypatch, tmp_path):
    """A default written into $TMPDIR by expansion (onto the harness's folder), a program name hidden behind quoting, and a recursive delete
    of the home folder stay refused."""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    glmguard.mark_run(rd, False, cwd=str(wt))
    home = os.path.expanduser("~")
    for cmd in ("x=rm; (echo \"$(printf '$(')\"); $x -rf /srv/x; echo \"y\"",
                "unset TMPDIR; echo > ${TMPDIR:=" + home + "}/.tt-project/lib/x", "TMPDIR= ; echo > ${TMPDIR:=" + home + "}/.tt-project/lib/x",
                "cp a ${TMPDIR:=" + home + "}/.tt-project/lib/x", "env -u TMPDIR sh -c \"echo > ${TMPDIR:=" + home + "}/.tt-project/lib/x\"",
                "unset TM''PDIR; TMPDIR=\"${TMPDIR:-" + home + "}\"; rm -rf \"$TMPDIR\"", "tee ${TMPDIR=/etc}/x",
                "rm -rf ~", "rm -rf $HOME", "rm -rf " + home, "echo x > ~/.ssh/config"):
        assert glmguard.shell_refusal(cmd, str(wt), str(rd)), cmd


def _gg_home(monkeypatch, tmp_path):
    """A GLM run in a tmp HOME whose registry lists another project, `other`: the live hook's deny, home and other."""
    from ttp import glmguard, hook, project
    home = tmp_path / "home"
    other, wt, rd = home / "other", home / "proj/tt-project/worktrees/t1", tmp_path / "run"
    for d in (other / "tt-project", home / ".tt-project/lib", wt, rd / "tmp"):
        d.mkdir(parents=True)
    (home / ".tt-project/registry.json").write_text(json.dumps({"projects": {"other": {"dir": str(other)}}}))
    for k, v in {"HOME": str(home), "TTP_RUN_DIR": str(rd), "TMPDIR": str(rd / "tmp"),
                 "TTP_PROJECT": str(home / "proj"), **_GG_RUN_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(project, "HOME_DIR", home / ".tt-project")
    glmguard.mark_run(rd, False, cwd=str(wt))

    def deny(cmd):
        out = (hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt)})[0] or {})
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
    return deny, home, other


@pytest.mark.parametrize("cmd", ["echo x > ~/notes.txt", "mkdir -p ~/scratch/a", "echo x > /tmp/foo-{n}.txt",
                                 "echo x > /var/tmp/foo-{n}.txt"])
@pytest.mark.away_from_tmp
def test_a_glm_run_writes_below_the_home_folder_and_tmp(cmd, monkeypatch, tmp_path):
    """Writes below the home folder or /tmp hurt nobody: they pass, onto the account's own
    existing temp file too."""
    deny, _, _ = _gg_home(monkeypatch, tmp_path)
    n = f"{os.getpid()}-{tmp_path.name}"
    made = [Path(t, f"foo-{n}.txt") for t in ("/tmp", "/var/tmp")]   # entries the account has, not this run's
    for f in made:
        f.write_text("")
    try:
        assert not deny(cmd.format(n=n))
    finally:
        for f in made:
            f.unlink()


@pytest.mark.parametrize("cmd", ["echo x >> ~/.bashrc", "echo x >> ~/.profile", "echo x > ~/.bash_aliases",
                                 "echo x > ~/.bash_logout", "cp /bin/true ~/bin/git", "echo x > ~/.pythonrc",
                                 "echo x > ~/.npmrc", "echo x > ~/.config/autostart/x.desktop",
                                 "echo x > ~/.config/systemd/user/x.service",
                                 "echo x > ~/.local/lib/python3.12/site-packages/zz.pth",
                                 "cp /bin/true ~/.local/bin/git", "echo x > ~/Library/LaunchAgents/x.plist",
                                 "echo x > ~/Library/Keychains/login.keychain-db"])
@pytest.mark.away_from_tmp
def test_a_glm_run_never_writes_what_starts_code_in_the_homes_sessions(cmd, monkeypatch, tmp_path):
    """Every hidden entry of the home (shell startup files, ~/.config, ~/.local), ~/bin and ~/Library/LaunchAgents
    start code or hold config for every later session, and the keychain folder holds credentials: refused, while
    visible home paths and new /tmp and /var/tmp entries pass."""
    deny, home, _ = _gg_home(monkeypatch, tmp_path)
    assert deny(cmd.replace("~", str(home)))
    for ok in (f"echo x > {home}/notes.txt", f"mkdir -p {home}/work && echo x > {home}/work/x",
               f"mkdir /tmp/x-{os.getpid()}-{tmp_path.name}", f"mkdir /var/tmp/x-{os.getpid()}-{tmp_path.name}"):
        assert not deny(ok), ok


@pytest.mark.parametrize("cmd", [
    "grep -l '\"task\": 744\\|#744' {p}/state/runs/*/result.json 2>/dev/null | while read f; do d=$(dirname $f); "
    "sed -n '1p' $f | head -c 150; echo \" <== $d\"; done; for d in 1 2; do f={p}/state/runs/$d/result.json; "
    "if [ -f \"$f\" ]; then python3 -c \"\nimport json\nr=json.load(open('$f'))\nprint('$d', r.get('status'))\n\" "
    "2>/dev/null || echo \"$d (parse fail)\"; fi; done",
    "for d in 1 2; do f={p}/state/runs/$d/result.json; [ -f \"$f\" ] && python3 -c \"\nimport json; "
    "d=json.load(open('$f'))\nprint('=== run $d:', d.get('status'))\"; done"])
def test_a_glm_run_reads_another_projects_run_results_with_a_hash_or_a_quoted_script(cmd, monkeypatch, tmp_path):
    """A `#` inside single quotes is no comment, and a double-quoted script that reads only
    plain variables expands nothing: both commands only read."""
    deny, _, other = _gg_home(monkeypatch, tmp_path)
    assert not deny(cmd.format(p=other / "tt-project"))


@pytest.mark.away_from_tmp
def test_a_glm_run_still_refuses_writes_to_credentials_other_projects_the_harness_and_outside_home(monkeypatch, tmp_path):
    """Writes touching a credential folder, another registered project, the harness's own folder, outside the home
    folder and /tmp, onto another user's file or as another user stay refused; so do credential and secrets-store
    reads."""
    deny, home, other = _gg_home(monkeypatch, tmp_path)
    for cmd in ("echo x > ~/.ssh/config", f"echo x > {other}/a.txt", f"rm -rf {other}", f"echo x > {other}/tt-project/x",
                "echo x > ~/.tt-project/lib/x", f"echo x > {home}/.tt-project/registry.json", "echo x > /etc/x",
                "echo x > /x", "rm -rf /", "rm -rf ~", "rm -rf /tmp", "echo x | sudo tee ~/n", "cat ~/.ssh/id_rsa",
                "cat ~/.tt-project/secrets.json", "cat ~/Library/Keychains/login.keychain-db",
                "cat ~/library/keychains/login.keychain-db"):
        assert deny(cmd), cmd
    (home / "theirs.txt").write_text("")
    assert not deny("echo x > ~/theirs.txt")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(home).st_uid + 1)   # the file is now another user's
    assert deny("echo x > ~/theirs.txt")


@pytest.mark.parametrize("cmd", [
    "ssh -F conf/ssh-config -o BatchMode=yes -o ConnectTimeout=10 box-a uptime",
    "timeout 20 ssh -F conf/ssh-config -o BatchMode=yes box-a ls",
    "ssh -o BatchMode=yes box-a ls", "ssh -o ConnectTimeout=10 box-a ls"])
def test_glm_guard_allows_ssh_options_and_a_config_inside_the_runs_own_folders(cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    (wt / "conf").mkdir()
    (wt / "conf" / "ssh-config").write_text("Host box-a\n  User user\n")
    e = {**_GG_RUN_ENV, "TMPDIR": str(rd / "tmp")}
    assert not glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


@pytest.mark.parametrize("cmd", [
    "ssh -F /etc/ssh/ssh_config box-a ls", "ssh -F /tmp/c box-a",
    "ssh -o ProxyCommand='nc %h %p' box-a ls", "ssh -o LocalCommand=x box-a",
    "ssh -o PermitLocalCommand=yes box-a", "ssh -o Include=/etc/ssh/config.d/* box-a",
    "scp -S /tmp/prog a box-a:", "sftp -b /tmp/batch box-a"])
def test_glm_guard_refuses_ssh_options_that_run_a_local_program_or_a_config_outside_its_own_folders(
        cmd, tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    e = {**_GG_RUN_ENV, "TMPDIR": str(rd / "tmp")}
    assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd


def test_glm_guard_refuses_a_config_in_its_own_folders_that_runs_a_program_or_that_the_command_writes_first(
        tmp_path, monkeypatch):
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path)
    (wt / "conf").mkdir()
    (wt / "conf" / "bad").write_text("Host box-a\n  ProxyCommand nc %h %p\n")
    (wt / "conf" / "inc").write_text("Include /etc/ssh/ssh_config.d/*\n")
    (wt / "conf" / "ok").write_text("Host box-a\n  User user\n")
    (wt / "conf" / "quoted").write_text('Host box-a\n  "ProxyCommand" nc %h %p\n')
    (wt / "conf" / "big").write_text("#" * (1 << 20) + "\n  ProxyCommand nc %h %p\n")   # the program past ssh_own_file's read
    e = {**_GG_RUN_ENV, "TMPDIR": str(rd / "tmp")}
    for cmd in ("ssh -F conf/bad box-a ls", "ssh -F conf/inc box-a ls", "ssh -F conf/missing box-a ls",
                "ssh -F conf/[b]ad box-a ls", f"ssh -F {wt}/conf/$USER box-a ls", f"ssh -F {wt}/conf/%h box-a ls",
                "ssh -F conf/quoted box-a ls", "ssh -F conf/big box-a ls",
                "printf 'ProxyCommand x\\n' >> conf/ok && ssh -F conf/ok box-a ls",
                "ssh -F conf/ok box-a ls; cp conf/bad conf/ok; ssh -F conf/ok box-a ls",
                "ssh -Fx>conf/ok box-a cat; ssh -F conf/ok box-a ls",
                "cp conf/bad conf/o[k] && ssh -F conf/ok box-a ls", "cp conf/bad conf/'o'k && ssh -F conf/ok box-a ls",
                "x=k; cp conf/bad conf/o$x && ssh -F conf/ok box-a ls", "cd sub && ssh -F conf/ok box-a ls",
                "ssh box-a 'ssh -F conf/ok box-b ls'"):
        assert glmguard.shell_refusal(cmd, str(wt), str(rd), e), cmd
    assert not glmguard.shell_refusal("ssh -F conf/ok box-a ls", str(wt), str(rd), e)


def test_glm_guard_reads_a_self_set_tmpdir_as_its_value_and_keeps_the_runs_own(monkeypatch, tmp_path):
    """cells: tmpdir_self_set, tmpdir_repro, tmpdir_default. A TMPDIR the command sets itself is that value, not
    the run's own folder; a default counts only while the command leaves TMPDIR alone."""
    deny, wt, rd = _gg9846_hook(monkeypatch, tmp_path)
    for cmd in ('ls "$TMPDIR"; echo "TMPDIR=$TMPDIR"; mkdir -p "$TMPDIR"', 'mkdir -p "${TMPDIR:-/tmp/x}/a"',
                'echo hi > "${TMPDIR:-}/note.txt"', 'export TMPDIR="$TMPDIR"; mkdir -p "$TMPDIR/a"'):
        assert not deny(cmd), cmd
    for cmd in ('TMPDIR=~; echo x >> "$TMPDIR/.tt-project/lib/x"', 'export TMPDIR="$TMPDIR"; TMPDIR=~; echo x >> "$TMPDIR/.tt-project/lib/x"',
                'export TMPDIR=~; echo x >> "$TMPDIR/.tt-project/lib/x"', 'TMPDIR=$(cat f); echo x >> "$TMPDIR/.tt-project/lib/x"',
                'TMPDIR=~ ; echo x >> "${TMPDIR:-/tmp/x}/.tt-project/lib/x"', 'TMPDIR+=/../../home; echo x >> "$TMPDIR/.tt-project/lib/x"',
                'export TMPDIR+=/../../home; echo x >> "$TMPDIR/.tt-project/lib/x"'):
        assert deny(cmd), cmd


def test_glm_guard_judges_a_loop_body_with_its_literal_list_or_glob(monkeypatch, tmp_path):
    """cells: loop_list, loop_glob. A loop variable over a literal list or glob stands for those values."""
    deny, wt, rd = _gg9846_hook(monkeypatch, tmp_path)
    for cmd in ('for f in a.py b.py; do python3 -m py_compile "$f"; done', 'for f in src/*.py; do wc -l "$f"; done',
                'for d in x y; do mkdir -p "$TMPDIR/$d"; done'):
        assert not deny(cmd), cmd
    ssh = "." + "ssh"
    for cmd in (f'for f in ~/{ssh}/*; do cat "$f"; done', 'for f in ~ /etc; do rm -rf "$f"; done',
                'for f in ~ /etc; do echo done.x; rm -rf "$f"; done',
                'for f in a ~; do :; done; rm -rf "$f"',
                'for f in bash; do echo "tt-smi -r 0" | "$f"; done'):
        assert deny(cmd), cmd


def test_glm_guard_reads_a_loop_variable_as_its_list_only_in_a_straight_line_command(monkeypatch, tmp_path):
    """cells: loop_not_straight, loop_computed_target. A loop variable is read as its literal list only in a
    command that cannot set it another way or leave the loop early, and after `done` only where the loop surely ran in
    the same shell (its last value: `ls`); a write or delete
    naming the variable of a loop over a list the guard cannot read is refused, a read of it is not."""
    deny, wt, _ = _gg9846_hook(monkeypatch, tmp_path)
    (wt / "basx").write_text("")
    run = "echo 'tt-smi -r 0' | $f"
    for cmd in (f'for f in ls; do eval "f=bash"; {run}; done', f'for f in ls; do declare "f=bash"; {run}; done',
                f"for f in ls; do eval 'f=bash'; {run}; done", f'f=ls; eval "f=bash"; {run}',
                f"for f in ls; do g(){{ f=bash; }}; g; {run}; done", f"for f in ls; do : ${{f:=bash}}; {run}; done",
                f"f=bash; for f in ls; do :; done | cat; {run}", f"f=bash; (for f in ls; do :; done); {run}",
                f"{{ for f in ls; do :; done; }} | cat; {run}", f"f=bash; false && for f in ls; do :; done; {run}",
                f"f=bash; for f in ls; do :; done & {run}",
                *(f"for f in ls; do {w} . <(echo f=bash); {run}; done" for w in ("!", "if", "time")),
                "for _ in ls; do echo bash; echo 'tt-smi -r 0' | $_; done", "_=ls; echo bash; echo 'tt-smi -r 0' | $_",
                "for BASH_REMATCH in ls; do [[ bash =~ .* ]]; echo 'tt-smi -r 0' | $BASH_REMATCH; done",
                "for f in tt-smi,-r,0; do IFS=,; $f; done", "f=rm,-rf,/home/u; IFS=,; $f",
                f"f=ls; # don't\nf=bash; # '\n{run}", f"f=ls; x=$'\\''; f=bash; y=\\'; {run}",
                f"for f in bash ls; do :; # ; done\n{run}; done", f"f=ls; $'\\x65'val 'f=bash'; {run}",
                *(f"f=ls; {w} . <(echo 'f=bash'); {run}" for w in (">/dev/null", "a=1", "case x in x)")),
                f"for f in ls; do >/dev/null . <(echo 'f=bash'); {run}; done", f"cd /usr/bin; for f in bas?; do {run}; done",
                *(f"{w} /usr/bin; for f in bas?; do {run}; done" for w in ('"cd"', "c\\d")),
                f"f=ls; .>/dev/null <(echo 'f=bash'); {run}", f"f=ls; .{{,}} <(echo 'f=bash'); {run}",
                f"f=ls; y=\"${{x#'\"'}}\"; f=bash; z=\\'; {run}",
                *(f"for f in bash ls; do {w}; done; {run}" for w in ("break", "return", "exit", "continue")),
                'for f in $(cat l); do rm -rf "$f"; done', 'for f in $(find . -name x); do rm -rf "$f"; done',
                'for f in $L; do rm -rf "$f"; done', 'for f in $(cat l); do echo x > "$f"; done'):
        assert deny(cmd), cmd
    for cmd in ('for f in a b; do rm -rf "$f"; done', 'for f in $(cat l); do cat "$f"; done',
                f"for f in bash ls; do :; done; {run}",
                'for py in ~/.local/bin/python3 /usr/bin/python3; do $py -c "import json"; done',
                'for d in 1 2; do f=x/$d/r.json; [ -f "$f" ] && python3 -c "print(\'$d\')"; done'):
        assert not deny(cmd), cmd


def test_glm_guard_refuses_every_sidefx_writer_naming_a_computed_loop_variable(monkeypatch, tmp_path):
    """cells: loop_computed_writer. Whatever sidefx.WRITE_RE counts as a write refuses a loop over a list the guard
    cannot read when it names the loop's variable; reading it passes."""
    from ttp import glmguard, sidefx
    deny, _, _ = _gg9846_hook(monkeypatch, tmp_path)
    for body in ('patch "$f" < /tmp/x.diff', 'wget -O "$f" u', 'unzip -o a.zip -d "$f"', 'curl -s u -o "$f"',
                 'git checkout -- "$f"', 'git -C "$f" checkout .', 'git clone u "$f"', 'chgrp g "$f"',
                 'find "$f" -delete', 'sed -i s/a/b/ "$f"', 'sed -e s/a/b/ -i "$f"', 'sed s/a/b/ --in-place "$f"',
                 'perl -pi -e s/a/b/ "$f"', 'perl -p -i -e s/a/b/ "$f"', 'rmdir "$f"', 'cp a "$f"',
                 'echo x | tee -a "$f"'):
        assert sidefx.WRITE_RE.search(body), body
        for head in ("for f in $(cat l)", "for f in $L"):
            cmd = f"{head}; do {body}; done"
            assert glmguard.computed_loop_write(cmd) == f"{head}; do", cmd
            assert deny(cmd), cmd
    for body in ('cat "$f"', 'grep x "$f"', 'wc -l "$f"'):
        assert not deny(f"for f in $(cat l); do {body}; done"), body


def test_a_refusal_off_the_glm_arm_is_neither_recorded_nor_ends_the_run(tmp_path):
    """cells: guard_off_arm. Only a run on the GLM arm (its marker) records a guard refusal and ends as a bounce."""
    from ttp import glmguard
    wt, rd = _gg_run(tmp_path, arm=False)
    text = glmguard.refused(rd, "`ttp upstream` is not allowed", cfg={})
    assert "not run" in text and not glmguard.hit_reason(rd)
    assert not (rd / glmguard.HIT).exists() and not (rd / glmguard.REFUSALS).exists()
    glmguard.mark(rd)
    off = {"route": {"glm_refusals": {"redirect": False}}}
    assert "goes back to Opus" in glmguard.refused(rd, "`ttp upstream` is not allowed", cfg=off) and glmguard.hit_reason(rd)



def _glm_staged(monkeypatch, tmp_path, cmd, run=True):
    """The hook's verdict on `cmd` in a GLM run and, if `run` and it passes, the command it lets run, run in bash:
    (hook decision, exit code, output); a refusal is never run."""
    import subprocess
    from ttp import glmguard, hook
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    glmguard.mark_run(rd, False, cwd=str(wt))
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(wt)})
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("permissionDecision") == "deny" or not run:
        return spec.get("permissionDecision"), None, str(spec.get("permissionDecisionReason"))
    staged = (spec.get("updatedInput") or {}).get("command", cmd)
    p = subprocess.run(["bash", "-c", staged], cwd=wt, capture_output=True, text=True, timeout=120)
    return ("staged" if staged != cmd else "plain"), p.returncode, p.stdout + p.stderr


@_pytest.mark.parametrize("cmd,seen", [
    ("P=$(command -v touch); $P ok.txt && ls ok.txt", "ok.txt"),                    # a program name from $(...)
    ("eval \"$(printf 'echo hi > out.txt')\"; cat out.txt", "hi"),                   # an eval of a non-literal
    ("for w in a b; do $(printf touch) \"$PWD/out-$w.txt\"; done; ls out-b.txt", "out-b.txt")])
def test_a_glm_run_runs_a_hidden_program_name_or_eval_once_its_expansion_is_judged(cmd, seen, monkeypatch, tmp_path):
    from ttp import glmguard
    assert glmguard.HIDDEN_WHY in glmguard.shell_refusal(cmd, str(tmp_path), "")   # refused up front before
    kind, rc, text = _glm_staged(monkeypatch, tmp_path, cmd)
    assert kind == "staged" and rc == 0 and seen in text, text


@_pytest.mark.parametrize("cmd,harm", [
    ("eval \"$(printf 'cat ~/.s%s/id_%s' sh rsa)\"", "credential"),    # a credential read, known only once expanded
    ("P=$(printf cat); $P ~/$(printf .s)sh/id_$(printf rs)a", "credential"),
    ("P=$(printf touch); $P ../proj-b/x", "outside"),                    # another project's folder
    ("T=$(printf /bin/touch); $T ../proj-b/x", "outside"),               # a program named by its path
    ("unset -f touch; P=$(printf touch); $P ../proj-b/x", None),         # still refused up front
    ("cd ..; P=$(rm -rf proj-b/src; printf ls); $P", "outside"),       # a body judged where it runs
    ("P=$(sleep 30; printf rm); $P -rf ../proj-b", "10 s"),             # an expansion that outlasts the hook
    ("P=$(printf echo); $P \"$DOCKER_AUTH_CONFIG\"", "secret"),          # any credential-like variable name
    ("eval \"$(printf 'rm -rf /srv/x')\"", ""),
    ("P=$(printf echo); $P \"$GITHUB_TOKEN\"", "secret"),               # a secret printed
    ("echo $(printenv GITHUB_TOKEN) | $(printf cat)", "secret")])
@pytest.mark.away_from_tmp
def test_a_glm_run_stops_a_hidden_command_once_its_expansion_shows_harm(cmd, harm, monkeypatch, tmp_path):
    from ttp import glmguard
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-secret")
    kind, _, why = _glm_staged(monkeypatch, tmp_path, cmd, run=False)
    assert kind == "deny" and "not-a-real-secret" not in why, why
    assert harm is None or (harm in why.lower() and glmguard.HIDDEN_WHY not in why), why


@_pytest.mark.parametrize("cmd", [
    "P=$(command -v mkdir); $P {u}; echo {u}",                                # the literal command makes the entry
    "D=$(mktemp -d); $(command -v touch) \"$D/x\"; echo \"$D\""])             # a body the hook runs makes it
def test_a_staged_command_may_make_and_remove_new_temp_entries(cmd, monkeypatch, tmp_path):
    import os
    import shutil
    from ttp import hook
    u, entry = f"/tmp/ttp-st-{os.getpid()}-{tmp_path.name}", ""
    try:
        kind, rc, text = _glm_staged(monkeypatch, tmp_path, cmd.format(u=u))
        entry = text.split()[-1]
        assert kind == "staged" and rc == 0 and entry.startswith("/tmp/"), text
        rm = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": f"rm -rf {entry}"}, "cwd": str(tmp_path / "wt")})[0]
        assert ((rm or {}).get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", rm
    finally:
        shutil.rmtree(entry or u, ignore_errors=True)


def _fresh_home(monkeypatch, tmp_path):
    """HOME and the config folders at a fresh empty folder, so a guard reads no path of the real home."""
    h = tmp_path / "home"
    h.mkdir()
    for v in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR"):
        monkeypatch.setenv(v, str(h))


@_pytest.mark.parametrize("cmd,seen", [
    ("echo a; T=ls; $T -d /", "/"),                                           # a literal set mid-command
    ("cd /; T=$(command -v ls); $T -d /", "/"),                               # set mid-command from a $(...)
    ("$TTP_RUN_DIR || echo ok", "ok")])                                      # the run folder as the program word
def test_a_glm_run_runs_a_program_name_from_a_variable_once_its_value_is_written_out(cmd, seen, monkeypatch, tmp_path):
    from ttp import glmguard
    _fresh_home(monkeypatch, tmp_path)
    assert glmguard.HIDDEN_WHY in glmguard.shell_refusal(cmd, str(tmp_path), "")   # refused up front before
    kind, rc, text = _glm_staged(monkeypatch, tmp_path, cmd)
    assert kind == "staged" and rc == 0 and seen in text, text


@_pytest.mark.parametrize("cmd", [
    "echo a; T=ls; $T -d /; cat > notes.txt <<'EOF'\neval \"make all\"\nEOF",
    "cat > notes.txt <<'EOF'\neval \"make all\"\nEOF"])
def test_staging_never_changes_a_heredoc_command(cmd, monkeypatch, tmp_path):
    _fresh_home(monkeypatch, tmp_path)
    kind, _, text = _glm_staged(monkeypatch, tmp_path, cmd)
    assert kind != "staged", text
    if kind != "deny":   # where it runs, the file holds the body as written
        assert (tmp_path / "wt" / "notes.txt").read_text() == 'eval "make all"\n'


@_pytest.mark.parametrize("cmd,harm", [
    ("echo a; T=cat; $T ~/.s" + "sh/id_" + "rsa", "credential"),
    ("echo a; T=$(printf cat); $T ~/.s" + "sh/id_" + "rsa", "credential"),
    ("echo a; T=rm; $T -rf ../proj-b", "outside"),                           # another project's folder
    ("echo a; T=touch; $T ~/.bashrc", "outside"),
    ("echo a; T=cp; $T x ~/bin/x", "outside"),
    ("echo a; T=echo; $T \"$GITHUB_TOKEN\"", "secret"),
    ("echo a; T=rm; $T -rf ~/.tt-project/lib", ""),                          # the installed harness
    ("false || T=bash; echo x | $T", glmguard_hidden := "expansion"),       # set on one branch only
    ("if true; then T=bash; fi; echo x | $T", glmguard_hidden),
    ("T=ls; eval \"T=bash\"; echo x | $T", glmguard_hidden),                 # set again by text run as code
    ("T=ls; echo x | T=bash; echo y | $T", glmguard_hidden),                 # set in a pipe's subshell
    ("T=ls; . ./env.sh; echo y | $T", glmguard_hidden),
    ("echo a; false && T=bash; echo x | $T", glmguard_hidden),               # set only if a test passes
    ("echo a; false &&\nT=bash; echo x | $T", glmguard_hidden),             # the same, over two lines
    ("echo a; echo x |\nT=bash; echo y | $T", glmguard_hidden),
    ("echo a; T=X=bash; echo x | $T", glmguard_hidden)])                     # a value that reads as a setting
@pytest.mark.away_from_tmp
def test_a_variable_written_out_still_stops_harm(cmd, harm, monkeypatch, tmp_path):
    _fresh_home(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "not-a-real-secret")
    kind, _, why = _glm_staged(monkeypatch, tmp_path, cmd, run=False)
    assert kind == "deny" and harm in why.lower() and "not-a-real-secret" not in why, why


def _audited_env(monkeypatch, tmp_path):
    """The run-time Python judge (pyaudit) on: its zip first on PYTHONPATH and its policy naming it."""
    import json
    zp = str(tmp_path / "pyaudit" / "pyaudit.zip")
    monkeypatch.setenv("TTP_PYAUDIT_POLICY", json.dumps({"zip": zp}))
    monkeypatch.setenv("PYTHONPATH", zp)


@_pytest.mark.parametrize("cmd,audited,allowed", [
    ("printf 'a\\nb\\n' | while read f; do python3 -c \"print('$f')\"; done", True, True),     # judged as it runs
    ("printf 'a\\nb\\n' | while read f; do python3 -c \"print('$f')\"; done", False, False),   # no run-time judge: refused up front
    ("printf 'a\\nb\\n' | while read f; do PYTHONPATH= python3 -c \"print('$f')\"; done", True, False),   # the judge may not load
    ("printf 'a\\nb\\n' | while read f; do env -i python3 -c \"print('$f')\"; done", True, False),
    ("printf 'a\\nb\\n' | while read f; do python3 -I -c \"print('$f')\"; done", True, False)])
def test_inline_python_code_with_an_expansion_runs_under_the_run_time_judge(cmd, audited, allowed, monkeypatch, tmp_path):
    from ttp import glmguard
    _fresh_home(monkeypatch, tmp_path)
    if audited:
        _audited_env(monkeypatch, tmp_path)
    assert glmguard.shell_refusal(cmd, str(tmp_path), "", env={})   # refused up front with no run-time judge
    kind, rc, text = _glm_staged(monkeypatch, tmp_path, cmd, run=allowed)
    assert (kind == "plain" and rc == 0 and text == "a\nb\n") if allowed else kind == "deny", text


_HANDOFF = ("cat > $TMPDIR/hand.py <<'EOF'\nimport json\n"
            "env = {'TTP_RUN_DIR': 'x'}   # a name set, in shell text\n"
            "json.dump(env, open('result.json', 'w'))\nEOF\n")


@_pytest.mark.parametrize("cmd,allowed", [
    (_HANDOFF + "python3 $TMPDIR/hand.py", True),             # python source, judged as it runs
    (_HANDOFF + "python3 -u $TMPDIR/hand.py | head -3", True),
    (_HANDOFF + "python3 -I $TMPDIR/hand.py", False),         # -I drops the judge
    (_HANDOFF + "bash $TMPDIR/hand.py", False),               # a shell would run it as shell text
    (_HANDOFF + "cp $TMPDIR/hand.py $TMPDIR/hand.sh; python3 $TMPDIR/hand.py", False),
    (_HANDOFF + "env PYTHONPATH= python3 $TMPDIR/hand.py", False),
    (_HANDOFF + "python3 < $TMPDIR/hand.py; python3 $TMPDIR/hand.py", False),
    (_HANDOFF + "python3 $TMPDIR/hand.py; bash $TMPDIR/hand.*", False),           # a glob or `find -exec` runs it as shell
    (_HANDOFF + "python3 $TMPDIR/hand.py; find $TMPDIR -type f -exec bash {} \\;", False),
    (_HANDOFF.replace("hand.py <<", "hand.py > $TMPDIR/b.sh <<") + "bash $TMPDIR/b.sh", False),   # a second file
    (_HANDOFF + "sudo python3 $TMPDIR/hand.py", False), (_HANDOFF + "./python3 $TMPDIR/hand.py", False),
    (_HANDOFF + "PATH=$TMPDIR:$PATH python3 $TMPDIR/hand.py", False),
    (_HANDOFF + "(PATH=$TMPDIR:$PATH python3 $TMPDIR/hand.py)", False),   # a subshell's assignment too
    (_HANDOFF + "BASH_ENV=$TMPDIR/hand.py python3 $TMPDIR/hand.py", False),
    (_HANDOFF + 'python3 $TMPDIR/hand.py; echo "$(bash $TMPDIR/hand.py)"', False),   # a substitution runs it
    (_HANDOFF + "python3 $TMPDIR/hand.py 2> >(bash $TMPDIR/hand.py)", False)])
def test_a_python_script_written_by_a_heredoc_and_run_by_python_is_judged_as_it_runs(cmd, allowed, monkeypatch, tmp_path):
    """cells: handoff_heredoc. The body of `cat > FILE <<'EOF'` that only `python FILE` runs is python code the
    run-time judge watches, like `python3 - <<'EOF'`: a name in a string of it is no write target. Any other use of
    the file keeps the body shell text, and no judge means no pass."""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMPDIR", str(rd / "tmp"))
    _audited_env(monkeypatch, tmp_path)
    assert bool(glmguard.shell_refusal(cmd, str(wt), str(rd), env=dict(os.environ))) != allowed
    if allowed:
        monkeypatch.delenv("PYTHONPATH")
        monkeypatch.delenv("TTP_PYAUDIT_POLICY")
        assert glmguard.shell_refusal(cmd, str(wt), str(rd), env=dict(os.environ))   # no judge: refused


@_pytest.fixture(autouse=True)
def _glm_tmp_tops_leave_the_test_base_temp(tmp_path_factory, monkeypatch):
    """A shared temp top the tests' own folders sit in (/dev/shm on a RAM-disk suite) is no scratch for the guard's
    cases: a path 'outside the worktree' stays outside. The /dev/shm cases put the tops back."""
    from ttp import glmguard
    base = str(tmp_path_factory.getbasetemp()) + "/"
    monkeypatch.setattr(glmguard, "_TMP", tuple(t for t in glmguard._TMP if not base.startswith(t + "/")))


@_pytest.mark.parametrize("cmd,allowed", [
    ("mkdir /dev/shm/x-{n} && echo 1 > /dev/shm/x-{n}/a && rm -rf /dev/shm/x-{n}", True),
    ('rm -rf "${{TTP_RUN_DIR:?}}"/tmp/x', True), ("rm -rf ${{HOME:?}}/work-{n}", True),
    ("rm -rf /dev/shm/ttp-{uid}/x", False), ("echo x > /tmp/ttp-{uid}/y", False),
    ("rm -rf ${{TTP_RUN_DIR?need}}/tmp/x", True),
    ("rm -rf /dev/shm", False), ("rm -rf /dev/shm/../etc", False), ("rm -rf ${{TTP_RUN_DIR:?}}/../../../../../../../etc/x", False),
    ("echo x > /dev/shm/../etc/x", False), ("echo x > /dev/null/../x", False)])
@_pytest.mark.away_from_tmp
def test_a_glm_run_uses_dev_shm_like_tmp_and_reads_a_checked_run_dir_expansion(cmd, allowed, monkeypatch, tmp_path):
    """cells: shm_and_checked_expansion. A new /dev/shm entry is scratch like one in /tmp (the top itself and a
    `..` out of it are not), and `${TTP_RUN_DIR:?}` names the run folder where it is set, else the shell stops."""
    from ttp import glmguard
    monkeypatch.setattr(glmguard, "_TMP", ("/tmp", "/var/tmp", "/dev/shm"))
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    (tmp_path / "home").mkdir()   # the session's own HOME sits in the base temp, which a records or ttp-<uid> folder may hold
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cmd = cmd.format(n=f"{os.getpid()}-{tmp_path.name}", uid=os.getuid())
    assert bool(glmguard.shell_refusal(cmd, str(wt), str(rd), env=dict(os.environ))) != allowed


def test_a_glm_run_keeps_the_judge_out_of_an_env_dash_i_test_run_and_refuses_a_hidden_home_basetemp(monkeypatch, tmp_path):
    """`env -i` drops the run-time judge from the tests' python, and a hidden home entry is outside the bar:
    both stay refused."""
    from ttp import glmguard
    wt, rd = _glm_run_env(monkeypatch, tmp_path)
    _audited_env(monkeypatch, tmp_path)
    env = dict(os.environ)
    assert glmguard.shell_refusal("env -i HOME=$HOME PATH=/usr/bin python3 -m pytest -q t.py", str(wt), str(rd), env=env)
    assert glmguard.shell_refusal(f"python3 -m pytest -q --basetemp={Path.home()}/.zz t.py && rm -rf {Path.home()}/.zz",
                                  str(wt), str(rd), env=env)

# GLM work share and wave gate (glmshare.py, `ttp stats --glm-share`) tests of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there). Add new glmshare tests here.
from __future__ import annotations

_GS_GLM = "example-org/GLM-9"


def _gs_task(p, kind="code", status="done", labels=()):
    tid = p.db.add_task(f"t {kind} {len(p.db.q('SELECT id FROM tasks'))}", "Do it.", kind=kind, tier="standard",
                        origin="coordinator", priority=3, labels=list(labels))
    p.db.x("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid


def _gs_run(p, task, start, hours, provider="chat", model=_GS_GLM, role="worker", note=None):
    return p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,cost_usd,note) VALUES(?,?,?,?,?,?,?,?,?)",
                  (task, role, provider, model, start, None if hours is None else start + hours * 3600, "done", 0.0,
                   json.dumps(note or {})))


def _gs_review(p, of, status):
    return _gs_task(p, kind="review", status=status, labels=[f"auto_review:{of}"])


def test_glm_share_counts_model_hours_and_runs_per_role_and_quality_per_arm(env, capsys):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    t = now - 20 * 3600
    _gs_run(p, None, t, 1, provider="claude", model="opus", role="coordinator")
    _gs_run(p, None, t, 1, role="coordinator")                                   # a coordinator turn on GLM
    a = _gs_task(p)                                                              # glm arm by its record
    p.db.set_kv(f"glm_work_arm:{a}", {"arm": "glm", "why": "covered", "ts": t})
    _gs_run(p, a, t, 2)
    _gs_review(p, a, "done")
    b = _gs_task(p, status="failed")                                             # holdout: the Claude arm
    p.db.set_kv(f"glm_work_arm:{b}", {"arm": "holdout", "why": "holdout", "ts": t})
    _gs_run(p, b, t, 1, provider="claude", model="opus")
    rb = _gs_review(p, b, "failed")
    fix = _gs_task(p, labels=[f"review_fix:{rb}", f"continues:{b}"])
    p.db.x("INSERT INTO events(ts,source,kind,text,task) VALUES(?,?,?,?,?)",
           (t + 9000, "user", "task_requeued", f"#{b} requeued from failed by the user", b))
    c = _gs_task(p, kind="work")                                                 # routed, fell back to Claude
    _gs_run(p, c, t, 0.5, provider="claude", model="opus", note={"route": {"to": "chat", "fallback": "needs_opus"}})
    d = _gs_task(p, kind="work", labels=["needs_device", "resource:box-a-device"])   # device work on a lower-case glm
    _gs_run(p, d, t, 1, model="other-org/glm-mini")
    p.db.x("INSERT INTO events(ts,source,kind,text,task) VALUES(?,?,?,?,?)", (t + 600, "watcher", "device_hang", "box-a-device hung", d))
    r = _gs_task(p, kind="review")
    _gs_run(p, r, t, 0.5, provider="claude", model="opus")
    _gs_run(p, None, now - 3 * 86400, 5, provider="chat", role="coordinator")   # before the window: not counted
    _gs_run(p, c, now - 3600, None, provider="claude", model="opus")             # still running: up to now
    p.db.x("INSERT INTO ledger(ts,provider,usd) VALUES(?,?,?)", (t, "claude", 12.0))
    p.db.x("INSERT INTO ledger(ts,provider,usd) VALUES(?,?,?)", (t, "chat", 5.0))
    p.db.set_kv("corrections", [{"msg": "m", "ts": t}, {"msg": "old", "ts": now - 9 * 86400}])
    s = glmshare.report(p, now - 86400, now)
    # 8 runs, 8 model-hours; GLM: the coordinator turn 1h, code 2h, device 1h.
    assert (s["total"]["runs"], s["total"]["glm_runs"], s["total"]["hours"], s["total"]["glm_hours"]) == (8, 3, 8.0, 4.0)
    assert (s["total"]["hour_pct"], s["total"]["run_pct"]) == (50.0, 37.5)
    ro = s["roles"]
    assert (ro["coordinator"]["hours"], ro["coordinator"]["hour_pct"], ro["coordinator"]["run_pct"]) == (2.0, 50.0, 50.0)
    assert (ro["code"]["hours"], ro["code"]["glm_hours"], ro["code"]["hour_pct"]) == (3.0, 2.0, 66.7)
    assert (ro["work"]["hours"], ro["work"]["hour_pct"]) == (1.5, 0.0)
    assert (ro["device"]["hours"], ro["device"]["hour_pct"]) == (1.0, 100.0)
    assert (ro["review"]["hours"], ro["review"]["hour_pct"]) == (0.5, 0.0)
    assert ro["plan"]["hour_pct"] is None
    g, c_ = s["arms"]["glm"], s["arms"]["claude"]
    assert (g["tasks"], g["done"], g["failed"], g["reviewed"], g["first_pass_pct"], g["incidents"]) == (2, 2, 0, 1, 100.0, 1)
    assert (c_["tasks"], c_["done"], c_["failed"], c_["reopened"], c_["first_pass_pct"]) == (2, 1, 1, 1, 0.0)
    assert (c_["fix_rounds_per_reviewed"], c_["bounces"], c_["incidents"], c_["done_per_day"]) == (1.0, 1, 0, 1.0)
    assert fix not in (a, b, c, d)
    assert s["claude_usd_per_day"] == 12.0
    assert s["corrections"]["n"] == 1 and s["corrections"]["per_arm"] is None and s["corrections"]["note"]
    out = glmshare.text(s)
    assert "GLM 50% of 8.0 model-h, 37.5% of 8 runs" in out and "Claude $12.00/day" in out
    # The CLI prints it, one project or each named one.
    from ttp import cli
    cli.main(["stats", "demo", "--glm-share", "--since", str(now - 86400), "--until", str(now), "--json"])
    assert json.loads(capsys.readouterr().out)["total"]["glm_hours"] == 4.0
    cli.main(["stats", "--glm-share", "--projects", "demo,nope", "--since", str(now - 86400), "--until", str(now)])
    got = capsys.readouterr().out
    assert "demo: GLM 50%" in got and "nope: not a project on this machine" in got


def _gs_decided(p, start, passed, provider="chat", arm=None, ts=None):
    tid = _gs_task(p, status="done" if passed else "failed")
    if arm:
        p.db.set_kv(f"glm_work_arm:{tid}", {"arm": arm, "why": "x", "ts": ts or start})
    _gs_run(p, tid, start, 1, provider=provider, model=_GS_GLM if provider == "chat" else "opus")
    _gs_review(p, tid, "done" if passed else "failed")
    return tid


def test_glm_share_judge_waits_for_enough_glm_tasks_then_compares_with_the_project_s_own_claude_baseline(env):
    from ttp import glmshare
    p = make(env)
    cfg = {"route": {"glm_work": {"min_tasks": 3, "min_base": 5, "worse_pts": 15}}}
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    assert glmshare.judge(p, cfg, now)["state"] == "wait"          # no arm records yet
    st = glmshare.settings({})
    assert (st["min_tasks"], st["min_base"], st["advance_pts"], st["rollback_pts"]) == (25, 10, 5.0, 15.0)
    assert glmshare.settings(cfg)["rollback_pts"] == 15.0                # the older worse_pts sets the rollback margin
    # Baseline: 4 of 5 passed in the 14 days before the first arm record, plus a passing holdout task.
    for ok in (1, 1, 1, 1, 0):
        _gs_decided(p, t0 - 3 * 86400, ok, provider="claude")
    _gs_decided(p, t0 - 20 * 86400, 0, provider="claude")           # older than 14 days: not baseline
    _gs_decided(p, t0 + 3600, 1, provider="claude", arm="holdout")
    g1 = _gs_decided(p, t0, 1, arm="glm")
    _gs_decided(p, t0 + 3600, 1, arm="glm")
    v = glmshare.judge(p, cfg, now)
    assert (v["state"], v["n"]) == ("wait", 2) and "2 of 3 GLM tasks" in v["why"]
    _gs_decided(p, t0 + 7200, 1, arm="glm")
    v = glmshare.judge(p, cfg, now)
    assert (v["state"], v["n"]) == ("ok", 3), v
    assert "GLM 3 tasks with an outcome; Claude baseline 6 with an outcome" in v["why"]
    assert "first review pass rate GLM 100 vs Claude 83.3 (too few outcomes to compare yet)" in v["why"]
    # Any device incident on a GLM run (on a device it changed): worse.
    p.db.update_task(g1, labels=["resource:box-a-device"])
    p.db.x("INSERT INTO events(ts,source,kind,text,task) VALUES(?,?,?,?,?)",
           (t0 + 60, "watcher", "device_reset", "box-a-device reset", g1))
    v = glmshare.judge(p, cfg, now)
    assert v["state"] == "worse" and "1 device incident" in v["why"]
    p.db.x("DELETE FROM events WHERE kind='device_reset'")
    for _ in range(3):
        _gs_decided(p, t0 + 9000, 0, arm="glm")
    v = glmshare.judge(p, cfg, now)
    assert (v["state"], v["n"]) == ("worse", 5)
    assert "first review pass rate GLM 40 vs Claude 83.3, worse by 43.3, past the rollback margin of 15" in v["why"]
    # Inside a wider rollback margin but past the advance margin: the wave holds.
    v = glmshare.judge(p, {"route": {"glm_work": {"min_tasks": 3, "min_base": 5, "rollback_pts": 50}}}, now)
    assert v["state"] == "wait" and "holding the wave: first review pass rate" in v["why"]



_GS_CFG = {"route": {"glm_work": {"min_tasks": 4, "min_base": 4}}}


def _gs_waved(p, now, glm_passed=(1, 1, 1, 1)):
    """A project at its wave gate: 4 passing Claude tasks in the baseline window, then GLM arm tasks."""
    t0 = now - 5 * 86400
    for _ in range(4):
        _gs_decided(p, t0 - 3 * 86400, 1, provider="claude")
    return t0, [_gs_decided(p, t0 + 3600 * (i + 1), ok, arm="glm") for i, ok in enumerate(glm_passed)]


def _gs_code_arm(p, start, passed, arm="glm", ts=None):
    """A decided code-arm task: a code_arm record, one worker run and its first review."""
    from ttp import codearm
    tid = _gs_task(p)
    p.db.set_kv(f"{codearm.ARM_KV}{tid}", {"arm": arm, "why": "coin at share 0.5", "ts": ts or start})
    _gs_run(p, tid, start, 1, provider="chat" if arm == "glm" else "claude",
            model=_GS_GLM if arm == "glm" else "claude-sonnet")
    _gs_review(p, tid, "done" if passed else "failed")
    return tid


def _gs_verdict(v, name):
    return next(s["verdict"] for s in v["signals"] if s["signal"] == name)


@pytest.mark.parametrize("signal", ["fix rounds per reviewed task", "bounce rate", "tasks done per day",
                                    "user corrections"])
def test_glm_share_judge_each_new_signal_past_its_advance_margin_holds_the_wave(env, signal):
    from ttp import correction, glmshare, route
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0, glm = _gs_waved(p, now)
    assert glmshare.judge(p, _GS_CFG, now)["state"] == "ok"
    if signal == "fix rounds per reviewed task":       # 2 of 4 reviewed GLM tasks took a fix round: 0.5 vs 0
        for tid in glm[:2]:
            rb = _gs_review(p, tid, "failed")
            _gs_task(p, labels=[f"review_fix:{rb}", f"continues:{tid}"])
    elif signal == "bounce rate":                      # 1 of 4 GLM tasks bounced to Opus: 25 points vs 0
        _gs_run(p, glm[0], t0 + 7200, 1, note={route.KEY: {"to": route.TO, "fallback": route.BOUNCE}})
    elif signal == "tasks done per day":               # 20 done in the 14 days before, 4 in the 5 since
        for _ in range(16):
            _gs_decided(p, t0 - 2 * 86400, 1, provider="claude")
    else:                                              # 4 user corrections in the last 7 days
        p.db.set_kv(correction.KEY, [{"msg": f"m{i}", "ts": now - 86400} for i in range(4)])
    v = glmshare.judge(p, _GS_CFG, now)
    assert (v["state"], _gs_verdict(v, signal)) == ("wait", "hold"), v
    assert f"holding the wave: {signal}" in v["why"]


def test_glm_share_rollback_reason_leads_with_the_failing_metric_and_its_numbers(env):
    from ttp import glmshare, route
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0, glm = _gs_waved(p, now)
    for tid in glm[:2]:   # 2 of 4 GLM tasks bounced to Opus: 50 points vs 0, past the 25-point rollback margin
        _gs_run(p, tid, t0 + 7200, 1, note={route.KEY: {"to": route.TO, "fallback": route.BOUNCE}})
    v = glmshare.judge(p, _GS_CFG, now)
    assert v["state"] == "worse", v
    assert v["why"].startswith("worse than the Claude baseline past the rollback margin: bounce rate; ")
    nums = v["why"].split("; ", 1)[1]
    assert "bounce rate GLM 50 vs Claude 0, worse by 50, past the rollback margin of 25" in nums.split("; ", 3)[2]
    assert "worse by -" not in v["why"]   # a passing signal is never printed as a negative "worse by"


def test_glm_share_judge_rolls_back_a_clearly_worse_glm_arm(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    _gs_waved(p, now, glm_passed=(1, 0, 0, 0))
    v = glmshare.judge(p, _GS_CFG, now)
    assert v["state"] == "worse", v
    assert _gs_verdict(v, "first review pass rate") == _gs_verdict(v, "failed or reopened rate") == "worse"


def test_glm_share_judge_says_not_enough_data_below_the_minimum_sample_in_either_arm(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    _gs_waved(p, now, glm_passed=(0, 0, 0, 0))                          # clearly worse, but 4 of 25 GLM tasks
    v = glmshare.judge(p, {}, now)
    assert v["state"] == "wait" and v["why"].startswith("not enough data: 4 of 25 GLM tasks"), v
    v = glmshare.judge(p, {"route": {"glm_work": {"min_tasks": 4, "min_base": 5}}}, now)   # 4 of 5 baseline
    assert v["state"] == "wait" and "4 of 5 baseline tasks" in v["why"], v

def test_a_re_arm_starts_a_new_window_and_a_rolled_back_project_waits_for_new_outcomes(env):
    from ttp import glmshare, rollout
    p = make(env)
    now = time.time()
    t0, _ = _gs_waved(p, now, glm_passed=(0, 0, 0, 0))
    assert glmshare.judge(p, _GS_CFG, now)["state"] == "worse"
    assert rollout.stop_project("glm_work", p.name, "worse") and "re-armed" in rollout.arm("glm_work", project=p.name)
    later = time.time() + 600
    v = glmshare.judge(p, _GS_CFG, later)
    assert (v["state"], v["n"]) == ("wait", 0), "the outcomes before the arm no longer count"
    for _ in range(3):
        _gs_decided(p, later, 1, arm="glm")
    v = glmshare.judge(p, _GS_CFG, later + 3600)
    assert (v["state"], v["n"]) == ("wait", 3) and "3 of 4 GLM tasks" in v["why"]
    _gs_decided(p, later, 1, arm="glm")
    assert glmshare.judge(p, _GS_CFG, later + 3600)["state"] == "ok"
    assert rollout.armed_at("other", "glm_work") == 0.0, "another project's arm is its own"


def test_a_glm_code_re_arm_alone_starts_a_new_window_for_the_glm_share_judge(env):
    from ttp import glmshare, rollout
    p = make(env)
    now = time.time()
    _gs_waved(p, now, glm_passed=(0, 0, 0, 0))
    assert glmshare.judge(p, _GS_CFG, now)["state"] == "worse"
    assert rollout.stop_project("glm_code", p.name, "worse") and "re-armed" in rollout.arm("glm_code", project=p.name)
    v = glmshare.judge(p, _GS_CFG, time.time() + 600)
    assert (v["state"], v["n"]) == ("wait", 0), "the outcomes before the glm_code re-arm no longer count"


def test_glm_device_runs_leave_the_glm_work_judgment_but_a_device_incident_after_the_arm_still_rolls_back(env):
    from ttp import glmshare, rollout
    p = make(env)
    now = time.time()
    t0, _ = _gs_waved(p, now)
    box = ["needs_device", "resource:box-a-device"]
    dev = [_gs_decided(p, t0 + 7200, 0, arm="glm") for _ in range(4)]
    for tid in dev:
        p.db.update_task(tid, labels=box)
    v = glmshare.judge(p, _GS_CFG, now)
    assert (v["state"], v["n"]) == ("ok", 4), "failed device tasks are not glm_work outcomes"
    for _ in range(2):   # no device mark: the spec names the device work, and the device route runs it on GLM
        p.db.x("UPDATE tasks SET spec=? WHERE id=?", ("Run the soak on the box.", _gs_decided(p, t0 + 7200, 0, arm="glm")))
    v = glmshare.judge(p, _GS_CFG, now)
    assert (v["state"], v["n"]) == ("ok", 4), "a device task by its spec text is no glm_work outcome either"
    rollout.stop_project("glm_work", p.name, "worse")
    rollout.arm("glm_work", project=p.name)
    later = time.time() + 600
    d = _gs_decided(p, later, 1, arm="glm")
    p.db.update_task(d, labels=box)
    p.db.x("INSERT INTO events(ts,source,kind,text,task) VALUES(?,?,?,?,?)", (later + 60, "watcher", "device_reset", "box-a-device reset", d))
    v = glmshare.judge(p, _GS_CFG, later + 3600)
    assert v["state"] == "worse" and "1 device incident" in v["why"] and v["n"] == 0


def test_glm_share_counts_bad_output_per_day_from_glm_runs_exit_records(env):
    """#3660: the bridge's bad-output counters (tool input errors, unparsed arguments, reasoning-only and junk
    answers) per home-zone day, with a rate per answer and the runs hit; `ttp stats --glm-share` and the daily
    review show them. Claude runs and runs with no exit record are not counted."""
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    t1, t2 = now - 30 * 3600, now - 3 * 3600

    def run(start, bridge, provider="chat", model=_GS_GLM):
        rid = _gs_run(p, None, start, 1, provider=provider, model=model)
        d = p.runs / str(rid)
        d.mkdir(parents=True, exist_ok=True)
        if bridge is not None:
            (d / "exit.json").write_text(json.dumps({"rc": 0, "bridge": bridge}))
        return rid

    run(t1, {"usage": {"calls": 40, "scanned": 40},
             "bad_output": {"tool_input_errors": 1, "args_unparsed": 1, "empty_content": 0,
                            "junk_responses": 2, "junk_hits": {"AMEAME": 2}}})
    run(t1 + 60, {"usage": {"calls": 60, "scanned": 60}})                       # clean
    run(t1 + 90, {"usage": {"calls": 70}})                                     # before the counters: not counted
    run(t2, {"usage": {"calls": 50, "scanned": 50}, "bad_output": {"empty_content": 1}})
    run(t2, {"usage": {"calls": 9}, "bad_output": {"junk_responses": 9}}, provider="claude", model="opus")
    run(t2, None)                                                             # no exit record yet
    s = glmshare.report(p, now - 2 * 86400, now)
    days = s["bad_output"]
    assert [d["runs"] for d in days] == [2, 1] and [d["answers"] for d in days] == [100, 50]
    assert (days[0]["tool_input_errors"], days[0]["args_unparsed"], days[0]["junk_responses"], days[0]["runs_hit"]) == (1, 1, 2, 1)
    assert days[0]["pct"]["junk_responses"] == 2.0 and days[1]["pct"]["empty_content"] == 2.0
    out = glmshare.text(s)
    assert "GLM bad output by day" in out and "junk 2 (2%); loop nudges 0, loop stops 0; runs hit 1" in out
    lines = glmshare.daily_lines(p, now)
    assert len(lines) == 2 and "reasoning-only 1 (2%)" in lines[1]


def test_glm_share_names_reviews_that_ran_on_glm_only_when_there_are_any(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    r = _gs_task(p, kind="review")
    _gs_run(p, r, now - 20 * 3600, 0.5, provider="claude", model="opus")
    assert "reviews on GLM" not in glmshare.text(glmshare.report(p, now - 86400, now))
    _gs_run(p, r, now - 30 * 3600, 0.1, note={"run_class": "light"})
    _gs_run(p, r, now - 10 * 3600, 0.1, note={"run_class": "rebase"})
    _gs_run(p, None, now - 9 * 3600, 0.1, role="coordinator")
    s = glmshare.report(p, now - 2 * 86400, now)
    newest = datetime.datetime.fromtimestamp(now - 10 * 3600).strftime("%Y-%m-%d")
    assert f"reviews on GLM: 2, newest {newest} (run class rebase)" in glmshare.text(s)


def test_glm_share_held_groups_claude_runs_by_the_rule_that_kept_them_and_marks_the_users_own(env, capsys):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    t = now - 5 * 3600
    cl = {"provider": "claude", "model": "opus"}
    def code(why, hours, **kw):
        tid = _gs_task(p)
        _gs_run(p, tid, t, hours, note={"code_arm": {"arm": "claude", "why": why}}, **cl, **kw)
        return tid
    code("policy: harness/policy_files.txt", 2)
    code("lineage of #5", 1)
    code("conclusion floor", 0.5)
    code("person: post to slack", 1)
    code("design: optimization", 0.25)
    code("holdout: seeded coin", 0.25)
    tid = _gs_task(p, kind="work")
    _gs_run(p, tid, t, 0.5, note={"route": {"to": "claude", "fallback": "rerun"}}, **cl)
    _gs_run(p, _gs_task(p, kind="review"), t, 1, **cl)
    _gs_run(p, _gs_task(p, kind="plan"), t, 0.5, **cl)
    _gs_run(p, None, t, 1, role="coordinator", **cl)
    _gs_run(p, _gs_task(p), t, 4)   # on GLM: never counted as held
    got = glmshare.held(p, now - 86400, now)
    assert list(got)[0] == "policy files" and got["policy files"]["hours"] == 2.0
    by = {k: (v["runs"], v["hours"], v["user_rule"]) for k, v in got.items()}
    assert by["lineage misses"] == (1, 1.0, False) and by["conclusion floor"] == (1, 0.5, False)
    assert by["rerun after a fallback"] == (1, 0.5, False) and by["policy files"][2] is False
    assert by["person"] == (1, 1.0, True) and by["design or optimization"][2] is True and by["holdout"][2] is True
    assert by["review"] == (1, 1.0, True) and by["plan"] == (1, 0.5, True) and by["coordinator turn"] == (1, 1.0, True)
    assert got["policy files"]["tasks"] == 1 and got["policy files"]["hour_pct"] == round(100 * 2.0 / 12.0, 1)
    s = glmshare.report(p, now - 86400, now)
    assert s["held"] == got
    assert "held off GLM by rule" in glmshare.text(s) and "policy files" in glmshare.text(s)


def test_glm_share_held_reads_the_core_reason_and_an_old_lineage_s_root(env):
    from ttp import codearm, glmshare, route
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    t, cl = now - 5 * 3600, {"provider": "claude", "model": "opus"}
    _gs_run(p, _gs_task(p, kind="work"), t, 1, note={route.CORE_WHY: "untrusted"}, **cl)
    root = _gs_task(p)
    p.db.set_kv(f"{codearm.ARM_KV}{root}", {"arm": "claude", "why": "design: optimization"})
    _gs_run(p, _gs_task(p), t, 2, note={"code_arm": {"arm": "claude", "why": f"lineage of #{root}"}}, **cl)
    by = {k: v["hours"] for k, v in glmshare.held(p, now - 86400, now).items()}
    assert by == {"untrusted": 1.0, "design or optimization": 2.0}, by


def _gs_outcome(p, start, passed, reviewed_at, provider="chat", arm=None, ts=None):
    """A decided task whose first auto review was queued at `reviewed_at`."""
    tid = _gs_decided(p, start, passed, provider=provider, arm=arm, ts=ts)
    p.db.x("UPDATE tasks SET created=? WHERE kind='review' AND labels LIKE ?", (reviewed_at, f'%"auto_review:{tid}"%'))
    return tid


_GS_SAME = {"route": {"glm_work": {"min_tasks": 20, "min_base": 10, "min_window": 20}}}


def _gs_period(p, t0, claude_after, glm_passed, claude_before=(1,) * 16 + (0,) * 4):
    """Claude reviewed before t0 (`claude_before`) and after it (`claude_after`), GLM armed at t0 and reviewed after."""
    for i, ok in enumerate(claude_before):
        _gs_outcome(p, t0 - 3 * 86400, ok, t0 - (i + 1) * 3600, provider="claude")
    for i, ok in enumerate(claude_after):
        _gs_outcome(p, t0 + 3600, ok, t0 + 86400 + i * 600, provider="claude")
    for i, ok in enumerate(glm_passed):
        _gs_outcome(p, t0 + 3600, ok, t0 + 86400 + i * 600, arm="glm", ts=t0)


def test_glm_share_judge_compares_glm_with_claude_reviewed_in_the_same_period_not_before_a_stricter_review(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    # Claude passed 80% before t0 and 60% after; GLM 55% after: no worse than Claude in its own period.
    _gs_period(p, t0, (1,) * 12 + (0,) * 8, (1,) * 11 + (0,) * 9)
    v = glmshare.judge(p, _GS_SAME, now)
    assert v["state"] != "worse", v["why"]
    assert (v["glm_pct"], v["base_pct"], v["base_window"]) == (55.0, 60.0, [t0, now])
    assert "Claude baseline 20 with an outcome since" in v["why"]


def test_glm_share_judge_rolls_back_glm_clearly_worse_than_claude_in_the_same_period(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    _gs_period(p, t0, (1,) * 12 + (0,) * 8, (1,) * 8 + (0,) * 12)
    v = glmshare.judge(p, _GS_SAME, now)
    assert v["state"] == "worse" and "first review pass rate GLM 40 vs Claude 60" in v["why"], v["why"]
    assert (v["glm_pct"], v["base_pct"], v["base_window"][0]) == (40.0, 60.0, t0)


def test_glm_share_judge_reaches_back_only_as_far_as_the_minimum_reviewed_claude_outcomes(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    # 5 Claude outcomes after t0; the 20 before it were reviewed one an hour back from t0: the newest 15 fill the window.
    _gs_period(p, t0, (1, 1, 1, 0, 0), (1,) * 20)
    v = glmshare.judge(p, _GS_SAME, now)
    assert v["base_window"] == [t0 - 15 * 3600, now]
    assert "Claude baseline 20 with an outcome" in v["why"] and v["base_pct"] == 90.0   # 3 of 5 + 15 of 15
    # Fewer than the minimum in all: the whole lookback, never further.
    v = glmshare.judge(p, {"route": {"glm_work": dict(_GS_SAME["route"]["glm_work"], min_window=40)}}, now)
    assert v["base_window"][0] == t0 - glmshare.BASE_DAYS * 86400 and "Claude baseline 25 with an outcome" in v["why"]


def test_glm_share_held_splits_person_by_how_the_task_reaches_a_person_and_reads_old_rows(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 30 * 86400
    t = now - 5 * 3600
    cl = {"provider": "claude", "model": "opus"}

    def task(spec, **kw):
        tid = p.db.add_task("t person", spec, kind="work", tier="standard", origin="coordinator", priority=3, **kw)
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        return tid
    _gs_run(p, task("Answer it.", reply_chat="c1"), t, 1, **cl)
    _gs_run(p, task("Fix it, then post a summary to the Slack channel."), t, 0.5, **cl)
    _gs_run(p, task("Tidy it.\nconclusion: the tidy-up is done"), t, 0.25, **cl)
    _gs_run(p, task("Do it."), t, 2, note={"code_arm": {"arm": "claude", "why": "person: post to slack"}}, **cl)   # an old row
    got = glmshare.held(p, now - 86400, now)
    by = {k: (v["runs"], v["hours"], v["user_rule"]) for k, v in got.items()}
    assert by["person:reply_chat"] == (1, 1.0, True) and by["person:instruction"] == (1, 0.5, True)
    assert by["person:conclusion"] == (1, 0.25, True) and by["person"] == (1, 2.0, True)


def test_glm_share_bounces_a_live_fix_covers_no_longer_count_toward_the_rollback(env, monkeypatch, capsys):
    """The glm_work judge reads `ttp rollout fixed` too: bounces from before a listed fix went live drop out."""
    from ttp import glmshare, route
    p, target, fix, head, older = _rearm_setup(env, monkeypatch)
    now = 1_000_000_000.0 + 60 * 86400
    t0, glm = _gs_waved(target, now)
    bounced = [_gs_run(target, tid, t0 + 7200, 1, note={route.KEY: {"to": route.TO, "fallback": route.BOUNCE}})
               for tid in glm[:2]]
    assert glmshare.judge(target, _GS_CFG, now)["state"] == "worse"
    _daemon_starts(monkeypatch, target, [(t0, older), (t0 + 86400, head)])
    code, out = _roll(capsys, "fixed", "glm_work", target.name, "--fix", str(fix), "--runs", ",".join(map(str, bounced)))
    v = glmshare.judge(target, _GS_CFG, now)
    assert code == 0 and v["state"] != "worse" and f"2 stops not counted: fixed by task #{fix}" in v["why"], (out, v)


def test_the_glm_code_judge_rolls_back_a_clearly_worse_code_arm_and_leaves_the_work_judge_alone(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    for _ in range(4):     # the Claude baseline: sonnet-arm code tasks, all passing
        _gs_code_arm(p, t0 - 3 * 86400, 1, arm="sonnet")
    for _ in range(4):     # the GLM arm: every task failing its first review
        _gs_code_arm(p, t0 + 3600, 0, arm="glm")
    v = glmshare.judge_code(p, _GS_CFG, now)
    assert v["state"] == "worse", v
    assert "worse than the Claude baseline past the rollback margin: first review pass rate" in v["why"]
    assert {s["signal"] for s in v["signals"]} == {"first review pass rate", "bounce rate", "tasks done per day"}
    assert _gs_verdict(v, "first review pass rate") == "worse"
    # glm_work's own judge is untouched: it still waits on the work arms, not the code arms
    w = glmshare.judge(p, _GS_CFG, now)
    assert (w["state"], w["why"]) == ("wait", "not enough data: no glm_work arm records yet")


def test_the_glm_code_judge_keeps_a_code_arm_that_matches_its_claude_baseline(env):
    from ttp import glmshare
    p = make(env)
    now = 1_000_000_000.0 + 60 * 86400
    t0 = now - 5 * 86400
    for _ in range(4):
        _gs_code_arm(p, t0 - 3 * 86400, 1, arm="sonnet")
    for _ in range(4):
        _gs_code_arm(p, t0 + 3600, 1, arm="glm")
    v = glmshare.judge_code(p, _GS_CFG, now)
    assert v["state"] == "ok", v
    assert v["why"].startswith("within every advance margin of the Claude baseline")
    assert "first review pass rate GLM 100 vs Claude 100, level, within the margins" in v["why"]

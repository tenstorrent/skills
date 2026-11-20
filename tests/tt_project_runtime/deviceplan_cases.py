# Device route tests (deviceplan.py: Opus plans, GLM runs only that plan) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new deviceplan tests here.
from __future__ import annotations

_DP_MODEL = "example-org/work-model"
_DP_PLAN = """1. Check who holds box-a. 2. Reset it through the broker.
```json
{"steps": [{"host": "box-a", "command": "mkdir -p /srv/out", "check": false},
           {"host": "local", "command": "broker reset box-a", "check": true},
           {"n": 7, "host": "box-a", "command": "test -e /srv/out", "check": true},
           {"host": "local", "command": "Check the holder first.", "check": false}]}
```"""


def _dp_cfg(**over):
    return {"providers": {"chat": {"endpoint": "placeholder", "base_url": "https://chat.example.invalid/v1"}},
            "route": {"glm_work": {"on": True, "model": _DP_MODEL, **over}}}


def _dp_run(tmp_path):
    from ttp import glmguard
    run = tmp_path / "run"
    run.mkdir()
    glmguard.mark_run(run, True, plan=True)
    return run, {"TTP_RUN_DIR": str(run)}


def test_device_plan_reads_misfire_no_more_and_writes_outside_still_change(tmp_path):
    """Sensor reads, occupancy, fd copies and prose lines run before any plan; a file write is a change."""
    from ttp import coordfix, deviceplan
    run, e = _dp_run(tmp_path)
    for c in ("ipmitool sdr", "ipmitool sensor list", "ttp occupancy", "ls 2>&1 | tail -5", "echo hi >&2",
              "ls 2>/dev/null", "Check the holder first.", "ipmitool power status", "tt-smi -s", "dmesg -T"):
        assert deviceplan.refusal(c, str(tmp_path), e, {}) is None, c
    for c in ("ipmitool sdr", "ttp occupancy", "ls 2>&1 | tail -5", "tt-smi -s"):
        assert coordfix.reads_only(c), f"one shared read table: {c}"
    for c in ("ls > /srv/x", "ls 2> /srv/x", "ssh box-a 'ls > /srv/x'", "tt-smi -r 0"):
        assert deviceplan.refusal(c, str(tmp_path), e, {}), c
        assert not coordfix.reads_only(c), c


def test_the_seventh_check_allows_power_only_through_the_approved_routes():
    from ttp import deviceplan
    for c in ("ipmitool chassis power cycle", "ipmitool -H box-a power reset", "ipmitool raw 0x00 0x02 0x03",
              "tt-smi -r 0", "ssh box-a 'sudo tt-smi -glx_reset'", "bash -c 'ipmitool chassis power off'"):
        assert deviceplan.power_refusal(c), c
    for c in ("broker reset box-a", "ipmitool power status", "ipmitool sdr", "tt-smi -s"):
        assert deviceplan.power_refusal(c) is None, c
    assert deviceplan.power_refusal("./scripts/cycle.sh box-a", ["broker", "./scripts/cycle.sh"]) is None
    assert deviceplan.risky("broker reset box-a") and not deviceplan.risky("git reset --hard")


def test_device_route_is_off_by_default_and_opens_the_device_keep_only_with_its_setting(env):
    from ttp import deviceplan, glmwork, rollout
    p = make(env)
    t = p.db.task(p.db.add_task("soak", "Run the soak on the box.", kind="work", tier="standard", priority=3))
    assert rollout.FLAGS[deviceplan.FLAG]["key"] == "route.glm_work.device" and rollout.level(deviceplan.FLAG) == "off"
    assert not deviceplan.settings({})["on"] and not deviceplan.on(_dp_cfg())
    assert glmwork.keeps_core(p.db, _dp_cfg(), t, "standard") == "device"
    assert glmwork.keeps_core(p.db, _dp_cfg(device=True), t, "standard") == ""
    cfg = _dp_cfg()
    assert deviceplan.gate(cfg, "alpha", {}) is cfg and not deviceplan.on(cfg), "no rollout: off"
    own = _dp_cfg(device=True)
    assert deviceplan.on(deviceplan.gate(own, "alpha", {"route": {"glm_work": {"device": True}}})), "its own word wins"


def test_a_plan_with_a_raw_power_step_or_needs_opus_goes_back_to_opus(tmp_path):
    from ttp import deviceplan
    run, _ = _dp_run(tmp_path)
    raw = _DP_PLAN.replace("broker reset box-a", "ipmitool chassis power cycle")
    assert "approved routes" in deviceplan.store(run, raw, str(tmp_path), {})
    assert deviceplan.plan(run) is None and json.loads((run / "glm_guard.json").read_text())["kind"] == "plan"
    run2 = tmp_path / "r2"
    run2.mkdir()
    assert deviceplan.store(run2, "needs_opus: the box is held by someone else", "", {})
    assert deviceplan.bounce_kind(run2) == "plan"


def test_a_glm_device_run_runs_reads_bookkeeping_and_plan_steps_and_checks_risky_ones(tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    ws = str(tmp_path)
    ref = lambda c, cfg=None: deviceplan.refusal(c, ws, e, cfg or {})
    assert ref("ssh box-a mkdir -p /srv/out").endswith(" first"), "no plan yet: take it first"
    assert ref("mkdir -p /srv/out").startswith("it writes outside"), "a local write outside: refused, plan or not"
    assert ref("ttp machines change add box-a 'drop-in' --undo 'rm it' --expires 2h"), "machine-ledger writes"
    for c in ("ipmitool sdr", "dmesg -c", "ttp machines change show 3", "ttp machines change add --help",
              "ttp obligation progress o1 'step 2 done'", "ttp note 'update: ok'", "ttp advise --plan 'x'"):
        assert ref(c) is None, c
    assert deviceplan.store(run, _DP_PLAN, ws, {}) is None
    got = deviceplan.plan(run)
    assert [s["n"] for s in got["steps"]] == [1, 2, 3] and len(got["sha256"]) == 64, \
        "a step is its place in the list, whatever n the plan wrote; prose is no step"
    gates = json.loads((run / "glm_run.json").read_text())["gates"]
    assert "broker reset box-a" in gates and "ssh box-a test -e /srv/out" in gates
    assert ref("ssh box-a mkdir -p /srv/out") is None and ref("ssh box-a rm -rf /srv/other").startswith("out of plan")
    assert ref("ssh box-a mkdir -p out") is None, "a relative path on the box is the run's own folder there"
    assert ref("rm -rf /srv/other").startswith("it writes outside")
    assert ref("./scripts/site-fan.sh box-a", {"route": {"glm_work": {"device_scripts": ["./scripts/site-fan.sh"]}}}) is None
    assert ref("broker reset box-a") == ("check: step 2 is a reset, power cycle, flash or firmware step: run "
                                         "`ttp advise --check-step 2` first")
    (run / "advisor.jsonl").write_text(json.dumps({"kind": "check", "step": 2, "verdict": "ok", "ts": time.time() - 5}) + "\n")
    assert ref("broker reset box-a") is None
    deviceplan.log_step(run, "ssh box-a mkdir -p /srv/out", 0, "")
    assert ref("broker reset box-a").endswith(" first"), "a change since the check needs a fresh one"
    assert ref("ipmitool chassis power cycle").startswith("power cycles and resets")
    assert deviceplan.hook_refusal("ssh box-a rm -rf /srv/other", ws, e)
    assert deviceplan.bounce_kind(run, "x") == "out_of_plan"
    assert deviceplan.refusal("rm -rf /srv/other", ws, {"TTP_RUN_DIR": str(tmp_path / "none")}) is None, "other runs"


def test_glm_device_runs_get_opus_audits_and_one_glm_own_harm_stops_the_route(env, tmp_path):
    from ttp import deviceplan, rollout
    p = make(env)
    cfg = _dp_cfg(device=True, device_audits=2)
    t = p.db.task(p.db.add_task("soak", "Run the soak on the box.", kind="work", tier="standard", priority=3))
    for i in range(3):
        (tmp_path / f"r{i}").mkdir()
        run, _ = _dp_run(tmp_path / f"r{i}")
        deviceplan.run_end(p.db, cfg, "alpha", t, i, run, "")
    audits = [x for x in p.db.q("SELECT * FROM tasks") if deviceplan.AUDIT_LABEL in str(x.get("labels") or "")]
    assert len(audits) == 2 and {a["model"] for a in audits} == {"opus"} and audits[0]["kind"] == "question"
    assert deviceplan.counters(p.db)["runs"] == 3 and deviceplan.judge(p, cfg, 0)["state"] == "wait"
    assert deviceplan.daily_line(p.db).startswith("GLM device route: 3 runs, bounce rate 0%, 0 audits")
    clean = tmp_path / "a0"
    clean.mkdir()
    (clean / "result.json").write_text(json.dumps({"device_audit": {"harmful": [
        {"step": "broker reset box-b", "source": "plan", "why": "held by another job"}]}}))
    deviceplan.run_end(p.db, cfg, "alpha", p.db.task(audits[0]["id"]), 9, clean)
    c = deviceplan.counters(p.db)
    assert (c["audits_done"], c["plan_harm"], c["glm_harm"], c["stopped"]) == (1, 1, 0, None)
    assert "alpha" not in rollout.rolled_back(deviceplan.FLAG)
    bad = tmp_path / "a1"
    bad.mkdir()
    (bad / "result.json").write_text(json.dumps({"device_audit": {"harmful": [
        {"step": "ipmitool raw 0x00", "source": "glm", "why": "raw bytes"}]}}))
    deviceplan.run_end(p.db, cfg, "alpha", p.db.task(audits[1]["id"]), 10, bad)
    assert deviceplan.counters(p.db)["stopped"] and "alpha" in rollout.rolled_back(deviceplan.FLAG)
    ev = p.db.q("SELECT kind, severity FROM events WHERE kind='glm_device_stopped'")
    assert [(r["kind"], r["severity"]) for r in ev] == [("glm_device_stopped", "high")]
    own = _dp_cfg(device=True)
    assert not deviceplan.on(deviceplan.gate(own, "alpha", {"route": {"glm_work": {"device": True}}})), \
        "the stop overrides even the project's own setting"
    assert deviceplan.judge(p, cfg, 0)["state"] == "worse"
    assert not rollout.stop_project(deviceplan.FLAG, "alpha", "again"), "once"
    assert deviceplan.daily_line(p.db).endswith("1 GLM-own harmful steps, 1 copied from the plan, stopped")


def test_device_runs_are_told_the_planner_rules_and_glm_runs_the_plan_route(env):
    from ttp import deviceplan
    assert deviceplan.daily_line(make(env).db) == "", "no device run: no daily line"
    assert deviceplan.prompt_lines(False) == ""
    assert "who holds it now" in deviceplan.prompt_lines(True) and "approved routes" in deviceplan.prompt_lines(True)
    assert "ttp advise --plan" in deviceplan.glm_lines() and "--check-step" in deviceplan.glm_lines()


def test_the_plan_prompt_asks_for_exact_check_and_complete_submit_commands():
    from ttp import deviceplan
    assert "runs exactly as written" in deviceplan.DEVICE_PLAN and "leave out any option you cannot confirm" in deviceplan.DEVICE_PLAN
    assert "--help" not in deviceplan.DEVICE_PLAN
    assert "job-submit step is the complete command line" in deviceplan.DEVICE_PLAN
    assert "`ttp machines change list <alias>`" in deviceplan.DEVICE_PLAN


def test_glm_stops_at_a_failed_check_step():
    from ttp import deviceplan
    assert "exits nonzero or prints an error has failed: stop there, run no later step" in deviceplan.glm_lines()


def _dp_planned_run(tmp_path, monkeypatch):
    """A marked plan run with a stored plan, in the environment the hook and the runner read."""
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert deviceplan.store(run, _DP_PLAN, str(ws), {}) is None
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.chdir(ws)
    return run, str(ws)


def test_the_hook_passes_device_reads_and_refuses_writes_outside_the_worktree_in_a_plan_run(tmp_path, monkeypatch):
    from ttp import hook
    run, ws = _dp_planned_run(tmp_path, monkeypatch)
    pre = lambda c: hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": ws})[0]
    for c in ("ipmitool sdr 2>&1 | tail -5", "ttp occupancy >&2", "tt-smi -s 2>/dev/null"):
        assert pre(c) is None, c
    for c in ("echo x > /tmp/f", "cmd 2> /tmp/f"):
        out = pre(c)
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", c
    assert (run / "glm_guard.json").exists(), "a refusal hands the run back to Opus"


def test_the_guarded_runner_passes_device_reads_and_refuses_writes_outside_the_worktree_in_a_plan_run(tmp_path, monkeypatch):
    from ttp import glmenv
    run, ws = _dp_planned_run(tmp_path, monkeypatch)
    ran: list = []

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            ran.append(action["command"])
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    reads = ["ipmitool sdr 2>&1 | tail -5", "ttp occupancy >&2", "tt-smi -s 2>/dev/null"]
    for c in reads:
        assert env.execute({"command": c}, ws)["returncode"] == 0, c
    for c in ("echo x > /tmp/f", "cmd 2> /tmp/f"):
        with pytest.raises(glmenv.Submitted):
            env.execute({"command": c}, ws)
    assert ran == reads, "a refused command never ran"


def test_a_program_merely_named_like_an_approved_route_is_not_that_route():
    from ttp import deviceplan
    assert deviceplan.power_refusal("ipmitool chassis power cycle", ["ipmi"]), "a name inside a program's name is no route"
    assert deviceplan.power_refusal("tt-smi -r 0", ["tt"]) and deviceplan.power_refusal("tt-smi -r 0", ["tt-smi"]) is None


def test_an_audit_that_ends_without_a_verdict_is_requeued_once_counted_and_cleared_by_a_later_verdict(env, tmp_path):
    from ttp import deviceplan
    p = make(env)
    cfg = _dp_cfg(device=True, device_audits=1)
    t = p.db.task(p.db.add_task("soak", "Run the soak on the box.", kind="work", tier="standard", priority=3))
    run, _ = _dp_run(tmp_path)
    deviceplan.run_end(p.db, cfg, "alpha", t, 1, run, "")
    audit = lambda: [x for x in p.db.q("SELECT * FROM tasks") if deviceplan.AUDIT_LABEL in str(x.get("labels") or "")]
    first = audit()[0]
    empty = tmp_path / "a0"
    empty.mkdir()
    (empty / "result.json").write_text(json.dumps({"summary": "done"}))
    deviceplan.run_end(p.db, cfg, "alpha", p.db.task(first["id"]), 2, empty)
    assert len(audit()) == 2 and deviceplan.counters(p.db)["audits_missing"] == 0, "requeued once"
    retry = [a for a in audit() if a["id"] != first["id"]][0]
    deviceplan.run_end(p.db, cfg, "alpha", p.db.task(retry["id"]), 3, empty)
    assert len(audit()) == 2 and deviceplan.counters(p.db)["audits_missing"] == 1
    assert [r["kind"] for r in p.db.q("SELECT kind FROM events WHERE source='deviceplan'")] == ["glm_device_audit_missing"]
    ok = tmp_path / "a1"
    ok.mkdir()
    (ok / "result.json").write_text(json.dumps({"device_audit": {"harmful": []}}))
    deviceplan.run_end(p.db, cfg, "alpha", p.db.task(retry["id"]), 4, ok)
    assert deviceplan.counters(p.db)["audits_clean"] == 1
    assert deviceplan.counters(p.db)["audits_missing"] == 0, "a later verdict of the same run settles it"
    assert deviceplan.judge(p, cfg, 0)["state"] != "wait", "settled: the route may leave 'wait'"


def test_a_plan_run_keeps_the_person_contact_guard_in_the_hook_and_the_runner(tmp_path, monkeypatch):
    """Mail, raw sockets, databases and routed notes are refused in a plan run, even when Opus's plan names them."""
    from ttp import glmenv, hook
    run, ws = _dp_planned_run(tmp_path, monkeypatch)
    named = ["mail -s x a@b", "sendmail x", "nc host 25", "ttp note --urg hi", "ttp note --to P hi",
             "ttp note --t=P hi", "ssh box-a ttp note --urgent x", "ssh box-a 'sendmail a@b < /dev/null'"]
    # a local command through an ssh option, before or after the host (reads on any host run)
    bad = named + ["ssh box-a -o ProxyCommand=sendmail true",
                   "ssh box-a -oProxyCommand=nc ls", "ssh -o ProxyCommand=sendmail box-a true",
                   "ssh -oLocalCommand=x -o PermitLocalCommand=yes box-a true", "ssh -o 'KnownHostsCommand x' box-a true"]
    plan = json.loads((run / "device_plan.json").read_text())
    plan["steps"] += [{"n": 9 + i, "host": "local", "command": c, "check": False} for i, c in enumerate(named)]
    (run / "device_plan.json").write_text(json.dumps(plan))
    pre = lambda c: hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": ws})[0]
    let = [c for c in bad if not ((out := pre(c)) and out["hookSpecificOutput"]["permissionDecision"] == "deny")]
    assert let == [], let
    for c in ("sqlite3 x.db", "ssh -J jump box-a ls", "ssh box-z ls", "ssh box-a -- ls", "ssh box-a -p 22 ls",
              "ttp obligation receipt o1 --evidence done"):
        assert pre(c) is None, c
    assert pre("ssh box-a test -e /srv/out") is None, "ssh to a host the plan's steps name, running a step"
    assert pre("ssh box-a ls -la /srv/out") is None, "flags after the remote command's first word are its own"

    ran: list = []

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            ran.append(action["command"])
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    for c in bad:   # refused, or an outward post kept as a draft for Opus
        try:
            assert env.execute({"command": c}, ws)["returncode"] != 0, c
        except glmenv.Submitted:
            pass
    assert ran == [], "none of them ran"


def test_a_plan_step_that_mails_or_routes_a_note_fails_the_plan_check(tmp_path):
    from ttp import deviceplan
    run, _ = _dp_run(tmp_path)
    for c in ("mail -s report ops@example.com < /dev/null", "ttp note --urgent done",
              "ssh box-a -o ProxyCommand=x ls", "ssh -oProxyCommand=x box-a ls", "ssh box-a -- ls",
              "ssh box-a -o ProxyCommand=x ls `true`"):
        plan = _DP_PLAN.replace("broker reset box-a", c)
        assert deviceplan.store(run, plan, str(tmp_path), {}), c
        assert deviceplan.plan(run) is None, c


def test_deviceplan_and_glmguard_read_ttp_note_flags_alike(tmp_path):
    """One reading of `ttp note` options (argparse prefixes too) in both guards."""
    from ttp import deviceplan, glmguard
    run, e = _dp_run(tmp_path)
    for c, routed in (("ttp note --urg hi", True), ("ttp note --to=P hi", True), ("ttp note --rep x hi", True),
                      ("ttp note --sev high hi", True), ("ttp note hi", False), ("ttp note 'update: ok'", False)):
        g = glmguard.shell_refusal(c, str(tmp_path), str(run), e)
        assert bool(g) is routed, c
        assert bool(deviceplan.refusal(c, str(tmp_path), e, {})) is routed, c


def test_scripts_and_routes_match_strictly(tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    assert deviceplan.store(run, _DP_PLAN, str(tmp_path), {}) is None
    scripts = {"route": {"glm_work": {"device_scripts": ["run.sh"], "power_routes": ["./scripts/"]}}}
    assert deviceplan.refusal("ssh box-a 'rm -rf /srv/x'", str(tmp_path), e, scripts).startswith("out of plan"), "a remote write"
    assert deviceplan.refusal("run.sh", str(tmp_path), e, scripts) is None
    # A redirect write is not the listed script's run: a local one is a local write, a quoted one a remote write.
    assert deviceplan.refusal("ssh box-a /srv/x/run.sh > /srv/out", str(tmp_path), e, scripts)
    for c in ("ssh box-a '/srv/x/run.sh > /srv/out'", "ssh box-a 'run.sh > /srv/out'", "ssh box-a 'run.sh>/srv/out'",
              "ssh box-a 'echo hi >> /srv/x/y' 2>&1"):
        assert not deviceplan._approved(c, [], ["run.sh"], str(tmp_path), str(run), e), c
        assert deviceplan.refusal(c, str(tmp_path), e, scripts).startswith("out of plan"), c
    for c in ("ssh box-a 'run.sh'", "ssh box-a 'run.sh 2>&1 >/dev/null'",
              """ssh box-a 'run.sh --x "a>b"'""", "ssh box-a 'run.sh 2>/dev/stderr'"):
        assert deviceplan._approved(c, [], ["run.sh"], str(tmp_path), str(run), e), c
    assert deviceplan.power_refusal("./scripts/../../usr/bin/ipmitool chassis power cycle", ["./scripts/"])
    assert deviceplan.power_refusal("ipmitool power cycle", ["./scripts/"])
    assert deviceplan.power_refusal("./scripts/cycle.sh box-a", ["./scripts/"]) is None
    assert deviceplan.step_match("git reset --hard", [{"command": "git reset --hard"}])
    assert not deviceplan.step_match("git -C /other/repo reset --hard", [{"command": "git reset --hard"}]), "a plan step matches exactly"


def _dp_asker(tmp_path, answers, seen):
    """A device plan run armed for `ttp advise --plan`, and a fake CLI giving `answers` in turn."""
    from ttp import deviceplan, glmassist
    run, _ = _dp_run(tmp_path)
    glmassist.arm(run, deviceplan.assist(None, {}, {"title": "reset box-a", "spec": "reset it"}))
    left = list(answers)

    def call(argv, text, timeout, cwd):
        seen.append(text)
        return {"result": left.pop(0), "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 5}}
    return run, call


_DP_BAD = _DP_PLAN.replace('"command": "mkdir -p /srv/out"', '"command": "echo "hi" > /srv/out"')


def test_a_plan_whose_json_does_not_parse_names_the_error_line_and_column():
    from ttp import deviceplan
    steps, why = deviceplan.parse_why(_DP_BAD)
    assert steps == [] and "not valid JSON" in why and "line 1 column" in why, why
    assert deviceplan.parse_why("no block")[1] == deviceplan.NO_BLOCK
    assert "steps" in deviceplan.parse_why('```json\n{"plan": []}\n```')[1]
    assert len(deviceplan.parse_why(_DP_PLAN)[0]) == 3 and deviceplan.parse_why(_DP_BAD)[0] == []


def test_opus_fixes_a_bad_plan_block_once_inside_the_plan_step_and_the_full_plan_is_kept(tmp_path):
    from ttp import deviceplan, glmassist
    seen = []
    long_plan = "x" * 5000 + "\n" + _DP_PLAN
    run, call = _dp_asker(tmp_path, [_DP_BAD, long_plan], seen)
    row = glmassist.ask(run, "reset box-a", cwd=str(tmp_path), plan=True, run=call, binary="claude")
    assert len(seen) == 2 and "line 1 column" in seen[1] and _DP_BAD in seen[1], "one fix-up turn with its error"
    assert row["ok"] and "not valid JSON" in row["plan_error"] and row["cost_usd"] == 1.0
    assert len(deviceplan.plan(run)["steps"]) == 3 and glmassist.planned(run)
    assert len(glmassist.calls(run)[-1]["answer"]) == 4000


def test_a_plan_still_bad_after_its_fix_up_fails_the_plan_step_and_glm_never_starts(tmp_path):
    from ttp import deviceplan, glmassist
    seen = []
    run, call = _dp_asker(tmp_path, [_DP_BAD, _DP_BAD], seen)
    row = glmassist.ask(run, "reset box-a", cwd=str(tmp_path), plan=True, run=call, binary="claude")
    assert len(seen) == 2 and not row["ok"] and row["handed_back"] and "line 1 column" in row["error"]
    guard = json.loads((run / "glm_guard.json").read_text())
    assert guard["kind"] == "plan" and "line 1 column" in guard["why"], guard
    assert deviceplan.plan(run) is None and not glmassist.planned(run)
    assert "line 1 column" in glmassist.calls(run)[-1]["error"]


def test_device_plan_reads_inline_code_as_text_and_a_route_verb_as_a_step():
    from ttp import deviceplan
    assert not deviceplan.risky("python3 -c \"print('reset')\"") and not deviceplan.risky("grep reset log")
    assert not deviceplan.risky("ipmitool power status") and deviceplan.risky("ipmitool chassis power cycle")
    assert deviceplan.risky("bash -c 'tt-smi -r 0'") and deviceplan.risky("broker reset box-a")


def test_the_post_run_scan_lists_writes_outside_that_were_no_plan_step(tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    assert deviceplan.store(run, _DP_PLAN, str(tmp_path), {}) is None
    for c in ("ls /srv", "ssh box-a mkdir -p /srv/out", "python3 -c \"open('/srv/y', 'w').write('x')\""):
        deviceplan.log_step(run, c, 0)
    got = deviceplan.outside_writes(run)
    assert [r["cmd"] for r in got] == ["python3 -c \"open('/srv/y', 'w').write('x')\""], got
    assert (run / deviceplan.OUTSIDE).exists()


def test_a_service_start_or_stop_runs_only_as_a_step_of_opus_plan(tmp_path):
    # starting or stopping a service on a shared box can take devices from others
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    ws = str(tmp_path)
    ref = lambda c: deviceplan.refusal(c, ws, e, {})
    assert ref("ssh box-a sudo systemctl restart svc-a").endswith(" first"), "no plan yet: take it first"
    plan = _DP_PLAN.replace('"command": "mkdir -p /srv/out"', '"command": "sudo systemctl restart svc-a"')
    assert deviceplan.store(run, plan, ws, {}) is None
    assert ref("ssh box-a sudo systemctl restart svc-a") is None, "a step of the plan"
    for c in ("ssh box-a sudo systemctl stop svc-b", "systemctl --user restart svc-a", "sudo service svc-a stop"):
        assert ref(c).startswith("out of plan: it changes a service's state"), c
    assert ref("ssh box-a systemctl status svc-a") is None, "a read"
    from ttp import glmguard
    sub = "out=$(sudo systemctl restart svc-a)"
    assert deviceplan._static(sub, deviceplan.settings({}), ws, run) is None and \
        glmguard.shell_refusal(sub, ws, str(run), e) is not None, "a plan step's $(...) service change"


def test_a_service_change_in_a_heredoc_over_ssh_runs_only_as_a_plan_step(tmp_path):
    import json
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    ws = str(tmp_path)
    ref = lambda c: deviceplan.refusal(c, ws, e, {})
    step = "ssh box-a bash -s <<'EOF'\nsudo systemctl restart svc-a\nEOF"
    other = "ssh box-a bash -s <<'EOF'\nsudo systemctl stop svc-b\nEOF"
    plan = _DP_PLAN.replace('{"host": "box-a", "command": "mkdir -p /srv/out", "check": false}',
                            '{"host": "local", "command": %s, "check": false}' % json.dumps(step))
    assert deviceplan.store(run, plan, ws, {}) is None
    assert ref(step) is None, "a step of the plan"
    assert "it changes a service's state" in (ref(other) or ""), other


def test_device_planner_is_told_to_publish_with_ttp_push_and_to_name_remote_scratch_from_its_task_id():
    """A plan stepped `git push` and failed its own check; another named its remote scratch folder with a made-up
    task number the GLM run then corrected, so the run went out of plan. The planner gets the task id."""
    from ttp import deviceplan
    assert "never `git push`" in deviceplan.DEVICE_PLAN and "named from the task id" in deviceplan.DEVICE_PLAN
    assert deviceplan.assist(None, {}, {"id": 42, "title": "stage a venv", "spec": "on box-a"})["task"].startswith(
        "task id 42\nstage a venv")
    assert deviceplan.assist(None, {}, {"title": "t", "spec": "s"})["task"] == "t\ns"
    assert "paths and names exactly" in deviceplan.glm_lines()


def test_glm_device_bounce_rule_counts_only_runs_after_the_projects_latest_re_arm(env, tmp_path, monkeypatch):
    """Rolled back on mostly-bounced runs from before a fix, a re-armed project waits for 10 new runs; the
    harmful-step rule stays immediate."""
    from ttp import deviceplan, rollout
    p = make(env)
    cfg = _dp_cfg(device=True, device_audits=2)
    t = p.db.task(p.db.add_task("soak", "Run the soak on the box.", kind="work", tier="standard", priority=3))
    clock = [1_000_000.0]

    def tick():
        clock[0] += 60
        return clock[0]
    monkeypatch.setattr(time, "time", tick)

    def runs(bounces):
        for b in bounces:
            n = int(clock[0])
            (tmp_path / f"r{n}").mkdir()
            run, _ = _dp_run(tmp_path / f"r{n}")
            deviceplan.run_end(p.db, cfg, p.name, t, n, run, b)
    runs(["x"] * 6 + [""])
    assert rollout.stop_project(deviceplan.FLAG, p.name, "most went back") and "re-armed" in rollout.arm(
        deviceplan.FLAG, project=p.name)
    runs(["", "", ""])
    v = deviceplan.judge(p, cfg, clock[0])
    assert v["state"] == "wait", ("7 old runs, 6 bounced, then a re-arm and 3 clean runs", v)
    runs(["x"] * 6 + [""])
    assert deviceplan.judge(p, cfg, clock[0])["state"] == "worse", "10 runs after the arm, 6 bounced"

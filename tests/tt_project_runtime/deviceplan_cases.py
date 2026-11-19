# Device route tests (deviceplan.py: Opus plans, GLM runs only that plan) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
# Add new deviceplan tests here.
from __future__ import annotations

_DP_MODEL = "example-org/work-model"
_DP_PLAN = """1. Check who holds box-a. 2. Reset it through the broker.
```json
{"steps": [{"n": 1, "host": "box-a", "command": "mkdir -p /srv/out", "check": false},
           {"n": 2, "host": "local", "command": "broker reset box-a", "check": true},
           {"n": 4, "host": "box-a", "command": "test -e /srv/out", "check": true},
           {"n": 3, "host": "local", "command": "Check the holder first.", "check": false}]}
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
    for c in ("ls > /srv/x", "ls 2> /srv/x", "ssh box-a 'ls > /srv/x'", "dmesg -c", "tt-smi -r 0"):
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
    assert ref("mkdir -p /srv/out").endswith(" first"), "no plan yet: take it first"
    for c in ("ipmitool sdr", "ttp machines change add box-a 'drop-in' --undo 'rm it' --expires 2h",
              "ttp obligation progress o1 'step 2 done'", "ttp note 'update: ok'", "ttp advise --plan 'x'"):
        assert ref(c) is None, c
    assert deviceplan.store(run, _DP_PLAN, ws, {}) is None
    got = deviceplan.plan(run)
    assert [s["n"] for s in got["steps"]] == [1, 2, 4] and len(got["sha256"]) == 64, "prose is no step"
    gates = json.loads((run / "glm_run.json").read_text())["gates"]
    assert "broker reset box-a" in gates and "ssh box-a test -e /srv/out" in gates
    assert ref("ssh box-a mkdir -p /srv/out") is None and ref("mkdir -p /srv/out").startswith("out of plan"), "on its host"
    assert ref("rm -rf /srv/other").startswith("out of plan")
    assert ref("./scripts/site-fan.sh box-a", {"route": {"glm_work": {"device_scripts": ["./scripts/site-fan.sh"]}}}) is None
    assert ref("broker reset box-a") == ("check: step 2 is a reset, power cycle, flash or firmware step: run "
                                         "`ttp advise --check-step 2` first")
    (run / "advisor.jsonl").write_text(json.dumps({"kind": "check", "step": 2, "verdict": "ok", "ts": time.time() - 5}) + "\n")
    assert ref("broker reset box-a") is None
    deviceplan.log_step(run, "ssh box-a mkdir -p /srv/out", 0, "")
    assert ref("broker reset box-a").endswith(" first"), "a change since the check needs a fresh one"
    assert ref("ipmitool chassis power cycle").startswith("power cycles and resets")
    assert deviceplan.hook_refusal("rm -rf /srv/other", ws, e)
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
    assert deviceplan._routed(["notbroker", "reset"], ["broker"]) is False
    assert deviceplan._routed(["/opt/bin/broker", "reset"], ["broker"]) is False, "a bare route names no path"
    assert deviceplan._routed(["broker", "reset"], ["broker"]) is True
    assert deviceplan._routed(["./scripts/cycle.sh", "box-a"], ["./scripts/"]) is True
    assert deviceplan._routed(["./scripts-evil/cycle.sh"], ["./scripts"]) is False


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
    named = ["mail -s x a@b", "sendmail x", "nc host 25", "sqlite3 x.db", "ttp note --urg hi", "ttp note --to P hi",
             "ttp note --t=P hi", "ssh box-a ttp note --urgent x", "ssh box-a 'sendmail a@b < /dev/null'"]
    # a host no step names, a jump, and a local command through an ssh option, before or after the host
    bad = named + ["ssh -J jump box-a ls", "ssh box-z ls", "ssh box-a -o ProxyCommand=sendmail true",
                   "ssh box-a -oProxyCommand=nc ls", "ssh -o ProxyCommand=sendmail box-a true",
                   "ssh -oLocalCommand=x -o PermitLocalCommand=yes box-a true", "ssh box-a -- ls", "ssh box-a -p 22 ls",
                   "ssh -o 'KnownHostsCommand x' box-a true", "ttp obligation receipt o1 --evidence done"]
    plan = json.loads((run / "device_plan.json").read_text())
    plan["steps"] += [{"n": 9 + i, "host": "local", "command": c, "check": False} for i, c in enumerate(named)]
    (run / "device_plan.json").write_text(json.dumps(plan))
    pre = lambda c: hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": ws})[0]
    for c in bad:
        out = pre(c)
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", c
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
    for c in ("mail -s report ops@example.com < /dev/null", "ttp note --urgent done", "sqlite3 x.db 'select 1'",
              "ssh box-a -o ProxyCommand=x ls", "ssh -oProxyCommand=x box-a ls", "ssh box-a -- ls"):
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
        assert deviceplan._free(c.split()[:2] + c.split()[2:]) is not routed, c


def test_scripts_and_routes_match_strictly(tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    assert deviceplan.store(run, _DP_PLAN, str(tmp_path), {}) is None
    scripts = {"route": {"glm_work": {"device_scripts": ["run.sh"], "power_routes": ["./scripts/"]}}}
    assert deviceplan.refusal("/tmp/x/run.sh", str(tmp_path), e, scripts).startswith("out of plan"), "bare name, other path"
    assert deviceplan.refusal("run.sh", str(tmp_path), e, scripts) is None
    assert deviceplan.power_refusal("./scripts/../../usr/bin/ipmitool chassis power cycle", ["./scripts/"])
    assert deviceplan.power_refusal("ipmitool power cycle", ["./scripts/"])
    assert deviceplan.power_refusal("./scripts/cycle.sh box-a", ["./scripts/"]) is None

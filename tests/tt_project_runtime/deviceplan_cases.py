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



# Replay of a device plan that ran a cluster validator, its all-link traffic pass included, straight on the box's
# chips (paths, box and checkout made generic): only the box's device broker runs a device tool.
_DP_VALIDATOR = ("./build/tools/scaleout/cluster_validation/run_cluster_validation --cabling-descriptor-path "
                 "/path/to/cabling.textproto --global-descriptor-path /path/to/global.yaml")
_DP_DIRECT_STEPS = [f"cd wt/work && {_DP_VALIDATOR} 2>&1 | tee /tmp/validator.log",
                    "echo \"exit: ${PIPESTATUS[0]:-$?}\"; grep -Ei 'link|mismatch|error' /tmp/validator.log | head -50",
                    f"{_DP_VALIDATOR} --send-traffic 2>&1 | tee /tmp/validator_traffic.log"]
_DP_VIA_BROKER = f"ttp lock box-a -- ssh box-a 'broker submit box-a -- {_DP_VALIDATOR} --send-traffic'"


def _dp_steps_plan(cmds):
    return "Validate the links.\n```json\n" + json.dumps(
        {"steps": [{"host": "local", "command": c, "check": False} for c in cmds]}) + "\n```"


def test_a_plan_running_a_device_tool_outside_the_boxs_broker_goes_back_to_opus(tmp_path):
    """cells: plan-10103-traffic-refused: replay: the advisor planned the validator, then its traffic pass, as local
    steps on the box; nothing stopped them. Each is refused by its step, the read between them and the broker's job pass."""
    from ttp import deviceplan
    run, _ = _dp_run(tmp_path)
    why = deviceplan.store(run, _dp_steps_plan(_DP_DIRECT_STEPS), str(tmp_path), {})
    assert why.startswith("step 1: a direct device tool") and "run it through the box's device broker" in why
    assert deviceplan.plan(run) is None and deviceplan.bounce_kind(run) == "plan"
    run2 = tmp_path / "r2"
    run2.mkdir()
    why = deviceplan.store(run2, _dp_steps_plan([_DP_VIA_BROKER, *_DP_DIRECT_STEPS[1:]]), str(tmp_path), {})
    assert why.startswith("step 3: a direct device tool"), "the traffic pass is refused by itself"
    run3 = tmp_path / "r3"
    run3.mkdir()
    help_step = "cd wt && " + _DP_VALIDATOR.split()[0] + " --help"   # prints usage, opens no device
    assert deviceplan.store(run3, _dp_steps_plan([help_step, _DP_VIA_BROKER, _DP_DIRECT_STEPS[1]]), str(tmp_path),
                            {}) is None
    assert "device jobs (validation, fabric or link checks, resets, tt-smi) go only through the broker" in \
        deviceplan.DEVICE_PLAN


def test_a_device_tool_runs_only_as_the_brokers_job_in_every_form():
    """cells: runtime-direct-tool-refused, broker-submit-allowed, device-tool-help-not-refused: a tool called with only
    --help or -h prints its usage (a replayed advisor step); any other direct call is refused."""
    from ttp import deviceplan
    v = _DP_VALIDATOR
    tool = v.split()[0]
    for c in (v, f"ttp lock box-a -- {v}", f"ssh box-a '{v} --send-traffic'", f"bash -c '{v}'", f"sudo {v}",
              f"timeout 600 {v}", f"env A=1 {v}", f"nohup sh -c '{v}' &", f"echo `{v}`", f"xargs -a list {v}",
              "python3 tools/test_system_health.py", "/opt/x/test_tt_fabric --gtest_filter='*'",
              f"other-cli run-bg box-a '{v}'", f"{tool} --help --send-traffic", f"{tool} -h; {tool}",
              f"other-cli run-bg box-a '{tool} --help; {tool}'"):
        assert "run it through the box's device broker" in deviceplan.direct_tool(c), c
    for c in (f"broker submit box-a -- {v}", _DP_VIA_BROKER, "grep run_cluster_validation /tmp/v.log",
              "ls build/tools/scaleout/cluster_validation/run_cluster_validation", _DP_DIRECT_STEPS[1],
              'git commit -m "add the run_cluster_validation step"', "tt-smi -s", f"{tool} --help 2>&1 | head -50",
              f"cd wt && {tool} -h > /tmp/usage.txt", f"ssh box-a '{tool} --help'",
              "python3 tools/test_system_health.py -h"):
        assert deviceplan.direct_tool(c) is None, c
    s = deviceplan.settings(_dp_cfg(device_tools=["fabric_check"], power_routes=["./bin/devq"]))
    assert deviceplan.direct_tool("python3 tools/fabric_check.py", s["power_routes"], s["device_tools"])
    assert deviceplan.direct_tool(f"./bin/devq submit -- python3 tools/fabric_check.py", s["power_routes"],
                                  s["device_tools"]) is None, "a project names its own broker and tools"
    assert deviceplan.direct_tool(f"broker submit box-a -- {v}", s["power_routes"], s["device_tools"]) is None


def test_a_glm_device_run_never_runs_a_device_tool_outside_the_broker_even_as_a_plan_step(tmp_path):
    """A plan stored before the plan check refused it still lists the direct validator: the run is refused and goes
    back to Opus; the broker's job and the reads run, and resets, power cycles, flashes and reboots still stop."""
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    ws = str(tmp_path)
    (run / deviceplan.PLAN_FILE).write_text(json.dumps({"steps": [
        {"n": i + 1, "host": "local", "command": c, "check": False}
        for i, c in enumerate([*_DP_DIRECT_STEPS, _DP_VIA_BROKER])]}))
    for c in _DP_DIRECT_STEPS[::2]:
        assert "run it through the box's device broker" in deviceplan.refusal(c, ws, e, {}), c
    for c in (_DP_DIRECT_STEPS[1], _DP_VIA_BROKER, "tt-smi -s", _DP_VALIDATOR.split()[0] + " --help"):
        assert deviceplan.refusal(c, ws, e, {}) is None, c
    for c in ("tt-smi -r 0", "ipmitool chassis power cycle", "tt-flash --fw-tar fw.tar.gz", "sudo reboot"):
        assert deviceplan.refusal(c, ws, e, {}), c
    assert deviceplan.hook_refusal(_DP_DIRECT_STEPS[2], ws, e)
    assert deviceplan.bounce_kind(run) == "guard", "a harmful refusal: the run is handed back to Opus"


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
        assert "Instead:" in out["hookSpecificOutput"]["permissionDecisionReason"], c
    assert not (run / "glm_guard.json").exists(), "a scratch write is a redirect: the run goes on"
    pre("cmd > /tmp/g")
    assert (run / "glm_guard.json").exists(), "the third refusal in one run hands it back to Opus"


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
        got = env.execute({"command": c}, ws)
        assert got["returncode"] == 2 and "Instead:" in got["output"], c
    with pytest.raises(glmenv.Submitted):
        env.execute({"command": "cmd > /tmp/g"}, ws)
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


def test_device_planner_is_told_to_publish_with_ttp_push_and_to_name_remote_scratch_from_its_task_id(monkeypatch):
    """A plan stepped `git push` and failed its own check; another named its remote scratch folder with a made-up
    task number the GLM run then corrected, so the run went out of plan. A third stepped a read over ssh to the host
    it ran on, whose alias did not resolve there, and its gate failed. The planner gets the task id and its host."""
    from ttp import deviceplan, machines
    monkeypatch.setattr(machines, "here", lambda known=None: ("box-a", {}))
    assert "never `git push`" in deviceplan.DEVICE_PLAN and "scratch folder given with the task" in deviceplan.DEVICE_PLAN
    assert deviceplan.assist(None, {}, {"id": 42, "title": "stage a venv", "spec": "on box-a"})["task"].startswith(
        "task id 42, scratch folder /tmp/ttp-42 on each other host\nthis host is box-a: a step on it runs locally, "
        "with host local and no ssh\nstage a venv")
    monkeypatch.setattr(machines, "here", lambda known=None: None)
    monkeypatch.setenv("TTP_HOST", "box1")
    assert "this host is box1:" in deviceplan.assist(None, {}, {"id": 42, "title": "t", "spec": "s"})["task"]
    assert deviceplan.assist(None, {}, {"title": "t", "spec": "s"})["task"] == "t\ns"
    assert "paths and names exactly" in deviceplan.glm_lines()


# Two GLM device runs went back to Opus for making their own scratch folder on the box, and a third stopped when its
# plan named the folder with another task's number. Hosts and numbers made generic.
_DP_SCRATCH_RUNS = {
    3910: "ttp lock box-a -- ssh box-a 'mkdir -p /tmp/ttp-3910 && tt-device-mcp run-bg -w /tmp/ttp-3910 -e /dev/null "
          "\"tt-smi -s >/dev/null\"' 2>&1; echo \"---RC=$?\"",
    3918: "ttp lock box-a -- ssh box-a \"mkdir -p /tmp/ttp-3918 && python3 -m venv --without-pip /tmp/ttp-3918/venv && "
          "printf 'PYTHON_ENV_DIR: /tmp/ttp-3918/venv\\n' > /tmp/ttp-3918/env.yaml && ls -ld /tmp/ttp-3918\" 2>&1",
}


def _dp_scratch_plan(remote=True, folder="/tmp/ttb3916"):
    """A plan with a broker call on the project host and, with `remote`, a step on box-a naming `folder`."""
    import json
    block = {"steps": [{"host": "local", "command": "broker reset box-a", "check": True}]}
    if remote:
        block["steps"].insert(0, {"host": "box-a", "command": f"ls {folder}", "check": False})
    return "Stage it on box-a.\n```json\n" + json.dumps(block) + "\n```"


def test_a_glm_device_run_writes_inside_the_plans_scratch_folder_on_its_host_and_nowhere_else(tmp_path):
    from ttp import deviceplan
    for tid, cmd in _DP_SCRATCH_RUNS.items():
        (tmp_path / str(tid)).mkdir()
        run, e = _dp_run(tmp_path / str(tid))
        ws = str(tmp_path)
        assert deviceplan.store(run, _dp_scratch_plan(), ws, {}) is None
        assert (deviceplan.refusal(cmd, ws, e, {}) or "").startswith("out of plan"), "no folder for the run: hands back"
        assert deviceplan.store(run, _dp_scratch_plan(), ws, {}, task=f"task id {tid}, x\nstage") is None
        assert deviceplan.plan(run)["scratch"] == {"box-a": f"/tmp/ttp-{tid}"}, "the run's own id, not the plan's"
        assert deviceplan.refusal(cmd, ws, e, {}) is None, cmd
        deviceplan.log_step(run, cmd, 0)
        assert deviceplan.outside_writes(run) == [], "inside the folder is no write outside"
        for c in (f"ssh box-a mkdir -p /tmp/other-{tid}", f"ssh box-a 'cp /tmp/ttp-{tid}/env.yaml /etc/'",
                  f"ssh box-a 'mv /etc/hosts /tmp/ttp-{tid}/'", f"ssh box-a 'ln -s / /tmp/ttp-{tid}/root'",
                  f"ssh box-a 'rm -rf /tmp/ttp-{tid}/../x'", f"ssh box-b mkdir -p /tmp/ttp-{tid}/x",
                  f"ssh box-a 'sudo mkdir /tmp/ttp-{tid}/x'", f"ssh box-a 'ssh box-b mkdir /tmp/ttp-{tid}/x'",
                  f"ssh box-a 'mkdir /tmp/ttp-{tid}/x; echo x > /tmp/y'", f"mkdir -p /tmp/ttp-{tid}/x"):
            assert deviceplan.refusal(c, ws, e, {}), c


def test_the_harness_sets_each_hosts_scratch_folder_from_the_runs_own_task_id():
    from ttp import deviceplan
    steps = lambda plan: deviceplan.parse_why(plan)[0]
    task = deviceplan.assist(None, {}, {"id": 3918, "title": "stage a venv", "spec": "on box-a"})["task"]
    for folder in ("/tmp/ttb3916", "/tmp/ttp-3916", "/tmp/ttp-<task id>", "/srv/out"):
        assert deviceplan.scratch_folders(steps(_dp_scratch_plan(folder=folder)), task) == {"box-a": "/tmp/ttp-3918"}
    assert deviceplan.scratch_folders(steps(_dp_scratch_plan(remote=False)), task) == {}, "no other host: no folder"
    assert deviceplan.scratch_folders(steps(_dp_scratch_plan()), "stage a venv") == {}, "no id: no folder"
    via = '```json\n{"steps": [{"host": "local", "command": "ssh box-b ls", "check": false}]}\n```'
    assert deviceplan.scratch_folders(steps(via), task) == {"box-b": "/tmp/ttp-3918"}, "an ssh call reaches its host"
    assert "scratch folder" in deviceplan.glm_lines()


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


def test_glm_device_harm_rule_counts_only_harms_after_the_projects_latest_re_arm_and_a_new_one_stops_it_again(
        env, tmp_path, monkeypatch):
    """A harmful step that rolled the route back never counts against it once re-armed: new runs are audited
    afresh, the judge waits for them, and a new harmful step stops the route at once again."""
    from ttp import deviceplan, rollout
    p = make(env)
    cfg = _dp_cfg(device=True, device_audits=2)
    t = p.db.task(p.db.add_task("soak", "Run the soak on the box.", kind="work", tier="standard", priority=3))
    clock = [1_000_000.0]

    def tick():
        clock[0] += 60
        return clock[0]
    monkeypatch.setattr(time, "time", tick)

    def run_and_audit(harmful):
        n = int(clock[0])
        (tmp_path / f"r{n}").mkdir()
        run, _ = _dp_run(tmp_path / f"r{n}")
        deviceplan.run_end(p.db, cfg, p.name, t, n, run, "")
        audit = [x for x in p.db.q("SELECT * FROM tasks WHERE status!='done'")
                 if deviceplan.AUDIT_LABEL in str(x.get("labels") or "")]
        assert len(audit) == 1, "each armed stretch audits its first runs afresh"
        (tmp_path / f"a{n}").mkdir()
        (tmp_path / f"a{n}" / "result.json").write_text(json.dumps({"device_audit": {"harmful": harmful}}))
        deviceplan.run_end(p.db, cfg, p.name, p.db.task(audit[0]["id"]), n + 1, tmp_path / f"a{n}")
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (audit[0]["id"],))
    harm = [{"step": "ipmitool raw 0x00", "source": "glm", "why": "raw bytes"}]
    run_and_audit(harm)
    assert p.name in rollout.rolled_back(deviceplan.FLAG) and deviceplan.judge(p, cfg, clock[0])["state"] == "worse"
    assert "re-armed" in rollout.arm(deviceplan.FLAG, project=p.name)
    assert deviceplan.judge(p, cfg, clock[0])["state"] == "wait", "the harm before the re-arm no longer counts"
    run_and_audit([])
    assert deviceplan.judge(p, cfg, clock[0])["why"] == "1 of 2 clean audited runs", "counted since the re-arm"
    run_and_audit(harm)
    assert p.name in rollout.rolled_back(deviceplan.FLAG), "a new harmful step stops the re-armed route at once"
    assert deviceplan.judge(p, cfg, clock[0])["state"] == "worse"
    assert len(p.db.q("SELECT id FROM events WHERE kind='glm_device_stopped'")) == 2


# Sourced device plans: replays of real plans with the box, paths and ports made generic. Three wrong commands
# (a health check on a TCP port the broker does not serve, push steps, a wrong submit option) and the steps
# that ran fine.
_SRC_STATUS = 'ttp lock box1 -- ssh box1 "tt-device-mcp status 10"'
_SRC_HEALTH = 'ttp lock box1 -- ssh box1 "curl -s -m 10 --unix-socket /run/broker/broker.sock http://localhost/health"'
_SRC_SUBMIT = ("ttp lock box1 -- ssh box1 \"tt-device-mcp run-bg -w /tmp/ttb101 -e /tmp/ttb101/env.yaml -t 120 "
               "'bash /tmp/ttb101/hb.sh'\"")


def _src_project(tmp_path, runs_done=1):
    """A project tree with a passing done run (101), a done run with a failed step (100), a failed run (102), a
    device script, and the current plan run (103) armed for `ttp advise --plan`."""
    from ttp import deviceplan, glmassist, glmguard
    root = tmp_path / "proj"
    runs = root / "tt-project" / "state" / "runs"
    script = root / "tt-project" / "harness" / "bin" / "box-check"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)

    def run(n, status, rows, steps):
        d = runs / str(n)
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"status": status}))
        (d / deviceplan.STEPS).write_text("".join(json.dumps(r) + "\n" for r in rows))
        (d / deviceplan.PLAN_FILE).write_text(json.dumps({"steps": [
            {"n": i + 1, "host": "local", "command": c, "check": True} for i, c in enumerate(steps)]}))

    ok = lambda c: {"cmd": c, "rc": None, "tail": "ok"}
    run(100, "done", [ok(_SRC_STATUS), {"cmd": 'ttp lock box1 -- ssh box1 "curl -s -m 10 http://127.0.0.1:9000/health"',
                                        "rc": 7, "tail": "Exit code 7\ncurl: (7) Connection refused"}],
        ['ttp lock box1 -- ssh box1 "curl -s -m 10 http://127.0.0.1:9000/health"'])
    run(101, "done", [ok(_SRC_STATUS), ok(_SRC_HEALTH), ok(_SRC_SUBMIT), ok("git ls-remote origin"),
                      ok(f"{script} box1"), ok('ttp lock box1 -- ssh box1 "tt-device-mcp status -j 77"')],
        [_SRC_STATUS, "tt-device-mcp status -j <job-id>", "git ls-remote origin", "git push origin HEAD"])
    run(102, "failed", [ok('ttp lock box1 -- ssh box1 "tt-device-mcp reset 0"')],
        ['ttp lock box1 -- ssh box1 "tt-device-mcp reset 0"'])
    cur = runs / "103"
    cur.mkdir()
    glmguard.mark_run(cur, True, plan=True)
    glmassist.arm(cur, deviceplan.assist(None, {}, {"id": 103, "title": "heartbeat on box1", "spec": "run it"}))
    return cur, script


def _src_plan(*steps):
    return "Plan.\n```json\n" + json.dumps({"steps": [
        {"host": "local", "command": c, "check": True, "source": src} for c, src in steps]}) + "\n```"


def test_the_device_planner_gets_the_commands_that_passed_in_the_projects_recent_done_runs(tmp_path):
    from ttp import deviceplan
    cur, script = _src_project(tmp_path)
    srcs = deviceplan.copy_sources(cur, {"device_scripts": ["bin/flash-check"]})
    cmds = [x["cmd"] for x in srcs if "cmd" in x]
    assert srcs[0] == {"script": "bin/flash-check", "src": "the project's device scripts"}
    assert {"script": str(script), "src": "run 101"} in srcs, srcs
    assert {_SRC_STATUS, _SRC_HEALTH, _SRC_SUBMIT, "tt-device-mcp status -j <job-id>"} <= set(cmds), cmds
    assert cmds.index(_SRC_STATUS) < cmds.index(_SRC_HEALTH), "newest first, one copy of each"
    assert cmds.count(_SRC_STATUS) == 1 and all("9000" not in c for c in cmds), "a failed step is no source"
    assert "git ls-remote origin" in cmds, "a read-only git step is a source"
    assert all("push" not in c and "reset" not in c for c in cmds), "no push step; a failed run is no source"
    block = deviceplan.source_block(srcs)
    assert block.startswith("--- commands to copy from") and f"- `{_SRC_HEALTH}` (run 101)" in block
    assert deviceplan.source_block([]) == "" and deviceplan.copy_sources(tmp_path / "none" / "1", {}) == []
    many = [{"cmd": f"echo {i}", "rc": 0} for i in range(200)]
    (cur.parent / "101" / deviceplan.STEPS).write_text("".join(json.dumps(r | {"cmd": f'ssh box1 "echo {r["cmd"]}"'})
                                                               + "\n" for r in many))
    capped = deviceplan.copy_sources(cur, {})
    assert len(capped) <= deviceplan.SOURCE_LINES and len(deviceplan.source_block(capped)) < 2 * deviceplan.SOURCE_BYTES


def test_replayed_wrong_device_commands_are_flagged_and_the_steps_that_ran_fine_pass(tmp_path):
    """Before: none of these steps failed the plan check (its guards are the denylist). After: each wrong command
    goes back to the planner and each step copied from a passing run passes."""
    from ttp import deviceplan
    cur, script = _src_project(tmp_path)
    srcs = deviceplan.copy_sources(cur, {})
    task = deviceplan.assist(None, {}, {"id": 103, "title": "heartbeat on box1", "spec": "run it"})["task"]
    wrong = {
        "wrong broker port": 'ttp lock box1 -- ssh box1 "curl -fsS -m 10 http://127.0.0.1:9000/health"',
        "audit why with no arguments": "ttp audit why",
        "audit why with a serves it does not take": 'ttp audit why "measure the box" --serves "task #101 read-only '
                                                    'measurement" --undo "none: read-only"',
        "unknown ttp option": "ttp occupancy --wait --step test --nope 1800 box1",
        "bare number that is not the task's": 'ttp lock box1 -- ssh box1 "tt-device-mcp status 5"',
        "push script": "/srv/proj/tt-project/harness/bin/push-handoff",
        "pr edit": "ttp gh-queue run -- pr edit 270 --body-file -",
        "git push": "cd /srv/wt && git commit -am x && git push origin HEAD",
        "marked unsourced": _SRC_STATUS,
    }
    good = [_SRC_STATUS, _SRC_HEALTH, "ttp machines change list box1", "ttp occupancy --wait --step test box1",
            'ttp audit why "read the hold records and run one job" --serves 103 --undo "none: read-only"',
            'git ls-remote https://example.com/org/repo refs/heads/work | grep -q "$(git -C /srv/wt rev-parse HEAD)"',
            "git -C /srv/repo fetch origin work && git -C /srv/wt diff --quiet HEAD",
            _SRC_SUBMIT.replace("ttb101", "ttb103"), f"{script} box1",
            'timeout 900 ttp lock box1 -- ssh box1 "sudo -n tt-device-mcp status -j <job-id> 2>&1"',
            'ttp lock box1 -- ssh box1 "rm -rf /tmp/ttb103; if [ -e /tmp/ttb103 ]; then echo left; else echo gone; fi"']
    one = lambda c, src="run 101": [{"n": 1, "command": c, "check": True, "source": src}]
    flagged = [k for k, c in wrong.items()
               if deviceplan.unsourced(one(c, "unsourced: no run used it" if k == "marked unsourced" else "run 101"),
                                       srcs, task, cur.parent.parent.parent.parent)]
    assert flagged == list(wrong), flagged
    assert "pushes or publishes" in deviceplan.unsourced(one(wrong["pr edit"]), srcs, task)
    passed = [c for c in good if not deviceplan.unsourced(one(c), srcs, task, cur.parent.parent.parent.parent)]
    assert passed == good, [c for c in good if c not in passed]
    assert not deviceplan.unsourced([{"n": 1, "command": wrong["wrong broker port"], "check": False}], srcs, task), \
        "a read step that is no check or submit needs no source"
    assert not deviceplan.unsourced(one(wrong["wrong broker port"]), [], task), "a project with no sources: no source check"
    assert deviceplan.unsourced(one(wrong["git push"]), [], task), "a push step fails everywhere"
    for c in (wrong["push script"], wrong["pr edit"], "git push", "gh pr create", "ttp push --land 3"):
        assert deviceplan.publish_step(c), c
    for c in ("git status", "tt-device-mcp status 10", "ttp machines change list box1", "echo pushed"):
        assert not deviceplan.publish_step(c), c
    named = deviceplan.unsourced(one(wrong["bare number that is not the task's"]), srcs, task + " read 5 lines", None)
    assert not named, "a number the task names may differ from the source"


def test_the_source_window_counts_done_device_runs_not_every_run_folder(tmp_path):
    """A busy project: 70 newer runs that are no device run, and 70 newer device runs that did not end done."""
    from ttp import deviceplan
    cur, _ = _src_project(tmp_path)
    for n in range(200, 270):
        (cur.parent / str(n)).mkdir()
        (cur.parent / str(n) / "result.json").write_text(json.dumps({"status": "done"}))
    for n in range(300, 370):
        (cur.parent / str(n)).mkdir()
        (cur.parent / str(n) / "result.json").write_text(json.dumps({"status": "failed"}))
        (cur.parent / str(n) / deviceplan.STEPS).write_text(json.dumps({"cmd": "tt-device-mcp reset 0", "rc": 0}) + "\n")
    cmds = [x["cmd"] for x in deviceplan.copy_sources(cur, {}) if "cmd" in x]
    assert _SRC_HEALTH in cmds and all("reset" not in c for c in cmds), cmds
    assert deviceplan.unsourced([{"n": 1, "command": 'ttp lock box1 -- ssh box1 "curl -fsS http://127.0.0.1:9000/health"',
                                  "check": True}], deviceplan.copy_sources(cur, {}), "", None), "the check is not open"


# Today's pilot and device-run plans, replayed with a placeholder box, repo and paths (the plans of the read-only
# device runs of a project that runs the device route).
_REPLAY_READ_ONLY_GIT = [
    "git ls-remote https://example.com/org/repo refs/heads/work",
    'git ls-remote https://example.com/org/repo refs/heads/work | grep -q "$(git -C /srv/wt rev-parse HEAD)"',
]
_REPLAY_PUBLISH = [
    'sha=$(git rev-parse HEAD) && remote=$(git ls-remote https://example.com/org/repo refs/heads/work) && '
    'if [ "$sha" = "$remote" ]; then echo pushed; else /srv/bin/mac-push-handoff 101 /srv/wt /srv/repo work "$sha"; fi',
    "if [ -d /srv/state/mac-push/101 ]; then /srv/bin/gh-push-direct --task 101; else echo none; fi",
    "python3 -I /srv/state/mac-push/101/patch_body.py && QIN=/tmp/q ttp gh-queue run -- pr edit 270 -R org/repo "
    "--body-file -",
]


def test_replayed_read_only_git_checks_are_not_flagged_and_publish_steps_always_are(tmp_path):
    """Before: every git check was flagged (copy_sources dropped git, `_needs` required a source for it)."""
    from ttp import deviceplan
    cur, _ = _src_project(tmp_path)
    srcs = deviceplan.copy_sources(cur, {})
    for c in _REPLAY_READ_ONLY_GIT:
        assert not deviceplan.unsourced([{"n": 1, "command": c, "check": True}], srcs, "", None), c
    for c in _REPLAY_PUBLISH:
        assert "pushes or publishes" in deviceplan.unsourced([{"n": 1, "command": c, "check": True}], srcs, "", None), c
    wt = "git -C /srv/repo worktree add --detach /srv/wt origin/work"
    assert deviceplan.unsourced([{"n": 1, "command": wt, "check": True}], srcs, "", None), "a git step that writes needs a source"


def test_replayed_ttp_steps_are_read_by_the_ttp_cli_not_exempt(tmp_path):
    """Before: every ttp step passed. After: a bare `audit why` (pilot runs: rc 2), a `--serves` it refuses (rc 2)
    and an unknown option go back; the audit why that ran fine passes."""
    from ttp import deviceplan
    cur, _ = _src_project(tmp_path)
    srcs = deviceplan.copy_sources(cur, {})
    one = lambda c: deviceplan.unsourced([{"n": 1, "command": c, "check": False}], srcs, "", None)
    assert "say why" in one("ttp audit why")
    assert "--serves" in one('ttp audit why "measure" --serves "task #101 read-only measurement" --undo "none: reads"')
    assert "unrecognized arguments: --nope" in one("ttp occupancy --nope box1")
    assert "--undo" in one('ttp audit why "measure" --serves task101')
    assert not one('ttp audit why "measure and run one job" --serves 101 --undo "none: read-only plus scratch removal"')
    assert not one("ttp machines change list box1; ttp machines activity box1")
    assert not one("ttp occupancy --wait --step test --timeout 1800 box1")
    assert not one("ttp audit why <why> --serves <ref> --undo <how>"), "a <placeholder> call is not judged"


def test_store_hands_back_a_device_plan_still_unsourced(tmp_path):
    from ttp import deviceplan, glmassist
    cur, _ = _src_project(tmp_path)
    ws = str(tmp_path / "ws")
    bad = _src_plan(('ttp lock box1 -- ssh box1 "curl -fsS -m 10 http://127.0.0.1:9000/health"', "memory"))
    why = deviceplan.store(cur, bad, ws, {})
    assert why and "matches no listed command" in why, why
    guard = json.loads((cur / "glm_guard.json").read_text())
    assert guard["kind"] == "plan" and "9000" in guard["why"] and deviceplan.plan(cur) is None
    assert deviceplan.store(cur, _src_plan((_SRC_HEALTH, "run 101"), (_SRC_STATUS, "run 101")), ws, {}) is None
    assert [s["source"] for s in deviceplan.plan(cur)["steps"]] == ["run 101", "run 101"]


def test_replayed_bare_bookkeeping_plan_step_is_not_offered_to_copy_and_the_full_call_passes(tmp_path):
    """Replay: an earlier done run's plan had a bare `ttp audit why` step; its log holds the full call that ran
    (with `--serves` and `--undo`), which contains the bare one, so the copy list offered `ttp audit why`. The next
    plan copied it into a read step and the plan check refused it ("say why"), ending the run. Before: the bare call
    was listed. After: no ttp call the plan check refuses is listed, and the step written as the full call passes."""
    from ttp import deviceplan
    cur, _ = _src_project(tmp_path)
    d = cur.parent / "98"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"status": "done"}))
    full = 'ttp audit why "read the backstop rules" --serves 98 --undo "none: read-only"'
    rows = [{"cmd": 'ttp audit why 2>&1; echo "---"; ttp advise --plan "read the backstop rules"', "rc": None,
             "tail": "say why: ttp audit why ..."},
            {"cmd": 'ttp audit why "x" --serves "a read" --undo "none"', "rc": None,
             "tail": "ttp: --serves 'a read': name an obligation, a task or a message"},
            {"cmd": full, "rc": None, "tail": "recorded"}]
    (d / deviceplan.STEPS).write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / deviceplan.PLAN_FILE).write_text(json.dumps({"steps": [
        {"n": 1, "host": "local", "command": "ttp audit why", "check": True}]}))
    cmds = [x["cmd"] for x in deviceplan.copy_sources(cur, {}) if "cmd" in x]
    assert "ttp audit why" not in cmds and _SRC_HEALTH in cmds, cmds
    reads = "; ttp said power cycle backstop; ttp memory proj --search backstop; grep -n backstop /srv/notes | head -40"
    one = lambda c: [{"n": 2, "command": c, "check": False, "source": "run 98"}]
    srcs = deviceplan.copy_sources(cur, {})
    assert "say why" in deviceplan.unsourced(one("ttp audit why" + reads), srcs), "a bare call still goes back"
    assert not deviceplan.unsourced(one(full.replace("98", "103") + reads), srcs)
    ws = str(tmp_path)
    plan = _src_plan((_SRC_HEALTH, "run 101"), (full.replace("98", "103") + reads, "run 98"))
    assert deviceplan.store(cur, plan, ws, {}) is None and not (cur / "glm_guard.json").exists()


def test_replayed_gate_cut_off_by_the_tool_time_limit_runs_once_more_and_a_failed_gate_still_stops(
        tmp_path, monkeypatch):
    """Replay: `ttp checks 2>&1 | tail -30` ran past the agent tool's 2-minute limit, which killed it (exit 143,
    "Command timed out after 2m 0s"); the guard counted that as a failed gate and ended the run. Before: the first
    cut-off stopped the run. After: the first cut-off of `ttp checks` goes on (the run is told to rerun it with a longer timeout), a
    second of the same command stops it, and a gate that ran and failed, or another gate cut off (a device check on a
    box that may hang), stops it at once."""
    from ttp import deviceplan, glmguard, hook
    run, _ = _dp_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    cut = "Exit code 143\nCommand timed out after 2m 0s"
    post = lambda c, err: hook._gate_stop({"tool_name": "Bash", "tool_input": {"command": c}, "error": err}, str(run))
    gate = "ttp checks 2>&1 | tail -30"
    assert post(gate, cut) == "" and not (run / "glm_guard.json").exists(), "a cut-off is no failed check"
    assert "time limit twice" in post(gate, cut)
    assert json.loads((run / "glm_guard.json").read_text())["kind"] == glmguard.GATE
    for c, err in ((gate, "Exit code 1\nFAILED tests/test_x.py::test_y"),
                   ("ttp checks", "Exit code 143\nterminated by the checks' own stop"),
                   ("ttp checks", "Exit code 1\nCommand timed out after 2m 0s"),
                   ("broker status box-a", cut)):
        (run / "glm_guard.json").unlink(missing_ok=True)
        assert "a gate failed" in post(c, err), (c, err)
    assert "longer tool timeout; cut off again, it hands back" in deviceplan.glm_lines()


def test_a_gate_cut_off_that_cannot_record_its_retry_stops_the_run_at_once(tmp_path, monkeypatch):
    """Replay: a symlink planted at the cut-off record made every cut-off read as the first one:
    durable_append refuses a symlink and returns False, which the guard took as a recorded retry, so
    the run never stopped. After: a cut-off whose retry cannot be recorded stops the run at once."""
    from ttp import glmguard, hook
    run, _ = _dp_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    (tmp_path / "cut-off record").write_text("")
    (run / glmguard.GATE_TIMEOUTS).symlink_to(tmp_path / "cut-off record")   # never appended to
    cut = "Exit code 143\nCommand timed out after 2m 0s"
    post = lambda c, err: hook._gate_stop({"tool_name": "Bash", "tool_input": {"command": c}, "error": err}, str(run))
    for _ in range(2):   # never a first retry: each cut-off stops the run
        assert "cut off by the tool's time limit" in post("ttp checks 2>&1 | tail -30", cut)
        assert json.loads((run / "glm_guard.json").read_text())["kind"] == glmguard.GATE
    assert (run / glmguard.GATE_TIMEOUTS).is_symlink(), "nothing was written through the symlink"


def _src_asker(tmp_path, answers, seen):
    cur, _ = _src_project(tmp_path)
    left = list(answers)

    def call(argv, text, timeout, cwd):
        seen.append(text)
        return {"result": left.pop(0), "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 5}}
    return cur, call


def test_an_unsourced_device_plan_goes_back_to_the_planner_once_with_why_and_the_sources(tmp_path):
    from ttp import deviceplan, glmassist
    seen = []
    bad = _src_plan(('ttp lock box1 -- ssh box1 "curl -fsS -m 10 http://127.0.0.1:9000/health"', "memory"))
    good = _src_plan((_SRC_HEALTH, "run 101"))
    cur, call = _src_asker(tmp_path, [bad, good], seen)
    row = glmassist.ask(cur, "check box1", cwd=str(tmp_path), plan=True, run=call, binary="claude")
    assert "--- commands to copy from" in seen[0] and _SRC_HEALTH in seen[0], "the planner gets the sources"
    assert "copy every check and job-submit command verbatim" in seen[0].lower()
    assert len(seen) == 2 and "fails its source check" in seen[1] and "9000" in seen[1] and _SRC_HEALTH in seen[1]
    assert row["ok"] and "9000" in row["source_error"] and deviceplan.plan(cur) and glmassist.planned(cur)


def test_a_re_plan_whose_json_breaks_gets_the_json_fix_up_too_and_reaches_the_run(tmp_path):
    """Replay: the source re-plan came back with a broken JSON block (a key missing its opening quote) and the plan
    step failed with no device step run. Its block now gets the same one JSON fix-up as the first plan."""
    from ttp import deviceplan, glmassist
    seen = []
    bad = _src_plan(('ttp lock box1 -- ssh box1 "curl -fsS -m 10 http://127.0.0.1:9000/health"', "memory"))
    good = _src_plan((_SRC_HEALTH, "run 101"))
    broken = good.replace('{"host"', '{\n     host"', 1)
    assert deviceplan.parse_why(broken)[1] and not deviceplan.parse_why(good)[1]
    cur, call = _src_asker(tmp_path, [bad, broken, good], seen)
    row = glmassist.ask(cur, "check box1", cwd=str(tmp_path), plan=True, run=call, binary="claude")
    assert len(seen) == 3 and "fails its source check" in seen[1] and broken in seen[2]
    assert row["ok"] and row["source_error"] and row["plan_error"] and not row.get("handed_back")
    assert deviceplan.plan(cur) and glmassist.planned(cur) and not (cur / "glm_guard.json").exists()


def test_a_re_plan_whose_json_fix_up_has_no_time_left_still_books_its_cost(tmp_path):
    from ttp import deviceplan, glmassist
    seen, ticks = [], iter([0, 0, 0] + [10 ** 6] * 9)
    bad = _src_plan(('ttp lock box1 -- ssh box1 "curl -fsS -m 10 http://127.0.0.1:9000/health"', "memory"))
    broken = _src_plan((_SRC_HEALTH, "run 101")).replace('{"host"', '{\n     host"', 1)
    cur, call = _src_asker(tmp_path, [bad, broken], seen)
    row = glmassist.ask(cur, "check box1", cwd=str(tmp_path), plan=True, run=call, binary="claude",
                        now=lambda: next(ticks))
    assert len(seen) == 2 and not row["ok"] and "no time is left" in row["error"]
    assert row["cost_usd"] == 1.0, "both calls are paid for"


def test_a_planned_job_may_use_the_runs_own_scratch_folder_and_nothing_else_may_differ(tmp_path):
    """Replay: a plan copied a passing run's job submit into its own scratch folder and the source check stopped it.
    Only the folder may differ: the command, broker verb, limit and script (its name too) stay as the source's."""
    from ttp import deviceplan
    cur, _ = _src_project(tmp_path)
    srcs = deviceplan.copy_sources(cur, {})
    task = deviceplan.assist(None, {}, {"id": 103, "title": "heartbeat on box1", "spec": "run it"})["task"]
    own = _SRC_SUBMIT.replace("/tmp/ttb101", "/tmp/ttp-103")
    renamed = own.replace("hb.sh", "hb2.sh")
    write = 'ttp lock box1 -- ssh box1 "printf \'cat hb\\n\' > /tmp/ttp-103/hb2.sh"'
    plan = lambda *cs: [{"n": i + 1, "command": c, "check": "printf" not in c, "source": "run 101"}
                        for i, c in enumerate(cs)]
    in_tmp = _SRC_SUBMIT.replace("/tmp/ttb101/hb.sh", "/tmp/hb.sh")   # a source running a file directly in /tmp
    srcs = srcs + [{"cmd": in_tmp}, {"cmd": _SRC_SUBMIT.replace("/tmp/ttb101", "/var/tmp/ttb101")},
                   {"cmd": "srun python3 /tmp/bench.py"}, {"cmd": "srun python3 /tmp/tt99/run.py"}]
    assert not deviceplan.unsourced(plan(own), srcs, task)
    assert not deviceplan.unsourced(plan(in_tmp.replace("/tmp/ttb101", "/tmp/ttp-103")), srcs, task)
    assert not deviceplan.unsourced(plan("srun python3 /tmp/ttp-103/run.py"), srcs, task), \
        "a source /tmp folder used as a folder still swaps to the run's own scratch folder"
    assert deviceplan.unsourced(plan("srun python3 /tmp/ttp-103"), srcs, task), \
        "the bare scratch folder the task names replaces no source word: _scratch_word alone decides swaps"
    still = {
        "the bare scratch folder for a file in /tmp": plan(own.replace("/tmp/ttp-103/hb.sh", "/tmp/ttp-103")),
        "renamed script the plan never writes": plan(renamed),
        "renamed script written with new content": plan(write, renamed),
        "another broker verb": plan(own.replace("run-bg", "reset")),
        "another time limit": plan(own.replace("-t 120", "-t 7200")),
        "more in the job's command": plan(own.replace("hb.sh'", "hb.sh; tt-smi -r'")),
        "another task's scratch folder": plan(_SRC_SUBMIT.replace("/tmp/ttb101", "/tmp/ttp-99")),
        "the same name in another folder": plan(_SRC_SUBMIT.replace("/tmp/ttb101", "/var/tmp/ttp-103")),
    }
    flagged = [k for k, st in still.items() if deviceplan.unsourced(st, srcs, task)]
    assert flagged == list(still), flagged
    assert deviceplan.unsourced(plan(own.replace("-t 120", "-t no")), srcs, task), \
        "the host line's words are no values a step may use"


def test_a_device_plan_still_unsourced_after_its_re_plan_never_reaches_the_run(tmp_path):
    from ttp import deviceplan, glmassist
    seen = []
    bad = _src_plan(("tt-device-mcp wait <job-id>", "unsourced: no passing run waits on a job"))
    cur, call = _src_asker(tmp_path, [bad, bad], seen)
    row = glmassist.ask(cur, "check box1", cwd=str(tmp_path), plan=True, run=call, binary="claude")
    assert len(seen) == 2 and not row["ok"] and row["handed_back"] and "marked unsourced" in row["error"]
    assert json.loads((cur / "glm_guard.json").read_text())["kind"] == "plan"
    assert deviceplan.plan(cur) is None and not glmassist.planned(cur)


# A host-side loop reading each chip's attributes with no plan, and other direct device reads
_DP_SYS_LOOP = ('ls /sys/class/tenstorrent/ | head -40; echo ---; for d in /sys/class/tenstorrent/tenstorrent*; do'
                ' c=$(cat "$d/chip_id" 2>/dev/null); hb=$(timeout 10 cat "$d/tt_heartbeat" 2>&1); echo "$d $c $hb";'
                ' done 2>&1 | head -40')
_DP_DEVICE_READS = [_DP_SYS_LOOP, "cat /sys/class/tenstorrent/tenstorrent0/tt_heartbeat",
                    "head -c4 < /sys/class/tenstorrent/tenstorrent0/chip_id", "cd /sys/class/tenstorrent && cat x/chip_id",
                    "d=/dev/tenstorrent/0; xxd $d", "ssh box-a 'cat /sys/class/tenstorrent/tenstorrent0/chip_id'",
                    "python3 -c 'open(\"/dev/tenstorrent/0\").read()'", "echo `cat /sys/class/tenstorrent/x/chip_id`",
                    "ls -d /sys/class/tenstorrent/*/chip_id | xargs cat", "cat $(ls -d /sys/class/tenstorrent/*/chip_id)",
                    "echo /dev/tenstorrent/0 | xargs xxd", "ls /dev/tenstorrent/ |& xargs xxd",
                    "cat <(ls /sys/class/tenstorrent/*/chip_id)", "ls /dev/tenstorrent/ | wc -l",
                    "{ ls /sys/class/tenstorrent; } | xargs cat",
                    "for d in /sys/class/tenstorrent/*; do echo $d/chip_id; done | xargs cat",
                    "while read d; do cat $d; done < <(ls -d /sys/class/tenstorrent/*/chip_id)",
                    "bash -c 'ls /sys/class/tenstorrent/*/chip_id | xargs cat'",
                    "ls -d /sys/class/tenstorrent/*/chip_id > f; xargs cat < f", "ls /dev/tenstorrent >> f && xxd $(cat f)"]


@pytest.mark.parametrize("cmd", _DP_DEVICE_READS)
def test_a_direct_device_read_with_no_plan_is_out_of_plan_and_hands_back(cmd, tmp_path):
    from ttp import deviceplan, glmguard
    run, e = _dp_run(tmp_path)
    why = deviceplan.refusal(cmd, str(tmp_path), e, {})
    assert why and glmguard.classify(why)[1] == glmguard.HARMFUL, why
    deviceplan.hook_refusal(cmd, str(tmp_path), e)
    assert (run / glmguard.HIT).exists(), "never a redirect: the read itself can bring the host down"


@pytest.mark.parametrize("cmd", ["ls /sys/class/tenstorrent/", "ls -l /dev/tenstorrent/",
                                 "git commit -m 'read /sys/class/tenstorrent attributes'",
                                 "ls /sys/class/tenstorrent && echo ok", "echo 'a | b /dev/tenstorrent/0'",
                                 "ls /sys/class/tenstorrent/ 2>/dev/null || true", "ls /dev/tenstorrent; ps aux | grep x",
                                 'echo "$(date) /dev/tenstorrent"', "ls /dev/tenstorrent && echo ok || echo no",
                                 "{ ls /sys/class/tenstorrent; } || true",
                                 "ls /dev/tenstorrent >/dev/null 2>&1 && echo ok", "ls /dev/tenstorrent 2>err >&2"])
def test_listing_or_naming_device_files_is_no_device_read(cmd, tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    assert deviceplan.refusal(cmd, str(tmp_path), e, {}) is None


def test_a_device_read_runs_as_a_step_of_opus_plan_and_only_that_step(tmp_path):
    from ttp import deviceplan
    run, e = _dp_run(tmp_path)
    step = "cat /sys/class/tenstorrent/tenstorrent0/tt_heartbeat"
    (run / deviceplan.PLAN_FILE).write_text(json.dumps({"steps": [{"n": 1, "command": step}]}))
    assert deviceplan.refusal(step, str(tmp_path), e, {}) is None
    assert deviceplan.refusal("cat /sys/class/tenstorrent/tenstorrent1/chip_id", str(tmp_path), e, {})


@pytest.mark.parametrize("tool,args", [("Bash", {"command": _DP_SYS_LOOP}),
                                       ("Read", {"file_path": "/sys/class/tenstorrent/tenstorrent0/chip_id"}),
                                       ("Grep", {"pattern": ".", "path": "/dev/tenstorrent/0"})])
def test_the_hook_hands_back_a_device_read_with_no_plan_from_any_tool(tool, args, tmp_path, monkeypatch):
    from ttp import glmguard, hook
    run, e = _dp_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    out = hook._pre_tool_use({"tool_name": tool, "tool_input": args, "cwd": str(tmp_path)})[0]
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (run / glmguard.HIT).exists()


def test_the_guarded_runner_hands_back_a_device_read_with_no_plan(tmp_path, monkeypatch):
    from ttp import glmenv
    run, e = _dp_run(tmp_path)
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    ran: list = []

    class Base:
        def execute(self, action, cwd="", *, timeout=None):
            ran.append(action["command"])
            return {"output": "ok", "returncode": 0}
    if glmenv.LocalEnvironment is not object:
        pytest.skip("minisweagent is importable here: its own environment class is the base")
    env = type("Env", (glmenv.GuardedEnvironment, Base), {})()
    with pytest.raises(glmenv.Submitted):
        env.execute({"command": _DP_SYS_LOOP}, str(tmp_path))
    assert ran == []

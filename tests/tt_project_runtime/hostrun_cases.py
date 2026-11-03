# `ttp host-run` tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new host-run route tests here, not there.
from __future__ import annotations


def _hostrun_setup(env, monkeypatch, allow, by="user"):
    from ttp import configwho
    p = make(env)
    for k in ("TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK", "TTP_TASK_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    p.set_config("runner.host_commands", allow, by=configwho.by(by))
    return p


def _hostrun_with_daemon(p, argv, served, **kw):
    """Run a worker's request while a stand-in daemon serves the queue in another thread."""
    from ttp import hostrun
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            served.append(hostrun.serve(p, wait=True))
            time.sleep(0.05)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        return hostrun.request(p, argv, wait_s=20, **kw)
    finally:
        stop.set()
        t.join(5)


def test_host_run_runs_an_allowed_command_and_returns_its_output_and_exit_code(env, monkeypatch, capfd):
    from ttp import hostrun
    cmd = [sys.executable, "-c", "import sys; print('reading 42'); sys.stderr.write('warn\\n'); sys.exit(3)"]
    p = _hostrun_setup(env, monkeypatch, [shlex.join(cmd)])
    monkeypatch.setenv("TTP_TASK", "77")
    served: list[int] = []
    assert _hostrun_with_daemon(p, cmd, served) == 3
    out, err = capfd.readouterr()
    assert "reading 42" in out and "warn" in err
    assert sum(served) == 1
    rows = [json.loads(l) for l in (p.state / hostrun.LOG).read_text().splitlines()]
    assert rows[-1]["task"] == "77" and rows[-1]["rc"] == 3 and rows[-1]["why"] == "ran" and rows[-1]["argv"] == cmd
    assert not list((p.state / hostrun.QUEUE).iterdir()), "request, claim and answer are all gone"


@pytest.mark.parametrize("argv", [
    ["/bin/echo", "hi"],                                   # not listed at all
    ["/usr/bin/env", "/bin/true"],                         # a listed entry's word, but only a later one
    ["/bin/true", "x"],                                    # listed `/bin/true -v` needs its -v next
    ["true", "-v"],                                        # the bare name is not the listed path
    ["/bin/true -v"],                                      # one word that only looks like the entry
    [],
])
def test_host_run_refuses_a_command_off_the_allowlist_client_and_daemon_side(env, monkeypatch, capfd, argv):
    from ttp import hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/true -v", "/usr/bin/id -u"])
    assert hostrun.request(p, argv) == hostrun.REFUSED
    assert "refused" in capfd.readouterr().err
    assert not (p.state / hostrun.QUEUE).exists(), "a refused command is never queued"
    # A request written straight into the queue (past the client) is refused by the daemon too.
    q = p.state / hostrun.QUEUE
    q.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(16)
    (q / f"{rid}.json").write_text(json.dumps({"argv": argv, "task": "9", "allow": [argv]}))
    hostrun.serve(p, wait=True)
    ans = json.loads((q / f"{rid}.out.json").read_text())
    assert ans["rc"] == hostrun.REFUSED and ans["stdout"] == ""
    assert json.loads((p.state / hostrun.LOG).read_text().splitlines()[-1])["why"] == "refused"


def test_host_run_allows_an_entry_with_more_words_after_it(env, monkeypatch, capfd):
    from ttp import hostrun
    p = _hostrun_setup(env, monkeypatch, [shlex.join([sys.executable, "-c"])])
    assert _hostrun_with_daemon(p, [sys.executable, "-c", "print('ok')"], []) == 0
    assert "ok" in capfd.readouterr().out


def test_host_run_times_out_a_command_past_the_config_limit(env, monkeypatch, capfd):
    from ttp import hostrun
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    p = _hostrun_setup(env, monkeypatch, [shlex.join(cmd)])
    p.set_config("runner.host_command_timeout_s", 1)
    assert _hostrun_with_daemon(p, cmd, []) == hostrun.TIMED_OUT
    assert "timed out after 1 s" in capfd.readouterr().err


def test_host_run_without_a_daemon_answer_withdraws_the_request(env, monkeypatch, capfd):
    from ttp import hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"])
    assert hostrun.request(p, ["/bin/true"], wait_s=0.3) == hostrun.NO_ANSWER
    assert "did not run" in capfd.readouterr().err
    assert not list((p.state / hostrun.QUEUE).glob("*.json"))


def test_host_run_drops_a_request_its_client_gave_up_on_long_ago(env, monkeypatch, tmp_path):
    from ttp import hostrun
    p = _hostrun_setup(env, monkeypatch, [shlex.join(["/usr/bin/touch", str(tmp_path / "ran")])])
    q = p.state / hostrun.QUEUE
    q.mkdir(parents=True)
    rid = secrets.token_hex(16)
    (q / f"{rid}.json").write_text(json.dumps({"argv": ["/usr/bin/touch", str(tmp_path / "ran")],
                                               "queued": time.time() - 3600}))
    assert hostrun.serve(p, wait=True) == 0
    assert not (tmp_path / "ran").exists() and not list(q.iterdir())


def test_a_worker_cannot_change_the_host_run_allowlist(env, monkeypatch, capfd):
    from ttp import cli, configwho, hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"])
    monkeypatch.setenv("TTP_RUN_ID", "5")
    monkeypatch.setenv("TTP_TASK", "12")
    for key, value in (("runner.host_commands", '["/bin/sh"]'), ("runner", '{"host_commands": ["/bin/sh"]}')):
        with pytest.raises(SystemExit):
            cli.main(["config", "demo", key, value])
        assert "never by a worker" in capfd.readouterr().err
    assert p.config()["runner"]["host_commands"] == ["/bin/true"]
    # The coordinator (a run with no task) may set it.
    monkeypatch.delenv("TTP_TASK")
    cli.main(["config", "demo", "runner.host_commands", '["/bin/true", "/usr/bin/id -u"]'])
    assert hostrun.allowlist(p)[0] == [["/bin/true"], ["/usr/bin/id", "-u"]]
    # A list a worker wrote anyway (provenance says worker) runs nothing.
    p.set_config("runner.host_commands", ["/bin/sh"], by=configwho.by("worker", task="12"))
    allow, why = hostrun.allowlist(p)
    assert allow == [] and "set by a worker" in why
    assert hostrun.request(p, ["/bin/sh"]) == hostrun.REFUSED


@pytest.mark.parametrize("error, hinted", [
    ("sudo: The \"no new privileges\" flag is set, which prevents sudo from running as root.", True),
    ("sudo: /etc/sudo.conf is owned by uid 65534, should be 0", True),
    ("sudo: a password is required", False), ("", False)])
def test_sudo_failing_in_the_sandbox_names_the_host_run_route(monkeypatch, error, hinted):
    from ttp import hook
    monkeypatch.delenv("TTP_RUN_DIR", raising=False)
    out, _ = hook.post_tool_use_failure({"tool_name": "Bash", "error": error})
    if not hinted:
        assert out is None
        return
    assert "ttp host-run --" in out["hookSpecificOutput"]["additionalContext"]


def test_host_run_covers_a_read_only_sudo_netns_bmc_query_and_refuses_its_other_verbs(env, monkeypatch, capfd, tmp_path):
    # The shape a project allows for a read-only BMC query: sudo, then a network namespace, then
    # ipmitool reads. A stand-in `sudo` records its argv, so no real privilege is needed.
    from ttp import hostrun
    sudo = tmp_path / "sudo"
    sudo.write_text(f"#!{sys.executable}\nimport sys\nprint('ran:', ' '.join(sys.argv[1:]))\n")
    sudo.chmod(0o755)
    reads = [[str(sudo), "-n", "ip", "netns", "exec", "mgmt", "ipmitool", *verb] for verb in (["mc", "info"], ["sel", "info"])]
    p = _hostrun_setup(env, monkeypatch, [shlex.join(r) for r in reads])
    for r in reads:
        assert _hostrun_with_daemon(p, r, []) == 0
        assert "ran: -n ip netns exec mgmt ipmitool " + " ".join(r[-2:]) in capfd.readouterr().out
    for other in (["chassis", "power", "cycle"], ["mc", "reset", "cold"], ["sel", "clear"]):
        assert hostrun.request(p, reads[0][:-2] + other) == hostrun.REFUSED
        assert "refused" in capfd.readouterr().err
    assert hostrun.request(p, [str(sudo), "-n", "ip", "netns", "exec", "other", "ipmitool", "mc", "info"]) == hostrun.REFUSED


def test_host_run_plain_entry_keeps_the_600_s_cap_and_a_dict_entry_sets_its_own(env, monkeypatch):
    from ttp import hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/plain", {"cmd": "/bin/power-cycle --host a", "timeout_s": 900},
                                          {"cmd": "/bin/power-cycle"}])
    p.set_config("runner.host_command_timeout_s", 5000)
    ents, why = hostrun.entries(p)
    assert why == "" and ents == [(["/bin/plain"], None), (["/bin/power-cycle", "--host", "a"], 900),
                                  (["/bin/power-cycle"], None)]
    assert hostrun.limit_for(p, ["/bin/plain", "x"], ents) == hostrun.MAX_TIMEOUT_S == 600
    assert hostrun.limit_for(p, ["/bin/power-cycle", "--host", "a"], ents) == 900   # the longest entry
    assert hostrun.limit_for(p, ["/bin/power-cycle", "--host", "b"], ents) == 600
    p.set_config("runner.host_command_timeout_s", None)
    assert hostrun.limit_for(p, ["/bin/plain"], hostrun.entries(p)[0]) == hostrun.DEFAULT_TIMEOUT_S
    for v in (float("nan"), float("inf"), -0.0, 3600.5):
        assert hostrun.entry({"cmd": "/bin/power-cycle", "timeout_s": v})[2], v
    assert hostrun.entry({"cmd": "/bin/power-cycle", "timeout_s": 3600})[1] == 3600
    assert hostrun.asked(900, 30) == (30, "")
    lim, note = hostrun.asked(900, 1200)
    assert lim == 900 and "over this command's limit; using its 900 s" in note


@pytest.mark.parametrize("bad, says", [
    ({"cmd": "/bin/power-cycle", "timeout_s": 3601}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": 0}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": -5}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": "900"}, "must be a number"),
    ({"cmd": "/bin/power-cycle", "timeout_s": True}, "must be a number"),
    ({"cmd": "/bin/power-cycle", "timeout": 900}, "unknown key timeout"),
    ({"cmd": ["/bin/power-cycle"]}, "must be a string"),
    ({"timeout_s": 900}, "must be a string"),
    ("'/bin/power-cycle", "does not split"),
    ("", "is empty"),
    (17, "must be a string"),
])
def test_host_run_refuses_an_over_cap_or_malformed_entry_and_names_it(env, monkeypatch, capfd, bad, says):
    from ttp import cli, hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/true", bad])
    ents, why = hostrun.entries(p)
    assert ents == [(["/bin/true"], None)], "the bad entry is refused, never clamped; the good one stays"
    assert "entry 2 " in why and says in why and json.dumps(bad)[:20] in why
    assert hostrun.request(p, ["/bin/power-cycle"]) == hostrun.REFUSED
    assert "entry 2 " in capfd.readouterr().err
    # `ttp config` refuses it at once, naming it, and keeps the old list.
    with pytest.raises(SystemExit):
        cli.main(["config", "demo", "runner.host_commands", json.dumps(["/bin/id", bad])])
    assert "entry 2 " in capfd.readouterr().err
    assert p.config()["runner"]["host_commands"] == ["/bin/true", bad]


def test_host_run_a_dict_entry_limit_above_600_s_is_honoured(env, monkeypatch, capfd):
    # Scaled clock: the plain cap is cut to 1 s, the entry allows 3; a 2 s command passes only through
    # the entry's own limit, and the same command as a plain entry times out at the cap.
    from ttp import hostrun
    monkeypatch.setattr(hostrun, "MAX_TIMEOUT_S", 1)
    cmd = [sys.executable, "-c", "import time; time.sleep(2); print('cycled')"]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(cmd), "timeout_s": 3}])
    p.set_config("runner.host_command_timeout_s", 600)
    assert _hostrun_with_daemon(p, cmd, []) == 0
    assert "cycled" in capfd.readouterr().out
    p.set_config("runner.host_commands", [shlex.join(cmd)])
    assert _hostrun_with_daemon(p, cmd, []) == hostrun.TIMED_OUT
    assert "timed out after 1 s" in capfd.readouterr().err
    # A long limit run synchronously is pointed at --detach.
    p.set_config("runner.host_commands", [{"cmd": "/bin/true", "timeout_s": 900}])
    hostrun.request(p, ["/bin/true"], wait_s=0.1)
    assert "ttp host-run --detach" in capfd.readouterr().err


def test_host_run_detached_writes_its_rc_file_survives_its_caller_and_polls_running_then_the_rc(env, monkeypatch, capfd, tmp_path):
    from ttp import hostrun
    gate = tmp_path / "go"
    script = f"import os, time, sys\nwhile not os.path.exists({str(gate)!r}): time.sleep(0.05)\nprint('cycled'); sys.exit(5)"
    cmd = [sys.executable, "-c", script]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(cmd), "timeout_s": 900}])
    q = p.state / hostrun.QUEUE
    # The caller is a process of its own that queues the run and exits; a stand-in daemon serves it.
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            hostrun.serve(p, wait=True)
            time.sleep(0.05)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        runtime = str(pathlib.Path(hostrun.__file__).resolve().parents[1])
        code = ("import sys; from ttp import hostrun; from ttp.project import Project; "
                "sys.exit(hostrun.request(Project(sys.argv[1]), sys.argv[2:], wait_s=20, detach=True))")
        caller = subprocess.run([sys.executable, "-c", code, str(p.base), *cmd], capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": runtime}, timeout=60)
    finally:
        stop.set()
        t.join(5)
    assert caller.returncode == 0, caller.stderr
    rid = re.search(r"started ([0-9a-f]{32}) \(limit 900 s\)", caller.stdout).group(1)
    assert f"ttp host-run --status {rid}" in caller.stdout
    claim = json.loads((q / f"{rid}.running").read_text())
    assert claim["limit"] == 900 and claim["pid"] != os.getpid()
    assert hostrun.status(p, rid) == 1 and "is running" in capfd.readouterr().out
    assert hostrun.status(p, rid, result=True) == hostrun.NO_ANSWER
    capfd.readouterr()
    gate.write_text("")   # the caller is long gone: the run goes on and answers by itself
    end = time.monotonic() + 30
    while not (q / f"{rid}.rc").exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert (q / f"{rid}.rc").read_text().strip() == "5"
    assert hostrun.status(p, rid) == 0 and "ended: exit 5" in capfd.readouterr().out
    assert hostrun.status(p, rid, result=True) == 5 and "cycled" in capfd.readouterr().out
    assert hostrun.status(p, rid) == 0, "polling is safe to repeat: the answer stays"
    assert not (q / f"{rid}.running").exists()
    whys = [json.loads(l)["why"] for l in (p.state / hostrun.LOG).read_text().splitlines()]
    assert whys == ["detached", "ran"]
    # A detached answer outlives the plain answer's pruning, then goes too.
    hostrun.serve(p)
    assert (q / f"{rid}.out.json").exists()
    old = time.time() - hostrun.DETACHED_KEEP_S - 10
    for f in (q / f"{rid}.out.json", q / f"{rid}.rc"):
        os.utime(f, (old, old))
    hostrun.serve(p)
    assert hostrun.status(p, rid) == hostrun.UNKNOWN


def test_host_run_detached_run_keeps_its_entry_limit_and_a_lost_one_says_so(env, monkeypatch, capfd):
    from ttp import hostrun
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(cmd), "timeout_s": 1}])
    assert _hostrun_with_daemon(p, cmd, [], detach=True) == 0
    rid = re.search(r"started ([0-9a-f]{32})", capfd.readouterr().out).group(1)
    q = p.state / hostrun.QUEUE
    end = time.monotonic() + 20
    while not (q / f"{rid}.rc").exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert (q / f"{rid}.rc").read_text().strip() == str(hostrun.TIMED_OUT)
    assert hostrun.status(p, rid, result=True) == hostrun.TIMED_OUT and "timed out after 1 s" in capfd.readouterr().err
    # A claim whose supervisor died with no answer (a reboot) is reported lost once past its limit.
    lost = secrets.token_hex(16)
    (q / f"{lost}.running").write_text(json.dumps({"limit": 5, "pid": 1, "started": time.time() - 300}))
    assert hostrun.status(p, lost) == hostrun.UNKNOWN and "lost" in capfd.readouterr().err
    assert hostrun.status(p, "../x") == hostrun.UNKNOWN


def test_host_run_cli_detach_status_and_timeout_flags(env, monkeypatch, capfd):
    from ttp import cli, hostrun
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"])
    seen = {}
    monkeypatch.setattr(hostrun, "request", lambda p, cmd, timeout=None, detach=False: seen.update(
        cmd=cmd, timeout=timeout, detach=detach) or 0)
    monkeypatch.setattr(hostrun, "status", lambda p, rid, result=False: seen.update(rid=rid, result=result) or 1)
    with pytest.raises(SystemExit) as e:
        cli.main(["host-run", "--detach", "--timeout", "900", "--", "/bin/true"])
    assert e.value.code == 0 and seen == {"cmd": ["/bin/true"], "timeout": 900.0, "detach": True}
    with pytest.raises(SystemExit) as e:
        cli.main(["host-run", "--status", "ab" * 16])
    assert e.value.code == 1 and seen["rid"] == "ab" * 16 and seen["result"] is False
    for bad in (["host-run", "--timeout", "0", "--", "/bin/true"], ["host-run", "--status", "x", "--", "/bin/true"],
                ["host-run"]):
        with pytest.raises(SystemExit) as e:
            cli.main(bad)
        assert e.value.code == 2

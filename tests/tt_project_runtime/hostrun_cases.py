# `ttp host-run` tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new host-run route tests here, not there.
from __future__ import annotations


def _hostrun_setup(env, monkeypatch, allow, by="user", unfenced=True):
    from ttp import configwho, privlock
    if unfenced:   # this test process stands in for the daemon, which runs unfenced (a sandbox sets the flag)
        monkeypatch.setattr(privlock, "fenced", lambda pid="self": False)
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


@pytest.mark.parametrize("argv", [
    [sys.executable, "-c", "print('ok')", "reboot.target"],   # a word appended to an exact entry
    [sys.executable, "-c", "print('ok')", ";", "reboot"],      # a shell-metacharacter variant
    [sys.executable, "-c", "print('ok'); import os"],         # the last word extended
    [sys.executable, "-c"],                                   # a word short
])
def test_host_run_exact_entry_allows_only_its_own_words_client_and_daemon_side(env, monkeypatch, capfd, argv):
    from ttp import cli, hostrun
    exact = [sys.executable, "-c", "print('ok')"]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(exact), "exact": True, "timeout_s": 30},
                                          {"cmd": "/bin/true", "exact": False}])
    ents, why = hostrun.entries(p)
    assert why == "" and ents == [(exact, 30), (["/bin/true"], None)] and [e.exact for e in ents] == [True, False]
    assert _hostrun_with_daemon(p, exact, []) == 0 and "ok" in capfd.readouterr().out
    assert hostrun.limit_for(p, exact, ents) == 30
    assert hostrun.allowed(["/bin/true", "x"], ents), "exact false keeps prefix matching"
    assert hostrun.request(p, argv) == hostrun.REFUSED and "refused" in capfd.readouterr().err
    q = p.state / hostrun.QUEUE   # written straight into the queue, past the client: the daemon refuses it too
    rid = secrets.token_hex(16)
    (q / f"{rid}.json").write_text(json.dumps({"argv": argv, "task": "9"}))
    hostrun.serve(p, wait=True)
    assert json.loads((q / f"{rid}.out.json").read_text())["rc"] == hostrun.REFUSED
    cli.main(["config", "demo", "runner.host_commands", json.dumps([{"cmd": "/bin/id -u", "exact": True}])])
    assert p.config()["runner"]["host_commands"] == [{"cmd": "/bin/id -u", "exact": True}]


@pytest.mark.parametrize("argv", [
    ["ttp", "lock", "r1", "--", sys.executable, "-c", "print('ok')", "x"],   # an extra word under an exact entry
    ["ttp", "lock", "r1", "--", "/bin/false"],                              # a command not on the list
    ["ttp", "lock", "--disruptive", "r1", "--", sys.executable, "-c", "print('ok')"],   # not the exact wrapper:
    ["ttp", "lock", "r1,r2", "--", sys.executable, "-c", "print('ok')"],    # an option, several resources,
    ["ttp", "lock", "r1", sys.executable, "-c", "print('ok')"],             # no `--`,
    ["/usr/bin/ttp", "lock", "r1", "--", sys.executable, "-c", "print('ok')"],   # another program path,
    ["ttp", "lock", "r1", "--", "ttp", "lock", "r2", "--", sys.executable, "-c", "print('ok')"],   # nested
    ["ttp", "lock", "r1", "--"],
])
def test_host_run_lock_wrapper_refuses_extra_words_an_unlisted_command_and_other_wrappers(env, monkeypatch, capfd,
                                                                                         argv):
    from ttp import hostrun
    exact = [sys.executable, "-c", "print('ok')"]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(exact), "exact": True}])
    ents, _ = hostrun.entries(p)
    assert hostrun.allowed(exact, ents) and hostrun.allowed(["ttp", "lock", "r1", "--", *exact], ents)
    assert hostrun.request(p, argv) == hostrun.REFUSED and "refused" in capfd.readouterr().err
    q = p.state / hostrun.QUEUE   # past the client: the daemon refuses it too
    q.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(16)
    (q / f"{rid}.json").write_text(json.dumps({"argv": argv, "task": "9"}))
    hostrun.serve(p, wait=True)
    assert json.loads((q / f"{rid}.out.json").read_text())["rc"] == hostrun.REFUSED


def test_host_run_lock_wrapper_runs_a_listed_command_with_its_limit_and_holds_the_lock(env, monkeypatch, capfd):
    from ttp import hostrun
    probe = [sys.executable, "-c", "import subprocess, sys; print('probe', subprocess.run([sys.executable, '-m', "
             "'ttp', 'lock', '--probe', 'hr-res']).returncode)"]
    p = _hostrun_setup(env, monkeypatch, [{"cmd": shlex.join(probe), "exact": True, "timeout_s": 45}, "/bin/true"])
    ents, _ = hostrun.entries(p)
    wrapped = ["ttp", "lock", "hr-res", "--", *probe]
    assert hostrun.limit_for(p, wrapped, ents) == hostrun.limit_for(p, probe, ents) == 45
    assert hostrun.allowed(["ttp", "lock", "hr-res", "--", "/bin/true", "x"], ents), "a plain entry stays a prefix"
    monkeypatch.setenv("PYTHONPATH", str(RUNTIME))   # the daemon's own environment, which a bare command gets
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    assert _hostrun_with_daemon(p, probe, []) == 0 and "probe 0" in capfd.readouterr().out   # bare: no lock
    assert _hostrun_with_daemon(p, wrapped, []) == 0
    assert "probe 75" in capfd.readouterr().out, "the resource is held while the wrapped command runs"
    run, env2 = hostrun.launch(p.base, wrapped, "12")
    assert run == [sys.executable, "-m", "ttp", "lock", "hr-res", "--", *probe]
    assert env2["TTP_PROJECT"] == str(p.base) and env2["TTP_TASK"] == "12" and "TTP_RUN_ID" not in env2
    assert hostrun.launch(p.base, probe, "12") == (probe, None)
    assert subprocess.run([sys.executable, "-m", "ttp", "lock", "--probe", "hr-res"]).returncode == 0, "released after"


def test_doctor_warns_on_a_plain_sudo_entry_only(env, monkeypatch):
    from ttp import hostrun
    raw = ["sudo -n systemctl enable --now a.service", "/usr/bin/sudo -n /usr/sbin/tool status",
           {"cmd": "sudo -n systemctl restart a.service", "exact": True}, {"cmd": "sudo -n /usr/sbin/tool info"},
           "/bin/true", "'sudo", {"cmd": "sudo -n x", "exact": "yes"}]
    warns = hostrun.sudo_prefix_warnings(raw)
    assert [w.split(" ")[2] for w in warns] == ["1", "2", "4"] and all("make it exact" in w for w in warns)
    assert hostrun.sudo_prefix_warnings("sudo reboot") == hostrun.sudo_prefix_warnings(None) == []


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
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"], by="coordinator")
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
    # set_config itself refuses a worker's write over the coordinator's list (configwho.guard).
    with pytest.raises(configwho.Protected):
        p.set_config("runner.host_commands", ["/bin/sh"], by=configwho.by("worker", task="12"))
    assert hostrun.allowlist(p)[0] == [["/bin/true"], ["/usr/bin/id", "-u"]]
    # A list a worker wrote anyway (provenance says worker: a write from before the guard) runs nothing.
    p.set_config("runner.host_commands", ["/bin/sh"], by=configwho.by("user"))
    configwho.record(p.db, "runner.host_commands", configwho.by("worker", task="12"), after=p.raw_config())
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
    ({"cmd": "/bin/power-cycle", "timeout_s": 0.5}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": 0.999}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": -5}, "outside 1..3600"),
    ({"cmd": "/bin/power-cycle", "timeout_s": "900"}, "must be a number"),
    ({"cmd": "/bin/power-cycle", "timeout_s": True}, "must be a number"),
    ({"cmd": "/bin/power-cycle", "timeout": 900}, "unknown key timeout"),
    ({"cmd": "/bin/power-cycle", "exact": "yes"}, "exact must be true or false"),
    ({"cmd": "/bin/power-cycle", "exact": 1}, "exact must be true or false"),
    ({"exact": True}, "must be a string"),
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
    from ttp.runner import boot_id
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
    assert claim["limit"] == 900 and claim["pid"] != os.getpid() and claim["boot"] == boot_id()
    assert hostrun.status(p, rid) == 1 and "is running" in capfd.readouterr().out
    assert hostrun.status(p, rid, result=True) == hostrun.NO_ANSWER
    capfd.readouterr()
    gate.write_text("")   # the caller is long gone: the run goes on and answers by itself
    # The supervisor writes the rc file, then logs the end, then drops the claim: wait for its last step,
    # since under load the rc file can stand a while before the claim goes.
    end = time.monotonic() + 120
    while ((q / f"{rid}.running").exists() or not (q / f"{rid}.rc").exists()) and time.monotonic() < end:
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
    end = time.monotonic() + 120
    while not (q / f"{rid}.rc").exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert (q / f"{rid}.rc").read_text().strip() == str(hostrun.TIMED_OUT)
    assert hostrun.status(p, rid, result=True) == hostrun.TIMED_OUT and "timed out after 1 s" in capfd.readouterr().err
    # A claim whose supervisor died with no answer (a reboot) is reported lost once past its limit.
    lost = secrets.token_hex(16)
    (q / f"{lost}.running").write_text(json.dumps({"limit": 5, "pid": 1, "started": time.time() - 300}))
    assert hostrun.status(p, lost) == hostrun.UNKNOWN and "lost" in capfd.readouterr().err
    assert hostrun.status(p, "../x") == hostrun.UNKNOWN


def test_a_live_detached_host_run_holds_a_drain_and_a_stale_claim_does_not(env, monkeypatch, capsys):
    # A planned host cycle must not reboot midway through a detached host-run (a power cycle): its live
    # supervisor counts as a running job; a dead, earlier-boot, overdue or ended claim never holds it.
    from ttp import cli, hostcycle, hostrun
    from ttp.runner import boot_id
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"])
    q = p.state / hostrun.QUEUE
    q.mkdir(parents=True, exist_ok=True)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    try:
        now = time.time()
        claim = {"argv": ["/bin/power-cycle"], "task": "7", "detach": True, "limit": 900, "boot": boot_id()}
        rids = {k: secrets.token_hex(16) for k in ("live", "dead", "earlier", "overdue", "ended", "unstarted")}
        stale = {"dead": {"pid": dead.pid, "started": now},
                 "earlier": {"pid": live.pid, "started": now, "boot": "an-earlier-boot"},
                 "overdue": {"pid": live.pid, "started": now - 900 - hostrun.QUEUE_WAIT_S - 5},
                 "ended": {"pid": live.pid, "started": now}}
        for k, extra in stale.items():
            (q / f"{rids[k]}.running").write_text(json.dumps({**claim, **extra}))
        (q / f"{rids['ended']}.rc").write_text("0\n")
        (q / f"{rids['unstarted']}.running").write_text(json.dumps({**claim, "started": now}))   # no pid
        cli.main(["drain", "demo", "--jobs", "--wait", "0.2"])
        assert "drained" in capsys.readouterr().out, "a stale claim held the drain"
        assert hostcycle.running_jobs(p) == []
        (q / f"{rids['live']}.running").write_text(json.dumps({**claim, "pid": live.pid, "started": now}))
        (job,) = hostcycle.running_jobs(p)
        assert job["name"] == f"host-run {rids['live']}" and job["task"] == 7 and job["command"] == "/bin/power-cycle"
        with pytest.raises(SystemExit) as e:
            cli.main(["drain", "demo", "--jobs", "--wait", "0.2"])
        err = capsys.readouterr().err
        assert e.value.code == 1 and "not drained" in err and "1 detached job(s)" in err and rids["live"] in err, err
        # A plain drain names it as a job a reboot would end; a --reboot drain records it as interrupted.
        cli.main(["drain", "demo", "--reboot", "a planned power cycle"])
        (f,) = (p.state / hostcycle.FOLDER).glob("*/record.json")
        assert [j["name"] for j in json.loads(f.read_text())["interrupted"]] == [f"host-run {rids['live']}"]
        capsys.readouterr()
        live.kill()
        live.wait()
        cli.main(["drain", "demo", "--jobs", "--wait", "0.2"])
        assert "drained" in capsys.readouterr().out, "a claim whose supervisor died still held the drain"
    finally:
        live.kill()
        live.wait()


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


_READ_FLAG = [sys.executable, "-c", "import re; print(re.search(r'NoNewPrivs:\\s*(\\d)', open('/proc/self/status').read())[1])"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="no_new_privs is Linux only")
def test_host_run_leaves_its_command_unfenced(env, monkeypatch, capfd):
    from ttp import privlock
    if privlock.fenced():
        pytest.skip("this test process already has no_new_privs: an unfenced child cannot be shown")
    p = _hostrun_setup(env, monkeypatch, [shlex.join(_READ_FLAG)])
    assert _hostrun_with_daemon(p, _READ_FLAG, []) == 0
    assert capfd.readouterr().out.strip() == "0"


def test_a_fenced_daemon_answers_host_run_with_the_plain_reason_and_runs_nothing(env, monkeypatch, capfd, tmp_path):
    from ttp import hostrun, privlock
    ran = tmp_path / "ran"
    cmd = ["/bin/touch", str(ran)]
    p = _hostrun_setup(env, monkeypatch, [shlex.join(cmd)], unfenced=False)
    monkeypatch.setattr(privlock, "fenced", lambda pid="self": True)
    assert _hostrun_with_daemon(p, cmd, []) == hostrun.REFUSED
    err = capfd.readouterr().err
    assert "did not run: the daemon" in err and "no_new_privs set" in err
    assert f"restart the daemon through its service manager: `ttp restart {p.name}`" in err
    assert not ran.exists()
    assert json.loads((p.state / hostrun.LOG).read_text().splitlines()[-1])["why"] == "fenced"
    # A detach request is checked first too: no supervisor starts and no <id>.rc is written.
    started = []
    monkeypatch.setattr(hostrun, "_start_supervisor", lambda *a: started.append(a) or 0)
    assert _hostrun_with_daemon(p, cmd, [], detach=True) == hostrun.REFUSED
    assert "did not run: the daemon" in capfd.readouterr().err
    assert not started and not ran.exists() and not list((p.state / hostrun.QUEUE).glob("*.rc"))
    assert json.loads((p.state / hostrun.LOG).read_text().splitlines()[-1])["why"] == "fenced"
    p.set_config("runner.host_commands", [])
    assert hostrun.fence_problem(p) == "", "nothing allowed: nothing to report"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="no_new_privs is Linux only")
def test_fence_problem_reads_the_named_daemons_flag(env, monkeypatch, tmp_path):
    """`ttp doctor` reads the daemon's pid, not its own: a real fenced process is reported, a gone one not."""
    from ttp import hostrun, privlock
    p = _hostrun_setup(env, monkeypatch, ["/usr/bin/id -u"], unfenced=False)
    child = subprocess.Popen(["sleep", "30"], preexec_fn=privlock.no_new_privs_preexec())
    try:
        assert privlock.fenced(child.pid) is True
        why = hostrun.fence_problem(p, child.pid)
        assert f"the daemon (pid {child.pid}) runs with no_new_privs set" in why
    finally:
        child.kill()
        child.wait()
    assert hostrun.fence_problem(p, child.pid) == "" and privlock.fenced(child.pid) is None


def test_the_coordinator_sets_the_host_run_allowlist_through_config_set_and_chat_settable_accepts_it(
        env, monkeypatch, capsys):
    from ttp import cli, configwho, hostrun
    from ttp import coordinator as coord
    p = _hostrun_setup(env, monkeypatch, [], by="coordinator")   # a value the user set it never changes
    good = ["sudo -n /usr/sbin/tool status", {"cmd": "/usr/local/sbin/power-cycle", "timeout_s": 900}]
    assert coord.apply(p, [{"type": "config_set", "key": hostrun.KEY, "value": good}]) == []
    assert p.config()["runner"]["host_commands"] == good
    assert configwho.lookup(p.db, hostrun.KEY, p.raw_config())["by"] == "coordinator"
    assert hostrun.entries(p) == ([(["sudo", "-n", "/usr/sbin/tool", "status"], None),
                                   (["/usr/local/sbin/power-cycle"], 900)], "")
    # A JSON list given as text is the same list; an empty list allows nothing.
    assert coord.USER_SETTABLE[hostrun.KEY](json.dumps(good)) == good
    assert coord.USER_SETTABLE[hostrun.KEY]([]) == []

    def ask(*argv):
        try:
            cli.main(["config", "--chat-settable", hostrun.KEY, *argv])
            rc = 0
        except SystemExit as e:
            rc = e.code
        return rc, capsys.readouterr().out
    assert ask() == (0, f"{hostrun.KEY}: accepted\n")
    assert ask(json.dumps(good))[0] == 0
    assert ask("sudo -n /usr/sbin/tool status")[0] == 1


@pytest.mark.parametrize("bad, says", [
    ("sudo -n /usr/sbin/tool status", "not one shell string"),        # a shell string, not a list
    ('"sudo -n /usr/sbin/tool status"', "not one shell string"),      # the same as a JSON string
    ({"cmd": "/bin/true"}, "give a JSON list"),                         # one entry, not a list
    (17, "give a JSON list"),
    (None, "give a JSON list"),
    ([""], "entry 1 "),                                                 # empty
    (["/bin/true", "   "], "entry 2 "),
    ([["/bin/true"]], "must be a string"),                              # an argv list as an entry
    ([None], "must be a string"),
    (["/bin/true", "sudo -n 'unclosed"], "does not split"),
    ([{"cmd": "/bin/true", "timeout_s": 0}], "outside 1..3600"),
    ([{"cmd": "/bin/true", "timeout_s": 9000}], "outside 1..3600"),
    ([{"cmd": "/bin/true", "timeout_s": True}], "must be a number"),
    ([{"cmd": "/bin/true", "shell": True}], "unknown key shell"),
    ([{"timeout_s": 60}], "must be a string"),
])
def test_config_set_and_ttp_config_never_store_a_host_run_list_hostrun_would_refuse(env, monkeypatch, capsys,
                                                                                      bad, says):
    from ttp import cli, hostrun
    from ttp import coordinator as coord
    p = _hostrun_setup(env, monkeypatch, ["/bin/true"])
    errs = coord.apply(p, [{"type": "config_set", "key": hostrun.KEY, "value": bad}])
    assert len(errs) == 1 and hostrun.KEY in errs[0] and says in errs[0], errs
    assert p.config()["runner"]["host_commands"] == ["/bin/true"], "nothing is stored"
    try:
        cli.main(["config", "--chat-settable", hostrun.KEY, bad if isinstance(bad, str) else json.dumps(bad)])
        rc = 0
    except SystemExit as e:
        rc = e.code
    assert rc == 1 and says in capsys.readouterr().out
    # `ttp config` refuses the same value (it used to store a non-list), as the whole key and under `runner`.
    for key, value in ((hostrun.KEY, bad), ("runner", {"host_commands": bad})):
        with pytest.raises(SystemExit):
            cli.main(["config", "demo", key, value if isinstance(value, str) else json.dumps(value)])
        assert says in capsys.readouterr().err
    assert p.config()["runner"]["host_commands"] == ["/bin/true"]

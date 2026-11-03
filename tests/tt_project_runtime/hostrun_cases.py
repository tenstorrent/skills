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


def _hostrun_with_daemon(p, argv, served):
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
        return hostrun.request(p, argv, wait_s=20)
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

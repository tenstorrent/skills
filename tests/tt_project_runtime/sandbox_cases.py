# Process sandbox tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp sandbox` and check isolation tests here.
from __future__ import annotations


def _live_sandbox(monkeypatch):
    """A real sandbox for this test (the project opted in), nested when the suite itself runs in one;
    skips where the host cannot create user namespaces."""
    from ttp import sandbox
    monkeypatch.setenv(sandbox.ENV, "1")
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    pre, why = sandbox.status()
    if not pre:
        pytest.skip(f"this host allows no process sandbox: {why}")
    return sandbox


def _no_sandbox_tools(monkeypatch):
    from ttp import sandbox
    monkeypatch.setenv(sandbox.ENV, "1")
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")   # on macOS the probe stops at "need Linux" first
    which = shutil.which
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, *a, **k: None if name in ("unshare", "bwrap") else which(name, *a, **k))
    return sandbox


def test_sandbox_hides_an_outside_process_and_a_kill_of_its_pid_reaches_nothing(monkeypatch):
    sandbox = _live_sandbox(monkeypatch)
    sentinel = subprocess.Popen(["sleep", "60"])
    try:
        assert sentinel.pid > 3   # never one of the namespace's own first pids
        probe = (f"import os\np = {sentinel.pid}\nprint(os.path.exists(f'/proc/{{p}}'))\n"
                 "try:\n    os.kill(p, 9)\n    print('signalled')\nexcept ProcessLookupError:\n    print('no such process')\n")
        r = subprocess.run(sandbox.wrap([sys.executable, "-c", probe]), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == ["False", "no such process"]
        assert sentinel.poll() is None   # still alive
    finally:
        sentinel.kill()
        sentinel.wait()


def test_a_kill_of_group_0_inside_the_sandbox_reaches_no_outside_process(monkeypatch):
    """`kill 0` or os.killpg(0, sig) in the command signals only its own group: a process outside
    the namespace that shares the caller's group lives on."""
    sandbox = _live_sandbox(monkeypatch)
    argv = sandbox.wrap(["sh", "-c", "trap 'echo caught' USR1; kill -USR1 0; echo done"])
    driver = ("import json, signal, subprocess, sys\nsignal.signal(signal.SIGUSR1, lambda *a: None)\n"
              "s = subprocess.Popen(['sleep', '60'])\n"
              "r = subprocess.run(json.loads(sys.argv[1]), capture_output=True, text=True, timeout=60)\n"
              "print(json.dumps([r.returncode, r.stdout, s.poll()]))\ns.kill()\n")
    r = subprocess.run([sys.executable, "-c", driver, json.dumps(argv)], start_new_session=True,
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, r.stderr
    rc, out, sentinel = json.loads(r.stdout)
    assert sentinel is None, "a signal to group 0 inside the sandbox reached a process outside it"
    assert rc == 0 and out.splitlines() == ["caught", "done"]


def test_a_term_or_int_to_the_callers_group_still_stops_the_command_in_its_own_group(tmp_path, monkeypatch):
    """The command has a group of its own; the sandbox init passes a group TERM (a runner's stop) or
    INT (Ctrl-C) on to it, and the command ends of that signal."""
    sandbox = _live_sandbox(monkeypatch)
    for sig in (signal.SIGTERM, signal.SIGINT):
        ready = tmp_path / f"ready{int(sig)}"
        proc = subprocess.Popen(sandbox.wrap(["sh", "-c", f"touch {shlex.quote(str(ready))}; sleep 60"]),
                                start_new_session=True)
        try:
            deadline = time.time() + 30
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert ready.exists()
            os.killpg(proc.pid, sig)
            assert proc.wait(timeout=20) == 128 + sig
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_sandbox_keeps_working_dir_environment_and_exit_code(tmp_path, monkeypatch):
    sandbox = _live_sandbox(monkeypatch)
    r = subprocess.run(sandbox.wrap(["sh", "-c", 'pwd; echo "$TTP_SANDBOX_TEST"; exit 7']), cwd=tmp_path,
                       env={**os.environ, "TTP_SANDBOX_TEST": "kept"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 7 and r.stdout.splitlines() == [str(tmp_path.resolve()), "kept"]
    # A command killed by a signal exits 128 + its number, as a shell reports it.
    r = subprocess.run(sandbox.wrap(["sh", "-c", "kill -TERM $$"]), timeout=60)
    assert r.returncode == 128 + signal.SIGTERM


def test_a_timeout_ends_the_whole_namespace_with_processes_that_ignore_term(tmp_path, monkeypatch):
    """A stray background process that ignores SIGTERM and outlives its shell still ends with the
    sandbox: its file lock comes free at once."""
    import fcntl
    sandbox = _live_sandbox(monkeypatch)
    lock, ready = tmp_path / "held", tmp_path / "ready"
    holder = ("import fcntl, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
              f"f = open({str(lock)!r}, 'w')\nfcntl.flock(f, fcntl.LOCK_EX)\nopen({str(ready)!r}, 'w').close()\n"
              "time.sleep(120)\n")
    script = f"{shlex.quote(sys.executable)} -c {shlex.quote(holder)} & sleep 120"
    proc = subprocess.Popen(sandbox.wrap(["sh", "-c", script]), start_new_session=True)
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        proc.kill()   # what subprocess.run(timeout=) and every runner's last resort send
        proc.wait(timeout=30)
        with open(lock, "w") as f:
            deadline = time.time() + 10
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    assert time.time() < deadline, "a process of the namespace outlived it"
                    time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_ttp_sandbox_under_timeout_ends_on_sigterm(tmp_path, monkeypatch, capsys):
    """`timeout N ttp sandbox -- <cmd>`: the TERM to ttp sandbox ends the namespace."""
    _live_sandbox(monkeypatch)
    runtime = pathlib.Path(__import__("ttp").__file__).resolve().parent.parent
    start = time.time()
    r = subprocess.run(["timeout", "2", sys.executable, "-m", "ttp", "sandbox", "--", "sh", "-c", "sleep 60 & sleep 60"],
                       env={**os.environ, "PYTHONPATH": str(runtime)}, timeout=60)
    assert r.returncode == 124 and time.time() - start < 20


def test_ttp_checks_runs_each_check_in_the_sandbox_once_the_project_opts_in(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    _live_sandbox(monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    # lives until killed below: a run queued for a busy host's checks slot can outlast any fixed sleep
    sentinel = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
    try:
        cli.main(["checks", "--fresh", "--", f"test ! -e /proc/{sentinel.pid}"])
        assert json.loads((run / "checks.json").read_text())["passed"] is True
        assert "no process sandbox" not in capsys.readouterr().out
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait()


def test_a_checks_reap_token_never_reads_as_the_projects_sandbox_opt_in(monkeypatch):
    """Each check's env carries checkreap's run token; the opt-in is another variable, so the token
    neither turns the sandbox off for an opted-in project nor on for one that did not opt in."""
    from ttp import checkreap, sandbox
    assert sandbox.ENV != checkreap.MARKER
    tok = checkreap.token()
    assert sandbox.opted_in({sandbox.ENV: "1", checkreap.MARKER: tok})
    assert not sandbox.opted_in({sandbox.ENV: "0", checkreap.MARKER: tok})
    assert not sandbox.opted_in({checkreap.MARKER: "1"})


def test_check_argv_wraps_local_checks_unless_one_opts_out(tmp_path, monkeypatch):
    from ttp import push, pushpass, sandbox
    monkeypatch.setenv(sandbox.ENV, "1")
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare", "--pid"], ""))
    plain = push.check_argv("make a", tmp_path)
    assert plain[:2] == ["/sb/unshare", "--pid"] and plain[-1] == "make a"
    assert sandbox.INIT.name in " ".join(plain)
    checks = push.check_list([{"run": "make b", "sandbox": False}, "make c", {"run": "make d", "host": "box1"}])
    off, on, remote = checks
    assert off.sandbox is False and off.config() == {"run": "make b", "sandbox": False} and on.config() == "make c"
    assert push.check_argv(off, tmp_path)[0] != "/sb/unshare" and push.check_argv(off, tmp_path)[-1] == "make b"
    assert push.check_argv(remote, tmp_path)[0] != "/sb/unshare"   # ssh runs it on another machine
    assert push.check_argv(on, tmp_path, isolate=False)[0] != "/sb/unshare"
    # Already in a sandbox: run as it is, never nested.
    monkeypatch.setattr(sandbox, "inside", lambda: True)
    assert push.check_argv("make a", tmp_path)[0] != "/sb/unshare"
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    # The opt-out is reported per check; a run where every local check is sandboxed says nothing.
    assert push.sandbox_lines(checks) == [f"make b: {sandbox.notice(False)}"]
    assert push.sandbox_lines([on, remote]) == []
    # A pass recorded sandboxed or not is the same pass.
    monkeypatch.setattr(sandbox, "status", lambda: (None, "off"))
    eid = pushpass.env_id(tmp_path, "make c", dict(os.environ), "")
    monkeypatch.setattr(sandbox, "status", lambda: (["/sb/unshare"], ""))
    assert pushpass.env_id(tmp_path, "make c", dict(os.environ), "") == eid
    # Only a boolean opts out.
    assert push.check_problems([{"run": "make b", "sandbox": "no"}])


def test_without_a_sandbox_tool_checks_run_and_say_why(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, push
    sandbox = _no_sandbox_tools(monkeypatch)
    pre, why = sandbox.status()
    assert pre is None and "neither unshare (util-linux) nor bwrap (bubblewrap) is installed" in why
    assert sandbox.wrap(["true"]) == ["true"]
    assert push.sandbox_lines(["make a"]) == [sandbox.notice()] and why in sandbox.notice()
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    cli.main(["checks", "--fresh", "--", "true"])
    assert json.loads((run / "checks.json").read_text())["passed"] is True
    assert f"ttp checks: no process sandbox: {why}" in capsys.readouterr().out
    assert "no process sandbox" in (run / "checks.log").read_text()


def test_a_blocked_user_namespace_names_the_setting_that_allows_it(tmp_path, monkeypatch):
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "unshare"
    fake.write_text("#!/bin/sh\necho 'unshare: write failed /proc/self/uid_map: Operation not permitted' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, *a, **k: str(fake) if name == "unshare" else None)
    sysctl = tmp_path / "sys"
    (sysctl / "kernel").mkdir(parents=True)
    (sysctl / "kernel" / "apparmor_restrict_unprivileged_userns").write_text("1\n")
    monkeypatch.setattr(sandbox, "SYSCTL_DIR", sysctl)
    pre, why = sandbox.status()
    assert pre is None
    assert why.startswith("sysctl kernel.apparmor_restrict_unprivileged_userns=1 turns off unprivileged user namespaces")
    assert "set it to 0" in why and "Operation not permitted" in why
    # Probed once per process (and PATH): the next caller gets the same answer without a new probe.
    fake.unlink()
    assert sandbox.status() == (None, why)


def _fake_unshare(tmp_path, monkeypatch, body: str):
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    fake = tmp_path / "bin" / "unshare"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text("#!/bin/sh\n" + body)
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name, *a, **k: str(fake) if name == "unshare" else None)
    return sandbox, fake


def test_a_probe_that_ended_during_a_stall_past_its_deadline_finds_the_sandbox(tmp_path, monkeypatch):
    """A stall (a loaded host) freezes the caller past the probe's deadline while the probe ends: the first
    wait times out with the answer already there. That is no verdict: the sandbox works."""
    sandbox, fake = _fake_unshare(tmp_path, monkeypatch, "exit 0\n")
    stalls = []

    class Stalled(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if timeout is not None and not stalls:
                stalls.append(self.wait())   # the command ended while this process stood still
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().communicate(input, timeout)

    monkeypatch.setattr(sandbox.subprocess, "Popen", Stalled)
    pre, why = sandbox.status()
    assert stalls == [0] and pre and pre[0] == str(fake), why


def test_a_timed_out_probe_is_not_kept_and_the_next_call_probes_again(tmp_path, monkeypatch):
    """A probe still running at its deadline (twice) reads as no sandbox for that call only: nothing kept,
    the next call probes again and finds it, so one slow moment never runs every later command unsandboxed.
    A probe that answers is kept."""
    flag = tmp_path / "slow"
    flag.write_text("")
    sandbox, fake = _fake_unshare(tmp_path, monkeypatch, f'[ -e {shlex.quote(str(flag))} ] && exec sleep 30\nexit 0\n')
    monkeypatch.setattr(sandbox, "PROBE_S", 0.3)
    pre, why = sandbox.status()
    assert pre is None and sandbox._STATE == {}, "a timed-out probe is kept"
    assert "still running" in why, why
    flag.unlink()
    assert sandbox.status()[0][0] == str(fake)
    fake.write_text("#!/bin/sh\nexit 1\n")
    assert sandbox.status()[0][0] == str(fake), "an answer is kept for the process"


def test_ttp_checks_reports_a_push_check_that_opts_out_of_the_sandbox(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, sandbox
    monkeypatch.setenv(sandbox.ENV, "1")
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    monkeypatch.setattr(sandbox, "status", lambda: (None, "unused"))
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    p.set_config("delivery.push_checks", [{"run": "true", "sandbox": False}])
    cli.main(["checks", "--fresh"])
    out = capsys.readouterr().out
    assert "ttp checks: true: sandbox off (\"sandbox\": false)" in out and "no process sandbox" not in out
    assert json.loads((run / "checks.json").read_text())["passed"] is True


def test_the_tool_hook_sees_the_command_inside_ttp_sandbox():
    from ttp import hook
    assert hook._strip_prefix("ttp sandbox -- pytest -q tests".split()) == ["pytest", "-q", "tests"]


def _sandbox_with(monkeypatch, tool):
    """A live sandbox that uses only `tool` (unshare or bwrap); skips where that one does not work."""
    from ttp import sandbox
    monkeypatch.setenv(sandbox.ENV, "1")   # the project opted in
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    which = shutil.which
    monkeypatch.setattr(sandbox.shutil, "which",
                        lambda name, *a, **k: None if name in ("unshare", "bwrap") and name != tool else which(name, *a, **k))
    pre, why = sandbox.status()
    if not pre:
        pytest.skip(f"no {tool} sandbox on this host: {why}")
    return sandbox


def _fake_device_nodes(tmp_path, monkeypatch):
    """A stand-in for the device nodes: a directory holding a node and a node file, never a real device."""
    (tmp_path / "devdir").mkdir()
    (tmp_path / "devdir" / "0").write_text("chip")
    (tmp_path / "node").write_text("chip")
    monkeypatch.setenv("TTP_DEVICE_NODES", f"{tmp_path / 'devdir'}:{tmp_path / 'node'}")
    probe = (f"import os\nprint(os.listdir({str(tmp_path / 'devdir')!r}))\n"
             f"try:\n    print(open({str(tmp_path / 'node')!r}).read() == 'chip')\n"
             "except OSError:\n    print(False)\n")
    return [sys.executable, "-c", probe]


@pytest.mark.parametrize("tool", ["unshare", "bwrap"])
def test_a_no_device_sandbox_hides_the_device_nodes(tool, tmp_path, monkeypatch):
    sandbox = _sandbox_with(monkeypatch, tool)
    probe = _fake_device_nodes(tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_NO_DEVICE", "1")
    r = subprocess.run(sandbox.wrap(probe), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["[]", "False"]
    # Outside nothing changed, and the command cannot unmount them again (no CAP_SYS_ADMIN left).
    assert os.listdir(tmp_path / "devdir") == ["0"] and (tmp_path / "node").read_text() == "chip"
    r = subprocess.run(sandbox.wrap(["sh", "-c", f"umount {tmp_path / 'devdir'} 2>/dev/null; ls -A {tmp_path / 'devdir'}"]),
                       capture_output=True, text=True, timeout=60)
    assert r.stdout == "", r.stdout


@pytest.mark.parametrize("value", ["0", None])
def test_a_sandbox_of_a_run_that_may_use_a_device_keeps_the_nodes(value, tmp_path, monkeypatch):
    sandbox = _live_sandbox(monkeypatch)
    probe = _fake_device_nodes(tmp_path, monkeypatch)
    if value is None:
        monkeypatch.delenv("TTP_NO_DEVICE", raising=False)
    else:
        monkeypatch.setenv("TTP_NO_DEVICE", value)
    r = subprocess.run(sandbox.wrap(probe), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["['0']", "True"]


def test_a_sandbox_nested_in_one_that_left_the_nodes_visible_hides_them(tmp_path, monkeypatch):
    from ttp import sandbox
    _fake_device_nodes(tmp_path, monkeypatch)
    monkeypatch.setattr(sandbox, "inside", lambda: True)
    monkeypatch.setattr(sandbox, "status", lambda: (["unshare", "--user"], ""))
    monkeypatch.setenv("TTP_NO_DEVICE", "0")
    assert sandbox.wrap(["true"]) == ["true"]
    # A check's own environment decides (push.check_argv passes it), not the caller's; only where the
    # project opted in (sandbox.checks), never by default.
    from ttp import push
    clean = {k: v for k, v in os.environ.items() if k != sandbox.ENV}
    assert "--keep-caps" not in push.check_argv("true", env={**clean, "TTP_NO_DEVICE": "1"})
    assert "--keep-caps" in push.check_argv("true", env={**clean, "TTP_NO_DEVICE": "1", sandbox.ENV: "1"})
    monkeypatch.setenv(sandbox.ENV, "1")
    monkeypatch.setenv("TTP_NO_DEVICE", "1")
    argv = sandbox.wrap(["true"])
    assert argv[:2] == ["unshare", "--keep-caps"] and f"--hide={tmp_path / 'devdir'}" in argv
    (tmp_path / "devdir" / "0").unlink()
    monkeypatch.setenv("TTP_DEVICE_NODES", str(tmp_path / "devdir"))   # empty: already hidden
    assert sandbox.wrap(["true"]) == ["true"]


def test_sudo_fails_inside_the_sandbox(monkeypatch):
    if not shutil.which("sudo"):
        pytest.skip("no sudo on this host")
    sandbox = _live_sandbox(monkeypatch)
    r = subprocess.run(sandbox.wrap(["sudo", "-n", "true"]), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, r.stderr


def _device_hook(monkeypatch, tmp_path, device_host=True, no_device="1"):
    from ttp import hook, machines
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    if no_device is None:
        monkeypatch.delenv("TTP_NO_DEVICE", raising=False)
    else:
        monkeypatch.setenv("TTP_NO_DEVICE", no_device)
    (tmp_path / "devdir").mkdir(exist_ok=True)
    (tmp_path / "devdir" / "0").write_text("")
    monkeypatch.setenv("TTP_DEVICE_NODES", str(tmp_path / "devdir"))
    monkeypatch.setattr(machines, "device_host", lambda known=None: device_host)

    def call(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        return out
    return call


@pytest.mark.parametrize("cmd", [
    "tt-smi -r 0", "sudo tt-smi -glx_reset", "tt-smi --reset=0,1", "ttp sandbox -- tt-smi -r",
    "ttp clip -- tt-flash --fw-tar fw.tar", "bash -c 'tt-topology -l mesh'", "ipmitool chassis power cycle",
    "ttp lock build -- tt-smi -r",
    "cat {dev}/0", "dd if={dev}/0 of=x bs=1 count=1", "head -c1 {dev}/0", "cp {dev}/0 x",
    "python3 -c 'open(1)' {dev}/0", "echo x > {dev}/0", "TT_VISIBLE_DEVICES=0 pytest -q",
    "unset TT_VISIBLE_DEVICES; pytest", "export TT_VISIBLE_DEVICES=0", "env -u TT_VISIBLE_DEVICES pytest",
    "env -i PATH=$PATH pytest", "sh -c 'TT_VISIBLE_DEVICES=1 ./run'",
])
def test_a_no_device_run_is_refused_device_commands_outside_a_device_lock(cmd, tmp_path, monkeypatch):
    call = _device_hook(monkeypatch, tmp_path)
    out = call(cmd.replace("{dev}", str(tmp_path / "devdir")))
    why = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "TTP_NO_DEVICE=1" in why, why
    assert "ttp lock <device> -- <cmd>" in why and "held or paused" in why
    assert '"kind": "no_device"' in (tmp_path / "refusals.jsonl").read_text()


@pytest.mark.parametrize("cmd", [
    "ttp lock box-device -- tt-smi -r 0", "ttp lock box-device -- sudo tt-smi -glx_reset",
    "ttp devq submit -- tt-smi -r", "ttp lock box-device -- cat {dev}/0",
    "ls -l {dev}/0", "stat {dev}", "pytest -q tests/x.py", "ttp clip -- ttp sandbox -- pytest -q", "tt-smi -s",
    "TTP_NO_DEVICE=0 pytest", "grep -rn {dev} src", "egrep -r {dev}/0 .", "rg {dev}", "ag {dev} src",
    "find {dev} -type c", "fd . {dev}", "git log -- {dev}", "git grep -n {dev}", "ls -la {dev}/", "tree {dev}",
])
def test_a_no_device_run_keeps_test_runs_listings_and_locked_device_commands(cmd, tmp_path, monkeypatch):
    call = _device_hook(monkeypatch, tmp_path)
    out = call(cmd.replace("{dev}", str(tmp_path / "devdir")))
    assert "permissionDecision" not in ((out or {}).get("hookSpecificOutput") or {}), out


@pytest.mark.parametrize("device_host, no_device", [(True, None), (True, "0"), (False, "1")])
def test_device_commands_pass_where_the_run_holds_a_device_or_the_host_has_none(device_host, no_device,
                                                                                tmp_path, monkeypatch):
    call = _device_hook(monkeypatch, tmp_path, device_host=device_host, no_device=no_device)
    out = call("tt-smi -r 0")
    assert "permissionDecision" not in ((out or {}).get("hookSpecificOutput") or {}), out


def test_no_default_path_wraps_a_command_in_a_namespace(tmp_path, monkeypatch):
    """The default worker or coordinator run, `ttp checks`, push checks and `ttp sandbox` start no
    bwrap or unshare, even with TTP_NO_DEVICE=1 on a device host whose nodes exist."""
    from ttp import machines, push, sandbox
    (tmp_path / "devdir").mkdir()
    (tmp_path / "devdir" / "0").write_text("")
    monkeypatch.setenv("TTP_DEVICE_NODES", str(tmp_path / "devdir"))
    monkeypatch.setenv("TTP_NO_DEVICE", "1")
    monkeypatch.delenv(sandbox.ENV, raising=False)
    monkeypatch.setattr(machines, "device_host", lambda known=None: True)
    clean = {k: v for k, v in os.environ.items() if k != sandbox.ENV}
    assert sandbox.wrap(["pytest", "-q"]) == ["pytest", "-q"]
    argv = push.check_argv("pytest -q", env=clean, isolate=False)
    assert not {"bwrap", "unshare"} & {os.path.basename(w) for w in argv}, argv
    runtime = pathlib.Path(__import__("ttp").__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "ttp", "sandbox", "--", "sh", "-c", f'ls {tmp_path / "devdir"}; echo $$'],
                       env={**clean, "PYTHONPATH": str(runtime)}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.split()[0] == "0", r.stdout + r.stderr   # same view, no namespace
    assert r.stdout.split()[1] != "1", r.stdout
    assert "sandbox" not in r.stderr, r.stderr


def _fake_xdist_python(tmp_path, xdist_rc: int):
    """A stand-in interpreter: its xdist probe (`-c ...`) exits `xdist_rc` (0 has xdist, 3 lacks it,
    other cannot tell); any other run prints its arguments."""
    py = tmp_path / f"python9.{xdist_rc}"   # a name the pytest detector takes for an interpreter
    py.write_text(f'#!/bin/sh\n[ "$1" = -c ] && exit {xdist_rc}\necho "ARGS: $*"\n')
    py.chmod(0o755)
    return str(py)


def test_drop_xdist_takes_out_worker_options_only_where_xdist_is_missing(tmp_path):
    from ttp import testslots
    py = _fake_xdist_python(tmp_path, 3)
    argv = [py, "-m", "pytest", "-q", "-n", "4", "-k", "-n", "-nauto", "--numprocesses=3", "--dist", "load",
            "--dist=loadfile", "t.py", "--", "-n", "2"]
    kept, said = testslots.drop_xdist(argv)
    assert kept == [py, "-m", "pytest", "-q", "-k", "-n", "t.py", "--", "-n", "2"], kept
    assert said.startswith(f"pytest-xdist is not installed for {py}: dropped -n 4 -nauto") and "one process" in said
    for form in (["-n4"], ["-n", "auto"], ["-n", "logical"], ["--numprocesses", "2"], ["--maxprocesses=2"]):
        assert testslots.drop_xdist([py, "-m", "pytest", *form, "t.py"])[0] == [py, "-m", "pytest", "t.py"], form
    # With xdist, or where the probe cannot tell, nothing changes and nothing is said.
    for rc in (0, 1):
        other = _fake_xdist_python(tmp_path, rc)
        assert testslots.drop_xdist([other, "-m", "pytest", "-n", "4"]) == ([other, "-m", "pytest", "-n", "4"], "")
    # Not pytest, or pytest with no xdist option: as it is, and no probe needed.
    for cmd in (["head", "-n", "5", "f"], [py, "t.py", "-n", "2"], [py, "-m", "pytest", "-q", "-k", "-n2"]):
        assert testslots.drop_xdist(cmd) == (cmd, ""), cmd
    # A pytest script's interpreter is read from its shebang, also through env.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pytest").write_text(f"#!/usr/bin/env {py}\n")
    (bindir / "pytest").chmod(0o755)
    script = str(bindir / "pytest")
    assert testslots.drop_xdist([script, "-n", "2", "t.py"])[0] == [script, "t.py"]


@pytest.mark.parametrize("tool", ["sandbox", "clip"])
def test_ttp_sandbox_and_clip_run_pytest_without_n_where_xdist_is_missing(tmp_path, tool):
    runtime = pathlib.Path(__import__("ttp").__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(runtime)}
    for var in ("TTP_RUN_DIR", "TTP_TEST_SLOTS"):   # outside a worker's run: no host test slots taken
        env.pop(var, None)
    for rc, args in ((3, "-m pytest -q t.py"), (0, "-m pytest -q -n 4 t.py")):
        py = _fake_xdist_python(tmp_path, rc)
        r = subprocess.run([sys.executable, "-m", "ttp", tool, "--", py, "-m", "pytest", "-q", "-n", "4", "t.py"],
                           env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and f"ARGS: {args}" in r.stdout, (r.stdout, r.stderr)
        assert ("dropped -n 4" in r.stdout + r.stderr) is (rc == 3), (r.stdout, r.stderr)


def _pid_ended(pid: int) -> bool:
    """The process ended: no such pid, or a zombie waiting for its reaper. It can vanish between any two
    reads, so every failed read means gone."""
    try:
        os.kill(pid, 0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:   # ProcessLookupError, FileNotFoundError
        return True


def _wait_ended(pid: int, cap_s: float = 60.0) -> bool:
    """Wait for process `pid` to end, on the kernel's word: its pidfd turns readable as it exits (a zombie
    too), so no poll interval or load decides it; `cap_s` only bounds a hang. Without pidfds (not Linux)
    it polls _pid_ended up to `cap_s`."""
    import select
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):   # no pidfds here
        deadline = time.monotonic() + cap_s
        while not _pid_ended(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True
    try:
        return bool(select.select([fd], [], [], cap_s)[0])
    finally:
        os.close(fd)


def _kill_left(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def test_a_check_run_without_a_sandbox_ends_the_processes_it_left_behind(tmp_path, monkeypatch):
    """No PID namespace reaps a check by default: the check runs in a process group of its own, and
    whatever of it is still alive once it ends, passed or failed, is killed with that group."""
    from ttp import push, sandbox
    monkeypatch.delenv(sandbox.ENV, raising=False)
    env = {k: v for k, v in os.environ.items() if k != sandbox.ENV}
    for rc, want in ((0, 0), (1, push.CHECKS_FAILED)):
        pidfile = tmp_path / f"left-{rc}.pid"
        (tmp_path / "check.sh").write_text(
            f"sleep 300 </dev/null >/dev/null 2>&1 &\necho $! > {pidfile}\nexit {rc}\n")
        assert push._run_one("bash check.sh", tmp_path, dict(env), lambda m: None) == want
        pid = int(pidfile.read_text())
        if not _wait_ended(pid):
            _kill_left(pid)
            pytest.fail(f"the check's leftover process {pid} outlived it (exit {rc})")


def test_checkgroup_started_outside_a_group_of_its_own_kills_only_what_it_started():
    """checkgroup kills its whole group only when it leads it: run in its caller's group (not as push
    starts it), a parent gone ends the command and itself, never the caller's group."""
    from ttp import push
    driver = ("import subprocess, sys\n"   # the group leader: checkgroup runs in its group, its parent is not pid 1
              "r = subprocess.run([*sys.argv[1:4], '1', '--', 'sleep', '60'], timeout=60)\n"
              "print('caller alive', r.returncode)\n")
    r = subprocess.run([sys.executable, "-c", driver, *push.CHECK_GROUP], start_new_session=True,
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0 and r.stdout.strip() == f"caller alive {128 + signal.SIGKILL}", (r.returncode, r.stdout,
                                                                                              r.stderr)


def test_a_check_whose_push_is_killed_outright_ends_with_it(tmp_path):
    """A check runs in a group of its own, so a SIGKILL of its push (or of the push's group) does not
    reach it: its group leader (checkgroup) sees the push gone and kills the check's whole group. The
    test waits on events, not on time: the check's leftover pid comes through a pipe and its end
    through a pidfd (_wait_ended); their caps only bound a hang."""
    import select
    from ttp import push
    r, w = os.pipe()
    check = f"sleep 300 </dev/null >/dev/null 2>&1 & echo $! >&{w}; sleep 300"
    start = ("import os, subprocess, sys, time\n"   # the push: its check in a session of its own, as _run_one
             "w = int(sys.argv[5])\n"
             "subprocess.Popen([*sys.argv[1:4], str(os.getpid()), '--', 'bash', '-c', sys.argv[4]],"
             " start_new_session=True, pass_fds=(w,))\nos.close(w)\ntime.sleep(300)\n")
    pusher = subprocess.Popen([sys.executable, "-c", start, *push.CHECK_GROUP, check, str(w)],
                              start_new_session=True, pass_fds=(w,))
    os.close(w)
    try:
        with os.fdopen(r) as out:
            assert select.select([out], [], [], 120)[0], "the check did not start"
            line = out.readline().strip()
        assert line.isdigit(), f"the check did not start: {line!r} (push exit {pusher.poll()})"
        left = int(line)
        os.killpg(pusher.pid, signal.SIGKILL)   # the push's group: the check's own group is not in it
        pusher.wait()
        if not _wait_ended(left):
            _kill_left(left)
            pytest.fail(f"the check's process {left} outlived its killed push")
    finally:
        if pusher.poll() is None:   # once reaped, its pid (and group id) may be another process's
            os.killpg(pusher.pid, signal.SIGKILL)
            pusher.wait()

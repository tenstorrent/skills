# `ttp sandbox` and check process tests of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new `ttp sandbox` and check process tests here.
from __future__ import annotations


def test_ttp_sandbox_under_timeout_ends_on_sigterm(tmp_path, monkeypatch, capsys):
    """`timeout N ttp sandbox -- <cmd>`: the TERM to ttp sandbox is passed on to the command."""
    runtime = pathlib.Path(__import__("ttp").__file__).resolve().parent.parent
    start = time.time()
    r = subprocess.run(["timeout", "2", sys.executable, "-m", "ttp", "sandbox", "--", "sh", "-c", "sleep 60 & sleep 60"],
                       env={**os.environ, "PYTHONPATH": str(runtime)}, timeout=60)
    assert r.returncode == 124 and time.time() - start < 20


def test_the_tool_hook_sees_the_command_inside_ttp_sandbox():
    from ttp import hook
    assert hook._strip_prefix("ttp sandbox -- pytest -q tests".split()) == ["pytest", "-q", "tests"]


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
    "ttp clip -- tt-flash --fw-tar fw.tar", "bash -c 'tt-topology -l mesh'", "ipmitool mc reset cold",
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
    from ttp import machines, push
    (tmp_path / "devdir").mkdir()
    (tmp_path / "devdir" / "0").write_text("")
    monkeypatch.setenv("TTP_DEVICE_NODES", str(tmp_path / "devdir"))
    monkeypatch.setenv("TTP_NO_DEVICE", "1")
    monkeypatch.setattr(machines, "device_host", lambda known=None: True)
    clean = dict(os.environ)
    argv = push.check_argv("pytest -q", env=clean)
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
    from ttp import push
    env = dict(os.environ)
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

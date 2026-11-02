# Process sandbox tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new `ttp sandbox` and check isolation tests here.
from __future__ import annotations


def _live_sandbox(monkeypatch):
    """A real sandbox for this test, nested when the suite itself runs in one; skips where the host
    cannot create user namespaces."""
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
    pre, why = sandbox.status()
    if not pre:
        pytest.skip(f"this host allows no process sandbox: {why}")
    return sandbox


def _no_sandbox_tools(monkeypatch):
    from ttp import sandbox
    monkeypatch.setattr(sandbox, "_STATE", {})
    monkeypatch.setattr(sandbox, "inside", lambda: False)
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


def test_ttp_checks_runs_each_check_in_the_sandbox_by_default(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    _live_sandbox(monkeypatch)
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    sentinel = subprocess.Popen(["sleep", "60"])
    try:
        cli.main(["checks", "--fresh", "--", f"test ! -e /proc/{sentinel.pid}"])
        assert json.loads((run / "checks.json").read_text())["passed"] is True
        assert "no process sandbox" not in capsys.readouterr().out
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait()


def test_check_argv_wraps_local_checks_unless_one_opts_out(tmp_path, monkeypatch):
    from ttp import push, pushpass, sandbox
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


def test_ttp_checks_reports_a_push_check_that_opts_out_of_the_sandbox(env, tmp_path, monkeypatch, capsys):
    from ttp import cli, sandbox
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
    # A check's own environment decides (push.check_argv passes it), not the caller's.
    from ttp import push
    assert "--keep-caps" in push.check_argv("true", env={**os.environ, "TTP_NO_DEVICE": "1"})
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


def _device_hook(monkeypatch, tmp_path, device_host=True, works=True, no_device="1", rewrite=False):
    from ttp import hook, machines, sandbox
    monkeypatch.setenv("TTP_RUN_DIR", str(tmp_path))
    if rewrite:
        monkeypatch.setenv(hook.HOOK_REWRITE, "1")
    else:
        monkeypatch.delenv(hook.HOOK_REWRITE, raising=False)
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    if no_device is None:
        monkeypatch.delenv("TTP_NO_DEVICE", raising=False)
    else:
        monkeypatch.setenv("TTP_NO_DEVICE", no_device)
    monkeypatch.setattr(machines, "device_host", lambda known=None: device_host)
    monkeypatch.setattr(sandbox, "status", lambda: (["unshare"], "") if works else (None, "no unshare"))

    def call(cmd):
        out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)})
        return out
    return call


@pytest.mark.parametrize("cmd, shown", [
    ("pytest -q tests/x.py", "ttp clip -- ttp sandbox -- pytest -q tests/x.py"),
    ("python3 -m pytest -k 'a and b'", "ttp clip -- ttp sandbox -- python3 -m pytest -k 'a and b'"),
    ("ttp clip -- python -m pytest", "ttp clip -- ttp sandbox -- python -m pytest"),
    ("PYTHONPATH=$PWD timeout 600 ctest --test-dir build", "PYTHONPATH=$PWD timeout 600 ttp clip -- ttp sandbox -- ctest"),
    ("cd x && ttp detach t -- pytest", "ttp detach t -- ttp sandbox -- pytest"),
    ("ttp sandbox -- true; pytest", "ttp clip -- ttp sandbox -- pytest"),
    ("bash -lc 'pytest -q'", "ttp clip -- ttp sandbox -- pytest -q"),
    ("ttp lock build -- pytest", "ttp lock build -- ttp sandbox -- pytest"),
    ("uv run pytest -q", "uv run ttp clip -- ttp sandbox -- pytest -q"),
])
def test_the_hook_refuses_a_no_device_test_run_outside_the_sandbox_on_a_device_host(cmd, shown, tmp_path, monkeypatch):
    out = _device_hook(monkeypatch, tmp_path)(cmd)
    why = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "TTP_NO_DEVICE=1" in why
    assert shown in why, why


@pytest.mark.parametrize("cmd", ["TTP_NO_DEVICE=0 ttp clip -- ttp sandbox -- pytest",
                                 "env -i PATH=$PATH ttp sandbox -- pytest", "unset TTP_NO_DEVICE; ttp sandbox -- pytest",
                                 "env -u TTP_NO_DEVICE ttp sandbox -- ctest"])
def test_the_hook_refuses_a_sandboxed_test_run_that_drops_ttp_no_device(cmd, tmp_path, monkeypatch):
    out = _device_hook(monkeypatch, tmp_path)(cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "leave TTP_NO_DEVICE" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", [
    "ttp clip -- ttp sandbox -- python3 -m pytest -q tests/x.py",
    "ttp sandbox -- ctest",
    "ttp sandbox -- bash -c 'pytest'",
    "TTP_NO_DEVICE=1 ttp sandbox -- pytest",
    "ttp lock box-device -- pytest -q",
    "ttp devq submit box --id a -- pytest -q",
    "git status; ls",
    "grep -n pytest notes.txt",
])
def test_the_hook_allows_sandboxed_device_and_non_test_runs(cmd, tmp_path, monkeypatch):
    assert _device_hook(monkeypatch, tmp_path)(cmd) is None


@pytest.mark.parametrize("kw", [{"no_device": "0"}, {"no_device": None}, {"device_host": False}])
def test_the_hook_allows_test_runs_of_device_runs_and_on_other_hosts(kw, tmp_path, monkeypatch):
    assert _device_hook(monkeypatch, tmp_path, **kw)("pytest -q") is None


@pytest.mark.parametrize("cmd, new", [
    ("pytest -q tests/x.py", "ttp clip -- ttp sandbox -- pytest -q tests/x.py"),
    ("cd x && python3 - <<'EOF'\nopen('a', 'w').write('b; pytest')\nEOF\npytest -q tests/a.py",
     "cd x && python3 - <<'EOF'\nopen('a', 'w').write('b; pytest')\nEOF\nttp clip -- ttp sandbox -- pytest -q tests/a.py"),
    ("cd x && python3 - <<EOF && python3 -m pytest -q -k 'a or b'\nimport a; a.b()\nEOF",
     "cd x && python3 - <<EOF && ttp clip -- ttp sandbox -- python3 -m pytest -q -k 'a or b'\nimport a; a.b()\nEOF"),
    ('PYTHONPATH="$PWD" timeout 600 ctest --test-dir "$B"', 'PYTHONPATH="$PWD" timeout 600 ttp clip -- ttp sandbox -- ctest --test-dir "$B"'),
    ("ttp clip -- python -m pytest", "ttp clip -- ttp sandbox -- python -m pytest"),
    ("git add -A; uv run pytest -q  # focused", "git add -A; uv run ttp clip -- ttp sandbox -- pytest -q  # focused"),
    ("make ||\n  pytest -q;\n", "make ||\n  ttp clip -- ttp sandbox -- pytest -q;\n"),
    ("ttp sandbox -- true; pytest", "ttp sandbox -- true; ttp clip -- ttp sandbox -- pytest"),
])
def test_the_hook_rewrites_a_trailing_no_device_test_run_into_the_sandbox(cmd, new, tmp_path, monkeypatch):
    from ttp import hook
    call = _device_hook(monkeypatch, tmp_path, rewrite=True)
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd, "description": "d", "timeout": 9},
                                "cwd": str(tmp_path)})
    o = out["hookSpecificOutput"]
    assert o["permissionDecision"] == "allow" and o["updatedInput"] == {"command": new, "description": "d", "timeout": 9}
    assert "ran in `ttp sandbox`" in o["additionalContext"] and "\n" not in o["additionalContext"]
    rows = [json.loads(x) for x in (tmp_path / hook.REFUSALS_FILE).read_text().splitlines()]
    assert [(r["kind"], r["command"], r["rewritten"]) for r in rows] == [("rewrite", cmd, new)]
    assert call(new) is None   # the rewritten command passes as it is


@pytest.mark.parametrize("cmd", [
    "pytest -q 2>&1 | tail -5", "pytest -q > out.txt", "pytest -q && git status", "(cd x && pytest)",
    "for f in a b; do pytest $f; done", "bash -lc 'pytest -q'", "sudo pytest", "pytest a; pytest b",
    "a |\npytest", "pytest -q &", "case a in a) pytest;; esac", "echo $(\npytest)", "pytest - <<EOF\nx\nEOF",
    "pytest -q \\\n  tests/x.py", "'pytest' -q", "cat <<EOF; pytest -q\nx\n", "bash <<EOF\npytest -q\nEOF",
])
def test_the_hook_refuses_a_no_device_test_run_it_cannot_rewrite_alone(cmd, tmp_path, monkeypatch):
    from ttp import hook
    out = _device_hook(monkeypatch, tmp_path, rewrite=True)(cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "updatedInput" not in out["hookSpecificOutput"]
    assert [json.loads(x)["kind"] for x in (tmp_path / hook.REFUSALS_FILE).read_text().splitlines()] == ["no_device"]


@pytest.mark.parametrize("cmd", ["TTP_NO_DEVICE=0 pytest -q", "env -i PATH=$PATH pytest", "unset TTP_NO_DEVICE; pytest",
                                 "env -u TTP_NO_DEVICE ctest", "TTP_NO_DEVICE= ttp sandbox -- pytest"])
def test_the_hook_never_rewrites_a_test_run_that_changes_ttp_no_device(cmd, tmp_path, monkeypatch):
    out = _device_hook(monkeypatch, tmp_path, rewrite=True)(cmd)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "leave TTP_NO_DEVICE" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_fault_in_the_rewrite_refuses_the_call(tmp_path, monkeypatch):
    from ttp import hook
    call = _device_hook(monkeypatch, tmp_path, rewrite=True)
    monkeypatch.setattr(hook, "_sandboxed_tail", lambda *a: 1 / 0)
    assert call("pytest -q")["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_hook_refuses_instead_of_rewriting_without_a_cli_that_applies_updated_input(tmp_path, monkeypatch):
    for value in (None, "0"):
        call = _device_hook(monkeypatch, tmp_path)
        if value:
            monkeypatch.setenv("TTP_HOOK_REWRITE", value)
        out = call("cd x && pytest -q")
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "cd x && ttp clip -- ttp sandbox -- pytest -q" not in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert _device_hook(monkeypatch, tmp_path, rewrite=True)("ttp clip -- ttp sandbox -- pytest -q") is None


@pytest.mark.parametrize("version, on", [("2.1.296 (Claude Code)", True), ("2.0.10 (Claude Code)", True),
                                         ("2.0.9 (Claude Code)", False), ("1.9.99", False), ("", False)])
def test_claude_workers_get_hook_rewrites_only_from_a_cli_that_applies_updated_input(version, on, monkeypatch):
    from ttp.hook import HOOK_REWRITE
    from ttp.providers import base, get_provider
    prov = get_provider("claude")
    monkeypatch.setattr(type(prov), "binary", lambda self: "/x/claude")
    monkeypatch.setattr(type(prov), "supports", lambda self, flag: False)
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/claude", "--version"), version)
    kw = dict(role="worker", model="opus", effort="low", cwd=".", budget_usd=None, schema=None, restrictions={})
    assert (prov.build(read_only=False, **kw)[1].get(HOOK_REWRITE) == "1") is on
    assert HOOK_REWRITE not in prov.build(read_only=True, **kw)[1]


def test_the_hook_gives_one_notice_where_no_sandbox_works(tmp_path, monkeypatch):
    call = _device_hook(monkeypatch, tmp_path, works=False)
    out = call("pytest -q")
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "no process sandbox works here (no unshare)" in out["hookSpecificOutput"]["additionalContext"]
    assert call("pytest -q") is None


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
    env.pop("TTP_RUN_DIR", None)
    for rc, args in ((3, "-m pytest -q t.py"), (0, "-m pytest -q -n 4 t.py")):
        py = _fake_xdist_python(tmp_path, rc)
        r = subprocess.run([sys.executable, "-m", "ttp", tool, "--", py, "-m", "pytest", "-q", "-n", "4", "t.py"],
                           env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and f"ARGS: {args}" in r.stdout, (r.stdout, r.stderr)
        assert ("dropped -n 4" in r.stdout + r.stderr) is (rc == 3), (r.stdout, r.stderr)

# Bounded children in a session of their own (ttp.boundedchild), and the static guard on the runtime's
# other own-session sites.

def _bc_alive(pid: int) -> bool:
    """`pid` runs (a zombie waiting for its reaper counts as gone)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _bc_all_gone(pids, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while any(_bc_alive(p) for p in pids):
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


def _bc_family(tmp_path):
    """A shell command that writes its own pid and a grandchild's, then waits on the grandchild."""
    kid, grand = tmp_path / "kid.pid", tmp_path / "grand.pid"
    cmd = f"sleep 60 & echo $! > {grand}.tmp; mv {grand}.tmp {grand}; echo $$ > {kid}; wait"
    return cmd, kid, grand


def _bc_pids(*files, timeout=10.0):
    end = time.monotonic() + timeout
    while not all(f.exists() and f.read_text().strip() for f in files):
        assert time.monotonic() < end, "the child never started"
        time.sleep(0.02)
    return [int(f.read_text()) for f in files]


@pytest.mark.parametrize("how", ["SIGTERM", "SIGHUP", "python-handler"])
def test_a_signalled_caller_of_boundedchild_leaves_no_child_or_grandchild(tmp_path, how):
    """The caller gets SIGTERM or SIGHUP while it waits: the child's whole group is killed, then the
    signal goes on as before (the default action ends the caller by it; a Python handler runs)."""
    cmd, kid, grand = _bc_family(tmp_path)
    handler = ("import signal\n"
               "def h(s, f):\n    raise SystemExit(7)\n"
               "signal.signal(signal.SIGTERM, h)\n") if how == "python-handler" else ""
    script = (f"import sys\nsys.path.insert(0, {str(RUNTIME)!r})\n{handler}"
              "from ttp import boundedchild\n"
              f"boundedchild.run(['sh', '-c', {cmd!r}], 60)\n"
              "sys.exit(0)\n")
    caller = subprocess.Popen([sys.executable, "-c", script])
    try:
        pids = _bc_pids(kid, grand)
        caller.send_signal(signal.SIGHUP if how == "SIGHUP" else signal.SIGTERM)
        rc = caller.wait(timeout=10)
    finally:
        if caller.poll() is None:
            caller.kill()
    assert rc == {"SIGTERM": -signal.SIGTERM, "SIGHUP": -signal.SIGHUP, "python-handler": 7}[how]
    assert _bc_all_gone(pids), "the child or grandchild outlived its caller"


def test_boundedchild_timeout_kills_grandchildren_and_says_timed_out(tmp_path):
    from ttp import boundedchild
    cmd, kid, grand = _bc_family(tmp_path)
    t0 = time.monotonic()
    res = boundedchild.run(["sh", "-c", f"echo started; {cmd}"], 0.5, grace=2)
    assert time.monotonic() - t0 < 5
    assert res.timed_out and res.rc is None and "timed out" in res.error
    assert res.out.startswith("started"), "the output so far is kept"
    assert _bc_all_gone(_bc_pids(kid, grand, timeout=1))


def test_boundedchild_normal_exit_returns_rc_and_output_and_kills_leftovers(tmp_path):
    from ttp import boundedchild
    res = boundedchild.run(["sh", "-c", "echo out; echo err >&2; exit 3"], 10)
    assert (res.rc, res.out, res.err, res.timed_out, res.error) == (3, "out\n", "err\n", False, "")
    res = boundedchild.run("cat; echo more >&2", 10, shell=True, input=b"in\n", text=False, stderr="stdout")
    assert (res.rc, res.out) == (0, b"in\nmore\n")
    # a grandchild left running in the group (its pipes closed) is killed when the call returns
    grand = tmp_path / "grand.pid"
    res = boundedchild.run(["sh", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {grand}"], 10)
    assert res.rc == 0 and _bc_all_gone([int(grand.read_text())])
    res = boundedchild.run([str(tmp_path / "missing")], 10)
    assert res.rc is None and not res.timed_out and res.error.startswith("could not start")


def test_boundedchild_restores_handlers_kills_on_an_exception_and_hooks_nothing_off_the_main_thread(tmp_path):
    from ttp import boundedchild
    before = {s: signal.getsignal(s) for s in boundedchild.HOOKED}
    seen = {}

    def started(pid):   # inside the call, on the main thread: hooked
        seen["main"] = {s: signal.getsignal(s) for s in boundedchild.HOOKED}
        raise KeyboardInterrupt
    cmd, kid, grand = _bc_family(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        boundedchild.run(["sh", "-c", cmd], 60, on_start=lambda pid: (_bc_pids(kid, grand), started(pid)))
    assert all(seen["main"][s] is not before[s] for s in before), "the main thread's call hooks the signals"
    assert {s: signal.getsignal(s) for s in boundedchild.HOOKED} == before, "the caller's handlers are back"
    assert _bc_all_gone(_bc_pids(kid, grand, timeout=1)), "an exception kills the group"
    # off the main thread: no hooks (Python cannot set them there), the timeout still kills the group
    cmd2, kid2, grand2 = _bc_family(tmp_path / "t")
    (tmp_path / "t").mkdir()
    out = {}

    def worker():
        out["res"] = boundedchild.run(["sh", "-c", cmd2], 0.5, grace=2,
                                      on_start=lambda pid: out.setdefault("h", signal.getsignal(signal.SIGTERM))
                                      is not None)
    th = threading.Thread(target=worker)
    th.start()
    th.join(10)
    assert out["res"].timed_out and out["h"] is before[signal.SIGTERM]
    assert _bc_all_gone(_bc_pids(kid2, grand2, timeout=1))
    # an on_start that says stop: killed at once
    res = boundedchild.run(["sleep", "60"], 60, on_start=lambda pid: False)
    assert res.rc is None and res.error == "stopped" and not res.timed_out


# The static guard. Each own-session site in runtime/ttp outside boundedchild is here, keyed by its
# module and enclosing function, with why it does not use the helper, or carries `# own session: <why>`
# on a line of its statement or the line above it.
OWN_SESSION_ALLOWED = {
    "awake.py::IdleHold.update": "the sleep inhibitor lives across calls; IdleHold stops it when work ends",
    "batch.py::_stream": "streams a push batch step's output line by line, so it cannot wait in one call; kills its group itself",
    "cli.py::cmd_web": "the web tunnel is meant to outlive the command that opened it",
    "cli.py::_run_check": "a ttp checks command; _CHECK_STOP's signal handling kills its group",
    "cli.py::_checks_start": "ttp checks --detach: the checks run on after the caller returns",
    "cli.py::<module>": "ttp detach --remote's driver script, meant to outlive ssh and this run",
    "daemon.py::<module>": "the detached tip-check driver script, meant to outlive the daemon's tick",
    "daemon.py::Daemon.start_run": "a run's supervisor, meant to outlive a daemon restart",
    "daemon.py::Daemon._probe_now": "daemon probe on its own timeout clock; kills its group itself",
    "daemon.py::Daemon._start_probe": "daemon probe polled across ticks, killed by the daemon on its timeout",
    "daemon.py::Daemon._start_tip_check": "detached tip-check rerun, meant to outlive the daemon's tick",
    "daemon.py::Daemon._start_on_pass": "detached on_pass check, polled across ticks",
    "deploy.py::launch_takeover": "a host-side upgrade takeover, meant to outlive its caller",
    "detach.py::start": "ttp detach: the job is meant to outlive its caller",
    "devq.py::start": "the device queue runner service",
    "devq.py::Runner.sh": "device queue runner step; the runner service kills its group on timeout",
    "devq.py::Runner._launch": "a device queue job, meant to outlive the runner's call",
    "devq.py::_readable": "bounded child; devq.py runs as a standalone script on hosts, so it cannot import the helper",
    "ends.py::spawn_probe": "daemon probe polled across ticks; its callers kill the group",
    "heal.py::_exec": "daemon heal check tracked by schedrun.spawned, killed by a daemon stop",
    "hold.py::_spawn": "ttp hold's holder process, meant to outlive the command",
    "machine_ledger.py::Probes._auto_undos": "daemon auto-undo polled across ticks",
    "machines.py::push_background": "background machines-list copy, meant to outlive the command",
    "push.py::_start": "ttp push --detach: the push runs on after the caller returns",
    "pushq.py::_spawn": "a push queue batch, meant to outlive the daemon's tick",
    "release.py::launch": "an auto-upgrade, meant to outlive its caller",
    "remotecheck.py::_run_script": "a script run on a remote host; its own watcher stops that group",
    "runner.py::_start_watchdog": "the watchdog that ends the agent's group when the runner dies",
    "runner.py::supervise": "the agent process, owned and stopped by its runner",
    "sandbox_init.py::main": "the sandbox's init puts its command in a session of its own",
    "schedrun.py::_run_group": "daemon schedules: schedrun.run_group owns and kills its groups",
    "service.py::_spawn": "starts the daemon service, meant to outlive the command",
    "service.py::take_restart_request": "the service restart helper, meant to outlive the daemon",
    "upstream.py::spawn_fallback": "background notes forwarder, meant to outlive the caller",
}

# in a string (a script run elsewhere): Python's start_new_session or os.setsid, or setsid as a shell command
_SHELL_SETSID = re.compile(r"start_new_session\s*=\s*(?!False)\w|\bos\.setsid\b"
                           r"|(?:^|[;&|(\n]|\bthen|\bdo|\bexec|\belse)[ \t]*setsid[ \t]", re.M)


def _own_session_sites(root: pathlib.Path) -> list[tuple[str, int, bool]]:
    """(module::function, line, marked) of each own-session use under `root`: start_new_session or
    setsid= not literally False (also as a dict key for **kwargs), os.setsid or an imported setsid, and
    a string (not a docstring) holding either or running setsid as a shell command."""
    import ast
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel == "boundedchild.py":
            continue
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src, rel)
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}

        def visit(node, scope, first):
            for child in ast.iter_child_nodes(node):
                inner, top = scope, child.lineno if isinstance(child, ast.stmt) else first
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    inner = f"{scope}.{child.name}" if scope else child.name
                hit = None
                if isinstance(child, ast.keyword) and child.arg in ("start_new_session", "setsid"):
                    hit = not (isinstance(child.value, ast.Constant) and child.value.value is False)
                elif isinstance(child, ast.Dict):
                    hit = any(isinstance(k, ast.Constant) and k.value in ("start_new_session", "setsid")
                              for k in child.keys)
                elif isinstance(child, ast.Attribute):
                    hit = child.attr == "setsid"
                elif isinstance(child, ast.ImportFrom):
                    hit = any(a.name == "setsid" for a in child.names)
                elif isinstance(child, ast.Constant) and isinstance(child.value, str) and id(child) not in docs:
                    hit = bool(_SHELL_SETSID.search(child.value))
                if hit:
                    n = getattr(child, "lineno", None) or getattr(child.value, "lineno", 0)
                    near = lines[max(0, min(n, top) - 2):n]   # from the line above its statement
                    found.append((f"{rel}::{scope or '<module>'}", n, any("# own session:" in ln for ln in near)))
                visit(child, inner, top)
        visit(tree, "", 1)
    return found


def _own_session_violations(root: pathlib.Path, allowed: dict) -> list[str]:
    return [f"{key} line {n}" for key, n, marked in _own_session_sites(root) if not marked and key not in allowed]


def test_own_session_sites_are_marked_or_allowlisted_and_the_list_is_current():
    root = RUNTIME / "ttp"
    assert not _own_session_violations(root, OWN_SESSION_ALLOWED), (
        "a new own-session child: run it through ttp.boundedchild, or mark it `# own session: <why>`")
    keys = {key for key, _, marked in _own_session_sites(root) if not marked}
    assert not set(OWN_SESSION_ALLOWED) - keys, "an allowlisted site is gone: drop it from the list"
    assert all(why.strip() for why in OWN_SESSION_ALLOWED.values())


@pytest.mark.parametrize("code", [
    "subprocess.Popen(['true'], start_new_session=True)",
    "subprocess.Popen(['true'],\n                 start_new_session=group)",
    "subprocess.Popen(['true'], **{'start_new_session': True})",
    "subprocess.Popen(['true'], preexec_fn=os.setsid)",
    "from os import setsid as s",
    "os.posix_spawnp('true', ['true'], {}, setsid=True)",
    "subprocess.run(['ssh', 'h', 'if x; then setsid sh -c y; fi'])",
    "CMD = '''\nsetsid nohup sh -c y\n'''",
    "SCRIPT = 'subprocess.Popen(argv, start_new_session=True)'",
])
def test_the_own_session_guard_fails_on_a_seeded_unmarked_site(tmp_path, code):
    mod = tmp_path / "seeded.py"
    mod.write_text(f"import os, subprocess\n\n\ndef spawn(group=True):\n    {code}\n")
    got = _own_session_violations(tmp_path, {})
    assert len(got) == 1 and got[0].startswith("seeded.py::spawn line "), got
    assert not _own_session_violations(tmp_path, {"seeded.py::spawn": "a reason"})
    marked = f"import os, subprocess\n\n\ndef spawn(group=True):\n    # own session: a test\n    {code}\n"
    mod.write_text(marked)
    assert not _own_session_violations(tmp_path, {})
    # mentions are not uses: a docstring, a help text, a False flag, a wrapper's name
    mod.write_text('"""Runs it under setsid nohup."""\nimport subprocess\nW = {"setsid", "nohup"}\n'
                   'H = "start the job with setsid nohup"\n'
                   "subprocess.Popen(['true'], start_new_session=False)\n")
    assert not _own_session_violations(tmp_path, {})

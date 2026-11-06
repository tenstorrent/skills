"""A process sandbox for check and test commands: `ttp sandbox -- <cmd...>`, and every check the
runtime runs (push.check_argv).

Off by default, with no exception: commands run as they are, unsandboxed, and see the host's processes,
network and sudo, and `ttp sandbox -- <cmd>` is a plain pass-through. A project turns it on with
project.json `sandbox.checks: true` (the daemon passes TTP_SANDBOX_CHECKS=1 to its runs and checks).
Device safety is no sandbox: a run that holds no device (TTP_NO_DEVICE=1) gets the TT_VISIBLE_DEVICES
mask and the tool hook refuses its device commands outside a device `ttp lock` or `ttp devq`
(hook.device_command); a project adds its own refusals in guards.local.

When on, the command runs in a PID namespace of its own with a private /proc, so it sees only its own process
tree: a kill, pkill or os.kill aimed at any other pid on the host reaches nothing, and every process
it started ends with it. Network, files, environment, working directory and exit code stay as they
are. It needs unprivileged user namespaces and util-linux `unshare` or bubblewrap `bwrap`; where
neither works (a sysctl or AppArmor setting, a missing tool, not Linux) commands run as before and
`notice` says why, once per run. A command already in a sandbox runs as it is.

In a sandbox the project turned on, under `TTP_NO_DEVICE=1` (a run that holds no device) the device driver's nodes
(machines.device_nodes()) are hidden too: an empty tmpfs over each that exists (bwrap --tmpfs; under
unshare, sandbox_init mounts it in the namespace's own mounts and then drops the capabilities), so a
raw open, or reset code that ignores TT_VISIBLE_DEVICES, finds no device. A sandbox nested in one
that left them visible hides them itself. sudo fails inside either way (host root is not mapped).
Accepted gap: a power command sent over the network to a board management controller needs neither
a device node nor sudo, and nothing here stops it."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

INIT = Path(__file__).with_name("sandbox_init.py")
PROBE_S = 20
TIMED_OUT = f"still running {2 * PROBE_S} s after its start"
# The sysctls that turn unprivileged user namespaces off: (name, value that does, what turns them on).
SETTINGS = (("kernel.apparmor_restrict_unprivileged_userns", "1",
             "set it to 0, or give unshare or bwrap an AppArmor profile that allows user namespaces"),
            ("kernel.unprivileged_userns_clone", "0", "set it to 1"),
            ("user.max_user_namespaces", "0", "set it above 0"))
SYSCTL_DIR = Path("/proc/sys")
_STATE: dict[str, tuple[list[str] | None, str]] = {}   # by PATH: what status() found
ENV = "TTP_SANDBOX_CHECKS"   # "1": the project turned the process sandbox on (sandbox.checks); not checkreap.MARKER


def opted_in(env: dict | None = None) -> bool:
    """Whether the project turned the process sandbox on (TTP_SANDBOX_CHECKS=1 in `env`, default this
    process's environment)."""
    return (os.environ if env is None else env).get(ENV, "").strip() == "1"


def _tools() -> list[tuple[str, list[str]]]:
    out = []
    if u := shutil.which("unshare"):
        out.append(("unshare", [u, "--user", "--map-current-user", "--pid", "--fork", "--mount-proc", "--kill-child"]))
    if b := shutil.which("bwrap"):
        out.append(("bwrap", [b, "--unshare-pid", "--die-with-parent", "--dev-bind", "/", "/", "--proc", "/proc"]))
    return out


def _init() -> list[str]:
    return [sys.executable or "python3", "-I", "-S", str(INIT)]


def _setting() -> str:
    """The kernel setting that keeps user namespaces off and how to turn them on, or ""."""
    for name, off, fix in SETTINGS:
        try:
            if (SYSCTL_DIR / name.replace(".", "/")).read_text().strip() == off:
                return f"sysctl {name}={off} turns off unprivileged user namespaces ({fix})"
        except OSError:
            pass
    return ""


def _probe() -> tuple[list[str] | None, str]:
    if not sys.platform.startswith("linux"):
        return None, "PID namespaces need Linux"
    tools = _tools()
    if not tools:
        return None, "neither unshare (util-linux) nor bwrap (bubblewrap) is installed; install either"
    # Proof of isolation, not only a start: this process must be out of sight inside.
    me = os.getpid()
    test = ["sh", "-c", f"test ! -e /proc/{me}"] if me > 10 else ["true"]
    errors = []
    for name, pre in tools:
        try:
            rc, err = _run_probe([*pre, *_init(), *test])
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{name}: {e}")
            continue
        if rc == 0:
            return pre, ""
        last = TIMED_OUT if rc is None else (err.strip().splitlines() or [f"exit {rc}"])[-1]
        errors.append(f"{name}: {last}")
    why = "; ".join(errors)
    return None, (f"{setting} ({why})" if (setting := _setting()) else why)


def _run_probe(argv: list[str]) -> tuple[int | None, str]:
    """(exit code, stderr) of a probe command; None for one still running at its second deadline.
    A stall that freezes this process past a deadline makes communicate() time out even on a command
    that ended meanwhile; the next deadline reads that one's answer at once."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        for _ in range(2):
            try:
                _, err = proc.communicate(timeout=PROBE_S)
                return proc.returncode, err
            except subprocess.TimeoutExpired:
                pass
        return None, ""
    finally:   # hung or interrupted: never left running; a child holding the pipes never blocks this
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for f in (proc.stdout, proc.stderr):
            f.close()


def status() -> tuple[list[str] | None, str]:
    """(the argv prefix that sandboxes a command, "") where a sandbox works, else (None, why).
    Probed once per process and PATH; a probe that timed out (a loaded host, say) is no verdict: the
    next call probes again, so a slow moment never runs every later command of the process unsandboxed."""
    key = os.environ.get("PATH", "")
    if key in _STATE:
        return _STATE[key]
    got = _probe()
    if TIMED_OUT not in got[1]:
        _STATE[key] = got
    return got


def inside() -> bool:
    """This process already runs in a `ttp sandbox` (its init is the namespace's first or, under
    bwrap, second process)."""
    for pid in (1, 2):
        try:
            if INIT.name.encode() in Path(f"/proc/{pid}/cmdline").read_bytes():
                return True
        except OSError:
            pass
    return False


def device_nodes(env: dict | None = None) -> list[str]:
    """The device nodes a command started with `env` (default this process's) must not see: those
    that exist, under TTP_NO_DEVICE=1."""
    if (os.environ if env is None else env).get("TTP_NO_DEVICE", "").strip() != "1":
        return []
    from .machines import device_nodes as nodes
    return [p for p in nodes() if os.path.lexists(p)]


def _visible(paths: list[str]) -> bool:
    """Any of `paths` still shows something: a node, or a directory that is not empty."""
    for p in paths:
        try:
            if not os.path.isdir(p) or os.listdir(p):
                return True
        except OSError:
            return True
    return False


def wrap(argv: list[str], on: bool = True, env: dict | None = None) -> list[str]:
    """`argv` run in a sandbox where one works, `on`, and the project opted in (opted_in), hiding the
    device nodes there (device_nodes); as it is otherwise, by default, or when already in one that hides
    what this run must not see. `env` is the command's environment (default this process's)."""
    hide = device_nodes(env) if on else []
    if not on or not opted_in(env) or (inside() and not _visible(hide)):
        return list(argv)
    pre, _ = status()
    if not pre:
        return list(argv)
    if not hide:
        return [*pre, *_init(), *argv]
    if os.path.basename(pre[0]) == "bwrap":
        mounts = [w for p in hide for w in (["--tmpfs", p] if os.path.isdir(p) else ["--ro-bind", "/dev/null", p])]
        return [*pre, *mounts, *_init(), *argv]
    return [pre[0], "--keep-caps", *pre[1:], *_init(), *[f"--hide={p}" for p in hide], *argv]


def notice(on: bool = True) -> str:
    """One line for a run's output when its commands run without the sandbox it should have; "" when
    they run in one, or by default (the project did not opt in)."""
    if not opted_in():
        return ""
    if not on:
        return ("sandbox off (\"sandbox\": false): this check sees and can signal every process of "
                "this user on the host")
    if inside():
        return ""
    pre, why = status()
    if pre:
        return ""
    devices = " and open the device nodes" if device_nodes() else ""
    return f"no process sandbox: {why}. Commands run unisolated and can see and signal other processes{devices}"


def _test_slots(argv: list[str], env: dict):
    """A worker's pytest run `argv` with its workers taken from the host's test pool (testslots.direct):
    its -n lowered to the grant, run at a lower priority, `env` given the grant's variables; and the grant
    (None: none taken), kept open while the command runs: its files are passed to the command (pass_fds),
    so they stay held for its life and free when it ends."""
    from . import testslots

    def tell(msg: str) -> None:
        print(f"ttp sandbox: {msg}", file=sys.stderr, flush=True)
    line = shlex.join(argv)
    who = f"ttp sandbox (task #{os.environ.get('TTP_TASK') or '?'}, pid {os.getpid()}): {line[:80]}"
    if (grant := testslots.direct(line, who, tell)) is None:
        return argv, None
    if (capped := testslots.clamp(line, grant.k)) != line:
        tell(f"pytest workers lowered to -n {grant.k} (this run's host test slots)")
        argv = shlex.split(capped)
    env.update(grant.env())
    return testslots.low_priority(argv), grant


def run(argv: list[str]) -> int:
    """`ttp sandbox -- <cmd...>`: run `argv` and return its exit code; by default it runs as it is (a
    pass-through: existing configs that call it keep working), in a sandbox only where the project
    turned one on. A SIGTERM or SIGHUP to this process (a `timeout`, a closed terminal) ends the whole namespace. A pytest run's xdist
    options are dropped where xdist is not installed (testslots.drop_xdist). The command gets the
    account's leak-pattern file by path (leakfile), as checks do, and the sudo wrapper (sudowatch): once
    it ends, stderr says what sudo it ran and which root processes that left running."""
    from . import hoststate, leakfile, sudowatch, testslots
    # The host's real crontab, systemd user units and power state are out of its reach (hoststate)
    run_dir = os.environ.get("TTP_RUN_DIR")
    env = hoststate.fake_env(leakfile.with_path(dict(os.environ)),
                             Path(run_dir) / "hoststate" if run_dir and os.path.isdir(run_dir) else None)
    temp = not os.environ.get(hoststate.MARKER) and not (run_dir and os.path.isdir(run_dir))
    argv, dropped = testslots.drop_xdist(argv)
    if dropped:
        print(f"ttp sandbox: {dropped}", file=sys.stderr, flush=True)
    argv, grant = _test_slots(list(argv), env)
    try:
        watch = sudowatch.Watch(env)
        env = watch.env
    except OSError:
        watch = None
    wrapped = wrap(argv)
    plain = wrapped == list(argv)
    if plain and (line := notice()):
        print(f"ttp sandbox: {line}", file=sys.stderr, flush=True)
    try:
        guard = hoststate.Guard(env)
        try:
            proc = subprocess.Popen(wrapped, env=env, pass_fds=grant.fds() if grant else ())
        except OSError as e:
            print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
            if temp and env.get(hoststate.MARKER):
                shutil.rmtree(env[hoststate.MARKER], ignore_errors=True)
            return 127 if isinstance(e, FileNotFoundError) else 126

        def end(*_):
            try:
                # unshare --kill-child / bwrap --die-with-parent: the namespace goes with it
                proc.send_signal(signal.SIGTERM) if plain else proc.kill()
            except OSError:
                pass

        signal.signal(signal.SIGTERM, end)
        signal.signal(signal.SIGHUP, end)
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a terminal's Ctrl-C reaches the command (or the sandbox init)
        rc = proc.wait()
        changed, said = guard.end()
        for line in said:
            print(f"ttp sandbox: {line}", file=sys.stderr, flush=True)
        if temp and env.get(hoststate.MARKER):
            shutil.rmtree(env[hoststate.MARKER], ignore_errors=True)
        rc = rc if rc >= 0 else 128 - rc
        return 1 if changed and rc == 0 else rc
    finally:
        if watch is not None:
            try:
                lines = watch.report(what="the command")
                for ln in lines:
                    print(f"ttp sandbox: {ln}", file=sys.stderr, flush=True)
                sudowatch.observe(lines, f"{os.environ.get('TTP_RUN_ID') or os.getpid()}:{watch.started}")
            finally:
                watch.close()

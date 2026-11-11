# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep the daemon alive across logouts, crashes and reboots, without root.

Linux: a systemd user unit (restart on failure) plus `loginctl enable-linger` so it runs without
a login session and starts at boot. Where linger is refused, a crontab watchdog (@reboot plus
every 5 minutes) restarts the daemon instead. macOS: a launchd agent with KeepAlive and the
project folder as working directory (never `/` or the home folder).

A daemon that is alive but hung (no tick progress for config `watchdog.hang_s`) is dumped and
restarted by `ttp.watchdog` (see there), run every 5 minutes: by a systemd user timer, a second
launchd agent or the crontab entry. systemd's WatchdogSec (the daemon pings it after each tick and
between the steps of a long one) is the backstop behind it, set past the watchdog's backed-off waits;
its WatchdogSignal=SIGUSR1 leaves the stacks in state/daemon-stacks.log before systemd kills it.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import poll_s
from .project import Project, durable_write, open_append_or_null, tree_writes
from . import gitmemo, hoststate


def unit_name(p: Project) -> str:
    return "tt-project-" + re.sub(r"[^A-Za-z0-9_.-]", "-", p.name)


WATCHDOG_EVERY_S = 300   # how often launchd and cron look for a stuck daemon
# launchd's ProcessType Background runs an agent and every child (workers, git, pytest, ttp push) at
# darwin background priority: efficiency cores only and throttled I/O. The daemon runs Standard; its
# workers, reviewers and checks stay polite at runner.nice. The watchdog runs Standard too (on a loaded
# host a Background watchdog is starved past the hang it should end); tunnels stay Background.
# An installed daemon or watchdog plist lacking any of these keys is rewritten on upgrade (refresh).
DAEMON_KEYS = {"ProcessType": "Standard", "AbandonProcessGroup": True}
WATCHDOG_KEYS = {"ProcessType": "Standard"}
# `launchctl bootout` can return before launchd has removed the job (it waits for the job's processes to
# exit, up to its ExitTimeOut): a bootstrap right after fails while the label is still loaded. A reload waits
# for the label to unload, at most UNLOAD_WAIT_S, then tries the bootstrap BOOTSTRAP_TRIES times; a job
# launchd already loaded (KeepAlive, the watchdog, another restart) counts as loaded.
UNLOAD_WAIT_S = 30
BOOTSTRAP_TRIES = 3
BOOTSTRAP_RETRY_S = 1.0
# Darwin background priority is inherited: a daemon started by hand from a background or niced starter
# runs at that priority too. launchd's ProcessType Background stays on its children through fork and
# exec and no setpriority lifts it, so restart_service starts the daemon only through launchd from its own
# plist when one is installed (a failure is reported, never a hand start);
# leave_background() drops what it can (taskpolicy -b) and logs the priority the kernel applies.
PRIO_DARWIN_PROCESS = 4   # <sys/resource.h>: the which of setpriority for darwin background state
DARWIN_BG_PRI = 4         # `ps -o pri=` of a process at darwin background priority


def _ps_pri(pid: int | None = None) -> int | None:
    """A process's scheduling priority as the kernel applies it (`ps -o pri=`, 4 at darwin background
    priority, whichever way it was set); None where ps cannot say."""
    try:
        r = subprocess.run(["ps", "-o", "pri=", "-p", str(pid or os.getpid())], capture_output=True,
                           text=True, timeout=10)
        return int(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _leave_darwin_bg() -> None:
    """setpriority(PRIO_DARWIN_PROCESS, 0, 0): clears the background state taskpolicy -b sets; launchd's
    Background process type is not cleared by it."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.setpriority.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_int]
        libc.setpriority(PRIO_DARWIN_PROCESS, 0, 0)
    except (OSError, AttributeError):
        pass


def leave_background(plist: Path | None) -> str:
    """Leave darwin background priority where it can, unless this process's launchd plist sets ProcessType
    Background (a value the user or a coordinator set there is kept). Returns the priority it runs at, as
    `ps` reads it, for the start log: a warning when it still runs background without its plist saying so."""
    if sys.platform != "darwin":
        return ""
    try:
        kept = bool(plist) and plistlib.loads(plist.read_bytes()).get("ProcessType") == "Background"
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
        kept = False
    pri = _ps_pri()
    if pri is None:
        return "cpu priority: unknown (ps could not read it)"
    if pri > DARWIN_BG_PRI:
        return f"cpu priority: normal (pri {pri})"
    if kept:
        return f"cpu priority: background (pri {pri}; its launchd plist sets ProcessType Background)"
    _leave_darwin_bg()
    after = _ps_pri()
    if after is not None and after > DARWIN_BG_PRI:
        return f"cpu priority: normal (pri {after}; left the background priority it inherited from its starter)"
    return (f"warning: cpu priority: background (pri {after}) though its launchd plist does not set ProcessType "
            "Background: it inherited it from a background starter (a hand start from a background caller); "
            "a restart through launchd (launchctl kickstart -k) runs it at its plist's priority")


def daemon_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.daemon", str(p.base)]


def watchdog_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.watchdog", str(p.base)]


def overseer_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.overseer", str(p.base)]


def launchd_label(p: Project, watchdog: bool = False) -> str:
    return f"com.tt-project.{unit_name(p)}" + (".watchdog" if watchdog else "")


def _agent(p: Project, watchdog: bool = False) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launchd_label(p, watchdog)}.plist"


def _unit(p: Project) -> Path:
    return hoststate.unit_dir() / f"{unit_name(p)}.service"   # a test run's fake under its marker


def _env(p: Project) -> dict:
    from .providers.base import service_path
    return {"PYTHONPATH": str(p.harness / "runtime"), "PATH": service_path(), "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "C.UTF-8")}


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(hoststate.argv(argv), capture_output=True, text=True, timeout=60)


# A run's shell (a worker, review or coordinator turn) acts for its own project only. Upgrading another
# project from it still merges files; starting that project's daemon is left to its own service or host.
NO_FOREIGN_START = ("a run of one project never starts, restarts or reinstalls another project's daemon or services; "
                    "its own service, or `ttp start` or `ttp restart` run on the host outside a run, does that")


class ForeignRun(RuntimeError):
    """A start, restart or service install of a project asked from a run of another project."""


def foreign_run(p: Project) -> str:
    """The project folder of the run this process is in (TTP_RUN_ID and TTP_PROJECT set) when it is not
    p's, else ''. Push batches, the daemon and cron carry no TTP_RUN_ID: they are never foreign."""
    base = os.environ.get("TTP_PROJECT") or ""
    if not os.environ.get("TTP_RUN_ID") or not base:
        return ""
    try:
        return "" if Path(base).resolve() == Path(p.base).resolve() else base
    except OSError:
        return base


def foreign_refusal(p: Project, what: str) -> str:
    """Why `what` (start, restart, reinstall) of p's daemon is refused here, or '' when it is allowed."""
    other = foreign_run(p)
    return (f"refused to {what} {p.name}'s daemon from a run of another project ({other}): {NO_FOREIGN_START}"
            if other else "")


def install(p: Project) -> str:
    if why := foreign_refusal(p, "install the services of"):
        raise ForeignRun(why)
    if sys.platform == "darwin":
        return _installed_as(p, _install_launchd(p))
    if shutil.which("systemctl") and _run("systemctl", "--user", "is-system-running").returncode in (0, 1):
        msg = _install_systemd(p)
        if msg.startswith(SYSTEMD_FAILED):
            return _installed_as(p, msg, "systemd --user could not start the unit")
        linger = _run("loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger").stdout.strip()
        if linger != "Linger=yes":
            r = _run("loginctl", "enable-linger")
            if r.returncode != 0:
                return _installed_as(p, msg + "; linger refused, adding crontab watchdog: " + _install_cron(p),
                                     "loginctl enable-linger was refused, so the systemd unit stops at logout "
                                     "and does not start at boot")
            msg += "; linger enabled"
        return _installed_as(p, msg)
    return _installed_as(p, _install_cron(p), "systemd --user is not available here")


def _record(p: Project) -> Path:
    return p.state / "service.json"


def _installed_as(p: Project, msg: str, fallback: str = "") -> str:
    """Record what install() put in place, and why a crontab watchdog was added: `ttp doctor` reports it.
    The message gains a note when it edited the user's crontab, so that never happens silently."""
    if CRON_FAILED in msg:      # no crontab entry was added: the message already says why
        fallback = ""
    durable_write(_record(p), json.dumps({"text": msg, "fallback": fallback, "at": time.time()}) + "\n")
    _WATCHDOG_SEEN.pop(str(p.base), None)
    return msg + (f". Fallback: {CRON_NOTE}, because {fallback}" if fallback else "")


CRON_NOTE = "a crontab watchdog (entries tagged `# tt-project:` in your crontab, see `crontab -l`)"


def recorded(p: Project) -> dict:
    """What the last install() recorded ({} when none: --no-service, or a project older than the record)."""
    try:
        rec = json.loads(_record(p).read_text())
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _cron_present(p: Project) -> bool:
    return bool(shutil.which("crontab")) and f"{CRON_TAG}{p.base}" in _run("crontab", "-l").stdout


def service_line(p: Project) -> str:
    """`ttp doctor`: which mechanism keeps the daemon running, and why, when it is a crontab fallback."""
    fallback = recorded(p).get("fallback")
    if fallback and _cron_present(p):
        return f"service: {CRON_NOTE}, a fallback because {fallback}"
    svc = installed(p)
    if not svc:
        return f"service: none keeps the daemon running; `ttp start {p.name}` installs one"
    return f"service: {svc['kind']}" + (" with the stuck-daemon watchdog" if svc["watchdog"] else
                                        f" without the stuck-daemon watchdog (`ttp restart {p.name}` adds it)")


def plan() -> str:
    """`ttp setup`: the mechanism `ttp new` would install on this machine, and why, read-only (it changes
    nothing; install() makes the actual choice)."""
    if sys.platform == "darwin":
        return "launchd agents"
    if shutil.which("systemctl") and _run("systemctl", "--user", "is-system-running").returncode in (0, 1):
        linger = _run("loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger").stdout.strip()
        if linger == "Linger=yes":
            return "systemd user units (linger is on)"
        return ("systemd user units; linger is off, so `ttp new` asks `loginctl enable-linger` and, if that is "
                f"refused, also adds {CRON_NOTE}")
    return f"{CRON_NOTE}, because systemd --user is not available here"


def _watchdog_unit(p: Project, ext: str) -> Path:
    return hoststate.unit_dir() / f"{unit_name(p)}-watchdog.{ext}"


def backstop_s(p: Project) -> int:
    """systemd's own WatchdogSec: past ttp.watchdog's third backed-off wait, so it acts only where the
    watchdog timer does not run."""
    from .watchdog import settings
    return int(4 * settings(p)["hang_s"] + 300)


def _watchdog_units(p: Project) -> dict[Path, str]:
    """A timer that runs `ttp.watchdog` every 5 minutes, and the oneshot service it starts."""
    env = "\n".join(f"Environment={k}={v}" for k, v in _env(p).items())
    return {_watchdog_unit(p, "service"): f"""[Unit]
Description=tt-project watchdog for {p.name}

[Service]
Type=oneshot
WorkingDirectory={p.base}
ExecStart={' '.join(shlex.quote(a) for a in watchdog_argv(p))}
{env}
TimeoutStartSec={WATCHDOG_RUN_S}
""", _watchdog_unit(p, "timer"): f"""[Unit]
Description=Run the tt-project watchdog for {p.name} every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
"""}


def _write_watchdog_units(p: Project) -> bool:
    """Write the watchdog timer units where they differ; whether any changed."""
    changed = False
    for path, text in _watchdog_units(p).items():
        if not path.exists() or path.read_text(errors="replace") != text:
            durable_write(path, text)
            changed = True
    return changed


# How long systemd waits after SIGTERM before it force-kills the daemon. A stop skips the tick's
# remaining steps and ends the running one at a safe point (Daemon._run_step: a finish round rolled
# back, a looping step's next item), so the daemon exits within seconds. KillMode stays `process`:
# workers, checks and detached jobs must outlive a daemon restart, and neither the stop's SIGTERM nor
# its final SIGKILL reaches them. They stay in the unit's cgroup ("remains running after unit
# stopped" is only a notice; the next start ignores them), so no separate scope is needed for them.
STOP_TIMEOUT_S = 120


def _unit_text(p: Project) -> str:
    env = "\n".join(f"Environment={k}={v}" for k, v in _env(p).items())
    return f"""[Unit]
Description=tt-project daemon for {p.name}
After=network-online.target

[Service]
Type=simple
WorkingDirectory={p.base}
ExecStart={' '.join(shlex.quote(a) for a in daemon_argv(p))}
{env}
Restart=always
RestartSec=10
KillMode=process
TimeoutStopSec={STOP_TIMEOUT_S}
WatchdogSec={backstop_s(p)}
WatchdogSignal=SIGUSR1
NotifyAccess=main

[Install]
WantedBy=default.target
"""


def _overseer_unit(p: Project) -> Path:
    return hoststate.unit_dir() / f"{unit_name(p)}-overseer.service"


def _overseer_text(p: Project) -> str:
    """The overseer's own service (ttp.overseer): no lock, process or tick step shared with the daemon."""
    env = "\n".join(f"Environment={k}={v}" for k, v in _env(p).items())
    return f"""[Unit]
Description=tt-project overseer for {p.name}

[Service]
Type=simple
WorkingDirectory={p.base}
ExecStart={' '.join(shlex.quote(a) for a in overseer_argv(p))}
{env}
Restart=always
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


def _write_overseer_unit(p: Project) -> bool:
    path, text = _overseer_unit(p), _overseer_text(p)
    if path.exists() and path.read_text(errors="replace") == text:
        return False
    durable_write(path, text)
    return True


def ensure_overseer(p: Project) -> str:
    """Start the overseer again (the daemon calls this when its heartbeat is stale): through its systemd
    unit where the daemon has one, else as a detached process. Its own lock keeps a second one out."""
    if _unit(p).exists():
        if _write_overseer_unit(p):
            _run("systemctl", "--user", "daemon-reload")
        r = _run("systemctl", "--user", "enable", "--now", _overseer_unit(p).name)
        if r.returncode == 0:
            r = _run("systemctl", "--user", "restart", _overseer_unit(p).name)
        return "restarted its unit" if r.returncode == 0 else f"systemctl failed: {r.stderr.strip()[:200]}"
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env(p).items())
    cmd = (f"cd {shlex.quote(str(p.base))} && {env} {' '.join(shlex.quote(a) for a in overseer_argv(p))} "
           f">> {shlex.quote(str(p.logs / 'overseer.out'))} 2>&1")
    # own session: the overseer must outlive the daemon that starts it (it watches that daemon's loop)
    subprocess.Popen(["sh", "-c", cmd], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "started a detached overseer"


SYSTEMD_FAILED = "systemd failed"


def _install_systemd(p: Project) -> str:
    _unit(p).parent.mkdir(parents=True, exist_ok=True)
    durable_write(_unit(p), _unit_text(p))
    _write_watchdog_units(p)
    _write_overseer_unit(p)
    _run("systemctl", "--user", "daemon-reload")
    r = _run("systemctl", "--user", "enable", "--now", f"{unit_name(p)}.service")
    if r.returncode != 0:
        # Remove the unit that never ran: installed(), refresh() and restart_service() go by its file,
        # and would treat the crontab watchdog that takes over as a systemd service.
        _run("systemctl", "--user", "disable", f"{unit_name(p)}.service")
        for path in (_unit(p), *_watchdog_units(p), _overseer_unit(p)):
            path.unlink(missing_ok=True)
        _run("systemctl", "--user", "daemon-reload")
        return SYSTEMD_FAILED + " (" + r.stderr.strip()[:200] + "); " + _install_cron(p)
    w = _run("systemctl", "--user", "enable", "--now", _watchdog_unit(p, "timer").name)
    _run("systemctl", "--user", "enable", "--now", _overseer_unit(p).name)
    return f"systemd user unit {unit_name(p)}" + (" with a watchdog timer" if w.returncode == 0 else
                                                   f" (watchdog timer failed: {w.stderr.strip()[:200]})")


def _install_launchd(p: Project) -> str:
    label = launchd_label(p)
    plist = _agent(p)
    plist.parent.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    job = {"Label": label, "ProgramArguments": daemon_argv(p), "WorkingDirectory": str(p.base),
           "EnvironmentVariables": _env(p), "RunAtLoad": True, "KeepAlive": True, **DAEMON_KEYS,
           "ThrottleInterval": 10, "StandardOutPath": str(p.logs / "launchd.log"),
           "StandardErrorPath": str(p.logs / "launchd.log")}
    durable_write(plist, plistlib.dumps(job))
    r = _reload(label, plist)
    if r.returncode != 0:
        return f"launchd bootstrap failed: {_ctl_out(r)[:200]}"
    return f"launchd agent {label}" + _install_launchd_watchdog(p)


def _install_launchd_watchdog(p: Project) -> str:
    """A second agent that runs `ttp.watchdog` every few minutes: KeepAlive restarts a daemon that
    exits, not one that is alive but stuck."""
    label, plist = launchd_label(p, watchdog=True), _agent(p, watchdog=True)
    if os.environ.get("XPC_SERVICE_NAME") == label:
        # Never from inside its own job: the bootout would end this process before its bootstrap.
        return " with a watchdog (kept as loaded: this runs inside it)"
    job = {"Label": label, "ProgramArguments": watchdog_argv(p), "WorkingDirectory": str(p.base),
           "EnvironmentVariables": _env(p), "StartInterval": WATCHDOG_EVERY_S, **WATCHDOG_KEYS,
           "StandardOutPath": str(p.logs / "launchd.log"), "StandardErrorPath": str(p.logs / "launchd.log")}
    plist.parent.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    durable_write(plist, plistlib.dumps(job))
    r = _reload(label, plist)
    return " with a watchdog" if r.returncode == 0 else f" (watchdog bootstrap failed: {_ctl_out(r)[:200]})"


def _ctl_out(r: subprocess.CompletedProcess) -> str:
    return " ".join(x.strip() for x in (r.stderr or "", r.stdout or "") if x and x.strip()) or f"exit {r.returncode}"


def _target(label: str) -> tuple[str, str]:
    domain = f"gui/{os.getuid()}"
    return f"{domain}/{label}", domain


def launchd_loaded(label: str) -> bool:
    """launchd has job `label` loaded (`launchctl print` exits 0; 113 when it has no such job)."""
    return _run("launchctl", "print", _target(label)[0]).returncode == 0


def _wait_unloaded(label: str, wait_s: float = UNLOAD_WAIT_S) -> bool:
    """After a bootout: wait, at most wait_s, until launchd has removed the job; whether it did."""
    deadline = time.time() + wait_s
    while launchd_loaded(label):
        if time.time() >= deadline:
            return False
        time.sleep(0.5)
    return True


def bootstrap(label: str, plist: Path) -> subprocess.CompletedProcess:
    """Load agent `label` from its plist: bootstrap, retried a few times; a job launchd already has
    loaded counts as success. A failure keeps launchctl's own output."""
    target, domain = _target(label)
    r = subprocess.CompletedProcess([], 1, "", "")
    for i in range(BOOTSTRAP_TRIES):
        r = _run("launchctl", "bootstrap", domain, str(plist))
        if r.returncode == 0:
            return r
        if launchd_loaded(label):   # "service already loaded": loaded is what this wants
            return subprocess.CompletedProcess(r.args, 0, r.stdout, r.stderr)
        if i + 1 < BOOTSTRAP_TRIES:
            time.sleep(BOOTSTRAP_RETRY_S * (i + 1))
    return r


def _reload(label: str, plist: Path) -> subprocess.CompletedProcess:
    """bootout, wait for the label to unload, then bootstrap: how launchd takes a changed plist."""
    target, _ = _target(label)
    _run("launchctl", "bootout", target)
    if not _wait_unloaded(label):
        return subprocess.CompletedProcess([], 1, "", f"launchctl bootout {target}: still loaded {UNLOAD_WAIT_S}s "
                                                     f"later, so its bootstrap was not tried")
    return bootstrap(label, plist)


def ensure_loaded(p: Project) -> str | None:
    """The watchdog's launchd step: KeepAlive restarts a daemon only while its agent is loaded, so an agent
    left unloaded (a bootout whose bootstrap failed) is bootstrapped from its plist. None when it is loaded
    or not installed; else what was done."""
    label, plist = launchd_label(p), _agent(p)
    if sys.platform != "darwin" or not plist.exists() or launchd_loaded(label):
        return None
    r = bootstrap(label, plist)
    if r.returncode == 0:
        return f"bootstrapped its unloaded launchd agent {label}"
    return _manager_said(f"launchctl bootstrap {label}", r).replace("\n", " ")[:300]


CRON_TAG = "# tt-project:"
CRON_FAILED = "crontab install failed"


def _install_cron(p: Project) -> str:
    cur = _run("crontab", "-l").stdout if shutil.which("crontab") else ""
    tag = f"{CRON_TAG}{p.base}"
    keep = [ln for ln in cur.splitlines() if tag not in ln]
    keep += _cron_lines(p)
    r = subprocess.run(hoststate.argv(["crontab", "-"]), input="\n".join(keep) + "\n", text=True, capture_output=True)
    if r.returncode != 0:
        return CRON_FAILED + ": " + r.stderr.strip()[:200]
    # The daemon refuses to start twice (lock file), so the 5-minute entry is a no-op while it runs.
    _spawn(p)
    return "crontab watchdog"


def _cron_lines(p: Project) -> list[str]:
    """At boot, start the daemon; every 5 minutes, end a stuck one, then start one if none runs."""
    tag, cmd = f"{CRON_TAG}{p.base}", _cron_cmd(p)
    return [f"@reboot {cmd} {tag}", f"*/5 * * * * {_cron_cmd(p, watchdog_argv(p))}; {cmd} {tag}"]


def _spawn(p: Project) -> None:
    if why := foreign_refusal(p, "start"):
        raise ForeignRun(why)
    subprocess.Popen(["sh", "-c", _cron_cmd(p)], start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=_spawn_env())


# A run's own variables: the agent's per-command env file and session markers (CLAUDECODE, CLAUDE_CODE_*).
RUN_ENV = ("CLAUDE_ENV_FILE", "CLAUDECODE")
RUN_ENV_PREFIXES = ("TTP_", "CLAUDE_CODE_")
# Host-wide settings and test doubles a service start would see too.
HOST_ENV = ("TTP_HOME", "TTP_HOST", hoststate.MARKER)
HOST_ENV_PREFIXES = ("TTP_TEST_", "TTP_FAKE_")


def _spawn_env() -> dict:
    """The caller's env without the variables a run sets for itself (TTP_ROLE, TTP_RUN_ID, TTP_PROJECT, the
    agent's CLAUDE_ENV_FILE...), as a service or cron start would have it: a restart or upgrade run from
    another project's worker shell must not hand them to this project's daemon and its workers (an
    inherited CLAUDE_ENV_FILE made every worker Bash call fail). Host settings and test doubles stay."""
    return {k: v for k, v in os.environ.items()
            if k in HOST_ENV or k.startswith(HOST_ENV_PREFIXES)
            or not (k in RUN_ENV or k.startswith(RUN_ENV_PREFIXES))}


def _cron_cmd(p: Project, argv: list[str] | None = None) -> str:
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env(p).items())
    argv = argv or daemon_argv(p)
    return f"cd {shlex.quote(str(p.base))} && {env} {' '.join(shlex.quote(a) for a in argv)} " \
           f">> {shlex.quote(str(p.logs / 'daemon.out'))} 2>&1"


def uninstall(p: Project) -> str:
    done = []
    if sys.platform == "darwin":
        for wd in (True, False):
            _run("launchctl", "bootout", f"gui/{os.getuid()}/{launchd_label(p, wd)}")
            _agent(p, wd).unlink(missing_ok=True)
        done.append("launchd")
    unit = _unit(p)
    if _watchdog_unit(p, "timer").exists():
        _run("systemctl", "--user", "disable", "--now", _watchdog_unit(p, "timer").name)
    for ext in ("timer", "service"):
        _watchdog_unit(p, ext).unlink(missing_ok=True)
    if _overseer_unit(p).exists():
        _run("systemctl", "--user", "disable", "--now", _overseer_unit(p).name)
        _overseer_unit(p).unlink()
    if unit.exists():
        _run("systemctl", "--user", "disable", "--now", unit.name)
        unit.unlink()
        _run("systemctl", "--user", "daemon-reload")
        done.append("systemd")
    if shutil.which("crontab"):
        cur = _run("crontab", "-l").stdout
        tag = f"{CRON_TAG}{p.base}"
        if tag in cur:
            keep = [ln for ln in cur.splitlines() if tag not in ln]
            subprocess.run(hoststate.argv(["crontab", "-"]), input="\n".join(keep) + "\n", text=True)
            done.append("cron")
    return ", ".join(done) or "nothing installed"


def installed(p: Project) -> dict | None:
    """The service that keeps the daemon running, {"kind": "systemd" | "launchd" | "cron", "watchdog":
    whether it also restarts a stuck daemon (one installed before the watchdog existed does not)}, or
    None when there is none (`ttp stop`, or a daemon started by hand). Cached for a minute: the web
    app asks often."""
    now = time.time()
    hit = _WATCHDOG_SEEN.get(str(p.base))
    if hit and now - hit[0] < 60:
        return hit[1]
    found = None
    unit = _unit(p)
    if unit.exists():
        found = {"kind": "systemd", "watchdog": _watchdog_unit(p, "timer").exists()}
    elif sys.platform == "darwin" and _agent(p).exists():
        found = {"kind": "launchd", "watchdog": _agent(p, watchdog=True).exists()}
    elif shutil.which("crontab"):
        mine = [ln for ln in _run("crontab", "-l").stdout.splitlines() if f"{CRON_TAG}{p.base}" in ln]
        if mine:
            found = {"kind": "cron", "watchdog": any("ttp.watchdog" in ln for ln in mine)}
    _WATCHDOG_SEEN[str(p.base)] = (now, found)
    return found


def down_note(p: Project) -> str:
    """What happens to a daemon that is down or stuck, naming a step only where the harness has none."""
    from .watchdog import settings
    svc = installed(p)
    if svc and svc["watchdog"]:
        return (f"its {svc['kind']} service restarts it by itself (a stuck one after "
                f"{int(settings(p)['hang_s'] // 60)} min without a tick)")
    if svc:
        return (f"its {svc['kind']} service restarts it if it exits, but predates the watchdog for a stuck "
                f"one: `ttp restart {p.name}` restarts it and adds the watchdog")
    return f"no service keeps it running (stopped on purpose?): `ttp start {p.name}` starts it"


_WATCHDOG_SEEN: dict[str, tuple[float, str | None]] = {}


def update_plist(path: Path, want: dict) -> bool:
    """Set `want`'s keys in an installed agent's plist, keeping its other keys; True when it changed.
    launchd reads a plist only at bootstrap: reload the agent after."""
    try:
        job = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    if not isinstance(job, dict) or all(job.get(k) == v for k, v in want.items()):
        return False
    job.update(want)
    durable_write(path, plistlib.dumps(job))
    return True


def _reload_script(target: str, domain: str, plist: Path, log: Path) -> str:
    """_reload as a shell script, for a reload from inside the job: bootout, wait for the label to unload,
    bootstrap with retries (already loaded counts), and log a failure."""
    t, pl, lg = shlex.quote(target), shlex.quote(str(plist)), shlex.quote(str(log))
    polls = int(UNLOAD_WAIT_S * 2)
    return (f"launchctl bootout {t} >/dev/null 2>&1; i=0; "
            f"while launchctl print {t} >/dev/null 2>&1; do i=$((i+1)); "
            f"if [ $i -gt {polls} ]; then echo \"$(date '+%Y-%m-%d %H:%M:%S') reload of {t} failed: still loaded "
            f"{UNLOAD_WAIT_S}s after its bootout\" >> {lg}; exit 1; fi; sleep 0.5; done; "
            f"n=0; while [ $n -lt {BOOTSTRAP_TRIES} ]; do n=$((n+1)); "
            f"out=$(launchctl bootstrap {domain} {pl} 2>&1) && exit 0; "
            f"launchctl print {t} >/dev/null 2>&1 && exit 0; [ $n -lt {BOOTSTRAP_TRIES} ] && sleep $n; done; "
            f"echo \"$(date '+%Y-%m-%d %H:%M:%S') reload of {t} failed: launchctl bootstrap: $out\" >> {lg}; exit 1")


def _reload_agent(label: str, plist: Path, inside: bool, log: Path) -> subprocess.CompletedProcess:
    """bootout, wait for the unload, then bootstrap: how launchd takes a changed plist (kickstart keeps the
    loaded one). From inside that agent's job (`inside`) it runs in a new session, which outlives the bootout,
    and logs a failure to `log`."""
    target, domain = _target(label)
    if inside:
        # own session: the reload outlives the bootout of the agent it runs inside
        subprocess.Popen(["sh", "-c", _reload_script(target, domain, plist, log)],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return subprocess.CompletedProcess([], 0, "", "")
    return _reload(label, plist)


def _inside(p: Project, label: str) -> bool:
    """This process runs in launchd job `label` (launchd names it in XPC_SERVICE_NAME) or is the daemon."""
    from .daemon import _read_pid
    return os.environ.get("XPC_SERVICE_NAME") == label or _read_pid(p.state / "daemon.pid") == os.getpid()


def refresh(p: Project) -> bool:
    """Bring an installed service up to date with this runtime (a service installed before the
    watchdog existed gets it) without starting or stopping anything. True when it rewrote the
    daemon's launchd plist to DAEMON_KEYS (an older Background one): restart_service reloads it."""
    rewrote = sys.platform == "darwin" and update_plist(_agent(p), DAEMON_KEYS)
    wd_label, wd_plist = launchd_label(p, watchdog=True), _agent(p, watchdog=True)
    # An older Background watchdog gets WATCHDOG_KEYS and a reload, never from inside its own job (the
    # bootout would end the watchdog mid-restart): the next upgrade or restart from elsewhere does it.
    if (sys.platform == "darwin" and os.environ.get("XPC_SERVICE_NAME") != wd_label
            and update_plist(wd_plist, WATCHDOG_KEYS)):
        _reload(wd_label, wd_plist)
    unit = _unit(p)
    if unit.exists():
        changed = unit.read_text(errors="replace") != _unit_text(p)
        if changed:
            durable_write(unit, _unit_text(p))
        if _write_overseer_unit(p) | _write_watchdog_units(p) or changed:
            _run("systemctl", "--user", "daemon-reload")
            _run("systemctl", "--user", "enable", "--now", _watchdog_unit(p, "timer").name)
    if sys.platform == "darwin" and _agent(p).exists() and not _agent(p, watchdog=True).exists():
        _install_launchd_watchdog(p)
    if shutil.which("crontab"):
        cur = _run("crontab", "-l").stdout
        tag = f"{CRON_TAG}{p.base}"
        if tag in cur and "ttp.watchdog" not in "".join(ln for ln in cur.splitlines() if tag in ln):
            keep = [ln for ln in cur.splitlines() if tag not in ln]
            keep += _cron_lines(p)
            subprocess.run(hoststate.argv(["crontab", "-"]), input="\n".join(keep) + "\n", text=True, capture_output=True)
    _WATCHDOG_SEEN.pop(str(p.base), None)
    return rewrote


def restart_service(p: Project) -> str:
    """Restart the daemon process. Running workers are not touched: the new daemon adopts them (they
    run in sessions of their own, and the agent's AbandonProcessGroup keeps them through a bootout)."""
    if why := foreign_refusal(p, "restart"):
        return why
    rewrote = refresh(p)
    label = launchd_label(p)
    if sys.platform == "darwin" and _agent(p).exists():
        if rewrote:
            r = _reload_agent(label, _agent(p), _inside(p, label), p.logs / "daemon.out")
        else:
            r = _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
        if r.returncode == 0:
            return "restarted"
        # The agent is not loaded: stop the old daemon, then load the agent so launchd starts the new one.
    elif (unit := _unit(p)).exists():
        try:
            r = _run("systemctl", "--user", "restart", unit.name)
        except subprocess.TimeoutExpired as e:
            return f"{TIMED_OUT}: systemctl --user restart did not return within {e.timeout:.0f}s"
        except (OSError, subprocess.SubprocessError) as e:
            return f"could not run systemctl --user restart: {e}"
        return "restarted" if r.returncode == 0 else _manager_said("systemctl --user restart", r)
    from .daemon import HEARTBEAT_STALE_S, _alive, _read_pid
    pid = _read_pid(p.state / "daemon.pid")
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        # The old daemon holds the lock until its current tick step ends (a git fetch, a watcher);
        # a new one started before that finds the lock held and exits at once.
        deadline = time.time() + HEARTBEAT_STALE_S
        while time.time() < deadline and _alive(pid):
            time.sleep(0.5)
    p.logs.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        if _agent(p).exists():
            # launchd starts it at its plist's ProcessType. Never by hand: a hand start runs outside
            # launchd at its starter's priority (a niced or Background caller's), so a failure is reported.
            r = bootstrap(label, _agent(p))
            if r.returncode == 0:
                r = _run("launchctl", "kickstart", _target(label)[0])
            if r.returncode == 0:
                return "restarted"
            why = _manager_said("launchctl bootstrap and kickstart", r).replace("\n", " ")[:300]
            line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} restart failed, the daemon was not started by hand "
                    f"(it would run outside launchd at its starter's CPU priority): {why}\n")
            with open_append_or_null(p.logs / "daemon.out", text=line) as f:
                f.write(line)
            return why
        line = (f"{datetime.now():%Y-%m-%d %H:%M:%S} restart by hand (no launchd agent is installed): the "
                f"daemon runs at its starter's CPU priority\n")
        with open_append_or_null(p.logs / "daemon.out", text=line) as f:
            f.write(line)
    _spawn(p)
    return "restarted"


def _git(h: Path, *args: str) -> subprocess.CompletedProcess:
    return gitmemo.run(["git", "-C", str(h), "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost",
                           *args], capture_output=True, text=True, timeout=120)


# A caller that cannot reach the service manager (a sandboxed worker: no user bus, no signals to the
# daemon) asks the running daemon to restart instead: it writes RESTART_REQUEST in state/, the daemon
# runs `python -m ttp.service` (with the harness runtime on disk) detached, and that restart, made from
# the host with the service manager in reach, writes RESTART_RESULT.
RESTART_REQUEST, RESTART_RESULT = "restart.request", "restart.result"
# A restart judges the new daemon only once the old one has exited: it waits for the old daemon's pid
# at most the service's own stop timeout (stop_timeout_s, capped at STOP_WAIT_CAP_S) plus
# STOP_MARGIN_S for the kill to land, and only then starts the new daemon's start window.
STOP_WAIT_CAP_S = 300
STOP_MARGIN_S = 15
LAUNCHD_EXIT_TIMEOUT_S = 20   # launchd's default ExitTimeOut: SIGKILL this long after SIGTERM (0: never)
# The new daemon's start window (a restart's wait_s, from the moment the old daemon exited) stretches with
# the host's load: with several runnable tasks per core its imports alone can take minutes (an upgrade on a
# laptop at load 57-78 on 14 cores was rolled back 61 s in, yet the same runtime imported in 5 s from a
# copy). It is wait_s times the highest 1-minute load per core seen while waiting (at least 1), at most
# START_WAIT_CAP_S (a longer wait_s is kept).
START_WAIT_CAP_S = 300
# A restart deferred because the old daemon outlived its stop timeout (restart() could not judge the new
# one) is recorded here; check_deferred judges the new daemon later, from the watchdog or its own start.
RESTART_DEFERRED = "restart.deferred"
# The watchdog runs as a oneshot unit killed WATCHDOG_RUN_S after it starts. A rollback it makes judges the
# restarted daemon only until JUDGE_MARGIN_S before that kill, so its alert is always posted; it starts one
# only with ROLLBACK_MIN_S left (the restart's own 60 s bound and the rollback's commits), else a later run does.
WATCHDOG_RUN_S = 300
JUDGE_MARGIN_S = 30
ROLLBACK_MIN_S = 90
# A helper that dies without a result (its runtime fails to import, a kill, a reboot) is reaped by the
# daemon: its own handle says it ended, or its claim outlived HELPER_BOUND_S, longer than a restart's own
# waits (the old daemon's stop, a new daemon's first tick, twice with a rollback, plus the service
# manager's time).
HELPER_BOUND_S = 2 * (STOP_WAIT_CAP_S + STOP_MARGIN_S + START_WAIT_CAP_S + 300) + 300
HELPER_STARTED = "restart helper running"     # main() logs it first: the helper got past its imports
_helpers: dict[str, subprocess.Popen] = {}    # the daemon's handle on the helper it started, per project
# The service manager could not be reached from here, as opposed to it trying and the daemon failing.
_UNREACHABLE_RE = re.compile(r"failed to connect to bus|failed to get d-bus connection|DBUS_SESSION_BUS_ADDRESS|"
                             r"XDG_RUNTIME_DIR|operation not permitted|permission denied|not authori[sz]ed|"
                             r"access denied|could not run", re.I)


class Restarted(str):
    """restart()'s report. `outcome`: running, busy (alive, still in its first tick), deferred (the
    restart has not happened yet; the old daemon runs on), rolled_back, failed, refused (from another
    project's run) or stopped (restart_running left a stopped daemon stopped)."""
    outcome = "running"


# The service manager took the restart but did not report back in time (the old daemon was still ending
# its tick step): the restart is pending, not failed. restart() waits its usual window for the new daemon.
TIMED_OUT = "restart timed out"


def _said(text: str, outcome: str) -> Restarted:
    r = Restarted(text)
    r.outcome = outcome
    return r


def _manager_said(cmd: str, r: subprocess.CompletedProcess) -> str:
    """The service manager's whole failure output, kept for the report (it is what says why)."""
    out = "\n".join(x.strip() for x in (r.stderr or "", r.stdout or "") if x and x.strip())
    return f"{cmd} failed (exit {r.returncode}): {out[-2000:] or 'no output'}"


def unreachable(msg: str) -> bool:
    """msg, a failed restart_service's report, says the service manager could not be reached from here."""
    return bool(_UNREACHABLE_RE.search(msg or ""))


def old_daemon_beats(p: Project, since: float, after: float | None = None) -> dict | None:
    """The heartbeat of a daemon started before `since` that still ticks: it showed progress within
    HEARTBEAT_STALE_S, and after `after` when given. Read from the file, so it works where the
    daemon's pid cannot be seen (a sandbox with its own pid namespace)."""
    from .daemon import HEARTBEAT_STALE_S, heartbeat
    hb = heartbeat(p)
    if not hb or float(hb.get("started") or 0) >= since or hb["age"] >= HEARTBEAT_STALE_S:
        return None
    if after is not None and time.time() - hb["age"] <= after:
        return None
    return hb


def request_restart(p: Project, why: str, wait_s: float = 60, result_wait_s: float | None = None) -> Restarted:
    """Ask the running daemon to restart itself from the host, and wait for the outcome: a new daemon
    ticking, or the result the daemon's restart wrote. The request stays when nothing took it yet:
    the daemon carries it out when its current tick ends."""
    from .daemon import heartbeat
    at = time.time()
    req = p.state / RESTART_REQUEST
    durable_write(req, json.dumps({"at": at, "pid": os.getpid(), "why": why[-2000:]}))
    hb = heartbeat(p) or {}
    if "restart_requests" not in (hb.get("takes") or []):
        return _said(f"{why}. The running daemon (pid {hb.get('pid', '?')}) is older than restart requests, so "
                     f"nothing here can restart it: it keeps running the previous runtime, and the change "
                     f"takes effect at its next restart from the host (a `ttp restart {p.name}` or `ttp upgrade` "
                     f"run there, a crash or a reboot). The request is left in {req}; nothing was rolled back",
                     "failed")
    result_wait_s = 2 * wait_s if result_wait_s is None else result_wait_s
    taken_by = at + wait_s
    while True:
        res = _restart_result(p, at)
        if res:
            return _said(f"{why}. The daemon restarted itself on request: {res.get('text', '')}",
                         res.get("outcome") or "failed")
        now = time.time()
        if req.exists() and now >= taken_by:
            return _said(f"{why}. Restart requested from the running daemon (pid {hb.get('pid', '?')}): it has "
                         f"not taken the request after {now - at:.0f}s (a long tick step) and restarts when that "
                         f"step ends. Nothing was rolled back. Check `ttp status {p.name}`", "deferred")
        if not req.exists() and now >= taken_by + result_wait_s:
            return _said(f"{why}. The daemon took the restart request but has reported no outcome after "
                         f"{now - at:.0f}s; it is still restarting. Check `ttp status {p.name}`", "deferred")
        time.sleep(poll_s(1))


def _restart_result(p: Project, at: float) -> dict | None:
    try:
        res = json.loads((p.state / RESTART_RESULT).read_text())
    except (OSError, ValueError):
        return None
    return res if isinstance(res, dict) and abs(float(res.get("at") or 0) - at) < 1e-3 else None


def take_restart_request(p: Project, started: float) -> bool:
    """The daemon's side, once per tick: carry out a restart request made since this daemon started,
    from a detached `python -m ttp.service` run with the harness runtime on disk. An older request is
    done (this daemon is the restart) and is dropped."""
    req = p.state / RESTART_REQUEST
    try:
        info = json.loads(req.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        info = {}
    if float(info.get("at") or 0) < started:
        req.unlink(missing_ok=True)
        return False
    taken = p.state / (RESTART_REQUEST + ".taken")
    os.replace(req, taken)
    durable_write(taken, json.dumps({**info, "taken": time.time()}))   # before the helper exists to remove it
    p.logs.mkdir(parents=True, exist_ok=True)
    line = (f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} restart requested by pid {info.get('pid', '?')}: "
            f"{str(info.get('why') or '')[-300:]}\n")
    with open_append_or_null(p.logs / "restart.log", text=line) as log:
        log.write(line)
        log.flush()
        _helpers[str(p.base)] = subprocess.Popen(
            [sys.executable, "-m", "ttp.service", str(p.base)], cwd=str(p.base), env={**os.environ, **_env(p)},
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return True


def reap_restart_request(p: Project, started: float) -> str | None:
    """The daemon's side, once per tick after a good one: a taken request whose helper ended without a
    result is finished here, so the requester's probe fires. A daemon started after the request was taken
    is the restart: outcome running. Otherwise the helper could not start, was interrupted, or hung past
    HELPER_BOUND_S (its process group is killed first) while the old daemon runs on: outcome failed, with
    the tail of logs/restart.log and one high alert. Returns the outcome written, or None."""
    taken = p.state / (RESTART_REQUEST + ".taken")
    try:
        info = json.loads(taken.read_text())
        took = float(info.get("taken") or taken.stat().st_mtime)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        info, took = {}, 0.0
    if not isinstance(info, dict):
        info = {}
    at = info.get("at")
    if at is not None and _restart_result(p, float(at)):
        taken.unlink(missing_ok=True)      # the helper reported, then ended before removing its claim
        _helpers.pop(str(p.base), None)
        return None
    proc = _helpers.get(str(p.base))
    rc = proc.poll() if proc else None
    age = time.time() - took
    log = _restart_log_tail(p)
    if started > took:
        outcome = "running"
        text = (f"the daemon (pid {os.getpid()}) started after the restart request was taken and completes "
                f"its ticks with the runtime on disk; the restart helper left no result")
    elif age < HELPER_BOUND_S and (proc is None or rc is None):
        return None                        # still restarting
    else:
        outcome = "failed"
        hung = proc is not None and rc is None
        if hung:                           # its own session (start_new_session): stop it before reporting
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        how = ("hung" if hung else "could not start (it never got past its imports)" if HELPER_STARTED not in log
               else "was interrupted before it reported")
        ended = (f"was still running after {age:.0f}s and was stopped" if hung else
                 f"exited with code {rc}" if rc is not None else f"left no result after {age:.0f}s")
        text = (f"The restart helper {how}: it {ended}. The old daemon (pid {os.getpid()}) still runs the "
                f"previous runtime; nothing was restarted or rolled back. logs/restart.log: {log or 'empty'}")
        from .deploy import redeploy_key
        p.db.post("out", f"The requested daemon restart failed. {text}", chat=None, kind="alert", severity="high",
                  ref=redeploy_key(p.name))
    durable_write(p.state / RESTART_RESULT, json.dumps({"at": at, "finished": time.time(), "outcome": outcome,
                                                        "text": text, "reaped": True}))
    taken.unlink(missing_ok=True)
    _helpers.pop(str(p.base), None)
    return outcome


def _restart_log_tail(p: Project, n: int = 1500) -> str:
    """logs/restart.log since the last request's header, at most the last n characters."""
    try:
        text = (p.logs / "restart.log").read_text(errors="replace")
    except OSError:
        return ""
    i = text.rfind("--- ")
    return text[i if i >= 0 else 0:].strip()[-n:]


def main() -> int:
    """The restart a request asked for, run by the daemon detached (see take_restart_request)."""
    p = Project(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    print(f"{HELPER_STARTED} (pid {os.getpid()})", flush=True)
    taken = p.state / (RESTART_REQUEST + ".taken")
    try:
        info = json.loads(taken.read_text())
    except (OSError, ValueError):
        info = {}
    try:
        res = restart(p, requested=True)
        out = {"outcome": res.outcome, "text": str(res)}
    except Exception as e:  # the requester waits for a result: always leave one
        out = {"outcome": "failed", "text": f"the restart raised {type(e).__name__}: {e}"}
    durable_write(p.state / RESTART_RESULT, json.dumps({"at": info.get("at"), "finished": time.time(), **out}))
    taken.unlink(missing_ok=True)
    print(out["text"])
    return 0 if out["outcome"] in ("running", "busy") else 1


def load_per_core() -> float:
    """The host's 1-minute load average per core (0 where it cannot be read)."""
    try:
        ld = os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0
    return ld / (os.cpu_count() or 1) if ld == ld and ld != float("inf") else 0.0


def start_window_s(wait_s: float, load: float | None = None) -> float:
    """How long a new daemon may take to start under `load` (per core; default: now): see START_WAIT_CAP_S."""
    load = load_per_core() if load is None else load
    return min(max(wait_s, START_WAIT_CAP_S), wait_s * max(1.0, load))


def wait_for_start(p: Project, since: float, wait_s: float, tick_wait_s: float | None = None,
                   waits_from: float | None = None, until: float | None = None) -> str:
    """How a daemon started after `since` came up: "running" once it completed a tick; "busy" if it
    is alive and still in its first tick after tick_wait_s (a first tick may fetch and add worktrees),
    or if no new daemon started because the old one is still alive and holds the lock; otherwise
    "broken": it did not start within its start window (wait_s stretched by the host's load, start_window_s),
    it exited, or its first tick failed twice. Both waits count from `waits_from` (default `since`).
    "unconfirmed" when `until` passes before any of these."""
    from .daemon import HEARTBEAT_STALE_S, _alive, _is_daemon, _read_pid, heartbeat, start_marker
    tick_wait_s = HEARTBEAT_STALE_S if tick_wait_s is None else tick_wait_s
    waits_from = since if waits_from is None else waits_from
    window = wait_s
    while True:
        window = max(window, start_window_s(wait_s))     # the highest load seen while it starts
        hb = heartbeat(p)
        if hb and float(hb.get("started") or 0) >= since:
            return "running"
        now = time.time()
        st = start_marker(p)
        if st and float(st.get("started") or 0) >= since:
            pid = int(st.get("pid") or 0)
            if pid <= 0 or not _alive(pid) or int(st.get("tick_errors") or 0) >= 2:
                return "broken"
            if now >= waits_from + max(wait_s, tick_wait_s):
                return "busy"
        elif now >= waits_from + window:
            old = _read_pid(p.state / "daemon.pid")
            return "busy" if old and _is_daemon(old) or old_daemon_beats(p, since, after=since) else "broken"
        if until is not None and now >= until:
            return "unconfirmed"
        time.sleep(poll_s(1))


def _off_files(p: Project) -> str | None:
    """The version the daemon's heartbeat reports when it differs from the runtime on disk, else None
    (None too when either is unknown: a daemon started after the restart runs the files on disk)."""
    from .daemon import heartbeat
    ran = ((heartbeat(p) or {}).get("receipt") or {}).get("version")
    try:
        m = re.search(r'__version__ = "([^"]+)"', (p.harness / "runtime" / "ttp" / "__init__.py").read_text())
    except OSError:
        m = None
    return ran if ran and m and ran != m.group(1) else None


_SYSTEMD_SPAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|s|min|m|h|d|w)?(?![a-z])")
_SYSTEMD_UNIT_S = {"us": 1e-6, "ms": 1e-3, "s": 1, None: 1, "min": 60, "m": 60, "h": 3600, "d": 86400,
                   "w": 604800}


def _systemd_span_s(text: str) -> float | None:
    """A systemd time span as `systemctl show` prints it ("2min", "1min 30s", "infinity") in seconds;
    None when it cannot be read, inf for infinity."""
    text = text.strip().lower()
    if text in ("infinity", "inf"):
        return float("inf")
    parts = _SYSTEMD_SPAN_RE.findall(text)
    if not parts or _SYSTEMD_SPAN_RE.sub("", text).strip():
        return None
    return sum(float(n) * _SYSTEMD_UNIT_S[u or None] for n, u in parts)


def _manager_view(p: Project) -> tuple[float, int | None]:
    """How long the service manager lets the daemon stop before it kills it (s, at most STOP_WAIT_CAP_S),
    and the daemon's pid as the manager knows it (systemd's MainPID), read before a restart."""
    if sys.platform == "darwin" and _agent(p).exists():
        try:
            job = plistlib.loads(_agent(p).read_bytes())
            stop = float(job.get("ExitTimeOut", LAUNCHD_EXIT_TIMEOUT_S))
            stop = stop if stop > 0 else STOP_WAIT_CAP_S   # launchd: 0 means it never kills the daemon
        except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            stop = LAUNCHD_EXIT_TIMEOUT_S
        return min(stop, STOP_WAIT_CAP_S), None
    if _unit(p).exists():
        stop, main = None, None
        try:
            r = _run("systemctl", "--user", "show", "-p", "TimeoutStopUSec", "-p", "MainPID", _unit(p).name)
            for line in (r.stdout or "").splitlines() if r.returncode == 0 else ():
                key, _, val = line.partition("=")
                if key == "TimeoutStopUSec":
                    stop = _systemd_span_s(val)
                elif key == "MainPID" and val.strip().isdigit():
                    main = int(val.strip()) or None
        except (OSError, subprocess.SubprocessError):
            pass
        return min(STOP_TIMEOUT_S if stop is None else stop, STOP_WAIT_CAP_S), main
    from .daemon import HEARTBEAT_STALE_S   # restart_service's own wait before it starts a new daemon
    return float(HEARTBEAT_STALE_S), None


def _running_daemons(p: Project, main: int | None) -> set[int]:
    """The daemon processes a restart is to replace: the pid file's and the manager's main pid, if live."""
    from .daemon import _is_daemon, _read_pid
    pids = {_read_pid(p.state / "daemon.pid"), main}
    return {pid for pid in pids if pid and pid != os.getpid() and _is_daemon(pid)}


def _await_exit(pids: set[int], until: float) -> set[int]:
    """Wait until each of pids has exited or `until` passes; returns those still running."""
    from .daemon import _is_daemon
    while True:
        left = {pid for pid in pids if _is_daemon(pid)}
        if not left or time.time() >= until:
            return left
        time.sleep(poll_s(1))


def _judge_start(p: Project, t0: float, old: set[int], stop_s: float, wait_s: float,
                 tick_wait_s: float | None, until: float | None = None) -> tuple[str | None, float, set[int]]:
    """After a restart made at t0, judge the new daemon only once the old daemons (old) have exited: wait
    for their pids at most the service's stop timeout plus STOP_MARGIN_S from t0, then give the new
    daemon wait_s from that moment (wait_for_start). Returns (its state, or None while an old daemon
    still runs; seconds the old daemons took to stop; the pids still running). Neither wait outlasts
    `until` when given (wait_for_start then says "unconfirmed")."""
    end = t0 + stop_s + STOP_MARGIN_S
    left = _await_exit(old, end if until is None else min(end, until)) if old else set()
    stopped = time.time() - t0
    if left:
        return None, stopped, left
    if until is None:
        return wait_for_start(p, t0, wait_s, tick_wait_s, waits_from=time.time()), stopped, left
    return wait_for_start(p, t0, wait_s, tick_wait_s, waits_from=time.time(), until=until), stopped, left


STOPPED_ON_PURPOSE = ("stopped on purpose: left out of deploys, rollout counts and health alerts until it is "
                      "started again (`ttp start`)")


def stopped_on_purpose(p: Project) -> bool:
    """Its daemon was stopped on purpose: no service keeps it running (`ttp stop`, or services its user
    removed) and no daemon runs. Deploys leave it out until it is started again. Read only."""
    try:
        return not installed(p) and daemon_stopped(p)
    except (OSError, ValueError):
        return False


def stopped_at(name: str, entry: dict, row: dict) -> bool:
    """stopped_on_purpose for registered project `name` from its deploy row (overview.deploy_status: its
    daemon is not alive and not mid-upgrade) and its registry entry. Read only: the project is not opened."""
    if not isinstance(entry, dict) or not entry.get("dir") or row.get("daemon_alive") or row.get("upgrading"):
        return False
    from types import SimpleNamespace
    try:
        return installed(SimpleNamespace(name=name, base=Path(str(entry["dir"])) / "tt-project")) is None
    except (OSError, ValueError):
        return False


def daemon_stopped(p: Project) -> bool:
    """No daemon of p runs: no live daemon process and no heartbeat that still ticks."""
    _, main = _manager_view(p)
    return not _running_daemons(p, main) and not old_daemon_beats(p, time.time())


def restart_running(p: Project) -> Restarted:
    """An upgrade's restart (`ttp upgrade`, and so a push's deploy): new files reach a running daemon only.
    A stopped daemon stays stopped (outcome `stopped`): it runs the files on disk at its next start, which
    is its owner's call. From a run of another project (NO_FOREIGN_START) a running daemon is asked to
    restart itself (request_restart), never restarted from the run."""
    if daemon_stopped(p):
        return _said(f"{p.name}'s daemon is stopped, so it was not started: it runs the new files at its next start",
                     "stopped")
    if other := foreign_run(p):
        return request_restart(p, f"new files for {p.name} from a run of another project ({other}); its daemon "
                                  f"restarts itself on request ({NO_FOREIGN_START})")
    return restart(p)


def restart(p: Project, wait_s: float = 60, restart_fn=None, tick_wait_s: float | None = None,
            requested: bool = False) -> Restarted:
    """Restart the daemon and confirm it ticks. If it is broken (never started, exited, or its first
    tick keeps failing) and the harness runtime changed since the commit a daemon last ran well on,
    put runtime/ back to that commit as a new commit (history, charter, memory and prompts untouched),
    restart again and alert. A daemon that is alive but still in a slow first tick is left alone.
    When the service manager refuses while the old daemon still ticks, nothing restarted: no rollback.
    If it could not be reached from here (a sandbox), the running daemon is asked to restart itself
    (unless this is that restart, `requested`). A restart the service manager did not confirm in time
    (TIMED_OUT) is pending: it counts once a new daemon runs the runtime on disk within the window.
    The new daemon is judged only after the old one has exited (_judge_start), so an old daemon slow to
    stop (up to its service's stop timeout) never makes a new runtime look broken.
    Refused (outcome `refused`, nothing touched) from a run of another project (NO_FOREIGN_START).
    The result's `outcome` says how it went."""
    from .daemon import start_marker
    if why := foreign_refusal(p, "restart"):
        return _said(why, "refused")
    restart_fn = restart_fn or restart_service
    stop_s, main = _manager_view(p)
    old = _running_daemons(p, main)
    (p.state / RESTART_DEFERRED).unlink(missing_ok=True)   # this restart judges the daemon itself
    t0 = time.time()
    msg = restart_fn(p)
    timed_out = msg.startswith(TIMED_OUT)
    if msg != "restarted" and not timed_out and old_daemon_beats(p, t0):
        if unreachable(msg) and not requested:
            return request_restart(p, f"The service manager could not be reached from here ({msg})", wait_s)
        return _said(f"The daemon was not restarted: {msg}. The old daemon still runs the previous runtime; "
                     f"nothing was rolled back", "failed")
    if msg != "restarted" and unreachable(msg):
        return _said(f"Restart unavailable from here: {msg}. No daemon heartbeat is fresh either, so it may be "
                     f"down; its service restarts it from the host. Nothing was rolled back: the new runtime "
                     f"was never started, so nothing shows it is at fault", "failed")
    state, stopped, left = _judge_start(p, t0, old, stop_s, wait_s, tick_wait_s)
    judged = time.time() - t0 - stopped        # how long the new daemon had once the old one was gone
    if state is None:
        _defer(p, t0, msg, left, stop_s, wait_s, tick_wait_s)
        return _said(f"{msg}; but the old daemon has not exited after {stopped:.0f}s (pid "
                     f"{', '.join(map(str, sorted(left)))}), past its service's stop timeout ({stop_s:.0f}s), so the new one has not "
                     f"started yet. Nothing was rolled back: the new runtime never ran. Check `ttp status {p.name}` "
                     f"shortly", "deferred")
    if state == "running":
        off = _off_files(p) if timed_out else None
        if off:
            return _said(f"{msg}; a new daemon came up but runs {off}, not the runtime on disk. "
                         f"Check `ttp status {p.name}`", "failed")
        return _said(f"{msg}; the daemon is running" + (" (the restart was pending: it came up on the new runtime "
                                                        "within the re-check window)" if timed_out else ""), "running")
    if state == "busy":
        st = start_marker(p)
        if not (st and float(st.get("started") or 0) >= t0):
            _defer(p, t0, msg, old | _running_daemons(p, main), stop_s, wait_s, tick_wait_s)
            return _said(f"{msg}; but the old daemon has not exited after {time.time() - t0:.0f}s, so the new one "
                         f"has not started yet. Check `ttp status {p.name}` shortly", "deferred")
        return _said(f"{msg}; the daemon is up but still in its first tick after {time.time() - t0:.0f}s. "
                     f"Check `ttp status {p.name}` shortly", "busy")
    return _roll_back(p, msg, judged, stopped, stop_s, wait_s, tick_wait_s, restart_fn)


def _roll_back(p: Project, msg: str, judged: float, stopped: float, stop_s: float, wait_s: float,
               tick_wait_s: float | None, restart_fn, note: str = "", until: float | None = None) -> Restarted:
    """The new daemon is broken: roll runtime/ back to the last commit a daemon ran well on, restart and alert
    (restart(), or check_deferred for a restart judged later). `note` opens the alert's failed phase. The
    restarted daemon is judged at most until `until` (the caller's own time limit), so the alert is posted."""
    h = p.harness
    good = (p.db.kv("harness_good") or {}).get("commit")
    changed = _git(h, "log", "--format=%h %s", f"{good}..HEAD", "--", "runtime").stdout.strip() if good else ""
    dirty = _git(h, "status", "--porcelain", "--", "runtime").stdout.strip() if good else ""
    if not changed and not dirty:
        return _said(f"{msg}; but the daemon did not start or its first tick failed. Check `ttp logs {p.name}`"
                     + ("" if good else " (no known-good harness commit to fall back to)"), "failed")
    if dirty:
        _git(h, "add", "-A", "--", "runtime")
        _git(h, "commit", "-q", "-m", "runtime edits in place when the daemon failed to start")
        changed = _git(h, "log", "--format=%h %s", f"{good}..HEAD", "--", "runtime").stdout.strip()
    with tree_writes(h):
        if _git(h, "restore", f"--source={good}", "--staged", "--worktree", "--", "runtime").returncode != 0:
            _git(h, "checkout", good, "--", "runtime")    # git older than 2.23
    _git(h, "commit", "-q", "-m", f"roll back runtime to {good[:10]}: the daemon did not start with it")
    load = load_per_core()
    phase = (note + f"Failed phase: the new daemon was not ready {judged:.0f}s after the old one "
             f"stopped (the old daemon took {stopped:.0f}s to stop; its service's stop timeout is {stop_s:.0f}s; "
             f"the host's load is {load:.1f} per core, so its start window was {start_window_s(wait_s, load):.0f}s). ")
    stop_s, main = _manager_view(p)
    failed = _running_daemons(p, main)
    t1 = time.time()
    restart_fn(p)
    again, _, _ = _judge_start(p, t1, failed, stop_s, wait_s, tick_wait_s, until)
    back = again not in (None, "broken", "unconfirmed")
    cut = until is not None and again in (None, "unconfirmed") and time.time() >= until
    commits = "; ".join(changed.splitlines()[:10])
    text = (f"The daemon did not start after a runtime change, so the harness runtime was rolled back to "
            f"{good[:10]}, the last version that ran (a new commit; nothing was deleted). Rolled back: {commits}. "
            + phase + ("" if msg == "restarted" else f"The service manager said: {msg}. ")
            + ("The daemon is running again." if back else
               f"Its restart was not confirmed before this check's time ran out: check `ttp status {p.name}`."
               if cut else f"It still does not start: check `ttp logs {p.name}`."))
    from .deploy import redeploy_key
    p.db.post("out", text, chat=None, kind="alert", severity="high", ref=redeploy_key(p.name))
    return _said(text, "rolled_back")


def _defer(p: Project, t0: float, msg: str, old: set[int], stop_s: float, wait_s: float,
           tick_wait_s: float | None) -> None:
    """Record a restart made at t0 that restart() could not judge (the old daemons, old, still ran), with
    the harness runtime tree it was to start, for check_deferred."""
    durable_write(p.state / RESTART_DEFERRED, json.dumps({
        "at": t0, "msg": msg, "old": sorted(old), "stop_s": stop_s, "wait_s": wait_s,
        "tick_wait_s": tick_wait_s, "runtime": _runtime_tree(p)}))   # unknown (None): never compared


def _runtime_tree(p: Project) -> str | None:
    """The git tree id of the harness's committed runtime/ (a memory or charter commit keeps it), or None."""
    try:
        r = _git(p.harness, "rev-parse", "HEAD:runtime")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def check_deferred(p: Project, who: str = "watchdog", restart_fn=None, budget_s: float | None = None) -> str | None:
    """Judge a deferred restart's new daemon (RESTART_DEFERRED) without waiting, as restart() would have:
    "pending" while an old daemon still runs or the new one is inside its start window (counted from the
    first look that found the old daemons gone); "running" or "busy" (a slow first tick, left alone) end
    it; a broken new daemon is rolled back as restart() does (_roll_back) and returns "rolled_back". The
    record ends with no verdict ("superseded") once the harness runtime differs from the one it was to start
    (a later deploy, or a rollback the updater made; commits outside runtime/ keep it), and a later
    restart() removes it: never two rollbacks. Only the caller that claims the record rolls back.
    `budget_s`: the seconds the caller may still run (the watchdog's unit kills it): the rollback's judge
    stops JUDGE_MARGIN_S short of it, and with under ROLLBACK_MIN_S left it stays "pending" for a later
    run. None when there is nothing to judge."""
    from .daemon import HEARTBEAT_STALE_S, _alive, _is_daemon, heartbeat, start_marker
    path = p.state / RESTART_DEFERRED
    try:
        rec = json.loads(path.read_text())
        t0 = float(rec["at"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError):
        path.unlink(missing_ok=True)
        return None
    hb = heartbeat(p)
    if hb and float(hb.get("started") or 0) >= t0:
        path.unlink(missing_ok=True)
        return "running"
    tree = _runtime_tree(p)
    if tree is None:
        return "pending"                       # unknown: never a reason to drop or roll back
    if rec.get("runtime") and tree != rec["runtime"]:
        path.unlink(missing_ok=True)
        return "superseded"
    if any(_is_daemon(int(pid)) for pid in rec.get("old") or []) or old_daemon_beats(p, t0, after=t0):
        return "pending"                       # the restart has not happened yet
    now = time.time()
    gone = float(rec.get("gone") or 0)
    if not gone:
        gone = now
        durable_write(path, json.dumps({**rec, "gone": gone}))
    wait_s = float(rec.get("wait_s") or 60)
    tick_wait_s = HEARTBEAT_STALE_S if rec.get("tick_wait_s") is None else float(rec["tick_wait_s"])
    st = start_marker(p)
    if st and float(st.get("started") or 0) >= t0:
        pid = int(st.get("pid") or 0)
        if pid > 0 and _alive(pid) and int(st.get("tick_errors") or 0) < 2:
            if now < gone + max(wait_s, tick_wait_s):
                return "pending"
            path.unlink(missing_ok=True)
            return "busy"
    elif now < gone + start_window_s(wait_s):
        return "pending"
    until = None if budget_s is None else now + budget_s - JUDGE_MARGIN_S
    if until is not None and until - now < ROLLBACK_MIN_S:
        return "pending"                       # too little time left to roll back and alert: a later run
    claim = path.with_name(f"{RESTART_DEFERRED}.{os.getpid()}")
    try:
        os.replace(path, claim)                # one judge rolls back: the daemon's start or the watchdog
    except FileNotFoundError:
        return None
    try:
        if float(json.loads(claim.read_text()).get("at") or 0) != t0:   # a newer restart's record: not ours
            os.replace(claim, path)
            return "pending"
        msg = str(rec.get("msg") or "restarted")
        note = (f"The restart was deferred (the old daemon outlived its service's stop timeout), so the {who} "
                f"checked the new daemon later. ")
        res = _roll_back(p, msg, now - gone, gone - t0, float(rec.get("stop_s") or 0), wait_s,
                         rec.get("tick_wait_s"), restart_fn or restart_service, note, until)
        if res.outcome == "failed":            # nothing to roll back to: no caller reports it, so alert
            p.db.post("out", f"{note}{res}", chat=None, kind="alert", severity="high")
        return res.outcome
    finally:
        claim.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

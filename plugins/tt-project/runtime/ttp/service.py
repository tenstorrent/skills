# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep the daemon alive across logouts, crashes and reboots, without root.

Linux: a systemd user unit (restart on failure) plus `loginctl enable-linger` so it runs without
a login session and starts at boot. Where linger is refused, a crontab watchdog (@reboot plus
every 5 minutes) restarts the daemon instead. macOS: a launchd agent with KeepAlive and the
project folder as working directory (never `/` or the home folder).

A daemon that is alive but stuck (no tick progress for WATCHDOG_S) is restarted too: systemd by
WatchdogSec (the daemon pings it after each tick and between the steps of a long one), launchd and cron by `ttp.watchdog`, run every
5 minutes, which ends it so the service starts a new one.
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
from pathlib import Path

from . import poll_s
from .project import Project, durable_write


def unit_name(p: Project) -> str:
    return "tt-project-" + re.sub(r"[^A-Za-z0-9_.-]", "-", p.name)


WATCHDOG_EVERY_S = 300   # how often launchd and cron look for a stuck daemon


def daemon_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.daemon", str(p.base)]


def watchdog_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.watchdog", str(p.base)]


def launchd_label(p: Project, watchdog: bool = False) -> str:
    return f"com.tt-project.{unit_name(p)}" + (".watchdog" if watchdog else "")


def _agent(p: Project, watchdog: bool = False) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launchd_label(p, watchdog)}.plist"


def _unit(p: Project) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{unit_name(p)}.service"


def _env(p: Project) -> dict:
    from .providers.base import service_path
    return {"PYTHONPATH": str(p.harness / "runtime"), "PATH": service_path(), "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "C.UTF-8")}


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=60)


def install(p: Project) -> str:
    if sys.platform == "darwin":
        return _install_launchd(p)
    if shutil.which("systemctl") and _run("systemctl", "--user", "is-system-running").returncode in (0, 1):
        msg = _install_systemd(p)
        linger = _run("loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger").stdout.strip()
        if linger != "Linger=yes":
            r = _run("loginctl", "enable-linger")
            if r.returncode != 0:
                return msg + "; linger refused, adding crontab watchdog: " + _install_cron(p)
            msg += "; linger enabled"
        return msg
    return _install_cron(p)


def _unit_text(p: Project) -> str:
    from .daemon import WATCHDOG_S
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
WatchdogSec={WATCHDOG_S}
NotifyAccess=main

[Install]
WantedBy=default.target
"""


def _install_systemd(p: Project) -> str:
    _unit(p).parent.mkdir(parents=True, exist_ok=True)
    durable_write(_unit(p), _unit_text(p))
    _run("systemctl", "--user", "daemon-reload")
    r = _run("systemctl", "--user", "enable", "--now", f"{unit_name(p)}.service")
    if r.returncode != 0:
        return "systemd failed (" + r.stderr.strip()[:200] + "); " + _install_cron(p)
    return f"systemd user unit {unit_name(p)}"


def _install_launchd(p: Project) -> str:
    label = launchd_label(p)
    plist = _agent(p)
    plist.parent.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    job = {"Label": label, "ProgramArguments": daemon_argv(p), "WorkingDirectory": str(p.base),
           "EnvironmentVariables": _env(p), "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
           "ThrottleInterval": 10, "AbandonProcessGroup": True, "StandardOutPath": str(p.logs / "launchd.log"),
           "StandardErrorPath": str(p.logs / "launchd.log")}
    durable_write(plist, plistlib.dumps(job))
    uid = os.getuid()
    _run("launchctl", "bootout", f"gui/{uid}/{label}")
    r = _run("launchctl", "bootstrap", f"gui/{uid}", str(plist))
    if r.returncode != 0:
        return f"launchd bootstrap failed: {r.stderr.strip()[:200]}"
    return f"launchd agent {label}" + _install_launchd_watchdog(p)


def _install_launchd_watchdog(p: Project) -> str:
    """A second agent that runs `ttp.watchdog` every few minutes: KeepAlive restarts a daemon that
    exits, not one that is alive but stuck."""
    label, plist = launchd_label(p, watchdog=True), _agent(p, watchdog=True)
    job = {"Label": label, "ProgramArguments": watchdog_argv(p), "WorkingDirectory": str(p.base),
           "EnvironmentVariables": _env(p), "StartInterval": WATCHDOG_EVERY_S, "ProcessType": "Background",
           "StandardOutPath": str(p.logs / "launchd.log"), "StandardErrorPath": str(p.logs / "launchd.log")}
    plist.parent.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    durable_write(plist, plistlib.dumps(job))
    uid = os.getuid()
    _run("launchctl", "bootout", f"gui/{uid}/{label}")
    r = _run("launchctl", "bootstrap", f"gui/{uid}", str(plist))
    return " with a watchdog" if r.returncode == 0 else f" (watchdog bootstrap failed: {r.stderr.strip()[:200]})"


CRON_TAG = "# tt-project:"


def _install_cron(p: Project) -> str:
    cur = _run("crontab", "-l").stdout if shutil.which("crontab") else ""
    tag = f"{CRON_TAG}{p.base}"
    keep = [ln for ln in cur.splitlines() if tag not in ln]
    keep += _cron_lines(p)
    r = subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, capture_output=True)
    if r.returncode != 0:
        return "crontab install failed: " + r.stderr.strip()[:200]
    # The daemon refuses to start twice (lock file), so the 5-minute entry is a no-op while it runs.
    _spawn(p)
    return "crontab watchdog"


def _cron_lines(p: Project) -> list[str]:
    """At boot, start the daemon; every 5 minutes, end a stuck one, then start one if none runs."""
    tag, cmd = f"{CRON_TAG}{p.base}", _cron_cmd(p)
    return [f"@reboot {cmd} {tag}", f"*/5 * * * * {_cron_cmd(p, watchdog_argv(p))}; {cmd} {tag}"]


def _spawn(p: Project) -> None:
    subprocess.Popen(["sh", "-c", _cron_cmd(p)], start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


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
            subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True)
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
        found = {"kind": "systemd", "watchdog": "WatchdogSec=" in unit.read_text(errors="replace")}
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
    from .daemon import WATCHDOG_S
    svc = installed(p)
    if svc and svc["watchdog"]:
        return (f"its {svc['kind']} service restarts it by itself (a stuck one after "
                f"{WATCHDOG_S // 60} min without a tick)")
    if svc:
        return (f"its {svc['kind']} service restarts it if it exits, but predates the watchdog for a stuck "
                f"one: `ttp restart {p.name}` restarts it and adds the watchdog")
    return f"no service keeps it running (stopped on purpose?): `ttp start {p.name}` starts it"


_WATCHDOG_SEEN: dict[str, tuple[float, str | None]] = {}


def refresh(p: Project) -> None:
    """Bring an installed service up to date with this runtime (a service installed before the
    watchdog existed gets it) without starting or stopping anything."""
    unit = _unit(p)
    if unit.exists() and unit.read_text(errors="replace") != _unit_text(p):
        durable_write(unit, _unit_text(p))
        _run("systemctl", "--user", "daemon-reload")
    if sys.platform == "darwin" and _agent(p).exists() and not _agent(p, watchdog=True).exists():
        _install_launchd_watchdog(p)
    if shutil.which("crontab"):
        cur = _run("crontab", "-l").stdout
        tag = f"{CRON_TAG}{p.base}"
        if tag in cur and "ttp.watchdog" not in "".join(ln for ln in cur.splitlines() if tag in ln):
            keep = [ln for ln in cur.splitlines() if tag not in ln]
            keep += _cron_lines(p)
            subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, capture_output=True)
    _WATCHDOG_SEEN.pop(str(p.base), None)


def restart_service(p: Project) -> str:
    """Restart the daemon process. Running workers are not touched: the new daemon adopts them."""
    refresh(p)
    label = launchd_label(p)
    if sys.platform == "darwin" and _agent(p).exists():
        r = _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
        if r.returncode == 0:
            return "restarted"
        # The agent is not loaded (its bootstrap failed): restart the daemon by hand like cron does.
    unit = _unit(p)
    if unit.exists():
        try:
            r = _run("systemctl", "--user", "restart", unit.name)
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
    _spawn(p)
    return "restarted"


def _git(h: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(h), "-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost",
                           *args], capture_output=True, text=True, timeout=120)


# A caller that cannot reach the service manager (a sandboxed worker: no user bus, no signals to the
# daemon) asks the running daemon to restart instead: it writes RESTART_REQUEST in state/, the daemon
# runs `python -m ttp.service` (with the harness runtime on disk) detached, and that restart, made from
# the host with the service manager in reach, writes RESTART_RESULT.
RESTART_REQUEST, RESTART_RESULT = "restart.request", "restart.result"
# A helper that dies without a result (its runtime fails to import, a kill, a reboot) is reaped by the
# daemon: its own handle says it ended, or its claim outlived HELPER_BOUND_S, longer than a restart's own
# waits (a new daemon's first tick, twice with a rollback, plus the service manager's time).
HELPER_BOUND_S = 2 * (60 + 300) + 300
HELPER_STARTED = "restart helper running"     # main() logs it first: the helper got past its imports
_helpers: dict[str, subprocess.Popen] = {}    # the daemon's handle on the helper it started, per project
# The service manager could not be reached from here, as opposed to it trying and the daemon failing.
_UNREACHABLE_RE = re.compile(r"failed to connect to bus|failed to get d-bus connection|DBUS_SESSION_BUS_ADDRESS|"
                             r"XDG_RUNTIME_DIR|operation not permitted|permission denied|not authori[sz]ed|"
                             r"access denied|could not run", re.I)


class Restarted(str):
    """restart()'s report. `outcome`: running, busy (alive, still in its first tick), deferred (the
    restart has not happened yet; the old daemon runs on), rolled_back, or failed."""
    outcome = "running"


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
    with open(p.logs / "restart.log", "a") as log:
        log.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} restart requested by pid {info.get('pid', '?')}: "
                  f"{str(info.get('why') or '')[-300:]}\n")
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
        p.db.post("out", f"The requested daemon restart failed. {text}", chat=None, kind="alert", severity="high")
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


def wait_for_start(p: Project, since: float, wait_s: float, tick_wait_s: float | None = None,
                   waits_from: float | None = None) -> str:
    """How a daemon started after `since` came up: "running" once it completed a tick; "busy" if it
    is alive and still in its first tick after tick_wait_s (a first tick may fetch and add worktrees),
    or if no new daemon started because the old one is still alive and holds the lock; otherwise
    "broken": it did not start within wait_s, it exited, or its first tick failed twice. Both waits
    count from `waits_from` (default `since`)."""
    from .daemon import HEARTBEAT_STALE_S, _alive, _is_daemon, _read_pid, heartbeat, start_marker
    tick_wait_s = HEARTBEAT_STALE_S if tick_wait_s is None else tick_wait_s
    waits_from = since if waits_from is None else waits_from
    while True:
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
        elif now >= waits_from + wait_s:
            old = _read_pid(p.state / "daemon.pid")
            return "busy" if old and _is_daemon(old) or old_daemon_beats(p, since, after=since) else "broken"
        time.sleep(poll_s(1))


def restart(p: Project, wait_s: float = 60, restart_fn=None, tick_wait_s: float | None = None,
            requested: bool = False) -> Restarted:
    """Restart the daemon and confirm it ticks. If it is broken (never started, exited, or its first
    tick keeps failing) and the harness runtime changed since the commit a daemon last ran well on,
    put runtime/ back to that commit as a new commit (history, charter, memory and prompts untouched),
    restart again and alert. A daemon that is alive but still in a slow first tick is left alone.
    When the service manager refuses while the old daemon still ticks, nothing restarted: no rollback.
    If it could not be reached from here (a sandbox), the running daemon is asked to restart itself
    (unless this is that restart, `requested`). The result's `outcome` says how it went."""
    from .daemon import start_marker
    restart_fn = restart_fn or restart_service
    t0 = time.time()
    msg = restart_fn(p)
    if msg != "restarted" and old_daemon_beats(p, t0):
        if unreachable(msg) and not requested:
            return request_restart(p, f"The service manager could not be reached from here ({msg})", wait_s)
        return _said(f"The daemon was not restarted: {msg}. The old daemon still runs the previous runtime; "
                     f"nothing was rolled back", "failed")
    if msg != "restarted" and unreachable(msg):
        return _said(f"Restart unavailable from here: {msg}. No daemon heartbeat is fresh either, so it may be "
                     f"down; its service restarts it from the host. Nothing was rolled back: the new runtime "
                     f"was never started, so nothing shows it is at fault", "failed")
    state = wait_for_start(p, t0, wait_s, tick_wait_s, waits_from=time.time())
    if state == "running":
        return _said(f"{msg}; the daemon is running", "running")
    if state == "busy":
        st = start_marker(p)
        if not (st and float(st.get("started") or 0) >= t0):
            return _said(f"{msg}; but the old daemon has not exited after {time.time() - t0:.0f}s, so the new one "
                         f"has not started yet. Check `ttp status {p.name}` shortly", "deferred")
        return _said(f"{msg}; the daemon is up but still in its first tick after {time.time() - t0:.0f}s. "
                     f"Check `ttp status {p.name}` shortly", "busy")
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
    if _git(h, "restore", f"--source={good}", "--staged", "--worktree", "--", "runtime").returncode != 0:
        _git(h, "checkout", good, "--", "runtime")    # git older than 2.23
    _git(h, "commit", "-q", "-m", f"roll back runtime to {good[:10]}: the daemon did not start with it")
    t1 = time.time()
    restart_fn(p)
    back = wait_for_start(p, t1, wait_s, tick_wait_s, waits_from=time.time()) != "broken"
    commits = "; ".join(changed.splitlines()[:10])
    text = (f"The daemon did not start after a runtime change, so the harness runtime was rolled back to "
            f"{good[:10]}, the last version that ran (a new commit; nothing was deleted). Rolled back: {commits}. "
            + ("" if msg == "restarted" else f"The service manager said: {msg}. ")
            + ("The daemon is running again." if back else f"It still does not start: check `ttp logs {p.name}`."))
    p.db.post("out", text, chat=None, kind="alert", severity="high")
    return _said(text, "rolled_back")


if __name__ == "__main__":
    sys.exit(main())

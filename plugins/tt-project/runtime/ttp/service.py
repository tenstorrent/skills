# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep the daemon alive across logouts, crashes and reboots, without root.

Linux: a systemd user unit (restart on failure) plus `loginctl enable-linger` so it runs without
a login session and starts at boot. Where linger is refused, a crontab watchdog (@reboot plus
every 5 minutes) restarts the daemon instead. macOS: a launchd agent with KeepAlive and the
project folder as working directory (never `/` or the home folder).
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import poll_s
from .project import Project


def unit_name(p: Project) -> str:
    return "tt-project-" + re.sub(r"[^A-Za-z0-9_.-]", "-", p.name)


def daemon_argv(p: Project) -> list[str]:
    return [sys.executable, "-m", "ttp.daemon", str(p.base)]


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


def _install_systemd(p: Project) -> str:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    env = "\n".join(f"Environment={k}={v}" for k, v in _env(p).items())
    (d / f"{unit_name(p)}.service").write_text(f"""[Unit]
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

[Install]
WantedBy=default.target
""")
    _run("systemctl", "--user", "daemon-reload")
    r = _run("systemctl", "--user", "enable", "--now", f"{unit_name(p)}.service")
    if r.returncode != 0:
        return "systemd failed (" + r.stderr.strip()[:200] + "); " + _install_cron(p)
    return f"systemd user unit {unit_name(p)}"


def _install_launchd(p: Project) -> str:
    label = f"com.tt-project.{unit_name(p)}"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    p.logs.mkdir(parents=True, exist_ok=True)
    job = {"Label": label, "ProgramArguments": daemon_argv(p), "WorkingDirectory": str(p.base),
           "EnvironmentVariables": _env(p), "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Background",
           "ThrottleInterval": 10, "AbandonProcessGroup": True, "StandardOutPath": str(p.logs / "launchd.log"),
           "StandardErrorPath": str(p.logs / "launchd.log")}
    with open(plist, "wb") as f:
        plistlib.dump(job, f)
    uid = os.getuid()
    _run("launchctl", "bootout", f"gui/{uid}/{label}")
    r = _run("launchctl", "bootstrap", f"gui/{uid}", str(plist))
    if r.returncode != 0:
        return f"launchd bootstrap failed: {r.stderr.strip()[:200]}"
    return f"launchd agent {label}"


CRON_TAG = "# tt-project:"


def _install_cron(p: Project) -> str:
    cur = _run("crontab", "-l").stdout if shutil.which("crontab") else ""
    tag = f"{CRON_TAG}{p.base}"
    keep = [ln for ln in cur.splitlines() if tag not in ln]
    cmd = _cron_cmd(p)
    keep += [f"@reboot {cmd} {tag}", f"*/5 * * * * {cmd} {tag}"]
    r = subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, capture_output=True)
    if r.returncode != 0:
        return "crontab install failed: " + r.stderr.strip()[:200]
    # The daemon refuses to start twice (lock file), so the 5-minute entry is a no-op while it runs.
    _spawn(p)
    return "crontab watchdog"


def _spawn(p: Project) -> None:
    subprocess.Popen(["sh", "-c", _cron_cmd(p)], start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _cron_cmd(p: Project) -> str:
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env(p).items())
    return f"cd {shlex.quote(str(p.base))} && {env} {' '.join(shlex.quote(a) for a in daemon_argv(p))} " \
           f">> {shlex.quote(str(p.logs / 'daemon.out'))} 2>&1"


def uninstall(p: Project) -> str:
    done = []
    if sys.platform == "darwin":
        label = f"com.tt-project.{unit_name(p)}"
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{label}")
        (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
        done.append("launchd")
    unit = Path.home() / ".config" / "systemd" / "user" / f"{unit_name(p)}.service"
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


def restart_service(p: Project) -> str:
    """Restart the daemon process. Running workers are not touched: the new daemon adopts them."""
    label = f"com.tt-project.{unit_name(p)}"
    if sys.platform == "darwin" and (Path.home() / "Library" / "LaunchAgents" / f"{label}.plist").exists():
        r = _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
        if r.returncode == 0:
            return "restarted"
        # The agent is not loaded (its bootstrap failed): restart the daemon by hand like cron does.
    unit = Path.home() / ".config" / "systemd" / "user" / f"{unit_name(p)}.service"
    if unit.exists():
        r = _run("systemctl", "--user", "restart", unit.name)
        return "restarted" if r.returncode == 0 else r.stderr.strip()[:200]
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
            return "busy" if old and _is_daemon(old) else "broken"
        time.sleep(poll_s(1))


def restart(p: Project, wait_s: float = 60, restart_fn=None, tick_wait_s: float | None = None) -> str:
    """Restart the daemon and confirm it ticks. If it is broken (never started, exited, or its first
    tick keeps failing) and the harness runtime changed since the commit a daemon last ran well on,
    put runtime/ back to that commit as a new commit (history, charter, memory and prompts untouched),
    restart again and alert. A daemon that is alive but still in a slow first tick is left alone."""
    from .daemon import start_marker
    restart_fn = restart_fn or restart_service
    t0 = time.time()
    msg = restart_fn(p)
    state = wait_for_start(p, t0, wait_s, tick_wait_s, waits_from=time.time())
    if state == "running":
        return f"{msg}; the daemon is running"
    if state == "busy":
        st = start_marker(p)
        if not (st and float(st.get("started") or 0) >= t0):
            return (f"{msg}; but the old daemon has not exited after {time.time() - t0:.0f}s, so the new one "
                    f"has not started yet. Check `ttp status {p.name}` shortly")
        return (f"{msg}; the daemon is up but still in its first tick after {time.time() - t0:.0f}s. "
                f"Check `ttp status {p.name}` shortly")
    h = p.harness
    good = (p.db.kv("harness_good") or {}).get("commit")
    changed = _git(h, "log", "--format=%h %s", f"{good}..HEAD", "--", "runtime").stdout.strip() if good else ""
    dirty = _git(h, "status", "--porcelain", "--", "runtime").stdout.strip() if good else ""
    if not changed and not dirty:
        return (f"{msg}; but the daemon did not start or its first tick failed. Check `ttp logs {p.name}`"
                + ("" if good else " (no known-good harness commit to fall back to)"))
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
            + ("The daemon is running again." if back else f"It still does not start: check `ttp logs {p.name}`."))
    p.db.post("out", text, chat=None, kind="alert", severity="high")
    return text

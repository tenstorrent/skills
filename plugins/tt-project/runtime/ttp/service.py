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
from pathlib import Path

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
           "ThrottleInterval": 10, "StandardOutPath": str(p.logs / "launchd.log"),
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
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in _env(p).items())
    cmd = f"cd {shlex.quote(str(p.base))} && {env} {' '.join(shlex.quote(a) for a in daemon_argv(p))} " \
          f">> {shlex.quote(str(p.logs / 'daemon.out'))} 2>&1"
    keep += [f"@reboot {cmd} {tag}", f"*/5 * * * * {cmd} {tag}"]
    r = subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, capture_output=True)
    if r.returncode != 0:
        return "crontab install failed: " + r.stderr.strip()[:200]
    # The daemon refuses to start twice (pid file), so the 5-minute entry is a no-op while it runs.
    subprocess.Popen(["sh", "-c", cmd], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "crontab watchdog"


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


def restart(p: Project) -> str:
    if sys.platform == "darwin":
        label = f"com.tt-project.{unit_name(p)}"
        r = _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
        return "restarted" if r.returncode == 0 else r.stderr.strip()[:200]
    unit = Path.home() / ".config" / "systemd" / "user" / f"{unit_name(p)}.service"
    if unit.exists():
        r = _run("systemctl", "--user", "restart", unit.name)
        return "restarted" if r.returncode == 0 else r.stderr.strip()[:200]
    pid = (p.state / "daemon.pid")
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 15)
        except (OSError, ValueError):
            pass
    return "stopped; the crontab watchdog restarts it within 5 minutes"

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A kept SSH tunnel to a remote project's web app (`ttp web <name> --tunnel --keep`).

A user service on the viewer's computer runs `ssh -N -L` and restarts it whenever it exits: after
a reboot (at login), a network drop or a sleep. ServerAliveInterval makes a dead connection exit
instead of hanging. macOS: a launchd agent (KeepAlive, ThrottleInterval between restarts). Linux:
a systemd user unit (Restart=always with a growing delay, never giving up). The service is named
com.tt-project.tunnel.<name>; installing again adopts or replaces it, and `--unkeep` removes it.
The ssh login must work without a prompt (a key, or an agent the service can reach).
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

from .project import HOME_DIR

FORWARD = re.compile(r"-L\s*(?:127\.0\.0\.1:)?(\d+):127\.0\.0\.1:(\d+)")


def label(name: str) -> str:
    return "com.tt-project.tunnel." + re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def ssh_argv(host: str, local: int, remote: int, ssh: str | None = None) -> list[str]:
    """The forward: this computer's localhost:local to the project machine's localhost:remote."""
    return [ssh or shutil.which("ssh") or "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
            "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}", host]


def systemd_unit(name: str, argv: list[str]) -> str:
    return (f"[Unit]\nDescription=tt-project: SSH tunnel to the web app of {name}\n"
            # Never give up: a laptop may be offline for hours.
            f"StartLimitIntervalSec=0\n\n"
            f"[Service]\nExecStart={' '.join(shlex.quote(a) for a in argv)}\n"
            # 5 s after a drop, growing to 5 min while the machine stays unreachable (systemd 254+;
            # older versions ignore the two step keys and keep the 5 s).
            f"Restart=always\nRestartSec=5\nRestartSteps=10\nRestartMaxDelaySec=300\n\n"
            f"[Install]\nWantedBy=default.target\n")


def launchd_plist(name: str, argv: list[str]) -> dict:
    log = str(HOME_DIR / f"tunnel-{re.sub(r'[^A-Za-z0-9_.-]', '-', name)}.log")
    return {"Label": label(name), "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30, "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log}


def service_file(name: str, platform: str | None = None) -> Path:
    if (platform or sys.platform) == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{label(name)}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{label(name)}.service"


def installed(name: str, platform: str | None = None) -> dict | None:
    """The kept tunnel's forward as installed: {"file", "host", "local", "remote"}, or None."""
    f = service_file(name, platform)
    if not f.is_file():
        return None
    try:
        if f.suffix == ".plist":
            argv = [str(a) for a in plistlib.loads(f.read_bytes()).get("ProgramArguments") or []]
        else:
            line = next((ln for ln in f.read_text().splitlines() if ln.startswith("ExecStart=")), "")
            argv = shlex.split(line[len("ExecStart="):])
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {"file": str(f), "host": "", "local": 0, "remote": 0}
    m = FORWARD.search(" ".join(argv))
    return {"file": str(f), "host": argv[-1] if argv else "", "local": int(m.group(1)) if m else 0,
            "remote": int(m.group(2)) if m else 0}


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=60)


def _stop(name: str, platform: str) -> None:
    if platform == "darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{label(name)}")
    else:
        _run("systemctl", "--user", "disable", "--now", f"{label(name)}.service")


def _start(name: str, platform: str) -> str:
    f = service_file(name, platform)
    if platform == "darwin":
        r = _run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(f))
        if r.returncode != 0 and "already" not in (r.stderr or "").lower():
            return f"launchd refused it: {r.stderr.strip()[:200]}"
        _run("launchctl", "kickstart", f"gui/{os.getuid()}/{label(name)}")
        return "launchd agent"
    _run("systemctl", "--user", "daemon-reload")
    r = _run("systemctl", "--user", "enable", "--now", f"{label(name)}.service")
    if r.returncode != 0:
        return f"systemd refused it: {r.stderr.strip()[:200]}"
    return "systemd user unit"


def keep(name: str, host: str, remote: int, pick_port, platform: str | None = None) -> tuple[int, str]:
    """Install, adopt or replace the kept tunnel; returns (local port, what was done).
    `pick_port(preferred)` returns a free local port. A service already forwarding to the same
    host and port is adopted as is (and started if it was stopped); any other is stopped and
    replaced, keeping its local port when that is free, so the page's address stays the same."""
    platform = platform or sys.platform
    have = installed(name, platform)
    if have and have["host"] == host and have["remote"] == remote and have["local"]:
        how = _start(name, platform)
        return have["local"], f"adopted the kept tunnel already installed ({how}, {have['file']})"
    if have:
        _stop(name, platform)   # frees its port before a new one is picked
    local = pick_port(have["local"] if have and have["local"] else remote + 100)
    argv = ssh_argv(host, local, remote)
    f = service_file(name, platform)
    f.parent.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        f.write_bytes(plistlib.dumps(launchd_plist(name, argv)))
    else:
        f.write_text(systemd_unit(name, argv))
    how = _start(name, platform)
    verb = "replaced the kept tunnel" if have else "installed a kept tunnel"
    return local, f"{verb} ({how}, {f})"


def unkeep(name: str, platform: str | None = None) -> str:
    platform = platform or sys.platform
    f = service_file(name, platform)
    if not f.is_file():
        return f"no kept tunnel for {name}"
    _stop(name, platform)
    f.unlink()
    if platform != "darwin":
        _run("systemctl", "--user", "daemon-reload")
    return f"removed the kept tunnel for {name} ({f})"

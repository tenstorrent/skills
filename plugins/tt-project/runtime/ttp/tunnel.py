# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A kept SSH tunnel to a remote project's web app (`ttp web <name> --tunnel --keep`).

A user service on the viewer's computer runs `ssh -N -L` and restarts it whenever it exits: after
a reboot (at login), a network drop or a sleep. ServerAliveInterval makes a dead connection exit
instead of hanging. ControlMaster=no and ControlPath=none make it own its forward: as a client of a
shared master from the user's ssh config it would hand the forward over and exit, and the forward
would die with that master. macOS: a launchd agent (KeepAlive, ThrottleInterval between restarts). Linux:
a systemd user unit (Restart=always with a growing delay, never giving up). The service is named
com.tt-project.tunnel.<name>; installing again adopts or replaces it, and `--unkeep` removes it.
Each restart first waits a random 0-60 s (RECONNECT_SPREAD_S), so the services of a computer waking
from sleep do not all log in to one host in the same second; the start that installs or restarts one
does not wait.
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

from . import hoststate, sshcmd
from .project import HOME_DIR, durable_write

# The forward's own connection, never a shared master's (an agent without both is replaced).
OWN = (re.compile(r"ControlMaster[=\s]+no\b", re.I), re.compile(r"ControlPath[=\s]+none\b", re.I))
# Ours, and the shapes a hand-made service uses: `-L <local>:localhost:<remote>`, `-L127.0.0.1:<local>:...`.
FORWARD = re.compile(r"-L\s*(?:(?:127\.0\.0\.1|localhost):)?(\d+):(?:127\.0\.0\.1|localhost):(\d+)")
# Each start of a kept ssh service (these tunnels, the bridge sessions) first sleeps a random 0..RECONNECT_SPREAD_S s:
# ~15 of them reconnecting in the same second after a wake trip the host's sshd MaxStartups (10 by default), which
# resets the logins past it. "$0" is the service's start marker: an install or restart leaves it (ready()) and the
# start it means removes it and skips the sleep. POSIX sh, so `$RANDOM` is out: two bytes of /dev/urandom.
RECONNECT_SPREAD_S = 60
SPREAD_SH = (f'rm -- "$0" 2>/dev/null || {{ n=$(od -An -N2 -tu2 /dev/urandom 2>/dev/null); '
             f'sleep $(( ${{n:-0}} % {RECONNECT_SPREAD_S + 1} )); }}; exec "$@"')


def spread(argv: list[str], marker: Path) -> list[str]:
    """argv started after the random wait, skipped once while `marker` exists (the host stays argv's last word)."""
    return ["/bin/sh", "-c", SPREAD_SH, str(marker), *argv]


def ready(argv: list[str]) -> None:
    """The next start of this spread service starts at once (an install or restart means to start it now)."""
    if argv[:3] == ["/bin/sh", "-c", SPREAD_SH]:
        Path(argv[3]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[3]).touch()


def marker(name: str) -> Path:
    return HOME_DIR / f"tunnel-{re.sub(r'[^A-Za-z0-9_.-]', '-', name)}.start"


def label(name: str) -> str:
    return "com.tt-project.tunnel." + re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def ssh_argv(host: str, local: int, remote: int, ssh: str | None = None) -> list[str]:
    """The forward: this computer's localhost:local to the project machine's localhost:remote."""
    sshcmd.check_host(host)
    return [ssh or shutil.which("ssh") or "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-L", f"127.0.0.1:{local}:127.0.0.1:{remote}", "--", host]


def _unit_escape(line: str) -> str:
    """systemd expands %specifiers and $VARIABLES in ExecStart; doubling them keeps them literal."""
    return line.replace("%", "%%").replace("$", "$$")


def _unit_unescape(line: str) -> str:
    return re.sub(r"([%$])\1", r"\1", line)


def systemd_unit(name: str, argv: list[str]) -> str:
    return (f"[Unit]\nDescription=tt-project: SSH tunnel to the web app of {name}\n"
            # Never give up: a laptop may be offline for hours.
            f"StartLimitIntervalSec=0\n\n"
            f"[Service]\nExecStart={_unit_escape(' '.join(shlex.quote(a) for a in argv))}\n"
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
    return hoststate.unit_dir() / f"{label(name)}.service"   # a test run's fake under its marker


def installed(name: str, platform: str | None = None) -> dict | None:
    """The kept tunnel's forward as installed: {"file", "host", "local", "remote", "own", "spread"}, or None.
    "own": its ssh keeps its own connection (not a client of a shared ControlMaster); "spread": its
    restarts wait the random start delay (spread())."""
    f = service_file(name, platform)
    if not f.is_file():
        return None
    try:
        if f.suffix == ".plist":
            argv = [str(a) for a in plistlib.loads(f.read_bytes()).get("ProgramArguments") or []]
        else:
            line = next((ln for ln in f.read_text().splitlines() if ln.startswith("ExecStart=")), "")
            argv = shlex.split(_unit_unescape(line[len("ExecStart="):]))
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {"file": str(f), "host": "", "local": 0, "remote": 0, "own": False, "spread": False}
    words = " ".join(argv).split()   # also sees into `sh -c "exec ssh ... host"`
    line = " ".join(words)
    m = FORWARD.search(line)
    return {"file": str(f), "host": words[-1] if words else "", "local": int(m.group(1)) if m else 0,
            "remote": int(m.group(2)) if m else 0, "own": all(o.search(line) for o in OWN),
            "spread": argv[:3] == ["/bin/sh", "-c", SPREAD_SH]}


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(hoststate.argv(argv), capture_output=True, text=True, timeout=60)


def _stop(name: str, platform: str) -> None:
    if platform == "darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{label(name)}")
    else:
        _run("systemctl", "--user", "disable", "--now", f"{label(name)}.service")


def _start(name: str, platform: str) -> str:
    f = service_file(name, platform)
    if platform == "darwin":
        target = f"gui/{os.getuid()}/{label(name)}"
        if _run("launchctl", "print", target).returncode != 0:   # not loaded yet
            from .service import bootstrap   # retried past launchd's error 5 right after a bootout
            r = bootstrap(label(name), f, _run)
            if r.returncode != 0:
                return f"launchd refused it: {r.stderr.strip()[:200]}"
        _run("launchctl", "kickstart", target)   # starts it if it is not running; never a second copy
        return "launchd agent"
    _run("systemctl", "--user", "daemon-reload")
    r = _run("systemctl", "--user", "enable", "--now", f"{label(name)}.service")
    if r.returncode != 0:
        return f"systemd refused it: {r.stderr.strip()[:200]}"
    return "systemd user unit"


def keep(name: str, host: str, remote: int, pick_port, platform: str | None = None) -> tuple[int, str]:
    """Install, adopt or replace the kept tunnel; returns (local port, what was done).
    `pick_port(preferred)` returns a free local port. A service already forwarding to the same
    host and port on its own connection with the start delay is adopted as is (and started if it was stopped);
    any other (also one that would ride a shared ControlMaster) is stopped and replaced, keeping its local port when that is free, so the page's address stays the same."""
    platform = platform or sys.platform
    have = installed(name, platform)
    if have and have["host"] == host and have["remote"] == remote and have["local"] and have["own"] and have["spread"]:
        how = _start(name, platform)
        return have["local"], f"adopted the kept tunnel already installed ({how}, {have['file']})"
    if have:
        _stop(name, platform)   # frees its port before a new one is picked
    local = pick_port(have["local"] if have and have["local"] else remote + 100)
    argv = spread(ssh_argv(host, local, remote), marker(name))
    f = service_file(name, platform)
    f.parent.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        durable_write(f, plistlib.dumps(launchd_plist(name, argv)))
    else:
        durable_write(f, systemd_unit(name, argv))
    ready(argv)
    how = _start(name, platform)
    verb = "replaced the kept tunnel" if have else "installed a kept tunnel"
    return local, f"{verb} ({how}, {f})"


def restart(name: str, platform: str | None = None) -> str:
    """Restart the kept tunnel's ssh (a forward that stopped answering)."""
    platform = platform or sys.platform
    if not service_file(name, platform).is_file():
        return "no kept tunnel"
    if installed(name, platform)["spread"]:
        marker(name).touch()   # a restart asked for now starts at once
    if platform == "darwin":
        r = _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label(name)}")
    else:
        r = _run("systemctl", "--user", "restart", f"{label(name)}.service")
    return "restarted" if r.returncode == 0 else f"refused: {r.stderr.strip()[:200]}"


def refresh(platform: str | None = None) -> list[str]:
    """After `ttp setup` or an upgrade: re-install each kept tunnel of ours (its own connection) whose restarts
    do not wait the start delay yet, on the same host and ports, so installed tunnels run the current command."""
    if os.environ.get("TTP_TEST_NO_BRIDGE_REFRESH"):
        return []
    platform = platform or sys.platform
    out, d = [], service_file("-", platform).parent
    for f in sorted(d.glob(label("") + "*" + (".plist" if platform == "darwin" else ".service"))):
        name = f.stem[len(label("")):]
        have = installed(name, platform)
        if not have or have["spread"] or not (have["own"] and have["host"] and have["local"] and have["remote"]):
            continue
        try:
            _, did = keep(name, have["host"], have["remote"], lambda port: port, platform)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            did = f"left as it is ({e})"
        out.append(f"tunnel {name}: start delay added; {did}")
    return out


def unkeep(name: str, platform: str | None = None) -> str:
    platform = platform or sys.platform
    f = service_file(name, platform)
    if not f.is_file():
        return f"no kept tunnel for {name}"
    _stop(name, platform)
    f.unlink()
    marker(name).unlink(missing_ok=True)
    if platform != "darwin":
        _run("systemctl", "--user", "daemon-reload")
    return f"removed the kept tunnel for {name} ({f})"

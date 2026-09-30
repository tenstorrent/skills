# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Desktop notifications for the machine the user sits at, with no account or setup.

One small agent per workstation watches every project in this machine's registry — local ones by
reading their database, remote ones with one ssh call per minute — and shows each new alert at
or above the severity floor as a native notification (macOS Notification Center, or notify-send
on a Linux desktop). It never marks anything read; chats and the web app keep their own cursors.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .db import DB, SEVERITY_RANK
from .project import FOLDER, HOME_DIR, Project, hostname, load_registry, write_json

STATE = HOME_DIR / "notifier-state.json"
CONF = HOME_DIR / "notifier.json"


def alerts_since(p: Project, after: int, floor: str = "high") -> list[dict]:
    """Broadcasts at or above the floor after `after`. Ones whose condition has since cleared are
    returned marked `cleared`, so the reader moves its cursor past them without showing them."""
    from .web import cleared
    rank = SEVERITY_RANK.get(floor, 2)
    # Filter severity in the query: a page of quieter broadcasts would otherwise stall the cursor.
    levels = [s for s, r in SEVERITY_RANK.items() if r >= rank]
    db = DB(p.state / "project.db")
    try:
        rows = db.q("SELECT id, ts, kind, severity, text, ref FROM messages WHERE direction='out' AND chat IS NULL "
                    f"AND id>? AND severity IN ({','.join('?' * len(levels))}) ORDER BY id LIMIT 50",
                    (after, *levels))
        now = time.time()
        for r in rows:
            r["cleared"] = cleared(db, r, now)
            del r["ref"]
    finally:
        db.close()
    return rows


def _remote_alerts(entry: dict, name: str, after: int, floor: str) -> list[dict] | None:
    cmd = f"{entry['dir']}/{FOLDER}/harness/bin/ttp alerts {shlex.quote(name)} --after {after} --floor {floor} --json"
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", entry.get("ssh") or entry["host"],
                              cmd], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout or "[]")
    except ValueError:
        return None


def show(title: str, body: str, url: str | None = None) -> None:
    body = body.replace("\n", " ")[:240]
    if sys.platform == "darwin":
        tn = shutil.which("terminal-notifier")
        if tn:
            args = [tn, "-title", title, "-message", body, "-sound", "default", "-group", title]
            if url:
                args += ["-open", url]
            subprocess.run(args, capture_output=True, timeout=10)
            return
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    elif shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        subprocess.run(["notify-send", "-a", "tt-project", title, body], capture_output=True, timeout=10)


def run_once(state: dict, floor: str) -> dict:
    for name, entry in sorted(load_registry().get("projects", {}).items()):
        after = int(state.get(name, -1))
        local = entry.get("host") in (None, hostname())
        if local:
            p = Project(entry["dir"])
            if not (p.state / "project.db").exists():
                continue
            if after < 0:   # first sighting: start from now, do not replay history
                db = DB(p.state / "project.db")
                state[name] = int(db.one("SELECT COALESCE(MAX(id),0) m FROM messages")["m"])
                db.close()
                continue
            rows = alerts_since(p, after, floor)
        else:
            rows = _remote_alerts(entry, name, max(after, 0), floor)
            if rows is None:
                continue
            if after < 0:
                state[name] = max([r["id"] for r in rows] + [0])
                continue
        for r in rows:
            if not r.get("cleared"):
                show(f"tt-project · {name}", r["text"])
            state[name] = max(int(state.get(name, 0)), int(r["id"]))
    return state


def main() -> int:
    conf = {"min_severity": "high", "interval_s": 60}
    try:
        conf.update(json.loads(CONF.read_text()))
    except (OSError, ValueError):
        pass
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    while True:
        try:
            state = run_once(state, conf["min_severity"])
            write_json(STATE, state)
        except Exception as e:  # keep notifying other projects even if one fails
            print(f"notifier: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        time.sleep(max(15, int(conf["interval_s"])))


def install() -> str:
    """Background agent for this user on this machine (launchd or systemd user unit)."""
    runtime = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "ttp.notifier"]
    from .providers.base import service_path
    env = {"PYTHONPATH": runtime, "PATH": service_path(), "HOME": str(Path.home())}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        import plistlib
        label = "com.tt-project.notifier"
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        with open(plist, "wb") as f:
            plistlib.dump({"Label": label, "ProgramArguments": argv, "WorkingDirectory": str(HOME_DIR),
                           "EnvironmentVariables": env, "RunAtLoad": True, "KeepAlive": True,
                           "ProcessType": "Background", "StandardOutPath": str(HOME_DIR / "notifier.log"),
                           "StandardErrorPath": str(HOME_DIR / "notifier.log")}, f)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
        return "installed (launchd)" if r.returncode == 0 else f"launchd failed: {r.stderr.strip()[:200]}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    envs = "\n".join(f"Environment={k}={v}" for k, v in env.items())
    (unit_dir / "tt-project-notifier.service").write_text(
        f"[Unit]\nDescription=tt-project desktop notifier\n\n[Service]\nWorkingDirectory={HOME_DIR}\n"
        f"ExecStart={' '.join(shlex.quote(a) for a in argv)}\n{envs}\nRestart=always\nRestartSec=15\n\n"
        f"[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "tt-project-notifier.service"],
                       capture_output=True, text=True)
    return "installed (systemd user unit)" if r.returncode == 0 else f"systemd failed: {r.stderr.strip()[:200]}"


if __name__ == "__main__":
    sys.exit(main())

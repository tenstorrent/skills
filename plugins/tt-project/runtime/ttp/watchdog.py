# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Restart a daemon that is alive but no longer completes ticks, where systemd's WatchdogSec cannot:
launchd runs this every few minutes, and so does the crontab entry. A stuck daemon is ended; the
service (launchd KeepAlive, or the crontab entry right after this) starts a new one, which adopts
the running workers.

One stale look is not enough: a laptop waking from sleep shows an old heartbeat until the daemon's
next tick. The daemon is ended only when two looks at least CONFIRM_S apart saw the same heartbeat
and it is older than WATCHDOG_S.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

from .daemon import HEARTBEAT_STALE_S, WATCHDOG_S, _alive, _is_daemon, _read_pid, heartbeat, log, start_marker
from .project import Project

CONFIRM_S = 60
TERM_GRACE_S = 15   # a daemon stuck in a system call may never run its TERM handler


def last_tick(p: Project, pid: int) -> float | None:
    """When daemon pid last showed progress: its last completed tick, else its start."""
    hb = heartbeat(p)
    if hb and int(hb.get("pid") or 0) == pid:
        return time.time() - hb["age"]
    st = start_marker(p)
    if st and int(st.get("pid") or 0) == pid:
        return float(st.get("started") or 0) or None
    return None


def check(p: Project, now: float | None = None, grace_s: float = TERM_GRACE_S) -> str:
    """Look once; end the daemon if it is stuck. Returns what it found."""
    now = time.time() if now is None else now
    mark = p.state / "watchdog.json"
    pid = _read_pid(p.state / "daemon.pid")
    since = last_tick(p, pid) if pid and _is_daemon(pid) else None
    if since is None or now - since <= HEARTBEAT_STALE_S:
        mark.unlink(missing_ok=True)
        return "ok" if since else "not running"
    try:
        prev = json.loads(mark.read_text())
    except (OSError, ValueError):
        prev = {}
    same = prev.get("pid") == pid and abs(float(prev.get("since") or 0) - since) < 1
    if not same:
        mark.write_text(json.dumps({"pid": pid, "since": since, "seen": now}))
        return "stale"
    if now - since <= WATCHDOG_S or now - float(prev.get("seen") or now) < CONFIRM_S:
        return "stale"
    mins = int((now - since) // 60)
    log(p, f"watchdog: daemon pid={pid} completed no tick for {mins} min; ending it so its service restarts it")
    _end(pid, grace_s)
    mark.unlink(missing_ok=True)
    p.db.post("out", f"The daemon completed no tick for {mins} min, so its watchdog restarted it. "
                     "Running workers were kept.", kind="info", severity="low", ref=f"watchdog:{pid}")
    return "restarted"


def _end(pid: int, grace_s: float) -> None:
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.time() + wait
        while time.time() < deadline and _alive(pid):
            time.sleep(0.5)
        if not _alive(pid):
            return


def main() -> int:
    p = Project(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
    if p.state.is_dir():
        found = check(p)
        if found not in ("ok", "not running"):   # it runs every few minutes: log only what matters
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} watchdog: {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

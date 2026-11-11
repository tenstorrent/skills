# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A planned project move: once the copy on the target host runs, the old copy stops, and the two
never start workers at the same time.

On the source, `ttp move <name> --to <host>[:<name>]` records the move (kv KEY, role "source").
Daemon.check_move asks the target every CHECK_S over ssh (`ttp alive <name> --within <time since the
move>`): it answers 0 once a model run there succeeded since the move began, which needs its daemon
up. Then the source pauses itself (stop()): no worker starts and its coordinator wakes no more, while
its running workers finish with their work kept, as with `ttp drain`. This happens on the first check
that passes, so within a few minutes of the target confirming.

On the target, `ttp move <name> --from <host>[:<name>]` records the move (role "target"). Its
coordinator runs, but it starts no worker (dispatch_hold) until the source has stopped: after its
first successful model run it calls `ttp move <name> --confirmed` on the source, which stops the
source at once and answers 0. A source that cannot be reached for UNREACHABLE_S counts as stopped
(a host that is down starts nothing), and the target logs that it went ahead.

The source's pause is via VIA, so it raises no long-pause alert (projpause.alert_due); `ttp resume`
on the source undoes it, and `ttp move <name> --cancel` forgets a move on either side.
"""
from __future__ import annotations

import shlex
import subprocess
import time

KEY = "project_move"     # kv: {"role", "host", "name", "since", "checked", "first_try", "done", "note"}
CHECK_S = 60
UNREACHABLE_S = 600
VIA = "ttp move"
HELD_NOTE = "moving here from"


def parse(where: str, default_name: str) -> tuple[str, str]:
    host, _, name = str(where).partition(":")
    if not host.strip():
        raise ValueError(f"give the other host as <ssh alias>[:<project name>], not {where!r}")
    return host.strip(), (name.strip() or default_name)


def record(db, role: str, host: str, name: str, now: float | None = None) -> dict:
    rec = {"role": role, "host": host, "name": name, "since": time.time() if now is None else now,
           "checked": 0, "first_try": 0, "done": 0, "note": ""}
    db.set_kv(KEY, rec)
    return rec


def current(db) -> dict | None:
    rec = db.kv(KEY)
    return rec if isinstance(rec, dict) and rec.get("role") in ("source", "target") else None


def stop(db, rec: dict | None, now: float | None = None) -> bool:
    """Pause this (old) copy for the move; False when it was already stopped."""
    now = time.time() if now is None else now
    rec = rec or {}
    if rec.get("done") and db.kv("paused", False):
        return False
    where = f"{rec.get('host')}" if rec.get("host") else "another host"
    if not db.kv("paused", False):
        db.set_paused(True, by="daemon", via=VIA, reason=f"the project moved to {where}: this copy stopped "
                      "once the copy there ran (running workers finish; `ttp resume` undoes it)")
    if rec:
        db.set_kv(KEY, dict(rec, done=now, note="stopped"))
    return True


def _ssh(host: str, cmd: str, timeout: float):
    from .globalcap import REMOTE_TTP, SSH
    return subprocess.run([*SSH, host, f"{REMOTE_TTP} {cmd}"], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def remote(host: str, cmd: str, timeout: float = 45) -> tuple[int, str]:
    """(exit code, last line) of `ttp <cmd>` on host; 255 when ssh or the call failed."""
    try:
        r = _ssh(host, cmd, timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 255, str(e)[:200]
    out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return r.returncode, (out[-1] if out else "")[:200]


def step(db, ran_since, now: float | None = None, call=remote) -> str:
    """One look at the move; what changed, for the log, or ''. `ran_since(t)`: whether a model run
    here succeeded since t."""
    now = time.time() if now is None else now
    rec = current(db)
    if not rec or rec.get("done") or now - float(rec.get("checked") or 0) < CHECK_S:
        return ""
    rec = dict(rec, checked=now)
    name = shlex.quote(rec["name"])
    if rec["role"] == "source":
        code, said = call(rec["host"], f"alive {name} --within {max(int(now - rec['since']), 1)}s")
        db.set_kv(KEY, dict(rec, note=said))
        if code == 0:
            stop(db, rec, now)
            return f"the copy on {rec['host']} runs ({said}): this copy stopped starting work"
        return ""
    if not ran_since(rec["since"]):
        db.set_kv(KEY, rec)
        return ""
    first = float(rec.get("first_try") or now)
    code, said = call(rec["host"], f"move {name} --confirmed")
    if code == 0:
        db.set_kv(KEY, dict(rec, first_try=first, done=now, note=said))
        return f"the old copy on {rec['host']} stopped: workers start here now"
    if now - first >= UNREACHABLE_S:
        db.set_kv(KEY, dict(rec, first_try=first, done=now, note=f"went ahead: {said}"))
        return (f"the old copy on {rec['host']} did not answer for {UNREACHABLE_S // 60} min ({said}): "
                "workers start here now")
    db.set_kv(KEY, dict(rec, first_try=first, note=said))
    return ""


def dispatch_hold(db) -> str:
    """Why this (new) copy starts no worker yet, or ''."""
    rec = current(db)
    if not rec or rec["role"] != "target" or rec.get("done"):
        return ""
    return f"{HELD_NOTE} {rec['host']}: workers start once the copy there has stopped"

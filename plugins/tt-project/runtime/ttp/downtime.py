# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Downtime the user should hear about: a daemon that will not come back by itself after a reboot,
and one that just came back after a long gap.

Linger: a systemd user service starts at boot only while its user lingers (`loginctl enable-linger`).
Without it the daemon and its watchdog timer start only at the user's next login after a reboot, and
nothing runs until then (one incident: about 6 h down). service.install turns linger on, but it can be
turned off later, so the daemon looks again at start and once a day (model-free; launchd and cron
services are not checked, every 10 min while it is off). While it is off one normal alert stays open
under LINGER_KEY (kv LINGER_KV holds it, alerts.holds); it clears itself once linger is on again.

Down notice: a daemon that starts more than config `daemon.down_notice_s` (1 h by default) after its
last heartbeat posts one normal message: how long it was down, why (the host rebooted, or the daemon
was stopped), the schedule runs missed and the runs cut short. When a reboot notice goes out for the
same start, it carries this (one message). A short restart posts nothing. The pending notice is kept
in kv DOWN_KEY until it is posted, so a daemon that dies again before its first tick does not lose it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from . import schedule as sched
from .outagemsg import duration

LINGER_KEY = "linger-off"     # the alert's condition key
LINGER_KV = "linger_off"      # kv while linger is off: {"since", "user"}
CHECK_EVERY_S = 86400         # how often linger is looked at while it is on
RECHECK_OFF_S = 600           # ... and while it is off, so the alert clears soon after it is turned on
DOWN_KEY = "down_notice"      # kv: the down notice not posted yet
DOWN_NOTICE_S = 3600          # default daemon.down_notice_s
NOT_LINGERING = "not logged in or lingering"   # loginctl's answer for a user with neither


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30)


def user() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def lingers(name: str, run=None) -> bool | None:
    """Whether user `name` lingers: True, False, or None when loginctl cannot tell."""
    try:
        r = (run or _run)("loginctl", "show-user", name, "-p", "Linger")
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    if r.returncode == 0 and out.startswith("Linger="):
        return out == "Linger=yes"
    return False if NOT_LINGERING in (r.stderr or "") else None


def linger_off(service: dict | None, name: str, run=None, platform: str | None = None) -> bool | None:
    """Whether this daemon's systemd service would not start at boot for lack of linger. False for
    launchd, cron or no service, which do not depend on it; None when loginctl cannot tell."""
    if not (platform or sys.platform).startswith("linux") or (service or {}).get("kind") != "systemd":
        return False
    on = lingers(name, run)
    return None if on is None else not on


def linger_text(name: str) -> str:
    cmd = f"loginctl enable-linger {name}" if name else "loginctl enable-linger"
    return (f"Linger is off for this user: after a reboot this project's daemon and its watchdog start "
            f"only at the user's next login, and nothing runs until then. Turn it on with `{cmd}`.")


def down_notice_s(cfg: dict | None) -> float:
    try:
        return max(0.0, float(((cfg or {}).get("daemon") or {}).get("down_notice_s") or DOWN_NOTICE_S))
    except (TypeError, ValueError):
        return DOWN_NOTICE_S


def missed_runs(db, now: float, sleeps: list | None = None) -> int:
    """Schedule runs missed in whole periods (sched.missed) by the enabled schedules now due."""
    return sum(sched.missed(dict(s), now, sleeps)
               for s in db.q("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?",
                             (now,)))


def note_start(db, last: float | None, now: float, threshold: float, cause: str, missed: int) -> dict | None:
    """At daemon start: keep a down notice to post when the last heartbeat (`last`) is more than
    `threshold` old. A notice still pending from a start that never posted it is kept as it was."""
    rec = db.kv(DOWN_KEY)
    if rec or last is None or now - last <= threshold:
        return rec or None
    running = [r["id"] for r in db.q("SELECT id FROM runs WHERE status='running'")]
    rec = {"last": last, "back": now, "cause": cause, "missed": int(missed), "running": running}
    db.set_kv(DOWN_KEY, rec)
    return rec


def cut_short(db, rec: dict) -> int:
    """Of the runs running when the daemon came back, how many have since ended lost."""
    ids = [int(i) for i in rec.get("running") or []]
    if not ids:
        return 0
    return int(db.one(f"SELECT COUNT(*) n FROM runs WHERE status='lost' AND id IN ({','.join('?' * len(ids))})",
                      ids)["n"])


def text(rec: dict, fmt, cut: int | None) -> str:
    """The notice: how long, why, schedule runs missed and (unless the reboot notice it joins says
    it) runs cut short. `fmt` formats a time for the user."""
    down = float(rec["back"]) - float(rec["last"])
    runs = "" if cut is None else f"; {cut} run(s) were cut short"
    return (f"The daemon was down for {duration(down)} (last heartbeat {fmt(rec['last'])}, back at "
            f"{fmt(rec['back'])}): {rec['cause']}. {int(rec.get('missed') or 0)} schedule run(s) were "
            f"missed{runs}.")


def take(db) -> dict | None:
    """The pending notice, removed (call inside the transaction that posts it)."""
    rec = db.kv(DOWN_KEY)
    if rec:
        db.x("DELETE FROM kv WHERE key=?", (DOWN_KEY,))
    return rec or None


def checked_ago(last: float, off: bool, now: float | None = None) -> bool:
    """Whether linger was looked at recently enough not to look again now."""
    return (now or time.time()) - last < (RECHECK_OFF_S if off else CHECK_EVERY_S)

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Alerts that clear themselves, and the split between what needs the user now and everything else.

A high alert posted with a condition key (Daemon.alert, the budget gate) opens an episode in the
`alerts` table. The daemon's sweep closes it once its condition no longer holds and stores when
and why; nothing is deleted. Readers (web app, `ttp status`, chat relay, desktop notifier, Slack)
ask `active()`, which also checks the condition live, so a cleared alert disappears at once.

A host reboot is information only: it is posted as kind `info` and never opens an episode.

One condition is one broadcast: while its episode is open a repeat only updates `last`, with one
reminder once a day has passed. A condition every project on the machine sees (a logged-out CLI, a
full shared disk) is broadcast by one of them: the first takes a claim in the per-user tt-project
folder, and the others post theirs on the `quiet` channel, shown in the web app and `ttp status`
but never sent to a chat, Slack or the desktop.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from . import prguard
from .db import DB, SEVERITY_RANK
from .schedule import failing

DAY = 86400.0
REMIND_S = DAY          # an open episode is broadcast again once, this long after it was raised
QUIET = "quiet"         # channel of a broadcast that stays out of chats (another project has the claim)
CLAIM_TTL = DAY + 3600  # a claim its holder stopped renewing (gone, or no reminder) passes on after this
FEED_DAYS = 7
# What the chat hears once an episode clears.
CLEARED_TEXT = {
    "budget": "Budget for {arg} is out of red; new work starts again.",
    "auth": "{arg} is logged in again; its queued work starts.",
    "limit": "{arg} accepts work again.",
    "coordinator": "The coordinator is working again: a turn succeeded.",
    "disk": "Disk space is back above the guard; held tasks start again.",
    "run-start": "Runs start again.",
    "schedule": "Schedule {arg} runs again.",
    "release-older": "The installed tt-project is no longer older than this harness.",
    "integrity": "The harness and the task worktrees check out again.",
    "pr-ready": "{arg} is back in draft, closed or approved.",
    "config": "project.json reads again; new work starts again.",
    "push_rejected": "Pushes go through again: a push batch pushed.",
    "after_push_failed": "after_push no longer fails: the last one succeeded, or after_push or the push queue is off.",
    "push_queue_dying": "Push batches finish again.",
}


def _urgent(severity: str | None) -> bool:
    return SEVERITY_RANK.get(severity or "normal", 1) >= SEVERITY_RANK["high"]


def tracked(kind: str, severity: str | None, ref: str | None) -> bool:
    """Whether a broadcast opens an episode: a high alert with a condition key, never a reboot."""
    return kind == "alert" and bool(ref) and _urgent(severity) and not str(ref).startswith("reboot:")


def open_episode(db: DB, key: str, ts: float, message: int, severity: str, text: str) -> None:
    """Called by DB.post in the message's own transaction: a repeat joins the open episode."""
    row = db.one("SELECT id FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1", (key,))
    if row:
        db.x("UPDATE alerts SET last=?, message=?, severity=?, text=? WHERE id=?",
             (ts, message, severity, text[:2000], row["id"]))
    else:
        db.x("INSERT INTO alerts(key,raised,last,message,severity,text) VALUES(?,?,?,?,?,?)",
             (key, ts, ts, message, severity, text[:2000]))


BREAKER = "auth_breaker:"   # kv per provider: its auth breaker (Daemon.open_breaker, check_logins)


def breaker(db: DB, provider: str) -> dict | None:
    """The provider's open auth breaker: no run starts on it until a login check passes."""
    rec = db.kv(BREAKER + provider) or {}
    return rec if rec.get("open") else None


def login_proven(db: DB, provider: str, since: float) -> bool:
    """Whether a run on `provider` started at or after `since` got past the login: it succeeded, or
    it is still running and spends or streams tokens (Daemon.meter_running). Age alone is no proof:
    a CLI that hangs or retries while logged out would release the whole queue."""
    return bool(db.one("SELECT id FROM runs WHERE provider=? AND started>=? AND (status='ok' OR status='running' "
                       "AND (cost_usd>0 OR input_tokens>0 OR output_tokens>0)) LIMIT 1", (provider, since)))


def holds(db: DB, key: str, since: float, now: float) -> bool:
    """Whether the condition behind an alert key is still true. `since` is when it was raised.
    Unknown keys cannot be checked and hold for a day after their last report."""
    kind, _, arg = key.partition(":")
    if kind == "auth":
        # A logout holds while the provider's auth breaker is open; its closing ends the alert.
        rec = db.kv(BREAKER + arg) or {}
        if rec.get("open"):
            return True
        if float(rec.get("closed") or 0) >= since:
            return False
        return not login_proven(db, arg, since)   # an alert from before the breaker
    if kind == "limit":
        # The next successful run on the provider ends it, as does the end of its pause.
        if db.one("SELECT id FROM runs WHERE provider=? AND status='ok' AND started>=? LIMIT 1", (arg, since)):
            return False
        return float((db.kv(f"limited:{arg}") or {}).get("until") or 0) > now
    if kind == "budget":
        return (db.kv("gates", {}).get(arg) or {}).get("level") == "red"
    if key == "disk":
        return bool(db.kv("disk_low"))
    if key == "coordinator":
        return int(db.kv("coordinator_failures", 0)) > 0
    if key == "config":   # the daemon keeps the flag while project.json and its last good copy are unreadable
        return bool(db.kv("config_unreadable"))
    if key == "release-older":
        return bool(db.kv("release_older"))
    if key == "integrity":
        last = db.kv("integrity") or {}
        return bool(last.get("bad") or last.get("worktrees"))
    if kind == "schedule":
        # The next run that does not fail ends it, as does disabling or removing the schedule.
        row = db.one("SELECT last_status FROM schedules WHERE name=? AND enabled=1", (arg,))
        return bool(row) and failing(row["last_status"])
    if kind == "pr-ready":   # the pr-watch watcher keeps the flag while the PR is out of draft unapproved
        return arg in (db.kv(prguard.UNAPPROVED_KEY, {}) or {}) and not prguard.approved(db, arg)
    if key == "run-start":
        return not db.one("SELECT id FROM runs WHERE role!='coordinator' AND started>? LIMIT 1", (since,))
    # The push queue (pushq.py): each lasts until a batch finalized after it was raised shows otherwise.
    if key == "push_rejected":
        return not db.one("SELECT id FROM push_batches WHERE outcome='pushed' AND finalized>=? LIMIT 1", (since,))
    if key == "after_push_failed":   # also over once no after_push will run (pushq.AFTER_PUSH_OFF)
        if db.kv("after_push_off"):
            return False
        return not db.one("SELECT id FROM push_batches WHERE after_push='ok' AND after_finalized>=? LIMIT 1",
                          (since,))
    if key == "push_queue_dying":
        return not db.one("SELECT id FROM push_batches WHERE outcome IS NOT NULL AND outcome NOT IN ('died','error') "
                          "AND finalized>=? LIMIT 1", (since,))
    return now - since < DAY


def episode(db: DB, key: str, ts: float) -> dict | None:
    """The stored episode a message about `key` posted at `ts` belongs to (None before this table)."""
    return db.one("SELECT * FROM alerts WHERE key=? AND raised<=? ORDER BY id DESC LIMIT 1", (key, ts))


def active(db: DB, key: str, ts: float, now: float) -> bool:
    """A message about `key` posted at `ts` still describes a live problem."""
    ep = episode(db, key, ts)
    if ep and ep["cleared"]:
        return False
    return holds(db, key, _since(ep) if ep else ts, now)


def _checkable(key: str) -> bool:
    return key.partition(":")[0] in ("auth", "limit", "budget", "disk", "coordinator", "run-start", "schedule",
                                         "release-older", "integrity", "pr-ready", "config", "push_rejected",
                                         "after_push_failed", "push_queue_dying")


def _since(ep: dict) -> float:
    """What a condition is checked from: when the episode began, or for a condition the harness
    cannot check, when it was last reported."""
    return ep["raised"] if _checkable(ep["key"]) else ep["last"]


def cleared(db: DB, m: dict, now: float) -> bool:
    """A high alert whose condition no longer holds: not worth delivering late. Lower-severity
    messages sharing the key (a budget back to normal) are news whatever the state."""
    return (m.get("kind") == "alert" and _urgent(m.get("severity")) and bool(m.get("ref"))
            and not active(db, m["ref"], m["ts"], now))


def sweep(db: DB, now: float | None = None) -> list[dict]:
    """Close every open episode whose condition cleared: store when and why, let the same condition
    alert again at once, and tell the chats once. Returns the episodes closed."""
    now = now or time.time()
    closed = []
    if not db.one("SELECT id FROM alerts WHERE cleared IS NULL LIMIT 1"):
        return closed   # the usual tick: no write lock taken
    with db.tx():
        for ep in db.q("SELECT * FROM alerts WHERE cleared IS NULL ORDER BY id"):
            if holds(db, ep["key"], _since(ep), now):
                continue
            kind, _, arg = ep["key"].partition(":")
            why = "no longer reported" if not _checkable(ep["key"]) else "condition cleared"
            db.x("UPDATE alerts SET cleared=?, cleared_why=? WHERE id=?", (now, why, ep["id"]))
            sent = db.kv("alerts_sent", {}) or {}
            if ep["key"] in sent:
                sent.pop(ep["key"])
                db.set_kv("alerts_sent", sent)
            if kind in CLEARED_TEXT:
                # A quiet episode clears quietly: the project holding the claim tells the chats.
                first = db.one("SELECT channel FROM messages WHERE id=?", (ep["message"],)) if ep["message"] else None
                db.post("out", "Cleared: " + CLEARED_TEXT[kind].format(arg=arg), chat=None, kind="resolved",
                        severity="normal", ref=ep["key"],
                        channel=QUIET if first and first["channel"] == QUIET else "chat")
            closed.append({**ep, "cleared": now, "cleared_why": why})
    return closed


def claims_path() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / "alert-claims.json"


def claim(claim_key: str, key: str, owner: str, now: float, cleared: float = 0.0) -> bool:
    """Take or renew this user's claim on broadcasting a machine-wide condition. False while another
    project holds a live claim. A claim is stale, and taken over, once it is older than the caller's
    last cleared episode of the condition (it belongs to an earlier one) or its owner's daemon is
    gone. A claim file that cannot be read or written never silences anyone."""
    path = claims_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # several daemons of this user share the file
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data = {k: v for k, v in (data if isinstance(data, dict) else {}).items()
                if isinstance(v, dict) and now - float(v.get("ts") or 0) < CLAIM_TTL}
        held = data.get(claim_key)
        if (held and held.get("owner") != owner and float(held.get("ts") or 0) > cleared
                and owner_alive(str(held.get("owner")))):
            return False
        data[claim_key] = {"owner": owner, "alert": key, "ts": now}
        from .project import write_json
        write_json(path, data, 0o600)
        return True
    except OSError:
        return True
    finally:
        os.close(fd)


def release(claim_keys: list[str], cleared: float) -> None:
    """Drop every claim on these machine-wide conditions taken before an episode of theirs cleared,
    whoever holds it: the condition went away for all projects alike, so the next project that sees
    it again broadcasts it at once, even while the old claimer sits idle or is stopped."""
    path = claims_path()
    if not claim_keys or not path.exists():
        return
    try:
        fd = os.open(str(path.with_suffix(".lock")), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        keep = {k: v for k, v in data.items()
                if not (k in claim_keys and isinstance(v, dict) and float(v.get("ts") or 0) <= cleared)}
        if keep != data:
            from .project import write_json
            write_json(path, keep, 0o600)
    except OSError:
        pass
    finally:
        os.close(fd)


def owner_alive(owner: str) -> bool:
    """The project at `owner` has a running daemon: its pid file (removed when the daemon stops)
    names a live one."""
    from .daemon import _is_daemon, _read_pid
    pid = _read_pid(Path(owner) / "state" / "daemon.pid")
    return bool(pid) and _is_daemon(pid)


KEYLESS_TTL = 3600   # seconds an alert without a condition key stays in the top section


def needs_you(db: DB, now: float, limit: int = 20) -> list[dict]:
    """The top section: open asks first, then high alerts about problems that are active now, each
    newest first. A newer alert on the same condition replaces the older one. Open asks are kept
    whatever their age and are never cut by `limit`, which only caps the alerts after them."""
    asks = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind='ask' AND handled=0 ORDER BY id DESC")
    rows = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind='alert' AND ts>? ORDER BY id DESC LIMIT 300", (now - 14 * DAY,))
    for m in asks:
        m.pop("ref")
    out, seen = [], set()
    for m in rows:
        if len(out) >= limit - len(asks):
            break
        ref = m.pop("ref")
        if not _urgent(m["severity"]):
            continue
        if ref:
            if ref in seen:
                continue
            seen.add(ref)
            if not active(db, ref, m["ts"], now):
                continue
        elif now - m["ts"] >= KEYLESS_TTL:   # an alert with no condition to check stays an hour
            continue
        out.append(m)
    return asks + out


def feed(db: DB, now: float, limit: int = 30) -> list[dict]:
    """Everything below the top section, newest first: FYI notes and decisions, milestones, reboots,
    cleared alerts (with when they cleared). Open asks and live alerts are in the top section."""
    rows = db.q("SELECT id,ts,kind,severity,text,ref FROM messages WHERE direction='out' AND chat IS NULL "
                "AND kind!='ask' AND ts>? ORDER BY id DESC LIMIT ?", (now - FEED_DAYS * DAY, limit * 3))
    out = []
    for m in rows:
        ref = m.pop("ref")
        m["state"] = "info" if m["kind"] in ("info", "resolved") else "fyi"
        if m["kind"] == "alert" and _urgent(m["severity"]):
            if (ref and active(db, ref, m["ts"], now)) or (not ref and now - m["ts"] < KEYLESS_TTL):
                continue   # in the top section
            m["state"] = "cleared"
            ep = episode(db, ref, m["ts"]) if ref else None
            m["cleared_at"] = ep["cleared"] if ep else None
        out.append(m)
        if len(out) >= limit:
            break
    return out

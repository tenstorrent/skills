# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Alerts that clear themselves, and the split between what needs the user now and everything else.

A high alert posted with a condition key (Daemon.alert, the budget gate) opens an episode in the
`alerts` table. The daemon's sweep closes it once its condition no longer holds and stores when
and why; nothing is deleted. Readers (web app, `ttp status`, chat relay, desktop notifier, Slack)
ask `active()`, which also checks the condition live, so a cleared alert disappears at once.

A host reboot is information only: it is posted as kind `info` and never opens an episode.
"""
from __future__ import annotations

import time

from . import prguard
from .db import DB, SEVERITY_RANK
from .schedule import failing

DAY = 86400.0
FEED_DAYS = 7
# What the chat hears once an episode clears.
CLEARED_TEXT = {
    "budget": "Budget for {arg} is out of red; new work starts again.",
    "auth": "{arg} works again: a run succeeded after the logout alert.",
    "limit": "{arg} accepts work again.",
    "coordinator": "The coordinator is working again: a turn succeeded.",
    "disk": "Disk space is back above the guard; held tasks start again.",
    "run-start": "Runs start again.",
    "schedule": "Schedule {arg} runs again.",
    "release-older": "The installed tt-project is no longer older than this harness.",
    "integrity": "The harness and the task worktrees check out again.",
    "pr-ready": "{arg} is back in draft, closed or approved.",
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


def holds(db: DB, key: str, since: float, now: float) -> bool:
    """Whether the condition behind an alert key is still true. `since` is when it was raised.
    Unknown keys cannot be checked and hold for a day after their last report."""
    kind, _, arg = key.partition(":")
    if kind in ("auth", "limit"):
        # The next successful run on the provider ends it. A logout is not over when its pause
        # lapses (the pause only spaces out the probes); a quota limit is.
        if db.one("SELECT id FROM runs WHERE provider=? AND status='ok' AND started>=? LIMIT 1", (arg, since)):
            return False
        if kind == "auth" and db.one(
                "SELECT id FROM runs WHERE provider=? AND status='running' AND started>=? "
                "AND (cost_usd>0 OR input_tokens>0 OR output_tokens>0) LIMIT 1", (arg, since)):
            # A probe that spends or streams tokens (Daemon.meter_running) shows the login works;
            # waiting for it to end would hold every other run for its whole run. Age alone is no
            # proof: a CLI that hangs or retries while logged out would release the whole queue.
            return False
        return kind == "auth" or float((db.kv(f"limited:{arg}") or {}).get("until") or 0) > now
    if kind == "budget":
        return (db.kv("gates", {}).get(arg) or {}).get("level") == "red"
    if key == "disk":
        return bool(db.kv("disk_low"))
    if key == "coordinator":
        return int(db.kv("coordinator_failures", 0)) > 0
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
                                         "release-older", "integrity", "pr-ready")


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
                db.post("out", "Cleared: " + CLEARED_TEXT[kind].format(arg=arg), chat=None, kind="resolved",
                        severity="normal", ref=ep["key"])
            closed.append({**ep, "cleared": now, "cleared_why": why})
    return closed


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

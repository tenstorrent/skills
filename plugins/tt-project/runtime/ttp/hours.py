# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The user's usual active hours, from the times of their own messages (`ttp hours`).

A time-limited step for the user (a sign-in link, a device code) lapses unused when it reaches them
asleep. Before starting one, a task asks `ttp hours --check`: outside the usual hours it hands off
waiting until the next window instead. A step with a deadline of minutes (an MCP sign-in link) also
needs the user at the keyboard: `--present M` passes only if they wrote in the chat in the last M
minutes (`presence`), so the step waits for them to come back instead of going out on a timer.

The hours come only from data, never from fixed defaults: each (day, hour) in the project's home zone
in which the user sent a message over the last DAYS days counts once, so a burst of messages counts
like one. The quiet hours are the longest run of consecutive hours (wrapping past midnight) holding at
most QUIET_SHARE of that activity; the rest of the day is the active window. With fewer than
MIN_MESSAGES messages, or on fewer than MIN_DAYS days, or no quiet run of MIN_QUIET hours, there is no
usual pattern to go by and the answer is "now" (`fallback`).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import timefmt

DAYS = 14
MIN_MESSAGES = 20
MIN_DAYS = 3
MIN_QUIET = 4          # hours
QUIET_SHARE = 0.10

# The user's own words: inbound user messages, not the project's own brief or system posts.
USER_SQL = ("SELECT ts FROM messages WHERE direction='in' AND kind='user' AND ts>=? "
            "AND COALESCE(provenance,'')!='system' AND COALESCE(channel,'')!='system'")


def user_times(db_path: Path, since: float) -> list[float]:
    """Timestamps of the user's messages since `since`, read-only (a run may not write the database)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return [r[0] for r in con.execute(USER_SQL, (since,))]
    finally:
        con.close()


def last_user_time(db_path: Path) -> float | None:
    """When the user last wrote in the project's chat (any time back), read-only; None if never."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        return con.execute(USER_SQL.replace("SELECT ts", "SELECT MAX(ts)", 1), (0,)).fetchone()[0]
    finally:
        con.close()


def presence(last: float | None, zone: str, minutes: float, now: float | None = None) -> dict:
    """Whether the user wrote within the last `minutes` (their last message at `last`), and one line
    saying when they last wrote, in the home zone with its abbreviation."""
    now = time.time() if now is None else now
    ok = last is not None and now - last <= minutes * 60
    if last is None:
        text = "the user has not written in the chat yet"
    else:
        ago = max(0.0, now - last) / 60
        ago_s = f"{ago:.0f} min ago" if ago < 120 else f"{ago / 60:.0f} h ago"
        text = f"the user last wrote in the chat {timefmt.short(last, zone, now)} ({ago_s})"
    text += (f", within the last {minutes:g} min: present" if ok
             else f", not within the last {minutes:g} min: wait until they write")
    return {"minutes": minutes, "last": last, "ok": ok, "text": f"ttp hours: {text}"}


def quiet_hours(counts: list[int]) -> list[int]:
    """The longest circular run of hours holding at most QUIET_SHARE of the activity in `counts`
    (24 per-hour counts); the quietest of equally long runs, then the earliest. [] when none reaches
    MIN_QUIET hours."""
    total = sum(counts)
    best: tuple[int, int, int] | None = None   # (-length, sum, start)
    for start in range(24):
        s = 0
        for length in range(1, 24):
            s += counts[(start + length - 1) % 24]
            if s > total * QUIET_SHARE:
                break
            if length >= MIN_QUIET and (best is None or (-length, s, start) < best):
                best = (-length, s, start)
    if best is None:
        return []
    return [(best[2] + i) % 24 for i in range(-best[0])]


def _next(now: float, tz, want: set[int], limit: int = 48) -> float | None:
    """The first local top of the hour after `now` whose hour is in `want`."""
    base = datetime.fromtimestamp(now, tz).replace(minute=0, second=0, microsecond=0).timestamp()
    for k in range(1, limit + 1):
        ts = base + k * 3600
        if datetime.fromtimestamp(ts, tz).hour in want:
            return ts
    return None


def assess(times: list[float], zone: str, now: float | None = None, need_s: float = 0) -> dict:
    """The active window from message `times` in `zone`, and whether `now` is a good time to start a
    step the user must act on within the next `need_s` seconds."""
    now = time.time() if now is None else now
    tz = timefmt.tzinfo(zone)
    slots = {(d.date(), d.hour) for d in (datetime.fromtimestamp(t, tz) for t in times)}
    days = {d for d, _ in slots}
    counts = [0] * 24
    for _, h in slots:
        counts[h] += 1
    out = {"zone": zone, "messages": len(times), "days": len(days), "counts": counts}
    quiet = quiet_hours(counts) if len(times) >= MIN_MESSAGES and len(days) >= MIN_DAYS else []
    if not quiet:
        why = (f"{len(times)} message(s) on {len(days)} day(s), too few to tell"
               if len(times) < MIN_MESSAGES or len(days) < MIN_DAYS else f"no quiet run of {MIN_QUIET} h")
        return {**out, "fallback": True, "why": why, "active": None, "ok": True, "active_now": True,
                "until": None, "next_start": now}
    active = [h for h in range(24) if h not in quiet]
    start, end = (quiet[-1] + 1) % 24, quiet[0]
    hour = datetime.fromtimestamp(now, tz).hour
    active_now = hour in active
    until = _next(now, tz, set(quiet)) if active_now else None
    ok = active_now and (until is None or until - now >= need_s)
    nxt = now if ok else _next(until if active_now and until else now, tz, set(active))
    return {**out, "fallback": False, "why": f"quiet {end:02d}:00-{start:02d}:00",
            "active": {"start": f"{start:02d}:00", "end": f"{end:02d}:00", "hours": active},
            "ok": ok, "active_now": active_now, "until": until, "next_start": nxt}


def describe(r: dict, now: float | None = None) -> str:
    """One line for people, times in the home zone with its abbreviation."""
    z = r["zone"]
    if r["fallback"]:
        return f"ttp hours: no usual active hours yet ({r['why']}); start now"
    a = r["active"]
    win = f"usual active hours {a['start']}-{a['end']} {timefmt.abbrev(z, now)}"
    if r["ok"]:
        return (f"ttp hours: {win}; now is inside them"
                + (f" until {timefmt.short(r['until'], z, now)}" if r["until"] else "") + "; start now")
    when = timefmt.short(r["next_start"], z, now) if r["next_start"] else "unknown"
    return (f"ttp hours: {win}; " + ("too little of today's window is left" if r["active_now"] else "now is outside them")
            + f"; next window starts {when}")

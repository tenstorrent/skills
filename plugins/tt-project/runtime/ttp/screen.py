# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Observation screening: decide cheaply whether something a watcher saw deserves the
coordinator's attention. Order: exact dedupe → rules → Jev (when configured) → wake or record.

The coordinator is the expensive component, so everything here errs toward recording an
observation as an issue and waking the coordinator only for new, actionable, important ones.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from .db import DB, SEVERITY_RANK

_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{7,64}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?(\.\d+)?(Z|[+-]\d\d:?\d\d)?\b", re.I), "<ts>"),
    (re.compile(r"/tmp/[^\s'\"]+"), "<tmp>"),
    (re.compile(r"\bv\d+(\.\d+)+"), "<n>"),        # versions like v1.0.0
    # numbers not glued to a preceding letter, so host03, gpu1 and t48 keep their identity;
    # units after a number are masked with it: 30s, 12ms, 4GB
    (re.compile(r"(?<![A-Za-z0-9_])\d+(\.\d+)*"), "<n>"),
    (re.compile(r"\s+"), " "),
]

RULES = [
    ("critical", re.compile(r"\b(segfault|core dumped|out of memory|oom-kill|kernel panic|data loss|"
                            r"corrupt(ed|ion)|outage|down for)\b", re.I)),
    ("high", re.compile(r"\b(fatal|panic|traceback|exception|assert(ion)? fail|hang(s|ing)?|timed? ?out|"
                        r"deadlock|regression|failed|failure|error)\b", re.I)),
    ("normal", re.compile(r"\b(warn(ing)?|deprecat|retry|slow|flaky)\b", re.I)),
]


def normalize(text: str) -> str:
    t = text.strip().lower()
    for rx, rep in _VOLATILE:
        t = rx.sub(rep, t)
    return t[:2000]


def fingerprint(source: str, text: str) -> str:
    return hashlib.sha1(f"{source}\n{normalize(text)}".encode()).hexdigest()[:16]


def rule_severity(text: str) -> str:
    for sev, rx in RULES:
        if rx.search(text):
            return sev
    return "info"


@dataclass
class Verdict:
    wake: bool
    severity: str
    reason: str
    fingerprint: str
    issue_id: int
    screen: str = "rules"   # rules | jev | dedupe


def screen(db: DB, cfg: dict, source: str, text: str, hint: str | None = None, jev=None,
           rewake_after_s: float | None = None, repeat: bool = False) -> Verdict:
    """Record the observation as an issue and say whether the coordinator should wake for it.

    A known open issue wakes again when `repeat` is set (the watcher says each report is a new
    event), or when it was last seen more than `rewake_after_s` ago (it came back after a quiet
    spell). Without either, a known open issue stays quiet. An observation an active mute covers
    is recorded and counted but never wakes (see mute)."""
    v = _screen(db, cfg, source, text, hint, jev, rewake_after_s, repeat)
    m = count_muted(db, source, text, v.severity)
    if m and v.wake:
        v.wake, v.reason = False, f"muted ({v.reason})"
    return v


def _screen(db: DB, cfg: dict, source: str, text: str, hint: str | None, jev,
            rewake_after_s: float | None, repeat: bool) -> Verdict:
    fp = fingerprint(source, text)
    now = time.time()
    floor = SEVERITY_RANK.get(cfg.get("screen", {}).get("wake_min_severity", "normal"), 1)
    row = db.one("SELECT * FROM issues WHERE fingerprint=?", (fp,))
    if row:
        db.x("UPDATE issues SET last_seen=?, count=count+1 WHERE id=?", (now, row["id"]))
        if row["status"] == "fixed":
            db.x("UPDATE issues SET status='open' WHERE id=?", (row["id"],))
            return Verdict(True, row["severity"], "regressed after fix", fp, row["id"], "dedupe")
        reason = "known issue"
        if row["status"] == "open" and SEVERITY_RANK.get(row["severity"], 1) >= floor:
            if repeat:
                reason = "repeated"
            elif rewake_after_s is not None and now - float(row["last_seen"] or 0) > rewake_after_s:
                reason = f"back after {(now - float(row['last_seen'] or 0)) / 3600:.1f} h quiet"
        return Verdict(reason != "known issue", row["severity"], reason, fp, row["id"], "dedupe")

    severity = hint or rule_severity(text)
    verdict_src, reason = "rules", f"rule severity {severity}"
    if jev is not None and jev.enabled():
        try:
            ans = jev.decide(state=f"source: {source}\nobservation:\n{text[:6000]}", questions={
                "actionable": {"type": "noul",
                               "instructions": "Does this observation describe a problem someone should act on?",
                               "criteria": {"true": "A failure, regression, hang, error or request needing action.",
                                            "false": "Routine output, progress, noise, or an already-resolved state."}},
                "severity": {"type": "score", "instructions": "How severe is it for the project?",
                             "criteria": ["informational", "minor", "significant", "critical outage or data loss"]},
            }, purpose="screen")
            if ans:
                p = float(ans.get("actionable", {}).get("noul", 0.5))
                s = float(ans.get("severity", {}).get("score", 1.0))
                severity = ["info", "normal", "high", "critical"][max(0, min(3, round(s)))]
                if p < 0.35:
                    severity = "info"
                verdict_src, reason = "jev", f"jev actionable={p:.2f} severity={s:.2f}"
        except Exception as e:  # screening must never take the daemon down
            reason += f" (jev unavailable: {type(e).__name__})"

    title = text.strip().splitlines()[0][:160] if text.strip() else source
    issue_id = db.x("INSERT INTO issues(fingerprint,source,first_seen,last_seen,count,title,severity,status,screen) "
                    "VALUES(?,?,?,?,1,?,?,?,?)", (fp, source, now, now, title, severity,
                                                   "open" if severity != "info" else "ignored",
                                                   json.dumps({"by": verdict_src, "reason": reason})))
    return Verdict(SEVERITY_RANK.get(severity, 1) >= floor, severity, reason, fp, issue_id, verdict_src)


# Mutes ----------------------------------------------------------------------------------------
# A known, recurring condition the user was already told about and nothing of ours can fix: its
# observations are still recorded and counted, but do not wake the coordinator until the mute
# ends, when one summary event does.
MUTES_KEY = "observation_mutes"   # kv: list of active mutes, oldest first
MUTE_MIN_MATCH, MUTE_MAX_H, MUTE_MAX = 3, 72, 20
MUTE_BELOW = ("normal", "high", "critical")


def mute(db: DB, source: Any, match: Any, hours: Any, below: Any = None, why: Any = "",
         now: float | None = None) -> dict:
    """Mute observations from `source` whose text contains `match` (any case) and whose severity is
    below `below` (default critical), for `hours`. Muting the same source and match again replaces
    its end, threshold and reason and keeps its count. Raises ValueError on bad input."""
    now = time.time() if now is None else now
    source, match = str(source or "").strip(), str(match or "").strip()
    if not source:
        raise ValueError("observation_mute needs `source`, the observations' source as the digest shows it "
                         "(e.g. watcher:<name>)")
    if len(match) < MUTE_MIN_MATCH:
        raise ValueError(f"observation_mute needs `match`, a piece of the observation's text of at least "
                         f"{MUTE_MIN_MATCH} characters; got {match!r}")
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = float("nan")
    if not 1 <= h <= MUTE_MAX_H:
        raise ValueError(f"observation_mute needs `hours` from 1 to {MUTE_MAX_H}; got {hours!r}")
    below = str(below or "critical").strip().lower()
    if below not in MUTE_BELOW:
        raise ValueError(f"observation_mute `below` must be one of {', '.join(MUTE_BELOW)}; got {below!r}")
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        old = next((m for m in active if _same_mute(m, source, match)), None)
        if old is None and len(active) >= MUTE_MAX:
            raise ValueError(f"observation_mute rejected: {MUTE_MAX} mutes are already active; let one end first")
        m = {"source": source, "match": match, "below": below, "hours": h, "why": str(why or "").strip()[:300],
             "since": old["since"] if old else now, "until": now + h * 3600,
             "count": old["count"] if old else 0, "last_at": old["last_at"] if old else None}
        db.set_kv(MUTES_KEY, [x for x in active if x is not old] + [m])
    return m


def _same_mute(m: dict, source: str, match: str) -> bool:
    return m["source"].lower() == source.lower() and m["match"].lower() == match.lower()


def mutes(db: DB, now: float | None = None) -> list[dict]:
    """The mutes still in force."""
    now = time.time() if now is None else now
    return [m for m in db.kv(MUTES_KEY, []) or [] if float(m["until"]) > now]


def count_muted(db: DB, source: str, text: str, severity: str, now: float | None = None) -> dict | None:
    """The active mute covering this observation, after counting it there; None when none does."""
    now = time.time() if now is None else now
    rank, low = SEVERITY_RANK.get(severity, 1), text.lower()
    if not db.kv(MUTES_KEY):
        return None
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        for m in active:
            if (float(m["until"]) > now and m["source"].lower() == source.lower() and m["match"].lower() in low
                    and rank < SEVERITY_RANK[m["below"]]):
                m["count"], m["last_at"] = int(m["count"]) + 1, now
                db.set_kv(MUTES_KEY, active)
                return m
    return None


def expire_mutes(db: DB, now: float | None = None) -> list[dict]:
    """End the mutes whose time is up, queuing one summary observation event for each."""
    now = time.time() if now is None else now
    with db.tx():
        active = db.kv(MUTES_KEY, []) or []
        done = [m for m in active if float(m["until"]) <= now]
        if not done:
            return []
        db.set_kv(MUTES_KEY, [m for m in active if float(m["until"]) > now])
        for m in done:
            n = int(m["count"])
            seen = (f"{n} observation{'' if n == 1 else 's'}, last at {_when(m['last_at'])}" if n
                    else "no observations")
            db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (now, m["source"], "observation", "info",
                  f"mute ended: muted {m['source']} {m['match']!r} below {m['below']} for {_hours(m)} h: "
                  f"{seen}" + (f". Muted because: {m['why']}" if m.get("why") else ""), "queued"))
    return done


def mute_line(m: dict, now: float | None = None) -> str:
    """One line for the digest and `ttp status`."""
    now = time.time() if now is None else now
    n = int(m["count"])
    seen = f"{n} muted, last at {_when(m['last_at'])}" if n else "none muted yet"
    return (f"{m['source']} {m['match']!r} below {m['below']}: {seen}; ends in "
            f"{max(0.0, (float(m['until']) - now) / 3600):.1f} h" + (f" ({m['why']})" if m.get("why") else ""))


def _hours(m: dict) -> str:
    return f"{(float(m['until']) - float(m['since'])) / 3600:.1f}".rstrip("0").rstrip(".")


def _when(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else "-"

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Rejected-action cooldown: after the daemon rejects an action of a coordinator turn, the events that
woke that turn do not wake the next one again at once for the same unchanged cause.

- `start`: a turn with a rejected action (not an information-only one) puts the key of each event
  that woke it (event kind, source and task) in a cooldown of `coordinator.rejection_cooldown_s`
  (default COOLDOWN_S; 0 turns it off).
- `screen_queued`: inside the cooldown, a queued event with the same key and the same evidence (its
  text with counters, ids and times masked, screen.normalize) and no higher severity is counted and
  set `muted`, as an observation mute's match is, not woken. Changed evidence or a higher severity
  ends the cooldown early and the event wakes; the user speaking ends every cooldown. When a
  cooldown ends with events counted (the condition persisted), one summary event (KIND) is queued:
  one wake. A turn that rejects again after it restarts the cooldown of the same key.
- `digest_lines`: each cooldown with events counted, as one digest line on the next turn.

Never cooled: a critical event, the summary event, an answered ask, and the safety class (spend or
budget caps, a restriction at risk, data loss, a device or machine hold). User messages are not
events and always wake. The state is a kv entry, so it survives a daemon restart. Model-free."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from . import timefmt
from .db import SEVERITY_RANK

KEY = "rejection_cooldowns"   # kv: {event key: cooldown}
KIND = "rejection_cooldown"   # the one summary event when a cooldown with counted events ends
COOLDOWN_S = 900.0
MAX_KEYS = 50                 # cooldowns kept at once; the oldest go first
REASON_CHARS = 200
SAFETY_RE = re.compile(r"\b(?:spend\w*|budget\w*|over[ -]?spend\w*|cost cap|restrictions?|data loss|lost (?:work|data|"
                       r"messages?)|hold|holds|held|holder|holding)\b", re.I)
ASK_RE = re.compile(r"\bask\b.*\banswer|\banswered\b", re.I)


def window_s(cfg: dict | None) -> float:
    """coordinator.rejection_cooldown_s, else COOLDOWN_S; 0 (or less) turns the cooldown off."""
    try:
        return max(0.0, float(((cfg or {}).get("coordinator") or {}).get("rejection_cooldown_s", COOLDOWN_S)))
    except (TypeError, ValueError):
        return COOLDOWN_S


def event_key(e: dict) -> str:
    """An event's wake key: its kind, source and task ('task_failed:daemon:#12')."""
    return f"{e['kind']}:{e['source']}" + (f":#{e['task']}" if e.get("task") else "")


def evidence(text: Any) -> str:
    """An event text's evidence, counters, ids, durations and times masked (screen.normalize)."""
    from .screen import normalize
    return hashlib.sha1(normalize(str(text or "")).encode()).hexdigest()[:16]


def exempt(e: dict) -> bool:
    """Whether event `e` never enters or meets a cooldown: critical, the summary event, an answered
    ask, or the safety class (named in its kind, source or text)."""
    if e.get("severity") == "critical" or e.get("kind") == KIND:
        return True
    words = " ".join(str(e.get(k) or "").replace("_", " ") for k in ("kind", "source", "text"))
    return bool(SAFETY_RE.search(words) or ASK_RE.search(str(e.get("kind") or "").replace("_", " ")))


def _rows(db, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    return db.q(f"SELECT id, ts, kind, source, task, severity, text, data FROM events "
                f"WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", list(ids))


def start(db, cfg: dict | None, event_ids: list[int], problems: list[str], now: float | None = None) -> list[str]:
    """Start (or restart) the cooldown of each key among `event_ids`, the events a turn woke on,
    when `problems` (that turn's rejections) has one that is not information only. Returns the keys."""
    from .coordinator import INFO_MARK
    win = window_s(cfg)
    real = [str(x) for x in problems or [] if INFO_MARK not in str(x)]
    if win <= 0 or not real or not event_ids:
        return []
    now = time.time() if now is None else now
    reason = " ".join(real[0].split())[:REASON_CHARS]
    started: list[str] = []
    with db.tx():
        state = db.kv(KEY, {}) or {}
        for e in _rows(db, event_ids):
            if e["kind"] == KIND:   # its summary woke the turn: the same key cools again
                try:
                    old = json.loads(e["data"] or "{}").get("cooldown") or {}
                except (TypeError, ValueError):
                    old = {}
                if not old.get("key"):
                    continue
                c = {k: old.get(k) for k in ("key", "sig", "severity", "last_text")}
            elif exempt(e):
                continue
            else:
                c = {"key": event_key(e), "sig": evidence(e["text"]), "severity": e["severity"],
                     "last_text": str(e["text"] or "")[:300]}
            c.update(since=now, until=now + win, reason=reason, count=0, last_at=None)
            state[c["key"]] = c
            started.append(c["key"])
        if started:
            keep = sorted(state, key=lambda k: float(state[k]["since"]))[-MAX_KEYS:]
            db.set_kv(KEY, {k: state[k] for k in keep})
    return started


def _end(db, c: dict, why: str, now: float) -> int | None:
    """Record the end of cooldown `c`: one queued summary event when it counted events, else nothing."""
    n = int(c.get("count") or 0)
    if not n:
        return None
    return db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                (now, "daemon", KIND, c.get("severity") or "normal",
                 f"{line(c)}; ended: {why}. Last seen: {c.get('last_text') or '-'}",
                 json.dumps({"cooldown": c}), "queued"))


def line(c: dict) -> str:
    n = int(c.get("count") or 0)
    return (f"cooldown: {c['key']}, {n} event{'' if n == 1 else 's'} since rejection, "
            f"rejected because {c.get('reason') or '-'}")


def screen_queued(db, cfg: dict | None, queued: list[dict], now: float | None = None,
                  user_spoke: bool = False) -> list[dict]:
    """The queued events that may wake a turn: same-key events with unchanged evidence and no higher
    severity inside a cooldown are counted and set `muted`; cooldowns that ended (time up, changed
    evidence, higher severity, the user spoke, or the setting turned off) queue their summary event,
    which is added. `queued` rows need id, kind, source, task and severity."""
    state = db.kv(KEY, {}) or {}
    if not state:
        return queued
    now = time.time() if now is None else now
    off = window_s(cfg) <= 0
    keep: list[dict] = []
    added: list[int | None] = []
    counted = False
    with db.tx():
        state = db.kv(KEY, {}) or {}
        for k in list(state):
            c = state[k]
            why = ("the user spoke" if user_spoke else "the cooldown is turned off" if off
                   else "its time is up" if float(c["until"]) <= now else "")
            if why:
                added.append(_end(db, state.pop(k), why, now))
        texts = {}
        hit = [e["id"] for e in queued if event_key(e) in state]
        if hit:
            texts = {r["id"]: r["text"] for r in _rows(db, hit)}
        for e in queued:
            c = state.get(event_key(e))
            if c is None or e["id"] not in texts or exempt({**e, "text": texts[e["id"]]}):
                keep.append(e)
                continue
            text = texts[e["id"]]
            if SEVERITY_RANK.get(e["severity"], 1) > SEVERITY_RANK.get(c.get("severity"), 1):
                why = f"severity rose to {e['severity']}"
            elif evidence(text) != c.get("sig"):
                why = "the evidence changed"
            else:
                c.update(count=int(c.get("count") or 0) + 1, last_at=now, last_text=str(text or "")[:300])
                db.x("UPDATE events SET status='muted' WHERE id=? AND status='queued'", (e["id"],))
                counted = True
                continue
            added.append(_end(db, state.pop(event_key(e)), why, now))
            keep.append(e)
        if added or counted:
            db.set_kv(KEY, state)
    ids = [i for i in added if i]
    if not ids:
        return keep
    return sorted(keep + [dict(r) for r in _rows(db, ids)], key=lambda e: e["id"])


def digest_lines(db, now: float | None = None, where: Any = None) -> list[str]:
    """One line per cooldown in force that counted events, for the next turn's digest."""
    now = time.time() if now is None else now
    out = []
    for c in (db.kv(KEY, {}) or {}).values():
        if int(c.get("count") or 0) and float(c["until"]) > now:
            out.append(f"{line(c)}; ends {timefmt.long(float(c['until']), where)}")
    return out

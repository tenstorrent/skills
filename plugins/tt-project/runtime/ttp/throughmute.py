# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Throughput-class observation mutes: a mute may quiet a known condition, never hide a throughput
problem (work not moving: no progress, stale or long waits, a stalled review, an under-used job, an
efficiency watcher, or any observation whose text names a limit, queue or wait as its cause).

- `vet`: the coordinator's `observation_mute` of that class is accepted only with an owner: a `why`
  naming an open task (`#<id>`) that owns the fix, an `end_when` probe, or a `why` saying only the
  user or an outside party can clear it. A renewal needs a fresh `why` (new evidence). It lasts at
  most `screen.throughput_mute_max_h` (default MAX_H), not the general 7 days.
- `tally`: per-hour counts of each throughput-class mute, so the digest's muted line shows a rising
  rate (`rate_text`).
- `review_lines`: every active mute for the daily review (source, match, count, first seen, owner),
  each throughput-class mute whose owner task is done or gone, or that has no owner, flagged as a
  problem to fix that day.

Model-free; reads only the project's own database."""
from __future__ import annotations

import re
import time
from typing import Any

MAX_H = 12.0          # screen.throughput_mute_max_h: the longest a throughput-class mute lasts before renewal
MAX_H_CAP = 72.0
HOURS_KEPT = 12       # hourly buckets kept per mute
RATE_SHOWN = 4        # hourly counts shown in the digest, oldest first
LOOKBACK_S = 7 * 86400   # recent observations read to classify a mute by their text

# Sources whose observations are about work not moving (in the mute's source, or named in its text).
SOURCE_RE = re.compile(r"progress|wait[_-]?stale|lane[_-]?wait|review[_-]?stall|job[_-]?underuse|"
                       r"efficiency(?:[_-]?watch)?|throughput", re.I)
# Text naming a limit, queue or wait as the cause.
CAUSE_RE = re.compile(r"\b(?:limit(?:s|ed)?|rate[- ]limit\w*|cap(?:s|ped)?|quota\w*|queue[sd]?|queueing|queuing|"
                      r"backlog\w*|wait(?:s|ed|ing)?|stall(?:s|ed)?|throttl\w*|no progress|under[- ]?used?)\b", re.I)
TASK_RE = re.compile(r"#(\d+)\b", re.A)
OUTSIDE_RE = re.compile(r"(?<!not )\bonly (?:the user|(?:an? |the )?(?:outside|external|third[- ]party) "
                        r"(?:party|team|service|vendor)|the vendor)\b[^.;]{0,40}?\bcan\b", re.I)


def max_h(cfg: dict | None) -> float:
    """screen.throughput_mute_max_h, else MAX_H, kept within 1..MAX_H_CAP."""
    try:
        h = float(((cfg or {}).get("screen") or {}).get("throughput_mute_max_h") or MAX_H)
    except (TypeError, ValueError):
        h = MAX_H
    return min(MAX_H_CAP, max(1.0, h))


def _texts(db: Any, source: str, match: str, old: dict | None, now: float) -> list[str]:
    """The mute's match, the conditions it already covers and recent observations it would cover."""
    texts = [match] + [str(c.get("text") or "") for c in ((old or {}).get("conds") or {}).values()]
    head, _, kind = source.partition(":")   # daemon:<kind> observations are stored as source daemon, kind <kind>
    try:
        rows = db.q("SELECT text FROM events WHERE ts>=? AND (source=? OR (source=? AND kind=?)) "
                    "AND instr(lower(text), ?)>0 ORDER BY id DESC LIMIT 5",
                    (now - LOOKBACK_S, source, head, kind or source, match.lower()))
    except Exception:   # a classification must not fail the mute on a read
        rows = []
    return texts + [str(r["text"] or "") for r in rows]


def is_class(source: Any, match: Any, texts: list[str] | None = None) -> bool:
    """Whether a mute of `source` and `match` (with the observation `texts` it covers) is throughput-class."""
    texts = [str(match or "")] + (texts or [])
    return any(SOURCE_RE.search(t) for t in [str(source or "")] + texts) or any(CAUSE_RE.search(t) for t in texts)


def marked(m: dict) -> bool:
    """Whether stored mute `m` is throughput-class (marked when muted, else judged by its source and match)."""
    return bool(m.get("throughput")) or is_class(m.get("source"), m.get("match"))


def open_task(db: Any, why: str) -> int | None:
    """The first task `why` names (#<id>) that exists and has not finished; None when none does."""
    from .db import TERMINAL_TASK_STATES
    for tid in dict.fromkeys(int(x) for x in TASK_RE.findall(why or "")):
        row = db.one("SELECT status FROM tasks WHERE id=?", (tid,))
        if row and row["status"] not in TERMINAL_TASK_STATES:
            return tid
    return None


def vet(db: Any, source: Any, match: Any, why: Any, end_when: Any, cfg: dict | None = None,
        now: float | None = None) -> dict | None:
    """None when a mute of `source`/`match` is not throughput-class; else its owner and longest life,
    {"owner": {"task": id} | {"probe": cmd} | {"outside": why}, "max_h": h}. Raises ValueError, with the
    reason, for one with no owner, or a renewal whose `why` repeats the last one. Neither starts with
    'observation_mute rejected: ' (coordinator.SETTLED_REFUSALS): the next turn can fix both."""
    from .screen import MUTES_KEY, _same_mute
    now = time.time() if now is None else now
    source, match, why = str(source or "").strip(), str(match or "").strip(), str(why or "").strip()
    if not source or not match:
        return None   # screen.mute rejects these with its own reason
    old = next((m for m in db.kv(MUTES_KEY, []) or [] if _same_mute(m, source, match)
                and float(m["until"]) > now), None)
    if not is_class(source, match, _texts(db, source, match, old, now)):
        return None
    probe = (end_when.strip() if isinstance(end_when, str) else "") or (old or {}).get("end_when") or ""
    if (tid := open_task(db, why)) is not None:
        owner: dict = {"task": tid}
    elif probe:
        owner = {"probe": probe}
    elif OUTSIDE_RE.search(why):
        owner = {"outside": why[:200]}
    else:
        named = ", ".join(f"#{t}" for t in dict.fromkeys(TASK_RE.findall(why)))
        raise ValueError(
            f"observation_mute needs an owner: {source} {match!r} is throughput-class (work not moving, or a limit, "
            f"queue or wait as its cause), so its mute needs a `why` naming an open task (#<id>) that "
            f"owns the fix, an `end_when` probe, or a `why` saying only the user or an outside party can clear it"
            + (f"; {named} is not an open task" if named else "") + ". Else queue the fix instead of muting")
    if old and old.get("throughput") and " ".join(why.lower().split()) == " ".join(str(old.get("why") or "").lower().split()):
        raise ValueError(f"observation_mute needs fresh evidence: renewing the throughput-class mute of {source} {match!r} "
                         f"needs fresh evidence in `why` (its count, rate or owner's state now), not the same reason")
    return {"owner": owner, "max_h": max_h(cfg)}


def cap_note(m: dict, hours: Any) -> str:
    """The ack of a throughput-class mute whose `hours` were cut to its max, else ''."""
    t = m.get("throughput") or {}
    try:
        asked = float(hours) if hours not in (None, "") else None
    except (TypeError, ValueError):
        asked = None
    if not t or asked is None or asked <= float(t["max_h"]):
        return ""
    return (f"observation_mute {m['source']} {m['match']!r}: throughput-class, so it lasts {t['max_h']:g} h, not "
            f"{asked:g}; renew it then with fresh evidence if its owner still works on it")


def owner_text(m: dict) -> str:
    o = (m.get("throughput") or {}).get("owner") or {}
    if "task" in o:
        return f"task #{o['task']}"
    if "probe" in o:
        return f"probe `{str(o['probe'])[:80]}`"
    if "outside" in o:
        return "only the user or an outside party (per its why)"
    if m.get("end_when"):
        return f"probe `{str(m['end_when'])[:80]}`"
    return "none"


def tally(m: dict, now: float) -> None:
    """Count one muted observation in `m`'s first-seen time and, for a throughput-class mute, its hour."""
    m.setdefault("first_seen", now)
    if not marked(m):
        return
    hours = m.setdefault("hourly", {})
    hour = str(int(now // 3600 * 3600))
    hours[hour] = int(hours.get(hour, 0)) + 1
    for k in sorted(hours, key=int)[:-HOURS_KEPT]:
        del hours[k]


def rates(m: dict, now: float, n: int = RATE_SHOWN) -> list[int]:
    """Muted observations in each of the last `n` whole hours up to now, oldest first."""
    hours = m.get("hourly") or {}
    top = int(now // 3600 * 3600)
    return [int(hours.get(str(top - 3600 * i), 0)) for i in range(n - 1, -1, -1)]


def rate_text(m: dict, now: float) -> str:
    """'; 1, 2, 5/h over the last 3 h' for a throughput-class mute, else ''."""
    if not marked(m):
        return ""
    since = float(m.get("since") or now)
    n = max(1, min(RATE_SHOWN, int((now - since) // 3600) + 1))
    r = rates(m, now, n)
    return f"; {', '.join(map(str, r))}/h over the last {n} h" + (" (rising)" if n > 1 and r[-1] > r[0] else "")


def review_lines(db: Any, now: float | None = None, where: Any = None) -> list[str]:
    """Each active mute, for the daily review; a throughput-class one whose owner task is done or gone,
    or that has no owner, is flagged as a problem to fix today."""
    from .db import TERMINAL_TASK_STATES
    from .screen import _when, mutes
    now = time.time() if now is None else now
    out = []
    for m in mutes(db, now):
        cls = marked(m)
        o = (m.get("throughput") or {}).get("owner") or {}
        problem = ""
        if cls and "task" in o:
            row = db.one("SELECT status FROM tasks WHERE id=?", (int(o["task"]),))
            if row is None or row["status"] in TERMINAL_TASK_STATES:
                problem = (f"PROBLEM: its owner task #{o['task']} is {'gone' if row is None else row['status']}; "
                           f"fix the condition today or end the mute")
        elif cls and not o and not m.get("end_when"):
            problem = "PROBLEM: throughput-class with no owner task or probe; give it one today or end the mute"
        first = m.get("first_seen") or min((float(c["first_at"]) for c in (m.get("conds") or {}).values()
                                            if c.get("first_at") is not None), default=None)
        out.append(f"{m['source']} {str(m['match'])[:60]!r}{' (throughput)' if cls else ''}: {int(m['count'])} "
                   f"matched, first seen {_when(first, where)}, owner {owner_text(m)}, ends in "
                   f"{max(0.0, (float(m['until']) - now) / 3600):.1f} h" + (rate_text(m, now) if cls else "")
                   + (f"; {problem}" if problem else ""))
    return out

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A coordinator wake's real mode, and what the coordinator may say about its always-on session. Model-free.

The mode is read from the record `ttp stats --coord-session` reads (coordstats): each coordinator run's
turn.session (mode "resume", "new" or "stateless" with the daemon's why) or turn.session_off (the mode
off, or an agent that cannot resume), never from the coordinator.session.mode setting: a setting that
is on says nothing of whether this wake resumed (the spend guard, a failed resume or a pause make it
stateless). A run with neither record counts as stateless, not recorded.

line() is the one prompt line the daemon puts first in each coordinator wake's prompt: this wake's mode
with its fallback reason, and today's resumed / new / stateless counts in the home zone, this wake
included.

guard() checks a coordinator reply, notify or ask before it is posted. When the latest coordinator wake
(the one whose actions are being applied) is stateless, or more than half of today's session wakes fell
back to stateless, a sentence claiming the always-on or persistent coordinator is on, or that this turn
runs in its session (claims()), is kept but marked UNVERIFIED and the recorded mode line is appended.
Nothing else in the message changes and it is posted at once, so a message is never lost to the check.
Accepted gap: a sentence that only names the session ('the coordinator session spend guard is ...')
can be marked too; the mark adds words and drops none."""
from __future__ import annotations

import json
import re
import time

from . import coordstats, timefmt

UNVERIFIED = "(unverified)"
HEAD = "Coordinator wake mode (from ttp stats --coord-session's record, not the setting):"
MODES = ("resume", "new", "stateless")

# The always-on session named, then a present state word; or 'runs in its/the ... session'.
_SESSION = re.compile(r"(?i)\b(?:always[- ]on|persistent)\b(?:\s+(?:coordinator|session|chat|mode))?")
_STATE = re.compile(r"(?i)\b(?:is|are|now|runs?|running|on|active|live|enabled|works?|working|in place|met|"
                    r"resumes?|resumed|remembers?|keeps?)\b")
_IN_SESSION = re.compile(r"(?i)\b(?:runs?|running|ran|am|is|are|was|were|i['’]m|operat\w+)\s+(?:now\s+|still\s+)?"
                         r"(?:inside|in|within)\s+(?:it|its|my|the|this|our|one)\s+(?:own\s+)?"
                         r"(?:(?:always[- ]on|persistent|coordinator|resumed)\s+)*session\b")
# In the same clause: a negation, a plan or a condition, or an honest fallback word: no present claim.
_NOT = re.compile(r"(?i)(?:\b(?:not|no longer|never|stateless|off|disabled|fell back|falls? back|fallback|paused|"
                  r"will|once|until|after|should|would|could|when|if|unless|planned|expected)\b|n['’]t\b)")
# 'Always-on coordinator: on', where a clause split would part the name from its state.
_LABELLED = re.compile(r"(?i)\b(?:always[- ]on|persistent)\b[\w \t-]{0,30}:\s*(?:on|active|live|enabled|yes|running)\b")
_CLAUSE = re.compile(r"(?i)[,;:()\u2013\u2014]|\s-\s|\b(?:and|but|while|though|although|whereas|yet|unlike)\b")


def _turn(note) -> dict:
    try:
        got = json.loads(note or "{}") if isinstance(note, str) else (note or {})
    except ValueError:
        return {}
    t = got.get("turn") if isinstance(got, dict) else None
    return t if isinstance(t, dict) else {}


def of_note(note, status: str = "ok") -> tuple[str, str]:
    """(mode, reason) of one coordinator run's note: mode "resume", "new" or "stateless"; the reason is
    coordstats.reason's label for a session wake, the session_off why, or 'not recorded'."""
    t = _turn(note)
    s = t.get("session")
    if isinstance(s, dict) and s.get("mode") in MODES:
        return s["mode"], coordstats.reason(s, status)
    off = str(t.get("session_off") or "").strip()
    return "stateless", off or "not recorded"


def today_counts(db, tz: str, now: float | None = None) -> dict:
    """Today's coordinator wakes in the home zone `tz` by mode, as `ttp stats --coord-session` counts them
    (session wakes only), plus "off": wakes with the mode off or no record."""
    now = time.time() if now is None else now
    start, end = coordstats._day_bounds(timefmt.today(now, tz), tz)
    out = {"resume": 0, "new": 0, "stateless": 0, "off": 0}
    for r in db.q("SELECT note FROM runs WHERE role='coordinator' AND started>=? AND started<?", (start, end)):
        s = _turn(r["note"]).get("session")
        mode = s.get("mode") if isinstance(s, dict) else None
        out[mode if mode in MODES else "off"] += 1
    return out


def latest(db) -> tuple[str, str] | None:
    """(mode, reason) of the latest coordinator wake, or None when there is none."""
    r = db.one("SELECT note, status FROM runs WHERE role='coordinator' ORDER BY id DESC LIMIT 1")
    return of_note(r["note"], r["status"] or "ok") if r else None


def _label(mode: str, why: str) -> str:
    if mode == "resume":
        return "this wake resumed its session" + (f" ({why})" if why and why != "resumed" else "")
    if mode == "new":
        return f"this wake started a new session ({why})"
    return f"this wake is STATELESS ({why}): it does not run in the always-on session"


def _counts(c: dict) -> str:
    text = f"today {c['resume']} resumed, {c['new']} new, {c['stateless']} stateless"
    return text + (f", {c['off']} with the mode off" if c.get("off") else "")


def line(mode: str, why: str, counts: dict) -> str:
    """The prompt line: this wake's mode and reason, today's counts (this wake included in `counts`)."""
    tail = ". Say you run in the session only on a resumed or new wake." if mode == "stateless" else "."
    return f"{HEAD} {_label(mode, why)}; {_counts(counts)}{tail}"


def prompt_line(db, tz: str, mode: str, why: str, session: bool = True, now: float | None = None) -> str:
    """line() for a wake not yet in the runs table: today's recorded counts plus this one (`session`: a
    session wake, recorded in turn.session; else one with the mode off, recorded in turn.session_off)."""
    c = today_counts(db, tz, now)
    c[mode if session and mode in MODES else "off"] += 1
    if session:   # the label `ttp stats --coord-session` gives it ("spend guard", "new: no session")
        why = coordstats.reason({"mode": mode, "why": why}, "running")
    return line(mode if session else "stateless", why, c)


def claims(sentence: str) -> bool:
    """Whether a sentence says the always-on coordinator is on, or that this turn runs in its session."""
    s = sentence.strip()
    if not s or UNVERIFIED in s or s.startswith(HEAD):
        return False
    if _LABELLED.search(s):
        return True
    for c in _CLAUSE.split(s):   # each clause on its own: a negation elsewhere hides no claim
        if not c.strip() or _NOT.search(c):
            continue
        if _IN_SESSION.search(c) or _SESSION.search(c) and _STATE.search(c):
            return True
    return False


def doubtful(db, tz: str, now: float | None = None) -> tuple[str, str] | None:
    """The mode line when a session claim cannot stand (the latest wake is stateless, or more than half
    of today's session wakes were), else None."""
    got = latest(db)
    if got is None:
        return None
    c = today_counts(db, tz, now)
    wakes = c["resume"] + c["new"] + c["stateless"]
    if got[0] != "stateless" and not (wakes and c["stateless"] * 2 > wakes):
        return None
    return got[0], f"{HEAD} {_label(*got)}; {_counts(c)}."


def guard(db, tz: str, text: str, now: float | None = None) -> tuple[str, list[str]]:
    """(text to post, notes for the coordinator). Unchanged unless a sentence claims the always-on session
    while doubtful() holds: that sentence is marked UNVERIFIED and the recorded mode line appended."""
    if not text:
        return text, []
    parts = re.split(r"((?<=[.!?;])[ \t]+|\n)", text)
    hits = [k for k in range(0, len(parts), 2) if claims(parts[k])]
    if not hits:
        return text, []
    bad = doubtful(db, tz, now)
    if bad is None:
        return text, []
    for k in hits:
        body = parts[k].rstrip()
        parts[k] = body + " " + UNVERIFIED + parts[k][len(body):]
    out = "".join(parts).rstrip() + "\n" + bad[1]
    note = ("wakemode: a message said the coordinator runs in its always-on session while the recorded wake "
            f"mode does not show it ({bad[1][len(HEAD) + 1:][:200]}); it was sent marked {UNVERIFIED}. "
            "Read the wake mode line and `ttp stats --coord-session`, not coordinator.session.mode")
    return out, [note]

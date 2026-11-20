# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One wording for every user-facing message about a resource outage (a machine down or held, a device
held with no tenant, a heal check's outage): when it started (home zone, with its abbreviation), how long
it has lasted so far, who is acting now (the owner, the fallback and why the owner was passed over, or
"nobody: escalating"), the tracked task and its due time when there is one, what was tried, and the next
step. `facts` builds that part; machine pages (machine_ledger.route), the daemon's outage alerts, heal
checks' outage and hold alerts all append it to their own first sentence.

A message that only says "nothing was changed" or "a note was sent" leaves the reader guessing who has
it. `bare` names such a text (that phrasing and no actor). Only a message its caller marks as about an
outage is checked, never one guessed from its words ("held", "down", "owner" are common elsewhere): a
daemon alert whose key starts with an outage prefix (OUTAGE_KEYS) or that passes `outage=True` gets the
actor added (`ensure_actor`), and a coordinator notify with `outage: true` saying it is refused with
what to say.

A tracked task and due time are read from the page or condition when a caller recorded them (`task`,
`due`); absent, the line says there is none.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from . import timefmt

ACTING = "Acting now:"
NOBODY = "nobody: escalating"
TO_USER = f"{NOBODY} to the user"
# "nothing was changed", "nothing has changed", "a note was sent", "sent the fallback a note", ...
BARE_RE = re.compile(r"\bnothing\s+(?:was\s+|has\s+been\s+|got\s+)?changed\b|\bno\s+changes?\s+(?:was|were)\s+made\b"
                     r"|\bnotes?\s+(?:was|were|has\s+been|have\s+been)\s+(?:sent|filed|posted)\b"
                     r"|\bsent\s+(?:\w+\s+){0,3}(?:a\s+)?note\b", re.I)
# Alert keys of outage paths: an outage page, a machine condition's page, a heal check's outage or
# untenanted hold, the network outage.
OUTAGE_KEYS = ("outage:", "machine:", "heal:", "heal-hold:", "network:")
ACTOR_RE = re.compile(rf"{re.escape(ACTING)}|\bnobody:\s*escalating\b", re.I)


def duration(s: float) -> str:
    """'25 min', '4.0 h', '1.5 d'."""
    s = max(0.0, float(s))
    if s >= 86400:
        return f"{s / 86400:.1f} d"
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def actor(name: Any = None, role: str = "", passed: str = "") -> str:
    """Who is acting now: `name` with its role ('owner', 'fallback', 'coordinator') and, when it is
    not the first in line, who was passed over and why; no name (or the user, last in line): nobody."""
    if not name or str(name) == "the user":
        return TO_USER + (f" ({passed})" if passed else "")
    extra = "; ".join(x for x in (role, passed) if x)
    return f"{name}" + (f" ({extra})" if extra else "")


def facts(since: float, now: float, acting: str, where: Any = None, task: Any = None, due: Any = None,
          tried: Iterable[str] = (), next_step: str = "") -> str:
    """'Since <start> (<duration> so far). Acting now: <who>. Tracked task: #N, due <time>. Tried: ....
    Next: ....' Times in the home zone of `where` (a Project, settings or zone name; None: the ambient
    zone). `acting` empty is "nobody: escalating". `task`: a task id, or an outage task's
    '<label> in <project>' (machine_ledger.tracked); any other `task`, or a `due` that is not a number, is left out."""
    where = where if where is not None else timefmt.ambient()
    out = [f"Since {timefmt.long(float(since), where)} ({duration(now - float(since))} so far).",
           f"{ACTING} {acting or NOBODY}."]
    tid = _int(task)
    when = _num(due)
    from .outagetask import LABEL
    name = f"#{tid}" if tid is not None else task if isinstance(task, str) and task.startswith(LABEL) else None
    if name:
        out.append(f"Tracked task: {name}" + (f", due {timefmt.short(when, where, now)}" if when else "") + ".")
    else:
        out.append("Tracked task: none" + (f" (due {timefmt.short(when, where, now)})" if when else "") + ".")
    done = [str(t).strip().rstrip(".") for t in tried if str(t or "").strip()]
    out.append("Tried: " + ("; ".join(done) if done else "nothing yet") + ".")
    if next_step:
        out.append(f"Next: {next_step.strip().rstrip('.')}.")
    return " ".join(out)


def outage_key(key: Any) -> bool:
    """Whether alert key `key` is on an outage path (OUTAGE_KEYS)."""
    return str(key or "").startswith(OUTAGE_KEYS)


def bare(text: str) -> str:
    """Why an outage message is bare (it says only that nothing was changed or a note was sent, and
    names nobody acting), or "". The caller decides the text is about an outage."""
    text = str(text or "")
    if not BARE_RE.search(text) or ACTOR_RE.search(text):
        return ""
    return ("an outage message that says only that nothing was changed or a note was sent leaves the user "
            "guessing: say when it started (home zone) and how long it has lasted, who is acting now (\""
            f"{ACTING} <project> (owner|fallback; why the owner was passed over)\" or \"{ACTING} {NOBODY}\"), "
            "the tracked task and its due time if any, what was tried, and the next step")


def ensure_actor(text: str, acting: str = TO_USER) -> str:
    """`text`, with the actor added when it is a bare outage message (bare)."""
    return f"{text.rstrip()} {ACTING} {acting}." if bare(text) else text


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    s = str(v).strip().lstrip("#")
    return int(s) if s.isdigit() and int(s) > 0 else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Non-blocking asks: an ask the user leaves unanswered for `asks.auto_resolve_min` minutes (default
30; 0 never) resolves to its recommendation through the ask timeout (coordinator.expire_asks), and
the coordinator acts on it. Exempt asks (exempt()) never do: they wait for the user, but never hold
other work. The timer itself applies nothing; the coordinator's own gates still check what it does.
"""
from __future__ import annotations

import re

KEY = "asks.auto_resolve_min"
DEFAULTS_KEY = "ask_defaults"   # kv the timer reads (coordinator.ASK_DEFAULTS_KEY)
DEFAULT_MIN = 30
# Only the user may give these: a review or merge, another person's request answered in their name,
# a step that cannot be undone, a restriction loosened (only their verbatim words do).
EXEMPT_REASONS = ("review", "merge", "human", "irreversible", "restriction")
# These resolve only to a recommendation that stays inside the existing gates (declines or grants nothing).
GATED_REASONS = ("access", "funds", "spend")
# A recommendation that speaks or acts as the user, whatever the ask's reason.
_AS_USER_RE = re.compile(r"\b(merge|merging|ready for review|out of draft|leave draft|undraft|publish|post|"
                         r"e-?mail|on (my|your|the user'?s) behalf|in (my|your|the user'?s) name|as (me|you|the user))\b",
                         re.I)
# A gated recommendation that grants something: a yes, more money or quota, a credential, a login.
_GRANT_RE = re.compile(r"\b(yes|y|ok|okay|approve[ds]?|grant|raise|increase|lift|extend|top[- ]?up|buy|purchase|"
                       r"pay|spend|credentials?|token|password|secret|api[- ]?key|ssh[- ]?key|log ?in|sign ?in|"
                       r"access|cap|budget|reserve)\b", re.I)
# A recommendation that declines: it keeps things as they are, inside the gates.
_DECLINE_RE = re.compile(r"^\W*(no|don'?t|do not|skip|keep|wait|decline|leave|hold|stay|not now|drop)\b", re.I)


def minutes(cfg: dict | None) -> float:
    """The configured auto-resolve delay in minutes; 0 when off or unreadable."""
    try:
        return max(0.0, float(((cfg or {}).get("asks") or {}).get("auto_resolve_min", DEFAULT_MIN) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def setting(v) -> float:
    """config_set's check of asks.auto_resolve_min: minutes, 0 or more (0 turns it off)."""
    try:
        m = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{KEY} takes minutes (0 turns it off), got {v!r}") from None
    if not m >= 0 or m == float("inf"):
        raise ValueError(f"{KEY} takes minutes (0 turns it off), got {v!r}")
    return m


def exempt(blocking: str | None, rec: str | None) -> str | None:
    """Why an ask with this blocking reason and recommendation never auto-resolves, or None."""
    rec = (rec or "").strip()
    if not rec:
        return "it has no recommendation"
    if blocking in EXEMPT_REASONS:
        return f"a {blocking} ask waits for the user"
    if _AS_USER_RE.search(rec):
        return "its recommendation speaks or acts as the user"
    if blocking not in GATED_REASONS:
        return f"unknown blocking reason {blocking!r}"
    if not _DECLINE_RE.match(rec) and _GRANT_RE.search(rec):
        return f"its recommendation grants {blocking} only the user holds"
    return None


def note(rec: str, at_text: str) -> str:
    """The line shown on an ask: 'acts on <recommendation> at HH:MM <zone> unless answered'."""
    rec = " ".join(rec.split())
    return f"acts on {rec if len(rec) <= 120 else rec[:119] + '…'} at {at_text} unless answered"


def due(ask_ts: float, cfg: dict | None) -> float | None:
    """When an auto-resolving ask sent at `ask_ts` resolves under `cfg`; None when turned off."""
    m = minutes(cfg)
    return ask_ts + m * 60 if m > 0 else None


def shown(db, cfg: dict | None, ask: dict, where, now: float | None = None) -> str | None:
    """The deadline line for an open ask in ttp status and the web app, or None when it waits for the user."""
    from . import timefmt
    v = (db.kv(DEFAULTS_KEY, {}) or {}).get(str(ask["id"]))
    at = due(float(ask["ts"]), cfg) if isinstance(v, dict) else None
    return note(str(v.get("rec") or ""), timefmt.short(at, where, now)) if at else None

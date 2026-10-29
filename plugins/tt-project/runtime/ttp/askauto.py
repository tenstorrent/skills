# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Non-blocking asks. Every ask stores its default when it is filed: the coordinator's
recommendation. Left unanswered for `coordinator.ask_timeout_h` (default 0.5; 0 never), the daemon
resolves it to that default (coordinator.expire_asks) and the coordinator acts on it. outcome() says
what the timer does with an ask: apply its recommendation, keep a restriction as written (only the
user's own words loosen one), or leave it open for the user (an action that could affect people
badly or speaks as the user, a credential or money only the user holds). An open one never holds
unrelated work. The timer itself performs nothing; the coordinator's gates still check what it does.
"""
from __future__ import annotations

import re

DEFAULTS_KEY = "ask_defaults"   # kv the timer reads (coordinator.ASK_DEFAULTS_KEY)
FLAGS_KEY = "ask_timer_flags"   # kv: the problems check() last reported, so each is raised once
DEFAULT_H = 0.5
STAY = "keep the restriction as written (only the user's own words loosen it)"
# Only the user may give these: a review or merge, an answer to another person's request.
AS_USER_REASONS = ("review", "merge", "human")
# These resolve only to a recommendation that grants nothing only the user holds.
GATED_REASONS = ("access", "funds", "spend")
# A recommendation that speaks or acts as the user, whatever the ask's reason.
_AS_USER_RE = re.compile(r"\b(merge|merging|ready for review|out of draft|leave draft|undraft|publish|post|"
                         r"e-?mail|on (my|your|the user'?s) behalf|in (my|your|the user'?s) name|as (me|you|the user))\b",
                         re.I)
# A gated recommendation that grants something: a yes, more money or quota, a credential, a login.
_GRANT_RE = re.compile(r"\b(yes|y|ok|okay|approve[ds]?|grant|raise|increase|lift|extend|top[- ]?up|buy|purchase|"
                       r"pay|spend|credentials?|token|password|secret|api[- ]?key|ssh[- ]?key|log ?in|sign ?in|"
                       r"access|cap|budget|reserve)\b", re.I)
# A recommendation that declines: it starts with a plain no and adds no later step ("no, then post
# it", "wait a day, then raise the cap" and "keep merging" do not decline), so it keeps things as they are.
_DECLINE_RE = re.compile(r"^\W*(no|don'?t|do not|skip|decline|not now|never)\b", re.I)
_LATER_RE = re.compile(r"\b(then|but|after(wards?)?|later|until|unless|instead|except)\b", re.I)


def hours(cfg: dict | None) -> float:
    """coordinator.ask_timeout_h: hours before an unanswered ask resolves to its default; 0 when off."""
    try:
        return max(0.0, float(((cfg or {}).get("coordinator") or {}).get("ask_timeout_h", DEFAULT_H) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def outcome(blocking: str | None, rec: str | None) -> tuple[str, str]:
    """What the timer does with an ask: ("apply", rec), ("stay", STAY) or ("open", why it waits)."""
    rec = " ".join((rec or "").split())
    declines = bool(_DECLINE_RE.match(rec)) and not _LATER_RE.search(rec)
    if not rec:
        return "open", "it has no recommendation"
    if blocking == "restriction":
        return ("apply", rec) if declines else ("stay", STAY)
    if blocking in AS_USER_REASONS:
        return "open", f"only the user gives a {blocking} answer"
    if _AS_USER_RE.search(rec) and not declines:
        return "open", "its recommendation speaks or acts as the user"
    if blocking == "irreversible":
        return ("apply", rec) if declines else ("open", "its recommendation cannot be undone")
    if blocking in GATED_REASONS and not declines and _GRANT_RE.search(rec):
        return "open", f"its recommendation needs {blocking} only the user holds"
    return "apply", rec


def entry(blocking: str | None, rec: str | None) -> dict:
    """The default stored for an ask when it is filed."""
    return {"rec": " ".join((rec or "").split()), "blocking": blocking or "", "auto": 1}


def note(v: dict, at_text: str) -> str:
    """The line shown on an ask: 'acts on <recommendation> at HH:MM <zone> unless answered'."""
    kind, what = outcome(v.get("blocking"), v.get("rec"))
    if kind == "open":
        return f"waits for the user ({what}); other work goes on"
    return f"acts on {what if len(what) <= 120 else what[:119] + '…'} at {at_text} unless answered"


def due(ask_ts: float, cfg: dict | None) -> float | None:
    """When an ask sent at `ask_ts` times out under `cfg`; None when the timer is off."""
    h = hours(cfg)
    return ask_ts + h * 3600 if h > 0 else None


def shown(db, cfg: dict | None, ask: dict, where, now: float | None = None) -> str | None:
    """The deadline line for an open ask in ttp status and the web app; None without a default."""
    from . import timefmt
    v = (db.kv(DEFAULTS_KEY, {}) or {}).get(str(ask["id"]))
    at = due(float(ask["ts"]), cfg) if isinstance(v, dict) else None
    return note(v, timefmt.short(at, where, now)) if at else None


def check(db, cfg: dict | None, now: float, grace_s: float = 900) -> list[str]:
    """Model-free check of the ask timer: each open ask with no stored default, and each that should
    have resolved over `grace_s` ago (outcome apply or stay) but is still open."""
    defaults = db.kv(DEFAULTS_KEY, {}) or {}
    out = []
    for a in db.q("SELECT id, ts FROM messages WHERE direction='out' AND kind='ask' AND handled=0 ORDER BY id"):
        if str(a["id"]) not in defaults:   # None: the user wrote after it, so it waits on purpose
            out.append(f"ask {a['id']} has no default")
            continue
        v = defaults[str(a["id"])]
        if isinstance(v, dict) and outcome(v.get("blocking"), v.get("rec"))[0] != "open" \
                and (at := due(float(a["ts"]), cfg)) and at + grace_s <= now:
            out.append(f"ask {a['id']} is past its timeout and still open")
    return out

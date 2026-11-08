# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model ranking: when the chat model a worker run would use is down, run the smartest one still served.

`providers.chat.model_ranking` lists the chat endpoint's model ids, smartest first (a list, or "a,b").
Unset or empty, the shipped default, nothing changes. Set, a model is down (down()) while a run was
refused it (route.py's model_missing hold for a tier, or a ranked model's own refusal here, kept for
route.RETRY_QUIET_S) or while the last good model-free /models check (route.MODELS_FILE) leaves it out; a check over STALE_S
old counts as none (route.models_due keeps it fresh while a ranking is set and chat may run).

A routed start (a tier route or a model pick to chat, route.py) whose model is down runs the
highest-ranked model that check lists and that is not down itself (pick); with none, it falls back
as before (model_missing: the core provider). A cool-down (the whole endpoint faulted) swaps nothing.
A task's own model pin on chat that is down is swapped the same way, unless the pin is required
(failover.required: only the user's own words mark it): such a task waits, held, until its model is
served again, and is never swapped. Each switch (per tier, routes rule or pinned model) is one low alert;
once the model is served again the next start runs it, and the switch record ends (back).

Only the chat provider has a served list; Claude Code, Codex and Cursor models are not ranked here
(provider failover moves work off a provider that cannot start, failover.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import route

TO = route.TO                         # the one provider with a served list
KEY = "model_rank"                    # the route entry's or run note's {"from", "why"} of a swapped run
SWITCH = "model_rank_switch:"         # kv per tier, "<tier>:rule<i>" or "pin:<model>": {"from", "to", "since"} while swapped
REFUSED = "model_rank_refused:"       # kv per ranked model a swapped run was refused: {"at", "run"}


def ranking_of(v: Any) -> list[str]:
    """providers.chat.model_ranking as a list of model ids, smartest first; ValueError when malformed."""
    if v is None or v == "":
        return []
    names = v.split(",") if isinstance(v, str) else v if isinstance(v, (list, tuple)) else None
    if names is None or not all(isinstance(n, str) for n in names):
        raise ValueError(f"providers.{route.TO}.model_ranking needs a list of model ids, smartest first, not {v!r}")
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


def ranking(cfg: dict) -> list[str]:
    """The project's ranking, or [] (unset or malformed: config_problems names it)."""
    try:
        return ranking_of(((cfg.get("providers") or {}).get(route.TO) or {}).get("model_ranking"))
    except ValueError:
        return []


STALE_S = 2 * route.MODELS_EVERY_S   # an older served list is unknown: no swap or hold rests on it


def served(state: Path, now: float) -> list[str] | None:
    """The ids the last /models check listed, or None when it failed, never ran or is over STALE_S old
    (nothing is known)."""
    rec = route.read_served(state).get(route.TO) or {}
    if not rec.get("ok") or now - float(rec.get("checked") or 0) > STALE_S:
        return None
    return list(rec.get("ids") or [])


def down(db, cfg: dict, state: Path, model: str, now: float) -> str:
    """Why `model` is down ('refused' or 'not listed'), else ''; always '' with no ranking."""
    if not ranking(cfg) or not model:
        return ""
    if any(h.get("model") == model and not h.get("retry") for h in route.holds(db).values()):
        return "refused"
    if now - float((db.kv(REFUSED + model) or {}).get("at") or 0) < route.RETRY_QUIET_S:
        return "refused"
    ids = served(state, now)
    return "not listed" if ids is not None and model not in ids else ""


def pick(db, cfg: dict, state: Path, model: str, now: float) -> tuple[str, str] | None:
    """(the model to run in place of `model`, why `model` is down), or None: it is up, no ranking is
    set, or no ranked model is served now."""
    why = down(db, cfg, state, model, now)
    ids = served(state, now) if why else None
    for m in ranking(cfg) if ids else ():
        if m != model and m in ids and not down(db, cfg, state, m, now):
            return m, why
    return None


def refused(db, model: str, run_id: int | None, now: float) -> None:
    """A swapped run was refused its ranked `model`: skip it for route.RETRY_QUIET_S."""
    db.set_kv(REFUSED + model, {"at": now, "run": run_id})


def switch(db, key: str, frm: str, to: str, now: float) -> bool:
    """Record `key`'s switch from `frm` to `to`; True when it is new (one low alert)."""
    last = db.kv(SWITCH + key) or {}
    if last.get("from") == frm and last.get("to") == to:
        return False
    db.set_kv(SWITCH + key, {"from": frm, "to": to, "since": now})
    return True


def back(db, key: str) -> dict | None:
    """End `key`'s switch record once its own model runs again: the record, or None when none was kept."""
    last = db.kv(SWITCH + key)
    if last:
        db.x("DELETE FROM kv WHERE key=?", (SWITCH + key,))
    return last or None


def text(where: str, frm: str, to: str, why: str) -> str:
    """The switch's low alert."""
    return (f"Model {frm} is down on the {route.TO} provider ({why}); {where} now runs {to}, the highest-ranked "
            f"model it serves (providers.{route.TO}.model_ranking). It goes back to {frm} once that is served again.")

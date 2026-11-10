# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model ranking: when the chat model a worker run would use is down, run the smartest one still served.

`providers.chat.model_ranking` lists the chat endpoint's model ids, smartest first (a list, or "a,b"): the
project's own, else the account's, which rankfresh.py keeps current from the Artificial Analysis
Intelligence Index wherever a chat endpoint is set. Unset or empty in both, nothing changes. Set, a model is down (down()) while a run was
refused it (route.py's model_missing hold for a tier, or a ranked model's own refusal here, kept for
route.RETRY_QUIET_S) or while the last good model-free /models check (the host's shared poll, served.py, else route.MODELS_FILE) leaves
it out; a check over 3 poll intervals old counts as none (served.py polls every 30 s while chat may run).

A routed start (a tier route or a model pick to chat, route.py) whose model is down runs the
highest-ranked model that check lists and that is not down itself (pick); with none, it falls back
as before (model_missing: the core provider). A cool-down (the whole endpoint faulted) swaps nothing.
A task's own model pin on chat that is down is swapped the same way, unless the pin is required
(failover.required: only the user's own words mark it): such a task waits, held, until its model is
served again, and is never swapped. Each switch (per tier, routes rule, picked model or pinned model) is one low alert;
once the model is served again the next start runs it, and the switch record ends (back).

A routed start (tier, routes rule or run class; never a task's own pin) whose model is up and ranked
runs a served model ranked above it instead (better), so a newly served smarter model is tried within
one /models check (route.models_every). Until it has TRIES runs that ended without a fallback it is
tried on light-tier starts only (escalated runs never route); then it runs at every tier by default.
A run on it that fails on the model itself (FAULTS_PARK: malformed, timed out or no hand-off) during
those runs parks it for PARK_S, a week, as do such faults in over BAD_SHARE of its runs later: no
route picks it, and once the week is over it is tried again from scratch (the weekly recheck). An endpoint fault (down, rate limit, auth) parks nothing.
The host's daily bench (modelbench.py) rechecks parked models weekly: a pass frees one at once, a weak
score keeps it parked.

A run in flight swaps too (chatbridge.py): its launch names the ranking's other unparked models
(standins, best first; none for a required pin), and its bridge moves the call and the rest of the run
to the first of them still served once the endpoint refuses its model, or errs SWAP_ERRORS times in a
row while the served list leaves it out. The daemon keeps each swap in the run note (KEY "mid_run":
[{from, to, at, why}]) with one low alert, and a refused model is down (refused()) for new starts.
Babysit decisions (babysit.py) ask the next ranked served model once before they fall back.

Only the chat provider has a served list; Claude Code, Codex and Cursor models are not ranked here
(provider failover moves work off a provider that cannot start, failover.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import route
from . import served as watch

TO = route.TO                         # the one provider with a served list
KEY = "model_rank"                    # the route entry's or run note's {"from", "why"} of a swapped run
SWITCH = "model_rank_switch:"         # kv per tier, "<tier>:rule<i>", "<tier>:pick:<model>" or "pin:<model>": {"from", "to", "since"} while swapped
REFUSED = "model_rank_refused:"       # kv per ranked model a swapped run was refused: {"at", "run"}
TRIED = "model_rank_tried:"           # kv per better() model: {"ok", "bad": its runs' ends, "since", "run"}
PARKED = "model_rank_parked:"         # kv per parked model: {"at", "run", "why"}
BETTER = "better"                     # a KEY "why": the run took a model ranked above its own
TRIES = 3                             # runs without a fallback before a better model runs past light starts
PARK_S = 7 * 86400                    # a parked model is tried again a week on
MIN_RUNS, BAD_SHARE = 8, 0.2         # a proven model's own faults over this share of at least these runs park it
FAULTS_PARK = ("malformed", "timeout", "failed", "no_handoff")   # a run's fallback that is the model's own fault


def ranking_of(v: Any) -> list[str]:
    """providers.chat.model_ranking as a list of model ids, smartest first; ValueError when malformed."""
    if v is None or v == "":
        return []
    names = v.split(",") if isinstance(v, str) else v if isinstance(v, (list, tuple)) else None
    if names is None or not all(isinstance(n, str) for n in names):
        raise ValueError(f"providers.{route.TO}.model_ranking needs a list of model ids, smartest first, not {v!r}")
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


def ranking(cfg: dict) -> list[str]:
    """The project's own ranking, else the account's (`ttp config --account`, kept current by rankfresh.py),
    or [] (unset or malformed: config_problems names it)."""
    v = ((cfg.get("providers") or {}).get(route.TO) or {}).get("model_ranking")
    if v is None:
        from .project import account_value
        v = account_value(f"providers.{route.TO}.model_ranking")
    try:
        return ranking_of(v)
    except ValueError:
        return []


STALE_S = watch.STALE_POLLS * watch.EVERY_S   # a list with no poll interval recorded this old is unknown: no swap or hold rests on it


def served(state: Path, now: float) -> list[str] | None:
    """The ids the last /models check listed (the host's shared poll first: served.py), or None when it
    failed, never ran or is over served.stale_s old (3 poll intervals: nothing is known)."""
    rec = route.read_served(state).get(route.TO) or {}
    if not rec.get("ok") or now - float(rec.get("checked") or 0) > watch.stale_s(rec):
        return None
    return list(rec.get("ids") or [])


def down(db, cfg: dict, state: Path, model: str, now: float, cls: str = "") -> str:
    """Why `model` is down ('refused', 'not listed' or, for a class `cls`, 'demoted for <cls>'
    (modeldemote.py)), else ''; always '' with no ranking."""
    if not ranking(cfg) or not model:
        return ""
    if cls:
        from . import modeldemote
        if modeldemote.demoted(db, model, cls, now):
            return f"demoted for {cls}"
    if any(h.get("model") == model and not h.get("retry") for h in route.holds(db).values()):
        return "refused"
    if now - float((db.kv(REFUSED + model) or {}).get("at") or 0) < route.RETRY_QUIET_S:
        return "refused"
    ids = served(state, now)
    return "not listed" if ids is not None and model not in ids else ""


def pick(db, cfg: dict, state: Path, model: str, now: float, cls: str = "") -> tuple[str, str] | None:
    """(the model to run in place of `model`, why `model` is down), or None: it is up, no ranking is
    set, or no ranked model is served now. A `cls` also counts demotions for that class."""
    why = down(db, cfg, state, model, now, cls)
    ids = served(state, now) if why else None
    for m in ranking(cfg) if ids else ():
        if m != model and m in ids and not down(db, cfg, state, m, now, cls) and not parked(db, m, now):
            return m, why
    return None


def standins(db, cfg: dict, model: str, now: float) -> list[str]:
    """The ranked models a run on `model` may swap to mid-run (chatbridge.py), best first: the ranking
    less `model` and parked ones. The bridge checks each against the served list when it swaps."""
    return [m for m in ranking(cfg) if m != model and not parked(db, m, now)]


def mid_text(run_id, frm: str, to: str, why: str) -> str:
    """A mid-run swap's low alert."""
    return (f"Model {frm} went away on the {route.TO} provider during run {run_id} ({why}); the run went on "
            f"with {to}, the highest-ranked model it serves (providers.{route.TO}.model_ranking), in the same session.")


def parked(db, model: str, now: float) -> bool:
    """`model` failed a run on its own within PARK_S (tried()) and no later host bench passed it, or its
    last host bench (modelbench.py, within two weeks) found it weak: no route picks it."""
    from . import modelbench
    at = float((db.kv(PARKED + model) or {}).get("at") or 0)
    v = modelbench.verdict(model, at, now)
    return v == "weak" or now - at < PARK_S and v != "passed"


def parked_models(db) -> dict[str, float]:
    """{model: when this project last parked it} (the bench's weekly recheck arms)."""
    return {r["key"][len(PARKED):]: float((db.kv(r["key"]) or {}).get("at") or 0)
            for r in db.q("SELECT key FROM kv WHERE key LIKE ?", (PARKED + "%",))}


def better(db, cfg: dict, state: Path, model: str, now: float, light: bool, cls: str = "") -> str:
    """The highest-ranked served model above `model` (up and ranked) that is neither down nor parked
    (nor demoted for `cls`), and on a non-`light` start has TRIES good runs; else ''."""
    rank = ranking(cfg)
    if model not in rank or down(db, cfg, state, model, now, cls) or (ids := served(state, now)) is None:
        return ""
    for m in rank[:rank.index(model)]:
        if m in ids and not down(db, cfg, state, m, now, cls) and not parked(db, m, now) and (
                light or int((db.kv(TRIED + m) or {}).get("ok") or 0) >= TRIES):
            return m
    return ""


def tried(db, model: str, fell: str, run_id: int | None, now: float) -> str:
    """Book a run's end on a better() model: 'parked' when it fell back on its own fault (FAULTS_PARK)
    within its first TRIES good runs, or such faults pass BAD_SHARE of its runs from MIN_RUNS on (its
    count then starts again from none); 'proven' at its TRIES-th good run; else ''."""
    if fell and fell not in FAULTS_PARK:   # an endpoint fault or a refusal (refused()): no verdict
        return ""
    rec = db.kv(TRIED + model) or {}
    ok, bad = int(rec.get("ok") or 0) + (not fell), int(rec.get("bad") or 0) + bool(fell)
    if bad and (ok < TRIES or ok + bad >= MIN_RUNS and bad > BAD_SHARE * (ok + bad)):
        db.set_kv(PARKED + model, {"at": now, "run": run_id, "why": fell, "ok": ok, "bad": bad})
        db.x("DELETE FROM kv WHERE key=?", (TRIED + model,))
        return "parked"
    db.set_kv(TRIED + model, {"ok": ok, "bad": bad, "since": rec.get("since") or now, "run": run_id})
    return "proven" if ok == TRIES and not fell else ""


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

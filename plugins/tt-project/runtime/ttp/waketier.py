# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Why a waiting task woke, and the tier its run takes, decided before any model turn starts.

Every wake of a waiting task records its reason (KEY in the hand-off; KINDS): what changed. The
dispatched run's note keeps it with the decision (`wake`: reason, tier, by), so `ttp stats --wakes`
counts task wakes and their $ per day by reason.

The wait-wake gate (waitgate.py) decides whether a wake runs a model at all. This module decides the
tier of an escalated wake that does run (the hand-off asked for the task's own tier):

- FIX reasons (a failed check, job, push or on_pass_cmd, a broken probe, a light wake that asked for
  more): the work is known, so the run keeps the tier its wake set. Rule, no call.
- JUDGE reasons (a spec change that slept with the wait): it may only add context. Jev, where its
  `wake_tier` use is allowed (jevuse) and it is reachable, says whether the run needs judgment; when
  it is confident it does not (score below `waiting.wake_tier.jev_light_below`), the run wakes at
  light and as a plain wake, so it may still ask for its own tier (retry_after_s 0 and a higher
  wake_tier) once it finds real work. Jev off, unreachable or unsure: the wake's tier, as before.
- Anything else: the wake's tier.

A light pick books as avoided the measured mean cost of a wake at its from-tier less a light wake
(tier_cost; LIGHT_SHARE of jevuse's fallback price per tier step while either is unmeasured), so
jevuse.review keeps the use on while it pays. Each Jev call is settled when its run ends: a light pick
whose run asked for more is wrong, and its avoided cost becomes minus the light run it added.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import budget as bud
from . import jevuse
from .db import DB

KEY = "wake_reason"
USE = "wake_tier"
PICK = "wake_pick"            # the hand-off's record of a lowered tier, copied into the run note
FIX = frozenset({"checks_failed", "tests_failed", "job_failed", "match_fail", "push_unlanded",
                 "on_pass_failed", "probe_broken", "asked", "underuse"})
JUDGE = frozenset({"spec_change"})
KINDS = FIX | JUDGE | {"timer", "probe_passed", "probe_changed", "wake_now", "checks_gone",
                       "stale_release", "reboot", "other"}
DEFAULT_LIGHT_BELOW = 0.3
LIGHT_SHARE = 0.25            # a light wake's price as a share of a standard one while unmeasured
JEV_TIMEOUT_S = 10.0


def reason(prev: dict) -> str:
    """The recorded reason of a due wake (hand-off `prev`): its KEY, else 'timer' when nothing woke
    it but its clock, else 'other'."""
    got = prev.get(KEY) if isinstance(prev, dict) else None
    if got in KINDS:
        return got
    return "other" if isinstance(prev, dict) and prev.get("woke") else "timer"


def _light_below(cfg: dict) -> float:
    try:
        return float(((cfg.get("waiting") or {}).get("wake_tier") or {}).get("jev_light_below",
                                                                          DEFAULT_LIGHT_BELOW))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_LIGHT_BELOW


def tier_cost(db: DB, cfg: dict, tier: str, now: float | None = None) -> float | None:
    """The mean cost of a finished task wake at `tier` over jevuse's window (its run note's wake), or
    None while none was measured."""
    import json as _json
    now = time.time() if now is None else now
    got = []
    for r in db.q("SELECT cost_usd, note FROM runs WHERE role!='coordinator' AND status!='running' AND cost_usd>0 "
                  "AND started>=? AND note LIKE '%\"wake\"%' ORDER BY id DESC LIMIT 500", (now - jevuse.window_s(cfg),)):
        try:
            if (_json.loads(r["note"] or "{}").get("wake") or {}).get("tier") == tier:
                got.append(float(r["cost_usd"]))
        except (ValueError, TypeError, AttributeError):
            continue
    return sum(got) / len(got) if got else None


def _fallback(tier: str) -> float:
    """An unmeasured wake's price: jevuse's fallback for standard, LIGHT_SHARE of it per tier below,
    and as much again per tier above."""
    step = bud.TIER_ORDER.index(tier) - bud.TIER_ORDER.index("standard") if tier in bud.TIER_ORDER else 0
    return jevuse.WAKE_COST_FALLBACK_USD * (LIGHT_SHARE ** -step if step < 0 else 1 + step)


def avoided(db: DB, cfg: dict, tier: str, now: float | None = None) -> float:
    """What a light wake saves against one at `tier`: their measured mean costs, else the fallbacks."""
    hi, lo = tier_cost(db, cfg, tier, now), tier_cost(db, cfg, bud.TIER_ORDER[0], now)
    return max(0.0, (hi if hi is not None else _fallback(tier)) - (lo if lo is not None else _fallback(bud.TIER_ORDER[0])))


def pick(db: DB, cfg: dict, task: dict, prev: dict, jev=None, now: float | None = None) -> dict | None:
    """For an escalated wake of a JUDGE reason above light: {"tier": "light", "by": "jev", "call",
    "from"} when Jev is confident the run needs no judgment, else None (the wake keeps its tier)."""
    if not isinstance(prev, dict) or not prev.get("escalated_wake") or reason(prev) not in JUDGE:
        return None
    tier = bud.wake_tier(task.get("tier") or "standard", prev, cfg)
    if tier is None or tier == bud.TIER_ORDER[0]:
        return None
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, USE):
        return None
    now = time.time() if now is None else now
    state = "\n".join([
        f"task: #{task['id']} {task.get('title') or ''}",
        f"why it woke: {str(prev.get('woke') or '')[:600]}",
        f"its spec now (the change is usually at the end): {str(task.get('spec') or '')[-2500:]}",
        f"its hand-off: {str(prev.get('summary') or '')[:800]}",
        f"what it waited for: {str(prev.get('waiting_for') or '')[:400]}",
        f"next step it named: {str(prev.get('next_step') or '')[:300]}",
    ])
    try:
        ans = jev.decide(state=state, questions={"judgment": {
            "type": "noul",
            "instructions": "A waiting task woke because its spec changed. Does its next run need real "
                            "judgment (new work, a changed plan, a fix), rather than only reading the change "
                            "and checking or reporting what it waited on?",
            "criteria": {"true": "The change asks for new or different work, or the wait's outcome needs a decision.",
                         "false": "The change only adds context or notes; a check or report is all that is left."}},
        }, purpose=USE, timeout=JEV_TIMEOUT_S)
        score = float((ans or {}).get("judgment", {}).get("noul", 1.0))
    except Exception:   # the tier stays: an error never lowers a run
        return None
    light = score < _light_below(cfg)
    call = jevuse.record(db, USE, {"task": task["id"], "judgment": round(score, 2), "light": light},
                         getattr(jev, "last_cost", 0.0), avoided(db, cfg, tier, now) if light else 0.0,
                         ref=f"task:{task['id']}", changed=light,
                         settle_s=None, now=now)
    if not light:
        return None
    return {"tier": bud.TIER_ORDER[0], "by": "jev", "call": call, "from": tier, "judgment": round(score, 2)}


def lowered(prev: dict, got: dict) -> dict:
    """The hand-off after a light pick: a plain light wake (not escalated, so its run may still ask for more)."""
    out = {k: v for k, v in prev.items() if k != "escalated_wake"}
    out.update({"wake_tier": got["tier"], PICK: got})
    return out


def settle(db: DB, note: dict, result: dict, now: float | None = None) -> None:
    """Resolve the Jev call behind a run's light pick: wrong when the run asked for more (a waiting
    hand-off with retry_after_s 0 and a higher wake_tier), else right."""
    got = ((note or {}).get("wake") or {}).get("pick") if isinstance(note, dict) else None
    if not isinstance(got, dict) or not got.get("call"):
        return
    more = (isinstance(result, dict) and result.get("status") == "waiting"
            and str(result.get("retry_after_s")) in ("0", "0.0") and result.get("wake_tier") in bud.TIER_ORDER[1:])
    if jevuse.resolve(db, int(got["call"]), not more, "its light run asked for more" if more else "", now) and more:
        # the light run it added is extra cost: a negative saving still counts against the use
        lo = tier_cost(db, {}, bud.TIER_ORDER[0], now)
        db.x("UPDATE jev_calls SET avoided_usd=? WHERE id=?",
             (-(lo if lo is not None else _fallback(bud.TIER_ORDER[0])), int(got["call"])))


def stats(db: DB, tz: str, days: float = 7, now: float | None = None) -> dict:
    """Task wakes per home-zone day by reason: runs, $ and how many only handed the wait back."""
    import json as _json
    from . import timefmt
    now = time.time() if now is None else now
    out: dict = {}
    for r in db.q("SELECT started, cost_usd, note, dir FROM runs WHERE role!='coordinator' AND started>=? "
                  "AND note LIKE '%\"wake\"%'", (now - days * 86400,)):
        try:
            note = _json.loads(r["note"] or "{}")
        except ValueError:
            continue
        wake = note.get("wake") if isinstance(note, dict) else None
        if not isinstance(wake, dict):
            continue
        day = timefmt.today(r["started"], tz)
        row = out.setdefault(day, {}).setdefault(str(wake.get("reason") or "unrecorded"),
                                                 {"wakes": 0, "usd": 0.0, "rewaited": 0, "by": {}})
        row["wakes"] += 1
        row["usd"] = round(row["usd"] + float(r["cost_usd"] or 0), 4)
        by = f"{wake.get('by') or 'rule'}/{wake.get('tier') or '?'}"
        row["by"][by] = row["by"].get(by, 0) + 1
        try:   # its hand-off, in its run dir
            if _json.loads(Path(r["dir"], "result.json").read_text()).get("status") == "waiting":
                row["rewaited"] += 1
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    return {"days": days, "per_day": dict(sorted(out.items()))}


def text(s: dict) -> str:
    lines = [f"Task wakes by reason, last {s['days']:g} day(s) (home zone):"]
    for day, reasons in s["per_day"].items():
        total = sum(v["usd"] for v in reasons.values())
        lines.append(f"{day}: {sum(v['wakes'] for v in reasons.values())} wakes, ${total:.2f}")
        for why, v in sorted(reasons.items(), key=lambda kv: -kv[1]["usd"]):
            by = ", ".join(f"{k} {n}" for k, n in sorted(v["by"].items()))
            lines.append(f"  {why}: {v['wakes']} wakes, ${v['usd']:.2f}, {v['rewaited']} handed the wait back ({by})")
    if not s["per_day"]:
        lines.append("  no task wakes recorded")
    return "\n".join(lines)

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev's pick of a code task's effort level.

With `effort_ab.jev_pick` on (off by default; chat-settable), a standard-tier code task with no effort
pin of its own has Jev score its spec once (its lineage keeps the pick): medium or high, else the
tier's effort when Jev is off or gives no answer. The pick changes only a standard-tier run whose tier
effort is high. It goes in the run note (`effort_ab`: `by`, `pick`, `root`, `effort` the pick gave it,
`effective` the effort it started at) and in jev_calls (use `effort`), which effort.settle resolves once
the task ends. A conclusion run's tier floor (tierfloor.py) may lift `effective` above `effort`
(`lifted`): a lifted pick is settled as no change.
"""
from __future__ import annotations

import time

from . import chaincap, jevuse
from .db import DB, assigned_tier, load_result

KEY = "effort_ab"            # project.json section, and the run note's entry
CONTROL, TEST = "high", "medium"
ARMS = (CONTROL, TEST)
JEV_CRITERIA = ["a routine change that medium reasoning effort handles well",
                "a tricky change that needs high reasoning effort"]


def _on(cfg: dict, key: str) -> bool:
    v = (cfg.get(KEY) or {}).get(key, False)
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


def jev_pick_on(cfg: dict) -> bool:
    return _on(cfg, "jev_pick")


def _root(db: DB, task: dict) -> dict:
    ch = chaincap.chain(db, task["id"]) if task.get("id") else []
    return ch[0] if ch else task


def eligible(db: DB, task: dict) -> dict | None:
    """The lineage root of a code task Jev's pick may cover, or None: a review, a task with
    its own effort pin, or one whose lineage was queued at another tier than standard."""
    if task.get("kind") != "code" or str(task.get("effort") or ""):
        return None
    root = _root(db, task)
    if assigned_tier(root) != "standard" or str(root.get("effort") or ""):
        return None
    return root


def _ran_effort(tier: str, tier_effort: str, effort: str) -> str:
    """The effort a run at `tier` with tier effort `tier_effort` takes from a pick of `effort`."""
    return effort if tier == "standard" and tier_effort == CONTROL else tier_effort


def _earlier_pick(db: DB, ids: list[int]) -> dict | None:
    """Jev's recorded pick from an earlier run of the lineage, so a retry or a hop never asks again."""
    if not ids:
        return None
    rows = db.q("SELECT note FROM runs WHERE task IN (%s) AND role!='coordinator' AND note LIKE ? ORDER BY id"
                % ",".join("?" * len(ids)), (*ids, f'%"{KEY}"%'))
    for r in rows:
        got = load_result(r["note"]).get(KEY)
        if isinstance(got, dict) and got.get("by") == "jev" and got.get("pick") in ARMS:
            return got
    return None


def _effort_cost(db: DB, cfg: dict, provider: str, model: str, effort: str) -> float | None:
    row = db.one("SELECT AVG(cost_usd) c, COUNT(*) n FROM runs WHERE role='worker' AND provider=? AND "
                 "COALESCE(model,'')=? AND COALESCE(effort,'')=? AND status!='running' AND cost_usd>0 AND started>=?",
                 (provider, model, effort, time.time() - jevuse.window_s(cfg)))
    return float(row["c"]) if row and row["n"] else None


def _jev_pick(db: DB, cfg: dict, task: dict, root: dict, provider: str, model: str, jev) -> dict | None:
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, "effort"):
        return None
    state = f"kind: {task.get('kind')}\ntitle: {root.get('title')}\nspec:\n{(root.get('spec') or '')[:6000]}"
    ans = jev.decide(state, {"effort": {"type": "score", "criteria": JEV_CRITERIA,
                                        "instructions": "How much reasoning effort does this change need?"}},
                     purpose="effort", timeout=10.0)
    if ans is None:
        return None
    try:
        score = float((ans.get("effort") or {}).get("score"))
    except (TypeError, ValueError):
        return None
    pick = TEST if score < 0.5 else CONTROL
    avoided = 0.0
    if pick == TEST:
        hi, med = _effort_cost(db, cfg, provider, model, CONTROL), _effort_cost(db, cfg, provider, model, TEST)
        avoided = max(0.0, hi - med) if hi is not None and med is not None else 0.0
    cid = jevuse.record(db, "effort", {"effort_level": pick, "score": score, "root": root["id"]},
                        getattr(jev, "last_cost", 0.0), avoided_usd=avoided, ref=f"task:{task['id']}",
                        changed=pick != CONTROL)
    return {"by": "jev", "pick": pick, "score": score, "jev_call": cid}


def assign(db: DB, cfg: dict, task: dict, tier: str, provider: str, jev=None) -> dict | None:
    """The run note's `effort_ab` entry for this start of `task` at `tier` on `provider`, or None:
    {"by": "jev", "pick", "effort", ...} from Jev's pick. `effort` is what the run takes (start_run
    applies it; never over a task's own pin)."""
    if not jev_pick_on(cfg):
        return None
    root = eligible(db, task)
    if root is None:
        return None
    t = ((cfg.get("providers") or {}).get(provider) or {}).get("tiers", {}).get(tier) or {}
    tier_effort, model = str(t.get("effort") or ""), str(t.get("model") or "")
    ids = [x["id"] for x in chaincap.chain(db, task["id"])] if task.get("id") else []
    got = _earlier_pick(db, ids)
    if got is None:
        try:
            got = _jev_pick(db, cfg, task, root, provider, model, jev)
        except Exception as e:   # picking must never hold a task back
            got = {"by": "tier", "jev_error": type(e).__name__}
    if not got or got.get("pick") not in ARMS:
        return {**(got or {}), "by": "tier", "root": root["id"], "effort": tier_effort}
    return {**{k: got[k] for k in ("by", "pick", "score", "jev_call") if k in got}, "root": root["id"],
            "effort": _ran_effort(tier, tier_effort, got["pick"])}


def ran(db: DB, ab: dict, effective: str) -> dict:
    """The run note's `effort_ab` entry once start_run settled the effort the run starts at: `effective`,
    and `lifted` when a conclusion run's tier floor raised it over the pick's `effort`. A lifted
    Jev pick never ran: its call settles as no change (it avoided nothing, and is scored neither way)."""
    out = {**ab, "effective": effective}
    if str(effective or "") != str(ab.get("effort") or ""):
        out["lifted"] = True
        if ab.get("jev_call"):
            jevuse.resolve(db, int(ab["jev_call"]), jevuse.NO_CHANGE,
                           f"tier floor ran it at {effective}, not {ab.get('effort')}")
    return out

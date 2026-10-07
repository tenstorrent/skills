# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev uses: what each use of Jev (the cheap scoring model) costs and saves, and switching off the
uses that do not pay for themselves.

A use is one kind of decision Jev makes for the harness: `screen` (is a new observation worth a
coordinator wake?) and any later one, such as picking a task's effort. Each call is recorded in
`jev_calls` with its decision, its cost and the cost it is estimated to have avoided, and later its
outcome where one becomes known (`resolve`). A call made with `settle_s` counts as right once that
long passed without an outcome; without it, a call's outcome is known only once resolved.

Over a rolling window of `jev.window_days`, a use whose net saving (avoided cost of its right or
unresolved calls, plus any negative saving, i.e. extra cost a call caused, minus the cost of all its
calls) is not positive is switched off once and reported (`review`). It is judged once it made
`jev.min_calls` calls in the window, or once its first call is a full window old (a use called
rarely is judged on what it did; one that saved nothing measurable is not positive). A use is also
switched off once it made `jev.idle_calls` calls in the window whose effect is known and none of them
changed the rules' decision (`record`'s `changed`; a call scored 'no change' changed nothing either):
it costs money and decides nothing. `jev.uses.<use>` = "on" or "off" forces a use either way; "auto"
(or unset) leaves it to the review.

An outcome is 'right', 'wrong' or 'no change' (the call's only effect cost more and did what the
rules' choice would have done: it saves nothing, and a negative saving still counts).
"""
from __future__ import annotations

import json
import time
from typing import Any

from .db import DB

OFF_KEY = "jev_uses_off"           # kv: {use: {"at", "why"}}: uses the review switched off
WINDOW_DAYS, MIN_CALLS = 7, 30     # defaults of jev.window_days and jev.min_calls
IDLE_CALLS = 20                    # default of jev.idle_calls: calls that changed no decision before it goes off
NO_CHANGE = "no change"            # an outcome: the call changed nothing the rules would not have done
WAKE_COST_FALLBACK_USD = 0.05      # a coordinator turn's price while none was measured in the window
LABELS = {"screen": "watcher screening", "effort": "task effort picking",
          "coord_effort": "coordinator effort check"}


def _jcfg(cfg: dict) -> dict:
    return cfg.get("jev") or {}


def window_s(cfg: dict) -> float:
    try:
        return max(1.0, float(_jcfg(cfg).get("window_days", WINDOW_DAYS))) * 86400
    except (TypeError, ValueError):
        return WINDOW_DAYS * 86400


def min_calls(cfg: dict) -> int:
    try:
        return max(1, int(_jcfg(cfg).get("min_calls", MIN_CALLS)))
    except (TypeError, ValueError):
        return MIN_CALLS


def idle_calls(cfg: dict) -> int:
    try:
        return max(1, int(_jcfg(cfg).get("idle_calls", IDLE_CALLS)))
    except (TypeError, ValueError):
        return IDLE_CALLS


def mode(cfg: dict, use: str) -> str:
    """'on', 'off' or 'auto': the config's say on `use`."""
    v = (_jcfg(cfg).get("uses") or {}).get(use, "auto")
    if v is True or str(v).lower() in ("on", "true", "1", "yes"):
        return "on"
    if v is False or str(v).lower() in ("off", "false", "0", "no"):
        return "off"
    return "auto"


def allowed(db: DB, cfg: dict, use: str) -> bool:
    """Whether `use` may call Jev now (Jev's own key and funds are checked by Jev.enabled)."""
    m = mode(cfg, use)
    return m == "on" or (m == "auto" and use not in (db.kv(OFF_KEY, {}) or {}))


def record(db: DB, use: str, decision: Any, cost_usd: float, avoided_usd: float = 0.0, ref: str | None = None,
           settle_s: float | None = None, now: float | None = None, changed: bool | None = None) -> int:
    """Log one Jev call: `decision` (anything JSON), what it cost, and the cost it is estimated to have
    avoided (0 when it changed nothing). `ref` names what it decided about (e.g. issue:12, task:40).
    `changed`: whether the decision differs from the one the rules alone would have made (None: not
    known). Returns the call's id, for `resolve`."""
    now = time.time() if now is None else now
    return db.x("INSERT INTO jev_calls(ts,use,ref,decision,cost_usd,avoided_usd,settle_at,changed) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now, use, ref, json.dumps(decision, default=str)[:2000], float(cost_usd or 0),
                 float(avoided_usd or 0), now + settle_s if settle_s is not None else None,
                 None if changed is None else int(bool(changed))))


def set_ref(db: DB, call_id: int, ref: str) -> None:
    db.x("UPDATE jev_calls SET ref=? WHERE id=? AND ref IS NULL", (ref, call_id))


def resolve(db: DB, call_id: int, right: bool | str, note: str = "", now: float | None = None) -> bool:
    """Record a call's outcome once: whether its decision turned out right, or NO_CHANGE. A wrong or
    no-change call's avoided cost no longer counts as saved (a negative one, extra cost it caused, still
    does). Returns whether this set it."""
    now = time.time() if now is None else now
    outcome = right if isinstance(right, str) else "right" if right else "wrong"
    return db.conn.execute("UPDATE jev_calls SET outcome=?, outcome_ts=?, note=? WHERE id=? AND outcome IS NULL",
                           (outcome, now, note[:300] or None, call_id)).rowcount > 0


def mean_turn_cost(db: DB, cfg: dict, now: float | None = None, effort: str | None = None) -> float:
    """The mean cost of a finished coordinator turn over the window (at `effort` when given): the
    price of a wake avoided. WAKE_COST_FALLBACK_USD while no such turn was measured."""
    now = time.time() if now is None else now
    sql = ("SELECT AVG(cost_usd) c FROM runs WHERE role='coordinator' AND status!='running' "
           "AND cost_usd>0 AND started>=?")
    args: tuple = (now - window_s(cfg),)
    if effort is not None:
        sql, args = sql + " AND COALESCE(effort,'')=?", args + (effort,)
    row = db.one(sql, args)
    return float(row["c"]) if row and row["c"] else WAKE_COST_FALLBACK_USD


def coordinator_effort(cfg: dict) -> str:
    """The coordinator's low (unraised) effort: coordinator.effort, else its tier's on the core provider."""
    c = cfg.get("coordinator") or {}
    tiers = ((cfg.get("providers") or {}).get(cfg.get("core_provider") or "claude") or {}).get("tiers") or {}
    return str(c.get("effort") or "") or str((tiers.get(c.get("tier") or "light") or {}).get("effort") or "")


def low_turn_cost(db: DB, cfg: dict, now: float | None = None) -> float:
    """The mean cost of a low-effort coordinator turn: what a wake Jev kept from happening would have cost."""
    return mean_turn_cost(db, cfg, now, effort=coordinator_effort(cfg))


def stats(db: DB, cfg: dict, now: float | None = None) -> dict[str, dict]:
    """Per use over the window: calls, cost, avoided (saved), net, right, wrong and no-change outcomes,
    and of the calls whose effect is known, how many changed the rules' decision and how many did not."""
    now = time.time() if now is None else now
    rows = db.q("SELECT use, COUNT(*) n, SUM(cost_usd) cost, "
                "SUM(CASE WHEN outcome IN ('wrong',?) THEN MIN(avoided_usd, 0) ELSE avoided_usd END) saved, "
                "SUM(outcome='wrong') n_wrong, SUM(outcome=?) n_same, "
                "SUM(outcome='right' OR (outcome IS NULL AND settle_at IS NOT NULL AND settle_at<=?)) n_right, "
                "SUM(changed=1 AND COALESCE(outcome,'')!=?) n_changed, "
                "SUM(changed=0 OR (changed=1 AND outcome=?)) n_unchanged "
                "FROM jev_calls WHERE ts>=? GROUP BY use ORDER BY use",
                (NO_CHANGE, NO_CHANGE, now, NO_CHANGE, NO_CHANGE, now - window_s(cfg)))
    return {r["use"]: {"calls": int(r["n"]), "cost": float(r["cost"] or 0), "saved": float(r["saved"] or 0),
                       "net": float(r["saved"] or 0) - float(r["cost"] or 0),
                       "wrong": int(r["n_wrong"] or 0), "right": int(r["n_right"] or 0),
                       "no_change": int(r["n_same"] or 0), "changed": int(r["n_changed"] or 0),
                       "unchanged": int(r["n_unchanged"] or 0)} for r in rows}


def review(db: DB, cfg: dict, now: float | None = None) -> list[tuple[str, dict]]:
    """Switch off each auto use whose net saving over a full window is not positive, or whose last
    jev.idle_calls known calls changed no decision. Returns the uses switched off by this call (to report
    once), with their stats. A use forced on loses its mark."""
    now = time.time() if now is None else now
    out = []
    with db.tx():
        off = db.kv(OFF_KEY, {}) or {}
        cleared = {u for u in off if mode(cfg, u) == "on"}
        for use, s in stats(db, cfg, now).items():
            if use in off or mode(cfg, use) != "auto":
                continue
            if not s["changed"] and s["unchanged"] >= idle_calls(cfg):
                off[use] = {"at": now, "why": f"no call changed the rules' decision in {s['unchanged']} calls"}
                out.append((use, s))
                continue
            if s["net"] > 0:
                continue
            if s["calls"] < min_calls(cfg):
                first = db.one("SELECT MIN(ts) t FROM jev_calls WHERE use=?", (use,))
                if not first or first["t"] is None or now - float(first["t"]) < window_s(cfg):
                    continue
            off[use] = {"at": now, "why": f"net {_usd(s['net'])} over {s['calls']} calls"}
            out.append((use, s))
        if out or cleared:
            db.set_kv(OFF_KEY, {u: v for u, v in off.items() if u not in cleared})
    return out


def off_text(use: str, s: dict, cfg: dict) -> str:
    """The one report of a use switched off."""
    if not s.get("changed") and s.get("unchanged", 0) >= idle_calls(cfg):
        return (f"Jev {LABELS.get(use, use)} is switched off: over the last {window_s(cfg) / 86400:g} d none of "
                f"its {s['unchanged']} calls changed the rules' decision, at a cost of {_usd(s['cost'])} "
                f"(net {_usd(s['net'])}). The rules decide alone now; set jev.uses.{use} to \"on\" to force it back.")
    return (f"Jev {LABELS.get(use, use)} is switched off: over the last {window_s(cfg) / 86400:g} d its "
            f"{s['calls']} calls cost {_usd(s['cost'])} and saved an estimated {_usd(s['saved'])} "
            f"(net {_usd(s['net'])}). The rules decide alone now; set jev.uses.{use} to \"on\" to force it back.")


def lines(db: DB, cfg: dict, now: float | None = None) -> list[str]:
    """One line per use for the daily review: calls, cost, estimated savings, net, error rate, state."""
    now = time.time() if now is None else now
    off = db.kv(OFF_KEY, {}) or {}
    out = []
    for use, s in stats(db, cfg, now).items():
        known = s["right"] + s["wrong"]
        err = f"errors {s['wrong']}/{known} ({100 * s['wrong'] / known:.0f}%)" if known else "error rate unknown"
        if s["no_change"]:
            err += f", no change {s['no_change']}"
        if s["changed"] + s["unchanged"]:
            err += f", changed the rules' decision {s['changed']}/{s['changed'] + s['unchanged']}"
        m = mode(cfg, use)
        state = ("forced on" if m == "on" else "forced off" if m == "off"
                 else f"switched off ({off[use]['why']})" if use in off else "on")
        out.append(f"{LABELS.get(use, use)} [{use}]: {s['calls']} calls, cost {_usd(s['cost'])}, "
                   f"est. saved {_usd(s['saved'])}, net {_usd(s['net'])}, {err}; {state}")
    return out


def _usd(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):.3f}"

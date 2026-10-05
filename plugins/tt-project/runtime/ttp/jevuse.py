# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev uses: what each use of Jev (the cheap scoring model) costs and saves, and switching off the
uses that do not pay for themselves.

A use is one kind of decision Jev makes for the harness: `screen` (is a new observation worth a
coordinator wake?) and any later one, such as picking a task's effort. Each call is recorded in
`jev_calls` with its decision, its cost and the cost it is estimated to have avoided, and later its
outcome where one becomes known (`resolve`). A call made with `settle_s` counts as right once that
long passed without an outcome; without it, a call's outcome is known only once resolved.

Over a rolling window of `jev.window_days` with at least `jev.min_calls` calls, a use whose net
saving (avoided cost of its right or unresolved calls, plus any negative saving, i.e. extra cost a
call caused, minus the cost of all its calls) is not
positive is switched off once and reported (`review`). `jev.uses.<use>` = "on" or "off" forces a
use either way; "auto" (or unset) leaves it to the review.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .db import DB

OFF_KEY = "jev_uses_off"           # kv: {use: {"at", "why"}}: uses the review switched off
WINDOW_DAYS, MIN_CALLS = 7, 30     # defaults of jev.window_days and jev.min_calls
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
           settle_s: float | None = None, now: float | None = None) -> int:
    """Log one Jev call: `decision` (anything JSON), what it cost, and the cost it is estimated to have
    avoided (0 when it changed nothing). `ref` names what it decided about (e.g. issue:12, task:40).
    Returns the call's id, for `resolve`."""
    now = time.time() if now is None else now
    return db.x("INSERT INTO jev_calls(ts,use,ref,decision,cost_usd,avoided_usd,settle_at) VALUES(?,?,?,?,?,?,?)",
                (now, use, ref, json.dumps(decision, default=str)[:2000], float(cost_usd or 0),
                 float(avoided_usd or 0), now + settle_s if settle_s is not None else None))


def set_ref(db: DB, call_id: int, ref: str) -> None:
    db.x("UPDATE jev_calls SET ref=? WHERE id=? AND ref IS NULL", (ref, call_id))


def resolve(db: DB, call_id: int, right: bool, note: str = "", now: float | None = None) -> bool:
    """Record a call's outcome once: whether its decision turned out right. A wrong call's avoided cost
    no longer counts as saved (a negative one, extra cost it caused, still does). Returns whether this set it."""
    now = time.time() if now is None else now
    return db.conn.execute("UPDATE jev_calls SET outcome=?, outcome_ts=?, note=? WHERE id=? AND outcome IS NULL",
                           ("right" if right else "wrong", now, note[:300] or None, call_id)).rowcount > 0


def mean_turn_cost(db: DB, cfg: dict, now: float | None = None) -> float:
    """The mean cost of a finished coordinator turn over the window: the price of a wake avoided."""
    now = time.time() if now is None else now
    row = db.one("SELECT AVG(cost_usd) c FROM runs WHERE role='coordinator' AND status!='running' "
                 "AND cost_usd>0 AND started>=?", (now - window_s(cfg),))
    return float(row["c"]) if row and row["c"] else WAKE_COST_FALLBACK_USD


def stats(db: DB, cfg: dict, now: float | None = None) -> dict[str, dict]:
    """Per use over the window: calls, cost, avoided (saved), net, right and wrong outcomes."""
    now = time.time() if now is None else now
    rows = db.q("SELECT use, COUNT(*) n, SUM(cost_usd) cost, "
                "SUM(CASE WHEN outcome='wrong' THEN MIN(avoided_usd, 0) ELSE avoided_usd END) saved, "
                "SUM(outcome='wrong') n_wrong, "
                "SUM(outcome='right' OR (outcome IS NULL AND settle_at IS NOT NULL AND settle_at<=?)) n_right "
                "FROM jev_calls WHERE ts>=? GROUP BY use ORDER BY use", (now, now - window_s(cfg)))
    return {r["use"]: {"calls": int(r["n"]), "cost": float(r["cost"] or 0), "saved": float(r["saved"] or 0),
                       "net": float(r["saved"] or 0) - float(r["cost"] or 0),
                       "wrong": int(r["n_wrong"] or 0), "right": int(r["n_right"] or 0)} for r in rows}


def review(db: DB, cfg: dict, now: float | None = None) -> list[tuple[str, dict]]:
    """Switch off each auto use whose net saving over a full window is not positive. Returns the uses
    switched off by this call (to report once), with their stats. A use forced on loses its mark."""
    now = time.time() if now is None else now
    out = []
    with db.tx():
        off = db.kv(OFF_KEY, {}) or {}
        cleared = {u for u in off if mode(cfg, u) == "on"}
        for use, s in stats(db, cfg, now).items():
            if use in off or mode(cfg, use) != "auto" or s["calls"] < min_calls(cfg) or s["net"] > 0:
                continue
            off[use] = {"at": now, "why": f"net {_usd(s['net'])} over {s['calls']} calls"}
            out.append((use, s))
        if out or cleared:
            db.set_kv(OFF_KEY, {u: v for u, v in off.items() if u not in cleared})
    return out


def off_text(use: str, s: dict, cfg: dict) -> str:
    """The one report of a use switched off."""
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
        m = mode(cfg, use)
        state = ("forced on" if m == "on" else "forced off" if m == "off"
                 else f"switched off ({off[use]['why']})" if use in off else "on")
        out.append(f"{LABELS.get(use, use)} [{use}]: {s['calls']} calls, cost {_usd(s['cost'])}, "
                   f"est. saved {_usd(s['saved'])}, net {_usd(s['net'])}, {err}; {state}")
    return out


def _usd(v: float) -> str:
    return f"{'-' if v < 0 else ''}${abs(v):.3f}"

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Budget governor: turns meter readings and the spend ledger into a gate for new work.

Two regimes, chosen per provider from what the provider reports:
- plan windows (subscription plans report utilization per window): the project may never take the
  account past 100 - reserve_pct, because the remainder belongs to the user's own work;
- dollar caps (usage-billed accounts report no window): rolling 24 h and 7 d caps on what THIS
  project spends across all its providers not on plan windows, with the user's defaults when the
  charter sets none.
A runaway check (spend rate far above this project's own norm) overrides both.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import DB

LEVELS = ("green", "yellow", "orange", "red")
HOUR, DAY, WEEK = 3600.0, 86400.0, 7 * 86400.0
SNAPSHOT_FRESH_S = 30 * 60
# Relative price of each token class (input = 1), used only to apply an observed rate to a token
# mix; not a price list. Override with budget.estimate_weights.
TOKEN_WEIGHTS = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}


@dataclass
class Window:
    provider: str
    window: str                 # e.g. "5h", "7d", "7d_opus", "primary", "secondary"
    utilization: float          # percent of the window used by the whole account, 0..100
    resets_at: float | None = None
    account: str = ""


@dataclass
class Gate:
    provider: str
    level: str = "green"
    reasons: list[str] = field(default_factory=list)
    regime: str = "caps"        # "windows" or "caps"
    max_parallel: int = 2
    max_tier: str = "deep"      # deepest reasoning tier allowed right now
    allow_optional: bool = True # recurring improvement work, exploration, nice-to-haves
    allow_new_work: bool = True # anything but replies to a user's direct message
    numbers: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _raise(g: Gate, level: str, reason: str) -> None:
    if LEVELS.index(level) > LEVELS.index(g.level):
        g.level = level
    g.reasons.append(reason)


def evaluate(db: DB, cfg: dict, provider: str, windows: list[Window], now: float | None = None) -> Gate:
    now = now or time.time()
    b = cfg["budget"]
    g = Gate(provider=provider, max_parallel=int(b.get("max_parallel_workers", 2)))
    limit = 100.0 - float(b.get("reserve_pct", 10))

    fresh = [w for w in windows if w.provider == provider]
    if fresh:
        g.regime = "windows"
        worst = max(fresh, key=lambda w: w.utilization)
        g.numbers.update({"window": worst.window, "utilization": round(worst.utilization, 1),
                          "limit": limit, "resets_at": worst.resets_at})
        for w in fresh:
            if w.utilization >= limit:
                _raise(g, "red", f"{w.window} window at {w.utilization:.0f}% (project stops at {limit:.0f}%)")
            elif w.utilization >= limit - 10:
                _raise(g, "orange", f"{w.window} window at {w.utilization:.0f}%")
            elif w.utilization >= limit - 25:
                _raise(g, "yellow", f"{w.window} window at {w.utilization:.0f}%")
    else:
        # The caps bound the project's dollars, whichever provider spends them. Providers on plan
        # windows are bounded by their windows instead, so their spend does not count here.
        day_cap, week_cap = float(b.get("daily_usd") or 0), float(b.get("weekly_usd") or 0)
        windowed = sorted({w.provider for w in windows} | plan_providers(db, now))
        d = db.spent_since(now - DAY, exclude=windowed)
        w7 = db.spent_since(now - WEEK, exclude=windowed)
        g.numbers.update({"spent_24h": round(d, 2), "spent_7d": round(w7, 2),
                          "estimated_24h": round(db.spent_since(now - DAY, exclude=windowed, estimated_only=True), 2),
                          "daily_cap": day_cap, "weekly_cap": week_cap})
        ratio = max(d / day_cap if day_cap else 0.0, w7 / week_cap if week_cap else 0.0)
        g.numbers["cap_ratio"] = round(ratio, 3)
        if ratio >= 1.0:
            _raise(g, "red", f"cap reached: ${d:.2f}/24h of ${day_cap:.0f}, ${w7:.2f}/7d of ${week_cap:.0f}")
        elif ratio >= 0.85:
            _raise(g, "orange", f"{ratio:.0%} of cap used")
        elif ratio >= 0.6:
            _raise(g, "yellow", f"{ratio:.0%} of cap used")

    # Runaway guard: catch loops, not busy projects. Three signals over the last hour:
    # - waste: spend on runs that ended without an outcome (failed, stalled, timed out, lost);
    # - thrash: coordinator spend (decisions should be cents; dollars mean it is spinning);
    # - total: all spend above max(k x this project's 7-day hourly norm, a floor sized to the
    #   parallel work it is allowed), so even "successful" repetition is bounded.
    hour_ago = now - HOUR
    last_h = db.spent_since(hour_ago, provider)
    runs_h = db.q("SELECT role, status, cost_usd FROM runs WHERE provider=? AND ended>=?", (provider, hour_ago))
    waste = sum(float(r["cost_usd"] or 0) for r in runs_h
                if r["status"] in ("failed", "stalled", "timeout", "lost", "killed", "budget"))
    thrash = sum(float(r["cost_usd"] or 0) for r in runs_h if r["role"] == "coordinator")
    norm = db.spent_since(now - WEEK, provider) / (7 * 24)
    per_task = max((b.get("task_default_usd") or {"deep": 25.0}).values())
    floor = float(b.get("hourly_floor_usd") or max(int(b.get("max_parallel_workers", 2)), 1) * per_task)
    ceiling = max(float(b.get("hourly_alarm_x", 4.0)) * norm, floor)
    waste_cap = float(b.get("hourly_waste_usd", 8.0))
    thrash_cap = float(b.get("hourly_coordinator_usd", 4.0))
    g.numbers.update({"spent_1h": round(last_h, 2), "hourly_ceiling": round(ceiling, 2),
                      "waste_1h": round(waste, 2), "coordinator_1h": round(thrash, 2)})
    if waste > waste_cap:
        _raise(g, "red", f"runaway guard: ${waste:.2f} spent on failed or stalled runs in the last hour "
                         f"(limit ${waste_cap:.0f}); resumes automatically as the hour rolls over")
    if thrash > thrash_cap:
        _raise(g, "red", f"runaway guard: coordinator spent ${thrash:.2f} in the last hour (limit ${thrash_cap:.0f})")
    if last_h > ceiling:
        _raise(g, "red", f"runaway guard: ${last_h:.2f} spent in the last hour, ceiling ${ceiling:.2f}/h; "
                         f"resumes automatically as the hour rolls over")

    if g.level == "yellow":
        g.max_tier, g.max_parallel = "standard", max(1, g.max_parallel // 2 or 1)
    elif g.level == "orange":
        g.max_tier, g.max_parallel, g.allow_optional = "light", 1, False
    elif g.level == "red":
        g.max_tier, g.max_parallel, g.allow_optional, g.allow_new_work = "light", 0, False, False
    return g


def estimate_cost(db: DB, cfg: dict, provider: str, model: str, tokens: dict[str, int],
                  now: float | None = None) -> float:
    """Cost of a run that ended without reporting one (killed, lost, cut off) from its token counts.

    The rate is this project's own: reported cost over weighted tokens of its recent runs on the same
    provider (same model when there are any). The weights only relate the token classes to each
    other. With no such runs yet, the configured fallback applies; it is set high on purpose.
    """
    now = now or time.time()
    b = cfg["budget"]
    w = {**TOKEN_WEIGHTS, **(b.get("estimate_weights") or {})}

    def weighted(i: float, o: float, cr: float, cw: float) -> float:
        return i * w["input"] + o * w["output"] + cr * w["cache_read"] + cw * w["cache_write"]

    rate = None
    for same_model in (True, False):
        rows = db.q("SELECT cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM runs "
                    "WHERE provider=? AND (? OR model=?) AND cost_estimated=0 AND cost_usd>0 AND ended>=? "
                    "ORDER BY id DESC LIMIT 50", (provider, int(not same_model), model, now - 14 * DAY))
        units = sum(weighted(r["input_tokens"] or 0, r["output_tokens"] or 0, r["cache_read_tokens"] or 0,
                             r["cache_write_tokens"] or 0) for r in rows)
        if units > 0:
            rate = sum(float(r["cost_usd"]) for r in rows) / units
            break
    if rate is None:
        rate = float(b.get("estimate_usd_per_mtok", 15.0)) / 1e6
    return round(rate * weighted(tokens.get("input", 0), tokens.get("output", 0), tokens.get("cache_read", 0),
                                 tokens.get("cache_write", 0)), 4)


TIER_ORDER = ("light", "standard", "deep")


def clamp_tier(tier: str, gate: Gate) -> str:
    tier = tier if tier in TIER_ORDER else "standard"
    return tier if TIER_ORDER.index(tier) <= TIER_ORDER.index(gate.max_tier) else gate.max_tier


def windows_from_snapshots(db: DB, now: float | None = None) -> list[Window]:
    """Latest reading per (provider, window), ignoring readings too old to trust."""
    now = now or time.time()
    rows = db.q("SELECT s.* FROM snapshots s JOIN (SELECT provider, window, MAX(ts) mts FROM snapshots "
                "GROUP BY provider, window) m ON s.provider=m.provider AND s.window=m.window AND s.ts=m.mts "
                "WHERE s.ts>=?", (now - SNAPSHOT_FRESH_S,))
    return [Window(r["provider"], r["window"], float(r["utilization"] or 0), r["resets_at"], r["account"] or "")
            for r in rows]


def plan_providers(db: DB, now: float | None = None) -> set[str]:
    """Providers with any window reading in the last week, i.e. billed by plan windows, not dollars."""
    now = now or time.time()
    return {r["provider"] for r in db.q("SELECT DISTINCT provider FROM snapshots WHERE ts>=?", (now - WEEK,))}


def record_windows(db: DB, windows: list[Window]) -> None:
    now = time.time()
    for w in windows:
        db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
             (now, w.provider, w.account, w.window, w.utilization, w.resets_at))


def history(db: DB, days: int = 14) -> dict:
    """Daily spend and daily peak window utilization for the web app's two-week view."""
    since = time.time() - days * DAY
    spend = db.q("SELECT date(ts,'unixepoch','localtime') d, provider, ROUND(SUM(usd),2) usd, "
                 "SUM(estimated) est FROM ledger WHERE ts>=? GROUP BY d, provider ORDER BY d", (since,))
    peaks = db.q("SELECT date(ts,'unixepoch','localtime') d, provider, window, ROUND(MAX(utilization),1) peak, "
                 "ROUND(AVG(utilization),1) avg FROM snapshots WHERE ts>=? GROUP BY d, provider, window ORDER BY d",
                 (since,))
    by_source = db.q("SELECT source, provider, ROUND(SUM(usd),2) usd, COUNT(*) n FROM ledger WHERE ts>=? "
                     "GROUP BY source, provider ORDER BY usd DESC LIMIT 40", (time.time() - WEEK,))
    return {"daily_spend": spend, "window_peaks": peaks, "top_sources_7d": by_source}

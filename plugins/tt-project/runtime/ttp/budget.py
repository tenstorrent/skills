# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Budget governor: turns meter readings and the spend ledger into a gate for new work.

Two regimes, chosen per provider from what the provider reports:
- plan windows (subscription plans report utilization per window): the project may never take the
  account past 100 - reserve_pct, because the remainder belongs to the user's own work;
- dollar caps (usage-billed accounts report no window): rolling 24 h and 7 d caps on what THIS
  project spends, with the user's defaults when the charter sets none.
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
        day_cap, week_cap = float(b.get("daily_usd") or 0), float(b.get("weekly_usd") or 0)
        d = db.spent_since(now - DAY, provider)
        w7 = db.spent_since(now - WEEK, provider)
        g.numbers.update({"spent_24h": round(d, 2), "spent_7d": round(w7, 2),
                          "daily_cap": day_cap, "weekly_cap": week_cap})
        ratio = max(d / day_cap if day_cap else 0.0, w7 / week_cap if week_cap else 0.0)
        g.numbers["cap_ratio"] = round(ratio, 3)
        if ratio >= 1.0:
            _raise(g, "red", f"cap reached: ${d:.2f}/24h of ${day_cap:.0f}, ${w7:.2f}/7d of ${week_cap:.0f}")
        elif ratio >= 0.85:
            _raise(g, "orange", f"{ratio:.0%} of cap used")
        elif ratio >= 0.6:
            _raise(g, "yellow", f"{ratio:.0%} of cap used")

    # Runaway: this project's last hour against a ceiling of max(k x its own 7-day hourly norm, a
    # floor). The floor lets a legitimately heavy task run; the multiple catches a loop that has
    # quietly settled into spending far more than this project ever does.
    last_h = db.spent_since(now - HOUR, provider)
    norm = db.spent_since(now - WEEK, provider) / (7 * 24)
    day_cap = float(b.get("daily_usd") or 0)
    floor = float(b.get("hourly_floor_usd") or (day_cap / 4 if g.regime == "caps" and day_cap else 10.0))
    ceiling = max(float(b.get("hourly_alarm_x", 4.0)) * norm, floor)
    g.numbers.update({"spent_1h": round(last_h, 2), "hourly_ceiling": round(ceiling, 2)})
    if last_h > ceiling:
        _raise(g, "red", f"runaway guard: ${last_h:.2f} spent in the last hour, ceiling ${ceiling:.2f}/h")

    if g.level == "yellow":
        g.max_tier, g.max_parallel = "standard", max(1, g.max_parallel // 2 or 1)
    elif g.level == "orange":
        g.max_tier, g.max_parallel, g.allow_optional = "light", 1, False
    elif g.level == "red":
        g.max_tier, g.max_parallel, g.allow_optional, g.allow_new_work = "light", 0, False, False
    return g


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

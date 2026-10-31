# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Budget governor: turns meter readings and the spend ledger into a gate for new work.

Two regimes, chosen per provider from what the provider reports:
- plan windows (subscription plans report utilization per window): a plan is paid for per period,
  so unused capacity is lost at each reset. Below the line (100 - reserve_pct) the project runs all
  its parallel workers; it does not spread use evenly over a window. Running work keeps burning
  after it starts, so near the line the project estimates what running work will still add, from
  measured burn, and stops starting runs once that would reach the line. It never takes the account
  past the line, because the remainder belongs to the user's own work;
- dollar caps (usage-billed accounts report no window): a daily cap (the fixed budget day, or the
  last 24 h) and a rolling 7 d cap on what THIS project spends across all its providers not on plan
  windows, with the user's defaults when the charter sets none; and a global daily cap on what the
  whole account spends today across every project tt-project can see (globalcap.py).
A runaway check (a loop: wasted runs, a coordinator spinning, spend far above this project's own
norm) overrides both. On plan windows its dollars are virtual, so it counts wasted runs and
coordinator turns there.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import ackwait, credits
from . import globalcap as gcap
from . import timefmt
from . import localspend  # noqa: F401  (adds this machine's other Claude Code spend to the global total)
from .billing import PLAN_LAPSE_RUNS, billed_by_account
from .db import DB

LEVELS = ("green", "yellow", "orange", "red")
HOUR, DAY, WEEK = 3600.0, 86400.0, 7 * 86400.0
SNAPSHOT_FRESH_S = 30 * 60
PLAN_MEMORY_S = 7 * 86400      # a provider that reported plan windows this recently is on a plan
# Where the daemon keeps the account each provider is logged in as (plan readings are keyed by it).
ACCOUNT_KV = "plan_account:"
# Length of each named window, used to measure burn over a sensible span and to roll a window over
# when its reset has passed without a new reading. Unknown names fall back to a week.
WINDOW_HOURS = {"five_hour": 5.0, "5h": 5.0, "seven_day": 168.0, "7d": 168.0, "seven_day_opus": 168.0,
                "seven_day_sonnet": 168.0}
# Spend up to billing.PLAN_GRACE_S after a plan provider's last window reading still counts as plan-billed.
# A plan provider's windows arrive with each run or from a meter read every few minutes. Once its
# last reading is stale and PLAN_LAPSE_RUNS paid runs have ended since, it is billed by use (an API
# key, an expired plan): the dollar caps apply again. Which spend was billed is decided per account
# and per time in billing.py.
# Relative price of each token class (input = 1), used only to apply an observed rate to a token
# mix; not a price list. Override with budget.estimate_weights.
TOKEN_WEIGHTS = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}
# Burn is measured over a quarter of the window, at most this long. Readings come in whole percents,
# so a weekly window needs hours of them for a steady slope: over 3 h its slope swung more than
# 30-fold with each one-point step.
BURN_SPAN_MAX_S = 12 * HOUR
# How long a worker run lasts before this project has finished any to measure it from.
RUN_HORIZON_DEFAULT_S = HOUR
# Below this many points under the line, with no burn measured yet, only one light worker runs.
LINE_MARGIN_PCT = 2.0


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


def red_class(reason: str) -> str | None:
    """The class of a red reason other than a plan window's line (daemon: one alert per episode of a
    class), or None."""
    for prefix, cls in (("runaway guard", "runaway guard"), ("cap reached", "cap"),
                        ("global daily cap reached", "global cap")):
        if reason.startswith(prefix):
            return cls
    return "spend limit" if "reports its spend limit reached" in reason else None


def red_share(g: Gate, cls: str) -> float | None:
    """How far a red class's measured value is toward its limit (1 = at it), None when not measured."""
    n = g.numbers or {}
    if cls == "runaway guard":
        return n.get("guard_ratio")
    if cls == "cap":
        return n.get("cap_ratio")
    if cls == "global cap" and float(n.get("global_cap") or 0) > 0:
        return float(n.get("global_today") or 0) / float(n["global_cap"])
    return None


def _raise(g: Gate, level: str, reason: str) -> None:
    if LEVELS.index(level) > LEVELS.index(g.level):
        g.level = level
    g.reasons.append(reason)


def in_flight(db: DB, provider: str | None = None, exclude: set[str] | None = None,
              billed_at: float | None = None) -> float:
    """Spend so far of runs still going, as the daemon last priced it; the ledger has it only
    once they end. With `billed_at`, only runs whose account is billed by use at that time."""
    if billed_at is not None:
        got = billed_by_account(db.conn, billed_at, billed_at, provider=provider, running_at=billed_at)
        return sum(usd for (p, _), usd in got.items() if p not in (exclude or set()))
    rows = db.q("SELECT provider, cost_usd FROM runs WHERE status='running' AND (? IS NULL OR provider=?)",
                (provider, provider))
    return sum(float(r["cost_usd"] or 0) for r in rows if r["provider"] not in (exclude or set()))


def spent_last_hour(db: DB, provider: str, now: float) -> float:
    """Spend in the hour before `now`, each run's cost spread evenly over the time it ran. The
    ledger books a run's whole cost when it ends, so a long run would otherwise land in one hour."""
    since = now - HOUR
    total = db.spent_since(since, provider)
    for r in db.q("SELECT started, ended, status, cost_usd FROM runs WHERE provider=? "
                  "AND (status='running' OR ended>=?)", (provider, since)):
        cost = float(r["cost_usd"] or 0)
        running = r["status"] == "running"
        end = now if running else float(r["ended"])
        start = min(float(r["started"] or end), end)
        before = min(max(since - start, 0.0), end - start) / (end - start) if end > start else 0.0
        total += cost * (1 - before) if running else -cost * before
    return max(total, 0.0)


WASTED = ("failed", "stalled", "timeout", "lost", "budget", "no_handoff", "refused_wait", "request_error")


def wasted(run: dict) -> bool:
    """A run that ended without an outcome, unless the daemon found a cause other than a loop: the
    host rebooted under it, or its hand-off stood. Its spend still counts everywhere else."""
    if run["status"] not in WASTED:
        return False
    try:
        return not (json.loads(run["note"] or "{}") or {}).get("not_waste")
    except (TypeError, ValueError, AttributeError):
        return True


def _peak_tasks(spans: list, now: float) -> int:
    """Most distinct tasks whose given runs (task, started, ended) were going at the same moment.
    Runs without a task count as one."""
    edges = []
    for s in spans:
        if not s["started"]:
            continue    # never launched
        start = float(s["started"])
        end = max(float(s["ended"]) if s["ended"] is not None else now, start)
        edges += [(start, 1, s["task"]), (end, -1, s["task"])]
    active: dict = {}
    peak = 0
    for _, step, task in sorted(edges, key=lambda e: e[:2]):   # an end sorts before a start at the same instant
        active[task] = active.get(task, 0) + step
        if not active[task]:
            del active[task]
        peak = max(peak, len(active))
    return peak


def evaluate(db: DB, cfg: dict, provider: str, windows: list[Window], now: float | None = None) -> Gate:
    now = now or time.time()
    b = cfg["budget"]
    g = Gate(provider=provider, max_parallel=int(b.get("max_parallel_workers", 6)))
    limit = 100.0 - float(b.get("reserve_pct", 10))

    last = plan_providers(db, now)
    # A provider whose readings all belong to another account (or predate a usage-billed one) has
    # left that plan: its spend since the last of them counts toward the dollar caps at once.
    moved = set(last) - {w.provider for w in plan_windows(db, now)}
    lapsed = moved | {p for p, ts in last.items() if now - ts > SNAPSHOT_FRESH_S and plan_lapsed(db, p, ts)}
    plan = [w for w in windows if w.provider == provider and provider not in lapsed]
    billed_since = account_billed_since(db, provider, last.get(provider, 0.0)) if provider in moved else None
    if billed_since is not None:
        # The current account is billed by use: the old account's plan readings are history, and
        # the switch was made on purpose. A neutral line, never an alert.
        g.numbers["billed_by_use_since"] = billed_since
        g.reasons.append(f"{provider} billed by use since {timefmt.short(billed_since, cfg, now)}")
    elif provider in moved:
        # The login has no plan readings of its own yet (its kind is unknown, or a plan account's
        # readings are missing): billed by use is the safe side until its next run reports them. A
        # self-clearing status line (daemon: alert account-mismatch:<provider>), not an instruction.
        g.numbers["account_mismatch"] = True
        g.reasons.append(f"{provider}'s current account has no plan readings of its own yet"
                         + ("; its spend counts toward the dollar caps until a run reports its plan windows"
                            if dollar_caps(b) else ""))
    elif provider in lapsed and dollar_caps(b):
        # With no dollar cap set (0 = unset) nothing changes for the user: no reason to tell.
        g.reasons.append(f"{provider} stopped reporting plan windows; its spend counts toward the dollar caps")
    reading = credits.latest(db, provider, now) if plan else None
    paid = credits.paid_open(reading)
    if plan:
        g.regime = "windows"
        _plan(db, g, provider, plan, limit, int(b.get("max_parallel_workers", 6)), now, paid=paid, cfg=cfg)
        if reading and reading.get("spend_control_reached") is True:
            _raise(g, "red", f"{provider} reports its spend limit reached; new work waits until it lifts")
        at_line = [r for r in g.numbers.get("plan") or [] if r["utilization"] >= limit]
        if paid and at_line:
            # Past the line the account runs on paid credits: real money, so this provider's spend
            # since the first reading at the line counts toward the dollar caps and the global cap.
            since = _paid_since(db, provider, at_line, limit, now)
            extra = db.spent_since(since, provider) + in_flight(db, provider)
            g.numbers["paid_credits"] = {"since": since, "usd": round(extra, 2)}
            _caps(db, g, b, provider, windows, last, now, extra=extra)
    else:
        _caps(db, g, b, provider, windows, last, now)

    # Runaway guard: catch loops, not busy projects. Three signals over the last hour:
    # - waste: spend on runs that ended without an outcome (failed, stalled, timed out, lost), except
    #   runs a reboot cut short or whose hand-off stood;
    # - thrash: spend on coordinator turns the daemon started by itself (events, schedules, idle
    #   wakes, sweeps): decisions should be cents, dollars mean it is spinning. A turn started for
    #   a user message or an ask answer (its note lists them) is the user's own work, not a loop;
    #   the total signal still bounds it;
    # - total: all spend above max(k x this project's 7-day hourly norm, a floor sized to the
    #   parallel work it is allowed), so even "successful" repetition is bounded.
    # On a plan below its line the dollars are virtual: the windows and their line already bound the
    # spend, so the waste signal counts wasted runs, the coordinator signal counts self-started turns
    # and turns re-woken on the same evidence instead, and the total signal is off. Billed by use or on paid credits past the line,
    # dollars.
    virtual = g.regime == "windows" and "paid_credits" not in g.numbers
    g.numbers["billing"] = "plan" if virtual else "usage"
    hour_ago = now - HOUR
    last_h = spent_last_hour(db, provider, now)
    runs_h = db.q("SELECT role, status, cost_usd, note FROM runs WHERE provider=? AND ended>=?", (provider, hour_ago))
    # A run stopped on purpose (a cancel, a pause, a redirect) is a decision, not waste.
    waste = sum(float(r["cost_usd"] or 0) for r in runs_h if wasted(r))
    coord_h = [r for r in runs_h if r["role"] == "coordinator"]
    asked = sum(float(r["cost_usd"] or 0) for r in coord_h if user_turn(r))
    thrash = sum(float(r["cost_usd"] or 0) for r in coord_h) - asked
    norm = db.spent_since(now - WEEK, provider) / (7 * 24)
    per_task = max((b.get("task_default_usd") or {"deep": 25.0}).values())
    # Spend limits are sized from the worker count before any `ttp throughput` raise: it adds slots, never spend.
    from .throughput import spend_workers
    floor = float(b.get("hourly_floor_usd") or max(spend_workers(db, int(b.get("max_parallel_workers", 2))), 1) * per_task)
    ceiling = max(float(b.get("hourly_alarm_x", 4.0)) * norm, floor)
    # The waste limit is per worker: parallel workers each losing a run (a shared device kept them
    # all waiting) are not a loop. Workers are the most tasks whose wasted runs went at once, so one
    # task failing over and over, or new tasks failing one after another, count as one worker.
    spans = [r for r in db.q(f"SELECT task, started, ended, status, note FROM runs WHERE provider=? "
                             f"AND role!='coordinator' AND ended>=? AND status IN ({','.join('?' * len(WASTED))})",
                             (provider, hour_ago, *WASTED)) if wasted(r)]
    workers = min(max(_peak_tasks(spans, now), 1), max(spend_workers(db, int(b.get("max_parallel_workers", 6))), 1))
    waste_cap = float(b.get("hourly_waste_usd", 8.0)) * workers
    thrash_cap = float(b.get("hourly_coordinator_usd", 4.0))
    # Each signal's share of its limit: red past 1, and a red alert's episode ends only well below it
    # (daemon: RED_CLEAR_RATIO), so a value hovering at a limit is one alert, not one per crossing.
    shares: list[float] = []
    g.numbers.update({"spent_1h": round(last_h, 2), "hourly_ceiling": round(ceiling, 2),
                      "waste_1h": round(waste, 2), "waste_limit": round(waste_cap, 2),
                      "coordinator_1h": round(thrash, 2), "coordinator_user_1h": round(asked, 2)})
    if virtual:
        # On a plan a costly failed run is no loop by itself: count the wasted runs instead.
        lost = sum(1 for r in runs_h if wasted(r))
        lost_cap = int(b.get("hourly_waste_runs", 4)) * workers
        g.numbers.update({"waste_runs_1h": lost, "waste_runs_limit": lost_cap})
        shares.append(_share(lost, lost_cap))
        if lost > lost_cap:
            _raise(g, "red", f"runaway guard: {lost} failed or stalled runs in the last hour "
                             f"(limit {lost_cap}); resumes automatically as the hour rolls over")
    else:
        shares.append(_share(waste, waste_cap))
    if not virtual and waste > waste_cap:
        _raise(g, "red", f"runaway guard: ${waste:.2f} spent on failed or stalled runs in the last hour "
                         f"(limit ${waste_cap:.0f}); resumes automatically as the hour rolls over")
    selfs = [r for r in coord_h if not user_turn(r)]
    same = repeated_turns(selfs)
    g.numbers.update({"coordinator_turns_1h": len(selfs), "coordinator_repeats_1h": same})
    if virtual:
        # Below coordinator.max_turns_per_hour, which stops self-started turns by itself: a guard at
        # or above it could never fire.
        turn_cap = int(b.get("hourly_coordinator_turns") or default_turn_cap(cfg))
        repeat_cap = int(b.get("hourly_coordinator_repeats", 4))
        shares += [_share(len(selfs), turn_cap), _share(same, repeat_cap)]
        if len(selfs) > turn_cap:
            _raise(g, "red", f"runaway guard: {len(selfs)} self-started coordinator turns in the last hour "
                             f"(limit {turn_cap}; turns for user messages not counted)")
        if same >= repeat_cap:
            _raise(g, "red", f"runaway guard: {same} self-started coordinator turns in the last hour met the "
                             f"same evidence (limit {repeat_cap - 1}); resumes automatically as the hour rolls over")
    else:
        shares += [_share(thrash, thrash_cap), _share(last_h, ceiling)]
    if not virtual and thrash > thrash_cap:
        _raise(g, "red", f"runaway guard: coordinator spent ${thrash:.2f} on self-started turns in the last hour "
                         f"(limit ${thrash_cap:.0f}; turns for user messages not counted)")
    if last_h > ceiling and not virtual:
        _raise(g, "red", f"runaway guard: ${last_h:.2f} spent in the last hour (long runs pro rata), ceiling ${ceiling:.2f}/h; "
                         f"resumes automatically as the hour rolls over")
    g.numbers["guard_ratio"] = round(max(shares, default=0.0), 3)

    if g.level == "yellow":
        # On a plan the headroom to the line already sets the workers; a deep run burns it fastest.
        g.max_tier = "standard"
        if g.regime == "caps":
            g.max_parallel = max(1, g.max_parallel // 2 or 1)
    elif g.level == "orange":
        # On a plan the headroom may already allow no new start at all.
        g.max_tier, g.max_parallel, g.allow_optional = "light", min(g.max_parallel, 1), False
    elif g.level == "red":
        g.max_tier, g.max_parallel, g.allow_optional, g.allow_new_work = "light", 0, False, False
    return g


def _share(value: float, limit: float) -> float:
    """`value` as a share of `limit`; a limit of 0 or less trips on any value (capped for JSON)."""
    return min(value / limit, 99.0) if limit > 0 else (99.0 if value > 0 else 0.0)


def default_turn_cap(cfg: dict) -> int:
    """The plan turn guard's default: 80% of coordinator.max_turns_per_hour, so it can fire before
    that limit holds the turns back."""
    return max(int(int((cfg.get("coordinator") or {}).get("max_turns_per_hour", 30)) * 0.8), 1)


def task_default_usd(cfg: dict, tier: str | None) -> float:
    """A task's per-run dollar cap when it was made without one: budget.task_default_usd[tier], else 8."""
    return float(((cfg.get("budget") or {}).get("task_default_usd") or {}).get(tier or "standard", 8.0))


def dollar_caps(b: dict) -> bool:
    """Whether any dollar cap is set (budget.daily_usd, weekly_usd, global_daily_usd; 0 = unset)."""
    return any(float(b.get(k) or 0) > 0 for k in ("daily_usd", "weekly_usd", "global_daily_usd"))


def _caps(db: DB, g: Gate, b: dict, provider: str, windows: list[Window], last: dict, now: float,
          extra: float = 0.0) -> None:
    """The dollar caps: the project's daily (or budget-day) and rolling 7 d spend billed by use, plus
    `extra` (paid-credit spend past a plan window), against budget.daily_usd and weekly_usd, then the
    global cap."""
    # The caps bound the project's dollars, whichever provider spends them. Providers on plan
    # windows are bounded by their windows instead, so their spend does not count here.
    day_cap, week_cap = float(b.get("daily_usd") or 0), float(b.get("weekly_usd") or 0)
    # Only spend whose account was billed by use when it was spent counts (billing.py): spend of
    # an account then on a plan stays plan spend after a switch drops its windows, and spend of
    # a usage-billed account stays billed whenever it was. Windows passed in without a recorded
    # reading cover their provider's spend up to now.
    windowed = {w.provider: now for w in windows if w.provider not in last}
    live = in_flight(db, exclude={p for p, until in windowed.items() if until >= now}, billed_at=now) + extra
    d = db.spent_since(now - DAY, exclude=windowed, billed=True) + live
    w7 = db.spent_since(now - WEEK, exclude=windowed, billed=True) + live
    g.numbers.update({"spent_24h": round(d, 2), "spent_7d": round(w7, 2), "in_flight": round(live, 2),
                      "estimated_24h": round(db.spent_since(now - DAY, exclude=windowed, estimated_only=True,
                                                            billed=True), 2),
                      "daily_cap": day_cap, "weekly_cap": week_cap})
    # The daily cap counts the fixed budget day (budget.day_start in budget.timezone) once one is
    # set; without one, the last 24 h. The weekly cap stays rolling.
    day = gcap.day_bounds(b, now)
    today, label = d, "24h"
    if day:
        today, label = db.spent_since(day[0], exclude=windowed, billed=True) + live, "today"
        g.numbers.update({"spent_today": round(today, 2), "day_start": day[0], "day_end": day[1]})
    ratio = max(today / day_cap if day_cap else 0.0, w7 / week_cap if week_cap else 0.0)
    g.numbers["cap_ratio"] = round(ratio, 3)
    if ratio >= 1.0:
        # A cap of 0 is unset: only the caps set are named.
        set_caps = ([f"${today:.2f}/{label} of ${day_cap:.0f}"] if day_cap else []) + \
                   ([f"${w7:.2f}/7d of ${week_cap:.0f}"] if week_cap else [])
        _raise(g, "red", "cap reached: " + ", ".join(set_caps))
    elif ratio >= 0.85:
        _raise(g, "orange", f"{ratio:.0%} of cap used")
    elif ratio >= 0.6:
        _raise(g, "yellow", f"{ratio:.0%} of cap used")
    _global(db, g, b, provider, day, now, extra)


def _paid_since(db: DB, provider: str, rows: list[dict], line: float, now: float) -> float:
    """When the account first read at the line in the current period of the windows `rows`: its
    spend from then on is paid-credit spend."""
    first = now
    for r in rows:
        got = db.one("SELECT MIN(ts) t FROM snapshots WHERE provider=? AND window=? AND utilization>=? "
                     "AND ABS(COALESCE(resets_at,0)-?)<600", (provider, r["window"], line, r["resets_at"] or 0))
        if got and got["t"] is not None:
            first = min(first, float(got["t"]))
    return first


def _global(db: DB, g: Gate, b: dict, provider: str, day: tuple[float, float] | None, now: float,
            extra: float = 0.0) -> None:
    """Red once the account's spend today on `provider`, across every project tt-project can see
    (globalcap.total), reaches budget.global_daily_usd (0 = off). It only ever closes the gate: a
    total that cannot be worked out leaves the project caps alone in charge. Red ends at the day's
    reset, when the total starts again from zero; its alert clears with it."""
    cap = float(b.get("global_daily_usd") or 0)
    if cap <= 0:
        return
    start, end, rolling = gcap.window(b, now)
    try:
        t = gcap.total(db, provider, start, end, now, rolling=rolling)
    except Exception as e:
        g.reasons.append(f"global daily total unavailable ({type(e).__name__}); the project caps still apply")
        return
    t = {**t, "usd": t["usd"] + extra}   # paid-credit spend past a plan window counts too
    g.numbers.update({"global_today": round(t["usd"], 2), "global_cap": cap, "global_stale": t["stale"],
                      "global_includes": t["includes"], "global_resets_at": end if day else None,
                      **{f"global_{k}": round(t.get(k) or 0, 2) for k in ("plan_usd", "unbilled_usd", "stale_usd")}})
    if t["usd"] >= cap:
        when = f"; new work resumes at the reset in {max(end - now, 0) / HOUR:.1f} h" if day else ""
        stale = f" ({len(t['stale'])} machine{'s' if len(t['stale']) != 1 else ''} stale)" if t["stale"] else ""
        _raise(g, "red", f"global daily cap reached: {gcap.money(t['usd'])} of ${cap:.0f} today across the "
                         f"account{stale}{when}")


def _plan(db: DB, g: Gate, provider: str, plan: list[Window], line: float, most: int, now: float,
          paid: bool = False, cfg: dict | None = None) -> None:
    """Run all `most` workers below `line`; hold back only where running work would reach it.

    Unused capacity is lost at each reset, so nothing spreads use evenly over a window. Running work
    keeps burning after it starts, though. For each window, `per_worker` is the measured burn
    (points of the window per hour, account-wide) over the time-weighted mean of this project's
    workers while it was measured, at least 1, so burn from other sessions on the account counts
    against this project. `horizon` is how long a run here usually lasts, at most until the reset.
    Each worker, running or about to start, may add `per_worker x horizon` points before it ends.
    The project runs as many workers as fit in the headroom to the line: all of them until the
    last stretch, fewer there. Once those slots are all busy no new run starts, still yellow; orange
    only when the running ones alone may reach the line, or not even one run fits. At the line
    nothing new starts until the window resets. Running work is never stopped. Before any burn
    is measured all workers may run, except within LINE_MARGIN_PCT of the line, where one does.
    """
    running = db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='running' AND role!='coordinator'",
                     (provider,))["n"]
    horizon = run_horizon(db, provider, now)
    allowed, rows = most, []
    for w in plan:
        hours_left = max((w.resets_at - now) / HOUR, 0.05) if w.resets_at else None
        readings = _readings(db, provider, w.window, w.resets_at, now, w.account or None)
        burn = _slope(readings)
        mean = None if burn is None else avg_running(db, provider, float(readings[0]["ts"]), now)
        per = None if burn is None else burn / mean
        span = min(horizon, hours_left) if hours_left else horizon
        add = None if per is None else per * span          # points one worker may still add
        headroom = max(line - w.utilization, 0.0)
        fit = min(most, int(headroom / add)) if add else most
        row = {"window": w.window, "utilization": round(w.utilization, 1), "resets_at": w.resets_at,
               "hours_left": round(hours_left, 2) if hours_left else None, "headroom": round(headroom, 1),
               "burn_per_h": None if burn is None else round(burn, 2),
               "avg_running": None if mean is None else round(mean, 2),
               "per_worker_per_h": None if per is None else round(per, 2), "horizon_h": round(span, 2),
               "running_add": None if add is None else round(running * add, 1), "allowed": fit}
        rows.append(row)
        if paid:
            # Paid credits carry the account past the window, so it holds nothing back; the dollar
            # caps bound the spend past the line instead (evaluate).
            if w.utilization >= line:
                g.reasons.append(f"{w.window} window at {w.utilization:.0f}%, past the {line:.0f}% line: paid "
                                 f"credits let the account go on; the dollar caps apply")
            row["allowed"] = most
        elif w.utilization >= line:
            resume = f"resumes at {timefmt.long(w.resets_at, cfg)}" if w.resets_at else "resumes when the window resets"
            _raise(g, "red", f"{w.window} window at {w.utilization:.0f}%; the project stops at {line:.0f}% "
                             f"and {resume}")
            row["allowed"] = 0
        elif add is None:
            if headroom <= LINE_MARGIN_PCT:
                _raise(g, "orange", f"{w.window} window at {w.utilization:.0f}%, just under the {line:.0f}% line "
                                    f"and no burn measured yet: one worker at a time")
                row["allowed"] = 1
        elif fit < most and (fit < running or fit == 0):
            # Every slot the headroom allows being busy is the plan working (yellow); orange is
            # only running work that alone may reach the line, or no room for even one run.
            held = (f"the {running} running workers may add ~{running * add:.1f} points before they end"
                    if running else f"one more run may add ~{add:.1f} points")
            _raise(g, "orange", f"{w.window} window at {w.utilization:.0f}%, {headroom:.1f} points under the "
                                f"{line:.0f}% line; {held}: no new starts")
        elif fit < most:
            _raise(g, "yellow", f"{w.window} window at {w.utilization:.0f}%, {headroom:.1f} points under the "
                                f"{line:.0f}% line: room for {fit} workers (~{add:.1f} points each before they end)")
        allowed = min(allowed, row["allowed"])
    worst = min(rows, key=lambda r: (r["allowed"], r["headroom"]))
    g.max_parallel = allowed
    g.numbers.update({"window": worst["window"], "utilization": worst["utilization"], "limit": line,
                      "resets_at": worst["resets_at"], "headroom": worst["headroom"], "running": running,
                      "starts": allowed > running or 0 < allowed >= most, "plan": rows})


def run_horizon(db: DB, provider: str, now: float) -> float:
    """Hours a worker run on `provider` usually lasts in this project: the median of its last 20
    runs that ended in the past week, RUN_HORIZON_DEFAULT_S before any did."""
    rows = db.q("SELECT started, ended FROM runs WHERE provider=? AND role!='coordinator' AND started IS NOT NULL "
                "AND ended IS NOT NULL AND ended>=? ORDER BY ended DESC LIMIT 20", (provider, now - WEEK))
    lengths = sorted(max(float(r["ended"]) - float(r["started"]), 0.0) for r in rows)
    if not lengths:
        return RUN_HORIZON_DEFAULT_S / HOUR
    return max(lengths[len(lengths) // 2], 300.0) / HOUR


def burn_rate(db: DB, provider: str, window: str, resets_at: float | None, now: float,
              account: str | None = None) -> float | None:
    """Points of the window used per hour, from this project's readings in the current period.

    None until two readings at least five minutes apart exist: no reading, no guess.
    """
    return _slope(_readings(db, provider, window, resets_at, now, account))


def _readings(db: DB, provider: str, window: str, resets_at: float | None, now: float,
              account: str | None = None) -> list:
    # Only readings of the current period count (same reset), so the span stops at its start; with
    # an account, only that account's (another account's window is another window).
    span = min(WINDOW_HOURS.get(window, 168.0) * HOUR / 4, BURN_SPAN_MAX_S)
    return db.q("SELECT ts, utilization FROM snapshots WHERE provider=? AND window=? AND ts>=? AND "
                "(resets_at=? OR (? IS NULL AND resets_at IS NULL)) AND (? IS NULL OR COALESCE(account,'')=?) "
                "ORDER BY ts", (provider, window, now - span, resets_at, resets_at, account, account))


def _slope(rows: list) -> float | None:
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 300:
        return None
    # Least-squares slope over every reading: readings come in whole percents, so a two-point
    # estimate jumps with each new reading and the room for workers would flap with it.
    ts = [(float(r["ts"]) - float(rows[0]["ts"])) / HOUR for r in rows]
    us = [float(r["utilization"]) for r in rows]
    mt, mu = sum(ts) / len(ts), sum(us) / len(us)
    var = sum((t - mt) ** 2 for t in ts)
    if var <= 0:
        return None
    return max(sum((t - mt) * (u - mu) for t, u in zip(ts, us)) / var, 0.0)


def avg_running(db: DB, provider: str, since: float, now: float, floor: float = 1.0) -> float:
    """Time-weighted mean of this project's workers on `provider` between `since` and `now`, at
    least `floor`: the burn over that span came from them."""
    if now <= since:
        return 1.0
    busy = 0.0
    for r in db.q("SELECT started, ended, status FROM runs WHERE provider=? AND role!='coordinator' "
                  "AND started IS NOT NULL AND started<? AND (status='running' OR ended>?)",
                  (provider, now, since)):
        start = float(r["started"])
        end = now if r["status"] == "running" or r["ended"] is None else float(r["ended"])
        busy += max(min(end, now) - max(start, since), 0.0)
    return max(busy / (now - since), floor)


def note_account(db: DB, provider: str, account: str) -> None:
    """Record the account `provider` is logged in as now, so a switch counts before its next run."""
    if account and (db.kv(ACCOUNT_KV + provider) or {}).get("account") != account:
        db.set_kv(ACCOUNT_KV + provider, {"account": account, "ts": time.time()})


def current_accounts(db: DB) -> dict[str, str]:
    """The account each provider is on now: the newer of the daemon's last look and the account its
    latest run started under (the provider's own account label: login, organization, billing)."""
    seen: dict[str, tuple[float, str]] = {}
    for r in db.q("SELECT provider, account, started FROM runs WHERE id IN (SELECT MAX(id) FROM runs "
                  "WHERE account IS NOT NULL AND account!='' GROUP BY provider)"):
        seen[r["provider"]] = (float(r["started"] or 0), r["account"])
    for r in db.q("SELECT key, value FROM kv WHERE key LIKE ?", (ACCOUNT_KV + "%",)):
        v = json.loads(r["value"] or "{}")
        prov = r["key"][len(ACCOUNT_KV):]
        if v.get("account") and float(v.get("ts") or 0) >= seen.get(prov, (0.0, ""))[0]:
            seen[prov] = (float(v["ts"]), v["account"])
    return {p: a for p, (_, a) in seen.items()}


def _latest_readings(db: DB, since: float) -> list:
    """The latest reading of every plan window since `since`, per provider, account and window."""
    return db.q("SELECT s.* FROM snapshots s JOIN (SELECT provider, COALESCE(account,'') acct, window, MAX(ts) mts "
                "FROM snapshots WHERE ts>=? GROUP BY provider, acct, window) m ON s.provider=m.provider AND "
                "COALESCE(s.account,'')=m.acct AND s.window=m.window AND s.ts=m.mts", (since,))


def live_readings(db: DB, rows: list) -> list:
    """Of the latest readings `rows`, the one per provider and window that counts now.

    Plan windows belong to an account: once the provider is on another one (another login or
    organization, or a usage-billed account, which has no windows), the old account's readings stop
    counting at once. Readings recorded without an account count until a keyed reading arrives after
    them, or until PLAN_LAPSE_RUNS paid runs of the current account ended after them without a
    reading (it is billed by use). With no account known, the latest keyed reading's account counts.
    """
    current, newest, best = current_accounts(db), {}, {}
    for r in sorted(rows, key=lambda r: float(r["ts"])):
        if r["account"]:
            newest[r["provider"]] = (float(r["ts"]), r["account"])
    for r in rows:
        prov, ts = r["provider"], float(r["ts"])
        if r["account"]:
            ok = r["account"] == (current.get(prov) or newest[prov][1])
        else:
            ok = ts >= newest.get(prov, (0.0, ""))[0] and not (current.get(prov) and db.one(
                "SELECT COUNT(*) n FROM runs WHERE provider=? AND account=? AND status='ok' AND ended>? AND cost_usd>0",
                (prov, current[prov], ts))["n"] >= PLAN_LAPSE_RUNS)
        key = (prov, r["window"])
        if ok and (key not in best or ts > float(best[key]["ts"])):
            best[key] = r
    return list(best.values())


def plan_windows(db: DB, now: float | None = None) -> list[Window]:
    """The latest reading of every plan window the provider's current account reported in the last week.

    Being on a plan is a fact about the account, so an old reading still says which regime applies;
    the headroom to the line works from the readings themselves. A window whose reset has passed since its last
    reading has started a new period: it counts as empty until the next reading says otherwise.
    """
    now = now or time.time()
    rows = live_readings(db, _latest_readings(db, now - PLAN_MEMORY_S))
    out = []
    for r in rows:
        util, resets = float(r["utilization"] or 0), r["resets_at"]
        if resets and now >= resets:
            hours = WINDOW_HOURS.get(r["window"], 168.0)
            while resets <= now:
                resets += hours * HOUR
            util = 0.0
        out.append(Window(r["provider"], r["window"], util, resets, r["account"] or ""))
    return out


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


def wake_tier(tier: str, prev: dict, cfg: dict | None = None, harness=None) -> str | None:
    """The tier of the run that wakes a task whose last hand-off (`prev`) was `waiting`, or None
    when the run is no wake. Most wakes only check whether the wait is over, so a hand-off that
    names what it waits on wakes at light unless it asks for a `wake_tier`; never above the task's
    own tier. A hand-off whose `next_step` is one mechanical step (say `push`; see mechanical_step)
    wakes at light: that run does the step itself. A `wake_tier` above light, an escalated light
    wake (`escalated_wake`) or a `next_step` too long to be one step means judgment: the hand-off's
    `wake_tier` wins, and without one a long `next_step` wakes at the task's own tier. A wait that a
    wake already handed back unchanged (`stale_wakes`) wakes at light whatever it asks: only a light
    wake that found work and escalated runs above it. So does a wait on another project's reply
    (ackwait.is_probe) or on a `ttp checks --result` / `ttp push --result` (result_wait): the daemon
    escalates failed checks and a push that did not land itself."""
    if not isinstance(prev, dict) or prev.get("status") != "waiting":
        return None
    tier = tier if tier in TIER_ORDER else "standard"
    if stale_wakes(prev) and not prev.get("escalated_wake"):
        return TIER_ORDER[0]
    if ackwait.is_probe(prev.get("retry_when"), cfg, harness) and not prev.get("escalated_wake"):
        return TIER_ORDER[0]   # another project's reply: its on_pass_cmd settles it, a wake only reads it
    if result_wait(prev.get("retry_when")) and not prev.get("escalated_wake"):
        return TIER_ORDER[0]   # a passed result only needs reporting; a failed one comes escalated
    want = "light" if mechanical_step(prev) else prev.get("wake_tier")
    if want not in TIER_ORDER:
        want = "light" if (prev.get("retry_when") or prev.get("waiting_for")) and not next_step(prev) else tier
    return min(want, tier, key=TIER_ORDER.index)


def result_wait(probe) -> tuple[str, str] | None:
    """("checks", run dir) or ("push", marker) when a retry_when is one `ttp checks --result <run dir>` or
    `ttp push --result <marker> [--pushed]` command, else None. Both exit 1 while the result is pending,
    so the daemon wakes nothing before it is final."""
    if not isinstance(probe, str):
        return None
    try:
        words = shlex.split(probe)
    except ValueError:
        return None
    if len(words) < 4 or os.path.basename(words[0]) != "ttp" or words[1] not in ("checks", "push"):
        return None
    args = [w for w in words[2:] if not (words[1] == "push" and w == "--pushed")]
    return (words[1], args[1]) if len(args) == 2 and args[0] == "--result" and not args[1].startswith("-") else None


def stale_wakes(prev: dict) -> int:
    """How many wakes in a row handed this wait back unchanged (the daemon's count), or 0."""
    try:
        return max(int(prev.get("stale_wakes") or 0), 0) if isinstance(prev, dict) else 0
    except (TypeError, ValueError):
        return 0


# A next_step longer than this is a plan, not one mechanical step.
NEXT_STEP_MAX = 200


def next_step(prev: dict) -> str:
    """The step a waiting hand-off left for after its wait (`next_step`), or ''."""
    step = prev.get("next_step") if isinstance(prev, dict) else None
    return " ".join(step.split())[:NEXT_STEP_MAX] if isinstance(step, str) else ""


def mechanical_step(prev: dict) -> str:
    """The hand-off's `next_step` when a light wake can do it, or '': it is short, the hand-off asks
    for no `wake_tier` above light, and it is not a light wake that already asked for more."""
    step = prev.get("next_step") if isinstance(prev, dict) else None
    if not isinstance(step, str) or len(" ".join(step.split())) > NEXT_STEP_MAX or prev.get("escalated_wake"):
        return ""
    return next_step(prev) if prev.get("wake_tier") not in TIER_ORDER[1:] else ""


# Not .txt: CMakeLists.txt and requirements.txt are build and dependency changes.
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")


# Test files: a directory of these names, or a file name of these globs.
TEST_DIRS = {"test", "tests", "__tests__"}
TEST_NAMES = ("test_*", "*_test.*", "*.test.*", "*_spec.*", "*.spec.*", "conftest.py")
# A code file whose path has a part matching one of these globs holds state, the database, a push,
# spend, a spend cap, a release or an upgrade: its review runs standard however small the change. `review.risky_names`
# replaces the list ([] turns it off); `review.risky_paths` adds a project's own paths.
RISKY_NAMES = ("*state*", "db", "db.*", "*_db.*", "*database*", "*schema*", "*migrat*", "push*", "*budget*",
               "*billing*", "*spend*", "*cap.*", "*upgrade*", "*release*")


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    return bool(TEST_DIRS & set(parts[:-1])) or any(fnmatch.fnmatch(parts[-1], g) for g in TEST_NAMES)


def review_pick(changes: dict[str, int | None], cfg: dict) -> tuple[str, str]:
    """The tier a review of `changes` (path -> changed lines, None for a binary file) needs and the
    rule that picked it. Standard for a risky path or name; light when only docs and tests change,
    or when at most light_max_lines non-doc lines change; standard otherwise. Doc lines do not
    count toward the size: prose next to a small code change is not risk. With light_paths set, a
    non-doc file must also match one of its globs for the diff to go light."""
    rules = cfg.get("review", {})
    risky = rules.get("risky_paths") or []
    hit = next((path for path in changes if any(fnmatch.fnmatch(path, g) for g in risky)), None)
    if hit:
        return "standard", f"risky path {hit}"
    code = {path: n for path, n in changes.items() if not path.lower().endswith(DOC_SUFFIXES)}
    names = rules.get("risky_names")
    names = RISKY_NAMES if names is None else names
    hit = next((path for path in code if not is_test_path(path)
                and any(fnmatch.fnmatch(part, g) for part in path.lower().split("/") for g in names)), None)
    if hit:
        return "standard", f"risky name {hit}"
    if not code:
        return "light", "docs only"
    allowed = rules.get("light_paths") or []
    if allowed and not all(any(fnmatch.fnmatch(path, g) for g in allowed) for path in code):
        return "standard", "outside light_paths"
    if all(is_test_path(path) for path in code):
        return "light", "tests and docs only"
    if any(n is None for n in code.values()):
        return "standard", "binary file"
    lines, limit = sum(code.values()), int(rules.get("light_max_lines", 80))
    return ("light", f"{lines} lines <= {limit}") if lines <= limit else ("standard", f"{lines} lines > {limit}")


def review_tier(changes: dict[str, int | None], cfg: dict) -> str:
    return review_pick(changes, cfg)[0]


def windows_from_snapshots(db: DB, now: float | None = None) -> list[Window]:
    """Latest reading per (provider, window) of the provider's current account, ignoring readings too
    old to trust."""
    now = now or time.time()
    rows = live_readings(db, _latest_readings(db, now - SNAPSHOT_FRESH_S))
    return [Window(r["provider"], r["window"], float(r["utilization"] or 0), r["resets_at"], r["account"] or "")
            for r in rows]


def plan_providers(db: DB, now: float | None = None) -> dict[str, float]:
    """Providers with any window reading in the last week (billed by plan windows, not dollars),
    mapped to the time of their last reading."""
    now = now or time.time()
    return {r["provider"]: float(r["ts"]) for r in
            db.q("SELECT provider, MAX(ts) ts FROM snapshots WHERE ts>=? GROUP BY provider", (now - WEEK,))}


def account_billed_since(db: DB, provider: str, last_reading: float) -> float | None:
    """Since when the account `provider` is on now is known to be billed by use, or None while its
    kind is unknown or it is a plan account whose own readings are missing. Known billed: at least
    PLAN_LAPSE_RUNS of its own paid runs ended after the provider's last plan reading without one of
    their own (billing.py's lapse rule, per account). A recorded plan -> billed budget change is no
    evidence: budgetkind derives that kind from this very gate (its dollar-caps treatment)."""
    account = current_accounts(db).get(provider)
    if not account:
        return None
    runs = db.one("SELECT COUNT(*) n, MIN(started) first FROM runs WHERE provider=? AND account=? AND status='ok' "
                  "AND ended>? AND cost_usd>0", (provider, account, last_reading))
    return float(runs["first"] or last_reading) if runs["n"] >= PLAN_LAPSE_RUNS else None


def plan_lapsed(db: DB, provider: str, last_reading: float) -> bool:
    """Whether the provider's paid runs since its last window reading say it is no longer on a plan.
    A run's windows are recorded when the daemon processes its end, so only runs that ended after
    the last reading had none. Only successful runs count: one that failed, was cut off or produced
    nothing may simply not have got as far as reporting its windows."""
    return db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='ok' AND ended>? AND cost_usd>0",
                  (provider, last_reading))["n"] >= PLAN_LAPSE_RUNS


def record_windows(db: DB, windows: list[Window]) -> None:
    now = time.time()
    for w in windows:
        db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
             (now, w.provider, w.account, w.window, w.utilization, w.resets_at))


def history(db: DB, days: int = 14) -> dict:
    """Daily spend and daily peak window utilization for the web app's two-week view. The peaks are
    those of the account each provider is on now; other accounts' readings stay in the database."""
    since = time.time() - days * DAY
    spend = db.q("SELECT date(ts,'unixepoch','localtime') d, provider, ROUND(SUM(usd),2) usd, "
                 "SUM(estimated) est FROM ledger WHERE ts>=? GROUP BY d, provider ORDER BY d", (since,))
    live = {(w.provider, w.account) for w in plan_windows(db)}
    peaks = [{k: r[k] for k in ("d", "provider", "window", "peak", "avg")} for r in db.q(
        "SELECT date(ts,'unixepoch','localtime') d, provider, COALESCE(account,'') acct, window, "
        "ROUND(MAX(utilization),1) peak, ROUND(AVG(utilization),1) avg FROM snapshots WHERE ts>=? "
        "GROUP BY d, provider, acct, window ORDER BY d", (since,)) if (r["provider"], r["acct"]) in live]
    by_source = db.q("SELECT source, provider, ROUND(SUM(usd),2) usd, COUNT(*) n FROM ledger WHERE ts>=? "
                     "GROUP BY source, provider ORDER BY usd DESC LIMIT 40", (time.time() - WEEK,))
    cache = coordinator_cache(db)
    for s in cache.values():   # the web app shows this as is: one wording with the CLI and daily review
        s["line"] = coordinator_cache_line(s)
    return {"daily_spend": spend, "window_peaks": peaks, "top_sources_7d": by_source,
            "coordinator_cache": cache}


NO_WORK_STATUSES = ("auth",)   # a run that ended before any model call, whatever tokens it shows


def no_work(r) -> bool:
    """A run that made no model call: it failed sign-in, or read and wrote no prompt tokens at all.
    It costs a wake, not context, so per-run averages leave it out (`reread_stats` counts it apart)."""
    return (r["status"] in NO_WORK_STATUSES
            or not ((r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0)))


def reread_stats(db: DB, days: float = 7, now: float | None = None, top: int = 10,
                 since: float | None = None, until: float | None = None) -> dict:
    """Context re-read (cache-read) tokens of the runs started in the last `days` (or between `since`
    and `until`): per run (mean and median), per $, their share of the weighted tokens, model calls
    per run and the first call's context (the prefix every later call re-reads), by role, task kind,
    tier and effort, and the top runs. Runs with no model call (`no_work`) are counted apart and left
    out of every average. A long run re-reads its whole context on every call, so this is what
    trimming output and splitting long tasks cut. `ttp stats --split-at` compares before and after."""
    now = now or time.time()
    since = now - days * 86400 if since is None else since
    until = now if until is None else until
    w = TOKEN_WEIGHTS
    rows = db.q("SELECT r.id, r.task, r.role, r.effort, r.status, r.started, r.ended, r.cost_usd, r.input_tokens, "
                "r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, r.note, t.kind, t.tier, t.title "
                "FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.started>=? AND r.started<?", (since, until))

    def total(rs: list, k: str) -> float:
        return sum(r[k] or 0 for r in rs)

    def mean(xs: list) -> float | None:
        return round(sum(xs) / len(xs), 1) if xs else None

    def summary(all_rs: list) -> dict:
        rs = [r for r in all_rs if not no_work(r)]
        n, usd, cr = len(rs), total(rs, "cost_usd"), int(total(rs, "cache_read_tokens"))
        units = (total(rs, "input_tokens") * w["input"] + total(rs, "output_tokens") * w["output"]
                 + cr * w["cache_read"] + total(rs, "cache_write_tokens") * w["cache_write"])
        tel = [(_note(r["note"]).get("cache_telemetry") or {}) for r in rs]
        calls = [t["calls"] for t in tel if isinstance(t, dict) and isinstance(t.get("calls"), int) and t["calls"] > 0]
        first = [t["first_context"] for t in tel
                 if isinstance(t, dict) and isinstance(t.get("first_context"), int) and t["first_context"] > 0]
        idle = [r for r in all_rs if no_work(r)]
        return {"runs": n, "usd": round(usd, 2), "cache_read": cr, "per_run": round(cr / n) if n else 0,
                "median": round(statistics.median(r["cache_read_tokens"] or 0 for r in rs)) if rs else 0,
                "per_usd": round(cr / usd) if usd else 0, "max": max((r["cache_read_tokens"] or 0 for r in rs), default=0),
                "share": round(cr * w["cache_read"] / units, 3) if units else 0.0,
                "calls_per_run": mean(calls), "calls_runs": len(calls),
                "first_context": round(sum(first) / len(first)) if first else None, "first_context_runs": len(first),
                "no_work": len(idle), "no_work_usd": round(total(idle, "cost_usd"), 2)}

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["role"], r["kind"] or "-", r["tier"] or "-", r["effort"] or "-"), []).append(r)
    by = [{"role": k[0], "kind": k[1], "tier": k[2], "effort": k[3], **summary(v)} for k, v in groups.items()]
    by.sort(key=lambda g: (-g["cache_read"], -g["no_work"]))
    worst = sorted((r for r in rows if not no_work(r)), key=lambda r: -(r["cache_read_tokens"] or 0))[:top]
    # Runs the split steer reached (daemon._suggest_split): what they re-read after it, how they handed off.
    after, handoffs = [], {}
    for r in rows:
        split = _note(r["note"]).get("split")
        if not isinstance(split, dict):
            continue
        after.append(max(0, (r["cache_read_tokens"] or 0) - int(split.get("cache_read") or 0)))
        how = split.get("handoff") or ("running" if r["status"] == "running" else "none")
        handoffs[how] = handoffs.get(how, 0) + 1
    steered = {"runs": len(after), "after_avg": round(sum(after) / len(after)) if after else 0,
               "after_max": max(after, default=0), "handoffs": dict(sorted(handoffs.items(), key=lambda kv: -kv[1]))}
    return {"days": round((until - since) / 86400, 2), "since": since, "until": until, **summary(rows), "groups": by,
            "steered": steered,
            "top": [{"run": r["id"], "task": r["task"], "role": r["role"], "kind": r["kind"], "tier": r["tier"],
                     "effort": r["effort"], "usd": round(r["cost_usd"] or 0, 2), "cache_read": r["cache_read_tokens"] or 0,
                     "minutes": round(((r["ended"] or now) - (r["started"] or now)) / 60),
                     "title": (r["title"] or "")[:60]} for r in worst]}


def reread_split(db: DB, at: float, since: float, until: float | None = None, now: float | None = None,
                 top: int = 10) -> dict:
    """`reread_stats` before and after `at` (a change landing), with each group's per-run change.
    Runs with no model call stay out of both sides' averages, so a burst of failed sign-ins after
    the change does not read as a rise in context."""
    now = now or time.time()
    before = reread_stats(db, now=now, top=top, since=since, until=at)
    after = reread_stats(db, now=now, top=top, since=at, until=until)
    key = lambda g: (g["role"], g["kind"], g["tier"], g["effort"])
    old = {key(g): g for g in before["groups"]}

    def change(a, b):
        return round(100 * (b - a) / a) if a and b is not None else None

    delta = [{"role": g["role"], "kind": g["kind"], "tier": g["tier"], "effort": g["effort"],
              "runs": (old[key(g)]["runs"], g["runs"]), "per_run": (old[key(g)]["per_run"], g["per_run"]),
              "per_run_pct": change(old[key(g)]["per_run"], g["per_run"]),
              "median_pct": change(old[key(g)]["median"], g["median"])}
             for g in after["groups"] if key(g) in old and g["runs"] and old[key(g)]["runs"]]
    return {"at": at, "before": before, "after": after, "per_run_pct": change(before["per_run"], after["per_run"]),
            "median_pct": change(before["median"], after["median"]), "groups": delta}


def reread_text(s: dict, tz=None) -> str:
    """`reread_stats` for people: totals, the groups and the top runs, one line each."""
    m = lambda n: f"{n / 1e6:.2f} M"
    when = (f"{timefmt.long(s['since'], tz)} to {timefmt.long(s['until'], tz)}" if tz is not None
            else f"last {s['days']:g} days")
    extra = lambda g: "".join((f", {g['calls_per_run']:g} calls per run" if g["calls_per_run"] is not None else "",
                               f", first call {g['first_context'] / 1e3:.0f} k" if g["first_context"] is not None else "",
                               f"; {g['no_work']} runs with no model call left out" if g["no_work"] else ""))
    lines = [f"Context re-reads, {when}: {s['runs']} runs, ${s['usd']:.2f}, {m(s['cache_read'])} "
             f"cache-read tokens ({m(s['per_run'])} per run, median {m(s['median'])}, {m(s['per_usd'])} per $, "
             f"{s['share']:.0%} of weighted tokens{extra(s)})", "By role / kind / tier / effort:"]
    lines += [f"  {g['role']}/{g['kind']}/{g['tier']}/{g['effort']}: {g['runs']} runs, ${g['usd']:.2f}, "
              f"{m(g['cache_read'])} ({m(g['per_run'])} per run, median {m(g['median'])}, max {m(g['max'])}{extra(g)})"
              for g in s["groups"]]
    st = s.get("steered") or {}
    if st.get("runs"):
        lines.append(f"Split steer: {st['runs']} runs steered, re-read after it {m(st['after_avg'])} avg (max "
                     f"{m(st['after_max'])}); handed off: "
                     + ", ".join(f"{k} {v}" for k, v in st["handoffs"].items()))
    else:
        lines.append("Split steer: no run steered")
    lines.append("Top runs:")
    lines += [f"  run {t['run']} (#{t['task'] or '-'} {t['kind'] or t['role']}/{t['tier'] or '-'}/{t['effort'] or '-'}, "
              f"{t['minutes']} min, ${t['usd']:.2f}): {m(t['cache_read'])} {t['title']}".rstrip() for t in s["top"]]
    return "\n".join(lines)


# What a tool's output was, for `tool_output_stats`
SEARCH_COMMANDS = {"grep", "egrep", "rg", "ag", "find", "fd", "ls", "git-grep"}


def tool_category(name: str, inp: dict) -> str:
    """'file read (Read)', 'file read (Bash)' (cat, sed -n, head, tail or nl of a file), 'search',
    'other bash' or 'other'."""
    from . import hook
    if name == "Read":
        return "file read (Read)"
    if name in ("Grep", "Glob", "LS"):
        return "search"
    if name != "Bash":
        return "other"
    cmd = str(inp.get("command") or "")
    try:
        argvs = hook._segments(hook._split(cmd)[0])
    except Exception:
        return "other bash"
    names = [os.path.basename(a[0]) + ("-grep" if a[:2] == ["git", "grep"] else "") for a in argvs]
    for argv, n in zip(argvs, names):
        files = [w for w in argv[1:] if not w.startswith("-")]
        if n in ("cat", "nl", "head", "tail") and files \
                or n == "sed" and not any(w.startswith(("-i", "--in-place")) for w in argv) \
                and any(w in ("--quiet", "--silent") or re.fullmatch(r"-[nEsrzu]*n[nEsrzu]*e?", w) for w in argv):
            return "file read (Bash)"
    return "search" if names and names[0] in SEARCH_COMMANDS else "other bash"


def _result_chars(block: dict) -> int:
    c = block.get("content")
    if isinstance(c, list):
        return sum(len(str(x.get("text") or "")) for x in c if isinstance(x, dict))
    return len(str(c or ""))


def tool_output_stats(db: DB, runs_dir: Path, days: float = 7, now: float | None = None) -> dict:
    """Characters of tool output the runs started in the last `days` took into their context, by
    category (tool_category), from each run's transcript. Every character stays in the context and is
    re-read on each later call, so the file-read share is what narrow reads cut."""
    now = now or time.time()
    by: dict[str, dict[str, int]] = {}
    runs = 0
    for r in db.q("SELECT id, dir FROM runs WHERE started>=?", (now - days * 86400,)):
        out = (Path(r["dir"]) if r["dir"] else runs_dir / str(r["id"])) / "output.jsonl"
        try:
            lines = out.read_text(errors="replace").splitlines()
        except OSError:
            continue
        runs += 1
        uses: dict[str, str] = {}
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") if isinstance(e, dict) else None
            for b in msg.get("content") or [] if isinstance(msg, dict) and isinstance(msg.get("content"), list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("id"):
                    uses[b["id"]] = tool_category(str(b.get("name") or ""), b.get("input") if isinstance(b.get("input"), dict) else {})
                elif b.get("type") == "tool_result":
                    g = by.setdefault(uses.get(b.get("tool_use_id"), "other"), {"calls": 0, "chars": 0})
                    g["calls"] += 1
                    g["chars"] += _result_chars(b)
    total = sum(g["chars"] for g in by.values())
    cats = sorted(({"category": k, **v, "share": round(v["chars"] / total, 3) if total else 0.0} for k, v in by.items()),
                  key=lambda g: -g["chars"])
    return {"days": days, "runs": runs, "chars": total, "categories": cats,
            "file_read_share": round(sum(g["chars"] for g in cats if g["category"].startswith("file read"))
                                     / total, 3) if total else 0.0}


def tool_output_text(s: dict) -> str:
    lines = [f"Tool output, last {s['days']:g} days: {s['runs']} run transcripts, {s['chars'] / 1e6:.2f} M chars; "
             f"file reads {s['file_read_share']:.0%}"]
    lines += [f"  {g['category']}: {g['calls']} calls, {g['chars'] / 1e6:.2f} M chars ({g['share']:.0%})"
              for g in s["categories"]]
    return "\n".join(lines)


def reread_split_text(s: dict, tz=None) -> str:
    """`reread_split` for people: the change per group, then each side in full."""
    m = lambda n: f"{n / 1e6:.2f} M"
    pct = lambda v: "n/a" if v is None else f"{v:+d}%"
    b, a = s["before"], s["after"]
    lines = [f"Before / after {timefmt.long(s['at'], tz or 'UTC')}: per run {m(b['per_run'])} -> {m(a['per_run'])} "
             f"({pct(s['per_run_pct'])}), median {m(b['median'])} -> {m(a['median'])} ({pct(s['median_pct'])}); "
             f"runs {b['runs']} -> {a['runs']}, no model call {b['no_work']} -> {a['no_work']} (left out)"]
    lines += [f"  {g['role']}/{g['kind']}/{g['tier']}/{g['effort']}: {g['runs'][0]} -> {g['runs'][1]} runs, "
              f"per run {m(g['per_run'][0])} -> {m(g['per_run'][1])} ({pct(g['per_run_pct'])}), "
              f"median {pct(g['median_pct'])}" for g in s["groups"]]
    return "\n".join(lines + ["", "Before:", reread_text(b, tz or "UTC"), "", "After:", reread_text(a, tz or "UTC")])


def user_turn(run: dict) -> bool:
    """A coordinator turn the daemon started for user messages (an ask's answer is one too): its
    note lists them. A missing or unreadable note counts as a self-started turn."""
    return bool(_note(run["note"]).get("messages"))


def repeated_turns(runs: list) -> int:
    """The most coordinator turns among `runs` that met the same evidence: the same background and
    events (their note's `gate`, turngate.material). Turns without a recorded gate count as none."""
    seen: dict[str, int] = {}
    for r in runs:
        gate = _note(r["note"]).get("gate")
        if isinstance(gate, dict) and gate.get("bg"):
            fp = json.dumps([gate.get("bg"), sorted(gate.get("keys") or [])])
            seen[fp] = seen.get(fp, 0) + 1
    return max(seen.values(), default=0)


def _note(raw) -> dict:
    try:
        note = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def cache_hit(read: float, write: float, fresh: float) -> float | None:
    """Share of a call's prompt tokens read from the provider's cache, or None with no prompt."""
    total = read + write + fresh
    return read / total if total > 0 else None


CACHE_MISS_THRESHOLD = 0.5   # existing status metric; measurement reports may use another threshold
CACHE_TOKEN_KEYS = ("input_tokens", "cache_read_tokens", "cache_write_tokens")


CACHE_LEGACY = "legacy"   # cache_telemetry.source of a turn from before runs recorded it


def _cache_summary(rows: list[dict]) -> dict:
    """Only explicit cache telemetry contributes to hits/misses; retain all raw counters.
    A turn from before the field existed (none recorded, or the backfill could not rebuild it)
    is legacy, not missing: missing means a recorded turn whose counters were incomplete."""
    states = dict.fromkeys(("zero", "partial", "full", "missing", "legacy"), 0)
    known, hits = [], []
    requests_unknown = requests_known = 0
    for r in rows:
        note = _note(r["note"])
        meta = note.get("cache_telemetry") or {}
        if not isinstance(meta, dict):
            meta = {}
        if isinstance(meta.get("requests"), int):
            requests_known += 1
        else:
            requests_unknown += 1
        if "cache_telemetry" not in note or meta.get("source") == CACHE_LEGACY:
            states["legacy"] += 1
            continue
        hit = cache_hit(*(r[k] or 0 for k in ("cache_read_tokens", "cache_write_tokens", "input_tokens")))
        if meta.get("complete") is not True or hit is None or not 0 <= hit <= 1:
            states["missing"] += 1
            continue
        states["zero" if hit == 0 else "full" if hit == 1 else "partial"] += 1
        known.append(r)
        hits.append(hit)
    tokens = lambda rs: {k: sum(r[k] or 0 for r in rs) for k in CACHE_TOKEN_KEYS}
    raw, measured = tokens(rows), tokens(known)
    total = cache_hit(*(measured[k] for k in ("cache_read_tokens", "cache_write_tokens", "input_tokens")))
    sizes = [d["tokens"] for d in (_note(r["note"]).get("digest") for r in rows)
             if isinstance(d, dict) and isinstance(d.get("tokens"), int)]
    return {"turns": len(rows), "known_turns": len(known), "states": states,
            "raw_tokens": raw, "measured_tokens": measured, "requests_unknown_turns": requests_unknown,
            "requests_known_turns": requests_known,
            "hit_pct": round(100 * total) if total is not None else None,
            "miss_turns": sum(h < CACHE_MISS_THRESHOLD for h in hits),
            "usd_per_turn": round(sum(r["cost_usd"] or 0 for r in rows) / len(rows), 4) if rows else None,
            "write_per_turn": round(measured["cache_write_tokens"] / len(known)) if known else None,
            "digest_tokens": round(sum(sizes) / len(sizes)) if sizes else None}


def _thread_turn(r: dict) -> int:
    thread = (_note(r["note"]).get("turn") or {}).get("thread")
    turn = thread.get("turn") if isinstance(thread, dict) else None
    return turn if isinstance(turn, int) else 0


def coordinator_cache(db: DB, now: float | None = None, window_s: float | None = None) -> dict:
    """Token hit denominator = fresh input + cache writes + cache reads (no output tokens).
    A miss is strictly below 50% read; missing metadata or a zero denominator is unknown.
    Zero/partial/full describe reads of 0%, between 0 and 100%, and 100%. Rates are
    token-weighted, not a mean of turn percentages. Raw counters include unknown turns.
    Gaps use the previous completed coordinator turn's end; configured TTL proves nothing
    about retention. `window_s` selects the daily review's own window."""
    now = time.time() if now is None else now
    spans = (("window", window_s),) if window_s is not None else (("24h", DAY), ("3d", 3 * DAY), ("7d", WEEK))
    out = {}
    for label, span in spans:
        rows = db.q("SELECT * FROM (SELECT provider, cost_usd, input_tokens, cache_read_tokens, "
                    "cache_write_tokens, note, started, LAG(COALESCE(ended,started)) OVER "
                    "(ORDER BY started,id) previous_end FROM runs WHERE role='coordinator' "
                    "AND status!='running') WHERE started>=?", (now - span,))
        out[label] = _cache_summary(rows)
        gap = lambda r: r["started"] - r["previous_end"] if r["previous_end"] is not None else None
        out[label]["gap_5_60m"] = _cache_summary([r for r in rows if gap(r) is not None and 300 <= gap(r) <= 3600])
        # Split at the 1 h cache lifetime: sparse turns lower the overall rate with a stable prefix,
        # so a drop is judged on the within-1h rate (daily review).
        out[label]["gap_within_60m"] = _cache_summary([r for r in rows if gap(r) is not None and gap(r) <= 3600])
        out[label]["gap_over_60m"] = _cache_summary([r for r in rows if gap(r) is not None and gap(r) > 3600])
        # Turns that continued the last turn's agent session (cachettl.pick_thread), against the rest.
        out[label]["continued"] = _cache_summary([r for r in rows if _thread_turn(r) >= 2])
    return out


def coordinator_cache_line(s: dict) -> str:
    """Shared status/daily-review wording, including the metric's existing threshold."""
    states = s["states"]
    hit = f"{s['hit_pct']}%" if s["hit_pct"] is not None else "unknown"
    legacy = f", legacy {states['legacy']} (before telemetry)" if states.get("legacy") else ""
    split = [f"{label}: " + ("no turns" if not b["turns"] else
                             f"{b['hit_pct']}%" if b["hit_pct"] is not None else "unknown")
             for label, b in (("within 1h", s.get("gap_within_60m")), ("after >1h", s.get("gap_over_60m")))
             if b is not None]
    split = f" ({', '.join(split)})" if split else ""
    line = (f"coordinator cache: {hit} token hit{split}; zero {states['zero']}, partial {states['partial']}, "
            f"full {states['full']}, missing {states['missing']} (unknown){legacy}; "
            f"{s['miss_turns']} misses (<50% read)")
    # Some providers never report request counts; the clause only says something when one does.
    if s.get("requests_known_turns") and s["requests_unknown_turns"]:
        line += f"; request count unknown for {s['requests_unknown_turns']} turns"
    return line


def backfill_cache_telemetry(db: DB, runs_dir, now: float | None = None, window_s: float = 8 * DAY) -> dict:
    """Finished coordinator turns from before runs recorded cache_telemetry get it from their
    output.jsonl, parsed by their provider. It counts as recorded only when the parse gives the
    stored token columns back; otherwise (no output, unknown provider, other tokens) the turn is
    marked legacy. Turns that already have the field are left alone, so a second pass changes
    nothing. Older turns than the status windows stay as they are (legacy without a mark)."""
    from pathlib import Path
    from .providers import get_provider
    now = time.time() if now is None else now
    done = {"filled": 0, "legacy": 0}
    rows = db.q("SELECT id, provider, dir, note, input_tokens, cache_read_tokens, cache_write_tokens "
                "FROM runs WHERE role='coordinator' AND status!='running' AND started>=?", (now - window_s,))
    for r in rows:
        note = _note(r["note"])
        if "cache_telemetry" in note:
            continue
        meta = None
        out = (Path(r["dir"]) if r["dir"] else Path(runs_dir) / str(r["id"])) / "output.jsonl"
        try:
            if out.exists():
                u = get_provider(r["provider"] or "").parse(out)
                got = u.extra.get("cache_telemetry")
                if isinstance(got, dict) and (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens) \
                        == tuple(r[k] or 0 for k in CACHE_TOKEN_KEYS):
                    meta = {**got, "backfilled": True}
        except (KeyError, OSError, ValueError):
            meta = None
        if meta is None:
            meta = {"complete": False, "requests": None, "source": CACHE_LEGACY}
        note["cache_telemetry"] = meta
        db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps(note), r["id"]))
        done["legacy" if meta.get("source") == CACHE_LEGACY else "filled"] += 1
    return done

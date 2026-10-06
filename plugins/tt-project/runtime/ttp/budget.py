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
A runaway check (spend rate far above this project's own norm) overrides both.
"""
from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import globalcap as gcap
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


WASTED = ("failed", "stalled", "timeout", "lost", "budget", "no_handoff")


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
    if provider in moved:
        g.reasons.append(f"{provider} is on another account than its plan readings; its spend counts toward "
                         f"the dollar caps")
    elif provider in lapsed:
        g.reasons.append(f"{provider} stopped reporting plan windows; its spend counts toward the dollar caps")
    if plan:
        g.regime = "windows"
        _plan(db, g, provider, plan, limit, int(b.get("max_parallel_workers", 6)), now)
    else:
        # The caps bound the project's dollars, whichever provider spends them. Providers on plan
        # windows are bounded by their windows instead, so their spend does not count here.
        day_cap, week_cap = float(b.get("daily_usd") or 0), float(b.get("weekly_usd") or 0)
        # Only spend whose account was billed by use when it was spent counts (billing.py): spend of
        # an account then on a plan stays plan spend after a switch drops its windows, and spend of
        # a usage-billed account stays billed whenever it was. Windows passed in without a recorded
        # reading cover their provider's spend up to now.
        windowed = {w.provider: now for w in windows if w.provider not in last}
        live = in_flight(db, exclude={p for p, until in windowed.items() if until >= now}, billed_at=now)
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
            _raise(g, "red", f"cap reached: ${today:.2f}/{label} of ${day_cap:.0f}, ${w7:.2f}/7d of ${week_cap:.0f}")
        elif ratio >= 0.85:
            _raise(g, "orange", f"{ratio:.0%} of cap used")
        elif ratio >= 0.6:
            _raise(g, "yellow", f"{ratio:.0%} of cap used")
        _global(db, g, b, provider, day, now)

    # Runaway guard: catch loops, not busy projects. Three signals over the last hour:
    # - waste: spend on runs that ended without an outcome (failed, stalled, timed out, lost), except
    #   runs a reboot cut short or whose hand-off stood;
    # - thrash: coordinator spend (decisions should be cents; dollars mean it is spinning);
    # - total: all spend above max(k x this project's 7-day hourly norm, a floor sized to the
    #   parallel work it is allowed), so even "successful" repetition is bounded.
    hour_ago = now - HOUR
    last_h = spent_last_hour(db, provider, now)
    runs_h = db.q("SELECT role, status, cost_usd, note FROM runs WHERE provider=? AND ended>=?", (provider, hour_ago))
    # A run stopped on purpose (a cancel, a pause, a redirect) is a decision, not waste.
    waste = sum(float(r["cost_usd"] or 0) for r in runs_h if wasted(r))
    thrash = sum(float(r["cost_usd"] or 0) for r in runs_h if r["role"] == "coordinator")
    norm = db.spent_since(now - WEEK, provider) / (7 * 24)
    per_task = max((b.get("task_default_usd") or {"deep": 25.0}).values())
    floor = float(b.get("hourly_floor_usd") or max(int(b.get("max_parallel_workers", 2)), 1) * per_task)
    ceiling = max(float(b.get("hourly_alarm_x", 4.0)) * norm, floor)
    # The waste limit is per worker: parallel workers each losing a run (a shared device kept them
    # all waiting) are not a loop. Workers are the most tasks whose wasted runs went at once, so one
    # task failing over and over, or new tasks failing one after another, count as one worker.
    spans = [r for r in db.q(f"SELECT task, started, ended, status, note FROM runs WHERE provider=? "
                             f"AND role!='coordinator' AND ended>=? AND status IN ({','.join('?' * len(WASTED))})",
                             (provider, hour_ago, *WASTED)) if wasted(r)]
    workers = min(max(_peak_tasks(spans, now), 1), max(int(b.get("max_parallel_workers", 6)), 1))
    waste_cap = float(b.get("hourly_waste_usd", 8.0)) * workers
    thrash_cap = float(b.get("hourly_coordinator_usd", 4.0))
    g.numbers.update({"spent_1h": round(last_h, 2), "hourly_ceiling": round(ceiling, 2),
                      "waste_1h": round(waste, 2), "waste_limit": round(waste_cap, 2),
                      "coordinator_1h": round(thrash, 2)})
    if waste > waste_cap:
        _raise(g, "red", f"runaway guard: ${waste:.2f} spent on failed or stalled runs in the last hour "
                         f"(limit ${waste_cap:.0f}); resumes automatically as the hour rolls over")
    if thrash > thrash_cap:
        _raise(g, "red", f"runaway guard: coordinator spent ${thrash:.2f} in the last hour (limit ${thrash_cap:.0f})")
    if last_h > ceiling:
        _raise(g, "red", f"runaway guard: ${last_h:.2f} spent in the last hour (long runs pro rata), ceiling ${ceiling:.2f}/h; "
                         f"resumes automatically as the hour rolls over")

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


def _global(db: DB, g: Gate, b: dict, provider: str, day: tuple[float, float] | None, now: float) -> None:
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
    g.numbers.update({"global_today": round(t["usd"], 2), "global_cap": cap, "global_stale": t["stale"],
                      "global_includes": t["includes"], "global_resets_at": end if day else None})
    if t["usd"] >= cap:
        when = f"; new work resumes at the reset in {max(end - now, 0) / HOUR:.1f} h" if day else ""
        stale = f" ({len(t['stale'])} machine{'s' if len(t['stale']) != 1 else ''} stale)" if t["stale"] else ""
        _raise(g, "red", f"global daily cap reached: {gcap.money(t['usd'])} of ${cap:.0f} today across the "
                         f"account{stale}{when}")


def _plan(db: DB, g: Gate, provider: str, plan: list[Window], line: float, most: int, now: float) -> None:
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
        if w.utilization >= line:
            _raise(g, "red", f"{w.window} window at {w.utilization:.0f}%; the project stops at {line:.0f}% "
                             f"until it resets")
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


def wake_tier(tier: str, prev: dict) -> str | None:
    """The tier of the run that wakes a task whose last hand-off (`prev`) was `waiting`, or None
    when the run is no wake. Most wakes only check whether the wait is over, so a hand-off that
    names what it waits on wakes at light unless it asks for a `wake_tier`; never above the task's
    own tier. A hand-off whose `next_step` names the one mechanical step left (say `push`) wakes at
    light whatever it asked: that run does the step itself."""
    if not isinstance(prev, dict) or prev.get("status") != "waiting":
        return None
    tier = tier if tier in TIER_ORDER else "standard"
    want = "light" if next_step(prev) else prev.get("wake_tier")
    if want not in TIER_ORDER:
        want = "light" if prev.get("retry_when") or prev.get("waiting_for") else tier
    return min(want, tier, key=TIER_ORDER.index)


def next_step(prev: dict) -> str:
    """The mechanical step a waiting hand-off left for after its wait (`next_step`), or ''."""
    step = prev.get("next_step") if isinstance(prev, dict) else None
    return " ".join(step.split())[:200] if isinstance(step, str) else ""


# Not .txt: CMakeLists.txt and requirements.txt are build and dependency changes.
DOC_SUFFIXES = (".md", ".markdown", ".rst", ".adoc")


def review_tier(changes: dict[str, int | None], cfg: dict) -> str:
    """Light for a doc-only diff or a small one that touches no risky path, standard otherwise.
    Doc lines do not count toward the size: prose next to a small code change is not risk."""
    rules = cfg.get("review", {})
    risky = rules.get("risky_paths") or []
    if any(fnmatch.fnmatch(path, g) for path in changes for g in risky):
        return "standard"
    code = [n for path, n in changes.items() if not path.lower().endswith(DOC_SUFFIXES)]
    if any(n is None for n in code):
        return "standard"
    return "light" if sum(code) <= int(rules.get("light_max_lines", 60)) else "standard"


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
    return {"daily_spend": spend, "window_peaks": peaks, "top_sources_7d": by_source,
            "coordinator_cache": coordinator_cache(db)}


def reread_stats(db: DB, days: float = 7, now: float | None = None, top: int = 10) -> dict:
    """Context re-read (cache-read) tokens of the runs started in the last `days`: per run, per $,
    their share of the weighted tokens, by role, task kind, tier and effort, and the top runs. A long
    run re-reads its whole context on every call, so this is what trimming output and splitting long
    tasks cut. `ttp stats` prints it; run it before and after a change to compare."""
    now = now or time.time()
    since = now - days * 86400
    w = TOKEN_WEIGHTS
    rows = db.q("SELECT r.id, r.task, r.role, r.effort, r.started, r.ended, r.cost_usd, r.input_tokens, "
                "r.output_tokens, r.cache_read_tokens, r.cache_write_tokens, t.kind, t.tier, t.title "
                "FROM runs r LEFT JOIN tasks t ON t.id=r.task WHERE r.started>=?", (since,))

    def total(rs: list, k: str) -> float:
        return sum(r[k] or 0 for r in rs)

    def summary(rs: list) -> dict:
        n, usd, cr = len(rs), total(rs, "cost_usd"), int(total(rs, "cache_read_tokens"))
        units = (total(rs, "input_tokens") * w["input"] + total(rs, "output_tokens") * w["output"]
                 + cr * w["cache_read"] + total(rs, "cache_write_tokens") * w["cache_write"])
        return {"runs": n, "usd": round(usd, 2), "cache_read": cr, "per_run": round(cr / n) if n else 0,
                "per_usd": round(cr / usd) if usd else 0, "max": max((r["cache_read_tokens"] or 0 for r in rs), default=0),
                "share": round(cr * w["cache_read"] / units, 3) if units else 0.0}

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["role"], r["kind"] or "-", r["tier"] or "-", r["effort"] or "-"), []).append(r)
    by = [{"role": k[0], "kind": k[1], "tier": k[2], "effort": k[3], **summary(v)} for k, v in groups.items()]
    by.sort(key=lambda g: -g["cache_read"])
    worst = sorted(rows, key=lambda r: -(r["cache_read_tokens"] or 0))[:top]
    return {"days": days, "since": since, **summary(rows), "groups": by,
            "top": [{"run": r["id"], "task": r["task"], "role": r["role"], "kind": r["kind"], "tier": r["tier"],
                     "effort": r["effort"], "usd": round(r["cost_usd"] or 0, 2), "cache_read": r["cache_read_tokens"] or 0,
                     "minutes": round(((r["ended"] or now) - (r["started"] or now)) / 60),
                     "title": (r["title"] or "")[:60]} for r in worst]}


def reread_text(s: dict) -> str:
    """`reread_stats` for people: totals, the groups and the top runs, one line each."""
    m = lambda n: f"{n / 1e6:.2f} M"
    lines = [f"Context re-reads, last {s['days']:g} days: {s['runs']} runs, ${s['usd']:.2f}, {m(s['cache_read'])} "
             f"cache-read tokens ({m(s['per_run'])} per run, {m(s['per_usd'])} per $, {s['share']:.0%} of "
             "weighted tokens)", "By role / kind / tier / effort:"]
    lines += [f"  {g['role']}/{g['kind']}/{g['tier']}/{g['effort']}: {g['runs']} runs, ${g['usd']:.2f}, "
              f"{m(g['cache_read'])} ({m(g['per_run'])} per run, max {m(g['max'])})" for g in s["groups"]]
    lines.append("Top runs:")
    lines += [f"  run {t['run']} (#{t['task'] or '-'} {t['kind'] or t['role']}/{t['tier'] or '-'}/{t['effort'] or '-'}, "
              f"{t['minutes']} min, ${t['usd']:.2f}): {m(t['cache_read'])} {t['title']}".rstrip() for t in s["top"]]
    return "\n".join(lines)



def cache_hit(read: float, write: float, fresh: float) -> float | None:
    """Share of a call's prompt tokens read from the provider's cache, or None with no prompt."""
    total = read + write + fresh
    return read / total if total > 0 else None


def coordinator_cache(db: DB, now: float | None = None) -> dict:
    """Prompt cache use of coordinator turns over the last 24 h and 7 d: `hit_pct` is cache reads
    over all prompt tokens, `miss_turns` the turns that read under half their prompt from it."""
    now = now or time.time()
    out = {}
    for label, span in (("24h", DAY), ("7d", WEEK)):
        rows = db.q("SELECT cost_usd, input_tokens, cache_read_tokens, cache_write_tokens FROM runs "
                    "WHERE role='coordinator' AND started>=? AND status!='running' "
                    "AND input_tokens + cache_read_tokens + cache_write_tokens > 0", (now - span,))
        hits = [cache_hit(r["cache_read_tokens"] or 0, r["cache_write_tokens"] or 0, r["input_tokens"] or 0)
                for r in rows]
        total = cache_hit(*(sum(r[k] or 0 for r in rows) for k in
                            ("cache_read_tokens", "cache_write_tokens", "input_tokens")))
        out[label] = {"turns": len(rows), "hit_pct": round(100 * total) if total is not None else None,
                      "miss_turns": sum(h < 0.5 for h in hits),
                      "usd_per_turn": round(sum(r["cost_usd"] or 0 for r in rows) / len(rows), 4) if rows else None}
    return out

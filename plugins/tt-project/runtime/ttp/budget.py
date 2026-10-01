# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Budget governor: turns meter readings and the spend ledger into a gate for new work.

Two regimes, chosen per provider from what the provider reports:
- plan windows (subscription plans report utilization per window): a plan is paid for per period,
  so unused capacity is lost at each reset. The project paces itself to land each window at
  100 - reserve_pct by its reset: it measures the account's burn rate from its own readings and
  runs as many parallel workers as that pace allows. It never takes the account past the target,
  because the remainder belongs to the user's own work;
- dollar caps (usage-billed accounts report no window): rolling 24 h and 7 d caps on what THIS
  project spends across all its providers not on plan windows, with the user's defaults when the
  charter sets none.
A runaway check (spend rate far above this project's own norm) overrides both.
"""
from __future__ import annotations

import fnmatch
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .db import DB

LEVELS = ("green", "yellow", "orange", "red")
HOUR, DAY, WEEK = 3600.0, 86400.0, 7 * 86400.0
SNAPSHOT_FRESH_S = 30 * 60
PLAN_MEMORY_S = 7 * 86400      # a provider that reported plan windows this recently is on a plan
# Length of each named window, used to measure burn over a sensible span and to roll a window over
# when its reset has passed without a new reading. Unknown names fall back to a week.
WINDOW_HOURS = {"five_hour": 5.0, "5h": 5.0, "seven_day": 168.0, "7d": 168.0, "seven_day_opus": 168.0,
                "seven_day_sonnet": 168.0}
# Spend this long after a plan provider's last window reading still counts as plan-billed.
PLAN_GRACE_S = HOUR
# A plan provider's windows arrive with each run or from a meter read every few minutes. Once its
# last reading is stale and this many paid runs have ended since, it is billed by use (an API key,
# an expired plan): the dollar caps apply again.
PLAN_LAPSE_RUNS = 2
# Relative price of each token class (input = 1), used only to apply an observed rate to a token
# mix; not a price list. Override with budget.estimate_weights.
TOKEN_WEIGHTS = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25}
# Burn is measured over a quarter of the window, at most this long. Readings come in whole percents,
# so a weekly window needs hours of them for a steady slope: over 3 h its projection swung between
# 13% and 428% with each one-point step.
BURN_SPAN_MAX_S = 12 * HOUR
# A window over pace stays over until its burn falls below this fraction of the burn it needs, so
# a slope hovering near the pace does not flip the gate between green and yellow on every reading.
PACE_EXIT = 0.85


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


def in_flight(db: DB, provider: str | None = None, exclude: set[str] | None = None) -> float:
    """Spend so far of runs still going, as the daemon last priced it; the ledger has it only
    once they end."""
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
    lapsed = {p for p, ts in last.items() if now - ts > SNAPSHOT_FRESH_S and plan_lapsed(db, p, ts)}
    plan = [w for w in windows if w.provider == provider and provider not in lapsed]
    if provider in lapsed:
        g.reasons.append(f"{provider} stopped reporting plan windows; its spend counts toward the dollar caps")
    if plan:
        g.regime = "windows"
        _pace(db, g, provider, plan, limit, int(b.get("max_parallel_workers", 6)), now,
              float(b.get("max_pace_hold_s", 7200)))
    else:
        # The caps bound the project's dollars, whichever provider spends them. Providers on plan
        # windows are bounded by their windows instead, so their spend does not count here.
        day_cap, week_cap = float(b.get("daily_usd") or 0), float(b.get("weekly_usd") or 0)
        # Only spend up to a provider's last reading (plus grace) is plan-billed: a provider that
        # stops reporting windows may have moved to usage billing. The windows passed in may be
        # days old, so only a fresh reading covers spend up to now.
        windowed = {p: ts if p in lapsed else now if now - ts <= SNAPSHOT_FRESH_S else ts + PLAN_GRACE_S
                    for p, ts in last.items()}
        windowed.update({w.provider: now for w in windows if w.provider not in last})
        live = in_flight(db, exclude={p for p, until in windowed.items() if until >= now})
        d = db.spent_since(now - DAY, exclude=windowed) + live
        w7 = db.spent_since(now - WEEK, exclude=windowed) + live
        g.numbers.update({"spent_24h": round(d, 2), "spent_7d": round(w7, 2), "in_flight": round(live, 2),
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
        # On a plan the pace already sets the workers; a deep run burns the window fastest.
        g.max_tier = "standard"
        if g.regime == "caps":
            g.max_parallel = max(1, g.max_parallel // 2 or 1)
    elif g.level == "orange":
        g.max_tier, g.max_parallel, g.allow_optional = "light", 1, False
    elif g.level == "red":
        g.max_tier, g.max_parallel, g.allow_optional, g.allow_new_work = "light", 0, False, False
    return g


def _pace(db: DB, g: Gate, provider: str, plan: list[Window], target: float, most: int, now: float,
          max_hold: float = 7200.0) -> None:
    """Size parallel work so each window lands at `target` by its reset, from measured burn.

    For each window: `need` is the burn (points of the window per hour) that reaches the target
    exactly at the reset; `burn` is what the account has actually used over the recent past. Burning
    slower than needed leaves paid capacity unused, so the project may run up to `most` workers.
    Burning faster scales this project's workers down in proportion (other projects on the same
    account see the same readings and do the same). Near the target only light work runs; at the
    target nothing new starts until the reset.

    The burn was produced by the workers that ran while it was measured, so the scale applies to
    their time-weighted mean, not to the count running now: after a burst the running count may be
    1, and scaling that would hold the project at 1 until the burst leaves the measured span.

    One worker can still be too many: the pace allows a fraction `duty = mean * need / burn` of one
    (mean here not floored at 1). Then new starts are spaced out: the next may start the last run's
    length x (1/duty - 1) after it ended, at most `max_hold` later, so noisy readings cannot stall
    the project. Running work is never stopped; the daemon lets a user's own tasks and reviews
    through (see `pace_hold`). While a hold is on the project runs nothing, so its mean falls with
    every tick and the duty with it: a hold, once set for the last run, can only move earlier.
    """
    running = db.one("SELECT COUNT(*) n FROM runs WHERE provider=? AND status='running' AND role!='coordinator'",
                     (provider,))["n"]
    allowed, rows, duty = most, [], None
    for w in plan:
        hours_left = max((w.resets_at - now) / HOUR, 0.05) if w.resets_at else None
        readings = _readings(db, provider, w.window, w.resets_at, now)
        burn = _slope(readings)
        mean = None if burn is None else avg_running(db, provider, float(readings[0]["ts"]), now)
        need = max(target - w.utilization, 0.0) / hours_left if hours_left else None
        projected = w.utilization + burn * hours_left if (burn is not None and hours_left) else None
        row = {"window": w.window, "utilization": round(w.utilization, 1), "resets_at": w.resets_at,
               "hours_left": round(hours_left, 2) if hours_left else None,
               "burn_per_h": None if burn is None else round(burn, 2),
               "need_per_h": None if need is None else round(need, 2),
               "projected": None if projected is None else round(projected, 1),
               "avg_running": None if mean is None else round(mean, 2), "allowed": most}
        rows.append(row)
        if w.utilization >= target:
            _raise(g, "red", f"{w.window} window at {w.utilization:.0f}%; the project stops at {target:.0f}% "
                             f"until it resets")
            row["allowed"] = 0
        elif w.utilization >= target - 2:
            _raise(g, "orange", f"{w.window} window at {w.utilization:.0f}%, just under the {target:.0f}% stop")
            row["allowed"] = 1
        elif burn is not None and need is not None and _over_pace(db, provider, w, burn, need):
            row["allowed"] = min(most, max(1, int(mean * need / burn)))
            raw = avg_running(db, provider, float(readings[0]["ts"]), now, floor=0.0)
            # Other sessions on the account burn the same window; say when the burn is not ours.
            own = (f"averaged {mean:.1f} while it was measured" if raw > 0 else
                   "none ran while it was measured: other sessions on the account made this burn")
            _raise(g, "yellow", f"{w.window} window on pace for {projected:.0f}% by its reset, over the "
                                f"{target:.0f}% target; running {row['allowed']} workers ({own})")
            allowed = min(allowed, row["allowed"])
            row["duty"] = round(raw * need / burn, 3)
            if row["duty"] < 1 and (duty is None or row["duty"] < duty["duty"]):
                duty = row
    worst = max(rows, key=lambda r: (r["projected"] if r["projected"] is not None else r["utilization"]))
    g.max_parallel = allowed
    g.numbers.update({"window": worst["window"], "utilization": worst["utilization"], "limit": target,
                      "resets_at": worst["resets_at"], "projected": worst["projected"], "running": running,
                      "pace": rows})
    if duty is None:
        return
    last = db.one("SELECT started, ended FROM runs WHERE provider=? AND role!='coordinator' AND status!='running' "
                  "AND started IS NOT NULL AND ended IS NOT NULL ORDER BY ended DESC LIMIT 1", (provider,))
    if not last:
        return
    end, length = float(last["ended"]), max(float(last["ended"]) - float(last["started"]), 0.0)
    wait = length * (1 / duty["duty"] - 1) if duty["duty"] > 0 else max_hold
    until = end + min(wait, max_hold)
    key, run = f"pace_hold:{provider}", [float(last["started"]), end]
    held = db.kv(key) or {}
    if held.get("run") == run:
        until = min(until, float(held.get("until") or until))
    else:
        db.set_kv(key, {"run": run, "until": until})
    if until > now:
        g.numbers["paced"] = {"until": until, "window": duty["window"], "projected": duty["projected"],
                              "duty": duty["duty"]}


def pace_hold(gate: Gate | dict | None, task: dict, now: float | None = None) -> float | None:
    """When a pace hold keeps `task` from starting, the time it ends. A task from the user's own
    request (added by the user, or answering a chat) and a review of finished work go ahead."""
    n = (gate.get("numbers") if isinstance(gate, dict) else getattr(gate, "numbers", None)) or {}
    until = float((n.get("paced") or {}).get("until") or 0)
    if until <= (now or time.time()):
        return None
    if task.get("origin") == "user" or task.get("reply_chat") or task.get("kind") == "review":
        return None
    return until


def burn_rate(db: DB, provider: str, window: str, resets_at: float | None, now: float) -> float | None:
    """Points of the window used per hour, from this project's readings in the current period.

    None until two readings at least five minutes apart exist: no reading, no guess.
    """
    return _slope(_readings(db, provider, window, resets_at, now))


def _over_pace(db: DB, provider: str, w: Window, burn: float, need: float) -> bool:
    """Whether `w` burns over pace: above it by 5% to go over, below PACE_EXIT of it to come back."""
    key = f"pace_over:{provider}:{w.window}"
    was = db.kv(key)
    was = was is not None and was == w.resets_at     # over pace earlier in this same period
    over = burn > need * (PACE_EXIT if was else 1.05)
    if over != was:
        db.set_kv(key, w.resets_at if over else None)
    return over


def _readings(db: DB, provider: str, window: str, resets_at: float | None, now: float) -> list:
    # Only readings of the current period count (same reset), so the span stops at its start.
    span = min(WINDOW_HOURS.get(window, 168.0) * HOUR / 4, BURN_SPAN_MAX_S)
    return db.q("SELECT ts, utilization FROM snapshots WHERE provider=? AND window=? AND ts>=? AND "
                "(resets_at=? OR (? IS NULL AND resets_at IS NULL)) ORDER BY ts",
                (provider, window, now - span, resets_at, resets_at))


def _slope(rows: list) -> float | None:
    if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 300:
        return None
    # Least-squares slope over every reading: readings come in whole percents, so a two-point
    # estimate jumps with each new reading and the pace would flap between over and under.
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


def plan_windows(db: DB, now: float | None = None) -> list[Window]:
    """The latest reading of every plan window reported in the last week.

    Being on a plan is a fact about the account, so an old reading still says which regime applies;
    the pacing works from the readings themselves. A window whose reset has passed since its last
    reading has started a new period: it counts as empty until the next reading says otherwise.
    """
    now = now or time.time()
    rows = db.q("SELECT s.* FROM snapshots s JOIN (SELECT provider, window, MAX(ts) mts FROM snapshots "
                "WHERE ts>=? GROUP BY provider, window) m ON s.provider=m.provider AND s.window=m.window "
                "AND s.ts=m.mts", (now - PLAN_MEMORY_S,))
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
    """Latest reading per (provider, window), ignoring readings too old to trust."""
    now = now or time.time()
    rows = db.q("SELECT s.* FROM snapshots s JOIN (SELECT provider, window, MAX(ts) mts FROM snapshots "
                "GROUP BY provider, window) m ON s.provider=m.provider AND s.window=m.window AND s.ts=m.mts "
                "WHERE s.ts>=?", (now - SNAPSHOT_FRESH_S,))
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

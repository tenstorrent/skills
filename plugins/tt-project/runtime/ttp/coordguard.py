# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The persistent coordinator session's spend guard (Daemon.check_coord_guard), model-free.

The charter rule: a coordinator in session mode (coordinator.session.mode on) must not cost more per
day than the stateless per-turn design it replaces. Once the mode reads on, the daemon records the
project's stateless baseline from the runs table in kv BASELINE_KEY: the coordinator runs started in
the BASE_DAYS before the switch-on (the mode's recorded set time, configwho, else the first check
that saw it on), as $/turn and $/day ({"at", "from", "turns", "usd", "days", "usd_turn", "usd_day"}).
A project with no coordinator turn in those days has no baseline and the guard stays inert.

Every CHECK_S it compares the session's coordinator spend with that baseline. The window is the last
WINDOW_H hours, starting no earlier than the switch-on or the end of the last trip, so stateless turns
run while tripped never count. Its allowance (allowance()):

    usd_day * h/24 * (1 + (EARLY - 1) * (1 - h/24)) + usd_turn * WARM_TURNS,   h = window hours in [MIN_H, 24]

- At a full day the allowance is the baseline day plus WARM_TURNS turns: the charter's "no more than
  today's design", with room only for the few extra cache writes a session start or rollover costs.
- In the first hours the day is prorated per hour, with headroom EARLY (2x) tapering to 1x at 24 h:
  stateless spend is not spread evenly over a day (busy hours run at about twice the mean), and a
  new session pays its cache warm-up up front. MIN_H keeps the first minutes on one hour's allowance,
  so a runaway is still caught within the first hour.

The spend judged against it (counted()) treats a new session's seed as the one-time cost it is: the
first SEED_AMORT new-session wakes in the window (seedcap.seed_of: their cache writes plus uncached
input) count only h/24 of their seed, spread over the day the session serves; any later seed in the
window counts whole, so a rollover loop still trips. At a full day every seed counts whole, so the
daily limit is unchanged. Without this, a switch-on whose first seed failed and was paid twice
tripped within its first hour on seeds alone, while its resumed turns stayed well under.

Over the allowance it trips: kv KEY becomes {"tripped": true, "until": now + the pause, "why", "at",
"trips": [recent trip times], "hold", "pause_h"}, the daemon raises an issue and sends a low notify.
The pause (pause_for()) is coordinator.session.pause_h hours (PAUSE_H if unset) for a repeat trip in
HOLD_DAYS days or spend at FULL_PAUSE_AT times the allowance; a first trip just over the line pauses
MIN_PAUSE_H, scaling linearly up to pause_h, so a small overrun does not keep the on-by-default
session off for hours. While tripped the coordinator runs stateless turns (the session code
reads tripped() or the key: tripped and now < until). After `until` it un-trips. The HOLD_TRIPS-th trip
within HOLD_DAYS days holds it: `until` is renewed on every check and it stays tripped until the
project's coordinator (or the user) sets coordinator.session.mode again (a configwho record newer than
the trip). Mode off: inert; both keys are dropped, so the next switch-on records a fresh baseline.
"""
from __future__ import annotations

import json
import time

BASELINE_KEY = "coord_session_baseline"
KEY = "coord_session_guard"
SOURCE = "daemon:coord-session-guard"
CHECK_S = 300.0
BASE_DAYS = 7
WINDOW_H = 24.0
MIN_H = 1.0
EARLY = 2.0
WARM_TURNS = 3
PAUSE_H = 6.0
MIN_PAUSE_H = 1.0
FULL_PAUSE_AT = 2.0   # spend at this multiple of the allowance gets the whole pause_h on a first trip
SEED_AMORT = 2        # new-session seeds per window spread over a day; later ones count whole
HOLD_TRIPS = 3
HOLD_DAYS = 7
ON = {"on", "true", "1", "yes", "session"}
SETTERS = ("coordinator", "user")   # whose new set of the mode releases a held guard


def session_cfg(cfg: dict) -> dict:
    s = (cfg.get("coordinator") or {}).get("session")
    return s if isinstance(s, dict) else {}


def mode_on(cfg: dict) -> bool:
    """coordinator.session.mode reads on (True, "on" and the like; unset: the default, coordsession.DEFAULTS)
    and the coordinator's provider (core_provider, as the daemon resolves it) runs a session
    (coordsession.PROVIDERS); off, shadow, bad or another provider: False."""
    from .coordsession import DEFAULTS, PROVIDERS
    if ((cfg or {}).get("core_provider") or "claude") not in PROVIDERS:
        return False
    m = session_cfg(cfg).get("mode")
    if m is None:
        m = DEFAULTS["mode"]
    return m is True or (isinstance(m, str) and m.strip().lower() in ON)


def pause_h(cfg: dict) -> float:
    v = session_cfg(cfg).get("pause_h")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return PAUSE_H
    return v if v > 0 else PAUSE_H


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _mode_set(db) -> dict:
    from . import configwho
    try:
        return configwho.get(db, "coordinator.session.mode") or {}
    except Exception:
        return {}


def baseline(db, on_at: float) -> dict:
    """The stateless coordinator's spend in the BASE_DAYS before `on_at`, per turn and per day."""
    start = on_at - BASE_DAYS * 86400
    r = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS usd, MIN(started) AS first FROM runs "
               "WHERE role='coordinator' AND started>=? AND started<?", (start, on_at))
    n, usd = int(r["n"] or 0), float(r["usd"] or 0)
    # A project younger than the window: its own days, at least one.
    days = min(float(BASE_DAYS), max(1.0, (on_at - float(r["first"] or on_at)) / 86400)) if n else 0.0
    return {"at": on_at, "from": start, "turns": n, "usd": round(usd, 4), "days": round(days, 3),
            "usd_turn": round(usd / n, 4) if n else 0.0, "usd_day": round(usd / days, 4) if n else 0.0}


def allowance(base: dict, hours: float) -> float:
    h = min(WINDOW_H, max(MIN_H, hours))
    frac = h / WINDOW_H
    return base["usd_day"] * frac * (1 + (EARLY - 1) * (1 - frac)) + base["usd_turn"] * WARM_TURNS


def tripped(db, now: float | None = None) -> bool:
    """Whether the coordinator runs stateless turns now because the guard tripped."""
    g, _ = db.kv_dict(KEY)
    now = time.time() if now is None else now
    try:
        return bool(g.get("tripped")) and (bool(g.get("hold")) or now < float(g.get("until") or 0))
    except (TypeError, ValueError):
        return bool(g.get("tripped"))


def spend(db, start: float) -> tuple[float, int]:
    r = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS usd FROM runs "
               "WHERE role='coordinator' AND started>=?", (start,))
    return float(r["usd"] or 0), int(r["n"] or 0)


def seeds(db, cfg: dict, start: float) -> list[float]:
    """The seed (seedcap.seed_of) of each new-session coordinator wake started at or after `start`, oldest first."""
    from . import seedcap
    got = []
    for r in db.q("SELECT provider, cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, note "
                  "FROM runs "
                  "WHERE role='coordinator' AND task IS NULL AND started>=? AND cost_usd>0 "
                  "AND note LIKE '%\"new\"%' ORDER BY started, id", (start,)):
        try:
            session = (json.loads(r["note"] or "{}").get("turn") or {}).get("session") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(session, dict) and session.get("mode") == "new":
            got.append(seedcap.seed_of(db, cfg, r["provider"] or "", r))
    return got


def counted(db, cfg: dict, start: float, hours: float) -> tuple[float, float, int, float]:
    """(dollars judged, dollars spent, turns, dollars deferred) of the window from `start`: the first
    SEED_AMORT seeds count only h/24 of their cost (h in [MIN_H, 24]), the rest of the spend counts whole."""
    usd, turns = spend(db, start)
    frac = min(WINDOW_H, max(MIN_H, hours)) / WINDOW_H
    deferred = min(usd, sum(seeds(db, cfg, start)[:SEED_AMORT]) * (1 - frac))
    return usd - deferred, usd, turns, deferred


def pause_for(cfg: dict, ratio: float, repeat: bool) -> float:
    """Hours of stateless turns after a trip at `ratio` times the allowance: pause_h for a repeat trip,
    else MIN_PAUSE_H just over the line, rising linearly to pause_h at FULL_PAUSE_AT."""
    full = pause_h(cfg)
    if repeat:
        return full
    scale = min(1.0, max(0.0, (ratio - 1) / (FULL_PAUSE_AT - 1)))
    return max(min(MIN_PAUSE_H, full), full * scale)


def check(db, cfg: dict, now: float | None = None) -> dict | None:
    """One guard step. Writes the kv keys; returns what happened for the daemon to report:
    {"event": "baseline"|"trip"|"untrip"|"release"|"off", "text": ...}, or None when nothing changed."""
    now = time.time() if now is None else now
    if not mode_on(cfg):
        if db.kv(BASELINE_KEY) is None and db.kv(KEY) is None:
            return None
        db.x("DELETE FROM kv WHERE key IN (?,?)", (BASELINE_KEY, KEY))
        return {"event": "off", "text": "coordinator session mode is off (or its provider runs no session): "
                                        "the spend guard's baseline and state are cleared"}
    base, _ = db.kv_dict(BASELINE_KEY)
    if not base:
        rec = _mode_set(db)
        try:
            on_at = min(now, float(rec.get("ts") or now))
        except (TypeError, ValueError):
            on_at = now
        base = baseline(db, on_at)
        db.set_kv(BASELINE_KEY, base)
        db.set_kv(KEY, {"tripped": False, "trips": []})
        return {"event": "baseline", "text": (
            f"coordinator session spend guard: stateless baseline ${base['usd_day']:.2f}/day, "
            f"${base['usd_turn']:.2f}/turn ({base['turns']} turns over {base['days']:g} days)" if base["turns"]
            else "coordinator session spend guard: no coordinator turn in the 7 days before; nothing to compare, inert")}
    g, _ = db.kv_dict(KEY)
    if g.get("tripped"):
        if g.get("hold"):
            rec = _mode_set(db)
            if rec.get("by") in SETTERS and _num(rec.get("ts")) > _num(g.get("at")):
                db.set_kv(KEY, {"tripped": False, "trips": [], "resumed": now, "released": now,
                                "released_by": rec.get("by")})
                return {"event": "release", "text": f"coordinator session spend guard released: the {rec.get('by')} "
                                                    f"set coordinator.session.mode again"}
            db.set_kv(KEY, {**g, "until": now + pause_h(cfg) * 3600})   # held: a reader of `until` sees it too
            return None
        if now < float(g.get("until") or 0):
            return None
        db.set_kv(KEY, {**g, "tripped": False, "resumed": float(g.get("until") or now)})
        return {"event": "untrip", "text": "coordinator session spend guard: the pause ended, session turns resume"}
    if not base.get("turns"):
        return None
    start = max(now - WINDOW_H * 3600, float(base.get("at") or 0), float(g.get("resumed") or 0))
    hours = max(0.0, (now - start) / 3600)
    usd, raw, turns, deferred = counted(db, cfg, start, hours)
    limit = allowance(base, hours)
    if usd <= limit:
        return None
    prior = [t for t in (g.get("trips") or []) if isinstance(t, (int, float)) and now - t < HOLD_DAYS * 86400]
    trips = prior + [now]
    hold = len(trips) >= HOLD_TRIPS
    pause = pause_for(cfg, usd / limit if limit > 0 else FULL_PAUSE_AT, bool(prior))
    seeded = (f" (${usd:.2f} counted: ${deferred:.2f} of new-session seeds spread over a day)"
              if deferred >= 0.005 else "")
    why = (f"coordinator session spent ${raw:.2f} in {turns} turns over the last {hours:.1f} h{seeded}, over its "
           f"allowance ${limit:.2f} (stateless baseline ${base['usd_day']:.2f}/day, ${base['usd_turn']:.2f}/turn)")
    db.set_kv(KEY, {"tripped": True, "until": now + pause * 3600, "why": why, "at": now, "pause_h": round(pause, 3),
                    "trips": trips, "hold": hold, "usd": round(usd, 4), "raw_usd": round(raw, 4),
                    "allowance": round(limit, 4)})
    then = (f"trip {len(trips)} in {HOLD_DAYS} days: it stays on stateless turns until the coordinator sets "
            f"coordinator.session.mode again" if hold
            else f"stateless turns for {round(pause, 1):g} h ({'a repeat trip: ' if prior else 'sized to the overrun, '}"
                 f"coordinator.session.pause_h {pause_h(cfg):g} h at most), then the session resumes")
    return {"event": "trip", "hold": hold, "at": now, "text": f"{why}. The guard reverted it to {then}."}


def status_line(db, cfg: dict, now: float, fmt=None) -> str | None:
    """One line for `ttp status --verbose`; None while the mode is off and the guard holds nothing."""
    base, _ = db.kv_dict(BASELINE_KEY)
    if not mode_on(cfg):
        return None
    if not base:
        return "session guard: mode on, baseline not yet recorded"
    if not base.get("turns"):
        return "session guard: no stateless baseline (no coordinator turn in the 7 days before switch-on), inert"
    g, _ = db.kv_dict(KEY)
    fmt = fmt or (lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)))
    head = f"session guard: baseline ${base['usd_day']:.2f}/day, ${base['usd_turn']:.2f}/turn"
    if g.get("tripped") and g.get("hold"):
        return f"{head} · HELD stateless (trip {len(g.get('trips') or [])} in {HOLD_DAYS} days) until the mode is set again"
    if tripped(db, now):
        return f"{head} · TRIPPED, stateless until {fmt(float(g['until']))}"
    start = max(now - WINDOW_H * 3600, float(base.get("at") or 0), float(g.get("resumed") or 0))
    hours = max(0.0, (now - start) / 3600)
    usd, _, _, _ = counted(db, cfg, start, hours)
    return (f"{head} · ok: ${usd:.2f} of ${allowance(base, hours):.2f} in the last {hours:.1f} h"
            f" · trips in {HOLD_DAYS} days: {len([t for t in g.get('trips') or [] if now - t < HOLD_DAYS * 86400])}")

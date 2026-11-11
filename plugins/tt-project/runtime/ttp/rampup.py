# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Ramp-up after an outage: work restarts a few tasks at a time, never all at once.

Each tick Daemon.track_outages hands observe() the outage signs it sees: the project pause, the
lease not held, a provider's open login breaker, a provider's budget gate closed to new work, a
provider whose API host does not resolve, the settle after the host woke, each paused resource. A
new boot counts as an outage that just ended. When a sign ends, a ramp starts (kv KEY): {since,
cause, base, scope}. While it runs, dispatch starts a worker only while the running workers (in its
scope) number fewer than base + start * 2**(elapsed // step_s): the starts double each step until
they pass budget.max_parallel_workers, and the ramp ends. ready_tasks puts urgent work first, so it
takes the first starts. A lifted resource pause ramps only that resource's tasks (scope).

budget.ramp_start: starts in the first step (default START; 0 turns ramp-up off).
budget.ramp_step_s: seconds per doubling (default STEP_S).
"""
from __future__ import annotations

import time

KEY = "rampup"                 # kv: the running ramp, or absent
SIGNS_KEY = "rampup_signs"     # kv: {"signs": [...], "boot": id} as last seen
START = 2
STEP_S = 180
HELD_NOTE = "ramping up after an outage:"


def settings(cfg: dict) -> tuple[int, float]:
    b = (cfg or {}).get("budget") or {}
    try:
        start = max(int(b.get("ramp_start", START)), 0)
    except (TypeError, ValueError):
        start = START
    try:
        step = max(float(b.get("ramp_step_s", STEP_S)), 1.0)
    except (TypeError, ValueError):
        step = float(STEP_S)
    return start, step


def _normal(cfg: dict) -> int:
    try:
        return max(int(((cfg or {}).get("budget") or {}).get("max_parallel_workers", 6)), 1)
    except (TypeError, ValueError):
        return 6


def observe(db, cfg: dict, signs: set[str], boot: str, running, now: float | None = None) -> str:
    """Record this tick's outage signs; start a ramp when one ended or the host booted since the last
    tick. `running(scope)` counts the running workers (on the resources in scope; None: all). Returns
    the cause of a ramp started now, else ''."""
    now = time.time() if now is None else now
    prev = db.kv(SIGNS_KEY) or {}
    seen = {"signs": sorted(signs), "boot": boot}
    if prev != seen:
        db.set_kv(SIGNS_KEY, seen)
    if not prev or not settings(cfg)[0]:
        return ""   # the first look on record, or ramp-up is off: nothing ended that we saw
    ended = sorted(set(prev.get("signs") or []) - signs)
    rebooted = bool(prev.get("boot")) and bool(boot) and prev["boot"] != boot
    if not ended and not rebooted:
        return ""
    cause = ", ".join((["host reboot"] if rebooted else []) + ended)
    scope = None
    if not rebooted and all(s.startswith("resource:") for s in ended):
        scope = sorted(s[len("resource:"):] for s in ended)
        old = db.kv(KEY)
        if old and active(db, cfg, now) and old.get("scope") is None:
            scope = None   # a wider ramp under way stays wide
        elif old and active(db, cfg, now):
            scope = sorted(set(scope) | set(old["scope"]))
    db.set_kv(KEY, {"since": now, "cause": cause, "base": int(running(scope)), "scope": scope})
    return cause


def allowed(ramp: dict, cfg: dict, now: float) -> int:
    start, step = settings(cfg)
    k = max(int((now - float(ramp.get("since") or now)) // step), 0)
    return int(ramp.get("base") or 0) + start * 2 ** min(k, 20)


def active(db, cfg: dict, now: float | None = None) -> dict | None:
    """The ramp under way, or None (cleared once its starts pass the normal limit)."""
    ramp = db.kv(KEY)
    if not ramp:
        return None
    now = time.time() if now is None else now
    start, _ = settings(cfg)
    if not start or allowed(ramp, cfg, now) - int(ramp.get("base") or 0) >= _normal(cfg):
        db.set_kv(KEY, None)
        return None
    return ramp


def covers(ramp: dict, resources: set[str]) -> bool:
    """Whether the ramp limits a task on these resources."""
    return ramp.get("scope") is None or bool(set(ramp["scope"]) & set(resources))


def note(ramp: dict, cfg: dict, now: float) -> str:
    _, step = settings(cfg)
    return (f"{HELD_NOTE} {allowed(ramp, cfg, now)} workers now (after {ramp.get('cause')}), "
            f"doubling every {step / 60:g} min up to the normal limit")

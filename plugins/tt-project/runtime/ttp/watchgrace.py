"""A command schedule's grace window for keyed lines (payload `grace_min`, default 0: off).

A check of a route that heals on its own (a tunnel, a relay, a flaky link) would otherwise wake the
coordinator, page another project or open an issue on a short blip that is gone by the next run.
With `grace_min` set, a keyed line (one with a JSON `"key"`) is held until that key has been reported
for that long: before then nothing is observed, paged or counted. The first run past the grace raises
it once, at the line's own severity but at least `normal`; later runs report it at `low` under the
same key (a line rated above what was raised still goes through as rated: a real escalation).

The first time each key was seen is kept on disk (kv `watch_grace:<schedule>`), so the window spans
runs and daemon restarts. A run that ends normally without the key, or that reports it cleared,
forgets it: the next sighting counts afresh. A failed run (no reports) keeps the clock: it says
nothing about the route. Unkeyed lines and error lines are never held: their text may change every
run, so no clock could span them.
"""
from __future__ import annotations

import time

KV = "watch_grace:"   # + schedule name: {"<key>": {"first": ts, "last": ts, "raised": severity|None}}
HOLD, RAISE, REPEAT, PASS = "hold", "raise", "repeat", "pass"
_RANK = {"info": 0, "low": 0, "normal": 1, "high": 2, "critical": 3}


def grace_s(payload: dict | None) -> float:
    """The schedule's grace in seconds (0: off, also for a bad value)."""
    try:
        v = float((payload or {}).get("grace_min") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 60 if v == v and v > 0 else 0.0


def step(state: dict, key: str, now: float, grace: float, severity: str | None) -> tuple[str, str | None]:
    """One sighting of `key`: (HOLD, None) while inside the grace, (RAISE, severity) the first time
    past it or on a higher rating, (REPEAT, "low") after. Updates `state` in place."""
    if grace <= 0:
        return PASS, severity
    e = state.get(key)
    if not isinstance(e, dict) or not isinstance(e.get("first"), (int, float)):
        e = state[key] = {"first": now}
    e["last"] = now
    if now - float(e["first"]) < grace:
        return HOLD, None
    sev = severity if _RANK.get(severity or "", -1) > _RANK["normal"] else "normal"
    raised = e.get("raised")
    if raised is None or _RANK.get(sev, 1) > _RANK.get(raised, 1):
        e["raised"] = sev
        return RAISE, sev
    return REPEAT, "low"


def forget(state: dict, seen: set[str]) -> dict:
    """The keys still reported this run (a normal run: a key it no longer prints is over)."""
    return {k: v for k, v in state.items() if k in seen}


def status(state: dict, grace: float, key: str | None = None, now: float | None = None) -> list[str]:
    """One line per key: `ok`, `retrying` (inside its grace) or `down` (raised past it)."""
    now = time.time() if now is None else now
    keys = [key] if key else sorted(state)
    out = []
    for k in keys:
        e = state.get(k)
        if not isinstance(e, dict) or not isinstance(e.get("first"), (int, float)):
            out.append(f"{k}: ok")
            continue
        age = max(0.0, now - float(e["first"]))
        if grace > 0 and age < grace:
            out.append(f"{k}: retrying for {age / 60:.0f} min ({(grace - age) / 60:.0f} min of grace left)")
        else:
            out.append(f"{k}: down for {age / 60:.0f} min (past its {grace / 60:g} min grace)")
    return out

"""How long a lost runner's task waits for its replacement: model-free, read-only, from the runs table.

A run the daemon found dead (status lost) has `ended` set to its last sign of life (the newest lease or
output mtime, Daemon._last_sign_of_life), so the gap from there to the task's next worker run is the whole
cost of the loss: detection, requeue and dispatch. `gaps` lists them; `summary` gives count, median and p90.
A task that never got a next run counts as open until `now`."""
from __future__ import annotations

import time

TARGET_S = 120.0   # a lost runner is replaced within 2 min


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def gaps(db, since_s: float = 7 * 86400.0, now: float | None = None) -> list[dict]:
    """One row per worker run lost in the window: run, task, gap_s to the task's next worker run (or to now
    while it has none), open (no next run yet)."""
    now = time.time() if now is None else now
    out = []
    for r in db.q("SELECT id, task, started, ended FROM runs WHERE status='lost' AND task IS NOT NULL "
                  "AND role != 'coordinator' AND ended IS NOT NULL AND ended > ?", (now - since_s,)):
        nxt = db.one("SELECT started FROM runs WHERE task=? AND id>? AND role != 'coordinator' "
                     "ORDER BY id LIMIT 1", (r["task"], r["id"]))
        start = float(nxt["started"]) if nxt and nxt["started"] else now
        out.append({"run": r["id"], "task": r["task"], "gap_s": max(0.0, start - float(r["ended"])),
                    "open": not nxt})
    return out


def summary(db, since_s: float = 7 * 86400.0, now: float | None = None) -> dict:
    g = gaps(db, since_s, now)
    xs = [x["gap_s"] for x in g]
    return {"count": len(g), "median_s": _pct(xs, 0.5), "p90_s": _pct(xs, 0.9), "over_target": sum(x > TARGET_S for x in xs),
            "open": sum(x["open"] for x in g), "target_s": TARGET_S}

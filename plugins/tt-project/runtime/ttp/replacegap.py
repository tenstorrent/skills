"""How long a lost runner's task waits for its replacement: model-free, read-only, from the runs table.

A run the daemon found dead (status lost) has `ended` set to its last sign of life (the newest lease or
output mtime, Daemon._last_sign_of_life), so the gap from there to the task's next worker run is the whole
cost of the loss: detection, requeue and dispatch. A run a reboot cut counts from this boot's daemon start
instead (its note's daemon_at, else boot_at), so host downtime the harness cannot control is left out.
`gaps` lists them; `summary` gives count, median and p90. A task that never got a next run counts as open
until `now`."""
from __future__ import annotations

import json
import time

TARGET_S = 120.0   # a lost runner is replaced within 2 min


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def _since(r) -> float:
    """Where a lost run's wait starts: its last sign of life, or for one a reboot cut, the later of that
    and this boot's daemon start."""
    try:
        note = json.loads(r["note"] or "{}")
    except ValueError:
        note = {}
    if not (isinstance(note, dict) and note.get("lost_to_reboot")):
        return float(r["ended"])
    up = note.get("daemon_at") or note.get("boot_at")
    return max(float(r["ended"]), float(up)) if isinstance(up, (int, float)) else float(r["ended"])


def gaps(db, since_s: float = 7 * 86400.0, now: float | None = None) -> list[dict]:
    """One row per worker run lost in the window: run, task, gap_s to the task's next worker run (or to now
    while it has none), open (no next run yet)."""
    now = time.time() if now is None else now
    out = []
    for r in db.q("SELECT id, task, started, ended, note FROM runs WHERE status='lost' AND task IS NOT NULL "
                  "AND role != 'coordinator' AND ended IS NOT NULL AND ended > ?", (now - since_s,)):
        nxt = db.one("SELECT started FROM runs WHERE task=? AND id>? AND role != 'coordinator' "
                     "ORDER BY id LIMIT 1", (r["task"], r["id"]))
        start = float(nxt["started"]) if nxt and nxt["started"] else now
        out.append({"run": r["id"], "task": r["task"], "gap_s": max(0.0, start - _since(r)),
                    "open": not nxt})
    return out


def summary(db, since_s: float = 7 * 86400.0, now: float | None = None) -> dict:
    g = gaps(db, since_s, now)
    xs = [x["gap_s"] for x in g]
    return {"count": len(g), "median_s": _pct(xs, 0.5), "p90_s": _pct(xs, 0.9), "over_target": sum(x > TARGET_S for x in xs),
            "open": sum(x["open"] for x in g), "target_s": TARGET_S}

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Health of a release-ring flag in one project, model-free (the daemon's rollout watch, every 15 min).

A project is compared with itself: its window starts when the flag reached its ring, its baseline is the 7
days before that. `check` returns the failing signals (rollback), `verdict` also says whether the window has
enough samples to vote "clean". Promotion needs a clean vote from every project listed in the flag's current
ring, after that ring's soak; silence never promotes (too few samples stretch the window to
MAX_WINDOW_H, then a low note says so). Thresholds are settings: rollout.thresholds overrides DEFAULTS.
"""
from __future__ import annotations

import statistics
import time

DEFAULTS = {"failed_runs_pts": 5.0, "min_runs": 20, "land_time_x": 1.5, "cost_x": 1.3, "min_landings": 3,
            "stuck_h": 2.0, "baseline_days": 7.0, "soak_h_0": 1.0, "soak_h_1": 24.0, "max_window_h": 72.0}
# High alerts that mean the landing path is hurt, whichever flag is on.
ALERT_KEYS = ("push_not_landing", "push_queue_dying", "after_push_failed", "coordinator_failed", "push_tip_failing")
H = 3600.0


def thresholds(cfg: dict | None = None) -> dict:
    out = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("thresholds") or {}).items():
        if k in out and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = float(v)
    return out


def entered(flag: str, ring: int, recs: list[dict]) -> float | None:
    """When `flag` first reached a level covering `ring` in its current stretch (after the last rollback or
    off), from the ledger; None when it does not cover it."""
    since = None
    for r in recs:
        if r.get("flag") != flag or r.get("kind") not in ("set", "promote", "rollback"):
            continue
        to = r.get("to")
        if r["kind"] == "rollback" or to == "off":
            since = None
        elif isinstance(to, int) and to >= ring and since is None:
            since = r.get("ts")
        elif isinstance(to, int) and to < ring:
            since = None
    return since


def _med(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _runs(db, a: float, b: float) -> tuple[int, int]:
    rows = db.q("SELECT status FROM runs WHERE role!='coordinator' AND ended>=? AND ended<? AND status NOT IN "
                "('running','auth')", (a, b))
    return len(rows), sum(1 for r in rows if r["status"] in ("failed", "lost"))


def _landings(db, a: float, b: float) -> list[tuple[float, float]]:
    """(seconds from task creation to its first landing, the task's spend) per task landed in [a, b)."""
    rows = db.q("SELECT t.created c, t.spent_usd s, MIN(l.ts) l FROM task_landings l JOIN tasks t ON t.id=l.task "
                "WHERE t.kind='code' GROUP BY t.id HAVING MIN(l.ts)>=? AND MIN(l.ts)<?", (a, b))
    return [(r["l"] - (r["c"] or r["l"]), float(r["s"] or 0)) for r in rows]


def signals(db, since: float, now: float, th: dict) -> dict:
    """The window [since, now) against the baseline before `since`: the measures and the failing signals."""
    base_a = since - th["baseline_days"] * 86400
    nb, fb = _runs(db, base_a, since)
    nw, fw = _runs(db, since, now)
    lb, lw = _landings(db, base_a, since), _landings(db, since, now)
    fail: list[str] = []
    if nw >= th["min_runs"] and nb and 100.0 * (fw / nw - fb / nb) > th["failed_runs_pts"]:
        fail.append(f"failed runs {100 * fw / nw:.0f}% against {100 * fb / nb:.0f}% ({nw} runs)")
    if len(lw) >= th["min_landings"] and len(lb) >= 1:
        mw, mb = _med([x[0] for x in lw]), _med([x[0] for x in lb])
        if mb and mw > th["land_time_x"] * mb:
            fail.append(f"median time to land {mw / H:.1f} h against {mb / H:.1f} h ({len(lw)} landings)")
        cw, cb = sum(x[1] for x in lw) / len(lw), sum(x[1] for x in lb) / len(lb)
        if cb and cw > th["cost_x"] * cb:
            fail.append(f"cost per landing ${cw:.2f} against ${cb:.2f} ({len(lw)} landings)")
    seen = {r["key"] for r in db.q("SELECT key FROM alerts WHERE severity='high' AND raised>=? AND raised<?",
                                   (base_a, since))}
    for r in db.q("SELECT DISTINCT key FROM alerts WHERE severity='high' AND raised>=? AND raised<?", (since, now)):
        if r["key"] in ALERT_KEYS and r["key"] not in seen:
            fail.append(f"new high alert {r['key']}")
    rev = db.one("SELECT COUNT(*) n FROM tasks WHERE origin='daemon' AND title LIKE 'Redo #%' AND created>=? AND created<?",
                 (since, now))["n"]
    if rev:
        fail.append(f"{rev} forward revert(s) of a landed change")
    stuck = db.q("SELECT id FROM tasks WHERE status='running' AND updated<? AND NOT EXISTS "
                 "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')", (now - th["stuck_h"] * H,))
    if stuck:
        fail.append("tasks with no run and no change for " + f"{th['stuck_h']:g} h: " + ", ".join(f"#{r['id']}" for r in stuck[:5]))
    enough = nw >= th["min_runs"] or len(lw) >= th["min_landings"]
    return {"runs": nw, "failed": fw, "landings": len(lw), "base_runs": nb, "base_landings": len(lb),
            "failing": fail, "enough": enough}


def verdict(db, flag: str, ring: int, recs: list[dict], cfg: dict, now: float | None = None) -> dict | None:
    """None when `flag` does not cover `ring`; else signals() plus 'since' and 'level' (the ring entered)."""
    now = now or time.time()
    since = entered(flag, ring, recs)
    if since is None:
        return None
    v = signals(db, since, now, thresholds(cfg))
    v["since"] = since
    return v


def ready(flag: str, level: int, project_names: list[str], recs: list[dict], cfg: dict, now: float | None = None) -> bool:
    """Whether `flag` may go from `level` to the next ring: its soak has passed and every project listed in that
    ring has a clean vote (a `clean` ledger record) since the flag reached the level."""
    now = now or time.time()
    th = thresholds(cfg)
    since = entered(flag, level, recs)
    if since is None or level not in (0, 1) or not project_names:
        return False
    if now - since < th[f"soak_h_{level}"] * H:
        return False
    voted = {r.get("reason", "").split(" ")[0] for r in recs
             if r.get("flag") == flag and r.get("kind") == "clean" and (r.get("ts") or 0) >= since}
    return all(n in voted for n in project_names)

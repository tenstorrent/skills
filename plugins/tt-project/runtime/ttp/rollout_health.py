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

import re
import statistics
import subprocess
import time

from . import postland, pushq, rollout

DEFAULTS = {"failed_runs_pts": 5.0, "min_runs": 20, "land_time_x": 1.5, "cost_x": 1.3, "min_landings": 3,
            "stuck_h": 2.0, "baseline_days": 7.0, "soak_h_0": 1.0, "soak_h_1": 24.0, "max_window_h": 72.0}
# Alerts a flag is judged on: only those of its own area (rollout.FLAGS[flag]["area"]), each a key prefix, new
# since the baseline. Only a high alert counts: it is the one stored in the alerts table. An area with no alert of
# its own is an empty tuple and is judged on the generic signals alone (failed runs, time to land, cost per
# landing, stuck tasks, forward reverts), which cover the landing path. The prefixes are keys the runtime raises
# and stores there (a test checks each against the source and the real raise path).
ALERT_AREAS = {
    "push": (pushq.LANDING_ALERT, pushq.BAD_TIP_ALERT, "push_queue_dying", "after_push_failed"),
    "route": ("auth:",),
    "eta": (),
    "device": (),
    "review": (),
    "worker": (),
    "coordinator": (),
    "output": (),
}
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


_SPAN = re.compile(r"Forward revert of ([0-9a-f]*)\.\.([0-9a-f]+) by ")


def _reverted_files(repo: str | None, spec: str) -> set[str] | None:
    """The files the reverted span (a revert task's "Forward revert of <base>..<sha>") changed, read from git
    in `repo`; None when the span or the repo cannot be read."""
    m = _SPAN.search(spec or "")
    if not m or not repo:
        return None
    base, sha = m.groups()
    cmd = ["git", "-C", repo, "diff", "--name-only", base, sha] if base else \
        ["git", "-C", repo, "show", "--name-only", "--format=", sha]
    try:
        got = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(got.stdout.split()) if got.returncode == 0 else None


def _reverts(db, since: float, now: float, flag: str | None, repo: str | None) -> tuple[list[str], list[str]]:
    """(the revert tasks that count against `flag`, those whose reverted commit cannot be resolved). A revert
    counts only when the commit it reverted changed a file of the flag's gated code (rollout.FLAGS[flag]["code"]);
    never against a shadow flag; an unresolved one counts against no flag."""
    meta = rollout.FLAGS.get(flag or "", {})
    code = set(meta.get("code") or ())
    hit, unknown = [], []
    for r in db.q("SELECT id, spec FROM tasks WHERE title LIKE ? ESCAPE '\\' AND created>=? AND created<?",
                  ("Revert %" + postland.REVERT_TITLE_TAIL.replace("%", "\\%").replace("_", "\\_"), since, now)):
        files = _reverted_files(repo, r["spec"])
        if files is None:
            unknown.append(f"#{r['id']}")
        elif code and not meta.get("shadow") and files & code:
            hit.append(f"#{r['id']}")
    return hit, unknown


def signals(db, since: float, now: float, th: dict, area: str | None = None, flag: str | None = None,
            repo: str | None = None) -> dict:
    """The window [since, now) against the baseline before `since`: the measures and the failing signals.
    `area` is the flag's area: a new high alert counts only if its key starts with one of ALERT_AREAS[area].
    `flag` and `repo` (the project's git root) attribute forward reverts: see _reverts; "unattributed" lists
    the reverts whose commit could not be read, shown and never failing."""
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
    prefixes = ALERT_AREAS.get(area or "", ())

    def raised(a: float, b: float) -> set[str]:
        return {r["key"] for r in db.q("SELECT DISTINCT key, severity FROM alerts WHERE raised>=? AND raised<?", (a, b))
                if r["key"].startswith(prefixes) and r["severity"] == "high"}
    seen = raised(base_a, since)
    for key in sorted(raised(since, now) - seen):
        fail.append(f"new alert {key}")
    rev, unattributed = _reverts(db, since, now, flag, repo)
    if rev:
        fail.append(f"{len(rev)} forward revert(s) of a landed change touching this flag's code: {', '.join(rev[:5])}")
    stuck = db.q("SELECT id FROM tasks WHERE status='running' AND updated<? AND NOT EXISTS "
                 "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')", (now - th["stuck_h"] * H,))
    if stuck:
        fail.append("tasks with no run and no change for " + f"{th['stuck_h']:g} h: " + ", ".join(f"#{r['id']}" for r in stuck[:5]))
    enough = nw >= th["min_runs"] or len(lw) >= th["min_landings"]
    return {"runs": nw, "failed": fw, "landings": len(lw), "base_runs": nb, "base_landings": len(lb),
            "failing": fail, "enough": enough, "unattributed": unattributed}


def verdict(db, flag: str, ring: int, recs: list[dict], cfg: dict, now: float | None = None,
            repo: str | None = None) -> dict | None:
    """None when `flag` does not cover `ring`; else signals() plus 'since' and 'level' (the ring entered)."""
    now = now or time.time()
    since = entered(flag, ring, recs)
    if since is None:
        return None
    v = signals(db, since, now, thresholds(cfg), rollout.FLAGS.get(flag, {}).get("area"), flag, repo)
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

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A task's detached jobs' finish from this project's measured job history (eta.py's device leg).

history() reads the runs the db knows in the window and their detach registries
(state/runs/<id>/detached.json): every ended job's duration by normalised name (a trailing run word or
id is dropped, so 'load-run2', 'load2' and 'load' are one job) and each run's start-up wait (its
start to its first job). finish() gives a task's finish from its own registry entries
(jobstop.task_jobs, so a task that continues another also reads its jobs): each job's time left is
the quantile of its name's samples less what has already elapsed, plus the queue wait behind
another open task's jobs of the same name (its running one and those queued ahead of ours). A job
name with no sample gives no estimate, never a padded default; the estimate says why.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

WINDOW_S = 14 * 86400
ROWS = 500
P50 = 0.5
P80 = 0.8


def normal(name) -> str:
    """A job name without its trailing run word or id: 'load-run2', 'load2' and 'load' are one job."""
    s = re.sub(r"(?:[-_ ]?run)?[-_ ]*\d+$", "", str(name or "").strip())
    return s or str(name or "").strip()


def job_history(state, db, now: float | None = None) -> dict:
    """The window's measured job history: {"job": {normalised name: [durations]}, "startup": [each
    run's wait from its start to its first detached job]}."""
    now = time.time() if now is None else now
    h: dict = {"job": {}, "startup": []}
    try:
        rows = db.q("SELECT id, started, dir FROM runs WHERE started>? AND role!='coordinator' "
                    "ORDER BY id DESC LIMIT ?", (now - WINDOW_S, ROWS))
    except Exception:
        return h
    runs = Path(state) / "runs"
    for r in rows:
        d = Path(r["dir"]) if r["dir"] else runs / str(r["id"])
        if d.resolve().parent != runs.resolve():
            continue   # a registry outside the project's runs folder is not this project's work
        try:
            jobs = json.loads((d / "detached.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(jobs, list):
            continue
        first = None
        for j in jobs:
            if not isinstance(j, dict):
                continue
            s, e = j.get("started"), j.get("ended")
            if j.get("status") == "ended" and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                  for x in (s, e)) and float(e) > float(s) > 0:
                h["job"].setdefault(normal(j.get("name")), []).append(float(e) - float(s))
            if isinstance(s, (int, float)) and not isinstance(s, bool) and float(s) > 0:
                first = float(s) if first is None else min(first, float(s))
        if isinstance(r["started"], (int, float)) and first is not None and first > float(r["started"]):
            h["startup"].append(first - float(r["started"]))
    return h


def job_entries(db, tid: int, cache: dict | None = None) -> list[dict]:
    """The registry entries of task `tid`'s detached jobs (jobstop.task_jobs, ancestors included).
    `cache` memoizes per task id across one estimate walk's queue scan (a dict the caller owns)."""
    if cache is not None and tid in cache:
        return cache[tid]
    import types
    from . import jobstop
    got = [e for _, e in jobstop.task_jobs(types.SimpleNamespace(db=db, runs=str(Path(db.path).parent / "runs")), tid)]
    if cache is not None:
        cache[tid] = got
    return got


def _is_queued(e: dict) -> bool:
    """A job the daemon has not started: recorded queued, or with no start time yet."""
    return e.get("status") == "queued" or not e.get("started")


def job_left(e: dict, xs: list, now: float, q: float) -> float | None:
    """A job's time left at quantile `q` of its name's samples: 0 once it ended, the full sample
    while it is queued, the sample less what has elapsed while it runs; None with no sample."""
    from .eta import pct
    rc = str(e.get("rc") or "")
    if e.get("status") in ("ended", "died") or (rc and (Path(rc)).exists()):
        return 0.0
    m = pct(xs, q)
    if m is None:
        return None
    if _is_queued(e):
        return m
    return max(0.0, m - (now - float(e["started"])))


def job_finish(db, t: dict, h: dict, now: float, cache: dict | None = None) -> dict | None:
    """The measured finish of task `t`'s detached jobs: {"end": the median (p50) finish,
    "conservative": the p80 one, "legs": [(name, seconds)], "why"}; None when the task has no
    registry entries. Legs are one per still-unfinished job, "queue" for the wait behind another open
    task's running job of the same name, "startup" while no job has started yet."""
    from .eta import pct
    ents = job_entries(db, t["id"], cache)
    if not ents:
        return None
    legs, hi, why, started, own = [], [], None, False, []
    for e in ents:
        name = normal(e.get("name"))
        xs = (h.get("job") or {}).get(name) or []
        left = job_left(e, xs, now, P50)
        if left is None:
            why = f"no measured {name} job run yet"
            continue
        if _is_queued(e):
            own.append((name, float(e.get("started") or 0)))   # when it entered its queue
        legs.append((name, left))
        l80 = job_left(e, xs, now, P80)
        hi.append(l80 if l80 is not None else left)
        started = started or e.get("status") not in ("queued",)
    if why:
        return {"end": None, "conservative": None, "legs": [], "why": why}
    wait = queue_wait(db, t["id"], own, h, now, cache)
    if wait:
        legs.append(("queue", wait))
        hi.append(wait)
    if not started and (s := pct(h.get("startup") or [], P50)) is not None:
        legs.insert(0, ("startup", s))
        hi.insert(0, s)
    open_jobs = sum(1 for e in ents if e.get("status") not in ("ended", "died"))
    return {"end": now + sum(x for _, x in legs), "conservative": now + sum(hi), "legs": legs,
            "why": None, "open": open_jobs}


def queue_wait(db, tid: int, own: list, h: dict, now: float, cache: dict | None = None) -> float:
    """The wait of a task's own queued jobs (`own`: (name, when it was queued) pairs) behind other open
    tasks' jobs of the same name: each waits for the running same-name job with the longest time left,
    plus the median of every same-name job queued ahead of it. The longest of those waits; 0 with
    nothing ahead, and none when the task's own job of that name is already running."""
    from .eta import pct
    best = 0.0
    rows = [r["id"] for r in db.q("SELECT id FROM tasks WHERE status IN ('running','waiting')")]
    for name, when in own:
        wait, run_left = 0.0, 0.0
        for rid in rows:
            if rid == tid:
                continue   # its own running job of the name holds the device: no queue
            for e in job_entries(db, rid, cache):
                if normal(e.get("name")) != name or e.get("status") in ("ended", "died"):
                    continue
                m = pct((h.get("job") or {}).get(name) or [], P50)
                if m is None:
                    continue
                if _is_queued(e):
                    if float(e.get("started") or 0) < when:   # queued ahead of ours
                        wait += m
                elif e.get("started"):
                    run_left = max(run_left, m - (now - float(e["started"])))
        best = max(best, wait + max(0.0, run_left))
    return best

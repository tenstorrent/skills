# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-task run cap at dispatch, beside chaincap's per-lineage caps: no task loops on its own.

A task whose runs since its last explicit requeue (dispatchguard.EXPLICIT_SOURCES: the coordinator, the
user or a chain continue; never the daemon's or push queue's own requeues) reach `chains.max_task_runs`
(4), or whose last `chains.max_idle_runs` (2) runs each spent money with no progress, is not started again. Daemon retries (a tier up after a failure, a lost or
stalled run, a waiting wake, a restart) all count; runs that never reached the model (FREE) or were preempted for urgent work do not.
A run that handed off `waiting` on its own progress (unblock.classify_wait 'self': its detached checks, a
batch or Slurm job, a planned window) is a step of one try, not a retry: it counts toward
`chains.max_self_waits` (12) instead, so a task mid-gate is not blocked while a fix-and-recheck loop still
ends; the idle rule and the task's budget bound it too.
Progress is a hand-off of done or waiting, or a commit made inside the run (chaincap.code_commits).
The task is blocked, and the coordinator gets one RUN_CAP_EVENT per requeue with a model-free summary:
each run's tier, end, spend and what it handed off, and the last error. The coordinator decides (a new
approach, a narrower spec, cancel); its requeue starts the count again. 0 turns a cap off.
Routine default (routinedefault.py): a task's first cap is only held and listed in the next digest, no
wake; the same task at its cap again wakes the coordinator."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import chaincap, routinedefault, unblock
from .db import load_result
from .carry import ROLE as CARRY_ROLE
from .dispatchguard import EXPLICIT_SOURCES, REQUEUED

RUN_CAP_EVENT = "run_cap"
TRIGGER = "task at its run cap"
FREE = ("limit", "auth", "fallback")   # ends that never reached the model: not a run here
PROGRESS = ("done", "waiting")
LINE_CHARS = 160


def limits(cfg: dict) -> tuple[int, int, int]:
    c = cfg.get("chains") or {}
    out = []
    for k in ("max_task_runs", "max_idle_runs", "max_self_waits"):
        try:
            out.append(max(0, int(c.get(k, chaincap.DEFAULTS[k]))))
        except (TypeError, ValueError):
            out.append(chaincap.DEFAULTS[k])
    return out[0], out[1], out[2]


# An ok inline review run of a code task (carry.py) is no try of its task: it counts toward no run cap or rank.
NOT_CARRY_OK = f"NOT (role='{CARRY_ROLE}' AND status='ok' AND task IN (SELECT id FROM tasks WHERE kind='code'))"


def runs(db, task_id: int) -> tuple[list[dict], int]:
    """The task's ended runs since its last explicit requeue, oldest first, and that requeue's event id (0)."""
    last = db.one("SELECT id, ts FROM events WHERE task=? AND kind=? "
                  f"AND source IN ({','.join('?' * len(EXPLICIT_SOURCES))}) ORDER BY id DESC LIMIT 1",
                  (task_id, REQUEUED, *EXPLICIT_SOURCES))
    rows = db.q("SELECT * FROM runs WHERE task=? AND role!='coordinator' AND ended IS NOT NULL AND started>=? "
                f"AND status NOT IN ({','.join('?' * len(FREE))}) AND {NOT_CARRY_OK} ORDER BY id",
                (task_id, float((last or {}).get("ts") or 0), *FREE))
    cut = [float(e["ts"]) for e in db.q("SELECT ts FROM events WHERE task=? AND kind='task_preempted' AND ts>=?",
                                        (task_id, float((last or {}).get("ts") or 0)))]
    # A run stopped to give its slot to urgent work (preempt.py) was not the task's try: it does not count.
    rows = [r for r in rows if not (r["status"] == "shutdown" and any(r["started"] <= t <= r["ended"] + 1 for t in cut))]
    return rows, int((last or {}).get("id") or 0)


def handoff(run: dict) -> dict:
    try:
        return load_result((Path(run["dir"]) / "result.json").read_text()) if run.get("dir") else {}
    except OSError:
        return {}


def self_wait(run: dict) -> bool:
    """Whether `run` handed off `waiting` on the task's own progress (its checks, job or window)."""
    h = handoff(run)
    return h.get("status") == "waiting" and unblock.classify_wait(h)[0] == "self"


def committed(p, task: dict, run: dict) -> bool | None:
    """Whether a code or harness `run` made a commit inside it; None when git (or no `p`) cannot tell. The
    waste ledger reads the same test (waste.py, over_runs)."""
    if task.get("kind") not in ("code", "harness"):
        return False
    try:
        win = [(int(run["started"]), -int(-float(run["ended"]) // 1))]
        return bool(chaincap.code_commits(p, task, wins=win))
    except Exception:
        return None


def idle(p, task: dict, run: dict) -> bool:
    """Whether `run` spent money and made no progress; git unreadable is no proof it made nothing."""
    return (bool(float(run.get("cost_usd") or 0)) and handoff(run).get("status") not in PROGRESS
            and committed(p, task, run) is False)


def why(p, cfg: dict, task: dict, rows: list[dict]) -> str:
    most, nidle, nself = limits(cfg)
    waits = sum(1 for r in rows if self_wait(r))
    tries = len(rows) - waits
    if most and tries >= most:
        also = f", not counting {waits} waits on its own checks or jobs" if waits else ""
        return (f"it already ran {tries} times since its last explicit requeue{also} "
                f"(chains.max_task_runs {most})")
    if nself and waits >= nself:
        return (f"it handed off {waits} waits on its own checks or jobs since its last explicit requeue "
                f"(chains.max_self_waits {nself})")
    tail = rows[-nidle:] if nidle else []
    if tail and len(tail) == nidle and all(idle(p, task, r) for r in tail):
        return (f"its last {nidle} runs spent ${sum(float(r['cost_usd'] or 0) for r in tail):.2f} with no hand-off "
                f"and no commit (chains.max_idle_runs {nidle})")
    return ""


def summary(task: dict, rows: list[dict]) -> str:
    lines = []
    for r in rows:
        h = handoff(r)
        note = load_result(r.get("note"))
        said = str(h.get("summary") or note.get("error") or note.get("launch_error") or "no hand-off")
        lines.append(f"run {r['id']} ({r.get('effort') or r.get('model') or '?'}): {r['status']}, "
                     f"${float(r['cost_usd'] or 0):.2f}, {h.get('status') or 'none'}: {' '.join(said.split())[:LINE_CHARS]}")
    last = load_result(task.get("result"))
    err = str(last.get("error") or last.get("summary") or task.get("blocked_reason") or "none")
    total = sum(float(r["cost_usd"] or 0) for r in rows)
    return (f"{len(rows)} runs, ${total:.2f}. " + "; ".join(lines)
            + f". Last error: {' '.join(err.split())[:LINE_CHARS * 2]}")


def check(p, cfg: dict, task: dict) -> str:
    """Why `task` must not start (it is blocked and the coordinator told once), or '' when it may."""
    db = p.db
    rows, req = runs(db, task["id"])
    if not rows or not (reason := why(p, cfg, task, rows)):
        return ""
    text = summary(task, rows)
    fp = f"{RUN_CAP_EVENT}:{task['id']}:{req}"
    # Routine default: the first cap only holds it and lists it in the next digest; the same task back wakes.
    quiet = routinedefault.on(cfg) and not routinedefault.seen(db, RUN_CAP_EVENT, f"#{task['id']}")
    with db.tx():
        db.x("UPDATE tasks SET status='blocked', blocked_reason=?, updated=? WHERE id=? AND status='queued'",
             (f"run cap: {reason}"[:500], time.time(), task["id"]))
        if not db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", RUN_CAP_EVENT, fp, "normal",
                  f"{TRIGGER}: #{task['id']} {task['title']}: {reason}. The daemon starts no same-approach retry. "
                  f"{text} Decide: requeue it with a new approach or narrower spec, or cancel it.",
                  json.dumps({"reason": reason, "runs": [r["id"] for r in rows]}), "handled" if quiet else "queued",
                  task["id"]))
            if quiet:
                routinedefault.record(db, RUN_CAP_EVENT, f"#{task['id']}",
                                      f"held {task['title'][:80]}: {reason}. {text}"[:300],
                                      "requeue it with a new approach or narrower spec, or cancel it")
    return reason

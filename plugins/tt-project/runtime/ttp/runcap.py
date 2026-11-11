# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-task run cap at dispatch, beside chaincap's per-lineage caps: no task loops on its own.

A task whose runs since its last explicit requeue (dispatchguard.REQUEUED: the coordinator, the user or a
chain continue) reach `chains.max_task_runs` (4), or whose last `chains.max_idle_runs` (2) runs each
spent money with no progress, is not started again. Daemon retries (a tier up after a failure, a lost or
stalled run, a waiting wake, a restart) all count; runs that never reached the model (FREE) do not.
Progress is a hand-off of done or waiting, or a commit made inside the run (chaincap.code_commits).
The task is blocked, and the coordinator gets one RUN_CAP_EVENT per requeue with a model-free summary:
each run's tier, end, spend and what it handed off, and the last error. The coordinator decides (a new
approach, a narrower spec, cancel); its requeue starts the count again. 0 turns a cap off."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import chaincap
from .db import load_result
from .dispatchguard import REQUEUED

RUN_CAP_EVENT = "run_cap"
TRIGGER = "task at its run cap"
FREE = ("limit", "auth", "fallback")   # ends that never reached the model: not a run here
PROGRESS = ("done", "waiting")
LINE_CHARS = 160


def limits(cfg: dict) -> tuple[int, int]:
    c = cfg.get("chains") or {}
    out = []
    for k in ("max_task_runs", "max_idle_runs"):
        try:
            out.append(max(0, int(c.get(k, chaincap.DEFAULTS[k]))))
        except (TypeError, ValueError):
            out.append(chaincap.DEFAULTS[k])
    return out[0], out[1]


def runs(db, task_id: int) -> tuple[list[dict], int]:
    """The task's ended runs since its last explicit requeue, oldest first, and that requeue's event id (0)."""
    last = db.one("SELECT id, ts FROM events WHERE task=? AND kind=? ORDER BY id DESC LIMIT 1", (task_id, REQUEUED))
    rows = db.q("SELECT * FROM runs WHERE task=? AND role!='coordinator' AND ended IS NOT NULL AND started>=? "
                f"AND status NOT IN ({','.join('?' * len(FREE))}) ORDER BY id",
                (task_id, float((last or {}).get("ts") or 0), *FREE))
    return rows, int((last or {}).get("id") or 0)


def handoff(run: dict) -> dict:
    try:
        return load_result((Path(run["dir"]) / "result.json").read_text()) if run.get("dir") else {}
    except OSError:
        return {}


def idle(p, task: dict, run: dict) -> bool:
    """Whether `run` spent money and made no progress: no done or waiting hand-off, no commit inside it."""
    if not float(run.get("cost_usd") or 0) or handoff(run).get("status") in PROGRESS:
        return False
    if task.get("kind") not in ("code", "harness"):
        return True
    try:
        win = [(int(run["started"]), -int(-float(run["ended"]) // 1))]
        return not chaincap.code_commits(p, task, wins=win)
    except Exception:
        return False   # git unreadable: no proof it made nothing


def why(p, cfg: dict, task: dict, rows: list[dict]) -> str:
    most, nidle = limits(cfg)
    if most and len(rows) >= most:
        return f"it already ran {len(rows)} times since its last explicit requeue (chains.max_task_runs {most})"
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
    with db.tx():
        db.x("UPDATE tasks SET status='blocked', blocked_reason=?, updated=? WHERE id=? AND status='queued'",
             (f"run cap: {reason}"[:500], time.time(), task["id"]))
        if not db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (time.time(), f"task:{task['id']}", RUN_CAP_EVENT, fp, "normal",
                  f"{TRIGGER}: #{task['id']} {task['title']}: {reason}. The daemon starts no same-approach retry. "
                  f"{text} Decide: requeue it with a new approach or narrower spec, or cancel it.",
                  json.dumps({"reason": reason, "runs": [r["id"] for r in rows]}), "queued", task["id"]))
    return reason

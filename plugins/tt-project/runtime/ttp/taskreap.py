# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Records kept in a task's name, released once the task ended (model-free, on the daemon tick).

Two records name a task rather than a live process, so the lock reaper (lockreap: a dead holder pid)
never frees them:
- a reservation (`<lock>.reserved`, locks.reserve): dispatch reserves an exclusive resource for a ready
  task so new `ttp lock` commands wait. It lapses after locks.RESERVE_STALE_S unless refreshed; this
  removes it at once.
- a device-queue job (`ttp devq submit`): queued or running on a device runner's host under the task's
  id. Nothing else drops it when the task ends: it keeps the device until it ends or times out. Each
  submit records it here (LEDGER), and the sweep asks the runner's host to drop the task's jobs
  (`devq.py drop`): a queued one ends `cancelled` before it starts, a running one is stopped and ends
  `cancelled`, so `ttp devq probe` still answers.

A task's records are released once it is cancelled, failed, exhausted or done, no run of it is live
(runs.status 'running': the daemon marks a dead run lost, so a daemon restart reaps the rest on its
first sweeps) and its state is at least GRACE_S old, so a run still starting or a task requeued at once
keeps them. A task with a device job an open task's hand-off names (its id in its result) keeps its jobs.
sweep returns each released record once; the daemon logs it and sends one low alert.

Gaps: a device job queued before submit recorded its project has no ledger entry and is never dropped
here; a host that does not answer is asked again after RETRY_S.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import hold, locks
from .project import durable_write

ENDED = ("cancelled", "failed", "exhausted", "done")
GRACE_S = 120          # a task that ended this recently keeps its records (a run still starting)
SWEEP_EVERY_S = 60
RETRY_S = 600          # a runner host that failed to answer is asked again after this
HOST_CALLS = 3         # host calls per sweep at most, so a slow host never stalls a tick long
LEDGER = "devq-jobs"   # state/devq-jobs/<runner>/<job>.json, written by `ttp devq submit`


def record_job(p, runner: str, spec: dict) -> None:
    """Note a submitted device job, so its task's end drops it. Best effort: a submit never fails here."""
    if not str(spec.get("task") or "").isdigit():
        return
    try:
        d = p.state / LEDGER / runner
        d.mkdir(parents=True, exist_ok=True)
        durable_write(d / f"{spec['id']}.json", json.dumps({"runner": runner, "job": spec["id"],
                                                           "task": int(spec["task"]), "ts": time.time()}))
    except (OSError, KeyError, ValueError):
        pass


def ended(db, tid: int, now: float, grace_s: float = GRACE_S) -> dict | None:
    """The task row when it ended at least grace_s ago with no live run, else None."""
    t = db.one("SELECT id, status, updated FROM tasks WHERE id=?", (tid,))
    if not t or t["status"] not in ENDED or now - float(t["updated"] or 0) < grace_s:
        return None
    if db.one("SELECT id FROM runs WHERE task=? AND status='running' LIMIT 1", (tid,)):
        return None
    return t


def reservations(p, dirs: list[Path], now: float, grace_s: float = GRACE_S) -> list[dict]:
    """Remove each live reservation in `dirs` held for an ended task of this project."""
    out = []
    for d in dirs:
        try:
            marks = sorted(Path(d).glob("*.reserved"))
        except OSError:
            continue
        for path in marks:
            who = locks.reserved_by(path)
            tid = hold.task_of(hold.label(who, "?"), p.name) if who else None
            t = ended(p.db, tid, now, grace_s) if tid is not None else None
            if not t:
                continue
            try:
                since = json.loads(path.read_text()).get("since")
            except (OSError, ValueError, AttributeError):
                since = None
            locks.unreserve(path, who)
            if not path.exists():
                name = path.name[:-len(".reserved")]
                out.append({"kind": "resource reservation", "name": name, "task": tid, "status": t["status"],
                            "key": f"{name}:{tid}:{since}"})
    return out


def waited_on(db, job: str) -> int | None:
    """An open task whose hand-off names this device job id, else None."""
    import re
    rx = re.compile(rf"(?<![\w.-]){re.escape(job)}(?![\w.-])")
    for t in db.q("SELECT id, result FROM tasks WHERE status NOT IN (?,?,?,?) AND result LIKE ?",
                  (*ENDED, f"%{job}%")):
        if rx.search(str(t["result"] or "")):
            return t["id"]
    return None


def device_jobs(p, runners: dict, call, now: float, grace_s: float = GRACE_S, state: dict | None = None) -> list[dict]:
    """Ask each runner host to drop the jobs of ended tasks in the ledger. call(runner, rc, task, why)
    returns the host's (rc, stdout). `state` keeps the next try per (runner, task) across sweeps."""
    state = state if state is not None else {}
    out, calls = [], 0
    root = p.state / LEDGER
    try:
        entries = sorted(root.glob("*/*.json"))
    except OSError:
        return out
    tasks: dict[tuple, list[Path]] = {}
    for e in entries:
        try:
            rec = json.loads(e.read_text())
            key = (str(rec["runner"]), int(rec["task"]))
        except (OSError, ValueError, KeyError, TypeError):
            e.unlink(missing_ok=True)
            continue
        if key[0] not in runners:
            e.unlink(missing_ok=True)   # the runner is no longer configured: nothing to ask
            continue
        tasks.setdefault(key, []).append(e)
    for (runner, tid), files in tasks.items():
        t = ended(p.db, tid, now, grace_s)
        if not t or state.get((runner, tid), 0) > now:
            continue
        if any(waited_on(p.db, f.stem) for f in files):
            continue   # an open task still waits on one: the host drops all of the task's jobs at once
        if calls >= HOST_CALLS:
            break
        calls += 1
        rc, text = call(runner, runners[runner], str(tid), f"task #{tid} is {t['status']}")
        try:
            dropped = json.loads((text or "").strip().splitlines()[-1]) if rc == 0 else None
        except (ValueError, IndexError):
            dropped = None
        if not isinstance(dropped, list):
            state[(runner, tid)] = now + RETRY_S
            continue
        state.pop((runner, tid), None)
        for f in files:
            f.unlink(missing_ok=True)
        for j in dropped:
            if isinstance(j, dict) and j.get("id"):
                out.append({"kind": f"device-queue job ({j.get('was', '?')})", "name": f"{runner}/{j['id']}",
                            "task": tid, "status": t["status"], "key": f"{runner}/{j['id']}"})
    return out

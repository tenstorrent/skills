"""Dispatch-time guard: a task that is done, cancelled or failed, or one already closed done as often as
its attempt limit since its last explicit requeue, is never started again.

Dispatch picks from a list read at the start of the tick (db.ready_tasks); a write between that read and
the start (a cancel, a run end, a stale row written back) can leave the list behind the store. And a task
that some unlogged write put back to the queue after a done hand-off would be run again on every wake,
each run handing off done once more. `refusal` re-reads the row just before the start; `refuse` puts an
over-limit task back to its last hand-off status and records why, model-free.

Every explicit requeue records a `task_requeued` event (`ttp task requeue`, the web app, the coordinator's
status queued on an ended task, a chain continue, the push queue's review re-runs, the daemon's lost-run
requeues): the done hand-offs before it no longer count."""
from __future__ import annotations

import time

from .db import load_result

REQUEUED = "task_requeued"   # event kind of an explicit requeue: the done count starts again after it
REFUSED = "dispatch_refused"


def done_closes(db, task_id: int) -> int:
    """How many times the task handed off done since its last explicit requeue."""
    last = db.one("SELECT MAX(id) AS id FROM events WHERE task=? AND kind=?", (task_id, REQUEUED))
    since = (last or {}).get("id") or 0
    row = db.one("SELECT COUNT(*) AS n FROM events WHERE task=? AND kind='task_done' AND id>?", (task_id, since))
    return int((row or {}).get("n") or 0)


def refusal(db, task: dict) -> str:
    """Why `task` must not start now ('' when it may), from a fresh read of its row."""
    t = db.task(task["id"])
    if not t:
        return "it no longer exists"
    if t["status"] != "queued":
        return f"it is {t['status']} now (picked while queued)"
    limit = int(t.get("max_attempts") or 3)
    if load_result(t.get("result")).get("status") == "done" and (n := done_closes(db, t["id"])) >= limit:
        return (f"it was already closed done {n} times (attempt limit {limit}) and was queued again "
                f"without an explicit requeue; attempts {int(t.get('attempts') or 0)} of {limit}")
    return ""


def refuse(db, task_id: int, why: str) -> None:
    """Put an over-limit queued task back to done (its last hand-off) and record why. A task no longer
    queued is left as it is: its status already keeps it from starting."""
    now = time.time()
    with db.tx():
        t = db.task(task_id)
        if t and t["status"] == "queued":
            db.x("UPDATE tasks SET status='done', updated=? WHERE id=? AND status='queued'", (now, task_id))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{task_id}", REFUSED, "low",
                  f"#{task_id} not dispatched: {why}. Put back to done; `ttp task requeue` runs it again.",
                  "handled", task_id))


def record_requeue(db, task_id: int, prev: str, by: str) -> None:
    """An explicit requeue of a task that had ended (or was blocked): logged, and the done count restarts."""
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (time.time(), by, REQUEUED, "low", f"#{task_id} requeued from {prev} by {by}", "handled", task_id))


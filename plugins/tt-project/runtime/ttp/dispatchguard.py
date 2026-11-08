"""Dispatch-time guard: a task that is done, cancelled or failed, or one already closed done as often as
its attempt limit since its last explicit requeue, is never started again.

Dispatch picks from a list read at the start of the tick (db.ready_tasks); a write between that read and
the start (a cancel, a run end, a stale row written back) can leave the list behind the store. And a task
that some unlogged write put back to the queue after a done hand-off would be run again on every wake,
each run handing off done once more. `refusal` re-reads the row just before the start; `refuse` puts an
over-limit task back to its last hand-off status and records why, model-free.

Every explicit requeue records a `task_requeued` event (`ttp task requeue`, the web app, the coordinator's
status queued on an ended task, a chain continue, the push queue's review re-runs, the daemon's lost-run
requeues): the done hand-offs before it no longer count. A task past its attempt limit whose last hand-off
was done (its last run ended in a task_done event) is refused too
unless an explicit requeue came after that hand-off, start_when probe or not: a deferral that passes
only makes it ready, never a reason to run it again."""
from __future__ import annotations

import time

from .db import load_result

REQUEUED = "task_requeued"   # event kind of an explicit requeue: the done count starts again after it
REFUSED = "dispatch_refused"
DONE_EVENT_SLACK_S = 60   # a run's end and its task_done event are written apart, within this


def done_closes(db, task_id: int) -> int:
    """How many times the task handed off done since its last explicit requeue."""
    last = db.one("SELECT MAX(id) AS id FROM events WHERE task=? AND kind=?", (task_id, REQUEUED))
    since = (last or {}).get("id") or 0
    row = db.one("SELECT COUNT(*) AS n FROM events WHERE task=? AND kind='task_done' AND id>?", (task_id, since))
    return int((row or {}).get("n") or 0)


def ended_done_unrequeued(db, task_id: int) -> bool:
    """Whether the task's last run ended in a done hand-off (its task_done event at or after that run's
    end) and no explicit requeue came after it. A run whose end queued the task again for its own
    reasons (a push rework, a busy resource) writes no task_done event and is not this."""
    ev = db.one("SELECT MAX(ts) AS ts FROM events WHERE task=? AND kind='task_done'", (task_id,))
    run = db.one("SELECT MAX(ended) AS ts FROM runs WHERE task=?", (task_id,))
    done_ts, run_ts = float((ev or {}).get("ts") or 0), float((run or {}).get("ts") or 0)
    if not done_ts or done_ts < run_ts - DONE_EVENT_SLACK_S:
        return False
    return not db.one("SELECT 1 FROM events WHERE task=? AND kind=? AND ts>=?",
                      (task_id, REQUEUED, max(done_ts, run_ts)))


def refusal(db, task: dict) -> str:
    """Why `task` must not start now ('' when it may), from a fresh read of its row."""
    t = db.task(task["id"])
    if not t:
        return "it no longer exists"
    if t["status"] != "queued":
        return f"it is {t['status']} now (picked while queued)"
    limit = int(t.get("max_attempts") or 3)
    done = load_result(t.get("result")).get("status") == "done"
    if done and (n := done_closes(db, t["id"])) >= limit:
        return (f"it was already closed done {n} times (attempt limit {limit}) and was queued again "
                f"without an explicit requeue; attempts {int(t.get('attempts') or 0)} of {limit}")
    if done and (n := int(t.get("attempts") or 0)) > limit and ended_done_unrequeued(db, t["id"]):
        return (f"its last hand-off was done and it is past its attempt limit (attempts {n} of {limit}), "
                f"queued again without an explicit requeue")
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


# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent lane: a time-sensitive task never waits behind routine work.

A task is urgent when its priority is 1 (or better) or its spec has a line `urgent: <why>`.

- Worker start: db.ready_tasks puts urgent tasks before every other ready task (obligation owners
  included). With every worker slot of its provider taken, an urgent task starts in up to
  budget.urgent_extra_slots (default 1; 0: off) slots beyond the gate's max_parallel. The spend gate
  (allow_new_work, a gate of 0 slots, the dollar caps), the auth gate, pauses and resource locks
  still hold it; a running run is never interrupted.
- Review: an urgent code task's review is queued as soon as it hands off (done or needs_review)
  at priority 1; the review sweeps skip review.sweep_after_s and review.unreviewed_per_pass for it,
  and an urgent task in review for WAIT_S raises the review_stall event (Daemon._raise_review_stalls).
- Push queue: an approved head of an urgent review or of the urgent task it reviews starts its batch
  at once (pushq.due); the guard against a running batch or deploy stays.
- Waiting lists: exclusive-resource reservations (Daemon._slot_plan), `ttp lock` waiters
  (locks.my_turn) and device runner queues (devq.Runner.next_job) serve urgent waiters first.
  Held locks, holds, pauses and recovery owners are untouched.
- Visibility: `ttp status` and the web app mark urgent tasks with how long each has waited
  (waited_s); Daemon.check_slot_wait raises one event when a ready urgent task has been held by
  anything for coordinator.urgent_slot_wait_min (default 10), naming the hold. A wait on another
  task's lane has its own lane_wait event, not a second one here."""
from __future__ import annotations

import re

PRIORITY = 1                    # a task at this priority or better is urgent
# A spec line `urgent: <why>`; `urgent: no` (none, not, false, n/a) says it is not.
LINE = re.compile(r"^[ \t]*urgent:[ \t]*(?!(?:no|none|not|false|n/?a)\b)(\S.*)$", re.I | re.M)
EXTRA_SLOTS = 1                 # default of budget.urgent_extra_slots
WAIT_S = 600                    # an urgent task in review this long raises review_stall


def _priority(task: dict) -> int:
    p = task.get("priority")
    try:
        return 3 if p is None else int(p)
    except (TypeError, ValueError):
        return 3


def is_urgent(task: dict | None) -> bool:
    """Priority 1 or better, or an `urgent: <why>` line in the spec."""
    if not task:
        return False
    return _priority(task) <= PRIORITY or bool(LINE.search(str(task.get("spec") or "")))


def why(task: dict) -> str:
    """The spec's `urgent:` reason, else "priority 1"; "" for a task that is not urgent."""
    m = LINE.search(str(task.get("spec") or ""))
    return m.group(1).strip()[:160] if m else f"priority {_priority(task)}" if is_urgent(task) else ""


def first(rows: list[dict]) -> list[dict]:
    """`rows` with the urgent ones first, each part in its own order (a stable sort)."""
    return sorted(rows, key=lambda r: not is_urgent(r))


def extra_slots(cfg: dict) -> int:
    """budget.urgent_extra_slots: worker slots an urgent task may take beyond max_parallel_workers."""
    try:
        return max(int(((cfg or {}).get("budget") or {}).get("urgent_extra_slots", EXTRA_SLOTS)), 0)
    except (TypeError, ValueError):
        return EXTRA_SLOTS


def covers_urgent(db, task: dict | None) -> bool:
    """Whether `task` is urgent, or is a review of an urgent task (a task it depends on)."""
    if not task:
        return False
    if is_urgent(task):
        return True
    if task.get("kind") != "review":
        return False
    from .db import dependency_ids
    return any(is_urgent(db.task(d)) for d in dependency_ids(task) if isinstance(d, int))


def waited_s(db, task: dict, now: float) -> float:
    """How long `task` has waited in its present state: a queued task since it was made or its last
    run ended (whichever is later), one in review since it entered review; 0 for other states."""
    if task.get("status") == "review":
        since = db.review_since().get(task["id"])
    elif task.get("status") == "queued":
        ended = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (task["id"],))
        since = max(float(task.get("created") or 0), float((ended or {}).get("e") or 0))
    else:
        return 0.0
    return max(now - float(since), 0.0) if since else 0.0


def mark(db, task: dict, now: float) -> str:
    """The status mark of an urgent task, e.g. `urgent, waited 12m`; "" for one that is not."""
    if not is_urgent(task):
        return ""
    s = waited_s(db, task, now)
    if not s:
        return "urgent"
    m = int(s // 60)
    return f"urgent, waited {m // 60}h{m % 60:02d}m" if m >= 60 else f"urgent, waited {m}m"

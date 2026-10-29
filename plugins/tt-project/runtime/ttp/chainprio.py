"""The priority of a daemon-queued review hop (`Review #<id>`, `Fix review #<id>`, `Re-review #<id>`):
the most urgent (lowest) of the tasks it covers (its `auto_review:`, `continues:` and `review_fix:`
labels) and of its chain's root (sharedchecks.root), so an urgent change stays urgent from its code
task to the push. A hop the coordinator set a priority on (SET_LABEL) keeps it; any other open hop
follows when a task of its chain changes priority (follow)."""
from __future__ import annotations

import json
from typing import Any

from .db import DB, TERMINAL_TASK_STATES

SET_LABEL = "priority_set"   # the coordinator set this task's priority: it no longer follows its chain
HOP_PREFIXES = ("Review #", "Fix review #", "Re-review #")
_COVER = ("auto_review:", "continues:", "review_fix:")


def _labels(task: dict) -> list:
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return []
    return labels if isinstance(labels, list) else []


def covered(task: dict) -> list[int]:
    """The ids `task`'s labels say it covers (the task it reviews, continues or fixes the review of)."""
    out = []
    for lb in _labels(task):
        for pre in _COVER:
            if isinstance(lb, str) and lb.startswith(pre) and lb[len(pre):].isdigit():
                out.append(int(lb[len(pre):]))
    return out


def is_hop(task: dict) -> bool:
    """A daemon-queued review hop (by its origin and title)."""
    return task.get("origin") == "daemon" and str(task.get("title") or "").startswith(HOP_PREFIXES)


def _effective(db: DB, task: dict, seen: set[int]) -> int | None:
    """`task`'s own priority, or for a hop nobody set one on, the one its chain gives it (so a closed
    hop's stale priority never holds its successors)."""
    if is_hop(task) and SET_LABEL not in _labels(task) and task["id"] not in seen:
        got = _of(db, [c for c in (db.task(i) for i in covered(task)) if c], seen | {task["id"]})
        if got is not None:
            return got
    return None if task.get("priority") is None else int(task["priority"])


def _of(db: DB, tasks: list[dict], seen: set[int]) -> int | None:
    from .sharedchecks import root
    prios = []
    for t in tasks:
        r = root(db, t["id"])
        for row in (t, db.task(r) if r != t["id"] else None):
            if row and (got := _effective(db, row, seen)) is not None:
                prios.append(got)
    return min(prios) if prios else None


def of(db: DB, *tasks: dict | None, default: int = 3) -> int:
    """The priority of a hop that covers `tasks`: the most urgent of theirs and their chain roots';
    `default` when none has one."""
    got = _of(db, [t for t in tasks if t], set())
    return default if got is None else got


def follow(db: DB, changed: int) -> list[tuple[int, int, int]]:
    """After task `changed`'s priority changed: give each open daemon-queued hop of its chain that the
    coordinator never set a priority on the priority `of` its covered tasks and roots. Returns
    (id, old, new) for each hop changed."""
    from .sharedchecks import root
    base = root(db, changed)
    marks = ",".join("?" * len(TERMINAL_TASK_STATES))
    rows = db.q(f"SELECT * FROM tasks WHERE origin='daemon' AND status NOT IN ({marks}) AND id!=? ORDER BY id",
                (*TERMINAL_TASK_STATES, changed))
    out: list[tuple[int, int, int]] = []
    for t in rows:
        if not is_hop(t) or SET_LABEL in _labels(t) or root(db, t["id"]) != base:
            continue
        cov = [c for c in (db.task(i) for i in covered(t)) if c]
        if not cov:
            continue
        new = of(db, *cov, default=int(t["priority"]))
        if new != t["priority"]:
            db.update_task(t["id"], priority=new)
            out.append((t["id"], int(t["priority"]), new))
    return out


def set_labels(task: dict, upd: dict[str, Any]) -> list | None:
    """The labels a task_update that sets `task`'s priority leaves on a hop (with SET_LABEL), or None
    when it needs no change."""
    if not is_hop(task):
        return None
    labels = upd.get("labels")
    labels = list(_labels(task) if labels is None else labels)
    return None if SET_LABEL in labels else labels + [SET_LABEL]

"""A task the owner of an open obligation or an urgent task waits on takes that task's rank.

Dispatch ranks the required owners of an open obligation first (obligations.owner_due) and urgent
tasks next (urgent.first). A dependency of one of them has neither mark, so a routine task could start
ahead of the work the deadline needs. `inherited_due` widens the owners' map: every unfinished task
that an owner, or an urgent task, depends on, directly or through other tasks, gets that task's due
(the earliest one when several want it; math.inf for an urgent one with no due). No model, no writes."""
from __future__ import annotations

import json
import math
from typing import Any

from . import obligations, urgent

OPEN = ("queued", "running", "waiting", "blocked", "review", "pushing")
MAX_OPEN = 5000   # an open table past this is read as unreadable: the boost is lost, never the queue


def _deps(row: dict) -> list[int]:
    try:
        raw = json.loads(row.get("depends_on") or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for d in raw if isinstance(raw, list) else [raw]:
        try:
            out.append(int(d))
        except (TypeError, ValueError):
            pass
    return out


def extend(rows: list[dict], due: dict[int, float]) -> dict[int, float]:
    """`due` plus every unfinished task `rows` (the open tasks) show an owner or an urgent task needs,
    transitively, each with the earliest due of the tasks that need it. Cycles end at a visited task."""
    by_id = {r["id"]: r for r in rows}
    out = dict(due)
    work = [(tid, d) for tid, d in due.items() if tid in by_id]
    work += [(r["id"], out.get(r["id"], math.inf)) for r in rows if r["id"] not in due and urgent.is_urgent(r)]
    best: dict[int, float] = {}
    while work:
        tid, d = work.pop()
        if tid in best and best[tid] <= d:
            continue
        best[tid] = d
        for dep in _deps(by_id[tid]):
            if dep in by_id:
                out[dep] = min(d, out.get(dep, math.inf))
                work.append((dep, d))
    return out


def inherited_due(db: Any) -> dict[int, float]:
    """obligations.owner_due with the inheritance applied; owner_due alone when the open tasks cannot be
    read (dispatch then only loses the boost)."""
    due = obligations.owner_due(db)
    try:
        marks = ",".join("?" * len(OPEN))
        rows = db.q(f"SELECT * FROM tasks WHERE status IN ({marks}) LIMIT ?", (*OPEN, MAX_OPEN + 1))
        if len(rows) > MAX_OPEN:
            return due
        return extend(rows, due)
    except Exception:
        return due

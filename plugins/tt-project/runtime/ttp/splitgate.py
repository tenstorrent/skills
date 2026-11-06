# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""chains.split_lines at a code hand-off, model-free: a code task, no split part (part_of), whose change
passes that many non-test lines (reviewstats.code_lines) goes back once before any review (send_back):
the same task, branch and worktree, an `## Update` ordering stacked parts, a LABEL. Its next hand-off
passes whatever its size (one return per task: it must cost less than the failed reviews it avoids), as
does one recording FIELD (why it cannot be split) or an urgent task; their review spec notes the size."""
from __future__ import annotations

import json
import re
import time

from . import chaincap, urgent
from .db import load_result
from .prompts import split_lines
from .reviewstats import code_lines

LABEL = "split_order:"   # split_order:<unix time>: the task went back once for a split
FIELD = "no_split"       # result.json: why the change cannot be split; the review is queued as it is
EVENT = "split_returned"
# A split part's title, as kind-code.md asks for it: "code: part 2 of <topic>".
PART_TITLE = re.compile(r"(?i)\bpart\s+\d+\s+of\b")
ORDER = ("## Update\nSplit order, from the daemon after your last hand-off: your change is {lines} non-test lines, "
         "over chains.split_lines ({limit}), and changes that size fail review far more often. No review was "
         "queued. Before you reshape it, keep the whole change: `git branch {branch}-full {head}`. Then make this "
         "branch carry a first part that stands alone and passes the checks (at most about {limit} non-test "
         "lines), and hand it off `done` with one follow-up `code: part 2 of <topic>` whose `start_when` is "
         "`landed:#{tid}` and whose spec names branch {branch}-full at {short} as the source of what is left "
         "(further parts the same way, each stacked on the last). If it truly cannot be split, leave the branch "
         "as it is and hand off with `\"" + FIELD + "\": \"<why>\"` in result.json: its review is then queued. "
         "This order comes once: your next hand-off is reviewed whatever its size.")


def returned(task: dict | None) -> bool:
    """Whether task `task` already went back for a split (its LABEL)."""
    try:
        labels = json.loads((task or {}).get("labels") or "[]")
    except ValueError:
        return False
    return isinstance(labels, list) and any(isinstance(lb, str) and lb.startswith(LABEL) for lb in labels)


def part_of(task: dict) -> bool:
    """Whether task `task` is itself a part of a split change: a `part N of` title, a hop that continues
    another task (also a review fix), or a head marked split."""
    return bool(PART_TITLE.search(str(task.get("title") or ""))) or chaincap.prev_of(task) is not None \
        or bool(chaincap.split_head(task))


def reason(*results) -> str:
    """The first FIELD reason the hand-offs `results` record, '' when none does."""
    for r in results:
        if isinstance(r, dict) and isinstance(r.get(FIELD), str) and r[FIELD].strip():
            return r[FIELD].strip()[:300]
    return ""


def check(cfg: dict, task: dict, changes: dict | None, *results) -> tuple[str, int, int] | None:
    """For code task `task`, no split part, changing more than chains.split_lines non-test lines in
    `changes` (path -> lines): ('return', lines, limit) when it goes back, else (why it passes, lines,
    limit). None otherwise, also with the gate off or the diff unknown."""
    limit, n = split_lines(cfg), code_lines(changes)
    if not limit or n is None or n <= limit or task.get("kind") != "code" or part_of(task):
        return None
    why = ("urgent: " + (urgent.why(task) or "priority 1")) if urgent.is_urgent(task) else \
        f"its hand-off says it cannot be split: {r}" if (r := reason(*results)) else \
        "it went back once for a split already" if returned(task) else "return"
    return why, n, limit


def send_back(db, cfg: dict, task: dict, lines: int, limit: int, head: str, attempts: int,
              now: float | None = None) -> bool:
    """Re-queue finished code task `task` (done or review) with the split order, once: the same id, branch
    and worktree, its budget topped up, its attempts back to `attempts`, its last hand-off kept. An EVENT
    records it. False leaves the task as it was."""
    now = time.time() if now is None else now
    if returned(task) or not task.get("branch"):
        return False
    labels, base = chaincap.budget_base(cfg, task)
    order = ORDER.format(lines=lines, limit=limit, branch=task["branch"], head=head or task["branch"],
                         short=(head or task["branch"])[:12], tid=task["id"])
    rows = db.conn.execute("UPDATE tasks SET status='queued', spec=?, labels=?, budget_usd=?, attempts=?, "
                           "not_before=NULL, blocked_reason=NULL, updated=? WHERE id=? AND status IN ('done','review')",
                           (f"{str(task.get('spec') or '').rstrip()}\n\n{order}",
                            json.dumps([*labels, f"{LABEL}{int(now)}"]),
                            round(float(task.get("spent_usd") or 0) + base, 2), int(attempts), now,
                            task["id"])).rowcount
    if not rows:
        return False
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, f"task:{task['id']}", EVENT, "low",
          f"#{task['id']} went back to its worker for a split: {lines} non-test lines over chains.split_lines ({limit})",
          json.dumps({"lines": lines, "limit": limit, "head": head}), "handled", task["id"]))
    return True


def review_line(cfg: dict, task: dict, changes: dict | None) -> str:
    """The review spec's line for a change over the limit that was not split, '' otherwise."""
    hit = check(cfg, task, changes, load_result(task.get("result")))
    if hit is None:
        return ""
    why, n, limit = hit
    why = "no reason recorded" if why == "return" else why
    return (f"Size: {n} non-test lines, over chains.split_lines ({limit}), not split ({why}). Large changes "
            f"fail review more often: read it in full.")

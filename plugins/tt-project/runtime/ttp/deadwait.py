# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Waits with no way to end, raised to the coordinator model-free.

- A failed review whose fixes the daemon could not queue (a third failed round, findings on
  branches that are not one stack, a task cap): one `review_nofix` event per review, naming the
  review, its branch and what waits on it (Daemon._fix_failed_review says why). One with no fix
  follow-ups says the same in its own task_failed event, which already wakes the coordinator.
- A start_when that is exactly `landed:#X` whose producer X is done, its work not landed, and nothing open can land
  it: no open review, fix, rebase or continue hop carrying X's change (landed.carriers), no open task
  on X's branch, and no push queue row. One `landed_wait_dead` event per X names the held tasks;
  once a path appears, X lands or no queued task waits on it, it is cleared (handled), and a later
  dead end raises it again. Saved database facts only: a daemon restart (empty probe verdicts) or a
  broken probe exit neither clears nor re-raises it. A `landed:#X` inside a longer probe is not judged.
"""
from __future__ import annotations

import json
import time

from . import landed
from .db import TERMINAL_TASK_STATES, deferral, dependency_ids

NOFIX_EVENT = "review_nofix"
DEAD_EVENT = "landed_wait_dead"
KEY = "dead_landed_waits"   # kv: producer id -> {"since": first seen dead, "event": event id or None}
GRACE_S = 900               # default of waiting.landed_wait_dead_s: a review or push row the same step queues


def waiters(db, ids: set[int], skip: set[int] = frozenset()) -> list[dict]:
    """Open tasks that depend on, or whose start_when waits on, any task in `ids`."""
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status NOT IN (?,?,?) ORDER BY id", TERMINAL_TASK_STATES):
        if t["id"] in ids or t["id"] in skip:
            continue
        refs = {int(r) for r in landed.task_refs(deferral(t).get("when")) if r.isdigit()}
        if ids & (set(dependency_ids(t)) | refs):
            out.append(t)
    return out


def names(tasks: list[dict], cap: int = 8) -> str:
    """'#3 (queued), #4 (blocked)', cut after `cap`; 'none' for no tasks."""
    if not tasks:
        return "none"
    s = ", ".join(f"#{t['id']} ({t['status']})" for t in tasks[:cap])
    return s + (f" and {len(tasks) - cap} more" if len(tasks) > cap else "")


def raise_nofix(db, review: dict, why: str, branch: str) -> int | None:
    """One `review_nofix` event for failed review `review` (none when one exists): the coordinator
    queues the fix and re-review itself, re-plans, or cancels what waits."""
    fp = f"{NOFIX_EVENT}:{review['id']}"
    if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return None
    code = {i for i in dependency_ids(review) if isinstance(i, int)}
    held = waiters(db, code | {review["id"]})
    text = (f"Review #{review['id']} {review['title']} failed and the daemon queued no fix or re-review: {why}. "
            f"Branch: {branch or 'none found'}. Waiting on it or its change: {names(held)}. Decide: queue the fix "
            f"(continues the reviewed task) and its re-review, re-plan the change, or re-point or cancel what waits.")
    return db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), f"task:{review['id']}", NOFIX_EVENT, fp, "normal", text,
                 json.dumps({"why": why, "branch": branch, "held": [t["id"] for t in held]}), "queued",
                 review["id"]))


class Rows(landed.Rows):
    """What path judges every held producer of one check from, read once: landed.Rows, the open
    tasks, the push queue rows waiting to push and the abandoned tasks."""

    def __init__(self, db, tids):
        super().__init__(db, tids)
        self.open = db.q("SELECT * FROM tasks WHERE status NOT IN (?,?,?) ORDER BY id", TERMINAL_TASK_STATES)
        self.queued = db.q("SELECT task, status FROM push_queue WHERE status IN ('approved','batched','held') "
                           "ORDER BY id")
        self.abandoned = {r["task"] for r in db.q("SELECT task FROM task_abandoned")}


def path(db, x: int, held: set[int], rows: Rows | None = None) -> str:
    """What can still land producer `x`'s work, '' when nothing: x itself not done, a landing
    recorded (landed.recorded), an open task carrying its change or on its branch (a held task, or one that depends on
    a held task, is no path: it waits on x too), or a push queue row. `rows`: read once for every
    producer of a check (Rows), else read here."""
    rows = rows if rows is not None else Rows(db, [x])
    t = rows.tasks.get(x)
    if not t or t["status"] != "done":
        return f"#{x} is {t['status'] if t else 'missing'}"
    carried = landed.carriers(db, x, rows)
    if landed.recorded(db, x, carried, rows)[0]:   # the same saved landings `ttp landed --task` counts
        return "a landing is recorded"
    branches = {b for b, _, _ in carried.values() if b}
    for o in rows.open:
        if o["id"] in held or held & set(dependency_ids(o)):
            continue
        if o["id"] in carried or (o["branch"] and o["branch"] in branches):
            return f"#{o['id']} ({o['status']})"
    row = next((r for r in rows.queued if r["task"] in carried), None)
    return f"push queue row of #{row['task']} ({row['status']})" if row else ""


def check(db, cfg: dict, held_by: dict[int, list[dict]], now: float) -> None:
    """`held_by`: producer id -> the queued tasks whose start_when is `landed:#<it>`. Raises
    one DEAD_EVENT per producer dead for `waiting.landed_wait_dead_s`, and clears it (handled, state
    dropped) once a path appears, the producer lands or nothing waits on it any more."""
    grace = float((cfg.get("waiting") or {}).get("landed_wait_dead_s", GRACE_S) or 0)
    state = db.kv(KEY, {}) or {}
    new: dict = {}
    rows = Rows(db, held_by) if held_by else None
    for x, tasks in sorted(held_by.items()):
        held = {t["id"] for t in tasks}
        if path(db, x, held, rows) or x in rows.abandoned:   # abandoned: its probe is raised as broken
            continue
        prev = state.get(str(x)) or {}
        got = {"since": prev.get("since") or now, "event": prev.get("event")}
        if got["event"] is None and now - got["since"] >= grace:
            got["event"] = _raise_dead(db, x, tasks, rows)
        new[str(x)] = got
    for k, v in state.items():
        if k not in new and v.get("event"):
            db.x("UPDATE events SET status='handled' WHERE id=? AND status='queued'", (v["event"],))
    if new != state:
        db.set_kv(KEY, new)


def _raise_dead(db, x: int, tasks: list[dict], rows: Rows) -> int:
    t = rows.tasks.get(x) or {"title": "?", "branch": ""}
    failed = db.one("SELECT id FROM tasks WHERE kind='review' AND status='failed' AND id IN "
                    f"({','.join('?' * len(c))}) ORDER BY id DESC", tuple(c)) \
        if (c := sorted(landed.carriers(db, x, rows))) else None
    text = (f"#{x} {t['title']} is done but its work has not landed, and nothing open can land it: no open "
            f"review, fix, rebase or push of branch {t.get('branch') or '?'}"
            + (f" (its last review #{failed['id']} failed)" if failed else "")
            + f". Held by start_when landed:#{x}: {names(tasks)}. Decide: queue the fix and re-review (or "
              f"the push), re-point or clear their start_when, or cancel them.")
    return db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), "daemon", DEAD_EVENT, f"{DEAD_EVENT}:{x}:{time.time():.0f}", "normal", text,
                 json.dumps({"producer": x, "held": [h["id"] for h in tasks]}), "queued", x))

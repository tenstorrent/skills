# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Carry tasks: one owner task carries a user's order from first commit to live, its reviews inline.

A code task carries when `enabled` says so: config `review.inline` is `ordered` (the default: the task
owns an open order, was ordered by a user message, or has an `urgent:` line), `all` (every code task) or
`off`, and the rollout flag `inline_review` is on for the project. Its phase lives in the `carry_state`
table (db.py) and every change of it is one transaction with one `carry_phase` event, the source of the
chain-time measurements. `reconcile` brings phases forward to the push queue's rows after a restart.
"""
from __future__ import annotations

import json
import time

CHANGE, REVIEW, FIX, QUEUED, LANDED, DEPLOY, DONE = "change", "review", "fix", "queued", "landed", "deploy", "done"
REBASE, CHECKFIX, CAPPED = "rebase", "checkfix", "capped"   # a push conflict, failed push checks, over the rounds cap
PHASES = (CHANGE, REVIEW, FIX, QUEUED, LANDED, DEPLOY, DONE, REBASE, CHECKFIX, CAPPED)
EVENT = "carry_phase"
MODES = ("ordered", "all", "off")
FIELDS = ("round", "head", "review_run", "push_row", "batch", "note")
# How far along the chain a phase is: reconcile only moves a task to a later one.
_RANK = {CHANGE: 0, REVIEW: 1, FIX: 1, REBASE: 1, CHECKFIX: 1, QUEUED: 2, LANDED: 3, DEPLOY: 4, DONE: 5}


def mode(cfg: dict | None) -> str:
    got = ((cfg or {}).get("review") or {}).get("inline", "ordered")
    return got if got in MODES else "ordered"


def ordered(task: dict, db) -> bool:
    """A task of an open user order, one a user message ordered on resources (userorder), or with an `urgent:` line."""
    from . import obligations, urgent, userorder
    tid = task.get("id")
    return (tid in obligations.order_tasks(db) or tid in {o.get("task") for o in userorder.active(db).values()}
            or bool(urgent.LINE.search(str(task.get("spec") or ""))))


def selected(cfg: dict | None, task: dict | None, db) -> bool:
    """The config's choice alone: a code task, under `review.inline`'s mode."""
    m = mode(cfg)
    return bool(task) and task.get("kind") == "code" and m != "off" and (m == "all" or ordered(task, db))


def enabled(cfg: dict | None, task: dict | None, db, p=None) -> bool:
    """Whether `task` carries: selected, and `inline_review` on for project `p` (off without one)."""
    from . import costlevers
    return selected(cfg, task, db) and p is not None and costlevers.lever_on(p, "inline_review")


def state(db, task_id: int) -> dict | None:
    return db.one("SELECT * FROM carry_state WHERE task=?", (int(task_id),))


def advance(db, task: dict | int, phase: str, **fields) -> dict:
    """Put `task` in `phase`, with any of FIELDS, in one transaction that also writes the one `carry_phase`
    event (task, from, to, ts), a record that wakes nobody. A task with no state starts from None; the same phase with no field is no change."""
    if phase not in PHASES:
        raise ValueError(f"unknown carry phase {phase!r}")
    if bad := set(fields) - set(FIELDS):
        raise ValueError(f"unknown carry field(s): {', '.join(sorted(bad))}")
    tid = int(task["id"] if isinstance(task, dict) else task)
    with db.tx():
        old = state(db, tid)
        before = old["phase"] if old else None
        if old and before == phase and not fields:
            return old
        now = time.time()
        row = {"round": 0, **{k: (old or {}).get(k) for k in FIELDS if old and old.get(k) is not None}, **fields}
        db.x("INSERT INTO carry_state(task, phase, round, head, review_run, push_row, batch, updated, note) "
             "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET phase=excluded.phase, round=excluded.round, "
             "head=excluded.head, review_run=excluded.review_run, push_row=excluded.push_row, batch=excluded.batch, "
             "updated=excluded.updated, note=excluded.note",
             (tid, phase, row["round"], row.get("head"), row.get("review_run"), row.get("push_row"),
              row.get("batch"), now, row.get("note")))
        if before != phase:
            data = {"task": tid, "from": before, "to": phase, "ts": now}
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", EVENT, "low", f"carry task #{tid}: {before or 'start'} -> {phase}",
                  json.dumps(data), "handled", tid))
    return state(db, tid)


def reconcile(db) -> list[tuple[int, str, str]]:
    """On daemon start: move each carry task's phase forward to its newest push row (approved or batched:
    queued; pushed or landed: landed) and, once that row's batch ended its deploy step well (ok or skipped), to done. Returns
    (task, from, to) for each move. A task in `capped` is the coordinator's and stays."""
    from .pushq import LANDED_OUTCOMES, OPEN_ROWS
    moved = []
    for s in db.q("SELECT task, phase FROM carry_state WHERE phase NOT IN (?,?)", (DONE, CAPPED)):
        row = db.one("SELECT q.id, q.status, q.batch, q.head, b.after_push FROM push_queue q "
                     "LEFT JOIN push_batches b ON b.id=q.batch WHERE q.task=? ORDER BY q.id DESC LIMIT 1", (s["task"],))
        if not row:
            continue
        landed = row["status"] in LANDED_OUTCOMES
        want = (DONE if landed and row["after_push"] in ("ok", "skipped") else LANDED if landed
                else QUEUED if row["status"] in OPEN_ROWS and row["status"] != "held" else None)
        if want and _RANK[want] > _RANK[s["phase"]]:
            advance(db, s["task"], want, push_row=row["id"], batch=row["batch"], head=row["head"])
            moved.append((s["task"], s["phase"], want))
    return moved


def show_line(db, task_id: int) -> str:
    """`carry: <phase> round <n>` for `ttp task show`, "" for a task with no state."""
    s = state(db, task_id)
    return f"carry: {s['phase']} round {s['round']}" if s else ""

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

from .db import dump_result, load_result

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
    """A task urgent or user-ordered by the shared rules (urgent), of an open user order, or one a user message ordered on resources (userorder)."""
    from . import obligations, urgent, userorder
    owners = obligations.order_tasks(db)
    return (urgent.is_urgent(task) or urgent.user_ordered(task, owners)
            or task.get("id") in {o.get("task") for o in userorder.active(db).values()})


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


def settle_kind(states: set[str], trim) -> str | None:
    """How one approval's row `states` settle a carry task: checkfix, rebase or landed; None leaves it to
    the review flow's own handling (a trim, a refusal, rows still waiting)."""
    from .pushq import LANDED_OUTCOMES
    if trim or "refused" in states:
        return None
    return (CHECKFIX if "check_failed" in states else REBASE if "conflict" in states
            else LANDED if states <= set(LANDED_OUTCOMES) else None)


def _approvals(db, tid: int) -> tuple[list[dict], list[dict]]:
    """A task's push rows and, of them, its newest approval's (the rows sharing one `created`)."""
    rows = db.q("SELECT * FROM push_queue WHERE task=? ORDER BY id", (tid,))
    return rows, [r for r in rows if r["created"] == rows[-1]["created"]]


def _land_done(p, task: dict, b: dict, m: dict, now: float) -> None:
    """The change is live: done as a landed review is (its hand-off, events and reply), phase `done`."""
    from . import pushq
    rows, current = _approvals(p.db, task["id"])
    if task["status"] != "pushing" or not any(r["status"] in pushq.LANDED_OUTCOMES for r in current):
        return
    prev = load_result(task["result"])
    with p.db.tx():
        pushq._settle_landed(p, task, b, m, prev, str(prev.get("summary") or ""), b["target"], rows, current, now)
        advance(p.db, task, DONE, batch=b["id"])


def _progress(p, tid: int, evidence: str) -> None:
    """`evidence` as progress on each open obligation task `tid` owns (no receipt: its owner or the coordinator closes it)."""
    from . import obligations
    for key in obligations.owned_open(p.db, tid):
        obligations.record(p, key, "progress", evidence, tid)


def on_settle(p, task: dict, kind: str, b: dict, m: dict, now: float, deploy: bool) -> None:
    """The push queue settled carry task `task` (`settle_kind`'s `kind`) in batch `b`. landed: phase `landed`,
    an owned obligation gets the sha as progress, then `deploy` (the batch's after_push follows) or done.
    rebase and checkfix: the owner is woken to fix it in place (the log's path and tail with it); past its
    attempts it goes `capped` and failed, to the coordinator."""
    from . import pushq
    db, tid, target = p.db, task["id"], b["target"]
    rows, current = _approvals(db, tid)
    hit = pushq.LANDED_OUTCOMES if kind == LANDED else ("conflict",) if kind == REBASE else ("check_failed",)
    row = next(r for r in reversed(current) if r["status"] in hit)
    at = {"push_row": row["id"], "batch": b["id"], "head": row["head"]}
    if kind == LANDED:
        _progress(p, tid, f"landed {pushq._short(row['pushed_sha'])} on {target} (batch {b['id']})")
        advance(db, tid, LANDED, **at)
        if deploy:
            advance(db, tid, DEPLOY)
        else:
            _land_done(p, task, b, m, now)
        return
    sent = pushq._approvals_sent(db, tid, "conflict" if kind == REBASE else "check_failed")
    if kind == REBASE:
        woke = pushq._conflict_woke(m, target, row, current) + "; rebase it in place on its own branch"
    else:
        d = pushq._detail(row)
        woke = (f"the push checks failed on {row['branch'] or '?'} at {pushq._short(row['head'])} in batch {b['id']}: "
                f"`{d.get('cmd') or 'the push checks'}`; log: {m.get('log') or '?'}\n{pushq._tail_of(m, d)}\n"
                "fix it in place; its delta is reviewed again")
    if sent > int(task["max_attempts"] or pushq.DEFAULT_MAX_ATTEMPTS):
        text = f"{woke}. It keeps failing its push after {sent - 1} fixes"
        db.update_task(tid, status="failed", blocked_reason=None,
                       result=dump_result({**load_result(task["result"]), "status": "failed", "summary": text[:1500]}))
        advance(db, tid, CAPPED, **at, note=kind)
        pushq._event(db, task, "task_failed", f"#{tid} {task['title']} -> failed: {text}", queued=True)
        return pushq._reply(db, task, "failed", text)
    advance(db, tid, kind, **at)
    pushq._requeue(db, task, woke)
    pushq._event(db, task, "task_requeued", f"#{tid} {task['title']}: {woke.splitlines()[0]}", queued=False)


def on_after(p, b: dict, m: dict, status: str, line: str, tail: str, now: float) -> None:
    """The batch's after_push ended with `status`, `line` in words and `tail` the end of its log. Each carry
    task that landed in the batch: ok, skipped or superseded finishes it, `line` the obligation's progress;
    a failure raises `carry_deploy_failed` to the coordinator and wakes the owner only if the log names a
    file its landed commit touched."""
    from . import pushq
    db = p.db
    for s in db.q("SELECT task FROM carry_state WHERE batch=? AND phase IN (?,?)", (b["id"], LANDED, DEPLOY)):
        task = db.task(s["task"])
        if not task or task["status"] != "pushing":
            continue
        if status in ("ok", "skipped", "superseded"):
            _progress(p, task["id"], line)
            _land_done(p, task, b, m, now)
            continue
        head = next((r["head"] for r in _approvals(db, task["id"])[1] if r["batch"] == b["id"]), None)
        named = [f for f in sorted(pushq._changed(p, m["tip"], head) or ()) if f in tail] if head and m.get("tip") else []
        pushq._event(db, task, "carry_deploy_failed", f"#{task['id']} {task['title']}: {line}"
                     + (f"; the log names {', '.join(named[:10])} from its change" if named else ""), queued=True)
        if named:
            pushq._requeue(db, task, f"{line}; the log names {', '.join(named[:10])} from your change:\n{tail}")


def show_line(db, task_id: int) -> str:
    """`carry: <phase> round <n>` for `ttp task show`, "" for a task with no state."""
    s = state(db, task_id)
    return f"carry: {s['phase']} round {s['round']}" if s else ""

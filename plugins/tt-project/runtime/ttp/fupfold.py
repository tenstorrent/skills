# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Fold a done task's follow-ups into open tasks: fewer, bigger tasks.

A worker follow-up that names the same branch (its `pr_branch:` line), the same files (slash paths
in its spec, as settled.files_in reads them, overlap.same) or the same task lineage (chaincap.chain
root) as a queued task of its kind is folded into that task instead of being proposed as a new one:
appended to its spec. The first match wins in that order (branch, files, lineage), the newest task
among equals.

Targets: only queued tasks that can start now (no start_after or start_when, no unmet dependency): a
running task may already have written its hand-off and never read the fold, and a deferred one would
make the follow-up wait. Urgency must match (urgentfup.why): an urgent or obligation-owning source's
follow-up folds only into a target that is urgent or owns one of the same obligations, else urgentfup
queues it at once; a routine follow-up never folds into urgent or owed work.

Never folded: upstream notes, deferred follow-ups (start_after, start_when), post-landing steps,
`continue:` and `mechanical:` follow-ups (the chain hops and their caps own them), follow-ups of a
review or an approval, and any whose kind no open task shares. A follow-up is never dropped: one
that does not fold stays proposed, and each fold is logged, named in the task's hand-off event
('folded into #N') and its followup_proposed event, which is marked handled.
`chains.fold_followups` false turns this off (default on). Model-free, no git call."""
from __future__ import annotations

import json
from . import chaincap, overlap, settled, upstream, urgent, urgentfup, worktree
from .db import deferral

SETTING = "fold_followups"   # chains.fold_followups
MAX_OPEN = 200               # open tasks read per hand-off
SPEC_CHARS = 4000            # of a folded follow-up's spec carried into its target


def enabled(cfg: dict) -> bool:
    return (cfg.get("chains") or {}).get(SETTING, True) not in (False, 0, "off", "false")


def eligible(fup: dict, task_id: int) -> bool:
    return not upstream.is_note(fup) and not fup.get("start_after") and not fup.get("start_when") \
        and not fup.get("continues") and not chaincap.CONT_TITLE.match(str(fup.get("title") or "")) \
        and not chaincap.post_landing(fup, task_id)


def _branches(t: dict) -> set[str]:
    out = {str(t["branch"])} if t.get("branch") else set()
    try:
        labels = json.loads(t.get("labels") or "[]")
    except (TypeError, ValueError):
        labels = []
    out |= {lb.split(":", 1)[1] for lb in labels if isinstance(lb, str) and lb.startswith("pr_branch:")}
    out |= {m.group("branch") for m in worktree.SPEC_PR_BRANCH.finditer(str(t.get("spec") or ""))}
    return {b for b in out if b}


def _files(text: str) -> list[str]:
    return [f for f in settled.files_in(text) if "/" in f]


def _root(db, tid: int) -> int:
    try:
        return int(chaincap.chain(db, tid)[0]["id"])
    except (IndexError, KeyError, TypeError, ValueError):
        return tid


def _owned(db, tid: int) -> set[str]:
    from . import obligations
    try:
        return set(obligations.owned_open(db, int(tid)))
    except Exception:
        return set()


def _targets(db, task: dict, rows: list[dict]) -> list[dict]:
    """The rows `task`'s follow-ups may fold into: queued, not deferred, dependencies met, same urgency."""
    rows = [t for t in rows if t["status"] == "queued" and not deferral(t)]
    try:
        unmet = db.unmet_dependencies(rows) if rows else {}
    except Exception:
        return []
    rows = [t for t in rows if not unmet.get(t["id"])]
    if not urgentfup.why(db, task):
        return [t for t in rows if not urgentfup.why(db, t)]
    mine = _owned(db, task["id"])
    return [t for t in rows if urgent.is_urgent(t) or (mine & _owned(db, t["id"]))]


def match(db, task: dict, fup: dict, open_rows: list[dict]) -> tuple[dict, str] | None:
    """(the open task `fup` folds into, why), or None."""
    kind = str(fup.get("kind") or task["kind"]).strip().lower()
    rows = _targets(db, task, [t for t in open_rows if t["kind"] == kind and t["id"] != task["id"]])
    if not rows:
        return None
    rows.sort(key=lambda t: -int(t["id"]))
    spec = str(fup.get("spec") or "")
    want = {m.group("branch") for m in worktree.SPEC_PR_BRANCH.finditer(spec)}
    if fup.get("pr_branch"):
        want.add(str(fup["pr_branch"]))
    for t in rows:
        if both := sorted(want & _branches(t)):
            return t, f"same branch {both[0]}"
    mine = _files(f"{fup.get('title') or ''}\n{spec}")
    for t in rows if mine else []:
        theirs = _files(str(t.get("spec") or ""))
        if hit := next((a for a in mine if any(overlap.same(a, b) for b in theirs)), None):
            return t, f"same file {hit}"
    root = _root(db, int(task["id"]))
    for t in rows:
        if _root(db, int(t["id"])) == root:
            return t, f"same lineage (#{root})"
    return None


def _text(task: dict, fup: dict, why: str) -> str:
    return (f"Folded follow-up from #{task['id']} ({why}): {str(fup.get('title') or '')[:200]}\n"
            f"{str(fup.get('spec') or '')[:SPEC_CHARS]}")


def fold(p, cfg: dict, task: dict, fups: list[dict], skip: set[int], log=None) -> dict:
    """Fold done `task`'s eligible follow-ups (not in `skip`) into open tasks: {id(follow-up): (target id,
    why)}. A write that fails leaves that follow-up proposed."""
    if not enabled(cfg) or task.get("kind") == "review":
        return {}
    want = [f for f in fups if id(f) not in skip and eligible(f, task["id"])]
    if not want:
        return {}
    db = p.db
    open_rows = [dict(r) for r in db.q("SELECT * FROM tasks WHERE status='queued' AND kind!='review' "
                                       f"ORDER BY id DESC LIMIT {MAX_OPEN}")]
    out: dict = {}
    for f in want:
        hit = match(db, task, f, open_rows)
        if not hit:
            continue
        t, why = hit
        text = _text(task, f, why)
        try:
            cur = db.task(t["id"]) or t
            if cur["status"] != "queued":   # started since: its run may never read the fold
                continue
            db.update_task(t["id"], spec=f"{str(cur.get('spec') or '').rstrip()}\n\n{text}")
        except Exception as e:   # never dropped: it stays proposed
            if log:
                log(f"task {task['id']}: follow-up {str(f.get('title'))[:80]!r} not folded into #{t['id']}: {e}")
            continue
        out[id(f)] = (int(t["id"]), why)
        if log:
            log(f"task {task['id']}: follow-up {str(f.get('title'))[:80]!r} folded into #{t['id']} "
                f"({cur['status']}; {why})")
    return out

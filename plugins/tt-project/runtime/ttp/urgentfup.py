# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent follow-ups: work on an urgent goal never stalls between hops.

A done task that is urgent (urgent.is_urgent: priority 1, a spec `urgent:` line) or a required owner
of an open obligation (obligations.owned_open) gets its `continue:` and next-step (`next:`, `next
step:`, `mechanical:`) follow-ups queued by the daemon in the same step as its hand-off, not proposed
to the next coordinator turn. Each is a task_add that `continues` the task (chaincap.auto_hop), so it
inherits what a continue inherits: the priority, budget, tier, pin and obligation attachment
(obligations.attach_carriers on its `follows:` or `continues:` label), a split head's branch; here it
also gets the parent's `urgent:` line (when its spec has none), its `resource:` and `exclusive:` labels
and its reply_chat.

Every check of a task_add still holds: the duplicate check, the chain caps (chaincap.check_add:
max_hops, max_stalls, max_usd, the review area) and the restrictions. Since each queued one is urgent
too and may queue several, chains.max_hops and max_usd also bound the whole tree under the chain root
(tree_cap: every task descending from it, an unfinished one counted at its budget), not one path.
A split code task (chaincap.split_head) queues only the continue that carries its change
(chaincap.code_hop), first: any other would take its branch over. A follow-up whose own `kind` is
not the task's is left to the coordinator. Before that a red or stopped
budget gate, a paused project or a paused resource of the task keeps them all proposed. A follow-up
any of these refuse stays proposed with the reason and wakes the coordinator. Upstream notes, deferred
follow-ups (start_after, start_when) and post-landing steps (chaincap.post_landing) keep their own
handling. Each queued one goes to the next digest's notices (chaincap.add_notice), where the
coordinator can cancel it. `chains.urgent_followups` false turns this off (default on)."""
from __future__ import annotations

import json
import re

from . import chaincap, urgent, upstream
from .db import TERMINAL_TASK_STATES as TERMINAL

NEXT_TITLE = re.compile(r"(?i)^\s*(?:continue|mechanical|next(?:\s+step)?)\s*:")
SETTING = "urgent_followups"   # chains.urgent_followups
MAX_QUEUED = 6                 # follow-ups queued per hand-off; the rest stay proposed
MAX_TREE = 400                 # tasks tree() reads under one chain root


def enabled(cfg: dict) -> bool:
    return (cfg.get("chains") or {}).get(SETTING, True) not in (False, 0, "off", "false")


def why(db, task: dict) -> str:
    """Why `task`'s follow-ups are queued at once: its urgent reason or the open obligations it owns; ''."""
    if urgent.is_urgent(task):
        return f"urgent: {urgent.why(task)}"
    from . import obligations
    try:
        owned = obligations.owned_open(db, int(task["id"]))
    except Exception:
        owned = []
    return f"owner of obligation {', '.join(owned)}" if owned else ""


def eligible(fup: dict, task_id: int) -> bool:
    """A next step this module queues: titled as one, not an upstream note, not deferred, not post-landing."""
    return not upstream.is_note(fup) and bool(NEXT_TITLE.match(str(fup.get("title") or ""))) \
        and not fup.get("start_after") and not fup.get("start_when") and not chaincap.post_landing(fup, task_id)


def held(db, task: dict, gate=None) -> str:
    """What keeps every follow-up proposed now: a red or stopped budget gate, a pause; ''."""
    from .coordinator import task_resources
    if gate is not None and (getattr(gate, "level", "") == "red" or not getattr(gate, "allow_new_work", True)):
        return f"the budget gate is {getattr(gate, 'level', 'closed')} (no new work)"
    if db.kv("paused", False):
        return "the project is paused"
    if paused := sorted(task_resources(task) & db.paused_resources().keys()):
        return f"its resource {', '.join(paused)} is paused"
    return ""


def _spec(task: dict, fup: dict) -> str:
    """`fup`'s spec with `task`'s `urgent:` line first when it has none of its own."""
    spec = str(fup.get("spec") or "")
    m = urgent.LINE.search(str(task.get("spec") or ""))
    if m and not urgent.LINE.search(spec):
        spec = f"{m.group(0).strip()}\n{spec}"
    return spec


def _inherit(db, task: dict, new_id: int) -> None:
    """Give the queued follow-up `task`'s resources and reply_chat (task_add takes the rest)."""
    try:
        mine = json.loads(task.get("labels") or "[]")
        row = db.task(new_id) or {}
        have = json.loads(row.get("labels") or "[]")
    except (TypeError, ValueError):
        return
    keep = [lb for lb in mine if isinstance(lb, str) and lb.split(":", 1)[0] in ("resource", "exclusive")
            and lb not in have]
    upd: dict = {"labels": [*have, *keep]} if keep else {}
    if task.get("reply_chat") and not row.get("reply_chat"):
        upd["reply_chat"] = task["reply_chat"]
    if upd:
        db.update_task(new_id, **upd)


def tree(db, root: int) -> list[dict]:
    """`root` and every task descending from it by continue or follow links (chaincap.prev_of), reviews
    aside, at most MAX_TREE."""
    top = db.task(root)
    out, todo = ([dict(top)], [root]) if top else ([], [])
    seen = {root}
    while todo and len(out) < MAX_TREE:
        i = todo.pop()
        for r in db.q("SELECT * FROM tasks WHERE parent=? OR labels LIKE ? OR labels LIKE ?",
                      (i, f'%"{chaincap.FOLLOWS}{i}"%', f'%"continues:{i}"%')):
            t = dict(r)
            if t["id"] not in seen and t["kind"] != "review" and chaincap.prev_of(t) == i:
                seen.add(t["id"])
                out.append(t)
                todo.append(t["id"])
    return out


def tree_cap(db, cfg: dict, task: dict) -> str:
    """Why another follow-up under `task`'s chain root passes chains.max_hops or max_usd for the whole
    tree (tree()): its tasks after the root, its spend with each unfinished task's unspent budget; ''."""
    c = chaincap.caps(cfg)
    root = chaincap.chain(db, int(task["id"]))[0]["id"] if db.task(int(task["id"])) else int(task["id"])
    nodes = tree(db, root)
    hops = len(nodes) - 1
    usd = sum(float(t.get("spent_usd") or 0) + (0.0 if t["status"] in TERMINAL else max(
        0.0, float(t.get("budget_usd") or 0) - float(t.get("spent_usd") or 0))) for t in nodes)
    head = f"the follow-up tree of #{root} "
    if c["max_hops"] and hops + 1 > c["max_hops"]:
        return head + f"has {hops} tasks after it; chains.max_hops is {c['max_hops']:g}"
    if c["max_usd"] and usd >= c["max_usd"]:
        return head + f"spent or holds ${usd:.2f}; chains.max_usd is ${c['max_usd']:g}"
    return ""


def queue(p, cfg: dict, task: dict, fups: list[dict], skip: set[int], gate=None) -> tuple[list, dict]:
    """Queue done `task`'s next-step follow-ups (eligible, not in `skip`: ids of follow-ups already handled)
    when why() holds: ([(follow-up, new task id)], {id(follow-up): why it stays proposed})."""
    reason = why(p.db, task) if enabled(cfg) else ""
    want = [f for f in fups if id(f) not in skip and eligible(f, task["id"])] if reason else []
    if not want:
        return [], {}
    if stop := held(p.db, task, gate):
        return [], {id(f): stop for f in want}
    split = task.get("kind") == "code" and bool(chaincap.split_head(task))
    if split:   # the continue carrying the change takes the branch over first
        want.sort(key=lambda f: not chaincap.code_hop(f, task["id"]))
    done: list = []
    left: dict = {}
    for f in want:
        kind = str(f.get("kind") or task["kind"]).strip().lower()
        stop = (f"over {MAX_QUEUED} follow-ups queued from one hand-off" if len(done) >= MAX_QUEUED else
                f"its kind {kind} is not #{task['id']}'s ({task['kind']}): the coordinator adds it"
                if kind != task["kind"] else
                f"#{task['id']} is a split: only the continue that carries its change takes its branch over"
                if split and not chaincap.code_hop(f, task["id"]) else tree_cap(p.db, cfg, task))
        if stop:
            left[id(f)] = stop
            continue
        refused: list = []
        got = chaincap.auto_hop(p, cfg, task, dict(f, spec=_spec(task, f)), reason, refused)
        if got:
            _inherit(p.db, task, got[0])
            done.append((f, got[0]))
        else:
            left[id(f)] = "; ".join(str(x) for x in refused)[:400] or "task_add did not add it"
    return done, left

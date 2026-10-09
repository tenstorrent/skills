# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a hold waits on, so no blocked task stalls silently.

The coordinator sets a task blocked (task_update status=blocked) only with `waits_on`, one of:
- `ask:<id>`: an open ask (`ask:new`: the ask this turn sends); over once it is resolved or expired,
- `resource:<name>`: over once the resource is not paused and its lock is free,
- `until:<time>`: over at that time (a delay such as 6h or an ISO time, as start_after),
- `when:<probe>`: over once the shell probe exits 0 (as start_when; the daemon runs it).
It is kept as the task label `waits:<kind>:<value>` (an until as epoch seconds). The daemon requeues an
anchored hold once its anchor is over, model-free (Daemon.sweep_holds). It lives only while the task is
blocked: a trigger in db.py drops it from any write that leaves it on a task in another status.

Blocks the daemon or a worker made carry no anchor, nor do holds from before anchors. A hold with no
anchor is stale once held past coordinator.hold_max_h, or once the user wrote after it was held (that
message may be what it waited for). A stale set with a member not in the last one raises one
`stale_holds` event: a high-effort coordinator turn that lists them.
"""
from __future__ import annotations

import json
import time
from typing import Any

PREFIX = "waits:"
KINDS = ("ask", "resource", "until", "when")
NEW_ASK = "new"                 # `ask:new`: the ask sent in the same turn
STALE_EVENT = "stale_holds"
TRIGGER = "stale holds"
SINCE_KEY = "holds_since"       # kv {task id: when the sweep first saw it blocked (or its last update)}
STALE_KEY = "stale_holds_seen"  # kv: the stale set the last sweep saw
HOLD_MAX_H = 12.0
LIST_MAX = 20


def _labels(task: dict) -> list:
    try:
        v = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def anchor(task: dict) -> tuple[str, str] | None:
    """The task's (kind, value) anchor, or None."""
    for lb in _labels(task):
        if isinstance(lb, str) and lb.startswith(PREFIX):
            kind, _, value = lb[len(PREFIX):].partition(":")
            if kind in KINDS and value:
                return kind, value
    return None


def without(labels: list) -> list:
    return [lb for lb in labels if not (isinstance(lb, str) and lb.startswith(PREFIX))]


def label(kind: str, value: str) -> str:
    return f"{PREFIX}{kind}:{value}"


def describe(kind: str, value: str) -> str:
    """'waits on ask #12' and the like, for the digest."""
    if kind == "ask":
        return f"waits on ask #{value}"
    if kind == "until":
        try:
            return "waits until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
        except ValueError:
            return f"waits until {value}"
    return f"waits on {kind} {value[:160]}"


def hold_max_h(cfg: dict) -> float:
    try:
        return max(0.0, float((cfg.get("coordinator") or {}).get("hold_max_h", HOLD_MAX_H)))
    except (TypeError, ValueError):
        return HOLD_MAX_H


def track(db: Any, now: float) -> dict[int, float]:
    """Since when each blocked task is held, {id: ts}, kept in SINCE_KEY so a spec edit does not make an
    old hold look new: the task's last update when first seen, which for a hold from before this sweep
    is the closest record of when it was blocked."""
    old = db.kv(SINCE_KEY, {}) or {}
    since = {}
    for t in db.q("SELECT id, updated FROM tasks WHERE status='blocked'"):
        v = old.get(str(t["id"]))
        since[t["id"]] = float(v) if isinstance(v, (int, float)) else float(t["updated"] or now)
    if {str(k): v for k, v in since.items()} != old:
        db.set_kv(SINCE_KEY, {str(k): v for k, v in since.items()})
    return since


def stale(db: Any, cfg: dict, now: float, since: dict[int, float]) -> list[dict]:
    """Holds with no anchor held past hold_max_h, or older than a user message: {task, title, age_s, why}."""
    limit = hold_max_h(cfg) * 3600
    last_msg = (db.one("SELECT MAX(ts) ts FROM messages WHERE direction='in'") or {}).get("ts")
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status='blocked' ORDER BY id"):
        if anchor(t):
            continue
        at = since.get(t["id"], float(t["updated"] or now))
        age = now - at
        why = (f"held {age / 3600:.1f}h" if limit and age >= limit else
               "the user wrote since" if last_msg and float(last_msg) > at else "")
        if why:
            out.append({"task": t["id"], "title": t["title"], "age_s": age, "why": why,
                        "reason": t["blocked_reason"] or ""})
    return out


def event_text(rows: list[dict]) -> str:
    shown = "; ".join(f"#{r['task']} {r['title'][:70]} ({r['why']}: {r['reason'][:120] or 'no reason given'})"
                      for r in rows[:LIST_MAX])
    more = f"; and {len(rows) - LIST_MAX} more" if len(rows) > LIST_MAX else ""
    return (f"{len(rows)} blocked task(s) wait on nothing anyone will act on: {shown}{more}. A hold never "
            f"replaces an ask or a decision. For each: decide it yourself and requeue or cancel it; or ask_user "
            f"with a valid blocking category and task_update status blocked waits_on ask:new; or give it an end "
            f"with waits_on until:<time>, when:<probe> or resource:<name>.")

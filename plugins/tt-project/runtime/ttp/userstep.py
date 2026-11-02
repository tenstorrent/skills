# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One standing "needs you" item per step only the user can take (a sign-in, a device code, a
credential), per task, however many runs of the task notify it.

A worker's `ttp notify --step <kind>` (or a notify whose text names such a step, STEP_RE) is keyed by
(task id, step kind). The first one is posted as an alert under the condition key
`user-step:<task>:<kind>` (an alerts.USER_BLOCKERS kind: the top section until it clears) and recorded
in kv STEPS with the self-serve command (`--do`), its deadline (`--expires-in`) and the model-free check
that the step is done (`--check`). A later notify from any run of the same task updates that record,
its episode and the posted message's text in place and posts nothing: a new link or code changes the
item, never sends a second alert.

The task's waiting hand-off must wait on that check, never on "the user's next message" (handoff):
the daemon puts the step's check in as its retry_when where the hand-off gave none or only such prose,
else tells the coordinator. Once the task's retry_when passes the step is done (probe_passed): the
item clears, told once, and the task wakes as any passed probe wakes it. A task that ends clears its
items on the next sweep (alerts.holds)."""
from __future__ import annotations

import hashlib
import re
import time

from . import alerts
from .db import DB, TERMINAL_TASK_STATES

STEPS = "user_steps"   # kv: "<task>:<kind>" -> the open step's record
KEY = "user-step"      # alert condition kind
KINDS = ("sign-in", "device-code", "credential")
# A notify naming a step only the user can take, when the worker gave no --step.
STEP_RE = (("device-code", re.compile(r"\bdevice[- ]code\b", re.I)),
           ("sign-in", re.compile(r"\b(sign|log)[- ]?in\b(?!\s+(?:node|host|shell|server|page|(?:is|was)\s+(?:done|ok|back)))", re.I)),
           ("credential", re.compile(r"\b(credential|api[- ]key|access token)s?\b", re.I)))
# A retry_when that is prose for "when the user answers", not a model-free check.
NEXT_MSG_RE = re.compile(r"\b(next|user(?:'s|’s)?|their)\s+(message|reply|answer|response)\b|\buser (?:replies|answers|signs)\b", re.I)
# What the user acts on: a link or a code; a change in it is a new item content.
CONTENT_RE = re.compile(r"https?://\S+|\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b")


def kind_of(n: dict) -> str | None:
    """The step a notify line names: its --step, else the first STEP_RE match of its text."""
    if n.get("step") in KINDS:
        return n["step"]
    text = str(n.get("text") or "")
    return next((k for k, rx in STEP_RE if rx.search(text)), None)


def _content(text: str) -> str:
    return hashlib.sha256(" ".join(sorted(CONTENT_RE.findall(text))).encode()).hexdigest()[:16]


def _open(db: DB, key: str) -> dict | None:
    return db.one("SELECT * FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id DESC LIMIT 1", (key,))


def item_text(head: str, n: dict, where) -> str:
    """The posted line: the notify with the command the user runs and when the step lapses."""
    from . import timefmt
    text = head
    if str(n.get("do") or "").strip():
        text += f" To do it: {' '.join(str(n['do']).split())[:300]}."
    if isinstance(n.get("deadline"), (int, float)) and n["deadline"] > 0:
        text += f" Lapses at {timefmt.short(float(n['deadline']), where)}."
    return text


def relay(db: DB, task: dict, kind: str, n: dict, text: str, now: float | None = None) -> bool:
    """Open the task's standing item for step `kind`, or update the open one in place. True when it
    posted (the first notify of the step), False when it only updated."""
    now = time.time() if now is None else now
    sid = f"{task['id']}:{kind}"
    steps, _ = db.kv_dict(STEPS)
    rec, ep = steps.get(sid), _open(db, f"{KEY}:{sid}")
    content = _content(text)
    fields = {"task": task["id"], "kind": kind, "text": text[:2000], "last": now, "content": content,
              "do": str(n.get("do") or "")[:300] or None, "deadline": n.get("deadline"),
              "check": str(n.get("check") or "")[:500] or (rec or {}).get("check")}
    if isinstance(rec, dict) and ep:
        changed = content != rec.get("content")
        steps[sid] = {**rec, **fields, "notifies": int(rec.get("notifies") or 1) + 1,
                      "changes": int(rec.get("changes") or 0) + changed}
        db.set_kv(STEPS, steps)
        db.x("UPDATE alerts SET last=?, text=? WHERE id=?", (now, text[:2000], ep["id"]))
        if ep["message"]:
            db.x("UPDATE messages SET text=? WHERE id=?", (text, ep["message"]))
        return False
    mid = db.post("out", text, chat=None, kind="alert", severity="normal", ref=f"{KEY}:{sid}")
    for old, r in list(steps.items()):   # records of ended tasks (their items cleared on a sweep)
        t = db.task(r["task"]) if isinstance(r, dict) and isinstance(r.get("task"), int) else None
        if not t or t["status"] in TERMINAL_TASK_STATES:
            steps.pop(old)
    steps[sid] = {**fields, "opened": now, "message": mid, "notifies": 1, "changes": 0}
    db.set_kv(STEPS, steps)
    return True


def for_task(db: DB, tid: int) -> list[dict]:
    steps, _ = db.kv_dict(STEPS)
    return [r for r in steps.values() if isinstance(r, dict) and r.get("task") == tid]


def holds(db: DB, arg: str) -> bool:
    """alerts.holds for `user-step:<task>:<kind>`: its record is open and its task has not ended."""
    steps, _ = db.kv_dict(STEPS)
    if not isinstance(steps.get(arg), dict):
        return False
    tid = arg.partition(":")[0]
    t = db.task(int(tid)) if tid.isdigit() else None
    return bool(t) and t["status"] not in TERMINAL_TASK_STATES


def handoff(db: DB, task: dict, result: dict) -> tuple[dict, str | None]:
    """A waiting hand-off of a task with an open step: one whose retry_when is missing or only prose
    for the user's next message gets the step's check as its retry_when, the prose dropped. (result, what
    was done: 'set', 'missing' when no step has a check, None when nothing applied.)"""
    probe = result.get("retry_when")
    if isinstance(probe, str) and probe.strip() and not NEXT_MSG_RE.search(probe):
        return result, None
    steps = for_task(db, task["id"])
    if not steps:
        return result, None
    check = next((r["check"] for r in steps if r.get("check")), None)
    out = {k: v for k, v in result.items() if k != "retry_when"}
    if isinstance(probe, str) and probe.strip():
        out["retry_when_refused"] = probe[:200]   # prose, never run as a shell command
    if not check:
        return out, "missing"
    return {**out, "retry_when": check, "retry_when_derived": KEY}, "set"


def probe_passed(db: DB, task: dict, probes: set, now: float | None = None) -> list[str]:
    """The task's retry_when passed: each open step it checks (the step's own check, or any step of
    the task that has none; `probes`: the retry_when as run and as the worker wrote it) is done. Its record goes and its item clears, told once. Returns the steps."""
    now = time.time() if now is None else now
    if not for_task(db, task["id"]):
        return []   # the usual pass: no write lock taken
    with db.tx():
        steps, _ = db.kv_dict(STEPS)
        done = [sid for sid, r in steps.items() if isinstance(r, dict) and r.get("task") == task["id"]
                and (not r.get("check") or r.get("check") in probes)]
        if not done:
            return []
        for sid in done:
            steps.pop(sid)
        db.set_kv(STEPS, steps)
        for sid in done:
            for ep in db.q("SELECT * FROM alerts WHERE key=? AND cleared IS NULL ORDER BY id", (f"{KEY}:{sid}",)):
                alerts._close(db, ep, now, "its check passed",
                              f"Cleared: #{task['id']} {task['title'][:80]}: the {sid.partition(':')[2]} step is done; "
                              f"the task goes on.", "low")
    return done

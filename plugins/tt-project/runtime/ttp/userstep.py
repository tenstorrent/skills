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
items on the next sweep (alerts.holds).

A queued task deferred on such a step (its start_why, blocked_reason or start_when names one, with the
user: deferral_kind) gets the same item, opened by `ttp task add`/`set-when`, the coordinator's
task_add/task_update and the daemon's deferral sweep (sync_deferrals), its record marked source
"deferral" and its check the start_when. It clears once that probe passes (probe_passed) or the task
starts, ends, is cancelled or no longer waits on the step (holds). A deferral or wait probe, or a why,
that only waits for the user's next message is refused (check_wait_text): no probe can see it."""
from __future__ import annotations

import hashlib
import re
import time

from . import alerts
from .coordinator import START_WHEN_CHARS
from .db import DB, TERMINAL_TASK_STATES, deferral

STEPS = "user_steps"   # kv: "<task>:<kind>" -> the open step's record
KEY = "user-step"      # alert condition kind
KINDS = ("sign-in", "device-code", "credential")
# A notify naming a step only the user can take, when the worker gave no --step.
STEP_RE = (("device-code", re.compile(r"\bdevice[- ]code\b", re.I)),
           ("sign-in", re.compile(r"\b(sign|log)[- ]?in\b(?!\s+(?:node|host|shell|server|page|(?:is|was)\s+(?:done|ok|back)))", re.I)),
           ("credential", re.compile(r"\b(credential|api[- ]key|access token)s?\b", re.I)))
# A retry_when that is prose for "when the user answers", not a model-free check.
NEXT_MSG_RE = re.compile(r"\b(next|user(?:'s|’s)?|their)\s+(message|reply|answer|response)\b|\buser (?:replies|answers|signs)\b", re.I)
# A why that waits only for the user's next message (a `why` may name the user's reply to an ask it probes).
NEXT_ONLY_RE = re.compile(r"\bnext\s+(?:message|reply|answer|response)\b", re.I)
# A deferral names a step only the user takes when its words also name the user.
USER_RE = re.compile(r"\b(?:user|users|user's|user’s|you|your|human)\b", re.I)
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
              **({"source": n["source"]} if n.get("source") else {}),
              "do": str(n.get("do") or "")[:300] or None, "deadline": n.get("deadline"),
              "check": str(n.get("check") or "")[:START_WHEN_CHARS] or (rec or {}).get("check")}
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
    """alerts.holds for `user-step:<task>:<kind>`: its record is open and its task has not ended; a
    deferral's, while its task is queued and still deferred on that step."""
    steps, _ = db.kv_dict(STEPS)
    rec = steps.get(arg)
    if not isinstance(rec, dict):
        return False
    tid = arg.partition(":")[0]
    t = db.task(int(tid)) if tid.isdigit() else None
    if rec.get("source") == "deferral":
        return bool(t) and deferral_kind(t) == rec.get("kind")
    return bool(t) and t["status"] not in TERMINAL_TASK_STATES


def check_wait_text(text, what: str) -> None:
    """Refuse a start_when/retry_when probe (NEXT_MSG_RE) or a why (NEXT_ONLY_RE) that waits for the
    user's next message: no probe can see it, so the task would never start or wake by itself."""
    if not isinstance(text, str) or not (NEXT_ONLY_RE if what == "why" else NEXT_MSG_RE).search(text):
        return
    raise ValueError(f"{what} {' '.join(text.split())[:120]!r} waits for the user's next message, which no probe "
                     f"can see. Give a model-free check that the user's step is done (a login or status command) "
                     f"as the probe and plain words as --why: a sign-in, device code or credential named there "
                     f"opens the task's one standing 'Needs you' item")


def deferral_kind(t: dict) -> str | None:
    """The user-only step a queued task's deferral waits on: STEP_RE over its start_why and blocked_reason
    (the start_when probe when there is no why), only when those words also name the user. None else."""
    if t.get("status") != "queued":
        return None
    d = deferral(t)
    if not d.get("when"):
        return None
    text = " ".join(str(x) for x in (d.get("why") or d["when"], t.get("blocked_reason")) if x)
    return kind_of({"text": text}) if USER_RE.search(text) else None


def deferral_text(t: dict, kind: str) -> str:
    """The item's plain words: what the user does, where, and what starts afterwards."""
    d = deferral(t)
    what = " ".join(str(d.get("why") or t.get("blocked_reason") or "").split())[:300]
    step = {"sign-in": "a sign-in", "device-code": "a device code", "credential": "a credential"}[kind]
    return (f"#{t['id']} waits on {step} only you can do" + (f": {what}" if what else "") + ". "
            f"Once a check sees it done, #{t['id']} {t['title'][:100]} starts by itself; nothing else is needed.")


def sync_deferrals(db: DB, tasks: list[dict] | None = None, now: float | None = None) -> list[str]:
    """Open (or update in place, never a second alert) the standing item of each queued task deferred on
    a user-only step (deferral_kind), and drop the deferral records whose item no longer holds. `tasks`:
    the queued deferred tasks (default: all). Returns the `<task>:<kind>` items it opened."""
    now = time.time() if now is None else now
    if tasks is None:
        tasks = db.q("SELECT * FROM tasks WHERE status='queued' AND labels LIKE '%\"start_when:%'")
    opened = []
    for t in tasks:
        kind = deferral_kind(t)
        if not kind:
            continue
        sid, text = f"{t['id']}:{kind}", deferral_text(t, kind)
        rec = db.kv_dict(STEPS)[0].get(sid)
        if isinstance(rec, dict) and rec.get("text") == text and rec.get("check") == deferral(t)["when"] \
                and _open(db, f"{KEY}:{sid}"):
            continue   # the usual tick: unchanged, nothing written
        with db.tx():   # one read-modify-write of STEPS with its post
            if relay(db, t, kind, {"text": text, "check": deferral(t)["when"], "source": "deferral"}, text, now):
                opened.append(sid)
    steps, _ = db.kv_dict(STEPS)
    gone = [sid for sid, r in steps.items() if isinstance(r, dict) and r.get("source") == "deferral"
            and not holds(db, sid)]
    if gone:
        with db.tx():
            steps, _ = db.kv_dict(STEPS)
            for sid in gone:
                steps.pop(sid, None)
            db.set_kv(STEPS, steps)
    return opened


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

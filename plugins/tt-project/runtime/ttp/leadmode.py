# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lead mode (coordinator.lead): the coordinator session works the task list itself.

Off by default. On, the persistent coordinator session (tools "full") does the project's work in its
own turns, where it already holds the context, instead of handing each task to a fresh worker run that
has to learn it again, report back, and wake the coordinator to read the report.

Dispatch (worker_task): a worker run starts only for
- a review (an independent review stays independent),
- work a worker already started (attempts, spend or a branch on record; a waiting hand-off has no
  attempt): its hand-off chain goes on,
- a task whose spec has a `worker:` line (the lead's own pick: a long unattended run, or parallel
  independent work),
- a code task the coordinator itself queued: it lands through a worker's hand-off, its review and
  the push queue.
Every other ready task (work, harness, question, plan; and anything the daemon queued on its own, such as a
deflake, waste or throughput task) waits for the lead, which does it, hands it to a worker with a
`worker:` line, or cancels it.

Continue wakes (due): while a task waits for the lead and no coordinator turn runs, the daemon starts
a turn `gap` seconds after the last one ended. A turn that changed nothing on the task list doubles
the next gap (base_gap_s up to max_gap_s); a change resets it. So the lead never sits idle with work
on its list, and a list it cannot move costs at most one turn per max_gap_s."""
from __future__ import annotations

import hashlib
import json
import re
import time

KEY = "lead_mode"                     # kv: {"fp": ..., "streak": n, "at": ts} of the last lead wake
HELD_NOTE = "lead: the coordinator works this task itself"
WORKER_LINE = re.compile(r"(?mi)^\s*worker:")
WORKER_KINDS = ("code",)
DEFAULTS = {"base_gap_s": 30, "max_gap_s": 1800, "list_max": 15}


RESULT_LINE = re.compile(r"(?mi)^result:[ \t]*")
RESULT_MAX = 4000                    # chars of a lead task's `result:` text kept as its result summary


def spec_result(spec: str | None) -> str:
    """The text of the last line of `spec` that starts with `result:` (prefix dropped), through the end
    of the spec, capped at RESULT_MAX; "" when there is none. A lead task the coordinator finishes
    itself ends its spec with that line: it is the task's result summary (coordinator.apply's
    task_update, and the readers of a summary that predate it)."""
    hits = list(RESULT_LINE.finditer(spec or ""))
    if not hits:
        return ""
    text = (spec or "")[hits[-1].end():].strip()
    return text if len(text) <= RESULT_MAX else text[:RESULT_MAX - 1].rstrip() + "…"


def on(cfg: dict) -> bool:
    return bool((cfg.get("coordinator") or {}).get("lead"))


def settings(cfg: dict) -> dict:
    c = cfg.get("coordinator") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        try:
            v = float(c.get(f"lead_{k}", DEFAULTS[k]))
            if v > 0:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def worker_task(task: dict) -> bool:
    """Whether dispatch may start a worker run for `task` in lead mode (module doc)."""
    if task.get("kind") == "review":
        return True
    if int(task.get("attempts") or 0) > 0 or task.get("branch") or float(task.get("spent_usd") or 0) > 0:
        return True
    if WORKER_LINE.search(task.get("spec") or ""):
        return True
    if task.get("origin") == "daemon":
        return False
    return task.get("kind") in WORKER_KINDS


def lead_tasks(ready: list[dict]) -> list[dict]:
    """The ready tasks that wait for the lead, in dispatch's pick order."""
    return [t for t in ready if not worker_task(t)]


def _fingerprint(db) -> str:
    """What a lead turn's progress changes: every open task's status and spec, the newest task, and
    the count of finished ones. Daemon bookkeeping (blocked_reason, labels) is left out."""
    rows = db.q("SELECT id, status, spec FROM tasks WHERE status NOT IN ('done','failed','cancelled') ORDER BY id")
    ended = db.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('done','failed','cancelled')")["n"]
    newest = db.one("SELECT COALESCE(MAX(id),0) n FROM tasks")["n"]
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['id']}|{r['status']}|{hashlib.sha256((r['spec'] or '').encode()).hexdigest()}\n".encode())
    h.update(f"ended={ended} newest={newest}".encode())
    return h.hexdigest()[:24]


def last_turn_end(db) -> float:
    r = db.one("SELECT MAX(COALESCE(ended, started)) t FROM runs WHERE role='coordinator'")
    return float(r["t"] or 0) if r else 0.0


def due(db, cfg: dict, ready: list[dict], now: float | None = None) -> dict | None:
    """A lead wake to start now, or None: {"due": "lead", "tasks": [ids], "fp", "streak", "gap"}.
    Writes nothing: started() records a wake once its turn starts."""
    if not on(cfg):
        return None
    now = time.time() if now is None else now
    tasks = lead_tasks(ready)
    if not tasks:
        return None
    s = settings(cfg)
    ended = last_turn_end(db)
    if now - ended < s["base_gap_s"]:
        return None
    st = db.kv(KEY) or {}
    fp = _fingerprint(db)
    streak = int(st.get("streak") or 0) + 1 if st.get("fp") == fp else 0
    gap = min(s["base_gap_s"] * (2 ** min(streak, 16)), s["max_gap_s"])
    if now - ended < gap:
        return None
    return {"due": "lead", "tasks": [t["id"] for t in tasks[:int(s["list_max"])]], "n": len(tasks),
            "fp": fp, "streak": streak, "gap": gap}


def started(db, lead: dict, now: float | None = None) -> None:
    db.set_kv(KEY, {"fp": lead.get("fp"), "streak": int(lead.get("streak") or 0),
                    "at": time.time() if now is None else now})


def digest(db, lead: dict) -> str:
    """The work-list section of a lead wake's digest."""
    ids = lead.get("tasks") or []
    if not ids:
        return ""
    rows = {r["id"]: r for r in db.q("SELECT id, kind, priority, origin, title, spec FROM tasks "
                                     "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}
    lines = [f"## Lead work list ({lead.get('n', len(ids))} ready for you; pick order)"]
    for i in ids:
        t = rows.get(i)
        if not t:
            continue
        spec = " ".join((t["spec"] or "").split())[:240]
        lines.append(f"- #{t['id']} p{t['priority']} {t['kind']} ({t['origin']}): {t['title'][:160]}"
                     + (f" | {spec}" if spec else ""))
    if lead.get("streak"):
        lines.append(f"(the last {lead['streak']} lead turn(s) changed nothing on this list: finish, hand off "
                     f"with a `worker:` line, defer with start_when, or cancel these tasks)")
    return "\n".join(lines)

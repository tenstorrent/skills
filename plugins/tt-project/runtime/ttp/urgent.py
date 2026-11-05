# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Urgent lane: a time-sensitive task never waits behind routine work.

A task is urgent when its priority is 1 (or better) or its spec has a line `urgent: <why>`, and so is
the harness task finishing a template merge after an upgrade (leads): until it ends the project runs
an older release, so it leads the lane, ahead of priority-1 work. One holds the merge at a time
(release.take_finish_lock), so it cannot starve other work.

- Worker start: db.ready_tasks puts urgent tasks before every other ready task (obligation owners
  included), and among them a fresh `urgent:` line (stated), then obligation owners, before priority
  alone, so a deadline task never waits behind a project's routine priority-1 work. With every worker
  slot of its provider taken, a stated one (extra_ok: user-ordered) starts in up to budget.urgent_extra_slots
  (default EXTRA_SLOTS; 0: off) burst slots beyond the gate's max_parallel, at green, yellow or orange (a
  plan gate of 0 slots included, as critical work); never at red (allow_new_work), past the dollar caps, the
  auth gate, a pause or a resource lock. With the burst slots full too, dispatch preempts the lowest-priority
  routine run of its provider at a safe point (preempt.py; budget.urgent_preempt, default on): stopped
  with its work kept and queued again at its place, never urgent work, a review, a push or a run holding
  a lock. The coordinator turn guard's red is no such hold: it holds routine work only, and urgent and
  user-ordered tasks (user_ordered) start under the rest of the gate (budget.for_task).
- Review: an urgent code task's review is queued as soon as it hands off (done or needs_review)
  at priority 1; the review sweeps skip review.sweep_after_s and review.unreviewed_per_pass for it,
  and an urgent task in review for WAIT_S raises the review_stall event (Daemon._raise_review_stalls).
- Push queue: an approved head of an urgent review or of the urgent task it reviews starts its batch
  at once (pushq.due); the guard against a running batch or deploy stays.
- Waiting lists: exclusive-resource reservations (Daemon._slot_plan), `ttp lock` waiters
  (locks.my_turn, also ahead of routine landings: locks.landing_first), `ttp checks` slots (checkcap) and
  device runner queues (devq.Runner.next_job) serve urgent waiters first, a user-ordered one
  (queue_priority: ORDERED_PRIORITY) ahead of the other urgent ones, in any project.
- Premise: an urgent code task checks its `premise:` line in its own run, not in a question first
  (premise.tend). The coordinator and plan prompts split an urgent goal into tasks that start at once.
  Held locks, holds, pauses and recovery owners are untouched.
- Visibility: `ttp status` and the web app mark urgent tasks with how long each has waited
  (waited_s); Daemon.check_slot_wait raises one event when a ready urgent task has been held by
  anything for coordinator.urgent_slot_wait_min (default 10), naming the hold, and
  Daemon.check_urgent_start one for a user-ordered one with a machine or device resource that has not
  started, or not got its `ttp lock`, within coordinator.urgent_start_wait_min (default START_WAIT_MIN). A wait on another
  task's lane has its own lane_wait event, not a second one here. A hold only time or the user can
  clear (the spend gate allowing no new work: the runaway guard, a cap, a plan window at its line)
  wakes nobody: such a wake would be a self-started coordinator turn adding to the guard's count.
  The task is listed in the next turn's digest instead (SPEND_HELD_KEY, spend_held_lines). One stuck
  task gives one of these (with check_unstarted's ready_not_started) per run and hold
  (Daemon._stuck_raised); a lock wait names only the holders of the lock it waits on (locks.wait_rows)."""
from __future__ import annotations

import json
import re
import time

from . import db as dbmod, timefmt
from .release import UPGRADE_TASK_TITLE

PRIORITY = 1                    # a task at this priority or better is urgent
# A spec line `urgent: <why>`; `urgent: no` (none, not, false, n/a) says it is not.
LINE = re.compile(r"^[ \t]*urgent:[ \t]*(?!(?:no|none|not|false|n/?a)\b)(\S.*)$", re.I | re.M)
# kv: {task id: {title, why, held, clears_at, clears, since}} of ready urgent tasks the spend gate alone
# holds past coordinator.urgent_slot_wait_min (Daemon.check_slot_wait rewrites it each check).
SPEND_HELD_KEY = "urgent_spend_held"
dbmod.DICT_KV.add(SPEND_HELD_KEY)
SINCE_LABEL = "urgent_since:"   # when a task_update added the spec's `urgent:` line (else: the task's creation)
STALE_S = 86400                 # an `urgent:` line this old shows its age (shown_why)
# A claim that the user is there right now: true only when it was written.
PRESENT_RE = re.compile(r"\b(?:the\s+)?user\s+is\s+(?:present|here|online|around|available|at the keyboard)(?:\s+(?:right\s+)?now)?\b", re.I)
EXTRA_SLOTS = 2                 # default of budget.urgent_extra_slots (burst slots past the cap)
ORDERED_PRIORITY = 0            # lock and checks queue priority of a user-ordered urgent task (queue_priority)
START_WAIT_MIN = 5              # default of coordinator.urgent_start_wait_min
RESERVE_SLOTS = 1               # default of budget.urgent_reserve_slots
RESERVE_DUE_S = 3600            # a waiting urgent task due within this long keeps the reserve
WAIT_S = 600                    # an urgent task in review this long raises review_stall


def _priority(task: dict) -> int:
    p = task.get("priority")
    try:
        return 3 if p is None else int(p)
    except (TypeError, ValueError):
        return 3


def leads(task: dict | None) -> bool:
    """The harness task finishing a template merge after an upgrade (release.UPGRADE_TASK_TITLE)."""
    return bool(task) and task.get("kind") == "harness" and task.get("title") == UPGRADE_TASK_TITLE


def user_ordered(task: dict | None, owners=()) -> bool:
    """A task the user asked for: one answering a user's chat (`reply_chat`, also a review of one) or a
    required owner of an open obligation (`owners`: obligations.owner_due's task ids)."""
    return bool(task) and (bool(task.get("reply_chat")) or task.get("id") in owners)


def is_urgent(task: dict | None) -> bool:
    """Priority 1 or better, an `urgent: <why>` line in the spec, or the template-merge task (leads)."""
    if not task:
        return False
    return _priority(task) <= PRIORITY or bool(LINE.search(str(task.get("spec") or ""))) or leads(task)


def why(task: dict) -> str:
    """The spec's `urgent:` reason, else "priority 1"; "" for a task that is not urgent."""
    m = LINE.search(str(task.get("spec") or ""))
    if m:
        return m.group(1).strip()[:160]
    if leads(task) and _priority(task) > PRIORITY:
        return "finishes a template upgrade"
    return f"priority {_priority(task)}" if is_urgent(task) else ""


def lines(spec: str | None) -> list[str]:
    """The reasons of every `urgent:` line in the spec, in order."""
    return [m.group(1).strip() for m in LINE.finditer(str(spec or ""))]


def new_line(old_spec: str | None, new_spec: str | None) -> bool:
    """A spec update that changes the spec's `urgent:` reason (the first such line) to a new one."""
    m = LINE.search(str(new_spec or ""))
    o = LINE.search(str(old_spec or ""))
    return bool(m) and (not o or o.group(1).strip() != m.group(1).strip())


def dated(labels: list, now: float) -> list:
    """`labels` with the urgent line's date (SINCE_LABEL) set to `now`."""
    return [x for x in labels if not str(x).startswith(SINCE_LABEL)] + [f"{SINCE_LABEL}{now:.0f}"]


def line_since(task: dict) -> float | None:
    """When the spec's `urgent:` line was written: its SINCE_LABEL, else when the task was made."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        labels = []
    for x in labels if isinstance(labels, list) else []:
        if str(x).startswith(SINCE_LABEL):
            try:
                return float(str(x)[len(SINCE_LABEL):])
            except ValueError:
                pass
    try:
        return float(task.get("created") or 0) or None
    except (TypeError, ValueError):
        return None


def _age(s: float) -> str:
    h = int(s // 3600)
    return f"{h // 24}d {h % 24}h" if h >= 48 else f"{h}h"


def shown_why(db, task: dict, now: float) -> str:
    """why() for the status board: an `urgent:` line STALE_S or older shows its age and whether a user
    message came since, and never claims the user is there now (PRESENT_RE)."""
    reason = why(task)
    since = line_since(task) if LINE.search(str(task.get("spec") or "")) else None
    if not since or now - since < STALE_S:
        return reason
    age = _age(now - since)
    heard = (db.one("SELECT MAX(ts) ts FROM messages WHERE direction='in' AND kind='user' AND ts>?",
                    (since,)) or {}).get("ts")
    reason = PRESENT_RE.sub(f"the user was there {age} ago", reason)
    return f"{reason} (written {age} ago" + ("" if heard else ", no user message since") + ")"


def stated(task: dict | None, now: float) -> bool:
    """An `urgent: <why>` line in the spec written under STALE_S ago (line_since). A project that runs its
    routine work at priority 1 leaves priority alone saying nothing: the stated line is what marks the
    deadline among them."""
    if not task or not LINE.search(str(task.get("spec") or "")):
        return False
    since = line_since(task)
    return since is None or now - since < STALE_S


def first(rows: list[dict], now: float | None = None, owners=()) -> list[dict]:
    """`rows` in the urgent lane's order, each part in its own order (a stable sort): the template-merge
    task (leads), then a fresh `urgent:` line (stated), then the `owners` (required owners of an open
    obligation, whatever their priority), then the other urgent ones (priority alone), then the rest."""
    now = time.time() if now is None else now
    return sorted(rows, key=lambda r: (not leads(r), not stated(r, now), r["id"] not in owners, not is_urgent(r)))


def extra_ok(task: dict | None, now: float) -> bool:
    """Whether `task` may start in budget.urgent_extra_slots past a full gate: the template-merge task
    or a fresh `urgent:` line, not priority alone, so a project's routine priority-1 work never holds
    the one extra slot a deadline task needs."""
    return leads(task) or stated(task, now)


def queue_priority(task: dict | None, now: float | None = None) -> int | None:
    """A `ttp lock` or `ttp checks` waiter's task priority for its queue ticket: ORDERED_PRIORITY for a
    user-ordered urgent task (extra_ok), PRIORITY for other urgent work, else None (routine)."""
    now = time.time() if now is None else now
    if extra_ok(task, now):
        return ORDERED_PRIORITY
    return PRIORITY if is_urgent(task) else None


def extra_slots(cfg: dict) -> int:
    """budget.urgent_extra_slots: worker slots an urgent task may take beyond max_parallel_workers."""
    try:
        return max(int(((cfg or {}).get("budget") or {}).get("urgent_extra_slots", EXTRA_SLOTS)), 0)
    except (TypeError, ValueError):
        return EXTRA_SLOTS


def reserve_due(db, now: float) -> list[dict]:
    """Urgent tasks dispatch could start now or within RESERVE_DUE_S: queued ones db.ready_tasks(later=True)
    keeps (dependencies met, no start_when probe) and waiting ones whose dependencies are met and that name
    no probe, each with its retry or start_after (not_before) due within RESERVE_DUE_S and no paused resource.
    Provider and a review's pre-review checks are checked per slot, in reserve_slots."""
    from .coordinator import task_resources
    from .db import deferral
    rows = [t for t in db.ready_tasks(later=True) if is_urgent(t)]
    waiting = [t for t in db.q("SELECT * FROM tasks WHERE status='waiting' AND COALESCE(not_before, 0)<?",
                               (now + RESERVE_DUE_S,)) if is_urgent(t)]
    if waiting:
        unmet = db.unmet_dependencies(waiting)
        rows += [t for t in waiting if not unmet[t["id"]] and "when" not in deferral(t)]
    paused = db.paused_resources().keys() if rows else set()
    return [t for t in rows if float(t.get("not_before") or 0) < now + RESERVE_DUE_S
            and float(deferral(t).get("after") or 0) < now + RESERVE_DUE_S and not task_resources(t) & paused]


def reserve_slots(cfg: dict, db, now: float, max_parallel: int, provider: str | None = None,
                  held=None, due: list[dict] | None = None) -> int:
    """Worker slots routine work leaves free (budget.urgent_reserve_slots, default 1, 0: off) while an
    urgent task could start now or within RESERVE_DUE_S (reserve_due; `due` passes a list read earlier):
    so a ready urgent task never waits for routine runs to end. Only one for `provider` (its own, else the
    core provider) counts, not one `held(task)` holds (a review behind its pre-review checks), and not one
    that has left queued or waiting since `due` was read (dispatch started it). Bounded: at least one slot
    stays open to routine work, and with no such task none is kept."""
    try:
        keep = max(int(((cfg or {}).get("budget") or {}).get("urgent_reserve_slots", RESERVE_SLOTS)), 0)
    except (TypeError, ValueError):
        keep = RESERVE_SLOTS
    keep = min(keep, max_parallel - 1)
    if keep <= 0:
        return 0
    core = (cfg or {}).get("core_provider", "claude")
    for t in reserve_due(db, now) if due is None else due:
        if provider and (t.get("provider") or core) != provider or held and held(t):
            continue
        if (db.one("SELECT status FROM tasks WHERE id=?", (t["id"],)) or {}).get("status") in ("queued", "waiting"):
            return keep
    return 0


def covers_urgent(db, task: dict | None) -> bool:
    """Whether `task` is urgent, or is a review of an urgent task (a task it depends on)."""
    if not task:
        return False
    if is_urgent(task):
        return True
    if task.get("kind") != "review":
        return False
    from .db import dependency_ids
    return any(is_urgent(db.task(d)) for d in dependency_ids(task) if isinstance(d, int))


def approvals_waiting(db, target: str) -> list[int]:
    """Queue ids of the approved urgent rows (covers_urgent) for push target `target`, best priority
    first: what a push batch searching for the entry that broke its checks stops for (pushq.yields_to).
    Here, not in pushq, so batch can import it at module level without an import cycle."""
    rows = db.q("SELECT q.id, q.task FROM push_queue q JOIN tasks t ON t.id=q.task WHERE q.status='approved' "
                "AND t.status='pushing' AND q.target=? ORDER BY t.priority, q.id", (target,))
    return [r["id"] for r in rows if covers_urgent(db, db.task(r["task"]))]


def waited_s(db, task: dict, now: float) -> float:
    """How long `task` has waited in its present state: a queued task since it was made or its last
    run ended (whichever is later), one in review since it entered review; 0 for other states."""
    if task.get("status") == "review":
        since = db.review_since().get(task["id"])
    elif task.get("status") == "queued":
        ended = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (task["id"],))
        since = max(float(task.get("created") or 0), float((ended or {}).get("e") or 0))
    else:
        return 0.0
    return max(now - float(since), 0.0) if since else 0.0


def mark(db, task: dict, now: float) -> str:
    """The status mark of an urgent task, e.g. `urgent, waited 12m`; "" for one that is not."""
    if not is_urgent(task):
        return ""
    s = waited_s(db, task, now)
    if not s:
        return "urgent"
    m = int(s // 60)
    return f"urgent, waited {m // 60}h{m % 60:02d}m" if m >= 60 else f"urgent, waited {m}m"


def spend_held_lines(p, db, now: float) -> list[str]:
    """The digest's lines for SPEND_HELD_KEY: one per task still queued, with when its hold is expected
    to clear. No action is asked: only time or the user clears these holds."""
    held, _ = db.kv_dict(SPEND_HELD_KEY)
    rows = {str(t["id"]): t for t in db.q("SELECT id, status FROM tasks WHERE status='queued'")} if held else {}
    lines = []
    for tid, h in sorted(held.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        if tid not in rows or not isinstance(h, dict):
            continue
        at = h.get("clears_at")
        when = (f"expected to clear by {timefmt.short(float(at), p, now)}" if isinstance(at, (int, float))
                else "no clear time known") + f" ({h.get('clears') or 'when the spend gate allows new work'})"
        waited = max(now - float(h.get("since") or now), 0) / 60
        lines.append(f"- #{tid} {h.get('title') or ''} (urgent: {h.get('why') or 'yes'}), waited {waited:.0f} min; "
                     f"held by {h.get('held') or 'the spend gate'}; {when}")
    if lines:
        lines.insert(0, "## Urgent tasks held only by a spend limit (time or the user clears it; it starts first "
                        "once that clears; never raise the limit, say why it waits when asked)")
    return lines

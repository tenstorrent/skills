# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A session coordinator's answer checked before its turn ends (coordinator.session.mode "on").

The coordinator's PreToolUse hook (coordtools.pre_tool_use) runs `refusal` on the turn's
StructuredOutput call. An action coord.apply would refuse (an unknown task id, a dependency that can
no longer finish, a task cap reached, a field a running or finished task keeps) is denied with its
reason as the tool result, so the model fixes or drops it in the same turn instead of learning it at
the next wake. `problems` only reads: it writes no task, kv row or file of the project. Actions that
pass are applied after the turn as before, and coord.apply still runs every check: this one is a
subset, and a refusal it misses is carried to the next wake as before (coordinator.REJECTED_KEY).

It never stops a turn from ending. An action resent unchanged after a denial goes through (apply
refuses it with its usual side effects, such as the retry wake of a task cap), at most MAX_DENIALS
answers per run are denied, and any error lets the answer through (apply is the real gate). The
denied action hashes are kept in the run dir (STATE_FILE), not in the project."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

MAX_DENIALS = 2   # denied answers per run; past it every answer goes through
STATE_FILE = "answer-precheck.json"   # in the run dir: {"denials": n, "seen": [action hashes]}
REQUEUE = ("queued", "blocked", "cancelled", "done")   # the statuses a task_update sets (coord.apply)


def _key(a: dict) -> str:
    return hashlib.sha256(json.dumps(a, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def problems(p, actions: list) -> list[tuple[int, str]]:
    """(action index, why) of each action coord.apply would refuse, by the checks it can make without
    a side effect; [] when it finds none. Reads the project's tasks and config only."""
    from . import coordinator as coord
    from .db import TERMINAL_TASK_STATES, _first_dead, dependency_ids
    from .providers.base import normalize_action
    db, cfg, out = p.db, p.config(), []
    # A config_set in the batch is applied first and may change a cap: the caps are left to apply then.
    caps = not any(isinstance(a, dict) and a.get("type") == "config_set"
                   and "tasks_per_day" in str(a.get("key") or "") for a in actions)
    left: dict[bool, int] = {}
    # The statuses this batch's task_updates set: apply runs them in order, so a dependency requeued
    # or a duplicate cancelled earlier in the batch counts as such (no refusal apply would not make).
    states = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM tasks")}

    def dead(deps: list):
        return _first_dead(deps, states)
    for i, raw in enumerate(actions):
        if not isinstance(raw, dict):
            continue
        try:
            a = normalize_action(raw)
        except ValueError as e:
            out.append((i, str(e)))
            continue
        t = a.get("type")
        try:
            if t == "task_update":
                try:
                    task = db.task(int(a.get("id")))
                except (TypeError, ValueError):
                    raise ValueError("task_update needs the id of a task") from None
                if not task:
                    raise ValueError(f"no task #{a.get('id')}")
                tid, status = task["id"], a.get("status")
                if status == "waiting":
                    raise ValueError(f"#{tid} rejected: {coord.NO_WAITING_STATUS}")
                if task["status"] == "running":
                    if status in REQUEUE and status != "cancelled":
                        raise ValueError(f"task #{tid} is running: send `spec` alone to steer it mid-run, "
                                         f"or set status cancelled")
                    if a.get("resources") is not None:
                        raise ValueError(f"task #{tid} is running: its resources change only while it is not; "
                                         f"cancel it and re-add it with `continues` to move it now")
                if task["status"] in ("running", *TERMINAL_TASK_STATES):
                    if a.get("pr_branch") is not None:
                        raise ValueError(f"task #{tid} is {task['status']}: its branch changes only before it "
                                         f"starts; add a new one with continues and pr_branch \"\"")
                    if a.get("start_after") is not None or a.get("start_when") is not None:
                        raise ValueError(f"task #{tid} is {task['status']}: only a task that has not started "
                                         f"can be deferred; add a new one with start_after/start_when")
                deps = dependency_ids(task)
                new_status = status if status in REQUEUE else task["status"]
                if a.get("depends_on") is not None:
                    deps = coord._new_dependencies(db, task, a["depends_on"])
                    if status not in REQUEUE and task["status"] == "blocked" and dead(dependency_ids(task)):
                        new_status = "queued"   # apply releases a task blocked on a dead dependency
                if new_status == "queued" and (status in REQUEUE or a.get("depends_on") is not None):
                    if found := dead(deps):
                        dep, why = found
                        raise ValueError(f"#{tid} rejected: depends on #{dep} which "
                                         f"{'does not exist' if why == 'does not exist' else 'is ' + why}; "
                                         f"drop or replace depends_on")
                states[tid] = new_status
            elif t == "task_add":
                title = str(a.get("title") or "").strip()
                missing = [f for f, v in (("title", title), ("spec", str(a.get("spec") or "").strip())) if not v]
                if missing:
                    raise ValueError(f"{coord.MISSING_FIELDS} {' and a '.join(missing)}: title and spec must "
                                     f"both be non-null text")
                kinds = coord.task_kinds(p)
                if (a.get("kind") or "work") not in kinds:
                    raise ValueError(f"{coord.UNKNOWN_KIND} {str(a.get('kind'))[:40]!r} is not one of "
                                     f"{', '.join(sorted(kinds))}")
                dup = [r["id"] for r in db.q("SELECT id FROM tasks WHERE title=?", (title,))
                       if states.get(r["id"]) not in TERMINAL_TASK_STATES]
                if dup and str(dup[0]) != str(a.get("continues")):
                    raise ValueError(f"duplicate of open task #{dup[0]}")
                if a.get("continues") is not None:
                    try:
                        old = db.task(int(a["continues"]))
                    except (TypeError, ValueError):
                        raise ValueError("task_add continues must be a task id") from None
                    if not old:
                        raise ValueError(f"task_add continues: no task #{a['continues']}")
                deps = coord._new_dependencies(db, None, a.get("depends_on") or [])
                if found := dead(deps):
                    raise ValueError(f"task_add rejected: depends on #{found[0]} which is {found[1]}; "
                                     f"drop or replace depends_on")
                if caps:
                    review = a.get("kind") == "review"
                    if review not in left:
                        cap = coord.task_cap(cfg, review)
                        left[review] = cap - len(coord.tasks_made(db, time.time() - 86400, review)) if cap > 0 else 0
                    if left[review] <= 0:
                        what = "review tasks" if review else "new tasks (reviews have their own cap)"
                        raise ValueError(f"the 24 h cap on {what} is reached; drop it, or resend it unchanged and "
                                         f"you are woken when a slot frees to add it again")
                    left[review] -= 1
        except ValueError as e:
            out.append((i, str(e).replace(f" {coord.INFO_MARK}", "")))
    return out


def refusal(payload: dict) -> str | None:
    """Why the session coordinator's StructuredOutput call is denied (None: it goes through).
    Fails open: no project, no run dir or any error lets the answer through (module doc)."""
    try:
        answer = payload.get("tool_input")
        actions = answer.get("actions") if isinstance(answer, dict) else None
        base, run_dir = os.environ.get("TTP_PROJECT"), os.environ.get("TTP_RUN_DIR")
        if not actions or not isinstance(actions, list) or not base or not run_dir:
            return None
        state_path = Path(run_dir) / STATE_FILE
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        denials, seen = int(state.get("denials") or 0), set(state.get("seen") or [])
        if denials >= MAX_DENIALS:
            return None
        from .project import Project
        proj = Project(base)
        if not (proj.state / "project.db").is_file():   # opening one would create it
            return None
        found = [(i, why) for i, why in problems(proj, actions)
                 if _key(actions[i]) not in seen]   # resent unchanged: apply decides (module doc)
        if not found:
            return None
        seen |= {_key(actions[i]) for i, _ in found}
        from .project import durable_write
        durable_write(state_path, json.dumps({"denials": denials + 1, "seen": sorted(seen)}))
        lines = "; ".join(f"actions[{i}] ({actions[i].get('type')}): {why}" for i, why in found)
        return (f"this answer was not applied. {lines}. Fix or drop each of these and send the whole answer "
                f"again; the other actions are fine. An action resent unchanged goes through and is refused "
                f"after the turn as before")
    except Exception:
        return None

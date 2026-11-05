# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Preemption for user-ordered urgent work (urgent.py): with every worker slot and every urgent burst slot
(budget.urgent_extra_slots) of its provider taken, a ready user-ordered urgent task (urgent.extra_ok) that
nothing else holds gets the slot of the lowest-priority routine run, stopped at a safe point through the
`ttp stop --kill` path (runner.request_stop with STOP_TEXT: its supervisor ends the agent, the task goes back
to the queue at its place, its worktree, branch and attempts kept, and resumes on its next start).

Never preempted (victims): urgent work or a review of it (urgent.covers_urgent), any review, a harness task,
a task pushing or not running, an open obligation's required owner, a run whose stop is already asked, and
a run holding a `ttp lock` (a device lock or any other: it may be mid-command on the device). Dispatch calls
pick once per held urgent task per tick; budget.urgent_preempt false turns it off.

A coordinator restart takes the same path (request_restart): a task_update on a running task whose spec has a
`restart: <why>` line, or `ttp task <project> restart <id> "<why>"`. The run gets a RESTART file and is stopped
with RESTART_TEXT, at once or, while it holds a `ttp lock`, once it releases it (release_pending, each reap pass).
It is requeued with its updated spec and work kept, spends no attempt, and its next run's prompt starts with the
why (prompts.restart_block). At most budget.restart_max (3) per budget.restart_window_h (6) per task. A run
that ends on its own before the stop (say, still holding its lock) keeps its outcome."""
from __future__ import annotations

import fcntl
import json
import re
import time
from pathlib import Path

from . import locks, runner, shared, urgent
from .project import durable_write

STOP_TEXT = "preempt"   # the STOP file's text: runner.stop_reason reads it as a resumable stop
RESTART_TEXT = "restart"   # the same, for a coordinator restart
RESTART_FILE = "RESTART"   # in the run dir: the restart's why and new instructions, until the run stops
RESTART_KEY = "restart"    # in the task's result: the why its next run's prompt starts with
RESTART_EVENT = "task_restart"
_RESTART_LINE = re.compile(r"(?im)^[ \t>*-]*restart:[ \t]*(\S.*)$")


def enabled(cfg: dict) -> bool:
    """budget.urgent_preempt (default true)."""
    v = ((cfg or {}).get("budget") or {}).get("urgent_preempt", True)
    return v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "no", "off")


def lock_dirs(p, cfg: dict) -> list[Path]:
    """The project's lock directory and each shared resource's (shared.locks_dir)."""
    return [p.state / "locks"] + [shared.root() / r for r in sorted(shared.names(cfg))]


def _taken_labels(d: Path) -> list[dict]:
    """The labels of the slots in `d` held right now (a slot file keeps its label after release), shared
    holders included."""
    out = []
    try:
        slots, groups = sorted(d.glob("*.lock")), sorted(d.glob("*.shared"))
    except OSError:
        return out
    for path in slots:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue   # free
                except OSError:
                    pass
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict):
            out.append(h)
    for g in groups:
        out += [h for h in locks.shared_holders([g.parent / f"{g.name[:-len('.shared')]}.0.lock"])
                if isinstance(h, dict)]
    return out


def lock_holders(dirs: list[Path]) -> set[tuple[str, str]]:
    """(run id, task id) of every lock held now in `dirs`, as their labels name them (locks.run_ids)."""
    return {(str(h.get("run_id") or ""), str(h.get("task_id") or "")) for d in dirs for h in _taken_labels(d)}


def eligible(db, run: dict, task: dict | None, owners, held: set[tuple[str, str]]) -> bool:
    """Whether running worker run `run` of `task` may be preempted (see the module docstring)."""
    if not task or task["status"] != "running" or task["kind"] in ("review", "harness"):
        return False
    if urgent.covers_urgent(db, task) or urgent.leads(task) or task["id"] in owners:
        return False
    if run.get("dir") and (Path(run["dir"]) / "STOP").exists():
        return False
    # a lock labelled with its run or its task (a detached job of it): another project's same id only spares it
    return not any(r == str(run["id"]) or t == str(task["id"]) for r, t in held)


def pick(db, provider: str, owners, held: set[tuple[str, str]]) -> dict | None:
    """The run to preempt among `provider`'s running worker runs: the lowest priority (highest number),
    then the latest started (least work redone); None when no run is eligible."""
    runs = db.q("SELECT id, task, dir, started FROM runs WHERE role='worker' AND status='running' AND provider IS ?",
                (provider,))
    best = None
    for r in runs:
        t = db.task(r["task"]) if r["task"] else None
        if not eligible(db, r, t, owners, held):
            continue
        key = (int(t["priority"] if t["priority"] is not None else 3), float(r["started"] or 0))
        if best is None or key > best[0]:
            best = (key, r, t)
    return None if best is None else {"run": best[1], "task": best[2]}


def stopping(db, provider: str) -> int:
    """`provider`'s running worker runs whose stop was asked (a STOP file) and not yet ended: slots freeing."""
    return sum(1 for r in db.q("SELECT dir FROM runs WHERE role='worker' AND status='running' AND provider IS ?",
                               (provider,)) if r["dir"] and (Path(r["dir"]) / "STOP").exists())


def restart_line(spec: str) -> str:
    """The why of a `restart: <why>` line in `spec`, or "" when it has none."""
    m = _RESTART_LINE.search(spec or "")
    return m.group(1).strip()[:500] if m else ""


def restart_cap(cfg: dict) -> tuple[int, float]:
    """(budget.restart_max, budget.restart_window_h in seconds): restarts a task may have in that window."""
    b = (cfg or {}).get("budget") or {}
    try:
        return max(0, int(b.get("restart_max", 3))), max(0.0, float(b.get("restart_window_h", 6.0))) * 3600
    except (TypeError, ValueError):
        return 3, 6 * 3600.0


def request_restart(p, db, cfg: dict, task: dict, why: str, update: str = "", defer=None) -> str:
    """Restart `task`'s running run with `why` and `update` (the new instructions): "" once asked, else why
    it was refused (not running, or the cap). The event is written in the caller's transaction; the files
    (RESTART, then STOP unless a `ttp lock` of the run or task is held) through `defer` (db.after_commit) when
    given, so a rolled-back update restarts nothing. A second restart before the stop updates the why and
    counts once."""
    runs = [r for r in db.q("SELECT id, dir, task FROM runs WHERE task=? AND status='running' AND role!='coordinator'",
                            (task["id"],)) if r["dir"]]
    if task["status"] != "running" or not runs:
        return f"#{task['id']} is {task['status']}, not running: a spec update reaches its next run"
    now, (cap, window) = time.time(), restart_cap(cfg)
    pending = any((Path(r["dir"]) / RESTART_FILE).exists() for r in runs)
    done = db.one("SELECT COUNT(*) AS n FROM events WHERE task=? AND kind=? AND ts>=?",
                  (task["id"], RESTART_EVENT, now - window))["n"]
    if not pending and done >= cap:
        text = (f"#{task['id']} restart refused: it was restarted {done} times in the last "
                f"{window / 3600:g} h (budget.restart_max {cap}); steer it with a plain spec update, or cancel it "
                f"and task_add a new task with continues={task['id']}")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "task_restart_refused", "low", text, "handled", task["id"]))
        return text
    if not pending:
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "coordinator", RESTART_EVENT, "low", f"#{task['id']} {task['title'][:80]} restarted "
              f"({done + 1} of {cap} in {window / 3600:g} h), its work kept: {why}", "handled", task["id"]))
    note = json.dumps({"why": why, "update": update, "at": now})

    def write():
        for r in runs:
            if Path(r["dir"]).is_dir():
                durable_write(Path(r["dir"]) / RESTART_FILE, note)
        release_pending(p, cfg, runs)
    (defer or (lambda fn: fn()))(write)
    return ""


def release_pending(p, cfg: dict, runs: list[dict]) -> list[int]:
    """Stop each of `runs` (rows with id, dir and task) that has a RESTART file and no STOP, once neither it nor
    its task holds a `ttp lock`. Returns the run ids stopped."""
    waiting = [r for r in runs if r["dir"] and (Path(r["dir"]) / RESTART_FILE).exists()
               and not (Path(r["dir"]) / "STOP").exists()]
    if not waiting:
        return []
    held = lock_holders(lock_dirs(p, cfg))
    out = []
    for r in waiting:
        if any(rid == str(r["id"]) or (r["task"] and t == str(r["task"])) for rid, t in held):
            continue
        runner.request_stop(Path(r["dir"]), RESTART_TEXT)
        out.append(r["id"])
    return out


def take_restart(run_dir: Path) -> dict | None:
    """The restart a run stopped for (its RESTART file), or None."""
    try:
        got = json.loads((run_dir / RESTART_FILE).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None

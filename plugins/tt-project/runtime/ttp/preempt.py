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
pick once per held urgent task per tick; budget.urgent_preempt false turns it off."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

from . import locks, shared, urgent

STOP_TEXT = "preempt"   # the STOP file's text: runner.stop_reason reads it as a resumable stop


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

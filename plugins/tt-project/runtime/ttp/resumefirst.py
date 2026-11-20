# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Ready to resume goes first. A queued task whose wait is over (a waiting hand-off the daemon woke:
its retry_when probe passed, its checks or job failed, its on_pass_cmd did not record done) has work
in flight a worker only has to finish. Within one priority, dispatch picks those before any other
queued task (db.ready_tasks), the one ready longest first; priority still wins across levels, and the
urgent and obligation boosts that ready_tasks applies after this order still come first.

Daemon.check_resume_wait is the starvation guard: one model-free `resume_wait` observation per wait
for a ready-to-resume task still not started `coordinator.resume_wait_min` (default WAIT_MIN) after it
became ready, naming what holds it."""
from __future__ import annotations

from .db import load_result

WAIT_MIN = 15
FP = "resume_wait"


def ready_at(task: dict) -> float | None:
    """When the task became ready to resume (its wake set not_before to then), or None when it is no
    woken wait."""
    prev = load_result(task.get("result"))
    if prev.get("status") != "waiting" or not prev.get("woke"):
        return None
    return float(task.get("not_before") or task.get("updated") or 0)


def order(rows: list[dict]) -> list[dict]:
    """`rows` in (priority, id) order, with each priority's ready-to-resume tasks first, the one ready
    longest first; the rest keep their order. Never moves a task across priorities."""
    out, i = [], 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j]["priority"] == rows[i]["priority"]:
            j += 1
        block = [(ready_at(r), n, r) for n, r in enumerate(rows[i:j])]
        out += [r for at, _, r in sorted(block, key=lambda b: (b[0] is None, b[0] or 0.0, b[1]))]
        i = j
    return out

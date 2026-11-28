# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A waiting task's probe runs the moment what it waits on ends, not when its poll gap runs out.

The daemon asks a task's `retry_when` every few seconds to minutes (waitpoll). Four things end a wait with
a record the daemon can read without a model: a `ttp checks` run's checks.rc, a local `ttp detach` job's
.rc file, a detached push's result marker, and a landing recorded for a task a probe names
(`landed:#<id>`). `ended_since` says whether the probe's evidence changed since the probe last ran, so
Daemon.probe_waiting skips the gap and the probe's own verdict wakes the task model-free; the poll stays
the fallback for everything else.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import checksgone, landed, rebootwait

PUSH_RESULT = re.compile(r"\bpush\s+--result\s+(\S+)")


def _changed(path: Path, at: float) -> bool:
    try:
        return path.stat().st_mtime >= at
    except OSError:
        return False


def ended_since(p, probe: str, at: float) -> bool:
    """Whether a run of checks, a local detached job or a detached push that `probe` waits on wrote its
    end, or a task it waits on to land landed, at or after `at` (the probe's last run)."""
    probe = str(probe or "")
    if checksgone.ended_since(probe, at) or any(_changed(rc, at) for rc in rebootwait.local_jobs(probe)):
        return True
    if any(_changed(Path(m.group(1).strip("'\"")), at) for m in PUSH_RESULT.finditer(probe)):
        return True
    tasks = [int(t) for t in landed.task_refs(probe) if t.isdigit()]
    return bool(tasks) and p.db.one(f"SELECT 1 FROM task_landings WHERE ts>=? AND task IN ({','.join('?' * len(tasks))})",
                                    (at, *tasks)) is not None

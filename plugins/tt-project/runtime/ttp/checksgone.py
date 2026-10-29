# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A wait on a detached `ttp checks` run whose checks left the queue: never asleep for good.

A probe (`retry_when` or `start_when`) that names a run's checks (`ttp checks --result <run dir>`, or
that run dir's checks.rc) waits for a result the checks write when they end. Checks taken out of the
queue before they ran (a cancel, a request removed from the daemon's queue, a waiter killed outright)
write none, so such a probe would say "not yet" for ever. `gone` finds those runs: no exit code, no
queued request and no live checks process, though checks were started there. The daemon records a
'dropped' result for each (cli._checks_drop), so the standard probe answers, and once the probe still
says "not yet" GRACE_S later it wakes the waiting task or clears its start_when itself.
"""
from __future__ import annotations

import shlex
from pathlib import Path

SETTLE_S = 60.0   # gone this long (two looks, not a launch's own gap) before the daemon records 'dropped'
GRACE_S = 600.0   # how long a probe on gone checks may still say "not yet" before the daemon wakes its owner


def run_dirs(probe: str) -> list[Path]:
    """The run dirs whose checks `probe` names: the argument of `ttp checks --result`, and the folder of
    any `<run dir>/checks.rc` it reads. Only existing folders, each once."""
    from .cli import CHECKS_RC
    try:
        words = shlex.split(str(probe or ""), posix=True)
    except ValueError:
        return []
    found: list[Path] = []
    for i, w in enumerate(words):
        if w == "--result" and i >= 1 and words[i - 1] == "checks" and i + 1 < len(words):
            found.append(Path(words[i + 1]))
        elif Path(w).name == CHECKS_RC and Path(w).is_absolute():
            found.append(Path(w).parent)
    out: list[Path] = []
    for d in found:
        if d.is_absolute() and d.is_dir() and d not in out:
            out.append(d)
    return out


def state(run_dir: Path) -> str:
    """'finished' (checks.rc written), 'queued' (a request waits for the daemon), 'running' (the checks
    process lives, waiting for a slot or running), 'gone' (checks were started here but none of those
    holds), or 'none' (no checks were ever started in this run dir)."""
    from . import detach
    from .cli import CHECKS_OUT, CHECKS_PID, CHECKS_RC, _checks_alive, _read_checks_pid
    rc = run_dir / CHECKS_RC
    if rc.exists():
        return "finished"
    if detach.pending(rc):   # a request taken out of the queue is recorded lost here
        return "queued"
    info = _read_checks_pid(run_dir)
    if info and _checks_alive(info):
        return "running"
    if info or (run_dir / CHECKS_PID).exists() or (run_dir / CHECKS_OUT).exists() or detach.lost(rc).exists():
        return "gone"
    return "none"


def gone(probe: str) -> list[Path]:
    """The run dirs `probe` waits on whose checks are neither queued, running nor finished."""
    return [d for d in run_dirs(probe) if state(d) == "gone"]


def stuck(probe: str) -> list[Path]:
    """The run dirs `probe` waits on whose checks are gone, or were recorded dropped (_checks_drop): a
    probe on them that still says "not yet" can never pass."""
    from .cli import _checks_dropped
    return [d for d in run_dirs(probe) if state(d) == "gone" or _checks_dropped(d)]


def settle(run_dir: Path) -> None:
    """Record a 'dropped' result for gone checks, so `ttp checks --result` and a checks.rc test answer."""
    from .cli import CHECKS_RC, _checks_drop
    if not (run_dir / CHECKS_RC).exists():
        _checks_drop(run_dir, "they are neither queued nor running and left no exit code "
                              "(taken out of the checks queue, or killed before they wrote one)")

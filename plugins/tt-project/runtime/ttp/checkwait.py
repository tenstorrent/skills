# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A push check's waits for a grant, so its time limit counts only the time it runs (push._run_one).

The push gives each check a folder of its own in TTP_CHECK_WAITS. Each wait for a grant inside the check
records itself there while it lasts: a `ttp lock` (also a project's own gate behind one), a checks lane
(testslots.suite_hold, the wrapper lane step) and a host-wide checks slot (checkcap.acquire). A record is
open while its process waits, closed once it is granted or gives up, and counts nothing once its process
is gone (locks.record_wait, locks.wait_spans). The push reads it with state(): the seconds waited since the
check started and whether it waits now.

A wait for a slot of a test pool (a check script's or its shards' own pick of a free test host, wrapped in
`ttp pool-wait`) records itself the same way in the folder's POOL subfolder: it is not run time either
(state() counts it), but it is no grant the push may queue on; pool() reads it on its own, for the push's
separate pool-wait bound and its log."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from . import locks

ENV = "TTP_CHECK_WAITS"
POOL = "pool"   # the subfolder of a check's waits for a test-pool slot (pool())


def begin(what: str = "", pool: bool = False) -> tuple[str, str, float] | None:
    """Record a wait for a grant (`pool`: for a test-pool slot) starting now in the check's folder; None
    outside a push check."""
    d = os.environ.get(ENV)
    if not d:
        return None
    if pool:
        d = os.path.join(d, POOL)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return None   # unrecorded: it only costs the check its extension
    start = time.time()
    token = (d, f"{os.getpid()}:{start}:{what}"[:200], start)
    locks.record_wait(Path(d), token[1], start, None)
    return token


def end(token: tuple[str, str, float] | None) -> None:
    """Close the wait begin() recorded (granted, or given up)."""
    if token:
        locks.record_wait(Path(token[0]), token[1], token[2], time.time())


def chain(waiting: Callable[[bool], None] | None, what: str = "") -> Callable[[bool], None] | None:
    """A `waiting(on)` callback (checkcap.acquire, testslots.suite_hold) that also records the wait in the
    check's folder; `waiting` unchanged outside a push check."""
    if not os.environ.get(ENV):
        return waiting
    open_: list = []

    def cb(on: bool) -> None:
        if on and not open_:
            open_.append(begin(what))
        elif not on and open_:
            end(open_.pop())
        if waiting:
            waiting(on)
    return cb


def _spans(d: Path, since: float, now: float) -> tuple[list[tuple[float, float]], bool]:
    """The waits recorded in `d`, clipped to [since, now], and whether one is open now, its process alive."""
    spans, live = [], False
    for start, end_ in locks.wait_spans(d):
        start = max(start, since)
        live = live or end_ is None
        end_ = now if end_ is None else min(end_, now)
        if end_ > start:   # one that ended before `since` counts nothing
            spans.append((start, end_))
    return spans, live


def _union(spans: list[tuple[float, float]]) -> float:
    total, reach = 0.0, float("-inf")
    for start, end_ in sorted(spans):
        if end_ > reach:
            total += end_ - max(start, reach)
            reach = end_
    return total


def state(d: str | Path, since: float, now: float | None = None) -> tuple[float, bool]:
    """(seconds the check with folder `d` waited for grants or test-pool slots since `since`, overlapping
    waits counted once; whether one of its waits for a grant is open now, its process alive). A wait whose
    process died open counts nothing. Liveness is the waiter's flock on its live file (locks.record_wait),
    never its pid: a check runs in a PID-namespace sandbox, so the pid it records names another process here."""
    now = time.time() if now is None else now
    spans, live = _spans(Path(d), since, now)
    return _union(spans + _spans(Path(d) / POOL, since, now)[0]), live


def pool(d: str | Path, since: float, now: float | None = None) -> tuple[float, bool]:
    """(seconds the check with folder `d` or any of its shards waited for a test-pool slot since `since`,
    overlapping waits counted once; whether one of those waits is open now)."""
    now = time.time() if now is None else now
    spans, live = _spans(Path(d) / POOL, since, now)
    return _union(spans), live

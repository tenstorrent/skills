# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A push check's waits for a grant, so its time limit counts only the time it runs (push._run_one).

The push gives each check a folder of its own in TTP_CHECK_WAITS. Each wait for a grant inside the check
records itself there while it lasts: a `ttp lock` (also a project's own gate behind one), a checks lane
(testslots.suite_hold, the wrapper lane step) and a host-wide checks slot (checkcap.acquire). A record is
open while its process waits, closed once it is granted or gives up, and counts nothing once its process
is gone (locks.record_wait, locks.wait_spans). The push reads it with state(): the seconds waited since the
check started and whether it waits now."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from . import locks

ENV = "TTP_CHECK_WAITS"


def begin(what: str = "") -> tuple[str, str, float] | None:
    """Record a wait for a grant starting now in the check's folder; None outside a push check."""
    d = os.environ.get(ENV)
    if not d:
        return None
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


def state(d: str | Path, since: float, now: float | None = None) -> tuple[float, bool]:
    """(seconds the check with folder `d` waited for grants since `since`, overlapping waits counted once;
    whether one of its waits is open now, its process alive). A wait whose process died open counts nothing.
    Liveness is the waiter's flock on its live file (locks.record_wait), never its pid: a check runs in a
    PID-namespace sandbox, so the pid it records names another process here."""
    now = time.time() if now is None else now
    spans, live = [], False
    for start, end_ in locks.wait_spans(Path(d)):
        start = max(start, since)
        live = live or end_ is None
        end_ = now if end_ is None else min(end_, now)
        if end_ > start:   # one that ended before `since` counts nothing
            spans.append((start, end_))
    total, reach = 0.0, float("-inf")
    for start, end_ in sorted(spans):
        if end_ > reach:
            total += end_ - max(start, reach)
            reach = end_
    return total, live

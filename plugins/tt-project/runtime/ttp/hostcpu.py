# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""This host's busy cores now, read from /proc/stat (no process spawned), for checkcap's live-cores gate.

busy_cores() is the CPU time every process used per wall second over the last few seconds: the change
in /proc/stat's aggregate counters since this process's previous reading, when that is SAMPLE_S to
FRESH_S old (a waiter polling every few seconds reads once per poll); else over a fresh SAMPLE_S
window. A reading under SAMPLE_S after the last one returns the last value. None where /proc/stat
cannot be read (macOS): the caller falls back to the load average.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

STAT = Path("/proc/stat")
SAMPLE_S = 1.0    # the shortest window a reading spans
FRESH_S = 30.0    # a previous reading older than this starts a fresh window

_LAST: dict = {}   # "at": (monotonic time, busy jiffies, total jiffies); "busy": (monotonic time, cores)


def cores() -> int:
    """The cores this process may run on: its CPU affinity where the OS has one, else os.cpu_count().
    busy_cores scales by it and checkcap.cores() returns it, so busy and free cores count the same set."""
    try:
        return len(os.sched_getaffinity(0)) or 1
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def jiffies(text: str) -> tuple[int, int] | None:
    """(busy, total) jiffies from /proc/stat's aggregate `cpu` line; idle and iowait are not busy, and
    guest time is already counted in user and nice."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:9]]
            if len(v) < 4:
                return None
            return sum(v) - v[3] - (v[4] if len(v) > 4 else 0), sum(v)
    return None


def _read(read=None) -> tuple[int, int] | None:
    try:
        return jiffies(STAT.read_text() if read is None else read())
    except (OSError, ValueError):
        return None


def busy_cores(read=None, sleep=time.sleep, clock=time.monotonic, cpus: int | None = None) -> float | None:
    """Cores busy now (above); `read` returns /proc/stat's text, `sleep` and `clock` are for tests."""
    now = clock()
    if (b := _LAST.get("busy")) and 0 <= now - b[0] < SAMPLE_S:
        return b[1]
    cur = _read(read)
    if cur is None:
        return None
    prev = _LAST.get("at")
    if prev is None or not SAMPLE_S <= now - prev[0] <= FRESH_S:
        prev = (now, *cur)
        sleep(SAMPLE_S)
        now, cur = clock(), _read(read)
        if cur is None:
            return None
    _LAST["at"] = (now, *cur)
    dt = cur[1] - prev[2]
    if dt <= 0:
        return None
    busy = max(0.0, min(1.0, (cur[0] - prev[1]) / dt)) * (cpus or cores())
    _LAST["busy"] = (now, busy)
    return busy

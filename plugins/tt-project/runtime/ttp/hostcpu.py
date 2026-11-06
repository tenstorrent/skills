# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""This host's busy cores now, read from /proc/stat (no process spawned), for checkcap's live-cores gate.

busy_cores() is the CPU time every process used per wall second over the last few seconds: the change
in /proc/stat's aggregate counters since this process's previous reading, when that is SAMPLE_S to
FRESH_S old (a waiter polling every few seconds reads once per poll); else over a fresh SAMPLE_S
window. A reading under SAMPLE_S after the last one returns the last value. None where /proc/stat
cannot be read (macOS): the caller falls back to the load average.

demand() is the runnable CPU demand over the same window, in cores: the busy cores, or more while tasks
wait to run (`procs_running` in /proc/stat, the mean of the window's two readings less the reader
itself). Unlike the load average it leaves out tasks in uninterruptible sleep (D state, mostly waiting
on I/O), so a host whose disk is contended but whose cores sit idle is not called CPU-overloaded; disk
contention has its own limit (diskgate).
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


def running(text: str) -> int | None:
    """`procs_running` from /proc/stat: the tasks running or waiting to run now (not those in D state)."""
    for line in text.splitlines():
        if line.startswith("procs_running "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _read(read=None) -> tuple[int, int, int | None] | None:
    try:
        text = STAT.read_text() if read is None else read()
        j = jiffies(text)
    except (OSError, ValueError):
        return None
    return None if j is None else (*j, running(text))


def _sample(read, sleep, clock, cpus) -> tuple[float, float | None] | None:
    """(busy cores, runnable tasks scaled to `cpus`, or None) over the window busy_cores describes."""
    now = clock()
    if (b := _LAST.get("busy")) and 0 <= now - b[0] < SAMPLE_S:
        return b[1], b[2]
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
    n = cpus or cores()
    busy = max(0.0, min(1.0, (cur[0] - prev[1]) / dt)) * n
    runnable = None
    if cur[2] is not None and prev[3] is not None:
        # procs_running is host-wide: scale it to the share of the cores this process may use; the
        # reader itself runs too.
        c = cores()
        runnable = max(0.0, (prev[3] + cur[2]) / 2 - 1) * min(1.0, c / (os.cpu_count() or c))
    _LAST["busy"] = (now, busy, runnable)
    return busy, runnable


def busy_cores(read=None, sleep=time.sleep, clock=time.monotonic, cpus: int | None = None) -> float | None:
    """Cores busy now (above); `read` returns /proc/stat's text, `sleep` and `clock` are for tests."""
    got = _sample(read, sleep, clock, cpus)
    return None if got is None else got[0]


def demand(read=None, sleep=time.sleep, clock=time.monotonic, cpus: int | None = None) -> float | None:
    """The runnable CPU demand now in cores (above): the busy cores, or the runnable tasks where more;
    None where /proc/stat cannot be read."""
    got = _sample(read, sleep, clock, cpus)
    if got is None:
        return None
    return got[0] if got[1] is None else max(got[0], got[1])

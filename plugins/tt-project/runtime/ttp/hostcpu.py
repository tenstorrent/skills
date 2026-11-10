# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""This host's busy cores now, read from /proc/stat (no process spawned), for checkcap's live-cores gate.

busy_cores() is the CPU time every process used per wall second over the last few seconds: the change
in /proc/stat's aggregate counters since this process's previous reading, when that is SAMPLE_S to
FRESH_S old (a waiter polling every few seconds reads once per poll); else over a fresh SAMPLE_S
window. A reading under SAMPLE_S after the last one returns the last value. On macOS the same counters
come from the kernel's CPU ticks (host_statistics HOST_CPU_LOAD_INFO, read in-process as top does). None
where neither can be read: the caller falls back to the load average.

demand() is the runnable CPU demand over the same window, in cores: the busy cores, or more while tasks
wait to run (`procs_running` in /proc/stat, the mean of the window's two readings less the reader
itself). Unlike the load average it leaves out tasks in uninterruptible sleep (D state, mostly waiting
on I/O), so a host whose disk is contended but whose cores sit idle is not called CPU-overloaded; disk
contention has its own limit (diskgate). macOS has no procs_running: its demand is the busy cores, or
the 1-minute load where more once the CPU is SATURATED (no idle left to show the waiters). A
Mac's load average also counts threads blocked on I/O (endpoint tools keep it at 30-57 on a mostly
idle laptop), so pacing reads these instead.

mem_free() is the share of memory free now: MemAvailable over MemTotal (/proc/meminfo) on Linux, the
kernel's memory-free level (kern.memorystatus_level, as memory_pressure prints it) on macOS; mem_short()
says why new work should wait when it is under MEM_FREE_MIN.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

STAT = Path("/proc/stat")
MEMINFO = Path("/proc/meminfo")
MACH = sys.platform == "darwin"   # read the kernel's CPU ticks and memory level where /proc cannot be read
MEM_FREE_MIN = 0.15   # under this share of memory free, new work waits (mem_short)
SATURATED = 0.95   # busy share at which the ticks alone (no run queue: macOS) stop telling the demand
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


_LIB: list = []   # [libSystem, host port] once loaded; [None] when it cannot be


def _libsystem():
    if not _LIB:
        try:
            import ctypes
            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            lib.mach_host_self.restype = ctypes.c_uint
            _LIB.extend([lib, lib.mach_host_self()])
        except (OSError, AttributeError):
            _LIB.extend([None, 0])
    return _LIB[0], _LIB[1]


def mach_ticks() -> tuple[int, int] | None:
    """(busy, total) CPU ticks of all cores from macOS's host_statistics(HOST_CPU_LOAD_INFO): user,
    system and nice are busy, idle is not. None where it cannot be read."""
    lib, port = _libsystem()
    if lib is None:
        return None
    import ctypes
    ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)   # CPU_STATE_USER, SYSTEM, IDLE, NICE
    if lib.host_statistics(port, 3, ticks, ctypes.byref(count)) != 0:   # 3: HOST_CPU_LOAD_INFO
        return None
    user, system, idle, nice = ticks
    return user + system + nice, user + system + idle + nice


def _read(read=None) -> tuple[int, int, int | None] | None:
    try:
        text = STAT.read_text() if read is None else read()
        j = jiffies(text)
    except (OSError, ValueError):
        if read is None and MACH and (m := mach_ticks()) is not None:
            return (*m, None)
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
    without a run queue (macOS) a CPU at least SATURATED busy takes the 1-minute load where more, as no
    idle is left to show the waiters. None where neither /proc/stat nor the ticks can be read."""
    got = _sample(read, sleep, clock, cpus)
    if got is None:
        return None
    if got[1] is not None:
        return max(got[0], got[1])
    if got[0] >= SATURATED * (cpus or cores()):   # macOS: no run queue to read; a saturated CPU's load
        try:                                      # average is mostly tasks waiting for a core
            return max(got[0], os.getloadavg()[0])
        except (OSError, AttributeError):
            pass
    return got[0]


def mem_free(read=None) -> float | None:
    """The share of memory free now (0-1): MemAvailable over MemTotal from /proc/meminfo (`read` returns its
    text, for tests), else on macOS kern.memorystatus_level (percent); None where neither can be read."""
    try:
        text = MEMINFO.read_text() if read is None else read()
        kb = {k: int(v.split()[0]) for k, _, v in (ln.partition(":") for ln in text.splitlines())
              if k in ("MemTotal", "MemAvailable") and v.split()}
        if kb.get("MemTotal", 0) > 0 and "MemAvailable" in kb:
            return max(0.0, min(1.0, kb["MemAvailable"] / kb["MemTotal"]))
        return None
    except (OSError, ValueError):
        pass
    return mach_mem_free() if read is None and MACH else None


def mach_mem_free() -> float | None:
    """macOS's kern.memorystatus_level as a share (the free percent memory_pressure prints), or None."""
    lib, _ = _libsystem()
    if lib is None:
        return None
    import ctypes
    level, size = ctypes.c_int(0), ctypes.c_size_t(4)
    if lib.sysctlbyname(b"kern.memorystatus_level", ctypes.byref(level), ctypes.byref(size), None, 0) != 0:
        return None
    return level.value / 100 if 0 < level.value <= 100 else None


def mem_short(free: float | None = None) -> str:
    """Why new work should wait for memory now, or "": its free share (mem_free) under MEM_FREE_MIN. An
    unreadable reading holds nothing."""
    free = mem_free() if free is None else free
    if free is None or free >= MEM_FREE_MIN:
        return ""
    return f"memory short: {free:.0%} free, under {MEM_FREE_MIN:.0%}"

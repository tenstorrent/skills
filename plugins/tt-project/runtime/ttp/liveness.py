"""Whether a process lives, as "alive", "dead" or "unknown".

A worker's shell may run in a sandbox with a PID namespace of its own: its /proc and kill(pid, 0) see
only the sandbox's few processes, so every pid recorded outside reads as gone, and a pid number there
may be another process entirely. pid_state judges a pid only where it can: in the namespace that
recorded it (`ns`, when the record keeps one), else in the daemon's (TTP_PIDNS, which the daemon sets
for every run it starts). Anywhere else it returns "unknown", which callers treat as possibly alive:
never a reason to break a lock, restart a job or declare a holder dead."""
from __future__ import annotations

import os
from typing import Any

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"
FOREIGN = "unknown (other PID namespace)"   # how callers name UNKNOWN in their output


def pid_ns() -> str | None:
    """This process's PID namespace ("pid:[4026531836]"); None where there is no /proc."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def foreign(ns: str | None = None) -> bool:
    """Whether this process runs in a PID namespace other than `ns` (the one a pid was recorded in), or,
    with no `ns`, other than the daemon's (TTP_PIDNS). False when either side cannot tell."""
    want = ns or os.environ.get("TTP_PIDNS")
    here = pid_ns()
    return bool(want and here and here != want)


def pid_state(pid: Any, ns: str | None = None) -> str:
    """ALIVE, DEAD, or UNKNOWN when this process cannot see the pid's namespace (foreign). A pid that is
    no positive number is DEAD; a pid that exists but is not ours to signal is ALIVE."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return DEAD
    if pid <= 0:
        return DEAD
    if foreign(ns):
        return UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return DEAD
    except PermissionError:
        return ALIVE
    except OSError:
        return UNKNOWN
    return ALIVE

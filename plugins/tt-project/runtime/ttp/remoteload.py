"""The runnable CPU demand of a remote checks host, so a check run there waits on that host's load.

A check that runs on another machine (its own "host", delivery.checks_host, a spill host: push.remote_host)
uses none of this machine's CPU, so it neither waits for this machine's load guard (testslots.load_guard)
nor takes its host-wide checks slot or test slots (push.run_slot). It waits instead while that host's
runnable demand is above checks.load_guard times its cores, at most checks.push_load_wait_s.

reading(host) is one ssh read of the host's /proc/stat twice, SAMPLE_S apart: the busy cores over that
window, or the runnable tasks (procs_running, less the reader) where more, and the host's core count
(its cpuN lines). Readings are cached CACHE_S per host in this process; an unreadable host (ssh fails,
no /proc/stat) gives None, cached too, so it never holds a run.
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable

from . import hostcpu

CACHE_S = 30.0
SAMPLE_S = 1
TIMEOUT_S = 15
POLL_S = 15.0
SEP = "--ttp-sample--"
SCRIPT = f"cat /proc/stat && sleep {SAMPLE_S} && echo {SEP} && cat /proc/stat"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]

_CACHE: dict = {}   # host: (monotonic time, (demand, cores) or None)


def parse(text: str) -> tuple[float, int] | None:
    """(runnable demand in cores, cores) from SCRIPT's output, or None."""
    if SEP not in text:
        return None
    a, b = text.split(SEP, 1)
    ja, jb = hostcpu.jiffies(a), hostcpu.jiffies(b)
    cores = sum(1 for line in b.splitlines() if line.startswith("cpu") and line[3:4].isdigit())
    if not ja or not jb or cores < 1 or (dt := jb[1] - ja[1]) <= 0:
        return None
    busy = max(0.0, min(1.0, (jb[0] - ja[0]) / dt)) * cores
    ra, rb = hostcpu.running(a), hostcpu.running(b)
    runnable = None if ra is None or rb is None else max(0.0, (ra + rb) / 2 - 1)
    return (busy if runnable is None else max(busy, runnable)), cores


def reading(host: str, run=subprocess.run, clock=time.monotonic) -> tuple[float, int] | None:
    """(demand, cores) of `host` (above), cached CACHE_S; None when it cannot be read."""
    now = clock()
    if (hit := _CACHE.get(host)) and 0 <= now - hit[0] < CACHE_S:
        return hit[1]
    got = None
    if host and not host.startswith("-"):
        try:
            r = run([*SSH, "--", host, SCRIPT], capture_output=True, text=True, timeout=TIMEOUT_S,
                    stdin=subprocess.DEVNULL)
            got = parse(r.stdout or "") if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            got = None
    _CACHE[host] = (now, got)
    return got


def held(host: str) -> str:
    """Why no new check run may start on `host` now, or "": its demand is above checks.load_guard
    (testslots.load_guard_factor; 0 is off) times its cores. An unreadable host never holds a run."""
    from . import testslots
    if not (factor := testslots.load_guard_factor()) or (r := reading(host)) is None or r[0] <= factor * r[1]:
        return ""
    return (f"the check host {host}'s CPU is overloaded: its runnable demand of {r[0]:.1f} cores is above "
            + (f"its {r[1]} cores" if factor == 1.0 else f"{factor:g} x its {r[1]} cores")
            + " (CPU limit checks.load_guard)")


def wait(hosts: list[str], say: Callable[[str], None], bound: float | None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    """Wait while one of `hosts` is overloaded (held), at most `bound` seconds (None: until it drops).
    "" once all may start, else the last reason when the bound passed (the run then goes ahead)."""
    start, said = clock(), ""
    while True:
        host, why = next(((h, w) for h in hosts if (w := held(h))), ("", ""))
        if not why:
            return ""
        if bound is not None and clock() - start >= bound:
            say(f"{why}; waited {clock() - start:.0f}s (checks.push_load_wait_s), starting anyway")
            return why
        if host != said:   # once per host it waits for, not at each new reading
            say(f"{why}; waiting for it to drop"
                + (f" (at most {bound:.0f}s, checks.push_load_wait_s)" if bound is not None else ""))
            said = host
        sleep(POLL_S)

"""The runnable CPU demand of a remote checks host, so a check run there waits on that host's load.

A check that runs on another machine (its own "host", delivery.checks_host, a spill host: push.remote_host)
uses none of this machine's CPU, so it neither waits for this machine's load guard (testslots.load_guard)
nor takes its host-wide checks slot or test slots (push.run_slot). It waits instead while that host's
runnable demand is near its cores (at or above checkspill.LOAD_NEAR x them, or checks.load_guard x them
where lower), and never starts there while it is: checks.push_load_wait_s only says how often it tells the
wait again. Runs from this machine that started there in the last LAG_S count in its demand, one run's
cores each (checklane.run_cores), since the host's reading may not show them yet: many runs waiting for
one host start a few at a time as its demand allows, never all at once.

reading(host) is one ssh read of the host's /proc/stat twice, SAMPLE_S apart: the busy cores over that
window, or the runnable tasks (procs_running, less the reader) where more, and the host's core count
(its cpuN lines). Readings are cached CACHE_S per host in this process; an unreadable host (ssh fails,
no /proc/stat) gives None, cached too, so it never holds a run.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
from typing import Callable

from . import hostcpu

CACHE_S = 30.0
SAMPLE_S = 1
TIMEOUT_S = 15
POLL_S = 15.0
TELL_MIN_S = 60.0   # the wait is told again at most this often
LAG_S = 90.0   # a run started this recently may not show in its host's demand yet (_starts)
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


def reading(host: str, run=subprocess.run, clock=time.monotonic, fresh: bool = False) -> tuple[float, int] | None:
    """(demand, cores) of `host` (above), cached CACHE_S unless `fresh`; None when it cannot be read."""
    now = clock()
    if not fresh and (hit := _CACHE.get(host)) and 0 <= now - hit[0] < CACHE_S:
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


def factor() -> float:
    """The share of a check host's cores its demand may reach before no new run starts there: the lower of
    checkspill.LOAD_NEAR and checks.load_guard (testslots.load_guard_factor); 0 (the guard off) holds none."""
    from . import checkspill, testslots
    return min(f, checkspill.LOAD_NEAR) if (f := testslots.load_guard_factor()) else 0.0


def _starts_file(host: str):
    from . import checkcap
    return checkcap.slot_dir() / "remote" / f"{host}.starts"


def _starts(text: str, now: float) -> list[float]:
    """The start times in a starts file's `text` from the last LAG_S."""
    try:
        got = json.loads(text or "[]")
    except ValueError:
        return []
    return [float(t) for t in got if isinstance(t, (int, float)) and 0 <= now - float(t) < LAG_S] \
        if isinstance(got, list) else []


def held(host: str, start: bool = False, fresh: bool = False, now: Callable[[], float] = time.time) -> str:
    """Why no new check run may start on `host` now, or "": its demand, with one run's cores for each run
    this machine started there in the last LAG_S, is at or above factor() x its cores. With `start` the
    answer "" also records this run's start (under the host's starts file lock, so runs waiting together
    each see the others). An unreadable host never holds a run."""
    from . import checklane
    if not (f := factor()) or (r := reading(host, fresh=fresh)) is None:
        return ""
    path = _starts_file(host)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        fh = None
    try:
        if fh is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
        t = now()
        recent = _starts(fh.read() if fh is not None else "", t)
        demand = r[0] + len(recent) * checklane.run_cores(None, r[1])
        if demand < f * r[1]:
            if start and fh is not None:
                fh.seek(0)
                fh.truncate(0)
                fh.write(json.dumps([*recent, t]))
                fh.flush()
            return ""
    finally:
        if fh is not None:
            fh.close()
    return (f"the check host {host}'s CPU is overloaded: its runnable demand of {r[0]:.1f} cores"
            + (f" (and {len(recent)} run(s) just started there from this machine)" if recent else "")
            + f" is near its {r[1]} cores (no check starts at {f:g} x or above: checkspill.LOAD_NEAR, "
              "CPU limit checks.load_guard)")


def wait(hosts: list[str], say: Callable[[str], None], bound: float | None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    """Wait while one of `hosts` is overloaded (held): never start on one that is. `bound` (seconds; None:
    once) is how often the wait is told again. "" once all may start, each start recorded (held's `start`)."""
    start, said, told_at = clock(), "", clock()
    while True:
        host, why = next(((h, w) for h in hosts if (w := held(h))), ("", ""))
        if not why:   # each host again, fresh and under its lock, recording this start
            host, why = next(((h, w) for h in hosts if (w := held(h, start=True, fresh=True))), ("", ""))
            if not why:
                return ""
        if host != said:   # once per host it waits for, not at each new reading
            say(f"{why}; waiting for it to drop, never starting there while it is"
                + (f" (told again every {bound:.0f}s, checks.push_load_wait_s)" if bound is not None else ""))
            said, told_at = host, clock()
        elif bound is not None and clock() - told_at >= max(bound, TELL_MIN_S):
            say(f"{why}; still waiting after {clock() - start:.0f}s, never starting there while it is")
            told_at = clock()
        sleep(POLL_S)

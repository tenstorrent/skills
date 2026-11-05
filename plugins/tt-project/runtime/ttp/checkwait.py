# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A push check's waits for a grant, so its time limit leaves them out, and for a test-pool slot, which it counts
(push._timed_wait).

The push gives each check a folder of its own in TTP_CHECK_WAITS. Each wait for a grant inside the check
records itself there while it lasts: a `ttp lock` (also a project's own gate behind one), a checks lane
(testslots.suite_hold, the wrapper lane step) and a host-wide checks slot (checkcap.acquire). A record is
open while its process waits, closed once it is granted or gives up, and counts nothing once its process
is gone (locks.record_wait, locks.wait_spans). The push reads it with grants(): the seconds waited since the
check started and whether it waits now.

A wait for a slot of a test pool (a check script's or its shards' own pick of a free test host, wrapped in
`ttp pool-wait`, marked live with `ttp pool-wait --start` / `--stop`, or reported once over with `--waited`)
records itself the same way in the folder's POOL subfolder: it is wall time of the check, which its time limit
counts, and no grant the push may queue on; pool() reads it on its own, for the push's pool-wait bound and the
live and final pool-wait figures of its log. The push keeps what it read in a Seen: a wait it once counted stays
counted, even when its waiter dies before closing its record (which then counts nothing here)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from . import locks

ENV = "TTP_CHECK_WAITS"
POOL = "pool"   # the subfolder of a check's waits for a test-pool slot (pool())
LIVE = "live:"  # the key prefix of a live pool wait start() marks and stop() ends
LIVE_POLL_S = 1.0   # how often a live mark's holder checks that it was stopped or its caller is gone
LIVE_MAX_S = 24 * 3600.0   # a live mark nobody stopped (its push killed, its folder kept) ends then


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


def waited(seconds: float, what: str = "") -> bool:
    """Record a test-pool wait that already ended: the last `seconds` up to now (a pool that times its own pick
    of a slot, as a running check's shard does, reports it once granted). False outside a push check."""
    token = begin(what, pool=True)
    if not token:
        return False
    now = token[2]
    locks.record_wait(Path(token[0]), token[1], now - seconds, now)
    return True


def end(token: tuple[str, str, float] | None) -> None:
    """Close the wait begin() recorded (granted, or given up)."""
    if token:
        locks.record_wait(Path(token[0]), token[1], token[2], time.time())


def _pool_dir() -> Path | None:
    d = os.environ.get(ENV)
    if not d:
        return None
    try:
        os.makedirs(os.path.join(d, POOL), exist_ok=True)
    except OSError:
        return None
    return Path(d) / POOL


def _records(d: Path) -> dict:
    try:
        waits = json.loads((d / locks.WAITS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return waits if isinstance(waits, dict) else {}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True   # someone else's process: it exists
    return True


def _live_prefix(what: str, owner: str) -> str:
    return f"{LIVE}{owner}:{what}:"[:150]


def start(what: str = "", owner: str = "", watch: int = 0) -> bool:
    """Mark a test-pool wait open from now (`ttp pool-wait --start`) until stop() with the same `what` and `owner`,
    or until process `watch` (the calling script) ends, which closes it then, or the check ends (its waits folder
    gone; LIVE_MAX_S at most). A forked holder keeps the record's live flock (locks.record_wait) and checks every
    LIVE_POLL_S; it holds no other file of the caller's open, so the check's output ends when the check does.
    Already open: nothing more. False outside a push check."""
    d = _pool_dir()
    if d is None:
        return False
    prefix = _live_prefix(what, owner)
    for key, rec in _records(d).items():
        if (str(key).startswith(prefix) and isinstance(rec, dict) and rec.get("end") is None
                and locks._wait_alive(d, rec)):
            return True
    t0 = time.time()
    key = f"{prefix}{t0}"
    r, w = os.pipe()
    pid = os.fork()
    if pid:
        os.close(w)
        try:
            ok = os.read(r, 1) == b"1"   # the holder wrote the open record and holds its flock
        finally:
            os.close(r)
            os.waitpid(pid, 0)   # the first fork; the holder is its child
        return ok
    try:   # the first fork: detach the holder so the caller never waits on it
        os.close(r)
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        os.closerange(3, w)
        os.closerange(w + 1, 1 << 16)
        locks.record_wait(d, key, t0, None)
        os.write(w, b"1")
        os.close(w)
        while True:
            time.sleep(LIVE_POLL_S)
            rec = _records(d).get(key)
            end_ = rec.get("end") if isinstance(rec, dict) else t0
            if end_ is not None:   # stopped: release the flock, the record stays as stop() wrote it
                locks.record_wait(d, key, t0, float(end_))
                break
            if (watch and not _alive(watch)) or time.time() - t0 >= LIVE_MAX_S:
                locks.record_wait(d, key, t0, time.time())   # its script ended while it waited: until then
                break
    finally:
        os._exit(0)


def stop(what: str = "", owner: str = "") -> int:
    """End the live pool waits start() marked with the same `what` and `owner` (`ttp pool-wait --stop`), now; how
    many were open (0 outside a push check, or with none open)."""
    d = _pool_dir()
    if d is None:
        return 0
    prefix, now, n = _live_prefix(what, owner), time.time(), 0
    for key, rec in _records(d).items():
        if str(key).startswith(prefix) and isinstance(rec, dict) and rec.get("end") is None:
            try:
                t0 = float(rec.get("start"))
            except (TypeError, ValueError):
                continue
            locks.record_wait(d, str(key), t0, max(now, t0))
            n += 1
    return n


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


def grants(d: str | Path, since: float, now: float | None = None) -> tuple[float, bool]:
    """(seconds the check with folder `d` waited for grants since `since`, overlapping waits counted once, its
    test-pool waits left out; whether one of its waits for a grant is open now, its process alive)."""
    now = time.time() if now is None else now
    spans, live = _spans(Path(d), since, now)
    return _union(spans), live


def pool(d: str | Path, since: float, now: float | None = None) -> tuple[float, bool]:
    """(seconds the check with folder `d` or any of its shards waited for a test-pool slot since `since`,
    overlapping waits counted once; whether one of those waits is open now)."""
    now = time.time() if now is None else now
    spans, live = _spans(Path(d) / POOL, since, now)
    return _union(spans), live


class Seen:
    """A check's wait total as the push read it, poll by poll, that never shrinks: each read adds only what the
    waits grew by since the last one. A wait whose waiter died open (killed mid-wait) drops out of state() and
    pool(), but the time it was seen waiting stays out of the check's run time; it stops growing from then on."""

    def __init__(self) -> None:
        self.total = self.last = 0.0

    def read(self, now_total: float, cap: float) -> float:
        """The total after a read of `now_total` seconds, never past `cap` (the check's time since it started)."""
        self.total = min(self.total + max(0.0, now_total - self.last), cap)
        self.last = now_total
        return self.total

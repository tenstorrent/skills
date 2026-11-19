# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A host-wide cap on chat-provider calls in flight (account setting chat.max_inflight).

A chat endpoint may be shared with other users, so the calls every project and run on this host
send to it at once (workers' bridges, benches, probes, babysat runs) are counted together: each
call holds one slot while it is in flight, from the request until its answer (a stream's last line)
is read. Unset or 0 (the shipped default): no cap, and acquire() returns at once.

Slots are files `slot.<i>` under the account's locks/chat-inflight folder, each held by an flock on
its own open file, so a process that dies frees its slot with it (the kernel drops the lock); a
call that finds no free slot waits, polling with jitter, and its wait is counted (Waits).

Refusals: a capacity or rate-limit answer (HTTP 429, 503, 529) halves the host's allowed level at
once (floor 1, refused()); it then comes back by one per quiet minute (RESTORE_S), never above the
cap. The level lives in level.json beside the slots, written under level.lock. Calls already in
flight when the level drops finish; new ones take only slots below the level.
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import time
from pathlib import Path

KEY = "chat.max_inflight"   # account setting: the cap (a whole number; unset or 0: no cap)
DIR = "chat-inflight"   # under the account home's locks/
RESTORE_S = 60.0   # one more call allowed per this many quiet seconds after a refusal
POLL_S = (0.2, 1.0)   # a waiting call's next try, a random wait in this range
REFUSALS = (429, 503, 529)   # answers that lower the level


def cap() -> int:
    """The host's cap from the account settings, read fresh; 0 for none."""
    from .project import load_account_settings
    try:
        v = (load_account_settings().get("chat") or {}).get("max_inflight")
    except (OSError, ValueError):
        return 0
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0


def folder() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / "locks" / DIR


def _read_level() -> dict:
    try:
        got = json.loads((folder() / "level.json").read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def allowed(now: float | None = None, limit: int | None = None) -> int:
    """How many calls may be in flight now: the cap, or the level a refusal lowered it to plus one per
    quiet RESTORE_S since; 0 when there is no cap."""
    c = cap() if limit is None else limit
    if c <= 0:
        return 0
    lv = _read_level()
    try:
        level, at = int(lv["level"]), float(lv["at"])
    except (KeyError, TypeError, ValueError):
        return c
    now = time.time() if now is None else now
    return max(1, min(c, level + int(max(0.0, now - at) // RESTORE_S)))


def refused(now: float | None = None) -> int:
    """Record a refusal: halve the allowed level at once (floor 1). Returns the new level (0: no cap)."""
    c = cap()
    if c <= 0:
        return 0
    d = folder()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "level.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return 0
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time() if now is None else now
        level = max(1, allowed(now, c) // 2)
        tmp = d / f"level.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"level": level, "at": round(now, 3)}))
        os.replace(tmp, d / "level.json")
        return level
    except OSError:
        return 0
    finally:
        os.close(fd)


class Slot:
    """One held slot (index -1, fd None: no cap); release() frees it, at most once."""

    def __init__(self, fd: int | None = None, index: int = -1, waited_s: float = 0.0):
        self.fd, self.index, self.waited_s = fd, index, waited_s

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)   # closing the file drops its flock
            except OSError:
                pass

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _try(d: Path, n: int) -> tuple[int, int] | None:
    for i in range(n):
        try:
            fd = os.open(d / f"slot.{i}", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, i
        except OSError:
            os.close(fd)
    return None


def acquire(timeout_s: float, sleep=time.sleep, clock=time.monotonic) -> Slot | None:
    """A slot for one call, waiting up to `timeout_s`; None when none came free in time. No cap: a
    Slot that holds nothing, at once. The cap and level are read again on every try."""
    start = clock()
    while True:
        n = allowed()
        if n <= 0:
            return Slot()
        d = folder()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Slot()   # the account home cannot be written: no cap can hold, never stop the call
        got = _try(d, n)
        if got:
            return Slot(got[0], got[1], round(clock() - start, 3))
        if clock() - start >= timeout_s:
            return None
        sleep(random.uniform(*POLL_S))


def held() -> int:
    """How many slots are held now (a read: each free one is locked and let go at once)."""
    d = folder()
    n = 0
    for f in sorted(d.glob("slot.*")) if d.is_dir() else ():
        try:
            fd = os.open(f, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            n += 1
        finally:
            os.close(fd)
    return n


class Waits:
    """One run's slot waits, for its exit record: calls, waits (calls that waited), total, p90, and
    calls that found no slot in time (timeouts) and refusals that lowered the level."""

    def __init__(self):
        self.calls, self.timeouts, self.refusals, self._w = 0, 0, 0, []

    def add(self, waited_s: float) -> None:
        self.calls += 1
        self._w.append(float(waited_s))

    def export(self) -> dict:
        w = sorted(self._w)
        waited = [x for x in w if x > 0.0]
        p90 = w[min(len(w) - 1, int(0.9 * len(w)))] if w else 0.0
        return {"calls": self.calls, "waits": len(waited), "waited_s": round(sum(waited), 1),
                "p90_s": round(p90, 2), "timeouts": self.timeouts, "refusals": self.refusals}

    def __bool__(self) -> bool:
        return bool(self.calls or self.timeouts or self.refusals)

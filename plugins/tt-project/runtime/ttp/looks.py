"""Looks the coordinator sets for itself: when it wakes next, what it checks and why.

The `look` action stores one. While any is pending, it replaces the fixed idle and idle-slot wakes
(daemon.idle_wake), so the coordinator, not a timer, decides when it checks in. A due look starts
a turn in maybe_coordinate unless its optional check, run model-free, exits 0 (nothing changed).
The overseer starts that turn itself when the tick loop is stuck (overseer.wake). Both paths take
the start lock, so they never start two turns."""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

from . import probespawn

KEY = "coord_looks"
DONE_KEY = "coord_looks_done"
MAX = 10                  # pending looks kept; a new one past this drops the latest-due
HORIZON_S = 7 * 86400     # a look further out is refused
CHECK_TIMEOUT_S = 20      # inside the coordinator tick step budget
START_LOCK = "coord-start.lock"


def pending(db) -> list[dict]:
    return sorted(db.kv(KEY) or [], key=lambda x: x["at"])


def add(db, at: float, text: str, check: str | None = None, by: str = "coordinator",
        now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if at - now > HORIZON_S:
        raise ValueError(f"look: `at` is more than {HORIZON_S // 86400} days out")
    if not text.strip():
        raise ValueError("look: give `text`: what to check and why")
    look = {"id": uuid.uuid4().hex[:8], "at": max(at, now), "text": text.strip()[:1000],
            "check": (check or "").strip() or None, "by": by, "set_at": now}
    with db.tx():
        db.set_kv(KEY, pending(db)[:MAX - 1] + [look])
    return look


def clear(db) -> int:
    with db.tx():
        n = len(pending(db))
        db.set_kv(KEY, [])
    return n


def due(db, now: float) -> list[dict]:
    return [x for x in pending(db) if x["at"] <= now]


def next_at(db) -> float | None:
    looks = pending(db)
    return looks[0]["at"] if looks else None


def settle(db, ids: list[str], status: str, run: int | None = None, now: float | None = None) -> None:
    """Drop the looks `ids` from pending; the last 20 settled stay in DONE_KEY for status and tests."""
    if not ids:
        return
    now = time.time() if now is None else now
    with db.tx():
        looks = pending(db)
        done = [{**x, "status": status, "run": run, "settled": now} for x in looks if x["id"] in ids]
        db.set_kv(KEY, [x for x in looks if x["id"] not in ids])
        db.set_kv(DONE_KEY, ((db.kv(DONE_KEY) or []) + done)[-20:])


def screen(db, base: Path, looks: list[dict], now: float) -> list[dict]:
    """The due looks that still need a turn: a look whose check exits 0 is settled 'skipped'."""
    fire, skipped = [], []
    for x in looks:
        if x.get("check"):
            try:
                cmd, env = probespawn.hidden(x["check"], None)
                rc = subprocess.run(cmd, shell=True, cwd=str(base), env=env, capture_output=True,
                                    timeout=CHECK_TIMEOUT_S).returncode
            except (subprocess.TimeoutExpired, OSError):
                rc = 1   # a check that cannot answer wakes, so a look is never lost
            if rc == 0:
                skipped.append(x["id"])
                continue
        fire.append(x)
    settle(db, skipped, "skipped", now=now)
    return fire


def digest(looks: list[dict], tz_fmt=None) -> str:
    rows = [f"- {tz_fmt(x['at']) if tz_fmt else int(x['at'])} ({x['by']}): {x['text']}" for x in looks]
    return "## Looks you set (due now)\n" + "\n".join(rows) + \
        "\nSet your next look with a `look` action, or none to fall back to the idle wake."


@contextlib.contextmanager
def start_lock(state: Path) -> Iterator[bool]:
    """Non-blocking: yields False while the other path (daemon tick or overseer wake) is starting a turn."""
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(state / START_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)

"""The scheduler's audit log: one JSON line in the project's state/scheduler.jsonl for each slot fill
(a dispatch pass with at least one free worker slot), so whether a pick followed priority and
obligation order can be checked after the fact. A line holds the time (ISO UTC), the free slots, the
runnable queued candidates in the order dispatch ranked them (id, priority, owns_obligation; the first
TOP of them plus their total), counts of the queued tasks that could not start, by reason (REASONS),
and the ids started. An idle daemon adds nothing: a line is written only when something started or
the candidates, held counts or pick differ from the last line written (read back after a restart).
The file rotates to scheduler.1.jsonl past MAX_BYTES. A write or rotation failure is reported once
(until a write succeeds again) and never raises: dispatch goes on either way."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Callable

FILE = "scheduler.jsonl"
OLD = "scheduler.1.jsonl"
MAX_BYTES = 2 * 1024 * 1024
TOP = 20
# Why a queued task could not start: its start_when probe, its start_after or another start timer
# (not_before), its own retry_when wait (a waiting hand-off's timer), a busy resource or `ttp lock`
# (device slots too), a pause or hold (a paused resource, a branch or worktree another task uses, a
# setting hold, the disk guard, a review's prechecks), its provider's gate (logged out, API host down,
# budget gate), an unfinished depends_on, or anything else.
REASONS = ("start_when", "start_after", "retry_when", "lock", "hold", "provider_gate", "depends_on", "other")


def path(p) -> Path:
    return p.state / FILE


def _key(line: dict) -> list:
    return [line.get("runnable"), line.get("runnable_total"), line.get("held"), line.get("picked")]


def _last_key(f: Path) -> list | None:
    """The dedup key of the file's last line, or None (no file, or an unreadable last line)."""
    try:
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 65536, 0))
            tail = fh.read().splitlines()
        return _key(json.loads(tail[-1])) if tail else None
    except (OSError, ValueError, AttributeError):
        return None


def entry(free: int, runnable: list[dict], held: dict[str, int], picked: list[int],
          now: float | None = None) -> dict:
    """The line for one slot fill. runnable: {"id", "priority", "owns_obligation"} in rank order."""
    ts = dt.datetime.fromtimestamp(now, dt.timezone.utc) if now is not None else dt.datetime.now(dt.timezone.utc)
    return {"ts": ts.isoformat(timespec="seconds").replace("+00:00", "Z"), "free_slots": int(free),
            "runnable": [{"id": int(c["id"]), "priority": c["priority"], "owns_obligation": bool(c["owns_obligation"])}
                         for c in runnable[:TOP]],
            "runnable_total": len(runnable), "held": {r: int(held.get(r, 0)) for r in REASONS},
            "picked": [int(i) for i in picked]}


def record(f: Path, line: dict, cache: dict, warn: Callable[[str], None]) -> bool:
    """Append `line` to f unless it repeats the last line written with nothing started. cache (kept by
    the caller across passes) holds the last key and whether a failure was reported. True if written."""
    try:
        if "last" not in cache:
            cache["last"] = _last_key(f)
        key = _key(line)
        if not line["picked"] and key == cache["last"]:
            return False
        data = (json.dumps(line, separators=(",", ":")) + "\n").encode()
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(data) > MAX_BYTES:
            os.replace(f, f.with_name(OLD))
        with open(f, "ab") as fh:
            fh.write(data)
        cache["last"], cache["failed"] = key, False
        return True
    except Exception as e:   # the log is for audits only: dispatch never waits on or fails with it
        if not cache.get("failed"):
            cache["failed"] = True
            try:
                warn(f"scheduler log: write to {f} failed ({type(e).__name__}: {e}); dispatch goes on")
            except Exception:
                pass
        return False

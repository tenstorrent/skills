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


def slot_users(db, provider: str | None = None) -> dict:
    """What holds the worker slots (running runs other than the coordinator's, of `provider` or all):
    {"running": n, "by_kind": {task kind or run role: n}, "unreaped": runs whose task is no longer running
    (it handed off or was stopped and the run is not reaped yet), "untracked": runs with no task}."""
    rows = db.q("SELECT r.role, t.kind, t.status FROM runs r LEFT JOIN tasks t ON t.id=r.task "
                "WHERE r.role!='coordinator' AND r.status='running'" + (" AND r.provider=?" if provider else ""),
                (provider,) if provider else ())
    out = {"running": len(rows), "by_kind": {}, "unreaped": 0, "untracked": 0}
    for r in rows:
        kind = r["kind"] or r["role"] or "other"
        out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
        if r["status"] is None:
            out["untracked"] += 1
        elif r["status"] != "running":
            out["unreaped"] += 1
    return out


def slot_words(users: dict) -> str:
    """slot_users in words: '5 runs (3 code, 2 review; 1 of them for a task no longer running)'."""
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(users.get("by_kind", {}).items(), key=lambda kv: -kv[1]))
    extra = [f"{users[k]} of them {w}" for k, w in (("unreaped", "for a task no longer running, not reaped yet"),
                                                    ("untracked", "with no task")) if users.get(k)]
    n = users.get("running", 0)
    return f"{n} run{'s' if n != 1 else ''}" + (f" ({kinds}" + (f"; {'; '.join(extra)}" if extra else "") + ")"
                                                 if kinds else "")


def entry(free: int, runnable: list[dict], held: dict[str, int], picked: list[int],
          now: float | None = None, slots: dict | None = None) -> dict:
    """The line for one slot fill. runnable: {"id", "priority", "owns_obligation"} in rank order. slots:
    what holds the worker slots (slot_users), so a line with few free slots names their users."""
    ts = dt.datetime.fromtimestamp(now, dt.timezone.utc) if now is not None else dt.datetime.now(dt.timezone.utc)
    return {"ts": ts.isoformat(timespec="seconds").replace("+00:00", "Z"), "free_slots": int(free),
            "runnable": [{"id": int(c["id"]), "priority": c["priority"], "owns_obligation": bool(c["owns_obligation"])}
                         for c in runnable[:TOP]],
            "runnable_total": len(runnable), "held": {r: int(held.get(r, 0)) for r in REASONS},
            "picked": [int(i) for i in picked], **({"slots": slots} if slots is not None else {})}


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
        from .project import open_append
        with open_append(f, "ab") as fh:
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

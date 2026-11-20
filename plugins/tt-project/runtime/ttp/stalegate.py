"""Skip a wait_stale event a coordinator turn already met for the same wait and decided nothing on.

The coordinator's only role on a wait_stale event (daemon._stale_wait_event) is to fix the task's
probe, clear it or cancel the task. Each turn that handled one records here, per wait (the task and
its hand-off's waiting_since), whether it changed any of that: the probe (cleared or rewritten), the
task's status (cancelled, requeued), its dependencies, waiting.max_hold_s or the wait itself
('decided'); otherwise 'no-change', with the state it left. A later wait_stale for the same wait (a
reply wait re-armed when its peer left 'blocked', or an event still queued when that turn ended) is
skipped while the task's probe, max_hold_s, status and dependencies are all as that turn left them
and the probe's fresh answer is the same 'not yet' (exit 1 or 75, the same output with numbers
masked) as the one it saw. A skip never touches the task; it counts one line per wait for the daily
audit. Never skipped: a new wait (a new waiting_since), a turn recorded 'decided', a stale, broken
or changed answer (255, host unreachable, is not stable), or any change to the task or its wait."""
from __future__ import annotations

import hashlib
import json
import re
import time

TURNS_KEY = "wait_stale_turns"   # kv: {wait: {outcome, snap, ts, run}} the last wait_stale turn per wait
SKIPS_KEY = "wait_stale_skips"   # kv: {wait: {task, since, reason, n, fps, ts}} skipped raises, for the audit
KEEP_S = 8 * 86400
STABLE_RCS = (1, 75)
_NUM = re.compile(r"\d+(?:\.\d+)?")


def wait_key(tid: int, since) -> str:
    return f"{tid}:{float(since):.0f}"


def snap(task: dict, prev: dict, max_hold: float, rc, out: str) -> dict:
    """What a wait_stale decision depends on, as it stands now."""
    return {"probe": str(prev.get("retry_when") or "").strip(), "max_hold": float(max_hold),
            "status": task.get("status"), "deps": str(task.get("depends_on") or ""),
            "since": prev.get("waiting_since"), "rc": rc,
            "out": hashlib.sha256(" ".join(_NUM.sub("#", out or "").split()).encode()).hexdigest()[:16]}


def _fresh(d: dict, now: float) -> dict:
    return {k: v for k, v in d.items() if isinstance(v, dict) and now - float(v.get("ts", 0)) < KEEP_S}


def record(db, wait: str, seen: dict, now_snap: dict, run: int | None, now: float | None = None) -> str:
    """After a turn that handled a wait_stale event for `wait`: 'decided' when the task's probe, status,
    dependencies, max_hold_s or wait moved from `seen` (the event's snapshot), else 'no-change'."""
    now = now or time.time()
    same = all(now_snap.get(k) == seen.get(k) for k in ("probe", "max_hold", "status", "deps", "since"))
    outcome = "no-change" if same else "decided"
    turns = _fresh(db.kv(TURNS_KEY) or {}, now)
    turns[wait] = {"outcome": outcome, "snap": {**now_snap, "rc": seen.get("rc"), "out": seen.get("out")},
                   "ts": now, "run": run}
    db.set_kv(TURNS_KEY, turns)
    return outcome


def skip_reason(db, wait: str, cur: dict, fresh: bool) -> str | None:
    """Why the wait_stale for `wait` needs no turn now (`cur`: snap() now; `fresh`: the probe answered
    since the wait began and recently), or None when it must be raised."""
    if not fresh or cur.get("rc") not in STABLE_RCS:
        return None
    last = (db.kv(TURNS_KEY) or {}).get(wait)
    if not isinstance(last, dict) or last.get("outcome") != "no-change":
        return None
    if any((last.get("snap") or {}).get(k) != cur.get(k)
           for k in ("probe", "max_hold", "status", "deps", "since", "rc", "out")):
        return None
    return (f"probe unchanged and still not yet (exit {cur['rc']}); the last wait_stale turn "
            f"(run {last.get('run')}) decided nothing")


def note_skip(db, wait: str, fp: str, reason: str, now: float | None = None) -> int:
    """Count one skipped raise `fp` for `wait`: the wait's skip count, or 0 when `fp` was counted already."""
    now = now or time.time()
    skips = _fresh(db.kv(SKIPS_KEY) or {}, now)
    row = skips.get(wait)
    if row and fp in row.get("fps", []):
        return 0
    tid, _, since = wait.partition(":")
    row = row or {"task": int(tid), "since": float(since), "n": 0, "fps": []}
    row.update(fps=(row["fps"] + [fp])[-20:], n=int(row.get("n", 0)) + 1, reason=reason, ts=now)
    skips[wait] = row
    db.set_kv(SKIPS_KEY, skips)
    return row["n"]


def skipped(db, since: float) -> list[dict]:
    """The waits with a skipped wait_stale since `since`, oldest first."""
    rows = [r for r in (db.kv(SKIPS_KEY) or {}).values() if isinstance(r, dict) and float(r.get("ts", 0)) >= since]
    return sorted(rows, key=lambda r: float(r.get("ts", 0)))


def event_data(raw) -> dict:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) and isinstance(d.get("wait_stale"), dict) else {}

"""Skip a coordinator turn that would meet only what a turn that decided nothing already met.

A coordinator turn that decided nothing (only `noop`) records what it saw: each event's material
signature (kind, source, task, severity, its text with numbers masked, the task's status) and the
background it decided against (open tasks, asks, schedules, red budget gates, pauses, the charter
and memory, open high or critical alerts, the push queue). A later batch of events that such a
turn already saw, against the same background, would decide nothing again: the daemon settles its
events without a model run and logs the skip, with the dollars the matching turn cost as the
saving. Never skipped: user messages, a blocker sweep, any high or
critical event, any event of a kind that makes a turn tricky or blocking (coordinator.UNBLOCK_KINDS), and
any batch coordinator.effort_triggers finds tricky or blocking (repeated failures or waits, held
work). Records last `coordinator.skip_repeat_window_s` (a day); 0 turns the gate off."""
from __future__ import annotations

import hashlib
import json
import re
import time

SEEN_KEY = "turn_gate"     # kv: [{ts, run, usd, bg, keys}] no-op turns' material content
SKIPS_KEY = "turn_skips"   # kv: [{ts, events, usd, run}] turns skipped, for the daily review
KEEP_SKIPS_S = 8 * 86400
MAX_SEEN = 100
URGENT = ("high", "critical")
_NUM = re.compile(r"\d+(?:\.\d+)?")


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, default=str, sort_keys=True).encode()).hexdigest()[:16]


def window(cfg: dict) -> float:
    try:
        return max(float((cfg.get("coordinator") or {}).get("skip_repeat_window_s", 86400)), 0.0)
    except (TypeError, ValueError):
        return 86400.0


def event_keys(db, event_ids: list[int]) -> list[str]:
    """Each event's material signature: what it says, numbers masked (costs, counts, times), and
    the state of the task it names now."""
    if not event_ids:
        return []
    rows = db.q(f"SELECT e.kind, e.source, e.task, e.severity, e.text, t.status FROM events e "
                f"LEFT JOIN tasks t ON t.id=e.task WHERE e.id IN ({','.join('?' * len(event_ids))})",
                list(event_ids))
    return sorted({_h([r["kind"], r["source"], r["task"], r["severity"],
                       " ".join(_NUM.sub("#", r["text"] or "").split()), r["status"]]) for r in rows})


def background(db, wake_fp: str) -> str:
    """The state a turn decides against besides its batch: `wake_fp` (daemon.wake_fingerprint:
    open tasks, asks, schedules, red gates, pauses, charter and memory), open high or critical
    alerts, and the push queue."""
    alerts = [r["key"] for r in db.q("SELECT key FROM alerts WHERE cleared IS NULL "
                                     "AND severity IN ('high','critical') ORDER BY key")]
    try:
        q = db.one("SELECT COUNT(*) n, MAX(updated) u FROM push_queue")
        push = [q["n"], q["u"]]
    except Exception:   # no push queue table yet
        push = []
    return _h([wake_fp, alerts, push])


def material(db, wake_fp: str, event_ids: list[int]) -> dict:
    """What a turn starting now meets, as its run note keeps it (`gate`)."""
    return {"bg": background(db, wake_fp), "keys": event_keys(db, event_ids)}


def _seen(db, now: float, keep: float) -> list[dict]:
    return [s for s in (db.kv(SEEN_KEY) or []) if isinstance(s, dict) and now - float(s.get("ts", 0)) < keep]


def record(db, cfg: dict, note: dict, run: int | None, usd: float, now: float | None = None) -> bool:
    """A turn that settled its batch and decided nothing: remember what it met."""
    gate, now, keep = note.get("gate") or {}, now or time.time(), window(cfg)
    if not keep or not gate.get("keys") or note.get("messages"):
        return False
    seen = _seen(db, now, keep) + [{"ts": now, "run": run, "usd": round(float(usd or 0), 4),
                                    "bg": gate.get("bg"), "keys": gate["keys"]}]
    db.set_kv(SEEN_KEY, seen[-MAX_SEEN:])
    return True


def repeat(db, cfg: dict, evs: list[dict], msgs: list, gate: dict, now: float,
           sweep_kind: str, triggers: list | None = None,
           unblock_kinds=frozenset()) -> dict | None:
    """The no-op turns whose material content covers this batch, or None when it must run.
    `triggers`: the turn's triggers (coordinator.effort_triggers); a tricky or blocking turn always runs."""
    keep = window(cfg)
    if not keep or msgs or triggers or not evs or not gate.get("keys"):
        return None
    if any(e["kind"] == sweep_kind or e["kind"] in unblock_kinds or e["severity"] in URGENT for e in evs):
        return None
    same = [s for s in _seen(db, now, keep) if s.get("bg") == gate["bg"]]
    want, by = set(gate["keys"]), []
    for s in reversed(same):
        hit = want & set(s.get("keys") or [])
        if hit:
            by.append(s)
            want -= hit
    if want:
        return None
    return {"runs": [s.get("run") for s in by], "usd": round(max(float(s.get("usd") or 0) for s in by), 4)}


def skip(db, evs: list[dict], match: dict, now: float) -> None:
    """Settle the batch as its matching turn did, and log the skip."""
    ids = [e["id"] for e in evs]
    db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(ids))})", ids)
    skips = [s for s in (db.kv(SKIPS_KEY) or []) if now - float(s.get("ts", 0)) < KEEP_SKIPS_S]
    db.set_kv(SKIPS_KEY, skips + [{"ts": now, "events": ids, "usd": match["usd"], "runs": match["runs"]}])


def skipped(db, since: float) -> dict:
    """Turns skipped since `since` and the dollars their matching turns cost."""
    rows = [s for s in (db.kv(SKIPS_KEY) or []) if float(s.get("ts", 0)) >= since]
    return {"n": len(rows), "usd": sum(float(s.get("usd") or 0) for s in rows)}

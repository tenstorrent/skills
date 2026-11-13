"""Skip a routine coordinator turn while every open task is held and nothing new arrived (quiet: all held).
A project with no open task at all counts as all held: its idle wakes are settled the same way.

Every open task is held when each one is queued, waiting or blocked and held by something no turn can
lift: a user pause (userpause.probe_of: its only hold is a probe of a user marker file), a deferral (a
`start_after` still ahead or a `start_when`), or a resource the user, the project or a co-tenant paused.
A running task, or an open task held by anything else, keeps every turn. Nothing new arrived when the
state the last coordinator turn left behind is unchanged: the wake fingerprint (open tasks, asks,
schedules, red gates, pauses, charter, config and memory), each open task's exact status and labels,
the newest user message, the newest event of a kind that is not routine (record-only schedule skips
never count), the project pause and the
open machine ledger changes and outages.

Then a batch of only routine daemon events (`coordinator.quiet_held_kinds`, default idle_capacity), or
an idle or starve wake with no events, is settled without a model run: its events are handled, a wake
counts as a repeat (its backoff doubles and the next wake waits from the skip), and the skip is
logged as QUIET. Never skipped: user
messages, any high or critical event, any other kind, and any batch with a trigger
(coordinator.effort_triggers). The state a turn leaves is stored when it finishes; a turn starting clears the
old one, so one that never finishes leaves nothing to match. `coordinator.quiet_held_max_s` (a day)
lets one turn through once the last one started that long ago; 0 sets no such limit (the gate stays on,
and skipped wakes still back off to one a day). `coordinator.quiet_held` false turns the gate off."""
from __future__ import annotations

import hashlib
import json
import time

from . import userpause
from .coordinator import task_resources
from .db import TERMINAL_TASK_STATES as CLOSED, deferral

QUIET = "quiet: all held"
LEFT_KEY = "quiet_left"      # kv: {fp, ts, run} the state the last finished coordinator turn left, ts its start
SKIPS_KEY = "quiet_skips"    # kv: [{ts, events, due, usd}] turns skipped, for the daily review
KEEP_SKIPS_S = 8 * 86400
ROUTINE_KINDS = ("idle_capacity",)
RECORD_KINDS = ("schedule_skip",)   # record-only rows (schedule.SKIP_KIND) that never change the fingerprint
URGENT = ("high", "critical")


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, default=str, sort_keys=True).encode()).hexdigest()[:16]


def _c(cfg: dict) -> dict:
    return cfg.get("coordinator") or {}


def enabled(cfg: dict) -> bool:
    return bool(_c(cfg).get("quiet_held", True))


def routine_kinds(cfg: dict) -> set[str]:
    v = _c(cfg).get("quiet_held_kinds")
    return {str(k) for k in v} if isinstance(v, list) else set(ROUTINE_KINDS)


def max_age(cfg: dict) -> float:
    try:
        return max(float(_c(cfg).get("quiet_held_max_s", 86400)), 0.0)
    except (TypeError, ValueError):
        return 86400.0


def hold(task: dict, paused: dict, cfg: dict, now: float) -> str | None:
    """What holds an open task that no turn can lift ('user pause', 'deferred', 'paused resource'), or None."""
    if task.get("status") not in ("queued", "waiting", "blocked"):
        return None
    if userpause.probe_of(task, cfg):
        return "user pause"
    if task.get("status") == "queued":
        d = deferral(task)
        if d.get("when") or float(d.get("after") or 0) > now:
            return "deferred"
    if task_resources(task) & set(paused):
        return "paused resource"
    return None


def all_held(db, cfg: dict, now: float) -> bool:
    tasks = db.q(f"SELECT * FROM tasks WHERE status NOT IN ({','.join('?' * len(CLOSED))})", CLOSED)
    paused = db.paused_resources()
    return all(hold(t, paused, cfg, now) for t in tasks)   # no open task at all: nothing a turn could start either


def fingerprint(db, cfg: dict, wake_fp: str, pause=None, ledger=None) -> str:
    """The state a turn leaves behind, without what routine events and probe retries change.
    `pause`: the project pause (projpause.current); `ledger`: the open machine ledger changes and outages."""
    kinds = sorted(routine_kinds(cfg) | set(RECORD_KINDS))
    tasks = [(t["id"], t["status"], t["labels"]) for t in db.q(
        f"SELECT id, status, labels FROM tasks WHERE status NOT IN ({','.join('?' * len(CLOSED))}) ORDER BY id", CLOSED)]
    msg = db.one("SELECT MAX(id) n FROM messages WHERE direction='in'")
    ev = db.one(f"SELECT MAX(id) n FROM events WHERE kind NOT IN ({','.join('?' * len(kinds))})"
                if kinds else "SELECT MAX(id) n FROM events", kinds)
    return _h([wake_fp, tasks, msg and msg["n"], ev and ev["n"], pause, ledger])


def started(db) -> None:
    """A turn starts: the state the previous one left no longer stands for the last turn."""
    db.x("DELETE FROM kv WHERE key=?", (LEFT_KEY,))


def left(db, fp: str, run, start: float | None = None) -> None:
    """A turn finished: remember the state it left and when it started (the max age counts from there,
    as the idle wake does)."""
    db.set_kv(LEFT_KEY, {"fp": fp, "ts": start or time.time(), "run": run})


def quiet(db, cfg: dict, evs: list[dict], msgs: list, triggers, due: str | None,
          fp: str, now: float, unblock_kinds=frozenset(), sweep_kind: str = "") -> bool:
    """Whether this routine turn would meet only what the last one left, with every task held.
    `triggers`: the turn's triggers, or a callable returning them, called only once all else holds."""
    if not enabled(cfg) or msgs or (triggers and not callable(triggers)):
        return False
    if evs:
        kinds = routine_kinds(cfg)
        if any(e["kind"] not in kinds or e["kind"] in unblock_kinds or e["kind"] == sweep_kind
               or e["severity"] in URGENT or e.get("source") != "daemon" for e in evs):
            return False
    elif due not in ("idle", "starve"):
        return False
    prev = db.kv(LEFT_KEY) or {}
    if not isinstance(prev, dict) or prev.get("fp") != fp:
        return False
    age = max_age(cfg)
    if age and now - float(prev.get("ts") or 0) >= age:
        return False
    if not all_held(db, cfg, now):
        return False
    return not (triggers() if callable(triggers) else triggers)


def _turn_usd(db) -> float:
    """What a routine turn costs: the mean of the last ten coordinator turns."""
    rows = db.q("SELECT cost_usd FROM runs WHERE role='coordinator' AND status!='running' "
                "AND cost_usd IS NOT NULL ORDER BY id DESC LIMIT 10")
    return round(sum(float(r["cost_usd"] or 0) for r in rows) / len(rows), 4) if rows else 0.0


def skip(db, evs: list[dict], due: str | None, now: float) -> float:
    """Settle the batch without a model run and record the skip; returns the estimated saving."""
    ids = [e["id"] for e in evs]
    if ids:
        db.x(f"UPDATE events SET status='handled' WHERE id IN ({','.join('?' * len(ids))})", ids)
    usd = _turn_usd(db)
    skips = [s for s in (db.kv(SKIPS_KEY) or []) if now - float(s.get("ts", 0)) < KEEP_SKIPS_S]
    db.set_kv(SKIPS_KEY, skips + [{"ts": now, "events": ids, "due": due, "usd": usd}])
    return usd


def skipped(db, since: float, until: float | None = None) -> dict:
    """Turns skipped as quiet in [since, until) and the estimated dollars saved."""
    rows = [s for s in (db.kv(SKIPS_KEY) or []) if float(s.get("ts", 0)) >= since
            and (until is None or float(s.get("ts", 0)) < until)]
    return {"n": len(rows), "usd": round(sum(float(s.get("usd") or 0) for s in rows), 4)}

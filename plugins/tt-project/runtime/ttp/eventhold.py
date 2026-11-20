"""Routine event-only coordinator turns wait out a short hold after the last turn, so one turn reads several.

When every queued event (ride-alongs aside) is a routine kind of HOLD_KINDS at a routine severity and
the last coordinator turn ended less than coordinator.event_hold_s ago (default 900; 0: off), the
daemon starts no turn until the hold ends; events that arrive meanwhile share that one turn. The hold
runs from the end of the last turn (runs.ended), so a daemon restart keeps it, and it never runs past
event_hold_s after that end.

Never held: a user message (an answered ask arrives as one), any event of another kind (a stall
check or blocker sweep, a budget or gate change, obligation_overdue, pause_end_due, charter_retry,
ask_timeout, machine and resource events, ...), any high or critical event, and a moment a worker
slot is free while no ready task is queued to fill it, so capacity never idles waiting on the hold.
The model-free start of ready work runs in dispatch, apart from the coordinator, and is not held.

The wake gap (gap_until) is the coalescing floor under that hold: an event-only batch with no
immediate event (task ends and hand-offs, timers, sweeps, notes, waits) waits until
coordinator.wake_gap_s (default 900; 0: off) after the last turn ended even when a worker slot is
free, so a stream of such events costs one wake per gap, not one per free slot. Dispatch starts ready
work without a model run meanwhile. A user message or answered ask, a high or critical event and the
kinds of IMMEDIATE_KINDS and IMMEDIATE_PREFIXES (must-act, overdue obligations, safety and outage
events) wake at once, and so does the end or hand-off of a hot task (hot(): a fresh `urgent:` line,
the template merge or a required owner of an open obligation). A held event stays queued on disk and
rides the next turn."""
from __future__ import annotations

KINDS = frozenset({"task_done", "followup_proposed", "upstream_note", "checks_slot_wait", "task_waiting"})
SEVERITIES = frozenset({"info", "low", "normal"})
DEFAULT_S = 900.0
# Kinds that never wait for the wake gap or the watcher, at any severity: must-act items (mustact.KINDS),
# answered or timed-out asks, overdue obligations, safety (charter, restriction, outside writes, spend)
# and outage events (machines, resources, providers, hosts, pauses, the daemon's boot).
IMMEDIATE_KINDS = frozenset({"turn_budget", "detached_job_spend_cap", "boot", "coord_session_start_failed",
                             "user_order_fanout", "relayer_ordered", "outside_writes", "outscan_refused"})
IMMEDIATE_PREFIXES = ("ask_", "charter_", "obligation_", "pause_", "machine_", "resource_", "provider_", "host_",
                      "outage", "gate_", "credential", "secret", "auth", "login", "restriction", "safety")


def hold_s(cfg: dict) -> float:
    try:
        return max(float((cfg.get("coordinator") or {}).get("event_hold_s", DEFAULT_S)), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_S


def routine(evs: list[dict]) -> bool:
    """Whether every event in `evs` (at least one) is a routine kind at a routine severity."""
    return bool(evs) and all(e.get("kind") in KINDS and e.get("severity") in SEVERITIES for e in evs)


def until(cfg: dict, msgs: list, evs: list[dict], last_end: float | None, now: float) -> float | None:
    """When the hold over this batch ends, or None when the turn may start now. The caller checks the
    free-slot rule (a free slot with nothing ready to fill it never waits)."""
    hold = hold_s(cfg)
    if hold <= 0 or msgs or not routine(evs) or not last_end:
        return None
    end = float(last_end) + hold
    return end if now < end else None


def immediate(e: dict) -> bool:
    """Whether event `e` wakes the coordinator at once: a high or critical one or a kind above."""
    from . import mustact
    kind = str(e.get("kind") or "")
    return e.get("severity") not in SEVERITIES or kind in mustact.KINDS or kind in IMMEDIATE_KINDS or \
        kind.startswith(IMMEDIATE_PREFIXES)


def can_wait(evs: list[dict]) -> bool:
    """Whether every event in `evs` (at least one) may wait for the next wake (none is immediate)."""
    return bool(evs) and not any(immediate(e) for e in evs)


def gap_until(cfg: dict, evs: list[dict], last_end: float | None, now: float) -> float | None:
    """When the wake gap over this event-only batch ends, or None when the turn may start now."""
    try:
        gap = max(float((cfg.get("coordinator") or {}).get("wake_gap_s", DEFAULT_S)), 0.0)
    except (TypeError, ValueError):
        gap = DEFAULT_S
    if gap <= 0 or not last_end or not can_wait(evs):
        return None
    end = float(last_end) + gap
    return end if now < end else None


def hot(db, evs: list[dict], now: float) -> bool:
    """Whether an event in `evs` is about a task a deadline hangs on: urgent.extra_ok (a fresh `urgent:`
    line or the template merge, not priority alone, so routine priority-1 work still batches) or a
    required owner of an open obligation (obligations.owner_due). Such events skip every wake hold."""
    from . import obligations, urgent
    tids = {int(e["task"]) for e in evs if e.get("task")}
    if not tids:
        return False
    try:
        owners = obligations.owner_due(db)
        return any(t in owners or urgent.extra_ok(db.task(t), now) for t in tids)
    except Exception:   # unreadable records: the holds decide as before
        return False

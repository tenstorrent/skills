"""Routine event-only coordinator turns wait out a short hold after the last turn, so one turn reads several.

When every queued event (ride-alongs aside) is a routine kind of HOLD_KINDS at a routine severity and
the last coordinator turn ended less than coordinator.event_hold_s ago (default 300; 0: off), the
daemon starts no turn until the hold ends; events that arrive meanwhile share that one turn. The hold
runs from the end of the last turn (runs.ended), so a daemon restart keeps it, and it never runs past
event_hold_s after that end.

Never held: a user message (an answered ask arrives as one), any event of another kind (a stall
check or blocker sweep, a budget or gate change, obligation_overdue, pause_end_due, charter_retry,
ask_timeout, machine and resource events, ...), any high or critical event, and a moment a worker
slot is free while no ready task is queued to fill it, so capacity never idles waiting on the hold.
The model-free start of ready work runs in dispatch, apart from the coordinator, and is not held."""
from __future__ import annotations

KINDS = frozenset({"task_done", "followup_proposed", "upstream_note", "checks_slot_wait", "task_waiting"})
SEVERITIES = frozenset({"info", "low", "normal"})
DEFAULT_S = 300.0


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

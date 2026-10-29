"""High events that arrive close together share one coordinator turn (leading-edge burst merge).

The first high event after a quiet spell wakes the coordinator at once. High events that arrive
within coordinator.burst_window_s (default 120; 0: off) after that turn ended start no turn of
their own: they stay queued in the database, so a daemon restart inside the window keeps them, and
the one follow-up turn when the window ends reads them all in its NEW EVENTS. The window runs from
the end of the turn, since events that arrive while it runs wait for it anyway. The follow-up,
carrying high events too, opens the next window: a burst costs at most two turns, a steady stream
one turn per turn length plus window.

Never held: a user message (an answered ask arrives as one), a critical event, and an event of a
safety class (spend cap, restriction at risk, data loss, a shared device or machine hold: a word of
SAFETY_WORDS in its kind, or SAFETY_TEXT in its text). Any of those in the queue starts the turn at
once, with the held events in it. Events of other severities are not held here (routine ones may
wait for batch_s); a queue with anything but high events and routine ones runs as before."""
from __future__ import annotations

import re

KEY = "burst_hold"   # kv: {"run": the turn high events woke, "start": its start, "held": [event ids held]}
DEFAULT_WINDOW_S = 120.0
# Kind words (a kind split at "_") of the safety classes: spend caps, restrictions at risk, data
# loss, shared device or machine holds and pauses, asks.
SAFETY_WORDS = frozenset({"spend", "budget", "billing", "credit", "credits", "restriction", "restrictions",
                          "charter", "grant", "integrity", "loss", "lost", "uncommitted", "worktree",
                          "hold", "holds", "pause", "paused", "device", "devq", "broker", "machine",
                          "outage", "down", "lock", "ask"})
SAFETY_TEXT = re.compile(r"\bspend(ing)? cap|\bover (its|the) (budget|cap)\b|\brestriction|\bdata loss\b|"
                         r"\blost (work|data|messages?|commits?|changes)\b|"
                         r"\b(device|machine|broker|box|chip|host) (hold|pause)|\bhold on (a|the) (device|machine)",
                         re.I)


def window(cfg: dict) -> float:
    try:
        return max(float((cfg.get("coordinator") or {}).get("burst_window_s", DEFAULT_WINDOW_S)), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_S


def safety(kind: str | None, text: str | None) -> bool:
    return bool(SAFETY_WORDS & set((kind or "").lower().split("_")) or SAFETY_TEXT.search(text or ""))


def until(db, cfg: dict, now: float) -> float | None:
    """When the open window ends, or None when none is open."""
    w, st = window(cfg), db.kv(KEY) or {}
    if not w or not isinstance(st, dict) or not st.get("start"):
        return None
    end = float(st["start"])
    if now < end:   # the clock went back: no window is held on it
        return None
    if st.get("run"):
        r = db.one("SELECT ended FROM runs WHERE id=?", (st["run"],))
        if r and r["ended"]:
            end = max(end, float(r["ended"]))
    end += w
    return end if now < end else None


def hold(db, cfg: dict, msgs: list, queued: list[dict], now: float, routine) -> float | None:
    """The window's end while this queue waits for the follow-up turn, else None (it runs now).
    `routine(kind, severity)`: events that may wait for company anyway (coordinator.batchable)."""
    if msgs or not queued or not any(e["severity"] == "high" for e in queued):
        return None
    if not all(e["severity"] == "high" or routine(e["kind"], e["severity"]) for e in queued):
        return None
    end = until(db, cfg, now)
    if end is None:
        return None
    high = [e["id"] for e in queued if e["severity"] == "high"]
    rows = db.q(f"SELECT kind, text FROM events WHERE id IN ({','.join('?' * len(high))})", high)
    if any(safety(r["kind"], r["text"]) for r in rows):
        return None
    return end


def held(db, queued: list[dict]) -> list[int]:
    """Record the high events held now; returns the ones not held before (to log them once)."""
    st = db.kv(KEY) or {}
    before = set(st.get("held") or [])
    new = [e["id"] for e in queued if e["severity"] == "high" and e["id"] not in before]
    if new:
        db.set_kv(KEY, {**st, "held": sorted(before | set(new))})
    return new


def carried(db, evs: list[dict]) -> dict:
    """What a turn starting on `evs` records in its note (`burst`): the events a window held for it."""
    ids = {e["id"] for e in evs}
    was = [i for i in ((db.kv(KEY) or {}).get("held") or []) if i in ids]
    return {"held": was} if was else {}


def started(db, evs: list[dict], run: int | None, now: float) -> None:
    """A turn started on `evs`: one carrying a high event opens the next window."""
    st = db.kv(KEY) or {}
    ids = {e["id"] for e in evs}
    left = [i for i in (st.get("held") or []) if i not in ids]
    if any(e["severity"] == "high" for e in evs):
        db.set_kv(KEY, {"run": run, "start": now, "held": left})
    elif left != (st.get("held") or []):
        db.set_kv(KEY, {**st, "held": left})

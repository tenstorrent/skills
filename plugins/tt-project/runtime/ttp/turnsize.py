"""Coordinator turns that go over their per-turn budget (coordinator.turn_budget_usd).

A large backlog (a daemon resumed after a long pause) can make a turn over the whole batch cost
more than the cap, and retrying that batch at the same cap fails the same way. The daemon keeps one
record per pending item that went over, keyed by the item (`m:<id>`, `e:<id>`), and nothing shared:
{attempts, cap (the cap at its last failure), base, ceiling (both then), size (its batch then)}.

- A failed batch of n > 1 items is split: each of its items joins only batches of at most n // 2,
  at the configured cap, down to one item alone.
- An item that fails alone at the base cap gets exactly one retry alone, raised to the ceiling
  (coordinator.turn_budget_ceiling_usd, 0: twice the cap; never above a dollar cap), when the caps leave room for it now; meanwhile the other items go on.
- An item that fails alone with no higher cap allowed is parked: it stays pending, and one normal
  alert names it with every other parked item. It runs again, alone, only once the base cap or the
  ceiling rose above the cap it went over.
- A record goes when its item is handled; a turn for other items never touches it.

Every pending item is either eligible (it joins a batch, or runs alone at its raise) or parked and
named in the alert (`status`, `parked`). Every step is an event (kind turn_budget) and a log line.

Accepted gaps: a split is by count, not by size, so one heavy item in a batch costs a few halvings
before it runs alone. A raise waiting for room under the hourly limit waits as long as the hourly
spend keeps it out (it is still eligible). The parked set the alert names is the last one the
daemon worked out: a setting raised while no turn can start clears it on the next turn's plan.
"""
from __future__ import annotations

import json
import time

from .db import DB

STATE_KEY = "turn_budget_items"    # {item key: record}
PARKED_KEY = "turn_budget_parked"  # the item keys the alert names (derived from the records)
ALERT_KEY = "coord-budget"
EVENT_KIND = "turn_budget"
OVER = "error_max_budget_usd"


def over_budget(status: str, usage) -> bool:
    """The run ended on its dollar budget: the agent's own cap, or the runner's mid-run check."""
    if status == "budget":
        return True
    extra = getattr(usage, "extra", None) or {}
    return extra.get("subtype") == OVER or str(getattr(usage, "error", "") or "").startswith(OVER)


def configured(cfg: dict) -> float:
    return round(float((cfg.get("coordinator") or {}).get("turn_budget_usd", 1.0) or 1.0), 2)


def ceiling(cfg: dict) -> float:
    """The most one raise may set: the configured ceiling (0: twice the cap), never above the
    project's daily or global daily cap."""
    c, b = cfg.get("coordinator") or {}, cfg.get("budget") or {}
    top = float(c.get("turn_budget_ceiling_usd") or 0) or 2 * configured(cfg)
    for cap in (b.get("daily_usd"), b.get("global_daily_usd")):
        if cap and float(cap) > 0:
            top = min(top, float(cap))
    return round(top, 2)


def headroom(cfg: dict, numbers: dict) -> float:
    """What the dollar caps leave now for one turn: each cap less its spend (no cap: no limit)."""
    dollars = dollar_room(numbers)
    return float("inf") if dollars is None else dollars


def dollar_room(numbers: dict) -> float | None:
    """What the project and account dollar caps leave now (None: no cap set). A coordinator session
    wake's cap is bounded by this alone (seedcap.py)."""
    room = []
    if numbers.get("daily_cap"):
        room.append(float(numbers["daily_cap"]) - float(numbers.get("spent_today", numbers.get("spent_24h")) or 0))
    if numbers.get("weekly_cap"):
        room.append(float(numbers["weekly_cap"]) - float(numbers.get("spent_7d") or 0))
    if numbers.get("global_cap"):
        room.append(float(numbers["global_cap"]) - float(numbers.get("global_today") or 0))
    return min(room) if room else None


def item_key(kind: str, i) -> str:
    return f"{kind}:{int(i)}"


def label(k: str) -> str:
    kind, _, i = k.partition(":")
    return f"{'user message' if kind == 'm' else 'event'} #{i}"


def _pending_keys(db: DB, keys) -> set:
    ms = [int(k[2:]) for k in keys if k.startswith("m:")]
    es = [int(k[2:]) for k in keys if k.startswith("e:")]
    got = set()
    if ms:
        got |= {item_key("m", r["id"]) for r in db.q(
            f"SELECT id FROM messages WHERE direction='in' AND handled=0 AND id IN ({','.join('?' * len(ms))})", ms)}
    if es:
        got |= {item_key("e", r["id"]) for r in db.q(
            f"SELECT id FROM events WHERE status='queued' AND id IN ({','.join('?' * len(es))})", es)}
    return got


def records(db: DB) -> dict:
    """The records of items still pending; those of handled items are dropped."""
    recs = db.kv(STATE_KEY) or {}
    live = _pending_keys(db, recs)
    if len(live) != len(recs):
        recs = {k: v for k, v in recs.items() if k in live}
        db.set_kv(STATE_KEY, recs) if recs else db.x("DELETE FROM kv WHERE key=?", (STATE_KEY,))
    return recs


def status(cfg: dict, rec: dict | None) -> tuple[str, float]:
    """("batch", the most items a batch with it may hold), ("solo", the cap it runs at alone) or
    ("parked", the cap it went over)."""
    if not rec:
        return "batch", float("inf")
    if int(rec.get("size") or 1) > 1:
        return "batch", float(int(rec["size"]) // 2)
    cap, base, top = float(rec.get("cap") or 0), configured(cfg), ceiling(cfg)
    if base > cap:
        return "solo", base
    if top > cap:
        return "solo", top
    return "parked", cap


def parked(db: DB, cfg: dict) -> list[str]:
    return [k for k, rec in records(db).items() if status(cfg, rec)[0] == "parked"]


def holds(db: DB) -> bool:
    """The alert's condition: an item the alert named is still parked and pending."""
    return bool(_pending_keys(db, db.kv(PARKED_KEY) or []))


def _event(db: DB, text: str, severity: str = "normal", data: dict | None = None) -> None:
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
         (time.time(), "daemon", EVENT_KIND, severity, text[:1500], json.dumps(data or {}), "handled"))


def plan(db: DB, cfg: dict, msgs: list[dict], evs: list[dict], numbers: dict,
         max_msgs: int = 30, max_evs: int = 40) -> dict:
    """This turn's batch and cap, from every pending item (user messages first, then events, oldest
    first). Returns {"msgs", "evs", "budget", "info"}: an item due alone at a raise the caps have room
    for runs first, by itself; else a batch of items without a solo record, as large as their records
    allow. Parked items, and raises waiting for room, are left out."""
    recs = records(db)
    base = configured(cfg)
    items = [("m", x) for x in msgs] + [("e", x) for x in evs]
    batch: list[tuple[str, dict]] = []
    lim, info, room = float("inf"), {}, None
    for kind, x in items:
        st, v = status(cfg, recs.get(item_key(kind, x["id"])))
        if st != "solo":
            continue
        if v > base:
            room = headroom(cfg, numbers) if room is None else room
            if room < v:
                info.setdefault("waiting", []).append(item_key(kind, x["id"]))
                continue
        one = {"msgs": [x] if kind == "m" else [], "evs": [x] if kind == "e" else []}
        return {**one, "budget": round(v, 2), "info": {**info, "solo": item_key(kind, x["id"]), "cap": round(v, 2)}}
    counts = {"m": 0, "e": 0}
    for kind, x in items:
        st, v = status(cfg, recs.get(item_key(kind, x["id"])))
        if st != "batch":
            continue
        if counts[kind] >= (max_msgs if kind == "m" else max_evs):
            continue
        if len(batch) + 1 > min(lim, v):
            break
        batch.append((kind, x))
        counts[kind] += 1
        lim = min(lim, v)
    if lim != float("inf"):
        info["split"] = int(lim)
    return {"msgs": [x for k, x in batch if k == "m"], "evs": [x for k, x in batch if k == "e"],
            "budget": base, "info": info}


def on_fail(db: DB, cfg: dict, project: str, note: dict, log=None) -> dict | None:
    """After a turn went over its cap: record it on each of its items. Returns {"text"} for the event,
    or None for a turn with no batch (not a backlog problem: the usual failure backoff)."""
    keys = [item_key("m", i) for i in note.get("messages") or []] + [item_key("e", i) for i in note.get("events") or []]
    if not keys:
        return None
    base, top = configured(cfg), ceiling(cfg)
    cap = round(float(note.get("budget_usd") or base), 2)
    recs = records(db)
    for k in keys:
        rec = recs.get(k) or {}
        recs[k] = {"attempts": int(rec.get("attempts") or 0) + 1, "cap": cap, "base": base, "ceiling": top,
                   "size": len(keys), "ts": time.time()}
    db.set_kv(STATE_KEY, recs)
    n = len(keys)
    if n > 1:
        did = f"split it: its items go in batches of at most {n // 2}"
    else:
        st, v = status(cfg, recs[keys[0]])
        did = (f"runs it alone once more at ${v:.2f}" if st == "solo" else
               f"parked it (cap ${base:.2f}, ceiling ${top:.2f}); it stays pending")
    what = label(keys[0]) if n == 1 else f"{n} items"
    text = f"Coordinator turn of {project} went over its ${cap:.2f} cap on {what}; the daemon {did}."
    _event(db, text, "normal", {"items": keys, "cap": cap, "base": base, "ceiling": top})
    if log:
        log(text)
    return {"text": text}


def alert_text(db: DB, cfg: dict, project: str) -> str | None:
    """The alert to send when an item was parked since the last one (it names every parked item),
    else None. Keeps the named set current, so the alert clears once none of them is parked."""
    now = sorted(parked(db, cfg), key=lambda k: (k[0] != "m", int(k[2:])))
    before = set(db.kv(PARKED_KEY) or [])
    if now != sorted(before, key=lambda k: (k[0] != "m", int(k[2:]))):
        db.set_kv(PARKED_KEY, now) if now else db.x("DELETE FROM kv WHERE key=?", (PARKED_KEY,))
    if not set(now) - before:
        return None
    recs = records(db)
    names = ", ".join(f"{label(k)} (over ${float(recs[k]['cap']):.2f})" for k in now)
    return (f"{len(now)} coordinator item{'s are' if len(now) != 1 else ' is'} parked in {project}: "
            f"{names}. Each went over the most a turn may spend on it here (cap ${configured(cfg):.2f}, "
            f"ceiling ${ceiling(cfg):.2f}); the other items go on. Raise coordinator.turn_budget_usd or "
            f"coordinator.turn_budget_ceiling_usd (it never goes above "
            f"the dollar caps) above it to run it.")

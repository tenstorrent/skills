"""Replay a project's coordinator wakes under coordinator.batch_s.

Reads `state/project.db` read-only and re-runs the daemon's wake rule over the events and messages
the coordinator turns of the last few days consumed, once without batching and once with it. Worker
runs give the busy slots over time; queued work counts as runnable at a moment when a task created
before it started a run soon after (the dispatcher starts runnable work within a tick). A run whose
task a held turn would only queue later does not count until the replay has read that turn's events.

    python3 -m ttp.replay <project folder or project.db> [--days 2] [--batch-s 300] [--slots N]

Reports the turns each rule starts, the share batching saves, `idle_slot_s` (free slots with nothing
to run while a batch is held) and `extra_idle_slot_s` (real run seconds the batched replay could not
run yet, beyond the unbatched one). `--rule always` replays a plain time window for comparison.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

from .coordinator import batchable

STEP_S = 5.0


def load(db_path: Path, days: float, now: float | None = None) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = time.time() if now is None else now
    since = now - days * 86400
    turns = [dict(r) for r in con.execute(
        "SELECT id, started, ended, status, cost_usd, note FROM runs WHERE role='coordinator' AND started>? "
        "ORDER BY started", (since,))]
    ev_ids, msg_ids, handled_by = set(), set(), {}
    for t in turns:
        note = json.loads(t["note"] or "{}")
        t["events"], t["messages"] = note.get("events") or [], note.get("messages") or []
        ev_ids.update(t["events"])
        msg_ids.update(t["messages"])
        if t["status"] == "ok":
            for e in t["events"]:
                handled_by[e] = t
    events = [dict(r) for r in con.execute(
        f"SELECT id, ts, kind, severity FROM events WHERE id IN ({','.join('?' * len(ev_ids))})", sorted(ev_ids))] \
        if ev_ids else []
    msgs = [dict(r) for r in con.execute(
        f"SELECT id, ts FROM messages WHERE id IN ({','.join('?' * len(msg_ids))})", sorted(msg_ids))] \
        if msg_ids else []
    workers = [dict(r) for r in con.execute(
        "SELECT r.task, r.started, COALESCE(r.ended, ?) ended, t.created FROM runs r LEFT JOIN tasks t "
        "ON t.id=r.task WHERE r.role!='coordinator' AND COALESCE(r.ended, ?)>? ORDER BY r.started",
        (now, now, since - 86400))]
    tasks = [dict(r) for r in con.execute(
        "SELECT t.id, t.created, MIN(r.started) first FROM tasks t LEFT JOIN runs r ON r.task=t.id "
        "AND r.role!='coordinator' WHERE t.created>? GROUP BY t.id", (since,))]
    con.close()
    return {"turns": turns, "events": events, "messages": msgs, "workers": workers, "tasks": tasks,
            "handled_by": handled_by, "now": now}


def _creators(data: dict) -> dict:
    """Task id -> the real event-driven turn that queued it, so its runs wait for that turn."""
    turns = [t for t in data["turns"] if t["status"] == "ok" and t["events"] and t["ended"]]
    out = {}
    for x in data["tasks"]:
        for t in turns:
            if x["created"] is not None and t["started"] <= x["created"] <= t["ended"] + 5:
                out[x["id"]] = t
                break
    return out


def simulate(data: dict, batch_s: float, slots: int, debounce_s: float = 15.0, window_s: float = 60.0,
             max_events: int = 40, max_per_hour: int = 30, turn_s: float | None = None,
             rule: str = "fill") -> dict:
    """Start turns like maybe_coordinate does. A real worker run counts only once the replayed turns
    have read the events of the real turn that queued its task; the run seconds lost before that are
    `lost_run_s`, the work a later turn would have kept off a free slot."""
    workers = sorted(data["workers"], key=lambda w: w["started"])
    creators = _creators(data)
    arrivals = sorted([(e["ts"], "e", e) for e in data["events"]] + [(m["ts"], "m", m) for m in data["messages"]],
                      key=lambda a: a[0])
    ok = [t["ended"] - t["started"] for t in data["turns"] if t["status"] == "ok" and t["ended"]]
    turn_s = turn_s if turn_s is not None else (statistics.median(ok) if ok else 60.0)
    turns, pending_e, pending_m = [], [], []
    handled_at: dict[int, float] = {}
    idle_slot_s = held_s = lost_run_s = 0.0
    if not arrivals:
        return {"turns": [], "handled_at": {}, "idle_slot_s": 0.0, "held_s": 0.0, "lost_run_s": 0.0}

    def exists(task, t: float) -> bool:
        r = creators.get(task)
        return r is None or all(handled_at.get(e, t + 1) + turn_s <= t for e in r["events"])

    i, j, live = 0, 0, []
    t, end, busy_until = arrivals[0][0], arrivals[-1][0] + batch_s + 4 * debounce_s + turn_s, 0.0
    while t <= end or pending_e or pending_m:
        while i < len(arrivals) and arrivals[i][0] <= t:
            (pending_e if arrivals[i][1] == "e" else pending_m).append(arrivals[i][2])
            i += 1
        while j < len(workers) and workers[j]["started"] <= t + window_s:
            live.append(workers[j])
            j += 1
        live = [w for w in live if w["ended"] > t]
        running = [w for w in live if w["started"] <= t]
        busy = sum(1 for w in running if exists(w["task"], t))
        lost_run_s += (len(running) - busy) * STEP_S
        # Queued before `t`, started within window_s after it: the dispatcher was about to start it.
        runnable = len({w["task"] for w in live if w["started"] > t and w["created"] is not None
                        and w["created"] < t and exists(w["task"], t)})
        if (pending_e or pending_m) and t >= busy_until:
            ts = [x["ts"] for x in pending_e + pending_m]
            if not (t - max(ts) < debounce_s and t - min(ts) < 4 * debounce_s):
                hold = False
                if batch_s > 0 and not pending_m and t - min(ts) < batch_s and all(
                        batchable(e["kind"], e["severity"]) for e in pending_e):
                    hold = {"fill": busy + runnable >= slots, "slots": busy >= slots or runnable > 0,
                            "always": True}[rule]
                    if hold:
                        held_s += STEP_S
                        idle_slot_s += max(0, slots - busy - runnable) * STEP_S
                recent = sum(1 for x in turns if x > t - 3600)
                if not hold and (recent < max_per_hour or pending_m):
                    turns.append(t)
                    for e in pending_e[:max_events]:
                        handled_at[e["id"]] = t
                    pending_e, pending_m = pending_e[max_events:], []
                    busy_until = t + turn_s
        t += STEP_S
    return {"turns": turns, "handled_at": handled_at, "idle_slot_s": idle_slot_s, "held_s": held_s,
            "lost_run_s": lost_run_s}


def report(data: dict, batch_s: float, slots: int, **kw) -> dict:
    base = simulate(data, 0, slots, **kw)
    batched = simulate(data, batch_s, slots, **kw)
    ok = [t for t in data["turns"] if t["status"] == "ok"]
    triggered = [t for t in ok if t["events"] or t["messages"]]
    cost = statistics.mean([t["cost_usd"] or 0 for t in triggered]) if triggered else 0.0
    nb, nn = len(base["turns"]), len(batched["turns"])
    return {"batch_s": batch_s, "slots": slots, "real_turns": len(ok), "real_event_turns": len(triggered),
            "replayed_turns": nb, "batched_turns": nn, "turns_saved": nb - nn,
            "saved_pct": round(100.0 * (nb - nn) / nb, 1) if nb else 0.0,
            "est_saved_usd": round((nb - nn) * cost, 2), "held_s": batched["held_s"],
            "idle_slot_s": batched["idle_slot_s"],
            "extra_idle_slot_s": batched["lost_run_s"] - base["lost_run_s"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ttp.replay", description=__doc__.split("\n")[0])
    ap.add_argument("path", help="project folder (with state/project.db) or the db file")
    ap.add_argument("--days", type=float, default=2.0)
    ap.add_argument("--batch-s", type=float, default=300.0)
    ap.add_argument("--slots", type=int, default=None, help="worker slots (default: the project's "
                    "budget.max_parallel_workers, else 6)")
    ap.add_argument("--debounce-s", type=float, default=15.0)
    ap.add_argument("--window-s", type=float, default=60.0)
    ap.add_argument("--rule", choices=("fill", "slots", "always"), default="fill",
                    help="fill: the daemon's rule; slots: hold while any work is runnable; always: a plain window")
    a = ap.parse_args(argv)
    path = Path(a.path)
    db = path if path.is_file() else path / "state" / "project.db"
    slots = a.slots
    if slots is None:
        cfg = db.parent.parent / "harness" / "project.json"
        try:
            slots = int(json.loads(cfg.read_text()).get("budget", {}).get("max_parallel_workers", 6))
        except (OSError, ValueError):
            slots = 6
    out = report(load(db, a.days), a.batch_s, slots, debounce_s=a.debounce_s, window_s=a.window_s,
                 rule=a.rule)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Holds that loop: work that looks held on something but that nothing will ever move.

Model-free, run by the daemon on every tick (Daemon._raise_hold_loops). Four conditions, each kept
under a key in LOOPS_KEY from when it was first seen; it raises one event while it lasts and the key
is dropped once it is resolved, so it raises again only if it comes back:

- `time_only_hold` (key time-only:<task>): a task held only until a time (a blocked hold on
  `until:`, or a queued `start_after` with no `start_when`) with nothing open that produces what it
  waits for (no open dependency, ask, resource or probe). A blocked `until:` hold is raised once its
  review passed, its time was re-armed, or it stayed time-only held past coordinator.hold_max_h. A
  queued start_after is a planned deferral (task_add/task_update chose that time so the wait costs
  no turns): it is raised only once its time was re-armed, or OVERDUE_S after its start_after
  passed while it is still queued. Re-arming the time keeps the key: the flag lives until the task
  closes or gets a real end (TIME_KEY).
- `blocked_dependency` (blocked-dependency:<task>:<dep>): a queued or blocked task depends on a
  blocked task whose hold has no end of its own (no ask, resource or probe anchor). Raised once a
  coordinator turn has passed since it was first seen, as dead_dependency is.
- `dead_dependency` (dead-dependency:<task>:<dep>): a blocked task depends on a failed, cancelled
  or missing one. Raised once a coordinator turn has passed since it was first seen (a re-hold no
  longer postpones it).
- `dependency_cycle` (dependency-cycle:<ids>): open tasks that depend on each other in a loop.
  task_add and task_update already refuse to close one; this catches any that exist.

All current ones are listed in the stale_holds event and flagged in each blocker sweep until they
are resolved.
"""
from __future__ import annotations

from . import anchors, timefmt
from .db import DB, _first_dead, deferral, dependency_ids, load_result

LOOPS_KEY = "hold_loops"        # kv {key: {"since", "raised", "task", "text", "kind"}}
TIME_KEY = "hold_time_only"     # kv {task id: {"since", "ends": [...], "flag": ts, "why"}}
REAL_ENDS = ("ask", "resource", "when")
ENDS_KEPT = 5
OVERDUE_S = 3600   # a planned start_after that passed this long ago and still did not start
LIST_MAX = 20
EVENTS = {"time-only": "time_only_hold", "blocked-dependency": "blocked_dependency",
          "dead-dependency": "dead_dependency", "dependency-cycle": "dependency_cycle"}
AFTER_TURN = ("blocked-dependency", "dead-dependency")   # raised only after a coordinator turn saw them


def _title(t: dict | None) -> str:
    return " ".join(str((t or {}).get("title") or "").split())[:70]


def _reviewed(db: DB) -> set[int]:
    """Tasks a finished review covered: its depends_on, or the auto_review:<id> label."""
    out: set[int] = set()
    for r in db.q("SELECT depends_on, labels FROM tasks WHERE kind='review' AND status='done'"):
        out.update(d for d in dependency_ids(r) if isinstance(d, int))
        for lb in anchors._labels(r):
            if isinstance(lb, str) and lb.startswith("auto_review:") and lb[12:].isdigit():
                out.add(int(lb[12:]))
    return out


def cycles(tasks: dict[int, dict]) -> list[list[int]]:
    """Groups of open tasks that depend on each other in a loop (strongly connected, iterative
    Tarjan), each sorted, smallest first."""
    edges = {i: [d for d in dependency_ids(t) if d in tasks] for i, t in tasks.items()}
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on: set[int] = set()
    out: list[list[int]] = []
    for root in sorted(edges):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = len(index)
                stack.append(v)
                on.add(v)
            if i < len(edges[v]):
                work.append((v, i + 1))
                w = edges[v][i]
                if w not in index:
                    work.append((w, 0))
                elif w in on:
                    low[v] = min(low[v], index[w])
                continue
            if low[v] == index[v]:
                group = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    group.append(w)
                    if w == v:
                        break
                if len(group) > 1 or v in edges[v]:
                    out.append(sorted(group))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return sorted(out)


def _time_end(t: dict, now: float) -> float | None:
    """The time a time-only hold ends, or None when the task is not held only on a time."""
    if t["status"] == "blocked":
        got = anchors.anchor(t)
        if got and got[0] == "until" and not deferral(t).get("when"):
            try:
                return float(got[1])
            except ValueError:
                return None
        return None
    if t["status"] == "queued":
        # only a start_after deferral: not_before alone is also the daemon's own retry backoff
        # (busy resource, failed start, a waiting task's probe), which it moves on every try
        d = deferral(t)
        if not d.get("when") and float(d.get("after") or 0) > now:
            return float(d["after"])
    return None


def _overdue(t: dict, now: float) -> float | None:
    """Seconds since a queued task's planned start_after (no start_when) passed, once that is at
    least OVERDUE_S and it still did not start; None otherwise."""
    if t["status"] != "queued":
        return None
    d = deferral(t)
    late = now - float(d.get("after") or now)
    return late if not d.get("when") and d.get("after") and late >= OVERDUE_S else None


def _real_end(t: dict, states: dict) -> bool:
    """The task waits on something other than a time: an ask, resource or probe anchor, a
    start_when probe, a queued waiting hand-off's retry_when or on_pass_cmd probe (the daemon runs
    no other), or a dependency not done (an open one produces its evidence; a blocked or dead one is its own condition)."""
    got = anchors.anchor(t) if t["status"] == "blocked" else None
    if got and got[0] in REAL_ENDS or deferral(t).get("when"):
        return True
    r = load_result(t["result"])
    if t["status"] == "queued" and r.get("status") == "waiting" and (r.get("retry_when") or r.get("on_pass_cmd")):
        return True
    return any(states.get(d) != "done" for d in dependency_ids(t))


def _track_time_only(db: DB, cfg: dict, now: float, tasks: dict[int, dict], states: dict) -> dict:
    """Update TIME_KEY and return it: an entry per task held only on a time with no producer, kept
    while the task is open and gets no real end (between holds too, so a requeue and re-hold, or a
    re-armed time, keeps it), with `flag` set once it is stale."""
    old = db.kv(TIME_KEY, {}) or {}
    limit = anchors.hold_max_h(cfg) * 3600
    reviewed = None
    new: dict[str, dict] = {}
    for tid, t in tasks.items():
        e = old.get(str(tid))
        end = _time_end(t, now)
        if _real_end(t, states):
            continue
        late = _overdue(t, now)
        if end is None and late is None:
            if e:
                new[str(tid)] = e   # between holds (queued or running): kept
            continue
        e = dict(e or {"since": now, "ends": []})
        ends = [float(x) for x in e.get("ends") or []]
        if end is not None and all(abs(end - x) > 60 for x in ends):
            e["n"] = int(e.get("n") or 0) + (1 if ends else 0)   # a new time on the same hold: re-armed
            ends = (ends + [end])[-ENDS_KEPT:]
        e["ends"] = ends
        if not e.get("flag"):
            rearmed = f"its time was re-armed {e['n']} time(s)" if e.get("n") else ""
            if t["status"] == "queued":   # a planned start_after deferral: only re-armed or overdue
                why = rearmed or (f"its start_after passed {late / 3600:.1f}h ago and it still did not start"
                                  if late is not None else "")
            else:
                if reviewed is None:
                    reviewed = _reviewed(db)
                age = now - float(e["since"])
                why = ("its review passed" if tid in reviewed else rearmed or
                       (f"held only on a time for {age / 3600:.1f}h" if limit and age >= limit else ""))
            if why:
                e.update(flag=now, why=why)
        new[str(tid)] = e
    if new != old:
        db.set_kv(TIME_KEY, new)
    return new


def current(db: DB, cfg: dict, now: float) -> dict[str, dict]:
    """Every loop condition true now: {key: {"kind", "task", "text"}}."""
    states = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM tasks")}
    tasks = {r["id"]: r for r in db.q("SELECT * FROM tasks WHERE status NOT IN ('done','failed','cancelled')")}
    out: dict[str, dict] = {}
    for tid, e in _track_time_only(db, cfg, now, tasks, states).items():
        t = tasks.get(int(tid))
        if not e.get("flag") or not t:
            continue
        end = _time_end(t, now)
        after = deferral(t).get("after") if t["status"] == "queued" else None
        held = (anchors.describe("until", str(end), cfg) if end is not None else
                "its start_after " + timefmt.long(float(after), cfg) + " passed, still queued"
                if after and _overdue(t, now) is not None else f"between time holds (now {t['status']})")
        out[f"time-only:{tid}"] = {
            "kind": "time-only", "task": int(tid),
            "text": f"#{tid} {_title(t)} is held only on a time ({held}) and nothing open produces what it "
                    f"waits for ({e.get('why')}). A planned start_after raises this only once re-armed or "
                    f"overdue; re-arming the time does not clear it. Decide once: close it "
                    f"as done and queue one validation task gated on a start_when probe (task_add start_when); "
                    f"give it a real end (task_update status blocked waits_on when:<probe>, or depends_on the "
                    f"task that produces it); or cancel it."}
    groups = cycles(tasks)
    in_cycle = {i: g for g in groups for i in g}
    for g in groups:
        waits = "; ".join(f"#{i} waits on " + ", ".join(f"#{d}" for d in dependency_ids(tasks[i]) if d in g)
                          for i in g)
        out["dependency-cycle:" + ",".join(map(str, g))] = {
            "kind": "dependency-cycle", "task": g[0],
            "text": f"Tasks {', '.join(f'#{i}' for i in g)} depend on each other in a loop ({waits}), so none of "
                    f"them can start. Break it: task_update depends_on on one member (a replan task must not "
                    f"depend on the task it is meant to unblock), or cancel one."}
    for tid, t in tasks.items():
        if t["status"] not in ("queued", "blocked"):
            continue
        deps = dependency_ids(t)
        if t["status"] == "blocked":
            dead = _first_dead(deps, states)
            if dead:
                dep, why = dead
                what = f"#{dep} ({why})" if dep is not None else "a dependency that is not a task id"
                redo = (f"re-add that work with task_add continues={dep} (its dependents move to the new task), "
                        if why in ("failed", "cancelled") else "")
                out[f"dead-dependency:{tid}:{dep}"] = {
                    "kind": "dead-dependency", "task": tid,
                    "text": f"#{tid} {_title(t)} is still blocked on {what} after a coordinator turn. Options: "
                            f"{redo}re-point #{tid} with task_update depends_on, or cancel it."}
        for d in deps:
            b = tasks.get(d) if isinstance(d, int) else None
            if not b or b["status"] != "blocked" or in_cycle.get(d) is not None and tid in in_cycle[d]:
                continue
            got = anchors.anchor(b)
            if got and got[0] in REAL_ENDS:
                continue   # its own end requeues it, and this task follows
            reason = " ".join(str(b["blocked_reason"] or "").split())[:120]
            out[f"blocked-dependency:{tid}:{d}"] = {
                "kind": "blocked-dependency", "task": tid,
                "text": f"#{tid} {_title(t)} ({t['status']}) depends on #{d} {_title(b)}, which is blocked "
                        f"({reason or 'no reason given'}) with no end of its own, so #{tid} cannot start. Options: "
                        f"re-point #{tid} with task_update depends_on (drop or replace #{d}), give #{d} a real "
                        f"end or unblock it, or cancel #{tid}."}
    return out


def due(db: DB, cfg: dict, now: float) -> list[tuple[str, dict]]:
    """Update LOOPS_KEY from current() and return the conditions to raise now, each marked raised:
    once per key while it lasts; a key that is no longer true is dropped (resolved)."""
    live = current(db, cfg, now)
    old = db.kv(LOOPS_KEY, {}) or {}
    new, out = {}, []
    for key, row in live.items():
        e = {**(old.get(key) or {"since": now}), "task": row["task"], "kind": row["kind"], "text": row["text"]}
        if not e.get("raised"):
            prefix = key.split(":", 1)[0]
            ready = prefix not in AFTER_TURN or db.one(
                "SELECT id FROM runs WHERE role='coordinator' AND status='ok' AND started>?", (float(e["since"]),))
            if ready and not (prefix == "dead-dependency" and db.one(
                    "SELECT id FROM events WHERE fingerprint=? AND ts>=?", (key, float(e["since"])))):
                e["raised"] = now
                out.append((key, e))
        new[key] = e
    if new != old:
        db.set_kv(LOOPS_KEY, new)
    return out


def flagged(db: DB) -> dict[int, str]:
    """Task id -> what loops it is in, for the blocker sweep and stale_holds."""
    out: dict[int, list[str]] = {}
    for key, e in (db.kv(LOOPS_KEY, {}) or {}).items():
        kind = key.split(":", 1)[0]
        ids = [int(x) for x in key.split(":", 1)[1].split(",")] if kind == "dependency-cycle" else [e.get("task")]
        what = {"time-only": "held only on a time, nothing produces its evidence",
                "dependency-cycle": "in dependency cycle " + ", ".join(f"#{i}" for i in ids),
                "dead-dependency": f"depends on dead #{key.rsplit(':', 1)[-1]}",
                "blocked-dependency": f"depends on blocked #{key.rsplit(':', 1)[-1]}"}.get(kind, kind)
        for i in ids:
            if isinstance(i, int):
                out.setdefault(i, []).append(what)
    return {i: "; ".join(v) for i, v in out.items()}


def summary(db: DB) -> str:
    """One line listing every current loop, for the stale_holds event ('' when none)."""
    rows = sorted(flagged(db).items())
    if not rows:
        return ""
    shown = "; ".join(f"#{i} {w}" for i, w in rows[:LIST_MAX])
    more = f"; and {len(rows) - LIST_MAX} more" if len(rows) > LIST_MAX else ""
    return f" Also stuck in a loop until re-pointed, given a real end or cancelled: {shown}{more}."


def event_row(key: str, e: dict, now: float) -> tuple:
    """The events row for a raised condition."""
    kind = EVENTS[key.split(":", 1)[0]]
    return (now, "daemon", kind, key, "normal", e["text"], "queued", e.get("task"))


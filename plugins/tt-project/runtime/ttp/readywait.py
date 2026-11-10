"""Ready-to-start wait (`ttp stats --ready-wait`): how long a ready task waits for its run to start,
and what held it.

A task is ready once its dependencies, start_after and start_when are all met (db.ready_tasks). Its
wait ends when its run starts. Hold causes:
- project_cap: every worker place of the project (or its provider) taken;
- machine_limit: the machine-wide worker limit across projects (machinecap.py);
- checks: a review's own pre-review checks, or the task's own `ttp checks`/`ttp push` run queued;
- budget_gate: the budget gate stopped new work, the dollar caps or the task budget;
- resource: a busy or paused resource or device;
- dispatch: nothing held it and a place was free: the time until dispatch started it;
- other: a branch clash, the disk guard, a login, the network, optional work held;
- unrecorded: a wait whose cause no record names.

Going forward the daemon records it on the run row itself (runs.note "ready_wait": when it became
ready and the seconds each cause held it, as the ready check (daemon.check_unstarted, every minute)
saw it): no write of its own. Runs that started before that record get a reconstruction from the
task's records (as unstarted.ready_since) and events: their cause is the one the last
ready_not_started, queue_wait or dispatch_hold event of the wait names, else "unrecorded". A ready time
is raised to the task's latest requeue, unblock or block before the start (REQUEUE). Counted only
(no_ready_time), with no recorded ready time: a resumed wait (a run after a `waiting` hand-off) with no
requeue event, and a first run whose rebuilt ready time is more than FIRST_EVENT_S before the task's
first event in this db (imported or parked with no record: a ready held task gets a hold event within
minutes, so that silence was not a wait). Separately: the project's host checks slot waits (checkcap.wait_records),
which a running or waiting task spends after its start."""
from __future__ import annotations

import json
import time

from .db import dependency_ids

CAUSES = ("project_cap", "machine_limit", "checks", "budget_gate", "resource", "dispatch", "other", "unrecorded")
LABELS = {"project_cap": "project cap", "machine_limit": "machine-wide limit", "checks": "own checks",
          "budget_gate": "budget gate", "resource": "resource", "dispatch": "dispatch (free place)",
          "other": "other hold", "unrecorded": "unrecorded"}
GAP_S = 180.0   # a gap between ready checks longer than this (daemon stopped, host asleep): no cause seen
REQUEUE = ("task_requeued", "start_when_passed", "push_requeued", "hold_released", "task_blocked")
FIRST_EVENT_S = 3600.0   # a rebuilt first-run ready time this long before the task's first event: unknown


def _requeued(q, tid: int, lo: float, hi: float) -> float | None:
    """The latest requeue, unblock or block of task `tid` in [lo, hi]: it was not ready before it."""
    row = q(f"SELECT MAX(ts) ts FROM events WHERE task=? AND kind IN ({','.join('?' * len(REQUEUE))}) "
            "AND ts>=? AND ts<=?", (tid, *REQUEUE, lo, hi))
    return float(row[0]["ts"]) if row and row[0]["ts"] is not None else None


def ready_since(db, task: dict) -> float:
    """unstarted.ready_since, raised to the task's latest requeue, unblock or block: a task that sat
    blocked or deferred was not ready then. (unstarted's own alert timing is left as it is.)"""
    from . import unstarted
    at = unstarted.ready_since(db, task)
    rq = _requeued(db.q, task["id"], at, time.time())
    return max(at, rq) if rq is not None else at


def cause(why, held_by: str | None, gate, free: bool, held: bool = False, precheck: bool = False) -> str:
    """The cause of `why` (daemon._start_hold's answer, or _own_checks_wait's words) for one ready task;
    `free`: a worker place of its provider is free; `held`: check_unstarted holds it; `precheck`: a review
    whose own pre-review checks run (_start_hold answers "held" for it too)."""
    if precheck:
        return "checks"
    if why is None:
        if free:
            return "checks" if held else "dispatch"   # held with nothing else: its own pre-review checks
        return "budget_gate" if gate is not None and not getattr(gate, "allow_new_work", True) else "project_cap"
    if why == "gate":
        return "budget_gate" if gate is not None and not getattr(gate, "allow_new_work", True) else "project_cap"
    if why in ("caps", "budget"):
        return "budget_gate"
    if why == "held":
        return "machine_limit" if str(held_by or "").startswith("the machine-wide worker limit") else "other"
    if why in ("resource", "device", "paused"):
        return "resource"
    if why in ("logged_out", "net", "optional"):
        return "other"
    return "checks"   # _own_checks_wait's words: its own checks or push run is queued or running


def note(acc: dict, tid: int, c: str, now: float, since) -> None:
    """One ready check's sight of task `tid` held by cause `c`; `since` (a value or a callable) when it
    became ready, read only on its first sight. The time since the previous sight goes to that sight's cause;
    a gap over GAP_S goes to "unrecorded"."""
    e = acc.get(tid)
    if e is None:
        acc[tid] = {"since": float(since() if callable(since) else since), "first": now, "first_cause": c,
                    "last": now, "cause": c, "causes": {}}
        return
    gap = now - e["last"]
    k = e["cause"] if gap <= GAP_S else "unrecorded"   # a longer gap: no check saw why it waited
    if gap > 0:
        e["causes"][k] = e["causes"].get(k, 0.0) + gap
    e["last"], e["cause"] = now, c


def prune(acc: dict, ready: set) -> None:
    """Forget the tasks no longer ready (started, or left the queue)."""
    for tid in [t for t in acc if t not in ready]:
        del acc[tid]


def take(acc: dict, tid: int, now: float, since, last_check: float | None, last_ready: set | None = None) -> dict:
    """The run note's "ready_wait" for task `tid` starting now: {"since", "causes"} (seconds, rounded).
    Without a sight of it, a ready check within GAP_S that looked (`last_ready`, its ready ids, not None)
    and did not find it ready bounds its ready time and the wait is dispatch's; else the wait is
    unrecorded (a reset check after a daemon start, settling or a gap evaluates nothing)."""
    e = acc.pop(tid, None)
    causes: dict[str, float] = {}

    def add(k: str, s: float) -> None:
        if s > 0:
            causes[k] = causes.get(k, 0.0) + s
    if e:
        start = e["since"]
        lead = e["first"] - start
        add(e["first_cause"] if lead <= GAP_S else "unrecorded", lead)
        for k, s in e["causes"].items():
            add(k, s)
        tail = now - e["last"]
        add(e["cause"] if tail <= GAP_S else "unrecorded", tail)
    else:
        start = float(since() if callable(since) else since)
        recent = last_check is not None and last_ready is not None and tid not in last_ready \
            and 0 <= now - last_check <= GAP_S
        if recent:
            start = max(start, last_check)
        add("dispatch" if recent else "unrecorded", now - start)
    return {"since": round(start, 1), "causes": {k: round(v, 1) for k, v in causes.items()}}


def _event_cause(kind: str, text: str, data: str | None) -> str | None:
    t = text or ""
    if kind == "ready_not_started":
        if "machine-wide worker limit" in t:
            return "machine_limit"
        if "slot taken" in t or "slots taken" in t:
            return "project_cap"
        if "spend gate" in t or "budget gate" in t or "dollar caps" in t:
            return "budget_gate"
        if "ttp checks" in t or "ttp push" in t:
            return "checks"
        if "held by nothing dispatch records" in t:
            return "dispatch"
        return "other"
    if kind == "queue_wait":
        try:
            key = (json.loads(data or "{}").get("must_act") or {}).get("key") or ""
        except (ValueError, AttributeError):
            key = ""
        q = key.rsplit("-", 1)[-1]
        return {"workers": "project_cap", "checks": "checks", "push": "checks"}.get(q, "other")
    if kind == "dispatch_hold":
        return "resource"
    return None


def _rebuild(q, r: dict) -> tuple[float | None, dict]:
    """(ready time, causes) of a run started before the ready_wait record; (None, {}) without a ready time."""
    started = float(r["started"])
    prev = q("SELECT ended FROM runs WHERE task=? AND id<? AND ended IS NOT NULL ORDER BY id DESC LIMIT 1",
             (r["task"], r["id"]))
    if prev:
        ended = float(prev[0]["ended"])
        rq = _requeued(q, r["task"], ended, started)
        waited = q("SELECT 1 FROM events WHERE task=? AND kind='task_waiting' AND ts>=? AND ts<=? LIMIT 1",
                   (r["task"], ended - 30, started))
        if rq is not None:
            ready = rq
        elif waited:
            return None, {}   # woken from its own wait: when the wait ended is not recorded
        else:
            ready = ended
    else:
        ready = float(r["created"] or 0)
        nb = r.get("not_before")
        if nb is not None and float(nb) <= started:
            ready = max(ready, float(nb))
        deps = [d for d in dependency_ids(r) if d is not None]
        if deps:
            row = q(f"SELECT MAX(ts) ts FROM events WHERE kind IN ('task_done','task_review') "
                    f"AND task IN ({','.join('?' * len(deps))}) AND ts<=?", (*deps, started))
            if row and row[0]["ts"] is not None:
                ready = max(ready, float(row[0]["ts"]))
        sw = q("SELECT MAX(ts) ts FROM events WHERE task=? AND kind='start_when_passed' AND ts<=?",
               (r["task"], started))
        if sw and sw[0]["ts"] is not None:
            ready = max(ready, float(sw[0]["ts"]))
        rq = _requeued(q, r["task"], ready, started)
        if rq is not None:
            ready = max(ready, rq)
        first = q("SELECT MIN(ts) ts FROM events WHERE task=? AND ts<=?", (r["task"], started))
        if first and first[0]["ts"] is not None and float(first[0]["ts"]) - ready > FIRST_EVENT_S:
            return None, {}   # silent long before its first event (imported, parked): no known ready time
    ready = min(ready, started)
    ev = q("SELECT kind, text, data FROM events WHERE task=? AND kind IN "
           "('ready_not_started','queue_wait','dispatch_hold') AND ts>=? AND ts<=? ORDER BY id DESC LIMIT 1",
           (r["task"], ready, started))
    c = (_event_cause(ev[0]["kind"], ev[0]["text"], ev[0]["data"]) if ev else None) or "unrecorded"
    return ready, ({c: started - ready} if started > ready else {})


def _pct(xs: list[float]) -> dict:
    xs = sorted(xs)
    if not xs:
        return {"n": 0}
    mid = len(xs) // 2
    med = xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2
    return {"n": len(xs), "median_s": round(med, 1), "p90_s": round(xs[-(-9 * len(xs) // 10) - 1], 1),
            "max_s": round(xs[-1], 1)}


def report(q, since: float, until: float | None = None, state=None) -> dict:
    """Ready-to-start waits of the worker runs started in [since, until): overall and per cause (the runs
    a cause held, and the seconds it held each), with how many came from the run's own record. `q(sql,
    args)` reads the project's db (rows as dicts); `state`: its state folder, for the checks slot waits."""
    until = time.time() if until is None else until
    rows = q("SELECT r.id, r.task, r.started, r.note, t.created, t.not_before, t.depends_on FROM runs r "
             "JOIN tasks t ON t.id=r.task WHERE r.role!='coordinator' AND r.started>=? AND r.started<? "
             "ORDER BY r.started", (since, until))
    waits: list[float] = []
    by: dict[str, list[float]] = {c: [] for c in CAUSES}
    recorded = no_ready = 0
    for r in rows:
        try:
            rw = (json.loads(r["note"] or "{}") or {}).get("ready_wait")
        except (ValueError, AttributeError):
            rw = None
        if isinstance(rw, dict) and rw.get("since") is not None:
            recorded += 1
            ready, causes = float(rw["since"]), {k: float(v) for k, v in (rw.get("causes") or {}).items()}
        else:
            ready, causes = _rebuild(q, r)
            if ready is None:
                no_ready += 1
                continue
        waits.append(max(0.0, float(r["started"]) - ready))
        for k, s in causes.items():
            if s > 0:
                by.setdefault(k if k in by else "other", []).append(s)
    out = {"since": since, "until": until, "runs": len(rows), "recorded": recorded, "no_ready_time": no_ready,
           "wait": _pct(waits), "by_cause": {c: dict(_pct(v), total_s=round(sum(v), 1)) for c, v in by.items() if v}}
    if state is not None:
        try:
            from . import checkcap
            recs = [x for x in checkcap.wait_records(state, until, until - since) if x["granted_at"] >= since]
            out["checks_slot"] = _pct([max(0.0, x["granted_at"] - x["queued_at"]) for x in recs])
        except Exception:   # an unreadable checks log loses only that line
            out["checks_slot"] = {"n": 0}
    return out


def _d(s) -> str:
    if s is None:
        return "-"
    return f"{s:.0f}s" if s < 60 else f"{s / 60:.1f}m" if s < 3600 else f"{s / 3600:.1f}h"


def _cells(st: dict) -> str:
    return f"{st.get('n', 0):>4} {_d(st.get('median_s')):>7} {_d(st.get('p90_s')):>7} {_d(st.get('max_s')):>7}"


def text(reports: dict) -> str:
    """{project name: report} as one table: overall, each cause, the checks slot waits."""
    head = f"{'project':<16} {'what':<22} {'n':>4} {'median':>7} {'p90':>7} {'max':>7}"
    out = [head]
    for name, s in reports.items():
        if s.get("error"):
            out.append(f"{name:<16} unreadable: {s['error']}")
            continue
        out.append(f"{name:<16} {'ready-to-start wait':<22} {_cells(s['wait'])}")
        for c in CAUSES:
            if c in s["by_cause"]:
                out.append(f"{'':<16} {'  ' + LABELS[c]:<22} {_cells(s['by_cause'][c])}")
        if (s.get("checks_slot") or {}).get("n"):
            out.append(f"{'':<16} {'host checks slot':<22} {_cells(s['checks_slot'])}")
        out.append(f"{'':<16} runs {s['runs']}: {s['recorded']} with a ready_wait record, "
                   f"{s['no_ready_time']} resumed waits without a ready time")
    return "\n".join(out)

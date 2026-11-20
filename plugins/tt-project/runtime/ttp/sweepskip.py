# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Skip a blocker sweep turn model-free when nothing in it can move without the daemon (sweep.py).

Before the daemon queues a blocker sweep (Daemon.blocker_sweep), it names what ends each stuck item
without a turn (end()):
- a user pause: a user-pause probe, a pause the user set on a resource, or a cover recorded on one;
- a pause the coordinator set on a resource this project checks (pauseends.owned), something
  model-free re-evaluates or ends before the next sweep (_pause_end): an `end_when` probe whose last
  answer is fresh and not broken (0, or a not yet: 1, 75, 255), an enabled heal check's health end
  (which lifts it after healthy results), or, with neither, an `until` before the next interval
  sweep is due (coordinator.blocker_sweep_h), when the pause_end_due event brings the re-evaluation;
- a time: a start_after or a hold's `waits_on until` still ahead, a promise, mitigation or outage not
  due yet;
- a working model-free probe: a deferral's start_when within coordinator.defer_max_days, a hold's
  `waits_on when` or a waiting task's retry_when, whose last answer is a fresh "not yet" (landed:#id
  is such a probe); a retry_when past waiting.max_hold_s is overdue;
- an open user ask a blocked task's hold is anchored on (`waits_on ask`) or names (sweep's `ask`);
- a free resource a hold waits on (`waits_on resource`, which the daemon lifts);
- a review younger than coordinator.review_backlog_h whose review task is queued or running.

The turn is skipped only when every item has such an end, the items and each one's status, spec,
reason and end are what they were at the last sweep a turn ran (MODEL_KEY; less the times the daemon
re-writes when it puts a task back to sleep, _parts), that sweep was less than MAX_AGE_H ago, and
nothing else needs a turn: no signal() request pending, no inbound message
unhandled, no other queued event but routine ones (quietgate.routine_kinds), no task pushing past
coordinator.review_backlog_h, no loop and no ask held past ask_rethink_h. A new or changed item (a pause:
its reason, `until` or `end_when`), a broken or stale probe, a deferral past defer_max_days, a pause the
coordinator set with none of those ends or past its end, a dependency or probe task that failed, was
cancelled or does not exist, a review past its expected time, or an item with no model-free end starts
the sweep turn as before; its reason names the items changed and their fields.

A re-check a sweep outcome asked for (its `next` or the daily re-check) that came due is a sweep due only by
time: a receipt stands in for its turn only while the last model sweep is less than
coordinator.blocker_sweep_recheck_skip_h (RECHECK_SKIP_H, 6; 0: never) old and each item's stamp (_stamp: its
task's fields less what a model-free re-sleep writes, its probe and that probe's last exit code) is what it was at the last sweep, a turn or a skip
(LAST_KEY). The receipt carries the re-check on, so once the model sweep is that old the next sweep due runs a
turn for it (the charter's high-effort re-evaluation at least every 6 hours).

A skip writes a receipt (RECEIPTS_KEY: each item and what ends it, with the estimated saving, the
quiet gate's mean coordinator turn) and sets sweep.STATE_KEY `receipt`, which the sweep's clock counts
from (sweep._clock). `ttp status` and the web feed show the latest receipt as one low line; the daily
review counts model sweeps and skipped ones. Coordinator effort is unchanged: the saving is the turn
not taken."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

from . import anchors, ends, holdloops, landed, quietgate, sweep, timefmt
from .db import DB, dependency_ids, deferral, load_result

MODEL_KEY = "blocker_sweep_model"        # kv: {ts, n, fps: {key: fp}, parts: {key: {field: h}}}: the last sweep a turn ran
RECEIPTS_KEY = "blocker_sweep_receipts"  # kv: [{ts, model, why, items: [[key, end]], usd}]
LAST_KEY = "blocker_sweep_last"         # kv: {ts, stamps: {key: h}, rechecks: [line]}: the last sweep, a turn or a skip
MAX_AGE_H = 24.0
RECHECK_SKIP_H = 6.0   # coordinator.blocker_sweep_recheck_skip_h: a due re-check skips only this soon after a model sweep
KEEP_S = 8 * 86400
DEFER_MAX_DAYS = 14.0
MAX_HOLD_S = 6 * 3600.0
DEAD = ("failed", "cancelled")
# What the daemon writes when it puts a task back to sleep: a reason's "; next try <timefmt.short>" tail, and a
# time-only deferral's "starts after <timefmt.long of not_before>" (sweep.items).
NEXT_TRY = re.compile(r"; next try ([A-Z][a-z]{2} )?\d{2}:\d{2} \S+$")
STARTS_AT = re.compile(r"^starts after \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+$")
RECHECKS = ("next check due", "daily re-check due")   # sweep._signals lines for a re-check a turn asked for

Probe = Callable[[int], Any]   # task id -> (rc, ts, probe) of its last probe answer, or None


def _f(v: Any, default: float) -> float:
    try:
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return default


def _dead_refs(db: DB, t: dict, probe: str | None = None) -> str:
    """Why task `t` can never move on its own: a dependency or a task its probe names is missing,
    failed or cancelled ('' when none)."""
    ids = [d for d in dependency_ids(t) if isinstance(d, int)]
    ids += [int(r) for r in landed.task_refs(probe) if r.isdigit()]
    if landed.missing_task_refs(db, probe):
        return "its probe names a missing task"
    for d in ids:
        st = (db.task(d) or {}).get("status")
        if st is None or st in DEAD:
            return f"#{d} is {st or 'missing'}"
    return ""


def _probe_end(probe: Any, got: Any, now: float, what: str) -> str | None:
    """`what` once the daemon's last answer to `probe` is a fresh "not yet", else None."""
    if not isinstance(probe, str) or not probe.strip():
        return None
    if not (isinstance(got, (tuple, list)) and len(got) >= 3 and got[2] == probe):
        return None
    if got[0] not in ends.NOT_YET_RCS or now - _f(got[1], 0) > sweep.PROBE_FRESH_S:
        return None
    return f"{what} `{sweep._clip(probe, 120)}`"


def _pause_end(db: DB, cfg: dict, name: str, row: dict, now: float, project: str | None) -> str | None:
    """What re-evaluates or ends a pause the coordinator set before the next sweep without a turn,
    with the rule it counts under (probe, heal, end before the next sweep), or None (module doc)."""
    from . import heal
    if row.get("by") != "coordinator" or row.get("fleet") or (row.get("shared") and row.get("project") != project):
        return None   # only the project that set a pause checks its end (pauseends.owned)
    until, probe = row.get("until"), row.get("end_when")
    if until is not None and _f(until, 0) <= now:
        return None   # past its end and never lifted
    if isinstance(probe, str) and probe.strip():
        got = ((db.kv(sweep.PAUSE_PROBES_KEY) or {}).get(name) or {})
        if got.get("probe") != probe or got.get("rc") not in (0, *ends.NOT_YET_RCS) \
                or now - _f(got.get("at"), 0) > sweep.PROBE_FRESH_S:
            return None   # broken, stale or never answered
        return f"probe: end_when `{sweep._clip(probe, 120)}`"
    if isinstance(probe, dict):
        check = probe.get("heal")
        return f"heal: heal check `{check}` lifts it" if any(r["name"] == check for r, _ in heal.checks(db)) else None
    every = sweep._cfg(cfg, "blocker_sweep_h", sweep.EVERY_H) * 3600
    if until is not None and every > 0 and _f(until, 0) <= now + every:
        return f"end before the next sweep: until {timefmt.long(_f(until, 0), cfg)}"
    return None


def end(db: DB, cfg: dict, i: dict, now: float, probe_of: Probe | None = None, asks: dict | None = None,
        paused: dict | None = None, project: str | None = None) -> str | None:
    """What ends sweep item `i` without a turn (see the module doc), or None when a turn must judge it."""
    c = cfg.get("coordinator") or {}
    where = cfg
    if i.get("ends") == "due":
        return "its due time"
    if i["kind"] == "paused":
        if i.get("ends") == "user":
            return "user pause"
        row = (db.paused_resources() if paused is None else paused).get(i["resource"])
        return _pause_end(db, cfg, i["resource"], row, now, project) if isinstance(row, dict) else None
    t = db.task(i["task"]) if i.get("task") else None
    if t is None:
        return None
    if _dead_refs(db, t, deferral(t).get("when")):
        return None
    if i.get("ends") == "user":
        return "user pause"
    if i.get("ends") == "time":
        return f"starts after {timefmt.long(_f(t['not_before'], now), where)}"
    probe_of = probe_of or (lambda _tid: None)
    if i["kind"] == "deferred":
        d = deferral(t)
        if now - _f(d.get("since") or t["created"], now) >= _f(c.get("defer_max_days"), DEFER_MAX_DAYS) * 86400:
            return None
        return f"start_when {sweep._clip(d.get('when'), 120)}" if (i.get("watch") or {}).get("failing") else None
    if i["kind"] == "review":
        backlog = _f(c.get("review_backlog_h"), sweep.REVIEW_BACKLOG_H) * 3600
        if i.get("age_s") is None or i["age_s"] >= backlog:
            return None
        for r in db.q("SELECT id, status, depends_on, labels FROM tasks WHERE kind='review' AND id>? "
                      "AND status IN ('queued','running') ORDER BY id DESC", (t["id"],)):
            if t["id"] in dependency_ids(r) or f"auto_review:{t['id']}" in (r["labels"] or ""):
                return f"review #{r['id']}"
        return None
    if i["kind"] == "waiting":
        prev = load_result(t["result"])
        if now - _f(prev.get("waiting_since"), now) > _f((cfg.get("waiting") or {}).get("max_hold_s"), MAX_HOLD_S):
            return None   # overdue: past the wait's own limit
        probe = prev.get("retry_when")
        if _dead_refs(db, t, probe):
            return None
        return _probe_end(probe, probe_of(t["id"]), now, "retry_when")
    if i["kind"] != "blocked":
        return None
    got = anchors.anchor(t)
    if got:
        kind, value = got
        if kind == "ask":
            return f"open ask #{value}" if value.isdigit() and int(value) in (asks or {}) else None
        if kind == "until":
            return f"until {timefmt.long(_f(value, 0), where)}" if _f(value, 0) > now else None
        if kind == "resource":
            return f"resource {value} free"
        if kind == "when":
            return None if _dead_refs(db, t, value) else _probe_end(value, probe_of(t["id"]), now, "when")
    a = i.get("ask") or {}
    return f"open ask #{a['id']}" if a.get("id") in (asks or {}) else None


def _parts(db: DB, i: dict, e: str | None, paused: dict | None = None) -> dict[str, Any]:
    """The item as a turn would judge it, by field: its kind, status, spec, reason, end and its dependencies' states.
    A coordinator pause counts its row's own ends (`until`, `end_when`) instead of the end found now: a dated end
    comes within the next sweep, and a probe first answers, as time passes, not as the pause changes. Text the daemon
    re-writes each time it puts a task back to sleep is left out: a reason's "; next try <time>" tail, and a time-only
    deferral's `not_before` (its "starts after <time>" reason and end), which a still-failing retry_when pushes on each
    hour; the deliberate start_after counts instead."""
    t = db.task(i["task"]) if i.get("task") else None
    deps = [[d, (db.task(d) or {}).get("status")] for d in dependency_ids(t) if isinstance(d, int)] if t else []
    row = (paused or {}).get(i["resource"]) if i["kind"] == "paused" else None
    if isinstance(row, dict) and i.get("ends") != "user":   # a user pause keeps its end as before
        e = [row.get(k) for k in ("by", "since", "until", "end_when")]
    reason = NEXT_TRY.sub("", str(i["reason"] or ""))
    if t and i.get("ends") == "time":
        e = ["time", deferral(t).get("after")]
        reason = STARTS_AT.sub("starts after", reason)
    return {"kind": i["kind"], "status": i["status"], "spec": i["spec_sha"], "reason": reason, "end": e, "deps": deps}


def _h(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, default=str).encode()).hexdigest()[:16]


def _fp(db: DB, i: dict, e: str | None, paused: dict | None = None) -> str:
    """The item's fingerprint: its key and _parts()."""
    return _h([i["key"], *_parts(db, i, e, paused).values()])


def _stamp(db: DB, i: dict, probe_of: Probe) -> str:
    """What a re-check skip also needs unchanged since the last sweep (turn or skip): the task's fields a turn
    judges (_task_stamp) and its probe with its last exit code (a start_when watch, or the task's last probe
    answer). The raw updated time is not used: the daemon moves it on each model-free re-sleep."""
    w = i.get("watch") or {}
    got = probe_of(i["task"]) if i.get("task") else None
    got = [got[0], got[2]] if isinstance(got, (tuple, list)) and len(got) >= 3 else None
    t = db.task(i["task"]) if i.get("task") else None
    return _h([_task_stamp(t) if t else None, w.get("probe"), w.get("rc"), got])


# Hand-off fields the daemon itself writes while a task waits: the wait's clock, wake counts, a peer hold's state
# and the wake gate's skip memory (waitgate.skipped).
TIMER_FIELDS = frozenset({"waiting_since", "stale_wakes", "peer_held_at", "peer_hold_live", "wake_gate"})


def _task_stamp(t: dict) -> list:
    """The task as a turn judges it, less what the daemon re-writes when it puts it back to sleep: not_before,
    the updated time, TIMER_FIELDS, a waiting task's reason (only the daemon writes it: "waiting for <its probe's
    latest line>; its probe says not yet; next try <time>", or the wake gate's skip; the wait itself is the hand-off
    and its task_waiting event), and a blocked reason's "; next try <time>" tail or "starts after <time>"."""
    res = {k: v for k, v in load_result(t["result"]).items() if k not in TIMER_FIELDS}
    reason = "" if t["status"] == "waiting" else \
        STARTS_AT.sub("starts after", NEXT_TRY.sub("", str(t["blocked_reason"] or "")))
    return [t["status"], t["spec"], t["priority"], res, t["labels"], t["depends_on"], reason]


def _recheck(cfg: dict, model: dict, last: dict, stamps: dict, due: list[str], now: float) -> list[str]:
    """Why a re-check a turn asked for still needs a turn ([] when a receipt may stand in for it): one came due
    since the clock or a re-check skip carried one, and the last model sweep is RECHECK_SKIP_H old or more, or an
    item's stamp (_stamp) differs from the last sweep's (or none is on record)."""
    held = [str(x) for x in (last.get("rechecks") or [])] if _f(last.get("ts"), 0) >= _f(model.get("ts"), 0) else []
    lines = due + [x for x in held if x not in due]
    if not lines:
        return []
    floor = _f((cfg.get("coordinator") or {}).get("blocker_sweep_recheck_skip_h"), RECHECK_SKIP_H)
    if floor <= 0 or now - _f(model.get("ts"), 0) >= floor * 3600:
        return [f"{lines[0]} (the last model sweep is {floor:g} h old)"]
    was = last.get("stamps")
    if not isinstance(was, dict):
        return [f"{lines[0]} (no item stamps on record)"]
    moved = sorted(k for k in stamps if was.get(k) != stamps[k]) + sorted(k for k in was.keys() - stamps.keys())
    return [f"{lines[0]} (re-written since the last sweep: {', '.join(moved[:5])})"] if moved else []


def _changed(model: dict, fps: dict, parts: dict) -> list[str]:
    """What changed since the model sweep: "key field/field" for a changed item (the key alone when the model
    sweep kept no fields), "+key" for a new one, "-key" for one gone."""
    old, was = model.get("fps") or {}, model.get("parts") or {}
    out = []
    for k in sorted(fps.keys() & old.keys()):
        if fps[k] != old[k]:
            prev = was.get(k) if isinstance(was.get(k), dict) else {}
            fields = [f for f, v in parts[k].items() if prev and prev.get(f) != v]
            out.append(f"{k} {'/'.join(fields)}" if fields else k)
    return out + [f"+{k}" for k in sorted(fps.keys() - old.keys())] + [f"-{k}" for k in sorted(old.keys() - fps.keys())]


def check(db: DB, cfg: dict, live: list[dict], now: float, probe_of: Probe | None = None,
          project: str | None = None) -> dict:
    """{skip: bool, why: why a turn is needed ('' on a skip), ends: {key: end or None}, fps: {key: fp}}.
    `project`: this project's name, which a shared pause names when this project set it."""
    asks = sweep._open_asks(db)
    paused = db.paused_resources() if any(i["kind"] == "paused" for i in live) else {}
    got = {i["key"]: end(db, cfg, i, now, probe_of, asks, paused, project) for i in live}
    parts = {i["key"]: {f: _h(v) for f, v in _parts(db, i, got[i["key"]], paused).items()} for i in live}
    fps = {i["key"]: _fp(db, i, got[i["key"]], paused) for i in live}
    stamps = {i["key"]: _stamp(db, i, probe_of or (lambda _tid: None)) for i in live}
    model = db.kv(MODEL_KEY) or {}
    model = model if isinstance(model, dict) else {}
    last = db.kv(LAST_KEY) or {}
    last = last if isinstance(last, dict) else {}
    state = db.kv(sweep.STATE_KEY, {}) or {}
    due = [s for s in sweep._signals(db, state, live, now) if s.startswith(RECHECKS)]
    out = {"skip": False, "why": "", "ends": got, "fps": fps, "parts": parts, "stamps": stamps, "rechecks": due}
    prev = state.get("items") or {}
    backlog = _f((cfg.get("coordinator") or {}).get("review_backlog_h"), sweep.REVIEW_BACKLOG_H) * 3600
    routine = sorted(quietgate.routine_kinds(cfg))
    checks = (
        (lambda: [k for k, e in got.items() if not e], "no model-free end: {}"),
        (lambda: not isinstance(model, dict) or not model.get("fps"), "no model sweep on record"),
        (lambda: now - _f(model.get("ts"), 0) >= MAX_AGE_H * 3600, "the last model sweep is 24 h old"),
        (lambda: _changed(model, fps, parts), "items changed: {}"),
        (lambda: bool(db.kv(sweep.SIGNAL_KEY, [])), "a signal is pending"),
        # A re-check a turn asked for (an outcome's `next`, a daily re-check) came due since the clock, or an
        # earlier re-check skip carried one: a receipt stands in for it only soon after a model sweep, with every
        # item as it was at the last sweep; the receipt carries it on (LAST_KEY) until a model sweep judges it.
        (lambda: _recheck(cfg, model, last, stamps, due, now), "{}"),
        (lambda: db.one("SELECT id FROM messages WHERE direction='in' AND handled=0 LIMIT 1"), "a message waits"),
        (lambda: db.one(f"SELECT id FROM events WHERE status='queued' AND kind NOT IN "
                        f"({','.join('?' * (len(routine) + 1))}) LIMIT 1", (sweep.SWEEP_KIND, *routine)),
         "another event needs a turn"),
        (lambda: db.one("SELECT id FROM tasks WHERE status='pushing' AND updated<? LIMIT 1", (now - backlog,)),
         "a task is pushing past its expected time"),
        (lambda: holdloops.flagged(db) or [i["key"] for i in live if sweep.loop(db, i, prev.get(i["key"]))], "a loop"),
        (lambda: sweep.rethink(db, cfg, live, now), "an ask held past its rethink time"),
    )
    for test, why in checks:
        if hit := test():
            out["why"] = why.format(", ".join(hit[:5]) + (f", +{len(hit) - 5} more" if len(hit) > 5 else "")) \
                if isinstance(hit, list) else why
            return out
    out["skip"] = True
    return out


def swept(db: DB, got: dict, now: float) -> None:
    """A sweep turn was queued: what later skips must match."""
    n = int((db.kv(sweep.STATE_KEY, {}) or {}).get("n") or 0)
    with db.tx():
        db.set_kv(MODEL_KEY, {"ts": now, "n": n, "fps": got["fps"], "parts": got.get("parts") or {}})
        db.set_kv(LAST_KEY, {"ts": now, "stamps": got.get("stamps") or {}, "rechecks": []})


def skip(db: DB, got: dict, why: str, now: float) -> dict:
    """Write the receipt and count it as the sweep for the interval clock; returns it."""
    model = db.kv(MODEL_KEY) or {}
    rec = {"ts": now, "model": _f(model.get("ts"), 0), "why": sweep._clip(why, 200),
           "items": [[k, e] for k, e in sorted(got["ends"].items())], "usd": quietgate._turn_usd(db)}
    last = db.kv(LAST_KEY) or {}
    held = [str(x) for x in (last.get("rechecks") or [])] \
        if isinstance(last, dict) and _f(last.get("ts"), 0) >= _f(model.get("ts"), 0) else []
    with db.tx():
        keep = [r for r in (db.kv(RECEIPTS_KEY) or []) if isinstance(r, dict) and now - _f(r.get("ts"), 0) < KEEP_S]
        db.set_kv(RECEIPTS_KEY, keep + [rec])
        db.set_kv(LAST_KEY, {"ts": now, "stamps": got.get("stamps") or {},
                             "rechecks": held + [x for x in got.get("rechecks") or [] if x not in held]})
        db.set_kv(sweep.STATE_KEY, {**(db.kv(sweep.STATE_KEY, {}) or {}), "receipt": now})
    return rec


def text(rec: dict, where, now: float) -> str:
    """The receipt as one line."""
    items = "; ".join(f"{k} ({e})" for k, e in rec.get("items") or [])
    return sweep._clip(f"blocker sweep skipped model-free at {timefmt.short(rec['ts'], where, now)}: "
                       f"{len(rec.get('items') or [])} item(s) unchanged since the model sweep at "
                       f"{timefmt.short(_f(rec.get('model'), 0), where, now)}, each ends without a turn: {items}", 400)


def latest(db: DB) -> dict | None:
    """The newest receipt, while no model sweep came after it."""
    recs = [r for r in (db.kv(RECEIPTS_KEY) or []) if isinstance(r, dict)]
    model = _f((db.kv(MODEL_KEY) or {}).get("ts"), 0)
    return recs[-1] if recs and _f(recs[-1].get("ts"), 0) > model else None


def status_lines(db: DB, now: float, where) -> list[str]:
    rec = latest(db)
    return [text(rec, where, now)] if rec else []


def feed(db: DB, now: float, where) -> list[dict]:
    """The web feed's one low row for the latest receipt (state 'status': never an alert)."""
    rec = latest(db)
    return [{"id": None, "ts": rec["ts"], "kind": "sweep", "severity": "low", "state": "status",
             "text": text(rec, where, now)}] if rec else []


def counts(db: DB, since: float, until: float | None = None) -> dict:
    """Model sweeps and skipped ones in [since, until), and the estimated dollars the skips saved."""
    until = time.time() if until is None else until
    model = (db.one("SELECT COUNT(*) n FROM events WHERE kind=? AND ts>=? AND ts<?", (sweep.SWEEP_KIND, since, until))
             or {}).get("n") or 0
    recs = [r for r in (db.kv(RECEIPTS_KEY) or []) if isinstance(r, dict) and since <= _f(r.get("ts"), 0) < until]
    return {"model": int(model), "skipped": len(recs), "usd": round(sum(_f(r.get("usd"), 0) for r in recs), 4)}

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Throughput review (`ttp throughput`): where this project's tasks waited, against which limit, and a
bounded, reversible raise of the sizing limits that held work back. Model-free.

measure() reads the task, run and push-queue records of a window (default 24 h) and sorts each wait
into a lane, from the time the task could start (created, its start_after, its dependencies ended)
to its first run, or to now for one still queued:
- worker_slots: no resource, deferral or provider hold. Limit budget.max_parallel_workers, or the
  provider gate's lower max_parallel (a spend gate: reported, never changed).
- resource:<name> / device:<name>: a `resource:`/`exclusive:` label. Limit its slot count (a device
  lock is always 1). Reported only: a box's headroom is not measurable from here.
- review_sweep: a code task's last run ending to its review task being queued. Limits
  review.sweep_after_s and review.unreviewed_per_pass (against the backlog of tasks in review now).
- review_stall: the same end to the review's first run. Limit coordinator.review_stall_s, the age at
  which a waiting review is raised at all.
- push_batch: a push-queue row's approval to its batch's start. Limit delivery.push_batch_s.
- landed_chain / deferral: `start_when` waits (a `landed:#<id>` chain or another probe); provider_gate:
  a task held because its provider is logged out or paused. Reported with their cause, no limit.
- checks_slots: `ttp checks` waits are not recorded; the lane shows the project's slot limit, which
  a project's own checks.test_project_max may only lower (testslots.project_max).

A lane is slow when the 90th percentile of its waits is over the threshold: P1_WAIT_S for priority 1
tasks, else the longer of WAIT_S and the median worker run (a wait shorter than one run is normal
while every slot is busy). Each slow lane names its limiting setting and whether that limit was
reached while work waited (`at_limit`) or capacity sat idle (`idle`).

apply() changes only LEVERS, each a key config_set accepts (coordinator.USER_SETTABLE), and only for a
slow lane whose limit was reached while work waited and whose resource has headroom: the provider
gate allows new work and is not the binding cap, no pause, and the host's load leaves CPU free. The
new value comes from load_size() over what was seen (work waiting at the limit, slots, hosts), never a
fixed step. Spend caps, budgets, provider gates, locks, holds, pauses, restrictions and review-before-
push are never touched (safe()). Each change is kept in KV with its reason and undo command
(`ttp throughput --undo <id>`), which the daily review and `ttp status` show."""
from __future__ import annotations

import json
import os
import re
import time

from .db import DB, deferral

KV = "throughput_changes"   # kv: [{"id", "ts", "key", "old", "new", "why", "undo", "trigger", "undone"}], newest last
LAST_KV = "throughput_last"  # kv: the last measured review: {"ts", "trigger", "lanes", "changes", "kept"}
KEEP_S = 14 * 86400
DAY = 86400
P1_WAIT_S = 600       # a priority 1 task waiting minutes is slow
WAIT_S = 1800         # any other task: tens of minutes, or one median run if runs are longer
CPU_FREE = 0.75       # headroom: 1-minute load under this share of the host's cores
ROWS = 5000
# The settings apply() may change: each sizes how soon queued work starts and is accepted by config_set.
LEVERS = ("budget.max_parallel_workers", "review.sweep_after_s", "review.unreviewed_per_pass",
          "coordinator.review_stall_s", "delivery.push_batch_s")
# Keys never changed here, whatever LEVERS says.
FORBIDDEN = re.compile(r"usd|spend|cap\b|_cap|gate|lock|hold|pause|restrict|review\.auto|before_push|reserve"
                       r"|credit|provider|budget\.(?!max_parallel_workers$)", re.I)
FLOORS = {"review.sweep_after_s": 300, "coordinator.review_stall_s": 1800, "delivery.push_batch_s": 60}


def safe(key: str) -> bool:
    return key in LEVERS and not FORBIDDEN.search(key)


def load_size(current: int, waiting: int, units: int) -> int:
    """The next value of a count limit, from load: `current` plus the work seen waiting at it, at most
    doubling, never above `units` (the cores, hosts or slots the work spreads over) unless current
    already is; never below current."""
    current = max(1, int(current))
    step = max(1, min(int(waiting), current))
    return max(current, min(current + step, int(units)))


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * len(s) + 0.5)) - 1))] if s else 0.0


def _labels(t: dict) -> list:
    try:
        v = json.loads(t.get("labels") or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def _deps(t: dict) -> list[int]:
    try:
        return [int(d) for d in json.loads(t.get("depends_on") or "[]") if str(d).isdigit()]
    except (ValueError, TypeError):
        return []


def _reviewed(t: dict) -> int | None:
    """The task a review task reviews: its parent, its `auto_review:<id>` label or its one dependency."""
    if t.get("parent"):
        return int(t["parent"])
    for lb in _labels(t):
        if isinstance(lb, str) and lb.startswith("auto_review:") and lb[12:].isdigit():
            return int(lb[12:])
    deps = _deps(t)
    return deps[0] if len(deps) == 1 else None


def _peak(spans: list[tuple[float, float]], t0: float, t1: float) -> int:
    """The most spans running at once at any point of [t0, t1]."""
    pts = [t0] + [s for s, _ in spans if t0 < s <= t1]
    return max((sum(1 for s, e in spans if s <= x < e) for x in pts), default=0)


def _cfg(cfg: dict, dotted: str, default):
    node = cfg
    for k in dotted.split("."):
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


def thresholds(spans: list[tuple[float, float]]) -> tuple[float, float]:
    """(priority 1 threshold, other threshold) in seconds, the latter sized from the median run."""
    durs = [e - s for s, e in spans if e > s]
    return float(P1_WAIT_S), max(float(WAIT_S), _pct(durs, 0.5))


def measure(db: DB, cfg: dict, since_s: float = DAY, now: float | None = None, gates: dict | None = None) -> dict:
    """The waits per lane over the last `since_s` seconds (module doc)."""
    from . import locks, shared
    now = time.time() if now is None else now
    t0 = now - since_s
    runs = db.q("SELECT task, started, ended, status FROM runs WHERE role!='coordinator' AND started IS NOT NULL "
                "AND (ended IS NULL OR ended>?) ORDER BY started DESC LIMIT ?", (t0 - DAY, ROWS))
    spans = [(float(r["started"]), float(r["ended"] or now)) for r in runs]
    first: dict[int, float] = {}
    last_end: dict[int, list[float]] = {}
    for r in runs:
        if r["task"] is None:
            continue
        first[r["task"]] = min(first.get(r["task"], r["started"]), r["started"])
        if r["ended"]:
            last_end.setdefault(r["task"], []).append(float(r["ended"]))
    p1_s, other_s = thresholds([sp for sp in spans if sp[0] >= t0])
    devices = locks.device_locks(cfg)
    all_gates = gates if gates is not None else (db.kv("gates") or {})
    gate = all_gates.get(cfg.get("core_provider", "claude")) or {} if isinstance(all_gates, dict) else {}
    workers = int(_cfg(cfg, "budget.max_parallel_workers", 6) or 6)
    lanes: dict[str, dict] = {}

    def lane(name: str, limit_key: str | None, limit) -> dict:
        return lanes.setdefault(name, {"lane": name, "limit_key": limit_key, "limit": limit, "waits": [],
                                       "p1": [], "at_limit": 0, "idle": 0, "waiting_now": 0, "measured": True})

    def add(ln: dict, w: float, prio: int, start: float, end: float, limit: int | None, queued: bool) -> None:
        ln["waits"].append(w)
        if prio <= 1:
            ln["p1"].append(w)
        ln["waiting_now"] += queued
        if limit and w > (p1_s if prio <= 1 else other_s):
            ln["at_limit" if _peak(spans, start, end) >= limit else "idle"] += 1

    tasks = db.q("SELECT * FROM tasks WHERE (created>? OR status IN ('queued','blocked')) ORDER BY id DESC LIMIT ?",
                 (t0, ROWS))
    for t in tasks:
        start = first.get(t["id"])
        if start is None and t["status"] not in ("queued", "blocked"):
            continue
        if start is not None and start < t0:
            continue
        d = deferral(t)
        ready = max(float(t["created"] or now), float(d.get("after") or 0), float(t.get("not_before") or 0))
        for dep in _deps(t):
            ends = [e for e in last_end.get(dep, []) if start is None or e <= start]
            ready = max([ready, *ends])
        end = start if start is not None else now
        prio, queued = int(t["priority"] or 3), start is None
        why = (t.get("blocked_reason") or "").lower()
        res = sorted({lb.split(":", 1)[1] for lb in _labels(t) if isinstance(lb, str) and ":" in lb
                      and lb.split(":", 1)[0] in ("resource", "exclusive")})
        when = str(d.get("when") or "")
        if when:
            ln = lane("landed_chain" if when.strip().startswith("landed:#") else "deferral", None, None)
            add(ln, max(0.0, end - float(d.get("since") or t["created"] or now)), prio, ready, end, None, queued)
        elif queued and ("logged out" in why or "provider" in why):
            add(lane("provider_gate", None, None), max(0.0, end - ready), prio, ready, end, None, queued)
        elif res:
            for r in res:
                lim = shared.own_slots(cfg, r)
                ln = lane(("device:" if r in devices else "resource:") + r, None if r in devices else f"resources.{r}", lim)
                ln["note"] = ("a device lock is one slot; its box's headroom is not measurable here" if r in devices
                              else "a resource's slots size a shared machine; its headroom is not measurable here")
                add(ln, max(0.0, end - ready), prio, ready, end, None, queued)
        else:
            add(lane("worker_slots", "budget.max_parallel_workers", workers), max(0.0, end - ready), prio, ready,
                end, workers, queued)
    # Reviews: a code task's last run ending to its review queued (sweep) and started (stall).
    sweep_s = float(_cfg(cfg, "review.sweep_after_s", 1800) or 0)
    per_pass = int(_cfg(cfg, "review.unreviewed_per_pass", 2) or 0)
    stall_s = float(_cfg(cfg, "coordinator.review_stall_s", 14400) or 0)
    rs, rt = lane("review_sweep", "review.sweep_after_s", sweep_s), lane("review_stall", "coordinator.review_stall_s", stall_s)
    for t in tasks:
        target = _reviewed(t) if t["kind"] == "review" else None
        if not target or float(t["created"] or 0) < t0:
            continue
        ends = [e for e in last_end.get(target, []) if e <= float(t["created"])]
        if not ends:
            continue
        done, prio = max(ends), int(t["priority"] or 3)
        add(rs, float(t["created"]) - done, prio, done, float(t["created"]), None, False)
        start = first.get(t["id"])
        add(rt, (start if start is not None else now) - done, prio, done, start or now, None, start is None)
    backlog = db.one("SELECT COUNT(*) n FROM tasks t WHERE t.status='review' AND t.kind!='review' AND NOT EXISTS "
                     "(SELECT 1 FROM tasks r WHERE r.kind='review' AND (r.parent=t.id OR r.labels LIKE '%\"auto_review:'||t.id||'\"%') "
                     "AND r.status IN "
                     "('queued','running','waiting','blocked'))")["n"]
    rs["backlog"], rs["per_pass"] = backlog, per_pass
    # Push queue: approval to its batch's start.
    push_s = float(_cfg(cfg, "delivery.push_batch_s", 300) or 0)
    pb = lane("push_batch", "delivery.push_batch_s", push_s)
    for r in db.q("SELECT q.created c, b.started s FROM push_queue q LEFT JOIN push_batches b ON b.id=q.batch "
                  "WHERE q.created>? LIMIT ?", (t0, ROWS)):
        add(pb, max(0.0, float(r["s"] or now) - float(r["c"])), 3, float(r["c"]), float(r["s"] or now), None, r["s"] is None)
    try:
        from . import testslots
        ck = testslots.project_max(cfg)
    except Exception:
        ck = None
    lane("checks_slots", "checks.test_project_max", ck).update(
        measured=False, note="checks waits are not recorded; a project's checks.test_project_max may only lower "
                             "the account's limit (an account setting, shared by every project on the host)")
    peak = _peak(spans, t0, now)
    out = []
    for ln in lanes.values():
        w, p1 = ln.pop("waits"), ln.pop("p1")
        ln.update(count=len(w), median_s=round(_pct(w, 0.5)), p90_s=round(_pct(w, 0.9)),
                  p1_count=len(p1), p1_p90_s=round(_pct(p1, 0.9)))
        ln["slow"] = bool(ln["measured"] and ((p1 and _pct(p1, 0.9) > p1_s) or (w and _pct(w, 0.9) > other_s)))
        if ln["lane"] == "worker_slots":
            ln["peak"] = peak
            if gate and int(gate.get("max_parallel") or workers) < workers:
                ln["gate_max"] = int(gate.get("max_parallel"))
        out.append(ln)
    out.sort(key=lambda x: (not x["slow"], -x["p90_s"]))
    return {"ts": now, "since_s": since_s, "thresholds_s": {"p1": p1_s, "other": round(other_s)}, "lanes": out,
            "gate": {k: gate.get(k) for k in ("level", "allow_new_work", "max_parallel")} if gate else {}}


def cpu_free() -> tuple[bool, str]:
    """(CPU free, why not): the host's 1-minute load under CPU_FREE of its cores."""
    try:
        load, cores = os.getloadavg()[0], os.cpu_count() or 1
    except OSError:
        return True, ""
    if load >= CPU_FREE * cores:
        return False, f"host load {load:.1f} on {cores} cores leaves no CPU free"
    return True, ""


def headroom(db: DB, cfg: dict, report: dict) -> tuple[bool, str]:
    """(may raise, why not): the provider gate allows new work, no provider or resource pause."""
    g = report.get("gate") or {}
    if g and (g.get("level") == "red" or g.get("allow_new_work") is False):
        return False, f"the provider gate is {g.get('level')} (spend or plan window): never raised here"
    lim = db.kv(f"limited:{cfg.get('core_provider', 'claude')}") or {}
    if isinstance(lim, dict) and float(lim.get("until") or 0) > time.time():
        return False, "the provider is paused"
    if db.paused_resources():
        return False, "a resource is paused"
    return True, ""


def plan(db: DB, cfg: dict, report: dict) -> tuple[list[dict], list[dict]]:
    """(changes [{key, old, new, why}], kept [{lane, key, why}]) for the slow lanes."""
    ok, why_not = headroom(db, cfg, report)
    changes, kept = [], []
    th = report["thresholds_s"]
    for ln in report["lanes"]:
        if not ln["slow"]:
            continue
        key, cur, name = ln.get("limit_key"), ln.get("limit"), ln["lane"]
        p90 = max(ln["p90_s"], ln["p1_p90_s"])
        if not key or not safe(key):
            kept.append({"lane": name, "key": key, "why": ln.get("note") or (
                f"{name} has no setting this may change: its cause is outside the project's sizing limits"
                if not key else f"{key} is not a sizing limit this changes: needs a person or a task")})
            continue
        if not ok:
            kept.append({"lane": name, "key": key, "why": why_not})
            continue
        if name == "worker_slots":
            if ln.get("gate_max"):
                kept.append({"lane": name, "key": key, "why": f"the provider gate holds parallel work at "
                             f"{ln['gate_max']} (spend or plan window), below {cur}: never raised here"})
            elif not (cpu := cpu_free())[0]:
                kept.append({"lane": name, "key": key, "why": cpu[1]})
            elif not ln["at_limit"]:
                kept.append({"lane": name, "key": key, "why": f"slots sat idle while work waited ({ln['idle']} waits): "
                             "the cause is not the slot count"})
            else:
                new = load_size(cur, max(ln["at_limit"], ln["waiting_now"]), max(cur, (os.cpu_count() or 2) // 2))
                if new > cur:
                    changes.append({"key": key, "old": cur, "new": new, "why": f"{ln['at_limit']} wait(s) over threshold "
                                    f"while all {cur} slots ran (peak {ln.get('peak')}), p90 {p90}s"})
                else:
                    kept.append({"lane": name, "key": key, "why": f"already at the host's share of cores ({cur})"})
        elif name == "review_sweep":
            per = ln.get("per_pass") or 0
            if per and ln.get("backlog", 0) > per:
                new = load_size(per, ln["backlog"] - per, ln["backlog"])
                changes.append({"key": "review.unreviewed_per_pass", "old": per, "new": new,
                                "why": f"{ln['backlog']} task(s) in review without one, {per} per pass; p90 {p90}s"})
            if cur and cur > FLOORS[key] and p90 > cur:
                changes.append({"key": key, "old": int(cur), "new": int(max(FLOORS[key], cur // 2)),
                                "why": f"reviews queued p90 {p90}s after the work ended, sweep every {int(cur)}s"})
        elif name in ("review_stall", "push_batch"):
            target = max(FLOORS[key], int(cur // 2), int(th["other"]) if name == "review_stall" else 0)
            if cur and int(cur) > target and (name == "push_batch" or p90 < cur):
                changes.append({"key": key, "old": int(cur), "new": target,
                                "why": f"p90 {p90}s over threshold" + (f", under the {int(cur)}s alarm, so no "
                                       "stall was raised" if name == "review_stall" else f" with batches waiting {int(cur)}s")})
            else:
                kept.append({"lane": name, "key": key, "why": f"{key} is at its floor or not what held the waits"})
    return [c for c in changes if safe(c["key"])], kept


def apply(p, cfg: dict, report: dict, trigger: str = "", now: float | None = None) -> tuple[list[dict], list[dict]]:
    """Make plan()'s changes through Project.set_config and record each in KV; (changes, kept)."""
    from .coordinator import settable
    now = time.time() if now is None else now
    changes, kept = plan(p.db, cfg, report)
    rec = [r for r in (p.db.kv(KV) or []) if isinstance(r, dict) and now - float(r.get("ts") or 0) < KEEP_S]
    nid = max([int(r.get("id") or 0) for r in (p.db.kv(KV) or []) if isinstance(r, dict)] + [0]) + 1
    done = []
    for c in changes:
        try:
            val = settable(c["key"])(c["new"])
            p.set_config(c["key"], val)
        except Exception as e:
            kept.append({"lane": c["key"], "key": c["key"], "why": f"could not set: {e}"[:200]})
            continue
        c.update(id=nid, ts=now, new=val, trigger=trigger, undone=False, undo=f"ttp throughput --undo {nid}")
        rec.append(c)
        done.append(c)
        nid += 1
    p.db.set_kv(KV, rec)
    return done, kept


def undo(p, cid: int) -> str:
    """Set change `cid`'s key back to its old value, unless something else changed it since."""
    rec = p.db.kv(KV) or []
    c = next((r for r in rec if isinstance(r, dict) and int(r.get("id") or 0) == cid), None)
    if not c:
        return f"no throughput change #{cid}"
    if c.get("undone"):
        return f"#{cid} was already undone"
    if not safe(c["key"]):
        return f"#{cid}: {c['key']} is not a throughput lever"
    cur = _cfg(p.config(), c["key"], None)
    if cur != c["new"]:
        return f"#{cid}: {c['key']} is {cur!r} now, not {c['new']!r}: left as it is"
    p.set_config(c["key"], c["old"])
    c["undone"] = time.time()
    p.db.set_kv(KV, rec)
    return f"#{cid}: {c['key']} back to {c['old']!r}"


def recent(db: DB, now: float | None = None, within: float = DAY) -> list[dict]:
    now = time.time() if now is None else now
    return [r for r in db.kv(KV) or [] if isinstance(r, dict) and now - float(r.get("ts") or 0) < within]


def review(p, cfg: dict, since_s: float = DAY, do_apply: bool = False, trigger: str = "",
           gates: dict | None = None, now: float | None = None) -> dict:
    """measure(), then apply() when asked; kept as LAST_KV. Changes of the last day are listed too."""
    now = time.time() if now is None else now
    rep = measure(p.db, cfg, since_s, now, gates)
    earlier = recent(p.db, now)
    if do_apply:
        rep["changes"], rep["kept"] = apply(p, cfg, rep, trigger, now)
    else:
        rep["would_change"], rep["kept"] = plan(p.db, cfg, rep)
        rep["changes"] = []
    rep["earlier_changes"] = earlier
    rep["trigger"] = trigger
    p.db.set_kv(LAST_KV, {k: rep[k] for k in ("ts", "trigger", "changes", "kept", "earlier_changes")}
                | {"slow": [ln["lane"] for ln in rep["lanes"] if ln["slow"]]})
    return rep


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"


def text(rep: dict) -> str:
    """The review as plain lines (`ttp throughput`, the coordinator's wake, the daily review)."""
    th = rep["thresholds_s"]
    out = [f"Throughput over the last {_dur(rep['since_s'])} (slow: p90 over {_dur(th['p1'])} for priority 1, "
           f"{_dur(th['other'])} otherwise):"]
    for ln in rep["lanes"]:
        if not ln["count"] and not ln["measured"]:
            out.append(f"- {ln['lane']}: limit {ln['limit_key']}={ln['limit']}; {ln.get('note', '')}")
            continue
        if not ln["count"]:
            continue
        extra = "".join(f", {k} {ln[k]}" for k in ("peak", "gate_max", "backlog") if ln.get(k) is not None)
        out.append(f"- {ln['lane']}{' SLOW' if ln['slow'] else ''}: {ln['count']} wait(s), median {_dur(ln['median_s'])}, "
                   f"p90 {_dur(ln['p90_s'])}" + (f" (priority 1: {ln['p1_count']}, p90 {_dur(ln['p1_p90_s'])})"
                                                 if ln["p1_count"] else "")
                   + (f"; limit {ln['limit_key']}={ln['limit']}" if ln.get("limit_key") else "; no limit")
                   + (f"; over threshold at limit {ln['at_limit']}, with capacity idle {ln['idle']}"
                      if ln["at_limit"] or ln["idle"] or ln["lane"] == "worker_slots" else "")
                   + (f", {ln['waiting_now']} waiting now" if ln["waiting_now"] else "") + extra)
    for c in rep.get("changes") or []:
        out.append(f"Changed #{c['id']}: {c['key']} {c['old']} -> {c['new']} ({c['why']}); undo: {c['undo']}")
    for c in rep.get("would_change") or []:
        out.append(f"Would change: {c['key']} {c['old']} -> {c['new']} ({c['why']}); run with --apply")
    for k in rep.get("kept") or []:
        out.append(f"Not changed: {k['lane']} ({k['key'] or 'no setting'}): {k['why']}")
    for c in rep.get("earlier_changes") or []:
        out.append(f"Earlier (last 24 h) #{c.get('id')}: {c.get('key')} {c.get('old')} -> {c.get('new')}"
                   + (" (undone)" if c.get("undone") else "") + f"; undo: {c.get('undo')}")
    if not any(ln["slow"] for ln in rep["lanes"]):
        out.append("No lane is slow.")
    return "\n".join(out)


def status_line(db: DB, now: float | None = None) -> str:
    """One `ttp status` line for the throughput changes of the last day, or ''."""
    rec = [r for r in recent(db, now) if not r.get("undone")]
    if not rec:
        return ""
    return "throughput: " + "; ".join(f"#{r['id']} {r['key']} {r['old']}->{r['new']} (undo: {r['undo']})"
                                      for r in rec[-4:])

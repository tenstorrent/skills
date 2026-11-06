# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Queue waits over coordinator.queue_wait_escalate_min (default 30, 0: off) become must-act items.

Model-free, each minute (Daemon.check_queue_waits): collect() finds every task of the project waiting on a
queue, cap or admission: a host-wide checks slot (checkcap.state, queued or parked) or its own detached checks
it polls (`ttp checks --result`, 'next try ...'), the push queue (`ttp push`), worker capacity (the next ready
task, never started while every worker slot is taken), a device or other `ttp lock` resource, an outside batch pool
(`ttp pool-wait`, Slurm) or CPU admission (a 'waiting for' reason naming CPU or load). Each task and queue
waiting past the threshold raises one must-act event (KIND, mustact.py): the queue, its cap and occupancy,
who holds and waits for each place, the wait so far, and options() computed from config and host state,
leaving out every route a user pause, a cap the user set or missing config forbids. Restrictions still bind.
The coordinator acts in that turn (mustact.acted: a config_set, a task_add or task_update naming the item's
key or updating the task, or a reply or notify naming the key and the safety rule that holds it). Not acted
on, the next threshold mark raises it again (fingerprint per mark). With no option left the user is told
once. Once the wait ends its open events are settled. daily_line() is the daily review's metric."""
from __future__ import annotations

import json
import re
import time

from . import msgparts, obligations

KIND = "queue_wait"
KV = "queue_wait"   # {"open": {episode: {task, queue, since, acted}}, "hist": [ended episodes], "told": [episode]}
DEFAULT_MIN = 30.0
HIST_KEEP_S = 7 * 86400.0
HIST_MAX = 300
MAX_RAISE = 3   # new items per scan, so a long queue never floods the coordinator with wakes
NAMES = {"checks": "checks queue", "push": "push queue", "workers": "worker slots", "device": "device queue",
         "lock": "lock", "pool": "outside batch pool", "cpu": "CPU admission"}


def threshold_s(cfg: dict) -> float:
    try:
        return max(0.0, float((cfg.get("coordinator") or {}).get("queue_wait_escalate_min", DEFAULT_MIN) or 0)) * 60
    except (TypeError, ValueError):
        return DEFAULT_MIN * 60


def classify(probe: str, reason: str, device: set | frozenset = frozenset()) -> tuple[str, str] | None:
    """(queue, resource) a waiting task's probe or blocked reason waits on; None for any other wait."""
    pr, r = str(probe or ""), str(reason or "").strip().lower()
    if re.search(r"\bttp checks\b[^;|&]*--result\b", pr) or "next try" in r:
        return "checks", ""
    if re.search(r"\bttp push\b", pr):
        return "push", ""
    if m := re.search(r"\bttp lock --probe\s+['\"]?([\w.,:/-]+)", pr):
        return ("device" if set(m[1].split(",")) & set(device) else "lock"), m[1]
    if re.search(r"\bttp devq\b", pr):
        return "device", ""
    if re.search(r"\bpool-wait\b|\bsqueue\b|\bsacct\b", pr):
        return "pool", ""
    if r.startswith("waiting for") and re.search(r"\bcpu\b|admission|\bload\b", r):
        return "cpu", ""
    if m := re.match(r"waiting for (?:resource )?([\w.:/-]+)", r):
        return ("device" if m[1] in device else "lock"), m[1]
    return None


def _task_of(label: str, project: str) -> int | None:
    m = re.match(rf"{re.escape(project)} task #(\d+)\b", label or "")
    return int(m[1]) if m else None


def collect(p, cfg: dict, now: float, host: dict | None = None, device: set | None = None) -> list[dict]:
    """The project's queue waits now: {task, queue, res, since, cap, running, queued}, one per task and queue."""
    from .db import load_result
    db, name, out = p.db, p.name, {}
    host = host or {}
    device = set(device or ())
    running = list(host.get("running") or []) + list(host.get("priority") or []) + list(host.get("urgent") or [])
    queued = list(host.get("queued") or []) + [h for h, _ in host.get("parked") or []]
    checks = {"cap": host.get("limit"), "running": running, "queued": queued, "cap_set": bool(host.get("limit_set"))}
    busy = {_task_of(x, name) for x in running}

    def add(tid, queue, res, since, info):
        k = (tid, queue)
        if since is None or (k in out and out[k]["since"] <= since):
            return
        out[k] = {"task": tid, "queue": queue, "res": res, "since": float(since), **info}

    for h, at in [*zip(host.get("queued") or [], host.get("queued_at") or []), *(host.get("parked") or [])]:
        if (tid := _task_of(h, name)) is not None and at:
            add(tid, "checks", "", at, checks)
    for t in db.q("SELECT id, blocked_reason, result FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
        prev = load_result(t["result"])
        if prev.get("status") != "waiting" or not isinstance(prev.get("waiting_since"), (int, float)):
            continue
        got = classify(prev.get("retry_when") or "", t["blocked_reason"] or "", device)
        if got and not (got[0] == "checks" and t["id"] in busy):
            add(t["id"], got[0], got[1], prev["waiting_since"],
                checks if got[0] == "checks" else _holders(db, p, got[0], got[1], cfg))
    cap = int((cfg.get("budget") or {}).get("max_parallel_workers", 6) or 0)
    run = db.q("SELECT r.task, t.priority FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role!='coordinator' "
               "AND r.status='running'")
    if cap and len(run) >= cap:
        info = {"cap": cap, "running": [f"{name} task #{r['task']} (priority {r['priority']})" for r in run],
                "queued": []}
        for t in db.ready_tasks()[:1]:   # the head of the line: more capacity unblocks every one behind it
            last = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (t["id"],))
            add(t["id"], "workers", "", max(float(t["created"] or now), float((last or {}).get("e") or 0),
                                            float(t["not_before"] or 0)), info)
    return list(out.values())


def _holders(db, p, queue: str, res: str, cfg: dict) -> dict:
    if queue == "push":
        rows = db.q("SELECT task, status FROM push_queue WHERE status IN ('approved','pushing') ORDER BY id")
        return {"cap": 1, "running": [f"{p.name} task #{r['task']}" for r in rows if r["status"] == "pushing"],
                "queued": [f"{p.name} task #{r['task']}" for r in rows if r["status"] == "approved"]}
    if queue in ("device", "lock") and res:
        from . import locks
        try:
            held = [h for h in locks.held(p.state / "locks", p) if h.split(":", 1)[0] in res.split(",")]
        except OSError:
            held = []
        return {"cap": None, "running": held, "queued": []}
    return {"cap": None, "running": [], "queued": []}


def options(db, w: dict, cfg: dict, paused: dict, device: set | None = None, cores: int = 0) -> list[str]:
    """Concrete unblock options for wait `w`, from config and host state; none that a user pause, a cap the
    user set or missing config forbids. Empty: only the user can clear it."""
    from . import configwho
    t = db.one("SELECT priority, origin FROM tasks WHERE id=?", (w["task"],)) or {}
    res = [r for r in str(w.get("res") or "").split(",") if r]
    if any((paused.get(r) or {}).get("by") == "user" for r in res):
        return []
    user_set = lambda key: configwho.role(configwho.get(db, key)) == "user"   # noqa: E731
    out, q = [], w["queue"]
    if q in ("checks", "cpu", "pool"):
        from . import checkspill
        hosts = checkspill.hosts(cfg)
        if hosts and not user_set("delivery.checks_spill_after_s"):
            out.append(f"spill to an allowed check host ({', '.join(hosts[:3])}): config_set "
                       f"delivery.checks_spill_after_s lower so it spills sooner")
        cap = w.get("cap")
        top = cores // 8 if cores else 0   # checkcap.CORES_PER_SLOT_MAX: the host's limit
        if q == "checks" and isinstance(cap, int) and 0 < cap < top and not w.get("cap_set"):
            out.append(f"raise the host-wide checks cap from {cap} to {min(top, cap + 2)} (host limit {top}): "
                       f"`ttp config --account checks.host_max {min(top, cap + 2)}`")
        if sum(1 for x in w.get("queued") or [] if x.startswith(f"{w.get('project')} task #")) > 1:
            out.append("cut the stale queued tail: cancel this project's older or superseded queued checks runs")
    if q == "workers":
        cap = int(w.get("cap") or 0)
        top = max(1, cores // 4) if cores else 0
        if not user_set("budget.max_parallel_workers") and cap and cap < top:
            out.append(f"raise budget.max_parallel_workers from {cap} to {cap + 1} (host limit {top}): config_set")
        out.append("cut the stale queued tail: cancel queued tasks a newer order superseded")
    if q in ("device", "lock"):
        other = sorted(set(device or ()) - set(res) - {k for k, v in paused.items() if v})
        if other:
            out.append(f"route to another allowed resource ({', '.join(other[:3])}): task_update resources")
    if int(t.get("priority") or 3) > 1:
        out.append(f"let it go ahead of routine work: task_update {w['task']} priority 1")
    if t.get("origin") != "user":
        out.append(f"cancel it if it is no longer needed: task_update {w['task']} status cancelled")
    return out


def key(w: dict) -> str:
    return f"qwait-{w['task']}-{w['queue']}"


def text(w: dict, opts: list[str], now: float) -> str:
    cap = w.get("cap")
    run, que = list(w.get("running") or []), list(w.get("queued") or [])
    some = lambda xs: "; ".join(xs[:4]) + ("; ..." if len(xs) > 4 else "") or "none seen"   # noqa: E731
    head = (f"{key(w)}: #{w['task']} has waited {(now - w['since']) / 60:.0f} min on the "
            f"{NAMES.get(w['queue'], w['queue'])}{' ' + w['res'] if w.get('res') else ''}: {len(run)} running"
            f" (cap {cap if cap else 'unknown'}), {len(que)} queued. Holding: {some(run)}. Waiting: {some(que)}.")
    if not opts:
        return head + " No option is open to the project (a user pause or a cap the user set holds it): the user is told."
    return head + " Options: " + " | ".join(f"({i}) {o}" for i, o in enumerate(opts, 1))


def scan(p, cfg: dict, now: float | None = None, host: dict | None = None, device: set | None = None,
         cores: int = 0) -> list[str]:
    """Raise, re-raise and clear queue-wait items; returns the fingerprints raised."""
    now = time.time() if now is None else now
    db, thr = p.db, threshold_s(cfg)
    if thr <= 0:
        return []
    waits = sorted((w for w in collect(p, cfg, now, host, device) if now - w["since"] >= thr), key=lambda w: w["since"])
    paused = db.paused_resources()
    raised, live = [], set()
    st = db.kv(KV) or {}
    opened, told = st.setdefault("open", {}), st.setdefault("told", [])
    for w in waits:
        ep = f"{w['task']}:{w['queue']}:{w['since']:.0f}"
        live.add(ep)
        rec = opened.setdefault(ep, {"task": w["task"], "queue": w["queue"], "since": w["since"], "acted": None})
        if rec.get("acted"):
            continue
        k, mark = key(w), int((now - w["since"]) // thr)
        if db.one("SELECT id FROM events WHERE kind=? AND status='queued' AND data LIKE ?", (KIND, f'%"key": "{k}"%')):
            continue
        opts = options(db, {**w, "project": p.name}, cfg, paused, device, cores)
        msg = text(w, opts, now)
        data = {"must_act": {"key": k, "chat": "all", "tasks": [w["task"]], "kind": KIND, "episode": ep}}
        fp = f"{KIND}:{ep}:{mark}"
        if len(raised) < MAX_RAISE and obligations._raise(db, now, fp, KIND, msg, w["task"], data):
            raised.append(fp)   # the longest waits first; the rest at a later scan
        if not opts and ep not in told:
            told.append(ep)
            msgparts.post(db, f"#{w['task']} has waited {(now - w['since']) / 60:.0f} min on the "
                              f"{NAMES.get(w['queue'], w['queue'])} and only you can clear it: {msg}",
                          kind="info", severity="normal")
    for ep in [e for e in opened if e not in live]:
        rec = opened.pop(ep)
        rec["end"] = now
        st.setdefault("hist", []).append(rec)
        db.x("UPDATE events SET status='handled' WHERE kind=? AND status='queued' AND data LIKE ?",
             (KIND, f'%"episode": "{ep}"%'))
    st["hist"] = [h for h in st.get("hist", []) if now - float(h.get("end") or now) <= HIST_KEEP_S][-HIST_MAX:]
    st["told"] = [e for e in told if e in opened][-HIST_MAX:]
    db.set_kv(KV, st)
    return raised


def record_act(db, ep: str, now: float | None = None) -> None:
    """The coordinator acted on the item of episode `ep`: no more raises for this wait; the metric's mark."""
    with db.tx():
        st = db.kv(KV) or {}
        rec = (st.get("open") or {}).get(ep)
        if rec is not None and not rec.get("acted"):
            rec["acted"] = time.time() if now is None else now
            db.set_kv(KV, st)


def daily_line(db, cfg: dict | None = None, now: float | None = None, window_s: float = 86400.0) -> str:
    """queue_wait_over: waits past the threshold in the window (ended or open), max and p95 wait, and the
    minutes from the threshold mark to the first unblock action."""
    now = time.time() if now is None else now
    thr = threshold_s(cfg or {}) or DEFAULT_MIN * 60
    st = db.kv(KV) or {}
    eps = [h for h in st.get("hist", []) if now - float(h.get("end") or now) <= window_s] \
        + list((st.get("open") or {}).values())
    eps = [e for e in eps if float(e.get("end") or now) - float(e["since"]) >= thr]
    if not eps:
        return f"queue_wait_over: no queue wait over {thr / 60:g} min in the last {window_s / 3600:g} h"
    waits = sorted((float(e.get("end") or now) - float(e["since"])) / 60 for e in eps)
    p95 = waits[min(len(waits) - 1, int(0.95 * len(waits)))]
    acts = [(float(e["acted"]) - float(e["since"]) - thr) / 60 for e in eps if e.get("acted")]
    by = {}
    for e in eps:
        by[e["queue"]] = by.get(e["queue"], 0) + 1
    acts.sort()
    act = (f"minutes from the mark to the first unblock action: median {acts[len(acts) // 2]:.0f}, max {acts[-1]:.0f}"
           if acts else "no unblock action")
    return (f"queue_wait_over: {len(eps)} queue wait(s) over {thr / 60:g} min in the last {window_s / 3600:g} h "
            f"({', '.join(f'{k} {v}' for k, v in sorted(by.items()))}); max {waits[-1]:.0f} min, p95 {p95:.0f} min; "
            f"{act}; {len(eps) - len(acts)} with none")

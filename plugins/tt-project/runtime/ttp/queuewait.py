# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Queue waits over coordinator.queue_wait_escalate_min (default 30, 0: off) become must-act items.

Model-free, each minute (Daemon.check_queue_waits): collect() finds every task of the project waiting on a
queue, cap or admission: a host-wide checks slot (checkcap.state, queued or parked) or its own detached checks
it polls while queued (`ttp checks --result`, or a reason saying so), the push queue (`ttp push`), worker capacity (the next ready
task dispatch holds only because its provider's live worker runs fill every slot it may take; a review held by
its daemon pre-checks waiting for a host slot is a checks wait), a device or other `ttp lock` resource, an outside
batch pool (`ttp pool-wait`, Slurm) or CPU admission (a reason naming admission and CPU or load). Each task and queue
waiting past the threshold raises one must-act event (KIND, mustact.py): the queue, its cap and occupancy,
who holds and waits for each place (rewritten each scan while the item is unread, so it names the holders of
the moment it is read), the wait so far, and options() computed from config and host state,
leaving out every route a user pause, a cap the user set or missing config forbids. Restrictions still bind.
A wait whose cause an open task already owns (an active throughput-class mute's owner, or the fix task an earlier
item on the same queue was acted on with) folds into it: one act per queue cause, not per waiting task.
The coordinator acts in that turn (mustact.acted: a config_set, a task_add or task_update naming the item's
key or updating the task, or a reply or notify naming the key and the safety rule that holds it). A task and
queue is one episode (`<task>:<queue>`) from its earliest start, so a start that flips between two sources
never reopens it; it ends only after GRACE_S with no sign of the wait. Not acted on, it is raised again only
when its cause (queue, resource, host-held, options left) changes or the wait has doubled since the last raise.
A checks wait the host-wide checks slots hold (host_held: no coordinator action lifts it) is raised at normal
and rides along (pagesev.RIDE_ALONG). With no option left the user is told once. Once the episode ends its
open events are settled. daily_line() is the daily review's metric.
Each event's data carries `fold` (FOLD): the parts the coordinator's NEW EVENTS joins into one line per queue."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import msgparts, obligations

KIND = "queue_wait"
KV = "queue_wait"   # {"open": {episode: {task, queue, since, opened, seen, acted, raise}}, "hist": [ended], "told": [episode]}
DEFAULT_MIN = 30.0
GRACE_S = 1800.0   # a wait not seen this long ends its episode; a shorter gap is the same wait
HIST_KEEP_S = 7 * 86400.0
HIST_MAX = 300
MAX_RAISE = 3   # new items per scan, so a long queue never floods the coordinator with wakes
FOLD = "fold"   # in event data: {"group", "item", "ctx", "opts"} (coordinator.fold_events)
NAMES = {"checks": "checks queue", "push": "push queue", "workers": "worker slots", "device": "device queue",
         "lock": "lock", "pool": "outside batch pool", "cpu": "CPU admission"}
# Words other observations use for a queue, so a mute of one (screen.mutes) matches its waits.
ALIASES = {"checks": ("host-wide checks slot", "checks slot", "checks queue")}


def threshold_s(cfg: dict) -> float:
    try:
        return max(0.0, float((cfg.get("coordinator") or {}).get("queue_wait_escalate_min", DEFAULT_MIN) or 0)) * 60
    except (TypeError, ValueError):
        return DEFAULT_MIN * 60


# A worker's own checks waiting for a slot, as its `ttp checks --result` probe's line says it (cli.checks_result).
CHECKS_WAIT_RE = re.compile(r"\bttp checks: (?:queued|waiting)\b|queued for a host-wide checks slot")


def classify(probe: str, reason: str, device: set | frozenset = frozenset()) -> tuple[str, str] | None:
    """(queue, resource) a waiting task's probe or blocked reason waits on; None for any other wait. The probe
    decides first; the reason only by its own words, never by the daemon's '; next try <time>' tail, which
    every waiting task's reason ends with."""
    from .sweepskip import NEXT_TRY
    pr, r = str(probe or ""), NEXT_TRY.sub("", str(reason or "").strip()).lower()
    if re.search(r"\bttp push\b", pr):
        return "push", ""
    if m := re.search(r"\bttp lock --probe\s+['\"]?([\w.,:/-]+)", pr):
        return ("device" if set(m[1].split(",")) & set(device) else "lock"), m[1]
    if re.search(r"\bttp devq\b", pr):
        return "device", ""
    if re.search(r"\bpool-wait\b|\bsqueue\b|\bsacct\b", pr):
        return "pool", ""
    if re.search(r"\bttp checks\b[^;|&]*--result\b", pr):
        return None if "ttp checks: running" in r else ("checks", "")   # running: holds its slot, no queue wait
    if CHECKS_WAIT_RE.search(r):
        return "checks", ""
    if re.search(r"\badmission\b", r) and re.search(r"\bcpu\b|\bcores?\b|\bload\b", r):
        return "cpu", ""
    return None


def _task_of(label: str, project: str) -> int | None:
    m = re.match(rf"{re.escape(project)} task #(\d+)\b", label or "")
    return int(m[1]) if m else None


def collect(p, cfg: dict, now: float, host: dict | None = None, device: set | None = None,
            hold=None) -> list[dict]:
    """The project's queue waits now: {task, queue, res, since, cap, running, queued}, one per task and queue.
    `hold(task)` is dispatch's own reason a ready task does not start (Daemon._queue_hold): None (it starts),
    {"queue": "workers", "cap", "provider", "why"} (its provider's live worker runs fill every slot it may
    take), {"queue": "checks", "since"} (a review's daemon pre-checks wait for a host checks slot) or any
    other hold (a pause, a lock, the spend gate: their own alarms name those). Only the first ready task
    held by full slots raises a workers wait; a task parked on its own checks or a detached wait runs no
    worker and never counts as a slot holder. A checks wait needs an open task with a live wait record (its
    label queued or parked on the host, its own run's checks waiting for a slot: _checks_waiting, or dispatch's
    hold) while the host queue has someone waiting: finished checks or 0 queued raise nothing."""
    from .db import TERMINAL_TASK_STATES, load_result
    db, name, out = p.db, p.name, {}
    host = host or {}
    device = set(device or ())
    running = list(host["held"]) if "held" in host else \
        list(host.get("running") or []) + list(host.get("priority") or []) + list(host.get("urgent") or [])
    queued = list(host.get("queued") or []) + [h for h, _ in host.get("parked") or []]
    checks = {"cap": host.get("limit"), "running": running, "queued": queued, "cap_set": bool(host.get("limit_set"))}
    busy = {_task_of(x, name) for x in running}

    def add(tid, queue, res, since, info):
        k = (tid, queue)
        if since is None or (k in out and out[k]["since"] <= since):
            return
        row = db.one("SELECT status FROM tasks WHERE id=?", (tid,))
        if row is None or row["status"] in TERMINAL_TASK_STATES:
            return   # a finished task's label left in the host queue: no wait of this project's to unblock
        out[k] = {"task": tid, "queue": queue, "res": res, "since": float(since), **info}

    for h, at in [*zip(host.get("queued") or [], host.get("queued_at") or []), *(host.get("parked") or [])]:
        if (tid := _task_of(h, name)) is not None and at:
            add(tid, "checks", "", at, checks)
    for t in db.q("SELECT id, blocked_reason, result FROM tasks WHERE status='queued' AND not_before IS NOT NULL"):
        prev = load_result(t["result"])
        if prev.get("status") != "waiting" or not isinstance(prev.get("waiting_since"), (int, float)):
            continue
        got = classify(prev.get("retry_when") or "", t["blocked_reason"] or "", device)
        if got and got[0] == "checks" and not (queued and _checks_waiting(p, prev.get("retry_when"))):
            continue   # its checks finished, run or never queued, or the host queue is empty: no slot to wait for
        if got and not (got[0] == "checks" and t["id"] in busy):
            info = checks if got[0] == "checks" else _holders(db, p, got[0], got[1], cfg, now)
            if not info.pop("live", False):   # a push in its turn, alive and in its time limit: a fair wait
                add(t["id"], got[0], got[1], prev["waiting_since"], info)
    for t in db.ready_tasks():
        got = hold(t) if hold else _config_hold(db, cfg)
        if got is None:
            break   # dispatch starts it: no ready task behind it waits on a slot
        if got.get("queue") == "checks":   # a review held by its daemon pre-checks waiting for a host slot
            if queued:   # 0 queued on the host: the slot is not what holds it
                add(t["id"], "checks", "", got.get("since"), checks)
        elif got.get("queue") == "workers":   # the head of the line: more capacity unblocks every one behind it
            prov = got.get("provider")
            run = db.q("SELECT r.task, t.priority FROM runs r JOIN tasks t ON t.id=r.task WHERE r.role!='coordinator' "
                       "AND r.status='running'" + (" AND r.provider=?" if prov else ""), (prov,) if prov else ())
            last = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (t["id"],))
            add(t["id"], "workers", "", max(float(t["created"] or now), float((last or {}).get("e") or 0),
                                            float(t["not_before"] or 0)),
                {"cap": got.get("cap"), "cap_why": got.get("why") or "", "queued": [],
                 "running": [f"{name} task #{r['task']} (priority {r['priority']})" for r in run]})
            break
    return list(out.values())


def _checks_waiting(p, probe) -> bool:
    """A worker's `ttp checks --result <run dir>` wait has a live record of waiting for a host-wide checks
    slot: no checks.rc yet, its checks process alive (or in a PID namespace this one cannot judge) and its
    checks-slot.wait file present. Read-only (never cli._checks_result, which settles the run). A run whose
    checks finished, run or are gone, or a probe naming no run dir, has none."""
    from . import cli
    m = re.search(r"\bttp checks\b[^;|&]*?--result\s+('[^']*'|\"[^\"]*\"|[^\s;|&]+)", str(probe or ""))
    if not m or "$" in m[1]:
        return False
    d = Path(m[1].strip("'\"")).expanduser()
    d = d if d.is_absolute() else p.base / d
    if (d / cli.CHECKS_RC).exists() or not (d / cli.CHECKS_SLOT_WAIT).exists():
        return False
    info = cli._read_checks_pid(d)
    return bool(info) and (cli._checks_alive(info) or cli._checks_elsewhere(info))


def _config_hold(db, cfg: dict) -> dict | None:
    """Without the daemon's dispatch check (Daemon._queue_hold): dispatch's own rule with no budget gate, every
    live worker run against budget.max_parallel_workers. None: a slot is free."""
    cap = int((cfg.get("budget") or {}).get("max_parallel_workers", 6) or 0)
    n = db.one("SELECT COUNT(*) n FROM runs WHERE role!='coordinator' AND status='running'")["n"]
    return {"queue": "workers", "cap": cap} if cap and n >= cap else None


def _holders(db, p, queue: str, res: str, cfg: dict, now: float | None = None) -> dict:
    if queue == "push":
        rows = db.q("SELECT task, status FROM push_queue WHERE status IN ('approved','pushing') ORDER BY id")
        out = {"cap": 1, "running": [f"{p.name} task #{r['task']}" for r in rows if r["status"] == "pushing"],
               "queued": [f"{p.name} task #{r['task']}" for r in rows if r["status"] == "approved"]}
        _push_turns(p, out, time.time() if now is None else now)
        return out
    if queue in ("device", "lock") and res:
        from . import locks
        try:
            held = [h for h in locks.held(p.state / "locks", p) if h.split(":", 1)[0] in res.split(",")]
        except OSError:
            held = []
        return {"cap": None, "running": held, "queued": []}
    return {"cap": None, "running": [], "queued": []}


PUSH_HOLD_LIMIT_S = 4 * 3600.0   # push.CHECK_LIMIT_FLOOR_S: a push turn held longer is overdue, not a fair wait


def _push_turns(p, out: dict, now: float) -> None:
    """Add to push occupancy `out` the pushes that never write push_queue (`ttp push`, detached or not):
    each live holder of a branch push lock as running, with its step and minutes held (`live` while one is
    within PUSH_HOLD_LIMIT_S, so no wait behind it is raised), and each other running detached push
    (push.running) as queued, with its last `ttp push:` log line. The lock and markers are what the
    waiting task's blocked reason reads; a holder whose pid died left the lock free, so it is not counted."""
    from . import locks, push
    try:
        paths = [x for x in sorted((p.state / "locks").glob("push:*.lock")) if not x.name.startswith("push:run-")]
        recs = locks.holder_records(paths, p)
    except OSError:
        recs = []
    pids, labels = set(), []
    for h in recs:
        if h.get("alive") is False or h.get("stale"):
            continue
        pids.add(h.get("pid"))
        labels.append(str(h.get("holder") or ""))
        try:
            held = max(0.0, now - float(h.get("since") or now))
        except (TypeError, ValueError):
            held = PUSH_HOLD_LIMIT_S + 1   # an unreadable start: counted, never trusted as within its limit
        step = f"step {h['step']}, " if h.get("step") else ""
        out["running"].append(f"{p.name} {h.get('holder') or 'a push'} ({step}held {held / 60:.0f} min)")
        out["live"] = out.get("live") or held <= PUSH_HOLD_LIMIT_S
    try:
        marks = push.running(p)
    except OSError:
        marks = []
    for m in marks:
        if m.get("status") != "running" or (m.get("pid") is not None and m.get("pid") in pids):
            continue
        if m.get("id") and any(str(m["id"]) in x for x in labels):   # a batch's run lock: the holder's own
            continue
        last = next((x for x in reversed(push._tail(m.get("log"), 40).splitlines()) if x.startswith("ttp push:")), "")
        out["queued"].append(f"{p.name} {'task #' + str(m['task']) if m.get('task') else 'push'} "
                             f"(push {m.get('id')}{': ' + last[:160] if last else ''})")


def spill_ready(p, cfg: dict) -> tuple[list[str], list[str]]:
    """(the spill hosts that can take checks now, why each other one cannot): allowed for this project's checks
    and not paused here or by another project (checkspill.allowed), and their machine lock not refused
    (checkspill.lock_busy, as `ttp lock --probe --shared` answers). Local reads only."""
    from . import checkspill
    ok, why = [], []
    for h in checkspill.hosts(cfg):
        try:
            no = checkspill.allowed(p, cfg, h) or checkspill.lock_busy(p, cfg, h)
        except Exception as e:   # unreadable: never offered as a route
            no = f"could not be read ({e})"
        if no:
            why.append(f"{h}: {no}")
        else:
            ok.append(h)
    return ok, why


def spill_note(spill: tuple[list[str], list[str]] | None, cfg: dict | None = None) -> str:
    """The item's line when spill hosts are set but none can take checks now (spill_ready), else "". With the
    project's checks.wrap_cmd keeping the checks behind it here (checkspill.wrap_note), it says so and names the
    settings, first: a host's pause is not why those never spill."""
    from . import checkspill
    wrapped = checkspill.wrap_note(cfg) if spill and (spill[0] or spill[1]) else ""
    if wrapped:
        rest = (f" Other checks: no spill host can take them now ({'; '.join(spill[1][:3])})."
                if not spill[0] and spill[1] else "")
        return f"No spill host can take checks behind checks.wrap_cmd: {wrapped}.{rest}"
    if not spill or spill[0] or not spill[1]:
        return ""
    return f"No spill host can take checks now ({'; '.join(spill[1][:3])}), so none is offered."


def options(db, w: dict, cfg: dict, paused: dict, device: set | None = None, cores: int = 0,
            spill: tuple[list[str], list[str]] | None = None) -> list[str]:
    """Concrete unblock options for wait `w`, from config and host state; none that a user pause, a cap the
    user set or missing config forbids. Empty: only the user can clear it. A spill host is offered only from
    `spill` (spill_ready): one that can take checks now; none without it."""
    from . import configwho
    t = db.one("SELECT priority, origin FROM tasks WHERE id=?", (w["task"],)) or {}
    res = [r for r in str(w.get("res") or "").split(",") if r]
    if any((paused.get(r) or {}).get("by") == "user" for r in res):
        return []
    user_set = lambda key: configwho.role(configwho.get(db, key)) == "user"   # noqa: E731
    out, q = [], w["queue"]
    if q in ("checks", "cpu", "pool"):
        hosts = list((spill or ([], []))[0])
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
        conf = int((cfg.get("budget") or {}).get("max_parallel_workers", 6) or 0)
        # slots a budget gate or the urgent reserve cut below the setting (cap_why) rise with no config raise
        if not user_set("budget.max_parallel_workers") and cap and cap == conf and cap < top and not w.get("cap_why"):
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


NO_OPTION = "No option is open to the project (a user pause or a cap the user set holds it): the user is told."


def _parts(w: dict, now: float, note: str = "") -> tuple[str, str, str]:
    """(queue name, the item's own words, the queue's state now) of wait `w`."""
    cap = w.get("cap")
    run, que = list(w.get("running") or []), list(w.get("queued") or [])
    some = lambda xs: "; ".join(xs[:4]) + ("; ..." if len(xs) > 4 else "") or "none seen"   # noqa: E731
    name = f"{NAMES.get(w['queue'], w['queue'])}{' ' + w['res'] if w.get('res') else ''}"
    ctx = (f"{len(run)} running (cap {cap if cap else 'unknown'}{', ' + w['cap_why'] if w.get('cap_why') else ''}),"
           f" {len(que)} queued. Holding: {some(run)}. Waiting: {some(que)}." + (f" {note}" if note else ""))
    return name, f"{key(w)}: #{w['task']} has waited {(now - w['since']) / 60:.0f} min", ctx


def text(w: dict, opts: list[str], now: float, note: str = "") -> str:
    name, item, ctx = _parts(w, now, note)
    head = f"{item} on the {name}: {ctx}"
    if not opts:
        return f"{head} {NO_OPTION}"
    return head + " Options: " + " | ".join(f"({i}) {o}" for i, o in enumerate(opts, 1))


def fold(w: dict, opts: list[str], now: float, note: str = "") -> dict:
    """The event's FOLD data: one line per queue in NEW EVENTS, the newest state once, each item's key and
    its options kept (an empty `opts`: NO_OPTION)."""
    name, item, ctx = _parts(w, now, note)
    return {"group": f"the {name}", "item": item, "ctx": ctx, "opts": list(opts) or [NO_OPTION]}


def episode(w: dict) -> str:
    """The episode key of wait `w`: one per task and queue, whatever start time a scan saw."""
    return f"{w['task']}:{w['queue']}"


def host_held(w: dict) -> bool:
    """A checks wait held host-wide: every host checks slot is taken (or no host view, so the account's test
    pool or test-worker budget holds it). No coordinator action lifts it; a wait with a slot free is a fault."""
    cap = w.get("cap")
    return w["queue"] == "checks" and (not cap or len(w.get("running") or []) >= int(cap))


def _rekey(opened: dict) -> None:
    """Open episodes recorded under the old `<task>:<queue>:<since>` key merge into `<task>:<queue>`; their
    raise state is unknown, so the current cause is adopted without a raise."""
    for ep in [e for e in opened if e.count(":") >= 2]:
        rec = opened.pop(ep)
        cur = opened.get(f"{rec['task']}:{rec['queue']}")
        if cur is not None:
            cur["since"] = min(float(cur["since"]), float(rec["since"]))
            cur["acted"] = cur.get("acted") or rec.get("acted")
            continue
        rec.setdefault("raise", {"cause": None, "len": None, "n": 0})
        opened[f"{rec['task']}:{rec['queue']}"] = rec


def _settle(db, ep: str) -> None:
    db.x("UPDATE events SET status='handled' WHERE kind=? AND status='queued' AND (data LIKE ? OR data LIKE ?)",
         (KIND, f'%"episode": "{ep}"%', f'%"episode": "{ep}:%'))


def scan(p, cfg: dict, now: float | None = None, host: dict | None = None, device: set | None = None,
         cores: int = 0, hold=None) -> list[str]:
    """Raise, re-raise and clear queue-wait items; returns the fingerprints raised. `hold`: collect's."""
    from . import pagesev
    now = time.time() if now is None else now
    db, thr = p.db, threshold_s(cfg)
    if thr <= 0:
        return []
    st = db.kv(KV) or {}
    opened, told = st.setdefault("open", {}), st.setdefault("told", [])
    _rekey(opened)
    waits, live = [], set()
    for w in collect(p, cfg, now, host, device, hold):
        ep = episode(w)
        if (rec := opened.get(ep)) is not None:   # the same wait: its earliest start, seen now
            live.add(ep)
            rec["since"], rec["seen"] = min(float(rec["since"]), w["since"]), now
            if parked := rec.pop("parked", None):   # back within the grace: its unread item is queued again
                db.x("UPDATE events SET status='queued' WHERE id=? AND status='handled'", (parked,))
            w = {**w, "since": rec["since"]}
        if now - w["since"] >= thr:
            waits.append(w)
    waits.sort(key=lambda w: w["since"])
    paused = db.paused_resources()
    raised, spill = [], None
    for w in waits:
        ep = episode(w)
        live.add(ep)
        rec = opened.setdefault(ep, {"task": w["task"], "queue": w["queue"], "res": w["res"], "since": w["since"],
                                     "opened": int(now), "seen": now, "acted": None})
        if rec.get("acted"):
            continue
        k = key(w)
        if spill is None and w["queue"] in ("checks", "cpu", "pool"):
            spill = spill_ready(p, cfg)   # once a scan, only when a wait could spill
        sp = spill if w["queue"] in ("checks", "cpu", "pool") else None
        opts = options(db, {**w, "project": p.name}, cfg, paused, device, cores, sp)
        msg = text(w, opts, now, spill_note(sp, cfg))
        if ev := db.one("SELECT id, text, data FROM events WHERE kind=? AND status='queued' AND data LIKE ?",
                        (KIND, f'%"key": "{k}"%')):
            got = json.loads(ev["data"] or "{}")
            now_fold = fold(w, opts, now, spill_note(sp, cfg))
            if ev["text"] != msg[:4000] or got.get(FOLD) != now_fold:   # still unread: the holders, occupancy
                got[FOLD] = now_fold   # and options of this scan, not the raise's, in its text and its FOLD data
                db.x("UPDATE events SET text=?, data=? WHERE id=?", (msg[:4000], json.dumps(got), ev["id"]))
            continue
        if _cause_owner(db, st, w, now) is not None:
            continue   # one act per queue cause: its fix is already owned; raised again once that owner ends
        held, age = host_held(w), now - w["since"]
        cause = [w["queue"], w["res"], held, bool(opts)]
        last = rec.get("raise") or {}
        if last and last.get("cause") is None:   # an episode carried over from the old key: adopt, no raise
            last.update(cause=cause, len=last.get("len") or age)
        if last and last.get("cause") == cause and age < 2 * float(last.get("len") or age):
            continue   # raised already: again only on a changed cause or a doubled wait
        n = int(last.get("n") or 0) + 1
        data = {"must_act": {"key": k, "chat": "all", "tasks": [w["task"]], "kind": KIND, "episode": ep},
                FOLD: fold(w, opts, now, spill_note(sp, cfg))}
        if held:
            data[pagesev.RIDE_ALONG] = True   # no coordinator action lifts it: joins the next turn
        # the episode's open time keeps a later episode of the same task and queue off this one's fingerprints
        fp = f"{KIND}:{ep}:{int(rec.get('opened') or rec['since'])}:{n}"
        if len(raised) < MAX_RAISE and obligations._raise(db, now, fp, KIND, msg, w["task"], data,
                                                          severity="normal" if held else "high"):
            raised.append(fp)   # the longest waits first; the rest at a later scan
            rec["raise"] = {"cause": cause, "len": age, "n": n}
        if not opts and ep not in told:
            told.append(ep)
            msgparts.post(db, f"#{w['task']} has waited {(now - w['since']) / 60:.0f} min on the "
                              f"{NAMES.get(w['queue'], w['queue'])} and only you can clear it: {msg}",
                          kind="info", severity="normal")
    for ep in [e for e in opened if e not in live]:
        rec = opened[ep]
        if now - float(rec.get("seen") or rec["since"]) < GRACE_S and _open(db, rec["task"]):
            # a gap shorter than the grace: the same wait if it shows again; its unread item waits parked
            if not rec.get("parked") and (ev := db.one(
                    "SELECT id FROM events WHERE kind=? AND status='queued' AND data LIKE ?",
                    (KIND, f'%"episode": "{ep}"%'))):
                db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
                rec["parked"] = ev["id"]
            continue
        opened.pop(ep)
        rec.pop("parked", None)
        rec["end"] = float(rec.get("seen") or now)
        st.setdefault("hist", []).append(rec)
        _settle(db, ep)
    st["hist"] = [h for h in st.get("hist", []) if now - float(h.get("end") or now) <= HIST_KEEP_S][-HIST_MAX:]
    st["told"] = [e for e in told if e in opened][-HIST_MAX:]
    db.set_kv(KV, st)
    return raised


def _open(db, tid) -> bool:
    from .db import TERMINAL_TASK_STATES
    row = db.one("SELECT status FROM tasks WHERE id=?", (tid,)) if isinstance(tid, int) else None
    return bool(row) and row["status"] not in TERMINAL_TASK_STATES


TASK_ID_RE = re.compile(r"(?:probe:)?#\d+|\btask \d+", re.I | re.A)   # task ids a reason names, not the queue


def _cause_owner(db, st: dict, w: dict, now: float) -> int | None:
    """An open task, other than the waiting one, that already owns this wait's cause: the owner of an active
    throughput-class observation mute whose match names this queue (screen.mutes, throughmute), or the fix
    task an earlier item on the same queue and resource was acted on with (record_act's `owner`). A mute
    matches only the queue's own words and the task's reason without its task ids and 'next try' tail, never
    other tasks' labels in the item text; a mute keyed to one task ('#<id>') only that task's wait. None:
    raise the per-task item."""
    from . import screen, throughmute
    from .sweepskip import NEXT_TRY
    row = db.one("SELECT blocked_reason FROM tasks WHERE id=?", (w["task"],)) or {}
    reason = TASK_ID_RE.sub(" ", NEXT_TRY.sub("", str(row.get("blocked_reason") or "")))
    hay = " | ".join([NAMES.get(w["queue"], w["queue"]), *ALIASES.get(w["queue"], ()), str(w.get("res") or ""),
                      reason]).lower()
    for m in screen.mutes(db, now):
        match = str(m.get("match") or "").strip().lower()
        if not throughmute.marked(m) or not match:
            continue
        if (t := screen.task_of(match)) is not None:
            if t != w["task"]:
                continue
        elif not re.search(r"(?<!\w)" + re.escape(match) + r"(?!\w)", hay):
            continue
        own = ((m.get("throughput") or {}).get("owner") or {}).get("task")
        ids = [own] + throughmute.named_tasks(m.get("why")) + [
            int(x) for x in throughmute.PROBE_TASK_RE.findall(str(m.get("end_when") or ""))]
        if tid := next((t for t in ids if t != w["task"] and _open(db, t)), None):
            return tid
    for rec in [*(st.get("open") or {}).values(), *(st.get("hist") or [])]:
        if (rec.get("queue"), rec.get("res", "")) == (w["queue"], w["res"]) and rec.get("owner") != w["task"] \
                and _open(db, rec.get("owner")):
            return rec["owner"]
    return None


def fix_owner(db, it: dict, actions: list, applied) -> int | None:
    """The task a queue item was acted on with that owns its cause: a task_update naming the item's key on
    another task not itself waiting on that queue (a priority bump of another waiter owns no cause), or the
    open task a task_add naming the key created (open titles are unique). A task_update of the waiting task
    itself (priority, cancel) acts on that task only: None."""
    from . import mustact
    k, own = str(it.get("key") or ""), set(it.get("tasks") or [])
    queue = (str(it.get("episode") or "").split(":") + ["", ""])[1]
    waiters = {r.get("task") for r in ((db.kv(KV) or {}).get("open") or {}).values() if r.get("queue") == queue}
    for i in sorted(applied or ()):
        a = actions[i] if 0 <= i < len(actions) else None
        if not isinstance(a, dict) or not any(k and mustact._names(v, k) for v in a.values() if isinstance(v, str)):
            continue
        if a.get("type") == "task_update" and str(a.get("id")).isdigit() and int(a["id"]) not in own | waiters \
                and not _waits_on(db, int(a["id"]), queue):
            return int(a["id"])
        if a.get("type") == "task_add" and (row := db.one(
                "SELECT id FROM tasks WHERE title=? AND status NOT IN ('done','failed','cancelled') ORDER BY id DESC",
                (str(a.get("title") or "").strip(),))):
            return int(row["id"])
    return None


def _waits_on(db, tid: int, queue: str) -> bool:
    """Task `tid` is itself waiting on `queue`, by its last waiting hand-off (collect's classify)."""
    from .db import load_result
    row = db.one("SELECT blocked_reason, result FROM tasks WHERE id=? AND status='queued'", (tid,))
    prev = load_result(row["result"]) if row else {}
    got = prev.get("status") == "waiting" and classify(prev.get("retry_when") or "", row["blocked_reason"] or "")
    lockish = {"device", "lock"}   # without the device set a device lock classifies as a plain lock
    return bool(got) and (got[0] == queue or {got[0], queue} <= lockish)


def record_act(db, ep: str, now: float | None = None, owner: int | None = None) -> None:
    """The coordinator acted on the item of episode `ep`: no more raises for this wait; the metric's mark.
    `owner` (fix_owner): the task that owns the cause, so other waits on that queue fold into it."""
    with db.tx():
        st = db.kv(KV) or {}
        opened = st.get("open") or {}
        rec = opened.get(ep) or opened.get(":".join(ep.split(":")[:2]))   # an item raised under the old key
        if rec is not None and not rec.get("acted"):
            rec["acted"] = time.time() if now is None else now
            if owner is not None:
                rec["owner"] = owner
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

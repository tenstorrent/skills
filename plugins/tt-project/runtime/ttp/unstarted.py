# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Known, ready work that was announced or is ready but has not started. Model-free, on the daemon tick
(Daemon.check_unstarted). Observation only: dispatch never reads these events.

announced(): each open promise (obligations.py's records, no second ledger) and each queued, blocked or
waiting task announced in a reply (reply_chat set) that no open promise owns. When ANNOUNCED_S after it
was made (ANNOUNCED_P1_S when an owner has priority 1) no owner task has a run started since then, and
no owner is held by a recorded external condition (external_hold), one keyed `announced_not_started`
event names it, its owner ('none' when it has none), its age and what parks it (parked). The event is
retired (handled, so it wakes no turn) once an owner starts, the promise closes or the task ends, or an
owner becomes held externally: nothing is left for a turn to decide. One per promise or task, never
raised again (accepted gap: a parked owner that is held, then parked again, is not raised twice).
Promises and tasks older than MAX_AGE_S are left to promise_overdue and the wait checks, so a deploy
does not raise an old backlog at once.

ready_since() and READY_S/READY_P1_S size the ready-not-started check, which needs dispatch's own
holds (Daemon._start_hold) and so lives in the daemon. It covers what check_slot_wait (urgent work and
obligation owners held by full slots or any hold) and check_stall (no worker run at all for hours) do
not: any queued task dispatch could start while a slot of its provider is free that still has not.

parked_nudge() is the coordinator's advice for a deferral that names no external fact: it never
refuses, it counts (NUDGE_KV)."""
from __future__ import annotations

import re
import time

from .db import dependency_ids, deferral, load_result

ANNOUNCED_KIND = "announced_not_started"
READY_KIND = "ready_not_started"
ANNOUNCED_S = 600       # a promise or announced task with no started owner run after this is parked
ANNOUNCED_P1_S = 300    # an owner of priority 1
READY_S = 600           # a ready task with a free slot not started after this
READY_P1_S = 120        # priority 1 or urgent
DEFER_S = 600           # task_update adding spec to a task deferred further than this gets the nudge
MAX_AGE_S = 86400
START_SLACK_S = 60      # a run started just before the reply that announced it counts
NUDGE_KV = "parked_work_nudges"   # kv: {"n": total, "day": "YYYY-MM-DD", "today": n, "at": ts}
LIVE = ("queued", "blocked", "waiting", "review")
# A deferral's `why` naming an external fact: a time another party sets, data that must accumulate, a
# person, a shared resource, or another task's landing.
EXTERNAL = re.compile(
    r"#\d+|\blands?\b|\blanded\b|\bmerge|\brelease|\bwindow|\bschedul|\bmaintenance|\bcut\b|\bdeadline|"
    r"\baccumulat|\bcollect|\bsoak|\bovernight|\bdata\b|\bsamples?\b|\bmetrics?\b|\bmeasure|\bnightly|"
    r"\buser\b|\breview(?:er)?\b|\bapprov|\banswer|\brepl(?:y|ies)|\back(?:s|ed)?\b|\bperson|\bowner\b|"
    r"\bteam\b|\bdevice|\bbox\b|\bmachine|\bnode|\bbroker|\block\b|\bqueue|\bslot|\bcluster|\breserv|"
    r"\bquota|\bpause|\blull|\bidle\b|\bbusy\b|\bci\b|\bbuild\b|\bupstream|\bprovider|\blimit|\bcredit",
    re.I)
TIME_ONLY = re.compile(r'^\s*(?:test|\[)\s+"?\$\(\s*date\s+\+%s\s*\)"?\s+-ge\s+\d+\s*\]?\s*$')


def threshold(prio: int | None, p1: float, rest: float) -> float:
    return p1 if prio is not None and int(prio) <= 1 else rest


def _runs_since(db, ids: list[int], since: float) -> bool:
    if not ids:
        return False
    q = ",".join("?" * len(ids))
    return bool(db.one(f"SELECT 1 FROM runs WHERE task IN ({q}) AND started>=? LIMIT 1", (*ids, since - START_SLACK_S))
                or db.one(f"SELECT 1 FROM tasks WHERE id IN ({q}) AND status='running' LIMIT 1", ids))


def _asks_naming(db, tid: int) -> int | None:
    pat = re.compile(rf"#{tid}\b")
    for r in db.q("SELECT id, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0"):
        if pat.search(r["text"] or ""):
            return int(r["id"])
    return None


def external_hold(db, task: dict, paused: dict) -> str | None:
    """The recorded external condition holding `task`, in words, or None: a start_when probe not yet
    passed, a dependency on a task still running, waiting or in review, a paused resource, an open ask
    naming it, its own wait probe (not a timer alone) or a review in progress."""
    st = task["status"]
    if st not in LIVE:
        return None
    if ask := _asks_naming(db, task["id"]):
        return f"open ask #{ask}"
    if st == "review":
        return "its review"
    if st == "waiting":
        probe = str(load_result(task["result"]).get("retry_when") or "")
        return f"its wait probe ({probe[:120]})" if probe.strip() and not TIME_ONLY.match(probe) else None
    if st != "queued":
        return None
    d = deferral(task)
    if d.get("when"):
        return f"its start_when probe ({(d.get('why') or d['when'])[:120]})"
    from .coordinator import task_resources
    if hit := sorted(task_resources(task) & set(paused)):
        return f"paused resource {', '.join(hit)}"
    deps = dependency_ids(task)
    for row in db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps) if deps else []:
        if row["status"] in ("running", "waiting", "review"):
            return f"dependency #{row['id']} ({row['status']})"
    return None


def parked(db, task: dict | None, now: float) -> str:
    """What keeps a task that no external condition holds from starting, in words."""
    if task is None:
        return "no owner task"
    st = task["status"]
    if st == "queued" and float(task["not_before"] or 0) > now:
        why = deferral(task).get("why")
        return (f"start_after in {(float(task['not_before']) - now) / 60:.0f} min"
                + (f" ({why[:80]})" if why else ", naming no external fact"))
    if st == "queued":
        deps = dependency_ids(task)
        for row in db.q(f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps) if deps else []:
            if row["status"] != "done":
                return f"dependency #{row['id']} ({row['status']}, not started)"
        return f"dispatch: {task['blocked_reason'][:160]}" if task["blocked_reason"] else "nothing: ready, not picked"
    if st == "waiting":
        return f"a timer: it wakes in {max(float(task['not_before'] or now) - now, 0) / 60:.0f} min"
    if st == "blocked":
        return f"blocked: {(task['blocked_reason'] or 'no reason')[:160]}"
    return f"{st} without starting"


def _subjects(db, now: float) -> list[dict]:
    """The open promises and announced tasks to check: {key, what, created, owners}."""
    from . import obligations
    out, owned = [], set()
    for o in obligations.open_promises(db):
        ids = obligations._owner_ids(o)
        owned |= set(ids)
        if now - float(o["created"]) <= MAX_AGE_S:
            out.append({"key": o["id"], "what": f"promise {o['id']} in chat {o['promise']['chat']} ({o['text'][:160]})",
                        "created": float(o["created"]), "owners": ids})
    for t in db.q("SELECT id, title, created FROM tasks WHERE reply_chat IS NOT NULL AND kind!='review' "
                  f"AND status IN ({','.join('?' * len(LIVE))}) AND created>=?", (*LIVE, now - MAX_AGE_S)):
        if t["id"] not in owned:
            out.append({"key": f"t{t['id']}", "what": f"task #{t['id']} {t['title'][:100]} (announced in a reply)",
                        "created": float(t["created"]), "owners": [t["id"]]})
    return out


def announced(p, now: float | None = None, paused: dict | None = None) -> list[str]:
    """Raise and retire the announced_not_started events (module doc). Returns the fingerprints raised."""
    now = time.time() if now is None else now
    db = p.db
    paused = db.paused_resources() if paused is None else paused
    from .obligations import _retire
    raised, live = [], set()
    for s in _subjects(db, now):
        fp = f"{ANNOUNCED_KIND}:{s['key']}"
        live.add(fp)
        tasks = {t["id"]: t for t in db.q(f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(s['owners']))})",
                                          s["owners"])} if s["owners"] else {}
        held = [h for t in tasks.values() if (h := external_hold(db, t, paused))]
        done = any(t["status"] == "done" for t in tasks.values())   # its work ended: promise_overdue covers delivery
        if done or _runs_since(db, s["owners"], s["created"]) or held:
            _retire(db, now, "owner started" if not held else f"owner held: {held[0]}", fp)
            continue
        prio = min((t["priority"] for t in tasks.values()), default=None)
        age = now - s["created"]
        if age < threshold(prio, ANNOUNCED_P1_S, ANNOUNCED_S) or db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            continue
        owner = ", ".join(f"#{i}" for i in s["owners"]) or "none"
        holds = "; ".join(f"#{i}: {parked(db, tasks.get(i), now)}" for i in s["owners"]) or parked(db, None, now)
        text = (f"Announced, not started: {s['what']}, announced {age / 60:.0f} min ago. Owner: {owner}. No owner run "
                f"has started and no external condition holds it; what parks it: {holds}. Known, ready work starts "
                f"now: start it in its own task (task_add, no start_after; or task_update the owner's start_after "
                f"to now), or close the promise if it is no longer owed.")
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", ANNOUNCED_KIND, fp, "normal", text[:4000], "queued", s["owners"][0] if s["owners"] else None))
        raised.append(fp)
    for r in db.q("SELECT fingerprint FROM events WHERE kind=? AND status='queued'", (ANNOUNCED_KIND,)):
        if r["fingerprint"] not in live:   # its promise closed or its task ended
            _retire(db, now, "closed", r["fingerprint"])
    return raised


def ready_since(db, task: dict) -> float:
    """When a queued task became ready by its records: made, its last run ended, its start_after came, or
    its last dependency was done, whichever is latest."""
    ended = db.one("SELECT MAX(ended) e FROM runs WHERE task=?", (task["id"],))
    at = max(float(task["created"] or 0), float((ended or {}).get("e") or 0), float(task["not_before"] or 0))
    deps = dependency_ids(task)
    if deps:
        row = db.one(f"SELECT MAX(updated) u FROM tasks WHERE id IN ({','.join('?' * len(deps))})", deps)
        at = max(at, float((row or {}).get("u") or 0))
    return at


RULE = ("work whose inputs are ready starts now in its own task without start_after; defer only for a named "
        "external fact (a time another party sets, data that must accumulate, a person, a shared resource)")


def parked_nudge(where: str, why: str | None, after: float | None, when: str | None) -> str | None:
    """The advice for a deferral (start_after or start_when) whose `why` (or start_when probe) names no
    external fact; None when there is no deferral or one is named (EXTERNAL)."""
    if not (after or when) or EXTERNAL.search(f"{why or ''} {when or ''}"):
        return None
    return (f"{where} is deferred with no external fact named"
            + (f" (why: {' '.join(str(why).split())[:80]!r})" if why else "") + f": {RULE}")


def update_nudge(where: str, task: dict, now: float) -> str | None:
    """The advice for spec added to a task whose start is still more than DEFER_S away (its start_after,
    or the timer of its wait); None otherwise."""
    at = float(task.get("not_before") or 0)
    if task.get("status") not in ("queued", "waiting") or at - now <= DEFER_S:
        return None
    return f"{where} added work to a task that starts in {(at - now) / 60:.0f} min: {RULE}"


def count_nudge(db, now: float | None = None) -> None:
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    cur = db.kv(NUDGE_KV) or {}
    db.set_kv(NUDGE_KV, {"n": int(cur.get("n") or 0) + 1, "day": day,
                         "today": (int(cur.get("today") or 0) if cur.get("day") == day else 0) + 1, "at": now})

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Idle spend of a long queue, model-free: expire the stale queued tail, refuse repeated status and
report tasks, and show the backlog against measured throughput.

expire() (Daemon.expire_queue, every EVERY_S): a task that never started (no run on record) is cancelled
- when queued longer than `queue.expire_h` (default 48; 0: off) with no open dependency (one still
  waiting on live work waits with it) and no start_when probe (a deliberate wait the wait checks own), or
- when blocked on a failed, cancelled or missing dependency (reconcile_tasks' block) for longer than
  `queue.dead_dep_h` (default 24; 0: off), time for the coordinator to redo or re-point it.
Never expired: an urgent task (urgent.is_urgent), a user-ordered one (user_ordered), a review, the owner of
an open obligation. Each is cancelled with its reason in blocked_reason and one handled `task_expired`
event, and recorded under KEY with its undo, `ttp task <project> requeue <id>` (requeue clears the
reason). review_lines() lists each record once in the daily review; digest_lines() shows the backlog.

covering() (coordinator task_add): a new status, report, summary, check-on, digest, page or dashboard task
(REPORT_RE on its title's head) is refused when an open task of the same kind, or one done or failed in the
last COVER_S, covers the same target (_target: the head's words without the report words, message
references and dates); a queued one takes the new spec instead (merged). force with a reason adds it, and
a hop of a continue chain (task_add `continues`) is never refused: it carries unfinished work. An urgent
or user-ordered request is merged into a queued task but never refused.

A project pause stops expiry and its time is no age; a task naming a paused resource never expires."""
from __future__ import annotations

import re
import time
from typing import Any

from .db import DB, deferral, dependency_ids

KEY = "queue_expired"          # kv: [{task, title, why, at, listed}] expired tasks, newest last
KEEP = 500                     # records kept
EVERY_S = 600.0
COVER_S = 86400.0              # a report task finished this recently still covers its target
THROUGHPUT_DAYS = 3            # done tasks per day are measured over this many days
SHOWN = 8                      # oldest queued ids named in the digest line
TERMINAL = ("done", "failed", "cancelled", "superseded")
DEAD_DEP = re.compile(r"^dependency #\d+ (?:failed|cancelled|does not exist)|^a dependency is not a task id")
USER_RE = re.compile(r"\b(?:user|owner)(?:'s)?\s+(?:msgs?|messages?|orders?|words)\b|\buser-ordered\b", re.I)
REPORT_RE = re.compile(r"\b(?:status|reports?|summary|summaries|summari[sz]e|check[- ]on|checking on|digest|recap|"
                       r"page|dashboard)\b", re.I)
_HEAD_END = re.compile(r"\s(?:\(|-|—)\s?|[:;(]")
_NOISE = [re.compile(p, re.I) for p in (
    r"\b(?:user|owner)(?:'s)?\s+(?:msgs?|messages?)\s*#?\d+(?:\s*(?:,|and)\s*#?\d+)*",
    r"\b\d{4}-\d\d-\d\d\b", r"\b\d{1,2}:\d\d\b", r"\b\d{1,2}-\d{1,2}\b",
    r"\b(?:today|tonight|now|daily|nightly|weekly|hourly|again|new|latest|current)\b")]
_STOP = {"a", "an", "the", "of", "on", "for", "to", "in", "and", "with", "from", "post", "send", "write",
         "update", "refresh", "publish", "give", "make", "do", "run", "check", "checking", "status", "report",
         "reports", "summary", "summaries", "summarise", "summarize", "digest", "recap", "page", "dashboard"}


def settings(cfg: dict) -> tuple[float, float]:
    """(expire after s, dead-dependency grace s) from the `queue` config; 0 turns either off."""
    q = cfg.get("queue") or {}
    try:
        return float(q.get("expire_h", 48) or 0) * 3600, float(q.get("dead_dep_h", 24) or 0) * 3600
    except (TypeError, ValueError):
        return 48 * 3600.0, 24 * 3600.0


def user_ordered(task: dict) -> bool:
    """Added by the user (`ttp task add`, the web app), answering a chat, or citing the user's or the
    owner's message, order or words in its title or spec."""
    return (task.get("origin") == "user" or bool(task.get("reply_chat"))
            or bool(USER_RE.search(f"{task.get('title') or ''}\n{task.get('spec') or ''}")))


def _kept(task: dict, owners: set) -> bool:
    from . import urgent
    return (urgent.is_urgent(task) or user_ordered(task) or task.get("kind") == "review"
            or task["id"] in owners)


def candidates(db: DB, cfg: dict, now: float | None = None) -> list[tuple[dict, str]]:
    """(task, why) for each never-started task expire() would cancel now."""
    now = time.time() if now is None else now
    age_s, dead_s = settings(cfg)
    if age_s <= 0 and dead_s <= 0:
        return []
    rows = db.q("SELECT * FROM tasks t WHERE status IN ('queued','blocked') "
                "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task=t.id) ORDER BY id")
    if not rows:
        return []
    try:
        from . import obligations
        owners = set(obligations.owner_due(db))
    except Exception:   # unreadable obligations keep every task, never expire an owner
        return []
    from . import projpause
    from .coordinator import task_resources
    if projpause.current(db, now):
        return []   # nothing runs while the project is paused: its queue is not stale
    paused = set(db.paused_resources())
    pauses = projpause.windows(db, min(float(t["created"] or now) for t in rows), now)
    states = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM tasks")}
    out = []
    for t in rows:
        if _kept(t, owners) or task_resources(t) & paused:   # held by a pause: the user's hold, not staleness
            continue
        if t["status"] == "blocked":
            since = float(t["updated"] or t["created"] or now)
            if dead_s > 0 and DEAD_DEP.match(t["blocked_reason"] or "") and now - since >= dead_s:
                out.append((t, f"blocked {(now - since) / 3600:.0f} h on {t['blocked_reason']}"))
            continue
        d = deferral(t)
        if age_s <= 0 or d.get("when") or float(d.get("after") or 0) > now:
            continue
        if any(states.get(dep) not in TERMINAL for dep in dependency_ids(t) if dep in states):
            continue
        created = float(t["created"] or now)
        age = now - created - sum(max(0.0, e - max(b, created)) for b, e in pauses)   # paused time is no age
        if age >= age_s:
            out.append((t, f"queued {age / 3600:.0f} h without starting (queue.expire_h {age_s / 3600:g})"))
    return out


def expire(db: DB, cfg: dict, project: str, now: float | None = None) -> list[str]:
    """Cancel each candidate with its reason and undo; one log line each."""
    now = time.time() if now is None else now
    lines = []
    for t, why in candidates(db, cfg, now):
        undo = f"ttp task {project} requeue {t['id']}"
        reason = f"expired: {why}; undo: {undo}"
        with db.tx():
            # One statement: a task dispatch starts or someone changes in between keeps its state.
            if not db.conn.execute("UPDATE tasks SET status='cancelled', blocked_reason=?, updated=? WHERE id=? "
                                   "AND status=? AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task=tasks.id)",
                                   (reason, now, t["id"], t["status"])).rowcount:
                continue
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", "task_expired", "low", f"#{t['id']} {t['title'][:160]} cancelled: {reason}",
                  "handled", t["id"]))
            recs = list(db.kv(KEY, []) or [])
            recs.append({"task": t["id"], "title": t["title"][:160], "why": why, "undo": undo, "at": now})
            db.set_kv(KEY, recs[-KEEP:])
        lines.append(f"task {t['id']} expired: {why}")
    return lines


def review_lines(db: DB) -> list[str]:
    """The daily review's lines: each expired task not listed before, then marked listed."""
    recs = list(db.kv(KEY, []) or [])
    new = [r for r in recs if not r.get("listed")]
    if not new:
        return []
    for r in new:
        r["listed"] = True
    db.set_kv(KEY, recs)
    return [f"#{r['task']} {r['title'][:120]}: {r['why']} (undo: `{r['undo']}`)" for r in new]


def throughput(db: DB, now: float | None = None, days: int = THROUGHPUT_DAYS) -> float:
    """Tasks done per day over the last `days` days."""
    now = time.time() if now is None else now
    n = db.one("SELECT COUNT(*) n FROM tasks WHERE status='done' AND updated>=?", (now - days * 86400,))["n"]
    return n / days


def digest_lines(db: DB, since: float, now: float | None = None) -> list[str]:
    """The queued backlog against measured throughput, shown while the queue holds more than a day of
    work or tasks expired since the last turn: the coordinator prunes what no longer serves the plan."""
    from . import urgent
    now = time.time() if now is None else now
    queued = db.q("SELECT id, priority, spec, kind, title FROM tasks WHERE status='queued' ORDER BY id")
    expired = [r for r in db.kv(KEY, []) or [] if float(r.get("at") or 0) > since]
    rate = throughput(db, now)
    if not expired and len(queued) <= max(rate, 1):
        return []
    plain = [t for t in queued if not urgent.is_urgent(t)]
    days = f"{len(queued) / rate:.1f} days" if rate else "no end (nothing done in 3 days)"
    out = ["## Queued backlog (prune: cancel queued tasks the plan no longer needs)",
           f"- {len(queued)} queued ({len(queued) - len(plain)} urgent) against {rate:.1f} done per day over "
           f"{THROUGHPUT_DAYS} days: {days} to clear at that rate."]
    if plain and (not rate or len(queued) > rate):
        out.append("- Oldest non-urgent: " + ", ".join(f"#{t['id']}" for t in plain[:SHOWN]))
    if expired:
        out.append(f"- {len(expired)} never-started task(s) expired since your last turn (listed in the daily "
                   f"review; `ttp task <project> requeue <id>` undoes one): "
                   + ", ".join(f"#{r['task']}" for r in expired[:SHOWN]))
    return out


def _head(title: str) -> str:
    title = re.sub(r"^\s*(?:\[[^\]]*\]\s*)?(?:continue\s*:\s*)?", "", title or "", flags=re.I)
    m = _HEAD_END.search(title)
    return title[:m.start()] if m and m.start() > 0 else title


def is_report(title: str) -> bool:
    """A status, report, summary, check-on, digest, page or dashboard task, by its title's head."""
    return bool(REPORT_RE.search(_head(title)))


def _target(title: str) -> frozenset:
    head = _head(title)
    for rx in _NOISE:
        head = rx.sub(" ", head)
    return frozenset(w for w in re.findall(r"[a-z0-9][\w.-]*", head.lower()) if w not in _STOP)


def same_target(a: str, b: str) -> bool:
    """Both report titles name the same target: equal word sets, or one inside the other."""
    ta, tb = _target(a), _target(b)
    return ta == tb or bool(ta and tb and (ta <= tb or tb <= ta))


def covering(db: DB, title: str, kind: str, skip: Any = (), now: float | None = None) -> dict | None:
    """The open task of `kind`, or one done or failed in the last COVER_S, that covers the target of the
    report task `title`; None when `title` is no report task or nothing covers it."""
    if not is_report(title):
        return None
    now = time.time() if now is None else now
    skip = {str(s) for s in (skip or ())}
    for t in db.q("SELECT id, title, status, kind, updated FROM tasks WHERE kind=? AND (status NOT IN "
                  "('done','failed','cancelled','superseded') OR (status IN ('done','failed') AND updated>=?)) "
                  "ORDER BY id DESC", (kind, now - COVER_S)):
        if str(t["id"]) not in skip and is_report(t["title"]) and same_target(title, t["title"]):
            return t
    return None


def merge(db: DB, task: dict, title: str, spec: str, now: float | None = None) -> bool:
    """Fold a repeated report request into the queued task that covers it; False when it is not queued."""
    now = time.time() if now is None else now
    add = f"\n\nMerged request (a repeat task_add): {title}\n{spec}".rstrip()
    with db.tx():
        return bool(db.conn.execute("UPDATE tasks SET spec=COALESCE(spec,'')||?, updated=? WHERE id=? AND "
                                    "status='queued'", (add, now, task["id"])).rowcount)


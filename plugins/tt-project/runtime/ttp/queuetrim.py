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
event, and recorded under KEY with its undo, `ttp task <project> requeue <id> --force` (requeue clears the
reason). review_lines() lists each record once in the daily review; digest_lines() shows the backlog.

covering() (coordinator task_add): a new status, report, summary, check-on, digest or recap task (REPORT_RE
on its title's head; never a code task or a fix title) is refused when an open task of the same kind, or one
done or failed in the last COVER_S, covers the same target (_target: the head's words without the report
words, message references and dates, else the words after a head of report words only; an empty target
matches nothing, and a one-word target covers only the same word); a queued one takes the new spec, reply
chat and better priority instead (merged; two different reply chats are never merged). force with a reason
adds it, and a hop of a continue chain (task_add `continues`) is never refused: it carries unfinished work.
An urgent or user-ordered request is merged into a queued task but never refused.

A project pause stops expiry and its time is no age; a task naming a paused resource never expires."""
from __future__ import annotations

import re
import time
from typing import Any

from .db import DB, deferral, dependency_ids

KEY = "queue_expired"          # kv: [{task, title, why, at, listed}] expired tasks, newest last
KEEP = 500                     # records kept, beyond those of open tasks (_trim)
EVERY_S = 600.0
COVER_S = 86400.0              # a report task finished this recently still covers its target
THROUGHPUT_DAYS = 3            # done tasks per day are measured over this many days
SHOWN = 8                      # oldest queued ids named in the digest line
TERMINAL = ("done", "failed", "cancelled", "superseded")
DEAD_DEP = re.compile(r"^dependency #\d+ (?:failed|cancelled|does not exist)|^a dependency is not a task id")
USER_RE = re.compile(r"\b(?:user|owner)(?:'s)?\s+(?:msgs?|messages?|orders?|words)\b|\buser-ordered\b", re.I)
REPORT_RE = re.compile(r"\b(?:status|reports?|summary|summaries|summari[sz]e|check[- ]on|checking on|digest|recap)\b",
                       re.I)
# Code work naming a report word is no report request: a fix or change verb leads it, or the word names a
# thing (a status page, a status field), not the report kind.
_CODE_RE = re.compile(r"^\s*(?:fix|add|implement|build|refactor|rewrite|repair|debug|port|remove|rename|change|"
                      r"improve|speed up|wire)\b", re.I)
_THING_RE = re.compile(r"\b(?:status|report|summary|digest)\s+(?:page|dashboard|field|bar|column|line|code|"
                       r"icon|badge|endpoint|api|button|chart|view|table schema|generator|parser)s?\b", re.I)
_HEAD_END = re.compile(r"\s(?:\(|-|—)\s?|[:;(]")
_NOISE = [re.compile(p, re.I) for p in (
    r"\b(?:user|owner)(?:'s)?\s+(?:msgs?|messages?)\s*#?\d+(?:\s*(?:,|and)\s*#?\d+)*",
    r"\b\d{4}-\d\d-\d\d\b", r"\b\d{1,2}:\d\d\b", r"\b\d{1,2}-\d{1,2}\b",
    r"\b(?:today|tonight|now|daily|nightly|weekly|hourly|again|new|latest|current)\b")]
_STOP = {"a", "an", "the", "of", "on", "for", "to", "in", "and", "with", "from", "post", "send", "write",
         "update", "refresh", "publish", "give", "make", "do", "run", "check", "checking", "status", "report",
         "reports", "summary", "summaries", "summarise", "summarize", "digest", "recap"}


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
    # A task expired before and open again was requeued (the recorded undo): it ages from that requeue,
    # never from its creation, so the undo sticks for a fresh expire_h (dead_dep_h when blocked again).
    back = {int(r["task"]): float(r.get("at") or 0) for r in db.kv(KEY, []) or [] if r.get("task")}
    out = []
    for t in rows:
        if _kept(t, owners) or task_resources(t) & paused:   # held by a pause: the user's hold, not staleness
            continue
        requeued = max(back[t["id"]], float(t["updated"] or 0)) if t["id"] in back else 0.0
        if t["status"] == "blocked":
            since = max(float(t["updated"] or t["created"] or now), requeued)
            if dead_s > 0 and DEAD_DEP.match(t["blocked_reason"] or "") and now - since >= dead_s:
                out.append((t, f"blocked {(now - since) / 3600:.0f} h on {t['blocked_reason']}"))
            continue
        d = deferral(t)
        if age_s <= 0 or d.get("when") or float(d.get("after") or 0) > now:
            continue
        if any(states.get(dep) not in TERMINAL for dep in dependency_ids(t) if dep in states):
            continue
        created = max(float(t["created"] or now), requeued)
        age = now - created - sum(max(0.0, e - max(b, created)) for b, e in pauses)   # paused time is no age
        if age >= age_s:
            out.append((t, f"queued {age / 3600:.0f} h without starting (queue.expire_h {age_s / 3600:g})"))
    return out


def _trim(db: DB, recs: list) -> list:
    """The newest KEEP records, plus the newest of each task still open: a requeued task ages from its
    record (candidates), so trimming it would expire the requeue again on the next pass."""
    if len(recs) <= KEEP:
        return recs
    ids = sorted({int(r["task"]) for r in recs[:-KEEP] if r.get("task")})
    open_ids = {r["id"] for r in db.q(f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(ids))}) "
                                      f"AND status NOT IN ({','.join('?' * len(TERMINAL))})",
                                      (*ids, *TERMINAL))} if ids else set()
    newest = {r.get("task"): i for i, r in enumerate(recs)}
    return [r for i, r in enumerate(recs) if i >= len(recs) - KEEP
            or (r.get("task") in open_ids and newest[r.get("task")] == i)]


def expire(db: DB, cfg: dict, project: str, now: float | None = None) -> list[str]:
    """Cancel each candidate with its reason and undo; one log line each."""
    now = time.time() if now is None else now
    lines = []
    for t, why in candidates(db, cfg, now):
        undo = f"ttp task {project} requeue {t['id']} --force"
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
            db.set_kv(KEY, _trim(db, recs))
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
                   f"review; `ttp task <project> requeue <id> --force` undoes one): "
                   + ", ".join(f"#{r['task']}" for r in expired[:SHOWN]))
    return out


def _split(title: str) -> tuple[str, str]:
    """(head, the rest after the head's end)."""
    title = re.sub(r"^\s*(?:\[[^\]]*\]\s*)?(?:continue\s*:\s*)?", "", title or "", flags=re.I)
    m = _HEAD_END.search(title)
    return (title[:m.start()], title[m.end():]) if m and m.start() > 0 else (title, "")


def _head(title: str) -> str:
    return _split(title)[0]


def is_report(title: str) -> bool:
    """A status, report, summary, check-on, digest or recap task, by its title's head; never code work."""
    head = _head(title)
    return not _CODE_RE.match(head) and bool(REPORT_RE.search(_THING_RE.sub(" ", head)))


def _words(text: str) -> frozenset:
    for rx in _NOISE:
        text = rx.sub(" ", text)
    return frozenset(w for w in re.findall(r"[a-z0-9][\w.-]*", text.lower()) if w not in _STOP)


def _target(title: str) -> frozenset:
    """The head's words without report words, message references and dates; a head of report words only
    ('Status report: X') takes its target from the text after it."""
    head, rest = _split(title)
    return _words(head) or _words(_head(rest))


def same_target(a: str, b: str) -> bool:
    """Both report titles name the same target: equal word sets, or a target of 2+ words inside the other.
    An empty target matches nothing."""
    ta, tb = _target(a), _target(b)
    small, big = sorted((ta, tb), key=len)
    return bool(small) and (ta == tb or (len(small) >= 2 and small <= big))


def covering(db: DB, title: str, kind: str, skip: Any = (), now: float | None = None) -> dict | None:
    """The open task of `kind`, or one done or failed in the last COVER_S, that covers the target of the
    report task `title`; None when `title` is no report task or nothing covers it."""
    if kind == "code" or not is_report(title):
        return None
    now = time.time() if now is None else now
    skip = {str(s) for s in (skip or ())}
    for t in db.q("SELECT id, title, status, kind, updated FROM tasks WHERE kind=? AND (status NOT IN "
                  "('done','failed','cancelled','superseded') OR (status IN ('done','failed') AND updated>=?)) "
                  "ORDER BY id DESC", (kind, now - COVER_S)):
        if str(t["id"]) not in skip and is_report(t["title"]) and same_target(title, t["title"]):
            return t
    return None


def merge(db: DB, task: dict, title: str, spec: str, reply_chat: str | None = None, priority: Any = None,
          now: float | None = None) -> bool:
    """Fold a repeated report request into the queued task that covers it: its spec is appended, its reply
    chat taken when the task has none, and the better (lower) priority kept. False when the task is not
    queued or answers another chat (the request is then added as its own task)."""
    now = time.time() if now is None else now
    add = f"\n\nMerged request (a repeat task_add): {title}\n{spec}".rstrip()
    chat = reply_chat or None
    try:
        prio = int(priority) if priority is not None else None
    except (TypeError, ValueError):
        prio = None
    with db.tx():
        return bool(db.conn.execute(
            "UPDATE tasks SET spec=COALESCE(spec,'')||?, updated=?, reply_chat=COALESCE(reply_chat, ?), "
            "priority=MIN(COALESCE(priority, 3), COALESCE(?, priority, 3)) "
            "WHERE id=? AND status='queued' AND (? IS NULL OR reply_chat IS NULL OR reply_chat=?)",
            (add, now, chat, prio, task["id"], chat, chat)).rowcount)

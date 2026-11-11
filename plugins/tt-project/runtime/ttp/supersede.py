"""No money on duplicate or superseded work, model-free.

At dispatch (`dispatch_hold`, just before a run starts), a queued task is held (blocked, one
`dispatch_held` event for the coordinator, nothing runs) when it:
- repeats an earlier open task, or one done in the last DUP_DONE_DAYS: the same title, or the same
  title and spec by task_add's own test (coordinator.looks_same). Daemon-made tasks (reviews, fixes,
  re-reviews, continues) count too. Not a duplicate: a task of its own lineage (one it continues, at any
  hop), one either depends on, a review of another subject, a spec saying `differs from #<id>`, a task
  owing other obligations (the owner note the harness appends reads alike for any two), and a
  schedule's runs (origin 'schedule': each run of a recurring schedule repeats the last by design);
- predates a user order whose `supersedes` steps (STEPS_KEY, recorded with the order) its spec still
  carries, and was not re-specified since.
A task is held once: queued again after its hold (the coordinator looked), it runs.

While running (`stop_superseded`, every STOP_EVERY_S), a task is cancelled, which stops its run (the
daemon's cancel path writes STOP; the worktree and branch stay, so its work is kept), once it is
superseded: its change landed through another task (`ttp task superseded`, recorded since that run began), a newer task continues it
(`continues:`), or every obligation it owns was dropped. One `superseded_stopped` event says why.
"""
from __future__ import annotations

import re
import time

HELD = "dispatch_held"
STOPPED = "superseded_stopped"
STEPS_KEY = "user_order_superseded_steps"   # kv: [{"msg", "ts", "steps"}] of the newest orders
STEPS_KEPT = 100
DUP_DONE_DAYS = 14
STOP_EVERY_S = 60
CLEAR_TTL_S = 60   # a task found clear is not compared again this soon unless its row changed
_DIFFERS = re.compile(r"differs from #(\d+)", re.I)
_OWES = re.compile(r"^## Obligation (o\d+) ", re.M)   # obligations._owner_note's heading
_clear: dict[int, tuple[float, float]] = {}   # task id -> (its `updated`, when it was found clear)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"\w+", (s or "").lower()))


def record_steps(db, msg: int, ts: float | None, steps) -> None:
    """Remember the older spec steps user order `msg` (sent at `ts`) supersedes, for dispatch."""
    steps = [str(s).strip() for s in steps or [] if len(_norm(str(s))) >= 8]
    if steps:
        got = db.kv(STEPS_KEY, []) or []
        got = [g for g in got if isinstance(g, dict) and g.get("msg") != int(msg)]
        db.set_kv(STEPS_KEY, (got + [{"msg": int(msg), "ts": float(ts or time.time()), "steps": steps}])[-STEPS_KEPT:])


def _lineage(db, task: dict) -> set[int]:
    from .chaincap import prev_of
    out, t = set(), task
    while t and (i := prev_of(t)) is not None and i not in out and len(out) < 50:
        out.add(i)
        t = db.task(i)
    return out


def duplicate_of(db, task: dict) -> dict | None:
    """An earlier open task, or one done in the last DUP_DONE_DAYS, that `task` repeats."""
    from .coordinator import looks_same
    from .db import dependency_ids, review_subject
    if task.get("origin") == "schedule":   # a recurring schedule's next run is never a duplicate of its last
        return None
    skip = _lineage(db, task) | {int(d) for d in dependency_ids(task) if str(d).isdigit()} \
        | {int(i) for i in _DIFFERS.findall(task.get("spec") or "")}
    title, spec = task.get("title") or "", task.get("spec") or ""
    since = time.time() - DUP_DONE_DAYS * 86400
    for o in db.q("SELECT * FROM tasks WHERE id<? AND kind=? AND (status NOT IN ('done','failed','cancelled') "
                  "OR (status='done' AND COALESCE(updated, created, 0) >= ?)) AND origin!='schedule' ORDER BY id DESC",
                  (task["id"], task["kind"], since)):
        if not (_norm(title) == _norm(o["title"]) or looks_same(title, spec, o["title"] or "", o["spec"] or "")):
            continue
        if o["id"] in skip or task["id"] in dependency_ids(o) or task["id"] in _lineage(db, o):
            continue
        if (owes := set(_OWES.findall(spec))) and owes != set(_OWES.findall(o["spec"] or "")):
            continue   # it owes another deliverable
        if task["kind"] == "review":   # same subject, and a done one only while its subject is unchanged since
            subj = review_subject(task)
            if subj != review_subject(o) or o["status"] == "done" and float(
                    (db.task(subj) or {}).get("updated") or 0) > float(o["updated"] or 0):
                continue
        return o
    return None


def superseded_step(db, task: dict) -> tuple[int, str] | None:
    """(user message, step) of a newer order whose superseded step `task`'s spec still carries."""
    from .userorder import _respecs
    marks = _respecs(db)
    stamp = max(float(task.get("created") or 0), float(marks.get(str(int(task["id"]))) or 0))
    spec = _norm(task.get("spec") or "")
    for o in reversed(db.kv(STEPS_KEY, []) or []):
        if isinstance(o, dict) and float(o.get("ts") or 0) > stamp:
            for s in o.get("steps") or []:
                if _norm(s) and _norm(s) in spec:
                    return int(o["msg"]), str(s)
    return None


def dispatch_hold(db, task: dict) -> str:
    """Why queued `task` must be held at dispatch ('' when it may start). Held once per task."""
    now, mark = time.time(), float(task.get("updated") or 0)
    seen = _clear.get(task["id"])
    if seen and seen[0] == mark and now - seen[1] < CLEAR_TTL_S or \
            db.one("SELECT 1 FROM events WHERE task=? AND kind=?", (task["id"], HELD)):
        return ""
    if (o := duplicate_of(db, task)) is not None:
        return (f"duplicates #{o['id']} {o['title'][:120]} ({o['status']}); cancel it, or queue it again "
                f"(its spec saying `differs from #{o['id']}: <how>`) if it is other work")
    if hit := superseded_step(db, task):
        return (f"its spec still carries a step user msg #{hit[0]} superseded ({hit[1][:160]}); re-spec it "
                f"(task_update spec) or cancel it")
    if len(_clear) > 2000:
        _clear.clear()
    _clear[task["id"]] = (mark, now)
    return ""


def hold(db, task: dict, why: str) -> None:
    """Block queued `task` with `why` and tell the coordinator once."""
    now = time.time()
    with db.tx():
        if db.conn.execute("UPDATE tasks SET status='blocked', blocked_reason=?, updated=? WHERE id=? "
                           "AND status='queued'", (f"held at dispatch: {why}", now, task["id"])).rowcount:
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{task['id']}", HELD, "normal", f"#{task['id']} {task['title'][:160]} was not started: "
                  f"{why}. Nothing ran.", "queued", task["id"]))


def stop_reason(db, task: dict, started: float) -> str:
    """Why running `task` (its run started at `started`) is superseded ('' while it is not)."""
    from . import obligations
    if row := db.one("SELECT sha FROM task_superseded WHERE task=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                     (task["id"], started)):   # an older record predates this run (a requeue after it)
        return f"its change landed through another commit ({row['sha'][:12]})"
    for t in db.q("SELECT * FROM tasks WHERE id>? AND kind!='review' AND COALESCE(created,0)>? "
                  "AND labels LIKE ?", (task["id"], started, f'%"continues:{int(task["id"])}"%')):
        return f"#{t['id']} {t['title'][:120]} continues it"
    owned = [o for o in obligations.items(db) if int(task["id"]) in obligations._owner_ids(o)]
    if owned and all(o.get("dropped") for o in owned):
        return "every obligation it owns was dropped (" + ", ".join(o["id"] for o in owned) + ")"
    return ""


def stop_superseded(db) -> list[int]:
    """Cancel each running task that `stop_reason` finds superseded; the cancel path stops its run."""
    from . import dispatchguard
    out = []
    for r in db.q("SELECT t.*, MIN(r.started) AS run_started FROM tasks t JOIN runs r ON r.task=t.id "
                  "AND r.status='running' AND r.role!='coordinator' WHERE t.status='running' GROUP BY t.id"):
        if not (why := stop_reason(db, r, float(r["run_started"] or 0))):
            continue
        now = time.time()
        with db.tx():
            if not db.conn.execute("UPDATE tasks SET status='cancelled', updated=? WHERE id=? AND status='running'",
                                   (now, r["id"])).rowcount:
                continue
            dispatchguard.record_cancel(db, r["id"], "running", "supersede")
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{r['id']}", STOPPED, "normal", f"#{r['id']} {r['title'][:160]} stopped: {why}. Its "
                  f"run is asked to stop; its worktree and branch are kept. `ttp task requeue` runs it again.",
                  "queued", r["id"]))
        out.append(r["id"])
    return out

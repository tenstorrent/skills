# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Interim mitigation: a known defect that degrades a live service gets a workaround with a due time,
not only a fix.

A fix goes through review, push and deploy, which can take days; the live service stays degraded all
that time. So a hand-off that states live impact in its structured field `live_impact` (top level, or
on one of its `followups`) makes the daemon queue, model-free, a priority-1 mitigation task beside the
fix (file()). The field is `"<resource> <what is degraded>"` or `{"resource": ..., "what": ...}` (a list
of either at top level). Free text is never parsed for it. The fix is the reporting code task itself
(a top-level `live_impact`), the follow-up that carries it, or none yet: then the QUEUED event stays
queued for the coordinator, which queues one.

The mitigation's spec names the resource, the defect and the fix, and asks for the least-disruptive
safe workaround the charter or memory already approves (a restart, a manual escalation, rerouting work,
a heal check). It is due coordinator.mitigation_due_h hours after it was filed (DUE_H; 0 turns filing
off). One open mitigation per resource: a later report for the same resource adds none.

tend(), every tick: an open mitigation past its due time (not done or cancelled) raises one high
OVERDUE event, which wakes the coordinator for a tricky turn; none once its fix task is done (the fix,
not the workaround, ends the impact). Once the fix task is done too, the mitigation's
temporary measures (its hand-off's `temporary_measures` [{what, undo}], and the open change-ledger
entries it recorded) are listed for undo in one UNDO event. The blocker sweep (sweep.items) and
`ttp status` list every open mitigation with its due time.
"""
from __future__ import annotations

import time
from typing import Any

from . import timefmt
from .db import load_result

KEY = "mitigations"     # kv: {task id: {resource, what, of, fix, fix_title, due, overdue, undo}}
LABEL = "mitigation"
DUE_H = 4.0
QUEUED = "mitigation_queued"
OVERDUE = "mitigation_overdue"
UNDO = "mitigation_undo"
CHARS = 300


def _one(text: Any, n: int = CHARS) -> str:
    return " ".join(str(text or "").split())[:n]


def parse(v: Any) -> dict | None:
    """{resource, what} from one `live_impact` value, or None when it names no resource and impact."""
    if isinstance(v, dict):
        res, what = _one(v.get("resource"), 120), _one(v.get("what") or v.get("impact") or v.get("degraded"))
    elif isinstance(v, str):
        res, _, what = _one(v).partition(" ")
        res, what = res[:120], what.strip()
    else:
        return None
    return {"resource": res, "what": what} if res and what else None


def impacts(task: dict, result: Any) -> list[dict]:
    """Each live impact a hand-off states: {resource, what, fix, fix_title}. `fix` is the reporting
    task's id when it is a code task stating the impact itself; a follow-up's impact names it as the fix."""
    if not isinstance(result, dict):
        return []
    out = []
    top = result.get("live_impact")
    for v in top if isinstance(top, list) else [top]:
        if (i := parse(v)):
            own = task.get("kind") == "code"
            out.append({**i, "fix": task["id"] if own else None, "fix_title": task["title"] if own else ""})
    for f in result.get("followups") or []:
        if isinstance(f, dict) and f.get("title") and (i := parse(f.get("live_impact"))):
            out.append({**i, "fix": None, "fix_title": _one(f["title"], 200)})
    return out


def due_h(cfg: dict | None) -> float:
    try:
        return max(0.0, float(((cfg or {}).get("coordinator") or {}).get("mitigation_due_h", DUE_H)))
    except (TypeError, ValueError):
        return DUE_H


def records(db) -> dict[str, dict]:
    v = db.kv(KEY, {})
    return v if isinstance(v, dict) else {}


def open_ones(db) -> list[tuple[dict, dict]]:
    """(task, record) of each mitigation not done or cancelled (a failed one is still owed)."""
    out = []
    for tid, rec in sorted(records(db).items(), key=lambda kv: int(kv[0])):
        t = db.task(int(tid))
        if t and t["status"] not in ("done", "cancelled"):
            out.append((t, rec))
    return out


def _fix_ref(i: dict) -> str:
    if i["fix"]:
        return f"#{i['fix']} {_one(i['fix_title'], 120)} (its review, push and deploy are still ahead)"
    if i["fix_title"]:
        return f"the proposed follow-up {i['fix_title']!r} (not queued yet when this was filed)"
    return "none queued yet: the coordinator queues one"


def spec(i: dict, task: dict, due: float, where) -> str:
    end = f"--until-task {i['fix']}" if i["fix"] else "--expires <when the fix should be deployed>"
    return (f"Interim mitigation, due {timefmt.long(due, where)}. Live impact now: {i['resource']}: {i['what']}. "
            f"Known defect reported by #{task['id']} {_one(task['title'], 160)}. Fix: {_fix_ref(i)}.\n\n"
            "Do not fix the code here. Until the fix is deployed, apply the least-disruptive safe workaround the "
            "charter or memory already approves (for example a restart, a manual escalation, rerouting work, or a "
            "heal check), confirm the impact is gone or reduced, and hand off `done` with what you saw. No such "
            "workaround approved: hand off `blocked` with the options and their risks.\n"
            f"Record each live change on a shared machine: `ttp machines change add <alias> \"<what>\" --undo "
            f"\"<cmd>\" {end}`. List every temporary measure in result.json as `temporary_measures`: "
            "[{\"what\": ..., \"undo\": ...}]; they are listed for undo once the fix is done.")


def file(db, cfg: dict | None, task: dict, result: Any, where=None, now: float | None = None) -> list[int]:
    """Queue a mitigation for each live impact `result` states whose resource has no open mitigation;
    returns the new task ids. A mitigation's own hand-off files none."""
    now = time.time() if now is None else now
    hours = due_h(cfg)
    if not hours or str(task["id"]) in records(db):
        return []
    found = impacts(task, result)
    if not found:
        return []
    added = []
    with db.tx():
        recs = records(db)
        busy = {r.get("resource") for t, r in open_ones(db)}
        for i in found:
            if i["resource"] in busy:
                continue
            due = now + hours * 3600
            tid = db.add_task(f"mitigate: {i['resource']}: {_one(i['what'], 120)}", spec(i, task, due, where),
                              kind="code", priority=1, tier="standard", labels=[LABEL], parent=task["id"])
            recs[str(tid)] = {**i, "of": task["id"], "due": due, "filed": now}
            busy.add(i["resource"])
            added.append(tid)
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{task['id']}", QUEUED, "normal",
                  f"#{task['id']} reported live impact on {i['resource']}: {_one(i['what'], 200)}. Mitigation #{tid} "
                  f"queued at priority 1, due {timefmt.long(due, where)}; fix: {_fix_ref(i)}"
                  + ("." if i["fix"] or i["fix_title"] else ". Queue the fix task."),
                  "handled" if i["fix"] or i["fix_title"] else "queued", tid))
        if added:
            db.set_kv(KEY, recs)
    return added


def _fix_task(db, rec: dict) -> dict | None:
    if rec.get("fix"):
        return db.task(int(rec["fix"]))
    title = str(rec.get("fix_title") or "").strip().lower()
    if not title:
        return None
    rows = db.q("SELECT * FROM tasks WHERE id>? AND lower(trim(title))=? ORDER BY id DESC LIMIT 1",
                (int(rec.get("of") or 0), title))
    return rows[0] if rows else None


def measures(db, tid: int, project: str = "") -> list[str]:
    """What mitigation `tid` changed for now: its hand-off's temporary measures and its open ledger entries."""
    out = []
    t = db.task(tid)
    for m in load_result(t["result"] if t else None).get("temporary_measures") or []:
        if isinstance(m, dict) and m.get("what"):
            out.append(f"{_one(m['what'], 200)}" + (f" (undo: {_one(m.get('undo'), 300)})" if m.get("undo") else ""))
        elif isinstance(m, str) and m.strip():
            out.append(_one(m, 300))
    try:
        from . import machine_ledger
        for c in machine_ledger.changes():
            if c.get("task") == tid and (not project or c.get("by") == project):
                out.append(f"ledger change {c['id']} on {c.get('alias')}: {_one(c.get('what'), 200)} "
                           f"(undo: {_one(c.get('undo'), 300)}; then `ttp machines change close {c['id']}`)")
    except Exception:   # an unreadable ledger still lists the hand-off's measures
        pass
    return out


def tend(db, project: str = "", where=None, now: float | None = None) -> list[str]:
    """Raise OVERDUE and UNDO events (each once per mitigation); returns what it raised, for the log.
    One transaction, so a mitigation file() records meanwhile is never overwritten."""
    now = time.time() if now is None else now
    said = []
    with db.tx():
        recs = records(db)
        changed = False
        for tid, rec in sorted(recs.items(), key=lambda kv: int(kv[0])):
            t = db.task(int(tid))
            if not t:
                continue
            late = t["status"] not in ("done", "cancelled") and now >= float(rec.get("due") or 0) and not rec.get("overdue")
            fix = _fix_task(db, rec) if late or (t["status"] == "done" and not rec.get("undo")) else None
            if late and not (fix and fix["status"] == "done"):
                rec["overdue"], changed = now, True
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, f"task:{tid}", OVERDUE, "high",
                      f"Mitigation #{tid} for {rec.get('resource')} ({_one(rec.get('what'), 160)}) is OVERDUE: due "
                      f"{timefmt.long(float(rec['due']), where)}, it is {t['status']}. The live service is still degraded "
                      f"while the fix is in flight: unblock it, do it another way, or record why it can wait.",
                      "queued", int(tid)))
                said.append(f"mitigation #{tid} overdue")
            if t["status"] == "done" and not rec.get("undo") and fix and fix["status"] == "done":
                rec["undo"], changed = now, True
                items = measures(db, int(tid), project)
                db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                     (now, f"task:{tid}", UNDO, "normal",
                      f"Fix #{fix['id']} for {rec.get('resource')} is done. Once it is deployed, undo mitigation #{tid}'s "
                      "temporary measures: " + ("; ".join(items) if items else "none recorded (check its hand-off)."),
                      "queued", int(tid)))
                said.append(f"mitigation #{tid} undo listed")
        if changed:
            db.set_kv(KEY, recs)
    return said


def due_text(rec: dict, now: float, where) -> str:
    late = now >= float(rec.get("due") or 0)
    return f"mitigation due {timefmt.long(float(rec.get('due') or 0), where)}" + (" OVERDUE" if late else "")


def status_lines(db, now: float, where) -> list[str]:
    rows = [f"  #{t['id']} {t['status']}, {due_text(rec, now, where)}: {t['title']}" for t, rec in open_ones(db)]
    return ["live impact, interim mitigations:", *rows] if rows else []


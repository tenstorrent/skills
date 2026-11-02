# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Premise checks: a code task whose spec has a line starting `premise: <read-only check>` first runs
that check as a cheap question, so a code run is not spent finding out there is nothing to change.

Model-free, from Daemon.reconcile_tasks and the start of each dispatch pass (tend):

- split: a queued code task with such a line that never ran, is no continue hop (`continues:` label:
  its chain's first task checked it) and has no `premise:` label gets one question task (kind
  question, tier light, origin daemon, label `premise-check:<code id>`, the code task's priority and
  provider). The code task then depends on it and carries `premise:<question id>`. A light question
  is the cheapest tier that reads records; a failed one is retried a tier up (effort.retry_tier). The
  conclusion floor (tierfloor) does not lift it: the daemon reads its answer, not the user.
- settle, once the question ended (the code task still queued or blocked on it):
  - its hand-off says `"premise": "holds"`: the code spec gets the question's summary under a
    `Premise check #<id>` line, so the code run does not redo the check, and the task runs;
  - `"premise": "fails"`: the code task is closed done as not needed (outcome rejected), the question's
    summary as its result, with the usual task_done event for the coordinator (and a chat reply);
  - no verdict, or the question failed or was cancelled: the code task runs anyway and its spec says
    the check gave no verdict, so the code run checks the premise itself (no work is lost).
  The label becomes `premise:<id>:holds|fails|unknown`, so each code task is settled once.
- a pending question whose code task ended (done, failed, cancelled) and that has not started is
  cancelled: nobody reads its answer.

The question's own done event is handled (it starts no coordinator turn): what follows is the code
task's own run or its close.
"""
from __future__ import annotations

import json
import re
import time

from .db import TERMINAL_TASK_STATES, dump_result, load_result

LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]+)?premise:[ \t]*(\S.*?)[ \t]*$")
CHECK_LABEL = "premise-check:"   # on the question: + the code task's id
LABEL = "premise:"                # on the code task: + the question's id, then + ":" + its verdict
PENDING = re.compile(r"^premise:(\d+)$")
VERDICTS = {"holds": "holds", "held": "holds", "true": "holds", "yes": "holds",
            "fails": "fails", "failed": "fails", "false": "fails", "no": "fails"}
SPEC_CHARS = 1500   # of the code spec quoted in the question, for context
SUMMARY_CHARS = 800  # of the question's summary carried into the code spec


def line(spec: str | None) -> str:
    """The check a spec's first `premise:` line names, or ""."""
    m = LINE.search(spec or "")
    return m.group(1) if m else ""


def _labels(task: dict) -> list:
    try:
        got = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return got if isinstance(got, list) else []


def is_check(task: dict) -> bool:
    """Whether `task` is a premise check the daemon queued for a code task."""
    return task.get("kind") == "question" and any(str(x).startswith(CHECK_LABEL) for x in _labels(task))


def verdict(result: dict) -> str:
    """holds, fails or "" (none) from a premise check's hand-off; only a done hand-off has one."""
    if str(result.get("status") or "") != "done":
        return ""
    v = result.get("premise")
    if isinstance(v, bool):
        return "holds" if v else "fails"
    return VERDICTS.get(str(v or "").strip().lower(), "")


def question_spec(task: dict, check: str) -> str:
    spec = task.get("spec") or ""
    if len(spec) > SPEC_CHARS:
        spec = spec[:SPEC_CHARS] + " ..."
    return (f"Premise check for code task #{task['id']} \"{task['title']}\", before it is run. Check only "
            f"by reading (records, files, git history, command output): change no file, commit, push or "
            f"queue nothing.\nThe premise to check: {check}\n"
            f"Hand off status done with \"premise\": \"holds\" or \"fails\" in result.json and the evidence "
            f"(records, commits, file:line, output) in \"summary\", 2-4 sentences. Say \"fails\" only when "
            f"the evidence shows it does not hold (for example the feature already exists or there is "
            f"nothing to fix); when unsure, \"holds\": the code task then runs and reads your summary.\n\n"
            f"The code task's spec, for context:\n{spec}")


def _split(db, task: dict, check: str) -> int | None:
    """Queue `task`'s premise check and make `task` wait on it; None when another pass did already."""
    with db.tx():
        t = db.task(task["id"])
        if not t or t["status"] != "queued" or any(str(x).startswith(LABEL) for x in _labels(t)):
            return None
        try:
            deps = json.loads(t["depends_on"] or "[]")
        except ValueError:
            deps = []
        deps = deps if isinstance(deps, list) else [deps]
        qid = db.add_task(f"premise check for #{t['id']}: {t['title']}", question_spec(t, check),
                          kind="question", tier="light", priority=t["priority"], provider=t["provider"],
                          origin="daemon", labels=[f"{CHECK_LABEL}{t['id']}"], tier_source="premise_check")
        db.update_task(t["id"], depends_on=[*deps, qid], labels=[*_labels(t), f"{LABEL}{qid}"])
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (time.time(), "daemon", "premise_check", "low",
              f"#{t['id']} {t['title']}: its premise is checked first by question #{qid} (light); "
              f"it starts only if the premise holds", "handled", t["id"]))
    return qid


def _settle(db, task: dict, qid: int) -> str | None:
    """Act on question `qid`'s outcome for code task `task`; the verdict, or None (not ended yet)."""
    from . import pushq
    q = db.task(qid)
    if q and q["status"] not in TERMINAL_TASK_STATES:
        return None
    res = load_result(q["result"]) if q else {}
    got = verdict(res) if q and q["status"] == "done" else ""
    summary = str(res.get("summary") or "").strip()
    now = time.time()
    with db.tx():
        t = db.task(task["id"])
        labels = _labels(t) if t else []
        if not t or t["status"] not in ("queued", "blocked") or f"{LABEL}{qid}" not in labels:
            return None
        labels = [f"{LABEL}{qid}:{got or 'unknown'}" if x == f"{LABEL}{qid}" else x for x in labels]
        try:
            deps = json.loads(t["depends_on"] or "[]")
        except ValueError:
            deps = []
        deps = [d for d in (deps if isinstance(deps, list) else [deps]) if str(d) != str(qid)]
        if got == "fails":
            line_ = f"Not needed: premise check #{qid} found the premise does not hold. {summary}".strip()
            result = {"status": "done", "outcome": "rejected", "summary": line_[:1500], "premise_check": qid}
            db.update_task(t["id"], status="done", blocked_reason=None, labels=labels, depends_on=deps,
                           result=dump_result(result))
            db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                 (now, f"task:{t['id']}", "task_done", "normal",
                  f"#{t['id']} {t['title']} → done, not run ({line_})"[:1500], "queued", t["id"]))
            pushq._reply(db, t, "done", result["summary"])
            return got
        if got == "holds":
            note = f"Premise check #{qid}: holds. {summary[:SUMMARY_CHARS]}".strip()
        else:
            why = "gave no verdict" if q and q["status"] == "done" else f"ended {q['status'] if q else 'missing'}"
            note = f"Premise check #{qid} {why}: check the premise yourself before changing code."
        upd = {"spec": f"{t['spec'].rstrip()}\n\n{note}", "labels": labels, "depends_on": deps}
        if t["status"] == "blocked" and str(t["blocked_reason"] or "").startswith(f"dependency #{qid} "):
            upd.update(status="queued", blocked_reason=None)   # reconcile_tasks blocked it on the dead check
        db.update_task(t["id"], **upd)
    return got or "unknown"


def tend(db) -> list[tuple[int, str]]:
    """Split, settle and clean up premise checks (see the module doc); (code task id, what) per action."""
    done: list[tuple[int, str]] = []
    for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status IN ('queued','blocked') "
                  "AND (spec LIKE '%premise:%' OR labels LIKE '%\"premise:%')"):
        labels = [str(x) for x in _labels(t)]
        pend = next((int(m.group(1)) for x in labels if (m := PENDING.match(x))), None)
        if pend is not None:
            if (got := _settle(db, t, pend)) is not None:
                done.append((t["id"], f"premise {got} (#{pend})"))
            continue
        if t["status"] != "queued" or any(x.startswith((LABEL, "continues:")) for x in labels) \
                or int(t["attempts"] or 0) or not (check := line(t["spec"])):
            continue
        if db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1", (t["id"],)):
            continue
        if (qid := _split(db, t, check)) is not None:
            done.append((t["id"], f"premise check #{qid} queued"))
    for q in db.q("SELECT * FROM tasks WHERE kind='question' AND status='queued' AND labels LIKE ?",
                  (f'%"{CHECK_LABEL}%',)):
        owner = next((x[len(CHECK_LABEL):] for x in map(str, _labels(q)) if x.startswith(CHECK_LABEL)), "")
        t = db.task(int(owner)) if owner.isdigit() else None
        if t and t["status"] in TERMINAL_TASK_STATES and not db.one(
                "SELECT 1 FROM runs WHERE task=? LIMIT 1", (q["id"],)):
            db.x("UPDATE tasks SET status='cancelled', updated=? WHERE id=? AND status='queued'", (time.time(), q["id"]))
            done.append((t["id"], f"premise check #{q['id']} cancelled: #{t['id']} {t['status']}"))
    return done

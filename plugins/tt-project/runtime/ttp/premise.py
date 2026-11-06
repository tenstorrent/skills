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
  - `"premise": "fails"` with full cover (`gaps`: a `covered` entry, each naming a condition and a
    file:line, for every condition, case or branch the premise or code spec names, and no `uncovered`):
    the code task is closed done as not needed (outcome rejected), the question's summary as its
    result, with the usual task_done event for the coordinator (and a chat reply). A "fails" short of
    that settles as holds and the code spec names the uncovered conditions: a wrong "already exists"
    would drop real work. A "fails" naming absent targets in `missing` (and no `uncovered`) needs no cover;
  - no verdict, or the question failed or was cancelled: the code task runs anyway and its spec says
    the check gave no verdict, so the code run checks the premise itself (no work is lost).
  The label becomes `premise:<id>:holds|fails|unknown`, so each code task is settled once.
- urgent (urgent.is_urgent: priority 1 or an `urgent:` line): no separate question first. The code
  task's spec gets a note to check the premise as its own first step (and to change nothing if it
  fails), its label becomes `premise:self` (`premise:<id>:self` for a question queued before it
  turned urgent, which is cancelled if it never started; one that started is settled as above).
- a pending question whose code task ended (done, failed, cancelled) and that has not started is
  cancelled: nobody reads its answer.

A premise a check proved false (a "fails" that closes its code task) is remembered for REPEAT_DAYS:
the coordinator's task_add refuses a new task whose `premise:` line repeats it (`repeat`: every
commit it names is one the failed premise named, or the same words where it names none), naming the
check, unless the spec says how it differs from that check and is sent with force.

While it waits, the code task's own check is no lane holder (lanewait.holders): it raises no
lane_wait event. The question is a task of its own: a stuck run of it is stopped at its tier's
stall_s and its no_progress key names it, and one dispatch never starts is Daemon.check_stall's.

The question's own done event is handled (it starts no coordinator turn): what follows is the code
task's own run or its close.
"""
from __future__ import annotations

import json
import re
import time

from .db import TERMINAL_TASK_STATES, dump_result, load_result
from .urgent import is_urgent

LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]+)?premise:[ \t]*(\S.*?)[ \t]*$")
CHECK_LABEL = "premise-check:"   # on the question: + the code task's id
LABEL = "premise:"                # on the code task: + the question's id, then + ":" + its verdict
PENDING = re.compile(r"^premise:(\d+)$")
VERDICTS = {"holds": "holds", "held": "holds", "true": "holds", "yes": "holds",
            "fails": "fails", "failed": "fails", "false": "fails", "no": "fails"}
CITE = re.compile(r"[\w+-][\w./+-]*[./][\w+-]+:\d+")   # file:line, a path with a dot or a slash
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


def pending(task: dict) -> int | None:
    """The id of the premise check `task` still waits on (its `premise:<id>` label, not yet settled), or None."""
    return next((int(m.group(1)) for x in _labels(task) if (m := PENDING.match(str(x)))), None)


def is_check(task: dict) -> bool:
    """Whether `task` is a premise check the daemon queued for a code task."""
    return task.get("kind") == "question" and any(str(x).startswith(CHECK_LABEL) for x in _labels(task))


def verdict(result: dict) -> str:
    """holds, fails or "" (none) from a premise check's hand-off; only a done hand-off has one."""
    if str(result.get("status") or "") != "done":
        return ""
    v = result.get("premise")
    if v is None and str(result.get("outcome") or "").strip().lower() == "rejected":
        return "fails"   # the premise rejected in the hand-off's own words: still needs gaps()'s cover
    if isinstance(v, bool):
        return "holds" if v else "fails"
    return VERDICTS.get(str(v or "").strip().lower(), "")


def gaps(result: dict) -> list[str]:
    """Why a "fails" hand-off does not prove the code already covers the premise, or [] when it does: it
    needs a non-empty `covered` list whose every entry names a condition and cites file:line (a dict
    with `condition` and `at`, or a string holding both), and an empty `uncovered`. A "fails" because a
    named target is absent at the base ref (or a note path lies outside the repository) instead lists those
    targets in `missing`, with an empty `uncovered`: there is no code to cite."""
    out = []
    unc = result.get("uncovered")
    unc = [unc] if isinstance(unc, str) else unc if isinstance(unc, list) else []
    out += [f"uncovered: {str(x).strip()[:200]}" for x in unc if str(x).strip()]
    miss = result.get("missing")
    miss = [miss] if isinstance(miss, str) else miss if isinstance(miss, list) else []
    if not out and any(str(x).strip() for x in miss):
        return []   # a named target absent at the base ref: there is no code to cite
    cov = result.get("covered")
    if not isinstance(cov, list) or not cov:
        return out + ["no `covered` list citing file:line per condition"]
    for x in cov:
        if isinstance(x, dict):
            what = str(x.get("condition") or "").strip()
            at = str(x.get("at") or "").strip()
        else:
            what = at = str(x or "").strip()
        if not what or not CITE.search(at):
            out.append(f"no file:line for: {what[:200] or '(unnamed condition)'}")
    return out


REPEAT_DAYS = 14
_SHA = re.compile(r"(?<![\w/.-])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}(?![\w/-])")
_ASKED = re.compile(r"^The premise to check: (.*)$", re.M)


def _commits(text: str) -> frozenset:
    """The commits `text` names, by their first 7 hex digits (a short and a full sha are one)."""
    return frozenset(m.group(0)[:7] for m in _SHA.finditer(text.lower()))


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def repeat(db, spec: str, now: float | None = None) -> dict | None:
    """The newest premise check of the last REPEAT_DAYS that proved false the premise `spec`'s
    `premise:` line repeats, or None: a done check whose "fails" closes its code task (verdict and
    gaps, as _settle reads it), whose premise names every commit the new one names (or, naming none,
    has the same words). {id, task, ts, summary}."""
    check = line(spec)
    if not check:
        return None
    mine, said = _commits(check), _words(check)
    since = (time.time() if now is None else now) - REPEAT_DAYS * 86400
    for q in db.q("SELECT id, spec, labels, result, COALESCE(updated, created, 0) ts FROM tasks WHERE "
                  "kind='question' AND status='done' AND labels LIKE ? AND COALESCE(updated, created, 0) >= ? "
                  "ORDER BY ts DESC, id DESC LIMIT 400", (f'%"{CHECK_LABEL}%', since)):
        res = load_result(q["result"])
        if verdict(res) != "fails" or gaps(res) or not (m := _ASKED.search(q["spec"] or "")):
            continue
        theirs = m.group(1)
        if (mine and mine <= _commits(theirs)) or (not mine and said and said == _words(theirs)):
            owner = next((x[len(CHECK_LABEL):] for x in map(str, _labels(q)) if x.startswith(CHECK_LABEL)), "")
            return {"id": q["id"], "task": owner, "ts": float(q["ts"]),
                    "summary": str(res.get("summary") or "").strip()}
    return None


def rejection(hit: dict) -> str:
    """task_add's refusal of a premise `repeat` found."""
    return (f"premise already proved false by premise check #{hit['id']}"
            + (f" (for #{hit['task']})" if hit["task"] else "")
            + f": {hit['summary'][:300]}. Do not queue it again; if this is really new, add "
              f"'differs from #{hit['id']}: <how>' to the spec and resend with force: true")


def question_spec(task: dict, check: str) -> str:
    spec = task.get("spec") or ""
    if len(spec) > SPEC_CHARS:
        spec = spec[:SPEC_CHARS] + " ..."
    return (f"Premise check for code task #{task['id']} \"{task['title']}\", before it is run. Check only "
            f"by reading (records, files, git history, command output): change no file, commit, push or "
            f"queue nothing, and run only commands that write nothing outside your run folder. An "
            f"experiment that feeds tool payloads to hooks or guards, or needs a file at a credential-like "
            f"path, runs with HOME, XDG_CONFIG_HOME and GH_CONFIG_DIR set to a fresh `mktemp -d`, never "
            f"the live home.\nThe premise to check: {check}\n"
            f"First confirm that each file, function or symbol the premise, the spec or its upstream note "
            f"names exists at the base ref the code task branches from (the project's push branch), with "
            f"git ls-tree or git grep on that ref. When a named target is absent and only an adjacent "
            f"or similarly named one exists, the premise fails: say \"fails\" and name what you found "
            f"instead, never pass it by pointing at the nearby code. When an upstream note names paths "
            f"outside the plugin or this repository (such as the sending project's own harness files), "
            f"say so, answer \"fails\" and recommend closing the note won't-fix.\n"
            f"Hand off status done with \"premise\": \"holds\" or \"fails\" in result.json and the evidence "
            f"(records, commits, file:line, output) in \"summary\", 2-4 sentences. Say \"fails\" only when "
            f"the evidence shows it does not hold (for example a named target is missing, the feature "
            f"already exists or there is nothing to fix); when unsure, \"holds\": the code task then runs "
            f"and reads your summary.\n"
            f"A \"fails\" for a named target that is absent at the base ref, or a note path outside this "
            f"repository, lists each such target in \"missing\": [\"...\"]. Any other \"fails\" must prove "
            f"full cover: list every condition, case or branch the premise or the "
            f"spec below names (each storage, platform, mode, path or error case) in \"covered\": "
            f"[{{\"condition\": \"...\", \"at\": \"path/file.py:123\"}}], each with the file:line of the "
            f"code that already handles it. Code covering only some of them is no \"fails\": answer "
            f"\"holds\" and list the rest in \"uncovered\". A \"fails\" with any condition uncited is "
            f"read as \"holds\" and the code task runs.\n\n"
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
    short = gaps(res) if got == "fails" else []
    if short:
        got = "holds"   # a "fails" short of full cover drops no work: the code task runs
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
        if short:
            note = (f"Premise check #{qid}: answered fails without file:line cover for every condition, so "
                    f"it counts as holds; check these yourself: {'; '.join(short)[:SUMMARY_CHARS]}. "
                    f"Its summary: {summary[:SUMMARY_CHARS]}").strip()
        elif got == "holds":
            note = f"Premise check #{qid}: holds. {summary[:SUMMARY_CHARS]}".strip()
        else:
            why = "gave no verdict" if q and q["status"] == "done" else f"ended {q['status'] if q else 'missing'}"
            note = f"Premise check #{qid} {why}: check the premise yourself before changing code."
        upd = {"spec": f"{t['spec'].rstrip()}\n\n{note}", "labels": labels, "depends_on": deps}
        if t["status"] == "blocked" and str(t["blocked_reason"] or "").startswith(f"dependency #{qid} "):
            upd.update(status="queued", blocked_reason=None)   # reconcile_tasks blocked it on the dead check
        db.update_task(t["id"], **upd)
    return got or "unknown"


SELF = "self"   # verdict label of an urgent task that checks its premise in its own run


def _self(db, task: dict, check: str, qid: int | None = None) -> bool:
    """Make urgent `task` check its premise as its own first step: a spec note and the `premise:self`
    label (`premise:<qid>:self`: its queued question `qid`, never started, is cancelled). False when
    another pass did already, or the question started meanwhile."""
    now = time.time()
    with db.tx():
        t = db.task(task["id"])
        labels = _labels(t) if t else []
        if not t or t["status"] not in ("queued", "blocked"):
            return False
        if qid is None:
            if any(str(x).startswith(LABEL) for x in labels):
                return False
            labels = [*labels, f"{LABEL}{SELF}"]
        else:
            q = db.task(qid)
            if f"{LABEL}{qid}" not in labels or not q or q["status"] != "queued" or db.one(
                    "SELECT 1 FROM runs WHERE task=? LIMIT 1", (qid,)):
                return False
            db.x("UPDATE tasks SET status='cancelled', updated=? WHERE id=? AND status='queued'", (now, qid))
            labels = [f"{LABEL}{qid}:{SELF}" if x == f"{LABEL}{qid}" else x for x in labels]
        try:
            deps = json.loads(t["depends_on"] or "[]")
        except ValueError:
            deps = []
        deps = [d for d in (deps if isinstance(deps, list) else [deps]) if qid is None or str(d) != str(qid)]
        note = (f"Premise (urgent: no separate check first): as your first step, check that it holds: {check}. "
                f"If it does not, change no file and hand off done with outcome rejected and the evidence.")
        upd = {"spec": f"{t['spec'].rstrip()}\n\n{note}", "labels": labels, "depends_on": deps}
        if t["status"] == "blocked" and str(t["blocked_reason"] or "").startswith(f"dependency #{qid} "):
            upd.update(status="queued", blocked_reason=None)
        db.update_task(t["id"], **upd)
        db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", "premise_check", "low",
              f"#{t['id']} {t['title']}: urgent, so it checks its premise as its own first step"
              + (f" (question #{qid} cancelled)" if qid is not None else ""), "handled", t["id"]))
    return True


def tend(db) -> list[tuple[int, str]]:
    """Split, settle and clean up premise checks (see the module doc); (code task id, what) per action."""
    done: list[tuple[int, str]] = []
    for t in db.q("SELECT * FROM tasks WHERE kind='code' AND status IN ('queued','blocked') "
                  "AND (spec LIKE '%premise:%' OR labels LIKE '%\"premise:%')"):
        labels = [str(x) for x in _labels(t)]
        pend = pending(t)
        if pend is not None:
            if is_urgent(t) and (check := line(t["spec"])) and _self(db, t, check, pend):
                done.append((t["id"], f"premise checked in its own run (#{pend} cancelled)"))
            elif (got := _settle(db, t, pend)) is not None:
                done.append((t["id"], f"premise {got} (#{pend})"))
            continue
        if t["status"] != "queued" or any(x.startswith((LABEL, "continues:")) for x in labels) \
                or int(t["attempts"] or 0) or not (check := line(t["spec"])):
            continue
        if db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1", (t["id"],)):
            continue
        if is_urgent(t):
            if _self(db, t, check):
                done.append((t["id"], "premise checked in its own run"))
        elif (qid := _split(db, t, check)) is not None:
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

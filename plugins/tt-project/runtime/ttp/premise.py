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

A premise command (model-free): a code spec line `premise_cmd: <read-only shell command>` (only
that key runs; a `premise:` line is always words for the light question) is run by the daemon itself
(Daemon._premise_cmds), from the project root with devices masked, before each dispatch: a ready code
task with one starts only once the command exited 0 within `premise.recheck_min` (default 30) minutes,
so the first dispatch and every later one after a longer wait, retries included, re-run it first.
Exit 0: the premise holds and the task dispatches. Exit 1: the premise is false: the task closes done
as not needed without a worker run (cmd_false: outcome rejected, the command, its exit code and
output tail in its result, label `premise:cmd:fails`, one task_done event). Any other exit, a
timeout (`premise.cmd_timeout_s`, default 60), a command that cannot start or one guard() refuses
(it must be one local, read-only command: no remote or machine access, no file writes) is unknown
(cmd_unknown): the task gets the `premise-cmd:unknown` label, runs no command again and takes
today's path (the light question, or its own first step when urgent or already checked once).
Projects map their own scripts onto 0 and 1 with a wrapper.

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
CMD_LINE = re.compile(r"(?im)^[ \t]*(?:[-*][ \t]+)?premise_cmd:[ \t]*(\S.*?)[ \t]*$")
CMD_FAILS = f"{LABEL}cmd:fails"        # a code task its premise command closed
CMD_UNKNOWN = "premise-cmd:unknown"    # its premise command gave no verdict: today's path takes over
CMD_TIMEOUT_S = 60
RECHECK_MIN = 30
CITE = re.compile(r"[\w+-][\w./+-]*[./][\w+-]+:\d+")   # file:line, a path with a dot or a slash
SPEC_CHARS = 1500   # of the code spec quoted in the question, for context
SUMMARY_CHARS = 800  # of the question's summary carried into the code spec


def line(spec: str | None) -> str:
    """The check a spec's first `premise:` line names, or ""."""
    m = LINE.search(spec or "")
    return m.group(1) if m else ""


def command(spec: str | None) -> str:
    """The shell command a spec's first `premise_cmd:` line names, or ""."""
    m = CMD_LINE.search(spec or "")
    return m.group(1) if m else ""


def check_text(spec: str | None) -> str:
    """What a light question checks: the `premise:` line, else the premise command's own claim."""
    cmd = command(spec)
    return line(spec) or (f"the premise command `{cmd}` would exit 0 (the premise holds), not 1 (it is false)"
                          if cmd else "")


# Remote or machine access, and writes: a premise command reads local state only.
_REMOTE = re.compile(r"\b(?:ssh|scp|rsync|docker|kubectl|srun|sbatch|salloc|devq|tt-smi)\b|--remote\b|--host\b")
_WRITES = re.compile(
    r"(?<![<>&])\d*>>?(?![&>])\s*(?!/dev/null\b)[^\s&|;)]|\btee\b|"
    r"\b(?:rm|mv|cp|dd|chmod|chown|truncate|install|mkdir|touch|ln)\b(?![.-])|\bsed\s+(?:-\w*\s+)*-i|"
    r"\bgit(?:\s+-[Cc]\s+\S+|\s+--?[\w-]+(?:=\S+)?)*\s+(?:push|commit|reset|checkout|switch|merge|rebase|pull|"
    r"fetch|tag|stash|clean|am|apply|cherry-pick|branch\s+-[dDmM]|worktree\s+(?:add|remove|prune)|update-ref|gc)"
    r"(?![\w-])|\bttp\s+(?!landed\b|task\s+\S+\s+(?:show|list)\b|lock\s+--probe\b|checks\s+--result\b)\w")


def guard(cmd: str) -> str:
    """Why premise command `cmd` may not run (the start_when probe checks: one POSIX sh command, not
    always true; and local and read-only: no remote or machine access, no file writes), or ""."""
    from .coordinator import check_probe
    try:
        check_probe(cmd, "premise_cmd")
    except ValueError as e:
        return str(e)
    if m := _REMOTE.search(cmd):
        return f"premise_cmd runs locally only: it may not reach a machine or device ({m.group(0).strip()!r})"
    if m := _WRITES.search(cmd):
        return f"premise_cmd must be read-only: it may not write ({m.group(0).strip()!r})"
    return ""


def cmd_config(cfg: dict | None) -> tuple[float, float]:
    """(timeout s, recheck s) of premise commands from config `premise`."""
    c = (cfg or {}).get("premise") or {}
    try:
        timeout = float(c.get("cmd_timeout_s") or CMD_TIMEOUT_S)
        recheck = float(c.get("recheck_min") or RECHECK_MIN) * 60
    except (TypeError, ValueError):
        timeout, recheck = CMD_TIMEOUT_S, RECHECK_MIN * 60
    return max(1.0, timeout), max(0.0, recheck)


def runs_cmd(task: dict) -> str:
    """The premise command the daemon runs before dispatching `task`, or "": a code task's, until it
    gave no verdict once."""
    if task.get("kind") != "code" or CMD_UNKNOWN in map(str, _labels(task)):
        return ""
    return command(task.get("spec"))


def _event(db, t: dict, kind: str, severity: str, text: str, status: str, now: float) -> None:
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
         (now, f"task:{t['id']}" if kind == "task_done" else "daemon", kind, severity, text[:1500], status, t["id"]))


def cmd_false(db, task: dict, cmd: str, rc, tail: str, now: float | None = None) -> bool:
    """Close queued code task `task` as not needed: its premise command exited 1. False when it is no
    longer queued (another pass, a cancel or a start won)."""
    from . import pushq
    now = time.time() if now is None else now
    tail = (tail or "").strip()[-600:]
    words = (f"Not needed: its premise command exited {rc}, so the premise is false: `{cmd[:300]}`"
             + (f". Its output: {tail}" if tail else ""))
    result = {"status": "done", "outcome": "rejected", "summary": words[:1500],
              "premise_cmd": {"cmd": cmd, "rc": rc, "tail": tail}}
    with db.tx():
        t = db.task(task["id"])
        if not t or t["status"] != "queued":
            return False
        db.update_task(t["id"], status="done", blocked_reason=None, labels=[*_labels(t), CMD_FAILS],
                       result=dump_result(result))
        _event(db, t, "task_done", "normal", f"#{t['id']} {t['title']} → done, not run ({words})", "queued", now)
        pushq._reply(db, t, "done", result["summary"])
    return True


def cmd_unknown(db, task: dict, cmd: str, why: str, now: float | None = None) -> bool:
    """Premise command `cmd` of `task` gave no verdict (`why`): label it CMD_UNKNOWN, so tend takes
    today's path. A task whose premise was already checked once (a `premise:` label) instead checks it
    as its own first step. False when it is no longer queued or was handled already."""
    now = time.time() if now is None else now
    with db.tx():
        t = db.task(task["id"])
        labels = _labels(t) if t else []
        if not t or t["status"] != "queued" or CMD_UNKNOWN in labels:
            return False
        upd: dict = {"labels": [*labels, CMD_UNKNOWN]}
        # tend splits only a task never run nor checked: any other checks its premise in its own run
        checked = any(str(x).startswith((LABEL, "continues:")) for x in labels) or bool(int(t["attempts"] or 0)) \
            or bool(db.one("SELECT 1 FROM runs WHERE task=? AND role!='coordinator' LIMIT 1", (t["id"],)))
        if checked:
            upd["spec"] = (f"{t['spec'].rstrip()}\n\nPremise command `{cmd[:300]}` gave no verdict ({why[:300]}): "
                           f"as your first step, check that the premise still holds. If it does not, change no "
                           f"file and hand off done with outcome rejected and the evidence.")
        db.update_task(t["id"], **upd)
        _event(db, t, "premise_check", "low",
               f"#{t['id']} {t['title']}: its premise command gave no verdict ({why[:300]}); "
               + ("it checks the premise as its own first step" if checked else
                  "it falls back to the premise check"), "handled", now)
    return True


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
    check, cmd = line(spec), command(spec)
    if not check and not cmd:
        return None
    mine, said = _commits(check), _words(check)
    since = (time.time() if now is None else now) - REPEAT_DAYS * 86400
    if cmd:   # a premise command that exited 1 for another task: the same command (spacing aside)
        for t in db.q("SELECT id, title, result, COALESCE(updated, created, 0) ts FROM tasks WHERE kind='code' "
                      "AND status='done' AND labels LIKE ? AND COALESCE(updated, created, 0) >= ? "
                      "ORDER BY ts DESC, id DESC LIMIT 400", (f'%"{CMD_FAILS}"%', since)):
            res = load_result(t["result"])
            if " ".join(str((res.get("premise_cmd") or {}).get("cmd") or "").split()) == " ".join(cmd.split()):
                return {"id": t["id"], "task": "", "ts": float(t["ts"]), "cmd": True,
                        "summary": str(res.get("summary") or "").strip()}
    if not check:
        return None
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
    return (f"premise already proved false by "
            + (f"task #{hit['id']}'s premise command" if hit.get("cmd") else f"premise check #{hit['id']}")
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
                  "AND (spec LIKE '%premise%' OR labels LIKE '%\"premise:%')"):
        labels = [str(x) for x in _labels(t)]
        pend = pending(t)
        if pend is not None:
            if is_urgent(t) and (check := check_text(t["spec"])) and _self(db, t, check, pend):
                done.append((t["id"], f"premise checked in its own run (#{pend} cancelled)"))
            elif (got := _settle(db, t, pend)) is not None:
                done.append((t["id"], f"premise {got} (#{pend})"))
            continue
        if t["status"] != "queued" or any(x.startswith((LABEL, "continues:")) for x in labels) \
                or int(t["attempts"] or 0) or runs_cmd(t) or not (check := check_text(t["spec"])):
            continue   # a premise command runs model-free first (Daemon._premise_cmds)
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

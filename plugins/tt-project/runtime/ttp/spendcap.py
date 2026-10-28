# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free spend cap for a detached job that calls a paid API: `ttp detach --spend-cmd <cmd>
--spend-cap <usd> [--spend-every <s>]`.

The job's detached.json entry keeps {"spend": {"cmd", "cap", "every"}} plus this module's state
("last", "read", "fails", "fail_since", "fail_raised", "capped"). While the job runs, the daemon's tick
(tend) runs the command every `every` seconds in a shell, from the project root, with the environment
of the daemon's other shell commands (project.command_env), within TIMEOUT_S. It must print one number,
the US dollars the job spent so far (a leading `$` is allowed). Then:

- read >= cap: the job's group is stopped as jobstop.end stops an ended task's job, a `failure` progress
  report keyed by the job's name says "spend cap", and ONE normal event (CAP_EVENT) to the coordinator
  names the job, the read and the cap. The job is never checked again.
- a failed read (non-zero exit, timeout, no single number): counted; FAIL_LIMIT in a row raise one
  normal event (FAIL_EVENT) and leave the job running. A good read resets the count, so a later streak
  raises a new event.

Nothing the command prints, nor its environment or its text (which may run `ttp secret env`), goes into
a log, an event or the registry state: only the number read and a fixed failure reason.
A job started without these options has no "spend" entry and is never touched here.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from . import boundedchild, detach, jobstop, progress
from .project import Project, command_env

DEFAULT_EVERY_S = 300
MIN_EVERY_S = 60
TIMEOUT_S = 20
FAIL_LIMIT = 3
SCAN_EVERY_S = 30   # the daemon looks for due checks at most this often
CAP_EVENT = "detached_job_spend_cap"
FAIL_EVENT = "detached_job_spend_check_failed"
_USD = re.compile(r"\$?\s*([0-9]+(?:\.[0-9]+)?)")


def options_error(cmd, cap, every) -> str:
    """Why these `ttp detach` options are refused, or "" when they are fine (all None: no cap)."""
    if cmd is None and cap is None:
        return "--spend-every goes with --spend-cmd and --spend-cap" if every is not None else ""
    if cmd is None or cap is None:
        return ("--spend-cmd and --spend-cap go together: the command prints the job's spend in USD so far, "
                "the cap stops the job once a read reaches it")
    if not str(cmd).strip():
        return "--spend-cmd needs a command that prints one USD number"
    if not (math.isfinite(cap) and cap > 0):
        return "--spend-cap needs a positive USD amount"
    if every is not None and every < MIN_EVERY_S:
        return f"--spend-every must be at least {MIN_EVERY_S} s"
    return ""


def entry(cmd: str, cap: float, every: int | None) -> dict:
    return {"cmd": cmd, "cap": float(cap), "every": int(every or DEFAULT_EVERY_S)}


def parse(out: str) -> float | None:
    """The one non-negative number the command printed, else None."""
    m = _USD.fullmatch((out or "").strip())
    if not m:
        return None
    v = float(m.group(1))
    return v if math.isfinite(v) else None


def read(p: Project, cmd: str, timeout: float | None = None) -> tuple[float | None, str]:
    """(USD, "") from one run of the spend command, or (None, why) with a fixed reason that never quotes
    what the command printed."""
    timeout = TIMEOUT_S if timeout is None else timeout
    r = boundedchild.run(cmd, timeout, shell=True, cwd=str(p.root), env=command_env(p), stderr="devnull", grace=1)
    if r.timed_out:
        return None, f"timed out after {timeout:g} s"
    if r.rc is None:
        return None, "could not start"
    if r.rc != 0:
        return None, f"exit {r.rc}"
    v = parse(r.out if isinstance(r.out, str) else r.out.decode(errors="replace"))
    return (v, "") if v is not None else (None, "its output was not one USD number")


def _run_id(p: Project, rc: Path) -> int | None:
    d = rc.parent
    row = p.db.one("SELECT id FROM runs WHERE dir IN (?,?) ORDER BY id DESC LIMIT 1", (str(d), str(d.resolve())))
    return row["id"] if row else None


def _event(p: Project, kind: str, fp: str, text: str, data: dict, task: int | None) -> bool:
    if p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
        return False
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
           (time.time(), "daemon", kind, fp, "normal", text, json.dumps(data), "queued", task))
    return True


def _save(rc: Path, upd: dict) -> None:
    with detach.registry(rc.parent) as jobs:
        for j in jobs:
            if isinstance(j, dict) and j.get("rc") == str(rc) and isinstance(j.get("spend"), dict):
                j["spend"].update(upd)


def _cap(p: Project, rc: Path, name: str, usd: float, cap: float) -> str:
    t = jobstop.task_of(p, rc)
    tid = t["id"] if t else None
    why = f"spend cap: read ${usd:.2f} reached the cap ${cap:.2f}"
    r = jobstop.end(p, rc, why, tid)
    run = _run_id(p, rc)
    env = {progress.JOB_ENV: str(rc), "TTP_TASK": str(tid or ""), "TTP_RUN_ID": str(run or "")}
    try:
        progress.append(p.state, progress.record("failure", name, "spend cap", env=env))
    except OSError:
        pass
    text = (f"Detached job {name}" + (f" (task #{tid})" if tid else "") + f" passed its spend cap: read "
            f"${usd:.2f}, cap ${cap:.2f}. The daemon stopped its process group"
            + (f" {r['pgid']}" if r and r.get("pgid") else "") + " and recorded it as capped. Check the job's "
            "calls (prompt caching, call count) before starting it again.")
    _event(p, CAP_EVENT, f"spend_cap:{rc}", text, {"job": name, "rc": str(rc), "read": usd, "cap": cap}, tid)
    return f"spend cap: stopped detached job {name} at ${usd:.2f} (cap ${cap:.2f})"


def check(p: Project, rc: Path, j: dict, now: float) -> str | None:
    """One due check of one job: a log line, or None when nothing was due."""
    s = j["spend"]
    if s.get("capped") or now - float(s.get("last") or 0) < max(int(s.get("every") or DEFAULT_EVERY_S), MIN_EVERY_S):
        return None
    try:
        cap = float(s["cap"])
    except (KeyError, TypeError, ValueError):
        return None
    name = str(j.get("name") or rc.stem)
    usd, why = read(p, str(s.get("cmd") or ""))
    if usd is not None:
        if usd >= cap:   # stop first: a daemon that dies before the save finds the job gone next time
            line = _cap(p, rc, name, usd, cap)
            _save(rc, {"last": now, "read": usd, "fails": 0, "capped": True})
            return line
        _save(rc, {"last": now, "read": usd, "fails": 0, "fail_raised": False})
        return None
    fails = int(s.get("fails") or 0) + 1
    since = float(s.get("fail_since") or now) if fails > 1 else now
    upd = {"last": now, "fails": fails, "fail_since": since, "why": why}
    line = None
    if fails >= FAIL_LIMIT and not s.get("fail_raised"):
        t = jobstop.task_of(p, rc)
        tid = t["id"] if t else None
        text = (f"The spend check of detached job {name}" + (f" (task #{tid})" if tid else "") + f" failed {fails} "
                f"times in a row (last: {why}); the job keeps running with no spend cap until a read succeeds. "
                "Fix its --spend-cmd, or stop the job if its spend cannot be read.")
        _event(p, FAIL_EVENT, f"spend_check_failed:{rc}:{since:.0f}", text,
               {"job": name, "rc": str(rc), "fails": fails, "why": why, "cap": cap}, tid)
        upd["fail_raised"] = True
        line = f"spend check of detached job {name} failed {fails} times in a row ({why})"
    _save(rc, upd)
    return line


def tend(p: Project, now: float | None = None) -> list[str]:
    """The daemon's pass: each running job of the last detach.CLOSE_DAYS days with a spend entry whose
    check is due. The log lines of what it did."""
    now = time.time() if now is None else now
    out = []
    for reg in sorted(p.runs.glob("*/" + detach.REGISTRY)) if p.runs.is_dir() else []:
        try:
            if now - reg.stat().st_mtime > detach.CLOSE_DAYS * 86400:
                continue
            jobs = json.loads(reg.read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:
            if not (isinstance(j, dict) and j.get("rc") and isinstance(j.get("spend"), dict)) \
                    or j.get("status") in detach.CLOSED:
                continue
            rc = Path(j["rc"])
            if not jobstop.running(rc):
                continue
            if line := check(p, rc, j, now):
                out.append(line)
    return out

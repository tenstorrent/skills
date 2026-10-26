# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's own hand-off checks (project.json `worker.handoff_checks`): model-free commands the
daemon runs on each worker's hand-off before it accepts it (Daemon._finish_worker), so a rule that
must hold every time is tested before a reply posts or a review or push is queued, not after.

Each entry is {name, run, timeout_s (60), kinds (all)}. `run` is the project's own shell command, run
from the project root; the hand-off's path is in TTP_RESULT_JSON (with TTP_TASK_ID and TTP_TASK_KIND)
and replaces a `{result}` placeholder, shell-quoted. Nothing else from the task reaches the command
line. Exit 0 passes; 1 fails: the run is sent back with the check's output, at most
`worker.handoff_check_max_sendbacks` times (2) per task, then the task goes to review for the
coordinator, its result kept. Any other exit or a timeout is a broken check: the hand-off is accepted
(a broken hook never costs work or a message) and the coordinator hears once per check signature.

Checks run on a hand-off whose status is done, needs_review, blocked or failed (a wait posts nothing
and its later hand-off is checked). They run before the transaction that records the hand-off
(prefetch, from Daemon._prefetch_lint), so the write lock never waits on them; verdict reads that
answer and runs them itself only when prefetch did not."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
import time
from pathlib import Path

from .project import Project, durable_write

KEY = "handoff_check_sendbacks"   # in the task's result: send-backs so far
BROKEN_EVENT = "handoff_check_broken"
STATUSES = ("done", "needs_review", "blocked", "failed")
DEFAULT_TIMEOUT_S = 60
DEFAULT_MAX_SENDBACKS = 2
OUTPUT_CHARS = 2000               # the tail of a check's output the next run and the coordinator get
EVENT_OUTPUT_CHARS = 1000         # of it, in a coordinator event
INPUT_FILE = "handoff_check.json"   # the hand-off checked, when it is not the run's result.json as is
SEEN_TTL_S = 300
KILL_WAIT_S = 5                   # the wait for a killed check's output; an escaped child is not waited for
_seen: dict[tuple[str, int, str], tuple[float, dict]] = {}


def rules(cfg: dict) -> tuple[list[dict], list[str]]:
    """The configured checks, normalised, and one problem line per entry skipped as malformed."""
    worker = cfg.get("worker") if isinstance(cfg.get("worker"), dict) else {}
    raw = worker.get("handoff_checks")
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["worker.handoff_checks: not a list; ignored"]
    out, problems = [], []
    for i, e in enumerate(raw):
        where = f"worker.handoff_checks[{i}]"
        try:
            if not isinstance(e, dict):
                raise ValueError("not an object")
            run = e.get("run")
            if not isinstance(run, str) or not run.strip():
                raise ValueError("run must be a non-empty command")
            t = e.get("timeout_s", DEFAULT_TIMEOUT_S)
            if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
                raise ValueError("timeout_s must be a positive number of seconds")
            kinds = e.get("kinds")
            if isinstance(kinds, str):
                kinds = [kinds]
            if kinds is not None and (not isinstance(kinds, list) or not all(isinstance(k, str) and k for k in kinds)):
                raise ValueError("kinds must be a list of task kinds")
            name = e.get("name")
            if name is not None and not isinstance(name, str):
                raise ValueError("name must be a string")
            out.append({"name": (name or "").strip() or where, "run": run.strip(), "timeout_s": float(t),
                        "kinds": set(kinds) if kinds else None})
        except ValueError as err:
            problems.append(f"{where}: {err}; skipped")
    cap = worker.get("handoff_check_max_sendbacks")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        problems.append(f"worker.handoff_check_max_sendbacks: {cap!r} is not a whole number >= 0; "
                        f"{DEFAULT_MAX_SENDBACKS} is used")
    return out, problems


def max_sendbacks(cfg: dict) -> int:
    v = (cfg.get("worker") or {}).get("handoff_check_max_sendbacks") if isinstance(cfg.get("worker"), dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else DEFAULT_MAX_SENDBACKS


def applies(cfg: dict, task: dict, handoff) -> list[dict]:
    """The checks that run on `task`'s hand-off: none unless it is a finished one (STATUSES)."""
    if not isinstance(handoff, dict) or handoff.get("status") not in STATUSES:
        return []
    kind = task.get("kind") or "work"
    return [c for c in rules(cfg)[0] if c["kinds"] is None or kind in c["kinds"]]


def _digest(handoff: dict) -> str:
    return hashlib.sha256(json.dumps(handoff, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _input_file(handoff: dict, run_dir: Path | None) -> Path:
    """The run's result.json when it holds `handoff` as is, else a copy of `handoff` in the run dir."""
    if run_dir is not None:
        own = run_dir / "result.json"
        try:
            if json.loads(own.read_text()) == handoff:
                return own
        except (OSError, ValueError):
            pass
        try:
            durable_write(run_dir / INPUT_FILE, json.dumps(handoff, indent=1))
            return run_dir / INPUT_FILE
        except OSError:
            pass
    fd, path = tempfile.mkstemp(prefix="ttp-handoff-", suffix=".json")
    os.close(fd)
    durable_write(Path(path), json.dumps(handoff))
    return Path(path)


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= OUTPUT_CHARS else "…" + text[-OUTPUT_CHARS:]


def _run_one(p: Project, check: dict, task: dict, path: Path) -> tuple[int | str, str]:
    """(exit code, or "timeout" or "error: ..."; its output's tail). The command's process group is
    killed on a timeout."""
    env = dict(os.environ, TTP_RESULT_JSON=str(path), TTP_TASK_ID=str(task["id"]),
               TTP_TASK_KIND=str(task.get("kind") or "work"))
    cmd = check["run"].replace("{result}", shlex.quote(str(path)))
    from . import boundedchild
    res = boundedchild.run(cmd, check["timeout_s"], shell=True, cwd=str(p.root), env=env, text=False,
                           stderr="stdout", grace=KILL_WAIT_S)
    out = _tail(res.out.decode(errors="replace"))
    if res.timed_out:
        return "timeout", out
    if res.rc is None:
        return f"error: {res.error.removeprefix('could not start: ')}", ""
    return res.rc, out


def run(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None) -> dict:
    """Runs the checks that apply, in order, up to the first that fails. {"failed": {name, output} or
    None, "broken": [{name, run, why, output}]}; {} when none apply."""
    checks = applies(cfg, task, handoff)
    if not checks:
        return {}
    path = _input_file(handoff, run_dir)
    got: dict = {"failed": None, "broken": []}
    for c in checks:
        rc, output = _run_one(p, c, task, path)
        if rc == 0:
            continue
        if rc == 1:
            got["failed"] = {"name": c["name"], "output": output}
            break
        why = rc if isinstance(rc, str) else f"exit {rc}"
        got["broken"].append({"name": c["name"], "run": c["run"], "why": why, "output": output})
    return got


def prefetch(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None) -> None:
    """Runs the checks before the transaction that records `task`'s hand-off; verdict reads it."""
    if not applies(cfg, task, handoff):
        return
    now = time.time()
    for k in [k for k, (until, _) in _seen.items() if until <= now]:
        _seen.pop(k, None)
    got = run(p, cfg, task, handoff, run_dir)   # its expiry counts from the answer, however long the checks took
    _seen[(str(p.root), int(task["id"]), _digest(handoff))] = (time.time() + SEEN_TTL_S, got)


def verdict(p: Project, cfg: dict, task: dict, handoff, run_dir: Path | None) -> dict:
    """prefetch's answer for this hand-off, else the checks run now; {} when none apply."""
    if not applies(cfg, task, handoff):
        return {}
    hit = _seen.pop((str(p.root), int(task["id"]), _digest(handoff)), None)
    if hit and hit[0] > time.time():
        return hit[1]
    return run(p, cfg, task, handoff, run_dir)


def reason(failed: dict, n: int, cap: int) -> str:
    """What the sent-back run is told: the check, its output and how to pass it."""
    out = failed["output"] or "(no output)"
    return (f"the project's hand-off check '{failed['name']}' rejected your hand-off (send-back {n} of {cap}); "
            f"nothing was posted or queued. Fix what it names and write result.json again. Its output:\n{out}")


def event_tail(output: str) -> str:
    """A check's output as a coordinator event shows it: its last EVENT_OUTPUT_CHARS."""
    output = (output or "").strip() or "(none)"
    return output if len(output) <= EVENT_OUTPUT_CHARS else "…" + output[-EVENT_OUTPUT_CHARS:]


def broken_fingerprint(b: dict) -> str:
    sig = f"{b['name']}\0{b['run']}\0{b['why']}"
    return f"{BROKEN_EVENT}:{hashlib.sha256(sig.encode()).hexdigest()[:12]}"


def broken_text(task: dict, b: dict) -> str:
    return (f"The project's hand-off check '{b['name']}' is broken ({b['why']}; only exit 0 = pass and 1 = send "
            f"back are valid), first on #{task['id']} {task['title']}: hand-offs are accepted without it until it "
            f"is fixed (worker.handoff_checks). Command: {b['run'][:300]}. Its output:\n{event_tail(b['output'])}")

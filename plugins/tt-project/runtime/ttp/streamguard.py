# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Runner-side tool guard for task runs the harness hook does not reach (Cursor; Codex when its hooks
feature is off or another hook source keeps the harness hook out, providers/codex.py _hook_for).

The runner reads each tool call the run starts from its output stream (Provider.activity: a call's
"payloads" are the hook-shaped {tool_name, tool_input} the harness hook would get) and asks
hook.pre_tool_use about it in a child process with the run's own environment, model-free. On a deny it
writes GUARD_FILE (the guard's reason, never the command) and stops the run. The daemon then queues the
task once more with that reason at the top of its next prompt, no attempt spent; a second trip in a row
blocks it for the coordinator.

This is weaker than a hook's deny: the call is stopped once it has started, so a quick command may have
run. A call whose result is already in the stream when the check ends (the command finished) never stops
the run: the runner records a warning instead (WARN_FILE, also GUARD_LOG and progress.md), the run goes on
(a secret refusal of an untrusted run still stops it: must_stop), and the daemon turns the warnings into
one event when the run ends. Every record names the call's id and its stage (started: stopped while it
ran; finished: it had already run). Rewrites and hints the hook would add are not applied. Runs whose provider passes the harness hook
(Provider.harness_hook) or whose hook already ran when it started (hook.HOOKED_FILE) are left alone; the
marker is read before the agent starts, since the agent could write it itself."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

GUARD_FILE = "GUARD"     # in the run dir: why the runner stopped it ({"why", "tool", "id", "stage", "at"})
GUARD_LOG = "guard.jsonl"   # in the run dir: one line per refused call, stopped or finished, same fields
WARN_FILE = "GUARD_WARNINGS"   # in the run dir: refused calls that had already finished (JSON lines)
KEY = "guard_stop"       # in the task's result: the refusal its next run's prompt starts with
CHECK_TIMEOUT_S = 20     # as the hook's own timeout: a check that hangs lets the call through


def active(prov, argv: list[str], env: dict, hooked: bool) -> bool:
    """Whether the runner guards this run: a task run without the harness hook. `hooked`: the hook's
    marker as read before the agent started."""
    return bool(env.get("TTP_TASK")) and not prov.harness_hook(argv) and not hooked


def refusal(payload: dict, env: dict, cwd: str, call_id: str = "") -> str | None:
    """hook.pre_tool_use's deny reason for `payload` (call `call_id`, named in the hook's refusal records),
    asked in a child with the run's environment; None when it allows it (or the check fails: the hook also
    lets a call through on an error)."""
    if call_id and not payload.get("tool_use_id"):
        payload = {**payload, "tool_use_id": call_id}
    from .providers.base import isolated_module_argv
    runtime = str(Path(__file__).resolve().parents[1])
    child_env = {**env, "PYTHONPATH": runtime}
    try:   # isolated: never imports a ttp/ or json.py the agent wrote in its worktree (cwd)
        out = subprocess.run(isolated_module_argv("ttp.streamguard", runtime), input=json.dumps({**payload, "cwd": cwd}),
                             capture_output=True, text=True, env=child_env, cwd=cwd if os.path.isdir(cwd) else None,
                             timeout=CHECK_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _entry(why: str, tool: str, call_id: str, stage: str) -> dict:
    return {"why": why, "tool": tool, "id": call_id, "stage": stage, "at": time.time()}


def record(run_dir: Path, why: str, tool: str, call_id: str = "") -> None:
    """The run is being stopped at call `call_id`, which had started and not finished."""
    from .project import durable_append, durable_write
    got = _entry(why, tool, call_id, "started")
    durable_write(run_dir / GUARD_FILE, json.dumps(got))
    durable_append(run_dir / GUARD_LOG, json.dumps(got) + "\n")


def finished(calls: list[dict], done: set[str]) -> None:
    """Add the ids of the calls `calls` (Provider.activity events) shows finished to `done`."""
    done.update(str(c["id"]) for c in calls if c.get("kind") == "result" and c.get("id"))


def must_stop(why: str) -> bool:
    """Whether a refusal stops the run even when its call had already finished: a secret refusal of an
    untrusted run (untrusted.secret_refusal). Its secret is then in the agent's context, and stopping the
    run keeps a planted instruction from sending it out in the next call."""
    from . import untrusted
    return untrusted.secret_refusal(why)


def warn(run_dir: Path, why: str, tool: str, head: str = "", call_id: str = "") -> None:
    """Record a call the hook refuses that had already finished when the runner read it (fsync'd): the
    warning file the daemon reports from, the guard log and progress.md. Never a stop, so no GUARD_FILE."""
    from . import timefmt
    from .project import durable_append
    got = _entry(why, tool, call_id, "finished")
    try:
        with open(run_dir / WARN_FILE, "a") as f:
            f.write(json.dumps({**got, "head": head[:200]}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        durable_append(run_dir / GUARD_LOG, json.dumps(got) + "\n")
        durable_append(run_dir / "progress.md", f"{timefmt.clock()} guard: a {tool or 'tool'} call (id {call_id or 'none'}) "
                                                f"the harness refuses had already finished, so the run goes on: {why[:300]}\n")
    except OSError:
        pass


def take_warnings(run_dir: Path) -> list[dict]:
    """The warnings a run recorded (warn), oldest first, each taken once: the file is renamed aside."""
    path = run_dir / WARN_FILE
    try:
        lines = path.read_text().splitlines()
        path.replace(path.with_name(WARN_FILE + ".taken"))
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict) and got.get("why"):
            out.append(got)
    return out


def take(run_dir: Path) -> dict | None:
    """The guard stop a run ended with (its GUARD_FILE), or None."""
    try:
        got = json.loads((run_dir / GUARD_FILE).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("why") else None


def prompt_block(prev: dict) -> str:
    """The top of a run after a guard stop; "" when the last run was not stopped by one."""
    got = prev.get(KEY)
    if not isinstance(got, dict):
        return ""
    return ("# STOPPED BY A HARNESS GUARD\nYour last run of this task was stopped when it started a "
            f"{got.get('tool') or 'tool'} call the harness refuses: {got.get('why')}\nThis costs no attempt. "
            "Your worktree, branch and commits are kept: check `git status` and `git log`, then go on without "
            "that call. A second stop in a row blocks the task.\n")


def _main() -> int:
    from . import hook
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out, _ = hook.pre_tool_use(payload, stage="started")
    except Exception:
        return 0
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("permissionDecision") == "deny":
        print(str(spec.get("permissionDecisionReason") or "refused"))
    return 0


if __name__ == "__main__":
    sys.exit(_main())

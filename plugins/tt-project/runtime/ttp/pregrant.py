# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project's optional pre-grant check (config `locks.pre_grant_check`): a shell command for a
project that keeps its own holds on top of the resource locks, such as a repair that keeps a
resource for one task. Before the runtime grants a task a resource's lock it asks the command:
dispatch of a task naming the resource (`resource:` or `exclusive:`), the run supervisor's whole-run
take of an `exclusive:` resource and `ttp lock`.

The command runs from the project root with the resource and the task id appended as arguments and
set as TTP_RESOURCE and TTP_TASK_ID (empty outside a task), for at most `locks.pre_grant_timeout_s`
(default 10 s). It should be quick and read-only. Exit 0 grants the lock. Exit 1 or 75 means held:
the lock is not taken and the task waits as for a busy resource, spending no attempt. Any other exit,
or no answer in time, means the check is broken: held as well (a repair's hold is never run over),
and recorded under state/pre_grant/ so the daemon raises it once (Daemon.raise_pre_grant_faults).
Unset, nothing runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

HELD_RCS = (1, 75)
TIMEOUT_S = 10.0
DIR = "pre_grant"   # under the project's state dir: one record per broken (resource, exit) for the daemon


@dataclass
class Answer:
    granted: bool
    rc: int | None      # None: timed out or could not start
    broken: bool
    line: str           # one line for logs, progress and `ttp lock`'s refusal


def command(cfg: dict) -> str:
    """The configured check, or "" when unset (nothing runs)."""
    v = (cfg.get("locks") or {}).get("pre_grant_check") if isinstance(cfg.get("locks"), dict) else None
    return v.strip() if isinstance(v, str) else ""


def timeout_s(cfg: dict) -> float:
    try:
        v = float((cfg.get("locks") or {}).get("pre_grant_timeout_s") or TIMEOUT_S)
    except (TypeError, ValueError, AttributeError):
        return TIMEOUT_S
    return v if v > 0 else TIMEOUT_S


def check(cmd: str, root: Path | str, resource: str, task: str | int | None = "", state: Path | str | None = None,
          timeout: float = TIMEOUT_S) -> Answer:
    """Ask the pre-grant check `cmd` whether `task` may have `resource`'s lock now. A broken answer is
    recorded under `state` (record_fault) when given."""
    task = "" if task is None else str(task)
    env = {**os.environ, "TTP_RESOURCE": resource, "TTP_TASK_ID": task}
    from . import boundedchild
    res = boundedchild.run(["/bin/sh", "-c", cmd + ' "$@"', "pre-grant-check", resource, task], timeout,
                           cwd=str(root), env=env, text=False, stderr="stdout", grace=2)
    rc, out = res.rc, res.out.decode(errors="replace")
    if res.timed_out:
        out = f"no answer within {timeout:g} s"
    elif rc is None:
        out = res.error
    said = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")[:200]
    if rc == 0:
        return Answer(True, 0, False, f"{resource}: pre-grant check granted")
    if rc in HELD_RCS:
        return Answer(False, rc, False, f"{resource}: held by the project's pre-grant check (exit {rc})"
                                        + (f": {said}" if said else ""))
    how = f"exited {rc}" if rc is not None else said or "gave no answer"
    ans = Answer(False, rc, True, f"{resource}: held, the project's pre-grant check is broken ({how}); "
                                  f"fix config locks.pre_grant_check")
    if state is not None:
        record_fault(Path(state), cmd, resource, task, rc, said)
    return ans


def record_fault(state: Path, cmd: str, resource: str, task: str, rc: int | None, said: str) -> None:
    """One file per (resource, exit) under state/pre_grant: rewritten, never added to, on each retry."""
    d = state / DIR
    name = hashlib.sha1(f"{resource}\0{rc}".encode()).hexdigest()[:16] + ".json"
    rec = {"hook": cmd, "resource": resource, "task": task, "rc": rc, "said": said, "ts": time.time()}
    from .project import durable_write
    try:
        durable_write(d / name, json.dumps(rec))
    except OSError:
        pass   # the check still holds; only its report is lost


def take_faults(state: Path) -> list[dict]:
    """The recorded faults, each removed as it is read."""
    out = []
    for f in sorted((state / DIR).glob("*.json")) if (state / DIR).is_dir() else []:
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            rec = None
        try:
            f.unlink()
        except OSError:
            pass
        if isinstance(rec, dict) and rec.get("resource"):
            out.append(rec)
    return out


def fault_key(rec: dict) -> str:
    return f"pre-grant:{rec.get('resource')}:{rec.get('rc')}"


def fault_text(rec: dict) -> str:
    rc = rec.get("rc")
    how = f"exited {rc}" if rc is not None else (rec.get("said") or "gave no answer")
    task = f" for task #{rec['task']}" if rec.get("task") else ""
    return (f"Pre-grant check `{rec.get('hook')}` on {rec.get('resource')}{task} {how}: only exit 0 (grant), "
            f"1 or 75 (held) are answers, so the resource's lock is held for every task until it answers. "
            f"Fix the command in config locks.pre_grant_check, or unset it.")

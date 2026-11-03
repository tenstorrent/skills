# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp secret env` from a sandboxed worker (cli.cmd_secret_env).

The sandbox denies reading the secrets store, so the command cannot get its secrets there. Instead the
worker queues `ttp secret env ... -- <command>` as a detached job (detach.enqueue), which the daemon
starts outside the sandbox (detach.tend): that served copy reads the store, puts the secrets into the
one command's environment and masks them in its output (every stored value, and each given value
line by line), which goes to the job's log in the run folder. The worker streams that log and exits
with the command's exit code. The value never enters the sandbox: the request, the job's record and
the log name only the secret, and the served copy records which secrets it gave, to which task and
when (USES, under the project's state).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

from . import detach
from .project import durable_append

SERVED_ENV = "TTP_ROUTED_RUN"   # set in the served copy: it runs the command itself, never queues again
# (a name detach._kept_env keeps: it drops every variable named like a secret)
SANDBOX_ENV = "SANDBOX_RUNTIME"    # set by the Claude Code Bash sandbox
USES = "secret-uses.jsonl"         # under the project's state: {"at", "task", "run", "secrets"} per served command
START_GRACE_S = 60                 # past detach.QUEUED_S still unstarted: the daemon is down, stop waiting
POLL_S = 0.2


def _store_denied() -> bool:
    from .project import secrets_path
    p = secrets_path()
    return p.exists() and not os.access(p, os.R_OK)


def state_dir() -> Path | None:
    """The project's state folder of this run (its run folder sits in state/runs), else None."""
    rd = os.environ.get("TTP_RUN_DIR")
    if not rd:
        return None
    rd_path = Path(rd).resolve()
    return rd_path.parent.parent if rd_path.parent.name == "runs" else None


def routed() -> bool:
    """Whether `ttp secret env` here goes through the daemon: a sandboxed run of a project (the
    sandbox's marker, or a store it cannot read), and not the served copy itself."""
    if os.environ.get(SERVED_ENV) or state_dir() is None:
        return False
    return bool(os.environ.get(SANDBOX_ENV)) or _store_denied()


def record_use(labels: list[str]) -> None:
    """The served copy's record of the secrets it gave: their names, the task, the run and the time."""
    state = state_dir()
    if os.environ.get(SERVED_ENV) and state is not None and labels:
        try:
            durable_append(state / USES, json.dumps({"at": time.time(), "task": os.environ.get("TTP_TASK_ID"),
                                                     "run": os.environ.get("TTP_RUN_ID"),
                                                     "secrets": labels}) + "\n")
        except OSError:
            pass


def job_command(alias: str | None, specs: list[str], argv: list[str]) -> list[str]:
    """The served copy's command line. Its markers ride in the command itself, never only in the job's
    environment: a dropped marker would make the served copy queue itself again."""
    from .cli import RUNTIME
    path = os.pathsep.join([str(RUNTIME), *filter(None, [os.environ.get("PYTHONPATH")])])
    return ["env", "-u", SANDBOX_ENV, f"{SERVED_ENV}=1", f"PYTHONPATH={path}", sys.executable, "-m", "ttp",
            *_shown(alias, specs, argv)[1:]]


def _shown(alias: str | None, specs: list[str], argv: list[str]) -> list[str]:
    """The command as the worker gave it, for the run folder's job list: the launcher prefix (a PYTHONPATH
    of any length) would push the secrets and the command past the list's 300-character cut."""
    return ["ttp", "secret", "env", *([alias] if alias else []), *(f"--secret={s}" for s in specs), "--", *argv]


def run(alias: str | None, specs: list[str], argv: list[str], out=None) -> int:
    """Queue the command for the daemon, stream its masked output to `out` and return its exit code."""
    out = out or sys.stdout.buffer
    state = state_dir()
    rd = Path(os.environ["TTP_RUN_DIR"]).resolve()
    name = f"secret-env-{uuid.uuid4().hex[:8]}"
    rc, log = rd / f"{name}.rc", rd / f"{name}.log"
    cmd = job_command(alias, specs, argv)
    detach.enqueue(state, rc, log, cmd, os.getcwd(), dict(os.environ))
    with detach.registry(rd) as jobs:
        jobs.append({"name": name, "rc": str(rc), "log": str(log), "pid": None, "started": time.time(),
                     "queued": True, "status": "queued",
                     "command": " ".join(_shown(alias, specs, argv))[:300]})
    print(f"ttp secret env: the sandbox cannot read the secrets store; the daemon runs the command "
          f"(log {log})", file=sys.stderr)
    deadline, pos = time.time() + detach.QUEUED_S + START_GRACE_S, 0
    try:
        while True:
            pos = _copy(log, pos, out)
            st, why = detach.state(rc)
            if st == "ended":
                _copy(log, pos, out)
                return int(why) if why.lstrip("-").isdigit() else 1
            if st == "lost":
                print(f"ttp secret env: the command did not run: {why}", file=sys.stderr)
                return 1
            if st == "queued" and time.time() > deadline:
                detach.withdraw(rc)
                print("ttp secret env: the daemon did not start the command; is it running "
                      "(`ttp status`)?", file=sys.stderr)
                return 1
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        if detach.pending(rc):
            detach.withdraw(rc)
        else:
            print(f"ttp secret env: interrupted; the command may still run, its output goes to {log}",
                  file=sys.stderr)
        return 130


def _copy(log: Path, pos: int, out) -> int:
    try:
        with open(log, "rb") as f:
            f.seek(pos)
            data = f.read()
    except OSError:
        return pos
    if data:
        out.write(data)
        out.flush()
    return pos + len(data)

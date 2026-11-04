# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Landing marks: how a `ttp lock` waiter inside a landing's checks goes ahead of other waiters.

The landing checks runner (push._run_one for checks in TTP_PUSH_MODE `target`: the push queue's batches,
`ttp push` to the push branch and `ttp land`) holds a mark while each check runs: an OS file lock in
marks_dir() (under the host's check-slot folder), labelled with the runner's pid and start time, its path
given to the check in ENV. A `ttp lock` in the check counts as a landing (verified) only when that file
is in marks_dir(), held right now, and its holder is one of the waiter's own ancestors in this PID
namespace with the start time the label records. A worker that sets ENV itself, names another push's
mark or a file nobody holds waits as a non-landing waiter (locks.landing_first orders the queue).

Known gap: a `ttp lock` inside a check's process sandbox (its own PID namespace) cannot see its
ancestors and waits as a non-landing one; a gate in checks.wrap_cmd runs outside the sandbox."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

from . import locks

ENV = "TTP_LANDING_MARK"


def marks_dir() -> Path:
    from . import checkcap
    return checkcap.slot_dir() / "landing"


class Mark:
    """A held landing mark; close() lets it go and removes its file."""

    def __init__(self, f, path: Path):
        self.f, self.path = f, path

    def close(self) -> None:
        if self.f is not None:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.f.close()
            self.f = None


def hold() -> Mark | None:
    """A landing mark held by this process until close(); None when the folder cannot be written (the
    check then waits as a non-landing one, never fails)."""
    try:
        d = marks_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{os.getpid()}-{time.time():.6f}.lock"
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(json.dumps({"since": time.time(), **locks._owner(os.getpid())}))
        f.flush()
        return Mark(f, path)
    except OSError:
        return None


def verified(env=None) -> bool:
    """Whether this process runs inside a landing's checks: ENV names a held mark in marks_dir() whose
    holder (pid, start time, node, PID namespace) is an ancestor of this process. Any doubt: False."""
    raw = (os.environ if env is None else env).get(ENV)
    if not raw:
        return False
    try:
        path = Path(raw).resolve()
        if path.parent != marks_dir().resolve() or not locks._ticket_live(path):
            return False
        label = json.loads(path.read_text() or "{}")
        pid, start = int(label["pid"]), label.get("pid_start")
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if not start or label.get("node") != os.uname().nodename or label.get("pidns") != locks._pid_ns():
        return False
    at = os.getppid()
    for _ in range(256):
        if not at or at <= 1:
            return False
        if at == pid:
            return locks.start_time(at) == start
        at = locks._parent(at)
    return False

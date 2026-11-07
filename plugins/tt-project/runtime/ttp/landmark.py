# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Landing marks: how a `ttp lock` waiter inside a landing's checks goes ahead of other waiters.

Each runner of a check in TTP_PUSH_MODE `target` holds a mark while the check runs: push._run_one
(`ttp push` to the push branch and `ttp land`) and batch._stream (the push queue's batch checks, their
reruns and cap probes; the batch process holds it). Each strips an inherited ENV first. The mark is
an OS file lock in marks_dir() (under the host's check-slot folder), labelled with the runner's pid and
start time, its path given to the check in ENV. A `ttp lock` in the check counts as a landing (verified) only when that file
is in marks_dir(), held right now, and its holder is one of the waiter's own ancestors in this PID
namespace with the start time the label records. A worker that sets ENV itself, names another push's
mark or a file nobody holds waits as a non-landing waiter (locks.landing_first orders the queue).
A batch that pushes an urgent approval labels its mark urgent (hold(urgent=True)): its `ttp lock` waiters
carry urgent.PRIORITY, read only from a verified mark (label(); cli.cmd_lock), so a forged ENV gains no rank either.

A mark's liveness is its OS lock alone, never its label's pid: the lock goes with its holder's open files, so a
file left by a holder that died (killed before close()) is never verified, even when its pid now names a live
process. hold() first reaps such files (reap): no live process holds them and they are older than GRACE.

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
GRACE = 60.0   # seconds a mark file may sit unlocked before reap() removes it (a hold() between open and flock)


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


def reap() -> int:
    """Remove mark files no live process holds (their holder died before close()), older than GRACE; the
    number removed. Each is removed under its own lock and only if the path still names that file."""
    d, n = marks_dir(), 0
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".lock"):
            continue
        path = d / name
        try:
            with open(path) as f:
                st = os.fstat(f.fileno())
                if time.time() - st.st_mtime < GRACE:
                    continue
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue   # held: its holder lives
                if os.stat(path).st_ino == st.st_ino:
                    path.unlink()
                    n += 1
        except OSError:
            continue
    return n


def hold(urgent: bool = False) -> Mark | None:
    """A landing mark held by this process until close(), labelled urgent when `urgent`; None when the
    folder cannot be written (the check then waits as a non-landing one, never fails). Reaps dead marks first."""
    try:
        d = marks_dir()
        d.mkdir(parents=True, exist_ok=True)
        reap()
        path = d / f"{os.getpid()}-{time.time():.6f}.lock"
        f = open(path, "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.stat(path).st_ino != os.fstat(f.fileno()).st_ino:   # reaped between open and flock
            f.close()
            return None
        f.write(json.dumps({"since": time.time(), **locks._owner(os.getpid()), **({"urgent": True} if urgent else {})}))
        f.flush()
        return Mark(f, path)
    except OSError:
        return None


def verified(env=None) -> bool:
    """Whether this process runs inside a landing's checks: ENV names a held mark in marks_dir() whose
    holder (pid, start time, node, PID namespace) is an ancestor of this process. Any doubt: False."""
    return label(env) is not None


def label(env=None) -> dict | None:
    """The verified mark's label (verified), else None."""
    raw = (os.environ if env is None else env).get(ENV)
    if not raw:
        return None
    try:
        path = Path(raw).resolve()
        if path.parent != marks_dir().resolve() or not locks._ticket_live(path):
            return None
        label = json.loads(path.read_text() or "{}")
        pid, start = int(label["pid"]), label.get("pid_start")
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not start or label.get("node") != os.uname().nodename or label.get("pidns") != locks._pid_ns():
        return None
    at = os.getppid()
    for _ in range(256):
        if not at or at <= 1:
            return None
        if at == pid:
            return label if locks.start_time(at) == start else None
        at = locks._parent(at)
    return None

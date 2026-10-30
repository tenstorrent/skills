# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Leftover processes and sandbox trees of `ttp checks`: none outlive the run that made them.

Every check command of one `ttp checks` run carries the run's random token in its environment
(MARKER). Once a check's exit code is known, and when the run is stopped or fails, the runner ends
every process still carrying that token (SIGTERM, a short grace, then SIGKILL): a background child a
check left behind (`&`, nohup, even one that left the check's group) dies with the check, and the
clean checkout's tree can go.

A run of a project records itself in the project's state (FOLDER/<token>/sandbox.json: runner pid,
start, boot, the check groups and the clean tree) and writes exit.json there once its checks ended.
`sweep`, at each run's start and in the daemon's housekeeping, reaps records of earlier runs past a
grace period that have an exit.json or a dead runner: it signals only processes of this user tied
to that record, by the token in their environment, or by a recorded check group together with a
working directory inside the recorded tree, so a reused pid is never signalled. A record whose
runner still runs and has no exit.json is left alone, as is one whose runner this process cannot
judge (another PID namespace). Without /proc (macOS) nothing is found to signal."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

MARKER = "TTP_CHECK_SANDBOX"
FOLDER = "check-sandboxes"
RECORD, EXIT = "sandbox.json", "exit.json"
GRACE_S = 5.0          # SIGTERM to SIGKILL
SWEEP_GRACE_S = 300.0  # an earlier run's record is swept only this long after it ended or was made
PROC = Path("/proc")


def token() -> str:
    return uuid.uuid4().hex


def _stat(pid: int) -> tuple[str, int] | None:
    """(state, pgid) of `pid`, or None when it is gone."""
    try:
        f = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return f[0], int(f[2])
    except (OSError, IndexError, ValueError):
        return None


def _start(pid: int) -> int | None:
    """The start time of `pid` in clock ticks since boot, or None when it is gone: with the pid,
    it names one process, even after the process stops showing its environment as it exits."""
    try:
        return int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _ancestors() -> set[int]:
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            pid = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return out


def _inside(path: str, root: str) -> bool:
    return bool(root) and (path == root or path.startswith(root.rstrip("/") + "/"))


def tied(pid: int, tok: str, pgids=(), root: str = "") -> bool:
    """Whether live process `pid` of this user belongs to the check run `tok`: its environment
    carries the token, or it is in one of the run's check groups with its working directory in
    `root`. A zombie, another user's process or an unreadable one never is."""
    st = _stat(pid)
    if not tok or st is None or st[0] in ("Z", "X"):
        return False
    try:
        if (PROC / str(pid)).stat().st_uid != os.getuid():
            return False
    except OSError:
        return False
    try:
        if f"{MARKER}={tok}".encode() in (PROC / str(pid) / "environ").read_bytes().split(b"\0"):
            return True
    except OSError:
        pass
    if st[1] in set(pgids) and root:
        try:
            return _inside(os.path.realpath(os.readlink(PROC / str(pid) / "cwd")), os.path.realpath(root))
        except OSError:
            return False
    return False


def members(tok: str, pgids=(), root: str = "") -> list[int]:
    """The live processes tied to run `tok` (never this process or its ancestors)."""
    if not tok or not PROC.is_dir():
        return []
    mine = _ancestors()
    out = []
    try:
        names = os.listdir(PROC)
    except OSError:
        return []
    for name in names:
        if name.isdigit() and int(name) not in mine and tied(int(name), tok, pgids, root):
            out.append(int(name))
    return out


def reap(tok: str, pgids=(), root: str = "", grace_s: float = GRACE_S) -> tuple[int, int]:
    """End every process tied to run `tok`: SIGTERM, up to `grace_s` for them to go, then SIGKILL
    those still there. Returns (processes found, processes still there, e.g. stuck in D state).
    A found process counts as there until it is gone or a zombie, by its pid and start time: one
    that is exiting no longer shows its environment, yet still runs."""
    found = {pid: _start(pid) for pid in members(tok, pgids, root)}

    def there(pid: int) -> bool:
        st = _stat(pid)
        return st is not None and st[0] not in ("Z", "X") and _start(pid) == found[pid]

    left = list(found)
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, min(grace_s, 2.0))):
        left = [pid for pid in left if there(pid)]
        for pid in left:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while left and time.monotonic() < deadline:
            # A zombie has ended: never reaped here, so whoever started it still gets its exit code.
            left = [pid for pid in left if there(pid)]
            if left:
                time.sleep(0.05)
        if not left:
            return len(found), 0
    return len(found), len(left)


def said(procs: int, trees: int) -> str:
    """The check log's line for what a reap ended and removed."""
    return (f"ttp checks: reaped {procs} leftover process{'es' if procs != 1 else ''} and "
            f"{trees} sandbox tree{'s' if trees != 1 else ''}")


def _write(path: Path, data: dict) -> None:
    from .project import durable_write
    durable_write(path, json.dumps(data))


class Record:
    """This run's record in the project's state; every method is safe to call twice."""

    def __init__(self, state: Path, tok: str, tree: str = "", top: str = ""):
        from .push import pid_ns
        from .runner import boot_id, proc_start
        self.dir = Path(state) / FOLDER / tok
        self.tok, self.data = tok, {"token": tok, "pid": os.getpid(), "started": proc_start(os.getpid()),
                                    "boot": boot_id(), "pidns": pid_ns(), "tree": tree, "top": top,
                                    "pgids": [], "ts": time.time()}
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.dir / RECORD, self.data)

    def group(self, pgid: int) -> None:
        if pgid not in self.data["pgids"]:
            self.data["pgids"].append(pgid)
            _write(self.dir / RECORD, self.data)

    def exit(self, rc) -> None:
        _write(self.dir / EXIT, {"rc": rc, "ts": time.time()})

    def forget(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _remove_tree(tree: str, top: str) -> int:
    """Remove a recorded clean checkout folder (only one of cleantree's, of this user); 1 if removed."""
    from . import cleantree, shorttmp
    d = Path(tree) if tree else None
    try:
        if (d is None or not d.name.startswith(cleantree.PREFIX) or d.is_symlink() or not d.is_dir()
                or d.stat().st_uid != os.getuid() or (Path(os.path.realpath(d.parent)) !=
                Path(os.path.realpath(tempfile.gettempdir())) and not shorttmp.on_tmpfs(os.path.realpath(d.parent)))):
            return 0
    except OSError:
        return 0
    shutil.rmtree(d, ignore_errors=True)
    if top:
        try:
            subprocess.run(["git", "-C", top, "worktree", "prune"], capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0 if d.exists() else 1


def sweep(state: Path, grace_s: float = SWEEP_GRACE_S, term_s: float = GRACE_S) -> tuple[int, int]:
    """Reap the leftovers of this project's earlier check runs (see the module's doc). Returns
    (processes found, trees removed)."""
    from .project import zombie
    from .push import pid_ns
    from .runner import proc_start, same_boot
    folder = Path(state) / FOLDER
    procs = trees = 0
    try:
        dirs = [d for d in folder.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return 0, 0
    now = time.time()
    for d in dirs:
        try:
            info = json.loads((d / RECORD).read_text())
            info = info if isinstance(info, dict) and info.get("token") == d.name else {}
        except (OSError, ValueError):
            info = {}
        ended = d / EXIT
        try:
            since = (ended if ended.exists() else d / RECORD if info else d).stat().st_mtime
        except OSError:
            continue
        if now - since < grace_s:
            continue
        if not info:
            shutil.rmtree(d, ignore_errors=True)   # a record cut off as it was written: nothing to tie
            continue
        this_boot = same_boot(info.get("boot"))
        pid = info.get("pid")
        if this_boot and info.get("pidns") and pid_ns() and info["pidns"] != pid_ns():
            continue   # its runner cannot be judged from here
        alive = (this_boot and isinstance(pid, int) and bool(info.get("started"))
                 and proc_start(pid) == info["started"] and not zombie(pid))
        if alive and not ended.exists():
            continue   # still running
        left = 0
        if this_boot:
            found, left = reap(d.name, info.get("pgids") or (), str(info.get("tree") or ""), term_s)
            procs += found
        trees += _remove_tree(str(info.get("tree") or ""), str(info.get("top") or ""))
        if not left:
            shutil.rmtree(d, ignore_errors=True)
    return procs, trees

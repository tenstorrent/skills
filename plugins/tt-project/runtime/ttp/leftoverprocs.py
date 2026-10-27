# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp leftover-procs <result.json>`: a hand-off check (worker.handoff_checks) that sends a done
hand-off back while a process of its task still runs on this host.

A process is the task's only when its working directory, resolved (realpath), is the task's own
worktree, <project>/tt-project/worktrees/t<id>, or below it, compared by whole path components. So
t39 never claims a process in t390 or t3922, and a same-numbered task of another project on the host
(its own worktrees/t<id>) is never this one's: a bare t<id> or a cwd substring never counts. A
process of another user, a zombie, or this check and its ancestors never counts either.

A process the hand-off means to leave running is listed in it as "leftover_procs":
[{"pid": N, "stop": "<how it stops>", "ends": "<when>"}]; one with all three is not reported.
Read-only: it lists processes from /proc (none known without it, as on macOS) and kills nothing."""
from __future__ import annotations

import json
import os
from pathlib import Path

PROC = Path("/proc")
CMD_CHARS = 100


def _ancestors() -> set[int]:
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            pid = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return out


def listing() -> list[dict]:
    """This user's live processes as {pid, cwd, cmd}; cwd as the kernel reports it (resolved later)."""
    try:
        names = [n for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return []
    uid, out = os.getuid(), []
    for name in names:
        d = PROC / name
        try:
            if d.stat().st_uid != uid or (d / "stat").read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X"):
                continue
            cwd = os.readlink(d / "cwd")
        except (OSError, IndexError):
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        out.append({"pid": int(name), "cwd": cwd, "cmd": cmd})
    return out


def _resolve(path: str) -> Path | None:
    if not path or not path.startswith("/"):
        return None
    try:
        return Path(os.path.realpath(path.removesuffix(" (deleted)")))
    except (OSError, ValueError):
        return None


def claims(cwd: str, worktree: Path) -> bool:
    """Whether a process with working directory `cwd` runs in `worktree` or below it."""
    c, w = _resolve(cwd), _resolve(str(worktree))
    return c is not None and w is not None and (c == w or w in c.parents)


def leftovers(worktree: Path, procs: list[dict], skip=()) -> list[dict]:
    """The processes of `procs` that run in `worktree`, leaving out the pids in `skip`."""
    skip = {int(x) for x in skip}
    return [pr for pr in procs if int(pr["pid"]) not in skip and claims(str(pr.get("cwd") or ""), worktree)]


def allowed(result: dict) -> set[int]:
    """The pids the hand-off lists as left running on purpose, each with how it stops and when."""
    out = set()
    for x in result.get("leftover_procs") or []:
        if isinstance(x, dict) and x.get("stop") and x.get("ends"):
            try:
                out.add(int(x.get("pid")))
            except (TypeError, ValueError):
                continue
    return out


def check(result: dict, worktree: Path, procs: list[dict] | None = None) -> tuple[int, str]:
    """(exit code, message) for a hand-off: 1 with the task's live processes listed, else 0."""
    if result.get("status") != "done":
        return 0, ""
    procs = listing() if procs is None else procs
    bad = leftovers(worktree, procs, allowed(result) | _ancestors())
    if not bad:
        return 0, ""
    lines = "\n  ".join(f"pid {pr['pid']} cwd {pr.get('cwd')}: {(pr.get('cmd') or '')[:CMD_CHARS]}" for pr in bad)
    return 1, (f"ttp leftover-procs: task {worktree.name} still has live processes in its worktree:\n  {lines}\n"
               "Stop the ones you started by pid, or list each in the hand-off as leftover_procs "
               "[{pid, stop, ends}], then hand off.")


def load(path: str) -> dict:
    r = json.loads(Path(path).read_text())
    return r if isinstance(r, dict) else {}

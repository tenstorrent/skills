# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The fast-forward of `ttp land` in a checkout a worker sandbox cannot write, handed to the daemon.

A worker sandbox writes the project's .git but not the project root's working tree, so a land or review
task cannot fast-forward the push branch where the project's main checkout has it checked out. `ttp land`
then asks the running daemon, which runs on the host: it writes a request under state/land-ff/, the
daemon takes it after a tick (renaming it to `.taken`), checks it again and writes `<id>.result`. The
requester keeps the landing lock throughout. The daemon moves only a worktree of the project root's own
repo that has the branch checked out, at the tip the requester saw, with no uncommitted changes, and only
by `git merge --ff-only` to a descendant of that tip: a dirty or diverged checkout is never overwritten.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import applyff, poll_s

DIR = "land-ff"
TAKES = "land_requests"     # the daemon's heartbeat lists it once it takes these requests
WAIT_S = 120                # how long `ttp land` waits for the daemon to take the request, then for its result


def request(p, wt: Path, branch: str, base: str, new: str, task: str,
            wait_s: float = WAIT_S) -> tuple[str, str]:
    """Ask the running daemon to fast-forward `branch` from `base` to `new` in worktree `wt` and wait
    for it: ("done", text) or ("failed", text). A request the daemon has not taken within wait_s is
    withdrawn, so it never moves the branch after this run gave up; one it took is waited on wait_s more."""
    from .daemon import heartbeat
    from .project import durable_write
    hb = heartbeat(p) or {}
    if TAKES not in (hb.get("takes") or []):
        return "failed", (f"the running daemon of {p.name} (pid {hb.get('pid', '?')}) does not take landing "
                          f"requests (not running, or an older runtime)")
    d = p.state / DIR
    d.mkdir(parents=True, exist_ok=True)
    at = time.time()
    rid = f"{os.getpid()}-{time.time_ns()}"
    req, taken, res = d / f"{rid}.req", d / f"{rid}.taken", d / f"{rid}.result"
    durable_write(req, json.dumps({"at": at, "worktree": str(wt), "branch": branch, "base": base,
                                   "new": new, "task": task}))
    end = at + wait_s
    while True:
        try:
            out = json.loads(res.read_text())
            res.unlink(missing_ok=True)
            return str(out.get("outcome") or "failed"), str(out.get("text") or "")
        except (OSError, ValueError, AttributeError):
            pass
        if time.time() >= end:
            if req.exists():
                try:
                    os.unlink(req)          # not taken: withdrawn, the daemon never moves the branch for it
                    return "failed", f"the daemon did not take the landing request within {wait_s:.0f}s"
                except FileNotFoundError:
                    pass                    # taken just now: wait for its result
            if time.time() >= end + wait_s or not taken.exists() and not req.exists() and not res.exists():
                return "failed", (f"the daemon took the landing request but reported no result within "
                                  f"{time.time() - at:.0f}s")
        time.sleep(poll_s(0.5))


def _holds(root: Path, wt: str, branch: str) -> bool:
    """`wt` is a worktree of `root`'s repo with local branch `branch` checked out."""
    cur, want = "", str(Path(wt).resolve())
    for line in applyff._git(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = str(Path(line[9:]).resolve())
        elif cur == want and line == f"branch refs/heads/{branch}":
            return True
    return False


def _why_not(p, info: dict) -> str:
    """Why the daemon must not carry out request `info` as it is now ("" when it may)."""
    wt, branch, base, new = (str(info.get(k) or "") for k in ("worktree", "branch", "base", "new"))
    if not (wt and branch and base and new) or time.time() - float(info.get("at") or 0) > WAIT_S:
        return "the request is malformed or older than its requester waits"
    h = Path(wt)
    if not _holds(p.root, wt, branch):
        return f"{wt} is not a checkout of {branch} in the project's repo"
    tip = applyff._git(h, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()
    if tip != base:
        return f"{branch} moved off {base[:10]} (it is at {tip[:10] or '?'}); nothing landed"
    if applyff._git(h, "merge-base", "--is-ancestor", base, new).returncode != 0:
        return f"{new[:10]} does not contain {branch}'s tip {base[:10]} (diverged); nothing landed"
    dirty = applyff._git(h, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0 or dirty.stdout.strip():
        return f"{branch} is checked out in {wt} with uncommitted changes; nothing landed"
    r = applyff._git(h, "merge", "--ff-only", "--quiet", new)
    if r.returncode != 0 and applyff._git(h, "rev-parse", "HEAD").stdout.strip() != new:
        return f"could not fast-forward {branch} in {wt}: {(r.stderr or r.stdout).strip()[-400:]}; nothing landed"
    return ""


def take(p) -> int:
    """The daemon's side, after a tick: carry out each landing request of a live `ttp land`, checked
    again here. Always leaves a result for the requester. Returns how many it took."""
    from .project import durable_write
    d, n = p.state / DIR, 0
    for req in sorted(d.glob("*.req")) if d.is_dir() else []:
        taken = req.with_suffix(".taken")
        try:
            os.replace(req, taken)
        except FileNotFoundError:
            continue
        try:
            info = json.loads(taken.read_text())
            assert isinstance(info, dict)
        except (OSError, ValueError, AssertionError):
            info = {}
        why = _why_not(p, info)
        new, branch = str(info.get("new") or ""), str(info.get("branch") or "")
        out = {"finished": time.time(), "outcome": "failed" if why else "done",
               "text": f"the daemon (pid {os.getpid()}) " + (f"did not land: {why}" if why else
                                                              f"fast-forwarded {branch} to {new[:10]}")}
        durable_write(req.with_suffix(".result"), json.dumps(out))
        taken.unlink(missing_ok=True)
        n += 1
    for old in [*d.glob("*.result"), *d.glob("*.taken")] if d.is_dir() else []:
        try:
            if time.time() - old.stat().st_mtime > 3 * WAIT_S:
                old.unlink()                # its requester gave up long ago
        except OSError:
            pass
    return n

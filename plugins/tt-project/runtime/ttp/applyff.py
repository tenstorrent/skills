# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The fast-forward of `ttp upgrade <project> --apply <commit>`, run here or handed to the daemon.

A worker sandbox may write the harness's .git but not its working tree, so an upgrade task's --apply
cannot move main itself. It then asks the running daemon, which runs on the host, to do the same
fast-forward: it writes APPLY_REQUEST in state/, the daemon takes it after a tick (renaming it to
`.taken`), checks it again and writes APPLY_RESULT. The requester keeps the upgrade lock throughout and
restarts the daemon itself afterwards, as when it moves main on its own. Either way a fast-forward that
fails part-way puts every file it touched back to main's version, except local edits made before it: never a
half-applied tree.

A daemon older than these requests (its heartbeat's `takes` lacks apply_requests) that takes restart
requests is first asked to restart onto the harness runtime on disk, when that one takes them (route):
the worker's own `ttp` runs that runtime, so it usually does. A daemon that takes neither leaves no way
from a sandbox: `ttp upgrade` then queues no sandboxed merge task, and --apply says which command to run
outside one.
"""
from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import gitmemo, poll_s

APPLY_REQUEST, APPLY_RESULT = "apply.request", "apply.result"
APPLY_WAIT_S = 120      # how long --apply waits for the daemon to take the request, then for its result
RESTART_WAIT_S = 60     # how long it waits for an older daemon to take a restart request (request_restart)
_NO_WRITE = (errno.EROFS, errno.EACCES, errno.EPERM)
_NO_WRITE_TEXT = ("read-only file system", "permission denied", "operation not permitted")


def _git(h: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """git with its output captured, within gitmemo's cap; a timeout comes back as exit 124, so a
    fast-forward cut short is put back like any other failure."""
    try:
        return gitmemo.run(["git", "-C", str(h), *args], capture_output=True, text=True, env=env,
                           timeout=gitmemo.timeout(None, args))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"git {args[0]} timed out")


def unwritable(h: Path, base: str, merged: str) -> str:
    """The first directory of the working tree the fast-forward base..merged must write and cannot
    (read-only or not permitted, as in a worker sandbox), or "" when all can be written."""
    r = _git(h, "diff", "--name-only", "--no-renames", base, merged)
    dirs = {h}
    for f in r.stdout.splitlines():
        d = (h / f).parent
        while not d.exists() and d != h:
            d = d.parent
        dirs.add(d)
    for d in sorted(dirs):
        try:
            fd, tmp = tempfile.mkstemp(prefix=".ttp-apply-", dir=d)
            os.close(fd)
            os.unlink(tmp)
        except OSError as e:
            if e.errno in _NO_WRITE:
                return f"{d}: {e.strerror}"
    return ""


def foreign_edits(h: Path, base: str, merged: str) -> list[str]:
    """The files the fast-forward base..merged writes that hold an uncommitted change in the working tree
    or index (or sit there untracked): someone else's edit, such as another harness task's, which git would
    refuse to overwrite. [] when the fast-forward can run."""
    from .upjournal import _differs, _touched
    return sorted(_differs(h, _touched(h, base, merged)))


def no_write(text: str) -> bool:
    """A failed fast-forward's output says the working tree could not be written."""
    return any(t in (text or "").lower() for t in _NO_WRITE_TEXT)


def local_changes(text: str) -> bool:
    """A failed fast-forward's output says uncommitted edits (or untracked files) in the checkout are in
    the way: git wrote nothing, so a later retry may pass."""
    t = (text or "").lower()
    return "would be overwritten" in t or "local changes" in t


def fast_forward(h: Path, merged: str, guard_env: str) -> str:
    """Fast-forward main in `h` to `merged`; "" when done, else why not. On a failure every file it
    touched that matched main before it is put back to main's version, so the tree is as it was: a local
    edit made before the merge is kept."""
    from .upjournal import _differs, _put_back, _touched
    base = _git(h, "rev-parse", "HEAD").stdout.strip()
    if not base:
        return "could not read main's commit"
    touched = _touched(h, base, merged)
    clean = [f for f in touched if f not in _differs(h, touched)]
    from .project import tree_writes
    with tree_writes(h):
        r = _git(h, "merge", "--ff-only", merged, env={**os.environ, guard_env: "1"})
    if r.returncode == 0:
        return ""
    why = (r.stderr or r.stdout).strip()[-400:] or f"git merge exited {r.returncode}"
    if _git(h, "rev-parse", "HEAD").stdout.strip() == merged:
        return ""                           # git moved the branch last: main holds it whole
    try:
        put = _put_back(h, clean)
    except OSError as e:
        return f"{why}; putting its files back failed too ({e})"
    return why + (f"; put back {len(put)} file(s) it had written" if put else "")


def request(p, merged: str, task: str, wait_s: float = APPLY_WAIT_S) -> tuple[str, str]:
    """Ask the running daemon to fast-forward main to `merged` and wait for it: ("done", text) or
    ("failed", text). A request the daemon has not taken within wait_s is withdrawn, so it never moves
    main after this run gave up; one it took is waited on for wait_s more."""
    from .project import durable_write
    why = ready(p, merged)
    if why:
        return "failed", why
    at = time.time()
    req, taken = p.state / APPLY_REQUEST, p.state / (APPLY_REQUEST + ".taken")
    durable_write(req, json.dumps({"at": at, "pid": os.getpid(), "merged": merged, "task": task}))
    end = at + wait_s
    while True:
        res = result(p, at)
        if res:
            return res.get("outcome") or "failed", str(res.get("text") or "")
        if time.time() >= end:
            if req.exists():
                try:
                    os.unlink(req)          # not taken: withdrawn, the daemon never moves main for it
                    return "failed", f"the daemon did not take the fast-forward request within {wait_s:.0f}s"
                except FileNotFoundError:
                    pass                    # taken just now: wait for its result
            if time.time() >= end + wait_s or not taken.exists() and not req.exists():
                return "failed", (f"the daemon took the fast-forward request but reported no result within "
                                  f"{time.time() - at:.0f}s")
        time.sleep(poll_s(0.5))


def _disk_takes(p) -> str:
    """The version of the harness runtime on disk when its daemon takes fast-forward requests, else ""."""
    rt = p.harness / "runtime" / "ttp"
    try:
        if '"apply_requests"' not in (rt / "daemon.py").read_text():
            return ""
        m = re.search(r'__version__ = "([^"]+)"', (rt / "__init__.py").read_text())
    except OSError:
        return ""
    return m.group(1) if m else "?"


def route(p) -> tuple[str, str]:
    """How a sandboxed --apply gets main moved, and why: "apply" (the running daemon takes fast-forward
    requests), "restart" (it predates them but takes restart requests, and the runtime on disk it would
    restart onto takes them), "down" (no daemon runs; one started later runs the runtime on disk,
    which takes them) or "none" (no way from a sandbox)."""
    from .daemon import HEARTBEAT_STALE_S, heartbeat
    hb = heartbeat(p) or {}
    takes = hb.get("takes") or []
    if "apply_requests" in takes:
        return "apply", ""
    disk = _disk_takes(p)
    if not hb or hb.get("age", 0) > HEARTBEAT_STALE_S:
        # a stopped daemon's heartbeat stays behind: the next one starts on the runtime on disk
        return ("down" if disk else "none"), f"no daemon of {p.name} runs"
    ran = (hb.get("receipt") or {}).get("version") or "an older runtime"
    who = f"the running daemon of {p.name} (pid {hb.get('pid', '?')}, ttp {ran}) does not take fast-forward requests"
    if "restart_requests" not in takes:
        return "none", f"{who} nor restart requests"
    if not disk:
        return "none", f"{who}, and the harness runtime on disk a restart would start predates them too"
    return "restart", f"{who}; asking it to restart onto the harness runtime on disk (ttp {disk}), which does"


def ready(p, merged: str = "<commit>", log=print) -> str:
    """Make sure the running daemon takes fast-forward requests: "" once one does, restarting an older one
    first when that is the route, else why not (naming the command that works outside a sandbox)."""
    from . import service
    from .daemon import heartbeat
    kind, why = route(p)
    if kind == "apply":
        return ""
    outside = (f"; run `ttp upgrade {p.name} --apply {merged}` outside a worker sandbox, where the harness "
               f"working tree is writable")
    if kind != "restart":
        return why + outside
    log(why)
    at = time.time()
    res = service.request_restart(p, f"`ttp upgrade {p.name} --apply` needs a daemon that takes fast-forward "
                                     f"requests", RESTART_WAIT_S)
    if getattr(res, "outcome", "") in ("failed", "rolled_back"):
        return f"{why}, but the restart did not bring one up: {res}"
    end = time.time() + RESTART_WAIT_S / 2
    while True:
        hb = heartbeat(p) or {}
        if "apply_requests" in (hb.get("takes") or []):
            log(f"the daemon of {p.name} restarted (pid {hb.get('pid', '?')}) and takes fast-forward requests")
            return ""
        if time.time() >= end:
            return (f"{why}, but no daemon taking them ticked within {time.time() - at:.0f}s ({res}). Any "
                    f"restart request left is taken later; rerun `ttp upgrade {p.name} --apply {merged}` then")
        time.sleep(poll_s(0.5))


def result(p, at: float) -> dict | None:
    try:
        res = json.loads((p.state / APPLY_RESULT).read_text())
    except (OSError, ValueError):
        return None
    return res if isinstance(res, dict) and abs(float(res.get("at") or 0) - at) < 1e-3 else None


def take(p) -> bool:
    """The daemon's side, after a tick: carry out a fast-forward request of a live `ttp upgrade --apply`.
    It is checked again here: the requester holds the upgrade lock and journals this merge, the commit
    takes in the template (upstream) and main as it is now. Always leaves a result for the requester."""
    from . import release, upjournal
    from .project import durable_write
    req, taken = p.state / APPLY_REQUEST, p.state / (APPLY_REQUEST + ".taken")
    try:
        os.replace(req, taken)
    except FileNotFoundError:
        return False
    try:
        info = json.loads(taken.read_text())
        assert isinstance(info, dict)
    except (OSError, ValueError, AssertionError):
        info = {}
    h, merged = p.harness, str(info.get("merged") or "")
    rec = upjournal.read(p) or {}
    if not merged or time.time() - float(info.get("at") or 0) > APPLY_WAIT_S:
        why = "the request is malformed or older than its requester waits"
    elif not release._taken(release.upgrade_lock(p)):
        why = "no `ttp upgrade` holds the upgrade lock"
    elif rec.get("step") != "merging" or rec.get("merged") != merged:
        why = "the running upgrade's journal does not name this merge"
    elif _git(h, "merge-base", "--is-ancestor", "upstream", merged).returncode != 0:
        why = f"{merged[:12]} does not contain the current template (upstream)"
    elif _git(h, "merge-base", "--is-ancestor", "HEAD", merged).returncode != 0:
        why = f"main moved and {merged[:12]} does not contain it"
    else:
        why = fast_forward(h, merged, release.GUARD_ENV)
    out = {"at": info.get("at"), "finished": time.time(), "outcome": "failed" if why else "done",
           "text": f"the daemon (pid {os.getpid()}) " + (f"did not fast-forward main: {why}" if why
                                                          else f"fast-forwarded main to {merged[:12]}")}
    durable_write(p.state / APPLY_RESULT, json.dumps(out))
    taken.unlink(missing_ok=True)
    return True

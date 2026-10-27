# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A run that commits runtime changes in the harness checkout itself is judged under its new code.

A harness task without a worktree of its own (harnesstree.workdir) edits the runtime/ modules the
daemon imported at its start in place. Its hand-off must not be judged by the code it just replaced:
the daemon would fail it for the bug it fixed. So when such a run ends (check), and the runtime/ .py
files its commits changed were written after this daemon imported its code (LOADED_AT), the run is
left unfinished (its row stays running, its result.json on disk) and the daemon asks itself to restart
through service.RESTART_REQUEST: the same restart, with its rollback, a harness worktree's merge asks
for (harnesstree.land). The new daemon finishes the run. The pending record (PENDING_KEY) is cleared
in the transaction that finishes the run, so a crash or reboot mid-restart only finishes it later,
once. If the restart rolled the runtime back (or did not happen), the files no longer match what the
run committed: its hand-off is held for the coordinator, not judged, never failed for a bug it fixed.
Runs in a worktree, and runs whose commits change no runtime module, finish as before."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .project import Project, durable_write

# When this process imported its code: a runtime file written later is not what it runs. The daemon
# imports this module first of its own, so the time is taken before the rest of its modules load.
LOADED_AT = time.time()
PENDING_KEY = "runtime_reload_pending"   # kv: run id -> {"start", "end", "files", "at"} (the restart request)
ROLLBACK_RE = re.compile(r"roll back runtime to ([0-9a-f]{7,40})")   # service.restart's rollback commit


def _git(h: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(h), *args], capture_output=True, text=True, timeout=60)


def head(h: Path) -> str | None:
    """The harness checkout's HEAD, or None when it is no git repository."""
    try:
        r = _git(h, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def changed_modules(h: Path, start: str, end: str) -> list[str]:
    """The runtime/ .py files the commits start..end changed (still on disk: a removed one is not run)."""
    if start == end:
        return []
    r = _git(h, "diff", "--name-only", start, end, "--", "runtime")
    return sorted(f for f in r.stdout.split() if f.endswith(".py") and (h / f).is_file()) \
        if r.returncode == 0 else []


def stale(h: Path, files: list[str], loaded_at: float) -> list[str]:
    """Those of `files` written after the daemon imported its code: it runs what was there before."""
    out = []
    for f in files:
        try:
            if (h / f).stat().st_mtime > loaded_at:
                out.append(f)
        except OSError:
            pass
    return out


def _restart_result(p: Project, at: float) -> dict | None:
    from . import service
    return service._restart_result(p, at)


def _request(p: Project, rid: int, why: str) -> float:
    from . import service
    at = time.time()
    durable_write(p.state / service.RESTART_REQUEST, json.dumps({"at": at, "pid": os.getpid(), "why": why}))
    return at


def _rolled_back_to(h: Path, end: str) -> str:
    """The commit service.restart rolled runtime/ back to after `end`, else HEAD's."""
    log = _git(h, "log", "--format=%s", f"{end}..HEAD", "--", "runtime").stdout
    m = ROLLBACK_RE.search(log)
    return m.group(1)[:10] if m else (head(h) or "?")[:10]


def check(p: Project, run: dict, loaded_at: float) -> tuple[str, str]:
    """What finish_run does with a worker run that ended: ("go", "") finishes it as always; ("defer",
    why) leaves it running until the daemon restarted on the run's runtime changes; ("hold", why) finishes
    it with its hand-off held, because those changes did not load."""
    try:
        note = json.loads(run.get("note") or "{}")
    except ValueError:
        return "go", ""
    start = note.get("harness_head") if isinstance(note, dict) else None
    if not start or run.get("role") == "coordinator":
        return "go", ""
    h, db, rid = p.harness, p.db, str(run["id"])
    rec = (db.kv(PENDING_KEY) or {}).get(rid)
    end = rec["end"] if rec else head(h)
    files = rec["files"] if rec else (changed_modules(h, start, end) if end else [])
    if not files:
        return "go", ""
    old = stale(h, files, loaded_at)
    if old:
        res = _restart_result(p, float(rec["at"])) if rec else None
        if res:   # the restart this run asked for ended, and this daemon still runs the old code
            return "hold", (f"the runtime change did not load: the daemon restart it asked for ended "
                            f"{res.get('outcome') or 'failed'} and this daemon (pid {os.getpid()}) still runs the "
                            f"previous code ({str(res.get('text') or '')[:300]})")
        from . import service
        asked = rec and ((p.state / service.RESTART_REQUEST).exists()
                         or (p.state / (service.RESTART_REQUEST + ".taken")).exists())
        if not asked:
            new = {"start": start, "end": end, "files": files, "at": 0.0}
            db.set_kv(PENDING_KEY, {**(db.kv(PENDING_KEY) or {}), rid: new})   # before the request exists
            new["at"] = _request(p, run["id"], f"run {rid} of task #{run.get('task')} committed runtime changes "
                                               f"in the harness checkout ({', '.join(files[:5])}); its hand-off "
                                               f"is judged after the restart")
            db.set_kv(PENDING_KEY, {**(db.kv(PENDING_KEY) or {}), rid: new})
        return "defer", f"waits for the daemon to restart on {', '.join(old[:5])}"
    if rec:
        differ = _git(h, "diff", "--name-only", end, "HEAD", "--", *files).stdout.split()
        if differ:
            return "hold", (f"the runtime change did not load, rolled back to {_rolled_back_to(h, end)}: "
                            f"{', '.join(differ[:5])} no longer match{'es' if len(differ) == 1 else ''} the "
                            f"run's commit {end[:10]}")
    return "go", ""


def clear(p: Project, rid) -> None:
    """The run is finished (in finish_run's transaction): its pending record goes with it."""
    pending = p.db.kv(PENDING_KEY) or {}
    if str(rid) in pending:
        pending.pop(str(rid))
        p.db.set_kv(PENDING_KEY, pending or None)

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The journal of a running `ttp upgrade`, so one cut short (a reboot, a kill, a power cycle) is finished or
rolled back by the next run instead of leaving a half-merged harness or a stopped daemon.

`ttp upgrade` writes `<state>/upgrade.journal` (JSON: pid, boot, started, kind, auto, step, base, merged)
durably before each step that changes the harness: "start", "committed" (local changes committed, `base`
known), "merging" (main is about to move to `merged`), "merged" (main is there and synced) and "restarting"
(the daemon is being restarted). It is removed when the run ends, also by a refusal or a failure it
handled; an uncaught error or a kill leaves it.

A journal whose process is gone (its pid ended, or another boot wrote it) is recovered, under the upgrade
lock, by the next `ttp upgrade` of the project (before anything else), by its daemon at start and on its
hourly release check:
- "start" or "committed": main never moved (a local-changes commit and the upstream snapshot are
  redone by any rerun), so the journal is dropped.
- "merging": main already holds `merged` (git moved the branch last): finished as "merged". Otherwise it
  is rolled back: an unfinished merge is aborted, a stale index lock of the dead run removed, and every
  file the merge touched put back to main's version (files it added are deleted).
- "merged" or "restarting": finished: files the cut left differing from main's version are restored,
  the disk synced, and the daemon restarted when it runs older code or is not running.
An automatic upgrade cut short is recorded "held", so the daemon tries it again (after
MAX_INTERRUPTS cuts in a row of the same release, "failed"), or "applied" once finished.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import gitmemo

JOURNAL = "upgrade.journal"
MAX_INTERRUPTS = 3      # an automatic upgrade cut short this often in a row is recorded failed, not retried


def path(p) -> Path:
    return p.state / JOURNAL


def read(p) -> dict | None:
    try:
        rec = json.loads(path(p).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        rec = {"step": "start"}      # torn by a power cut: written before anything changed, or rewritten
    return rec if isinstance(rec, dict) else {"step": "start"}


def _write(p, rec: dict) -> None:
    from .project import durable_write
    p.state.mkdir(parents=True, exist_ok=True)
    durable_write(path(p), json.dumps(rec))


def begin(p, kind: str, auto: bool = False) -> None:
    from .runner import boot_id
    _write(p, {"v": 1, "pid": os.getpid(), "boot": boot_id(), "started": time.time(), "kind": kind,
               "auto": bool(auto), "step": "start", "at": time.time()})


def step(p, name: str, **fields) -> None:
    rec = read(p) or {}
    if rec.get("pid") != os.getpid():
        return          # not this run's journal (begin was not called): journal nothing
    _write(p, {**rec, **fields, "step": name, "at": time.time()})


def end(p) -> None:
    from .project import fsync_dir
    rec = read(p)
    if rec is not None and rec.get("pid") == os.getpid():
        path(p).unlink(missing_ok=True)
        try:
            fsync_dir(p.state)
        except OSError:
            pass


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _daemon_up(p) -> bool:
    try:
        return _alive(int((p.state / "daemon.pid").read_text().strip()))
    except (OSError, ValueError):
        return False


def dead(rec: dict) -> bool:
    """The run that wrote `rec` is gone: another boot wrote it, or its pid ended."""
    from .runner import same_boot
    return not same_boot(rec.get("boot")) or not _alive(rec.get("pid"))


def _git(h: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(h), *args], capture_output=True, text=True, timeout=gitmemo.timeout(None, args))


def _contains(h: Path, commit: str) -> bool:
    return bool(commit) and _git(h, "merge-base", "--is-ancestor", commit, "HEAD").returncode == 0


def _touched(h: Path, base: str, merged: str) -> list[str]:
    if not base or not merged:
        return []
    r = _git(h, "diff", "--name-only", "--no-renames", base, merged)
    return [f for f in r.stdout.splitlines() if f] if r.returncode == 0 else []


def _put_back(h: Path, files: list[str]) -> list[str]:
    """Put each file back to HEAD's version (deleting one HEAD lacks); the ones that differed."""
    if not files:
        return []
    changed = set(_git(h, "diff", "--name-only", "--no-renames", "HEAD", "--", *files).stdout.split())
    changed |= set(_git(h, "ls-files", "--others", "--exclude-standard", "--", *files).stdout.split())
    out = []
    for f in sorted(changed):
        if _git(h, "cat-file", "-e", f"HEAD:{f}").returncode == 0:
            _git(h, "checkout", "HEAD", "--", f)
        else:
            _git(h, "rm", "-q", "--cached", "--ignore-unmatch", "--", f)
            (h / f).unlink(missing_ok=True)
        out.append(f)
    return out


def _stale_index_lock(h: Path) -> None:
    r = _git(h, "rev-parse", "--git-path", "index.lock")
    if r.returncode == 0 and r.stdout.strip():
        lk = Path(r.stdout.strip())
        (lk if lk.is_absolute() else h / lk).unlink(missing_ok=True)


def recover(p, daemon_started: float | None = None, restart=None, log=print) -> str:
    """Finish or roll back the journalled upgrade of a run that is gone; the line saying what was done, ""
    when there was none (or it still runs). Call it under the upgrade lock. `daemon_started`: called by
    that daemon, which started then; `restart(p)`: restarts the daemon (from the CLI)."""
    rec = read(p)
    if rec is None or not dead(rec):
        return ""
    from . import release
    from .project import sync_all
    h = p.harness
    st = str(rec.get("step") or "start")
    base, merged = str(rec.get("base") or ""), str(rec.get("merged") or "")
    _stale_index_lock(h)
    if st == "merging" and _contains(h, merged):
        st = "merged"
    finished = st in ("merged", "restarting")
    if st == "merging":
        if _git(h, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            _git(h, "merge", "--abort")
        files = _put_back(h, _touched(h, base, merged))
        sync_all()
        what = "rolled back: main never moved" + (f"; put back {len(files)} file(s) it had written" if files else "")
    elif finished:
        files = _put_back(h, _touched(h, base, merged))
        sync_all()
        try:
            release.clear_stuck(p)
        except Exception:
            pass
        what = "finished: main holds the merge" + (f"; restored {len(files)} file(s) it cut short" if files else "")
    else:
        what = "dropped: it stopped before main moved"
    if finished:
        if daemon_started is not None:
            if float(rec.get("at") or 0) >= daemon_started:    # this daemon runs the code from before
                from .service import RESTART_REQUEST
                _write_request(p, RESTART_REQUEST, "an interrupted upgrade merged new code")
                what += "; this daemon restarts on it"
        elif restart is not None and not _daemon_up(p):     # a live daemon restarts itself on moved code
            what += f"; daemon: {restart(p)}"
    if rec.get("auto"):
        auto = p.db.kv(release.KV_AUTO) or {}
        if finished:
            release.finish(p, "applied", why="finished after an interruption", interrupted=0)
        elif auto.get("outcome") == "running" or auto.get("interrupted"):
            n = int(auto.get("interrupted") or 0) + 1
            release.finish(p, "held" if n < MAX_INTERRUPTS else "failed", interrupted=n,
                           why=f"cut short at step {rec.get('step')} ({n} time(s) in a row; a reboot?)")
    path(p).unlink(missing_ok=True)
    line = f"an earlier `ttp upgrade` (pid {rec.get('pid')}) stopped at step {rec.get('step')}; {what}"
    log(line)
    return line


def _write_request(p, name: str, why: str) -> None:
    from .project import durable_write
    durable_write(p.state / name, json.dumps({"at": time.time(), "pid": os.getpid(), "why": why}))

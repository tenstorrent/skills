# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One shard of a project's own test pool, ended when it makes no file progress (`ttp pool-shard`).

A check script that runs its test files on a pool of hosts starts each shard as
`ttp pool-shard --shard <id> --pool <alias> [--pool <alias>...] [--cancel <cmd>] -- <cmd...>`. The
command ({pool} and {shard} in its words are replaced, TTP_POOL and TTP_POOL_SHARD set) runs the shard's
files on that pool and prints one line per file done (DONE_RE, or --done-re). Its output passes through.

- No progress: when no file is done for the stall window (window()) since the shard started or since its
  last file, the shard is ended (its process group, then the --cancel command, which cancels its batch
  job), the pool gets a stall mark (mark()), the stall is logged with the shard id and pool alias
  (LOG), and the shard's files run again on the next pool given that has no fresh mark (`local`, a pool
  alias the command maps to this host, included). A shard that stalls on every pool given exits STALLED.
- Stall marks: a later shard skips a pool marked in the last MARK_TTL_S; once the mark is older, the pool
  gets a try again, and its first file done clears the mark. When every pool given is freshly marked the
  shard still runs, on the one marked longest ago, rather than not at all.
- Waiting: the time before the shard's first file (its job queue, environment setup, a tree sync) is
  recorded as a test-pool wait of its push check (checkwait, pool=True), as `ttp pool-wait` records one:
  the push log shows it, and it counts against the check's time limit (delivery.push_checks_timeout_s,
  wall time), its pool-wait bound (delivery.push_pool_wait_s) and the stall window.

The window: `checks.pool_shard_stall_s` (account settings, or TTP_POOL_SHARD_STALL_S) when set, else
STALL_FACTOR times the median seconds per file this machine measured (the time between two files of one
shard, the newest KEEP per --what), at least the floor `checks.pool_shard_stall_floor_s` (or
TTP_POOL_SHARD_STALL_FLOOR_S; default STALL_FLOOR_S)."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import queue
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import checkwait, project

STALLED = 124
DONE_RE = r"^(PASS|FAIL)\b|\[\s*\d+%\]\s*$"   # a PASS/FAIL line per file, or pytest's per-file progress line
STALL_FLOOR_S = 600.0
STALL_FACTOR = 4.0
MARK_TTL_S = 1800.0
KEEP = 200
GRACE_S = 10.0     # TERM, then KILL this long after
CANCEL_S = 60.0    # the --cancel command's own bound
LOG = "pool-stalls.log"


def _dir() -> Path:
    return project.HOME_DIR / "check-slots"


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _setting(env: str, key: str) -> float | None:
    try:
        checks = project.load_account_settings().get("checks") or {}
    except Exception:   # unreadable settings only lose the override
        checks = {}
    for v in (os.environ.get(env), checks.get(key)):
        if v not in (None, "") and (n := _num(v)) is not None:
            return n
    return None


@contextlib.contextmanager
def _locked():
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "pool-shards.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield d


def _history_path() -> Path:
    return _dir() / "pool-shard-files.json"


def _read(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _secs(v) -> list[float]:
    return [s for s in (v if isinstance(v, list) else []) if not isinstance(s, bool) and (_num(s) or s == 0)]


def per_file(what: str = "") -> float | None:
    """The median seconds per file measured for `what`, None before any."""
    got = _secs(_read(_history_path()).get(what or "-"))
    return statistics.median(got) if got else None


def record(seconds: list[float], what: str = "") -> None:
    if not seconds:
        return
    with _locked():
        h = _read(_history_path())
        h[what or "-"] = (_secs(h.get(what or "-")) + [round(s, 2) for s in seconds])[-KEEP:]
        project.durable_write(_history_path(), json.dumps(h, sort_keys=True))


def window(what: str = "") -> float:
    fixed = _setting("TTP_POOL_SHARD_STALL_S", "pool_shard_stall_s")
    if fixed:
        return fixed
    floor = _setting("TTP_POOL_SHARD_STALL_FLOOR_S", "pool_shard_stall_floor_s") or STALL_FLOOR_S
    med = per_file(what)
    return max(floor, STALL_FACTOR * med) if med else floor


def _mark_path(pool: str) -> Path:
    return _dir() / "pool-stalls" / (re.sub(r"[^A-Za-z0-9._-]", "_", pool)[:100] + ".json")


def mark(pool: str, shard: str, why: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _locked() as d:
        project.durable_write(_mark_path(pool), json.dumps({"pool": pool, "since": now, "shard": shard, "why": why}))
        with open(d / LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(now))}\tstall\t{pool}\t{shard}\t{why}\n")


def marked(pool: str, now: float | None = None) -> dict | None:
    """The pool's stall mark while it is fresh (younger than MARK_TTL_S), else None."""
    m = _read(_mark_path(pool))
    since = _num(m.get("since"))
    now = time.time() if now is None else now
    return m if since and now - since < MARK_TTL_S else None


def clear(pool: str) -> bool:
    """Drop the pool's stall mark (a file was done there); True when it had one."""
    try:
        _mark_path(pool).unlink()
    except FileNotFoundError:
        return False
    with _locked() as d, open(d / LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}\tclear\t{pool}\t-\tfile progress\n")
    return True


def order(pools: list[str], now: float | None = None) -> tuple[list[str], list[str]]:
    """(the pools to try, in order, the pools skipped for a fresh stall mark). Every pool marked: the one
    marked longest ago only."""
    fresh = {p: marked(p, now) for p in pools}
    ok = [p for p in pools if not fresh[p]]
    if ok:
        return ok, [p for p in pools if fresh[p]]
    oldest = min(pools, key=lambda p: fresh[p]["since"])
    return [oldest], [p for p in pools if p != oldest]


def _end(proc: subprocess.Popen, grace: float) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


def _cancel(cancel: str, env: dict, tell: Callable[[str], None]) -> None:
    if not cancel:
        return
    try:
        rc = subprocess.run(["sh", "-c", cancel], env=env, timeout=CANCEL_S, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        tell(f"its cancel command failed: {e}")
        return
    if rc:
        tell(f"its cancel command exited {rc}")


def attempt(cmd: list[str], pool: str, shard: str, win: float, done: re.Pattern, cancel: str = "",
            what: str = "", tell: Callable[[str], None] = print, out=None,
            grace: float = GRACE_S) -> int | None:
    """Run the shard once on `pool`: its exit code, or None when it made no file progress for `win` s
    (it is ended, its job cancelled, the pool marked)."""
    out = sys.stdout if out is None else out
    env = {**os.environ, "TTP_POOL": pool, "TTP_POOL_SHARD": shard}
    argv = [w.replace("{pool}", pool).replace("{shard}", shard) for w in cmd]
    wait = checkwait.begin(f"pool shard {shard} on {pool} before its first file", pool=True)
    lines: queue.Queue = queue.Queue()
    proc = None
    try:   # own session: a stalled shard is ended whole (its process group), batch-job client included
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                stdin=subprocess.DEVNULL, start_new_session=True)

        def pump() -> None:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="replace")
                out.write(line)
                out.flush()
                if done.search(line.rstrip("\n")):
                    lines.put(time.monotonic())
            lines.put(None)
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        last, files, gaps, eof = time.monotonic(), 0, [], False
        while True:
            if eof:   # its output closed: it may still run (a daemonized job), so the window still holds
                t = False
                try:
                    proc.wait(timeout=max(0.05, min(1.0, last + win - time.monotonic())))
                    t = None
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    t = lines.get(timeout=max(0.05, min(1.0, last + win - time.monotonic())))
                except queue.Empty:
                    t = False
                    if proc.poll() is not None:   # it ended; a leftover holding its output open is not waited on
                        reader.join(1)
                        if not lines.empty():
                            continue
                        t = None
            if t is False:
                if time.monotonic() - last < win:
                    continue
                _end(proc, grace)
                why = f"no file done in {win:.0f} s" + (f" after its file {files}" if files else " (none started)")
                tell(f"shard {shard} on {pool} STALLED: {why}; ended")
                _cancel(cancel, env, tell)
                mark(pool, shard, why)
                record(gaps, what)
                return None
            if t is None:
                if proc.poll() is None:
                    eof = True
                    continue
                reader.join(1)
                rc = proc.wait()
                record(gaps, what)
                return 128 - rc if rc < 0 else rc
            files += 1
            if files == 1:
                checkwait.end(wait)
                wait = None
                if clear(pool):
                    tell(f"{pool} made progress again; its stall mark is cleared")
            else:
                gaps.append(t - last)
            last = t
    finally:
        checkwait.end(wait)
        if proc is not None and proc.poll() is None:   # interrupted: leave nothing behind
            _end(proc, grace)
            _cancel(cancel, env, tell)
        elif proc is not None:   # ended: its leftovers too (the group outlives its leader only while they run)
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGTERM)


def run(cmd: list[str], pools: list[str], shard: str, cancel: str = "", what: str = "",
        win: float | None = None, done_re: str = DONE_RE, tell: Callable[[str], None] = print,
        out=None, grace: float = GRACE_S) -> int:
    """Run the shard on the first pool without a fresh stall mark, then on the next after each stall: the
    exit code of the run that finished, STALLED when it stalled on every pool it could try."""
    done = re.compile(done_re)
    win = win or window(what)
    todo, skipped = order(pools)
    for p in skipped:
        m = marked(p) or {}
        tell(f"{p} skipped: stalled ({m.get('why', '')}, shard {m.get('shard', '?')}) until a file is done "
             f"there, or {MARK_TTL_S / 60:.0f} min after its stall")
    tried = []
    for pool in todo:
        if tried:
            tell(f"shard {shard}: re-running its files on {pool} after a stall on {tried[-1]}")
        rc = attempt(cmd, pool, shard, win, done, cancel, what, tell, out, grace)
        if rc is not None:
            return rc
        tried.append(pool)
    tell(f"shard {shard} made no file progress on {', '.join(tried)}; no other pool to try")
    return STALLED

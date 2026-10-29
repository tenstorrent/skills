# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The wall-time cap of a `ttp checks` run, so a stuck run never holds its project's checks slot for good.

Once its slots are granted, a run's check commands share one deadline: the cap from the time the first
one starts. A command still running at the deadline is stopped (its process group, and whatever it left,
as any stopped check: cli._run_check), the run ends, its slots free with it, and the outcome is a
timeout, never a pass: checks.json and check_failure.json say "timed_out", the project's TIMEOUTS file
keeps it per task, and the task's next `ttp checks` says so first (last_timeout). The steps between
commands (a clean checkout) are not under the cap. A run cut by a reboot, a kill or a crash frees its
slots with its process (they are OS file locks); its next run starts afresh.

The cap is on by default and needs no setup (cap()): FACTOR times the longest of the project's last
RECENT completed runs (RUNS_LOG; a timed-out run is not one), within FLOOR_S and CEILING_S, once it has
MIN_RUNS of them; DEFAULT_S before that. `checks.wall_cap_s` (seconds; in project.json, else the account
settings, else TTP_CHECKS_WALL_CAP_S) sets it; 0 turns it off.

A hang need not wait for the cap: a command whose output has not grown for its silence limit (SILENT_S,
10 min; a check object's `"silent_s"` sets it, 0 turns it off) is stopped the same way and recorded as a
timeout with "silent_s" (wait, said_silent). A check that is silent longer by design raises its silent_s.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import project
from .project import durable_append, durable_write, write_json

FLOOR_S = 3600.0          # never below 1 h, however fast recent runs were
CEILING_S = 6 * 3600.0    # never above 6 h by default (a set checks.wall_cap_s may be higher)
DEFAULT_S = 3 * 3600.0    # before MIN_RUNS completed runs are known
FACTOR = 3.0
RECENT = 20
MIN_RUNS = 3
KEEP_S = 30 * 86400.0     # older run times no longer count
ENV = "TTP_CHECKS_WALL_CAP_S"
RUNS_LOG = "checks-run-times.jsonl"
RUNS_MAX_BYTES = 64 * 1024
TIMEOUTS = "checks-timeouts.json"
TIMEOUTS_MAX = 50
RC = 124   # a timed-out check's exit code in the log and records (as timeout(1)'s)
SILENT_S = 600.0   # a command whose output has not grown this long is a hang (Check.silent_s; 0: off)
POLL_S = 5.0       # how often wait() looks at the output's size
clock = time.monotonic   # wait()'s clock; tests swap in a test clock


def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        n = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def recent(state: Path | None, now: float | None = None) -> list[float]:
    """The wall times in seconds of the project's last RECENT completed runs within KEEP_S."""
    if state is None:
        return []
    now = time.time() if now is None else now
    out = []
    try:
        lines = (Path(state) / RUNS_LOG).read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            r = json.loads(line)
            if now - float(r["ts"]) <= KEEP_S and float(r["s"]) > 0:
                out.append(float(r["s"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out[-RECENT:]


def cap(p, now: float | None = None) -> tuple[float, str]:
    """(the cap in seconds, 0 for none; how it was set) for project `p` (None: the account's or default)."""
    own = ((p.config() if p else {}).get("checks") or {}) if p else {}
    for v, how in ((own.get("wall_cap_s") if isinstance(own, dict) else None, "checks.wall_cap_s"),
                   ((project.load_account_settings().get("checks") or {}).get("wall_cap_s"), "account checks.wall_cap_s"),
                   (os.environ.get(ENV), ENV)):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n, how
    runs = recent(p.state if p else None, now)
    if len(runs) < MIN_RUNS:
        return DEFAULT_S, f"default, before {MIN_RUNS} timed runs"
    return (min(CEILING_S, max(FLOOR_S, FACTOR * max(runs))),
            f"{FACTOR:g}x the longest of the last {len(runs)} runs, within {FLOOR_S / 3600:g}-{CEILING_S / 3600:g} h")


def record_run(state: Path | None, seconds: float, passed: bool, key: str = "") -> None:
    """Keep one completed run's wall time for cap(), with `key` (its command list's hash, for
    checklane.median_s) when given; a failed write only loses a sample."""
    if state is None or seconds <= 0:
        return
    path = Path(state) / RUNS_LOG
    try:
        if path.exists() and path.stat().st_size > RUNS_MAX_BYTES:
            durable_write(path, "".join(f"{x}\n" for x in path.read_text().splitlines()[-RECENT:]))
        durable_append(path, json.dumps({"ts": time.time(), "s": round(seconds, 3), "passed": bool(passed),
                                            **({"k": key} if key else {})}) + "\n")
    except OSError:
        pass


def deadline_left(deadline: float | None) -> float | None:
    """The seconds a command may still run before `deadline` (None: no cap), at least a moment."""
    return None if deadline is None else max(0.01, deadline - time.time())


class Silent(subprocess.TimeoutExpired):
    """A command stopped by wait() for no output for its silence limit (`timeout`)."""


def silent_limit(check) -> float:
    """The silence limit in seconds of `check` (a push.Check or plain command): its silent_s, else SILENT_S."""
    v = getattr(check, "silent_s", None)
    return SILENT_S if v is None else float(v)


def wait(proc: subprocess.Popen, out, timeout: float | None, silent_s: float) -> int:
    """`proc`'s exit code. subprocess.TimeoutExpired past `timeout` seconds; Silent once the file `out`
    (its output) has not grown for `silent_s` seconds (0, or an `out` with no file, watches nothing)."""
    try:
        fd = out.fileno() if silent_s > 0 and out is not None else None
    except (AttributeError, OSError, ValueError):
        fd = None
    if fd is None:
        return proc.wait(timeout=timeout)

    def size() -> int:
        try:
            return os.fstat(fd).st_size
        except OSError:
            return -1

    start = clock()
    end = None if timeout is None else start + timeout
    last, quiet_since = size(), start
    while True:
        step = POLL_S if end is None else max(0.01, min(POLL_S, end - clock()))
        try:
            return proc.wait(timeout=step)
        except subprocess.TimeoutExpired:
            pass
        now = clock()
        if end is not None and now >= end:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        if (grown := size()) != last:
            last, quiet_since = grown, now
        elif now - quiet_since >= silent_s:
            raise Silent(proc.args, silent_s)


def _mins(s: float) -> str:
    m = int(round(max(0.0, s) / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def span(s: float) -> str:
    """`s` seconds as said to the user: whole seconds under a minute, else _mins."""
    return f"{s:.0f} s" if s < 60 else _mins(s)


def said(check: str, cap_s: float, how: str, head: str) -> str:
    return (f"{check!r} was stopped on {head[:12]} at the checks wall-time cap of {_mins(cap_s)} ({how}): a timeout, "
            f"not a pass; its process group was ended and the checks slot released (a stuck or slow suite: find "
            f"what hangs, or raise checks.wall_cap_s)")


def said_silent(check: str, silent_s: float, head: str) -> str:
    return (f"{check!r} was stopped on {head[:12]} after no output for {span(silent_s)} (its silence limit, "
            f"silent_s): a hang, a timeout, not a pass; its process group was ended and the checks slot released "
            f"(find what hangs; a check that is silent longer by design: raise its \"silent_s\", 0 turns it off)")


def said_for(rec: dict) -> str:
    """What a timed-out run's record (checks.json "timed_out") says: no output, else the wall-time cap."""
    if rec.get("silent_s"):
        return said_silent(str(rec.get("check")), float(rec["silent_s"]), str(rec.get("head") or ""))
    return said(str(rec.get("check")), float(rec.get("cap_s") or 0), str(rec.get("how") or ""), str(rec.get("head") or ""))


def record_timeout(state: Path | None, task: str | None, rec: dict) -> None:
    """Keep the timeout per task (TIMEOUTS), so the task's next run says so (last_timeout)."""
    if state is None or not task:
        return
    path = Path(state) / TIMEOUTS
    try:
        try:
            got = json.loads(path.read_text())
        except (OSError, ValueError):
            got = {}
        got = got if isinstance(got, dict) else {}
        got[str(task)] = {**rec, "ts": time.time()}
        write_json(path, dict(sorted(got.items(), key=lambda kv: float((kv[1] or {}).get("ts") or 0))[-TIMEOUTS_MAX:]))
    except OSError:
        pass


def clear_timeout(state: Path | None, task: str | None) -> None:
    """Drop the task's TIMEOUTS entry once a later run completed without the wall-time cap,
    so the task's next run no longer reports the old timeout as its last checks run."""
    if state is None or not task:
        return
    path = Path(state) / TIMEOUTS
    try:
        try:
            got = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        if not (isinstance(got, dict) and str(task) in got):
            return
        got.pop(str(task), None)
        write_json(path, got)
    except OSError:
        pass


def last_timeout(state: Path | None, task: str | None) -> dict | None:
    """The task's last recorded checks timeout, or None."""
    if state is None or not task:
        return None
    try:
        rec = json.loads((Path(state) / TIMEOUTS).read_text()).get(str(task))
    except (OSError, ValueError, AttributeError):
        return None
    return rec if isinstance(rec, dict) else None

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Stalled `ttp checks` runs, told apart from slow ones without a model (Daemon.check_checks_stall).

A running check's lane label names its checks.log (testslots.suite_running, written by the checks process
as its first command starts). Each minute the daemon reads, for each such lane holder of the project, when
that log last grew (its mtime, never before the run started) and the CPU its process tree has used (live
processes' utime and stime, with their reaped children's: tree_cpu_s). A run is `stalled` once its log has
not grown for stall_s(): `checks.stall_s` seconds when set (0: off), else STALL_FACTOR times that check's
median passing run (its lane label's cost.est_s), never under STALL_MIN_S. A log that has not grown for a
whole usual run is far past any pause of a working suite, and the floor sits above checkwall.SILENT_S, so
the checks process's own silence limit on one silent command acts first. Only growth counts: a run slowed
by host load whose log still grows is never stalled, however long it takes.

Each stall episode (a run and the log size it stopped at) raises one `checks_stalled` event and writes
MARKER in the run's folder, so `ttp checks --result` says so too; `ttp checks status` and the lane wait
lines say it from the log alone (view). The run is stopped (jobstop.end_checks, only when checks.pid in
the log's folder is the lane holder) only when its log is stalled AND its process tree has used no CPU
(under CPU_IDLE_FRAC of one core, plus CPU_EPS_S) for the same threshold: a busy loop is left to the
wall-time cap (checkwall.cap). The stopped run ends with a failing exit code and MARKER's `stopped`:
a retryable failure, never a pass. The daemon keeps each run's CPU reading in kv KV, so a restart keeps
its clock; a run first seen counts its CPU as moving from then (never stopped early).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

STALL_MIN_S = 900.0     # never stalled under 15 min without growth
STALL_FACTOR = 1.0      # ... else as long as one usual (median) passing run of that check
CPU_IDLE_FRAC = 0.01    # a tree using under 1% of one core between readings (plus CPU_EPS_S) is idle
CPU_EPS_S = 0.5
KV = "checks_stall"     # kv: {run key: {"cpu": seconds, "at": when it last moved}}
EVENT = "checks_stalled"
MARKER = "checks.stalled"   # in the run's folder: the last stall episode, and whether it was stopped


def stall_s(cfg: dict | None, rec: dict) -> float:
    """The stall threshold of lane holder `rec` in seconds (0: off): checks.stall_s, else STALL_FACTOR times
    its label's median (cost.est_s), at least STALL_MIN_S."""
    v = ((cfg or {}).get("checks") or {}).get("stall_s") if isinstance((cfg or {}).get("checks"), dict) else None
    try:
        if v is not None and float(v) >= 0:
            return float(v)
    except (TypeError, ValueError):
        pass
    est = (rec.get("cost") or {}).get("est_s") if isinstance(rec.get("cost"), dict) else None
    return max(STALL_MIN_S, STALL_FACTOR * float(est) if isinstance(est, (int, float)) and est > 0 else 0.0)


def key(rec: dict) -> str:
    return f"{rec.get('pid')}:{float(rec.get('run_since') or 0):.3f}"


def _mtime(path: str) -> tuple[float, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime, st.st_size


def tree_cpu_s(pid: int) -> float | None:
    """The CPU seconds process `pid` and its live descendants have used (with their reaped children's);
    None when it cannot be read (gone, another PID namespace, no /proc)."""
    from . import jobuse
    tree = jobuse._tree(pid)
    if not tree:
        return None
    hz = jobuse._hz()
    return sum(jobuse._cpu(x, hz) for x in tree)


def view(rec: dict, now: float, cfg: dict | None, mark: dict | None = None, stat=_mtime) -> dict | None:
    """Lane holder `rec`'s stall state: log, grew_at, log_idle_s, threshold_s, stalled, and from the daemon's
    CPU reading `mark` (else None) cpu_idle_s and stop (stalled and idle that long). None for a holder that
    names no log or is not running its commands yet."""
    log, since = rec.get("log"), rec.get("run_since")
    if not log or not isinstance(since, (int, float)):
        return None
    st = stat(str(log))
    grew = max(float(since), st[0]) if st else float(since)
    limit = stall_s(cfg, rec)
    idle = max(0.0, now - grew)
    stalled = limit > 0 and idle >= limit
    cpu_idle = max(0.0, now - float(mark["at"])) if isinstance(mark, dict) and "at" in mark else None
    return {"key": key(rec), "pid": rec.get("pid"), "task": str(rec.get("task") or ""), "log": str(log),
            "size": st[1] if st else None, "grew_at": grew, "log_idle_s": idle, "threshold_s": limit,
            "stalled": stalled, "cpu_idle_s": cpu_idle,
            "stop": bool(stalled and cpu_idle is not None and cpu_idle >= limit)}


def read_cpu(mark: dict | None, cpu: float | None, now: float) -> dict:
    """The next CPU reading of a run: `mark` kept while its tree's CPU (`cpu` seconds now) grew by no more
    than CPU_IDLE_FRAC of one core since then plus CPU_EPS_S, else moved to now. An unreadable tree
    (None) counts as moving: it is never stopped on a reading it could not take."""
    if cpu is None or not isinstance(mark, dict) or "cpu" not in mark:
        return {"cpu": cpu, "at": now}
    if cpu < float(mark["cpu"]) or cpu - float(mark["cpu"]) > CPU_IDLE_FRAC * (now - float(mark["at"])) + CPU_EPS_S:
        return {"cpu": cpu, "at": now}
    return mark


def holders(project_name: str) -> list[dict]:
    """The project's checks lane holders running their commands with a log named (suite_running), alive."""
    from . import locks, testslots
    out = []
    for base in testslots.suite_bases(project_name):
        for rec in locks.holder_records(testslots._lanes_of(base)):
            if rec.get("log") and rec.get("run_since") and rec.get("alive") is not False \
                    and isinstance(rec.get("pid"), int):
                out.append(rec)
    return out


def scan(project_name: str, cfg: dict | None, marks: dict, now: float, cpu=tree_cpu_s,
         stat=_mtime) -> tuple[list[dict], dict]:
    """(each running holder's view after this reading, the marks to keep: only of runs still holding)."""
    rows, keep = [], {}
    for rec in holders(project_name):
        k = key(rec)
        keep[k] = read_cpu(marks.get(k), cpu(int(rec["pid"])), now)
        if (v := view(rec, now, cfg, keep[k], stat)) is not None:
            rows.append(v)
    return rows, keep


def fingerprint(v: dict) -> str:
    return f"{EVENT}:{v['key']}:{v['size']}"


def mins(s: float) -> str:
    from . import testslots
    return testslots._mins(s)


def text(v: dict) -> str:
    """'STALLED: its log has not grown for 22 min (threshold 15 min, checks.stall_s), ...' for a stalled view."""
    cpu = (", its processes used no CPU for " + mins(v["cpu_idle_s"]) if v.get("stop")
           else ", its processes still use CPU (left running)" if v.get("cpu_idle_s") is not None else "")
    return (f"STALLED: its log has not grown for {mins(v['log_idle_s'])} (threshold {mins(v['threshold_s'])}, "
            f"checks.stall_s){cpu}")


def mark_run(v: dict, stopped: bool, now: float) -> None:
    """Write MARKER in the run's folder (the log's) for `ttp checks --result`. Best effort."""
    from .project import write_json
    try:
        write_json(Path(v["log"]).parent / MARKER, {**{k: v[k] for k in ("pid", "task", "grew_at", "log_idle_s",
                                                                          "threshold_s", "cpu_idle_s", "size")},
                                                    "stopped": stopped, "ts": now})
    except OSError:
        pass


def marker(run_dir: Path, pid, now: float | None = None) -> dict | None:
    """MARKER of run folder `run_dir` written for its checks of pid `pid` (checks.pid's: never an earlier
    run's): once stopped, else only while the log has not grown since (still that episode, its log_idle_s
    read again now)."""
    try:
        m = json.loads((Path(run_dir) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or pid is None or m.get("pid") != pid:
        return None
    if m.get("stopped"):
        return m
    st = _mtime(str(Path(run_dir) / "checks.log"))
    if st and st[1] != m.get("size"):
        return None
    now = time.time() if now is None else now
    return {**m, "log_idle_s": max(0.0, now - float(m.get("grew_at") or now))}


def line(m: dict) -> str:
    """`ttp checks --result`'s words for MARKER `m`."""
    head = (f"stalled: the log had not grown for {mins(float(m.get('log_idle_s') or 0))} "
            f"(threshold {mins(float(m.get('threshold_s') or 0))}, checks.stall_s)")
    if m.get("stopped"):
        return (f"{head} and its processes used no CPU for {mins(float(m.get('cpu_idle_s') or 0))}: the daemon "
                "stopped them; a failure (reason stalled, retryable), never a pass: run `ttp checks --detach` again")
    return f"{head}; its processes still use CPU, so it is left running"


def stop(p, v: dict) -> dict | None:
    """Stop stalled view `v`'s run through jobstop.end_checks, only when checks.pid in its log's folder is
    the lane holder's pid; None when it is not, or it already ended."""
    from . import jobstop
    from .cli import _read_checks_pid
    run_dir = Path(v["log"]).parent
    if _read_checks_pid(run_dir).get("pid") != v["pid"]:
        return None
    mark_run(v, True, time.time())
    why = (f"stalled: its log had not grown for {mins(v['log_idle_s'])} and its processes used no CPU for "
           f"{mins(v['cpu_idle_s'])} (threshold {mins(v['threshold_s'])}, checks.stall_s)")
    got = jobstop.end_checks(p, run_dir, why, int(v["task"]) if v["task"].isdigit() else None)
    if got is None:
        mark_run(v, False, time.time())
    return got

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Detached run supervisor. The daemon writes <run_dir>/run.json and starts
`python -m ttp._run <run_dir>` in its own session; this process owns the agent child:

- feeds the prompt on stdin (no argv size limit), streams output to output.jsonl;
- refreshes <run_dir>/lease every 30 s while the child lives, so liveness never depends on a model
  remembering to report it;
- enforces the wall-clock limit with TERM, then KILL 30 s later (a child that ignores TERM would
  otherwise run on past its bound);
- enforces the run's dollar budget mid-flight when the provider streams usage;
- ends the child when <run_dir>/STOP appears (a cancel, or `ttp stop --kill` writing "shutdown");
- writes exit.json exactly once, then asks the provider adapter for usage and records it.

It survives a daemon restart: the daemon re-adopts runs by run_dir, pid and boot id.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

LEASE_EVERY_S = 30
KILL_AFTER_S = 30
POLL_S = 5
BUDGET_EVERY_S = 10


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    try:  # macOS: whole seconds of kern.boottime; the microseconds field drifts between reads
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
        return out.split("sec =")[1].split(",")[0].strip()
    except Exception:
        return "unknown"


def _touch(p: Path) -> None:
    p.touch()
    os.utime(p, None)


def request_stop(run_dir: Path, why: str = "cancel") -> None:
    """Ask a run's supervisor to end its agent (TERM, then KILL after the grace period)."""
    (run_dir / "STOP").write_text(why)


def stop_runs(db, runs_dir: Path, task: int | None = None, why: str = "cancel") -> list[int]:
    """Request a stop for every running run (of one task, if given). Returns their run ids."""
    sql, args = "SELECT id, dir FROM runs WHERE status='running'", ()
    if task is not None:
        sql, args = sql + " AND task=?", (task,)
    ids = []
    for r in db.q(sql, args):
        run_dir = Path(r["dir"]) if r["dir"] else runs_dir / str(r["id"])
        if run_dir.is_dir():
            request_stop(run_dir, why)
            ids.append(r["id"])
    return ids


def stop_reason(run_dir: Path) -> str | None:
    """"shutdown" when the whole project was stopped (the task goes back to the queue), else
    "stopped" (a cancel). None while there is no STOP file."""
    try:
        text = (run_dir / "STOP").read_text().strip()
    except OSError:
        return None
    return "shutdown" if text == "shutdown" else "stopped"


def supervise(run_dir: Path) -> int:
    spec = json.loads((run_dir / "run.json").read_text())
    argv, env_extra, cwd = spec["argv"], spec.get("env", {}), spec["cwd"]
    timeout_s, budget = float(spec.get("timeout_s", 3600)), spec.get("budget_usd")
    stall_s = float(spec.get("stall_s") or 0)
    env = {**os.environ, **env_extra}
    lease, out_path = run_dir / "lease", run_dir / "output.jsonl"
    _touch(lease)
    prompt = open(run_dir / "prompt.md", "rb")
    out = open(out_path, "wb")
    err = open(run_dir / "stderr.log", "wb")
    started = time.time()
    child = subprocess.Popen(argv, stdin=prompt, stdout=out, stderr=err, cwd=cwd, env=env,
                             start_new_session=True)
    (run_dir / "child.pid").write_text(str(child.pid))
    reason: list[str] = []

    def stop(why: str) -> None:
        if reason:
            return
        reason.append(why)
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + KILL_AFTER_S
        while time.time() < deadline and child.poll() is None:
            time.sleep(1)
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def watch() -> None:
        from .providers import get_provider  # local import: keeps startup cheap
        prov = get_provider(spec["provider"])
        last_lease = last_budget = 0.0
        while child.poll() is None:
            now = time.time()
            if now - last_lease >= LEASE_EVERY_S:
                _touch(lease)
                last_lease = now
            if time.time() - started > timeout_s:
                threading.Thread(target=stop, args=("timeout",), daemon=True).start()
            if stall_s:
                # Stalled = the agent has produced nothing (no stream event, no progress note) for
                # stall_s. A tool call waiting on a long job stays inside one event, so stall_s must
                # exceed the longest single command the task is expected to run.
                marks = [started] + [f.stat().st_mtime for f in (out_path, run_dir / "progress.md") if f.exists()]
                if time.time() - max(marks) > stall_s:
                    threading.Thread(target=stop, args=("stalled",), daemon=True).start()
            if budget is not None and now - last_budget >= BUDGET_EVERY_S:
                last_budget = now
                try:
                    so_far = prov.cost_so_far(out_path)
                except Exception:
                    so_far = None
                if so_far is not None and so_far > float(budget):
                    threading.Thread(target=stop, args=("budget",), daemon=True).start()
            why = stop_reason(run_dir)
            if why:
                threading.Thread(target=stop, args=(why,), daemon=True).start()
            time.sleep(POLL_S)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    rc = child.wait()
    ended = time.time()
    for f in (prompt, out, err):
        f.close()
    exit_info = {"rc": rc, "started": started, "ended": ended, "stopped": reason[0] if reason else None}
    tmp = run_dir / "exit.json.tmp"
    tmp.write_text(json.dumps(exit_info))
    os.replace(tmp, run_dir / "exit.json")
    return rc


if __name__ == "__main__":
    sys.exit(supervise(Path(sys.argv[1])))

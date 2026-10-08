# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whole-run resource slots kept for a run's detached jobs.

An `exclusive:<name>` task's run supervisor holds a slot of each resource until its agent ends. A job
the agent started with `ttp detach` may still use the resource then, and its own `ttp lock` passes
through on the run's hold. So when the agent ends with such a job alive, the supervisor hands its
open slot files to a keeper (`python -m ttp.hold <run_dir> <fd...>`, a session of its own): the lock
never lapses, and it ends once every job wrote its .rc or is gone (a kill, a reboot), or when a later
run of the same task takes the slots over (`hold.handover` in the run folder).

<run_dir>/hold.json records the hold; the keeper holds <run_dir>/hold.lock while it lives. The
project's state/holds/<run>.json points at it, so the daemon (tend) logs each release, and rebuilds the
hold of a keeper gone while its jobs still run (a `ttp stop --kill`, a crash): a dead keeper never
keeps a slot, and a lost one is taken again unless someone else got it meanwhile.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import locks, poll_s
from .project import durable_write

POLL_S = 2
RECORD, LIVE, HANDOVER = "hold.json", "hold.lock", "hold.handover"
LABEL = "detached jobs"


def label(holder: str, run: str) -> str:
    """What a kept slot reads as in `ttp status` and the web app."""
    return f"{holder} (run {run}), {LABEL}"


def own_keeper(lbl: str, holder: str) -> bool:
    """Whether a slot label is a keeper's of this holder (a task, or a project's task when shared)."""
    return lbl.startswith(f"{holder} (run ") and lbl.endswith(f", {LABEL}")


def read(run_dir: Path) -> dict | None:
    try:
        rec = json.loads((Path(run_dir) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _write(run_dir: Path, rec: dict) -> None:
    durable_write(Path(run_dir) / RECORD, json.dumps(rec, indent=1))


def run_jobs(run_dir: Path) -> list[str]:
    """The .rc paths of the jobs this run detached."""
    try:
        jobs = json.loads((Path(run_dir) / "detached.json").read_text())
    except (OSError, ValueError):
        return []
    return [str(j["rc"]) for j in jobs if isinstance(j, dict) and j.get("rc")] if isinstance(jobs, list) else []


def live(rcs: list[str]) -> list[str]:
    return [r for r in rcs if not locks.job_ended(Path(r))]


def _spawn(run_dir: Path, fds: list[int]) -> int:
    with open(Path(run_dir) / "hold.log", "ab") as out:
        proc = subprocess.Popen([sys.executable, "-m", "ttp.hold", str(run_dir), *map(str, fds)],
                                cwd=str(Path(__file__).resolve().parent.parent),
                                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                pass_fds=tuple(fds))
    return proc.pid


def keep(run_dir: Path, spec: dict, held: list, wanted: list[dict], inherited: list[str]) -> int | None:
    """Called by the run supervisor once its agent ended, before it closes `held` (the open slot files,
    in `wanted` order): when a job of this run, or one a handed-over hold kept for (`inherited`), still
    runs, a keeper takes the slots over and its pid is returned. None when nothing is kept."""
    jobs = live(run_jobs(run_dir) + list(inherited))
    registry = spec.get("holds")
    if not held or not jobs or not registry:
        return None
    env = spec.get("env") or {}
    task, run = str(env.get("TTP_TASK") or "?"), str(env.get("TTP_RUN_ID") or Path(run_dir).name)
    lock = locks.try_take([Path(run_dir) / LIVE], f"keeper of run {run}")
    if lock is None:
        return None
    resources = []
    for w, f in zip(wanted, held):   # the same open file: relabelled, never let go
        holder = w.get("holder") or f"task #{task}"   # a shared resource's names the project too
        resources.append({"resource": w.get("resource"), "path": f.name, "holder": holder,
                          "label": label(holder, run)})
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"holder": label(holder, run), "since": time.time(), "command": LABEL}))
        f.flush()
    rec = {"task": task, "run": run, "since": time.time(), "state": "holding", "resources": resources,
           "jobs": jobs}
    _write(run_dir, rec)
    Path(registry).mkdir(parents=True, exist_ok=True)
    durable_write(Path(registry) / f"{run}.json", json.dumps({"run_dir": str(Path(run_dir).resolve())}))
    try:
        rec["keeper"] = _spawn(run_dir, [lock.fileno(), *(f.fileno() for f in held)])
    except OSError as e:
        rec.update(state="released", ended=time.time(), why=f"its keeper could not start: {e}")
        _write(run_dir, rec)
        lock.close()
        return None
    lock.close()
    _write(run_dir, rec)
    return rec["keeper"]


def take_over(registry: str | None, holder: str, resource: str, run: str) -> list[str]:
    """A later run of the same task wants a slot its earlier run's keeper holds: ask that keeper to let
    go (it does within POLL_S) and return the jobs it kept for, so this run keeps them in turn."""
    jobs: list[str] = []
    if not registry:
        return jobs
    try:
        entries = sorted(Path(registry).glob("*.json"))
    except OSError:
        return jobs
    for entry in entries:
        try:
            rd = Path(json.loads(entry.read_text())["run_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        rec = read(rd)
        if not rec or rec.get("state") != "holding" or not any(
                r.get("resource") == resource and r.get("holder") == holder for r in rec.get("resources") or []):
            continue
        if not (rd / HANDOVER).exists():
            durable_write(rd / HANDOVER, run)
        jobs += [j for j in rec.get("jobs") or [] if j not in jobs]
    return jobs


def _settle(run_dir: Path, rec: dict, state: str, why: str) -> None:
    rec.update(state=state, ended=time.time(), why=why)
    _write(run_dir, rec)
    try:
        with open(Path(run_dir) / "progress.md", "a") as pf:
            pf.write(f"{time.strftime('%H:%M:%S')} {', '.join(r['resource'] for r in rec['resources'])}: {why}\n")
    except OSError:
        pass


def watch(run_dir: Path, fds: list[int]) -> int:
    """The keeper: hold the inherited slots until every job ended or a later run takes them over."""
    run_dir = Path(run_dir)
    lock_fd, slot_fds = fds[0], fds[1:]
    while True:
        rec = read(run_dir)
        if rec is None or rec.get("state") != "holding":
            why = None
        elif (run_dir / HANDOVER).exists():
            nxt = (run_dir / HANDOVER).read_text().strip()
            why, state = f"released: handed over to run {nxt or '?'} of the same task", "handed_over"
        elif not live(rec.get("jobs") or []):
            why, state = "released: every detached job of the run ended", "released"
        else:
            time.sleep(poll_s(POLL_S))
            continue
        if why:   # recorded first, so a slot seen free always reads as released
            _settle(run_dir, rec, state, why)
        for fd in (*slot_fds, lock_fd):
            os.close(fd)
        return 0


def holding(registry: Path) -> list[dict]:
    """The holds in the registry still kept for detached jobs, each with its `run_dir`."""
    out: list[dict] = []
    try:
        entries = sorted(Path(registry).glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        try:
            rd = Path(json.loads(entry.read_text())["run_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        rec = read(rd)
        if rec and rec.get("state") == "holding":
            out.append({**rec, "run_dir": str(rd)})
    return out


def tend(registry: Path) -> list[dict]:
    """Settle the holds in the registry: a dead keeper's whose jobs ended is released, one whose jobs
    still run is taken again by a new keeper (or recorded lost if someone else holds a slot now). Returns
    each hold that changed (released, handed over, rebuilt or lost) once, for the caller to log; an
    ended hold leaves the registry then."""
    out: list[dict] = []
    try:
        entries = sorted(Path(registry).glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        try:
            rd = Path(json.loads(entry.read_text())["run_dir"])
        except (OSError, ValueError, KeyError, TypeError):
            entry.unlink(missing_ok=True)
            continue
        rec = read(rd)
        if rec and rec.get("state") == "holding":
            lock = locks.try_take([rd / LIVE], f"keeper of run {rec.get('run')}")
            if lock is None:
                continue   # its keeper lives
            if not live(rec.get("jobs") or []):
                lock.close()
                _settle(rd, rec, "released", "released: its detached jobs ended while no keeper ran")
            else:
                slots = [locks.try_take([Path(r["path"])], r.get("label") or LABEL, LABEL)
                         for r in rec.get("resources") or []]
                if all(slots):
                    try:
                        rec["keeper"] = _spawn(rd, [lock.fileno(), *(s.fileno() for s in slots)])
                        rec["rebuilt"] = int(rec.get("rebuilt") or 0) + 1
                        _write(rd, rec)
                        out.append({**rec, "state": "rebuilt"})
                    except OSError as e:
                        _settle(rd, rec, "lost", f"its keeper died and a new one could not start: {e}")
                else:
                    taken = [r["resource"] for r, s in zip(rec.get("resources") or [], slots) if not s]
                    _settle(rd, rec, "lost", f"its keeper died and {', '.join(taken)} was taken meanwhile; "
                                             f"its detached jobs run without the hold")
                for s in slots:
                    if s:
                        s.close()
                lock.close()
                if rec.get("state") == "holding":
                    continue
        if rec:
            out.append(rec)
        entry.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    sys.exit(watch(Path(sys.argv[1]), [int(x) for x in sys.argv[2:]]))

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whole-run resource slots kept for a run's detached jobs.

An `exclusive:<name>` task's run supervisor holds a slot of each resource until its agent ends. A job
the agent started with `ttp detach` may still use the resource then, and its own `ttp lock` passes
through on the run's hold. So when the agent ends with such a job alive, the supervisor hands its
open slot files to a keeper (`python -m ttp.hold <run_dir> <fd...>`, a session of its own): the lock
never lapses, and it ends once every job wrote its .rc or is gone (a kill, a reboot), or when a later
run of the same task takes the slots over (`hold.handover` in the run folder). A job that drives
another host itself (`ttp detach --remote-host`) also ends once that host reset or its remote pid is
gone with no completion marker: the keeper asks the host every REMOTE_PROBE_S, from a thread of its own
so a slow ssh never delays a handover, and ends the job with that verdict (detach.remote_verdict). The same probe refreshes the mirrored
exit code and log tail of the run's `ttp detach --remote` jobs (detach.mirror_refresh).

<run_dir>/hold.json records the hold; the keeper holds <run_dir>/hold.lock while it lives and touches
<run_dir>/hold.beat every BEAT_EVERY_S, so a lock probe in another PID namespace, which can neither see its
pid nor read its flock, still tells it lives (locks._through_run). The
project's state/holds/<run>.json points at it, so the daemon (tend) logs each release, and rebuilds the
hold of a keeper gone while its jobs still run (a `ttp stop --kill`, a crash): a dead keeper never
keeps a slot, and a lost one is taken again unless someone else got it meanwhile.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import locks, poll_s, timefmt
from .project import durable_write

POLL_S = 2
REMOTE_PROBE_S = 120   # how often a keeper asks a remote host whether a job's remote side lives
RECORD, LIVE, HANDOVER, BEAT = "hold.json", "hold.lock", "hold.handover", "hold.beat"
BEAT_EVERY_S = 30   # how often a keeper touches BEAT while it holds
LABEL = "detached jobs"
WHOLE_RUN = "whole run"

# Every hold label is built by label() and read by parse(): a `ttp lock` command's "task #7 (run 3)",
# a run supervisor's "task #7 (run 3), whole run" and a keeper's "task #7 (run 3), detached jobs". On a
# shared resource the holder names the project first (shared.holder): "demo task #7 (run 3)".
_LABEL_RE = re.compile(r"(?P<holder>(?:(?P<project>[A-Za-z0-9][A-Za-z0-9._-]*) )?task #(?P<task>\d+))"
                       rf" \(run (?P<run>[^()]*)\)(?:, (?P<kind>{WHOLE_RUN}|{LABEL}))?")


def who(task) -> str:
    """The holder a task's holds and reservations name (shared.holder adds the project on a shared one)."""
    return f"task #{task if task not in (None, '') else '?'}"


def label(holder: str, run, kind: str = "") -> str:
    """A slot's hold label, as `ttp status` and the web app show it: kind is "" for a `ttp lock` command,
    WHOLE_RUN for an exclusive task's run supervisor and LABEL for a keeper of its detached jobs."""
    return f"{holder} (run {run if run not in (None, '') else '?'})" + (f", {kind}" if kind else "")


def parse(lbl) -> dict | None:
    """A label() as {"holder", "project" (None when not shared), "task" (int), "run" (its text, "?" when
    unknown), "kind"}; None for any other text (a reservation, another lock's label, a hold taken
    outside a task)."""
    m = _LABEL_RE.fullmatch(str(lbl or ""))
    return m and {"holder": m["holder"], "project": m["project"], "task": int(m["task"]), "run": m["run"],
                  "kind": m["kind"] or ""}


def task_of(lbl, project: str | None = None) -> int | None:
    """The task id a hold label names; None when it is not a hold label, or names a project other than
    `project` (when given)."""
    h = parse(lbl)
    if not h or (project is not None and h["project"] not in (None, project)):
        return None
    return h["task"]


def own_keeper(lbl: str, holder: str) -> bool:
    """Whether a slot label is a keeper's of this holder (a task, or a project's task when shared)."""
    h = parse(lbl)
    return bool(h) and h["kind"] == LABEL and h["holder"] == holder


def covers(paths: list[Path], holder: str, spec: dict, env=None) -> str | None:
    """The label of a slot among `paths` that the caller's own run lineage holds, so its `ttp lock` runs
    under that hold (the whole-run pass-through), else None. The caller is a run of an exclusive task
    holding the resource (its run.json `spec`, from TTP_RUN_DIR), or a `ttp detach` job of one; a slot
    covers it only when held by the same `holder` (task, and project when shared) as: this very run
    (TTP_RUN_ID), a later run of the task (which took this run's jobs over), or the keeper of detached
    jobs whose hold.json lists the caller's job (TTP_DETACH_RC). Another task's or project's hold, a
    plain `ttp lock` command's, or a keeper that does not list the caller never covers it."""
    from .progress import JOB_ENV
    env = os.environ if env is None else env
    run = str(env.get("TTP_RUN_ID") or "")
    job = env.get(JOB_ENV) if env.get("TTP_DETACHED") else None
    job = str(Path(job).resolve()) if job else None
    for lbl in locks.held_labels(paths):
        h = parse(lbl)
        if not h or h["holder"] != holder:
            continue
        if h["kind"] == WHOLE_RUN and (h["run"] == run or job):
            return lbl
        if h["kind"] == LABEL and job and spec.get("holds"):
            try:
                rd = Path(json.loads((Path(spec["holds"]) / f"{h['run']}.json").read_text())["run_dir"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            rec = read(rd) or {}
            if rec.get("state") == "holding" and job in {str(Path(j).resolve()) for j in rec.get("jobs") or []}:
                return lbl
    return None


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
        holder = w.get("holder") or who(task)   # a shared resource's names the project too
        resources.append({"resource": w.get("resource"), "path": f.name, "holder": holder,
                          "label": label(holder, run, LABEL)})
        f.seek(0)
        try:   # the taker's pid stays named until label_pid names the keeper: never a label without one
            was = json.loads(f.read() or "{}")
        except ValueError:
            was = {}
        owner = {k: was[k] for k in ("pid", "node", "pidns") if isinstance(was, dict) and k in was}
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"holder": label(holder, run, LABEL), "since": time.time(), "command": LABEL, **owner}))
        f.flush()
    rec = {"task": task, "run": run, "since": time.time(), "state": "holding", "resources": resources,
           "jobs": jobs}
    _write(run_dir, rec)
    Path(registry).mkdir(parents=True, exist_ok=True)
    durable_write(Path(registry) / f"{run}.json", json.dumps({"run_dir": str(Path(run_dir).resolve())}))
    try:
        rec["keeper"] = _spawn(run_dir, [lock.fileno(), *(f.fileno() for f in held)])
        for f in held:
            locks.label_pid(f, rec["keeper"])
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
            pf.write(f"{timefmt.clock()} {', '.join(r['resource'] for r in rec['resources'])}: {why}\n")
    except OSError:
        pass


def _remote_round(jobs: list[str], memo: dict, verdicts: list[str], run_dir: Path | None = None) -> None:
    """End each job whose remote side is dead (detach.remote_verdict), its verdict noted first; then
    refresh the mirrored exit code and log tail of the run's `--remote` jobs (detach.mirror_refresh)."""
    from . import detach
    for rc in jobs:
        why = detach.remote_verdict(Path(rc), memo.setdefault(rc, {}))
        if why:
            verdicts.append(f"{Path(rc).stem}: {why}")
            detach.end_remote(Path(rc), why)
    if run_dir is not None:
        detach.mirror_refresh(run_dir)


def watch(run_dir: Path, fds: list[int]) -> int:
    """The keeper: hold the inherited slots until every job ended or a later run takes them over."""
    run_dir = Path(run_dir)
    lock_fd, slot_fds = fds[0], fds[1:]
    memo: dict = {}
    verdicts: list[str] = []
    probe, next_probe = None, time.time() + poll_s(REMOTE_PROBE_S)
    beat = 0.0
    while True:
        rec = read(run_dir)
        if rec is None or rec.get("state") != "holding":
            why = None
        elif (run_dir / HANDOVER).exists():
            nxt = (run_dir / HANDOVER).read_text().strip()
            why, state = f"released: handed over to run {nxt or '?'} of the same task", "handed_over"
        elif not live(rec.get("jobs") or []):
            why, state = "released: every detached job of the run ended", "released"
            if verdicts:
                why += f" ({'; '.join(verdicts)})"
        else:
            if probe is not None and not probe.is_alive():
                probe, next_probe = None, time.time() + poll_s(REMOTE_PROBE_S)
            if probe is None and time.time() >= next_probe:
                probe = threading.Thread(target=_remote_round, args=(live(rec.get("jobs") or []), memo, verdicts, run_dir),
                                         daemon=True)
                probe.start()
            if time.time() - beat >= BEAT_EVERY_S:
                try:
                    (run_dir / BEAT).touch()
                except OSError:
                    pass
                beat = time.time()
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
                        for s in slots:
                            locks.label_pid(s, rec["keeper"])
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

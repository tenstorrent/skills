# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One task's time, step by step: `ttp timeline <project> <task>`, read-only and model-free.

Where a task's wall time went, from records the harness already keeps: the task row (created,
status), its runs (dispatch, worker start, end), each run folder's job registry and job files
(`detached.json`, `<name>.started`, `<name>.rc`, `<name>.queued`, `exit.json`, `result.json`) and
its `task_waiting` events. A wait runs from a run's waiting hand-off to the run that woke it (or to
now, while it still waits). A wait on `ttp detach --check --host` stands for the remote job it
polls: that job's files live on another host, so the wait's own span is the job's local record.

The wall time is painted interval by interval into buckets: queue (created to the first dispatch),
start-up (a run's dispatch to its first detached job), test (detached jobs running), wait (idle
between steps, probe and wake gaps) and model (a run with no detached job); what the records do
not cover is `unexplained`. `failed and redone` sums the attempts of jobs that ended nonzero and
were run again; that time is inside test, not extra.

Accepted gaps, kept as-is: time after a run with no waiting hand-off and no recorded interval is
`unexplained`, and a start_when deferral before the first run counts as queue.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import db, detach, landings, runcap

EPS = 1e-6
# Earlier wins where intervals overlap: a running job outranks the idle run around it.
RANK = {"test": 0, "start-up": 1, "model": 2, "wait": 3}


def _mtime_s(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _job(entry: dict) -> dict | None:
    """One registry entry as a timeline job: start and end from the job's own files, which the
    wrapper stamps as it runs, falling back to the registry's request time; rc from the .rc file."""
    if not entry.get("name") or not entry.get("rc"):
        return None
    rc = Path(entry["rc"])
    end = _mtime_s(rc)
    code = None
    if end is not None:
        try:
            code = rc.read_text().strip() or "?"
        except OSError:
            code = "?"
    start = _mtime_s(detach.started(rc))
    if start is None:
        start = entry.get("started")
    state = "ended" if end is not None else "lost" if detach.lost(rc).exists() \
        else "queued" if detach.queued(rc).exists() else "running"
    return {"name": entry["name"], "start": start, "end": end, "rc": code, "state": state}


def jobs(run_dir: Path) -> list[dict]:
    """The run's detached jobs from its registry, in start order."""
    try:
        reg = json.loads((run_dir / "detached.json").read_text())
    except (OSError, ValueError):
        return []
    got = [_job(e) for e in reg if isinstance(e, dict)] if isinstance(reg, list) else []
    return sorted((j for j in got if j), key=lambda j: j["start"] or 0.0)


def remote_waits(probe: str) -> list[dict]:
    """The remote jobs a `detach --check --host <alias> [--mirror <dir>] <path>...` probe polls, one
    {"host", "name", "mirror", "path"} per path; [] for any other probe. The probe is the job's local
    record: its mirror folder holds the job's exit code and log tail (detach.mirror_save)."""
    host, mirror, paths = detach.check_args(probe)
    return [{"host": host, "mirror": mirror, "path": path, "name": detach.mirror_name(path)}
            for path in paths] if host else []


def _mirror_job(p, info: dict, start: float, wake: float) -> dict:
    """A remote job's step from its local mirror record: the exit code and end its mirror kept, and
    the wait's span while it ran. A job without a mirror (an older record, or the wait spans it)
    is timed by the wait alone."""
    d = Path(info["mirror"] or ".")
    if not d.is_absolute():   # the probe's cwd is the project root, so a relative mirror is too
        d = p.root / d
    code, end = None, wake
    m = detach.mirror_read(d, info["name"], info["host"], info["path"])
    if m and isinstance(m.get("end"), dict) and "rc" in m["end"]:   # no mirror, or another job's: the wait's span
        code = str(m["end"]["rc"])
        end = _mtime_s(d / f"{info['name']}.remote.rc") or wake
    return {"name": info["name"], "start": start, "end": end, "rc": code, "state": "remote",
            "host": info["host"]}


def _wait_start(conn, tid: int, run: dict, handoff: dict) -> float:
    """When the run's waiting hand-off started: its `waiting_since`, else its own `task_waiting`
    event, else the run's end. An earlier run's event never answers for this one."""
    since = handoff.get("waiting_since")
    if isinstance(since, (int, float)):
        return float(since)
    row = conn.one("SELECT ts FROM events WHERE task=? AND kind='task_waiting' AND ts BETWEEN ? AND ?"
                 " ORDER BY ts DESC", (tid, run["start"], run["end"] + 60))
    return float(row["ts"]) if row and row["ts"] else run["end"]


def task(p, row: dict, now: float | None = None) -> dict:
    """The task's timeline: its steps in order, and its wall time painted by bucket."""
    now = time.time() if now is None else now
    tid, created = row["id"], float(row["created"] or now)
    closed = row["status"] in ("done", "failed", "cancelled", "superseded")
    rows = p.db.q("SELECT id, started, ended, status, dir FROM runs WHERE task=? AND started IS NOT NULL"
                  " AND role!='coordinator' ORDER BY started", (tid,))
    runs = []
    for r in rows:
        folder = Path(r["dir"] or (p.runs / str(r["id"])))
        try:
            exit_j = json.loads((folder / "exit.json").read_text())
        except (OSError, ValueError):
            exit_j = {}
        runs.append({"start": float(r["started"]), "end": float(r["ended"] or now),
                     "status": r["status"], "jobs": jobs(folder),
                     "handoff": runcap.handoff({"dir": str(folder)}), "worker_start": exit_j.get("started")})
    ends = [r["end"] for r in runs] + [j["end"] for r in runs for j in r["jobs"] if j["end"]]
    if not closed:
        ends.append(now)   # an open task's spans run to now
    end = max(ends + [created])

    waiting = db.waiting_handoff(row)   # the daemon's record of a task asleep on its retry_when
    steps, ivs, attempts = [], [], []
    steps.append({"kind": "created", "at": created})
    queued = row["status"] == "queued"
    if runs:
        steps.append({"kind": "queued", "start": created, "end": runs[0]["start"]})
    elif queued:
        steps.append({"kind": "queued", "start": created, "end": None})
    for i, r in enumerate(runs):
        last = i + 1 == len(runs)
        steps.append({"kind": "run", "label": f"run {i + 1}", "start": r["start"],
                      "worker_start": r["worker_start"], "end": r["end"], "status": r["status"]})
        busy = None   # latest job end so far: a later start past it is idle time between jobs
        for j in r["jobs"]:
            if busy and j["start"] and j["start"] > busy:
                steps.append({"kind": "idle", "start": busy, "end": j["start"]})
            busy = max(busy or 0.0, j["end"] or 0.0) or busy
            steps.append({"kind": "job", "name": j["name"], "start": j["start"], "end": j["end"],
                          "rc": j["rc"], "state": j["state"], "host": None})
            if j["start"] and j["end"] and j["end"] > j["start"]:
                ivs.append((j["start"], j["end"], "test"))
            attempts.append((j["start"], j["end"], j["rc"], j["name"]))
        starts = [j["start"] for j in r["jobs"] if j["start"]]
        if starts and r["start"] < min(starts):   # dispatch to the first detached job
            ivs.append((r["start"], min(min(starts), r["end"]), "start-up"))
        ivs.append((r["start"], r["end"], "wait" if r["jobs"] else "model"))   # idle between and after jobs
        if r["handoff"].get("status") == "waiting":
            start = _wait_start(p.db, tid, r, r["handoff"])
            probe = str(r["handoff"].get("retry_when") or "")
            wake = runs[i + 1]["start"] if i + 1 < len(runs) else end
            still_open = last and (waiting or row["status"] == "blocked")
            steps.append({"kind": "wait", "start": start, "end": None if still_open else wake,
                          "probe": probe, "waiting_for": r["handoff"].get("waiting_for")})
            ivs.append((start, wake, "wait"))
            for info in remote_waits(probe):   # each polled job's own record, when its mirror kept one
                j = _mirror_job(p, info, start, wake)
                steps.append({"kind": "job", **j})
                attempts.append((j["start"], j["end"], j["rc"], j["name"]))
                if j["end"] > j["start"]:
                    ivs.append((j["start"], j["end"], "test"))
    buckets: dict[str, float] = {}
    for a, b, c in landings.paint(ivs, created, end, runs[0]["start"] if runs else None, RANK):
        buckets[c] = buckets.get(c, 0.0) + (b - a)
    unexplained = buckets.pop("unknown", 0.0)
    buckets = {k: round(v, 3) for k, v in buckets.items()}
    return {"task": tid, "title": row["title"], "status": row["status"], "created": created,
            "end": end, "wall_s": round(max(end - created, 0.0), 3), "steps": steps,
            "buckets": buckets, "unexplained_s": round(unexplained, 3), **failed_redone(attempts)}


def failed_redone(attempts: list[tuple[float | None, float | None, str, str]]) -> dict:
    """Seconds on attempts that ended nonzero and were run again (a job of the same task started
    at or after their end under the same name). That time sits inside test; it is waste, not extra wall time."""
    wasted, names = 0.0, []
    for i, (s, e, rc, name) in enumerate(attempts):
        if rc in (None, "", "0") or not s or not e or e <= s:
            continue
        if any(a0 is not None and a0 >= e - EPS and n == name for j, (a0, _e, _rc, n) in enumerate(attempts) if j != i):
            wasted += e - s
            names.append(name)
    return {"failed_redone_s": round(wasted, 3), "failed_redone_jobs": names}


def timeline_text(rep: dict, p) -> str:
    """The `ttp timeline` listing, in the project's home zone."""
    from . import timefmt
    span = lambda s: timefmt.dur(s["end"] - s["start"]) if isinstance(s.get("start"), (int, float)) \
        and isinstance(s.get("end"), (int, float)) else ""
    clock = lambda t: timefmt.short(t, p) if isinstance(t, (int, float)) else "unknown"
    out = [f"task #{rep['task']} {rep['title']}",
           f"status: {rep['status']}   created {timefmt.long(rep['created'], p)}"]
    for s in rep["steps"]:
        kind = s["kind"]
        if kind == "created":
            out.append(f"created {timefmt.long(s['at'], p)}")
        elif kind == "queued":
            out.append(f"queued {span(s)} (to run 1's dispatch)" if s["end"] is not None else "still queued")
        elif kind == "run":
            out.append(f"{s['label']}: dispatched {clock(s['start'])}, worker start "
                       f"{clock(s['worker_start'])}, ended {clock(s['end'])} ({span(s)}, {s['status']})")
        elif kind == "job":
            note = {"remote": f", remote on {s['host']}", "queued": ", queued",
                    "lost": ", lost", "running": ", running"}.get(s["state"], "")
            out.append(f"  job {s['name']}: {clock(s['start'])} → {clock(s['end'])} ({span(s)}, "
                       f"rc={s['rc']}{note})")
        elif kind == "idle":
            out.append(f"  idle between jobs {span(s)}")
        elif kind == "wait":
            woke = "still waiting" if s["end"] is None else clock(s["end"])
            dur = timefmt.dur((s["end"] or rep["end"]) - s["start"])
            what = f" ({s['waiting_for']})" if s.get("waiting_for") else ""
            out.append(f"wait {clock(s['start'])} → {woke} ({dur}) on: {s['probe']}{what}")
    parts = ", ".join(f"{k} {timefmt.dur(v)}" for k, v in rep["buckets"].items()) or "none explained"
    out.append("buckets: " + parts
               + (f"; unexplained {timefmt.dur(rep['unexplained_s'])}" if rep["unexplained_s"] > 0 else ""))
    if rep["failed_redone_s"]:
        out.append(f"failed and redone {timefmt.dur(rep['failed_redone_s'])} "
                   f"(inside test: {', '.join(rep['failed_redone_jobs'])})")
    return "\n".join(out)

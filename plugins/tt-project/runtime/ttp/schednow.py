# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp schedules <project> run <schedule>`: run one command schedule now, through the daemon.

A worker sometimes needs a command schedule's result now, and its sandbox cannot run the command
itself (it denies the secrets store the command's tool reads). So the command never runs in the caller:
it queues a request under state/schedule-now (as `ttp detach --stop` does, jobstop.request), and the
daemon's schedule step (Daemon.run_schedules, serve) starts that schedule as if it were due, with the
same runner, environment, timeout, heal and observation handling, and records the run (last_run) the
same way. Only a schedule already in the project's config runs, and only a command schedule: the
request names a schedule, never a command.

Files, per request id:
- <id>.json  the request ({schedule, caller, at}); the caller removes it to withdraw it while unclaimed.
- <id>.run   the daemon claimed it (an atomic rename of <id>.json) and wrote {timeout}: how long the caller waits.
- <id>.out   the result: {rc, stdout, status}. rc is the command's exit code, None when none ran
             (a heal check only, a timeout). The caller reads it and removes all three.
A run already going for that schedule is joined, never started twice. Files left by a caller that
died are removed after KEEP_S.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .project import Project, write_json

QUEUE = "schedule-now"
PICKUP_S = 120       # the daemon claims a request within this long, else the caller says it is not serving
SLACK_S = 60         # the caller waits this long past the run's own timeout for its result
KEEP_S = 3600        # request and result files older than this are swept (their caller is gone)
POLL_S = 0.5


def refusal(row: dict | None, name: str) -> str | None:
    """Why schedule `name` (its row, None when unknown) cannot run now, or None when it can."""
    if row is None:
        return f"no schedule {name!r} in this project's config"
    if row["kind"] != "command":
        return f"schedule {name!r} is a {row['kind']} schedule; only command schedules run now"
    return None


def folder(p: Project) -> Path:
    return p.state / QUEUE


def request(p: Project, name: str, caller: str | None) -> str:
    """Queue a run of schedule `name` for the daemon; returns the request id."""
    rid = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    d = folder(p)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{rid}.tmp"
    write_json(tmp, {"schedule": name, "caller": caller or "", "at": time.time()})
    tmp.rename(d / f"{rid}.json")   # whole or absent: the daemon never reads half a request
    return rid


def _gone(*paths: Path) -> None:
    for f in paths:
        f.unlink(missing_ok=True)


def wait(p: Project, rid: str, pickup_s: float | None = None, slack_s: float | None = None,
         poll_s: float | None = None) -> dict:
    """The result of request `rid`: {rc, stdout, status}; {"error": why} when the daemon did not claim it
    within pickup_s (the request is withdrawn) or gave no result within its run's timeout plus slack_s."""
    pickup_s = PICKUP_S if pickup_s is None else pickup_s
    slack_s = SLACK_S if slack_s is None else slack_s
    poll_s = POLL_S if poll_s is None else poll_s
    d = folder(p)
    req, run, out = d / f"{rid}.json", d / f"{rid}.run", d / f"{rid}.out"
    end = time.monotonic() + pickup_s
    while req.exists() and not run.exists():
        if time.monotonic() >= end:
            try:
                req.unlink()   # withdrawn; a claim that won the race (rename) leaves no request to unlink
                return {"error": f"the daemon did not take the request within {pickup_s:.0f} s; is it running?"}
            except FileNotFoundError:
                break
        time.sleep(poll_s)
    begun, timeout = time.monotonic(), 0.0
    while not out.exists():
        if not timeout:   # the daemon writes it just after the claim
            try:
                timeout = float(json.loads(run.read_text()).get("timeout") or 0)
            except (OSError, ValueError, AttributeError):
                pass
        if time.monotonic() >= begun + timeout + slack_s:
            _gone(run)
            return {"error": f"no result within {timeout + slack_s:.0f} s"}
        time.sleep(poll_s)
    try:
        res = json.loads(out.read_text())
    except (OSError, ValueError):
        res = {"error": "its result file could not be read"}
    _gone(req, run, out)
    return res if isinstance(res, dict) else {"error": "its result file holds no result"}


def pending(p: Project, now: float | None = None) -> list[tuple[str, str]]:
    """Claim each queued request: (id, schedule name). Sweeps files whose caller is gone."""
    d, now = folder(p), now or time.time()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.iterdir()):
        try:
            if now - f.stat().st_mtime > KEEP_S:
                f.unlink(missing_ok=True)
                continue
        except OSError:
            continue
        if f.suffix != ".json" or f.name.startswith("."):
            continue
        rid, run = f.stem, d / f"{f.stem}.run"
        try:
            m = json.loads(f.read_text())
            f.rename(run)   # claimed: the caller can no longer withdraw it
        except (OSError, ValueError):
            continue
        name = m.get("schedule") if isinstance(m, dict) else None
        out.append((rid, name if isinstance(name, str) else ""))
    return out


def started(p: Project, rid: str, timeout: float) -> None:
    write_json(folder(p) / f"{rid}.run", {"timeout": timeout})


def answer(p: Project, rid: str, result: dict[str, Any]) -> None:
    """Write request `rid`'s result, unless its caller already gave up on it (no .run left)."""
    d = folder(p)
    if (d / f"{rid}.run").exists():
        write_json(d / f"{rid}.out", result)

"""The evidence an llm schedule's precheck admitted a run on, bound to that run.

A precheck that exits nonzero (go ahead) may print one JSON object as its last stdout line:
`snapshot` (an absolute path) and `captured_at` (ISO time or epoch) are the keys the harness
names; any others are kept as they are. The object is stored with the task in the transaction
that queues it and handed to the run unchanged: a file named by $TTP_PRECHECK_JSON and one line
in its spec. Nothing later (another precheck, a moved latest pointer, a feed refresh) replaces
it. Output that is missing, not a JSON object or over MAX_BYTES, and a precheck that failed or
timed out, bind `{"binding": "unknown"}`: the run is never handed another snapshot instead.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import project

KEY = "precheck_binding:"   # + task id
MAX_BYTES = 4096
ENV = "TTP_PRECHECK_JSON"
KEEP_S = 30 * 86400


def unknown(why: str) -> dict:
    return {"binding": "unknown", "why": why}


def parse(stdout: str | None) -> dict:
    """The object on the precheck's last non-empty stdout line, or an unknown binding saying why not."""
    last = next((x.strip() for x in reversed((stdout or "").splitlines()) if x.strip()), "")
    if not last:
        return unknown("no output")
    if len(last.encode()) > MAX_BYTES:
        return unknown(f"last line over {MAX_BYTES} bytes")
    try:
        obj = json.loads(last)
    except ValueError:
        return unknown("last line is not JSON")
    if not isinstance(obj, dict):
        return unknown("last line is not a JSON object")
    snap = obj.get("snapshot")
    if snap is not None and not (isinstance(snap, str) and os.path.isabs(snap)):
        return unknown("snapshot is not an absolute path")
    return obj


def line(bound: dict | None) -> str:
    """The spec's context line for `bound`: "" when no precheck ran."""
    if bound is None:
        return ""
    return (f"\n\nPrecheck output at admission (the evidence this run was queued on; use it, not a newer "
            f"snapshot; also in ${ENV}): {json.dumps(bound, sort_keys=True)}")


def store(db, task_id: int, bound: dict | None) -> None:
    """Bind `bound` to task `task_id`, inside the caller's transaction; drops bindings past KEEP_S."""
    if bound is None:
        return
    db.x("DELETE FROM kv WHERE key LIKE ? AND ts<?", (KEY + "%", time.time() - KEEP_S))
    db.set_kv(f"{KEY}{task_id}", bound)


def hand_over(db, task_id, run_dir) -> dict:
    """Write task `task_id`'s binding into its run folder: {ENV: path}, or {} when it has none."""
    bound = db.kv(f"{KEY}{task_id}", None) if task_id else None
    if bound is None:
        return {}
    path = Path(run_dir) / "precheck.json"
    project.durable_write(path, json.dumps(bound, sort_keys=True) + "\n")
    return {ENV: str(path)}

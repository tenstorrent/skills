# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Live check that a running worker read its coordinator update (Daemon.check_steer_reads, each tick).

An update to a running task is appended to its run's steer.md (coordinator._append_update). The
worker reads it through its provider's hook (Claude Code, Codex where the hook runs) or from `ttp note`
and `ttp steer` (any provider); each marks steer.offset read up to the end and records the time in
steer.reads (hook.mark_seen). Here, a running worker run whose steer.md holds text past its
steer.offset for longer than `steer.ack_after_s` (default 300; 0: off) raises one `steer_unread`
event to the coordinator, model-free, naming the task and the update. The clock starts at steer.md's
last change when the daemon first sees the unread text (a later start, never an earlier one). Once
read, a still-queued event is marked handled. A run that ended raises nothing: Daemon._missed_update
stays the end-of-run check."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .hook import OFFSET_FILE, STEER_FILE, unread_update

KIND = "steer_unread"
ACK_AFTER_S = 300
TEXT_MAX = 600


def ack_after_s(cfg: dict) -> float:
    try:
        return float(((cfg or {}).get("steer") or {}).get("ack_after_s", ACK_AFTER_S) or 0)
    except (TypeError, ValueError, AttributeError):
        return ACK_AFTER_S


def _seen(run_dir: Path) -> int:
    try:
        return int((run_dir / OFFSET_FILE).read_text())
    except (OSError, ValueError):
        return 0


def check(db, cfg: dict, runs: list[dict], state: dict, now: float | None = None) -> list[int]:
    """Raise a steer_unread event for each running worker run in `runs` past the delay; `state` (the
    daemon's, by run id) keeps when each unread stretch was first seen. Returns the new event ids."""
    now = time.time() if now is None else now
    after = ack_after_s(cfg)
    live = {r["id"] for r in runs}
    for rid in [k for k in state if k not in live]:
        del state[rid]
    if after <= 0:
        state.clear()
        return []
    raised = []
    for r in runs:
        if r.get("role") == "coordinator" or not r.get("task") or not r.get("dir"):
            continue
        run_dir = Path(r["dir"])
        try:
            st = (run_dir / STEER_FILE).stat()
        except OSError:
            state.pop(r["id"], None)
            continue
        seen = _seen(run_dir)
        text = unread_update(run_dir)[0] if st.st_size > seen else ""
        if not text:
            if state.pop(r["id"], None):
                db.x("UPDATE events SET status='handled' WHERE kind=? AND fingerprint LIKE ? AND status='queued'",
                     (KIND, f"{KIND}:{r['id']}:%"))
            continue
        entry = state.get(r["id"])
        if not entry or entry["seen"] != seen:
            entry = state[r["id"]] = {"seen": seen, "since": min(st.st_mtime, now)}
        if now - entry["since"] < after:
            continue
        fp = f"{KIND}:{r['id']}:{seen}"
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            continue
        task = db.task(r["task"]) or {}
        body = text if len(text) <= TEXT_MAX else text[:TEXT_MAX] + " …"
        msg = (f"#{r['task']} {(task.get('title') or '')[:120]}: its running worker (run {r['id']}, "
               f"{r.get('provider') or 'unknown provider'}) has not read the update to its task for "
               f"{(now - entry['since']) / 60:.0f} min. The update: {body} -- Decide: keep waiting for its "
               f"next tool call or `ttp note`, or cancel and requeue the task with the change in its spec.")
        raised.append(db.x(
            "INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
            (now, "daemon", KIND, fp, "normal", msg,
             json.dumps({"run": r["id"], "task": r["task"], "offset": seen, "size": st.st_size,
                         "unread_s": round(now - entry["since"])}), "queued", r["task"])))
    return raised

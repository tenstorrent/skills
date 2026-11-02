# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who set each project config key: the setter's role, never a person's name.

Project.set_config records one entry per dotted key in kv KEY: {"by": role, "ts": when, and where it
applies "msgs" (the user's message ids), "task" (a worker's task id), "run" and "via" (the path:
cli, chat, web, grant, upgrade...)}. Roles: user, coordinator, worker, harness (a model-free check
of the daemon), default (a value the harness fills in) and unknown (a caller that names none). A key
set before this record existed has none and reads as unknown. Setting a section replaces the records
of the keys under it; a key with no record of its own takes its nearest section's.

`ttp config get --why KEY` prints it; selfblock.py checks a worker's 'this setting is the user's'
against it.
"""
from __future__ import annotations

import os
import re
import time

KEY = "config_provenance"
ROLES = ("user", "coordinator", "worker", "harness", "default", "unknown")


def by(role: str, msgs=(), task=None, run=None, via: str = "") -> dict:
    """A setter record (without its timestamp) for set_config's `by`."""
    out: dict = {"by": role if role in ROLES else "unknown"}
    ids = [int(m) for m in msgs or () if str(m).strip().isdigit()]
    if ids:
        out["msgs"] = ids
    if task not in (None, ""):
        out["task"] = int(task) if str(task).isdigit() else str(task)
    if run not in (None, ""):
        out["run"] = str(run)
    if via:
        out["via"] = str(via)[:80]
    return out


def msg_ids(text: str) -> list[int]:
    """The message ids a grant names ("user message #12, #14")."""
    return [int(m) for m in re.findall(r"#(\d+)", text or "")]


def from_env(env=None) -> dict:
    """The setter of a `ttp config` command: a worker run (its task id), a coordinator run (a run with
    no task), else whoever typed it at a terminal: the user."""
    from .project import in_run
    env = os.environ if env is None else env
    if in_run(env):
        task = str(env.get("TTP_TASK_ID") or env.get("TTP_TASK") or "").strip()
        return by("worker" if task else "coordinator", task=task or None, run=env.get("TTP_RUN_ID"), via="cli")
    return by("user", via="cli")


def _all(db) -> dict:
    got, _ = db.kv_dict(KEY)
    return got


def record(db, key: str, setter: dict | None, now: float | None = None) -> None:
    """Keep `setter` as who set `key` (and everything under it). One short transaction."""
    rec = dict(setter or by("unknown"))
    rec["ts"] = time.time() if now is None else now
    with db.tx():
        allrec = {k: v for k, v in _all(db).items() if k != key and not k.startswith(key + ".")}
        allrec[key] = rec
        db.set_kv(KEY, allrec)


def get(db, key: str) -> dict | None:
    """The record of who set `key`, or of its nearest section; None when there is none (unknown)."""
    allrec = _all(db)
    parts = key.split(".")
    for i in range(len(parts), 0, -1):
        rec = allrec.get(".".join(parts[:i]))
        if isinstance(rec, dict):
            return rec
    return None


def role(rec: dict | None) -> str:
    r = (rec or {}).get("by")
    return r if r in ROLES else "unknown"


def describe(rec: dict | None, where=None) -> str:
    """One line: 'user (message #12) via chat, 2026-11-02 09:30 PST'; 'unknown' with no record."""
    if not rec:
        return "unknown (set before setters were recorded, or by hand in project.json)"
    who = role(rec)
    if rec.get("msgs"):
        who += " (message " + ", ".join(f"#{m}" for m in rec["msgs"]) + ")"
    if rec.get("task") is not None:
        who += f" (task #{rec['task']})"
    if rec.get("via"):
        who += f" via {rec['via']}"
    try:
        from . import timefmt
        when = timefmt.long(float(rec.get("ts") or 0), where)   # where None: UTC
    except Exception:
        when = ""
    return f"{who}, {when}" if when else who

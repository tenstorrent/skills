# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who set each project config key: the setter's role, never a person's name.

Project.set_config records one entry per dotted key in kv KEY: {"by": role, "ts": when, and where it
applies "msgs" (the user's message ids), "task" (a worker's task id), "run" and "via" (the path:
cli, chat, web, grant, upgrade...)}. Roles: user, coordinator, worker, harness (a model-free check
of the daemon), default (a value the harness fills in) and unknown (a caller that names none). A key
set before this record existed has none and reads as unknown. Setting a section replaces the records
of the keys under it; a key with no record of its own takes its nearest section's.

Each record also keeps "h", a short digest of the value the recorded set wrote (of the whole section for
a section). lookup() reads a record against project.json as it is now: a key absent from it with no
record of its own was never set (default); a value that no longer matches its digest was changed
outside set_config (a hand edit) and reads as unknown, never as the old setter. A recorded set under a
section moves the section's digest along only when the section still matched it before the write.

`ttp config get --why KEY` prints it; selfblock.py checks a worker's 'this setting is the user's'
against it.
"""
from __future__ import annotations

import hashlib
import json
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


_ABSENT = object()


def at(raw, key: str):
    """The value of dotted `key` in project.json `raw`; _ABSENT when it is not there."""
    node = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _ABSENT
        node = node[part]
    return node


def digest(value) -> str:
    """A short digest of a value as set ('-' for a key not there)."""
    if value is _ABSENT:
        return "-"
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def record(db, key: str, setter: dict | None, now: float | None = None, before=None, after=None) -> None:
    """Keep `setter` as who set `key` (and everything under it). One short transaction. With `after`
    (project.json once written) it keeps the digest of what the set wrote; with `before` too, each
    section record above `key` follows the write only if it still matched before it (else that
    section was edited by hand, and stays a mismatch)."""
    rec = dict(setter or by("unknown"))
    rec["ts"] = time.time() if now is None else now
    if after is not None:
        rec["h"] = digest(at(after, key))
    with db.tx():
        allrec = {k: v for k, v in _all(db).items() if k != key and not k.startswith(key + ".")}
        if after is not None:
            for k, v in allrec.items():
                if isinstance(v, dict) and "h" in v and key.startswith(k + "."):
                    kept = before is not None and v["h"] == digest(at(before, k))
                    allrec[k] = {**v, "h": digest(at(after, k)) if kept else "edited"}
        allrec[key] = rec
        db.set_kv(KEY, allrec)


def _nearest(db, key: str) -> tuple[str, dict | None]:
    allrec = _all(db)
    parts = key.split(".")
    for i in range(len(parts), 0, -1):
        rec = allrec.get(".".join(parts[:i]))
        if isinstance(rec, dict):
            return ".".join(parts[:i]), rec
    return "", None


def get(db, key: str) -> dict | None:
    """The record of who set `key`, or of its nearest section; None when there is none (unknown).
    The record only: lookup() reads it against project.json as it is now."""
    return _nearest(db, key)[1]


def lookup(db, key: str, raw: dict | None) -> dict | None:
    """Who set the value `key` has now in project.json `raw`: its record, or a stand-in record
    ({"by": role, "why": ...}): default when it is not in project.json and has no record of its own
    (never set; unknown when the account settings set it), unknown when the value changed after its
    recorded set. None (unknown) for a value with no record. `raw` None: the record as kept."""
    name, rec = _nearest(db, key) if db is not None else ("", None)
    if raw is None:
        return rec
    if at(raw, key) is _ABSENT:
        if name == key and rec is not None:
            if rec.get("h") == "-":     # a recorded removal: the default applies, as that setter left it
                return rec
            return {"by": "unknown", "why": "removed from project.json after its last recorded set"}
        from .project import load_account_settings
        if at(load_account_settings(), key) is not _ABSENT:
            return {"by": "unknown", "why": "not set in this project: the account settings set it"}
        return {"by": "default", "why": "never set"}
    if rec is not None and "h" in rec and rec["h"] != digest(at(raw, name)):
        return {"by": "unknown", "why": "changed in project.json after its last recorded set"}
    return rec


def role(rec: dict | None) -> str:
    r = (rec or {}).get("by")
    return r if r in ROLES else "unknown"


def describe(rec: dict | None, where=None) -> str:
    """One line: 'user (message #12) via chat, 2026-11-02 09:30 PST'; 'unknown' with no record."""
    if not rec:
        return "unknown (set before setters were recorded, or by hand in project.json)"
    if rec.get("why"):
        return f"{role(rec)} ({rec['why']})"
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

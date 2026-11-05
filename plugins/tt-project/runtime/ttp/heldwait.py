# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Waits held elsewhere: no coordinator turn here can move them, so the 'repeated waits' effort trigger
(coordinator.effort_triggers) neither counts them nor raises for them, and its seen-state for them is left
as it was.

A waiting task's wait is held elsewhere (why()) while:
- the user holds it: its hand-off's wait kind is a user step (unblock.USER_WAIT_KINDS), its retry_when is a
  user-pause probe (userpause.is_probe), or it waits on a resource (a `ttp lock --probe` resource or one its
  labels name) the user paused;
- a peer project holds it: its reply-wait probe's peer is recovering (ackwait.peer_recovering), or it is
  listed in the project's own PEERS_FILE (a list of project names, or an object keyed by them, or
  {"peers": [...]}) and named as a whole word by the wait's probe, owner or `waiting_for`;
- another project's known outage holds it: a machine its retry_when reaches (its ssh target or `--host`) or
  its lock resource names (the alias, or `<alias>-...`/`<alias>:...`) has an open known outage
  (`ttp machines outage`) and no entry of it was recorded by this project.

Every other wait still counts and raises as before. Unreadable state never silences the trigger."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import ackwait, unblock, userpause
from .db import load_result

PEERS_FILE = "peer_recovery.json"   # in the project's harness dir: peers whose own recovery holds waits on them


def peers(harness: Any) -> set[str]:
    """The project names PEERS_FILE lists (empty when there is none or it cannot be read)."""
    if not harness:
        return set()
    try:
        doc = json.loads((Path(harness) / PEERS_FILE).read_text())
    except (OSError, ValueError):
        return set()
    if isinstance(doc, dict):
        doc = doc.get("peers", list(doc))
    return {str(n).strip() for n in doc if str(n).strip()} if isinstance(doc, list) else set()


def state(db: Any, now: float | None = None) -> tuple[dict, dict]:
    """(paused resources, machine_ledger.outage_entries) for why(), read once; {} for either that cannot be read."""
    from . import machine_ledger
    try:
        paused = db.paused_resources() or {}
    except Exception:
        paused = {}
    try:
        outages = machine_ledger.outage_entries(time.time() if now is None else now)
    except Exception:   # an unreadable ledger never silences the trigger
        outages = {}
    return paused, outages


def _names(name: str, alias: str) -> bool:
    return name == alias or name.startswith((alias + "-", alias + ":"))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[\w.-]+", text or ""))


def why(db: Any, cfg: dict, task: dict | None, harness: Any = None, now: float | None = None,
        paused: dict | None = None, outages: dict | None = None) -> str:
    """Why `task`'s current wait is held elsewhere (module doc), or '' when a turn may move it.
    `paused`, `outages`: the paused resources and machine_ledger.outage_entries, read once by a caller
    checking many tasks."""
    if not task or task.get("status") in ("done", "failed", "cancelled"):
        return ""
    r = load_result(task.get("result"))
    if r.get("status") not in (None, "waiting"):
        return ""
    probe = str(r.get("retry_when") or "")
    if unblock.classify_wait(r)[0] == "user":
        return "a user step"
    if probe and userpause.is_probe(probe, cfg):
        return "a user pause"
    locked = (unblock.wait_probes(probe) or ([], []))[1]
    from .coordinator import task_resources
    res = set(locked) | task_resources(task)
    try:
        paused = db.paused_resources() if paused is None else paused
    except Exception:
        paused = {}
    for name in sorted(res):
        row = paused.get(name)
        if isinstance(row, dict) and row.get("by") == "user":
            return f"resource {name} paused by the user"
    peer, held = ackwait.peer_recovering(probe, cfg, harness)
    if peer:
        return f"peer {peer}: {held}"
    listed = peers(harness)
    if listed:
        said = {ackwait.peer(probe, cfg, harness)} | _words(probe) | _words(unblock.wait_owner(r)) \
            | _words(str(r.get("waiting_for") or ""))
        hit = sorted(listed & said)
        if hit:
            return f"peer {hit[0]} (listed in {PEERS_FILE})"
    return _outage(cfg, probe, locked, now, outages)


def _outage(cfg: dict, probe: str, locked: list[str], now: float | None, outages: dict | None) -> str:
    """'machine <alias> under <project>'s known outage' when a machine the wait reaches has an open known
    outage recorded only by other projects, else ''."""
    from . import machine_ledger, netwait
    here = str(cfg.get("name") or "")
    try:
        outages = machine_ledger.outage_entries(time.time() if now is None else now) if outages is None else outages
    except Exception:   # an unreadable ledger never silences the trigger
        return ""
    if not outages:
        return ""
    targets = [h.rpartition("@")[2] for h in netwait.hosts(probe)] + list(locked) if probe else list(locked)
    for alias in sorted(outages):
        entries = outages.get(alias) or {}
        if not isinstance(entries, dict) or not entries or here in entries:
            continue
        if any(_names(t, alias) for t in targets):
            return f"machine {alias} under {', '.join(sorted(entries))}'s known outage"
    return ""

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free notes to another local project: the optional `to` of a command schedule's JSON line and
of a heal block.

`to` names a project on this machine (`ttp list`), or OWNER: the project that owns the line's (or
check's) `machine` in `ttp machines` (a known outage's owner when that is a local project, else its
`recovery_owner`, else its `recovery_fallback`). The daemon files the note itself (upstream.send,
sender "daemon"), so no run (TTP_RUN_DIR) is needed and nothing posts as the user. A target that
names this project, or cannot be resolved, is not paged: the caller handles the line as if it had
no `to` (a normal observation or escalation), and logs why for an unknown one.
"""
from __future__ import annotations

from pathlib import Path

OWNER = "@machine-owner"
PER_RUN = 10   # notes one command schedule run files at most; the rest go on its next runs


def local(name: str) -> bool:
    """Whether `name` is a project registered on this machine."""
    from .project import hostname, load_registry
    entry = (load_registry().get("projects") or {}).get(name) or {}
    return bool(entry.get("dir")) and entry.get("host") in (None, hostname()) and Path(entry["dir"]).is_dir()


def resolve(to: object, machine: object = None) -> tuple[str | None, str]:
    """(the local project `to` names, "") or (None, why it names none)."""
    if not isinstance(to, str) or not to.strip():
        return None, "`to` is not a project name"
    to = to.strip()
    if to != OWNER:
        return (to, "") if local(to) else (None, f"no project {to!r} on this machine")
    if not isinstance(machine, str) or not machine.strip():
        return None, f"`to`: {OWNER} needs the machine's alias in `machine`"
    from . import machine_ledger, machines
    alias = machine.strip()
    m = machines.load().get(alias)
    if m is None:
        return None, f"no machine {alias!r} in `ttp machines list`"
    out = (machine_ledger.outages().get(alias) or {}).get("owner")
    for name in (out, m.get("recovery_owner"), m.get("recovery_fallback")):
        if name and local(str(name)):
            return str(name), ""
    return None, f"machine {alias} has no owner project on this machine (`ttp machines set {alias} --owner ...`)"

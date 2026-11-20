# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Failures that are the run's own, not its resource's. Model-free.

A run failed or blocked by one of these says nothing about the machine or device its task names:
- `guard`: a worker-role, provenance or project local guard refused one of its commands (hook.REFUSALS_FILE
  kinds in GUARD_KINDS; a full-suite, no-device or user-site refusal is no local fault);
- `credential`: its hand-off was failed for a credential file change (credwatch, result `cred_changed`);
- `check_bootstrap`: its checks stopped at a runner or env fault (envfault.MARKER) or a lost test worker
  (workerloss.MARKER).
machines.stats counts such a failure apart, as local, for every resource the run never used (onhost.FILE:
no `ttp lock` grant, no `exclusive:` hold, no `ttp detach --remote`, no ssh, scp or rsync there): resource trouble and its migrate
or pause advice count only failures of commands that ran on the resource. Guards stay enforced; only the
count moves."""
from __future__ import annotations

import json
from pathlib import Path

KINDS = {"guard": "guard refusals", "credential": "credential alerts", "check_bootstrap": "check bootstrap faults"}
GUARD_KINDS = ("worker_role", "provenance", "local")   # refusal kinds (and `local:<guard>`) that are guard faults


def guard_kind(kind) -> bool:
    k = str(kind or "")
    return k in GUARD_KINDS or k.startswith("local:")


def _guard_refused(path: Path) -> bool:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            if guard_kind((json.loads(line) or {}).get("kind")):
                return True
        except (ValueError, AttributeError):
            continue
    return False


def kinds(run_dir, result=None) -> list[str]:
    """The local fault kinds a run's directory and hand-off show, in KINDS order."""
    from .envfault import MARKER as ENV
    from .hook import REFUSALS_FILE
    from .workerloss import MARKER as LOSS
    d = Path(run_dir) if run_dir else None
    found = set()
    if isinstance(result, dict) and result.get("cred_changed"):
        found.add("credential")
    if d is not None:
        if _guard_refused(d / REFUSALS_FILE):
            found.add("guard")
        if (d / ENV).exists() or (d / LOSS).exists():
            found.add("check_bootstrap")
    return [k for k in KINDS if k in found]


def used(run_dir) -> set[str]:
    """The resources (canonical lock names) the run held or ran a remote command on."""
    from .onhost import FILE
    try:
        got = json.loads((Path(run_dir) / FILE).read_text()) if run_dir else {}
    except (OSError, ValueError):
        return set()
    return {str(k) for k in got} if isinstance(got, dict) else set()


def tag(run_dir, result, names, cfg: dict | None = None) -> dict:
    """{"local_fault": kinds, "used": the names among `names` the run used}; {} when no local fault."""
    from .locks import canonical
    found = kinds(run_dir, result)
    if not found:
        return {}
    held = used(run_dir)
    held |= {canonical(cfg or {}, h) for h in held}
    return {"local_fault": found, "used": sorted(n for n in names if n in held or canonical(cfg or {}, n) in held)}


def local_for(data, name: str) -> list[str]:
    """The local fault kinds an event's or run's tag counts apart for resource `name`; [] when it counts."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    if not isinstance(data, dict) or name in (data.get("used") or []):
        return []
    return [k for k in data.get("local_fault") or [] if k in KINDS]

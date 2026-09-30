# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slots of a shared resource (a device, a remote build directory) as OS file locks.

Every holder takes one slot file under state/locks: `ttp lock` for one command, the run supervisor
of an `exclusive:<name>` task for its whole run. A lock ends with the process that holds it, so a
crash or a reboot never leaves a resource taken.

An exclusive task that finds every slot held reserves the resource: new `ttp lock` commands wait
until it has its slot, so commands that keep taking the lock in turn cannot starve it. Whoever
reserves refreshes the reservation while it waits; one not refreshed for RESERVE_STALE_S no longer
counts, so a crashed daemon or supervisor never wedges the resource.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

RESERVE_STALE_S = 120


def slot_paths(locks_dir: Path, resource: str, slots: int) -> list[Path]:
    return [Path(locks_dir) / f"{resource}.{i}.lock" for i in range(max(int(slots or 1), 1))]


def try_take(paths: list[Path], holder: str, what: str = ""):
    """The open file of the first free slot, now held and labelled with its holder; None if all
    are taken."""
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            continue
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"holder": holder, "since": time.time(), "command": what[:300]}))
        f.flush()
        return f
    return None


def any_free(paths: list[Path]) -> bool:
    """Whether some slot is free right now. Testing takes the slot for an instant; a `ttp lock`
    that tries in that instant just retries."""
    for path in paths:
        if not path.exists():
            return True
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            return True
    return False


def holders(paths: list[Path]) -> list[str]:
    out = []
    for path in paths:
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if h:
            out.append(f"{h.get('holder')} since {time.strftime('%H:%M', time.localtime(h.get('since', 0)))}")
    return out


def reserve_path(locks_dir: Path, resource: str) -> Path:
    return Path(locks_dir) / f"{resource}.reserved"


def reserve(path: Path, holder: str) -> None:
    """Reserve for holder, or refresh its reservation. A fresh one by another holder stays."""
    other = reserved_by(path)
    if other and other != holder:
        return
    try:
        since = json.loads(path.read_text()).get("since") if other else None
    except (OSError, ValueError):
        since = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"holder": holder, "since": since or time.time(), "ts": time.time()}))
    os.replace(tmp, path)


def reserved_by(path: Path) -> str | None:
    """The holder of a live reservation; None when there is none or it went stale."""
    try:
        r = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return None
    age = time.time() - float(r.get("ts") or 0)
    # A stamp from the future means the clock went back; trusting it would hold the resource
    # until the clock caught up.
    if age > RESERVE_STALE_S or age < -5:
        return None
    return str(r.get("holder") or "") or None


def unreserve(path: Path, holder: str) -> None:
    try:
        if json.loads(path.read_text() or "{}").get("holder") == holder:
            path.unlink()
    except (OSError, ValueError):
        pass

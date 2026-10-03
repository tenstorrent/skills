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

from .project import durable_write

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


def held(locks_dir: Path) -> list[str]:
    """Who holds each resource right now, and who has one reserved: "device: task #3 (run 9) since
    10:02". A slot file keeps its label after release, so only slots whose lock is taken count;
    testing takes a free one for an instant, which a `ttp lock` trying then just retries."""
    out, locks_dir = [], Path(locks_dir)
    try:
        slots = sorted(locks_dir.glob("*.lock"))
        marks = sorted(locks_dir.glob("*.reserved"))
    except OSError:
        return out
    for path in slots:
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    continue
                except OSError:
                    pass
        except OSError:
            continue
        out += [f"{path.name.rsplit('.', 2)[0]}: {h}" for h in holders([path])]
    for path in marks:
        who = reserved_by(path)
        if who:
            out.append(f"{path.name[:-len('.reserved')]}: reserved for {who}")
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
    durable_write(path, json.dumps({"holder": holder, "since": since or time.time(), "ts": time.time()}))


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


WAITS_FILE = "lock_waits.json"


def record_wait(run_dir: Path, key: str, start: float, end: float | None) -> None:
    """Record one `ttp lock` wait of a run: open (end None) while it waits, closed once it ends.
    Several commands of one run may wait at once; the flock keeps their updates from overwriting
    each other, and the rename keeps readers from seeing half a file."""
    run_dir = Path(run_dir)
    try:
        with open(run_dir / f"{WAITS_FILE}.lock", "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                waits = json.loads((run_dir / WAITS_FILE).read_text())
            except (OSError, ValueError):
                waits = {}
            waits[key] = {"start": start, "end": end, "pid": os.getpid()}
            durable_write(run_dir / WAITS_FILE, json.dumps(waits))
    except OSError:
        pass    # a missing record only costs the run its extension, never the command


def waited(run_dir: Path, now: float | None = None) -> float:
    """Seconds the run has spent waiting in `ttp lock`, overlapping waits counted once. A wait still
    going counts up to now; one whose process died without closing it counts nothing."""
    now = time.time() if now is None else now
    try:
        waits = json.loads((Path(run_dir) / WAITS_FILE).read_text())
    except (OSError, ValueError):
        return 0.0
    spans = []
    for w in waits.values():
        try:
            start, end = float(w["start"]), w.get("end")
            if end is None:
                end = now if _alive(int(w.get("pid") or 0)) else start
            spans.append((start, min(float(end), now)))
        except (KeyError, TypeError, ValueError):
            continue
    total, reach = 0.0, float("-inf")
    for start, end in sorted(spans):
        if end > reach:
            total += end - max(start, reach)
            reach = end
    return total


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

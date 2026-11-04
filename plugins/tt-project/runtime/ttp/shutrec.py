# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Signs that the previous boot ended in an orderly shutdown, besides the daemon's own clean-stop
record (daemon.CLEAN_STOP_KEY): best effort, model-free, bounded and never raising.

1. wtmp's shutdown record ('shutdown system down' in `last -x`): the init system writes it on an
   orderly shutdown or reboot, never on a power cut, hard reset or crash. It is read straight from
   the file's last TAIL_RECORDS records, so no tool is needed; a missing or unreadable file, or one
   in another format, says nothing.
2. The daemon's stop-signal record (STOP_FILE in its state folder), written the moment it gets
   SIGTERM or SIGINT (a service manager stopping its units in order at a shutdown). A stop that
   outlasts the unit's stop timeout is killed before it writes the clean-stop record; this one is
   already on disk.

Each counts only from SLACK_S before the previous boot's last heartbeat (the daemon may still tick
while the shutdown begins); a wtmp record also only up to this boot, a stop-signal record only for
the previous boot's id. Tests point TTP_TEST_WTMP at a fixture file, so the
host's own wtmp is never read.
"""
from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path

WTMP = "/var/log/wtmp"
# struct utmp on Linux (glibc, 64-bit and 32-bit alike): ut_type, ut_pid, ut_line, ut_id, ut_user,
# ut_host, ut_exit, ut_session, ut_tv (32-bit seconds and microseconds), ut_addr_v6, unused.
_UTMP = struct.Struct("=h2xi32s4s32s256shhiii4i20s")
RECORD = _UTMP.size   # 384
TAIL_RECORDS = 4096   # about 1.5 MB: a shutdown before the last heartbeat is never further back
RUN_LVL = 1           # ut_type of the shutdown record (ut_user "shutdown")
SLACK_S = 300
STOP_FILE = "stop_signal.json"


def wtmp_path() -> str:
    return os.environ.get("TTP_TEST_WTMP") or WTMP


def wtmp_shutdown(after: float, before: float | None, path: str | None = None) -> float | None:
    """The time of wtmp's latest shutdown record from `after` up to `before` (None: no bound), or None
    when there is none, or the file is missing, unreadable or not in the utmp format."""
    try:
        with open(path or wtmp_path(), "rb") as f:
            size = f.seek(0, os.SEEK_END)
            whole = size - size % RECORD   # a torn last record is skipped
            start = max(0, whole - TAIL_RECORDS * RECORD)
            f.seek(start)
            data = f.read(whole - start)
    except (OSError, ValueError):
        return None
    found = None
    for off in range(0, len(data) - RECORD + 1, RECORD):
        try:
            kind, _pid, _line, _id, user, *_rest = _UTMP.unpack_from(data, off)
            sec = _rest[4]
        except struct.error:
            return None
        if kind != RUN_LVL or not user.startswith(b"shutdown"):
            continue
        if sec >= after and (before is None or sec <= before) and (found is None or sec > found):
            found = float(sec)
    return found


def note_stop_signal(state: Path, boot: str | None, signame: str) -> None:
    """Record that the daemon on boot `boot` was asked to stop by `signame`, now (durable replace,
    so the record outlasts the power-off that may follow). Called from the signal handler: a plain
    file, never the database (the interrupted code may hold a transaction)."""
    from .project import durable_write
    try:
        durable_write(state / STOP_FILE, json.dumps({"boot": boot, "ts": time.time(), "signal": signame}))
    except OSError:
        pass


def stop_signal(state: Path) -> dict:
    try:
        rec = json.loads((state / STOP_FILE).read_text())
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def clear_stop_signal(state: Path) -> None:
    try:
        (state / STOP_FILE).unlink()
    except OSError:
        pass


def orderly_by(state: Path, prev_boot: str, last_heartbeat: float | None, boot_at: float | None) -> str | None:
    """What says boot `prev_boot` ended in an orderly shutdown after its last heartbeat: the daemon's
    stop-signal record or wtmp's shutdown record, as a short phrase; None when neither does (also
    when the last heartbeat is unknown, to stay on the safe side)."""
    if last_heartbeat is None:
        return None
    after = last_heartbeat - SLACK_S
    rec = stop_signal(state)
    try:
        # Its boot id dates it (no upper bound: the clock may step at boot).
        if rec.get("boot") == prev_boot and float(rec.get("ts")) >= after:
            return f"stop signal ({rec.get('signal') or 'unknown'})"
    except (TypeError, ValueError):
        pass
    if wtmp_shutdown(after, boot_at) is not None:
        return "wtmp shutdown record"
    return None

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hold new local `ttp checks` runs while this host's disk is saturated by writes.

Heavy write load (any process on the host) can fill the page cache with dirty pages and stretch
filesystem commits to seconds, which stalls every reader on the host: tests time out and fail for
reasons that are not the change's. nice and ionice do not protect against it (no IO scheduler on
NVMe, or no io controller delegated to user services), so admission is the lever: while either
signal below is over its limit, no new check run takes a host-wide checks slot here (checkcap._take).
A run that may spill tries the spill hosts first (cli._checks_slot); with none free it keeps its place
in the queue and rechecks each poll, model-free. Runs already started go on; the push branch's checks
(their priority slot, checkcap.acquire_priority) are not held, so a write storm elsewhere never stops a delivery.

Signals, each with its limit read from TTP_CHECKS_<KEY> in the environment, else the account's
checks.<key>, else the default; 0 turns that signal off, an unreadable value is the default:
- `Dirty:` in /proc/meminfo over checks.dirty_max_mb megabytes (default DIRTY_MAX_MB).
- `full avg60` in /proc/pressure/io (the share of the last minute every non-idle task waited on IO, in
  percent) over checks.io_full_max (default IO_FULL_MAX). A kernel without PSI has no such file: only
  that signal is skipped.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import project

MEMINFO = Path("/proc/meminfo")
PSI_IO = Path("/proc/pressure/io")
DIRTY_KEY, IO_KEY = "dirty_max_mb", "io_full_max"
DIRTY_MAX_MB = 2048.0   # default checks.dirty_max_mb
IO_FULL_MAX = 10.0      # default checks.io_full_max (percent of the last minute)


def _limit(key: str, default: float) -> float:
    """TTP_CHECKS_<KEY>, else the account's checks.<key>, else `default`; negative or unreadable: the next."""
    for v in (os.environ.get(f"TTP_CHECKS_{key.upper()}"),
              (project.load_account_settings().get("checks") or {}).get(key)):
        if v in (None, "") or isinstance(v, bool):
            continue
        try:
            n = float(str(v).strip())
        except ValueError:
            continue
        if n >= 0 and n == n and n != float("inf"):
            return n
    return default


def dirty_mb(path: Path | None = None) -> float | None:
    """Dirty page cache in MB from /proc/meminfo; None when unreadable."""
    try:
        m = re.search(r"^Dirty:\s+(\d+)\s*kB", (path or MEMINFO).read_text(), re.M)
    except OSError:
        return None
    return int(m.group(1)) / 1024.0 if m else None


def io_full_avg60(path: Path | None = None) -> float | None:
    """The `full avg60` of /proc/pressure/io; None without PSI or when unreadable."""
    try:
        m = re.search(r"^full\b.*\bavg60=([\d.]+)", (path or PSI_IO).read_text(), re.M)
    except OSError:
        return None
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def reading() -> dict:
    """The measured values, their limits and why new local runs are held ("" when they are not)."""
    dirty, dirty_max = dirty_mb(), _limit(DIRTY_KEY, DIRTY_MAX_MB)
    io, io_max = io_full_avg60(), _limit(IO_KEY, IO_FULL_MAX)
    why = []
    if dirty_max and dirty is not None and dirty > dirty_max:
        why.append(f"Dirty {dirty / 1024:.1f} GB over the {dirty_max / 1024:.1f} GB limit (checks.{DIRTY_KEY})")
    if io_max and io is not None and io > io_max:
        why.append(f"io pressure full avg60 {io:.1f}% over {io_max:g}% (checks.{IO_KEY})")
    return {"dirty_mb": dirty, "dirty_max_mb": dirty_max, "io_full_avg60": io, "io_full_max": io_max,
            "held": "; ".join(why)}


def saturated() -> str:
    """Why no new local check run may start now for the disk, else ""."""
    return reading()["held"]


def line(r: dict | None = None) -> str:
    """One status line: the values, their limits and whether new local runs are held."""
    r = reading() if r is None else r
    dirty = "unreadable" if r["dirty_mb"] is None else f"{r['dirty_mb'] / 1024:.1f} GB"
    io = "no PSI" if r["io_full_avg60"] is None else f"{r['io_full_avg60']:.1f}%"
    lim = lambda v, unit: "off" if not v else unit(v)   # noqa: E731
    vals = (f"Dirty {dirty} (limit {lim(r['dirty_max_mb'], lambda v: f'{v / 1024:.1f} GB')}), "
            f"io full avg60 {io} (limit {lim(r['io_full_max'], lambda v: f'{v:g}%')})")
    if r["held"]:
        return f"disk saturated: new local check runs held (spill hosts first, else they wait): {r['held']}; {vals}"
    return f"disk: {vals}"

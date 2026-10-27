# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free watch on the host's process churn (Daemon.check_churn).

Every daemon reads the host's fork count (`processes` in /proc/stat) about every CHECK_S. Only while
the rate is at or above checks.churn_forks_s (TTP_CHURN_FORKS_S; default FORKS_S, 0 turns it off) does
it list /proc, and it reads the cgroup of each process it has not seen yet: one in a
tt-project-<project>.service unit belongs to that project's daemon, its workers and their tests. Once
the rate has stayed that high for checks.churn_window_s (TTP_CHURN_WINDOW_S; default WINDOW_S), the
project with the most new processes (else the most live pytest-xdist workers) owns the churn, and that
project's own daemon raises one observation, severity normal, keyed `process-churn:<unit>`, once per
episode. A burst shorter than the window, or a rate dropping below the threshold, wakes nobody.

Read-only: it never kills, throttles or signals a process. Without /proc or cgroups (macOS) it does
nothing."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import project

PROC = Path("/proc")
CHECK_S = 30.0
FORKS_S = 800      # new processes per second, host-wide: a test suite at a sane worker count stays well below it
WINDOW_S = 120.0   # how long the rate stays at or above it before anyone hears
UNIT = re.compile(r"(tt-project-[^/]+)\.service")
XDIST = (b"xdist", b"execnet", b"exec(eval(sys.stdin.readline()))")   # a pytest-xdist worker's command line


def _setting(env: str, key: str, default: float) -> float:
    for v in (os.environ.get(env), (project.load_account_settings().get("checks") or {}).get(key)):
        if v not in (None, "") and not isinstance(v, bool):
            try:
                return max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    return default


def forks(proc: Path = PROC) -> int | None:
    """Processes created on the host since boot, or None where /proc/stat has no such line."""
    try:
        for line in (proc / "stat").read_text().splitlines():
            if line.startswith("processes "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def owner(pid: int, proc: Path = PROC) -> str | None:
    """The tt-project unit (tt-project-<project>) a process runs in, from its cgroup; None for any other."""
    try:
        m = UNIT.search((proc / str(pid) / "cgroup").read_text())
    except OSError:
        return None
    return m.group(1).replace("\\x2d", "-") if m else None


def is_xdist(pid: int, proc: Path = PROC) -> bool:
    try:
        cmd = (proc / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return any(x in cmd for x in XDIST)


class Watch:
    """One daemon's churn watch; step() once per check, returns the observation to raise, if any."""

    def __init__(self, proc: Path = PROC):
        self.proc = proc
        self.last: tuple[float, int] | None = None   # (time, fork count) at the previous step
        self.since: float | None = None              # when the rate went at or above the threshold
        self.seen: dict[int, str | None] = {}        # pid -> its unit, of processes listed this episode
        self.new: dict[str, int] = {}                # unit -> new processes seen this episode
        self.told = False                            # this episode's observation is out
        self.rates: list[float] = []                 # the rates seen this episode

    def _pids(self) -> set[int]:
        try:
            return {int(n) for n in os.listdir(self.proc) if n.isdigit()}
        except OSError:
            return set()

    def _reset(self) -> None:
        self.since, self.seen, self.new, self.told, self.rates = None, {}, {}, False, []

    def step(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        limit = _setting("TTP_CHURN_FORKS_S", "churn_forks_s", FORKS_S)
        total = forks(self.proc)
        if total is None or not limit:
            return None
        last, self.last = self.last, (now, total)
        if last is None or now <= last[0]:
            return None
        rate = (total - last[1]) / (now - last[0])
        if rate < limit:
            self._reset()
            return None
        pids, first = self._pids(), self.since is None
        if first:
            self.since = last[0]
        self.rates.append(rate)
        for pid in pids - self.seen.keys():
            unit = self.seen[pid] = owner(pid, self.proc)
            if unit and not first:   # the first listing is the baseline, not new
                self.new[unit] = self.new.get(unit, 0) + 1
        for pid in self.seen.keys() - pids:
            del self.seen[pid]
        if self.told or now - self.since < _setting("TTP_CHURN_WINDOW_S", "churn_window_s", WINDOW_S):
            return None
        workers: dict[str, int] = {}
        for pid, unit in self.seen.items():
            if unit and is_xdist(pid, self.proc):
                workers[unit] = workers.get(unit, 0) + 1
        units = set(self.new) | set(workers)
        if not units:
            return None   # not a tt-project project's: no project to tell
        top = max(units, key=lambda u: (self.new.get(u, 0), workers.get(u, 0)))
        self.told = True
        avg = sum(self.rates) / len(self.rates)
        return {"key": f"process-churn:{top}", "severity": "normal", "unit": top,
                "forks_s": round(avg), "new": self.new.get(top, 0), "xdist_workers": workers.get(top, 0),
                "text": (f"process churn: the host has created about {avg:.0f} processes/s for "
                         f"{(now - self.since) / 60:.0f} min (threshold {limit:.0f}/s); {top}.service owns it "
                         f"({self.new.get(top, 0)} of {sum(self.new.values())} new processes seen, "
                         f"{workers.get(top, 0)} pytest-xdist workers live). Lower its test parallelism "
                         f"(TTP_TEST_SLOTS, checks.test_run_max) or its loops. Nothing was stopped.")}

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

Each such event (muted or not) is also appended as one JSON line to the owning project's
state/logs/churn.jsonl (record): the time with its offset, the rate and threshold, new processes per
unit and the TOP_PARENTS command lines that spawned the most new processes (any unit), redacted
(clean) and cut to CMD_MAX chars. Past MAX_BYTES it rotates to churn.jsonl.1 (one old file is kept);
a write failure is logged once and never raises. summary() counts the events and parents of the last
days, model-free.

Read-only: it never kills, throttles or signals a process. Without /proc or cgroups (macOS) it does
nothing."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from . import project

PROC = Path("/proc")
CHECK_S = 30.0
FORKS_S = 800      # new processes per second, host-wide: a test suite at a sane worker count stays well below it
WINDOW_S = 120.0   # how long the rate stays at or above it before anyone hears
UNIT = re.compile(r"(tt-project-[^/]+)\.service")
XDIST = (b"xdist", b"execnet", b"exec(eval(sys.stdin.readline()))")   # a pytest-xdist worker's command line
FILE = "churn.jsonl"
MAX_BYTES = 1024 * 1024
TOP_PARENTS = 5
CMD_MAX = 200
# Secret-looking arguments: a value after a --token/--password/--secret/--key style flag, a NAME=VALUE
# pair whose name holds TOKEN, SECRET, PASSWORD or KEY, an Authorization header or bearer token, and a
# password in a URL.
SECRETS = (
    (re.compile(r"(?i)((?:^|\s)--?[\w.-]*(?:token|passw(?:or)?d|secret|key)[\w.-]*)(=|\s+)(?!-)\S+"), r"\1\2***"),
    (re.compile(r"(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|key)[\w.-]*)=(?!\*\*\*)\S+"), r"\1=***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token|digest)\s+)?\S+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)(?!\*\*\*)\S+"), r"\1***"),
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1***@"),   # a password in a URL
)


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


def cmdline(pid: int, proc: Path = PROC) -> str | None:
    """A process's command line (args joined by spaces; `[comm]` for a kernel thread), or None if gone."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()[:4096]
        if raw.strip(b"\0"):
            return raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")
        return f"[{(proc / str(pid) / 'comm').read_text().strip()}]"
    except OSError:
        return None


def ppid(pid: int, proc: Path = PROC) -> int | None:
    """A process's parent pid from /proc/<pid>/stat, or None."""
    try:
        return int((proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def clean(cmd: str) -> str:
    """A command line safe to log: secret-looking values redacted, then cut to CMD_MAX chars."""
    for rx, sub in SECRETS:
        cmd = rx.sub(sub, cmd)
    return cmd[:CMD_MAX]


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
        self.parents: dict[str, int] = {}            # parent command line -> new processes it spawned
        self.pcmd: dict[int, str | None] = {}        # parent pid -> its command line, read once per episode

    def _pids(self) -> set[int]:
        try:
            return {int(n) for n in os.listdir(self.proc) if n.isdigit()}
        except OSError:
            return set()

    def _reset(self) -> None:
        self.since, self.seen, self.new, self.told, self.rates = None, {}, {}, False, []
        self.parents, self.pcmd = {}, {}

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
            if first:   # the first listing is the baseline, not new
                continue
            if unit:
                self.new[unit] = self.new.get(unit, 0) + 1
            if (pp := ppid(pid, self.proc)) is not None:
                if pp not in self.pcmd:
                    self.pcmd[pp] = cmdline(pp, self.proc)
                if cmd := self.pcmd[pp]:
                    self.parents[cmd] = self.parents.get(cmd, 0) + 1
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
        parents = sorted(self.parents.items(), key=lambda kv: -kv[1])[:TOP_PARENTS]
        return {"key": f"process-churn:{top}", "severity": "normal", "unit": top,
                "forks_s": round(avg), "new": self.new.get(top, 0), "xdist_workers": workers.get(top, 0),
                "rate": round(avg, 1), "threshold": limit, "window_s": round(now - self.since),
                "units": dict(self.new), "parents": [{"cmd": clean(c), "count": n} for c, n in parents],
                "text": (f"process churn: the host has created about {avg:.0f} processes/s for "
                         f"{(now - self.since) / 60:.0f} min (threshold {limit:.0f}/s); {top}.service owns it "
                         f"({self.new.get(top, 0)} of {sum(self.new.values())} new processes seen, "
                         f"{workers.get(top, 0)} pytest-xdist workers live). Lower its test parallelism "
                         f"(TTP_TEST_SLOTS, checks.test_run_max) or its loops. Nothing was stopped.")}


def entry(obs: dict, now: float | None = None) -> dict:
    """The churn.jsonl line for one event (an observation step() returned)."""
    ts = dt.datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    return {"ts": ts.isoformat(timespec="seconds"), "unit": obs.get("unit"), "rate": obs.get("rate", obs.get("forks_s")),
            "threshold": obs.get("threshold"), "window_s": obs.get("window_s"),
            "xdist_workers": obs.get("xdist_workers"), "units": dict(obs.get("units") or {}),
            "parents": [{"cmd": clean(str(x.get("cmd", ""))), "count": int(x.get("count", 0))}
                        for x in (obs.get("parents") or [])[:TOP_PARENTS]]}


def record(f: Path, obs: dict, now: float | None, cache: dict, warn: Callable[[str], None]) -> bool:
    """Append the event's line to f (rotating it to f.1 past MAX_BYTES). Never raises: a failure is
    reported through warn once, until a write succeeds again. cache is kept by the caller. True if written."""
    try:
        data = (json.dumps(entry(obs, now), separators=(",", ":")) + "\n").encode()
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(data) > MAX_BYTES:
            os.replace(f, f.with_name(f.name + ".1"))
        with open(f, "ab") as fh:
            fh.write(data)
        cache["failed"] = False
        return True
    except Exception as e:  # noqa: BLE001 - the daemon tick goes on whatever went wrong
        if not cache.get("failed"):
            cache["failed"] = True
            try:
                warn(f"churn log: could not write {f}: {e}")
            except Exception:  # noqa: BLE001
                pass
        return False


def summary(f: Path, days: float = 7.0, now: float | None = None) -> dict:
    """Events in f (and its rotated f.1) over the last `days`: their count, the new processes per unit
    and the TOP_PARENTS parent command lines by summed count. Unreadable lines are skipped."""
    since = (time.time() if now is None else now) - days * 86400
    events, units, parents = 0, {}, {}
    for g in (f.with_name(f.name + ".1"), f):
        try:
            lines = g.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
                if dt.datetime.fromisoformat(e["ts"]).timestamp() < since:
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            events += 1
            for u, n in (e.get("units") or {}).items():
                units[u] = units.get(u, 0) + int(n)
            for x in e.get("parents") or []:
                parents[x["cmd"]] = parents.get(x["cmd"], 0) + int(x["count"])
    top = sorted(parents.items(), key=lambda kv: -kv[1])[:TOP_PARENTS]
    return {"events": events, "units": units, "parents": [{"cmd": c, "count": n} for c, n in top]}

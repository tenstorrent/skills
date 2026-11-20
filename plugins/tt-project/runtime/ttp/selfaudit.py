# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The self-watch's view past its own host (Daemon.check_self_watch), kept apart from selfwatch's
spawn-rate logic: what else this project holds and installed, in the same one line.
- Held machines: each machine alias (machines.json) a running task of the project names in its
  resources gets a read-only probe over its own ssh route (BatchMode, ConnectTimeout, at most
  SSH_TIMEOUT_S) about every PROBE_EVERY_S, in a thread of its own so a slow host never delays the
  daemon: load against its cores, processes created per second (the `processes` delta of /proc/stat
  between two probes), available memory and free disk. A machine under a pause or hold
  (lockprobe.pause_in: the user's pauses, shared pauses, fleet holds) is never probed; one that does
  not answer is skipped and shown as unreachable.
- Installed entries: about every ENTRIES_EVERY_S, `crontab -l` and the systemd user unit files are
  read. The project's own (crontab lines ending in its tag, its daemon and watchdog units, see service)
  are listed; an entry that appears between two reads while a run of this project ran is flagged with
  those run ids (the first read is the baseline, so nothing that was there before is flagged).
- Holdings: the slot locks labelled with this project's runs (state/locks, its shared resources' and
  the check slots), the keeper holds kept for detached jobs (hold.holding) and the detached jobs of
  its running runs still alive. A runaway the self-watch stops gets its locks released at once
  (run_locks before and after the daemon's reaper sweep), and the stop text says how many.
The readers (crontab, units, ssh) are injected, so tests never touch the real crontab or systemd."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

PROBE_EVERY_S = 120.0
ENTRIES_EVERY_S = 60.0
SSH_TIMEOUT_S = 8.0
CMD_TIMEOUT_S = 5.0
NEW_KEPT = 10   # flagged new entries kept in the snapshot, newest last
PROBE_CMD = ("nproc; cat /proc/loadavg; grep '^processes ' /proc/stat; grep '^MemAvailable:' /proc/meminfo; "
             "df -Pk ~ | tail -1")


def _out(argv: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    if os.environ.get("TTP_TEST_NO_SELFAUDIT_READ"):   # the test suite never reads the real crontab, units or hosts
        return None
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def read_crontab() -> list[str] | None:
    """The user's crontab lines; [] with none; None when it cannot be read."""
    r = _out(["crontab", "-l"], CMD_TIMEOUT_S)
    if r is None:
        return None
    if r.returncode:
        return [] if "no crontab" in (r.stderr or "").lower() else None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def read_units() -> list[str] | None:
    """The names of the systemd user unit files; None without systemd or on an error."""
    r = _out(["systemctl", "--user", "list-unit-files", "--no-legend", "--plain"], CMD_TIMEOUT_S)
    if r is None or r.returncode:
        return None
    return [ln.split()[0] for ln in r.stdout.splitlines() if ln.split()]


def ssh_probe(alias: str) -> str | None:
    """PROBE_CMD's output on `alias` over its ssh route; None when it does not answer in time."""
    r = _out(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", alias, PROBE_CMD], SSH_TIMEOUT_S)
    return r.stdout if r is not None and r.returncode == 0 else None


def parse_probe(text: str) -> dict | None:
    """{cores, load1, processes, mem_avail_gb, disk_free_gb} from PROBE_CMD's output; None if malformed."""
    lines = [ln.split() for ln in (text or "").splitlines() if ln.strip()]
    try:
        cores, load1 = int(lines[0][0]), float(lines[1][0])
        procs = next(int(ln[1]) for ln in lines if ln[0] == "processes")
        mem = next(int(ln[1]) for ln in lines if ln[0] == "MemAvailable:")
        disk = int(lines[-1][3])
    except (IndexError, ValueError, StopIteration):
        return None
    return {"cores": cores, "load1": load1, "processes": procs, "mem_avail_gb": round(mem / 2**20, 1),
            "disk_free_gb": round(disk / 2**20, 1)}


class Audit:
    def __init__(self, base, unit: str, crontab: Callable = read_crontab, units: Callable = read_units,
                 ssh: Callable = ssh_probe):
        from .service import CRON_TAG
        self.tag, self.own_units = f"{CRON_TAG}{base}", {f"{unit}.service", f"{unit}-watchdog.service",
                                                         f"{unit}-watchdog.timer"}
        self.read_cron, self.read_units, self.ssh = crontab, units, ssh
        self.seen: dict[str, set] | None = None
        self.runs_then: set[str] = set()
        self.entries_at = self.probe_at = float("-inf")   # the first call reads at once
        self.new: list[dict] = []
        self.installed = {"crontab": 0, "units": []}
        self.machines: dict[str, dict] = {}
        self._last: dict[str, tuple[float, int]] = {}
        self._thread: threading.Thread | None = None

    def entries(self, now: float, runs: set[str]) -> dict:
        """Read the crontab and units when due; flag what appeared since the last read while `runs`
        (this project's running run ids, now or at the last read) ran."""
        if now - self.entries_at >= ENTRIES_EVERY_S:
            self.entries_at = now
            cron, units = self.read_cron(), self.read_units()
            got = {"crontab": set(cron) if cron is not None else None,
                   "unit": set(units) if units is not None else None}
            during = sorted(runs | self.runs_then, key=lambda r: (len(r), r))
            for kind, now_set in got.items():
                before = (self.seen or {}).get(kind)
                if now_set is None or before is None or not during:
                    continue
                self.new += [{"ts": now, "kind": kind, "entry": e[:200], "runs": during}
                             for e in sorted(now_set - before)]
            self.new = self.new[-NEW_KEPT:]
            self.seen = {k: v if v is not None else (self.seen or {}).get(k) for k, v in got.items()}
            self.runs_then = set(runs)
            if cron is not None:
                self.installed["crontab"] = sum(1 for ln in cron if ln.rstrip().endswith(self.tag))
            if units is not None:
                self.installed["units"] = sorted(self.own_units & set(units))
        return {**self.installed, "new": list(self.new)}

    def probe(self, now: float, aliases: list[str], held: Callable[[str], object]) -> dict:
        """Start a probe round of `aliases` when due (never of one `held` names a pause or hold for);
        returns the last result per alias."""
        for a in list(self.machines):
            if a not in aliases:
                self.machines.pop(a)
        due = now - self.probe_at >= PROBE_EVERY_S and not (self._thread and self._thread.is_alive())
        if due and aliases:
            self.probe_at = now
            todo = []
            for a in aliases:
                if held(a):
                    self.machines[a] = {"skipped": "paused or held"}
                else:
                    todo.append(a)
            if todo:
                self._thread = threading.Thread(target=self._round, args=(todo,), daemon=True)
                self._thread.start()
        return {a: dict(v) for a, v in list(self.machines.items())}   # the round thread may be writing it

    def _round(self, aliases: list[str]) -> None:
        for a in aliases:
            got = parse_probe(self.ssh(a) or "")
            t = time.time()
            if got is None:
                self.machines[a] = {"skipped": "unreachable"}
                continue
            last = self._last.get(a)
            got["forks_s"] = round((got["processes"] - last[1]) / (t - last[0]), 1) \
                if last and t > last[0] and got["processes"] >= last[1] else None
            self._last[a] = (t, got["processes"])
            self.machines[a] = got

    def wait(self, timeout: float = 30.0) -> None:
        if self._thread:
            self._thread.join(timeout)


def _held(path: Path) -> bool:
    from . import lockreap
    return lockreap._held(path) in ("held", "shared")


def own_locks(dirs, base) -> list[dict]:
    """The slot locks held now whose label names this project (its project_root): {lock, run, task}."""
    from . import lockreap
    from .procowner import base_of
    me = str(base_of(base))
    out = []
    for f in lockreap.slot_files(dirs):
        h = lockreap.label(f)
        if h.get("project_root") and str(base_of(h["project_root"])) == me and _held(f):
            out.append({"lock": f.name.rsplit(".", 2)[0], "path": str(f), "run": str(h.get("run_id") or ""),
                        "task": h.get("task_id")})
    return out


def run_locks(dirs, base, run) -> list[dict]:
    return [x for x in own_locks(dirs, base) if x["run"] == str(run)]


def holdings(dirs, base, registry: Path, run_dirs: list[Path]) -> dict:
    """{locks: [names], holds: n, jobs: n}: what this project holds now."""
    from . import hold
    jobs = sum(len(hold.live(hold.run_jobs(Path(d)))) for d in run_dirs)
    return {"locks": sorted({x["lock"] for x in own_locks(dirs, base)}), "holds": len(hold.holding(registry)),
            "jobs": jobs}


def line(snap: dict | None) -> str:
    """The part of the self-watch line this module adds ("" when it has nothing)."""
    snap = snap or {}
    parts = []
    gb = lambda v: "?" if v is None else f"{v:g}"
    ms = snap.get("machines") or {}
    if ms:
        parts.append("held machines: " + ", ".join(
            f"{a} {v['skipped']}" if v.get("skipped") else
            f"{a} load {gb(v.get('load1'))} on {v.get('cores')} cores, {gb(v.get('forks_s'))} new processes/s, "
            f"{gb(v.get('mem_avail_gb'))} GB memory and {gb(v.get('disk_free_gb'))} GB disk free"
            for a, v in sorted(ms.items())))
    inst = snap.get("installed")
    if inst:
        parts.append(f"installed: {inst.get('crontab', 0)} crontab entr{'y' if inst.get('crontab') == 1 else 'ies'}, "
                     f"units {', '.join(inst.get('units') or []) or 'none'}")
        for n in (inst.get("new") or [])[-3:]:
            parts.append(f"new {n['kind']} entry during run(s) {', '.join(n['runs'])}: {n['entry'][:80]}")
    hd = snap.get("holding")
    if hd:
        parts.append(f"holding {len(hd['locks'])} lock(s){' (' + ', '.join(hd['locks'][:5]) + ')' if hd['locks'] else ''}, "
                     f"{hd['holds']} keeper hold(s), {hd['jobs']} detached job(s)")
    return "; ".join(parts)

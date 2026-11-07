# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free watch on this project's own host and process tree that stops its own runaway
(Daemon.check_self_watch).
About every EVERY_S the daemon samples the host (load against its cores, processes created per second
from `processes` in /proc/stat, available memory, free disk under the project) and the project's own
process tree: every process whose environment marks it as this project's (procowner.owns_env: its
TTP_PROJECT, else its TTP_RUN_DIR) and the daemon with its own children. They are grouped by run
(TTP_RUN_ID, with TTP_TASK_ID) and counted: processes, spawn rate, CPU cores used and memory (RSS).
A run's spawn rate is the new processes of its own seen per second since the last sample. Only when
that alone reaches SPAWN_SEEN of the spawn limit is the run also given its share of the host's fork
rate (from /proc/stat, which counts every process and thread any user creates), so a storm's
short-lived processes count once the run is seen forking, and a storm the watch does not see the run
make is never put on it.
A run runs away when, at every sample for WINDOW_S, one limit holds: its spawn rate is at or above
SPAWN_S, its processes at or above PROCS, or the host's load is at least LOAD_X times its cores while
the run uses at least LOAD_SHARE of them. Then the daemon (act()) asks the run's supervisor to stop,
sends TERM to each process group of the run whose members are all this project's own (one with any
other process gets its owned members signalled one by one) and KILL after GRACE_S to what is left; the
kernel drops the flock locks they held and the lock reaper clears their labels. The task gets one
event with the reason and numbers, and the user one notify. The daemon's own group and processes are
never signalled; a fork storm on the host that this project's processes do not account for is only
reported in the line, never acted on.
Settings: config `selfwatch.<key>` (every_s, window_s, spawn_s, procs, load_x, load_share, grace_s,
act), else env TTP_SELFWATCH_<KEY>; 0 turns a limit off, act false only reports. The defaults sit
well above a heavy build or test suite (make -j64 starts some hundreds of processes a second and keeps
a few hundred alive; pytest -n 64 a few hundred) and well below a fork storm (thousands a second).
The last snapshot goes to db kv KV; line() is its one line in ttp status, the web app and the
coordinator's Host section. Linux only: without /proc nothing is sampled."""
from __future__ import annotations

import os
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path

from . import procowner

PROC = Path("/proc")
KV = "selfwatch"
EVERY_S = 30.0
BREACH_EVERY_S = 10.0   # sampled faster while a limit holds, so a runaway is stopped near WINDOW_S
WINDOW_S = 60.0
SPAWN_S = 1000.0        # new processes per second from one run
SPAWN_SEEN = 0.1        # host forks are shared out to a run only while its own seen ones reach this share of SPAWN_S
PROCS = 3000            # live processes of one run
LOAD_X = 2.0            # host load over its cores ...
LOAD_SHARE = 0.5        # ... while the run uses this share of them
GRACE_S = 5.0
DEFAULTS = {"every_s": EVERY_S, "window_s": WINDOW_S, "spawn_s": SPAWN_S, "procs": PROCS,
            "load_x": LOAD_X, "load_share": LOAD_SHARE, "grace_s": GRACE_S, "act": True}
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def settings(cfg: dict | None) -> dict:
    out, conf = dict(DEFAULTS), (cfg or {}).get("selfwatch") or {}
    for k, d in DEFAULTS.items():
        v = conf.get(k, os.environ.get(f"TTP_SELFWATCH_{k.upper()}"))
        if v in (None, ""):
            continue
        if isinstance(d, bool):
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "no", "off")
        else:
            try:
                out[k] = max(0.0, float(v))
            except (TypeError, ValueError):
                pass
    return out


def _stat(proc: Path, pid: int) -> tuple[int, int, int, int, int] | None:
    """(ppid, pgid, start, cpu ticks, rss bytes) of pid, None once gone."""
    try:
        f = (proc / str(pid) / "stat").read_text().rpartition(")")[2].split()
        return int(f[1]), int(f[2]), int(f[19]), int(f[11]) + int(f[12]), int(f[21]) * PAGE
    except (OSError, ValueError, IndexError):
        return None


def _environ(proc: Path, pid: int) -> dict | None:
    try:
        raw = (proc / str(pid) / "environ").read_bytes()
    except OSError:
        return None   # another user's process, or gone: never ours
    return dict(kv.split("=", 1) for kv in raw.decode(errors="replace").split("\0") if "=" in kv)


def host(proc: Path, base: Path, cores: int) -> dict:
    out: dict = {"cores": cores, "forks": None, "load1": None, "mem_avail_gb": None, "disk_free_gb": None}
    try:
        for line in (proc / "stat").read_text().splitlines():
            if line.startswith("processes "):
                out["forks"] = int(line.split()[1])
        out["load1"] = float((proc / "loadavg").read_text().split()[0])
        for line in (proc / "meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                out["mem_avail_gb"] = round(int(line.split()[1]) / 2**20, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        out["disk_free_gb"] = round(shutil.disk_usage(base).free / 2**30, 1)
    except OSError:
        pass
    return out


class Watch:
    """Samples kept across daemon ticks. step() returns the snapshot, with `runaway`: the runs past a
    limit for the whole window (each once per episode)."""

    def __init__(self, base, proc: Path = PROC, daemon_pid: int | None = None, cores: int | None = None):
        self.base, self.proc = Path(base), Path(proc)
        self.daemon_pid = daemon_pid or os.getpid()
        self.cores = cores or os.cpu_count() or 1
        self.env: dict[tuple[int, int], tuple] = {}     # (pid, start) -> (owned, run, task)
        self.last: dict | None = None                   # previous sample: ts, forks, procs {(pid, start): ticks}
        self.since: dict[str, float] = {}               # run -> first sample past a limit
        self.acted: set[str] = set()

    def _whose(self, pid: int, start: int, ppid: int, seen: dict) -> tuple:
        if (pid, start) not in self.env:
            env = _environ(self.proc, pid)
            if pid == self.daemon_pid or (ppid in seen and seen[ppid][0] == "daemon" and not (env or {}).get("TTP_RUN_ID")):
                self.env[(pid, start)] = ("daemon", "daemon", None)
            elif procowner.owns_env(self.base, env):
                self.env[(pid, start)] = ("own", env.get("TTP_RUN_ID") or "other", env.get("TTP_TASK_ID"))
            else:
                self.env[(pid, start)] = (None, None, None)
        return self.env[(pid, start)]

    def scan(self) -> dict[int, tuple]:
        """pid -> (owner, run, task, ppid, pgid, start, ticks, rss) of every live process, parents first."""
        try:
            pids = sorted(int(d) for d in os.listdir(self.proc) if d.isdigit())
        except OSError:
            return {}
        out: dict[int, tuple] = {}
        for pid in pids:
            if (st := _stat(self.proc, pid)) is None:
                continue
            ppid, pgid, start, ticks, rss = st
            out[pid] = (*self._whose(pid, start, ppid, out), ppid, pgid, start, ticks, rss)
        return out

    def step(self, s: dict, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        h = host(self.proc, self.base, self.cores)
        procs = self.scan()
        live = {(pid, v[5]) for pid, v in procs.items()}
        self.env = {k: v for k, v in self.env.items() if k in live}
        prev = self.last or {}
        dt = max(1e-3, now - prev.get("ts", now - 1))
        forks_s = max(0.0, (h["forks"] - prev["forks"]) / dt) if prev.get("forks") is not None and h["forks"] is not None else None
        old = prev.get("procs", {})
        new_all = sum(1 for k in live if k not in old)
        runs: dict[str, dict] = {}
        for pid, (who, run, task, _pp, pgid, start, ticks, rss) in procs.items():
            if not who:
                continue
            r = runs.setdefault(run, {"run": run, "task": task, "procs": 0, "new": 0, "ticks": 0, "rss": 0, "pgids": set()})
            r["procs"] += 1
            r["rss"] += rss
            r["pgids"].add(pgid)
            r["task"] = r["task"] or task
            r["ticks"] += ticks - old.get((pid, start), 0 if self.last else ticks)
            r["new"] += (pid, start) not in old if self.last else 0
        for r in runs.values():
            seen_s = r["new"] / dt
            backed = new_all and s["spawn_s"] and seen_s >= SPAWN_SEEN * s["spawn_s"]
            r["spawn_s"] = round(max(seen_s, (forks_s or 0) * r["new"] / new_all) if backed else seen_s, 1)
            r["cpu"] = round(max(0, r["ticks"]) / TICK / dt, 2)
            r["why"] = _past(r, h, s) if r["run"] != "daemon" else ""
        self.last = {"ts": now, "forks": h["forks"], "procs": {(pid, v[5]): v[6] for pid, v in procs.items()}}
        runaway = []
        for run in list(self.since):
            if not runs.get(run, {}).get("why"):
                self.since.pop(run)
                self.acted.discard(run)
        for run, r in runs.items():
            if r["why"]:
                first = self.since.setdefault(run, now)
                if now - first >= s["window_s"] and run not in self.acted and run != "other":
                    self.acted.add(run)
                    runaway.append(r)
        mine = sum(r["spawn_s"] for r in runs.values())
        tree = {"procs": sum(r["procs"] for r in runs.values()), "spawn_s": round(mine, 1),
                "cpu": round(sum(r["cpu"] for r in runs.values()), 2), "rss_gb": round(sum(r["rss"] for r in runs.values()) / 2**30, 2),
                "runs": len([r for r in runs if r not in ("daemon", "other")])}
        foreign = bool(forks_s and s["spawn_s"] and forks_s >= s["spawn_s"] and mine < s["spawn_s"] / 2)
        top = max(runs.values(), key=lambda r: (r["spawn_s"], r["procs"]), default=None)
        return {"ts": now, "host": {**h, "forks_s": None if forks_s is None else round(forks_s, 1)}, "tree": tree,
                "top": _public(top) if top else None, "breach": [_public(r) for r in runs.values() if r["why"]],
                "foreign_churn": foreign, "runaway": runaway, "sampled": bool(procs)}


def _past(r: dict, h: dict, s: dict) -> str:
    if s["spawn_s"] and r["spawn_s"] >= s["spawn_s"]:
        return f"{r['spawn_s']:.0f} processes/s (limit {s['spawn_s']:.0f})"
    if s["procs"] and r["procs"] >= s["procs"]:
        return f"{r['procs']} processes (limit {s['procs']:.0f})"
    if s["load_x"] and s["load_share"] and h["load1"] is not None and h["load1"] >= s["load_x"] * h["cores"] \
            and r["cpu"] >= s["load_share"] * h["cores"]:
        return f"host load {h['load1']:.0f} on {h['cores']} cores with {r['cpu']:.0f} cores used by it"
    return ""


def _public(r: dict) -> dict:
    return {k: r[k] for k in ("run", "task", "procs", "spawn_s", "cpu", "why")} | {"rss_gb": round(r["rss"] / 2**30, 2)}


def stop(w: Watch, r: dict, grace_s: float, kill: Callable[[int, int, bool], None] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> dict:
    """TERM, then KILL after grace_s, each of run r's processes this project owns (see the module doc).
    kill(target, sig, group) is os.killpg / os.kill by default. Returns what was signalled."""
    kill = kill or (lambda t, sig, group: (os.killpg if group else os.kill)(t, sig))
    own_pgid = os.getpgid(0) if hasattr(os, "getpgid") else None

    def targets() -> tuple[set[int], set[int]]:
        procs = w.scan()
        mine = {pid for pid, v in procs.items() if v[0] == "own" and v[1] == r["run"] and pid != w.daemon_pid}
        daemon_pgids = {procs[w.daemon_pid][4]} if w.daemon_pid in procs else set()
        groups, singles = set(), set()
        for pgid in {procs[pid][4] for pid in mine}:
            members = {pid for pid, v in procs.items() if v[4] == pgid}
            if members <= mine and pgid not in daemon_pgids and pgid != own_pgid:
                groups.add(pgid)
            else:
                singles |= members & mine
        return groups, singles

    done: dict = {"groups": [], "pids": [], "killed": []}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        groups, singles = targets()
        if sig == signal.SIGKILL and not (groups or singles):
            break
        for t, group in [(g, True) for g in sorted(groups)] + [(p, False) for p in sorted(singles)]:
            try:
                kill(t, sig, group)
            except OSError:
                continue
            if sig == signal.SIGTERM:
                done["groups" if group else "pids"].append(t)
            else:
                done["killed"].append(t)
        if sig == signal.SIGTERM:
            sleep(grace_s)
    return done


def line(snap: dict | None, now: float | None = None) -> str:
    """One line: host, own tree, and the last runaway stopped within the hour."""
    if not snap or not snap.get("sampled"):
        return ""
    now = time.time() if now is None else now
    h, t = snap["host"], snap["tree"]
    gb = lambda v: "?" if v is None else f"{v:g}"
    out = (f"self-watch: host load {gb(h.get('load1'))} on {h['cores']} cores, {gb(h.get('forks_s'))} new processes/s, "
           f"{gb(h.get('mem_avail_gb'))} GB memory and {gb(h.get('disk_free_gb'))} GB disk free; own tree {t['procs']} "
           f"processes in {t['runs']} run(s), {t['spawn_s']:g}/s spawned, {t['cpu']:g} cores, {t['rss_gb']:g} GB")
    if snap.get("breach"):
        out += "; past a limit: " + ", ".join(f"run {b['run']} ({b['why']})" for b in snap["breach"][:3])
    if snap.get("foreign_churn"):
        out += "; the host's process churn is not from this project's processes (reported, not acted on)"
    last = snap.get("stopped") or {}
    if last and now - float(last.get("ts", 0)) < 3600:
        out += f"; stopped runaway run {last['run']} (task #{last.get('task')}): {last['why']}"
    return out

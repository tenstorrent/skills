# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a detached job (`ttp detach`) uses what it asked for.

`ttp detach` records the job's request in detached.json: cores (`--cores`, else read from the command:
-j N, --jobs N, --threads N, --workers N, and OMP_NUM_THREADS or MAKEFLAGS only where the command sets
them or the value differs from the daemon's own environment; nothing for a command that hands its
work to another host over ssh), devices (`--devices`) and memory (`--mem-gb`, recorded only). A task
holding a device lock (`device.locks`) asks for the device too.

The daemon samples each running job of a live task, model-free, every `sample_s`: the CPU time of
the job's own process tree (its wrapper's pid in <name>.started and that pid's descendants; Linux
/proc, macOS ps over the wrapper's process group) and, with `device.nodes` globs set (Linux), the
device nodes the tree holds open. Docker containers run outside that tree (their processes hang under
the container runtime's shim), so on Linux the CPU of each running container the daemon's user can
see (`docker ps`) that started after the job's root process is counted too, read from the container's
cgroup (else its init process's tree). A container that ends keeps its last reading in the job's
total. Containers started by other work after the job began also count: that can hide under-use,
never invent it. Without docker, or when it cannot be read, only the tree counts. Averaged over
`window_s`, cores used under `under_frac` of the request, or a requested device that no process of
the job held open for the whole window, is under-use: the stretch is recorded on the task (kv
`job_use`, shown by `ttp status` and the web app), raises one `job_underuse` event per stretch, and
pulls the task's next check in to at most `check_cap_s` away (one worker check per stretch). Where
sampling is unavailable the job shows as unmeasured and nothing fires.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

KV = "job_use"
DEFAULTS = {"sample_s": 60, "window_s": 900, "under_frac": 0.25, "check_cap_s": 1800}
MIN_CORES = 2   # a single-core request is never judged: an idle wait on one core is no waste

_CORES = [re.compile(r"(?:^|\s)-j\s*(\d+)\b"), re.compile(r"--(?:jobs|threads|workers|parallel)[= ](\d+)\b"),
          # set by the command itself: `VAR=N cmd`, `env VAR=N ...`, `export VAR=N` inside `sh -c`
          re.compile(r"(?:^|[\s;&|(])OMP_NUM_THREADS=['\"]?(\d+)"),
          re.compile(r"(?:^|[\s;&|(])MAKEFLAGS=['\"]?[^'\";&|]*?-j\s*(\d+)")]
_REMOTE = re.compile(r"(?:^|[\s;&|(/])ssh\s")   # the work runs on another host: its cores are not ours


def settings(cfg: dict) -> dict:
    got = ((cfg.get("waiting") or {}).get("job_use") or {})
    return {k: float(got.get(k, v)) for k, v in DEFAULTS.items()}


def _env_cores(env: dict) -> list[int]:
    found = [int(env["OMP_NUM_THREADS"])] if str(env.get("OMP_NUM_THREADS") or "").isdigit() else []
    mk = re.search(r"-j\s*(\d+)", str(env.get("MAKEFLAGS") or ""))
    return found + ([int(mk.group(1))] if mk else [])


def daemon_env(pidfile: Path) -> dict | None:
    """The project daemon's own environment (Linux /proc), which every worker and job inherits; None
    when it cannot be read."""
    try:
        raw = Path(f"/proc/{int(Path(pidfile).read_text().strip())}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    return dict(x.split("=", 1) for x in raw.decode(errors="replace").split("\0") if "=" in x)


def requested(cmd: list, env: dict, cores: int | None = None, devices: int | None = None,
              mem_gb: float | None = None, base: dict | None = None) -> dict:
    """What a job asks for: explicit values win; cores are else read from what its command states.
    The environment (OMP_NUM_THREADS, MAKEFLAGS) counts only where it differs from `base`, the daemon's
    environment every job inherits: an ambient default is no request. A command that runs its work
    on another host (ssh) asks for no cores here."""
    if cores is None:
        text = " ".join(map(str, cmd))
        if not _REMOTE.search(text + " "):
            found = [int(m) for rx in _CORES for m in rx.findall(text)]
            if base is not None:
                found += [n for n in _env_cores(env) if n not in _env_cores(base)]
            cores = max(found) if found else None
    out = {}
    if cores:
        out["cores"] = int(cores)
    if devices:
        out["devices"] = int(devices)
    if mem_gb:
        out["mem_gb"] = float(mem_gb)
    return out


def root_pid(job: dict) -> int | None:
    """The job's wrapper pid: <name>.started, which the wrapper writes as it starts, else the launcher's record."""
    try:
        return int(Path(job["rc"]).with_suffix(".started").read_text().strip())
    except (OSError, ValueError, KeyError):
        pass
    try:
        return int(job.get("pid") or 0) or None
    except (TypeError, ValueError):
        return None


def _stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _children(pid: int) -> list[int] | None:
    kids = []
    try:
        for t in Path(f"/proc/{pid}/task").iterdir():
            kids += [int(x) for x in (t / "children").read_text().split()]
    except (OSError, ValueError):
        return None
    return kids


def _tree(pid: int) -> list[int] | None:
    seen, todo = [], [pid]
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        kids = _children(cur)
        if kids is None:
            if cur == pid:
                return None
            continue   # ended meanwhile
        seen.append(cur)
        todo += kids
    return seen


def _clock(s: str) -> float:
    """ps's cputime, [dd-][hh:]mm:ss[.ss], in seconds."""
    days, _, rest = s.rpartition("-")
    total = 0.0
    for part in rest.split(":"):
        total = total * 60 + float(part)
    return total + (int(days) * 86400 if days else 0)


def _cpu(pid: int, hz: float) -> float:
    f = _stat(pid)   # utime, stime, cutime, cstime: reaped children count in their parent
    return sum(int(x) for x in f[11:15]) / hz if f else 0.0


def _hz() -> float:
    return os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def containers(timeout: float = 10) -> list[dict] | None:
    """Running docker containers the daemon's user can see: [{"id", "pid"}] (short id, init pid on this
    host). [] where docker is absent or this is not Linux; None when docker is there but cannot be
    read (no permission, its daemon down or hung)."""
    if not Path("/proc/self/stat").exists() or not shutil.which("docker"):
        return []
    try:
        ps = subprocess.run(["docker", "ps", "-q", "--no-trunc"], capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL)
        if ps.returncode:
            return None
        ids = ps.stdout.split()
        if not ids:
            return []
        # A container that ended between the two calls makes inspect exit non-zero: the rest still print.
        out = subprocess.run(["docker", "inspect", "--format", "{{.Id}} {{.State.Pid}}", *ids],
                             capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = []
    for row in out.splitlines():
        f = row.split()
        if len(f) == 2 and f[1].isdigit() and int(f[1]) > 0:
            found.append({"id": f[0][:12], "pid": int(f[1])})
    return found


def _cgroup_cpu(pid: int) -> float | None:
    """CPU seconds of the cgroup `pid` runs in (cgroup v2 cpu.stat, else v1 cpuacct), None if unreadable."""
    try:
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    except OSError:
        return None
    for row in rows:
        num, ctrl, path = (row.split(":", 2) + ["", ""])[:3]
        if num == "0" and ctrl == "":
            try:
                for kv in Path(f"/sys/fs/cgroup{path}/cpu.stat").read_text().splitlines():
                    if kv.startswith("usage_usec "):
                        return int(kv.split()[1]) / 1e6
            except (OSError, ValueError):
                pass
        elif "cpuacct" in ctrl.split(","):
            for mount in (ctrl, "cpuacct", "cpu,cpuacct"):
                try:
                    return int(Path(f"/sys/fs/cgroup/{mount}{path}/cpuacct.usage").read_text()) / 1e9
                except (OSError, ValueError):
                    continue
    return None


def container_cpu(ctrs: list | None, start) -> dict:
    """CPU seconds so far of each container in `ctrs` (from containers()) whose init process started at
    or after `start`, the job root's start (/proc clock ticks since boot): id -> seconds. A container
    started before the job is not its work. Read from the container's cgroup, which also holds what
    `docker exec` runs; else the init process's tree."""
    try:
        since = int(start)
    except (TypeError, ValueError):
        return {}
    out = {}
    for c in ctrs or []:
        st = _stat(c["pid"])
        try:
            if not st or int(st[19]) < since:
                continue
        except ValueError:
            continue
        cpu = _cgroup_cpu(c["pid"])
        if cpu is None:
            cpu = sum(_cpu(q, _hz()) for q in _tree(c["pid"]) or [])
        out[c["id"]] = round(cpu, 2)
    return out


def sample(pid: int, nodes: list | None = None) -> dict | None:
    """The job tree's CPU seconds so far (`cpu`), its root's start (`start`, to tell a reused pid) and,
    with device globs on Linux, how many device nodes it holds open (`dev`, else None). None when
    this platform cannot be sampled or the root is gone."""
    if Path("/proc/self/stat").exists():
        st = _stat(pid)
        pids = _tree(pid) if st else None
        if not st or not pids:
            return None
        cpu, dev = 0.0, set() if nodes else None
        hz = _hz()
        wanted = {os.path.realpath(x) for g in nodes or [] for x in glob.glob(g)}
        for q in pids:
            cpu += _cpu(q, hz)
            if dev is not None:
                try:
                    for fd in Path(f"/proc/{q}/fd").iterdir():
                        try:
                            target = os.readlink(fd)
                        except OSError:
                            continue
                        if target in wanted:
                            dev.add(target)
                except OSError:
                    pass
        return {"cpu": cpu, "start": st[19], "dev": None if dev is None else len(dev)}
    if sys.platform == "darwin":
        try:   # -S counts each process's exited children in its time
            out = subprocess.run(["ps", "-A", "-S", "-o", "pgid=,lstart=,pid=,time="], capture_output=True,
                                 text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        cpu, start = 0.0, None
        for line in out.splitlines():
            f = line.split()
            if len(f) < 8 or f[0] != str(pid):
                continue
            try:
                cpu += _clock(f[-1])
            except ValueError:
                continue
            if f[-2] == str(pid):
                start = "_".join(f[1:6])
        return {"cpu": cpu, "start": start, "dev": None} if start else None
    return None


def assess(state: dict | None, got: dict | None, req: dict, now: float, s: dict) -> tuple[dict, bool]:
    """Fold one sample into a job's state; True when an under-use stretch begins (fire once).

    got: `cpu` (the tree's), `start`, `dev` and optionally `ctr` (container_cpu()), whose CPU adds in.
    state: `ring` of [t, cpu] over the last window, `start` (root's start), `used` (cores, window
    average), `dev_seen` (last time a device node was open), `since` (first sample), `under`
    (the stretch's start or absent), `why`, `unmeasured`, `ctr` (last container readings) and
    `ctr_done` (CPU of the job's containers that ended, kept so the total never drops)."""
    st = dict(state or {})
    if got is None:
        st.update(unmeasured=True, ring=[])
        st.pop("under", None)
        return st, False
    win = s["window_s"]
    ring = st.get("ring") or []
    ctr = {k: float(v) for k, v in (got.get("ctr") or {}).items()}

    def total(st: dict) -> tuple[float, float]:
        last = st.get("ctr") or {}
        done = float(st.get("ctr_done") or 0) + sum(float(v) for k, v in last.items() if k not in ctr)
        return got["cpu"] + done + sum(ctr.values()), done

    cpu, done = total(st)
    if st.get("start") != got["start"] or (ring and (now - ring[-1][0] > win or cpu < ring[-1][1])):
        ring, st = [], {}   # a new process under that pid, a long gap (host asleep) or a lost tree: start over
        cpu, done = total(st)
    if ctr or done:
        st.update(ctr=ctr, ctr_done=round(done, 2))
    else:
        st.pop("ctr", None)
        st.pop("ctr_done", None)
    st.pop("unmeasured", None)
    st.setdefault("since", now)
    st["start"] = got["start"]
    ring = [r for r in ring if now - r[0] <= win + 2 * s["sample_s"]] + [[now, cpu]]
    st["ring"] = ring
    full = [r for r in ring if now - r[0] >= win]
    base = full[-1] if full else None
    if base and now > base[0]:
        st["used"] = round((cpu - base[1]) / (now - base[0]), 2)
    if got.get("dev"):
        st["dev_seen"] = now
    if got.get("dev") is not None:
        st["dev_measured"] = True
    why = []
    cores = req.get("cores") or 0
    if base and cores >= MIN_CORES and st.get("used") is not None and st["used"] < s["under_frac"] * cores:
        why.append(f"asked for {cores} cores, used {st['used']:g} on average over {win / 60:g} min")
    if req.get("devices") and got.get("dev") is not None and now - max(st.get("dev_seen") or 0, st["since"]) >= win:
        why.append(f"holds {req['devices']} device(s) but no process of it opened one in {win / 60:g} min")
    if not why:
        st.pop("under", None)
        st.pop("why", None)
        return st, False
    st["why"] = "; ".join(why)
    if "under" in st:
        return st, False
    st["under"] = now
    return st, True


def line(req: dict, st: dict | None) -> str:
    """One short line: requested vs used."""
    asked = ", ".join(x for x in (f"{req['cores']} cores" if req.get("cores") else "",
                                  f"{req['devices']} device(s)" if req.get("devices") else "",
                                  f"{req['mem_gb']:g} GB" if req.get("mem_gb") else "") if x) or "nothing stated"
    st = st or {}
    if st.get("unmeasured"):
        return f"asked {asked}; use unmeasured (cannot sample here)"
    used = [f"{st['used']:g} cores" if st.get("used") is not None else "cores: measuring"]
    if req.get("devices"):
        used.append("device in use" if st.get("dev_seen") else
                    "no device open" if st.get("dev_measured") else "device unmeasured")
    return f"asked {asked}; used {', '.join(used)}" + (" (UNDER-USED)" if st.get("under") else "")

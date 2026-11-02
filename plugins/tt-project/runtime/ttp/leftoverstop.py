# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End the leftover processes of a task that has ended (done, failed, cancelled, exhausted), such as a
test script its run started that still holds a checks slot or a `ttp lock` lane.

A process is an ended task's own only when all of these hold (attributed()): it runs as this user; its
environment carries what the daemon set when it started the task's run, this project's TTP_PROJECT, the
task's TTP_TASK and a TTP_RUN_DIR that is one of the task's own recorded runs (runs.dir), or the
check-run folder `ttp checks` gave its commands, whose owner.json names that run's checks.pid; and no
run of the task is running now. A command line, a working directory or a name never counts. Never
touched, even when attributed: the caller and its ancestors, every running worker, the project's live
daemon (its daemon.pid), a `ttp detach` job's process (TTP_DETACHED: jobstop and `ttp detach --stop` own
those), and a process with a Tenstorrent device open or a device lock's file open (its device's broker
and recovery owner end it). An attributed process that leads a process group takes the group with it
(what it started that dropped the task's environment), unless a member is one never touched above or
another task's, project's or user's process: then only the leader goes and the output says why. Only
local processes are read or signalled: nothing on a remote machine.

sweep(), each jobstop pass of the daemon: a lock file under the project's lock folders or the host's
check-slots folder that is held now, and whose every opener this user can see is such a process of a task
that ended at least GRACE_S ago, stops counting as held: every attributed process of that task (but
those its hand-off lists in leftover_procs with how and when they stop) gets SIGTERM, then SIGKILL after
TERM_S, and one receipt event (task, pids, commands, locks freed) goes to the feed.

`ttp leftover-procs --stop <task> [--pid N ...]` (stop()) is the same stop on request, for a worker or the
coordinator: all of the task's attributed processes, or only the named pids, each refused with its reason
when it is not (another user's, another project's, another task's, a running task's, unattributable). A
sandboxed caller, which cannot see the processes, queues the request (jobstop.QUEUE) for the daemon,
which checks again. Linux only (/proc); elsewhere nothing is attributable and nothing is stopped."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

from .db import load_result
from .project import Project, write_json

ENDED = ("done", "failed", "cancelled", "exhausted")
GRACE_S = 120.0   # the sweep leaves a task's processes alone this long after it ended (its run's own teardown)
TERM_S = 5.0      # SIGTERM to SIGKILL
CMD_CHARS = 120
EVENT = "leftover_procs_stopped"
DEVICE_PREFIX = "/dev/tenstorrent"
PROC = Path("/proc")


def _read_env(pid: int) -> dict[str, str] | None:
    try:
        raw = (PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    return dict(x.decode(errors="replace").split("=", 1) for x in raw.split(b"\0") if b"=" in x)


def _start(pid: int) -> str | None:
    try:
        f = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return None if f[0] in ("Z", "X") else f[19]
    except (OSError, IndexError):
        return None


def _pgrp(pid: int) -> int | None:
    try:
        return int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[2])
    except (OSError, IndexError, ValueError):
        return None


def _uid(pid: int) -> int | None:
    try:
        return (PROC / str(pid)).stat().st_uid
    except OSError:
        return None


def _cmd(pid: int) -> str:
    try:
        return (PROC / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _fds(pid: int) -> list[str]:
    """What `pid`'s open file descriptors point at ([] when unreadable)."""
    d = PROC / str(pid) / "fd"
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for n in names:
        try:
            out.append(os.readlink(d / n))
        except OSError:
            continue
    return out


def _real(path: str | None) -> str:
    return os.path.realpath(path) if path else ""


def _daemon_pid(p: Project) -> int | None:
    """The project's live daemon, from its pid file (None when missing or stale)."""
    from .daemon import _is_daemon, _read_pid
    pid = _read_pid(p.state / "daemon.pid")
    return pid if pid and _is_daemon(pid) else None


def _guarded(p: Project) -> set[int]:
    """jobstop's (the caller and its ancestors, every running worker) and the live daemon, which may
    carry a run's environment (daemon._on_pass_env)."""
    from .jobstop import _guarded as guarded
    out = guarded(p)
    if d := _daemon_pid(p):
        out.add(d)
    return out


def _run_of(p: Project, run_dir: str) -> str:
    """The run `run_dir` stands for, resolved: itself, or for a `ttp checks` command's check-run folder
    (checkdirs.environment rewrites its TTP_RUN_DIR) the run whose checks.pid its owner.json names."""
    from . import checkdirs
    from .cli import CHECKS_PID
    real = _real(run_dir)
    if Path(real).parent != Path(_real(str(p.state / checkdirs.FOLDER))):
        return real
    try:
        marker = json.loads((Path(real) / checkdirs.OWNER).read_text()).get("marker")
    except (OSError, ValueError, AttributeError):
        return real
    return _real(str(Path(marker).parent)) if marker and Path(marker).name == CHECKS_PID else real


def lock_dirs(p: Project) -> list[str]:
    """The folders whose files are lock slots or lanes this project may take: its own, its shared
    resources' and the host's check-slots folder."""
    from . import checkcap, jobstop
    return [_real(str(d)) for d in [*jobstop.lock_dirs(p), checkcap.slot_dir()]]


def _device_lock(p: Project, path: str, dirs: list[str]) -> bool:
    from . import locks
    devs = locks.device_locks(p.config())
    for d in dirs:
        if path.startswith(d.rstrip("/") + "/"):
            first = path[len(d.rstrip("/")) + 1:].split("/", 1)[0]
            return any(first == x or first.startswith(x + ".") for x in devs)
    return False


def _task_runs(p: Project, task: int) -> tuple[set[str], bool]:
    """(the task's recorded run dirs, resolved; whether one of its runs is running now)."""
    rows = p.db.q("SELECT dir, status FROM runs WHERE task=?", (int(task),))
    return {_real(r["dir"]) for r in rows if r["dir"]}, any(r["status"] == "running" for r in rows)


def why_not(p: Project, task: int, pid: int, guarded: set[int] | None = None) -> str | None:
    """None when live process `pid` is ended task `task`'s own (the module's rules), else why not."""
    if _start(pid) is None:
        return "gone"
    if _uid(pid) != os.getuid():
        return "another user's process"
    env = _read_env(pid)
    if env is None:
        return "its environment cannot be read: not attributable"
    base = env.get("TTP_PROJECT")
    if not base or not str(env.get("TTP_TASK") or "").isdigit() or not env.get("TTP_RUN_DIR"):
        return "carries no task marker (TTP_PROJECT, TTP_TASK, TTP_RUN_DIR): not attributable"
    if _real(base) != _real(str(p.base)):
        return "another project's process"
    if int(env["TTP_TASK"]) != int(task):
        return f"task #{env['TTP_TASK']}'s process, not #{task}'s"
    row = p.db.one("SELECT status FROM tasks WHERE id=?", (int(task),))
    if not row:
        return f"no task #{task} in this project"
    dirs, running = _task_runs(p, task)
    if row["status"] not in ENDED or running:
        return f"task #{task} is {row['status'] if row['status'] not in ENDED else 'running a run'}: not ended"
    if _run_of(p, env["TTP_RUN_DIR"]) not in dirs:
        return f"its run dir is none of task #{task}'s recorded runs: not attributable"
    if pid in (guarded if guarded is not None else _guarded(p)):
        return GUARDED
    return _untouchable(p, pid, env)


GUARDED = "the caller, one of its ancestors, a running worker or the daemon"


def _untouchable(p: Project, pid: int, env: dict) -> str | None:
    if env.get("TTP_DETACHED"):
        return "a `ttp detach` job's process: stop it with `ttp detach --stop`"
    dl = lock_dirs(p)
    if any(f.startswith(DEVICE_PREFIX) or _device_lock(p, f, dl) for f in _fds(pid)):
        return "uses a device or holds a device lock: end it through the device's broker"
    return None


def _members(pgid: int) -> list[int]:
    try:
        names = [int(n) for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return []
    return [n for n in names if _pgrp(n) == pgid]


def group_why(p: Project, task: int, pgid: int, protected: set[int]) -> str | None:
    """None when every live member of process group `pgid` may be signalled with it, else why not: a
    member is protected (guarded, or declared in the hand-off), another user's, unreadable, another
    project's or task's, a `ttp detach` job's or a device user's. A member with no task marker may go."""
    mine = _real(str(p.base))
    for m in _members(pgid):
        if _start(m) is None:
            continue
        if m in protected:
            return f"member pid {m} is {GUARDED} or one the task's hand-off keeps"
        if _uid(m) != os.getuid():
            return f"member pid {m} is another user's process"
        env = _read_env(m)
        if env is None:
            return f"member pid {m}'s environment cannot be read"
        if env.get("TTP_PROJECT") and _real(env["TTP_PROJECT"]) != mine:
            return f"member pid {m} is another project's process"
        if env.get("TTP_TASK") and env["TTP_TASK"] != str(int(task)):
            return f"member pid {m} is task #{env['TTP_TASK']}'s process"
        if why := _untouchable(p, m, env):
            return f"member pid {m} {why if why.startswith('uses') else 'is ' + why}"
    return None


def with_groups(p: Project, task: int, own: list[dict], protected: set[int]) -> tuple[list[dict], list[dict]]:
    """(own plus the other live members of each process group an own process leads and that may go
    whole (group_why), the leader marked with "pgid"; the leaders whose group may not, as {pid, why}:
    only the leader itself goes)."""
    targets, kept, seen = [dict(t) for t in own], [], {t["pid"] for t in own}
    for t in list(targets):
        if _pgrp(t["pid"]) != t["pid"]:
            continue
        if why := group_why(p, task, t["pid"], protected):
            kept.append({"pid": t["pid"], "why": f"its process group was not signalled ({why}): only pid {t['pid']} itself"})
            continue
        t["pgid"] = t["pid"]
        for m in _members(t["pid"]):
            if m not in seen and (s := _start(m)):
                seen.add(m)
                targets.append({"pid": m, "start": s, "cmd": _cmd(m)[:CMD_CHARS], "group": t["pid"]})
    return targets, kept


def _uid_pids() -> list[int]:
    try:
        names = [int(n) for n in os.listdir(PROC) if n.isdigit()]
    except OSError:
        return []
    me = os.getuid()
    return [n for n in names if _uid(n) == me]


def task_procs(p: Project, task: int, pids=None, guarded: set[int] | None = None) -> tuple[list[dict], list[dict]]:
    """(the task's own live processes, as {pid, start, cmd}; refusals, as {pid, why}). With `pids`, only
    those, each refused unless it is; else every live process of this user whose environment names the task."""
    guarded = _guarded(p) if guarded is None else guarded
    if pids is None:
        want = f"{int(task)}"
        cand = [pid for pid in _uid_pids() if (e := _read_env(pid)) and e.get("TTP_TASK") == want
                and _real(e.get("TTP_PROJECT")) == _real(str(p.base))]
    else:
        cand = [int(x) for x in pids]
    own, refused = [], []
    for pid in cand:
        why = why_not(p, task, pid, guarded)
        if why == "gone" and pids is None:
            continue
        if why:
            refused.append({"pid": pid, "why": why})
        else:
            own.append({"pid": pid, "start": _start(pid), "cmd": _cmd(pid)[:CMD_CHARS]})
    return own, refused


def _locks_of(pids, dirs: list[str]) -> set[str]:
    out = set()
    for pid in pids:
        out |= {f for f in _fds(pid) if any(f.startswith(d.rstrip("/") + "/") for d in dirs)}
    return out


def _held(path: str) -> bool:
    from . import locks
    try:
        return not locks.any_free([Path(path)])
    except OSError:
        return False


def _signal(targets: list[dict], sig: int, group_ok=None) -> None:
    alive = [t for t in targets if _start(t["pid"]) == t["start"]]   # a reused pid is never signalled
    for g in {t["pgid"] for t in targets if t.get("pgid")} if group_ok else ():
        # Only while one of its listed processes still lives in it (so the group id is not reused) and
        # it may still go whole: also catches what its members started since it was read.
        if any(t["pid"] == g or t.get("group") == g for t in alive if _pgrp(t["pid"]) == g) and group_ok(g):
            try:
                os.killpg(g, sig)
            except OSError:
                pass
    for t in alive:
        try:
            os.kill(t["pid"], sig)
        except OSError:
            pass


def end(targets: list[dict], term_s: float = TERM_S, group_ok=None) -> list[dict]:
    """SIGTERM, up to term_s for them to exit, then SIGKILL to those left. Returns those signalled. A
    leader with "pgid" also gets its whole group signalled while group_ok(pgid) holds."""
    targets = [t for t in targets if _start(t["pid"]) == t["start"]]
    _signal(targets, signal.SIGTERM, group_ok)
    deadline = time.monotonic() + term_s
    while time.monotonic() < deadline and any(_start(t["pid"]) == t["start"] for t in targets):
        time.sleep(0.05)
    _signal(targets, signal.SIGKILL, group_ok)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(_start(t["pid"]) == t["start"] for t in targets):
        time.sleep(0.05)
    return targets


def _receipt(p: Project, task: int, stopped: list[dict], locks_: set[str], why: str, by: str) -> dict:
    freed = sorted(Path(x).name for x in locks_ if not _held(x))
    held = sorted(Path(x).name for x in locks_ if _held(x))
    procs = "; ".join(f"pid {t['pid']}: {t['cmd'] or '?'}" for t in stopped)
    text = (f"stopped {len(stopped)} leftover process{'es' if len(stopped) != 1 else ''} of ended task #{task} "
            f"({by}: {why}): {procs or 'none'}; locks freed: {', '.join(freed) or 'none'}"
            + (f"; still held: {', '.join(held)}" if held else ""))
    out = {"task": task, "pids": [t["pid"] for t in stopped], "commands": [t["cmd"] for t in stopped],
           "freed": freed, "held": held, "by": by, "text": text}
    if stopped:
        fp = f"{EVENT}:{task}:" + ",".join(f"{t['pid']}@{t['start']}" for t in stopped)
        if not p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (time.time(), "daemon" if by.startswith("daemon") else "ttp", EVENT, fp, "low", text, json.dumps(out), "handled", task))
    return out


def stop(p: Project, task: int, pids=None, by: str = "ttp leftover-procs --stop", dry: bool = False) -> dict:
    """Stop ended task `task`'s own leftover processes (all, or only `pids`). {stopped, refused, freed,
    held, text}; nothing is stopped for a task that has not ended."""
    row = p.db.one("SELECT status FROM tasks WHERE id=?", (int(task),))
    if not row:
        return {"stopped": [], "refused": [], "error": f"no task #{task} in this project"}
    _, running = _task_runs(p, task)
    if row["status"] not in ENDED or running:
        return {"stopped": [], "refused": [], "error": f"task #{task} is {row['status']}"
                + (" with a running run" if running else "") + ": a running task's processes are its own to stop"}
    guarded = _guarded(p)
    own, refused = task_procs(p, task, pids, guarded)
    own, kept = with_groups(p, task, own, guarded)
    if dry:
        return {"stopped": own, "refused": refused, "group_refused": kept, "dry": True}
    held = {x for x in _locks_of([t["pid"] for t in own], lock_dirs(p)) if _held(x)}
    stopped = end(own, group_ok=lambda g: group_why(p, task, g, guarded) is None)
    r = _receipt(p, task, stopped, held, "stopped on request", by)
    return {**r, "stopped": stopped, "refused": refused, "group_refused": kept}


def _declared(p: Project, task: int) -> set[int]:
    """The pids the task's last hand-off lists in leftover_procs with how and when they stop."""
    from .leftoverprocs import allowed
    row = p.db.one("SELECT result FROM tasks WHERE id=?", (int(task),))
    try:
        return allowed(load_result(row["result"]) if row else {})
    except Exception:
        return set()


def sweep(p: Project, now: float | None = None) -> list[dict]:
    """The model-free pass: for each ended task (GRACE_S past) whose own processes are the only openers
    this user can see of a lock that is held now, stop them all (but declared ones) and write a receipt."""
    now = time.time() if now is None else now
    mine = _real(str(p.base))
    pids = _uid_pids()
    by_task: dict[int, list[int]] = {}
    for pid in pids:   # first only environments: most passes find no ended task's process and stop here
        e = _read_env(pid) or {}
        if str(e.get("TTP_TASK") or "").isdigit() and e.get("TTP_PROJECT") and _real(e["TTP_PROJECT"]) == mine:
            by_task.setdefault(int(e["TTP_TASK"]), []).append(pid)
    ended = {}
    for task in by_task:
        row = p.db.one("SELECT status, updated FROM tasks WHERE id=?", (task,))
        if not row or row["status"] not in ENDED or now - float(row["updated"] or 0) < GRACE_S:
            continue
        if not p.db.one("SELECT 1 FROM runs WHERE task=? AND (status='running' OR ended>?)", (task, now - GRACE_S)):
            ended[task] = row["status"]
    if not ended:
        return []
    dirs = lock_dirs(p)
    openers: dict[str, set[int]] = {}
    for pid in pids:
        for f in _fds(pid):
            if any(f.startswith(d.rstrip("/") + "/") for d in dirs):
                openers.setdefault(f, set()).add(pid)
    out = []
    guarded = _guarded(p)
    for task, status in sorted(ended.items()):
        declared = _declared(p, task)
        own = {pid for pid in by_task[task] if why_not(p, task, pid, guarded) is None} - declared
        stuck = {f for f, who in openers.items() if who & own and who <= own and _held(f)}
        if not stuck:
            continue
        targets, _ = task_procs(p, task, guarded=guarded)
        targets, _ = with_groups(p, task, [t for t in targets if t["pid"] not in declared], guarded | declared)
        stopped = end(targets, group_ok=lambda g, task=task, keep=guarded | declared: group_why(p, task, g, keep) is None)
        out.append(_receipt(p, task, stopped, stuck, f"task #{task} is {status} and its leftovers held "
                            f"{', '.join(sorted(Path(x).name for x in stuck))}", "daemon sweep"))
    return out


# A sandboxed caller's request --------------------------------------------------------------------

def request(p: Project, task: int, pids, caller: str) -> Path:
    from .jobstop import QUEUE
    q = p.state / QUEUE / f"leftovers-{int(task)}-{caller or 'x'}-{time.time_ns()}.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    write_json(q, {"leftovers": int(task), "pids": [int(x) for x in pids] if pids else None, "caller": caller,
                   "at": time.time()})
    return q


def serve(p: Project, m: dict) -> list[dict]:
    """The daemon's side of request(): check again and stop; the receipt goes to the feed."""
    r = stop(p, int(m["leftovers"]), m.get("pids"), by="ttp leftover-procs --stop (queued)")
    return [r] if r.get("stopped") else []

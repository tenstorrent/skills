# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Stale harness locks and long lock waits, model-free.

A slot lock (locks.try_take: `<name>.<n>.lock`) is an OS file lock: it ends when the last open file
that holds it closes. Its label names the holder process (pid, pgid, and since this release its start
time as pid_start and its run as run_id). When that holder is dead (its pid is gone, or the pid now
belongs to a process with another start time: a reused pid) while the lock is still taken, some other
process has the lock's file open: one that inherited it (a process an older `ttp lock` command left
running). The sweep (Reaper.sweep: every daemon tick, and each try of a waiting `ttp lock`) then
- keeps it while any process of the holder's process group, or the `ttp lock` that took it, still
  holds it (a long, legitimate hold);
- reaps the process(es) keeping it when every one is the harness's own from the holder's same run
  (TTP_RUN_ID in its environment matches the label's run_id, and TTP_PROJECT its project when it names one): SIGTERM, then SIGKILL on a later sweep;
- leaves it otherwise: the processes are named in the wait alert (long_waits).
A detached job's lock (label via "job", detach.start) is never reaped: every process of the job holds
it by design, and the kernel frees it once the last one ends. A label with no pid_start (an older release) counts as dead only when its pid is gone. A holder is
judged dead on two sweeps GRACE_S apart before anything is reaped.

long_waits finds each lock whose oldest live waiter (its queue ticket) has waited past the config
`locks.wait_alert_min` (default WAIT_ALERT_MIN): the daemon raises one keyed alert per lock
(`lock-wait:<name>`), naming the holder, and clears it when the wait ends.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from . import locks

WAIT_ALERT_MIN = 30   # config locks.wait_alert_min: a lock wait longer than this raises lock-wait:<name>
GRACE_S = 60.0        # a holder seen dead this long (two sweeps) before what keeps its lock is reaped
KILL_AFTER_S = 10.0   # a reaped process still there this long after SIGTERM gets SIGKILL
EVENT_KIND = "lock_released"


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def cmdline(pid: int) -> str:
    """The command line of `pid` ("" when gone or unreadable)."""
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode(errors="replace").strip()[:200]
    except (OSError, ValueError):
        pass
    if sys.platform.startswith("linux"):
        return ""
    import subprocess
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True,
                              timeout=5).stdout.strip()[:200]
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def environ(pid: int) -> dict:
    """The environment of `pid` ({} when unreadable: another user's, gone, or not Linux)."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError):
        return {}
    return dict(x.decode(errors="replace").split("=", 1) for x in raw.split(b"\0") if b"=" in x)


def fd_holders(path: Path) -> list[int] | None:
    """The pids of the processes that have `path` open (this one excluded); None when this cannot
    tell (no /proc: lsof where it is installed)."""
    me, target = os.getpid(), os.path.realpath(path)
    if not os.path.isdir("/proc/self/fd"):
        import subprocess
        try:
            out = subprocess.run(["lsof", "-t", "--", target], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return sorted({int(x) for x in out.stdout.split() if x.isdigit() and int(x) != me})
    found = set()
    for n in os.listdir("/proc"):
        if not n.isdigit() or int(n) == me:
            continue
        try:
            fds = os.listdir(f"/proc/{n}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{n}/fd/{fd}") == target:
                    found.add(int(n))
                    break
            except OSError:
                continue
    return sorted(found)


def verdict(h: dict) -> tuple[str, str]:
    """("alive" | "dead" | "unknown", why) for the holder process a slot label names. Dead: its pid is
    gone, or lives with another start time than the label recorded (pid_start: the pid was reused). A
    label without pid_start (an older release) is dead only when its pid is gone. Unknown: no pid, or one
    this process cannot judge (another host or PID namespace, a kernel thread)."""
    try:
        pid = int(h.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return "unknown", "its label names no pid"
    if not locks._judgeable(h):
        return "unknown", f"pid {pid} was recorded on another host or PID namespace"
    if locks.kthread(pid):
        return "unknown", f"pid {pid} is a kernel thread here"
    if not locks._alive(pid):
        return "dead", f"its pid {pid} is gone"
    rec, now = h.get("pid_start"), locks.start_time(pid)
    if rec and now and str(rec) != str(now):
        return "dead", f"its pid {pid} now belongs to another process (started {now}, recorded {rec})"
    return "alive", f"pid {pid} alive"


def _held(path: Path) -> str:
    """"free", "shared" (read locks only) or "held" (an exclusive lock) for slot file `path`; "" when
    it cannot be opened."""
    import fcntl
    try:
        with open(path) as f:
            for mode, state in ((fcntl.LOCK_EX, "free"), (fcntl.LOCK_SH, "shared")):
                try:
                    fcntl.flock(f, mode | fcntl.LOCK_NB)
                    return state
                except OSError:
                    continue
            return "held"
    except OSError:
        return ""


def label(path: Path) -> dict:
    try:
        h = json.loads(Path(path).read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return h if isinstance(h, dict) else {}


def _same_run(h: dict, pid: int) -> bool:
    """Whether `pid` is the harness's own process of the label's run: its TTP_RUN_ID (and TTP_TASK,
    when both say one) match the label's, and so does its project (TTP_PROJECT's folder name) when the
    label names one: run ids are unique only per project. A label without project (an older release)
    is judged on the run and task alone."""
    run = str(h.get("run_id") or "")
    if not run:
        return False
    env = environ(pid)
    if env.get("TTP_RUN_ID") != run:
        return False
    proj = str(h.get("project") or "")
    if proj and Path(env.get("TTP_PROJECT") or "").name != proj:
        return False
    task = str(h.get("task_id") or "")
    return not (task and env.get("TTP_TASK") and env["TTP_TASK"] != task)


def _signal(pid: int, start: str | None, sig: int) -> bool:
    """Send `sig` to `pid` only while it is still the process that started at `start` (a pidfd where
    there is one, so a pid reused in between is never hit)."""
    pfd = -1
    try:
        pfd = os.pidfd_open(pid)
    except (AttributeError, OSError):
        pfd = -1
    try:
        if start is None or locks.start_time(pid) != start:
            return False
        if pfd >= 0:
            signal.pidfd_send_signal(pfd, sig)
        else:
            os.kill(pid, sig)
        return True
    except OSError:
        return False
    finally:
        if pfd >= 0:
            os.close(pfd)


class Reaper:
    """Remembers, across sweeps of one long-lived process (the daemon, a waiting `ttp lock`), when each
    lock's holder was first seen dead and which processes it sent SIGTERM."""

    def __init__(self, grace_s: float = GRACE_S):
        self.grace_s, self.dead_since, self.termed = grace_s, {}, {}

    def sweep(self, paths, now: float | None = None) -> dict:
        """Judge each slot file in `paths` (see the module doc). Returns {"released": [...], "kept":
        {str(path): {...}}}: a release names the lock, its holder (label, pid, command, task, run,
        project), why it was dead and the pids reaped; a kept lock whose holder is dead names the
        processes that keep it (survivors) and why they were left."""
        now = time.time() if now is None else now
        out: dict = {"released": [], "kept": {}}
        for path in paths:
            key = str(path)
            if _held(path) != "held":
                self.dead_since.pop(key, None)
                continue
            h = label(path)
            if h.get("via") == "job":   # a detached job's lock: every process of the job holds it by design
                self.dead_since.pop(key, None)
                continue
            state, why = verdict(h)
            taker = int(h.get("taker_pid") or 0) if str(h.get("taker_pid") or "").isdigit() else 0
            if state != "dead" or (taker > 0 and locks._alive(taker) and locks._judgeable(h)):
                self.dead_since.pop(key, None)
                continue
            first = self.dead_since.setdefault(key, now)
            info = holder_info(path, h)
            if now - first < self.grace_s:
                continue
            procs = fd_holders(path)
            if procs is None:
                out["kept"][key] = {**info, "dead_why": why, "survivors": [], "left": "cannot list who has it open"}
                continue
            pg = h.get("pgid")
            group = [x for x in procs if pg and _pgid(x) == pg]
            if group:   # a live member of its process group holds it: a legitimate hold
                self.dead_since.pop(key, None)
                continue
            if not procs:
                continue   # let go meanwhile
            own = [x for x in procs if _same_run(h, x)]
            survivors = [{"pid": x, "command": cmdline(x), "same_run": x in own} for x in procs]
            if len(own) < len(procs):
                out["kept"][key] = {**info, "dead_why": why, "survivors": survivors,
                                    "left": "a process not of the holder's run keeps it: left running"}
                continue
            reaped = []
            for x in own:
                start = locks.start_time(x)
                term = self.termed.get((x, start))
                sig = signal.SIGKILL if term and now - term >= KILL_AFTER_S else signal.SIGTERM
                if term and sig == signal.SIGTERM:
                    continue
                if _signal(x, start, sig):
                    self.termed[(x, start)] = term or now
                    reaped.append({"pid": x, "command": cmdline(x) or "?",
                                   "signal": "SIGKILL" if sig == signal.SIGKILL else "SIGTERM"})
            if reaped:
                out["released"].append({**info, "dead_why": why, "reaped": reaped})
        return out


def holder_info(path: Path, h: dict | None = None) -> dict:
    """What an alert or release event says about the holder of slot file `path`."""
    h = label(path) if h is None else h
    since = float(h.get("since") or 0) if isinstance(h.get("since"), (int, float)) else 0.0
    return {"lock": Path(path).name, "path": str(path), "holder": str(h.get("holder") or "?"),
            "pid": h.get("pid"), "command": str(h.get("command") or "")[:200], "task": h.get("task_id"),
            "run": h.get("run_id"), "project": h.get("project"), "since": since}


def slot_files(dirs) -> list[Path]:
    """Every slot file (`*.lock`) directly in each of `dirs`."""
    out = []
    for d in dirs:
        try:
            out += sorted(Path(d).glob("*.lock"))
        except OSError:
            continue
    return out


def release_text(r: dict) -> str:
    procs = ", ".join(f"pid {x['pid']} ({x['command'][:80]}, {x['signal']})" for x in r["reaped"])
    return (f"Released lock {r['lock']}: its holder {r['holder']} (pid {r['pid']}) is dead ({r['dead_why']}); "
            f"stopped what its run left holding it: {procs}.")[:900]


def record(db, released: list[dict], now: float | None = None) -> None:
    """Log each release as a handled event (kind EVENT_KIND) that the daily review counts."""
    now = time.time() if now is None else now
    for r in released:
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
             (now, "daemon", EVENT_KIND, "low", release_text(r), json.dumps(r, default=str), "handled"))


def released_count(db, since: float) -> int:
    row = db.one("SELECT COUNT(*) n FROM events WHERE kind=? AND ts>=?", (EVENT_KIND, since))
    return int(row["n"]) if row else 0


def wait_alert_s(cfg: dict) -> float:
    """The config's lock-wait alert threshold (locks.wait_alert_min) in seconds; 0: off."""
    try:
        v = float(((cfg or {}).get("locks") or {}).get("wait_alert_min", WAIT_ALERT_MIN))
    except (TypeError, ValueError):
        v = float(WAIT_ALERT_MIN)
    return max(v, 0.0) * 60


def long_waits(dirs, threshold_s: float, now: float | None = None, mine=None) -> list[dict]:
    """Each lock (by resource name) in `dirs` that a live waiter has waited on for threshold_s or more,
    with its waiters, oldest wait and the holder(s) of its taken slots. `mine`, when given, is a
    predicate on a ticket's text (its waiter's label): only locks with such a waiter count."""
    now = time.time() if now is None else now
    out = []
    if threshold_s <= 0:
        return out
    for d in dirs:
        try:
            queues = sorted(Path(d).glob("*.queue"))
        except OSError:
            continue
        for q in queues:
            res = q.name[:-len(".queue")]
            tickets = locks.queued(Path(d), res)
            if mine is not None:
                tickets = [t for t in tickets if mine(_ticket_text(t))]
            if not tickets:
                continue
            oldest = min(locks._arrival(t) or now for t in tickets)
            if now - oldest < threshold_s:
                continue
            slots = [s for s in sorted(Path(d).glob(f"{res}.*.lock")) if _held(s) in ("held", "shared")]
            holders = []
            for s in slots:
                h = label(s)
                state, why = verdict(h)
                holders.append({**holder_info(s, h), "state": state, "why": why})
            out.append({"lock": res, "dir": str(d), "waiters": len(tickets), "waited_s": now - oldest,
                        "holders": holders})
    return out


def _ticket_text(t: Path) -> str:
    try:
        return t.read_text()[:300]
    except OSError:
        return ""


def _dur(s: float) -> str:
    return f"{s / 3600:.1f} h" if s >= 5400 else f"{int(s // 60)} min"


def wait_text(w: dict, now: float | None = None, kept: dict | None = None) -> str:
    """The lock-wait alert: the lock, how long its oldest waiter has waited, and for each holder its
    pid and command, task and project, how long it has held the lock and whether it is alive; for a
    dead holder, the processes that keep the lock and why they were left."""
    now = time.time() if now is None else now
    parts = []
    for h in w["holders"] or [{}]:
        if not h:
            parts.append("no holder found (a slot may have been freed just now)")
            continue
        who = h["holder"]
        if h.get("task") or h.get("project"):
            who += " (" + ", ".join(x for x in (f"task #{h['task']}" if h.get("task") else "",
                                                 f"project {h['project']}" if h.get("project") else "") if x) + ")"
        held = f", held {_dur(now - h['since'])}" if h.get("since") else ""
        alive = {"alive": "alive", "dead": f"dead: {h['why']}"}.get(h["state"], f"alive unknown: {h['why']}")
        line = f"{who}, pid {h.get('pid') or '?'} (`{h['command'][:120] or '?'}`){held}, holder {alive}"
        k = (kept or {}).get(h["path"])
        if k and k.get("survivors"):
            line += "; kept by " + ", ".join(f"pid {x['pid']} (`{x['command'][:80]}`)" for x in k["survivors"][:3])
            line += f" ({k['left']})"
        parts.append(line)
    return (f"Lock {w['lock']} has a waiter for {_dur(w['waited_s'])} ({w['waiters']} waiting). "
            f"Holder: " + "; ".join(parts) + ".")[:1500]

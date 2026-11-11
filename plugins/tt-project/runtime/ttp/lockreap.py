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
  holds it (a long, legitimate hold), unless the label names a detached job that is over (ended_job:
  its .rc written, no process of the job left, its task done, failed or cancelled): then a process of
  its group that keeps it is a leftover, reaped as below unless it carries another project's TTP_PROJECT;
- reaps the process(es) keeping it when every one is the harness's own from the holder's same run
  (TTP_RUN_ID in its environment matches the label's run_id, and TTP_PROJECT its project_root (or its
  TTP_RUN_DIR) its project_root when it names one) and provably the sweeping project's own (procowner.owns: its TTP_PROJECT or TTP_RUN_DIR is
  this project's folder; run and task ids overlap across projects on one host, the lock folders are
  shared): SIGTERM, then SIGKILL on a later sweep. A sweep with no project reaps nothing;
- leaves it otherwise: the processes are named in the wait alert (long_waits).
A detached job's lock (label via "job", detach.start) is never reaped: every process of the job holds
it by design, and the kernel frees it once the last one ends. A label with no pid_start (an older release) counts as dead only when its pid is gone. A holder is
judged dead on two sweeps GRACE_S apart before anything is reaped.

long_waits finds each lock whose oldest live waiter (its queue ticket) has waited past the config
`locks.wait_alert_min` (default WAIT_ALERT_MIN): the daemon raises one keyed alert per lock
(`lock-wait:<name>`), naming the holder, and clears it when the wait ends. The checks-slot queue
(checkcap) counts only while a holder is not judged alive (stuck_only): a wait behind running checks is
normal queueing. A device-queue job queued behind another (devq, read from its waiting task's probe)
raises `lock-wait:devq:<runner>` the same way (devq_waits).

The release guarantee is the daemon's tick (RELEASE_WITHIN_S): a waiter's own try (a `ttp lock` in a
sandboxed worker, whose PID namespace hides the holder) may judge nothing, and checks-slot and devq
waiters do not sweep at all.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from . import locks, procowner

WAIT_ALERT_MIN = 30   # config locks.wait_alert_min: a lock wait longer than this raises lock-wait:<name>
GRACE_S = 60.0        # a holder seen dead this long (two sweeps) before what keeps its lock is reaped
KILL_AFTER_S = 10.0   # a reaped process still there this long after SIGTERM gets SIGKILL
FREE_WAIT_S = 3.0     # free_ended (`ttp detach --reap/--stop`) waits this long after each signal for a slot to go free
EVENT_KIND = "lock_released"
# While the daemon runs, a dead holder's lock that only its own run keeps is released this soon: the first
# tick that sees it dead, GRACE_S later the SIGTERM, KILL_AFTER_S after that the SIGKILL, plus tick slack.
RELEASE_WITHIN_S = GRACE_S + KILL_AFTER_S + 30.0


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


def _probe(path: Path) -> str:
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


def _held(path: Path) -> str:
    """"free", "shared" (read locks only) or "held" (an exclusive lock) for slot file `path`; "" when
    it cannot be opened. Every daemon on the host probes the same slot files, so a slot whose label
    names no live holder (locks.holder_live) is probed locks.PROBE_TRIES times: free in any of them is
    free, and another reader's momentary probe never makes its stale label a holder."""
    state = _probe(path)
    if state in ("free", ""):
        return state
    try:
        if locks.holder_live(locks.read_label(path)):
            return state
    except (OSError, ValueError):
        pass
    for _ in range(locks.PROBE_TRIES - 1):
        time.sleep(locks.PROBE_GAP_S)
        again = _probe(path)
        if again in ("free", ""):
            return again
        state = "held" if "held" in (state, again) else again
    return state


def label(path: Path) -> dict:
    try:
        return locks.read_label(path)   # re-read when caught mid-rewrite
    except (OSError, ValueError):
        return {}


def _same_run(h: dict, pid: int) -> bool:
    """Whether `pid` is the harness's own process of the label's run: its TTP_RUN_ID (and TTP_TASK,
    when both say one) match the label's, and so does its project (procowner.owns_env: TTP_PROJECT or
    TTP_RUN_DIR) when the label names its project_root: run ids are unique only per project. A label without one (an older
    release) is judged on the run and task alone; Reaper.sweep also requires procowner.owns."""
    run = str(h.get("run_id") or "")
    if not run:
        return False
    env = environ(pid)
    if env.get("TTP_RUN_ID") != run:
        return False
    if h.get("project_root") and not procowner.owns_env(h["project_root"], env):
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

    def __init__(self, grace_s: float = GRACE_S, ended=None, project=None):
        self.grace_s, self.dead_since, self.termed = grace_s, {}, {}
        self.ended = ended   # label -> why its detached job and task are over ("" if not): ended_job
        self.project = project   # the sweeping project's folder: only its own processes are reaped

    def _owned(self, h: dict, pid: int, group: list, over: str) -> bool:
        """Whether `pid` may be reaped: a process of the label's run that is provably this project's
        (procowner.owns), or, once `over` (ended_job: the label's job .rc is in this project's state), a
        member of that job's process group carrying no other project's TTP_PROJECT (one that dropped
        its environment is still the job's)."""
        if self.project is None:
            return False
        if _same_run(h, pid) and procowner.owns(self.project, pid):
            return True
        return bool(over) and pid in group and not procowner.foreign_env(self.project, procowner.environ(pid))

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
            over = (self.ended(h) if self.ended and h.get("job_rc") else "") or ""
            if group and not over:   # a live member of its process group holds it: a legitimate hold
                self.dead_since.pop(key, None)
                continue
            if over:
                why = f"{why}; {over}"
            if not procs:
                continue   # let go meanwhile
            own = [x for x in procs if self._owned(h, x, group, over)]
            survivors = [{"pid": x, "command": cmdline(x), "same_run": x in own} for x in procs]
            if len(own) < len(procs):
                out["kept"][key] = {**info, "dead_why": why, "survivors": survivors,
                                    "left": "a process not provably this project's, of the holder's run, keeps it: left running"}
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


def ended_job(p, h: dict) -> str:
    """Why slot label `h` names a detached job of project `p` that is over, else "": its .rc (job_rc,
    under the project's state) is written, no process of the job holds its job lock, and its task
    (task_id) is done, failed or cancelled. Another project's label, or a task still running or queued
    to retry, gives "". What else keeps the slot then is a leftover of the job (Reaper.sweep)."""
    from .db import TERMINAL_TASK_STATES
    rc, task = str(h.get("job_rc") or ""), str(h.get("task_id") or "")
    if not rc or not task.isdigit():
        return ""
    if h.get("project_root"):   # the root tells projects apart, whatever their names
        try:
            if Path(h["project_root"]).resolve() != Path(p.base).resolve():
                return ""
        except (OSError, ValueError):
            return ""
    elif h.get("project") and h["project"] != Path(p.base).name:   # an older label: its folder's name
        return ""
    try:
        rc = Path(rc).resolve()
        if not rc.is_relative_to(Path(p.state).resolve()) or not rc.exists():
            return ""
    except (OSError, ValueError):
        return ""
    if not locks.any_free([locks.job_lock(rc)]):
        return ""   # a process of the job still lives: jobstop ends it
    row = p.db.one("SELECT status FROM tasks WHERE id=?", (int(task),))
    if not row or row["status"] not in TERMINAL_TASK_STATES:
        return ""
    return f"its detached job {rc.stem} ended (exit code written, no process left) and its task #{task} is {row['status']}"


def free_ended(p, rc: Path) -> dict:
    """`ttp detach --reap` and `--stop` on a job that is over: free now, without the sweep's grace, each
    slot whose label names this job (jobstop.job_slots) and that ended_job judges over, by reaping its
    leftovers as the sweep would (SIGTERM, then SIGKILL after KILL_AFTER_S). Logs each release as an
    event. Returns {"freed": [...], "held": {slot: why}} by slot name."""
    from .jobstop import job_slots
    slots = job_slots(p, Path(rc).resolve())
    r, t0 = Reaper(grace_s=0, ended=lambda h: ended_job(p, h), project=p.base), time.time()
    released, kept = [], {}
    for step in (0.0, KILL_AFTER_S):   # the sweep's clock: SIGTERM first, SIGKILL once KILL_AFTER_S "passed"
        left = [s for s in slots if _held(s) == "held"]
        if not left:
            break
        got = r.sweep(left, t0 + step)
        released += got["released"]
        kept.update(got["kept"])
        end = time.monotonic() + FREE_WAIT_S
        while got["released"] and time.monotonic() < end and any(_held(s) == "held" for s in left):
            time.sleep(0.1)
    record(p.db, released)
    held = {}
    for s in slots:
        if _held(s) != "held":
            continue
        h = label(s)
        if str(s) in kept:
            held[s.name] = kept[str(s)]["left"]
        elif not ended_job(p, h):
            held[s.name] = "its job or its task is not over"
        else:
            held[s.name] = f"its holder is {verdict(h)[0]} ({verdict(h)[1]})"
    return {"freed": [s.name for s in slots if s.name not in held], "held": held, "released": released}


def holder_info(path: Path, h: dict | None = None) -> dict:
    """What an alert or release event says about the holder of slot file `path`."""
    h = label(path) if h is None else h
    since = float(h.get("since") or 0) if isinstance(h.get("since"), (int, float)) else 0.0
    return {"lock": Path(path).name, "path": str(path), "holder": str(h.get("holder") or "?"),
            "pid": h.get("pid"), "command": str(h.get("command") or "")[:200], "task": h.get("task_id"),
            "run": h.get("run_id"), "project": locks.project_of(h, Path(path).parent), "since": since}


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


def long_waits(dirs, threshold_s: float, now: float | None = None, mine=None, only=None,
               stuck_only: bool = False) -> list[dict]:
    """Each lock (by resource name) in `dirs` that a live waiter has waited on for threshold_s or more,
    with its waiters, oldest wait and the holder(s) of its taken slots. `mine`, when given, is a
    predicate on a ticket's text (its waiter's label): only locks with such a waiter count. `only`: these
    resource names alone. `stuck_only`: only a lock with a holder not judged alive (dead, a reused pid, or
    one that cannot be judged) counts."""
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
            if only is not None and res not in only:
                continue
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
            if stuck_only and all(h["state"] == "alive" for h in holders):
                continue
            out.append({"lock": res, "dir": str(d), "waiters": len(tickets), "waited_s": now - oldest,
                        "holders": holders})
    return out


DEVQ_WAITS_KEY = "devq_waits"   # kv: task id -> what its plain `ttp devq probe` last said of its job's queue wait


def note_devq_wait(db, tid: int, runner: str, probe: str, text: str, rc, now: float | None = None) -> None:
    """Keep what waiting task `tid`'s `ttp devq probe <runner> <job>` answered (devq.wait_line): its job
    queued since when, behind which job. Another answer (running, done, unknown) ends the wait; a timeout
    or an unreachable host (rc None or 255) changes nothing. Written only when the holder changes."""
    from . import devq
    if rc is None or rc == 255:
        return
    now = time.time() if now is None else now
    got = devq.parse_wait(text) if rc == 1 else None
    waits = db.kv(DEVQ_WAITS_KEY, {}) or {}
    k, prev = str(tid), waits.get(str(tid))
    if not got:
        if prev is None:
            return
        waits.pop(k)
    else:
        h = got["holder"]
        holder = h and {**h, "since": round(now - h["run_s"]) if h.get("run_s") is not None else None}
        same = bool(prev) and prev.get("probe") == probe and prev.get("job") == got["job"]
        if same and _steady(prev.get("holder")) == _steady(holder):
            return
        waits[k] = {"runner": runner, "probe": probe, "job": got["job"], "ahead": got["ahead"],
                    "since": prev["since"] if same else round(now - got["waited_s"]), "holder": holder}
    db.set_kv(DEVQ_WAITS_KEY, waits)


def _steady(h):
    """A devq holder without what each probe answer moves on (its run time)."""
    return {k: v for k, v in h.items() if k not in ("run_s", "since")} if h else None


def devq_waits(db, threshold_s: float, now: float | None = None, current=None) -> list[dict]:
    """Each device runner a waiting task's job has been queued on for threshold_s or more (note_devq_wait),
    shaped like long_waits' answers (lock `devq:<runner>`). `current(task)`: the probe the task waits on
    now; an entry whose task no longer waits on its probe is dropped."""
    from .liveness import FOREIGN
    now = time.time() if now is None else now
    waits = db.kv(DEVQ_WAITS_KEY, {}) or {}
    keep, by = {}, {}
    for k, w in waits.items():
        t = db.task(int(k)) if str(k).isdigit() else None
        if not t or t["status"] != "queued" or (current is not None and current(t) != w.get("probe")):
            continue
        keep[k] = w
        by.setdefault(w["runner"], []).append(w)
    if keep != waits:
        db.set_kv(DEVQ_WAITS_KEY, keep)
    out = []
    for runner, ws in sorted(by.items()) if threshold_s > 0 else ():
        oldest = min(float(w["since"]) for w in ws)
        if now - oldest < threshold_s:
            continue
        holders = []
        for h in {(w.get("holder") or {}).get("job"): w.get("holder") for w in ws if w.get("holder")}.values():
            state, why = ("alive", f"pid {h['pid']} alive") if h.get("alive") else \
                ("dead", f"its pid {h['pid']} is gone") if h.get("alive") is False else \
                ("unknown", f"its pid {h['pid']} is {FOREIGN}") if h.get("pid") else ("unknown", "no pid recorded yet")
            holders.append({"lock": f"devq:{runner}", "path": "", "holder": f"device job {h['job']}",
                            "pid": h.get("pid"), "command": "", "task": h.get("task"), "run": None,
                            "project": None, "since": float(h["since"]) if h.get("since") else 0.0,
                            "state": state, "why": why})
        out.append({"lock": f"devq:{runner}", "dir": "", "waiters": len(ws), "waited_s": now - oldest,
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
            f"Holder: " + "; ".join(parts) + ".")[:1300] + " " + BOUND_TEXT


# The release guarantee, in every lock-wait alert: waiters do not sweep (or cannot judge from a sandbox).
BOUND_TEXT = (f"The daemon's tick releases a dead holder's lock that only its own run keeps within about "
              f"{int(RELEASE_WITHIN_S // 60) + 1} min; one it does not release stays named here.")

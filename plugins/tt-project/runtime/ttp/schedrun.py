"""Schedule runs off the daemon's tick thread (Daemon.run_schedules).

A command, heal or builtin watcher schedule runs in a thread of its own, with its own database
connection, so a slow or hung one (a gh call, ssh to a box that does not answer) never holds the tick:
messages, dispatch and the other steps go on. The tick starts due runs, records the ones that ended
with the usual failure handling, and never starts a schedule whose run is still going. A run past its
timeout has its process groups killed and is recorded as a failed run.

The process groups a run starts (spawned) are kept in the database while they live, so a daemon that
stops or restarts kills the ones its runs left (kill_all); such a run recorded nothing, so its schedule
is still due and runs once more. A run's result is recorded only when it ends, never twice. A daemon
that stops marks its runs stopping before it kills their groups (stop): a command or fix killed that
way raises Stopped rather than returning its exit code, so the run unwinds without recording the kill
as a failure, escalating or saving state. A run killed past its timeout is marked the same way: the
tick records its timeout once, and the kill's -9 is no second result.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from . import scriptwatch
from .runner import proc_start

GROUPS_KEY = "schedule_groups"   # kv: pgid -> {schedule, start}: live process groups of schedule runs

_local = threading.local()


class Stopped(BaseException):
    """The daemon stopped while this run went on: its kill is no result. A BaseException, so that no
    `except Exception` on the way records it; the run's thread ends silently."""


# A run killed at its timeout whose thread is still going this many times its timeout later is stuck
# (a builtin watcher looping in pure Python ignores kills): it is observed once, as its schedule cannot run again.
STUCK_FACTOR = 2


class Run:
    """One schedule run in its thread. `result` is (status, run again at) once `done` is set."""

    def __init__(self, s: dict, payload: dict, timeout: float):
        self.s, self.payload, self.timeout = s, payload, timeout
        self.name = s["name"]
        self.started = time.monotonic()
        self.deadline = self.started + timeout
        self.done = threading.Event()
        self.result: tuple[str, float | None] = ("error: run ended without a result", None)
        self.timed_out = False   # recorded failed at its timeout; its late result is dropped
        self.killed_at: float | None = None   # when the tick's backstop killed it (monotonic)
        self.stuck = False   # still alive STUCK_FACTOR x its timeout after that kill: observed once
        self.stopping = threading.Event()   # the daemon stopped it (a stop, its timeout): what its groups return is no result
        self.stopped = False   # it unwound on Stopped: nothing to record
        self.groups: dict[int, str | None] = {}   # pgid -> its leader's start token
        self.db: Any = None


def current() -> Run | None:
    """The schedule run on this thread, if any."""
    return getattr(_local, "run", None)


def start(run: Run, body: Callable[[], tuple[str, float | None]], open_db: Callable[[], Any]) -> Run:
    """Run `body` in a daemon thread with its own database connection (`open_db`)."""
    def work() -> None:
        _local.run = run
        try:
            run.db = open_db()
            run.result = body()
        except Stopped:
            run.stopped = True
        except Exception as e:
            run.result = (f"error: {type(e).__name__}: {e}"[:200], None)
        finally:
            _local.run = None
            try:
                if run.db is not None:
                    run.db.close()
            except Exception:
                pass
            run.done.set()
    threading.Thread(target=work, daemon=True, name=f"schedule:{run.name}").start()
    return run


def spawned(pid: int) -> None:
    """The schedule run on this thread started process group `pid` (a session leader)."""
    run = current()
    if run is None or run.db is None:
        return
    start_token = proc_start(pid)
    run.groups[pid] = start_token
    if run.stopping.is_set():   # started after the stop's kill: it would outlive the daemon
        _kill(pid, start_token)
        return
    with run.db.tx():
        groups = run.db.kv(GROUPS_KEY, {}) or {}
        groups[str(pid)] = {"schedule": run.name, "start": start_token}
        run.db.set_kv(GROUPS_KEY, groups)


def track(run: Run, pid: int) -> bool:
    """Process group `pid` started for `run` on a thread that is not the run's (a builtin watcher's pool):
    kept in memory only (the run's database connection belongs to its own thread), so a stop (stop) or
    the timeout's kill (kill) kills it, a daemon killed outright does not. One started after the run was
    stopped is killed at once: returns False then."""
    start_token = proc_start(pid)
    run.groups[pid] = start_token
    if run.stopping.is_set():   # the stop's kill may have looked before it was added: it would outlive the daemon
        _kill(pid, start_token)
        return False
    return True


def ended(pid: int) -> None:
    """Process group `pid` of the schedule run on this thread is over."""
    run = current()
    if run is None or run.db is None:
        return
    run.groups.pop(pid, None)
    _forget(run.db, [pid])


def _forget(db: Any, pids: list[int]) -> None:
    with db.tx():
        groups = db.kv(GROUPS_KEY, {}) or {}
        if any(str(p) in groups for p in pids):
            db.set_kv(GROUPS_KEY, {k: v for k, v in groups.items() if int(k) not in pids})


def _kill(pid: int, start_token: str | None) -> bool:
    """SIGKILL group `pid`, unless its pid now belongs to another process (a new start token).
    A leader that is gone leaves the group's other members, which no other group can share the id of."""
    now = proc_start(pid)
    if now is not None and start_token is not None and now != start_token:
        return False
    try:
        os.killpg(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def kill(db: Any, run: Run) -> int:
    """Kill the process groups of `run` (past its timeout); returns how many were signalled. The run is
    marked stopping first, so the kill unwinds it on Stopped instead of returning -9 as its result."""
    run.stopping.set()
    groups = dict(run.groups)
    n = sum(_kill(pid, tok) for pid, tok in groups.items())
    _forget(db, list(groups))
    return n


def check_stopped() -> None:
    """Raise Stopped when the daemon stopped the schedule run on this thread (a stop or its timeout):
    what its group returned (killed, -9) is not the command's result."""
    run = current()
    if run is not None and run.stopping.is_set():
        raise Stopped()


def stop(db: Any, runs: Any) -> list[str]:
    """A daemon's stop: mark its `runs` stopping, then kill every recorded group (kill_all) and the ones
    only their run knows (track). The runs unwind on Stopped and record nothing; their schedules are
    due again at the next start."""
    runs = list(runs)
    for run in runs:
        run.stopping.set()
    recorded = {int(pid) for pid in db.kv(GROUPS_KEY, {}) or {}}
    lines = kill_all(db)
    for run in runs:
        lines += [f"killed process group {pid} of schedule {run.name}" for pid, tok in list(run.groups.items())
                  if pid not in recorded and _kill(pid, tok)]
    return lines


def kill_all(db: Any) -> list[str]:
    """Kill every recorded group: at a daemon's start (its predecessor's runs died with it) and at its
    stop. Returns one line per group killed, for the log."""
    groups = db.kv(GROUPS_KEY, {}) or {}
    lines = [f"killed process group {pid} left by schedule {rec.get('schedule')}"
             for pid, rec in groups.items() if _kill(int(pid), rec.get("start"))]
    if groups:
        _forget(db, [int(p) for p in groups])
    return lines


def run_group(cmd: str, *, cwd: str, timeout: float, env: dict | None,
              ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, shell=True) in a session of its own, kept in GROUPS_KEY while it runs. A
    timeout kills the whole group (the shell's children too) and raises TimeoutExpired; a daemon stop
    in the meantime raises Stopped. A run that exits outside `ok` (the caller's normal answers) while a
    script it names was rewritten runs once more in the time left (scriptwatch), its stderr starting
    with that note; a normal answer is never rerun, so its output is never dropped."""
    end = time.monotonic() + timeout
    said: list[str] = []
    out = scriptwatch.rerun(lambda: _run_group(cmd, cwd=cwd, timeout=max(end - time.monotonic(), 1), env=env),
                            lambda r: r.returncode not in ok, cmd, cwd, env, said.append,
                            again=lambda: end - time.monotonic() >= 1)
    if said:
        out.stderr = "".join(f"ttp: {line}\n" for line in said) + (out.stderr or "")
    return out


def _run_group(cmd: str, *, cwd: str, timeout: float, env: dict | None) -> subprocess.CompletedProcess:
    # stdin /dev/null: a command that reads it gets EOF, never the daemon's own stdin (an open pipe would
    # hold it to its timeout)
    proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=cwd, env=env, start_new_session=True)
    spawned(proc.pid)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:   # a child that left the group still holds the pipes
                pass
            raise
    finally:
        ended(proc.pid)
        check_stopped()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

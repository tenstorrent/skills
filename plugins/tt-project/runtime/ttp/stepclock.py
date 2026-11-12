# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One time budget for the daemon tick step running now, kept by every step and every child it waits on.

Daemon._run_step opens each step with begin(name, budget_s, slack_s) and closes it with end(). While a
step is open, on the thread that opened it:

- over() says it spent its budget: a step looping over items stops before the next one, keeps its place
  and resumes there next tick (Daemon._step_over).
- at budget + slack a step still running is stopped where it is: SIGALRM raises StepBudgetSpent in it
  (main thread only), so a pure Python loop or a blocking wait ends too. Inside critical() (a db
  transaction's commit or rollback, a git write, a child's spawn) the stop waits until the section ends.
  A caller that starts a detached child holds critical() from the spawn until it has recorded the child,
  so a stop never leaves one running untracked.
- every child it waits on (subprocess.run, call, check_output, Popen.wait and communicate) waits at
  most until then: no timeout, or a longer one, gets the time left. On expiry the child's whole tree is
  killed and StepBudgetSpent names the command. A wait inside critical() keeps its own timeout
  (CRITICAL_WAIT_S when it names none).

Every ssh started in an installed process gets ConnectTimeout and ServerAlive options unless it names
its own, and runs through the harness's ssh wrapper (connection reuse, sshmux.py) while ssh.reuse is
on. install() patches subprocess.Popen once per process (the daemon, at start); off a step, or on
another thread, a wait is left as it is.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from typing import Iterator

from . import sshmux

SSH_OPTS = ("-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4")
CRITICAL_WAIT_S = 120   # a wait inside critical() that names no timeout
MIN_WAIT_S = 0.5   # a wait past the deadline still gets this long, so a child that already ended is reaped


class StepBudgetSpent(BaseException):
    """A tick step ran past its budget and was stopped; `cmd` names the child it waited on, if any.
    A BaseException, so a step's own `except Exception` never swallows it."""

    def __init__(self, step: str, cmd: str = "") -> None:
        super().__init__(f"tick step {step} ran past its budget" + (f" waiting on {cmd}" if cmd else ""))
        self.step, self.cmd = step, cmd


class _State:
    step = ""
    soft: float | None = None
    hard: float | None = None
    thread: int | None = None
    depth = 0
    pending = fired = armed = False
    prev = None
    timer: tuple[float, float] | None = None   # another user's ITIMER_REAL left, and when: put back by end()


_s = _State()
_orig: dict = {}
# Children started in this process since spawns() last took them: {(step or thread name, program): n}.
# The daemon logs them per window (Daemon._log_spawns): how much process churn each step causes.
_spawned: dict[tuple[str, str], int] = {}
_spawn_lock = threading.Lock()


def _count_spawn(args) -> None:
    """Count one child by the step (or thread) starting it and its program. Never in Popen's way."""
    try:
        if isinstance(args, (str, bytes, os.PathLike)):
            prog = os.fsdecode(args).split(" ", 1)[0]
        else:
            prog = os.fsdecode(args[0]) if args else ""
        prog = os.path.basename(prog) or "?"
        who = _s.step if active() else threading.current_thread().name
        with _spawn_lock:
            _spawned[(who, prog)] = _spawned.get((who, prog), 0) + 1
    except Exception:   # an odd argv is Popen's to reject, not the counter's
        pass


def spawns() -> dict[tuple[str, str], int]:
    """The children started since the last call, {(step or thread, program): count}; resets the count."""
    global _spawned
    with _spawn_lock:
        got, _spawned = _spawned, {}
    return got


def begin(name: str, budget_s: float, slack_s: float) -> None:
    now = time.monotonic()
    _s.step, _s.soft, _s.hard, _s.thread = name, now + budget_s, now + budget_s + slack_s, threading.get_ident()
    _s.depth, _s.pending, _s.fired = 0, False, False
    _arm(budget_s + slack_s)


def end() -> None:
    _disarm()
    _s.step, _s.soft, _s.hard, _s.thread, _s.depth, _s.pending = "", None, None, None, 0, False


def active() -> bool:
    return _s.hard is not None and threading.get_ident() == _s.thread


def over() -> bool:
    return active() and time.monotonic() >= _s.soft


def left() -> float | None:
    """Seconds until the open step is stopped, None off a step."""
    return _s.hard - time.monotonic() if active() else None


def _arm(t: float) -> None:
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        return
    try:
        if not _s.armed:
            left_s = signal.getitimer(signal.ITIMER_REAL)[0]
            _s.timer = (left_s, time.monotonic()) if left_s > 0 else None
            _s.prev = signal.signal(signal.SIGALRM, _on_alarm)
        _s.armed = True
        signal.setitimer(signal.ITIMER_REAL, max(t, 0.001))
    except (ValueError, OSError):
        pass


def _disarm() -> None:
    if not _s.armed:
        return
    _s.armed = False
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, _s.prev if _s.prev is not None else signal.SIG_DFL)
        if _s.timer:
            signal.setitimer(signal.ITIMER_REAL, max(_s.timer[0] - (time.monotonic() - _s.timer[1]), 0.001))
    except (ValueError, OSError):
        pass


def _on_alarm(signum, frame) -> None:
    if not active() or _s.fired:
        return
    if _s.depth:   # a commit or a git write: stop once it ends (release)
        _s.pending = True
        return
    if frame is not None and frame.f_code in _HANDOFF:
        # Between a child's spawn and its bounded wait (_HANDOFF): a stop here would leave the child, or
        # its group, running with nothing to kill it whole. Stop a moment later, once the wait has it.
        _arm(0.001)
        return
    _s.fired = True
    raise StepBudgetSpent(_s.step)


def hold() -> None:
    if active():
        _s.depth += 1


def release() -> None:
    """End a hold(); a stop that came during it fires right after, outside the section."""
    if not active() or not _s.depth:
        return
    _s.depth -= 1
    if not _s.depth and _s.pending and not _s.fired:
        _s.pending = False
        _arm(0.001)


@contextlib.contextmanager
def critical() -> Iterator[None]:
    hold()
    try:
        yield
    finally:
        release()


def ssh_argv(argv):
    """`argv` with SSH_OPTS after `ssh`, for each option it does not name itself."""
    if not isinstance(argv, (list, tuple)) or not argv or os.path.basename(str(argv[0])) != "ssh":
        return argv
    named = " ".join(map(str, argv[1:])).lower()
    extra = [x for k, v in zip(SSH_OPTS[::2], SSH_OPTS[1::2]) if v.split("=")[0].lower() not in named
             for x in (k, v)]
    return type(argv)([argv[0], *extra, *argv[1:]]) if extra else argv


def _init(self, args, *a, **kw):
    _count_spawn(args)
    if not kw.get("shell"):
        args = sshmux.argv(ssh_argv(args), kw.get("env"))
    if not active() or _s.depth:
        return _orig["init"](self, args, *a, **kw)
    hold()   # a stop while the child starts would leave it running with no Popen to kill it by
    try:
        _orig["init"](self, args, *a, **kw)
    except BaseException:
        release()
        raise
    _s.depth -= 1
    if _s.pending and not _s.fired:   # the budget ran out while it started: stop the step, child and all
        _s.pending, _s.fired = False, True
        kill_tree(self)
        with contextlib.suppress(Exception):
            _orig["wait"](self, timeout=MIN_WAIT_S)
        for f in (self.stdin, self.stdout, self.stderr):
            if f:
                with contextlib.suppress(OSError):
                    f.close()
        raise StepBudgetSpent(_s.step, _cmd(self))


def _bounded(proc, call, timeout):
    if not active():
        return call(timeout)
    if _s.depth:   # a git write or a commit keeps its own timeout, never none
        return call(CRITICAL_WAIT_S if timeout is None else timeout)
    capped = True
    try:
        room = max(_s.hard - time.monotonic(), MIN_WAIT_S)
        capped = timeout is None or timeout > room
        return call(room if capped else timeout)
    except subprocess.TimeoutExpired:
        if not capped:
            raise
        kill_tree(proc)
        _s.fired = True
        raise StepBudgetSpent(_s.step, _cmd(proc)) from None
    except StepBudgetSpent as e:
        kill_tree(proc)
        e.cmd = e.cmd or _cmd(proc)
        raise


def _wait(self, timeout=None):
    return _bounded(self, lambda t: _orig["wait"](self, timeout=t), timeout)


def _communicate(self, input=None, timeout=None):
    return _bounded(self, lambda t: _orig["communicate"](self, input, timeout=t), timeout)


# The code a child passes through from its spawn to its bounded wait (run, call: Popen, __enter__, then
# wait or communicate): a stop is put off while one of these is the frame running (_on_alarm).
_HANDOFF = frozenset(c for c in (getattr(f, "__code__", None) for f in (
    subprocess.run, subprocess.call, subprocess.Popen.__enter__, _init, _wait, _communicate, _bounded)) if c)


def install() -> None:
    if _orig:
        return
    P = subprocess.Popen
    _orig.update(init=P.__init__, wait=P.wait, communicate=P.communicate)
    P.__init__, P.wait, P.communicate = _init, _wait, _communicate


def uninstall() -> None:
    if _orig:
        P = subprocess.Popen
        P.__init__, P.wait, P.communicate = _orig.pop("init"), _orig.pop("wait"), _orig.pop("communicate")


def _cmd(proc) -> str:
    a = proc.args
    return (a if isinstance(a, str) else " ".join(map(str, a)))[:200]


def _children() -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc") if os.path.isdir("/proc") else ():
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as fh:
                    ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            kids.setdefault(ppid, []).append(int(d))
    return kids


def kill_tree(proc) -> None:
    """SIGKILL `proc`, every descendant and, when it leads a group of its own, that whole group. The tree
    is stopped first, scan by scan until no new descendant shows, so none forks one past the kill."""
    if proc.returncode is not None:
        return
    tree: list[int] = []
    for _ in range(5):
        kids, todo, new = _children(), [proc.pid], []
        while todo:
            pid = todo.pop()
            if pid not in tree:
                new.append(pid)
            todo.extend(kids.get(pid, ()))
        for pid in new:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGSTOP)
        tree += new
        if not new:
            break
    try:
        if os.getpgid(proc.pid) == proc.pid != os.getpgrp():
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    for pid in reversed(tree):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

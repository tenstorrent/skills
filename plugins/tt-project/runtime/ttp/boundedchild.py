# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One bounded child process in a session of its own, never outliving its caller.

`run` starts a command in a new session (its own process group), waits for it within `timeout` and
returns a Result (rc, output, timed_out). Whatever way the call ends, the child's whole group, its
grandchildren included, is killed with SIGKILL:

- the timeout: the group is killed, then its output so far is collected for at most `grace` seconds;
- the caller gets SIGTERM or SIGHUP: the group is killed, the call waits for it at most `grace`
  seconds, puts the caller's earlier handlers back and lets the signal go on to them (the default
  action ends the process as the signal would have; a Python handler is called; an ignored signal
  is not hooked at all);
- an exception (KeyboardInterrupt, SystemExit included): the group is killed, then it is re-raised;
- a normal return: what the child left behind in its group is killed.

The caller's signal handlers are restored when the call returns. Called off the main thread (a pool
thread, say), Python cannot set signal handlers, so `run` hooks none there; it still kills the group
on the timeout, an exception and its return. The thread's caller then answers for signals itself
(a daemon's stop kills the groups it tracks: pass `on_start`).

Detached jobs meant to outlive their caller (ttp detach, push --detach, service and daemon spawns)
and daemon schedules (schedrun.run_group) do not use this. Any other own-session child in the runtime
is either on the static guard's list in the tests or marked `# own session: <why>`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

GRACE_S = 3.0   # the bounded wait for a killed group (and its output) before going on
HOOKED = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s is not None)


@dataclass
class Result:
    rc: int | None          # the child's exit code; None when it timed out, could not start or was stopped
    out: str | bytes        # its stdout (with stderr="stdout", both)
    err: str | bytes        # its stderr when stderr="pipe", else empty
    timed_out: bool = False
    error: str = ""         # why there is no rc: "could not start: ...", "timed out after N s", "stopped"


def run(argv: list[str] | str, timeout: float, *, shell: bool = False, cwd: str | None = None,
        env: dict | None = None, input: str | bytes | None = None, text: bool = True, stderr: str = "pipe",
        grace: float = GRACE_S, on_start: Callable[[int], bool] | None = None) -> Result:
    """Run `argv` in a session of its own within `timeout` seconds (see the module). stdin is `input`,
    else /dev/null; `stderr` is "pipe" (Result.err), "stdout" (merged into Result.out) or "devnull".
    `on_start(pid)` runs once the child started; when it returns False the group is killed at once and
    the Result says "stopped" (a schedule run that was stopped meanwhile)."""
    empty: str | bytes = "" if text else b""
    errs = {"pipe": subprocess.PIPE, "stdout": subprocess.STDOUT, "devnull": subprocess.DEVNULL}[stderr]
    box: list[subprocess.Popen] = []
    saved = _hook(box, grace)   # before the start: a signal in between still finds the child
    try:
        try:
            proc = subprocess.Popen(argv, shell=shell, cwd=cwd, env=env, text=text, stdout=subprocess.PIPE,
                                    stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                    stderr=errs, start_new_session=True)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return Result(None, empty, empty, error=f"could not start: {e}")
        box.append(proc)
        try:
            if on_start is not None and not on_start(proc.pid):
                stop(proc, grace)
                out, err = _drain(proc, grace, empty)
                return Result(None, out, err, error="stopped")
            try:
                out, err = proc.communicate(input, timeout=timeout)
            except subprocess.TimeoutExpired:
                stop(proc, grace)
                out, err = _drain(proc, grace, empty)
                return Result(None, out, err, timed_out=True, error=f"timed out after {timeout:g} s")
            return Result(proc.returncode, out or empty, err or empty)
        except BaseException:
            stop(proc, grace)
            _close(proc)
            raise
        finally:
            _killpg(proc.pid)   # a normal end: what the child left running in its group
    finally:
        _unhook(saved)


def stop(proc: subprocess.Popen, grace: float = GRACE_S) -> None:
    """SIGKILL `proc`'s process group, then wait at most `grace` seconds for the child to be reaped and
    the group to be gone. Never raises."""
    _killpg(proc.pid)
    end = time.monotonic() + grace
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError):
        pass
    while time.monotonic() < end and not _own_group(proc.pid):
        try:
            os.killpg(proc.pid, 0)
        except OSError:
            return
        time.sleep(0.02)


def _own_group(pgid) -> bool:
    """`pgid` names no group of a child here: 0 or 1, or the caller's own (a stand-in Popen, say)."""
    try:
        return not isinstance(pgid, int) or pgid <= 1 or pgid == os.getpgrp()
    except OSError:
        return True


def _killpg(pgid: int) -> None:
    if _own_group(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _drain(proc: subprocess.Popen, grace: float, empty):
    """The killed child's output so far; a child that left the group still holds the pipes: not waited for."""
    try:
        out, err = proc.communicate(timeout=grace)
        return out or empty, err or empty
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        _close(proc)
        got = getattr(e, "output", None) or empty
        if isinstance(got, bytes) and isinstance(empty, str):
            got = got.decode(errors="replace")
        return got, empty


def _close(proc: subprocess.Popen) -> None:
    for f in (proc.stdin, proc.stdout, proc.stderr):
        if f is not None:
            try:
                f.close()
            except OSError:
                pass


def _hook(box: list, grace: float) -> dict:
    """Install the SIGTERM/SIGHUP handlers of one call; {signal: earlier handler}; empty off the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    saved: dict = {}

    def handler(signum, frame):
        if box:
            stop(box[0], grace)
        _unhook(saved)
        prev = saved.get(signum)
        if callable(prev):
            prev(signum, frame)
            return
        signal.signal(signum, signal.SIG_DFL)   # the default action: the caller ends as the signal says
        os.kill(os.getpid(), signum)

    for sig in HOOKED:
        try:
            prev = signal.getsignal(sig)
            if prev is None or prev == signal.SIG_IGN:   # not set from Python, or ignored: the caller lives on
                continue
            signal.signal(sig, handler)
            saved[sig] = prev
        except (ValueError, OSError):   # not the main interpreter
            continue
    saved["handler"] = handler
    return saved


def _unhook(saved: dict) -> None:
    """Put back the earlier handlers, where this call's handler is still the one installed."""
    mine = saved.get("handler")
    for sig, prev in list(saved.items()):
        if sig == "handler":
            continue
        try:
            if signal.getsignal(sig) is mine:
                signal.signal(sig, prev)
        except (ValueError, OSError):
            pass

"""Shell probes started off the daemon's tick.

Starting a probe opens its output file (tempfile.TemporaryFile, on /tmp) and forks it. Either can
block for minutes when that file system stalls: one stalled open held a whole daemon tick for over
ten minutes (no worker started, no message handled). So the tick only queues a start here
(Pending) and returns at once; a few daemon threads do the open and the fork. A Pending answers
poll() like a subprocess.Popen: None while it starts or runs, its exit code after, or
"could not start" (its .error says why). A start still queued or stuck when its caller gives
up (stop()) never runs its probe, or kills it the moment it starts."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

SLOTS = 4   # probe starts at once, per daemon; more wait in the queue (the tick never does)
KILL_WAIT_S = 5   # how long a stop waits for a killed probe to be reaped

_jobs: queue.Queue = queue.Queue()
_started: list[threading.Thread] = []
_lock = threading.Lock()


def _work() -> None:
    while True:
        job = _jobs.get()
        try:
            job()
        except BaseException:   # a start never takes its thread down
            pass


def _submit(job: Callable[[], None]) -> None:
    with _lock:
        if len(_started) < SLOTS:
            # Daemon threads: one stuck in a stalled open never holds up the daemon's exit.
            t = threading.Thread(target=_work, name="ttp-probe-start", daemon=True)
            t.start()
            _started.append(t)
    _jobs.put(job)


TIMEOUTS_BROKEN = 3   # a probe killed on its timeout this many runs in a row is broken; fewer are "not yet"
_timeouts: dict[str, int] = {}   # probe -> its runs in a row killed on their timeout


def timed_out(probe: str):
    """The verdict of a run of `probe` killed on its timeout: 1 ("not yet": a stalled ssh or file
    system passes) until it has timed out TIMEOUTS_BROKEN runs in a row, then "timeout" (broken: a
    worker fixes the probe)."""
    n = _timeouts[probe] = _timeouts.get(probe, 0) + 1
    return "timeout" if n >= TIMEOUTS_BROKEN else 1


def answered(probe: str) -> None:
    """A run of `probe` ended by itself: its timeouts in a row start over."""
    _timeouts.pop(probe, None)


def kill(proc: Any) -> None:
    """End a probe's whole session (it runs in its own; ssh children too) and reap it, waiting at
    most KILL_WAIT_S: a process stuck in the kernel must not hold the tick either."""
    if isinstance(proc, Pending):
        proc.stop()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except (OSError, subprocess.SubprocessError):
        pass


class Pending:
    """A shell probe being started on the probe-start threads; Popen-like for its callers."""

    def __init__(self, cmd, *, cwd: str, env: dict | None, stdout: bool = True):
        self._cmd, self._cwd, self._env, self._stdout = cmd, cwd, env, stdout
        self._mu = threading.Lock()
        self._stopped = False
        self._taken = False
        self.proc: subprocess.Popen | None = None
        self.file = None
        self.error: str | None = None
        self.started = threading.Event()   # set once the start ended: started, failed or skipped
        _submit(self._start)

    def _start(self) -> None:
        try:
            with self._mu:
                if self._stopped:
                    return
            f = tempfile.TemporaryFile()
            try:
                proc = subprocess.Popen(self._cmd, shell=True, cwd=self._cwd, env=self._env,
                                        stdin=subprocess.DEVNULL, stdout=f if self._stdout else subprocess.DEVNULL,
                                        stderr=f, start_new_session=True)
            except BaseException:
                f.close()
                raise
            with self._mu:
                late = self._stopped
                if not late:
                    self.proc, self.file = proc, f
            if late:   # given up while it started: leave nothing running
                kill(proc)
                f.close()
        except Exception as e:
            self.error = str(e) or type(e).__name__
        finally:
            self.started.set()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None else None

    def poll(self):
        with self._mu:
            proc, err = self.proc, self.error
        if err is not None:
            return "could not start"
        return proc.poll() if proc is not None else None

    def wait(self, timeout: float | None = None):
        """Popen.wait: its exit code (or "could not start"), raising subprocess.TimeoutExpired after
        `timeout`. Never called by the tick, which only polls."""
        end = None if timeout is None else time.monotonic() + timeout
        if not self.started.wait(timeout):
            raise subprocess.TimeoutExpired(self._cmd, timeout)
        if self.proc is None:
            return self.poll()
        return self.proc.wait(None if end is None else max(0.0, end - time.monotonic()))

    def stop(self) -> None:
        """Give up on it: a start not yet done never runs the probe; a running one is killed."""
        with self._mu:
            self._stopped = True
            proc = self.proc
        if proc is not None:
            kill(proc)

    def take_file(self):
        """Its output file, once, for the caller to read and close; None when it never started (a
        start that ends after stop() closes its own file)."""
        with self._mu:
            if self._taken:
                return None
            self._taken = True
            return self.file

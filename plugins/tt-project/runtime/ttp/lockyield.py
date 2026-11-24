# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's optional yield probe for `ttp lock` (config `locks.yield_probe`): a quick, read-only
shell command asked while the locked command runs, so a project can give the resource way to
something that must not be disturbed. The lock names reach it in $TTP_LOCK_NAMES. Exit 0 means
yield: the command's process group is stopped (SIGTERM, then SIGKILL after 10 s), the locks are
released and `ttp lock` exits 75, so the task hands itself back as waiting. Exit 1 means keep going.
Any other answer, or none in time, is reported once and never stops the command. `locks.yield_exempt`
(globs of lock names) and `--no-yield` opt a call out (a rollout or heal take). Unset, nothing runs.
"""
from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import sys
import time

EVERY_S = 15.0        # config locks.yield_every_s: how often the probe is asked while the command runs
KILL_AFTER_S = 10.0    # a yielded command gets SIGTERM, then SIGKILL once this is up
PROBE_TIMEOUT_S = 10.0   # the most a probe answer may take; a slower one counts as broken
LINE_MAX = 200         # how much of the probe's answer is kept
MIN_INTERVAL_S = 0.1   # a smaller configured interval would spin
BUSY_RC = 75           # the exit of a busy or, now, yielded lock


def runner_kill_after() -> float:
    from .runner import KILL_AFTER_S   # the watchdog's grace before its SIGKILL
    return KILL_AFTER_S


def _locks(cfg: dict) -> dict:
    return cfg.get("locks") if isinstance(cfg.get("locks"), dict) else {}


def command(cfg: dict) -> str:
    """The configured probe, or "" when unset (nothing runs)."""
    v = _locks(cfg).get("yield_probe")
    return v.strip() if isinstance(v, str) else ""


def every_s(cfg: dict) -> float:
    try:
        v = float(_locks(cfg).get("yield_every_s"))
    except (TypeError, ValueError):
        return EVERY_S
    return v if v > 0 else EVERY_S


def armed(cfg: dict, names: list[str], root) -> "Probe | None":
    """The probe watching a `ttp lock` of `names`, or None when it is off: config unset, or every
    name matches a `locks.yield_exempt` glob."""
    cmd = command(cfg)
    globs = [g for g in (_locks(cfg).get("yield_exempt") or []) if isinstance(g, str)]
    if isinstance(_locks(cfg).get("yield_exempt"), str):   # a bare string would glob per character
        globs = []
    keep = [n for n in names if not any(fnmatch.fnmatch(n, g) for g in globs)]
    if not cmd or not keep:
        return None
    return Probe(cmd, every_s(cfg), keep, root)


class Probe:
    """One `ttp lock` command's probe: asks it on a timer while the command runs."""

    def __init__(self, cmd: str, interval: float, names: list[str], root) -> None:
        self.cmd, self.names, self.root = cmd, names, root
        self.interval = max(interval, MIN_INTERVAL_S)
        self.told = False   # a broken probe is reported once, however often it is asked
        self.dog = None     # (pipe, process) of the watchdog that ends the command's group if we die

    def guard(self, popen: subprocess.Popen) -> None:
        """Start the command's watchdog (runner's): it ends the command's group, which a run's kill
        of this process (a SIGKILL no handler sees) does not reach, once this process is gone."""
        from .runner import _start_watchdog
        w, proc = _start_watchdog(popen.pid)
        self.dog = (w, proc) if proc else None

    def _unguard(self, normal: bool) -> None:
        """The command is over: a word on the pipe tells the watchdog so (no word: it ends the group)."""
        if self.dog is None:
            return
        (w, proc), self.dog = self.dog, None
        try:
            if normal:
                os.write(w, b"1")
        except OSError:
            pass
        os.close(w)
        try:
            proc.wait(runner_kill_after() + 5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def says(self, why: str) -> str:
        return f"ttp lock: yielding {', '.join(self.names)}: {why}"

    def wait(self, popen: subprocess.Popen) -> int:
        """Wait for the command, asking the probe every interval; BUSY_RC once it yields."""
        ok = False
        try:
            rc = self._wait(popen)
            ok = True
            return rc
        finally:
            self._unguard(ok)

    def _wait(self, popen: subprocess.Popen) -> int:
        at = time.time() + self.interval   # the first ask comes one interval after the command starts
        while True:
            try:
                return popen.wait(timeout=max(0.0, at - time.time()))
            except subprocess.TimeoutExpired:
                pass
            why = self.ask()
            if why is not None:
                print(self.says(why), file=sys.stderr, flush=True)
                return self.stop(popen)
            at = time.time() + self.interval

    def ask(self) -> "str | None":
        """Run the probe: the one line saying why it yields, or None to keep the command running."""
        from . import boundedchild
        res = boundedchild.run(["/bin/sh", "-c", self.cmd], max(MIN_INTERVAL_S, min(self.interval, PROBE_TIMEOUT_S)),
                               cwd=str(self.root), env={**os.environ, "TTP_LOCK_NAMES": ",".join(self.names)},
                               text=False, stderr="stdout")
        if res.rc == 0:
            said = res.out.decode(errors="replace").strip().splitlines()
            line = next((ln.strip() for ln in said if ln.strip()), "")
            return line[:LINE_MAX] if line else "the project's yield probe asked for the resource"
        if res.rc != 1 and not self.told:   # a broken or slow probe never stops the command
            self.told = True
            how = f"exited {res.rc}" if res.rc is not None else (res.error or "gave no answer")
            print(f"ttp lock: yield probe {how}; ignored, the command runs on", file=sys.stderr, flush=True)
        return None

    def stop(self, popen: subprocess.Popen) -> int:
        """Stop the command's whole process group: SIGTERM, then SIGKILL after KILL_AFTER_S, and
        return BUSY_RC. A command that ended on its own (its rc, or a signal that is not ours)
        returns that instead: a finished command is never reported as a yield."""
        sent = False
        for sig, grace in ((signal.SIGTERM, KILL_AFTER_S), (signal.SIGKILL, None)):
            if popen.poll() is not None and not sent:
                return popen.returncode   # it ended on its own: a finished command is never a yield
            try:
                os.killpg(popen.pid, sig)   # the command runs in a group of its own for this
                sent = True
            except OSError:
                pass   # it left the group (setsid) or just ended between check and signal
            try:
                popen.wait(grace)
            except subprocess.TimeoutExpired:
                continue   # it ignores this signal: the SIGKILL comes next
            # Our signal ended it (or a shell that trapped it ended with 128+sig): a yield.
            return BUSY_RC if sent else (popen.returncode if popen.returncode is not None else BUSY_RC)


def run_whole(cmd: list, env: dict, probe: "Probe | None") -> int:
    """The exclusive whole-run path: `subprocess.call` without a probe, else the command in a group
    of its own watched by it."""
    if probe is None:
        return subprocess.call(cmd, env=env)
    # a run's kill of this process is passed on to the command's group (signals forwarded, a watchdog);
    # one that comes before the command runs is kept and passed on as it starts
    started: list = []
    pending: list = []

    def _forward(signum, _frame):
        if not started:
            pending.append(signum)
            return
        try:
            os.killpg(started[0].pid, signum)
        except OSError:
            pass
    old = {s: signal.signal(s, _forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        # own session: the probe stops the command's whole process group
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
        started.append(proc)
        probe.guard(proc)
        while pending:
            _forward(pending.pop(0), None)
        return probe.wait(proc)
    finally:
        for s, h in old.items():
            signal.signal(s, h)

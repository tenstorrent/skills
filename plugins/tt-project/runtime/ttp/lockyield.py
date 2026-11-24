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


def _locks(cfg: dict) -> dict:
    return cfg.get("locks") if isinstance(cfg.get("locks"), dict) else {}


def command(cfg: dict) -> str:
    """The configured probe, or "" when unset (nothing runs)."""
    v = _locks(cfg).get("yield_probe")
    return v.strip() if isinstance(v, str) else ""


def every_s(cfg: dict) -> float:
    try:
        v = float(_locks(cfg).get("yield_every_s") or EVERY_S)
    except (TypeError, ValueError):
        v = EVERY_S
    return v if v > 0 else EVERY_S


def armed(cfg: dict, names: list[str], root) -> "Probe | None":
    """The probe watching a `ttp lock` of `names`, or None when it is off: config unset, or every
    name matches a `locks.yield_exempt` glob."""
    cmd = command(cfg)
    globs = _locks(cfg).get("yield_exempt") or []
    keep = [n for n in names if not any(isinstance(g, str) and fnmatch.fnmatch(n, g) for g in globs)]
    if not cmd or not keep:
        return None
    return Probe(cmd, every_s(cfg), keep, root)


class Probe:
    """One `ttp lock` command's probe: asks it on a timer while the command runs."""

    def __init__(self, cmd: str, interval: float, names: list[str], root) -> None:
        self.cmd, self.names, self.root = cmd, names, root
        self.interval = max(interval, MIN_INTERVAL_S)
        self.told = False   # a broken probe is reported once, however often it is asked

    def wait(self, popen: subprocess.Popen) -> int:
        """Wait for the command, asking the probe every interval; BUSY_RC once it yields."""
        at = time.time() + self.interval   # the first ask comes one interval after the command starts
        while True:
            try:
                return popen.wait(timeout=max(0.0, at - time.time()))
            except subprocess.TimeoutExpired:
                pass
            why = self.ask()
            if why is not None:
                print(f"ttp lock: yielding {', '.join(self.names)}: {why}", file=sys.stderr, flush=True)
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
        for sig, grace in ((signal.SIGTERM, KILL_AFTER_S), (signal.SIGKILL, None)):
            if popen.poll() is not None:
                return popen.returncode   # it ended on its own: never report a finished command as yielded
            try:
                os.killpg(popen.pid, sig)   # the command runs in a group of its own for this
                sent = True
            except OSError:
                sent = False   # it left the group (setsid) or just ended between check and signal
            try:
                popen.wait(grace)
            except subprocess.TimeoutExpired:
                if sent:
                    continue   # it ignores SIGTERM: the SIGKILL comes next
                return BUSY_RC   # out of reach; the resource is still yielded
            # Our signal ended it (or a shell that trapped it ended with 128+sig): a yield.
            return BUSY_RC if sent else (popen.returncode if popen.returncode is not None else BUSY_RC)


def run_whole(cmd: list, env: dict, probe: "Probe | None") -> int:
    """The exclusive whole-run path: `subprocess.call` without a probe, else the command in a group
    of its own watched by it."""
    if probe is None:
        return subprocess.call(cmd, env=env)
    # own session: the probe stops the command's whole process group
    return probe.wait(subprocess.Popen(cmd, env=env, start_new_session=True))

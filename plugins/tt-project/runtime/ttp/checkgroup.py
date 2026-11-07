# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run a check in its process group and end that group if the process that started it dies first.

push starts a local check in a session of its own (its own process group) and kills what is left of the
group once the check ends (push._end_group). That cannot happen when the push itself is killed outright
(SIGKILL, or a `kill -- -<pgid>` of the push's group): the check's group is not the push's. This script
leads the check's group, starts the command in it and, should the push (`parent`) go away while the
command runs, kills the whole group, so no check outlives its push. On Linux the kernel tells it at once
(PR_SET_PDEATHSIG), so the check lets go of what it holds (its scratch lock) as the push dies; elsewhere
it notices within POLL_S.

Usage: python3 -I checkgroup.py <parent pid> -- <command...>. Stdlib only: it runs as a script, so a
check's environment (its PYTHONPATH) stays as it was. Its exit code is the command's; a command ended by
a signal ends this script by the same signal.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

POLL_S = 1.0   # how soon a dead parent is noticed where the kernel does not say
PR_SET_PDEATHSIG = 1


def _end_group(*_) -> None:
    os.killpg(0, signal.SIGKILL)


def _on_parent_death() -> None:
    """SIGTERM this process when its parent dies (Linux), and end the group on it."""
    signal.signal(signal.SIGTERM, _end_group)
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):   # no prctl: the poll below notices
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: checkgroup.py <parent pid> -- <command...>", file=sys.stderr)
        return 2
    parent = int(argv[0])
    _on_parent_death()
    if os.getppid() != parent:   # it died before the hook was set
        _end_group()
    try:
        proc = subprocess.Popen(argv[2:], close_fds=False)   # the check's inherited fds (checkdirs) go on
    except OSError as e:
        print(f"checkgroup: could not start {argv[2]}: {e}", file=sys.stderr)
        return 127
    while True:
        try:
            rc = proc.wait(timeout=POLL_S)
            break
        except subprocess.TimeoutExpired:
            if os.getppid() != parent:   # the push is gone: its check goes with it
                _end_group()
    if rc < 0:
        signal.signal(-rc, signal.SIG_DFL)
        os.kill(os.getpid(), -rc)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

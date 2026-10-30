# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The step between checks.wrap_cmd and a command with checks.wrap_lane "after" (checklane.lane_argv).

The wrapper runs this step once it holds its own lock; the step takes the project's lane of that wrapper
lock (testslots.suite_hold with `lock`), runs the command and frees the lane when the command ends. So a
`ttp checks` run queued on the wrapper's lock holds no lane, and the lane is always taken after the
wrapper's lock, never before it. The lane's lock belongs to this process's open file: it ends with the
process, a crash or kill too. Waiting for the lane is bounded (--wait-s): past it the command runs
without the lane, as the wrapper's own lock already serializes it. Signals the step gets go on to the
command; its exit code is the command's (128 + the signal for one a signal ended)."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys


def main(argv: list[str]) -> int:
    from . import checkcap, testslots
    ap = argparse.ArgumentParser(prog="ttp-wrap-lane")
    ap.add_argument("--project", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--wait-s", type=float, default=300.0)
    ap.add_argument("--who", default="")
    ap.add_argument("--priority", type=int, default=None)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        print("ttp checks: the wrapper lane step got no command", file=sys.stderr)
        return 2

    def tell(line: str) -> None:
        print(f"ttp checks: {line}", file=sys.stderr, flush=True)

    # The run's own grants mark a nested hold, which suite_hold answers at once without a lane: this hold
    # is the run's lane, so it asks without them; the command still gets them.
    nested = {k: os.environ.pop(k) for k in (testslots.HELD_ENV, checkcap.HELD_ENV) if k in os.environ}
    grant = None
    try:
        grant = testslots.suite_hold(a.project or None, tell, max(0.0, a.wait_s),
                                     who=f"{a.who}, behind its wrapper" if a.who else "behind its wrapper",
                                     priority=a.priority, what="checks behind checks.wrap_cmd", lock=a.key,
                                     name=a.name)
    except Exception as e:   # noqa: BLE001 - the lane is a convenience here: the wrapper's lock serializes
        tell(f"could not take the wrapper lock's lane ({type(e).__name__}: {e}); running without it")
    finally:
        os.environ.update(nested)
    if grant is None:
        tell(f"the wrapper lock's lane stayed busy for {a.wait_s:g}s; running without it, behind the wrapper's lock")
    try:
        child = subprocess.Popen(cmd)
    except OSError as e:
        if grant is not None:
            grant.close()
        tell(f"could not start {cmd[0]!r}: {e}")
        return 127

    def forward(sig, _frame):
        try:
            child.send_signal(sig)
        except OSError:
            pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    try:
        rc = child.wait()
    finally:
        if grant is not None:
            grant.close()
    return 128 - rc if rc < 0 else rc

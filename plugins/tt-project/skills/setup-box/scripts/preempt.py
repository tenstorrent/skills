"""setup-box: let a device job on a tt-device-mcp broker step aside cleanly (`device-runs.md`).

A broker job submitted with `--preemptable -t <s>` is never stopped for time while nobody waits.
Once it is past its limit with another job queued, it has one minute to end itself; then its process
tree gets SIGTERM, and SIGKILL ten seconds later. Exit 0 on SIGTERM skips the device check, a SIGKILL
means a device reset. This helper makes a job end cleanly and early. Standard library only, Python
3.8+, Linux and macOS; copy it to a box with `box.py copy`.

In a Python job:    import preempt; preempt.exit_on_sigterm(close_device)
                    ... if preempt.should_yield(started, limit_s): save a checkpoint;
                    preempt.mark_yielded(); break
Around a child:     python3 preempt.py run -- <program> [args]   (forwards SIGTERM, exits its code)
pytest:             PYTHONPATH=<scripts dir> python3 -m pytest -p preempt ...
                    (SIGTERM ends the session after fixture teardown, exit 0)
From a shell:       python3 preempt.py waiting [--job <id>]   exit 0 while another job is queued
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

MAX_LIMIT_S = 1500        # the broker's hard ceiling on -t
GRACE_S = 60              # past the limit with a waiter: a minute to end itself, no signal sent
KILL_GRACE_S = 10         # SIGTERM, then SIGKILL this much later
YIELD_FILE_ENV = "TTP_DEVQ_YIELD_FILE"   # where mark_yielded writes (a devq runner sets it)
YIELD_FILE = ".ttp-devq-yielded"         # else this file in the current folder
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def exit_on_sigterm(cleanup: Optional[Callable[[], None]] = None) -> None:
    """On SIGTERM run `cleanup` (close the device), then exit 0: the broker skips its device check.
    The exit unwinds `finally` blocks too. A long native call delays it: stop at step boundaries
    with should_yield instead of relying on the signal."""
    def _handler(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # one clean stop; SIGKILL follows anyway
        try:
            if cleanup is not None:
                cleanup()
        finally:
            sys.stdout.flush()
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handler)


def run_child(argv: List[str], **popen_kwargs) -> int:
    """Run `argv`, forwarding SIGTERM to it, and wait for it to end. Returns its exit code, or
    128 + the signal that ended it. Never subprocess.run with a timeout: it kills the child."""
    child = subprocess.Popen(argv, **popen_kwargs)

    def _forward(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
    old = signal.signal(signal.SIGTERM, _forward)
    try:
        while True:
            try:
                rc = child.wait()
                break
            except InterruptedError:
                continue
    finally:
        signal.signal(signal.SIGTERM, old)
    return 128 - rc if rc < 0 else rc


def broker_status(timeout: float = 20) -> str:
    """`tt-device-mcp status` text, or "" when the broker CLI is missing or does not answer."""
    try:
        out = subprocess.run(["tt-device-mcp", "status"], capture_output=True, text=True,
                             timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def queued_ids(status_text: str) -> List[str]:
    """The job ids in the QUEUED section of `tt-device-mcp status` text (ids may be shortened)."""
    ids, inside = [], False
    for line in _ANSI.sub("", status_text).splitlines():
        word = line.strip()
        if not inside:
            inside = word == "QUEUED"
            continue
        if not word or not line.startswith(" "):
            break   # the blank line or the next header ends the section
        parts = word.split()
        if parts[0] == "#" or word.startswith("("):
            continue   # the column header, or "(none)"
        if len(parts) >= 2 and parts[0].isdigit():
            ids.append(parts[1])
    return ids


def others_waiting(job_id: str = "", status_text: Optional[str] = None) -> bool:
    """True when a job other than `job_id` waits in the broker's queue."""
    text = broker_status() if status_text is None else status_text
    me = job_id.strip()
    return any(not (me and (me.startswith(i) or i.startswith(me))) for i in queued_ids(text))


def should_yield(started: float, limit_s: float, job_id: str = "", margin_s: float = 0,
                 status_text: Optional[str] = None, now: Optional[float] = None) -> bool:
    """Past `limit_s - margin_s` since `started` (time.time()) with another job waiting: stop at
    this step boundary, inside the broker's grace minute. Checks the queue only once past it."""
    elapsed = (time.time() if now is None else now) - started
    if elapsed < max(0.0, limit_s - margin_s):
        return False
    return others_waiting(job_id, status_text)


def mark_yielded(path: str = "") -> Path:
    """Record that the job stopped early to let a waiter in, so a devq runner requeues it.
    Write the checkpoint first; the rerun sees TTP_DEVQ_PREEMPTS and resumes from it."""
    target = Path(path or os.environ.get(YIELD_FILE_ENV) or YIELD_FILE)
    target.write_text(f"{time.time():.0f}\n")
    return target


def preempts() -> int:
    """How many times a devq runner has rerun this job after a preempt or a yield."""
    try:
        return max(0, int(os.environ.get("TTP_DEVQ_PREEMPTS", "0")))
    except ValueError:
        return 0


def pytest_configure(config) -> None:
    """`-p preempt`: SIGTERM ends the pytest session (fixtures torn down) with exit 0."""
    import pytest

    def _handler(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pytest.exit("preempted: SIGTERM from the device broker", returncode=0)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: List[str]) -> int:
    if argv[:1] == ["run"]:
        rest = argv[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        if not rest:
            print("usage: preempt.py run -- <program> [args]", file=sys.stderr)
            return 2
        return run_child(rest)
    if argv[:1] == ["waiting"]:
        job = argv[2] if argv[1:2] == ["--job"] and len(argv) > 2 else ""
        return 0 if others_waiting(job) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

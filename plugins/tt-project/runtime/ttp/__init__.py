# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""tt-project runtime: a per-project daemon, CLI and web app (standard library only, Python 3.9+)."""
__version__ = "0.2.1659"


def _hold_use() -> None:
    """Note this process on the runtime hold copy it runs from, so a later hold keeps the copy whole
    while it still imports from it. A child that gets the copy through PYTHONPATH never sees the
    launcher, which records the processes it starts itself."""
    import os
    hold = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <git dir>/<hold>/<this pkg>
    d = os.path.dirname(hold)   # a hold copy sits in the harness's git dir: .git itself, or a
    while d and d != os.path.dirname(d) and os.path.basename(d) != ".git":
        d = os.path.dirname(d)   # linked worktree's git dir, somewhere under its .git
    if os.path.basename(d) == ".git" and os.path.basename(hold).startswith("ttp-runtime-hold"):
        try:
            marks = os.path.join(hold, ".pids")          # project.HOLD_PIDS
            with open(marks, "a") as f:
                f.write("%d\n" % os.getpid())
        except OSError:
            pass


def poll_s(default: float) -> float:
    """Seconds a wait loop sleeps between checks. The test suite sets TTP_TEST_POLL_S to shorten
    every such loop; unset (always, in real use) it is the loop's own default."""
    import os
    return float(os.environ.get("TTP_TEST_POLL_S") or default)


_hold_use()

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""tt-project runtime: a per-project daemon, CLI and web app (standard library only, Python 3.9+)."""
__version__ = "0.2.1516"


def _hold_use() -> None:
    """Note this process on the runtime hold copy it runs from (project.note_hold_use)."""
    from pathlib import Path
    try:
        from .project import note_hold_use
        note_hold_use(Path(__file__).resolve().parent.parent)
    except Exception:
        pass    # never a reason a ttp process fails to start


def poll_s(default: float) -> float:
    """Seconds a wait loop sleeps between checks. The test suite sets TTP_TEST_POLL_S to shorten
    every such loop; unset (always, in real use) it is the loop's own default."""
    import os
    return float(os.environ.get("TTP_TEST_POLL_S") or default)


_hold_use()

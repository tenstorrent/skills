# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""tt-project runtime: a per-project daemon, CLI and web app (standard library only, Python 3.9+)."""
__version__ = "0.2.229"


def poll_s(default: float) -> float:
    """Seconds a wait loop sleeps between checks. The test suite sets TTP_TEST_POLL_S to shorten
    every such loop; unset (always, in real use) it is the loop's own default."""
    import os
    return float(os.environ.get("TTP_TEST_POLL_S") or default)

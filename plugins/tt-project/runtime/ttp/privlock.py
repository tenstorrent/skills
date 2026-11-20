# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""NoNewPrivs of a process."""
from __future__ import annotations

from pathlib import Path


def fenced(pid: int | str = "self") -> bool | None:
    """Whether process `pid` runs with no_new_privs set (NoNewPrivs in /proc/<pid>/status); None
    where that cannot be read (off Linux, a pid gone or out of sight)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NoNewPrivs:"):
                return line.split(":", 1)[1].strip() == "1"
    except (OSError, ValueError):
        return None
    return None

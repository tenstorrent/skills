# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Small helpers many modules share. Standard library only, so any module can import this one."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterator


def task_labels(task: Any) -> list[str]:
    """A task's string labels. `task` is a task (its "labels": JSON text when stored, a list when
    pending), that labels value itself, or None; [] when there are none or they do not parse."""
    raw = task.get("labels") if hasattr(task, "get") else task
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except ValueError:
            return []
    return [x for x in raw if isinstance(x, str)] if isinstance(raw, (list, tuple)) else []


def loads_dict(raw: Any) -> dict:
    """JSON text as a dict; {} when it is empty, not JSON or not an object."""
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def read_dict(path: Path | str) -> dict:
    """A JSON file as a dict; {} when it is missing, unreadable, not JSON or not an object."""
    try:
        return loads_dict(Path(path).read_text())
    except OSError:
        return {}


@contextlib.contextmanager
def flocked(path: Path, mode: int = 0o600) -> Iterator[None]:
    """Hold an exclusive flock on `path` until the block ends, waiting for it. The file (made with
    `mode`, less the umask) and its folder are created when missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def num_in(v: Any, lo: float, hi: float, default: float) -> float:
    """`v` as a float when it is a number (not a bool) within [lo, hi], else `default`."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default


def as_float(v: Any, default: float) -> float:
    """float(v), or `default` when `v` is no number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def as_int(v: Any) -> int | None:
    """int(v), or None when `v` is no integer."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

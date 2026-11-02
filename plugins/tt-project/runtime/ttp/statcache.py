# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""JSON files parsed once per change, not once per read.

An idle daemon tick read the same few files (machines.json, project.json, settings.json) a dozen
times each. load_json(path) keeps a file's parsed value under its stat (inode, size, mtime and ctime
in ns) and hands each caller its own copy, so a caller may change what it got. A file written within
the last RACY_S seconds is read every time: a filesystem with coarse timestamps could give two quick
writes of one size the same stat (git's "racy" rule). A file that cannot be read or parsed raises as
json.loads(path.read_text()) does, and nothing is kept for it."""

from __future__ import annotations

import json
import os
import pickle
import threading
import time
from pathlib import Path
from typing import Any

RACY_S = 2.0
MAX_FILES = 256

_lock = threading.Lock()
_kept: dict[str, tuple[tuple, bytes]] = {}


def _stamp(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def load_json(path: Path | str) -> Any:
    """json.loads(Path(path).read_text()), read again only once the file changed."""
    key = os.fspath(path)
    st = os.stat(key)
    stamp = _stamp(st)
    with _lock:
        hit = _kept.get(key)
    if hit and hit[0] == stamp:
        return pickle.loads(hit[1])
    data = json.loads(Path(key).read_text())
    if time.time() - max(st.st_mtime, st.st_ctime) > RACY_S and _stamp(os.stat(key)) == stamp:
        blob = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
        with _lock:
            if len(_kept) >= MAX_FILES:
                _kept.clear()
            _kept[key] = (stamp, blob)
    return data


def clear() -> None:
    with _lock:
        _kept.clear()

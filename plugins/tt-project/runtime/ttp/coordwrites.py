# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a coordinator turn changed files: a model-free detector in place of a sandbox.

Coordinator turns run unsandboxed by default (no Codex `-s read-only` unless providers.<name>.write_fence
is true); the daemon applies their output only through its action gates. The daemon takes a cheap
snapshot of the project's harness folder and its repo checkout before each coordinator turn and again
when the turn ends, before it applies the turn's output, and logs a warning when they differ. A folder
that is the top of a git work tree is read as `git status --porcelain`, any other as the mtimes of its
top-level entries. Accepted gap: a change another run makes in the same window reads as the turn's."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import gitmemo
from .project import durable_write

FILE = "coord-writes.json"   # in the run dir: the snapshot taken before the turn
GIT_S = 10.0


def _state(folder: Path) -> str | None:
    """One folder's snapshot; None when it cannot be read (no compare then)."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        top = gitmemo.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True,
                          text=True, timeout=GIT_S, read=True, stdin=subprocess.DEVNULL, env=env)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == folder.resolve():
            st = gitmemo.run(["git", "-C", str(folder), "status", "--porcelain"], capture_output=True, text=True,
                             timeout=GIT_S, read=True, stdin=subprocess.DEVNULL, env=env)
            return st.stdout if st.returncode == 0 else None
        return json.dumps(sorted((e.name, e.stat(follow_symlinks=False).st_mtime_ns) for e in os.scandir(folder)))
    except (OSError, subprocess.SubprocessError):
        return None


def snapshot(folders: list[Path]) -> dict:
    """{folder: its state} for each folder that exists."""
    return {str(f): _state(f) for f in folders if f.is_dir()}


def save(run_dir: Path, folders: list[Path]) -> None:
    durable_write(run_dir / FILE, json.dumps(snapshot(folders)))


def changed(run_dir: Path) -> list[str]:
    """The folders whose state differs from the snapshot saved in `run_dir`; [] without one."""
    try:
        before = json.loads((run_dir / FILE).read_text())
    except (OSError, ValueError):
        return []
    now = snapshot([Path(f) for f in before])
    return [f for f, was in before.items() if was is not None and now.get(f) is not None and now[f] != was]

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which project a process belongs to: tt-project's identity for its own processes.

Every worker, daemon-run command, `ttp detach` job and `ttp checks` command carries TTP_PROJECT in its
environment: the full path of its project's harness folder (<project root>/tt-project), next to
TTP_TASK, TTP_TASK_ID, TTP_RUN_ID and TTP_RUN_DIR. Task and run ids are numbered per project, so they
overlap across projects that share a host, and every project's folder has the same name: neither
proves whose a process is. Code that signals a process it chose by a task or run id, a lock label or
a checks lane signals it only once `owns` holds: its TTP_PROJECT is this project's folder, or its
TTP_RUN_DIR lies inside it. Linux only (/proc); elsewhere nothing is provably owned."""
from __future__ import annotations

import os
from pathlib import Path

FOLDER = "tt-project"   # project.FOLDER


def base_of(path: str | os.PathLike | None) -> Path | None:
    """The resolved project folder a TTP_PROJECT value names (a project root or its tt-project folder),
    None when empty."""
    if not path:
        return None
    try:
        p = Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None
    return p if p.name == FOLDER else p / FOLDER


def environ(pid: int) -> dict[str, str] | None:
    """`pid`'s environment, None when it cannot be read (gone, another user's, no /proc)."""
    try:
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
    except (OSError, ValueError):
        return None
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


def owns_env(base, env: dict | None) -> bool:
    """Whether environment `env` is of a process of the project at `base` (see the module doc)."""
    mine = base_of(base)
    if mine is None or not env:
        return False
    if base_of(env.get("TTP_PROJECT")) == mine:
        return True
    run = env.get("TTP_RUN_DIR")
    if not run:
        return False
    try:
        return Path(os.path.realpath(run)).is_relative_to(mine)
    except (OSError, ValueError):
        return False


def foreign_env(base, env: dict | None) -> bool:
    """Whether `env` names another project than `base` (a TTP_PROJECT that is not this one's)."""
    there = base_of((env or {}).get("TTP_PROJECT"))
    return there is not None and there != base_of(base)


def owns(base, pid: int) -> bool:
    """Whether live process `pid` is one of the project at `base`'s own (owns_env on its environment)."""
    return owns_env(base, environ(pid))

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Resources shared by all of the user's projects on this machine.

A resource is project-scoped by default: its slot files live in the project's state/locks and a
pause of it is kept in the project's database, so other projects neither wait for it nor see the
pause. A resource is declared shared in the project's `shared_resources` config (a list of names)
or in the user's machines list (`ttp machines add <alias> --shared [names]`, the alias itself when
no names are given). A shared resource keeps its slot files, its reservation and its pause under
~/.tt-project/locks/<resource>/, so `ttp lock` and `exclusive:` tasks of every project that names
it take turns on the same slots, and a pause set from one project holds in all of them. Holders
are labelled with their project. Each project's `resources` config still gives the slot count;
projects sharing a resource should agree on it.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from . import locks, project

PAUSE_FILE = "paused.json"


def root() -> Path:
    return project.HOME_DIR / "locks"


def names(cfg: dict) -> set[str]:
    """The resources this project shares with the user's other projects."""
    from . import machines
    out = {str(x) for x in cfg.get("shared_resources") or [] if isinstance(x, str) and x}
    for alias, m in machines.load().items():
        got = m.get("shared")
        if got is True:
            out.add(alias)
        elif isinstance(got, list):
            out |= {str(x) for x in got if isinstance(x, str) and x}
    return out


def is_shared(p, res: str, cfg: dict | None = None) -> bool:
    return res in names(cfg if cfg is not None else p.config())


def locks_dir(p, res: str, cfg: dict | None = None) -> Path:
    """Where the resource's slot files and reservation live."""
    return root() / res if is_shared(p, res, cfg) else p.state / "locks"


def holder(p, res: str, who: str, cfg: dict | None = None) -> str:
    """A holder label: on a shared resource it names the project too, so `task #3` of one project
    is told apart from `task #3` of another."""
    return f"{p.name} {who}" if is_shared(p, res, cfg) else who


def held(p, cfg: dict | None = None) -> list[str]:
    """Who holds each of the project's resources right now, shared ones included."""
    out = locks.held(p.state / "locks")
    for res in sorted(names(cfg if cfg is not None else p.config())):
        out += locks.held(root() / res)
    return out


def paused(cfg: dict) -> dict[str, dict]:
    """Pauses of this project's shared resources: {"reason", "since", "by", "project", "shared"}."""
    out = {}
    for res in sorted(names(cfg)):
        try:
            v = json.loads((root() / res / PAUSE_FILE).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(v, dict):
            out[res] = {**v, "shared": True}
    return out


def paused_for_db(db_path: str | Path) -> dict[str, dict]:
    """`paused` for the project whose database is at db_path (<project>/state/project.db)."""
    try:
        return paused(project.Project(Path(db_path).parent.parent).config())
    except Exception:   # a broken config or list must not hide the project's own pauses
        return {}


def update_pause(res: str, change) -> tuple[dict | None, dict | None]:
    """Set the shared resource's pause to change(current pause or None); None lifts it. Returns
    (before, after). A guard file keeps two projects changing it at once from losing a change."""
    d = root() / res
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".pause.guard", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            was = json.loads((d / PAUSE_FILE).read_text())
        except (OSError, ValueError):
            was = None
        was = was if isinstance(was, dict) else None
        new = change(was)
        if new is None:
            try:
                (d / PAUSE_FILE).unlink()
            except FileNotFoundError:
                pass
        else:
            tmp = d / f"{PAUSE_FILE}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(new))
            os.replace(tmp, d / PAUSE_FILE)
    return was, new

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The folders a run would otherwise hunt for, resolved by the same functions the code uses: the
Known paths block every worker, review and coordinator prompt carries, and `ttp paths`. A run that
searched / or the home folder for one of them (the push lock) popped one macOS privacy dialog per
protected folder, so the block leads with the rule against such searches."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import checkcap, project, runnerenv, shared

HEADER = "KNOWN PATHS (never search / or the home folder)"
RULE = ("Never search from /, from ~ or across the home folder (find, du, grep -r, rg, mdfind, os.walk, "
        "rglob, glob **): slow, and on macOS each walk pops a privacy dialog per protected folder. "
        "Search only the project tree, ~/.tt-project and the folders below or the ones your task names; "
        "`ttp paths` prints these.")


def walk_root_ok(d: str | Path) -> bool:
    """Whether a recursive walk may start at `d`: never / (a drive's root) or a home folder, or above one."""
    d, home = Path(d).absolute(), Path(os.path.expanduser("~")).absolute()
    return not (d == Path(d.anchor) or d == home or d in home.parents)


def glob_root_ok(pattern: str) -> bool:
    """Whether a glob pattern (~ expanded) walks only below a folder walk_root_ok allows: its fixed
    prefix (the parts before the first wildcard) passes, or it only lists that one folder (`~/*.log`)."""
    parts = Path(os.path.expanduser(pattern)).parts
    wild = [i for i, x in enumerate(parts) if any(c in x for c in "*?[")]
    if not wild or ("**" not in pattern and wild == [len(parts) - 1]):
        return True
    return walk_root_ok(Path(*parts[:wild[0]]) if wild[0] else Path.cwd())


def push_lock(p) -> Path | None:
    """The lock file of pushes to `delivery.push_branch` (push.lock_paths), or None when no push branch
    is set or git cannot name the remote."""
    from . import push
    try:
        return push.lock_paths(p, *push.target(p, p.root))[0]
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def known(p, run_dir: str | Path | None = None) -> list[tuple[str, Path]]:
    """(label, absolute path) of each known folder; `run_dir` (default $TTP_RUN_DIR) adds the run's own."""
    from .daemon import PRECHECKS_DIR
    from .push import DETACHED
    out = [("project root", p.root), ("tt-project folder", p.base), ("harness", p.harness), ("state", p.state), ("runs", p.runs),
           ("prechecks", p.state / PRECHECKS_DIR), ("pushes", p.state / DETACHED),
           ("worktrees", p.worktrees), ("logs", p.logs), ("locks", p.state / "locks")]
    if lk := push_lock(p):
        out.append(("push lock", lk))
    out += [("shared locks", shared.root()), ("checks queue", checkcap.slot_dir()), ("checks env", runnerenv.root()),
            ("tt-project home", project.HOME_DIR)]
    run_dir = run_dir if run_dir is not None else os.environ.get("TTP_RUN_DIR")
    if run_dir:
        out.append(("this run", Path(run_dir)))
    return [(k, Path(v).absolute()) for k, v in out]


def block(p) -> str:
    """The prompts' Known paths block: the rule, then the project root and each folder of known(), those
    under the tt-project folder or ~/.tt-project grouped on that base's line (relative), so every run's
    prompt stays short. No run folder: the block is shared by every run; a worker's header names its own."""
    bases = {p.base.absolute(): [], project.HOME_DIR.absolute(): []}
    lines = []
    for k, v in known(p, run_dir=""):
        base = next((b for b in bases if v != b and b in v.parents), None)
        if base is None and v not in bases:
            lines.append(f"- {k}: {v}")
        elif base is not None:
            bases[base].append(f"{v.relative_to(base)}/ ({k})" if k != v.name else f"{v.relative_to(base)}/")
    for (b, rel), name in zip(bases.items(), ("tt-project folder", "tt-project home")):
        lines.append(f"- {name}: {b}" + (f"; in it {', '.join(rel)}" if rel else ""))
    return f"# {HEADER}\n{RULE}\n" + "\n".join(lines) + "\n"

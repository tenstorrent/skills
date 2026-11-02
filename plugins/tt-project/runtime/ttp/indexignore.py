# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keeps agent CLIs' start-up file index out of a project's bulky folders.

A Claude Code run counts the files under its working directory when it starts (`rg --no-config
--files --hidden <cwd>`, cut off after a few seconds). Started in a project root holding a large
results tree, or in the harness folder with its run folders, that walk takes many cores for every
worker and coordinator turn. rg honours a `.ignore` file even with --no-config and outside a git
repository (where the harness folder's own `.gitignore` is not read), so the daemon keeps a marked
block in the project root's `.ignore` listing those folders, anchored at the root.

What it changes: an rg walk that starts above a listed folder skips it (the start-up count, a
Grep with no path). A walk started inside one (a code task's worktree), a search given the folder's
path and git are unaffected; Claude's Glob reads ignored files by default. A root `.ignore` the
project tracks in git and that has no block is left alone; one the daemon creates in a git root is
listed in the repository's local exclude file (never in a tracked `.gitignore`), so it shows in no
`git status`."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import gitmemo
from .project import FOLDER, Project, durable_write

NAME = ".ignore"
BEGIN = "# >>> tt-project: folders kept out of agent CLIs' start-up file index (managed; edits are replaced)"
END = "# <<< tt-project"
# Anchored at the project root: a nested folder of the same name (src/results/) is still searched.
FOLDERS = (f"{FOLDER}/state/", f"{FOLDER}/worktrees/", "results/", "tmp/", "runs/", "worktrees/", ".venv/", "venv/")
GIT_S = 10


def block() -> str:
    return "\n".join([BEGIN, "# Search one of these by giving its path (rg PATTERN results/).",
                      *(f"/{d}" for d in FOLDERS), END]) + "\n"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return gitmemo.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=GIT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _tracked(root: Path) -> bool:
    r = _git(root, "ls-files", "--error-unmatch", "--", NAME)
    return bool(r and r.returncode == 0)


def _exclude_locally(root: Path) -> None:
    """List /.ignore in the repository's info/exclude when the root is a git work tree's top."""
    r = _git(root, "rev-parse", "--show-toplevel")
    if not r or r.returncode != 0 or Path(r.stdout.strip()).resolve() != root.resolve():
        return
    r = _git(root, "rev-parse", "--git-path", "info/exclude")
    if not r or r.returncode != 0 or not r.stdout.strip():
        return
    path = Path(r.stdout.strip())
    path = path if path.is_absolute() else root / path
    text = path.read_text() if path.is_file() else ""
    if f"/{NAME}" in text.splitlines():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    durable_write(path, text + ("" if not text or text.endswith("\n") else "\n") + f"/{NAME}\n")


def ensure(p: Project) -> str:
    """Write or refresh the block; returns what it did ("" when nothing changed). Never raises for a
    file it cannot read or write: no project start or daemon start waits on it."""
    try:
        return _ensure(p)
    except (OSError, ValueError) as e:
        return f"left {p.root / NAME} as it is: {e}"


def _ensure(p: Project) -> str:
    path = p.root / NAME
    if p.root.resolve() in (Path.home().resolve(), Path("/")):   # every rg search under it would skip them
        return ""
    if path.is_symlink() or (path.exists() and not path.is_file()):   # the project's own arrangement
        return ""
    text = path.read_text() if path.exists() else None
    if text is None:
        durable_write(path, block())
        _exclude_locally(p.root)
        return f"wrote {path}"
    lines = text.splitlines(keepends=True)
    if BEGIN + "\n" in lines and END + "\n" in lines[lines.index(BEGIN + "\n"):]:
        start = lines.index(BEGIN + "\n")
        end = lines.index(END + "\n", start) + 1
        new = "".join(lines[:start]) + block() + "".join(lines[end:])
        if new == text:
            return ""
        durable_write(path, new)
        return f"refreshed the tt-project block in {path}"
    if _tracked(p.root):   # the project's own, under version control: never dirty it
        return ""
    durable_write(path, text + ("" if not text or text.endswith("\n") else "\n") + block())
    return f"added the tt-project block to {path}"

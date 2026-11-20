"""Keep macOS Spotlight out of a project's busy folders.

Spotlight (mds_stores) indexes every file a worker writes under state/runs and worktrees/, which
can cost a whole core. It skips any folder whose name ends in `.noindex` (a `.metadata_never_index`
file is ignored), so on macOS `state` and `worktrees` live in `state.noindex` and `worktrees.noindex`
with a relative compatibility link at the old name: code and people that name `<base>/state/...`
still reach the same files. Project hands out the real folders, so git worktree records and
no-follow opens see no link. Linux and other systems are unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SUFFIX = ".noindex"
ROOTS = ("state", "worktrees")


def wanted(platform: str | None = None) -> bool:
    return (platform or sys.platform) == "darwin"


def real(base: Path, name: str) -> Path:
    """The folder that holds <base>/<name>: <name>.noindex once it was moved there (the link points at
    it, or the link is not made yet after a cut-short move), else <base>/<name> itself."""
    link, moved = base / name, base / (name + SUFFIX)
    try:
        if link.is_symlink():
            return moved if os.readlink(link) == moved.name else link
        if not link.exists() and moved.is_dir():
            return moved
    except OSError:
        pass
    return link


def _link(base: Path, name: str, report: list[str]) -> None:
    """Point <name> at <name>.noindex. A real folder that appeared at <name> (something wrote there
    between the rename and the link) is set aside whole, never merged or deleted."""
    link, moved = base / name, base / (name + SUFFIX)
    for _ in range(3):
        if link.is_symlink():
            if os.readlink(link) == moved.name:
                return
            link.unlink()
        elif link.exists():
            aside = base / f"{name}.stray-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
            os.rename(link, aside)
            report.append(f"noindex: {link} reappeared during the move; kept as {aside}")
        try:
            os.symlink(moved.name, link)
            return
        except FileExistsError:
            continue
    raise OSError(f"could not link {link} to {moved.name}")


def ensure(base: Path, platform: str | None = None) -> None:
    """A new project's folders: <name>.noindex plus its link on macOS, nothing elsewhere."""
    if not wanted(platform):
        return
    for name in ROOTS:
        (base / (name + SUFFIX)).mkdir(parents=True, exist_ok=True)
        _link(base, name, [])


def _repair(worktrees: Path, report: list[str]) -> None:
    """git keeps each worktree's real path: point its records at the moved folders."""
    for wt in sorted(worktrees.iterdir()) if worktrees.is_dir() else ():
        if not (wt / ".git").is_file():
            continue
        try:
            r = subprocess.run(["git", "-C", str(wt), "worktree", "repair"], capture_output=True,
                               text=True, timeout=60)
            if r.returncode:
                report.append(f"noindex: git worktree repair in {wt} failed: {(r.stderr or '').strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            report.append(f"noindex: git worktree repair in {wt} failed: {e}")


def migrate(base: Path, platform: str | None = None) -> list[str]:
    """Move an existing project's folders under .noindex (macOS only). A rename on the same disk keeps
    every file, open handle and running worker's cwd; the link keeps old paths working. Safe to rerun
    and to resume after a cut-short run. Returns what it did."""
    report: list[str] = []
    if not wanted(platform):
        return report
    for name in ROOTS:
        link, moved = base / name, base / (name + SUFFIX)
        if link.is_symlink():
            if os.readlink(link) != moved.name:
                report.append(f"noindex: {link} links elsewhere; left as it is")
                continue
            if moved.is_dir():
                continue
        elif link.is_dir():
            if moved.exists():
                report.append(f"noindex: both {link} and {moved} exist; left as they are")
                continue
            os.rename(link, moved)
            report.append(f"noindex: moved {link} to {moved}")
        elif not moved.is_dir():
            moved.mkdir(parents=True)
        _link(base, name, report)
        if name == "worktrees":
            _repair(moved, report)
    return report

"""Which tree `ttp checks` checks, and the line and warning that name it.

The rule (one place): `ttp checks` checks the git worktree that contains its starting directory,
`--worktree <path>` when given, else the shell's current directory, at that worktree's top and on
its HEAD commit. It never picks the task's worktree or the project root by itself: a shell left in
the project root checks the project root. Every start (inline and `--detach`) prints `line()`, and
the `--result` probe prints it again from checks.pid.

`warning()` is loud, never a refusal: it fires when the checked tree is not the one the caller is
in (the shell's directory, or the run's own working directory from run.json) and that tree is the
project root or is stale (its HEAD lacks the push branch's tip as last fetched), and when the
project root itself is checked while stale. Nothing here fetches: it reads local refs only.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 20


def _git(top: str | Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def toplevel(path: str | Path | None) -> str:
    """The git worktree top containing `path`, resolved, or "" when it is in none."""
    if not path or not Path(path).is_dir():
        return ""
    top = _git(path, "rev-parse", "--show-toplevel")
    return str(Path(top).resolve()) if top else ""


def branch(top: str | Path) -> str:
    """The branch checked out in `top`, or "" for a detached HEAD."""
    return _git(top, "symbolic-ref", "--short", "-q", "HEAD")


def line(head: str, top: str, br: str | None = None) -> str:
    """The start line: the commit, branch and worktree the checks run on."""
    br = branch(top) if br is None and top and Path(top).is_dir() else (br or "")
    return (f"ttp checks: checking {head[:12] or '?'} ({f'branch {br}' if br else 'detached HEAD'}) "
            f"in worktree {top or '?'}")


def run_worktree(run_dir: str | Path | None) -> str:
    """The worktree top of the run's own working directory (run.json `cwd`), or ""."""
    try:
        cwd = json.loads((Path(run_dir) / "run.json").read_text()).get("cwd") if run_dir else None
    except (OSError, ValueError, AttributeError):
        return ""
    return toplevel(cwd) if isinstance(cwd, str) else ""


def behind(top: str | Path, head: str, tip_ref: str) -> int | None:
    """How many commits of `tip_ref` the checked `head` lacks: 0 when it is the tip or descends from
    it, None when either is unknown here."""
    tip = _git(top, "rev-parse", "--verify", "-q", tip_ref)
    if not tip or not head:
        return None
    n = _git(top, "rev-list", "--count", f"{head}..{tip}")
    return int(n) if n.isdigit() else None


def warning(top: str, head: str, *, root: str | Path | None = None, tip_ref: str = "",
            callers: list[str] | tuple = ()) -> str:
    """The stale-tree warning for checks of `top` at `head`, or "". `root`: the project root;
    `tip_ref`: the push branch's local remote ref (refs/remotes/<remote>/<branch>); `callers`: the
    worktrees the caller is in (shell directory, run's working directory)."""
    top_r = str(Path(top).resolve()) if top else ""
    is_root = bool(root) and top_r == str(Path(root).resolve())
    others = sorted({c for c in callers if c and c != top_r})
    lag = behind(top, head, tip_ref) if tip_ref else None
    stale = bool(lag)
    if not ((others and (is_root or stale)) or (is_root and stale)):
        return ""
    what = "the project root checkout" if is_root else "this worktree"
    tip = tip_ref.removeprefix("refs/remotes/")
    why = f"its HEAD {head[:12]} is {lag} commit(s) behind {tip} as last fetched" if stale else \
        f"its HEAD is {head[:12]}"
    where = f"; you are in {', '.join(others)}" if others else ""
    return (f"ttp checks: WARNING: checking {what} {top_r} ({why}){where}. If you meant another tree, "
            "this result is not about it: `cd` into that worktree (or pass `--worktree <path>`), run "
            "`ttp checks` again and confirm the printed head is the commit you meant")


def callers(cwd: str | Path | None = None, run_dir: str | Path | None = None) -> list[str]:
    """The worktrees the caller is in: the shell's directory and the run's own working directory."""
    return [t for t in (toplevel(cwd or os.getcwd()), run_worktree(run_dir)) if t]

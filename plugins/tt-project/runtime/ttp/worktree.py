# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Isolated workspaces for code tasks: one git worktree and branch per task, under the project's
ignored folder, so parallel workers never share a working tree with each other or the user."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .db import continues_id
from .project import Project


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def is_git(path: Path) -> bool:
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def git_common_dir(path: Path) -> str | None:
    """The repository's shared .git directory; for a worktree it lies outside the worktree."""
    try:
        out = _git(path, "rev-parse", "--git-common-dir", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    common = (Path(path) / out).resolve() if out else None
    return str(common) if common and common.is_dir() else None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def base_ref(p: Project) -> str:
    cfg = p.config()
    ref = cfg.get("delivery", {}).get("base_ref")
    if ref:
        return ref
    head = _git(p.root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    return head or _git(p.root, "rev-parse", "--abbrev-ref", "HEAD")


def ensure(p: Project, task: dict) -> tuple[Path, str]:
    """Create (or reuse) the task's worktree. A retried task continues on its own branch."""
    branch = task.get("branch") or f"ttp/t{task['id']}-{slug(task['title'])}"
    path = p.worktrees / f"t{task['id']}"
    if path.exists() and is_git(path):
        return path, branch
    p.worktrees.mkdir(parents=True, exist_ok=True)
    exists = _git(p.root, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists:
        _git(p.root, "worktree", "add", str(path), branch)
    else:
        _git(p.root, "fetch", "--quiet", "origin", check=False)
        _git(p.root, "worktree", "add", "-b", branch, str(path), continued_head(p, task) or resolve_base(p))
    return path, branch


def continued_head(p: Project, task: dict) -> str | None:
    """The head of the branch of the code task this one continues, so its commits carry over.
    A branch of its own lets the old worktree stay checked out until it is pruned."""
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if not old or old["kind"] != "code" or not old["branch"]:
        return None
    for cand in (old["branch"], f"origin/{old['branch']}"):
        head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
        if head:
            return head
    return None


def resolve_base(p: Project) -> str:
    """The configured base as a commit git can find: the name itself, else the remote's branch of
    that name (a fresh clone has `origin/<name>` but no local `<name>`). A miss names close matches,
    so whoever set it can fix it in one step."""
    ref = base_ref(p)
    for cand in (ref, f"origin/{ref}"):
        if _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False):
            return cand
    tail = ref.split("/")[-1]
    near = [b for b in _git(p.root, "branch", "-a", "--format=%(refname:short)", check=False).split()
            if tail and tail in b][:5]
    raise RuntimeError(f"base branch {ref!r} not found here or on origin"
                       + (f"; similar: {', '.join(near)}" if near else ""))


def remove(p: Project, task_id: int) -> None:
    """Remove a task's worktree; its branch stays. Refuses (raises) if git sees edits in it."""
    path = p.worktrees / f"t{task_id}"
    if path.exists():
        _git(p.root, "worktree", "remove", str(path))


def keep_reason(p: Project, path: Path) -> str | None:
    """Why this worktree must stay, or None when removing it loses nothing: no uncommitted or
    untracked files, and its HEAD is on a remote branch or already in the base branch."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=120)
    st = git("status", "--porcelain")
    if st.returncode != 0:
        return "git status failed"
    if st.stdout.strip():
        return "uncommitted changes"
    remote = git("branch", "-r", "--contains", "HEAD")
    if remote.returncode == 0 and remote.stdout.strip():
        return None
    try:
        base = resolve_base(p)
    except RuntimeError as e:
        return str(e)
    if git("merge-base", "--is-ancestor", "HEAD", base).returncode == 0:
        return None
    return "commits not pushed or merged"


def has_changes(path: Path, since_ref: str) -> bool:
    """Durable progress for a code task: commits past the base, or uncommitted edits."""
    ahead = _git(path, "rev-list", "--count", f"{since_ref}..HEAD", check=False)
    dirty = _git(path, "status", "--porcelain", check=False)
    return (ahead.isdigit() and int(ahead) > 0) or bool(dirty)


_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def reviewed_refs(p: Project, task: dict) -> list[str]:
    """What a review task reviews: the branches of the tasks it depends on, and the task branches
    and commit hashes its spec names."""
    from .db import dependency_ids
    spec = task.get("spec") or ""
    ids = [i for i in dependency_ids(task) if i is not None]
    refs = [r["branch"] for r in p.db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL AND branch!=''")
            if r["id"] in ids or re.search(rf"(?<![\w/.-]){re.escape(r['branch'])}(?![\w/-])", spec)]
    refs += _HEX.findall(spec)
    return list(dict.fromkeys(refs))


def diff_lines(p: Project, refs: list[str]) -> dict[str, int | None] | None:
    """Lines changed per file by `refs` since they left the base branch (None for a binary file),
    or None when they change nothing measurable, such as a commit already on the base."""
    base = resolve_base(p)
    out: dict[str, int | None] = {}
    for ref in refs:
        if not _git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False):
            continue
        for line in _git(p.root, "diff", "--numstat", "--no-renames", f"{base}...{ref}").splitlines():
            added, deleted, path = line.split("\t", 2)
            prev = out.get(path, 0)
            n = None if added == "-" else int(added) + int(deleted)
            out[path] = None if n is None or prev is None else max(n, prev)
    return out or None

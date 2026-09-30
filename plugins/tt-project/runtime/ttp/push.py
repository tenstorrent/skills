# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Guarded push: publish the current commit onto the project's target branch only after the
project's checks passed on exactly the commit being pushed, and never with force."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .project import Project

# Exit codes, distinct so a worker can say why it did not push.
REFUSED, CONFLICT, CHECKS_FAILED, KEPT_MOVING, REJECTED = 2, 3, 4, 5, 6
DEFAULT_ROUNDS = 3
PROTECTED = {"HEAD", "main", "master"}


def check_list(v: Any) -> list[str]:
    """Check commands from a config value: a list, a JSON-encoded list, or one command per line."""
    if isinstance(v, str):
        text = v.strip()
        try:
            v = json.loads(text) if text.startswith("[") else text.splitlines()
        except ValueError:
            v = text.splitlines()
    return [str(c).strip() for c in (v or []) if str(c).strip()]


def _git(repo: Path, *args: str, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run git; when not quiet its output goes to stderr, keeping stdout for the verdict."""
    if quiet:
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=2)


def _fetch(repo: Path, remote: str, branch: str) -> str:
    """The remote branch's current tip, or "" when it cannot be read. The explicit refspec also
    works where the remote's configured refspec does not cover the branch."""
    if _git(repo, "fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            quiet=False).returncode != 0:
        return ""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}").stdout.strip()


def target(p: Project, repo: Path) -> tuple[str, str]:
    """(remote, branch) to push to: `delivery.push_branch` only. `delivery.base_ref` is where work
    starts, which may be the default branch, so it is never a push target."""
    d = p.config().get("delivery") or {}
    ref = str(d.get("push_branch") or "").strip()
    if not ref:
        raise ValueError("no target branch: set delivery.push_branch")
    remote, _, rest = ref.partition("/")
    if not (rest and remote in _git(repo, "remote").stdout.split()):
        remote, rest = "origin", ref
    return remote, rest[len("refs/heads/"):] if rest.startswith("refs/heads/") else rest


def refusal(repo: Path, remote: str, branch: str) -> str:
    """Why `branch` on `remote` must not be pushed to, or "" when it may. Fails closed when the
    remote cannot be asked for its default branch."""
    if branch in PROTECTED:
        return f"refusing to push to {remote}/{branch}"
    ls = _git(repo, "ls-remote", "--symref", remote, "HEAD")
    if ls.returncode != 0:
        return f"cannot reach {remote}: {ls.stderr.strip()}"
    for line in ls.stdout.splitlines():
        if line.startswith("ref: ") and line[5:].split("\t")[0] == f"refs/heads/{branch}":
            return f"refusing to push to {remote}/{branch}, the remote's default branch"
    return ""


def rounds_of(v: Any) -> int:
    """`delivery.push_rounds` as an integer of at least 1; ValueError when it is not an integer."""
    if v is None or v == "":
        return DEFAULT_ROUNDS
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}")
    try:
        return max(1, int(v))
    except ValueError:
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}") from None


def push(repo: Path, remote: str, branch: str, checks: list[str], rounds: int = DEFAULT_ROUNDS,
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)) -> int:
    """Rebase HEAD onto remote/branch, run `checks` on the result, and push it if the remote did not
    move meanwhile; if it did, start over, at most `rounds` times."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        say("uncommitted changes; commit first")
        return REFUSED
    why = refusal(repo, remote, branch)
    if why:
        say(why)
        return REFUSED
    upstream = f"{remote}/{branch}"
    for rnd in range(1, rounds + 1):
        tip = _fetch(repo, remote, branch)
        if not tip:
            say(f"cannot fetch {upstream}")
            return REFUSED
        if _git(repo, "rebase", tip, quiet=False).returncode != 0:
            _git(repo, "rebase", "--abort")
            say(f"rebase onto {upstream} conflicts; resolve it keeping both sides' intents, then rerun")
            return CONFLICT
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        for cmd in checks:
            if subprocess.run(cmd, shell=True, cwd=repo).returncode != 0:
                say(f"check failed on {head[:10]}: {cmd}; not pushing")
                return CHECKS_FAILED
        if _fetch(repo, remote, branch) != tip:
            say(f"{upstream} moved during the checks; round {rnd + 1}")
            continue
        # Without --force the remote refuses anything that is not a fast-forward of what we tested on.
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            say(f"pushed {head[:10]} to {upstream}")
            return 0
        if _fetch(repo, remote, branch) == tip:
            say(f"push to {upstream} was rejected")
            return REJECTED
        say(f"{upstream} moved before the push; round {rnd + 1}")
    say(f"{upstream} kept moving for {rounds} rounds; not pushing")
    return KEPT_MOVING


def run(p: Project, repo: Path) -> int:
    """`ttp push` for a project: target, checks and rounds come from its `delivery` config."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    checks = check_list(d.get("push_checks"))
    if not checks:
        print("ttp push: no checks configured: set delivery.push_checks to the commands that must "
              "pass before a push", file=sys.stderr)
        return REFUSED
    try:
        remote, branch = target(p, repo)
        rounds = rounds_of(d.get("push_rounds"))
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    return push(repo, remote, branch, checks, rounds)

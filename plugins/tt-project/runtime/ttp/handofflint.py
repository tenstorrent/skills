# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model-free hand-off lint: the daemon runs it on every code task's hand-off (Daemon._finish_worker),
so no worker has to call it.

A code task that may publish its own branch (delivery.own_push "on", or unset with draft PRs on:
project.own_creates_pr_branch) publishes it itself with `ttp push --own-branch`. A follow-up that
proposes pushing that branch while it is not on the remote leaves the task's own step to another
run. The hand-off is flagged in the digest as a task note, which also keeps the daemon from queueing
that follow-up as the next hop by itself (a hand-off with notes is the coordinator's). Upstream
notes and `continue:` hops are not linted. The remote is asked only when a follow-up matches, and
only by prefetch, before the transaction that records the hand-off (Daemon._prefetch_remote): lint
runs inside it and reads that answer, so the write lock never waits on the network. A task that
worked in another repository's checkout (its branch is not in the project root) is asked about that
repository's remote, not the root's. No answer, or an unreadable remote, flags nothing."""
from __future__ import annotations

import re
import time
from pathlib import Path

from .project import Project, own_creates_pr_branch, own_push
from . import upstream

REMOTE_TIMEOUT_S = 30   # one ls-remote, asked before the transaction (prefetch)
SEEN_TTL_S = 300        # prefetch's answer is read inside the transaction this long
_seen: dict[tuple[str, str], tuple[float, bool]] = {}   # (root, branch): (until, on the remote)

CONTINUE = re.compile(r"(?i)^\s*continue\s*:")   # a split's hop carries the rest of the work, not a push
_PUBLISH = r"\bpublish(?:es|ing)?\b|\bopen(?:s|ing)?\s+(?:its\s+|the\s+|a\s+)?(?:draft\s+)?(?:pr|pull\s+request)\b"
_TO = r"\bpush(?:es|ing)?\s+(?:up\s+)?(?:(?:the|its|this|own|task'?s?|work)\s+)*(?:branch\b|{b}(?![\w./-]))"


def proposes_push(result: dict, branch: str) -> str:
    """The title of the first follow-up of `result` that proposes publishing `branch`: its title or
    spec says "push <branch>" or "push the branch", "publish" or "open its (draft) PR". Never an
    upstream note or a `continue:` hop, and never "pushed" or "the push queue". "" if none."""
    rule = re.compile(f"{_PUBLISH}|{_TO.format(b=re.escape(branch.lower()))}")
    for f in result.get("followups") or []:
        if not isinstance(f, dict) or upstream.is_note(f) or CONTINUE.match(str(f.get("title") or "")):
            continue
        if rule.search(f"{f.get('title') or ''} {f.get('spec') or ''}".lower()):
            return str(f.get("title") or "")[:200]
    return ""


def applies(cfg: dict) -> bool:
    """Whether a code task may publish its own branch under the project's delivery settings."""
    return own_push(cfg) == "on" or own_creates_pr_branch(cfg)


def _has_branch(repo: Path, branch: str) -> bool:
    from . import push
    return push._git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0


def _repo_of(p: Project, task: dict, branch: str) -> Path:
    """The checkout holding `task`'s branch: the project root when it has it (or no other repository
    of the task's runs does), else the newest other repository the task's runs worked in that has it."""
    if _has_branch(p.root, branch):
        return p.root
    from .landed import _task_repos
    try:
        repos = _task_repos(p, int(task.get("id") or 0))
    except Exception:
        repos = []
    return next((r for r in repos if _has_branch(r, branch)), p.root)


def _ask_remote(p: Project, repo: Path, branch: str) -> bool | None:
    """Whether `branch` exists on the remote `ttp push --own` publishes to (for another repository
    than the project root: the remote its branch tracks, else origin; delivery.own_push_remote when
    set); None when unreadable or the
    repo has no remote. Asks the network: never inside a transaction (prefetch only)."""
    from . import push
    d = p.config().get("delivery") or {}
    try:
        if push.remote_less(repo):
            return None
        if repo != p.root:
            tracked = push._git(repo, "config", "--get", f"branch.{branch}.remote").stdout.strip()
            remote = tracked if tracked and tracked != "." else "origin"
        else:
            remote = push.target(p, repo)[0] if str(d.get("push_branch") or "").strip() else "origin"
        remote = push.own_push_remote(d, repo) or remote
    except Exception:
        return None
    ls = push._git_net(repo, "ls-remote", "--heads", push.push_side(repo, remote), f"refs/heads/{branch}",
                       timeout_s=REMOTE_TIMEOUT_S)
    if ls.returncode != 0:
        return None
    return any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())


def _flagged(cfg: dict, task: dict, result) -> tuple[str, str]:
    """(branch, the matching follow-up's title) when `task`'s hand-off is linted and proposes
    publishing its branch, else ("", "")."""
    branch = str(task.get("branch") or "")
    if task.get("kind") != "code" or not branch or not isinstance(result, dict) or not applies(cfg):
        return "", ""
    title = proposes_push(result, branch)
    return (branch, title) if title else ("", "")


def prefetch(p: Project, cfg: dict, task: dict, result) -> None:
    """Asks the remote, before the transaction that records `task`'s hand-off, whether its branch is
    there, only when lint would need the answer. Bounded and best effort."""
    branch, _ = _flagged(cfg, task, result)
    if not branch:
        return
    on = _ask_remote(p, _repo_of(p, task, branch), branch)
    key = (str(p.root), branch)
    if on is None:
        _seen.pop(key, None)
    else:
        _seen[key] = (time.time() + SEEN_TTL_S, on)


def _on_remote(p: Project, branch: str) -> bool | None:
    """prefetch's answer for `branch`, None when it got none within SEEN_TTL_S. Never asks."""
    hit = _seen.get((str(p.root), branch))
    return hit[1] if hit and hit[0] > time.time() else None


def lint(p: Project, cfg: dict, task: dict, result) -> list[str]:
    """Problems of a code task's hand-off, each one line for the digest; [] when clean or not linted.
    Runs inside the transaction: reads only what prefetch got from the remote."""
    branch, title = _flagged(cfg, task, result)
    if not title or _on_remote(p, branch) is not False:
        return []
    return [f"hand-off lint: follow-up {title!r} proposes pushing {branch}, which is not on the remote. "
            f"Publishing it is the task's own step (`ttp push --own-branch --detach` from its branch): requeue "
            f"the task to do it rather than queueing that follow-up."]

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Harness tasks that run at the same time each get a working tree of their own.

A harness task works in the project's harness checkout, where the daemon reads prompts, config and
runtime live. Two at once there would sweep up each other's uncommitted edits in a commit. So a
harness task starts in the harness checkout only while no other harness task's run works there and the
checkout is not left on a harness task's branch.
Otherwise it gets a git worktree of the harness repo of its own (state/harness-worktrees/t<id>,
branch ttp/harness-t<id>, started from the harness main line, never from what the checkout has checked
out), and keeps it on later runs while it exists.

When its run ends the daemon lands it (land): the branch's commits are merged into the harness
checkout. The worktree and branch stay while the task is open (a waiting task's detached job may run
in it, and its next run works there again); they are removed once the task ended (or is gone from the
database), nothing uncommitted is left in them and no detached job or `ttp push` of it runs (held). A merge that
conflicts, or that git refuses over uncommitted changes there, is undone and kept for the next
pass (sweep), and reported to the coordinator once per branch head (a refusal only while no harness
run works in the checkout). A conflict is merged again only once its branch head or the harness HEAD
moved: each try briefly leaves MERGE_HEAD in the live checkout, in the way of `ttp upgrade`. A merge
that changed runtime/ asks the daemon to restart, as `ttp restart` would."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .project import Project, durable_write, git_fsync_args
from . import gitmemo

DIR = "harness-worktrees"
IDENT = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
REPORTED_KEY = "harness_merge_reported"   # kv: task id -> the branch head a conflict was reported for
CONFLICT_KEY = "harness_merge_conflicted"  # kv: task id -> [branch head, harness HEAD, "conflict"|"refused"] last seen
MERGE_MSG = "tt-project: land harness task"   # the start of land's merge message (MERGE_MSG mid-merge)
SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
TASK_BRANCH_RE = re.compile(r"ttp/harness-t\d+")   # a harness task's own branch


def path(p: Project, tid: int) -> Path:
    return p.state / DIR / f"t{int(tid)}"


def branch(tid: int) -> str:
    return f"ttp/harness-t{int(tid)}"


def _git(cwd: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *git_fsync_args(), *IDENT, *args], capture_output=True,
                          text=True, timeout=gitmemo.timeout(timeout, args))


def _is_repo(p: Project) -> bool:
    return (p.harness / ".git").exists() and _git(p.harness, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def dirty(cwd: Path) -> bool:
    r = _git(cwd, "status", "--porcelain")
    return r.returncode != 0 or bool(r.stdout.strip())


def checkout_busy(p: Project, tid: int) -> bool:
    """The harness checkout is in use: another harness task's run works there (it has no worktree of
    its own). Uncommitted changes alone do not count: a config edit may stay there for days."""
    for r in p.db.q("SELECT runs.task FROM runs JOIN tasks ON tasks.id=runs.task WHERE runs.status='running' "
                    "AND tasks.kind='harness' AND runs.task!=?", (int(tid),)):
        if not path(p, r["task"]).exists():
            return True
    return False


def on_branch(cwd: Path) -> str:
    """The branch working tree `cwd` has checked out, or "" (detached, or git cannot tell)."""
    r = _git(cwd, "symbolic-ref", "-q", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def main_branch(p: Project) -> str:
    """The harness repo's main line, which a new task branch starts from: `main` (or `master`) when it
    has one, else the checkout's own branch unless that is a harness task's branch; "" when none."""
    for b in ("main", "master"):
        if _git(p.harness, "rev-parse", "--verify", "-q", f"refs/heads/{b}").returncode == 0:
            return b
    here = on_branch(p.harness)
    return "" if TASK_BRANCH_RE.fullmatch(here) else here


def off_branch(p: Project, tid: int) -> str:
    """Where harness task `tid`'s commits go when that is not its own line, or "": its worktree is on
    another branch than ttp/harness-t<tid> (or detached), or the harness checkout (where its work is
    merged, or done) sits on a harness task's branch instead of its main line."""
    own = path(p, tid)
    if own.exists() and (got := on_branch(own)) != branch(tid):
        return got or "a detached HEAD"
    if _is_repo(p) and TASK_BRANCH_RE.fullmatch(here := on_branch(p.harness)):
        return here
    return ""


def _checked(p: Project, tid: int, own: Path) -> Path:
    got = on_branch(own)
    if got != branch(tid):
        raise RuntimeError(f"harness worktree for #{tid} ({own}) is on {got or 'a detached HEAD'}, not its own "
                           f"branch {branch(tid)}: not run there, so its commits cannot land on another branch")
    return own


def workdir(p: Project, tid: int) -> Path:
    """Where harness task `tid` works: its worktree when it has one, else the harness checkout while
    that is free and on its main line, else a new worktree of its own: branch ttp/harness-t<tid>,
    kept as it is when it exists (a retry), else started from the main line (main_branch), never from
    whatever the checkout has checked out. A worktree not on that branch is refused (RuntimeError)."""
    own = path(p, tid)
    if own.exists():
        return _checked(p, tid, own)
    if not _is_repo(p) or not (checkout_busy(p, tid) or TASK_BRANCH_RE.fullmatch(on_branch(p.harness))):
        return p.harness
    own.parent.mkdir(parents=True, exist_ok=True)
    have = _git(p.harness, "rev-parse", "--verify", "-q", f"refs/heads/{branch(tid)}").returncode == 0
    if have:
        args = ["worktree", "add", "-q", str(own), branch(tid)]
    elif start := main_branch(p):
        args = ["worktree", "add", "-q", "-b", branch(tid), str(own), start]
    else:
        raise RuntimeError(f"harness worktree for #{tid}: the harness repo has no main branch to start "
                           f"{branch(tid)} from (its checkout is on {on_branch(p.harness) or 'a detached HEAD'})")
    r = _git(p.harness, *args, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"harness worktree for #{tid}: {(r.stderr or r.stdout).strip()[-400:]}")
    return _checked(p, tid, own)


def held(p: Project, tid: int) -> str | None:
    """Why harness task `tid`'s worktree must stay, or None: the task is still open, or a detached
    job, detached checks or a detached `ttp push` of it still runs (in it, for all we know)."""
    from .db import TERMINAL_TASK_STATES
    task = p.db.task(int(tid))
    if task and task["status"] not in TERMINAL_TASK_STATES:
        return f"task #{tid} ({task['status']}) is still open"
    from .push import running
    own = path(p, tid).resolve()
    if any(Path(m.get("repo") or "/").resolve() == own for m in running(p)):
        return "a detached `ttp push` runs in it"
    from . import hold
    from .cli import CHECKS_RC, _checks_alive, _read_checks_pid
    for r in p.db.q("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL", (int(tid),)):
        d = Path(r["dir"])
        if hold.live(hold.run_jobs(d)):
            return "a detached job of it still runs"
        info = _read_checks_pid(d)
        if info and not (d / CHECKS_RC).exists() and _checks_alive(info):
            return "its detached checks still run"
    return None


def land(p: Project, tid: int) -> str:
    """Merge harness task `tid`'s worktree branch into the harness checkout (call it when no run of
    the task is live); remove the worktree and branch once nothing is left in them and nothing holds
    them (held). Returns what happened: '' (no worktree), 'merged' (and removed), 'kept' (its commits
    landed; the worktree stays, for the open task or its live jobs, or with uncommitted changes for
    the task's next run), 'busy' (git refused, e.g. over the checkout's own changes), 'conflict' (new)
    or 'conflict-known' (the same branch head and harness HEAD as last time: not merged again). While
    the checkout sits on a harness task's branch, not its main line, nothing is merged ('busy')."""
    own = path(p, tid)
    if not own.exists():
        return ""
    if not _is_repo(p) or TASK_BRANCH_RE.fullmatch(on_branch(p.harness)):
        return "busy"   # never merged into another task's branch the checkout was left on
    b = branch(tid)
    ahead = _git(p.harness, "rev-list", "--count", f"HEAD..{b}")
    outcome = ""
    if ahead.returncode == 0 and int(ahead.stdout.strip() or 0) > 0:
        before = _git(p.harness, "rev-parse", "HEAD").stdout.strip()
        pair = [_git(p.harness, "rev-parse", b).stdout.strip(), before]
        last = (p.db.kv(CONFLICT_KEY, {}) or {}).get(str(tid))
        if last == pair + ["conflict"]:
            return "conflict-known"     # the same two commits conflict again: no merge until one moves
        r = _git(p.harness, "merge", "--no-edit", "-m", f"{MERGE_MSG} #{tid} ({b})", b, timeout=120)
        if r.returncode != 0:
            said = (r.stdout + r.stderr).strip()
            conflicted = bool(_git(p.harness, "diff", "--name-only", "--diff-filter=U").stdout.strip())
            if conflicted:
                _git(p.harness, "merge", "--abort")
            # a refusal over files another live run is editing passes when it commits; else it needs a hand
            if conflicted or ("would be overwritten" in said and not checkout_busy(p, tid)):
                # a refusal leaves no MERGE_HEAD and passes once the checkout's changes go: tried each pass
                seen = pair + ["conflict" if conflicted else "refused"]
                if last == seen:
                    return "conflict-known"
                _set_conflicted(p, tid, seen)
                _report_conflict(p, tid, said)
                return "conflict"
            return "busy"
        _set_conflicted(p, tid, None)
        changed = _git(p.harness, "diff", "--name-only", before, "HEAD").stdout.split()
        if any(f.startswith("runtime/") for f in changed):
            from . import service
            durable_write(p.state / service.RESTART_REQUEST,
                          json.dumps({"at": time.time(), "pid": os.getpid(),
                                      "why": f"harness task #{tid}'s merged commits changed runtime/"}))
        outcome = "merged"
    if dirty(own) or held(p, tid):
        return "kept"
    r = _git(p.harness, "worktree", "remove", str(own), timeout=120)
    if r.returncode != 0 and own.exists():
        return outcome or "kept"
    _git(p.harness, "branch", "-d", b)
    _set_conflicted(p, tid, None)
    return outcome or "merged"


def named_commits(task: dict) -> list[str]:
    """The commit hashes harness task `task`'s hand-off names (its `commit`, `head` or `sha`, then any
    hash in its summary), unresolved, in order."""
    from .db import load_result
    res = load_result(task.get("result"))
    named = [str(res.get(k) or "").strip() for k in ("commit", "head", "sha")]
    named += SHA_RE.findall(str(res.get("summary") or ""))
    return [c for c in dict.fromkeys(named) if SHA_RE.fullmatch(c)]


def unmerged(p: Project, tid: int) -> int:
    """How many commits harness task `tid`'s worktree branch has that the harness checkout's HEAD does
    not (0 without the branch; -1 when git cannot tell)."""
    b = branch(tid)
    if _git(p.harness, "rev-parse", "--verify", "-q", f"refs/heads/{b}").returncode != 0:
        return 0
    ahead = _git(p.harness, "rev-list", "--count", f"HEAD..{b}")
    return int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else -1


def merged_commit(p: Project, task: dict) -> str:
    """The commit harness task `task` handed off with (named_commits), once it is in the harness
    checkout's HEAD (its main line), or "": no harness repo, no named commit the harness has, one of
    them not in HEAD, or commits on the task's worktree branch not merged yet. Hashes the harness does
    not have (of another repository) are skipped."""
    if not _is_repo(p) or unmerged(p, task["id"]):
        return ""
    found = ""
    for c in named_commits(task):
        full = _git(p.harness, "rev-parse", "--verify", "-q", f"{c}^{{commit}}")
        if full.returncode != 0:
            continue
        if _git(p.harness, "merge-base", "--is-ancestor", full.stdout.strip(), "HEAD").returncode != 0:
            return ""
        found = found or full.stdout.strip()
    return found


def landing_merge(h: Path) -> bool:
    """True while checkout `h` is mid-merge in a merge land started (MERGE_HEAD set, its message ours)."""
    if _git(h, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0:
        return False
    r = _git(h, "rev-parse", "--git-path", "MERGE_MSG")
    m = Path(r.stdout.strip())
    try:
        return r.returncode == 0 and (m if m.is_absolute() else h / m).read_text().startswith(MERGE_MSG)
    except OSError:
        return False


def _set_conflicted(p: Project, tid: int, seen: list[str] | None) -> None:
    """Record (or with None clear) the (branch head, harness HEAD, kind) a merge last failed on."""
    with p.db.tx():
        known = p.db.kv(CONFLICT_KEY, {}) or {}
        if known.get(str(tid)) == seen:
            return
        if seen is None:
            known.pop(str(tid))
        else:
            known[str(tid)] = seen
        p.db.set_kv(CONFLICT_KEY, known)


def _report_conflict(p: Project, tid: int, why: str) -> None:
    """Tell the coordinator once per branch head: the task's commits wait on the branch."""
    head = _git(p.harness, "rev-parse", branch(tid)).stdout.strip()
    seen = p.db.kv(REPORTED_KEY, {}) or {}
    if seen.get(str(tid)) == head:
        return
    with p.db.tx():
        seen[str(tid)] = head
        p.db.set_kv(REPORTED_KEY, seen)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (time.time(), "harness", "harness_merge_conflict", "normal",
                f"Harness task #{tid}'s commits on branch {branch(tid)} do not merge into the harness checkout; "
                f"they wait in {path(p, tid)}. A harness task should merge them by hand "
                f"(`git merge {branch(tid)}` in the harness) and resolve, keeping both sides' intents. "
                f"git said: {why[-400:]}", "queued", tid))


def sweep(p: Project, running: set[int]) -> dict[int, str]:
    """Land every harness worktree whose task has no live run (`running`: task ids with one); a
    worktree whose task is gone from the database is landed too. Each is removed only once its task
    ended and nothing holds it (land). Returns task id -> outcome."""
    root = p.state / DIR
    if not root.is_dir():
        return {}
    if _is_repo(p) and landing_merge(p.harness):    # a merge of ours a killed daemon left: undo it first
        _git(p.harness, "merge", "--abort")
    out = {}
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and d.name.startswith("t") and d.name[1:].isdigit()):
            continue
        tid = int(d.name[1:])
        if tid in running:
            continue
        got = land(p, tid)
        if got:
            out[tid] = got
    try:
        root.rmdir()   # only when empty
    except OSError:
        pass
    return out

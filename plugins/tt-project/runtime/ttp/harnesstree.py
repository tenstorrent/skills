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
database), nothing uncommitted is left in them and no detached job or `ttp push` of it runs (held). The
daemon's own uncommitted memory writes there are committed first (_commit_memory). A merge that
conflicts, or that git refuses over uncommitted changes there, is undone and kept for the next
pass (sweep), and reported to the coordinator once per branch head (a refusal only while no harness
run works in the checkout). The merge is made aside and the checkout only moves forward to it: the
ref first, then git updates the files that still match the old HEAD (_fast_forward). An uncommitted
copy there of a file the merge changes lands as it is only when it already holds the merged content
(staged, never rewritten); any other stops the land, named, and every uncommitted file is left alone.
A conflict is merged again only once its branch head or the harness HEAD moved. A merge
that changed runtime/ asks the daemon to restart, as `ttp restart` would."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .project import MEMORY_PATHS, Project, durable_write, git_fsync_args, tree_writes
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
    return gitmemo.run(["git", "-C", str(cwd), *git_fsync_args(), *IDENT, *args], capture_output=True,
                       text=True, timeout=timeout)


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
        args = ["worktree", "add", "-q", "--no-track", "-b", branch(tid), str(own), start]
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


LAND_LOCK_WAIT_S = 5.0   # land's wait for the memory index lock (Project.memory_locked) before 'busy'


def land(p: Project, tid: int, other: str = "") -> str:
    """Merge harness task `tid`'s worktree branch into the harness checkout (call it when no run of
    the task is live); remove the worktree and branch once nothing is left in them and nothing holds
    them (held). With `other`, a branch of the task's outside its own worktree (land_stranded), merge
    that branch the same way and remove nothing. Returns what happened: '' (no worktree), 'merged' (and removed), 'kept' (its commits
    landed; the worktree stays, for the open task or its live jobs, or with uncommitted changes for
    the task's next run), 'busy' (git refused, e.g. over the checkout's own changes), 'conflict' (new)
    or 'conflict-known' (the same branch head and harness HEAD as last time: not merged again). While
    the checkout sits on a harness task's branch, not its main line, nothing is merged ('busy')."""
    own = path(p, tid)
    if not (other or own.exists()):
        return ""
    if not _is_repo(p) or TASK_BRANCH_RE.fullmatch(on_branch(p.harness)):
        return "busy"   # never merged into another task's branch the checkout was left on
    b = other or branch(tid)
    ahead = _git(p.harness, "rev-list", "--count", f"HEAD..{b}")
    outcome = ""
    if ahead.returncode == 0 and int(ahead.stdout.strip() or 0) > 0:
        # The memory commit and the merge hold the memory index lock: an index write waits for both,
        # so none lands between git's look at MEMORY.md and the merge rewriting it.
        with p.memory_locked(LAND_LOCK_WAIT_S) as locked:
            if not locked:
                return "busy"   # a writer holds it past the bound: the next pass tries again
            _commit_memory(p, tid)
            before = _git(p.harness, "rev-parse", "HEAD").stdout.strip()
            pair = [_git(p.harness, "rev-parse", b).stdout.strip(), before]
            last = (p.db.kv(CONFLICT_KEY, {}) or {}).get(str(tid))
            if last == pair + ["conflict"]:
                return "conflict-known"     # the same two commits conflict again: no merge until one moves
            # Merged aside, never in the checkout: a merge there that conflicts needs `merge --abort`
            # (a reset), which takes back what runs working in the checkout staged meanwhile.
            r, merged, conflicted = _merge_aside(p, tid, b, before)
            if merged:
                r = _fast_forward(p, tid, before, merged)
        if r.returncode != 0:
            said = (r.stdout + r.stderr).strip()
            # a refusal over files another live run is editing passes when it commits; else it needs a hand
            if conflicted or ("would be overwritten" in said and not checkout_busy(p, tid)):
                # a refusal leaves no MERGE_HEAD and passes once the checkout's changes go: tried each pass
                seen = pair + ["conflict" if conflicted else "refused"]
                if last == seen:
                    return "conflict-known"
                _set_conflicted(p, tid, seen)
                _report_conflict(p, tid, said, b)
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
    if other:
        return outcome
    if dirty(own) or held(p, tid):
        return "kept"
    from .worktree import unkeep
    unkeep(own)   # the daemon's keep lock (worktree.keep) only; any other lock makes git refuse
    r = _git(p.harness, "worktree", "remove", str(own), timeout=120)
    if r.returncode != 0 and own.exists():
        return outcome or "kept"
    _git(p.harness, "branch", "-d", b)
    _set_conflicted(p, tid, None)
    return outcome or "merged"


MERGE_ASIDE = ".merge-aside"   # under DIR: land's scratch worktree (not a task's: no t<id> name)


def _merge_aside(p: Project, tid: int, b: str, base: str) -> tuple[subprocess.CompletedProcess, str, bool]:
    """Merge branch `b` into commit `base` in a scratch worktree of the harness, so a conflict (or a
    leftover merge) never touches the checkout's files or index. Returns git's answer, the merge
    commit ('' when it failed) and whether files conflicted. The scratch worktree is removed after."""
    aside = p.state / DIR / MERGE_ASIDE
    _drop_aside(p, aside)
    r = _git(p.harness, "worktree", "add", "-q", "--detach", str(aside), base, timeout=120)
    if r.returncode != 0:
        _drop_aside(p, aside)
        return r, "", False
    try:
        r = _git(aside, "merge", "--no-edit", "-m", f"{MERGE_MSG} #{tid} ({b})", b, timeout=120)
        if r.returncode == 0:
            return r, _git(aside, "rev-parse", "HEAD").stdout.strip(), False
        return r, "", bool(_git(aside, "diff", "--name-only", "--diff-filter=U").stdout.strip())
    finally:
        _drop_aside(p, aside)


def _drop_aside(p: Project, aside: Path) -> None:
    if aside.exists():
        _git(p.harness, "worktree", "remove", "--force", "--force", str(aside), timeout=120)
        if aside.exists():
            import shutil
            shutil.rmtree(aside, ignore_errors=True)
    _git(p.harness, "worktree", "prune")


def _fast_forward(p: Project, tid: int, before: str, merged: str) -> subprocess.CompletedProcess:
    """Move the harness checkout from `before` to its descendant `merged` without writing over anything
    uncommitted there: never `checkout --`, reset, stash or merge --abort. The ref moves first (a refusal,
    such as the template guard's, then leaves every file as it was), then git's two-tree read-tree updates
    only files that still match `before` and refuses over any other. An uncommitted copy of a file the
    merge changes lands as it is only when it already holds the merged content (its index entry, or its
    working copy over an index entry still at `before`'s): that one is staged, never rewritten, and only
    while no other harness run works in the checkout (its copies are its own). Any other such copy stops
    the land and is named; a refusal after staging takes the staging back. Returns git's answer."""
    h = p.harness
    raw = _git(h, "diff", "-z", "--raw", "--no-renames", "--no-abbrev", before, merged)
    words = raw.stdout.split("\0")
    ends: dict[str, tuple] = {}   # path -> (before's (mode, blob) or None, merged's)
    for meta, f in zip(words[0::2], words[1::2]):
        m = meta.lstrip(":").split()
        if raw.returncode == 0 and len(m) >= 5:
            ends[f] = (None if set(m[2]) == {"0"} else (m[0], m[2]), None if set(m[3]) == {"0"} else (m[1], m[3]))
    index: dict[str, tuple] = {}
    for e in _git(h, "ls-files", "-s", "-z").stdout.split("\0"):
        meta, _, f = e.partition("\t")
        if f in ends:
            mode, blob, stage = meta.split()
            index[f] = (mode, blob) if stage == "0" and f not in index else ("conflicted", "")
    unstaged = set(_git(h, "diff", "-z", "--name-only", "--no-renames").stdout.split("\0"))
    stage, keep = [], []
    for f, (old, new) in sorted(ends.items()):
        cur, fp = index.get(f), h / f
        if cur == old and f not in unstaged and not (cur is None and os.path.lexists(fp)):
            continue   # as `before` has it: git updates it
        if cur == new or new is None and cur in (old, None) and not os.path.lexists(fp):
            continue   # already the merged content staged (any further edit stays), or already deleted
        if (new is not None and cur in (old, None) and not fp.is_symlink() and fp.is_file()
                and _git(h, "hash-object", "--", f).stdout.strip() == new[1]):
            stage.append((f, cur))
            continue
        keep.append(f)
    if keep or stage and checkout_busy(p, tid):
        return subprocess.CompletedProcess([], 1, "", "Your local changes to the following files would be "
                                           "overwritten by merge (land left them as they are):\n\t"
                                           + "\n\t".join(keep or [f for f, _ in stage]))
    with tree_writes(h):
        for f, _ in stage:
            _git(h, "update-index", "--add", "--", f)
        r = _git(h, "read-tree", "-m", "-u", "-n", before, merged, timeout=120)   # would git refuse?
        if r.returncode != 0:
            r.stderr = ("Your local changes would be overwritten by merge (land left them as they are): "
                        + r.stderr)
        else:
            r = _git(h, "update-ref", "-m", f"merge {merged}: Fast-forward", "HEAD", merged, before)
            if r.returncode == 0:
                r = _git(h, "read-tree", "-m", "-u", before, merged, timeout=120)
                if r.returncode != 0:   # refused meanwhile: the ref goes back, the files were not written
                    _git(h, "update-ref", "-m", "land refused: back", "HEAD", before, merged)
        if r.returncode != 0:
            for f, cur in stage:
                if cur is None:
                    _git(h, "update-index", "--force-remove", "--", f)
                else:
                    _git(h, "update-index", "--cacheinfo", f"{cur[0]},{cur[1]},{f}")
    if stage and r.returncode == 0:
        from .daemon import log
        log(p, f"harness worktree of task {tid}: landed with {len(stage)} uncommitted cop"
               f"{'y' if len(stage) == 1 else 'ies'} that already held the merged content, left as "
               f"they were ({', '.join(f for f, _ in stage)})")
    return r


def _commit_memory(p: Project, tid: int) -> None:
    """Commit the daemon's own uncommitted memory writes (MEMORY.md, memory/) before harness task `tid`'s
    branch merges, so git never refuses the merge over them and a clash on one is a conflict the merge
    reports, with both sides kept in commits. Left alone while another harness task's run works in
    the checkout (its edits there are its own), and where sweep_harness leaves a file (a write that
    looks cut, a boot the integrity check has not run on yet). Other uncommitted files are never
    committed: git refuses over them as before."""
    if checkout_busy(p, tid):
        return
    p.sweep_harness(min_age_s=0, paths=MEMORY_PATHS,
                    message=f"memory: commit the daemon's writes before landing harness task #{tid}")


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


def stranded(p: Project, task: dict) -> list[tuple[str, list[str]]]:
    """Harness task `task`'s named commits (named_commits) the harness has but its HEAD lacks, each
    with the local branches that contain it: [(full sha, [branch, ...])]. [] with no harness repo,
    while the task has a worktree of its own (land merges that), or when each named commit the harness
    has is in HEAD."""
    if not _is_repo(p) or path(p, task["id"]).exists():
        return []
    out = []
    for c in named_commits(task):
        full = _git(p.harness, "rev-parse", "--verify", "-q", f"{c}^{{commit}}")
        if full.returncode != 0:
            continue   # another repository's commit
        sha = full.stdout.strip()
        if _git(p.harness, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0:
            continue
        r = _git(p.harness, "branch", "--format=%(refname:short)", "--contains", sha)
        out.append((sha, r.stdout.split() if r.returncode == 0 else []))
    return out


def land_stranded(p: Project, task: dict) -> tuple[str, list[tuple[str, list[str]]]]:
    """Land a done harness task's named commits that sit outside the harness HEAD and outside a worktree
    of its own (stranded): each on a branch of the task's own, ttp/t<id>-<slug> (a worker that
    committed in another worktree of the harness repo), is merged as land merges ttp/harness-t<id>.
    Returns (outcome, the commits on no such branch, with their branches): outcome '' (nothing to do),
    'merged', or land's 'busy' / 'conflict' / 'conflict-known' (tried again on a later pass)."""
    left = stranded(p, task)
    if not left:
        return "", []
    own = re.compile(rf"ttp/t{int(task['id'])}-.+")
    alone = [(sha, bs) for sha, bs in left if not any(own.fullmatch(b) for b in bs)]
    outcome = ""
    for b in dict.fromkeys(b for _, bs in left for b in bs if own.fullmatch(b)):
        got = land(p, task["id"], b)
        if got not in ("", "merged"):
            return got, alone
        outcome = got or outcome
    return outcome, alone


def handoff_landing(p: Project, task: dict) -> tuple[str, str]:
    """Whether harness task `task`'s hand-off commits land, for a hand-off check (handoffcheck): ("merged",
    why) when each named commit the harness has is in its HEAD; ("lands", why) when some are only on the
    task's branch and that branch fast-forwards HEAD (land merges it once the run ended: the worker's
    sandbox cannot write the harness checkout), also when the branch holds commits HEAD lacks beyond the
    named ones; ("none", why) for a `commit` of "none: <why>" (nothing needed changing) while the task's
    branch has no commit HEAD lacks; ("refused", what the worker can do) for no named commit the harness
    has, one on neither, a "none:" hand-off whose branch still holds commits HEAD lacks (that work would be
    lost), or a branch that neither fast-forwards HEAD nor is in it; ("", "") with no
    harness repo. Unlike merged_commit, which lets dependents start, "lands" is not merged yet."""
    if not _is_repo(p):
        return "", ""
    b, own, main = branch(task["id"]), path(p, task["id"]), main_branch(p) or "the harness's main line"
    has_b = _git(p.harness, "rev-parse", "--verify", "-q", f"refs/heads/{b}").returncode == 0
    fwd = has_b and _git(p.harness, "merge-base", "--is-ancestor", "HEAD", f"refs/heads/{b}").returncode == 0
    rebase = (f"rebase it onto {main} in your worktree {own} (`git rebase {main}` there), commit and hand off "
              f"again; the daemon merges it when your run ends (never merge into the harness checkout yourself: "
              f"your run cannot write it)")
    behind = has_b and not fwd and _git(p.harness, "merge-base", "--is-ancestor", f"refs/heads/{b}",
                                        "HEAD").returncode == 0   # merged already, the harness moved on
    if has_b and not fwd and not behind:
        return "refused", f"branch {b} does not fast-forward the harness HEAD: {rebase}"
    from .db import load_result
    commit = str(load_result(task.get("result")).get("commit") or "").strip()
    if commit[:5].lower() == "none:":
        ahead = unmerged(p, task["id"])
        if ahead == 0:
            return "none", commit[5:].strip() or "nothing needed changing"
        return "refused", (f"the hand-off's `commit` is \"none:\" but branch {b} has "
                           f"{'commits' if ahead < 0 else f'{ahead} commit(s)'} the harness HEAD lacks: name "
                           f"its head commit in `commit` so the daemon merges it, or drop them from {b} (in {own})")
    found, lands = [], False
    for c in named_commits(task):
        full = _git(p.harness, "rev-parse", "--verify", "-q", f"{c}^{{commit}}")
        if full.returncode != 0:
            continue   # another repository's commit
        sha = full.stdout.strip()
        found.append(sha[:10])
        if _git(p.harness, "merge-base", "--is-ancestor", sha, "HEAD").returncode == 0:
            continue
        if not (fwd and _git(p.harness, "merge-base", "--is-ancestor", sha, f"refs/heads/{b}").returncode == 0):
            return "refused", (f"commit {sha[:10]} is neither in the harness HEAD nor on branch {b}: commit your "
                               f"work {f'on {b} (in {own})' if has_b else 'in the harness checkout'} and name "
                               f"that commit in the hand-off")
        lands = True
    if not found:
        return "refused", (f"the hand-off names no commit of the harness: put the commit on {b} (or in the "
                           f"harness HEAD) in its `commit` field")
    if lands or (fwd and unmerged(p, task["id"]) > 0):   # its branch has more for the daemon to merge
        return "lands", (f"{', '.join(found)} on branch {b}, which fast-forwards the harness HEAD: the daemon "
                         f"merges it when the run ends")
    return "merged", f"{', '.join(found)} in the harness HEAD"


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


def _report_conflict(p: Project, tid: int, why: str, b: str = "") -> None:
    """Tell the coordinator once per branch head: the task's commits wait on the branch (`b`, default
    its own)."""
    b = b or branch(tid)
    head = _git(p.harness, "rev-parse", b).stdout.strip()
    seen = p.db.kv(REPORTED_KEY, {}) or {}
    if seen.get(str(tid)) == head:
        return
    with p.db.tx():
        seen[str(tid)] = head
        p.db.set_kv(REPORTED_KEY, seen)
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (time.time(), "harness", "harness_merge_conflict", "normal",
                f"Harness task #{tid}'s commits on branch {b} do not merge into the harness checkout; "
                f"they wait {f'in {path(p, tid)}' if b == branch(tid) else 'on that branch'}. A harness task "
                f"should merge them by hand (`git merge {b}` in the harness) and resolve, keeping both sides' intents. "
                f"git said: {why[-400:]}", "queued", tid))


def sweep(p: Project, running: set[int]) -> dict[int, str]:
    """Land every harness worktree whose task has no live run (`running`: task ids with one); a
    worktree whose task is gone from the database is landed too. Each is removed only once its task
    ended and nothing holds it (land). Returns task id -> outcome."""
    root = p.state / DIR
    if not root.is_dir():
        return {}
    if _is_repo(p) and landing_merge(p.harness):    # a merge of ours a killed daemon left: undo it first
        with tree_writes(p.harness):
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

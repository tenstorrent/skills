# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp land <head>`: land a reviewed head on a push branch that is a local branch of a code repo
with no git remote, where `ttp push` has nowhere to push. Without it every move of that branch
between a review and its landing costs a rebase task and a re-review.

It replays the head's commits that the branch lacks (git cherry) onto the branch's tip in a scratch
worktree, each with a Ttp-Replayed-From trailer naming the reviewed commit, runs delivery.push_checks
there, and fast-forwards the branch to the result (in the worktree that has it checked out, when
one does). The landing is recorded for the task (task_landings), so `landed:#<id>` passes for it and
the review that ran it, and the review's verdict covers the landed commit too (reviewreuse).

A commit that becomes empty on the branch's tip (its change is already there) is dropped. When the
branch moves during the checks or the fast-forward, it starts over on the new tip, up to MOVE_TRIES
times in all.

Exit codes: 0 landed (or already there); 1 git failed for a reason other than a conflict; 2 refused;
3 a conflict (unmerged paths); 4 a check failed; 5 the branch kept moving; 6 the
checkout that has the branch has uncommitted changes; 75 another landing held the branch. Only 0
moves the branch; the scratch worktree is always removed.

`--root <repo>` lands in that repo instead of the current directory's, e.g. a temporary clone with
its remote removed, to try a landing first. A landing in a repo other than the project's (one whose
git directory is not that of the project root or the current directory) is not recorded, and lands
on a main/master push branch without delivery.allow_protected_push_branch: it has no git remote,
so nothing is published; the project's own main/master still needs that setting. `--dry-run` writes nothing and moves no ref: it replays the
commits in a throwaway shared clone (so not even the repo's object store changes), says whether they
conflict, which push checks would run (it does not run them), whether the branch's checkout is dirty,
and the landing and review records it would write, and exits with the code a landing would get as
far as it can tell without running the checks (0, 1, 2, 3, 4 when no check could pass, or 6)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import push, reviewreuse
from .project import Project, push_allowed
from . import gitmemo

LANDED, FAILED, REFUSED, CONFLICT, CHECKS_FAILED, MOVED, DIRTY, BUSY = 0, 1, push.REFUSED, push.CONFLICT, \
    push.CHECKS_FAILED, push.KEPT_MOVING, push.REJECTED, push.BUSY
MOVE_TRIES = 3   # landings in all while the branch keeps moving during the checks
STEP = "If it passes, land the head you reviewed with `ttp land"   # the review spec's land step starts so


def _git(repo: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    # The C locale keeps git's messages in English whatever the user's: _replay reads one.
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=gitmemo.timeout(timeout, args),
                          stdin=subprocess.DEVNULL,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": ""})


def local_branch(p: Project, repo: Path, scratch: bool = False) -> str:
    """The push branch `ttp land` lands on: delivery.push_branch, when pushing is allowed, `repo`
    has no git remote and has that local branch (main/master only with
    delivery.allow_protected_push_branch, or in a `scratch` repo); "" otherwise."""
    return _target(p, repo, scratch)[0]


def _target(p: Project, repo: Path, scratch: bool = False) -> tuple[str, str]:
    """(local_branch, why it is "" when it is). `scratch`: `repo` is not the project's (a temporary
    clone, --root): its main/master may be landed on without delivery.allow_protected_push_branch,
    since `repo` has no git remote, so the landing publishes nothing."""
    d = p.config().get("delivery") or {}
    if not (push_allowed(d) and str(d.get("push_branch") or "").strip()) or not push.remote_less(repo):
        return "", ""
    branch = push.target(p, repo)[1]
    if branch == "HEAD" or branch in push.PROTECTED and not (scratch or push.allow_protected(d)):
        return "", (f"refusing to land on {branch}: delivery.push_branch is {branch}, a protected branch that "
                    f"`ttp land` moves in the project's own repo only when delivery.allow_protected_push_branch "
                    f"is set (only on the user's word). Name a push branch of its own, or try the landing in "
                    f"a temporary clone with its remote removed (`ttp land <head> --root <clone>`)"
                    if branch != "HEAD" else "")
    return (branch if push._local_tip(repo, branch) else ""), ""


def step(branch: str, head: str) -> str:
    """The review spec's land step for local branch `branch`; `head` is the commit to land."""
    return (f"{STEP} {head}` from any checkout of the code repo (it has no git remote, so `ttp push` has "
            f"nowhere to push): it replays the head's commits onto {branch}'s tip in a scratch worktree, runs "
            f"the project's checks there and fast-forwards {branch}. Exit 0: landed; report the commit it "
            f"prints. Exit 3: a conflict with newer commits on {branch}, nothing landed: hand off `failed` with "
            f"a finding to rebase the change onto {branch}, naming the files. Exit 5 ({branch} kept moving) or 6 "
            f"(the checkout that has {branch} has uncommitted changes): not a fault of the change; run it once "
            f"more, and if it repeats hand off `waiting` naming it, with retry_after_s 1800 and a retry_when that "
            f"only waits for that time. Exit 75: hand off `waiting` with the retry_when it prints. Any other "
            f"exit: nothing landed; hand off `failed` with its output. If the project's checks may outlast one "
            f"tool call, run it as `ttp detach land -- ttp land {head}` and hand off `waiting` with the "
            f"retry_when that prints; on waking, read land.log and land.rc in your run folder and act on its "
            f"exit code as above. Never move {branch} by hand, and do not approve it for the push queue.")


def _holder(repo: Path, branch: str) -> str:
    """The worktree (`repo` itself included) that has local branch `branch` checked out, or ""."""
    cur = ""
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = line[9:]
        elif line == f"branch refs/heads/{branch}":
            return cur
    return ""


def _replay(wt: Path, commits: list[str], say: Callable[[str], None]) -> int:
    """Cherry-pick `commits` in scratch worktree `wt`, each keeping its author, date and message and
    gaining a trailer naming the commit it replays (landed.on_branch reads it); one that becomes
    empty on the new base is dropped. 0, CONFLICT on unmerged paths, or FAILED."""
    for c in commits:
        r = _git(wt, "cherry-pick", "--allow-empty", c)
        if r.returncode != 0:
            files = _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()
            out = (r.stderr or r.stdout).strip()
            first = out.splitlines()[0] if out else ""
            staged = _git(wt, "diff", "--cached", "--quiet", "HEAD").returncode
            _git(wt, "cherry-pick", "--abort")
            if files:
                say(f"conflict replaying {c[:10]} in {', '.join(files[:8])}: {first}")
                return CONFLICT
            if staged == 0 and "empty" in out:
                say(f"dropped {c[:10]}: its change is already on the branch")
                continue
            say(f"could not replay {c[:10]} (not a conflict): {out[-400:]}")
            return FAILED
        msg = _git(wt, "log", "-1", "--format=%B").stdout
        msg = subprocess.run(["git", "-C", str(wt), "interpret-trailers", "--trailer", f"{push.landed.REPLAYED}: {c}"],
                             input=msg, text=True, capture_output=True, timeout=60).stdout or msg
        if subprocess.run(["git", "-C", str(wt), "commit", "-q", "--amend", "--no-verify", "--allow-empty", "-F", "-"],
                          input=msg, text=True, capture_output=True, timeout=60).returncode != 0:
            say(f"could not add the replay trailer to {c[:10]}")
            return FAILED
    return 0


def _checks(p: Project, wt: Path, new: str, base: str, say: Callable[[str], None]) -> int:
    """delivery.push_checks on `new` in scratch worktree `wt`, as `ttp push` runs them on its rebased
    head: 0 when they pass. With none configured, only a docs-only change passes."""
    d = p.config().get("delivery") or {}
    checks = push.with_host(push.check_list(push.repo_checks(d, wt, p)[0]), d.get("checks_host"))
    if not checks:
        if code := push.code_paths(wt, base, new):
            say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}); not landing")
            return CHECKS_FAILED
        return 0
    try:
        todo, skipped = push.applicable(wt, new, checks, say, base)
    except push.ScopeError as e:
        say(f"{e}; not landing")
        return CHECKS_FAILED
    if not todo:
        if push.outside_scope(checks, skipped):
            say(f"{new[:10]}: {push.OUT_OF_SCOPE}")
            return 0
        say(f"{new[:10]}: {push.NONE_APPLY}; not landing: {push.NONE_APPLY_FIX}")
        return CHECKS_FAILED
    env = push.check_env("target", base)
    if why := push.runner_wait(todo, wt, env, say, p):
        return why
    try:
        slot = push.check_slot(new, say, proj=p)
    except push.checkcap.Unavailable as e:
        say(f"{e}; not landing")
        return CHECKS_FAILED
    with slot:
        started = time.time()
        env = {**env, **slot.env()}
        for line in push.sandbox_lines(todo):
            say(line)
        for cmd in todo:
            if (rc := push._run_one(cmd, wt, env, say)) != 0:
                if rc != BUSY:
                    say(f"check failed on {new[:10]}: {cmd}{push.where(cmd)}; not landing")
                return rc
    push.record_check_s(p, time.time() - started)
    return 0


def _move(repo: Path, branch: str, base: str, new: str, say: Callable[[str], None]) -> int:
    """Fast-forward local branch `branch` from `base` to `new`: in the worktree that has it checked
    out (only while it has no uncommitted changes), else the ref itself, guarded by `base`."""
    if push._local_tip(repo, branch) != base:
        say(f"local branch {branch} moved off {base[:10]} during the checks; nothing landed. Run `ttp land` again")
        return MOVED
    if held := _holder(repo, branch):
        wt = Path(held)
        dirty = _git(wt, "status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0 or dirty.stdout.strip():
            say(f"{branch} is checked out in {held} with uncommitted changes: fast-forwarding it there would mix "
                f"them with this work; nothing landed. Commit or discard them there, then run `ttp land` again")
            return DIRTY
        r = _git(wt, "merge", "--ff-only", "--quiet", new)
        if r.returncode != 0 and push._local_tip(repo, branch) != base:
            say(f"local branch {branch} moved off {base[:10]} in {held}; nothing landed. Run `ttp land` again")
            return MOVED
        if r.returncode != 0:
            say(f"could not fast-forward {branch} in {held}: {(r.stderr or r.stdout).strip()}; nothing landed")
            return DIRTY
        return LANDED
    r = _git(repo, "update-ref", "-m", "ttp land", f"refs/heads/{branch}", new, base)
    if r.returncode != 0:
        say(f"could not move local branch {branch}: {r.stderr.strip()}; nothing landed")
        return MOVED
    return LANDED


def record(p: Project, tid: int | None, head: str, new: str, branch: str, dry: bool = False) -> list[str]:
    """Record that task `tid` landed reviewed `head` as `new` on local branch `branch`: a task_landings
    row (landed.task_shas: `landed:#<id>` for it and every task it carries), and, on a review that
    recorded `head` as the head it reviews, the same verdict for `new` (reviewreuse). Returns a line
    per record; with `dry`, writes nothing."""
    task = p.db.task(tid) if tid else None
    if not task:
        return []
    lines = [f"a landing of task #{tid} as {new[:10]} on {push.LOCAL}/{branch} (task_landings; `landed:#{tid}` passes)"]
    if not dry:
        p.db.x("INSERT INTO task_landings(marker, task, sha, target, ts) VALUES(?,?,?,?,?) "
               "ON CONFLICT(marker) DO NOTHING", (f"land-t{tid}-{new}", tid, new, f"{push.LOCAL}/{branch}", time.time()))
    if task.get("kind") == "review" and new != head and (rec := reviewreuse.recorded(task)) and rec[0] == head:
        labels = push._labels(task)
        lines.append(f"review #{tid}'s verdict on {head[:10]} also for {new[:10]} (reviewreuse)")
        if not dry and (lb := reviewreuse.head_label(new, rec[1])) not in labels:
            p.db.update_task(tid, labels=labels + [lb])
    return lines


def _record(p: Project, tid: int | None, head: str, new: str, branch: str, say: Callable[[str], None],
            dry: bool) -> None:
    for line in record(p, tid, head, new, branch, dry):
        if dry:
            say(f"dry run: would record {line}")


def land(p: Project, repo: Path, head: str, tid: int | None = None,
         say: Callable[[str], None] = lambda m: print(f"ttp land: {m}", file=sys.stderr), dry: bool = False,
         recorded: bool = True) -> int:
    """Land `head` on the project's local push branch (local_branch); see the module's exit codes.
    `dry`: only say what it would do (the module's --dry-run). `recorded` False (a repo other than the
    project's): land, but record nothing for the task."""
    if not recorded:
        say(("dry run: would not record " if dry else "not recording ") + f"the landing: {repo} is not the "
            "project's repo, so the project's task records stay as they are")
        tid = None
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    branch, why = _target(p, repo, scratch=not recorded)
    if why:
        say(why)
        return REFUSED
    if not branch:
        say("nothing to land on: `ttp land` is for a push branch (delivery.push_branch) that is a local branch "
            "of a code repo with no git remote; elsewhere use `ttp push`")
        return REFUSED
    full = _git(repo, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}").stdout.strip()
    if not full:
        say(f"no commit {head} in {repo}")
        return REFUSED
    if dry:
        return _attempt(p, repo, branch, full, tid, say, dry)
    d = p.config().get("delivery") or {}
    wait = d.get("push_wait_s")
    lock = push.take(p, push.LOCAL, branch, push.default_wait(push.last_check_s(p)) if wait in (None, "")
                     else push.wait_of(wait), say=say)
    if lock is None:
        return BUSY
    try:
        for n in range(1, MOVE_TRIES + 1):
            rc = _attempt(p, repo, branch, full, tid, say)
            if rc != MOVED or n == MOVE_TRIES:
                return rc
            say(f"starting over on {branch}'s new tip ({n + 1} of {MOVE_TRIES})")
        return MOVED
    finally:
        lock.close()


def _attempt(p: Project, repo: Path, branch: str, full: str, tid: int | None, say: Callable[[str], None],
             dry: bool = False) -> int:
    """One landing of `full` on `branch`'s current tip, under the landing lock (`dry`: a dry run)."""
    base = push._local_tip(repo, branch)
    if _git(repo, "merge-base", "--is-ancestor", full, base).returncode == 0:
        say(f"{full[:10]} is already on local branch {branch}")
        _record(p, tid, full, full, branch, say, dry)
        return LANDED
    fork = _git(repo, "merge-base", base, full).stdout.strip()
    if not fork:
        say(f"{full[:10]} shares no history with local branch {branch}")
        return REFUSED
    ff = fork == base
    commits = [] if ff else [ln.split()[1] for ln in _git(repo, "cherry", base, full).stdout.splitlines()
                             if ln.startswith("+ ")]
    if not ff and _git(repo, "rev-list", "--merges", f"{fork}..{full}").stdout.strip():
        say(f"{full[:10]} has merge commits past {fork[:10]}: they cannot be replayed; nothing landed")
        return REFUSED
    if not ff and not commits:
        say(f"every commit of {full[:10]} is already on local branch {branch} (same patch)")
        _record(p, tid, full, base, branch, say, dry)
        return LANDED
    if dry:
        return _dry(p, repo, branch, base, full, commits, tid, say)
    return push.in_check_scratch(p, lambda: _land(p, repo, branch, base, full, commits, tid, say), "ttp land",
                                 repo)


def _land(p: Project, repo: Path, branch: str, base: str, full: str, commits: list[str], tid: int | None,
          say: Callable[[str], None]) -> int:
    with tempfile.TemporaryDirectory(prefix="ttp-land-") as tmp:
        wt = Path(tmp) / "wt"
        if _git(repo, "worktree", "add", "-q", "--detach", str(wt), base if commits else full).returncode:
            say("could not make a scratch worktree; nothing landed")
            return REFUSED
        try:
            if commits and (rc := _replay(wt, commits, say)):
                say(f"nothing landed; local branch {branch} is still at {base[:10]}")
                return rc
            new = _git(wt, "rev-parse", "HEAD").stdout.strip()
            if new == base:
                say(f"every commit of {full[:10]} is already on local branch {branch}")
                record(p, tid, full, base, branch)
                return LANDED
            if rc := _checks(p, wt, new, base, say):
                return rc
            if rc := _move(repo, branch, base, new, say):
                return rc
        finally:
            _git(repo, "worktree", "remove", "--force", str(wt))
            _git(repo, "worktree", "prune")
    record(p, tid, full, new, branch)
    how = f"replayed {len(commits)} commit(s) of {full[:10]} as {new[:10]}" if commits else f"fast-forwarded to {new[:10]}"
    say(f"landed {new} on local branch {branch} ({how})")
    return LANDED


def _dry(p: Project, repo: Path, branch: str, base: str, full: str, commits: list[str], tid: int | None,
         say: Callable[[str], None]) -> int:
    """The dry run of _land: the replay in a shared clone of `repo` (its own objects, refs and
    worktree, all thrown away), then what the checks, the move and the records would be."""
    with tempfile.TemporaryDirectory(prefix="ttp-land-dry-") as tmp:
        wt = Path(tmp) / "clone"
        if _git(repo, "clone", "-q", "--shared", "--no-checkout", str(repo), str(wt)).returncode \
                or _git(wt, "checkout", "-q", "--detach", base if commits else full).returncode:
            say("dry run: could not make a scratch clone")
            return REFUSED
        for key in ("user.name", "user.email"):
            if val := _git(repo, "config", key).stdout.strip():
                _git(wt, "config", key, val)
        if commits and (rc := _replay(wt, commits, say)):
            say(f"dry run: a landing would exit {rc}; local branch {branch} would stay at {base[:10]}")
            return rc
        new = _git(wt, "rev-parse", "HEAD").stdout.strip()
        if new == base:
            say(f"every commit of {full[:10]} is already on local branch {branch}")
            _record(p, tid, full, base, branch, say, True)
            return LANDED
        how = (f"replays {len(commits)} commit(s) of {full[:10]} onto {base[:10]} cleanly (as {new[:10]} here; "
               f"the landing's commit ids differ by their commit time)") if commits else \
            f"fast-forwards {branch} from {base[:10]} to {new[:10]}"
        say(f"dry run: {how}")
        d = p.config().get("delivery") or {}
        checks = push.with_host(push.check_list(push.repo_checks(d, wt, p)[0]), d.get("checks_host"))
        if not checks:
            if code := push.code_paths(wt, base, new):
                say(f"dry run: no checks configured, and this change touches more than docs ({', '.join(code[:3])}); "
                    f"a landing would exit {CHECKS_FAILED}")
                return CHECKS_FAILED
            say("dry run: no checks configured; a docs-only change lands without them")
        else:
            try:
                todo, skipped = push.applicable(wt, new, checks, say, base)
            except push.ScopeError as e:
                say(f"dry run: {e}; a landing would exit {CHECKS_FAILED}")
                return CHECKS_FAILED
            if todo:
                say(f"dry run: would run the push checks on the replayed head (not run now): {'; '.join(todo)}")
            elif not push.outside_scope(checks, skipped):
                say(f"dry run: {push.NONE_APPLY}; a landing would exit {CHECKS_FAILED}: {push.NONE_APPLY_FIX}")
                return CHECKS_FAILED
    if held := _holder(repo, branch):
        dirty = _git(Path(held), "status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0 or dirty.stdout.strip():
            say(f"dry run: {branch} is checked out in {held} with uncommitted changes; a landing would exit {DIRTY}")
            return DIRTY
    say(f"dry run: then fast-forwards {branch} from {base[:10]}" + (f" in {held}" if held else ""))
    _record(p, tid, full, new, branch, say, True)
    say("dry run: nothing written, no ref moved")
    return LANDED


def run(p: Project, args) -> int:
    """`ttp land <head> [--task <id>] [--root <repo>] [--dry-run]`: the task defaults to the run's
    own (TTP_TASK), the repo to the current directory's."""
    tid = args.task if args.task is not None else (int(os.environ["TTP_TASK"]) if re.fullmatch(
        r"[0-9]+", os.environ.get("TTP_TASK", "")) else None)
    other = getattr(args, "root", None)
    root = Path(other).expanduser() if other else Path.cwd()
    if not root.is_dir():
        print(f"ttp land: --root {root}: no such directory", file=sys.stderr)
        return REFUSED
    ours = not other or _common(root) in {_common(Path.cwd()), _common(p.root)} - {""}
    return land(p, root, args.head, tid, dry=bool(getattr(args, "dry_run", False)), recorded=ours)


def _common(path: Path) -> str:
    """`path`'s git directory shared by all its worktrees (resolved), or "" outside a repo."""
    out = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(out.stdout.strip()).resolve()) if out.returncode == 0 and out.stdout.strip() else ""

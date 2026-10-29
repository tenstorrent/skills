# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Isolated workspaces for code tasks: one git worktree and branch per task, under the project's
ignored folder, so parallel workers never share a working tree with each other or the user."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .db import continues_id
from .project import Project, durable_write


def _git(cwd: Path, *args: str, check: bool = True, timeout: float = 120, env: dict | None = None) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


def is_git(path: Path) -> bool:
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def git_dirs(path: Path) -> list[str]:
    """The git metadata a commit in `path` writes: the worktree's own gitdir (its index, HEAD and
    locks, under <common>/worktrees/<name>) and the shared .git directory, in that order, without
    duplicates. Codex's sandbox makes the gitdir a worktree's `.git` file points to read-only, even
    inside a writable common directory, so a worker must be granted it by name."""
    try:
        out = _git(path, "rev-parse", "--git-dir", "--git-common-dir", check=False).splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    found = [(Path(path) / line).resolve() for line in out[:2] if line]
    return list(dict.fromkeys(str(d) for d in found if d.is_dir()))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


# "branch <name>", "branch: <name>" or "branch `<name>`" in the charter.
CHARTER_BRANCH = re.compile(r"\bbranch\b[:\s]+`?([A-Za-z0-9][\w./-]*[\w-])`?")


def charter_branches(p: Project) -> list[str]:
    """Branch names the charter gives as `branch <name>`, in order of appearance, without main,
    master and HEAD (a charter says those to forbid them)."""
    try:
        text = p.charter_path.read_text()
    except OSError:
        return []
    out: list[str] = []
    for name in CHARTER_BRANCH.findall(text):
        if name not in ("HEAD", "main", "master") and name not in out:
            out.append(name)
    return out


def _has(p: Project, ref: str) -> bool:
    return bool(_git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False))


def base_ref(p: Project) -> str:
    """Where code tasks branch from, the first of: `delivery.base_ref` as set; the project's working
    branch, `delivery.push_branch`, then a branch the charter names (`branch <name>`), each as
    origin/<name> when the remote has it (a local branch of that name may be behind; ensure()
    fetches first), else the local name; the remote's default branch (origin/HEAD); the
    checked-out branch, never a detached HEAD or a branch the root was moved to (root_base)."""
    return named_base(p) or root_base(p)


ROOT_HOME_KEY = "root_home"   # kv: the branch the daemon last saw the project root's checkout on, no alert open


def root_branch(p: Project) -> str:
    """The branch the project root's checkout belongs on: `worktree.root_branch` as set, else the one
    the daemon last saw it on with no root alert open (ROOT_HOME_KEY); "" when neither is known."""
    w = p.config().get("worktree") or {}
    if str(w.get("root_branch") or "").strip():
        return str(w["root_branch"]).strip()
    try:
        return str(p.db.kv(ROOT_HOME_KEY, "") or "")
    except Exception:
        return ""


def root_base(p: Project) -> str:
    """base_ref's last fallback: the root's checked-out branch, unless the root is detached or on
    another branch than root_branch; then root_branch when it exists, else "" (resolve_base refuses:
    no worktree is branched from a HEAD a task moved the root to)."""
    cur = _git(p.root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    home = root_branch(p)
    if home and cur != home and _has(p, f"refs/heads/{home}"):
        return home
    return cur


def named_base(p: Project) -> str:
    """base_ref without its last fallback (the checked-out branch): "" when nothing names a base."""
    d = p.config().get("delivery") or {}
    if d.get("base_ref"):
        return d["base_ref"]
    for ref in [str(d.get("push_branch") or "").strip(), *charter_branches(p)]:
        for cand in (f"origin/{ref}", ref) if ref else ():
            if _has(p, cand):
                return cand
    return _git(p.root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)


def git_timeout(p: Project, scale: float = 1.0) -> float:
    """The timeout for the git commands that make a task's workspace: `worktree.git_timeout_s`
    (default 120 s) scaled by the host's load per CPU when above 1 (a loaded host checks out
    slower) and by `scale` (a later attempt waits longer), at most `worktree.git_timeout_max_s`
    (default 900 s)."""
    w = p.config().get("worktree") or {}
    base = float(w.get("git_timeout_s") or 120)
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except (OSError, AttributeError):
        load = 0.0
    return min(float(w.get("git_timeout_max_s") or 900), base * max(1.0, load) * scale)


def _records(p: Project, repo: Path | None = None) -> dict[str, dict]:
    """Path -> `git worktree list --porcelain`'s record of each worktree of the repository (`repo`,
    default the project's) ({"path", "branch", "locked": its reason ("" when it has none) or None,
    "prunable": bool}). Raises when git fails or times out: an unknown record is no missing one."""
    out: dict[str, dict] = {}
    rec: dict | None = None
    for line in _git(repo or p.root, "worktree", "list", "--porcelain", timeout=git_timeout(p)).splitlines() + [""]:
        key, _, val = line.partition(" ")
        if key == "worktree":
            rec = {"path": val, "branch": "", "locked": None, "prunable": False}
        elif rec is not None and key == "branch":
            rec["branch"] = val.removeprefix("refs/heads/")
        elif rec is not None and key == "locked":
            rec["locked"] = val
        elif rec is not None and key == "prunable":
            rec["prunable"] = True
        elif not line and rec is not None:
            out[rec["path"]] = rec
            rec = None
    return out


def _record_of(records: dict[str, dict], path: Path) -> dict | None:
    want = str(path.resolve()) if path.exists() else str(path)
    return records.get(want) or records.get(str(path))


def _registered(p: Project, path: Path, repo: Path | None = None) -> dict | None:
    """git's record of the worktree at `path` (see _records), or None when git has none there."""
    return _record_of(_records(p, repo), path)


def _whole(p: Project, path: Path, rec: dict, branch: str) -> bool:
    """Whether the registered worktree at `path` (`rec`, from _registered) is whole and can be reused
    (True), or positively half-made, so removing it loses nothing (False): locked "initializing" by a
    `git worktree add` that never finished its checkout (no `.git` file, no index or no HEAD), so no
    task ever ran in it. Anything else is kept: a worktree with an index stays whatever it holds (a
    whole one left locked "initializing" is unlocked), an unlocked one is never removed. Raises,
    touching nothing, when a check fails or times out, or the worktree is on another branch."""
    adding = (rec["locked"] or "").startswith("initializing")
    try:
        if not (path / ".git").is_file():
            if adding:
                return False
            raise RuntimeError("it has no .git file")
        t = git_timeout(p)
        top, index = _git(path, "rev-parse", "--show-toplevel", "--git-path", "index", timeout=t).splitlines()[:2]
        if Path(top).resolve() != path.resolve():
            raise RuntimeError(f"git finds {top} there")
        if adding and not (path / index).exists():
            return False
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
                             capture_output=True, text=True, timeout=t)
        if out.returncode != 0 or not out.stdout.strip():
            if adding and out.returncode == 1 and not out.stderr.strip():
                return False
            raise RuntimeError(f"its HEAD does not resolve {out.stderr.strip()[:200]}")
        if rec["branch"] and rec["branch"] != branch:
            raise RuntimeError(f"it is on branch {rec['branch']}, not {branch}")
        if adding:
            _git(p.root, "worktree", "unlock", str(path), timeout=t)
        return True
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"worktree {path} left as it is: {e}") from e


STALE_PREFIX = ".stale-"   # worktrees/.stale-<UTC time>/<name>: a folder _clear_partial moved aside


def _clear_partial(p: Project, path: Path, rec: dict | None, repo: Path | None = None,
                   task: dict | None = None) -> None:
    """Remove what an unfinished `git worktree add` left at `path` so it can be made again: a half-made
    registered worktree (`rec`, which _whole found half-made), or git's record of one whose directory is
    gone. A directory git does not know holding more than a `.git` file is never deleted: it is not
    ours (a copy restored from another host, say). It moves aside to <parent>/.stale-<UTC time>/<name>
    and a worktree_recovered event says where. `repo`: the repository it belongs to (default the
    project's); `task`: the task whose worktree this is, for the event."""
    repo = repo or p.root
    if rec and not rec["prunable"]:
        _git(repo, "worktree", "remove", "--force", "--force", str(path), check=False, timeout=git_timeout(p))
    _git(repo, "worktree", "prune", check=False, timeout=git_timeout(p))
    if path.is_dir() and not _registered(p, path, repo):
        if any(c.name != ".git" for c in path.iterdir()):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            dest = next(d for n in range(1000) if not (d := path.parent / f"{STALE_PREFIX}{stamp}"
                                                       f"{f'-{n}' if n else ''}" / path.name).exists())
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), "daemon", RECOVERED_EVENT, "low",
                    f"{path} was a folder git does not know as a worktree: moved aside to {dest}"[:500],
                    json.dumps({"stale": str(dest), "was": str(path)}), "handled", task["id"] if task else None))
            return
        shutil.rmtree(path)


def _reusable(p: Project, path: Path, branch: str) -> tuple[bool, dict | None]:
    """(whether the worktree at `path` is whole, git's record of it); see _whole."""
    rec = _registered(p, path)
    if rec and not rec["prunable"] and path.is_dir():
        return _whole(p, path, rec, branch), rec
    return False, rec


class WorkspaceStuck(RuntimeError):
    """A workspace ensure cannot make by itself: retrying will not help, so the daemon blocks the task
    at once instead of retrying it."""
    final = True


def dangling_root(p: Project) -> str | None:
    """Why the worktrees folder cannot be made, or None: it is a symlink whose target is gone (the
    project moved hosts or a mount went away), where mkdir(exist_ok=True) fails with a bare
    [Errno 17] File exists."""
    w = p.worktrees
    if not w.is_symlink() or w.exists():
        return None
    try:
        target = os.readlink(w)
    except OSError:
        target = "?"
    return (f"the worktrees folder {w} is a symlink to {target}, which does not exist; create that folder "
            f"or point the link at an existing one")


def _make_root(p: Project) -> None:
    if (why := dangling_root(p)):
        raise WorkspaceStuck(why)
    p.worktrees.mkdir(parents=True, exist_ok=True)


RECOVERED_EVENT = "worktree_recovered"   # the task's event when ensure fixed a worktree on another branch or moved a folder aside


def _owns(p: Project, task: dict, name: str) -> bool:
    """Whether branch `name` is this task's work: its own (ttp/t<id>-...), or the branch of the task
    it continues (that task's own or the one recorded on its row)."""
    old_id = continues_id(task)
    if any(name.startswith(f"ttp/t{i}-") for i in (task["id"], old_id) if i):
        return True
    old = p.db.task(old_id) if old_id else None
    return bool(old and old["branch"] == name)


def _resolved(p: Project, *refs: str) -> list[str]:
    return [r for r in refs if r and _has(p, r)]


def _raw(path: Path, *args: str, timeout: float = 120) -> bytes:
    """git's output as bytes: a diff or a file name need not be UTF-8."""
    out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.decode(errors='replace').strip()[:300]}")
    return out.stdout


def _names(path: Path, *args: str, timeout: float = 120) -> list[str]:
    """The paths a `-z` git listing names, decoded as the file system does (any bytes survive)."""
    return [e.rstrip("/") for e in (os.fsdecode(b) for b in _raw(path, *args, "-z", timeout=timeout).split(b"\0"))
            if e.strip("/")]


def _other_branch(p: Project, task: dict, path: Path, there: str, branch: str) -> str | None:
    """The task's worktree at `path` is on branch `there`, not on `branch` (the task's branch changed
    after its worktree was made). Recovered without a model, never losing work:
    - `there` has commits that neither the base nor `branch` has, and it is this task's own branch or
      the one of the task it continues (_owns): it is adopted as it is; returns `there`.
    - it has none: its untracked, ignored and modified files move to the project's
      worktree-leftovers/<t<id>-date>/ (modified tracked files with a patch, uncommitted.patch), and
      the worktree is removed, so ensure makes it again on `branch`; returns None. The branch stays.
      Ignored build and tool caches (disk.cache_dirs, as clear_caches matches them) do not move: they
      are regenerable and go with the worktree, except hand-off artifacts in them (handoff_artifacts),
      which move. prune_leftovers deletes old leftovers folders.
    Anything else (commits of another task, submodules set up in it, `branch` checked out in another
    worktree) raises WorkspaceStuck, touching nothing. Each recovery is a `worktree_recovered` event."""
    t = git_timeout(p)
    try:
        base = resolve_base(p)
    except RuntimeError:
        base = ""
    ahead = int(_git(p.root, "rev-list", "--count", there, "--not",
                     *_resolved(p, base, branch, f"origin/{branch}"), timeout=t) or 0)
    if ahead:
        if not _owns(p, task, there):
            raise WorkspaceStuck(f"worktree {path} left as it is: it is on branch {there}, not {branch}, and "
                                 f"{there} has {ahead} commit(s) of its own that are not this task's")
        _recovered(p, task, f"worktree {path} was on branch {there}, not {branch}; {there} has {ahead} commit(s) "
                   f"of this task's own, so the task now works on it", adopted=there, was=branch)
        return there
    why = inspect(path)["why"] or ""
    if why.startswith("has submodules"):
        raise WorkspaceStuck(f"worktree {path} left as it is: it is on branch {there}, not {branch}, and {why}")
    held = next((r["path"] for r in _records(p).values()
                 if r["branch"] == branch and Path(r["path"]).resolve() != path.resolve()), None)
    if held:
        raise WorkspaceStuck(f"worktree {path} left as it is: it is on branch {there}, not {branch}, and {branch} "
                             f"is checked out in {held}")
    dest = p.state / LEFTOVERS_DIR / f"t{task['id']}-{time.strftime('%Y%m%d-%H%M%S')}"
    links = set((p.config().get("worktree") or {}).get("link_paths", LINK_PATHS) or [])
    patch = _raw(path, "diff", "HEAD", "--binary", timeout=t)
    changed = [e for e in _names(path, "diff", "HEAD", "--name-only", timeout=t) if (path / e).exists()]
    others = ["ls-files", "--others", "--exclude-standard", "--directory"]
    ignored = _names(path, *others, "--ignored", timeout=t)
    caches = _caches(set(ignored), (p.config().get("disk") or {}).get("cache_dirs"))
    # As clear_caches' keep: a cache inside a hand-off artifact moves with it, and an artifact
    # inside a cache moves by itself.
    arts = handoff_artifacts(p, task["id"], path, ignored=set(ignored))
    caches = {c for c in caches if not any(c.startswith(a + "/") for a in arts)}
    held = [a for a in arts if any(a == c or a.startswith(c + "/") for c in caches)]
    loose: list[str] = []
    for e in sorted(dict.fromkeys(_names(path, *others, timeout=t) + changed + held
                                  + [i for i in ignored if i not in caches])):
        # An entry inside a directory that moves already goes with it.
        if not any(e.startswith(d + "/") for d in loose) and not ((path / e).is_symlink() and e in links):
            loose.append(e)
    size = _size(path, loose)
    if patch:
        durable_write(dest / "uncommitted.patch", patch)
    moved = move_leftovers(path, loose, dest)
    _git(p.root, "worktree", "remove", "--force", "--force", str(path), timeout=t)
    _git(p.root, "worktree", "prune", check=False, timeout=t)
    kept = f"; its {moved} uncommitted file(s){' and patch' if patch else ''} moved to {dest}" \
        if moved or patch else "; it held no uncommitted files"
    _recovered(p, task, f"worktree {path} was on branch {there} with no commits of its own, not on {branch}: "
               f"made again on {branch}{kept}", leftovers=str(dest) if moved or patch else None,
               moved=loose[:50], bytes=size, caches=sorted(caches)[:50], was=there)
    return None


def _recovered(p: Project, task: dict, text: str, **data) -> None:
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
           (time.time(), "daemon", RECOVERED_EVENT, "low", text[:500], json.dumps(data), "handled", task["id"]))


def recovery_note(p: Project, task: dict) -> str:
    """What ensure did to the task's worktree since its last run started (worktree_recovered events),
    for the worker's start note; "" when nothing."""
    last = p.db.one("SELECT MAX(started) s FROM runs WHERE task=?", (task["id"],))
    rows = p.db.q("SELECT text FROM events WHERE task=? AND kind=? AND ts>? ORDER BY id",
                  (task["id"], RECOVERED_EVENT, float((last["s"] if last else 0) or 0)))
    return "".join(f"Workspace recovered: {r['text']}.\n" for r in rows)


def ensure(p: Project, task: dict) -> tuple[Path, str]:
    """Create (or reuse) the task's worktree. A retried task continues on its own branch. A task
    carrying a branch (`pr_branch:<branch>`) works on that branch when it may (on_carried), else on
    its own branch from that branch's head. A git command that times out (git_timeout) or fails
    because an earlier, timed-out attempt left the branch or worktree behind is recovered here: a
    whole worktree is reused, a positively half-made one removed and made again (_whole), up to
    `worktree.create_attempts` (default 2) attempts, each with a longer timeout. A check that fails
    raises and leaves the worktree as it is: an error never counts as half-made. A branch is never
    deleted: one with commits beyond where it would start is checked out as it is; one without is
    reset to that start. A worktree on another branch than the task's is recovered (_other_branch)."""
    carried = on_carried(p, task) if not task.get("branch") else ""
    own = f"ttp/t{task['id']}-{slug(task['title'])}"
    path = p.worktrees / f"t{task['id']}"
    if carried and path.exists() and (_registered(p, path) or {}).get("branch") == own:
        # An earlier attempt, when the carried branch was held elsewhere, made it on the task's own
        # branch and failed before the branch was stored: it stays there.
        carried = ""
    branch = task.get("branch") or carried or own
    rec = _registered(p, path) if path.is_dir() else None
    if rec and rec["branch"] and rec["branch"] != branch and not rec["prunable"] \
            and not (rec["locked"] or "").startswith("initializing"):
        # The task's branch changed after its worktree was made: adopt the worktree's branch, or move
        # its loose files out and make it again (_other_branch).
        branch = _other_branch(p, task, path, rec["branch"], branch) or branch
    attempts = max(1, int((p.config().get("worktree") or {}).get("create_attempts") or 2))
    for n in range(attempts):
        whole, rec = _reusable(p, path, branch)
        if whole:
            if not carried and task.get("kind") != "code":
                _refresh(p, path, git_timeout(p, 1))
            break
        try:
            if path.exists() or rec:
                _clear_partial(p, path, rec, task=task)
            _create(p, task, path, branch, carried, git_timeout(p, 1 + n / 2))
            break
        except (RuntimeError, subprocess.SubprocessError):
            if n + 1 == attempts:
                if _reusable(p, path, branch)[0]:
                    break
                raise
    prepare(p, path)
    return path, branch


def _refresh(p: Project, path: Path, timeout: float) -> None:
    """A reused worktree of a non-code task (it only reads) catches up to the current base, after a
    fetch, so a retry never judges what landed from an old tree. Fast-forward only: a worktree with
    changes or commits of its own stays as it is."""
    try:
        _git(p.root, "fetch", "--quiet", "origin", check=False, timeout=timeout)
        start = resolve_base(p)
        if _git(path, "status", "--porcelain", check=False):
            return
        if subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", start],
                          capture_output=True, timeout=timeout).returncode == 0:
            _git(path, "merge", "--ff-only", "--quiet", start, check=False, timeout=timeout, env=checkout_env(p))
    except (RuntimeError, subprocess.SubprocessError):
        pass


def checkout_env(p: Project) -> dict | None:
    """The environment of the git commands that check a task's worktree out: Git LFS files stay
    pointers (GIT_LFS_SKIP_SMUDGE=1), so a checkout never downloads large objects and outlasts its
    timeout; a task that needs them runs `git lfs pull` in its worktree. `worktree.lfs_smudge: true`
    downloads them at checkout. None (git's own environment) then."""
    if (p.config().get("worktree") or {}).get("lfs_smudge"):
        return None
    return {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}


def _create(p: Project, task: dict, path: Path, branch: str, carried: str, timeout: float) -> None:
    _make_root(p)
    env = checkout_env(p)
    exists = _git(p.root, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists and carried:
        _git(p.root, "worktree", "add", str(path), branch, timeout=timeout, env=env)
        # A local copy left behind by the remote's catches up, fast-forward only.
        _git(path, "merge", "--ff-only", "--quiet", f"origin/{branch}", check=False, timeout=timeout, env=env)
        return
    if not exists:
        _git(p.root, "fetch", "--quiet", "origin", check=False, timeout=timeout)
    # A rebase starts from the branch it rebases, even when it also continues a review.
    rebase = (delivery_branch(task) or ("", False))[1]
    start = carried_head(p, task) if carried or rebase else None
    started = start if rebase else None
    start = start or (None if carried else continued_head(p, task)) or carried_head(p, task)
    start = start or (None if carried else early_head(p, task))
    try:
        start = start or resolve_base(p)
    except RuntimeError:
        if not exists:
            raise
    if exists and (not start or subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", branch,
                                                start], capture_output=True, timeout=timeout).returncode != 0):
        # Commits beyond its start (a retried task's own work): checked out as it is, never reset.
        _git(p.root, "worktree", "add", str(path), branch, timeout=timeout, env=env)
        return
    # New, or left behind with nothing of its own by an attempt that timed out: (re)made at its start.
    _git(p.root, "worktree", "add", "-B" if exists else "-b", branch, str(path), start, timeout=timeout, env=env)
    if started:
        record_rebase_start(p.root, task["id"], started)


_VALID_BRANCHES: dict[str, bool] = {}


def valid_branch(name: str) -> bool:
    """Whether `name` is one valid branch name (`git check-ref-format --branch`): never prose, several
    words, an option-like `-x`, main/master or HEAD (a task never carries a protected branch)."""
    if not name or name != name.strip() or any(c.isspace() for c in name) or name.startswith("-") \
            or name in ("HEAD", "main", "master"):
        return False
    if name not in _VALID_BRANCHES:
        r = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True, text=True)
        _VALID_BRANCHES[name] = r.returncode == 0 and r.stdout.strip() == name
    return _VALID_BRANCHES[name]


# A spec line naming the branch a task delivers onto (coordinator: task_add labels it; push: --own
# publishes onto another task's branch only for a task whose spec has one).
# The branch may be quoted, or the whole `pr_branch: <branch>` in backticks: all read the same.
SPEC_PR_BRANCH = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?(?P<w>`?)pr_branch:[ \t]*(?P<q>[`'\"]?)"
                            r"(?P<branch>[A-Za-z0-9][\w./-]*[\w-])(?P=q)(?P=w)(?=[.,;:]?(?:[ \t]|$))", re.M)


def spec_names_pr_branch(spec: str, branch: str) -> bool:
    """Whether `spec` has a `pr_branch:` line (SPEC_PR_BRANCH) naming `branch`."""
    return any(m.group("branch") == branch for m in SPEC_PR_BRANCH.finditer(spec or ""))


# A task that carries no branch, even one the task it continues carries (push.pr_branch_of): a redo
# of a mislabelled task that lands on the push branch.
NO_PR_BRANCH = "no_pr_branch"


# A task_update appends its spec text after a line `## Update`; a `pr_branch:` line there, or
# `pr_branch: ""` (no own branch), sets the task's branch from then on (recorded_pr_branch).
UPDATE_SECTION = re.compile(r"^## Update\b.*$", re.M)
SPEC_CLEARED_PR_BRANCH = re.compile(r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)?(?P<w>`?)pr_branch:[ \t]*(?:\"\"|''|``)"
                                    r"(?P=w)[ \t]*$", re.M)


def spec_line_branch(text: str) -> str | None:
    """The branch the last `pr_branch:` line of `text` names (SPEC_PR_BRANCH), as written; "" when it
    is `pr_branch: ""` (SPEC_CLEARED_PR_BRANCH); None when `text` has neither."""
    hits = [(m.start(), m.group("branch")) for m in SPEC_PR_BRANCH.finditer(text or "")]
    hits += [(m.start(), "") for m in SPEC_CLEARED_PR_BRANCH.finditer(text or "")]
    return max(hits)[1] if hits else None


def recorded_pr_branch(task: dict) -> str | None:
    """The branch `task` is recorded as delivering onto, as written (callers check valid_branch): the
    latest `pr_branch:` line a task_update appended to its spec (UPDATE_SECTION; `pr_branch: ""`
    clears it), else its `pr_branch:<branch>` label (task_add's, from its original spec's line, or a
    task_update's `pr_branch` or appended line: coordinator keeps the label in step). "" when it carries none (a cleared line, NO_PR_BRANCH); None when
    nothing names one, so push.pr_branch_of looks at the task it continues. Reviews and harness tasks
    (NEVER_OWN_KINDS) go by their labels only: their specs quote other tasks'."""
    try:
        labels = [lb for lb in json.loads(task.get("labels") or "[]") if isinstance(lb, str)]
    except (ValueError, TypeError):
        labels = []
    if task.get("kind") not in NEVER_OWN_KINDS:
        spec = str(task.get("spec") or "")
        latest = None
        for start in (m.end() for m in UPDATE_SECTION.finditer(spec)):
            nxt = UPDATE_SECTION.search(spec, start)
            part = spec[start:nxt.start() if nxt else len(spec)]
            if (named := spec_line_branch(part)) is not None:
                latest = named
        if latest is not None:
            return latest
    if NO_PR_BRANCH in labels:
        return ""
    return next((lb[10:] for lb in labels if lb.startswith("pr_branch:")), None)


def carried_branch(task: dict) -> str:
    """The branch a task delivers onto (recorded_pr_branch), or "" (also when it is not a valid
    branch name: valid_branch)."""
    found = recorded_pr_branch(task) or ""
    return found if valid_branch(found) else ""


def rebased_branch(task: dict) -> str:
    """The branch a task's `rebases:<branch>` label names: it starts from that branch's head on its own
    branch and delivers there (delivery.rebase_replace new_branch, the default) or back onto it under a
    lease (lease), or "": a rebase cannot fast-forward it."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    found = next((lb[8:] for lb in labels if isinstance(lb, str) and lb.startswith("rebases:")), "")
    return found if valid_branch(found) else ""


def named_branch(task: dict) -> str:
    """The branch `task`'s latest pr_branch record, else its `rebases:` label, names as written, even
    one valid_branch drops (main, master): `ttp push --own` refuses such a task rather than publish its
    own branch in its place."""
    named = recorded_pr_branch(task)
    if named is not None:
        return named
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return ""
    return next((lb[8:] for lb in labels if isinstance(lb, str) and lb.startswith("rebases:")), "")


REBASE_START_REF = "refs/ttp/rebase-start/t"   # + task id: the tip of the branch a rebase task started from


def record_rebase_start(repo: Path, task_id: int, sha: str) -> None:
    """Record `sha`, the tip of the branch a `rebases:` task was built on, as where its rebase
    started (REBASE_START_REF): `ttp push --own` replaces that branch only when every commit it has
    beyond this start is in the rebased HEAD (push.publish), so work pushed to it since is never lost."""
    _git(repo, "update-ref", f"{REBASE_START_REF}{task_id}", sha, check=False)


def rebase_start(repo: Path, task_id: int) -> str:
    """The commit record_rebase_start recorded for task `task_id`, or "" when none was."""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{REBASE_START_REF}{task_id}^{{commit}}", check=False)


def prune_rebase_starts(p: Project, open_tasks: list[dict]) -> list[int]:
    """Delete the REBASE_START_REF of every finished task whose worktree is gone and that no open
    task still continues or names (needed_by); returns the task ids pruned. Safe to repeat."""
    from .db import TERMINAL_TASK_STATES
    pruned = []
    for line in _git(p.root, "for-each-ref", "--format=%(refname)", REBASE_START_REF[:-1], check=False).split():
        tail = line[len(REBASE_START_REF):]
        task = p.db.task(int(tail)) if tail.isdigit() else None
        if (not task or task["status"] not in TERMINAL_TASK_STATES or (p.worktrees / f"t{task['id']}").exists()
                or needed_by(task, open_tasks)):
            continue
        _git(p.root, "update-ref", "-d", line, check=False)
        pruned.append(task["id"])
    return pruned


def delivery_branch(task: dict) -> tuple[str, bool] | None:
    """The one reading of the branch `task` itself builds on and delivers onto, for the dispatcher
    (carried_head, ensure) and `ttp push --own` (push.delivery_source) alike: (branch, rebase). The
    latest record wins: a `pr_branch:` line a task_update appended, else its pr_branch label
    (recorded_pr_branch; ("", False) when cleared: it lands on the push branch), else its
    `rebases:<branch>` label (rebase True: the branch is rewritten, so --own replaces it only under a
    lease). None when nothing names one (push.delivery_source then looks at the task it continues).
    A name that is no valid branch counts as none."""
    named = recorded_pr_branch(task)
    if named == "":
        return "", False
    if named is not None and valid_branch(named):
        return named, False
    if old := rebased_branch(task):
        return old, True
    return None


def on_carried(p: Project, task: dict) -> str:
    """The task's carried branch when its worktree may check it out: it exists here or on origin,
    no other worktree holds it (the task's own, which an earlier attempt made on it, does not
    count), and it is no shared branch (main/master, the push branch or the base work starts from).
    "" otherwise: the task then works on its own branch."""
    branch = carried_branch(task)
    if not branch:
        return ""
    d = p.config().get("delivery") or {}
    shared = {"main", "master"}
    for ref in (str(d.get("push_branch") or "").strip(), named_base(p)):
        shared |= {ref, ref.partition("/")[2]} if ref else set()
    if branch in shared:
        return ""
    own, path = p.worktrees / f"t{task['id']}", ""
    for line in _git(p.root, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == f"branch refs/heads/{branch}" and path not in (str(own), str(own.resolve())):
            return ""
    _git(p.root, "fetch", "--quiet", "origin", check=False)
    return branch if carried_head(p, task) else ""


LINEAGE_HOLD = "waits_end:"   # + task id: task_add chained this task after an open one on its branch or lineage


def lineage_hold(task: dict) -> int | None:
    """The task id a LINEAGE_HOLD label names, or None."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    return next((int(lb[len(LINEAGE_HOLD):]) for lb in labels if isinstance(lb, str)
                 and lb.startswith(LINEAGE_HOLD) and lb[len(LINEAGE_HOLD):].isdigit()), None)


def _shared_branches(p: Project) -> set[str]:
    """Branches many tasks start from without working on them alone: main, master, the push branch,
    delivery.base_ref and the charter's branches, with and without `origin/`. No git command."""
    d = p.config().get("delivery") or {}
    out = {"main", "master"}
    for ref in (str(d.get("push_branch") or "").strip(), str(d.get("base_ref") or "").strip(), *charter_branches(p)):
        if ref:
            out |= {ref, ref[7:] if ref.startswith("origin/") else ref}
    return out


def claims(p: Project, task: dict) -> tuple[set[str], set[str]]:
    """(branches, worktree paths) a task outside NEVER_OWN_KINDS works on: its recorded branch, the one
    it carries (pr_branch:) or rebases, the branch of the task it continues, and its own and that task's
    worktree paths (once that one ran: a task cancelled before it started has neither) (worktrees/t<id>: in a project root that is no git repo the worker makes its worktree
    itself, and may pick the continued task's). Shared branches (_shared_branches) never count. Empty for
    a review or harness task. No git command: dispatch runs it for every ready task each tick."""
    t = dict(task)
    if t.get("kind") in NEVER_OWN_KINDS:
        return set(), set()
    branches = {t.get("branch") or "", carried_branch(t), rebased_branch(t)}
    paths = {str(p.worktrees / f"t{t['id']}")}
    from .db import continues_id
    old = p.db.task(cid) if (cid := continues_id(t)) else None
    if old and old["kind"] not in NEVER_OWN_KINDS and (old["branch"] or old["attempts"]):   # it ran
        branches.add(old["branch"] or "")
        paths.add(str(p.worktrees / f"t{old['id']}"))
    return branches - _shared_branches(p) - {""}, paths


def clash(mine: tuple[set[str], set[str]], theirs: tuple[set[str], set[str]]) -> str:
    """What two claims share, as 'branch <b>' or 'worktree <path>' (a branch first), or ""."""
    if b := sorted(mine[0] & theirs[0]):
        return f"branch {b[0]}"
    if w := sorted(mine[1] & theirs[1]):
        return f"worktree {w[0]}"
    return ""


LINK_PATHS = [".venv"]   # worktree.link_paths: the default
NEVER_OWN_KINDS = ("review", "harness")   # reviews work in the change's worktree, harness tasks in the harness


def gets_worktree(p: Project, kind: str) -> bool:
    """Whether a task of this kind runs in a worktree of its own: code always; another kind when
    `worktree.kinds` lists it (off by default: a fresh worktree lacks the root's untracked build
    trees, outputs and submodule checkouts that such tasks often use, which no check can tell from
    disposable ones; the root's HEAD is guarded instead: Daemon.guard_root_head). Never review or harness."""
    if kind == "code":
        return True
    kinds = (p.config().get("worktree") or {}).get("kinds")
    return kind not in NEVER_OWN_KINDS and isinstance(kinds, list) and kind in kinds


# The tasks that ran in a worktree of their own, as SQL over tasks: code, and another kind
# gets_worktree gave one (its branch is ttp/t<id>-...). Their commits are on that branch only.
OWN_WORKTREE_SQL = "(kind='code' OR (kind NOT IN ('review','harness') AND branch LIKE 'ttp/t' || id || '-%'))"


def own_worktree(task) -> bool:
    """OWN_WORKTREE_SQL for one task row."""
    t = dict(task)
    return t.get("kind") == "code" or (t.get("kind") not in NEVER_OWN_KINDS
                                       and str(t.get("branch") or "").startswith(f"ttp/t{t.get('id')}-"))


def _ignores(repo: Path, rel: str) -> bool:
    """Git ignores `rel` in `repo` (a tracked path never counts as ignored)."""
    return subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
                          capture_output=True, timeout=120).returncode == 0


def prepare(p: Project, path: str | Path, root: str | Path | None = None) -> list[str]:
    """Set up a worktree of the project's repository the same way whatever made it: a code or fix
    task's (ensure), one a reviewer works in or made itself, the push queue's and after_push's
    checkouts. Today that is link_paths from the main checkout `root` (the project root by default).
    Nothing happens for the main checkout itself or a worktree of another repository. Returns the
    entries linked; failures are skipped."""
    root = Path(root or p.root)
    try:
        path = Path(path)
        top = Path(_git(path, "rev-parse", "--show-toplevel"))
        if top.resolve() == root.resolve():
            return []
        common = lambda d: (Path(d) / _git(d, "rev-parse", "--git-common-dir")).resolve()   # noqa: E731
        if common(top) != common(root):
            return []
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return []
    return link_paths(p, top, root)


def link_paths(p: Project, path: Path, root: str | Path | None = None) -> list[str]:
    """Symlink the main checkout's (`root`, the project root by default) git-ignored environment
    entries (`worktree.link_paths`, relative paths) into the worktree at `path`, so a check that runs
    `.venv/bin/python` works in a fresh worktree. An entry is linked only when it exists in the checkout, git ignores it there (so
    it is not tracked) and nothing is at its place in the worktree, tracked or not. A rule like
    `.venv/` matches directories only, not a link, so a link git would not ignore gets a `/<entry>`
    line in the repository's info/exclude: `git add -A` never commits it and the worktree still
    counts as clean. Removing the worktree deletes the link, never what it points to. Returns the
    entries linked; failures are skipped."""
    rels = (p.config().get("worktree") or {}).get("link_paths", LINK_PATHS)
    if not isinstance(rels, list):
        return []
    root, path, done = Path(root or p.root), Path(path), []
    for rel in rels:
        if not isinstance(rel, str) or not rel.strip("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
            continue
        rel = rel.strip("/")
        src, dest, made = root / rel, path / rel, False
        try:
            if (not src.exists() or os.path.lexists(dest) or not dest.parent.is_dir()
                    or not _ignores(root, rel) or _git(path, "ls-files", "--", rel, check=False)):
                continue
            os.symlink(src.resolve(), dest, target_is_directory=src.is_dir())
            made = True
            if not _ignores(path, rel):
                common = Path(path) / _git(path, "rev-parse", "--git-common-dir")
                exclude = common / "info" / "exclude"
                text = exclude.read_text() if exclude.is_file() else ""
                if f"/{rel}" not in text.splitlines():
                    durable_write(exclude, text + ("" if not text or text.endswith("\n") else "\n") + f"/{rel}\n")
            done.append(rel)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            if made:   # a link git might commit goes again
                dest.unlink(missing_ok=True)
    return done


VENV_NAMES = (".venv", "venv")


def _is_venv(path: Path) -> bool:
    """A usable Python virtual environment: pyvenv.cfg and an interpreter that exists (a venv
    whose base Python was removed has a dangling bin/python)."""
    return (path / "pyvenv.cfg").is_file() and any((path / b).exists() for b in ("bin/python", "Scripts/python.exe"))


def project_venv(p: Project, cwd: str | Path | None = None) -> Path | None:
    """The project's venv for a run in `cwd` to use (see `worktree.venv` in the config), or None:
    none is set up, it is turned off, or `cwd` has another venv of its own (VENV_NAMES)."""
    want = (p.config().get("worktree") or {}).get("venv", "auto")
    if not want or not isinstance(want, str):
        return None
    if want == "auto":
        cands = [p.root / n for n in VENV_NAMES]
    else:
        q = Path(want).expanduser()
        cands = [q if q.is_absolute() else p.root / q]
    found = next((c.resolve() for c in cands if _is_venv(c)), None)
    own = [v.resolve() for n in VENV_NAMES for v in [Path(cwd or ".") / n] if cwd and _is_venv(v)]
    return None if own and found not in own else found


def venv_env(venv: Path, path: str) -> dict[str, str]:
    """Environment that activates `venv` in front of `path` (PATH), as its activate script does."""
    bindir = venv / ("Scripts" if (venv / "Scripts/python.exe").exists() else "bin")
    return {"VIRTUAL_ENV": str(venv), "PATH": f"{bindir}{os.pathsep}{path}"}


def continued_head(p: Project, task: dict) -> str | None:
    """The head of the branch of the task this one continues (one that ran in a worktree of its
    own, own_worktree), so its commits carry over; for a continued review, the head of what it
    reviewed (reviewed_head), so a re-plan of a failed review builds on the reviewed branch.
    A branch of its own lets the old worktree stay checked out until it is pruned."""
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if old and old["kind"] == "review":
        return reviewed_head(p, old)
    if not old or not own_worktree(old) or not old["branch"]:
        return None
    for cand in (old["branch"], f"origin/{old['branch']}"):
        head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
        if head:
            return head
    return None


def covered_heads(p: Project, review: dict) -> list[str]:
    """The commits of the refs a review covers (reviewed_refs) that resolve here or on origin, without
    duplicates: the heads of the review's dependencies (the code tasks it reviews, and fix branches on
    them: dependency_refs) first, then the other spec-named ones."""
    heads = []
    for ref in _dependency_first(p, review, reviewed_refs(p, review)):
        for cand in (ref,) if _HEX.fullmatch(ref) else (ref, f"origin/{ref}"):
            head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
            if head:
                heads.append(head)
                break
    return list(dict.fromkeys(heads))


def is_ancestor(p: Project, older: str, newer: str) -> bool:
    return older == newer or subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", older, newer],
                                            capture_output=True, timeout=120).returncode == 0


def reviewed_head(p: Project, review: dict) -> str | None:
    """The commit a review reviewed: of the heads it covers (covered_heads), the one that contains all
    the others (a fix branch on top of the reviewed branch), else, when they diverged (a rebase review
    whose spec also names the old head), the first: its dependency's head over older spec-named ones.
    None when none resolves."""
    heads = covered_heads(p, review)
    for tip in heads:
        if all(is_ancestor(p, h, tip) for h in heads):
            return tip
    return heads[0] if heads else None


def dependency_refs(p: Project, review: dict) -> list[str]:
    """The branches of the tasks a review depends on, and the fix branches built on them (fix_branches)."""
    from .db import dependency_ids
    ids = [i for i in dependency_ids(review) if i is not None]
    deps = [r["branch"] for r in p.db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL AND branch!=''")
            if r["id"] in ids]
    return list(dict.fromkeys(deps + list(fix_branches(p, review, deps))))


def _dependency_first(p: Project, review: dict, refs: list[str]) -> list[str]:
    deps = set(dependency_refs(p, review))
    return [r for r in refs if r in deps] + [r for r in refs if r not in deps]


def review_workdir(p: Project, review: dict, prefer: str = "") -> Path | None:
    """Where a review runs, never the project root: a review that rebases or checks out a head there
    moves the checkout other tasks branch from. The worktree under worktrees/ that holds a branch the
    review covers (`prefer` first, e.g. a push-conflict fix's branch, then its dependencies' branches;
    reviewed_refs), else one of
    its own (worktrees/t<id>, detached at reviewed_head) made here. None when it reviews no commit
    found here: it then runs in the root."""
    own = p.worktrees / f"t{review['id']}"
    recs = _records(p)
    names = [r for r in dict.fromkeys([prefer] + _dependency_first(p, review, reviewed_refs(p, review)))
             if r and not _HEX.fullmatch(r)]
    base = p.worktrees.resolve()
    for name in names:
        for rec in recs.values():
            path = Path(rec["path"])
            if rec["branch"] == name and not rec["prunable"] and path.parent == base and path.is_dir():
                return path
    rec = _record_of(recs, own) if own.is_dir() else None
    if rec and not rec["prunable"] and is_git(own):
        return own   # an earlier run's: it may hold that run's rebase
    head = reviewed_head(p, review)
    if not head:
        return None
    _make_root(p)
    if own.exists() or rec:
        _clear_partial(p, own, rec)
    _git(p.root, "worktree", "add", "--detach", str(own), head, timeout=git_timeout(p), env=checkout_env(p))
    return own


# Git operations whose state files in the git directory mean one is under way in a checkout.
GIT_BUSY = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")


def restore_root(repo: Path, branch: str, run_id: int, timeout_s: float = 60, tag: str = "") -> tuple[bool, str]:
    """Put the checkout at `repo` back on `branch` after a run left it detached or on another branch,
    losing nothing: a detached HEAD with commits `branch` lacks is saved first as rescue/root-run<id>-<sha>
    (rescue/root-<tag>-<sha> with a `tag`),
    then `git checkout <branch>` (never forced or reset: git refuses to overwrite uncommitted changes,
    and `branch` itself is not moved). Not while a rebase, merge, cherry-pick, revert or bisect is under
    way there, nor while tracked paths have uncommitted changes (they would move onto `branch`).
    (True, the rescue branch or "") or (False, why)."""
    try:
        gd = Path(_git(repo, "rev-parse", "--absolute-git-dir", timeout=timeout_s))
        busy = [x for x in GIT_BUSY if (gd / x).exists()]
        if busy:
            return False, f"a git operation is under way there ({busy[0]})"
        dirty = dirty_tracked(repo, timeout_s)
        if dirty is None or dirty:
            return False, (f"{len(dirty)} tracked path{'' if len(dirty) == 1 else 's'} there "
                           f"{'has' if len(dirty) == 1 else 'have'} uncommitted changes ({', '.join(dirty[:3])})"
                           if dirty else "git could not list its uncommitted changes")
        if not _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False, timeout=timeout_s):
            return False, f"branch {branch} is gone"
        head = _git(repo, "rev-parse", "HEAD", timeout=timeout_s)
        detached = not _git(repo, "symbolic-ref", "--quiet", "HEAD", check=False, timeout=timeout_s)
        rescue = ""
        if detached and subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head,
                                        f"refs/heads/{branch}"], capture_output=True, timeout=timeout_s).returncode:
            rescue = f"rescue/root-{tag or f'run{run_id}'}-{head[:12]}"
            if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{rescue}", check=False,
                    timeout=timeout_s) != head:
                _git(repo, "branch", rescue, head, timeout=timeout_s)   # no -f: an existing one is kept
        _git(repo, "checkout", "--quiet", branch, timeout=timeout_s)
        return True, rescue
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:300]


def carried_head(p: Project, task: dict) -> str | None:
    """The head of the task's carried branch (carried_branch), so it builds on what it delivers onto,
    or of the branch it rebases (rebased_branch)."""
    branch = (delivery_branch(task) or ("", False))[0]
    for cand in (branch, f"origin/{branch}") if branch else ():
        head = _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False)
        if head:
            return head
    return None


def early_head(p: Project, task: dict) -> str | None:
    """The approved head of the change a task started on before it landed (lanewait.START_FROM), when
    it resolves here; else None (the base)."""
    from .lanewait import START_FROM, label_value
    sha = label_value(task, START_FROM)
    if not sha or not _HEX.fullmatch(sha):
        return None
    return _git(p.root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False) or None


def resolve_base(p: Project) -> str:
    """The configured base as a commit git can find: the name itself, else the remote's branch of
    that name (a fresh clone has `origin/<name>` but no local `<name>`). A miss names close matches,
    so whoever set it can fix it in one step."""
    ref = base_ref(p)
    if not ref:
        raise RuntimeError("the project root's checkout is on a detached HEAD and no base branch is named or "
                           "known: set delivery.base_ref (or worktree.root_branch), or put the root back on its "
                           "branch; no worktree is branched from a detached HEAD")
    for cand in (ref, f"origin/{ref}"):
        if _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False):
            return cand
    tail = ref.split("/")[-1]
    near = [b for b in _git(p.root, "branch", "-a", "--format=%(refname:short)", check=False).split()
            if tail and tail in b][:5]
    raise RuntimeError(f"base branch {ref!r} not found here or on origin"
                       + (f"; similar: {', '.join(near)}" if near else ""))


def remove(p: Project, task_id: int, force: bool = False) -> None:
    """Remove a task's worktree; its branch stays. Call only once keep_reason() found nothing to lose.
    Ignored files go with it (sweep archives hand-off artifacts first). No --force: git itself then
    refuses a worktree with changes or with submodules, whose commits may live only in the worktree's
    own git directory. `force` only once its tracked changes are in a verified archived patch."""
    path = p.worktrees / f"t{task_id}"
    remove_path(_repo_of(path) or p.root, path, force)   # a PR fix's may be a worktree of another clone (ensure_in)


def remove_path(repo: Path, path: Path, force: bool = False) -> None:
    """remove() for the worktree at `path` of the repository `repo`, then `git worktree prune` there."""
    if path.exists():
        _git(repo, "worktree", "remove", *(["--force"] if force else []), str(path))
    _git(repo, "worktree", "prune", check=False)


def _repo_of(path: Path) -> Path | None:
    """The repository whose linked worktree is at `path`, read from its `.git` file (gitdir: <common
    dir>/worktrees/<name>, whose commondir file names the common dir) without running git; None when
    `path` holds no linked worktree of a non-bare repository."""
    try:
        text = (path / ".git").read_text()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gd = Path(text[len("gitdir:"):].strip())
    gd = gd if gd.is_absolute() else path / gd
    try:
        common = (gd / (gd / "commondir").read_text().strip()).resolve()
    except OSError:
        common = gd.parent.parent.resolve()
    return common.parent if common.name == ".git" else None


def _origin_slug(repo: Path) -> str:
    """"owner/repo" (lower case) of `repo`'s origin URL, or "" when it has none."""
    try:
        url = _git(repo, "config", "--get", "remote.origin.url", check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"[/:]([^/:]+)/([^/]+?)(?:\.git)?/*$", url)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def pr_clone(p: Project, key: str) -> Path | None:
    """A local clone of the repository of PR `key` ("owner/repo#N") when that is not the project
    root's own repository (a root that is no git repository, or a clone of another one): a clone
    directly in the root, or the repository of a linked worktree one folder below the root or in the
    project's worktrees folder (one a task made by hand). None when the root is that repository or
    no clone of it is found."""
    want = (key or "").partition("#")[0].lower()
    root = p.root.resolve()
    if not want or (is_git(root) and _origin_slug(root) == want):
        return None
    base, dirs = p.base.resolve(), []
    for top in (root, p.worktrees):
        try:
            dirs += sorted(Path(d.path) for d in os.scandir(top) if d.is_dir(follow_symlinks=False)
                           and Path(d.path).resolve() != base)
        except OSError:
            pass
    for d in [d for d in dirs if d.parent == root and not (d / ".git").exists()]:
        try:
            dirs += sorted(Path(k.path) for k in os.scandir(d) if k.is_dir(follow_symlinks=False))
        except OSError:
            pass
    seen = set()
    for d in dirs:
        git = d / ".git"
        repo = d if git.is_dir() else _repo_of(d) if git.is_file() else None
        if repo and repo.resolve() not in seen:
            seen.add(repo.resolve())
            if _origin_slug(repo) == want:
                return repo.resolve()
    return None


def pr_repo(p: Project, task: dict) -> Path | None:
    """pr_clone for the PR a PR-fix task fixes (its `pr_fix:<owner/repo>#<n>` label), or None."""
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    key = next((lb[7:] for lb in labels if isinstance(lb, str) and lb.startswith("pr_fix:")), "")
    return pr_clone(p, key) if key else None


def ensure_in(p: Project, task: dict, repo: Path) -> tuple[Path, str]:
    """ensure() for a task whose PR is in another repository than the project root's (pr_repo): its
    worktree at worktrees/t<id> is a linked worktree of `repo`, reused as it is by a retry. A new one
    checks out the task's stored branch, else its carried branch (the PR's) when no other worktree of
    `repo` holds it, else a branch of its own from that branch's head (the local one first, so a
    holder's unpushed commits carry over). Raises when neither branch is found."""
    path, t, env = p.worktrees / f"t{task['id']}", git_timeout(p), checkout_env(p)
    if path.is_dir() and _repo_of(path) == repo.resolve() and is_git(path):
        return path, _git(path, "rev-parse", "--abbrev-ref", "HEAD")

    def ref(b: str) -> str:
        return next((c for c in (b, f"origin/{b}") if b and _git(
            repo, "rev-parse", "--verify", "--quiet", f"{c}^{{commit}}", check=False, timeout=t)), "")
    _git(repo, "fetch", "--quiet", "origin", check=False, timeout=t)
    _git(repo, "worktree", "prune", check=False, timeout=t)
    held = {r["branch"] for r in _records(p, repo).values() if not r["prunable"]}
    want = task.get("branch") or carried_branch(task)
    _make_root(p)
    if want and want not in held and (start := ref(want)):
        branch = want
        if start == want:
            _git(repo, "worktree", "add", str(path), want, timeout=t, env=env)
            _git(path, "merge", "--ff-only", "--quiet", f"origin/{want}", check=False, timeout=t, env=env)
        else:
            _git(repo, "worktree", "add", "-b", want, str(path), start, timeout=t, env=env)
    else:
        if not (start := ref(want)):
            raise RuntimeError(f"branch {want or '(none)'} not found in {repo}")
        branch = f"ttp/t{task['id']}-{slug(task['title'])}"
        exists = _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False, timeout=t)
        _git(repo, "worktree", "add", *((str(path), branch) if exists else ("-b", branch, str(path), start)),
             timeout=t, env=env)
    prepare(p, path, repo)
    return path, branch


def run_worktree_dirs(p: Project) -> list[Path]:
    """Run directories' subfolders holding a `.git` file (state/runs/<id>/<name>/.git): the linked
    worktrees workers made there, found by listing two levels without running git."""
    if not p.runs.is_dir():
        return []
    runs = p.runs.resolve()
    try:
        dirs = [d.path for d in os.scandir(runs) if d.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    out = []
    for d in dirs:
        try:
            out += [Path(k.path) for k in os.scandir(d)
                    if k.is_dir(follow_symlinks=False) and os.path.isfile(os.path.join(k.path, ".git"))]
        except OSError:
            continue
    return sorted(out)


def run_worktrees(p: Project) -> list[dict]:
    """The git worktrees inside run directories (state/runs/<run id>/...), which workers make for
    themselves and which neither a task's end nor the task-worktree sweep knew of. Found from git's
    records (`git worktree list`) of the project's repository and of each repository that a `.git`
    file directly in a run directory's subfolder points to; nothing else is scanned. One whose
    directory is gone is left to `git worktree prune`. Each is {"path", "repo", "run" (its runs row,
    None when no run of this project owns the folder), "rec" (git's record, see _records)}; a
    repository whose git fails is skipped."""
    if not p.runs.is_dir():
        return []
    runs = p.runs.resolve()
    repos = {p.root.resolve()} | {r for r in map(_repo_of, run_worktree_dirs(p)) if r is not None}
    out = []
    for repo in sorted(repos):
        try:
            recs = _records(p, repo)
        except Exception:
            continue
        for rec in recs.values():
            if rec["prunable"]:
                continue
            path = Path(rec["path"]).resolve()
            try:
                rel = path.relative_to(runs)
            except ValueError:
                continue
            if len(rel.parts) < 2:
                continue
            name = rel.parts[0]
            run = p.db.one("SELECT * FROM runs WHERE id=?", (int(name),)) if name.isdigit() else None
            if run and run["dir"] and Path(run["dir"]).resolve() != runs / name:
                run = None
            out.append({"path": path, "repo": repo, "run": dict(run) if run else None, "rec": rec})
    return sorted(out, key=lambda w: str(w["path"]))


STRAY_NAME = re.compile(r"(?:wt-|t|r)(\d+)(?!\d)")   # a stray worktree's folder name: its task id first
STRAY_ARCHIVE_DIR = "archive/worktrees"   # under state/: a stray worktree's dirty files, by folder name
STRAY_ARCHIVE_MAX_MB = 5   # a stray worktree with more dirty files than this is kept, not archived


def stray_worktrees(p: Project) -> list[dict]:
    """Linked worktrees under the project root that neither the task sweep (worktrees/t<id>) nor the
    run-directory sweep (run_worktrees) looks at: ones workers made elsewhere (state/worktrees/t<id>,
    state/wt-<id>, worktrees/t<id>-review or r<id>, <repo>/.wt/t<id>). Found from git's records of
    the project's repository and of each repository directly in the project root; nothing else is
    scanned. Only a folder whose name starts with t, r or wt- and a number counts: that number is
    its task's id. Left out: the harness's own worktrees and anything inside another tt-project
    project's folder. Each is {"path", "repo", "task" (the id), "rec" (git's record, see _records)};
    a repository whose git fails is skipped."""
    root = p.root.resolve()
    base, runs, own = p.base.resolve(), p.runs.resolve(), p.worktrees.resolve()
    repos = {root} if is_git(root) else set()
    try:
        repos |= {Path(d.path) for d in os.scandir(root) if d.is_dir(follow_symlinks=False)
                  and Path(d.path) != base and os.path.isdir(os.path.join(d.path, ".git"))}
    except OSError:
        pass
    out, seen = [], set()
    for repo in sorted(repos):
        try:
            recs = _records(p, repo)
        except Exception:
            continue
        for rec in recs.values():
            path = Path(rec["path"]).resolve()
            m = STRAY_NAME.match(path.name)
            if (rec["prunable"] or not m or path in seen or path == repo or root not in path.parents
                    or runs in path.parents or (path.parent == own and re.fullmatch(r"t\d+", path.name))
                    or (p.state.resolve() / "harness-worktrees") in path.parents):
                continue
            # Another project's folder below this root (<dir>/tt-project) holds its own tasks' worktrees.
            if base not in path.parents and any((a / base.name).is_dir() for a in path.parents if root in a.parents):
                continue
            seen.add(path)
            out.append({"path": path, "repo": repo, "task": int(m.group(1)), "rec": rec})
    return sorted(out, key=lambda w: str(w["path"]))


def _sweep_stray(p: Project, w: dict, open_tasks: list[dict], now: float, older_than_s: float,
                 entries: list, dry_run: bool) -> dict:
    """Sweep one stray worktree (see stray_worktrees): it goes once its task is done or cancelled, no
    run, process or other task holds it (held_by) and its HEAD is on a remote branch. Its dirty files
    (changed tracked and untracked ones, and hand-off artifacts) are first copied to
    state/archive/worktrees/<folder name>/ with a patch of the tracked changes, when they total at
    most STRAY_ARCHIVE_MAX_MB; more keeps it. A worktree of an open task is never touched."""
    from .db import TERMINAL_TASK_STATES
    path, rec = w["path"], w["rec"]
    task = p.db.task(w["task"])
    res = {"task": w["task"], "run": None, "stray": True, "path": str(path),
           "branch": rec["branch"] or (task or {}).get("branch"), "status": (task or {}).get("status") or "?",
           "updated": (task or {}).get("updated"), "cleared": []}
    held = None
    if task is None:
        why = f"no task #{w['task']} in this project"
    elif task["status"] not in TERMINAL_TASK_STATES:
        why = held = f"its task #{task['id']} ({task['status']}) is still open"
    elif task["status"] not in ("done", "cancelled"):
        why = f"its task #{task['id']} {task['status']}: only a done or cancelled task's stray worktree goes"
    elif now - float(task["updated"] or now) < older_than_s:
        why = held = "its task ended within disk.worktree_retention_days"
    elif p.db.one("SELECT id FROM runs WHERE task=? AND status='running' LIMIT 1", (task["id"],)):
        why = held = f"a run of task #{task['id']} is still going"
    else:
        why = held = held_by(p, task, open_tasks, now, path=path)
    users = [] if why else _users(path)
    if users:
        why = held = f"process {users[0]} runs in it" + (f" (+{len(users) - 1} more)" if len(users) > 1 else "")
    if why:
        return {**res, "why": why, **({"held": True} if held else {})}
    if rec["locked"] is not None:
        return {**res, "why": f"locked ({rec['locked'] or 'no reason given'})"}
    if not is_git(path):
        return {**res, "why": "not a git checkout"}
    head = _git(path, "rev-parse", "HEAD", check=False).strip()
    if not _git(path, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/remotes", check=False).strip():
        return {**res, "why": "its HEAD is on no remote branch"}
    info = inspect(path)
    if info["why"] and not info["dirty"]:   # submodules, or git failed
        return {**res, "why": info["why"]}
    tracked = [f for f in _git(path, "diff", "HEAD", "--name-only", "-z", check=False).split("\0") if f]
    untracked = [f for f in _git(path, "ls-files", "--others", "--exclude-standard", "-z", check=False).split("\0") if f]
    arts = handoff_artifacts(p, w["task"], path, entries=entries)
    files = [f for f in dict.fromkeys(tracked + untracked + arts) if (path / f).exists() or (path / f).is_symlink()]
    if tracked or files:
        size = _size(path, files)
        if size > STRAY_ARCHIVE_MAX_MB * 1e6:
            return {**res, "why": f"{len(files)} uncommitted file(s) of {size / 1e6:.1f} MB, over the "
                                  f"{STRAY_ARCHIVE_MAX_MB} MB archived before a stray worktree goes"}
        dest = p.state / STRAY_ARCHIVE_DIR / path.name
        n = 1
        while dest.exists():
            dest, n = p.state / STRAY_ARCHIVE_DIR / f"{path.name}.{n}", n + 1
        res["archived"] = {"files": len(files), "bytes": size, "dir": str(dest), "dropped": 0, "dropped_bytes": 0}
        if not dry_run:
            dest.mkdir(parents=True)
            for f in files:
                src, to = path / f, dest / "files" / f
                to.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir() and not src.is_symlink():
                    shutil.copytree(src, to, symlinks=True)
                else:
                    shutil.copy2(src, to, follow_symlinks=False)
            if tracked:
                patch = subprocess.run(["git", "-C", str(path), "diff", "HEAD", "--binary"], capture_output=True,
                                       timeout=300, check=True).stdout
                durable_write(dest / "tracked.patch", patch)
            durable_write(dest / "manifest.json", json.dumps(
                {"path": str(path), "repo": str(w["repo"]), "task": w["task"], "branch": res["branch"], "head": head,
                 "tracked": tracked, "files": files, "bytes": size, "at": now}, indent=1))
    if not dry_run:
        remove_path(w["repo"], path, force=bool(tracked or files))
    return {**res, "why": None}


def keep_reason(path: Path) -> str | None:
    """Why this worktree must stay, or None when removing it loses nothing: no submodules set up in
    it, no uncommitted or untracked files, and its HEAD is on a local or remote branch (the task's
    own branch is never deleted, so its commits stay reachable)."""
    return inspect(path)["why"]


def inspect(path: Path) -> dict:
    """keep_reason with what lies behind it: `why` (None: nothing to lose), `dirty` "untracked" (the
    only dirty entries are untracked files, `untracked` lists them, HEAD is on a branch, no submodules)
    or "tracked" (modified tracked files, `tracked` lists their paths, `fingerprint` hashes the
    status and diff), else None."""
    import hashlib

    def git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=text, timeout=300)
    out = {"why": None, "dirty": None}
    # A submodule's repository lives in the worktree's own git directory (modules/), so commits made
    # in it exist nowhere else and removing the worktree would delete them. Such a worktree stays.
    gd = git("rev-parse", "--absolute-git-dir")
    if gd.returncode != 0:
        return {**out, "why": "git rev-parse failed"}
    modules = Path(gd.stdout.strip()) / "modules"
    subs = git("submodule", "status")
    if subs.returncode != 0:
        return {**out, "why": "git submodule status failed"}
    inited = [ln.split()[1] for ln in subs.stdout.splitlines() if ln.strip() and not ln.startswith("-")]
    if inited or (modules.is_dir() and any(modules.iterdir())):
        return {**out, "why": "has submodules set up (" + (", ".join(inited[:3]) or "modules/") + "); their commits "
                "may exist only here, so it is never removed automatically"}
    st = git("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")
    if st.returncode != 0:
        return {**out, "why": "git status failed"}
    dirty = st.stdout.splitlines()
    held = git("for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)", "refs/heads", "refs/remotes")
    if held.returncode != 0:
        return {**out, "why": "git for-each-ref failed"}
    detached = None if held.stdout.strip() else "commits on no branch (detached HEAD)"
    if not dirty:
        return {**out, "why": detached}
    why = f"uncommitted changes in {len(dirty)} path(s), e.g. {dirty[0][3:].strip()[:80]}"
    tracked = [ln[3:].strip() for ln in dirty if not ln.startswith("??")]
    if tracked:
        diff = git("diff", "HEAD", "--binary", text=False)
        fp = hashlib.sha256(st.stdout.encode() + b"\0" + diff.stdout).hexdigest()[:16]
        return {**out, "why": why, "dirty": "tracked", "tracked": tracked, "fingerprint": fp}
    if detached:
        return {**out, "why": f"{why}; {detached}"}
    files = git("ls-files", "--others", "--exclude-standard", "-z")
    if files.returncode != 0:
        return {**out, "why": why}
    return {**out, "why": why, "dirty": "untracked", "untracked": [e for e in files.stdout.split("\0") if e]}


def move_leftovers(path: Path, rels: list[str], dest: Path) -> int:
    """Move these untracked files (relative to the worktree) under `dest`, keeping their relative
    paths; a name already there gets a numbered suffix. Returns how many moved."""
    import shutil
    moved = 0
    for rel in rels:
        src, to = path / rel, dest / rel
        n = 1
        while to.exists() or to.is_symlink():
            to = dest / f"{rel}.{n}"
            n += 1
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(to))
        moved += 1
    return moved


def _size(path: Path, rels: list[str]) -> int:
    """Bytes these entries take (a directory: everything in it), links not followed. A directory
    holding a network or FUSE mount is skipped: a hung one would block the walk in D state."""
    from .daemon import network_mounts
    nets = network_mounts()
    total = 0
    for rel in rels:
        full = path / rel
        if any(m == full or full in m.parents for m in nets):
            continue
        try:
            if full.is_dir() and not full.is_symlink():
                total += sum(f.lstat().st_size for f in full.rglob("*") if f.is_symlink() or f.is_file())
            else:
                total += full.lstat().st_size
        except OSError:
            continue
    return total


def last_run_dir(p: Project, task_id: int) -> Path | None:
    r = p.db.one("SELECT id, dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (task_id,))
    return (Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) if r else None


LEFTOVERS_DIR = "worktree-leftovers"
DIRTY_EVENT = "worktree_uncommitted"   # the coordinator's event for a finished worktree's tracked changes
LEFTOVERS_MAX_MB = 50   # disk.worktree_leftovers_max_mb: untracked files up to this total move out (see sweep)
LEFTOVERS_KEEP_DAYS = 30   # disk.worktree_leftovers_keep_days: recovered leftovers folders kept this long


def prune_leftovers(p: Project, keep_days: float | None = None, now: float | None = None) -> list[str]:
    """Delete the project's worktree-leftovers/ folders (written by _other_branch's recovery) older
    than `keep_days` (null: LEFTOVERS_KEEP_DAYS; 0 or less: keep all). A folder's age counts from the
    later of the time in its name (t<id>-YYYYmmdd-HHMMSS) and its own mtime, so none newer than that
    goes. Returns the names deleted."""
    days = LEFTOVERS_KEEP_DAYS if keep_days is None else float(keep_days)
    root = p.state / LEFTOVERS_DIR
    if days <= 0 or not root.is_dir():
        return []
    now = time.time() if now is None else now
    gone = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir():
            continue
        try:
            made = d.stat().st_mtime
        except OSError:
            continue
        m = re.search(r"-(\d{8}-\d{6})$", d.name)
        if m:
            try:
                made = max(made, time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S")))
            except ValueError:
                pass
        if now - made < days * 86400:
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            gone.append(d.name)
    return gone


# Build output and tool caches: regenerated on demand, often gigabytes. Only ignored entries are
# cleared, so a tracked file or an uncommitted new one (which keeps the worktree) is never touched.
CACHE_DIRS = ["build", "_build", "cmake-build-*", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".tox", ".nox", ".venv", "venv", "node_modules", "*.egg-info", ".eggs",
              ".gradle", ".ccache"]


def _ignored(path: Path) -> set[str] | None:
    """Git-ignored entries in a worktree, relative to it. With --directory git lists a directory
    itself only when everything in it is ignored. None when git fails."""
    out = subprocess.run(["git", "-C", str(path), "ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "-z"], capture_output=True, text=True, timeout=300)
    return {e.rstrip("/") for e in out.stdout.split("\0") if e} if out.returncode == 0 else None


def handoff_paths(p: Project) -> list[tuple[int, str]]:
    """(task, entry) for each `artifacts` entry of every hand-off (result.json) any run wrote. Other
    tasks often leave files in an earlier task's worktree, so a sweep reads them all, once."""
    import json
    out = []
    for r in p.db.q("SELECT id, task, dir FROM runs WHERE task IS NOT NULL"):
        try:
            data = json.loads(((Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) / "result.json").read_text())
        except (OSError, ValueError):
            continue
        arts = data.get("artifacts") if isinstance(data, dict) else None
        out += [(r["task"], a) for a in arts if isinstance(a, str)] if isinstance(arts, list) else []
    return out


def _existing(entry: str, bases: list[Path]) -> list[Path]:
    """The files an artifact entry names. Entries may be absolute or relative to one of `bases`,
    may be globs (tmp/*.pt), and may carry trailing text ("out.bin (the result)", "run.log:12"),
    so the longest leading part that exists wins."""
    import glob
    words = entry.strip().split()
    for n in range(len(words), 0, -1):
        text = " ".join(words[:n])
        for cand in dict.fromkeys((text, text.rstrip(".,;:)]}'\"`"), re.sub(r"(:\d+)+[.,;:]?$", "", text))):
            cand = cand.strip("'\"`(")
            if not cand or "://" in cand:
                continue
            q = Path(cand).expanduser()
            for full in ([q] if q.is_absolute() else [b / q for b in bases]):
                try:
                    if full.exists():
                        return [full]
                    found = [Path(m) for m in glob.glob(str(full), recursive=True)] if any(
                        c in cand for c in "*?[") else []
                    if found:
                        return found
                except OSError:
                    continue
    return []


def handoff_artifacts(p: Project, task_id: int, path: Path, ignored: set[str] | None = None,
                      entries: list[tuple[int, str]] | None = None) -> list[str]:
    """Hand-off artifacts (any task's result.json `artifacts`, see handoff_paths) that still exist
    inside this task's worktree and that git ignores, relative to the worktree. An entry is read
    relative to its own task's worktree or the project. `git worktree remove` deletes ignored files,
    and only those: tracked ones are on the branch, other untracked ones keep the worktree. A named
    directory git does not ignore wholly gives only the ignored entries inside it, never itself, so
    its tracked files are never archived (and so never taken out of the worktree)."""
    ignored = _ignored(path) if ignored is None else ignored
    if not ignored:
        return []
    root = path.resolve()
    found: list[str] = []
    for tid, entry in handoff_paths(p) if entries is None else entries:
        for full in _existing(entry, [path if tid == task_id else p.worktrees / f"t{tid}", p.root, p.base]):
            try:
                rel = full.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if rel == ".":
                continue
            if any(rel == e or rel.startswith(e + "/") for e in ignored):
                hits = [rel]
            else:
                hits = sorted(e for e in ignored if e.startswith(rel + "/"))
            found += [h for h in hits if h not in found]
    return found


def _caches(listed: set[str], names: list[str] | None = None) -> set[str]:
    """The cache entries among a worktree's ignored ones (`listed`, as _ignored gives them): an
    ignored entry named like one of `names` (null: CACHE_DIRS), or the first such directory in an
    ignored entry's path that git lists itself."""
    import fnmatch
    pats = CACHE_DIRS if names is None else names
    found: set[str] = set()
    for entry in listed:
        parts = entry.split("/")
        for i, part in enumerate(parts):
            prefix = "/".join(parts[:i + 1])
            if prefix in listed and any(fnmatch.fnmatchcase(part, pat) for pat in pats):
                found.add(prefix)
                break
    return found


def clear_caches(path: Path, names: list[str] | None = None, keep: list[str] = ()) -> list[str]:
    """Delete ignored build and cache directories in a worktree. A cache-named directory goes only
    when git ignores it wholly; else just the ignored entries named like one inside it. Nothing that
    is, holds or lies in a path of `keep` (hand-off artifacts) goes. Returns what went."""
    import shutil
    listed = _ignored(path)
    if listed is None:
        return []
    found = _caches(listed, names)
    gone = []
    for rel in sorted(found):
        if any(rel.startswith(g + "/") for g in gone) or any(
                k == rel or k.startswith(rel + "/") or rel.startswith(k + "/") for k in keep):
            continue
        target = path / rel
        try:
            if target.is_symlink() or not target.is_dir():
                target.unlink()
            else:
                shutil.rmtree(target)
            gone.append(rel)
        except FileNotFoundError:
            continue
        except OSError:
            pass
    return gone


def needed_by(task: dict, open_tasks: list[dict]) -> dict | None:
    """The first unfinished task that may still work in this task's worktree (a review that runs
    `ttp push` there, say): it depends on or continues the task, or its spec names the task's
    branch or id (#12, t12, task 12, worktrees/t12)."""
    from .db import dependency_ids
    tid, branch = task["id"], task.get("branch")
    named = re.compile(rf"(?<![\w.-])(?:#|t|task\s+){tid}(?!\d)"
                       + (rf"|(?<![\w/.-]){re.escape(branch)}(?![\w/-])" if branch else ""), re.I)
    for t in open_tasks:
        if t["id"] != tid and (tid in dependency_ids(t) or continues_id(t) == tid or named.search(t.get("spec") or "")):
            return t
    return None


# A task worktree's path in a probe: <root>/tt-project/worktrees/t<id>, or relative from the project root.
WORKTREE_REF = re.compile(r"(?<![\w.-])worktrees/t(\d+)(?![\w-])")


def worktree_refs(text: str | None, own: int | None = None) -> list[int]:
    """The ids of the task worktrees `text` (a probe) names by path, its own task `own` left out."""
    return sorted({int(m.group(1)) for m in WORKTREE_REF.finditer(text or "")} - {own})


def probe_hold_text(tid: int, probe: str | None) -> str | None:
    """One line saying which other tasks' worktrees task `tid`'s probe reads (probed_by keeps them
    from pruning while it waits), or None when it reads none."""
    refs = worktree_refs(probe, tid)
    return (f"task {tid} retry_when reads the worktree of task(s) {', '.join(f'#{r}' for r in refs)}: "
            f"kept from pruning while it waits") if refs else None


def probed_by(task: dict, open_tasks: list[dict]) -> dict | None:
    """The first unfinished task whose probe (its waiting hand-off's retry_when, or its start_when)
    reads this task's worktree: removing the worktree would leave that probe failing forever."""
    from .db import deferral, load_result
    for t in open_tasks:
        if t["id"] == task["id"]:
            continue
        probes = (load_result(t.get("result")).get("retry_when"), deferral(t).get("when"))
        if any(isinstance(pr, str) and task["id"] in worktree_refs(pr, t["id"]) for pr in probes):
            return t
    return None


# A `cd <dir>` that starts a command in a probe (at its start or after &&, ||, ;, | or a newline).
CD_TARGET = re.compile(r"(?:^|&&|\|\||[;|(\n])\s*cd\s+('[^']*'|\"[^\"$`]*\"|[^\s;&|()'\"$`]+)(?=[\s;&|)]|$)")


def missing_cd_worktree(probe: str | None, p: Project) -> str | None:
    """The first `cd` target in `probe` that lies in a task worktree under this project's worktrees
    folder which no longer exists (pruned), or None. Such a probe can never pass: `cd` fails, and
    under bash (or with `|| exit 1`) that reads as "not yet" forever. A directory a job may still
    make is not one of these: worktrees are made only by the harness, at a task's start."""
    root = p.worktrees.resolve()
    for m in CD_TARGET.finditer(probe or ""):
        arg = m.group(1).strip("'\"")
        target = Path(os.path.expanduser(arg))
        target = (target if target.is_absolute() else p.root / target).resolve()
        if target == root or root not in target.parents:
            continue
        top = root / target.relative_to(root).parts[0]
        if re.fullmatch(r"t\d+", top.name) and not top.exists():
            return str(target)
    return None


# A finished task's worktree stays at least this long, and while the coordinator has not yet seen how
# the task ended: the review that pushes from it is often queued only after that.
FINISH_GRACE_S = 3600


_HOLDER_STATUS = re.compile(r"\b(task #\d+) \([\w-]+\)")


def keep_key(why: str | None) -> str | None:
    """A keep reason without its holder task's status, so the daemon logs a worktree's reason again
    when the holder changes, not each time that task goes queued, running or pushing."""
    return _HOLDER_STATUS.sub(r"\1", why) if why else why


def held_by(p: Project, task: dict, open_tasks: list[dict], now: float, path: Path | None = None) -> str | None:
    """Why a finished task's worktree (or another worktree of the task, at `path`) must stay
    untouched for now, or None."""
    from .push import running
    path = (path or p.worktrees / f"t{task['id']}").resolve()
    if any(Path(m.get("repo") or "/").resolve() == path for m in running(p)):
        return "a detached `ttp push` runs in it"
    user = probed_by(task, open_tasks)
    if user:
        return f"task #{user['id']} ({user['status']}) waits on a probe that reads it"
    user = needed_by(task, open_tasks)
    if user:
        return f"task #{user['id']} ({user['status']}) may still use it"
    if p.db.one("SELECT id FROM events WHERE task=? AND status='queued' AND kind!=? LIMIT 1",
                (task["id"], DIRTY_EVENT)):
        return "the coordinator has not yet seen how it ended"
    if now - float(task["updated"] or now) < FINISH_GRACE_S and not review_over(p, task):
        return f"it ended under {FINISH_GRACE_S // 60} min ago; kept for a review"
    return None


def run_held(p: Project, w: dict, task: dict | None, open_tasks: list[dict], now: float,
             older_than_s: float = 0) -> str | None:
    """Why the worktree `w` in a run directory (see run_worktrees) must stay for now, or None: no run
    of this project owns it, its run is still going, its task is still open, its run ended under
    FINISH_GRACE_S (or `older_than_s`) ago, or the task's own worktree would be held (held_by)."""
    from .db import TERMINAL_TASK_STATES
    from .push import running
    run = w["run"]
    if run is None:
        return "no run of this project owns its folder"
    if run["status"] == "running" or not run["ended"]:
        return f"its run #{run['id']} is still going"
    if task and task["status"] not in TERMINAL_TASK_STATES:
        return f"its task #{task['id']} ({task['status']}) is still open"
    age, grace = now - float(run["ended"]), max(FINISH_GRACE_S, older_than_s)
    if age < grace:
        return f"its run #{run['id']} ended under {grace / 3600:g} h ago"
    if task:
        return held_by(p, task, open_tasks, now, path=w["path"])
    if any(Path(m.get("repo") or "/").resolve() == w["path"] for m in running(p)):
        return "a detached `ttp push` runs in it"
    return None


def review_over(p: Project, task: dict) -> bool:
    """Whether the review a finished task's worktree is kept for is over: the task is itself a
    review, or a review of it has ended (passed or failed) and the coordinator has seen that. A failed
    review's fix continues the task, so needed_by keeps the worktree for it."""
    from .db import TERMINAL_TASK_STATES, dependency_ids
    if task.get("kind") == "review":
        return True
    rows = p.db.q("SELECT id, depends_on FROM tasks WHERE kind='review' AND status IN (%s) AND depends_on LIKE ?"
                  % ",".join("?" * len(TERMINAL_TASK_STATES)), (*TERMINAL_TASK_STATES, f"%{task['id']}%"))
    return any(task["id"] in dependency_ids(r) and not p.db.one(
        "SELECT id FROM events WHERE task=? AND status='queued' LIMIT 1", (r["id"],)) for r in rows)


HARNESS_LOCKS = ("initializing",)   # worktree lock reasons git or the harness sets; cleared once stale
ARCHIVE_MAX_MB = 200   # disk.worktree_archive_max_mb: unnamed untracked files archived per task, the rest dropped


def _small_logs(path: Path, limit: int = 200) -> list[str]:
    """Ignored *.log files of at most archive.LOG_MAX_MB each (up to `limit`), relative to the worktree:
    `git worktree remove` would delete them, and they are cheap to keep."""
    from . import archive
    out = []
    for f in archive.files_of(path, sorted(_ignored(path) or [])):
        try:
            if f.endswith(".log") and not (path / f).is_symlink() and (path / f).stat().st_size <= archive.LOG_MAX_MB * 1e6:
                out.append(f)
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _users(path: Path) -> list[int]:
    """Live processes whose working directory is in `path` (from /proc; none known without it)."""
    root = path.resolve()
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []
    out = []
    for pid in pids:
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if cwd == root or root in cwd.parents:
            out.append(int(pid))
    return out


def sweep(p: Project, *, older_than_s: float = 0, names: list[str] | None = None,
          skip=lambda task: False, leftovers_max_mb: float | None = None, archive_max_mb: float | None = None,
          patch_tracked: bool = False, archive_limit: int | None = None, deadline: float | None = None,
          reserve_bytes: float = 0, progress=lambda: None, dry_run: bool = False,
          skip_run=lambda w: False) -> list[dict]:
    """Tidy the worktrees of finished tasks (done, failed, cancelled) with no run or process still
    going, and the worktrees workers made inside finished runs' directories (run_worktrees; reported
    with `run` set, kept while run_held says so; `skip_run(w)` leaves one out): clear their build and
    cache directories, then remove each one whose removal loses nothing
    (see keep_reason). A worktree that may still be wanted (see held_by: an unfinished task needs it,
    the coordinator has not seen the finish yet, or it ended under FINISH_GRACE_S ago and its review
    is not over) is left as it is, and reported with `held` set.
    Files outside git go first: hand-off artifacts (any task's result.json `artifacts`, see
    handoff_artifacts; ignored or untracked) are archived (archive.store, state/archive/tasks/<id>/,
    reported in `archived`). Other untracked files (HEAD on a branch) move to the last run's
    directory when they total at most `leftovers_max_mb` (see leftovers; reported in `moved`); larger
    ones are listed in the archive's manifest and kept there up to `archive_max_mb` (None:
    ARCHIVE_MAX_MB), smallest first, the rest dropped. Small ignored logs (*.log up to
    archive.LOG_MAX_MB each) are archived with them. With `patch_tracked` (the disk guard's sweep,
    `ttp prune --patch-tracked`), a done or cancelled task's tracked changes go into a binary patch
    verified to apply to its head (archive.tracked_patch) and the worktree is removed, when that head
    is on a branch. `leftovers_max_mb` 0 or less keeps the worktree
    of untracked files as it is. One with modified tracked files is kept and reported with `tracked`
    (paths) and `fingerprint`. A stale lock git or the harness set (HARNESS_LOCKS: a `git worktree
    add` that never finished) is cleared (`unlocked`); a half-made worktree goes. Any other lock keeps
    the worktree. Branches stay, so a task that `continues` one starts from its commits. `dry_run`
    changes nothing and reports what a sweep would do (`why` None: it would go). Worktrees workers made
    elsewhere under the project root (stray_worktrees) are swept last (_sweep_stray), reported with
    `stray` set (and `run` None; `skip_run(w)` leaves one out). One sweep at a time
    per project; a busy lock returns no results.
    Removing, unlocking and clearing caches is cheap and always done. Archiving (hashing, compressing,
    patching) is the costly part: `archive_limit` caps how many worktrees one call archives, and none
    starts after `deadline` (a time.time()); the others are kept with `deferred` set, for a later
    call (the daemon archives one per tick step, `ttp prune` all). `progress()` is called per worktree
    and, while archiving, per file. An archive that does not fit with `reserve_bytes` free on
    state/'s filesystem is skipped and the worktree kept (archive.NoRoom; `why` names the sizes); an
    archiving cut off part way is finished or rolled back first (archive.recover). Neither a skipped
    nor a failed archive (`why` 'error: ...') counts against `archive_limit`."""
    import fcntl
    import time
    from . import archive
    from .db import TERMINAL_TASK_STATES
    if not p.worktrees.is_dir() and not p.runs.is_dir():
        return []
    p.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.state / "prune.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []
        out = []
        now = time.time()
        entries = None
        records: dict | None = None
        limit = LEFTOVERS_MAX_MB if leftovers_max_mb is None else float(leftovers_max_mb)
        cap = ARCHIVE_MAX_MB if archive_max_mb is None else max(float(archive_max_mb), 0.0)
        archived = 0

        def costly_ok() -> bool:
            return dry_run or ((archive_limit is None or archived < archive_limit)
                               and (deadline is None or time.time() < deadline))
        deferred = "archiving deferred to a later sweep (each daemon tick step archives at most one worktree)"
        open_tasks = p.db.q("SELECT id, status, spec, depends_on, labels, result FROM tasks WHERE status NOT IN (%s)"
                            % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)
        targets: list[tuple[Path, dict | None, dict | None]] = []   # (path, task, run worktree or None)
        for path in sorted(p.worktrees.iterdir()) if p.worktrees.is_dir() else []:
            m = re.fullmatch(r"t(\d+)", path.name)
            task = p.db.task(int(m.group(1))) if m else None
            if (not task or task["status"] not in TERMINAL_TASK_STATES
                    or now - float(task["updated"] or now) < older_than_s or skip(task)
                    or p.db.one("SELECT id FROM runs WHERE task=? AND status='running' LIMIT 1", (task["id"],))):
                continue
            targets.append((path, task, None))
        for w in run_worktrees(p):
            tid = (w["run"] or {}).get("task")
            if not skip_run(w):
                targets.append((w["path"], p.db.task(tid) if tid else None, w))
        for path, task, w in targets:
            if w is not None:
                rec = w["rec"]
            else:
                if records is None:
                    try:
                        records = _records(p)
                    except Exception:
                        records = {}
                rec = _record_of(records, path)
            lock = rec["locked"] if rec else None
            half = lock is not None and lock.startswith(HARNESS_LOCKS) and not (path / ".git").is_file()
            if not half and not is_git(path):
                continue
            if w is None:
                res = {"task": task["id"], "path": str(path), "branch": task["branch"], "status": task["status"],
                       "updated": task["updated"], "cleared": []}
                why = held_by(p, task, open_tasks, now)
            else:
                run = w["run"] or {}
                res = {"task": task["id"] if task else None, "run": run.get("id"), "path": str(path),
                       "branch": rec["branch"] or (task or {}).get("branch"),
                       "status": (task or run).get("status") or "?", "updated": run.get("ended"), "cleared": []}
                why = run_held(p, w, task, open_tasks, now, older_than_s)
            users = [] if why else _users(path)
            if users:
                why = f"process {users[0]} runs in it" + (f" (+{len(users) - 1} more)" if len(users) > 1 else "")
            if why:
                out.append({**res, "held": True, "why": why})
                continue
            progress()
            try:
                if not dry_run and task and archive.task_dir(p, task["id"]).is_dir():
                    archive.recover(p, task["id"])
                if lock is not None:
                    if not lock.startswith(HARNESS_LOCKS):
                        out.append({**res, "why": f"locked ({lock or 'no reason given'}): only a stale lock git "
                                                  f"or the harness set is cleared"})
                        continue
                    res["unlocked"] = lock
                    if not dry_run:
                        if half:
                            _clear_partial(p, path, rec, w["repo"] if w else None)
                        else:
                            _git(w["repo"] if w else p.root, "worktree", "unlock", str(path), timeout=git_timeout(p))
                    if half:
                        out.append({**res, "why": None, "half": True})
                        continue
                entries = handoff_paths(p) if entries is None else entries
                tid = task["id"] if task else None
                arts = handoff_artifacts(p, tid, path, entries=entries)
                res["cleared"] = [] if dry_run else clear_caches(path, names, keep=arts)
                info = inspect(path)
                why = info["why"]
                others: list[str] = []
                patch = None
                if info["dirty"] == "tracked" and patch_tracked and task and task["status"] in ("done", "cancelled") \
                        and _git(path, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/heads", "refs/remotes",
                                 check=False).strip():
                    if not costly_ok():
                        out.append({**res, "why": deferred, "deferred": True})
                        continue
                    patch = archive.tracked_patch(path)
                    res["patched"] = info["tracked"]
                    untracked = _git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
                    untracked = [u for u in untracked if u]
                    arts += handoff_artifacts(p, tid, path, ignored=set(untracked), entries=entries)
                    others = [u for u in untracked if not any(u == a or u.startswith(a + "/") for a in arts)]
                    why = None
                elif info["dirty"] == "tracked":
                    res.update(tracked=info["tracked"], fingerprint=info["fingerprint"])
                elif info["dirty"] == "untracked" and limit <= 0:
                    why = (f"uncommitted untracked files only ({len(info['untracked'])}); moving them out is off "
                           f"(disk.worktree_leftovers_max_mb)")
                elif info["dirty"] == "untracked":
                    untracked = info["untracked"]
                    arts += handoff_artifacts(p, tid, path, ignored=set(untracked), entries=entries)
                    others = [u for u in untracked if not any(u == a or u.startswith(a + "/") for a in arts)]
                    why = None
                    home = p.runs / str(w["run"]["id"]) if w else last_run_dir(p, task["id"])
                    size, run = (_size(path, others), home) if others else (0, None)
                    if others and size <= limit * 1e6 and run is not None:
                        res["moved"] = {"files": len(others), "bytes": size, "to": str(run / LEFTOVERS_DIR)}
                        if not dry_run:
                            move_leftovers(path, others, run / LEFTOVERS_DIR)
                        others = []
                if why is None:
                    others += [f for f in _small_logs(path) if f not in others and not any(
                        f == a or f.startswith(a + "/") for a in arts)]
                if why is None and (arts or others or patch) and task is None:
                    why = f"no task to archive its {len(arts) + len(others)} file(s) under"
                if why is None and (arts or others or patch) and not costly_ok():
                    out.append({**res, "why": deferred, "deferred": True})
                    continue
                if why is None and (arts or others or patch):
                    if not dry_run:
                        archived += 1
                    try:
                        res["archived"] = (archive.plan(path, arts, others, cap, patch) if dry_run else archive.store(
                            p, task, path, arts, others, cap, head=_git(path, "rev-parse", "HEAD", check=False),
                            patch=patch, tracked=res.get("patched") or [], reserve_bytes=reserve_bytes,
                            progress=progress))
                    except archive.NoRoom as e:
                        archived -= 1   # nothing was written: it does not use up the step's archiving
                        if res.pop("patched", None):
                            res.update(tracked=info["tracked"], fingerprint=info["fingerprint"])
                        out.append({**res, "why": str(e)})
                        continue
                    except Exception:
                        if not dry_run:
                            archived -= 1   # failed (store rolled it back): the next worktree gets the slot
                        raise
                if why is None and not dry_run and not patch and (res.get("moved") or res.get("archived")):
                    why = keep_reason(path)
                res["why"] = why
                if why is None and not dry_run:
                    if w is not None:
                        remove_path(w["repo"], path, force=bool(patch))
                    else:
                        remove(p, task["id"], force=bool(patch))
            except Exception as e:
                res["why"] = f"error: {e}"[:300]
            out.append(res)
        if not dry_run:
            try:
                prune_rebase_starts(p, open_tasks)
            except Exception:
                pass
        for w in stray_worktrees(p):
            if skip_run(w):
                continue
            progress()
            entries = handoff_paths(p) if entries is None else entries
            try:
                out.append(_sweep_stray(p, w, open_tasks, now, older_than_s, entries, dry_run))
            except Exception as e:
                out.append({"task": w["task"], "run": None, "stray": True, "path": str(w["path"]),
                            "branch": w["rec"]["branch"], "status": "?", "updated": None, "cleared": [],
                            "why": f"error: {e}"[:300]})
        return out
    finally:
        os.close(fd)


def has_changes(path: Path, since_ref: str) -> bool:
    """Durable progress for a code task: commits past the base, or uncommitted edits."""
    ahead = _git(path, "rev-list", "--count", f"{since_ref}..HEAD", check=False)
    dirty = _git(path, "status", "--porcelain", check=False)
    return (ahead.isdigit() and int(ahead) > 0) or bool(dirty)


_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")


def reviewed_refs(p: Project, task: dict) -> list[str]:
    """What a review task reviews: named_refs, and the fix branches that build on them (fix_branches)."""
    refs = named_refs(p, task)
    return list(dict.fromkeys(refs + list(fix_branches(p, task, refs))))


# What follows a branch a review spec says it reviews: "branch B, head H", "B at H", "B rebased as H".
_SAID_HEAD = re.compile(r"(?:,?\s*head:?|\s+(?:rebased\s+)?(?:at|as))\s+([0-9a-f]{7,40})\b")


def named_refs(p: Project, task: dict) -> list[str]:
    """The branches of the tasks a review depends on, and the task branches and commit hashes its
    spec names. With such a branch, a spec-quoted hash counts only when it is on one of them (an
    ancestor of its head), builds on one (a descendant: a fix or a settled push conflict), or is the
    head the spec says it reviews ("branch B, head H", "B at H", "B rebased as H"): a spec often
    quotes other commits as evidence, and their files are not the change under review.
    When none of those branches resolves here, every quoted hash counts."""
    from .db import dependency_ids, names_branch
    spec = task.get("spec") or ""
    ids = [i for i in dependency_ids(task) if i is not None]
    branches = list(dict.fromkeys(
        r["branch"] for r in p.db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL AND branch!=''")
        if r["id"] in ids or names_branch(spec, r["branch"])))
    hashes = list(dict.fromkeys(_HEX.findall(spec)))
    heads = [h for h in (_branch_head(p, b) for b in branches) if h] if hashes else []
    if heads:
        said = {m.group(1) for b in branches for i in _find_all(spec, b)
                if (m := _SAID_HEAD.match(spec, i + len(b)))}
        hashes = [h for h in hashes if h in said or any(_on(p, h, tip) or _on(p, tip, h) for tip in heads)]
    return list(dict.fromkeys(branches + hashes))


def _branch_head(p: Project, branch: str) -> str:
    for cand in (branch, f"origin/{branch}"):
        if head := _git(p.root, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}", check=False):
            return head
    return ""


def _find_all(text: str, sub: str) -> list[int]:
    out, i = [], text.find(sub)
    while i >= 0:
        out.append(i)
        i = text.find(sub, i + 1)
    return out


def _on(p: Project, commit: str, tip: str) -> bool:
    """Whether `commit` is `tip` or an ancestor of it; False when either does not resolve or git hangs."""
    try:
        return tip.startswith(commit) or is_ancestor(p, commit, tip)
    except subprocess.TimeoutExpired:
        return False


def fix_branches(p: Project, task: dict, refs: list[str]) -> dict[str, str]:
    """{fix branch: the reviewed branch it builds on}: for each branch in `refs`, the own branch of
    the newest code task that carries it, other than the review itself. A fix or re-plan of a failed
    review often cannot check the reviewed branch out (the reviewed task's worktree still holds it),
    so it commits on its own ttp/t<id>-... branch from that branch's head (ensure); its re-review,
    queued with it, names only the reviewed branch. A task carries a branch it labels
    `pr_branch:<branch>`, or the branch of the task it continues (a code task's branch, or a branch
    a continued review reviews). Failed and cancelled tasks carry nothing."""
    from .db import continues_id
    names = {r for r in refs if not _HEX.fullmatch(r)}
    if not names:
        return {}
    newest: dict[str, tuple[int, str]] = {}
    for t in p.db.q("SELECT * FROM tasks WHERE kind='code' AND branch IS NOT NULL AND branch!='' AND id!=? "
                    "AND status NOT IN ('failed','cancelled') AND (labels LIKE '%\"pr_branch:%' "
                    "OR labels LIKE '%\"continues:%')", (task["id"],)):
        if t["branch"] in names:
            continue
        on = {carried_branch(t)} & names
        prev = p.db.task(c) if not on and (c := continues_id(t)) is not None else None
        if prev and prev["kind"] == "review":
            on = set(named_refs(p, prev)) & names
        elif prev:
            on = {prev.get("branch")} & names
        for ref in on:
            if ref not in newest or t["id"] > newest[ref][0]:
                newest[ref] = (t["id"], t["branch"])
    return {b: ref for ref, (_, b) in sorted(newest.items(), key=lambda kv: kv[1][0])}


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


def _closest_ancestor(p: Project, heads: list[str], ref: str) -> str | None:
    """The head in `heads` that is an ancestor of `ref` with the fewest commits between them."""
    best: tuple[int, str] | None = None
    for head in heads:
        if not _git(p.root, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}", check=False):
            continue
        if subprocess.run(["git", "-C", str(p.root), "merge-base", "--is-ancestor", head, ref],
                          capture_output=True, timeout=120).returncode != 0:
            continue
        n = int(_git(p.root, "rev-list", "--count", f"{head}..{ref}") or 0)
        if best is None or n < best[0]:
            best = (n, head)
    return best[1] if best else None


FETCH_TIMEOUT_S = 30   # the local-only check's fetch; an unreachable remote must not hold up the tick


def dirty_tracked(repo: Path, timeout_s: float = 30) -> list[str] | None:
    """The tracked paths with uncommitted changes (staged or not) in the checkout at `repo`, sorted;
    untracked files and changes inside submodules are left out. None when `repo` is not a git work
    tree or git fails or is slow. Reads only: nothing is staged, committed or changed."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=no",
                              "--ignore-submodules=dirty"], capture_output=True, text=True, timeout=timeout_s,
                             stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    paths, items = set(), iter(out.stdout.split("\0"))
    for e in items:
        if len(e) > 3:
            paths.add(e[3:])
            if e[0] in "RC":
                next(items, None)   # a rename's or copy's source follows its new path
    return sorted(paths)


CHECKOUT_DIRTY_MAX = 2000   # checkout_state: past this many dirty paths, `dirty` is None (not compared)


def checkout_state(repo: Path, timeout_s: float = 30, dirty: bool = True) -> dict | None:
    """Where the checkout at `repo` stands: `branch` (its short name, "" when HEAD is detached),
    `head` (the commit) and `dirty` (dirty_tracked; None past CHECKOUT_DIRTY_MAX, when git
    failed or without `dirty`). None when `repo` is not a git work tree or git fails. Reads only."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD"],
                              capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
        if head.returncode != 0:
            return None
        branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    dirty = dirty_tracked(repo, timeout_s) if dirty else None
    return {"branch": branch.stdout.strip() if branch.returncode == 0 else "", "head": head.stdout.strip(),
            "dirty": dirty if dirty is not None and len(dirty) <= CHECKOUT_DIRTY_MAX else None}


def local_only(repo: Path, branches: list[str], targets: list[str] = (), known: set = frozenset(),
               timeout_s: float = FETCH_TIMEOUT_S, pushed: set | frozenset = frozenset(),
               superseded: dict | None = None) -> tuple[dict, bool, dict] | None:
    """Which of `branches` hold finished work whose only copy is on this machine. Returns ({branch:
    (head, commits on no remote)}, whether the fetch of every remote worked, {branch: head} of the
    others), after that fetch; with a failed fetch the remote refs may be old, so a branch may be
    listed that the remote has. A branch is on a remote when a remote-tracking branch contains its
    head, or when its changes already are in one of `targets` (the branch it is delivered to, as
    `origin/<name>` or `<name>`, and each remote's default branch) although its head is not: a
    reviewer rebased, amended, squashed or batched it (see delivered). A (branch, head) in `known`
    was found on a remote before and is not looked at again. So is a branch whose head the push
    queue pushed or landed (`pushed`, see pushq.pushed_heads), or an ancestor of one, and a branch
    a task recorded as superseded (`superseded`, {branch: [(head, sha)]}, see is_superseded). A
    branch that no longer exists is left out. None when `repo` is not a git repository or has no remote: there
    is nothing to compare with. Pushes nothing."""
    try:
        if not _git(repo, "remote", check=False).split():
            return None
        fetched = subprocess.run(["git", "-C", str(repo), "fetch", "--all", "--quiet"], capture_output=True,
                                 text=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                                 env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).returncode == 0
    except subprocess.TimeoutExpired:
        fetched = False
    except (OSError, subprocess.SubprocessError):
        return None
    refs: list[str] = []
    for t in [*targets, *_git(repo, "for-each-ref", "--format=%(symref)", "refs/remotes/*/HEAD", check=False).split()]:
        for cand in (t, f"refs/remotes/{t}", f"refs/remotes/origin/{t}"):
            full = _git(repo, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", cand, check=False)
            if full.startswith("refs/remotes/"):
                if full not in refs:
                    refs.append(full)
                break
    out, clean = {}, {}
    for b in dict.fromkeys(branches):
        head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}^{{commit}}", check=False)
        if not head:
            continue
        if ((b, head) in known or queue_pushed(repo, head, pushed)
                or is_superseded(repo, head, (superseded or {}).get(b, ()))
                or _git(repo, "branch", "-r", "--contains", head, check=False)
                or any(delivered(repo, head, ref) for ref in refs)):
            clean[b] = head
            continue
        ahead = _git(repo, "rev-list", "--count", head, "--not", "--remotes", check=False)
        out[b] = (head, int(ahead) if ahead.isdigit() else 0)
    return out, fetched, clean


def is_superseded(repo: Path, head: str, records) -> bool:
    """Whether a task recorded `head`'s work as superseded by a commit a remote has: a record
    (recorded head, sha) whose head is `head` or contains it, and whose sha a remote-tracking branch
    contains. A branch that moved past its recorded head has work the record does not cover."""
    for rec_head, sha in records:
        if rec_head != head and subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, rec_head],
                                               capture_output=True, timeout=120).returncode != 0:
            continue
        if _git(repo, "branch", "-r", "--contains", sha, check=False):
            return True
    return False


def queue_pushed(repo: Path, head: str, pushed: set | frozenset) -> bool:
    """Whether the push queue pushed or landed `head` or a commit that contains it: `pushed` holds
    the heads of its pushed and landed rows (pushq.pushed_heads). Heads git no longer has count
    only by name."""
    if not pushed:
        return False
    if head in pushed:
        return True
    r = subprocess.run(["git", "-C", str(repo), "rev-list", "--ignore-missing", "-n1", head, "--not", *sorted(pushed)],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0 and not r.stdout.strip()


_DELIVERED: dict[tuple[str, str, str], bool] = {}   # (repo, head, ref commit) -> delivered(): both fixed, so is the answer
_DELIVERED_MAX = 4096


def delivered(repo: Path, head: str, ref: str, pushed: set | frozenset = frozenset()) -> bool:
    """Whether the changes of `head` since it left `ref` are already in `ref`: the push queue pushed
    or landed it (`pushed`, see queue_pushed), every commit has a patch-equivalent one there (`git
    cherry`: rebased or cherry-picked) or one with the same author, author date and subject (a rebase
    with conflicts, or an amend, keeps them; a subject alone is not enough), its whole change has the
    patch-id of one commit there (squashed into one), every file it changed is the same there
    (amended or squashed, maybe with other work), or its whole diff reverts cleanly from `ref`'s tree (batched, and later work
    touched the same files elsewhere). Remembered per (repo, head, commit of `ref`). The revert is only
    tried when `ref` changed every file `head` did since they parted (else it cannot apply): against
    a far-behind branch of a large repository the binary diff can take minutes. A git call that times
    out counts as not delivered."""
    if queue_pushed(repo, head, pushed):
        return True
    ref_commit = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if not ref_commit:
        return False
    key = (str(repo), head, ref_commit)
    if key not in _DELIVERED:
        if len(_DELIVERED) >= _DELIVERED_MAX:
            _DELIVERED.clear()
        try:
            _DELIVERED[key] = _delivered(repo, head, ref_commit)
        except subprocess.TimeoutExpired:
            _DELIVERED[key] = False   # too big to tell: not delivered, and the other branches still get checked
    return _DELIVERED[key]


def _patch_ids(repo: Path, cmd: list[str]) -> set[str]:
    """The `git patch-id --stable` ids of the patches git `cmd` prints."""
    out = subprocess.run(["git", "-C", str(repo), *cmd], capture_output=True, timeout=120).stdout
    ids = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=out, capture_output=True,
                         timeout=120).stdout.decode(errors="replace")
    return {ln.split()[0] for ln in ids.splitlines() if ln.split()}


def _delivered(repo: Path, head: str, ref: str) -> bool:
    import tempfile
    base = _git(repo, "merge-base", ref, head, check=False)
    if not base:
        return False
    cherry = subprocess.run(["git", "-C", str(repo), "cherry", ref, head], capture_output=True, text=True, timeout=120)
    if cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines()):
        return True
    fmt = "--format=%an%x00%ae%x00%at%x00%s"
    mine = _git(repo, "log", "--no-merges", fmt, f"{base}..{head}", check=False).splitlines()
    if mine and set(mine) <= set(_git(repo, "log", "--no-merges", fmt, f"{base}..{ref}", check=False).splitlines()):
        return True
    files = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, check=False).split("\0")
    files = [f for f in files if f]
    # A squash of exactly this change touches only these files: scan only them, not the whole range.
    if len(mine) > 1 and files and (whole := _patch_ids(repo, ["diff", "--no-renames", base, head])) and (
            whole <= _patch_ids(repo, ["log", "-p", "--no-renames", "--no-merges", "--format=commit %H",
                                       f"{base}..{ref}", "--", *files])):
        return True
    if not files or not _git(repo, "diff", "--no-renames", "--name-only", ref, head, "--", *files, check=False):
        return True
    theirs = set(_git(repo, "diff", "--no-renames", "--name-only", "-z", base, ref, check=False).split("\0"))
    if not set(files) <= theirs:
        return False   # a file `ref` left as it was at `base` cannot hold `head`'s change to it
    patch = subprocess.run(["git", "-C", str(repo), "diff", "--binary", "--no-renames", base, head],
                           capture_output=True, timeout=120).stdout
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if subprocess.run(["git", "-C", str(repo), "read-tree", ref], capture_output=True, env=env,
                          timeout=120).returncode != 0:
            return False
        return subprocess.run(["git", "-C", str(repo), "apply", "--cached", "--reverse", "--check"], input=patch,
                              capture_output=True, env=env, timeout=120).returncode == 0

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Isolated workspaces for code tasks: one git worktree and branch per task, under the project's
ignored folder, so parallel workers never share a working tree with each other or the user."""
from __future__ import annotations

import os
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
    checked-out branch."""
    d = p.config().get("delivery") or {}
    if d.get("base_ref"):
        return d["base_ref"]
    for ref in [str(d.get("push_branch") or "").strip(), *charter_branches(p)]:
        for cand in (f"origin/{ref}", ref) if ref else ():
            if _has(p, cand):
                return cand
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
    """Remove a task's worktree; its branch stays. Call only once keep_reason() found nothing to lose.
    Ignored files go with it (sweep keeps one holding hand-off artifacts). No --force: git itself then
    refuses a worktree with changes or with submodules, whose commits may live only in the worktree's
    own git directory."""
    path = p.worktrees / f"t{task_id}"
    if path.exists():
        _git(p.root, "worktree", "remove", str(path))
    _git(p.root, "worktree", "prune", check=False)


def keep_reason(path: Path) -> str | None:
    """Why this worktree must stay, or None when removing it loses nothing: no submodules set up in
    it, no uncommitted or untracked files, and its HEAD is on a local or remote branch (the task's
    own branch is never deleted, so its commits stay reachable)."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=300)
    # A submodule's repository lives in the worktree's own git directory (modules/), so commits made
    # in it exist nowhere else and removing the worktree would delete them. Such a worktree stays.
    gd = git("rev-parse", "--absolute-git-dir")
    if gd.returncode != 0:
        return "git rev-parse failed"
    modules = Path(gd.stdout.strip()) / "modules"
    subs = git("submodule", "status")
    if subs.returncode != 0:
        return "git submodule status failed"
    inited = [ln.split()[1] for ln in subs.stdout.splitlines() if ln.strip() and not ln.startswith("-")]
    if inited or (modules.is_dir() and any(modules.iterdir())):
        return ("has submodules set up (" + (", ".join(inited[:3]) or "modules/") + "); their commits may exist "
                "only here, so it is never removed automatically")
    st = git("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")
    if st.returncode != 0:
        return "git status failed"
    dirty = st.stdout.splitlines()
    if dirty:
        return f"uncommitted changes in {len(dirty)} path(s), e.g. {dirty[0][3:].strip()[:80]}"
    held = git("for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)", "refs/heads", "refs/remotes")
    if held.returncode != 0:
        return "git for-each-ref failed"
    return None if held.stdout.strip() else "commits on no branch (detached HEAD)"


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
    may be globs (tmp/px_*.pt), and may carry trailing text ("out.mp4 (the video)", "run.log:12"),
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
    and only those: tracked ones are on the branch, other untracked ones keep the worktree."""
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
            if rel != "." and rel not in found and any(
                    rel == e or rel.startswith(e + "/") or e.startswith(rel + "/") for e in ignored):
                found.append(rel)
    return found


def clear_caches(path: Path, names: list[str] | None = None, keep: list[str] = ()) -> list[str]:
    """Delete ignored build and cache directories in a worktree. A cache-named directory goes only
    when git ignores it wholly; else just the ignored entries named like one inside it. Nothing that
    is, holds or lies in a path of `keep` (hand-off artifacts) goes. Returns what went."""
    import fnmatch
    import shutil
    pats = CACHE_DIRS if names is None else names
    listed = _ignored(path)
    if listed is None:
        return []
    found: set[str] = set()
    for entry in listed:
        parts = entry.split("/")
        for i, part in enumerate(parts):
            prefix = "/".join(parts[:i + 1])
            if prefix in listed and any(fnmatch.fnmatchcase(part, pat) for pat in pats):
                found.add(prefix)
                break
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


# A finished task's worktree stays at least this long, and while the coordinator has not yet seen how
# the task ended: the review that pushes from it is often queued only after that.
FINISH_GRACE_S = 3600


def held_by(p: Project, task: dict, open_tasks: list[dict], now: float) -> str | None:
    """Why a finished task's worktree must stay untouched for now, or None."""
    user = needed_by(task, open_tasks)
    if user:
        return f"task #{user['id']} ({user['status']}) may still use it"
    if p.db.one("SELECT id FROM events WHERE task=? AND status='queued' LIMIT 1", (task["id"],)):
        return "the coordinator has not yet seen how it ended"
    if now - float(task["updated"] or now) < FINISH_GRACE_S:
        return f"it ended under {FINISH_GRACE_S // 60} min ago; kept for a review"
    return None


def sweep(p: Project, *, older_than_s: float = 0, names: list[str] | None = None,
          skip=lambda task: False) -> list[dict]:
    """Tidy the worktrees of finished tasks (done, failed, cancelled) with no run still going: clear
    their build and cache directories, then remove each one whose removal loses nothing (see
    keep_reason) and that holds no task's hand-off artifacts (see handoff_artifacts:
    `git worktree remove` deletes ignored files, where workers often leave them). A worktree that may still be wanted (see held_by: an unfinished task needs it, the
    coordinator has not seen the finish yet, or it ended under FINISH_GRACE_S ago) is left as it
    is, and reported with `held` set. Branches stay, so a task that `continues` one starts from its
    commits. One sweep at a time per project; a busy lock returns no results."""
    import fcntl
    import time
    from .db import TERMINAL_TASK_STATES
    if not p.worktrees.is_dir():
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
        open_tasks = p.db.q("SELECT id, status, spec, depends_on, labels FROM tasks WHERE status NOT IN (%s)"
                            % ",".join("?" * len(TERMINAL_TASK_STATES)), TERMINAL_TASK_STATES)
        for path in sorted(p.worktrees.iterdir()):
            m = re.fullmatch(r"t(\d+)", path.name)
            task = p.db.task(int(m.group(1))) if m else None
            if (not task or task["status"] not in TERMINAL_TASK_STATES or not is_git(path)
                    or now - float(task["updated"] or now) < older_than_s or skip(task)
                    or p.db.one("SELECT id FROM runs WHERE task=? AND status='running' LIMIT 1", (task["id"],))):
                continue
            res = {"task": task["id"], "path": str(path), "branch": task["branch"], "status": task["status"],
                   "updated": task["updated"]}
            why = held_by(p, task, open_tasks, now)
            if why:
                out.append({**res, "cleared": [], "held": True, "why": why})
                continue
            try:
                entries = handoff_paths(p) if entries is None else entries
                arts = handoff_artifacts(p, task["id"], path, entries=entries)
                res["cleared"] = clear_caches(path, names, keep=arts)
                res["why"] = keep_reason(path) or (
                    f"hand-off artifacts inside: {', '.join(arts[:3])}"[:200] + (f" (+{len(arts) - 3} more)"
                                                                               if len(arts) > 3 else "")
                    if arts else None)
                if res["why"] is None:
                    remove(p, task["id"])
            except Exception as e:
                res["why"] = f"error: {e}"[:300]
            out.append(res)
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
    """What a review task reviews: the branches of the tasks it depends on, and the task branches
    and commit hashes its spec names."""
    from .db import dependency_ids
    spec = task.get("spec") or ""
    ids = [i for i in dependency_ids(task) if i is not None]
    refs = [r["branch"] for r in p.db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL AND branch!=''")
            if r["id"] in ids or re.search(rf"(?<![\w/.-]){re.escape(r['branch'])}(?![\w/-])", spec)]
    refs += _HEX.findall(spec)
    return list(dict.fromkeys(refs))


def diff_lines(p: Project, refs: list[str], since: list[str] | None = None) -> dict[str, int | None] | None:
    """Lines changed per file by `refs` since they left the base branch (None for a binary file),
    or None when they change nothing measurable, such as a commit already on the base.
    With `since` (heads an earlier review saw), a ref descending from one of them is measured from
    the closest such head instead: only what changed after that review."""
    base = resolve_base(p)
    out: dict[str, int | None] = {}
    for ref in refs:
        if not _git(p.root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False):
            continue
        start = _closest_ancestor(p, since or [], ref) or base
        for line in _git(p.root, "diff", "--numstat", "--no-renames", f"{start}...{ref}").splitlines():
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

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
from .project import Project, durable_write


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
    checked-out branch."""
    return named_base(p) or _git(p.root, "rev-parse", "--abbrev-ref", "HEAD")


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


def ensure(p: Project, task: dict) -> tuple[Path, str]:
    """Create (or reuse) the task's worktree. A retried task continues on its own branch."""
    branch = task.get("branch") or f"ttp/t{task['id']}-{slug(task['title'])}"
    path = p.worktrees / f"t{task['id']}"
    if path.exists() and is_git(path):
        link_paths(p, path)
        return path, branch
    p.worktrees.mkdir(parents=True, exist_ok=True)
    exists = _git(p.root, "rev-parse", "--verify", "--quiet", branch, check=False)
    if exists:
        _git(p.root, "worktree", "add", str(path), branch)
    else:
        _git(p.root, "fetch", "--quiet", "origin", check=False)
        _git(p.root, "worktree", "add", "-b", branch, str(path), continued_head(p, task) or resolve_base(p))
    link_paths(p, path)
    return path, branch


LINK_PATHS = [".venv"]   # worktree.link_paths: the default
NEVER_OWN_KINDS = ("review", "harness")   # reviews work in the change's worktree, harness tasks in the harness


def gets_worktree(p: Project, kind: str) -> bool:
    """Whether a task of this kind runs in a worktree of its own: code always; another kind when
    `worktree.kinds` lists it (off by default: a fresh worktree lacks the root's untracked build
    trees, outputs and submodule checkouts that such tasks often use). Never review or harness."""
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


def link_paths(p: Project, path: Path) -> list[str]:
    """Symlink the project checkout's git-ignored environment entries (`worktree.link_paths`,
    relative paths) into the worktree at `path`, so a check that runs `.venv/bin/python` works in a
    fresh worktree. An entry is linked only when it exists in the checkout, git ignores it there (so
    it is not tracked) and nothing is at its place in the worktree, tracked or not. A rule like
    `.venv/` matches directories only, not a link, so a link git would not ignore gets a `/<entry>`
    line in the repository's info/exclude: `git add -A` never commits it and the worktree still
    counts as clean. Removing the worktree deletes the link, never what it points to. Returns the
    entries linked; failures are skipped."""
    rels = (p.config().get("worktree") or {}).get("link_paths", LINK_PATHS)
    if not isinstance(rels, list):
        return []
    done = []
    for rel in rels:
        if not isinstance(rel, str) or not rel.strip("/") or Path(rel).is_absolute() or ".." in Path(rel).parts:
            continue
        rel = rel.strip("/")
        src, dest, made = p.root / rel, path / rel, False
        try:
            if (not src.exists() or os.path.lexists(dest) or not dest.parent.is_dir()
                    or not _ignores(p.root, rel) or _git(path, "ls-files", "--", rel, check=False)):
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
    own, own_worktree), so its commits carry over.
    A branch of its own lets the old worktree stay checked out until it is pruned."""
    old_id = continues_id(task)
    old = p.db.task(old_id) if old_id else None
    if not old or not own_worktree(old) or not old["branch"]:
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
    """Bytes these entries take (a directory: everything in it), links not followed."""
    total = 0
    for rel in rels:
        full = path / rel
        try:
            if full.is_dir() and not full.is_symlink():
                total += sum(f.lstat().st_size for f in full.rglob("*") if f.is_symlink() or f.is_file())
            else:
                total += full.lstat().st_size
        except OSError:
            continue
    return total


def leftovers(p: Project, task_id: int, path: Path, rels: list[str], max_mb: float | None,
              dry_run: bool = False) -> tuple[str | None, dict]:
    """A finished task's worktree whose only dirty entries are untracked files (inspect): move them
    to <its last run dir>/worktree-leftovers/ when they total at most `max_mb` (None: LEFTOVERS_MAX_MB;
    0 or less: never). Returns (why it must stay or None, what moved or would move)."""
    limit = LEFTOVERS_MAX_MB if max_mb is None else float(max_mb)
    size = _size(path, rels)
    info = {"files": len(rels), "bytes": size}
    head = f"uncommitted untracked files only ({len(rels)}, {size / 1e6:.1f} MB, e.g. {rels[0][:80] if rels else '?'})"
    if limit <= 0:
        return f"{head}; moving them out is off (disk.worktree_leftovers_max_mb)", info
    if size > limit * 1e6:
        return f"{head}, over the {limit:g} MB limit for moving them out (disk.worktree_leftovers_max_mb)", info
    run = last_run_dir(p, task_id)
    if run is None:
        return f"{head}; the task has no run directory to move them to", info
    info["to"] = str(run / LEFTOVERS_DIR)
    if dry_run:
        return None, info
    move_leftovers(path, rels, run / LEFTOVERS_DIR)
    return keep_reason(path), info


def last_run_dir(p: Project, task_id: int) -> Path | None:
    r = p.db.one("SELECT id, dir FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (task_id,))
    return (Path(r["dir"]) if r["dir"] else p.runs / str(r["id"])) if r else None


LEFTOVERS_DIR = "worktree-leftovers"
DIRTY_EVENT = "worktree_uncommitted"   # the coordinator's event for a finished worktree's tracked changes
LEFTOVERS_MAX_MB = 50   # disk.worktree_leftovers_max_mb: untracked files up to this size move out


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
    from .push import running
    path = (p.worktrees / f"t{task['id']}").resolve()
    if any(Path(m.get("repo") or "/").resolve() == path for m in running(p)):
        return "a detached `ttp push` runs in it"
    user = needed_by(task, open_tasks)
    if user:
        return f"task #{user['id']} ({user['status']}) may still use it"
    if p.db.one("SELECT id FROM events WHERE task=? AND status='queued' AND kind!=? LIMIT 1",
                (task["id"], DIRTY_EVENT)):
        return "the coordinator has not yet seen how it ended"
    if now - float(task["updated"] or now) < FINISH_GRACE_S:
        return f"it ended under {FINISH_GRACE_S // 60} min ago; kept for a review"
    return None


def sweep(p: Project, *, older_than_s: float = 0, names: list[str] | None = None,
          skip=lambda task: False, leftovers_max_mb: float | None = None, dry_run: bool = False) -> list[dict]:
    """Tidy the worktrees of finished tasks (done, failed, cancelled) with no run still going: clear
    their build and cache directories, then remove each one whose removal loses nothing (see
    keep_reason) and that holds no task's hand-off artifacts (see handoff_artifacts:
    `git worktree remove` deletes ignored files, where workers often leave them). A worktree that may still be wanted (see held_by: an unfinished task needs it, the
    coordinator has not seen the finish yet, or it ended under FINISH_GRACE_S ago) is left as it
    is, and reported with `held` set. One whose only dirty entries are small untracked files has
    them moved to its last run's directory first (see leftovers; reported in `moved`); one with
    modified tracked files is kept and reported with `tracked` (paths) and `fingerprint`. Branches
    stay, so a task that `continues` one starts from its commits. `dry_run` changes nothing and
    reports what a sweep would do (`why` None: it would go). One sweep at a time per project; a
    busy lock returns no results."""
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
                res["cleared"] = [] if dry_run else clear_caches(path, names, keep=arts)
                info = inspect(path)
                why = info["why"]
                if info["dirty"] == "tracked":
                    res.update(tracked=info["tracked"], fingerprint=info["fingerprint"])
                elif info["dirty"] == "untracked" and not arts:
                    why, moved = leftovers(p, task["id"], path, info["untracked"], leftovers_max_mb, dry_run)
                    if "to" in moved:
                        res["moved"] = moved
                res["why"] = why or (
                    f"hand-off artifacts inside: {', '.join(arts[:3])}"[:200] + (f" (+{len(arts) - 3} more)"
                                                                               if len(arts) > 3 else "")
                    if arts else None)
                if res["why"] is None and not dry_run:
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


def checkout_state(repo: Path, timeout_s: float = 30) -> dict | None:
    """Where the checkout at `repo` stands: `branch` (its short name, "" when HEAD is detached),
    `head` (the commit) and `dirty` (dirty_tracked; None past CHECKOUT_DIRTY_MAX or when git
    failed). None when `repo` is not a git work tree or git fails. Reads only."""
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
    dirty = dirty_tracked(repo, timeout_s)
    return {"branch": branch.stdout.strip() if branch.returncode == 0 else "", "head": head.stdout.strip(),
            "dirty": dirty if dirty is not None and len(dirty) <= CHECKOUT_DIRTY_MAX else None}


def local_only(repo: Path, branches: list[str], targets: list[str] = (), known: set = frozenset(),
               timeout_s: float = FETCH_TIMEOUT_S, pushed: set | frozenset = frozenset()) -> tuple[dict, bool, dict] | None:
    """Which of `branches` hold finished work whose only copy is on this machine. Returns ({branch:
    (head, commits on no remote)}, whether the fetch of every remote worked, {branch: head} of the
    others), after that fetch; with a failed fetch the remote refs may be old, so a branch may be
    listed that the remote has. A branch is on a remote when a remote-tracking branch contains its
    head, or when its changes already are in one of `targets` (the branch it is delivered to, as
    `origin/<name>` or `<name>`, and each remote's default branch) although its head is not: a
    reviewer rebased, amended, squashed or batched it (see delivered). A (branch, head) in `known`
    was found on a remote before and is not looked at again. So is a branch whose head the push
    queue pushed or landed (`pushed`, see pushq.pushed_heads), or an ancestor of one. A branch that
    no longer exists is left out. None when `repo` is not a git repository or has no remote: there
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
                or _git(repo, "branch", "-r", "--contains", head, check=False)
                or any(delivered(repo, head, ref) for ref in refs)):
            clean[b] = head
            continue
        ahead = _git(repo, "rev-list", "--count", head, "--not", "--remotes", check=False)
        out[b] = (head, int(ahead) if ahead.isdigit() else 0)
    return out, fetched, clean


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
    cherry`: rebased or cherry-picked) or one with the same subject (at least 20 characters: rebased
    with conflicts, or amended), every file it changed is the same there (amended or squashed, maybe
    with other work), or its whole diff reverts cleanly from `ref`'s tree (batched, and later work
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


def _delivered(repo: Path, head: str, ref: str) -> bool:
    import tempfile
    base = _git(repo, "merge-base", ref, head, check=False)
    if not base:
        return False
    cherry = subprocess.run(["git", "-C", str(repo), "cherry", ref, head], capture_output=True, text=True, timeout=120)
    if cherry.returncode == 0 and not any(line.startswith("+") for line in cherry.stdout.splitlines()):
        return True
    mine = _git(repo, "log", "--no-merges", "--format=%s", f"{base}..{head}", check=False).splitlines()
    if mine and all(len(m) >= 20 for m in mine) and set(mine) <= set(
            _git(repo, "log", "--no-merges", "--format=%s", f"{base}..{ref}", check=False).splitlines()):
        return True
    files = _git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, check=False).split("\0")
    files = [f for f in files if f]
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

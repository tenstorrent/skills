"""Clean checkouts for `ttp checks`: its local checks run in a fresh detached checkout of the commit
whose tree the pass is recorded for (a scratch clone, `clone`, in the system's temporary folder),
never in the authoring worktree, so a file that exists only on disk there (ignored, untracked, or an
uncommitted edit) cannot make a check pass. `ttp push`, `ttp land` and the push queue already run
their checks in scratch worktrees of the exact commit they push; remote checks copy that commit with
`git archive` (remotecheck).

Submodules the authoring worktree has checked out are cloned from its copies (no network) and
checked out at the commit's gitlink; one that cannot be is an error, never a fall back to the
authoring tree. A check with `"cwd": "authoring"` opts out and runs in the authoring worktree (it
needs build outputs or a venv made there); its pass is labelled as not run from a clean tree.

Each checkout holds an flock on its folder's lock file while it is in use: a checkout whose lock
is free was left by a run that died (a reboot, SIGKILL) and is removed by the next `make`."""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import gitmemo, repoid

PREFIX = "ttp-checks-"
AUTHORING = "authoring"   # a check's "cwd" that runs it in the authoring worktree
CWDS = (AUTHORING, "clean")
HINT_MAX = 5
LOCK = ".lock"
_NOISE = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules")
_TOKEN = re.compile(r"[A-Za-z0-9_.@+/-]+")


class Error(Exception):
    """The clean checkout could not be made: the checks fail with this message."""


def _git(cwd: str | Path, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
    run = lambda: subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=/dev/null", "-c",   # noqa: E731
                                  "submodule.recurse=false", *args], capture_output=True, text=True,
                                 timeout=gitmemo.timeout(timeout, args))
    return repoid.read(cwd, args, run) if repoid.identity(args) else run()


def _last(text: str) -> str:
    return " ".join((text or "").strip().splitlines()[-2:])[:300]


def authoring(c) -> bool:
    """Whether check `c` opted out of the clean checkout (`"cwd": "authoring"`)."""
    return getattr(c, "cwd", "") == AUTHORING


class Checkout:
    """A clean detached checkout of `head` of the repository at `top`; `close` removes it."""

    def __init__(self, top: str | Path, head: str, base: str | Path | None = None):
        """`base`: the folder to make it in (a check's RAM-disk scratch, removed with it), else the
        system temp folder."""
        self.top, self.head = Path(top), head
        prune(self.top)
        self.dir = Path(tempfile.mkdtemp(prefix=f"{PREFIX}{os.getpid()}-", dir=base))
        self.path = self.dir / "tree"
        self._lock = open(self.dir / LOCK, "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            clone(self.top, self.path, head, hooks=False)
            submodules(self.top, self.path)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Remove the checkout; safe to call twice."""
        if self._lock is None:
            return
        shutil.rmtree(self.dir, ignore_errors=True)
        self._lock.close()
        self._lock = None

    def __enter__(self) -> "Checkout":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def prune(top: str | Path | None = None, base: str | Path | None = None, grace_s: float = 600) -> list[Path]:
    """Remove the clean checkouts a dead run left (their lock is free; one with no lock file only once
    older than `grace_s`, as a run may be just making it), then, when one was a worktree an older
    version made, git's records of worktrees whose folder is gone in `top`. Returns the folders removed."""
    gone, worktrees = [], False
    for d in Path(base or tempfile.gettempdir()).glob(PREFIX + "*"):
        try:
            if not d.is_dir() or d.is_symlink() or d.stat().st_uid != os.getuid():
                continue
            lock = d / LOCK
            if not lock.exists():
                if time.time() - d.stat().st_mtime < grace_s:
                    continue
            else:
                with open(lock) as f:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        continue   # in use
            worktrees |= legacy(d)
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
        except OSError:
            continue
    if top and worktrees:
        try:
            _git(top, "worktree", "prune", timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    return gone


def legacy(d: Path) -> bool:
    """Whether checkout folder `d` holds a registered worktree (made before checkouts were clones)."""
    return (Path(d) / "tree" / ".git").is_file()


_PER_WORKTREE = ("refs/bisect/", "refs/worktree/", "refs/rewritten/")


def _plain(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """git as a check run in the checkout would call it (no hook or submodule override)."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          timeout=gitmemo.timeout(600, args))


def clone(top: str | Path, path: str | Path, head: str, hooks: bool = True) -> None:
    """Make `path` a detached checkout of `head` of the repository at `top` that adds nothing under
    its .git/worktrees: a sandbox that starts while a worktree there is added or removed fails to mount
    it (docs/providers.md). Like `git clone --shared` it borrows `top`'s objects (alternates) and
    copies none; it holds `top`'s refs as they are (heads, origin/*, tags) and reads `top`'s config
    (an include, so origin and its fetches work), hooks, info/exclude, info/attributes and LFS store,
    as a worktree of `top` would. It needs `top`'s objects: remove the folder as soon as its step
    ends; nothing of it is left in `top`. `hooks`: whether the checkout runs a post-checkout hook, as
    `git worktree add` does. Raises Error."""
    top, path = Path(top), Path(path)
    fail = lambda what, r: Error(f"cannot make a clean checkout of {head[:12]}: {what}: {_last(r.stderr)}")  # noqa: E731
    r = _plain(top, "rev-parse", "--git-common-dir")
    sha = _plain(top, "rev-parse", "--verify", "-q", f"{head}^{{commit}}")
    if r.returncode or sha.returncode:
        raise fail("git rev-parse", r if r.returncode else sha)
    common, sha = (top / r.stdout.strip()).resolve(), sha.stdout.strip()
    fmt = _plain(top, "rev-parse", "--show-object-format").stdout.strip()
    r = _plain(top, "init", "-q", *([f"--object-format={fmt}"] if fmt and fmt != "sha1" else []), str(path))
    if r.returncode:
        raise fail("git init", r)
    gitdir = path / ".git"
    (gitdir / "objects" / "info" / "alternates").write_text(f"{common / 'objects'}\n")
    sets = [("include.path", str(common / "config"))]
    for key, val in (("core.hooksPath", common / "hooks"), ("lfs.storage", common / "lfs")):
        if not _plain(top, "config", "--get", key).stdout.strip():
            sets.append((key, str(val)))
    for key, val in sets:
        if (r := _plain(path, "config", "--add", key, val)).returncode:
            raise fail("git config", r)
    (gitdir / "info").mkdir(exist_ok=True)
    for name in ("exclude", "attributes"):
        if (common / "info" / name).is_file():
            shutil.copyfile(common / "info" / name, gitdir / "info" / name)
    r = _plain(top, "for-each-ref", "--format=%(objectname) %(refname)")
    if r.returncode:
        raise fail("git for-each-ref", r)
    refs = [ln for ln in r.stdout.splitlines() if ln and not ln.partition(" ")[2].startswith(_PER_WORKTREE)]
    (gitdir / "packed-refs").write_text("".join(f"{ln}\n" for ln in refs))
    r = _plain(path, *([] if hooks else ["-c", "core.hooksPath=/dev/null"]), "checkout", "-q", "--detach", sha)
    if r.returncode:
        raise fail("git checkout", r)


def submodules(src: Path, dst: Path) -> None:
    """Populate in `dst` each submodule `src` has checked out, by cloning `src`'s copy (no network) and
    checking out the gitlink `dst`'s commit records, then theirs in turn. One `src` has not checked out
    stays empty, as it is there. Raises Error when one cannot be populated."""
    if not (dst / ".gitmodules").is_file():
        return
    r = _git(dst, "config", "--file", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$")
    for line in r.stdout.splitlines():
        rel = line.partition(" ")[2].strip()
        if not rel:
            continue
        have, want = src / rel, dst / rel
        top = _git(have, "rev-parse", "--show-toplevel") if have.is_dir() else None
        if not top or top.returncode or Path(top.stdout.strip()).resolve() != have.resolve():
            continue   # not checked out in the authoring worktree either
        sha = _git(dst, "rev-parse", f"HEAD:{rel}").stdout.strip()
        if not sha:
            continue
        want.mkdir(parents=True, exist_ok=True)
        r = _git(dst, "-c", "protocol.file.allow=always", "clone", "-q", "--no-checkout", str(have.resolve()),
                 str(want))
        if r.returncode == 0:
            r = _git(want, "checkout", "-q", "--detach", sha)
        if r.returncode:
            raise Error(f"cannot populate submodule {rel} at {sha[:12]} in the clean checkout from the "
                        f"authoring worktree's copy (no network is used; fetch that commit there first): "
                        f"{_last(r.stderr)}")
        submodules(have, want)


def _noise(rel: str) -> bool:
    return any(part in _NOISE for part in rel.split("/")) or rel.endswith((".pyc", ".pyo"))


def _rel(token: str, roots: tuple[str, ...]) -> str:
    """`token` as a repo-relative path: an absolute one under one of `roots`, or a relative one."""
    while token.startswith("./"):
        token = token[2:]
    for root in roots:
        if token.startswith(root + "/"):
            return token[len(root) + 1:]
    if token.startswith("/") or not token:
        return ""
    norm = os.path.normpath(token)
    return "" if norm in (".", "..") or norm.startswith("../") else norm


def authoring_only(top: str | Path, clean: str | Path, cmd: str, output: str, limit: int = HINT_MAX) -> list[str]:
    """Files (or folders) in the authoring worktree `top`, not in the clean checkout `clean`, that a
    failing check may have needed: paths its `output` or command `cmd` name, then the untracked and
    ignored files under the folders the command names. Caches and venvs are left out. Model-free and
    cheap: a stat per named path and one `git ls-files` per kind."""
    top, clean = Path(top), Path(clean)
    roots = tuple(str(r) for r in {clean, clean.resolve(), top, top.resolve()})
    found: list[str] = []
    dirs: list[str] = []
    named = set(_TOKEN.findall(cmd))
    for token in dict.fromkeys(_TOKEN.findall(cmd + "\n" + (output or "")[-65536:])):
        rel = _rel(token, roots)
        if not rel or _noise(rel):
            continue
        a, b = top / rel, clean / rel
        try:
            if os.path.lexists(a) and not os.path.lexists(b):
                found.append(rel)
            elif token in named and os.path.lexists(b):   # a folder the check runs, or a file's folder
                dirs.append(rel if b.is_dir() else os.path.dirname(rel))
        except OSError:
            continue
        if len(found) >= limit:
            return found
    dirs = list(dict.fromkeys(d for d in dirs if d))
    if dirs:
        for extra in ([], ["--ignored", "--directory"]):
            try:
                r = _git(top, "ls-files", "-z", "--others", "--exclude-standard", *extra, "--", *dirs, timeout=60)
            except (OSError, subprocess.SubprocessError):
                continue
            for rel in r.stdout.split("\0"):
                rel = rel.rstrip("/")
                if rel and rel not in found and not _noise(rel) and not os.path.lexists(clean / rel):
                    found.append(rel)
                    if len(found) >= limit:
                        return found
    return found


def hint(files: list[str]) -> str:
    """The failure message's line naming the files only the authoring worktree has, or ""."""
    if not files:
        return ""
    return ("present only in the authoring worktree, not committed: " + ", ".join(files)
            + "; the checks ran on a clean checkout of the commit, so commit what they need "
              "(or give a check that needs build outputs \"cwd\": \"authoring\")")

"""Clean checkouts for `ttp checks`: its local checks run in a fresh detached worktree of the commit
whose tree the pass is recorded for (`git worktree add --detach`, in the system's temporary folder),
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

from . import gitmemo

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
    return subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=/dev/null", "-c", "submodule.recurse=false",
                           *args], capture_output=True, text=True, timeout=gitmemo.timeout(timeout, args))


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
            r = _git(self.top, "worktree", "add", "-q", "--detach", str(self.path), head)
            if r.returncode:
                raise Error(f"cannot make a clean checkout of {head[:12]}: {_last(r.stderr)}")
            submodules(self.top, self.path)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Remove the checkout and its worktree record; safe to call twice."""
        if self._lock is None:
            return
        try:
            _git(self.top, "worktree", "remove", "--force", "--force", str(self.path), timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            _git(self.top, "worktree", "prune", timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
        self._lock.close()
        self._lock = None

    def __enter__(self) -> "Checkout":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def prune(top: str | Path | None = None, base: str | Path | None = None, grace_s: float = 600) -> list[Path]:
    """Remove the clean checkouts a dead run left (their lock is free; one with no lock file only once
    older than `grace_s`, as a run may be just making it), then git's records of worktrees whose folder
    is gone in `top`. Returns the folders removed."""
    gone = []
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
            shutil.rmtree(d, ignore_errors=True)
            gone.append(d)
        except OSError:
            continue
    if top and gone:
        try:
            _git(top, "worktree", "prune", timeout=120)
        except (OSError, subprocess.SubprocessError):
            pass
    return gone


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

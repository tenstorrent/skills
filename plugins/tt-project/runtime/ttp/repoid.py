# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A repository's identity, read with git once per process and reused while it cannot have changed.

A `ttp push` or a daemon tick asks the same repository the same questions from many call sites:
its toplevel, its common directory, whether it is bare, its remotes and their URLs. Each answer
depends only on where git finds the repository from that path and on its configuration, so read()
keeps a call's successful result under (path, args) with a stamp of both, taken with a few stat()
calls and no git:

- the path's real path and the directory where git's discovery finds `.git` walking up from it,
  with that `.git`'s identity (device and inode; a gitfile's content too), the gitdir and the common
  directory it points to (a `worktree add` or `remove`, a re-init, a moved repository);
- the repository's config, the worktree's config.worktree, the global and system config files
  (device, inode, mtime and size: git rewrites a config file through a lock and a rename, so
  `remote add`, `remote set-url`, `remote rename` and any `git config` write show);
- the environment variables git reads its location and configuration from.

A lookup whose stamp differs, or older than MAX_AGE_S (a config file pulled in by [include] is not
stamped), runs git again. Nothing is kept for a call that failed or raised, nor where discovery
cannot be followed without git: GIT_DIR and the like set, a walk up to a GIT_CEILING_DIRECTORIES
entry, a bare repository or a path inside a git directory. Never use it for HEAD, branches, the index or status: they move without any of this
changing."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

MAX_AGE_S = 60.0
# Set, they move discovery off the path's own `.git`: such a call is never cached.
_BYPASS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_NAMESPACE")

_lock = threading.Lock()
_memo: dict[tuple, tuple[tuple, float, Any]] = {}


def clear() -> None:
    """Forget every kept answer."""
    with _lock:
        _memo.clear()


def _st(path: str, full: bool = True) -> tuple | None:
    try:
        s = os.stat(path)
    except OSError:
        return None
    return (s.st_dev, s.st_ino, s.st_mtime_ns, s.st_size) if full else (s.st_dev, s.st_ino)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(4096).strip()
    except OSError:
        return None


def _is_gitdir(d: str) -> bool:
    """`d` looks like a git directory itself (a bare repository, a .git or a worktree's gitdir)."""
    return os.path.isfile(os.path.join(d, "HEAD")) and (
        os.path.isdir(os.path.join(d, "objects")) or os.path.isfile(os.path.join(d, "commondir")))


def stamp(path: str | Path) -> tuple | None:
    """What the identity of the repository at `path` depends on, or None when it cannot be told
    without git (see the module's docstring)."""
    if any(os.environ.get(v) for v in _BYPASS):
        return None
    real = os.path.realpath(path)
    if not os.path.isdir(real):   # git -C fails there: never answer for it from the memo
        return None
    # git never walks up into a GIT_CEILING_DIRECTORIES entry; a walk reaching one, or a directory above
    # one, is not followed here (the path's own directory is always looked at, as git does).
    ceilings = [os.path.realpath(c) for c in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if c]
    d = real
    while True:
        dot = os.path.join(d, ".git")
        try:
            st = os.stat(dot)
        except FileNotFoundError:
            st = None
        except OSError:
            return None
        if st is not None:
            break
        if _is_gitdir(d):
            return None
        up = os.path.dirname(d)
        if up == d or any(c == up or c.startswith(up.rstrip(os.sep) + os.sep) for c in ceilings):
            return None
        d = up
    pointer = None
    if stat.S_ISDIR(st.st_mode):
        gitdir = dot
    else:
        pointer = _read(dot)
        if not pointer or not pointer.startswith("gitdir:"):
            return None
        gitdir = os.path.normpath(os.path.join(d, pointer[len("gitdir:"):].strip()))
    rel = _read(os.path.join(gitdir, "commondir"))
    common = os.path.normpath(os.path.join(gitdir, rel)) if rel else gitdir
    env = os.environ
    home = env.get("HOME", "")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    configs = [os.path.join(common, "config"), os.path.join(gitdir, "config.worktree")]
    configs += [env["GIT_CONFIG_GLOBAL"]] if env.get("GIT_CONFIG_GLOBAL") else \
        [os.path.join(home, ".gitconfig"), os.path.join(xdg, "git", "config")]
    if not env.get("GIT_CONFIG_NOSYSTEM"):
        configs.append(env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig")
    return (real, d, pointer, (st.st_dev, st.st_ino), _st(gitdir, False), rel, _st(common, False),
            tuple(_st(c) for c in configs),
            tuple(sorted((k, v) for k, v in env.items() if k.startswith("GIT_") or k in ("HOME", "XDG_CONFIG_HOME"))))


# The reads read() may keep (git's arguments after `-C path`); `remote get-url` takes a remote's name last.
_EXACT = frozenset({("rev-parse", "--show-toplevel"), ("rev-parse", "--git-common-dir"),
                    ("rev-parse", "--path-format=absolute", "--git-common-dir"),
                    ("rev-parse", "--git-dir", "--git-common-dir"), ("rev-parse", "--is-bare-repository"),
                    ("rev-parse", "--is-inside-work-tree"), ("remote",)})
_URL = frozenset({("remote", "get-url"), ("remote", "get-url", "--push"), ("remote", "get-url", "--all"),
                  ("remote", "get-url", "--push", "--all")})


def identity(args: tuple | list) -> bool:
    """Whether `args` is one of the identity reads read() may keep."""
    args = tuple(args)
    return args in _EXACT or (len(args) >= 3 and args[:-1] in _URL and not args[-1].startswith("-"))


def _ok(value: Any) -> bool:
    return not isinstance(value, subprocess.CompletedProcess) or value.returncode == 0


def read(path: str | Path, args: tuple | list, fn: Callable[[], Any]) -> Any:
    """fn()'s result for the identity read `args` (git's arguments after `-C path`) in `path`: the
    kept one while its stamp holds, else fn() run again (and kept when it succeeded: a call that
    raised, or a CompletedProcess that exited non-zero, is not)."""
    key = (os.path.abspath(path), tuple(args))
    s = stamp(path)
    if s is None:
        return fn()
    now = time.monotonic()
    with _lock:
        hit = _memo.get(key)
    if hit is not None and hit[0] == s and now - hit[1] < MAX_AGE_S:
        return hit[2]
    value = fn()
    if _ok(value):
        with _lock:
            _memo[key] = (s, now, value)
    return value

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Git reads answered from memory while what they read has not changed.

An idle daemon with push work spawned a few git processes a second: `git remote` and
`git remote get-url` for the same remotes on every tick, and one `merge-base --is-ancestor` per
commit per landed check. run(repo, args, call) answers three kinds of read without git:

- `remote` and `remote get-url ...`: kept under the stat of every config file git reads for the
  repository (its common and worktree config, the global and system files) and the GIT_* environment.
  Git rewrites a config file by renaming a new one over it, so any change gives a new stat; one
  changed within RACY_S seconds is not kept. Nothing is
  kept for a repository whose config includes other files, or under GIT_DIR-style overrides.
- `merge-base --is-ancestor <sha> <tip>` on full commit ids: ancestry between two commits never
  changes, so the answer is keyed on both ids (a "no" also on the pack directory, and given only for a
  commit rev-parse found, as git answers 128 for a missing one). The answer comes from the tip's ancestor set, read with
  one `git rev-list` per tip (from the previous tip's set with `rev-list --boundary <new> ^<old>`
  when the branch moved forward), so N commits against one tip cost one git call, and none once
  read. A tip with more than MAX_SET ancestors keeps a partial set; an id outside it asks git itself.
- `rev-parse --verify --quiet <full id>^{commit}` that found the commit: kept while the repository's
  pack directory is unchanged (gc and repack rewrite it).

A repository whose history can change under the same ids (shallow, grafts, replace refs) is never
cached. A call that raised or timed out keeps nothing, and every answer is a new CompletedProcess."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import gitmemo

RACY_S = 2.0            # a config file changed this recently is read by git every time
MAX_SET = 100_000       # ancestors kept per tip; more and the set is partial
MAX_TIPS = 4            # ancestor sets kept at once
MAX_ENTRIES = 50_000    # pair, config and commit answers kept at once
FULL = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# Environment that moves where git looks for the repository or its config: nothing is kept under it.
# (GIT_CONFIG_* and the rest are part of a config answer's key; GIT_CEILING_DIRECTORIES is followed.)
UNSAFE_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY",
              "GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE",
              "GIT_DISCOVERY_ACROSS_FILESYSTEM")

_lock = threading.Lock()
_answers: dict[tuple, tuple[tuple, int, str, str]] = {}     # (kind, dirs, args) -> (stamp, rc, out, err)
_pairs: dict[tuple, tuple[bool, tuple]] = {}               # (common dir, sha, tip) -> (ancestor, pack stamp)
_sets: dict[tuple, tuple[frozenset, bool]] = {}             # (common dir, tip) -> (ancestors, complete)
_last_tip: dict[str, str] = {}                              # common dir -> the tip last read
_includes: dict[str, tuple[tuple, bool]] = {}               # config path -> (stamp, has include)
_system: list[str] = []
calls = {"git": 0}                                          # git processes this module started


def clear() -> None:
    with _lock:
        for d in (_answers, _pairs, _sets, _last_tip, _includes):
            d.clear()


def _st(path: str) -> tuple:
    try:
        s = os.stat(path)
    except OSError:
        return ()
    return (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _settled(stamp: tuple) -> bool:
    """No config file changed within RACY_S: a coarse clock cannot hide a second quick write."""
    newest = max((max(st[2], st[3]) for st in stamp[0] if st), default=0)
    return time.time_ns() - newest > RACY_S * 1e9


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _dirs(repo: Path | str) -> tuple[str, str] | None:
    """(git dir, common dir) of the repository at or above `repo`, as git finds it; None when unsure."""
    if any(k in os.environ for k in UNSAFE_ENV):
        return None
    try:
        d = os.path.realpath(os.fspath(repo))
    except (OSError, ValueError):
        return None
    ceilings = {os.path.realpath(c) for c in os.environ.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep) if c}
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            gitdir = dot
            break
        if os.path.isfile(dot):
            line = _read(dot)
            if not line.startswith("gitdir:"):
                return None
            gitdir = os.path.realpath(os.path.join(d, line[len("gitdir:"):].strip()))
            break
        if os.path.isfile(os.path.join(d, "HEAD")) and os.path.isdir(os.path.join(d, "objects")):
            gitdir = d    # a bare repository
            break
        up = os.path.dirname(d)
        if up == d or up in ceilings:
            return None
        d = up
    common = gitdir
    if os.path.isfile(os.path.join(gitdir, "commondir")):
        common = os.path.realpath(os.path.join(gitdir, _read(os.path.join(gitdir, "commondir"))))
    if not os.path.isdir(common):
        return None
    return gitdir, common


def _system_config() -> list[str] | None:
    """Where this git keeps its system config (known once per process; a git too old to name it uses
    /etc/gitconfig); None when git did not answer in time."""
    if not _system:
        try:
            r = gitmemo.run(["git", "var", "GIT_CONFIG_SYSTEM"], capture_output=True, text=True,
                            timeout=10, read=True, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            calls["git"] += 1
        found = r.stdout.strip() if r.returncode == 0 else ""
        _system.extend(sorted({found, "/etc/gitconfig"} - {""}))
    return _system


def _config_files(gitdir: str, common: str) -> list[str] | None:
    home = os.environ.get("HOME", "")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    glob = ([os.environ["GIT_CONFIG_GLOBAL"]] if "GIT_CONFIG_GLOBAL" in os.environ
            else [os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")])
    system = [] if os.environ.get("GIT_CONFIG_NOSYSTEM") else (
        [os.environ["GIT_CONFIG_SYSTEM"]] if "GIT_CONFIG_SYSTEM" in os.environ else _system_config())
    if system is None:
        return None
    return [*system, *glob, os.path.join(common, "config"), os.path.join(gitdir, "config.worktree")]


def _has_include(path: str, st: tuple) -> bool:
    hit = _includes.get(path)
    if hit and hit[0] == st:
        return hit[1]
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return True
    inc = bool(re.search(r"^\s*\[\s*include", text, re.I | re.M))
    with _lock:
        _includes[path] = (st, inc)
    return inc


def _config_stamp(dirs: tuple[str, str]) -> tuple | None:
    out = []
    files = _config_files(*dirs)
    if files is None:
        return None
    for f in files:
        st = _st(f)
        if st and _has_include(f, st):
            return None
        out.append(st)
    env = tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("GIT_") or k in ("HOME", "XDG_CONFIG_HOME")))
    return (tuple(out), env)


def _history_fixed(common: str) -> bool:
    """Ids name the same history here: not shallow, no grafts, no replace refs."""
    if os.path.exists(os.path.join(common, "shallow")) or os.path.exists(os.path.join(common, "info", "grafts")):
        return False
    rep = os.path.join(common, "refs", "replace")
    if os.path.isdir(rep) and any(os.scandir(rep)):
        return False
    packed = os.path.join(common, "packed-refs")
    st = _st(packed)
    if not st:
        return True
    hit = _includes.get(packed)
    if not (hit and hit[0] == st):
        try:
            text = Path(packed).read_text(errors="replace")
        except OSError:
            return False
        hit = (st, " refs/replace/" in text)
        with _lock:
            _includes[packed] = hit
    return not hit[1]


def _packs(common: str) -> tuple:
    """The pack directory's stat: gc and repack, which may drop unreachable commits, change it."""
    alts = [a for a in os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES", "").split(os.pathsep) if a]
    return tuple(_st(os.path.join(o, "pack")) for o in (os.path.join(common, "objects"), *alts))


def _keep(d: dict, key, value) -> None:
    with _lock:
        if len(d) >= MAX_ENTRIES:
            d.clear()
        d[key] = value


def _git(repo: Path | str, *args: str, timeout: float | None) -> subprocess.CompletedProcess:
    calls["git"] += 1
    return gitmemo.run(["git", "-C", os.fspath(repo), *args], capture_output=True, text=True,
                       timeout=timeout,
                       stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _ancestor_set(repo: Path | str, common: str, tip: str, timeout: float | None) -> tuple[frozenset, bool] | None:
    """(ancestors of `tip` including it, complete) with one git call; None when git could not say."""
    with _lock:
        got = _sets.get((common, tip))
        old = _last_tip.get(common)
        base = _sets.get((common, old)) if old else None
    if got:
        return got
    if base and old != tip:
        r = _git(repo, "rev-list", "--boundary", tip, f"^{old}", timeout=timeout)
        lines = r.stdout.split()
        if r.returncode == 0 and f"-{old}" in lines:      # old is an ancestor of tip: grow its set
            new = {ln for ln in lines if not ln.startswith("-")}
            got = (base[0] | new, base[1] and len(base[0]) + len(new) <= MAX_SET)
    if not got:
        r = _git(repo, "rev-list", f"--max-count={MAX_SET}", tip, timeout=timeout)
        if r.returncode != 0:
            return None
        ids = r.stdout.split()
        got = (frozenset(ids), len(ids) < MAX_SET)
    with _lock:
        if len(_sets) >= MAX_TIPS:
            _sets.clear()
        _sets[(common, tip)] = got
        _last_tip[common] = tip
    return got


def is_ancestor(repo: Path | str, sha: str, tip: str, timeout: float | None = None) -> bool | None:
    """`sha` is an ancestor of (or is) `tip`, both full ids; None when it cannot be answered from cache
    or one rev-list (the caller then asks git itself)."""
    dirs = _dirs(repo)
    if not dirs or not FULL.fullmatch(sha) or not FULL.fullmatch(tip) or not _history_fixed(dirs[1]):
        return None
    key, packs = (dirs[1], sha, tip), _packs(dirs[1])
    hit = _pairs.get(key)
    if hit is not None and (hit[0] or hit[1] == packs):
        return hit[0]
    found = _ancestor_set(repo, dirs[1], tip, timeout)
    if found is None:
        return None
    ids, complete = found
    if sha in ids:
        _keep(_pairs, key, (True, packs))
        return True
    # Not an ancestor, if the commit exists (git says 128, not 1, for a missing one): known only once
    # rev-parse found it with the packs as they are now.
    known = _answers.get(("commit", dirs[1], f"{sha}^{{commit}}"))
    if complete and known and known[0] == packs:
        _keep(_pairs, key, (False, packs))
        return False
    return None


def ancestors(repo: Path | str, shas, tip: str, timeout: float | None = None) -> dict[str, bool | None]:
    """{sha: is_ancestor(repo, sha, tip)} for several commits: one git call for all of them."""
    return {s: is_ancestor(repo, s, tip, timeout) for s in shas}


def fixed(repo: Path | str, what: str, sha: str, tip: str, fn: Callable[[], bool | None]) -> bool | None:
    """fn()'s answer about two full commit ids (`what` names the question), kept like an ancestry one:
    a yes for good, a no while the pack directory is unchanged. fn() answers None when a git read it
    made failed: that is returned and kept nowhere. Just fn() for anything else."""
    dirs = _dirs(repo) if FULL.fullmatch(sha or "") and FULL.fullmatch(tip or "") else None
    if not dirs or not _history_fixed(dirs[1]):
        return fn()
    key, packs = (what, dirs[1], sha, tip), _packs(dirs[1])
    hit = _pairs.get(key)
    if hit is not None and (hit[0] or hit[1] == packs):
        return hit[0]
    got = fn()
    if got is None:
        return None
    got = bool(got)
    _keep(_pairs, key, (got, packs))
    return got


def _done(rc: int, out: str, err: str, args) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["git", *args], rc, out, err)


def run(repo: Path | str, args: tuple | list, call: Callable[[], subprocess.CompletedProcess],
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """call()'s result (git `args` in `repo`, text output), from memory when the read is one kept here."""
    args = tuple(args)
    if args[:1] == ("remote",) and (len(args) == 1 or args[1] == "get-url"):
        dirs = _dirs(repo)
        stamp = _config_stamp(dirs) if dirs else None
        if stamp is None:
            return call()
        key = ("config", dirs, args)
        hit = _answers.get(key)
        if hit and hit[0] == stamp:
            return _done(hit[1], hit[2], hit[3], args)
        r = call()
        if r.returncode in (0, 2) and isinstance(r.stdout, str) and _settled(stamp) and _config_stamp(dirs) == stamp:
            _keep(_answers, key, (stamp, r.returncode, r.stdout, r.stderr or ""))
        return r
    if len(args) == 4 and args[:2] == ("merge-base", "--is-ancestor"):
        got = is_ancestor(repo, args[2], args[3], timeout)
        if got is not None:
            return _done(0 if got else 1, "", "", args)
        r = call()
        dirs = _dirs(repo) if r.returncode in (0, 1) else None
        if dirs and FULL.fullmatch(args[2]) and FULL.fullmatch(args[3]) and _history_fixed(dirs[1]):
            _keep(_pairs, (dirs[1], args[2], args[3]), (r.returncode == 0, _packs(dirs[1])))
        return r
    if (len(args) == 4 and args[:3] == ("rev-parse", "--verify", "--quiet") and args[3].endswith("^{commit}")
            and FULL.fullmatch(args[3][:-len("^{commit}")])):
        dirs = _dirs(repo)
        if not dirs or not _history_fixed(dirs[1]):
            return call()
        stamp = _packs(dirs[1])
        key = ("commit", dirs[1], args[3])
        hit = _answers.get(key)
        if hit and hit[0] == stamp:
            return _done(0, hit[2], "", args)
        r = call()
        if r.returncode == 0 and isinstance(r.stdout, str) and r.stdout.strip():
            _keep(_answers, key, (stamp, 0, r.stdout, ""))
        return r
    return call()

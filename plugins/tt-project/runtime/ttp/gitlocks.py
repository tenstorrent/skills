# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Stale `.git/*.lock` files in the harness repository, moved aside model-free.

A git command killed mid-write (a crash, a reboot, a timeout) leaves its lock file behind, and every
later harness commit then fails until someone removes it. A lock counts as stale only when it is
older than `harness_git.stale_lock_s` (600 s; 0: off; the old `watchdog.stale_git_lock_s` is read
when only it is set), far past any harness git command's timeout, and nothing holds it: no process
has the file open (Linux) and no git process of this user runs in or names the repository. A git
process whose working directory cannot be read counts as holding it. Stale locks are moved to
`.git/ttp-stale-locks/<stamp>/`, never deleted. Project.clear_stale_git_locks runs this before each
harness commit and in the daemon's harness sweep.

The project root's own repository gets a narrower sweep (clear_root_refs, run by the daemon's harness
sweep): only ref locks (`refs/**/*.lock`, `packed-refs.lock`) in its common git dir, never index.lock,
HEAD.lock, shallow.lock, a config.lock git wrote or anything under worktrees/. Such a lock, left by a
writer a kill or reboot ended, makes ref updates and `git gc --auto` fail with "cannot lock ref". It
counts as stale once it is older than `harness_git.root_ref_lock_s` (3600 s; 0: off), no process has it
open, and no
live process that started before the lock was written works in the repository (its working directory
or GIT_DIR inside it): git may close a ref lock before renaming it into place, so an open-file check
alone does not prove a lock abandoned, while a process started after the lock cannot own it. Without
/proc it does nothing. Moved locks go to `<common dir>/ttp-stale-locks/<stamp>/`, never deleted.
One config.lock is also swept there: the empty read-only placeholder a sandbox (bwrap) bind-mounts for
each sandboxed command, which a killed run leaves behind and which then blocks every `git config` write.
It counts as stale only when it is empty, has no write bits, is older than root_ref_lock_s, no process has
it open and no live git process working in the repository started before it. git itself writes
config.lock writable, so a non-empty, writable, young or open one is never touched.
Beyond the harness, only these ref locks are ever touched, never a user's checkout or a task's worktree.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

STALE_S = 600.0
ROOT_STALE_S = 3600.0
KEEP_DIR = "ttp-stale-locks"
_SKIP_DIRS = {"objects", "lfs", KEEP_DIR}


def settings(cfg: dict) -> float:
    """The age in seconds at which a lock counts as stale (0: off) from project.json's settings."""
    cfg = cfg or {}
    v = (cfg.get("harness_git") or {}).get("stale_lock_s")
    if v is None:
        v = (cfg.get("watchdog") or {}).get("stale_git_lock_s")
    try:
        return max(float(STALE_S if v is None else v), 0.0)
    except (TypeError, ValueError):
        return STALE_S


def root_settings(cfg: dict) -> float:
    """harness_git.root_ref_lock_s: the age at which a root repository's ref lock may be stale (0: off)."""
    v = ((cfg or {}).get("harness_git") or {}).get("root_ref_lock_s")
    try:
        return max(float(ROOT_STALE_S if v is None else v), 0.0)
    except (TypeError, ValueError):
        return ROOT_STALE_S


def git_dir(repo: Path) -> Path | None:
    """The repository's git directory: `.git` itself, or where a `.git` file points."""
    dot = Path(repo) / ".git"
    if dot.is_dir():
        return dot
    try:
        line = dot.read_text().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    target = Path(line[len("gitdir:"):].strip())
    return target if target.is_absolute() else (Path(repo) / target).resolve()


def candidates(gdir: Path, now: float, min_age_s: float) -> list[Path]:
    """Lock files under the git directory older than min_age_s."""
    out = []
    for top, dirs, files in os.walk(gdir):
        if Path(top) == gdir:
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if not f.endswith(".lock"):
                continue
            path = Path(top) / f
            try:
                if now - path.lstat().st_mtime >= min_age_s:
                    out.append(path)
            except OSError:
                continue
    return sorted(out)


def _own_processes() -> list[tuple[int, str]]:
    """(pid, command line) of this user's processes; ps -U works on Linux and macOS."""
    r = subprocess.run(["ps", "-o", "pid=,args=", "-U", str(os.getuid())], capture_output=True, text=True,
                       timeout=20)
    out = []
    for line in r.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != os.getpid():
            out.append((int(pid), args.strip()))
    return out


def _is_git(args: str) -> bool:
    name = Path(args.split(" ", 1)[0]).name if args else ""
    return name == "git" or name.startswith("git-")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return next((Path(x[1:]) for x in r.stdout.splitlines() if x.startswith("n/")), None)


def _open_fds(pid: int) -> set[str]:
    fd_dir = Path(f"/proc/{pid}/fd")
    out = set()
    try:
        for fd in fd_dir.iterdir():
            try:
                out.add(os.readlink(fd))
            except OSError:
                continue
    except OSError:
        pass
    return out


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def holders(repo: Path, gdir: Path, locks: list[Path], procs: list[tuple[int, str]] | None = None) -> list[int]:
    """Pids that may hold a lock of this repository: a process with one of the lock files open, or a
    git process running in or naming the repository (or whose working directory is unreadable)."""
    roots = [Path(repo).resolve(), Path(gdir).resolve()]
    lock_names = {str(p) for p in locks} | {str(p.resolve()) for p in locks if p.exists()}
    out = []
    for pid, args in (_own_processes() if procs is None else procs):
        if lock_names & _open_fds(pid):
            out.append(pid)
            continue
        if not _is_git(args):
            continue
        if any(str(r) in args for r in roots):
            out.append(pid)
            continue
        cwd = _cwd(pid)
        if cwd is None and not _alive(pid):
            continue   # ended since ps listed it
        if cwd is None or _within(cwd.resolve(), roots):
            out.append(pid)
    return out


def keep_dir(gdir: Path, now: float) -> Path:
    """Where clear() moves the stale locks it finds at `now`."""
    return Path(gdir) / KEEP_DIR / time.strftime("%Y%m%dT%H%M%S", time.localtime(now))


def clear(repo: Path, min_age_s: float = STALE_S, now: float | None = None,
          procs: list[tuple[int, str]] | None = None) -> tuple[list[Path], list[int]]:
    """Move the repository's stale lock files aside to keep_dir(): (moved, pids that kept them), the
    moved ones by their old paths. Nothing is moved while any process may hold one."""
    if min_age_s <= 0:
        return [], []
    gdir = git_dir(Path(repo))
    if gdir is None or not gdir.is_dir():
        return [], []
    now = time.time() if now is None else now
    locks = candidates(gdir, now, min_age_s)
    if not locks:
        return [], []
    held = holders(Path(repo), gdir, locks, procs)
    if held:
        return [], held
    dest = keep_dir(gdir, now)
    moved = []
    for path in locks:
        try:
            if now - path.lstat().st_mtime < min_age_s:   # written again since: a live holder
                continue
            to = dest / path.relative_to(gdir)
            to.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, to)
            moved.append(path)
        except OSError:
            continue
    return moved, []


def common_dir(gdir: Path) -> Path:
    """The common git dir of a git dir: a linked worktree's `commondir` target, else the git dir itself."""
    try:
        line = (Path(gdir) / "commondir").read_text().strip()
    except OSError:
        return Path(gdir)
    target = Path(line)
    return target if target.is_absolute() else (Path(gdir) / target).resolve()


def ref_locks(cdir: Path, now: float, min_age_s: float) -> list[tuple[Path, float]]:
    """(path, age in seconds) of the regular-file ref locks under the common dir older than min_age_s."""
    found = [Path(cdir) / "packed-refs.lock"]
    for top, _dirs, files in os.walk(Path(cdir) / "refs"):   # never follows symlinked dirs
        found += [Path(top) / f for f in files if f.endswith(".lock")]
    out = []
    for path in found:
        try:
            st = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and now - st.st_mtime > min_age_s:
            out.append((path, now - st.st_mtime))
    return sorted(out)


def config_placeholder(cdir: Path, now: float, min_age_s: float) -> list[tuple[Path, float]]:
    """[(path, age s)] of the common dir's config.lock when it is a sandbox's leftover placeholder: a
    regular empty file with no write bits older than min_age_s; else []."""
    path = Path(cdir) / "config.lock"
    try:
        st = path.lstat()
    except OSError:
        return []
    if (stat.S_ISREG(st.st_mode) and st.st_size == 0 and not st.st_mode & 0o222
            and now - st.st_mtime > min_age_s):
        return [(path, now - st.st_mtime)]
    return []


def _boot_time(proc: str) -> float | None:
    try:
        line = next(x for x in Path(f"{proc}/stat").read_text().splitlines() if x.startswith("btime "))
        return float(line.split()[1])
    except (OSError, StopIteration, IndexError, ValueError):
        return None


def _proc_table(proc: str) -> list[dict]:
    """Every process this one can see in `proc`: pid, uid, start (epoch s, None: unreadable), comm, cwd and
    GIT_DIR/GIT_COMMON_DIR as absolute paths (None: unreadable), and the paths its descriptors open."""
    btime, tick = _boot_time(proc), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    me, out = os.getpid(), []
    for n in os.listdir(proc):
        if not n.isdigit() or int(n) == me:
            continue
        base = f"{proc}/{n}"
        row = {"pid": int(n), "uid": None, "start": None, "comm": "", "cwd": None, "gitdirs": [], "fds": set()}
        try:
            row["uid"] = os.stat(base).st_uid
            fields = Path(f"{base}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                continue   # a zombie holds nothing
            ticks = int(fields[19])
            row["start"] = None if btime is None else btime + ticks / tick
        except (OSError, IndexError, ValueError):
            if row["uid"] is None:
                continue   # gone
        try:
            row["comm"] = Path(f"{base}/comm").read_text().strip()
        except OSError:
            pass
        try:
            row["cwd"] = os.readlink(f"{base}/cwd")
        except OSError:
            pass
        try:
            env = dict(x.decode(errors="replace").split("=", 1)
                       for x in Path(f"{base}/environ").read_bytes().split(b"\0") if b"=" in x)
        except OSError:
            env = {}
        for key in ("GIT_DIR", "GIT_COMMON_DIR"):
            if env.get(key) and row["cwd"]:
                row["gitdirs"].append(os.path.normpath(os.path.join(row["cwd"], env[key])))
        try:
            for fd in os.listdir(f"{base}/fd"):
                try:
                    row["fds"].add(os.readlink(f"{base}/fd/{fd}"))
                except OSError:
                    continue
        except OSError:
            pass
        out.append(row)
    return out


def ref_lock_holder(lock: Path, mtime: float, roots: list[Path], table: list[dict], git_only: bool = False) -> str:
    """Why a ref lock may still be owned ("" when nothing holds it): a process has it open, or a live
    process that started before the lock was written (2 s slack for the clock's rounding) has its
    working directory or GIT_DIR inside the repository. A git process of this user whose working
    directory cannot be read counts as one; any other process that cannot be read does not. git_only:
    only git processes count for the working-in-the-repository check (any opener still counts)."""
    names = {str(lock), os.path.realpath(lock)}
    mine = os.getuid()
    for row in table:
        if names & row["fds"]:
            return f"pid {row['pid']} has it open"
    for row in table:
        start = row["start"]
        if start is not None and start > mtime + 2.0:
            continue
        if git_only and not _is_git(row["comm"]):
            continue
        paths = ([row["cwd"]] if row["cwd"] else []) + row["gitdirs"]
        if any(_within(Path(os.path.realpath(x)), roots) for x in paths):
            return f"pid {row['pid']} works in the repository and started before the lock"
        if row["uid"] == mine and row["cwd"] is None and _is_git(row["comm"]):
            return f"pid {row['pid']} cannot be checked"
    return ""


def clear_root_refs(repo: Path, min_age_s: float = ROOT_STALE_S, now: float | None = None,
                    proc: str = "/proc") -> tuple[list[tuple[Path, float, Path]], list[tuple[Path, float, str]]]:
    """Move the stale ref locks and a stale config.lock placeholder (config_placeholder) of the repository
    at `repo` (its common git dir when it is a linked worktree) aside to keep_dir() of that dir: (moved as
    (old path, age s, new path), kept as (path, age s, holder-check result)) for locks past min_age_s.
    Without `proc` (/proc) nothing is checked or moved."""
    if min_age_s <= 0 or not Path(proc).is_dir():
        return [], []
    gdir = git_dir(Path(repo))
    if gdir is None or not gdir.is_dir():
        return [], []
    cdir = common_dir(gdir)
    now = time.time() if now is None else now
    locks = ref_locks(cdir, now, min_age_s) + config_placeholder(cdir, now, min_age_s)
    if not locks:
        return [], []
    roots = [Path(repo).resolve(), cdir.resolve()]
    table = _proc_table(proc)
    dest, moved, kept = keep_dir(cdir, now), [], []
    for path, age in locks:
        config = path.name == "config.lock" and path.parent == cdir
        why = ref_lock_holder(path, now - age, roots, table, git_only=config)
        if why:
            kept.append((path, age, why))
            continue
        try:
            if now - path.lstat().st_mtime <= min_age_s:   # written again since: a live holder
                continue
            if config and not config_placeholder(cdir, now, min_age_s):   # git took it over since
                continue
            to = dest / path.relative_to(cdir)
            to.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, to)
            moved.append((path, age, to))
        except OSError:
            continue
    return moved, kept

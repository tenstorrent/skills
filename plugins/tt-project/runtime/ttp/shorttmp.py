# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Short temp dirs for checks, detached jobs and fenced workers: /tmp/ttp-<uid>/<8 hex>.

A temp dir under the project's state is long, and tests add more to it (pytest's
pytest-of-<user>/pytest-N/<test>0): an AF_UNIX socket there passes the 108-byte limit (104 on
macOS) and fails with "AF_UNIX path too long". So each allocation gets a dir of its own under a
per-user 0700 root in the system temp root, which its owner records (owner.json, a detach job's
record, run.json) so finishing and recovery remove it. The root is named without links in its
base (/private/tmp on macOS) and is refused when it is a link, not a
directory or not this user's; callers then keep their old path under the state. A fenced worker
may write the whole root, as it may write the whole project state: it starts checks and detached
jobs itself.

A check's dir goes on the host's RAM disk (Linux tmpfs, ttp-<uid> beside the /tmp one) when it has
room for one more check suite's scratch (its clean checkout and temp files) beside every check dir
already there, keeps a quarter of the disk free, and leaves a fifth of the RAM available; else it
goes under /tmp as before. Suites write many short-lived git repositories: on a disk filesystem
several concurrent suites queue on its locks (measured 2.7-3.2x slower than tmpfs with two suites at
once beside four others), on tmpfs they do not.
"""
from __future__ import annotations

import os
import re
import stat
import sys
import uuid
from pathlib import Path

BASE = '/tmp'   # short on every supported host; tempfile.gettempdir() is long on macOS
TMPFS = '/dev/shm'
SUITE_BYTES = 4 << 30   # room kept per check on the RAM disk: a clean checkout plus its temp files
KEEP_FREE = 0.25        # of the RAM disk's size, never planned for
KEEP_RAM = 0.20         # of MemTotal, left available after the planned scratch
MOUNTS, MEMINFO = '/proc/self/mounts', '/proc/meminfo'
_NAME = re.compile(r'[0-9a-f]{8}')


def root() -> Path:
    # The base without links (macOS /tmp is a link to /private/tmp): checks validate a recorded dir
    # component by component without following links. ttp-<uid> itself is still refused as a link.
    # Tests point it at a short folder of their own.
    base = os.path.realpath(os.environ.get('TTP_TEST_SHORT_TMP') or BASE)
    return Path(base) / f'ttp-{os.getuid()}'


def tmpfs_root() -> Path | None:
    """ttp-<uid> on the RAM disk, or None off Linux or in tests that did not name one of their own."""
    if os.environ.get('TTP_TEST_SHORT_TMP'):
        base = os.environ.get('TTP_TEST_TMPFS')
    else:
        base = TMPFS if sys.platform.startswith('linux') else None
    return Path(os.path.realpath(base)) / f'ttp-{os.getuid()}' if base else None


def _roots() -> list[Path]:
    return [r for r in (root(), tmpfs_root()) if r is not None]


def _is_tmpfs(path: Path) -> bool:
    """The filesystem holding `path` (its longest mount point) is a tmpfs."""
    try:
        with open(MOUNTS, encoding='utf-8') as f:
            mounts = {p[1]: p[2] for p in (line.split() for line in f) if len(p) > 2}
    except OSError:
        return False
    holder = max((m for m in mounts if Path(m) == path or Path(m) in path.parents), key=len, default=None)
    return holder is not None and mounts[holder] == 'tmpfs'


def _space(path: Path) -> tuple[int, int]:
    """Free and total bytes of the filesystem at `path`."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def _ram() -> tuple[int, int]:
    """MemTotal and MemAvailable in bytes (0, 0 when unreadable)."""
    vals = {}
    try:
        with open(MEMINFO, encoding='utf-8') as f:
            for line in f:
                k, _, v = line.partition(':')
                if k in ('MemTotal', 'MemAvailable'):
                    vals[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return vals.get('MemTotal', 0), vals.get('MemAvailable', 0)


def tmpfs_room() -> Path | None:
    """The RAM disk root when it has room for one more check's scratch beside the check dirs already
    in it, each counted at SUITE_BYTES (free space already excludes what they hold: a margin), keeping
    KEEP_FREE of the disk free and KEEP_RAM of the RAM available; else None."""
    top = tmpfs_root()
    if top is None or not _is_tmpfs(top.parent):
        return None
    try:
        free, size = _space(top.parent)
        others = sum(1 for e in os.scandir(top) if _NAME.fullmatch(e.name)) if top.is_dir() else 0
    except OSError:
        return None
    need = SUITE_BYTES * (others + 1)
    total, avail = _ram()
    if free - need < KEEP_FREE * size or not total or avail - need < KEEP_RAM * total:
        return None
    return top


def on_tmpfs(value) -> Path | None:
    """`value` as a Path when it is a check dir make() put on the RAM disk, else None."""
    top = tmpfs_root()
    return Path(value) if top is not None and owned(value) and Path(value).parent == top else None


def _open_root(create: bool, top: Path | None = None) -> int:
    """The root, pinned, only when it is this user's own directory (made 0700 if it is not)."""
    top = top or root()
    if create:
        try:
            os.mkdir(top, 0o700)
        except FileExistsError:
            pass
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f'{top} is not this user\'s own directory')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def ready(top: Path | None = None) -> bool:
    """The root exists and is usable (a fence grants it to a worker, which makes dirs in it)."""
    try:
        os.close(_open_root(create=True, top=top))
        return True
    except OSError:
        return False


def make(check: bool = False) -> Path | None:
    """A new 0700 dir under the root, or None when the root is unusable (callers fall back). A check's
    dir goes on the RAM disk when tmpfs_room() names it and it is usable."""
    fast = tmpfs_room() if check else None
    return (_make(fast) if fast else None) or _make(root())


def _make(top: Path) -> Path | None:
    try:
        fd = _open_root(create=True, top=top)
    except OSError:
        return None
    try:
        for _ in range(8):
            name = uuid.uuid4().hex[:8]
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                return top / name
            except FileExistsError:
                continue
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def owned(value) -> bool:
    """A dir make() could have returned: a direct child of the root with its name pattern."""
    return (isinstance(value, (str, Path)) and bool(str(value)) and Path(value).parent in _roots()
            and bool(_NAME.fullmatch(Path(value).name)))


def remove(value) -> None:
    """Remove a dir make() returned, only through pinned descriptors; anything else is left alone."""
    if not owned(value):
        return
    from .checkdirs import _remove
    try:
        fd = _open_root(create=False, top=Path(value).parent)
    except OSError:
        return
    try:
        _remove(fd, Path(value).name)
    except OSError:
        pass
    finally:
        os.close(fd)

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""State-owned check outputs and temporary storage; never follow directory or lock links.

Managed scratch is only for check processes holding the lifetime guard. Supervisors and
non-check commands (including after_push) keep their ordinary temporary environment.
Legacy allocations without owner.json and old run-local scratch are left untouched, with
no migration. Only owned, unlocked check allocations are reclaimed; nothing else is.
A check's TMPDIR is a short dir (shorttmp) its owner.json records, so an AF_UNIX socket a test
binds beneath it stays within the path limit; it goes with the allocation. It is on the RAM disk
when that has room (shorttmp.make), and the check's clean checkout then goes inside it. Without a
usable short root the check keeps checks-tmp inside the allocation.

The folder-wide lock (check-runs/checks-tmp.lock) covers only short steps: publishing a new
allocation, and claiming an old one by renaming it to a trash name. Removing the tree runs after
it is released, so a slow or stuck removal never holds up new checks. new() waits for that lock
at most WAIT_S seconds, naming its holder once, then allocates without it: a fresh uuid name made
with mkdir is unique on its own, and cleanup skips an allocation until its owner record names a
live owner. Removal never follows a link and never descends into another filesystem (a mount
point, such as /sys, /proc or /dev bound or linked into a check's scratch).
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

FOLDER = 'check-runs'
SCRATCH, LOCK = 'checks-tmp', 'checks-tmp.lock'
OWNER = 'owner.json'
TRASH = '.trash-'   # an allocation claimed for removal; removed with no folder lock held
WAIT_S = 5.0        # longest wait for the folder-wide lock
PROC_LOCKS = '/proc/locks'           # names a bare flock's holder (Linux)
PSEUDO = ('/proc', '/sys', '/dev')   # their filesystems are never descended into on removal


@contextmanager
def directory(value, *, create=False):
    """Open every component without following links, keeping the directory pinned for operations."""
    if not value or not Path(value).is_absolute() or len(Path(value).parts) == 1 or '..' in Path(value).parts:
        raise OSError('TTP_RUN_DIR must be a non-root absolute directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(value).parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as e:
        raise OSError(f'TTP_RUN_DIR directory unavailable or unsafe: {value}: {e}') from e
    finally:
        os.close(fd)


def writable(value, *, create=False) -> str:
    """Fail before launching a check if its output directory is missing, linked or unwritable."""
    with directory(value, create=create) as fd:
        name = '.write-probe-' + uuid.uuid4().hex
        probe = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.write(probe, b'check output\n')
        finally:
            os.close(probe)
            os.unlink(name, dir_fd=fd)
    return str(value)


def new(state: Path, owner: Path | None = None) -> Path:
    from . import shorttmp
    folder = state / FOLDER
    tmp = shorttmp.make(check=True)
    try:
        name = _publish(folder, owner, tmp)
    except BaseException:
        shorttmp.remove(tmp)
        raise
    return folder / name


def _publish(folder: Path, owner: Path | None, tmp) -> str:
    from .runner import boot_id, proc_start
    with directory(folder, create=True) as fd:
        # Publish the creator identity before recovery can inspect this allocation. A lock
        # still held after WAIT_S does not stop it: the name is unique (uuid, mkdir).
        with _catalogue(fd, WAIT_S, folder / LOCK):
            for attempt in range(8):   # mkdir, not the lock, makes the name this run's own
                name = uuid.uuid4().hex
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    break
                except FileExistsError:
                    if attempt == 7:
                        raise
            with directory(folder / name) as child:
                out = os.open(OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=child)
                with os.fdopen(out, 'w') as f:
                    json.dump({'marker': str(owner) if owner else None, 'pid': os.getpid(),
                               'started': proc_start(os.getpid()), 'boot': boot_id(),
                               'tmp': str(tmp) if tmp else None}, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.fsync(child)
            os.fsync(fd)
    return name


def reports(path: Path) -> Path:
    """Stable artifacts live apart from the disposable allocation and its temporary files."""
    return path.parent.parent / 'check-reports' / path.name


def environment(path: Path, env: dict) -> dict:
    writable(path)
    scratch = path / SCRATCH
    writable(scratch, create=True)
    output = writable(reports(path), create=True)
    env = {k: v for k, v in env.items() if k not in ('TTP_PIDNS', 'TTP_CGROUP')}
    env.update(TTP_RUN_DIR=output, TTP_CHECK_OUTPUT_DIR=output)
    return env


def for_check(env: dict) -> dict:
    """Assign managed scratch only at the guarded check subprocess boundary."""
    if env.get('TTP_CHECKS_TMP_FD') and env.get('TTP_CHECK_OUTPUT_DIR'):
        output = Path(env['TTP_CHECK_OUTPUT_DIR'])
        folder = output.parent.parent / FOLDER
        scratch = _short(folder / output.name) or writable(folder / output.name / SCRATCH)
        env.update({k: scratch for k in ('TMPDIR', 'TMP', 'TEMP')})
        # Preserve user ceilings; this one only bounds discovery beneath check-runs.
        inherited = env.get('GIT_CEILING_DIRECTORIES', '')
        env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(filter(None, (inherited, str(folder))))
    return env


def _short(path: Path) -> str | None:
    """The short scratch the allocation's owner.json records, when it is still usable."""
    from . import shorttmp
    try:
        with directory(path) as fd:
            tmp = _read(fd, OWNER).get('tmp')
        return writable(tmp) if shorttmp.owned(tmp) else None
    except OSError:
        return None


def inherited() -> tuple[int, ...]:
    value = os.environ.get('TTP_CHECKS_TMP_FD')
    return (int(value),) if value is not None else ()


def finish(path: Path) -> None:
    """The owner has durably recorded its outcome; descendants still keep their lifetime lock."""
    if not _allocation(path):
        return
    try:
        with directory(path) as fd:
            if not _read(fd, OWNER):
                return
            done = os.open('finished', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=fd)
            os.close(done)
            os.fsync(fd)
    except OSError:
        pass
    # Another owner may be publishing or removing an allocation. Completion must not
    # abandon its cleanup just because that short catalogue operation is in flight.
    cleanup(path, whole=True, wait=True)


def release(path: Path) -> None:
    for fd in inherited():
        os.close(fd)
    os.environ.pop('TTP_CHECKS_TMP_FD', None)
    finish(path)


def restore(state: Path, value) -> Path:
    """Only rebuild a recorded direct child of this project's check-runs directory."""
    folder = state / FOLDER
    if not isinstance(value, str) or Path(value).parent != folder or len(Path(value).name) != 32 or \
            any(c not in '0123456789abcdef' for c in Path(value).name):
        raise OSError('TTP_RUN_DIR has no valid state-owned check output directory in its queue record')
    writable(value, create=True)
    return Path(value)


def take(path: Path):
    """A lifetime flock with no writes: links, devices and multiply-linked files are refused."""
    with directory(path) as fd:
        return _take_fd(fd)


@contextmanager
def _catalogue(fd: int, bound: float, where=LOCK):
    """The folder-wide lock, waited for at most `bound` seconds (none for 0); yields None when it
    stays busy. Its holder's pid is written in the lock file, so a waiter can name it."""
    deadline, said = time.monotonic() + bound, False
    while (f := _take_fd(fd)) is None:
        if bound <= 0:
            break
        holder = _holder(fd)
        if not said:
            print(f'ttp checks: {where} is busy: waiting for pid {holder or "(unknown)"} '
                  f'(up to {bound:g} s)', file=sys.stderr, flush=True)
            said = True
        if time.monotonic() >= deadline:
            print(f'ttp checks: pid {holder or "(unknown)"} still holds {where} after {bound:g} s; '
                  'going on without it', file=sys.stderr, flush=True)
            break
        time.sleep(0.05)
    if f is None:
        yield None
        return
    try:
        try:
            f.truncate()
            f.write(json.dumps({'pid': os.getpid()}))
            f.flush()
        except OSError:
            pass
        yield f
    finally:
        try:
            f.seek(0)
            f.truncate()
        except OSError:
            pass
        f.close()


def _holder(fd: int) -> int | None:
    """The pid holding the folder lock: from the lock file, else (Linux) /proc/locks. There a lock
    names the superblock's device, which on btrfs differs from the st_dev stat gives: an exact device
    match wins, else the one FLOCK on that inode number."""
    pid = _read(fd, LOCK).get('pid')
    if isinstance(pid, int) and pid > 0:
        return pid
    try:
        st = os.stat(LOCK, dir_fd=fd, follow_symlinks=False)
        dev = (os.major(st.st_dev), os.minor(st.st_dev))
        others = set()
        with open(PROC_LOCKS) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6 or parts[1] != 'FLOCK':
                    continue
                where = parts[5].split(':')
                if len(where) != 3 or where[2] != str(st.st_ino) or not parts[4].isdigit():
                    continue
                if (int(where[0], 16), int(where[1], 16)) == dev:
                    return int(parts[4])
                others.add(int(parts[4]))
        if len(others) == 1:
            return others.pop()
    except (OSError, ValueError):
        pass
    return None


def _take_fd(fd, *, wait=False):
    lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    f = os.fdopen(lock_fd, 'r+')
    try:
        st = os.fstat(lock_fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError('checks lifetime lock must be a single-link regular file')
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            f.close()
            return None
        return f
    except BaseException:
        f.close()
        raise


def _lstat(name: str, fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=fd, follow_symlinks=False)


def _pseudo() -> set:
    devs = set()
    for p in PSEUDO:
        try:
            devs.add(os.stat(p).st_dev)
        except OSError:
            pass
    return devs


def _remove(parent: int, name: str, *, last: str | None = None) -> None:
    """Remove only through pinned directory descriptors (also on Python 3.9). Links are unlinked,
    never followed; a directory on another filesystem (a mount point, or /proc, /sys, /dev) is
    left in place, never descended into. `last` (a top-level name) is unlinked after the rest."""
    _clear(parent, name, None, _pseudo(), last)


def _clear(parent: int, name: str, dev, pseudo: set, last: str | None = None) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        here = os.fstat(fd).st_dev
        if here in pseudo or (dev is not None and here != dev):
            raise OSError(f'{name}: on another filesystem; not removed')
        for entry in sorted(os.listdir(fd), key=lambda e: e == last):
            st = _lstat(entry, fd)
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != here or st.st_dev in pseudo:
                    continue   # a mount point: left in place (the rmdir below then fails)
                _clear(fd, entry, here, pseudo)
            else:
                os.unlink(entry, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _read(fd: int, name: str) -> dict:
    try:
        raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(raw) as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return {}
            data = json.loads(f.read(65536))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _allocation(path: Path) -> bool:
    return (path.is_absolute() and path.parent.name == FOLDER and len(path.name) == 32
            and all(c in '0123456789abcdef' for c in path.name))


def _reserved(fd: int, path: Path) -> bool:
    from .runner import boot_id, proc_start
    try:
        os.stat('finished', dir_fd=fd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        pass
    owner = _read(fd, OWNER)
    if marker := owner.get('marker'):
        try:
            with directory(Path(marker).parent) as parent:
                m = _read(parent, Path(marker).name)
        except OSError:
            m = {}
        if m.get('check_run_dir') == str(path):
            # Queued pushes have no process/lock yet, including after a restart.
            # Running owners are protected by the inherited lifetime lock.
            if m.get('status') == 'queued':
                return True
            if not (m.get('status') == 'running' and not m.get('pid')):
                return False
    # Covers the interval before the launcher writes the marker, and foreground pushes.
    return bool(owner.get('pid') and owner.get('started') and owner.get('boot') == boot_id()
                and proc_start(owner['pid']) == owner['started'])


def cleanup(path: Path, *, whole=False, wait=False) -> None:
    """Claim an unreserved allocation under the folder lock (a rename to a trash name, waiting up
    to WAIT_S with `wait`), then remove it with that lock released."""
    if not whole or not _allocation(path):
        return
    try:
        with directory(path.parent) as parent:
            claimed = False
            # Serialize allocation publication and claims; never follow a renamed path.
            with _catalogue(parent, WAIT_S if wait else 0, path.parent / LOCK) as catalogue:
                if catalogue is None:
                    return
                with directory(path) as fd:
                    if not _read(fd, OWNER):
                        return
                    guard = _take_fd(fd)
                    if guard is None:
                        return
                    try:
                        if not _reserved(fd, path):
                            os.rename(path.name, TRASH + path.name, src_dir_fd=parent, dst_dir_fd=parent)
                            os.fsync(parent)
                            claimed = True
                    finally:
                        guard.close()
            if claimed:
                _discard(parent, path.parent, TRASH + path.name)
    except OSError:
        pass   # unsafe, busy or already gone; recovery must never follow it elsewhere


def _discard(parent: int, folder: Path, trash: str) -> None:
    """Remove a claimed allocation, its short temp dir and its empty report dir; no folder lock is
    held. Its own lock keeps a second remover (recovery) out until the rest is gone."""
    from . import shorttmp
    try:
        fd = os.open(trash, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        return
    try:
        guard = _take_fd(fd)
        if guard is None:
            return
        try:
            shorttmp.remove(_read(fd, OWNER).get('tmp'))
            _remove(parent, trash, last=LOCK)
        finally:
            guard.close()
    except OSError:
        return
    finally:
        os.close(fd)
    try:
        with directory(reports(folder / trash[len(TRASH):]).parent) as report_fd:
            os.rmdir(trash[len(TRASH):], dir_fd=report_fd)  # keep actual reports
    except OSError:
        pass


def recover(state: Path, runs: Path) -> None:
    try:
        with directory(state / FOLDER) as fd:
            names = os.listdir(fd)
        for name in names:
            if name.startswith(TRASH) and _allocation(state / FOLDER / name[len(TRASH):]):
                with directory(state / FOLDER) as fd:
                    _discard(fd, state / FOLDER, name)   # a removal a killed run left unfinished
            else:
                cleanup(state / FOLDER / name, whole=True)
    except OSError:
        pass

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Per-task archives of what a finished task's worktree held outside git, so the worktree can go.

state/archive/tasks/<id>/ holds manifest.json and the archived files: small ones in a compressed
tarball (files[.N].tar.gz, a new name per archiving), each over TAR_MAX_MB as it is under
objects/<sha256[:2]>/<sha256> (moved by a hard link on the same filesystem: it needs no space).
The manifest lists every file the sweep took out of the worktree: path (relative to the worktree),
size, sha256, whether a hand-off named it, and where it went ("tar" with `archive`, the tarball;
"file" with `blob`, its object; or "dropped": over the cap for unnamed files, listed only). It also
records the task's status, branch and head, so archived output is tied to the commit that made it,
and `patches`: binary patches of uncommitted tracked changes (tracked.patch, verified to apply to
that head; `git apply` restores them). state/archive/tasks/MANIFEST.md gets one line per archiving.

Archived files are write-once. Big files are content-addressed: identical content is stored once,
and different content never shares a path. Nothing is ever renamed or written onto an existing
archive path (_place); an object already there with other content raises Conflict and the worktree
is kept. Manifests written before this layout list big files under files/<path> with no `blob`;
they are still read (location). Known gap: such old-layout entries could be overwritten by a later
archiving of the same path, so one of two copies with different sha256 may already be lost.

store() is transactional. It first checks that what it writes fits in the free space of state/'s
filesystem (see room); if not, it archives nothing and raises NoRoom. It then writes the manifest
with the new entries marked `pending`, writes the tarball, patch and objects, checks every new entry
against what was stored (the object or tarball member is there with its sha256), and finalizes the
manifest; only then are files deleted from the worktree. A failure part way undoes what it wrote
(temporary files, the tarball, objects it added, the patch) and re-raises, so the worktree is kept
as it was. Known gap: a kill (power cut, watchdog) between storing and finalizing the manifest is not
undone at once; the next sweep's recover() finishes or rolls back the pending entries, so every
object is listed and resolve() finds it; files still in the worktree are archived again then
(identical objects are not stored twice)."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from . import gitmemo
from .project import Project, durable_append, fsync_dir, write_json

ARCHIVE_DIR = "archive/tasks"
MANIFEST = "manifest.json"
TAR_MAX_MB = 256   # a file over this is stored as it is under objects/ instead of in the tarball
RESERVE_FLOOR_MB = 1024   # free space an archive leaves while the disk is already below the guard (see room)
LOG_MAX_MB = 1     # ignored *.log files up to this size are archived with the rest (see worktree.sweep)


def task_dir(p: Project, task_id: int) -> Path:
    return p.state / ARCHIVE_DIR / str(int(task_id))


def manifest(p: Project, task_id: int) -> dict | None:
    """The task's archive manifest, or None when it has none."""
    try:
        data = json.loads((task_dir(p, task_id) / MANIFEST).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256(path: Path, progress=lambda: None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for n, block in enumerate(iter(lambda: fh.read(1 << 20), b""), 1):
            h.update(block)
            if n % 64 == 0:
                progress()
    return h.hexdigest()


COPY_CHUNK = 64 << 20


def copy(src: Path, dst: Path, progress=lambda: None) -> None:
    """shutil.copy2 in COPY_CHUNK pieces, calling progress() per piece: a huge file on a slow disk keeps
    the daemon's watchdogs told."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while chunk := fi.read(COPY_CHUNK):
            fo.write(chunk)
            progress()
    shutil.copystat(src, dst)


def files_of(root: Path, rels: list[str]) -> list[str]:
    """The regular files and links these entries (files or directories, relative to `root`) hold,
    relative to it, links not followed."""
    out: list[str] = []
    for rel in rels:
        full = root / rel
        if full.is_dir() and not full.is_symlink():
            out += sorted(f.relative_to(root).as_posix() for f in full.rglob("*")
                          if f.is_symlink() or f.is_file())
        elif full.is_symlink() or full.is_file():
            out.append(rel)
    return list(dict.fromkeys(out))


def tracked_files(root: Path) -> set[str]:
    """The files git tracks in the checkout `root`, relative to it (empty when it is no checkout).
    They are on the branch or in the tracked patch, so never archived or taken out of the worktree."""
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
                           timeout=gitmemo.timeout(300))
    except (OSError, subprocess.SubprocessError):
        return set()
    return {f for f in r.stdout.decode(errors="surrogateescape").split("\0") if f} if r.returncode == 0 else set()


def tracked_patch(root: Path) -> bytes:
    """A binary patch of the worktree's uncommitted tracked changes against HEAD, checked to apply to
    HEAD in a scratch index (`git apply --check --cached`). Raises when there is none or it does not
    apply, so the worktree is then kept."""
    def git(*args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True,   # diff, and apply --check
                              timeout=gitmemo.timeout(300, read=True), **kw)          # on a scratch index: reads
    diff = git("diff", "--binary", "HEAD")
    if diff.returncode != 0 or not diff.stdout.strip():
        raise RuntimeError("no patch of the tracked changes: " + (diff.stderr.decode(errors="replace")[:200]
                                                                  or "git diff found no change"))
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        (Path(tmp) / "changes.patch").write_bytes(diff.stdout)
        for args in (("read-tree", "HEAD"), ("apply", "--check", "--cached", str(Path(tmp) / "changes.patch"))):
            r = git(*args, env=env)
            if r.returncode != 0:
                raise RuntimeError(f"the patch of the tracked changes does not apply to HEAD: "
                                   f"{r.stderr.decode(errors='replace')[:200]}")
    return diff.stdout


class NoRoom(Exception):
    """An archive does not fit in the free space of state/'s filesystem; nothing was archived."""


class Conflict(RuntimeError):
    """What is stored at an archive path is not what the manifest says belongs there; nothing is
    overwritten and the worktree is kept."""


def blob(digest: str) -> str:
    """Where a big file with this sha256 is stored, relative to the task's archive."""
    return f"objects/{digest[:2]}/{digest}"


def location(d: Path, e: dict) -> Path | None:
    """The stored file of a "file" entry: its object, or files/<path> for one written before objects/."""
    if e.get("stored") != "file":
        return None
    return d / str(e["blob"]) if e.get("blob") else d / "files" / str(e["path"])


def where(p: Project, task_id: int, e: dict) -> str:
    """Where this manifest entry's file is stored: its object or old-layout file, `<tarball>:<member>`,
    or "" (dropped)."""
    d = task_dir(p, task_id)
    if e.get("stored") == "tar":
        return f"{d / str(e.get('archive'))}:{e['path']}"
    return str(location(d, e) or "")


def _place(src: Path, dst: Path) -> bool:
    """Put `src` at `dst`, never replacing anything there: a hard link, which fails if `dst` exists
    (True: `src` stays until its caller deletes it). A filesystem without hard links gets a rename
    after checking `dst` is free (False: `src` is gone); prune.lock keeps other sweeps out meanwhile."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "an archived file is never overwritten", str(dst))
    try:
        os.link(src, dst, follow_symlinks=False)
        return True
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "an archived file is never overwritten", str(dst))
        os.rename(src, dst)
        return False


def _write_new(path: Path, data: bytes) -> None:
    """Write a new archive file: synced under a temporary name, then placed (never over another)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _place(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(path.parent)


def _check_blob(f: Path, e: dict, progress=lambda: None) -> None:
    """Raise Conflict unless `f` is a regular file with the entry's size and sha256."""
    if f.is_symlink() or not f.is_file() or f.stat().st_size != e.get("size") or sha256(f, progress) != e.get("sha256"):
        # The outcome first: callers cut the reason, and a state path can be long (macOS temp dirs).
        raise Conflict(f"nothing overwritten, the worktree is kept: {f} does not hold {e.get('path')} "
                       f"(sha256 {str(e.get('sha256'))[:12]})")


def _check_tar(f: Path, entries: list[dict], progress=lambda: None) -> None:
    """Raise Conflict unless the tarball holds every entry: a link as a link, a file with its sha256."""
    with tarfile.open(f, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        for e in entries:
            progress()
            m = members.get(e["path"])
            ok = m is not None and (m.issym() if not e.get("sha256") else m.isfile() and m.size == e.get("size"))
            if ok and e.get("sha256"):
                h = hashlib.sha256()
                with tar.extractfile(m) as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
                ok = h.hexdigest() == e["sha256"]
            if not ok:
                raise Conflict(f"nothing overwritten, the worktree is kept: {f} does not hold {e['path']} as listed")


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def need_bytes(root: Path, dest: Path, small: list[str], big: list[str], sizes: dict, patch: bytes | None) -> int:
    """Bytes an archive writes on state/'s filesystem: the tarball's inputs (with header room), the
    patch, the manifest, and the big files unless a rename moves them there (the same filesystem)."""
    out = sum(sizes[f] + 1024 for f in small) + len(patch or b"") + 64 * 1024 + 512 * (len(small) + len(big))
    if big and not _same_fs(root, dest):
        out += sum(sizes[f] for f in big)
    return out


def room(dest: Path, need: int, reserve: float) -> str | None:
    """Why `need` bytes do not fit under `dest` leaving `reserve` (the disk guard's threshold) free, or
    None when they fit. While free space is already below `reserve` (the disk guard's own sweep, which
    frees far more than it archives), RESERVE_FLOOR_MB must stay free instead."""
    try:
        free = shutil.disk_usage(dest).free
    except OSError as e:
        return f"archiving skipped: cannot read the free space under {dest} ({e}); the worktree is kept"
    keep = reserve if free >= reserve else min(reserve, RESERVE_FLOOR_MB * 1e6)
    if need <= free - keep:
        return None
    return (f"archiving skipped: it needs {need / 1e6:.1f} MB under {dest}, {free / 1e6:.1f} MB are free and "
            f"{keep / 1e6:.1f} MB must stay free; nothing archived, the worktree is kept")


def recover(p: Project, task_id: int) -> int:
    """Finish or roll back an archiving cut off part way (a kill between writing and finalizing):
    a pending entry whose data is in place (the tarball it names, its object or old-layout file at its
    size, a patch with its sha256) is finalized, the rest are dropped (an archived file is never
    deleted: objects are written whole, by link or rename), and temporary and partial files are
    removed. Returns how many pending entries it found. Run under prune.lock."""
    m = manifest(p, task_id)
    dest = task_dir(p, task_id)
    for tmp in ([*dest.glob("*.tmp"), *dest.glob(".*.tmp"), *dest.glob("objects/*/.*.part")]
                if dest.is_dir() else []):
        tmp.unlink(missing_ok=True)
    if not m:
        return 0
    files = [e for e in m.get("files") or [] if isinstance(e, dict)]
    patches = [e for e in m.get("patches") or [] if isinstance(e, dict)]
    pending = [e for e in files + patches if e.get("pending")]
    if not pending:
        return 0

    def present(e: dict) -> bool:
        if "file" in e and "paths" in e:   # a patch
            f = dest / str(e["file"])
            return f.is_file() and f.stat().st_size == e.get("size") and sha256(f) == e.get("sha256")
        if e.get("stored") == "tar":
            return (dest / str(e.get("archive"))).is_file()
        if e.get("stored") == "file":
            f = location(dest, e)
            if not e.get("blob"):
                f.with_name(f.name + ".part").unlink(missing_ok=True)
            return f.is_symlink() or (f.is_file() and f.stat().st_size == e.get("size"))
        return False   # "dropped": the files were never deleted before finalizing
    kept_files = [{k: v for k, v in e.items() if k != "pending"} for e in files
                  if not e.get("pending") or present(e)]
    kept_patches = [{k: v for k, v in e.items() if k != "pending"} for e in patches
                    if not e.get("pending") or present(e)]
    for e in patches:
        if e.get("pending") and not present(e):
            (dest / str(e["file"])).unlink(missing_ok=True)
    write_json(dest / MANIFEST, {**m, "updated": time.time(), "files": kept_files, "patches": kept_patches})
    return len(pending)


def store(p: Project, task: dict, root: Path, named: list[str], others: list[str], cap_mb: float,
          head: str = "", patch: bytes | None = None, tracked: list[str] = (), reserve_bytes: float = 0,
          progress=lambda: None) -> dict:
    """Archive files of the worktree `root` before it is removed. `named` (entries a hand-off lists)
    are always kept. `others` (untracked files no hand-off names) are listed with size and sha256,
    and kept smallest first while they total at most `cap_mb`; the rest are dropped (listed only).
    Transactional (see the module docstring): raises NoRoom, writing nothing, when the archive does
    not fit with `reserve_bytes` left free (see room); the manifest is written with the new entries
    pending before anything leaves the worktree, and finalized once all is written; a file is deleted
    from the worktree only after that; a failure undoes what it wrote and re-raises. An earlier
    archiving cut off part way is recovered first (recover); archiving again adds to the manifest.
    `patch` (tracked_patch, for the `tracked` paths) is saved as tracked[.N].patch. `progress()` is
    called per file (and per 64 MB hashed) so a daemon's watchdog sees it move.
    Returns {"dir", "files", "bytes" (archived), "dropped", "dropped_bytes"}."""
    dest = task_dir(p, task["id"])
    dest.mkdir(parents=True, exist_ok=True)
    recover(p, task["id"])
    old = manifest(p, task["id"]) or {}
    entries = [e for e in old.get("files") or [] if isinstance(e, dict)]
    patches = [e for e in old.get("patches") or [] if isinstance(e, dict)]
    named_files, keep, dropped, sizes = _split(root, named, others, cap_mb)
    big = [f for f in keep if sizes[f] > TAR_MAX_MB * 1e6 and not (root / f).is_symlink()]
    big_set = set(big)
    small = [f for f in keep if f not in big_set]
    why = room(dest, need_bytes(root, dest, small, big, sizes, patch), reserve_bytes)
    if why:
        raise NoRoom(why)
    tar_name = patch_name = ""
    if small:
        n = 0
        while (dest / (tar_name := "files.tar.gz" if not n else f"files.{n}.tar.gz")).exists():
            n += 1
    if patch:
        n = 0
        while (dest / (patch_name := "tracked.patch" if not n else f"tracked.{n}.patch")).exists():
            n += 1
    new = _entries(root, keep + dropped, sizes, set(named_files), set(dropped), big_set, tar_name, progress)
    new_patches = [{"file": patch_name, "size": len(patch), "sha256": hashlib.sha256(patch).hexdigest(),
                    "paths": list(tracked), "at": time.time()}] if patch else []
    info = {"task": task["id"], "status": task.get("status") or "", "branch": task.get("branch") or "",
            "head": head or old.get("head") or "", "worktree": str(root)}
    write_json(dest / MANIFEST, {**info, "updated": time.time(), "files": entries + [{**e, "pending": True} for e in new],
                                  "patches": patches + [{**e, "pending": True} for e in new_patches]})
    made: list[Path] = []          # files this call wrote under dest, removed on failure
    moved: list[tuple[Path, Path]] = []   # big files moved out of the worktree into objects/, moved back on failure
    release: list[str] = []        # big files copied or already stored: deleted from the worktree after
    done = False
    try:
        if patch:
            _write_new(dest / patch_name, patch)
            made.append(dest / patch_name)
        if small:
            tmp = dest / (tar_name + ".tmp")
            made.append(tmp)
            with tarfile.open(tmp, "w:gz", compresslevel=1) as tar:
                for f in small:
                    progress()
                    tar.inodes = {}   # a hard-linked file is stored in full, never as a link member
                    tar.add(str(root / f), arcname=f, recursive=False)
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            _place(tmp, dest / tar_name)
            made.append(dest / tar_name)
            tmp.unlink(missing_ok=True)
            fsync_dir(dest)
        for e in new:
            if e["stored"] != "file":
                continue
            progress()
            f, to = e["path"], dest / e["blob"]
            to.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(to):
                _check_blob(to, e, progress)   # the same content is stored already; other content raises
                release.append(f)
            elif _same_fs(root / f, to.parent):
                if _place(root / f, to):
                    (root / f).unlink()   # moved, so the worktree's file and the object never share an inode
                moved.append((to, root / f))
            else:
                part = to.with_name(f".{to.name}.{os.getpid()}.part")
                made.append(part)
                copy(root / f, part, progress)
                with open(part, "rb") as fh:
                    os.fsync(fh.fileno())
                _place(part, to)
                made.append(to)
                part.unlink(missing_ok=True)
                release.append(f)
            fsync_dir(to.parent)
        # Every new entry is checked against what was stored before the worktree loses anything.
        for e in new:
            if e["stored"] == "file":
                _check_blob(dest / e["blob"], e, progress)
        if small:
            _check_tar(dest / tar_name, [e for e in new if e["stored"] == "tar"], progress)
        write_json(dest / MANIFEST, {**info, "updated": time.time(), "files": entries + new,
                                      "patches": patches + new_patches})
        done = True
    finally:
        if not done:
            for to, back in reversed(moved):
                try:
                    if _place(to, back):
                        to.unlink()
                except OSError:
                    pass   # left pending: recover() finalizes it, so the file stays listed and findable
            for f in made:
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                recover(p, task["id"])
            except Exception:
                pass   # the next sweep recovers it
    line = (f"- {time.strftime('%Y-%m-%d %H:%M', time.localtime())} task {task['id']} ({task.get('status') or '?'}), "
            f"branch {task.get('branch') or '-'}, head {(head or '-')[:12]}: {len(keep)} file(s) "
            f"({sum(sizes[f] for f in keep) / 1e6:.1f} MB) archived, {len(dropped)} listed only"
            + (f", a patch of {len(tracked)} tracked file(s)" if patch else "") + "\n")
    durable_append(p.state / ARCHIVE_DIR / "MANIFEST.md", line)
    for f in small + dropped + release:
        try:
            (root / f).unlink()
        except FileNotFoundError:
            pass
    return {"dir": str(dest), "files": len(keep), "bytes": sum(sizes[f] for f in keep),
            "dropped": len(dropped), "dropped_bytes": sum(sizes[f] for f in dropped),
            **({"patch": str(dest / patch_name)} if patch else {})}


def _entries(root: Path, files: list[str], sizes: dict, named: set, dropped: set, big: set, tar_name: str,
             progress=lambda: None) -> list[dict]:
    """A manifest entry per file, hashed. Membership is looked up in sets: a hand-off may name a
    directory of hundreds of thousands of files, and a list scan per file made that hours of CPU."""
    new = []
    for f in files:
        full = root / f
        progress()
        digest = "" if full.is_symlink() else sha256(full, progress)
        stored = "dropped" if f in dropped else "file" if f in big else "tar"
        new.append({"path": f, "size": sizes[f], "sha256": digest, "named": f in named, "stored": stored,
                    **({"archive": tar_name} if stored == "tar" else {}),
                    **({"blob": blob(digest)} if stored == "file" else {}), "at": time.time()})
    return new


def _split(root: Path, named: list[str], others: list[str], cap_mb: float):
    """(the named files, the files to archive, the files to drop, size of each); see store. A file git
    tracks is left out: archiving would delete it from the worktree, which then never looks clean."""
    tracked = tracked_files(root)
    named_files = [f for f in files_of(root, named) if f not in tracked]
    seen = set(named_files) | tracked
    other_files = [f for f in files_of(root, others) if f not in seen]
    sizes = {f: (root / f).lstat().st_size for f in named_files + other_files}
    keep, left, dropped = list(named_files), cap_mb * 1e6, []
    for f in sorted(other_files, key=lambda f: (sizes[f], f)):
        if sizes[f] <= left:
            keep.append(f)
            left -= sizes[f]
        else:
            dropped.append(f)
    return named_files, keep, dropped, sizes


def plan(root: Path, named: list[str], others: list[str], cap_mb: float, patch: bytes | None = None) -> dict:
    """What store() would archive and drop, changing nothing (for a dry run)."""
    _, keep, dropped, sizes = _split(root, named, others, cap_mb)
    return {"files": len(keep), "bytes": sum(sizes[f] for f in keep), "dropped": len(dropped),
            "dropped_bytes": sum(sizes[f] for f in dropped), **({"patch": "(dry run)"} if patch else {})}


def resolve(p: Project, entry: str, task_id: int | None = None) -> dict | None:
    """Where an archived artifact is now: `entry` is a path inside a removed task worktree (absolute,
    or relative to it when `task_id` is given). Returns its manifest entry with `location` (its object
    under objects/, an old-layout file under files/, or `<tarball>:<member>`), or None when no archive holds it."""
    raw = Path(entry.strip())
    if raw.is_absolute():
        try:
            rel = raw.relative_to(p.worktrees)
        except ValueError:
            return None
        head, *rest = rel.parts
        if not head.startswith("t") or not head[1:].isdigit() or not rest:
            return None
        task_id, path = int(head[1:]), Path(*rest).as_posix()
    elif task_id is None:
        return None
    else:
        path = raw.as_posix()
    m = manifest(p, task_id)
    if not m:
        return None
    for e in reversed(m.get("files") or []):
        if e.get("path") == path or str(e.get("path", "")).startswith(path.rstrip("/") + "/"):
            return {**e, "task": task_id, "location": where(p, task_id, e)}
    return None


def restore(p: Project, task_id: int, dest: Path) -> list[str]:
    """Extract every archived file of the task under `dest` (relative paths kept). Returns the paths."""
    m = manifest(p, task_id) or {}
    d = task_dir(p, task_id)
    out = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in sorted({e["archive"] for e in m.get("files") or [] if e.get("stored") == "tar"}):
        with tarfile.open(d / name, "r:gz") as tar:
            members = [x for x in tar.getmembers() if (x.isfile() or x.issym()) and not x.name.startswith("/")
                       and ".." not in Path(x.name).parts]
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(dest, members=members, **safe)
            out += [x.name for x in members]
    for e in m.get("files") or []:
        src = location(d, e)
        if src is not None and src.exists():
            to = dest / e["path"]
            to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, to)
            out.append(e["path"])
    return out

"""Run-start checks of what a reboot or a cleanup can take away, before a task's workspace is made:

- the task's worktree, when its `.git` file names a git dir that is gone (dangling). When the
  repository it belonged to is gone too (a clone left empty or removed), the commits it never
  pushed went with it: its files move aside and the task is blocked (worktree.WorkspaceStuck), never
  continued on a fresh worktree as if nothing happened. So is one whose repository is there but no
  longer has the task's recorded branch (cloned again). When only git's record of it is gone, its
  commits are kept in the repository: its files move aside and the worktree is made again.
- the clone code worktrees are made from (the project root): one that was a git repository and
  now has no .git directory blocks the task; one on a file system a reboot empties (tmpfs, ramfs)
  is reported, as is a worktrees folder there.
- ~/.ssh/known_hosts, missing or empty while this project uses ssh (machines are listed, or the
  file was seen before): ssh to verified hosts then fails host key checks.

Each finding is a model-free observation for the coordinator (one per condition a day), a line in
the worker's prompt, the run's note (`start_checks`) and the task's stored hand-off. Reads files
only, apart from moving a dangling worktree aside."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import worktree
from .project import Project

KV_BASE = "startcheck_base"            # {"root": path}: the root was a git repository when last checked
KV_KNOWN_HOSTS = "startcheck_known_hosts"  # {"path": ..., "seen": ts}: known_hosts held entries once
EVENT = "start_check"
REMADE = "was dangling"   # the finding of a worktree made again: a session that worked in the old one is not resumed
VOLATILE_FS = {"tmpfs", "ramfs"}
REPEAT_S = 86400
# Set where a temporary root is expected (the test suite runs on tmpfs): no volatile-file-system finding.
ALLOW_VOLATILE_ENV = "TTP_ALLOW_VOLATILE_BASE"


def dangling(path: Path) -> dict | None:
    """{"gitdir", "repo", "repo_gone"} when `path`'s `.git` file names a git dir that is gone, else
    None (no `.git` file, a `.git` directory, or a git dir that is there). `repo_gone`: the
    repository it belongs to (<repo>/.git/worktrees/<name>) has no HEAD or objects either."""
    try:
        text = (path / ".git").read_text()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gd = Path(text[len("gitdir:"):].strip())
    gd = gd if gd.is_absolute() else path / gd
    if (gd / "HEAD").is_file():
        return None
    common = gd.parent.parent
    try:
        common = (gd / (gd / "commondir").read_text().strip())
    except OSError:
        pass
    gone = not ((common / "HEAD").is_file() and (common / "objects").is_dir())
    return {"gitdir": str(gd), "repo": str(common.parent if common.name == ".git" else common), "repo_gone": gone}


def volatile(path: Path, mounts: str = "/proc/mounts") -> str | None:
    """The type of the file system `path` is on when a reboot empties it (VOLATILE_FS, read from
    `mounts`; on macOS a folder under /private/tmp or /private/var/folders), else None, also when it
    cannot tell."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    if sys.platform == "darwin":
        return "temporary folder" if real.startswith(("/private/tmp/", "/private/var/folders/")) else None
    best, kind = "", None
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, kind = mnt, parts[2]
    return kind if kind in VOLATILE_FS else None


def known_hosts_problem(home: Path | None = None) -> str | None:
    """Why ~/.ssh/known_hosts cannot verify a host ("missing" or "empty"), or None."""
    f = (home or Path(os.environ.get("HOME") or Path.home())) / ".ssh" / "known_hosts"
    try:
        return "empty" if f.stat().st_size == 0 else None
    except OSError:
        return "missing"


def _uses_ssh(p: Project) -> bool:
    try:
        from . import machines
        return bool(machines.load())
    except Exception:
        return False


def _observe(p: Project, fp: str, severity: str, text: str, task: dict, now: float) -> None:
    """One queued observation per condition (`fp`) per REPEAT_S, and the task's own record of it."""
    db = p.db
    with db.tx():
        if not db.one("SELECT 1 FROM events WHERE kind='observation' AND fingerprint=? AND ts>?", (fp, now - REPEAT_S)):
            db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
                 (now, "daemon", "observation", fp, severity, text[:4000], "queued", task["id"]))
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "daemon", EVENT, "low", text[:500], json.dumps({"fingerprint": fp}), "handled", task["id"]))


def _move_aside(path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = next(d for n in range(1000) if not (d := path.parent / f"{worktree.STALE_PREFIX}{stamp}"
                                               f"{f'-{n}' if n else ''}" / path.name).exists())
    dest.parent.mkdir(parents=True, exist_ok=True)
    path.rename(dest)
    return dest


def _has_branch(repo: Path, ref: str) -> bool:
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check(p: Project, task: dict, now: float | None = None) -> list[str]:
    """Run the checks for `task`, which is about to start; returns the findings it may start with.
    Raises worktree.WorkspaceStuck (the daemon blocks the task) for a dangling worktree whose
    repository or recorded branch is gone, and for a root that was a git repository and has no .git directory now,
    when the task would work in a worktree of it."""
    now = time.time() if now is None else now
    found: list[str] = []
    if task["kind"] == "harness":
        return found
    path = p.worktrees / f"t{task['id']}"
    d = dangling(path) if path.is_dir() else None
    if d:
        dest = _move_aside(path)
        if d["repo_gone"]:
            text = (f"Task #{task['id']}'s worktree {path} is dangling: its git dir {d['gitdir']} is gone, and so is "
                    f"its repository {d['repo']} (missing or emptied, e.g. by a reboot). Commits it had not pushed "
                    f"are lost unless that repository is restored. Its files moved to {dest}. The task is blocked: "
                    f"restore {d['repo']} and run `git worktree repair` in {dest}, or requeue the task to start "
                    f"afresh.")
            _observe(p, f"dangling:{task['id']}:{d['gitdir']}", "high", text, task, now)
            raise worktree.WorkspaceStuck(f"worktree dangling: its repository {d['repo']} is gone, with any commits "
                                          f"not pushed; files moved to {dest}; restore it or requeue to start afresh")
        branch = task.get("branch") or ""
        if branch and not _has_branch(Path(d["repo"]), f"refs/heads/{branch}"):
            # The repository was emptied and cloned again: it is there, but the task's branch is not.
            on_origin = _has_branch(Path(d["repo"]), f"refs/remotes/origin/{branch}")
            lost = (f"only origin/{branch} has it, so its commits not pushed may be lost" if on_origin
                    else "its commits not pushed are lost")
            text = (f"Task #{task['id']}'s worktree {path} is dangling: its git dir {d['gitdir']} is gone, and its "
                    f"repository {d['repo']} no longer has the task's branch {branch} (cloned again?): {lost} unless "
                    f"the branch is restored. Its files moved to {dest}. The task is blocked: restore {branch} in "
                    f"{d['repo']} and requeue it, or requeue it to start afresh.")
            _observe(p, f"dangling:{task['id']}:{d['gitdir']}", "high", text, task, now)
            raise worktree.WorkspaceStuck(f"worktree dangling: branch {branch} is gone from {d['repo']} ({lost}); "
                                          f"files moved to {dest}; restore the branch or requeue to start afresh")
        text = (f"Task #{task['id']}'s worktree {path} {REMADE}: its git dir {d['gitdir']} was gone, but its "
                f"repository {d['repo']} is there, so the branch's commits are kept. Its files moved to {dest} "
                f"and the worktree is made again; uncommitted changes are only in {dest}.")
        _observe(p, f"dangling:{task['id']}:{d['gitdir']}", "normal", text, task, now)
        found.append(text)
    makes = worktree.gets_worktree(task["kind"]) or bool(worktree.carried_branch(task))
    if makes:
        found += _check_base(p, task, now)
    found += _check_known_hosts(p, task, now)
    return found


def _check_base(p: Project, task: dict, now: float) -> list[str]:
    db, root = p.db, p.root
    seen = db.kv(KV_BASE) or {}
    if (root / ".git").exists():
        if seen.get("root") != str(root):
            db.set_kv(KV_BASE, {"root": str(root)})
    elif seen.get("root") == str(root):
        try:
            empty = not any(c.name != p.base.name for c in root.iterdir())
        except OSError:
            empty = True
        text = (f"The repository code worktrees are made from, {root}, has no .git directory any more "
                f"({'empty' if empty else 'other files are there'}: removed, emptied or not mounted, e.g. after a "
                f"reboot). Task #{task['id']} is blocked: clone it again or restore its .git, then requeue the "
                f"task. Nothing was changed.")
        _observe(p, f"base-gone:{root}", "high", text, task, now)
        raise worktree.WorkspaceStuck(f"the repository worktrees are made from, {root}, has no .git directory; "
                                      f"restore or clone it again, then requeue")
    else:
        return []   # never a git repository: tasks work in the root, as before
    out = []
    if (why := worktree.dangling_root(p)):
        text = f"Task #{task['id']} cannot get a worktree: {why}."
        _observe(p, f"dangling-root:{p.worktrees}", "high", text, task, now)
        out.append(text)
    if os.environ.get(ALLOW_VOLATILE_ENV, "").strip() not in ("", "0"):
        return out
    for what, where in (("repository code worktrees are made from", root), ("worktrees folder", p.worktrees)):
        kind = volatile(where)
        if kind:
            text = (f"The {what}, {where}, is on a file system a reboot empties ({kind}): worktrees made there lose "
                    f"every commit not pushed at the next reboot. Move the project to a disk that survives reboots.")
            _observe(p, f"volatile:{where}", "normal", text, task, now)
            out.append(text)
    return out


def _check_known_hosts(p: Project, task: dict, now: float) -> list[str]:
    db = p.db
    home = Path(os.environ.get("HOME") or Path.home())
    f = home / ".ssh" / "known_hosts"
    why = known_hosts_problem(home)
    seen = db.kv(KV_KNOWN_HOSTS) or {}
    if not why:
        if seen.get("path") != str(f):
            db.set_kv(KV_KNOWN_HOSTS, {"path": str(f), "seen": now})
        return []
    if seen.get("path") != str(f) and not _uses_ssh(p):
        return []
    text = (f"{f} is {why}: ssh to a host whose key was verified fails host key checking (or asks to trust "
            f"an unverified key). Restore it from a backup or verify each host's key again before ssh work; "
            f"never accept an unverified key blindly.")
    _observe(p, f"known_hosts:{f}:{why}", "high", text, task, now)
    return [text]


def remade(found: list[str]) -> bool:
    """Whether the task's worktree was made again (a dangling one moved aside)."""
    return any(REMADE in f for f in found)


def prompt_lines(found: list[str]) -> str:
    """The worker's start note for the findings: each one, and that its hand-off reports it."""
    if not found:
        return ""
    return ("\n\nRun-start checks found (report each in your hand-off's summary):\n"
            + "".join(f"- {f}\n" for f in found))


def of_run(note: str | None) -> list[str]:
    """The findings a run started with, from its note."""
    try:
        return list((json.loads(note or "{}") or {}).get("start_checks") or [])
    except (ValueError, TypeError, AttributeError):
        return []

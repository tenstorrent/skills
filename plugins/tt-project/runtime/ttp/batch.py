# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The push queue's batch process, `ttp push --batch <marker>`. It calls no model.

The daemon writes a marker naming the reviewed changes to push (entries: a branch and its reviewed
head), takes the batch's run lock and starts this process, which inherits the lock. The process
replays the entries in order onto the target's tip in a worktree of its own (`worktrees/push`) and
settles the conflicts that need no judgment: version lines, and both sides adding different lines at
one spot. It bumps the version once for all entries, runs the push checks once on the result and
pushes it without force. It always runs them itself and never reads the passes `ttp checks` records. When the checks fail, a binary search over prefixes finds the first failing
entry, and the passing prefix is pushed. The outcome goes into the marker per entry; the daemon
applies it to the reviews.

After the push the process lets go of the push lock and of its `push:run-<id>` lock (automatic
upgrades wait only for that one). It then runs `delivery.after_push` (a deploy) at the pushed commit,
under a lock of its own, `after_push:run-<id>`. A marker that has an outcome but whose after_push did
not finish is resumed: only the after_push step runs."""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from . import locks, push
from .project import Project, durable_write, write_json

WORKTREE = "push"             # the batch's own checkout under the project's worktrees (never t<id>)
AFTER_PUSH = "after_push-"    # + batch id: the temporary checkout after_push runs in
DEFAULT_AFTER_PUSH_TIMEOUT_S = 1800
STALE = "(stale plugin version)"   # the `cmd` of a check failure that is push.stale_versions
REPLAYED = "Ttp-Replayed-From"     # trailer of a commit the batch settled: the entry commit it replays
TOP_DEF = re.compile(r"^(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_]\w*)", re.M)
TOP_START = re.compile(r"(?:@|(?:async[ \t]+)?def[ \t]|class[ \t])")
EXIT = {"pushed": 0, "landed": 0, "nothing": 0, "busy": push.BUSY, "moved": push.KEPT_MOVING,
        "rejected": push.REJECTED, "refused": push.REFUSED, "tip_failed": push.CHECKS_FAILED}


def say(msg: str) -> None:
    try:
        print(f"ttp push: {msg}", flush=True)
    except (OSError, ValueError):   # the log went away; the marker still gets the outcome
        pass


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """git with its output captured. No editor opens (a rebase continues with the message it has),
    and no recorded resolution (rerere) settles a conflict behind the resolver's back."""
    return subprocess.run(["git", "-C", str(cwd), "-c", "rerere.enabled=false", *args], text=True,
                          capture_output=True, env={**os.environ, "GIT_EDITOR": "true"})


def _last(text: str, n: int = push.LOG_TAIL) -> str:
    return "\n".join((text or "").strip().splitlines()[-n:])


def _safe(bid: str) -> str:
    return bid if re.fullmatch(r"[A-Za-z0-9._-]{1,120}", bid) else push._slug(bid)


# Locks ----------------------------------------------------------------------------------------------

class _Fd:
    """A lock inherited from the launcher as a bare descriptor; closing it lets go."""

    def __init__(self, fd: int):
        self.fd = fd

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


LOCK_FD_ENV = "TTP_BATCH_LOCK_FD"


def _inherited(path: Path) -> int | None:
    """The descriptor the launcher passed down on `path` (named in TTP_BATCH_LOCK_FD), or None. Only
    that descriptor counts: another one open on the same file (e.g. a caller's own lock when this runs
    in-process) is never taken or closed."""
    try:
        fd = int(os.environ.get(LOCK_FD_ENV, ""))
        st, fst = path.stat(), os.fstat(fd)
    except (OSError, ValueError):
        return None
    return fd if (fst.st_dev, fst.st_ino) == (st.st_dev, st.st_ino) else None


def _hold(path: Path, holder: str):
    """`path`'s lock, held: the one the launcher passed down, else taken now. None when another
    process holds it."""
    fd = _inherited(path)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # a no-op when the launcher locked it already
            return _Fd(fd)
        except OSError:
            return None
    return locks.try_take([path], holder, "ttp push --batch")


def _forget(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _after_lock(p: Project, bid: str) -> Path:
    """The lock of a batch's after_push. Its name does not start with `push:`, so automatic upgrades
    (release.push_in_flight) and `ttp push --free` do not wait for a deploy."""
    return p.state / "locks" / f"after_push:run-{_safe(bid)}.0.lock"


# Commands and worktrees ------------------------------------------------------------------------------

def _kill(proc: subprocess.Popen) -> None:
    """End `proc`'s process group: TERM, a short grace, then KILL whatever is left."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _stream(cmd: str, cwd: Path, env: dict | None = None, timeout: float | None = None,
            group: bool = False) -> tuple[int | None, str, bool]:
    """Run shell `cmd`, copying its output into this process's output (the batch log): (exit code,
    the last lines of its output, whether it ran out of time and was killed). `group` starts it in a
    process group of its own, which a timeout kills whole."""
    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=group)
    tail: deque[str] = deque(maxlen=push.LOG_TAIL)

    def pump() -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace")
            tail.append(line.rstrip("\n"))
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
    reader.join(timeout=10)      # a child that left the group may keep the pipe open; do not wait on it
    return proc.returncode, "\n".join(tail), timed_out


def _registered(repo: Path, wt: Path) -> bool:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return any(line == f"worktree {wt}" or (line.startswith("worktree ") and Path(line[9:]).resolve() == wt.resolve())
               for line in out.splitlines())


def _remove_worktree(repo: Path, wt: Path) -> None:
    if wt.exists() and _git(repo, "worktree", "remove", "--force", "--force", str(wt)).returncode != 0:
        shutil.rmtree(wt, ignore_errors=True)
    _git(repo, "worktree", "prune")


def _rebasing(wt: Path) -> bool:
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        path = _git(wt, "rev-parse", "--git-path", name).stdout.strip()
        if path and (Path(path) if os.path.isabs(path) else wt / path).exists():
            return True
    return False


def _sweep_after_push(p: Project, repo: Path) -> None:
    """Remove the after_push checkouts that a killed deploy left behind (their lock is free)."""
    try:
        dirs = sorted(p.worktrees.glob(f"{AFTER_PUSH}*"))
    except OSError:
        return
    for wt in dirs:
        if locks.any_free([_after_lock(p, wt.name[len(AFTER_PUSH):])]):
            _remove_worktree(repo, wt)


# Conflicts that need no judgment ---------------------------------------------------------------------

def merge3(ours: str, base: str, theirs: str, py: bool = False) -> str | None:
    """git's three-way merge of the texts, where each conflict in which both sides only added lines
    at one spot (an empty base section) keeps both: ours first, then theirs (in Python, see _seam).
    None when any other conflict remains, or when keeping both could be wrong (_clash). The conflict
    markers carry a random tag, so file content never passes for one; a hunk whose base-to-end part
    holds more than one separator line is not read as an addition."""
    tag = secrets.token_hex(8)
    # built, not spelled out: a literal marker line would make this file read as a conflicted one
    start, mid, end = (f"{c * 7} {side}-{tag}" for c, side in (("<", "ours"), ("|", "base"), (">", "theirs")))
    sep = "=" * 7
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(d) / name
            path.write_bytes(text.encode())
            paths.append(str(path))
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", f"ours-{tag}", "-L", f"base-{tag}",
                            "-L", f"theirs-{tag}", *paths], capture_output=True)
    if not 0 <= r.returncode < 127:     # an error, or too many conflicts to count
        return None
    out = r.stdout.decode(errors="replace")
    if r.returncode == 0:
        return out
    lines, res, i = out.splitlines(keepends=True), [], 0
    while i < len(lines):
        if lines[i].rstrip("\r\n") != start:
            res.append(lines[i])
            i += 1
            continue
        j, mine = i + 1, []
        while j < len(lines) and lines[j].rstrip("\r\n") != mid:
            mine.append(lines[j])
            j += 1
        if j + 1 >= len(lines) or lines[j + 1].rstrip("\r\n") != sep:
            return None                 # the base section is not empty: a real conflict
        j, theirs_lines = j + 2, []
        while j < len(lines) and lines[j].rstrip("\r\n") != end:
            if lines[j].rstrip("\r\n") == sep:
                return None             # ambiguous: content that looks like the separator
            theirs_lines.append(lines[j])
            j += 1
        if j >= len(lines) or _clash(mine, theirs_lines, py):
            return None
        res += _seam(mine, theirs_lines) if py else mine + theirs_lines
        i = j + 1
    return "".join(res)


def _clash(mine: list[str], theirs: list[str], py: bool) -> bool:
    """Whether keeping both added sections, ours first, could be wrong without anyone seeing it: they
    share a line (_said), so the result would hold it twice (two changes adding one import, or one
    change replayed onto a tip that holds it already); or theirs opens indented, inside the block it
    was written for, while ours holds a line indented less, which ends that block before theirs."""
    if _said(mine, py) & _said(theirs, py):
        return True
    first = next((line for line in theirs if line.strip()), None)
    depth = _indent(first) if first else 0
    return depth > 0 and any(_indent(line) < depth for line in mine if line.strip())


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _said(lines: list[str], py: bool) -> set[str]:
    """The lines of one side's addition that the other's must not repeat, stripped: those holding a
    letter or digit (brackets, punctuation or quotes alone repeat nothing). In Python, the decorators
    and body of a top-level def or class that the addition opens are left out: the block is new as a
    whole and settle refuses a repeated name, so `p = make(env)` in two new tests repeats nothing.
    The def or class line itself counts."""
    out: set[str] = set()
    decorators: list[str] = []          # at the margin, until a def or class claims them
    inside = False                      # in the body of a def or class opened here
    for line in lines:
        s = line.strip()
        if not any(c.isalnum() for c in s):
            continue
        if not py:
            out.add(s)
        elif line[0] in " \t":
            if decorators:
                decorators.append(s)
            elif not inside:
                out.add(s)
        elif s.startswith("@"):
            decorators.append(s)
            inside = False
        else:
            inside = bool(TOP_DEF.match(line))
            if not inside:
                out.update(decorators)
            decorators = []
            out.add(s)
    out.update(decorators)
    return out


def _seam(mine: list[str], theirs: list[str]) -> list[str]:
    """Both sides' added Python lines, ours first. Where theirs opens a top-level def, class or
    decorator after code of ours, the two meet with PEP 8's two blank lines: each side spaced its
    block only against the old text (past conflicts left none and three there)."""
    m = list(mine)
    while m and not m[-1].strip():
        m.pop()
    k = next((k for k, line in enumerate(theirs) if line.strip()), None)
    if not m or k is None or not TOP_START.match(theirs[k]):
        return mine + theirs
    nl = "\r\n" if m[-1].endswith("\r\n") else "\n"
    return m + [nl, nl] + theirs[k:]


def _duplicates(text: str) -> set[str]:
    seen, dup = set(), set()
    for name in TOP_DEF.findall(text):
        (dup if name in seen else seen).add(name)
    return dup


def _show(wt: Path, stage: int, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(wt), "show", f":{stage}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def settle(wt: Path, path: str, version_files: list[str]) -> bool:
    """Settle the conflicted `path` of a stopped rebase in `wt`, if it needs no judgment, and write
    the result: True when settled. Ours (stage 2) is the batch head, theirs (stage 3) the entry.
    In a version file every stage first takes the batch head's version, so a version line alone
    never conflicts. A pure addition keeps both sides (merge3), unless in a .py file that leaves a
    top-level def or class name twice where neither side had it twice (it would silently shadow a
    test). A file both sides created is never settled."""
    ours, base, theirs = _show(wt, 2, path), _show(wt, 1, path), _show(wt, 3, path)
    if ours is None or theirs is None or base is None:
        return False                    # deleted on one side, or created on both
    try:
        o, b, t = ours.decode(), (base or b"").decode(), theirs.decode()
    except UnicodeDecodeError:
        return False
    if "\0" in o + b + t:
        return False
    if path in version_files and (mv := push.VERSION_RE.search(o)):
        v = ".".join(mv.group(2, 3, 4))
        b, t = (push.VERSION_RE.sub(lambda m: f"{m.group(1)}{v}{m.group(5)}", x, count=1) for x in (b, t))
    merged = merge3(o, b, t, py=path.endswith(".py"))
    if merged is None:
        return False
    if path.endswith(".py") and _duplicates(merged) - _duplicates(o) - _duplicates(t):
        return False
    durable_write(wt / path, merged)
    return True


def _commit_replayed(wt: Path) -> None:
    """Commit the settled step of a stopped rebase in `wt` as the commit it replays, plus a trailer
    naming that commit. Settling changed its patch, so a later batch that gets the commit again (one
    died between its push and its marker) finds it landed by the trailer where git cherry cannot.
    A step that settled into nothing is left to the rebase, which drops it."""
    orig = _git(wt, "rev-parse", "--verify", "--quiet", "REBASE_HEAD").stdout.strip()
    if not orig or _git(wt, "diff", "--cached", "--quiet", "HEAD").returncode == 0:
        return
    msg = subprocess.run(["git", "-C", str(wt), "interpret-trailers", "--trailer", f"{REPLAYED}: {orig}"],
                         input=_git(wt, "log", "-1", "--format=%B", orig).stdout, text=True,
                         capture_output=True).stdout
    who = _git(wt, "log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", orig).stdout.strip().split("\0")
    if not msg.strip() or len(who) != 3:
        return                          # the rebase commits it with the message it has
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "--no-verify", "--cleanup=verbatim", "-F", "-"],
                   input=msg, text=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1],
                        "GIT_AUTHOR_DATE": who[2]})


# The batch --------------------------------------------------------------------------------------------

class Refused(Exception):
    pass


class Batch:
    def __init__(self, p: Project, marker: Path, m: dict):
        self.p, self.marker, self.m = p, marker, m
        self.id = str(m.get("id") or marker.stem)
        self.repo = Path(m.get("repo") or p.root)
        self.target = str(m.get("target") or "")
        self.remote, _, self.branch = self.target.partition("/")
        self.d = p.config().get("delivery") or {}
        self.entries = [e for e in m.get("entries") or [] if isinstance(e, dict)]
        self.wt = p.worktrees / WORKTREE
        self.status: dict[int, str] = {}       # entry index -> status
        self.detail: dict[int, dict] = {}
        self.checks = {"runs": 0, "seconds": 0.0, "flaky": False}
        self.tip = self.pushed_sha = self.version = None
        self.message = ""
        self.rounds = 0
        self.cfg: dict | None = None
        self.target_lock = None
        self.detail_tip: dict | None = None     # the tip's own failing check, for tip_failed
        self.info: dict[int, dict] = {}

    # results

    def _set(self, i: int, status: str, **detail) -> None:
        self.status[i], self.detail[i] = status, detail

    def results(self) -> list[dict]:
        return [{"id": e.get("id"), "task": e.get("task"), "status": self.status.get(i, "requeued"),
                 "detail": self.detail.get(i, {})} for i, e in enumerate(self.entries)]

    def _all(self, status: str, outcome: str, message: str = "", keep: tuple = ()) -> str:
        """Every entry not in a status of `keep` gets `status`; returns `outcome`."""
        for i in range(len(self.entries)):
            if self.status.get(i) not in keep:
                self._set(i, status, **({"message": message} if message and status == "refused" else {}))
        self.message = message or self.message
        if message:
            say(message)
        return outcome

    def error(self, e: BaseException) -> str:
        if self.pushed_sha:             # pushed already: that stays the outcome
            return "pushed"
        return self._all("requeued", "error", f"{type(e).__name__}: {e}", keep=("landed", "refused"))

    # the push phase

    def push_phase(self) -> str:
        """Steps 1-10; the outcome. Every entry has its result when this returns."""
        allowed = self.d.get("push_allowed")
        if not (allowed is None or allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
            return self._all("refused", "refused", "this project does not allow pushing (delivery.push_allowed)")
        try:
            checks = push.check_list(self.d.get("push_checks"))
            rounds = push.rounds_of(self.d.get("push_rounds"))
            self.cfg = push.bump_of(self.d.get("version_bump"))
            now = "/".join(push.target(self.p, self.repo))
        except ValueError as e:
            return self._all("refused", "refused", str(e))
        if not (self.remote and self.branch) or now != self.target:
            return self._all("requeued", "refused", f"the push target is {now}, not {self.target or '(none)'}")
        self.target_lock = push.take(self.p, self.remote, self.branch, 0, say=lambda _m: None,
                                     who=f"push batch {self.id}")
        if self.target_lock is None:
            who = ", ".join(locks.holders(push.lock_paths(self.p, self.remote, self.branch))) or "another push"
            return self._all("requeued", "busy", f"{push.lock_name(self.remote, self.branch)} is busy (held by {who})")
        why = push.refusal(self.repo, self.remote, self.branch)
        if why.startswith("refusing"):
            return self._all("refused", "refused", why)
        if why:
            return self._all("requeued", "error", why)
        for rnd in range(1, rounds + 1):
            self.rounds = rnd
            tip = push._fetch(self.repo, self.remote, self.branch)
            if not tip:
                return self._all("requeued", "error", f"cannot fetch {self.target}", keep=("refused",))
            self.tip = tip
            self.status, self.detail = {}, {}
            self._prepare(tip)
            outcome = self._round(tip, checks)
            if outcome != "moved":
                return outcome
            say(f"{self.target} moved; round {rnd + 1}")
        return self._all("requeued", "moved", f"{self.target} kept moving for {rounds} rounds",
                         keep=("landed", "refused"))

    def _prepare(self, tip: str) -> None:
        """A clean checkout of `tip` in the batch's own worktree, made anew when broken or dirty."""
        _sweep_after_push(self.p, self.repo)
        wt = self.wt
        ok = (wt / ".git").exists() and _registered(self.repo, wt) and not _rebasing(wt)
        if ok:
            ok = (_git(wt, "checkout", "-q", "--detach", "--force", tip).returncode == 0
                  and _git(wt, "clean", "-fdq").returncode == 0
                  and not _git(wt, "status", "--porcelain").stdout.strip())
        if not ok:
            say(f"preparing a fresh worktree at {wt}")
            _remove_worktree(self.repo, wt)
            wt.parent.mkdir(parents=True, exist_ok=True)
            r = _git(self.repo, "worktree", "add", "-q", "--detach", str(wt), tip)
            if r.returncode != 0:
                raise RuntimeError(f"cannot make the push worktree {wt}: {_last(r.stderr, 3)}")

    def _checkout(self, head: str) -> None:
        r = _git(self.wt, "checkout", "-q", "--detach", "--force", head)
        if r.returncode != 0 or _git(self.wt, "clean", "-fdq").returncode != 0:
            raise RuntimeError(f"cannot check out {head[:10]} in {self.wt}: {_last(r.stderr, 3)}")

    def _round(self, tip: str, checks: list[str]) -> str:
        """One round on `tip`: replay, bump, check, push. "moved" when the tip moved meanwhile."""
        carried = self._replay(tip, checks)
        if not carried:
            return self._close(0, "nothing")
        try:
            heads = {len(carried): self._head(tip, carried, len(carried))}
        except Refused as e:
            for i, _ in carried:
                self._set(i, "refused", message=str(e))
            self.message = str(e)
            say(str(e))
            return self._close(0, "refused")
        k, head, failed = self._judge(tip, carried, heads, checks)
        if head is None:
            return self._close(0, "tip_failed" if failed is None else "nothing", carried, failed)
        if push._fetch(self.repo, self.remote, self.branch) != tip:
            return "moved"
        # Without --force the remote takes only a fast-forward of the tip the checks ran on.
        r = _git(self.repo, "push", self.remote, f"{head}:refs/heads/{self.branch}")
        if r.returncode != 0:
            if push._fetch(self.repo, self.remote, self.branch) != tip:
                return "moved"
            self.message = f"push to {self.target} was rejected: {_last(r.stderr, 5)}"
            say(self.message)
            return self._close(0, "rejected", carried)
        self.pushed_sha = head
        outcome = self._close(k, "pushed", carried, failed)
        say(f"pushed {head[:10]} to {self.target}")
        try:
            self.version = self._version(head)
        except Exception:
            pass
        return outcome

    def _close(self, k: int, outcome: str, carried: list | None = None, failed: tuple | None = None) -> str:
        """Settle every entry once the first `k` carried entries are pushed: the one after them is
        check_failed when `failed` names its failure, and later ones are requeued. An entry that
        added nothing on top of carried entries (a rider), or conflicted with them, follows them:
        if they did not go, it is requeued, as it may apply to the tip as it is."""
        for n, (i, _) in enumerate(carried or [], 1):
            if n <= k:
                self._set(i, "pushed")
            elif n == k + 1 and failed:
                self._set(i, "check_failed", cmd=failed[0], tail=failed[1])
            else:
                self._set(i, "requeued")
        for i, st in list(self.status.items()):
            after = self.detail[i].pop("_after", 0)   # carried entries before it
            if st == "rider":
                self._set(i, "pushed" if outcome == "pushed" and after <= k else "requeued")
            elif st == "conflict" and after > k:
                self._set(i, "requeued")
        if outcome == "nothing" and "landed" in self.status.values():
            outcome = "landed"
        return outcome

    def _replay(self, tip: str, checks: list[str]) -> list[tuple[int, str]]:
        """Replay every entry in order onto the batch head, which starts at `tip`. Returns the
        carried entries as (index, batch head after it); the others get their status here."""
        carried: list[tuple[int, str]] = []
        h = tip
        self.info = {}
        csdir = self.cfg["changeset_dir"] if self.cfg else ""
        for i, e in enumerate(self.entries):
            sha = str(e.get("head") or "")
            if not re.fullmatch(r"[0-9a-f]{7,64}", sha) or \
                    _git(self.repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
                self._set(i, "refused", message=f"the reviewed head {sha[:10] or '(none)'} is not in {self.repo}")
                continue
            base = _git(self.repo, "merge-base", tip, sha).stdout.strip()
            if not base:
                self._set(i, "refused", message=f"{sha[:10]} shares no history with {self.target}")
                continue
            top = sha
            while top != base and re.search(rf"^{push.BUMP_TRAILER}: ",
                                            _git(self.repo, "log", "-1", "--format=%B", top).stdout, re.M):
                parent = _git(self.repo, "rev-parse", "--verify", "--quiet", f"{top}^").stdout.strip()
                if not parent:
                    break
                top = parent
            if top == base or self._on_tip(tip, base, top):
                say(f"entry {e.get('id')} ({e.get('branch')}): already on {self.target}")
                self._set(i, "landed")
                continue
            subjects = [s for s in _git(self.repo, "log", "--no-merges", "--reverse", "--format=%s",
                                        f"{base}..{top}").stdout.splitlines() if s.strip()]
            brings = bool(csdir) and any(f.endswith(".md") for f in _git(
                self.repo, "diff", "--name-only", "--diff-filter=A", base, top, "--", csdir).stdout.split())
            self.info[i] = {"subjects": subjects, "changeset": brings}
            if not checks and (code := push.code_paths(self.repo, base, top)):
                more = f" and {len(code) - 3} more" if len(code) > 3 else ""
                self._set(i, "refused", message=f"no checks configured, and this change touches more than "
                                                f"docs ({', '.join(code[:3])}{more}): " + push.NO_CHECKS)
                continue
            new, files = self._rebase(h, top)
            if files is not None:
                say(f"entry {e.get('id')} ({e.get('branch')}) conflicts with {h[:10]} in {', '.join(files)}")
                self._set(i, "conflict", files=files, onto=h, _after=len(carried))
            elif new == h:
                on_tip = h == tip or not any(line.startswith("+") for line in _git(
                    self.repo, "cherry", tip, top, base).stdout.splitlines())
                say(f"entry {e.get('id')} ({e.get('branch')}): adds nothing to "
                    + (self.target if on_tip else "the entries before it"))
                if on_tip:
                    self._set(i, "landed")
                else:
                    self._set(i, "rider", _after=len(carried))
            else:
                carried.append((i, new))
                self._set(i, "carried")
                h = new
        return carried

    def _on_tip(self, tip: str, base: str, top: str) -> bool:
        """Every commit of base..top is on `tip` already: as the same patch (git cherry), or replayed
        there by a batch that settled its conflict (its REPLAYED trailer)."""
        marks = [line.split() for line in _git(self.repo, "cherry", tip, top, base).stdout.splitlines()]
        left = {m[1] for m in marks if len(m) == 2 and m[0] == "+"}
        if not marks:
            return False
        if left:
            log = _git(self.repo, "log", "--format=%B", f"{base}..{tip}").stdout
            left -= set(re.findall(rf"^{REPLAYED}: ([0-9a-f]{{40,64}})[ \t]*$", log, re.M))
        return not left

    def _rebase(self, onto: str, top: str) -> tuple[str, list[str] | None]:
        """Rebase the entry's commits up to `top` onto `onto` in the worktree, settling conflicts that
        need no judgment: (new head, None), or (onto, conflicted files) after aborting a real one."""
        wt = self.wt
        version_files = self.cfg["files"] if self.cfg else []
        r = _git(wt, "rebase", "--no-keep-empty", onto, top)
        for _ in range(10000):
            if r.returncode == 0:
                return _git(wt, "rev-parse", "HEAD").stdout.strip(), None
            stopped = _rebasing(wt)
            files = [f for f in _git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0")
                     if f] if stopped else []
            real = [f for f in files if not settle(wt, f, version_files)]
            if real or not files:       # a real conflict, or a stop that is no conflict at all
                if stopped:
                    _git(wt, "rebase", "--abort")
                return onto, real or [f"(the rebase stopped: {_last(r.stderr or r.stdout, 3)})"]
            say(f"settled {', '.join(files)} (version lines, or lines both sides added)")
            _git(wt, "add", "--", *files)
            _commit_replayed(wt)
            r = _git(wt, "rebase", "--continue")
            if r.returncode != 0 and _rebasing(wt) and _git(wt, "diff", "--cached", "--quiet", "HEAD").returncode == 0 \
                    and not _git(wt, "diff", "--name-only", "--diff-filter=U").stdout.strip():
                r = _git(wt, "rebase", "--skip")     # the settled commit became empty (older gits stop)
        _git(wt, "rebase", "--abort")
        return onto, ["(the rebase did not finish)"]

    def _head(self, tip: str, carried: list[tuple[int, str]], k: int) -> str:
        """The head that pushes the first `k` carried entries: their replayed head plus one version
        bump (push.bump's rules), with a batch changeset for the entries that bring none."""
        h = carried[k - 1][1]
        cfg = self.cfg
        if not cfg:
            return h
        self._checkout(h)
        if not push.needs_bump(self.wt, tip, cfg):
            return h
        new = push.next_version(self.wt, tip, cfg["files"])
        if new is None:
            return h
        try:
            package = push.set_version(self.wt, cfg["files"], new, cfg["package"])
        except ValueError as e:
            self._checkout(h)
            raise Refused(f"{e}; not pushing") from None

        def subjects(i: int) -> list[str]:
            out = [re.sub(rf"^{re.escape(package)}: ", "", s) if package else s
                   for s in self.info.get(i, {}).get("subjects") or []]
            return out or ["update"]
        first = subjects(carried[0][0])[-1]
        more = f"; +{k - 1} more" if k > 1 else ""
        title = f"{package}: {new} ({first}{more})" if package else f"{new} ({first}{more})"
        lines = ["; ".join(subjects(i)) for i, _ in carried[:k] if not self.info.get(i, {}).get("changeset")]
        csdir, wrote = cfg["changeset_dir"], False
        if csdir and package and lines:
            body = (f"`{package}`: {lines[0]}." if len(lines) == 1
                    else f"`{package}`:\n\n" + "\n".join(f"- {s}" for s in lines))
            cs = self.wt / csdir / f"{push._slug(package)}-{push._slug(self.id)}.md"
            cs.parent.mkdir(parents=True, exist_ok=True)
            durable_write(cs, f'---\n"{package}": patch\n---\n\n{body}\n')
            _git(self.wt, "add", "--", str(cs.relative_to(self.wt)))
            wrote = True
        _git(self.wt, "add", "--", *cfg["files"])
        if _git(self.wt, "diff", "--cached", "--quiet").returncode == 0:
            return h
        r = _git(self.wt, "commit", "-q", "-m", f"{title}\n\n{push.BUMP_TRAILER}: {new}")
        if r.returncode != 0:
            self._checkout(h)
            raise Refused(f"delivery.version_bump: the bump commit failed ({_last(r.stderr, 3)}); not pushing")
        say(f"bumped {package or 'the version'} to {new} for {k} entr{'y' if k == 1 else 'ies'}"
            + (" with a batch changeset" if wrote else ""))
        return _git(self.wt, "rev-parse", "HEAD").stdout.strip()

    def _version(self, head: str) -> str | None:
        if not self.cfg:
            return None
        show = _git(self.repo, "show", f"{head}:{self.cfg['files'][0]}")
        m = push.VERSION_RE.search(show.stdout) if show.returncode == 0 else None
        return ".".join(m.group(2, 3, 4)) if m else None

    def _check(self, head: str, checks: list[str]) -> tuple[str, str] | None:
        """Check `head` in the worktree: None when it passes, else (command, output tail). The stale
        version guard (push.stale_versions) comes first and runs no command."""
        self._checkout(head)
        stale = push.stale_versions(self.wt, self.tip)
        if stale:
            msg = (f"{', '.join(stale)}: this change edits the plugin but keeps the version already on "
                   f"{self.target}; bump it past that (in every manifest, plus a changeset where the "
                   "repository wants one), commit, then rerun")
            say(f"{head[:10]}: {msg}")
            return STALE, msg
        if not checks:
            return None
        todo, skipped = push.applicable(self.wt, head, checks, say)
        for line in skipped:
            if line not in self.checks.setdefault("skipped", []):
                self.checks["skipped"].append(line)
        if not todo:
            say(f"{head[:10]}: {push.NONE_APPLY}")
            return push.NONE_APPLY, ""
        say(f"checking {head[:10]}")
        started = time.time()
        self.checks["runs"] += 1
        try:
            for cmd in todo:
                rc, tail, _ = _stream(cmd, self.wt)
                if rc != 0:
                    say(f"check failed on {head[:10]}: {cmd}")
                    return cmd, tail
        finally:
            self.checks["seconds"] = round(self.checks["seconds"] + time.time() - started, 1)
        push.record_check_s(self.p, time.time() - started)
        return None

    def _judge(self, tip: str, carried: list, heads: dict, checks: list[str]):
        """Check the full head; when it fails, find the first failing entry by a binary search over
        prefixes (each prefix: entries 1..k plus a bump), checking the tip alone only when prefix 1
        fails. Returns (entries to push, head to push or None, (cmd, tail) of the blamed entry's
        failure or None). When every prefix passes and the full head passes on a rerun, the failure
        was flaky: the full head goes."""
        m = len(carried)
        fail = self._check(heads[m], checks)
        if fail is None:
            return m, heads[m], None
        fails = {m: fail}
        lo, hi = 0, m
        while hi - lo > 1:
            mid = (lo + hi) // 2
            heads[mid] = self._head(tip, carried, mid)
            f = self._check(heads[mid], checks)
            if f is None:
                lo = mid
            else:
                hi, fails[mid] = mid, f
        if hi == 1 and fails[1][0] == STALE:
            return 0, None, fails[1]        # the version guard blames entry 1 without running anything
        if hi == 1:
            say(f"checking the tip {tip[:10]} alone")
            if (tf := self._check(tip, checks)) is not None:
                self.message = f"the tip of {self.target} fails its checks on its own: {tf[0]}"
                say(self.message)
                self.detail_tip = {"cmd": tf[0], "tail": tf[1]}
                return 0, None, None
            return 0, None, fails[1]
        if hi == m and fails[m][0] != STALE:
            say(f"every prefix passed; checking {heads[m][:10]} again")
            f = self._check(heads[m], checks)
            if f is None:
                self.checks["flaky"] = True
                say("the full head passed on the rerun: the failure was flaky")
                return m, heads[m], None
            fails[m] = f
        return hi - 1, heads[hi - 1], fails[hi]


# Marker, hand-over, after_push ---------------------------------------------------------------------------

def _wait_for_go() -> None:
    """The launcher closes our stdin once the marker is written."""
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except (OSError, ValueError, AttributeError):
        pass


def _seconds(v, default: float) -> float:
    try:
        s = float(v)
    except (TypeError, ValueError):
        return float(default)
    return s if s > 0 and s == s and s != float("inf") else float(default)


def after_push(p: Project, marker: Path, m: dict) -> dict:
    """Step 12: run `delivery.after_push` at the pushed commit, in a fresh detached worktree, under
    `delivery.after_push_timeout_s`. Its output goes to the batch log."""
    d = p.config().get("delivery") or {}
    cmds = push.check_list(d.get("after_push"))
    sha = m.get("pushed_sha")
    if m.get("outcome") != "pushed" or not sha:
        return {"status": "skipped", "reason": "nothing was pushed"}
    if not cmds:
        return {"status": "skipped", "reason": "delivery.after_push is not set"}
    timeout = _seconds(d.get("after_push_timeout_s"), DEFAULT_AFTER_PUSH_TIMEOUT_S)
    repo = Path(m.get("repo") or p.root)
    bid = str(m.get("id") or marker.stem)
    wt = p.worktrees / f"{AFTER_PUSH}{_safe(bid)}"
    started = time.time()
    _remove_worktree(repo, wt)
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(repo, "worktree", "add", "-q", "--detach", str(wt), sha)
    if r.returncode != 0:
        say(f"after_push: cannot check out {sha[:10]}: {_last(r.stderr, 3)}")
        return {"status": "failed", "exit": None, "cmd": "git worktree add", "started": started,
                "ended": time.time(), "tail": _last(r.stderr)}
    tasks = []
    for res in m.get("results") or []:
        t = res.get("task")
        if res.get("status") == "pushed" and t is not None and str(t) not in tasks:
            tasks.append(str(t))
    env = {**os.environ, "TTP_PUSHED_SHA": sha, "TTP_PUSHED_VERSION": str(m.get("version") or ""),
           "TTP_PUSH_TARGET": str(m.get("target") or ""), "TTP_PUSH_TASKS": ",".join(tasks),
           "TTP_PUSH_BATCH": str(marker), "TTP_PROJECT": str(p.base)}
    out = {"status": "ok", "exit": 0, "started": started, "tail": ""}
    try:
        for cmd in cmds:
            if why := push.skip_reason(wt, sha, cmd):
                say(f"after_push: {push.skipped_line(cmd, why)}")
                continue
            left = started + timeout - time.time()
            if left <= 0:
                out.update(status="timeout", exit=None, cmd=cmd)
                break
            say(f"after_push: {cmd}")
            rc, tail, timed_out = _stream(cmd, wt, env=env, timeout=left, group=True)
            out["tail"] = tail
            if timed_out:
                say(f"after_push: {cmd} ran past {timeout:.0f} s; killed")
                out.update(status="timeout", exit=None, cmd=cmd)
                break
            if rc != 0:
                say(f"after_push: {cmd} failed (exit {rc})")
                out.update(status="failed", exit=rc, cmd=cmd)
                break
    finally:
        _remove_worktree(repo, wt)
    out["ended"] = time.time()
    if out["status"] == "ok":
        say("after_push: done")
    return out


def _finish(p: Project, marker: Path, m: dict, after: dict, lock) -> None:
    m.update(after_push=after, phase="finished", status="finished", ended=time.time())
    write_json(marker, m)
    _forget(_after_lock(p, str(m.get("id") or marker.stem)))
    if lock is not None:
        lock.close()


def run_batch(p: Project, marker: Path) -> int:
    """`ttp push --batch <marker>`: push the batch the marker names and record the outcome in it
    (steps 1-11), then run after_push (12). A marker that has an outcome runs only after_push (13)."""
    marker = Path(marker)
    _wait_for_go()
    m = push._read(marker)
    if m.get("kind") != "batch":
        print(f"ttp push: no push batch marker at {marker}", file=sys.stderr)
        return push.REFUSED
    bid = str(m.get("id") or marker.stem)
    print(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} push batch {bid}, marker {marker}", flush=True)
    if m.get("phase") == "finished":
        say(f"batch {bid} finished already")
        return 0
    if m.get("outcome"):
        return _resume(p, marker, m, bid)
    run_lock_path = Path(m.get("lock") or push._run_lock(marker))
    run_lock = _hold(run_lock_path, f"push batch {bid}")
    if run_lock is None:
        say(f"another process holds the run lock of batch {bid}; leaving the batch to it")
        return push.BUSY
    b = Batch(p, marker, m)
    try:
        outcome = b.push_phase()
    except Exception as e:      # recorded, never left without an outcome
        outcome = b.error(e)
    m = push._read(marker) or m
    m.update(outcome=outcome, tip=b.tip, pushed_sha=b.pushed_sha, version=b.version, results=b.results(),
             checks=b.checks, rounds=b.rounds, message=b.message or None)
    if outcome == "tip_failed":
        m["tip_check"] = b.detail_tip
    write_json(marker, m)
    say(f"batch {bid}: {outcome}" + (f" ({b.message})" if b.message and outcome != "pushed" else ""))
    # Hand over: the deploy runs under its own lock, so upgrades no longer wait (push_in_flight).
    ap_path = _after_lock(p, bid)
    ap_lock = _hold(ap_path, f"push batch {bid} (after_push)")
    m.update(lock=str(ap_path), phase="after_push")
    write_json(marker, m)
    _forget(run_lock_path)
    run_lock.close()
    if b.target_lock is not None:
        b.target_lock.close()
    if ap_lock is None:
        after = {"status": "skipped", "reason": "another process holds this batch's after_push lock"}
    else:
        try:
            after = after_push(p, marker, m)
        except Exception as e:
            after = {"status": "failed", "exit": None, "tail": f"{type(e).__name__}: {e}", "ended": time.time()}
    _finish(p, marker, m, after, ap_lock)
    return EXIT.get(outcome, 1)


def _resume(p: Project, marker: Path, m: dict, bid: str) -> int:
    """Step 13: the push phase ended (its outcome is in the marker) but after_push did not finish."""
    ap_path = _after_lock(p, bid)
    lock = _hold(ap_path, f"push batch {bid} (after_push)")
    if lock is None:
        say(f"another process runs the after_push of batch {bid}")
        return push.BUSY
    for old in {Path(m.get("lock") or ap_path), push._run_lock(marker)} - {ap_path}:
        if old.name.startswith("push:run-"):     # the push phase is over; upgrades need not wait
            fd = _inherited(old)
            _forget(old)
            if fd is not None:
                _Fd(fd).close()
    m.update(lock=str(ap_path), phase="after_push")
    write_json(marker, m)
    say(f"resuming the after_push of batch {bid} ({m.get('outcome')})")
    done = m.get("after_push") if isinstance(m.get("after_push"), dict) and m["after_push"].get("ended") else None
    try:
        after = done or after_push(p, marker, m)
    except Exception as e:
        after = {"status": "failed", "exit": None, "tail": f"{type(e).__name__}: {e}", "ended": time.time()}
    _finish(p, marker, m, after, lock)
    return 0


def alive(marker: Path) -> bool:
    """Whether the batch process of `marker` still runs: the lock the marker names is held. The
    marker is read again after the test, as the process moves to another lock after the push."""
    m = push._read(marker)
    if m.get("phase") == "finished":
        return False
    seen = set()
    while m.get("lock") and m["lock"] not in seen:
        seen.add(m["lock"])
        if not locks.any_free([Path(m["lock"])]):
            return True
        m = push._read(marker)
        if m.get("phase") == "finished":
            return False
    return False


def summary(marker: Path) -> int:
    """`ttp push --result <marker>` for a batch marker: 0 once it finished (or died), 1 while it runs."""
    m = push._read(marker)
    bid = m.get("id") or Path(marker).stem
    live = alive(Path(marker))
    m = push._read(marker) or m
    if live:
        took = time.time() - float(m.get("started") or time.time())
        print(f"ttp push: batch {bid} still running ({m.get('phase')}, pid {m.get('pid')}, {took:.0f} s); "
              f"log: {m.get('log')}")
        return 1
    outcome = m.get("outcome")
    if not outcome:
        print(f"ttp push: batch {bid} ended without an outcome (killed, crashed or rebooted); log: {m.get('log')}")
        return 0
    sha = m.get("pushed_sha")
    head = f"batch {bid}: {outcome}" + (f", {str(sha)[:10]}" if sha else "") \
        + (f" as {m['version']}" if sha and m.get("version") else "") + f" to {m.get('target')}"
    print(f"ttp push: {head}" + (f" ({m['message']})" if m.get("message") and outcome != "pushed" else ""))
    entries = {e.get("id"): e for e in m.get("entries") or [] if isinstance(e, dict)}
    for r in m.get("results") or []:
        e = entries.get(r.get("id")) or {}
        d = r.get("detail") or {}
        why = (f" in {', '.join(d['files'])}" if d.get("files") else "") + (f": {d['cmd']}" if d.get("cmd") else "") \
            + (f": {d['message']}" if d.get("message") else "")
        print(f"  #{r.get('id')} task {e.get('task')} {e.get('branch')}: {r.get('status')}{why}")
    a = m.get("after_push") or {}
    if m.get("phase") != "finished":
        print(f"  after_push: did not finish (the process ended in {m.get('phase')})")
    elif a.get("status") and a.get("status") != "skipped":
        print(f"  after_push: {a.get('status')}" + (f" ({a.get('cmd')}, exit {a.get('exit')})"
                                                     if a.get("status") != "ok" else ""))
    print(f"  log: {m.get('log')}")
    if outcome not in ("pushed", "landed", "nothing") or a.get("status") in ("failed", "timeout"):
        tail = push._tail(m.get("log"))
        if tail:
            print(tail)
    return 0

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The push queue's batch process, `ttp push --batch <marker>`. It calls no model.

The daemon writes a marker naming the reviewed changes to push (entries: a branch and its reviewed
head), takes the batch's run lock and starts this process, which inherits the lock. The process
replays the entries in order onto the target's tip in a worktree of its own (`worktrees/push`) and
settles the conflicts that need no judgment: version lines, both sides adding different lines at
one spot, and both sides changing different lines that git calls a conflict only because they are
adjacent. The marker counts the entries that conflicted, were settled and were sent back. It bumps the version once for all entries, runs the push checks once on the result and
pushes it without force. It always runs them itself and never reads the passes `ttp checks` records;
it skips only a check a batch recorded passing on the same tree, command and environment (pushpass). When the checks fail, a binary search over prefixes finds the first failing
entry, and the passing prefix is pushed. A failed check's failing pytest ids are first rerun once at
the same head: passing, the failure was flaky (recorded, reported once) and the batch goes on. Before
an entry is blamed, they are rerun on the tip alone up to TIP_RERUNS times: failing there once, the
failure is the tip's own (flaky or already failing), and the entry is not sent back (the tip is broken
when the entry is the first; else the prefix that passed goes and the rest is requeued). The outcome goes into the marker per entry; the daemon
applies it to the reviews.

After the push it moves `delivery.fast_forward_also` and pushes the same commit to
`delivery.mirror_remotes`, fast-forward only; neither undoes the push. Then the process lets go of the push lock and of its `push:run-<id>` lock (automatic
upgrades wait only for that one). It then runs `delivery.after_push` (a deploy) at the pushed commit,
under a lock of its own, `after_push:run-<id>`. A marker that has an outcome but whose after_push did
not finish is resumed: only the after_push step runs. Before `git push` the marker records what the push
lands (`pushing`), so a process that dies between the push and its outcome (a reboot) is settled as
pushed once its head is found on the branch, never pushed twice (pushq.finalize).

While a device broker announces a power cycle of this host (cyclenotice.py), after_push does not start:
the marker (or an ordinary push's receipt) records `after_held`, and the daemon runs it once the notice
is gone, spending no resume. An ordinary push's after_push that a reboot cut short is run once more after
the boot, like a batch's (pushq.tend_receipts)."""
from __future__ import annotations

import difflib
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
import tokenize
from collections import deque
from pathlib import Path

from . import (checkcap, checkdirs, conflictfix, cyclenotice, envfault, locks, push, pushpass, runner, scopehold,
               scriptwatch,
               sharedchecks, slowtests, suitecost, worktree)
from .project import Project, command_env, durable_write, write_json

WORKTREE = "push"             # the batch's own checkout under the project's worktrees (never t<id>)
AFTER_PUSH = "after_push-"    # + batch id: the temporary checkout after_push runs in
DEFAULT_AFTER_PUSH_TIMEOUT_S = 1800
STALE = "(stale plugin version)"   # the `cmd` of a check failure that is push.stale_versions
SETTLED_WORDS = {"version": "version lines", "added": "lines both sides added",
                 "hunks": "changes of different lines", "taken_in": "a change the other side holds"}
MERGED_KINDS = {"added", "hunks"}   # settlements that keep both sides' changes: kept only when the checks pass
REPLAYED = "Ttp-Replayed-From"     # trailer of a commit the batch settled: the entry commit it replays
RESCUES = 1                        # checks per batch of the entries after a blamed one, replayed without it
NO_TESTS = 5                       # pytest's exit status when nothing was selected: a cap probe of a check without cap tests
RERUN_S = 600.0                    # delivery.push_rerun_s: the time limit of one rerun of a failed check's failing tests
RERUN_IDS = 10                     # a check failing more tests than this is not rerun: that is no flake
TIP_RERUNS = 3                     # reruns on the tip alone of a blamed failure's failing tests: one failing clears the entry
OVER = re.compile(r"^E\s+assert (\d[\d_]*) <= (\d[\d_]*)\s*$")   # pytest's size-over-cap line
SECTION = re.compile(r"^_{3,} (\S.*?) _{3,}$")                     # pytest's per-test failure header
OVER_KEPT = 10                     # overshoot lines a check's tail keeps from its whole output
TOP_DEF = re.compile(r"^(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_]\w*)", re.M)
LEAD_KEY = re.compile(r"""["']?([A-Za-z_][\w.-]*)["']?[ \t]*[:=](?![=/])""")   # `"key":`, `key =`, `key:`
TOP_START = re.compile(r"(?:@|(?:async[ \t]+)?def[ \t]|class[ \t])")
EXIT = {"pushed": 0, "landed": 0, "nothing": 0, "busy": push.BUSY, "env_fault": push.BUSY, "moved": push.KEPT_MOVING,
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


def _stream(cmd: str | list[str], cwd: Path, env: dict | None = None, timeout: float | None = None,
            group: bool = False, pass_fds: tuple = ()) -> tuple[int | None, str, bool]:
    """Run `cmd` (a shell string, or an argv run without a shell), copying its output into this
    process's output (the batch log): (exit code, the last lines of its output, whether it ran out of time and was killed). `group` starts it in a
    process group of its own, which a timeout kills whole. The tail starts with up to OVER_KEPT
    pytest `assert <size> <= <cap>` lines where the size is over, each naming its test, from
    anywhere in the output: a prompt size cap's measured overshoot (conflictfix.cap_failure)."""
    sys.stdout.flush()
    sys.stderr.flush()
    proc = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=group, pass_fds=pass_fds)
    tail: deque[str] = deque(maxlen=push.LOG_TAIL)
    over: list[str] = []
    section = [""]

    def pump() -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace")
            tail.append(line.rstrip("\n"))
            if (sm := SECTION.match(line.strip())):
                section[0] = sm.group(1).split()[0]
            elif (om := OVER.match(line.rstrip())) and len(over) < OVER_KEPT \
                    and int(om.group(1).replace("_", "")) > int(om.group(2).replace("_", "")):
                over.append(f"E       assert {om.group(1)} <= {om.group(2)}" + (f"  (in {section[0]})" if section[0] else ""))
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
    return proc.returncode, "\n".join([*over, *tail]), timed_out


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

def merge3(ours: str, base: str, theirs: str, py: bool = False, taken_in: bool = False,
           hunks: bool = False, kinds: set | None = None, superseded: bool = False,
           near: bool = False) -> str | None:
    """git's three-way merge of the texts, where each conflict in which both sides only added lines
    at one spot (an empty base section) keeps both: ours first, then theirs (in Python, see _seam).
    With `taken_in`, a conflict whose change on our side theirs already holds (_taken_in) takes
    theirs. With `hunks`, a conflict where the two sides changed different lines of the base section
    (git calls adjacent changes a conflict) takes both changes (_hunks). With `superseded`, a conflict
    whose change on our side theirs already makes, comments and spacing aside, takes theirs
    (_superseded). With `near`, both sides' added lines are not kept (by the added or hunks rule)
    when one nearly repeats the other (_near). None when any other conflict
    remains, or when keeping both could be wrong (_clash). `kinds` collects how each conflict was
    settled ("added", "taken_in", "hunks"). The conflict markers carry a random tag, so file content
    never passes for one; a hunk whose base-to-end part holds more than one separator line is not read.
    git ends every side of a conflict at the end of the file with a newline; the result ends as the
    sides had it (the side that changed it from the base wins)."""
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
    lines, res, i, at_end = out.splitlines(keepends=True), [], 0, False
    no_comments: dict | None = None     # built on the first conflict that needs it (_py_code)
    while i < len(lines):
        if lines[i].rstrip("\r\n") != start:
            res.append(lines[i])
            i += 1
            continue
        j, mine = i + 1, []
        while j < len(lines) and lines[j].rstrip("\r\n") != mid:
            mine.append(lines[j])
            j += 1
        j, old = j + 1, []
        while j < len(lines) and lines[j].rstrip("\r\n") != sep:
            old.append(lines[j])
            j += 1
        if old and not (taken_in or hunks or superseded):
            return None                 # the base section is not empty: a real conflict
        j, theirs_lines = j + 1, []
        while j < len(lines) and lines[j].rstrip("\r\n") != end:
            if lines[j].rstrip("\r\n") == sep:
                return None             # ambiguous: content that looks like the separator
            theirs_lines.append(lines[j])
            j += 1
        if j >= len(lines):
            return None
        if superseded and py and no_comments is None:
            no_comments = _py_code((ours, base, theirs))
        if taken_in and _taken_in(mine, old, theirs_lines):
            res += theirs_lines
            how = "taken_in"
        elif superseded and _superseded(mine, old, theirs_lines, py, no_comments):
            res += theirs_lines
            how = "superseded"
        elif not old and not _clash(mine, theirs_lines, py) and not (near and _near(mine, theirs_lines, py)):
            res += _seam(mine, theirs_lines) if py else mine + theirs_lines
            how = "added"
        elif old and hunks and (both := _hunks(mine, old, theirs_lines, py, near)) is not None:
            res += both
            how = "hunks"
        else:                           # a real conflict
            return None
        if kinds is not None:
            kinds.add(how)
        at_end, i = j == len(lines) - 1, j + 1
    merged = "".join(res)
    nl = [t.endswith("\n") for t in (ours, base, theirs)]
    eol = lines[-1][len(lines[-1].rstrip("\r\n")):]     # the newline git ends its lines with
    if at_end and not (nl[2] if nl[0] == nl[1] else nl[0]) and eol and merged.endswith(eol):
        merged = merged[:-len(eol)]
    return merged


def _hunks(mine: list[str], old: list[str], theirs: list[str], py: bool, near: bool = False) -> list[str] | None:
    """Both sides' changes of the `old` lines of one conflict, when they touch different lines: each
    side's edits (line diff against old) share no old line, and neither inserts lines at the edge of
    or inside an edit of the other (the order would be a guess). None on an overlap, or when the
    lines the two sides added share one (_said: the result would hold it twice), or with `near`,
    when one nearly repeats the other (_near)."""
    def edits(side: list[str]) -> list[tuple[int, int, list[str]]]:
        sm = difflib.SequenceMatcher(None, old, side, autojunk=False)
        return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    a, b = edits(mine), edits(theirs)
    if not a or not b:
        return None
    for i1, i2, _ in a:
        for j1, j2, _ in b:
            if i1 < j2 and j1 < i2:                     # both changed an old line
                return None
            if (i1 == i2 and j1 <= i1 <= j2) or (j1 == j2 and i1 <= j1 <= i2):
                return None                             # an insertion where the other edits
    new_a, new_b = [x for *_, new in a for x in new], [x for *_, new in b for x in new]
    if _said(new_a, py) & _said(new_b, py) or near and _near(new_a, new_b, py):
        return None
    out, pos = [], 0
    for i1, i2, new in sorted(a + b, key=lambda e: e[:2]):
        out += old[pos:i1] + new
        pos = i2
    return out + old[pos:]


def _taken_in(mine: list[str], old: list[str], theirs: list[str]) -> bool:
    """Whether theirs already holds our side's change of the `old` lines (a local fix the other side
    shipped too): ours added a line, every line ours added is in theirs, and no line ours removed
    is. A change of ours that only removes lines is left for judgment: theirs may have edited them.
    Lines compare stripped; lines without a letter or digit are left out."""
    def said(lines: list[str]) -> set[str]:
        return {s for s in (line.strip() for line in lines) if any(c.isalnum() for c in s)}
    m, o, t = said(mine), said(old), said(theirs)
    return bool(m - o) and (m - o) <= t and not (o - m) & t


def _py_code(texts: tuple[str, ...]) -> dict[str, str]:
    """Each physical line of the Python `texts` mapped to itself without its comment, as tokenize reads
    it: a `#` inside a string, such as a heading in prompt text kept in a triple-quoted string, stays.
    A line that reads differently in two places (a comment here, string content there) maps to
    itself. Empty, so that nothing is set aside, when a text does not tokenize."""
    out: dict[str, str] = {}
    for text in texts:
        rows = text.splitlines(keepends=True)
        cut: dict[int, int] = {}
        try:
            for tok in tokenize.generate_tokens(iter(rows).__next__):
                if tok.type == tokenize.COMMENT:
                    cut[tok.start[0]] = tok.start[1]
        except (tokenize.TokenError, SyntaxError, ValueError):
            return {}
        for n, row in enumerate(rows, 1):
            key = row.rstrip()
            code = row[:cut[n]].rstrip() if n in cut else key
            out[key] = code if out.get(key, code) == code else key
    return out


def _code(lines: list[str], no_comments: dict[str, str] | None) -> list[str]:
    """`lines` as compared for _superseded: blank lines left out; in Python (`no_comments`, _py_code)
    comments cut and the indent kept; elsewhere runs of spaces count as one."""
    out = []
    for line in lines:
        s = line.rstrip()
        s = no_comments.get(s, s) if no_comments is not None else " ".join(s.split())
        if s.strip():
            out.append(s)
    return out


def _indel(a: list[str], b: list[str]) -> int:
    """Lines to delete and insert to turn `a` into `b` (via their longest common subsequence)."""
    row = [0] * (len(b) + 1)
    for x in a:
        prev, row = row, [0]
        for j, y in enumerate(b):
            row.append(prev[j] + 1 if x == y else max(prev[j + 1], row[j]))
    return len(a) + len(b) - 2 * row[-1]


def _superseded(mine: list[str], old: list[str], theirs: list[str], py: bool,
                no_comments: dict[str, str] | None = None) -> bool:
    """Whether theirs already makes our side's change of the `old` lines, comments and spacing aside
    (_code; in Python `no_comments` from _py_code): ours lies on a shortest way from old to theirs, so
    every line ours removed theirs removed too and every line ours added theirs holds, in order. A
    local fix that upstream shipped in its own words, or a local edit of comments alone. A line ours
    removed must be gone from theirs, not reworded: not when it is close to a line theirs added that
    ours did not (_reworded; in Python the body of a def or class theirs opens is new as a whole,
    _said), and, when ours only removed lines, each must sit in a stretch theirs only deleted. Big
    sections are left for judgment."""
    m, o, t = (_code(x, (no_comments or {}) if py else None) for x in (mine, old, theirs))
    if (len(m) + len(o)) * (len(t) + len(o)) > 250_000:
        return False
    if _indel(o, m) + _indel(m, t) != _indel(o, t):
        return False
    removed = {i for tag, i1, i2, _, _ in _opcodes(o, m) if tag != "equal" for i in range(i1, i2)}
    added = [x for tag, _, _, j1, j2 in _opcodes(o, m) if tag != "equal" for x in m[j1:j2]]
    if not added and any(tag != "delete" and i1 <= i < i2 for i in removed
                         for tag, i1, i2, _, _ in _opcodes(o, t) if tag != "equal"):
        return False
    new = [x for tag, _, _, j1, j2 in _opcodes(o, t) if tag != "equal" for x in t[j1:j2]]
    for x in added:
        if x in new:
            new.remove(x)
    return not any(_reworded(o[i].strip(), x) for i in removed for x in _said(new, py))


def _opcodes(a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()


def _reworded(a: str, b: str) -> bool:
    """Whether line `b` reads as a rewording of line `a` (most of their text in common)."""
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() > 0.6


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


def _near(mine: list[str], theirs: list[str], py: bool) -> bool:
    """Whether a line one side added nearly repeats one the other added (lines as _said reads them):
    the same leading key (`"key":`, `key =`, `key:`) or most of the text alike (difflib ratio above
    0.8). Keeping both would then likely hold two versions of one line, such as a key given twice.
    In Python a def or class line is not compared by its text: settle refuses a repeated name."""
    a, b = _said(mine, py), _said(theirs, py)
    keys = {m.group(1) for y in b if (m := LEAD_KEY.match(y))}
    for x in a:
        if (m := LEAD_KEY.match(x)) and m.group(1) in keys:
            return True
        if py and TOP_DEF.match(x):
            continue
        for y in b:
            if py and TOP_DEF.match(y):
                continue
            sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
            if sm.real_quick_ratio() > 0.8 and sm.quick_ratio() > 0.8 and sm.ratio() > 0.8:
                return True
    return False


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


def settle(wt: Path, path: str, version_files: list[str], taken_in: bool = False, hunks: bool = False,
           kinds: set | None = None, superseded: bool = False, near: bool = False) -> bool:
    """Settle the conflicted `path` of a stopped rebase in `wt`, if it needs no judgment, and write
    the result: True when settled. Ours (stage 2) is the batch head, theirs (stage 3) the entry.
    In a version file every stage first takes the batch head's version, so a version line alone
    never conflicts. A pure addition keeps both sides (merge3), unless in a .py file that leaves a
    top-level def or class name twice where neither side had it twice (it would silently shadow a
    test). `taken_in`, `hunks`, `superseded`, `near` and `kinds` are merge3's; a version line taken adds "version" to
    `kinds`. A file both sides created is never settled."""
    ours, base, theirs = _show(wt, 2, path), _show(wt, 1, path), _show(wt, 3, path)
    if ours is None or theirs is None or base is None:
        return False                    # deleted on one side, or created on both
    try:
        o, b, t = ours.decode(), (base or b"").decode(), theirs.decode()
    except UnicodeDecodeError:
        return False
    if "\0" in o + b + t:
        return False
    how: set = set()
    if path in version_files and (mv := push.VERSION_RE.search(o)):
        v = ".".join(mv.group(2, 3, 4))
        vb, vt = (push.VERSION_RE.sub(lambda m: f"{m.group(1)}{v}{m.group(5)}", x, count=1) for x in (b, t))
        if (vb, vt) != (b, t):
            how.add("version")
        b, t = vb, vt
    merged = merge3(o, b, t, py=path.endswith(".py"), taken_in=taken_in, hunks=hunks, kinds=how,
                    superseded=superseded, near=near)
    if merged is None:
        return False
    if path.endswith(".py") and _duplicates(merged) - _duplicates(o) - _duplicates(t):
        return False
    durable_write(wt / path, merged)
    if kinds is not None:
        kinds |= how
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
        self.landed_at: dict[int, str] = {}    # entry index -> its own commit on the branch (pushed or landed)
        self.check_run_dir = m.get("check_run_dir")
        self.checks = {"runs": 0, "seconds": 0.0, "flaky": False}
        self.tip = self.pushed_sha = self.version = None
        self.reach: dict | None = None          # push.reach of what it pushed or found landed
        self.message = ""
        self.rounds = 0
        self.cfg: dict | None = None
        self.target_lock = None
        self.detail_tip: dict | None = None     # the tip's own failing check, for tip_failed
        self.tip_passed = False                 # the tip passed its checks alone this round
        self.tip_tried = 0                      # leading entries shown not to fix a failing tip (tip_failed)
        self.tip_runs = 0                       # the rerun on the tip (of TIP_RERUNS) that failed last (_tip_ids)
        self.info: dict[int, dict] = {}
        self.settled: dict[int, dict] = {}     # entry index -> the conflicts its replay settled
        self.conflicted: set[int] = set()      # entries whose replay hit a conflict it did not settle
        self._settling: tuple[list[str], set] = ([], set())   # files and kinds of the replay running
        self.rescued: dict[int, str] = {}      # entry index -> its head, replayed again without a blamed entry
        self.held: dict[int, dict] = {}        # entry index -> the token scope a refused push needs for it
        self.scopes: list[dict] = []           # those refusals: scope, files, entry ids, check (scopehold)
        self.retried = False                   # this round checked the entries after a blamed one (_rescue)
        self.rescues = 0                       # such checks in the batch: at most RESCUES
        self.reran: dict[str, set[str]] = {"head": set(), "tip": set()}   # failing test ids rerun: once each per batch
        self.flaky: list[dict] = []            # failures whose tests passed on that rerun: {ids, head, cmd}
        self.pre_existing: list[dict] = []     # failures whose tests failed on the tip alone too: {ids, tip, cmd, entry, runs}
        self.fail_rc: dict[str, int] = {}      # a failed check's output tail -> its exit code (_tip_ids)

    # results

    def _set(self, i: int, status: str, **detail) -> None:
        self.status[i], self.detail[i] = status, detail

    def results(self) -> list[dict]:
        """Each entry's outcome; the detail of one whose replay settled conflicts names them
        ("settled": files, "settled_by": kinds), so a check failure after it can be traced to it. A
        pushed or landed entry names its own commit on the branch ("sha": the replayed head that ends
        with it, or the tip it was found on), which a rebase made different from its reviewed head."""
        out = []
        for i, e in enumerate(self.entries):
            st = "scope_held" if i in self.held else self.status.get(i, "requeued")
            r = {"id": e.get("id"), "task": e.get("task"), "status": st,
                 "detail": {**self.detail.get(i, {}), **self.settled.get(i, {})}}
            if st in ("pushed", "landed") and i in self.landed_at:
                r["sha"] = self.landed_at[i]
            out.append(r)
        return out

    def conflicts(self) -> dict:
        """The round's conflict counts, for the marker and the log: entries replayed, those that hit a
        conflict (also when requeued after it; a rebase stop that is no conflict is none), those whose
        conflicts all settled without judgment and went on, those of them settled by taking both
        sides' changes of different lines ("hunks"), and those sent back (status conflict)."""
        back = {i for i in self.conflicted | set(self.settled) if self.status.get(i) == "conflict"}
        kept = [d for i, d in self.settled.items() if i not in back]
        return {"entries": len(self.entries), "conflicted": len(self.conflicted) + len(self.settled),
                "auto_resolved": len(kept), "by_hunks": sum(1 for d in kept if "hunks" in d["settled_by"]),
                "sent_back": len(back)}

    def _all(self, status: str, outcome: str, message: str = "", keep: tuple = ()) -> str:
        """Every entry not in a status of `keep` gets `status`; returns `outcome`."""
        for i in range(len(self.entries)):
            if self.status.get(i) not in keep and i not in self.held:
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
            checks = push.with_host(push.check_list(push.repo_checks(self.d, self.repo, self.p)[0]),
                                    self.d.get("checks_host"))
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
            who = ", ".join(locks.holders_now(push.lock_paths(self.p, self.remote, self.branch), self.p)) or "another push"
            return self._all("requeued", "busy", f"{push.lock_name(self.remote, self.branch)} is busy (held by {who})")
        why = push.refusal(self.repo, self.remote, self.branch, push.allow_protected(self.d))
        if why.startswith("refusing"):
            return self._all("refused", "refused", why)
        if why:
            return self._all("requeued", "error", why)
        rnd = 0
        while rnd < rounds:
            rnd += 1
            self.rounds += 1
            tip = push._fetch(self.repo, self.remote, self.branch)
            if not tip:
                return self._all("requeued", "error", f"cannot fetch {self.target}", keep=("refused",))
            self.tip = tip
            self.status, self.detail, self.landed_at = {}, {}, {}
            for i, d in self.held.items():
                self._set(i, "scope_held", **d)
            self._prepare(tip)
            try:
                outcome = self._round(tip, checks)
            except envfault.Fault as e:
                # The host, not an entry: every entry goes back to the queue untouched, which holds
                # until the runner is back (pushq.due); the coordinator hears of it once.
                envfault.record(self.p, e.fault, queue=True)
                return self._all("requeued", "env_fault", f"{e}; the batch is held, not failed",
                                 keep=("landed", "refused"))
            if outcome == "held":   # a round of its own: each one holds at least one more entry
                rnd -= 1
                say("the rest of the batch goes again without the held entries")
                continue
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
        worktree.prepare(self.p, wt, self.repo)   # the .venv a check like `.venv/bin/python ...` runs

    def _checkout(self, head: str) -> None:
        r = _git(self.wt, "checkout", "-q", "--detach", "--force", head)
        if r.returncode != 0 or _git(self.wt, "clean", "-fdq").returncode != 0:
            raise RuntimeError(f"cannot check out {head[:10]} in {self.wt}: {_last(r.stderr, 3)}")

    def _round(self, tip: str, checks: list[str]) -> str:
        """One round on `tip`: replay, bump, check, push. "moved" when the tip moved meanwhile."""
        self.tip_passed = False
        self.tip_tried = 0
        self.rescued, self.retried = {}, False
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
        if failed is not None and (more := self._rescue(tip, carried, k, checks)):
            head = more
        if head is None:
            return self._close(0, "tip_failed" if failed is None else "nothing", carried, failed)
        if push._fetch(self.repo, self.remote, self.branch) != tip:
            return "moved"
        self._mark_pushing(tip, head, k, carried, failed)
        # Without --force the remote takes only a fast-forward of the tip the checks ran on.
        r = _git(self.repo, "push", self.remote, f"{head}:refs/heads/{self.branch}")
        if r.returncode != 0:
            if push._fetch(self.repo, self.remote, self.branch) != tip:
                return "moved"
            if self._hold_scoped(tip, carried, r.stderr):
                return "held"
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

    def _hold_scoped(self, tip: str, carried: list, stderr: str) -> bool:
        """A push the remote refused because the credential lacks a token scope (scopehold): hold the
        carried entries whose changes need it, in batch order, so the rest goes again at once. False
        when the refusal is no such one or no entry's changes match it."""
        found = False
        for need in scopehold.classify(stderr):
            ids = []
            for i, _ in carried:
                if i in self.held:
                    continue
                files = scopehold.needs(scopehold.touched(self.repo, tip, str(self.entries[i].get("head"))), need)
                if files:
                    self.held[i] = {"scope": need["scope"], "files": files[:20],
                                    "check": scopehold.check_cmd(self.d, need["scope"])}
                    ids.append(self.entries[i].get("id"))
                    say(f"entry {self.entries[i].get('id')} ({self.entries[i].get('branch')}): held, its "
                        f"{', '.join(files[:3])} needs the token scope `{need['scope']}`")
            if ids:
                found = True
                self.scopes.append({**need, "entries": ids, "check": scopehold.check_cmd(self.d, need["scope"]),
                                    "stderr": _last(stderr, 5)})
        return found

    def _mark_pushing(self, tip: str, head: str, k: int, carried: list, failed: tuple | None) -> None:
        """Record in the marker, before `git push`, what this push lands and what each entry's result
        is if it does. A process that dies between the push and its outcome (a reboot) leaves this
        behind; the queue finds `head` on the branch and settles the batch as pushed from it
        (pushq.finalize), so the push is neither lost nor made twice and its after_push still runs."""
        saved = (dict(self.status), {i: dict(d) for i, d in self.detail.items()}, dict(self.landed_at))
        try:
            self._close(k, "pushed", carried, failed)
            results = self.results()
        finally:
            self.status, self.detail, self.landed_at = saved
        try:
            version = self._version(head)
        except Exception:
            version = None
        m = push._read(self.marker) or self.m
        m["pushing"] = {"sha": head, "tip": tip, "version": version, "results": results, "at": time.time(),
                        "checks": self.checks, "rounds": self.rounds}
        write_json(self.marker, m)

    def _close(self, k: int, outcome: str, carried: list | None = None, failed: tuple | None = None) -> str:
        """Settle every entry once the first `k` carried entries are pushed: the one after them is
        check_failed when `failed` names its failure, and later ones are pushed when _rescue passed them
        without it, else requeued ("untested" when no check ran without it); one whose replay
        settled a conflict by keeping both sides' changes (MERGED_KINDS) is sent back as a conflict
        instead, as that merge may be what fails: such a settlement is kept only when the checks pass. An entry that added nothing on top of carried entries (a
        rider), or conflicted with them, follows them: if they did not go, it is requeued, as it may
        apply to the tip as it is."""
        for n, (i, h) in enumerate(carried or [], 1):
            if n <= k:
                self._set(i, "pushed")
                self.landed_at[i] = h
            elif n == k + 1 and failed and failed[0] not in (STALE, push.NONE_APPLY) \
                    and MERGED_KINDS & set(self.settled.get(i, {}).get("settled_by", [])):
                say(f"entry {self.entries[i].get('id')}: its checks failed after the batch merged both sides' "
                    "changes; sent back for a rebase")
                self._set(i, "conflict", files=self.settled[i]["settled"], cmd=failed[0], tail=failed[1])
            elif n == k + 1 and failed:
                self._set(i, "check_failed", cmd=failed[0], tail=failed[1])
            elif i in self.rescued and outcome == "pushed":
                self._set(i, "pushed")
                self.landed_at[i] = self.rescued[i]
            elif failed and not self.retried:
                self._set(i, "requeued", untested=True)    # after the blamed entry, never checked without it
            else:
                self._set(i, "requeued")
        for i, st in list(self.status.items()):
            after = self.detail[i].pop("_after", 0)   # carried entries before it
            if st == "rider":
                self._set(i, "pushed" if outcome == "pushed" and after <= k else "requeued")
                if outcome == "pushed" and 0 < after <= k:
                    self.landed_at[i] = carried[after - 1][1]
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
        self.info, self.settled, self.conflicted = {}, {}, set()
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
            if i in self.held:   # its push needs a token scope the credential lacks (_hold_scoped)
                continue
            if top == base or self._on_tip(tip, base, top):
                say(f"entry {e.get('id')} ({e.get('branch')}): already on {self.target}")
                self._set(i, "landed")
                self.landed_at[i] = tip
                continue
            subjects = [s for s in _git(self.repo, "log", "--no-merges", "--reverse", "--format=%s",
                                        f"{base}..{top}").stdout.splitlines() if s.strip()]
            brings = bool(csdir) and any(f.endswith(".md") for f in _git(
                self.repo, "diff", "--name-only", "--diff-filter=A", base, top, "--", csdir).stdout.split())
            if csdir and (csfile := self.cfg.get("changeset_file") or "") and not brings:
                # an entry that edits the release changeset itself brings its own changeset
                brings = csfile in _git(self.repo, "diff", "--name-only", base, top, "--", csfile).stdout.split()
            self.info[i] = {"subjects": subjects, "changeset": brings, "top": top}
            if not checks and (code := push.code_paths(self.repo, base, top)):
                more = f" and {len(code) - 3} more" if len(code) > 3 else ""
                self._set(i, "refused", message=f"no checks configured, and this change touches more than "
                                                f"docs ({', '.join(code[:3])}{more}): " + push.NO_CHECKS)
                continue
            if why := push.scalar_problem(self.d.get("push_exclude_paths")):   # never push unguarded
                self._set(i, "refused", message=f"delivery.push_exclude_paths: {why}")
                continue
            if bad := push.excluded(self.repo, tip, top, push.exclude_list(self.d.get("push_exclude_paths"))):
                self._set(i, "refused", message=push.excluded_refusal(bad, self.target))
                continue
            self._settling = ([], set())
            new, files = self._rebase(h, top)
            if files is None and self._settling[0]:
                self.settled[i] = {"settled": sorted(set(self._settling[0])), "settled_by": sorted(self._settling[1])}
            if files is not None:
                say(f"entry {e.get('id')} ({e.get('branch')}) conflicts with {h[:10]} in {', '.join(files)}")
                self._set(i, "conflict", files=files, onto=h, _after=len(carried))
                if not files[0].startswith("(the rebase"):
                    self.conflicted.add(i)
            elif new == h:
                on_tip = h == tip or not any(line.startswith("+") for line in _git(
                    self.repo, "cherry", tip, top, base).stdout.splitlines())
                say(f"entry {e.get('id')} ({e.get('branch')}): adds nothing to "
                    + (self.target if on_tip else "the entries before it"))
                if on_tip:
                    self._set(i, "landed")
                    self.landed_at[i] = tip
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
        need no judgment (the push checks then run on the result, as on any other): (new head, None),
        or (onto, conflicted files) after aborting a real one. The files and kinds settled go to
        self._settling. An entry already on `onto` goes as it is, and one with merges keeps them
        (--rebase-merges): a plain rebase flattens a merge landing and rewrites its commits, so their
        tags drop out."""
        wt = self.wt
        version_files = self.cfg["files"] if self.cfg else []
        if push.descends(wt, onto, top):
            self._checkout(top)
            return top, None
        merges = ["--rebase-merges"] if _git(wt, "rev-list", "--merges", "-1", top, f"^{onto}").stdout.strip() else []
        r = _git(wt, "rebase", "--no-keep-empty", *merges, onto, top)
        for _ in range(10000):
            if r.returncode == 0:
                return _git(wt, "rev-parse", "HEAD").stdout.strip(), None
            stopped = _rebasing(wt)
            files = [f for f in _git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0")
                     if f] if stopped else []
            kinds: set = set()
            real = [f for f in files if not settle(wt, f, version_files, hunks=True, kinds=kinds)]
            if real or not files:       # a real conflict, or a stop that is no conflict at all
                if stopped:
                    _git(wt, "rebase", "--abort")
                if real and merges and (new := push.squash_merge(wt, onto, top)):
                    # A commit after a merge edits lines the merge resolved; the merge itself is clean.
                    say(f"rebase of {top[:10]} conflicts in {', '.join(real)}, but its merge with {onto[:10]} "
                        f"is clean: carrying that merge as one commit {new[:10]}")
                    self._checkout(new)
                    return new, None
                return onto, real or [f"(the rebase stopped: {_last(r.stderr or r.stdout, 3)})"]
            how = ", ".join(SETTLED_WORDS.get(k, k) for k in sorted(kinds)) or "a clean three-way merge"
            say(f"settled {', '.join(files)} ({how})")
            self._settling[0].extend(files)
            self._settling[1].update(kinds)
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
        bump (push.bump's rules), with a batch changeset for the entries that bring none (added to
        the release changeset instead when `changeset_file` is set)."""
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
        if csdir and package and lines and cfg.get("changeset_file"):
            push.release_changeset(self.wt, cfg["changeset_file"], package, lines)
            wrote = True
        elif csdir and package and lines:
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

    def _runners(self, cmds: list, env: dict) -> None:
        """Raise envfault.Fault when a check's runner is missing on this host: the batch is held,
        not failed (push_phase)."""
        if fault := envfault.precheck(cmds, self.wt, env):
            raise envfault.Fault(fault)

    def _check(self, head: str, checks: list[str]) -> tuple[str, str] | None:
        """Check `head` in the worktree: None when it passes, else (command, output tail). The stale
        version guard (push.stale_versions, with `delivery.plugin_version_guard` on) comes first and
        runs no command. A check whose failing tests pass on one rerun at `head` passes (_flaky)."""
        self._checkout(head)
        stale = push.stale_versions(self.wt, self.tip) if push.version_guard(self.d) else []
        if stale:
            msg = (f"{', '.join(stale)}: this change edits the plugin but keeps the version already on "
                   f"{self.target}; bump it past that (in every manifest, plus a changeset where the "
                   "repository wants one), commit, then rerun")
            say(f"{head[:10]}: {msg}")
            return STALE, msg
        if not checks:
            return None
        try:
            todo, skipped = push.applicable(self.wt, head, checks, say, self.tip or "")
        except push.ScopeError as e:
            say(f"{head[:10]}: {e}")
            return str(e.check), str(e)
        for line in skipped:
            if line not in self.checks.setdefault("skipped", []):
                self.checks["skipped"].append(str(line))
        if not todo and push.outside_scope(checks, skipped):
            say(f"{head[:10]}: {push.OUT_OF_SCOPE}")
            return None
        if not todo:
            say(f"{head[:10]}: {push.NONE_APPLY}")
            return push.NONE_APPLY, ""
        say(f"checking {head[:10]}")
        self.checks["runs"] += 1
        env = push.check_env("target", self.tip, self.check_run_dir)
        tree, run = pushpass.tree_of(self.wt, head), []
        for cmd in todo:
            parts = pushpass.env_parts(self.wt, cmd, env, self.tip or "")
            eid = pushpass.digest(parts)
            if hit := pushpass.passed(self.p, tree, str(cmd), eid):
                say(f"reused pass from {hit.get('batch') or '?'} (tree {tree[:10]}): {cmd}")
                self.checks["reused"] = self.checks.get("reused", 0) + 1
            else:
                if why := pushpass.missed(self.p, tree, str(cmd), parts):
                    say(f"not reusing a pass: {why}: {cmd}")
                run.append((cmd, eid, parts))
        if not run:
            return None
        self._runners([c for c, _, _ in run], env)
        suite_out: list[str] = []
        slot = push.check_slot(head, say, checkcap.label(self.p.name, f"push batch {self.id}"),
                               priority=push.priority_key(self.repo, self.remote, self.branch), proj=self.p)
        started = time.time()   # a wait for the host-wide slot is not check time
        slow = Path(self.check_run_dir) / slowtests.FILE if self.check_run_dir else None
        if slow:
            slow.unlink(missing_ok=True)   # this head's run only
        try:
            for line in push.sandbox_lines([cmd for cmd, _, _ in run]):
                say(line)
            for cmd, eid, parts in run:
                resume = pushpass.resume_file(self.p, tree, str(cmd), eid) if tree and pushpass.resumable(str(cmd)) else None
                extra = pushpass.resume_env(resume, env) if resume else {}
                if slow and pushpass.resumable(str(cmd)) and not push.remote_host(cmd):
                    extra |= slowtests.env(slow, {**env, **extra})
                if done := pushpass.resumed(resume) if resume else []:
                    say(f"resuming {cmd}: {len(done)} test file(s) passed in an earlier run on tree {tree[:10]}; running the rest")
                if push.remote_host(cmd):
                    say(f"running {cmd}{push.where(cmd)}")
                rc, tail, _ = scriptwatch.rerun(
                    lambda: _stream(push.check_argv(cmd, self.wt), self.wt,
                                    env={**env, **extra, **slot.env()}, pass_fds=checkdirs.inherited()),
                    lambda r: r[0] != 0, None if push.remote_host(cmd) else str(cmd), self.wt, env, say, on_pass=True)
                suite_out.append(tail)
                if fault := push.remote_fault(cmd, rc, self.wt):
                    raise envfault.Fault(fault)   # the check host, not this head: push_phase holds the batch
                if rc != 0:
                    say(f"check failed on {head[:10]}: {cmd}{push.where(cmd)}")
                    self.fail_rc[tail] = rc
                    if self._flaky(head, cmd, rc, tail, env, slot.env()):
                        if resume:
                            resume.unlink(missing_ok=True)
                        continue
                    return cmd, tail
                if why := pushpass.record(self.p, tree, str(cmd), eid, self.id, parts):
                    say(f"could not record the pass of {cmd}: {why}")
                if resume:
                    resume.unlink(missing_ok=True)
        finally:
            slot.close()
            self.checks["seconds"] = round(self.checks["seconds"] + time.time() - started, 1)
        push.record_check_s(self.p, time.time() - started)
        for line in slowtests.lines(slow) if slow else []:
            say(line)
        try:
            suitecost.record(self.p.db, self.id, head, self._version(head), "\n".join(suite_out))
        except Exception as e:   # a cost note never fails a batch
            say(f"suite cost not recorded: {e}")
        return None

    def _rerun_ids(self, cmd, rc: int | None, tail: str, where: str) -> list[str]:
        """The failing pytest ids of a failed check to rerun once at `where` ("head" or "tip"): none when
        the check is not one plain local pytest run (pushpass.resumable), it did not exit 1 (tests
        failed; `rc` None: not known) or pytest stopped early (-x, --maxfail, an interrupt:
        sharedchecks.stopped_early, so later tests never ran), its list of failing ids is incomplete
        (sharedchecks.failing_ids), it names more than RERUN_IDS, or one of them was rerun there in this
        batch already."""
        if cmd == STALE or push.remote_host(cmd) or not pushpass.resumable(str(cmd)):
            return []
        if rc not in (1, None) or sharedchecks.stopped_early(tail):
            return []
        ids, complete = sharedchecks.failing_ids(tail)
        if not complete or not ids or len(ids) > RERUN_IDS or self.reran[where] & set(ids):
            return []
        return ids

    def _run_ids(self, at: str, cmd, ids: list[str], where: str, env: dict, slot_env: dict) -> tuple[int | None, str]:
        """Run only `ids` of check `cmd` once at commit `at`: the check's own command, its other tests
        deselected by name (pushpass.select_env), killed after delivery.push_rerun_s. (pytest's exit
        code, None when it ran out of time; the output tail)."""
        self.reran[where].update(ids)
        self._checkout(at)
        rx = "^(?:" + "|".join(re.escape(n) for n in sorted({i.rpartition("::")[2] for i in ids})) + ")$"
        self.checks["reruns"] = self.checks.get("reruns", 0) + 1
        rc, tail, late = _stream(push.check_argv(cmd, self.wt), self.wt,
                                 env={**env, **pushpass.select_env(rx, env), **slot_env},
                                 timeout=_seconds(self.d.get("push_rerun_s"), RERUN_S), group=True,
                                 pass_fds=checkdirs.inherited())
        return (None if late else rc), tail

    def _flaky(self, head: str, cmd, rc: int, tail: str, env: dict, slot_env: dict) -> bool:
        """A failed check's failing tests (_rerun_ids) run once more at the same `head`. True when they all
        pass: the failure was flaky. It is recorded (the marker's flaky_tests: ids, head, command; the
        queue tells the coordinator once per test) and the check counts as passed in this batch only: no
        pass is recorded for the tree. A test that fails again, or a rerun that runs out of time, keeps
        the failure."""
        ids = self._rerun_ids(cmd, rc, tail, "head")
        if not ids:
            return False
        say(f"rerunning the {len(ids)} failing test(s) once on {head[:10]}: {' '.join(ids)}")
        rc, _ = self._run_ids(head, cmd, ids, "head", env, slot_env)
        if rc != 0:
            say(f"the rerun on {head[:10]} " + ("ran out of time" if rc is None else "failed too") + ": the failure stands")
            return False
        self.flaky.append({"ids": ids, "head": head, "cmd": str(cmd)})
        self.checks["flaky"] = True
        say(f"they passed on the rerun: flaky, the batch goes on: {' '.join(ids)}")
        return True

    def _tip_ids(self, tip: str, fail: tuple) -> tuple[str, str] | bool | None:
        """The search is about to blame an entry for a failed check: rerun only its failing tests
        (_rerun_ids) on `tip`, without the batch, up to TIP_RERUNS times. (cmd, tail) when one rerun fails
        there (the failure is the tip's own, flaky or already failing, not the entry's), True when every
        rerun passes or the tests are not on the tip (the entry's own failure), None when that cannot tell
        (no such ids, a rerun out of time or one pytest ends with neither). The batch log and the
        marker's checks["tip_reruns"] keep the counts."""
        cmd = fail[0]
        ids = self._rerun_ids(cmd, self.fail_rc.get(fail[1]), fail[1], "tip")
        if not ids:
            return None
        say(f"rerunning the {len(ids)} failing test(s) on the tip {tip[:10]} alone, up to {TIP_RERUNS} times: "
            f"{' '.join(ids)}")
        env = push.check_env("target", self.tip, self.check_run_dir)
        self._runners([cmd], env)
        slot = push.check_slot(tip, say, checkcap.label(self.p.name, f"push batch {self.id}"),
                               priority=push.priority_key(self.repo, self.remote, self.branch), proj=self.p)
        try:
            for n in range(1, TIP_RERUNS + 1):
                rc, tail = self._run_ids(tip, cmd, ids, "tip", env, slot.env())
                self.checks["tip_reruns"] = self.checks.get("tip_reruns", 0) + 1
                if rc == 1:
                    say(f"they failed on the tip {tip[:10]} on rerun {n} of {TIP_RERUNS}")
                    self.tip_runs = n
                    return str(cmd), tail
                if rc == NO_TESTS:
                    say(f"they are not on the tip {tip[:10]}")
                    return True
                if rc != 0:
                    say(f"rerun {n} on the tip {tip[:10]} " + ("ran out of time" if rc is None else f"exited {rc}"))
                    return None
        finally:
            slot.close()
        say(f"they passed {TIP_RERUNS} of {TIP_RERUNS} reruns on the tip {tip[:10]}")
        return True

    def _caps(self, head: str, checks: list[str], plain: str) -> tuple[str, str] | bool | None:
        """A cap probe of `head`: only the prompt size cap tests (delivery.push_cap_tests) of each check
        that is one plain pytest run (pushpass.resumable), seconds instead of the whole suite. None when
        they pass, (command, output tail) when they fail only on cap tests (conflictfix.cap_failure),
        False when it cannot tell (no such check, a stale version, another failure): the full checks
        decide then. Each probe is recorded on the tree of `plain`, the replayed entries before their
        bump, and one known there is not run again (pushpass.probe_known), also after a reboot."""
        rx = self.d.get("push_cap_tests") or conflictfix.CAP_TESTS
        try:
            re.compile(rx)
        except (re.error, TypeError):
            rx = conflictfix.CAP_TESTS
        if not checks:
            return False
        self._checkout(head)
        if push.version_guard(self.d) and push.stale_versions(self.wt, self.tip):
            return False
        try:
            todo, _ = push.applicable(self.wt, head, checks, lambda _m: None, self.tip or "")
        except push.ScopeError:
            return False
        todo = [c for c in todo if pushpass.resumable(str(c))]
        if not todo:
            return False
        env = push.check_env("target", self.tip, self.check_run_dir)
        self._runners(todo, env)
        tree, slot, started = pushpass.tree_of(self.wt, plain), None, time.time()
        try:
            for cmd in todo:
                label = f"caps {rx}: {cmd}"
                eid = pushpass.env_id(self.wt, cmd, env, self.tip or "")
                if hit := pushpass.probe_known(self.p, tree, label, eid):
                    ok, tail = bool(hit.get("ok")), str(hit.get("tail") or "")
                    say(f"cap probe of {head[:10]} {'passed' if ok else 'failed'} in {hit.get('batch') or '?'} "
                        f"(tree {tree[:10]}); not run again: {cmd}")
                    self.checks["cap_reused"] = self.checks.get("cap_reused", 0) + 1
                else:
                    if slot is None:
                        slot = push.check_slot(head, say, checkcap.label(self.p.name, f"push batch {self.id}"),
                                               priority=push.priority_key(self.repo, self.remote, self.branch), proj=self.p)
                        started = time.time()
                    say(f"cap probe of {head[:10]}: {cmd}")
                    rc, tail, _ = _stream(push.check_argv(cmd, self.wt), self.wt,
                                          env={**env, **pushpass.select_env(rx, env), **slot.env()},
                                          pass_fds=checkdirs.inherited())
                    if fault := push.remote_fault(cmd, rc, self.wt):
                        raise envfault.Fault(fault)
                    ok = rc in (0, NO_TESTS)
                    self.checks["cap_probes"] = self.checks.get("cap_probes", 0) + 1
                    if why := pushpass.probe_record(self.p, tree, label, eid, ok, tail, self.id):
                        say(f"could not record the cap probe of {cmd}: {why}")
                if not ok:
                    return (str(cmd), tail) if conflictfix.cap_failure(tail, rx) else False
        finally:
            if slot is not None:
                slot.close()
                self.checks["cap_seconds"] = round(self.checks.get("cap_seconds", 0.0) + time.time() - started, 1)
        return None

    def _judge(self, tip: str, carried: list, heads: dict, checks: list[str]):
        """Check the full head; when it fails, find the first failing entry by a binary search over
        prefixes (each prefix: entries 1..k plus a bump), checking the tip alone only when prefix 1
        fails. Returns (entries to push, head to push or None, (cmd, tail) of the blamed entry's
        failure or None). When every prefix passes and the full head passes on a rerun, the failure
        was flaky: the full head goes. A failing check whose failing pytest ids pass on one rerun at the
        same head passes there (_flaky). Before an entry is blamed, its failing ids are rerun on the tip
        alone up to TIP_RERUNS times (_tip_ids). For entry 1: failing there once, the tip is broken
        (tip_failed); passing every time, entry 1 is blamed; only when that cannot tell is the tip checked
        alone in full. For a later entry: failing there once, the failure is pre-existing (_pre_existing):
        the prefix that passed goes and nothing is blamed; else it is blamed as before.

        A tip the queue knows fails on its own (the marker's "bad_tip", pushq._known_bad) is never
        checked alone again: no prefix passes unless it fixes the tip. The prefix of entries not yet
        tried on that tip ("fresh") is checked first; when it passes, it fixed the tip and the search
        runs above it, else inside it, as a fix may come before an entry that breaks the checks. A
        search that ends at prefix 1 failing shows only that entry 1 does not fix the tip: the batch
        ends tip_failed and names the entries up to it ("tip_tried"), so the later ones stay untried
        and go first in the next batch. A batch with none untried ends at once and names none.

        A combined head gets a cap probe first (_caps). When it fails only on prompt size cap tests,
        the search runs on cap probes alone, then one full check of the longest prefix that passed
        them: the entry that breaks a cap goes back to be trimmed (pushq's trim requeue) without a full
        check of any head that breaks one. A full failure of that prefix searches below it in full."""
        m = len(carried)
        known = self.m.get("bad_tip") if isinstance(self.m.get("bad_tip"), dict) else {}
        bad = known.get("tip") == tip
        lo, hi = 0, m
        capped = not bad and m > 1 and isinstance(cap := self._caps(heads[m], checks, carried[m - 1][1]), tuple)
        if capped:
            fails = {m: cap}
            say(f"{heads[m][:10]} breaks a prompt size cap; searching on the cap tests alone")
            while hi - lo > 1:
                mid = (lo + hi) // 2
                heads[mid] = self._head(tip, carried, mid)
                f = self._caps(heads[mid], checks, carried[mid - 1][1])
                if f is None:
                    lo = mid
                elif isinstance(f, tuple):
                    hi, fails[mid] = mid, f
                else:
                    break
            # A cap probe that passed is no full pass: below hi the full checks decide.
            if hi - lo == 1 and lo:
                say(f"checking the {lo} entr{'y' if lo == 1 else 'ies'} before the one that breaks the cap in full")
                f = self._check(heads[lo], checks)
                if f is None:
                    return lo, heads[lo], fails[hi]
                hi, fails[lo] = lo, f
            lo = 0
        else:
            fail = self._check(heads[m], checks)
            if fail is None:
                return m, heads[m], None
            fails = {m: fail}
        if bad and fails[m][0] != STALE:
            fresh = sum(1 for i, _ in carried if i < int(known.get("fresh") or 0))
            if not fresh:
                return self._tip_red(tip, known)
            if fresh < m:
                say(f"checking the {fresh} entr{'y' if fresh == 1 else 'ies'} not yet tried on the failing tip {tip[:10]}")
                heads[fresh] = self._head(tip, carried, fresh)
                if (f := self._check(heads[fresh], checks)) is None:
                    lo = fresh
                else:
                    hi, fails[fresh] = fresh, f
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
            self.tip_tried = carried[0][0] + 1   # entry 1, and those before it that carried nothing
        if hi == 1 and bad:
            return self._tip_red(tip, known)
        if hi == 1 and fails[1][0] != STALE and conflictfix.cap_failure(fails[1][1], self.d.get("push_cap_tests")) \
                and self._caps(tip, checks, tip) is None:
            say(f"entry 1 breaks a prompt size cap the tip {tip[:10]} keeps (its cap tests alone)")
            return 0, None, fails[1]
        if hi == 1 and (tf := self._tip_ids(tip, fails[1])) is not None:
            if tf is True:
                say(f"they pass on the tip {tip[:10]}: the failure is entry 1's own")
                return 0, None, fails[1]
            self.message = f"the tip of {self.target} fails its checks on its own: {tf[0]} (its failing tests rerun on it)"
            say(self.message)
            self.detail_tip = {"cmd": tf[0], "tail": tf[1]}
            return 0, None, None
        if hi == 1:
            say(f"checking the tip {tip[:10]} alone")
            if (tf := self._check(tip, checks)) is not None:
                self.message = f"the tip of {self.target} fails its checks on its own: {tf[0]}"
                say(self.message)
                self.detail_tip = {"cmd": tf[0], "tail": tf[1]}
                return 0, None, None
            self.tip_passed = True
            return 0, None, fails[1]
        if hi == m and fails[m][0] != STALE:
            say(f"every prefix passed; checking {heads[m][:10]} again")
            f = self._check(heads[m], checks)
            if f is None:
                self.checks["flaky"] = True
                say("the full head passed on the rerun: the failure was flaky")
                return m, heads[m], None
            fails[m] = f
        # A tip the queue knows fails is not rerun: the prefix that passed fixed it, the entry may break it again.
        if not bad and fails[hi][0] not in (STALE, push.NONE_APPLY) \
                and isinstance(tf := self._tip_ids(tip, fails[hi]), tuple):
            return self._pre_existing(tip, carried, hi, tf), heads[hi - 1], None
        return hi - 1, heads[hi - 1], fails[hi]

    def _pre_existing(self, tip: str, carried: list, hi: int, tf: tuple[str, str]) -> int:
        """Carried entry `hi` (not the first) failed a check whose failing tests fail on `tip` alone too
        (_tip_ids): the failure is the tip's own, flaky or already failing. The entry is not blamed: the
        prefix before it (which passed) goes, it and the later ones are requeued, and the failure is
        recorded (the marker's pre_existing; the queue tells the coordinator once per test)."""
        entry = self.entries[carried[hi - 1][0]].get("id")
        ids = sharedchecks.failing_ids(tf[1])[0]
        self.pre_existing.append({"ids": ids, "tip": tip, "cmd": tf[0], "entry": entry,
                                  "runs": self.tip_runs, "of": TIP_RERUNS})
        say(f"entry {entry} is not sent back: its failing tests fail on the tip {tip[:10]} alone too "
            f"(pre-existing); the {hi - 1} entr{'y' if hi == 2 else 'ies'} before it go, it and the later ones are requeued")
        return hi - 1

    def _rescue(self, tip: str, carried: list, k: int, checks: list[str]) -> str | None:
        """After _judge blamed carried entry k+1: replay the later carried entries onto the first k
        (the tip when k is 0) and check that head once, in this batch. Returns the head that pushes the
        first k entries and the replayed ones (self.rescued), or None (nothing after the blamed entry,
        a replay that conflicts or adds nothing, or a failing check: those entries are requeued). A
        batch makes at most RESCUES such checks, whatever their outcome."""
        later = carried[k + 1:]
        if not later or self.detail_tip is not None or self.rescues >= RESCUES:
            return None
        h = carried[k - 1][1] if k else tip
        replayed: list[tuple[int, str]] = []
        for i, _ in later:
            self._settling = ([], set())
            new, files = self._rebase(h, self.info[i]["top"])
            if files is not None or new == h:
                break
            self.settled.pop(i, None)
            if self._settling[0]:
                self.settled[i] = {"settled": sorted(set(self._settling[0])), "settled_by": sorted(self._settling[1])}
            replayed.append((i, new))
            h = new
        if len(replayed) < len(later):
            say(f"the entries after {self.entries[carried[k][0]].get('id')} do not replay without it; requeued")
            return None
        say(f"checking the {len(replayed)} entr{'y' if len(replayed) == 1 else 'ies'} after "
            f"{self.entries[carried[k][0]].get('id')} without it")
        try:
            head = self._head(tip, carried[:k] + replayed, k + len(replayed))
        except Refused:
            return None
        self.rescues += 1
        self.retried = True
        if len(carried[:k] + replayed) > 1 and isinstance(self._caps(head, checks, replayed[-1][1]), tuple):
            say("they break a prompt size cap too; requeued")
            return None
        if self._check(head, checks) is not None:
            say("they fail too; requeued")
            return None
        self.rescued = dict(replayed)
        return head

    def _tip_red(self, tip: str, known: dict) -> tuple[int, None, None]:
        """tip_failed for a tip known to fail on its own, without checking it alone again."""
        self.message = (f"the tip of {self.target} fails its checks on its own (known from earlier batches, "
                        f"not checked alone again), and no entry here fixes it")
        say(self.message)
        self.detail_tip = {"cmd": known.get("cmd") or "the push checks",
                           "tail": f"{tip[:10]} failed its checks on its own in earlier batches"}
        return 0, None, None


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
    sha = m.get("pushed_sha")
    if m.get("outcome") != "pushed" or not sha:
        return {"status": "skipped", "reason": "nothing was pushed"}
    if why := push.scalar_problem(d.get("after_push")):   # malformed (true, 5): a failure, never a quiet skip
        say(f"after_push: delivery.after_push: {why}")
        now = time.time()
        return {"status": "failed", "exit": None, "cmd": None, "started": now, "ended": now,
                "tail": f"delivery.after_push: {why}"}
    cmds = push.check_list(d.get("after_push"))
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
    worktree.prepare(p, wt, repo)
    tasks = []
    for res in m.get("results") or []:
        t = res.get("task")
        if res.get("status") == "pushed" and t is not None and str(t) not in tasks:
            tasks.append(str(t))
    env = {**command_env(p), "TTP_PUSHED_SHA": sha, "TTP_PUSHED_VERSION": str(m.get("version") or ""),
           "TTP_PUSH_TARGET": str(m.get("target") or ""), "TTP_PUSH_TASKS": ",".join(tasks),
           "TTP_PUSH_BATCH" if m.get("kind") == "batch" else "TTP_PUSH_RECEIPT": str(marker)}
    # Deploy commands have no check lifetime guard and use the supervisor's ordinary temp dir.
    for key in ("TTP_CHECKS_TMP_FD", "TTP_CHECK_OUTPUT_DIR"):
        env.pop(key, None)
    out = {"status": "ok", "exit": 0, "started": started, "tail": ""}
    try:
        for cmd in cmds:
            try:
                why = push.skip_reason(wt, sha, cmd, str(m.get("tip") or ""))
            except push.ScopeError as e:
                say(f"after_push: {e}")
                out.update(status="failed", exit=None, cmd=cmd, tail=str(e))
                break
            if why:
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


RECEIPTS = "after_push"   # under the project's state: one receipt per after_push of an ordinary push


def after_ordinary_push(p: Project, repo: Path, sha: str, target: str, tip: str = "") -> dict | None:
    """Run `delivery.after_push` after an ordinary or detached `ttp push` that pushed `sha` to
    `target` (a batch runs its own). A receipt under the project's state records it before it starts
    and how it ended; the daemon turns it into an event, and an alert when it failed
    (pushq.tend_receipts). Its failure never changes the push. None when after_push is not set; a
    malformed one is recorded as a failure, so it alerts."""
    if not push.after_set(p):
        return None
    rid = f"p{time.strftime('%Y%m%dT%H%M%S')}-{sha[:10]}-{secrets.token_hex(2)}"
    folder = p.state / RECEIPTS
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / f"{rid}.json"
    task = os.environ.get("TTP_TASK") or ""
    m = {"v": 1, "kind": "push", "id": rid, "status": "running", "outcome": "pushed", "pushed_sha": sha,
         "target": target, "tip": tip, "repo": str(repo), "task": task, "pid": os.getpid(), "boot": runner.boot_id(),
         "started": time.time(), "results": [{"task": task, "status": "pushed"}] if task else []}
    try:
        m["version"] = push.pushed_version(p, repo, sha)
    except Exception:      # an unreadable version leaves TTP_PUSHED_VERSION empty; the deploy still runs
        m["version"] = None
    if why := _notice_hold(p):    # the daemon runs it once the notice is gone (pushq.tend_receipts)
        say(f"after_push held: {why}; the daemon runs it once the notice is gone")
        m.update(status="held", after_held={"at": time.time(), "why": why})
        write_json(receipt, m)
        return {"status": "held", "reason": why, "receipt": str(receipt)}
    lock = locks.try_take([_after_lock(p, rid)], f"after_push of {sha[:10]}", "ttp push")
    write_json(receipt, m)
    try:
        try:
            after = after_push(p, receipt, m)
        except Exception as e:     # recorded as a failure; the push stands
            say(f"after_push: {type(e).__name__}: {e}")
            after = {"status": "failed", "exit": None, "cmd": None, "tail": f"{type(e).__name__}: {e}",
                     "started": m["started"], "ended": time.time()}
        m.update(after_push=after, status="finished", ended=time.time())
        write_json(receipt, m)     # before the lock goes: running with a free lock reads as killed
    finally:
        if lock is not None:
            lock.close()
    _forget(_after_lock(p, rid))
    return {**after, "receipt": str(receipt)}


def _notice_hold(p: Project) -> str:
    """The line of a power cycle of this host a device broker announced, or "" (cyclenotice.py)."""
    if not push.after_set(p):
        return ""
    h = cyclenotice.held(cyclenotice.project_paths(p))
    return cyclenotice.line(h, p) if h else ""


def _deploy_held(p: Project, marker: Path, m: dict, lock_path: Path, lock) -> bool:
    """While a power cycle is announced, after_push waits: the marker records `after_held` and the lock goes,
    so the daemon (pushq.finalize) resumes it once the notice is gone, without spending a resume."""
    why = _notice_hold(p)
    if not why:
        return False
    say(f"after_push held: {why}; the daemon runs it once the notice is gone")
    m.update(after_held={"at": time.time(), "why": why})
    write_json(marker, m)
    _forget(lock_path)
    lock.close()
    return True


def rerun_argv(p: Project, receipt: Path) -> list[str]:
    """The process that runs an ordinary push's held or reboot-cut after_push (rerun_receipt); tests stub it."""
    return [sys.executable, "-c", "import sys; from ttp import batch; from ttp.project import Project; "
            "sys.exit(batch.rerun_receipt(Project(sys.argv[1]), sys.argv[2]))", str(p.base), str(receipt)]


def rerun_receipt(p: Project, receipt) -> int:
    """Run the after_push a receipt names once more (pushq.tend_receipts started this): held again while
    a power cycle is still announced."""
    receipt = Path(receipt)
    m = push._read(receipt)
    rid = str(m.get("id") or receipt.stem)
    lock = locks.try_take([_after_lock(p, rid)], f"after_push of {str(m.get('pushed_sha') or '')[:10]} (again)",
                          "the push queue")
    if lock is None:
        return push.BUSY
    try:
        m = push._read(receipt) or m
        if m.get("status") not in ("held", "rerun"):
            return 0      # another process ran it meanwhile
        if why := _notice_hold(p):
            m.update(status="held", after_held={"at": time.time(), "why": why})
            write_json(receipt, m)
            return 0
        m.update(status="running", pid=os.getpid(), boot=runner.boot_id(), resumed=time.time())
        m.pop("after_held", None)
        write_json(receipt, m)
        try:
            after = after_push(p, receipt, m)
        except Exception as e:
            after = {"status": "failed", "exit": None, "cmd": None, "tail": f"{type(e).__name__}: {e}",
                     "started": m.get("resumed"), "ended": time.time()}
        m.update(after_push=after, status="finished", ended=time.time())
        write_json(receipt, m)
    finally:
        lock.close()
    _forget(_after_lock(p, rid))
    return 0


def _finish(p: Project, marker: Path, m: dict, after: dict, lock) -> None:
    m.update(after_push=after, phase="finished", status="finished", ended=time.time())
    write_json(marker, m)
    _forget(_after_lock(p, str(m.get("id") or marker.stem)))
    if lock is not None:
        lock.close()
    if m.get("check_run_dir"):
        checkdirs.release(Path(m["check_run_dir"]))


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
        b.check_run_dir = str(checkdirs.reports(checkdirs.restore(p.state, b.check_run_dir)))
        if os.environ.get("TTP_RUN_DIR") != b.check_run_dir:
            raise OSError("TTP_RUN_DIR is missing or differs from the queued check output directory")
        outcome = b.push_phase()
    except Exception as e:      # recorded, never left without an outcome
        outcome = b.error(e)
    ff = []
    if b.pushed_sha and (also := push.fast_forward_list(b.d.get("fast_forward_also"))):
        try:                    # still under the target's push lock; a failure never undoes the push
            ff = push.fast_forward(b.repo, b.remote, b.pushed_sha, also, b.branch, say=say)
        except Exception as e:
            ff = [f"not ff {x}: {type(e).__name__}: {e}" for x in also]
    if b.pushed_sha and (mirrors := push.mirror_list(b.d.get("mirror_remotes"))):
        ff += push.mirror_safely(b.repo, b.pushed_sha, mirrors, f"{b.remote}/{b.branch}", say=say)
    # The reach check comes before the outcome is written: finalize settles a marker as soon as it has
    # one, so a later reach would miss the review's summary. Its fetch is bounded and it never raises.
    if outcome in ("pushed", "landed"):
        try:
            b.reach = push.reach(p, b.repo, b.target, b.pushed_sha or b.tip)
        except Exception as e:      # a fact for the notice; never in the way of the outcome
            say(f"cannot tell whether it is on the branch it is meant to reach: {type(e).__name__}: {e}")
        if b.reach:
            say(push.landing(b.pushed_sha or b.tip, b.target, b.reach)
                if b.pushed_sha else f"already on {b.target}{push.reach_words(b.reach)}")
    m = push._read(marker) or m
    conflicts = b.conflicts()
    m.update(outcome=outcome, tip=b.tip, pushed_sha=b.pushed_sha, version=b.version, results=b.results(),
             checks=b.checks, rounds=b.rounds, message=b.message or None, conflicts=conflicts,
             reach=b.reach or None, scope_held=b.scopes or None, flaky_tests=b.flaky or None,
             pre_existing=b.pre_existing or None)
    if ff:
        m["fast_forward"] = ff
    if conflicts["conflicted"]:
        say(f"conflicts: {conflicts['conflicted']} of {conflicts['entries']} entries, {conflicts['auto_resolved']} "
            f"settled without judgment ({conflicts['by_hunks']} by changes of different lines), "
            f"{conflicts['sent_back']} sent back")
    if outcome == "tip_failed":
        m["tip_check"] = b.detail_tip
        m["tip_tried"] = [e.get("id") for e in b.entries[:b.tip_tried]]
    if b.tip_passed:
        m["tip_passed"] = True
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
    if ap_lock is not None and outcome == "pushed" and _deploy_held(p, marker, m, ap_path, ap_lock):
        return EXIT.get(outcome, 1)
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
    done = m.get("after_push") if isinstance(m.get("after_push"), dict) and m["after_push"].get("ended") else None
    if not done and m.get("outcome") == "pushed" and _deploy_held(p, marker, m, ap_path, lock):
        return 0
    say(f"resuming the after_push of batch {bid} ({m.get('outcome')})")
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


def summary(marker: Path, pushed_only: bool = False) -> int:
    """`ttp push --result <marker>` for a batch marker: 0 once it finished (or died), 1 while it runs.
    With `pushed_only`: 0 only when it pushed or landed, `push.NOT_PUSHED` when it finished without."""
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
        return push.NOT_PUSHED if pushed_only else 0
    sha = m.get("pushed_sha")
    head = f"batch {bid}: {outcome}" + (f", {str(sha)[:10]}" if sha else "") \
        + (f" as {m['version']}" if sha and m.get("version") else "") + f" to {m.get('target')}" \
        + (push.reach_words(m.get("reach")) if outcome in ("pushed", "landed") else "")
    print(f"ttp push: {head}" + (f" ({m['message']})" if m.get("message") and outcome != "pushed" else ""))
    for line in m.get("fast_forward") or []:
        print(f"  {'warning: ' if push.ff_warnings([line]) else ''}{line}")
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
    return push.NOT_PUSHED if pushed_only and outcome not in ("pushed", "landed") else 0

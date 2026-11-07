# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The push queue's batch process, `ttp push --batch <marker>`. It calls no model.

The daemon writes a marker naming the reviewed changes to push (entries: a branch and its reviewed
head), takes the batch's run lock and starts this process, which inherits the lock. The process
replays the entries in order onto the target's tip in a worktree of its own (`worktrees/push`) and
settles the conflicts that need no judgment: version lines, both sides adding different lines at
one spot, and both sides changing different lines that git calls a conflict only because they are
adjacent. The marker counts the entries that conflicted, were settled and were sent back. It bumps the version once for all entries, runs the push checks once on the result and
pushes it without force. It always runs them itself; it skips only a check a batch recorded passing on the
same tree, command and environment (pushpass), one carried from another tip, or a task's own full `ttp checks`
pass of the push checks on the same change (pushpass.task_carriable) where the patch is unchanged and nothing the
check covers moved besides the batch's version bump (_task_carried). When the checks fail, a binary search over prefixes finds the first failing
entry, and the passing prefix is pushed. A failed check is judged as `ttp checks` judges one
(gatejudge): its failing pytest ids not yet judged at that head are rerun once there, those failing
again on the tip alone (the base, this host) up to TIP_RERUNS times; a base result saved for the tip
counts without a rerun, and a quarantined test the batch does not touch is excused at once. A check
none of whose failing tests is new (flaky: passed on the rerun, then quarantined; pre-existing: failed
on the tip too; quarantined) passes, recorded and reported once, and the batch goes on. The search
probes each prefix with only the new failing tests, then checks the prefix those passed in full once
before it is pushed. The outcome goes into the marker per entry; the daemon applies it to the reviews.

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

import ast
import difflib
import fcntl
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
from collections import Counter, deque
from pathlib import Path

from . import (checkcap, checkdirs, cleantree, conflictfix, cyclenotice, envfault, gatejudge, landmark, locks, push, pushpass, runner, scopehold,
               scriptwatch, setupfault, syntaxfault, testslots, workerloss, urgent,
               sharedchecks, slowtests, suitecost, verdictcarry, worktree)
from .project import Project, command_env, durable_write, write_json
from . import gitmemo

WORKTREE = "push"             # the batch's own checkout under the project's worktrees (never t<id>)
AFTER_PUSH = "after_push-"    # + batch id: the temporary checkout after_push runs in
DEFAULT_AFTER_PUSH_TIMEOUT_S = 1800
STALE = "(stale plugin version)"   # the `cmd` of a check failure that is push.stale_versions
SETTLED_WORDS = {"version": "version lines", "added": "lines both sides added",
                 "hunks": "changes of different lines", "taken_in": "a change the other side holds",
                 "imports": "names both sides import", "release": "release notes both sides wrote",
                 "cells": "different cells of a table row",
                 "counters": "a test's size cap both sides raised",
                 "union": "lines both sides added to a shared doc (delivery.mechanical_merge_paths)"}
RELEASE_KINDS = {"version", "release"}   # settlements in the shared release files only (release_kind)
MERGED_KINDS = {"added", "hunks", "imports", "cells", "counters", "union"}   # settlements that keep both sides' changes: kept only when the checks pass
# delivery.mechanical_merge_paths default: shared docs and changesets, whose conflicts settle_union
# settles by keeping the lines both sides added (fnmatch on the repo path, so `*` also crosses `/`).
MECHANICAL_PATHS = ("docs/*.md", "*/docs/*.md", "*README.md", "*CHANGELOG.md", "*.changeset/*.md")
REPLAYED = "Ttp-Replayed-From"     # trailer of a commit the batch settled: the entry commit it replays
# sequence.editor of a rebase that leaves out an entry's commits already on the target (Batch._landed):
# drops each todo `pick` whose commit one of the shas before the todo file's path starts with
DROP_TODO = ("import sys\nf, drop = sys.argv[-1], sys.argv[1:-1]\nkeep = [ln for ln in open(f).readlines() "
             "if not (ln.split()[:1] in (['pick'], ['p']) and len(ln.split()) > 1 and len(ln.split()[1]) >= 7 "
             "and any(d.startswith(ln.split()[1]) for d in drop))]\nopen(f, 'w').writelines(keep)\n")
# that rebase's own settings, whatever the user's: no todo reordering, no check of the dropped lines,
# and no other branch moved along (rebase.updateRefs would move a task branch the replay passes)
DROP_CONFIG = ("-c", "rebase.autoSquash=false", "-c", "rebase.missingCommitsCheck=ignore", "-c", "rebase.updateRefs=false")
RESCUES = 1                        # checks per batch of the entries after a blamed one, replayed without it
NO_TESTS = 5                       # pytest's exit status when nothing was selected: a cap probe of a check without cap tests
RERUN_S = 600.0                    # delivery.push_rerun_s: the time limit of one rerun of a failed check's failing tests
YIELD_GRACE_S = 300.0              # delivery.push_yield_grace_s: an urgent approval stops a batch only if approved this soon after its checks began
RERUN_IDS = 10                     # a check failing more tests than this is not rerun: that is no flake
TIP_RERUNS = 3                     # reruns on the tip alone of a blamed failure's failing tests: one failing clears the entry
HOST_FAULTS = (envfault.Fault, workerloss.Infra, syntaxfault.Infra, setupfault.Infra)   # the host's, never an entry's
CARRY_TRIES = 5                    # recorded passes of one check on other tips tried for a carry (verdictcarry)
OVER = re.compile(r"^E\s+assert (\d[\d_]*) <= (\d[\d_]*)\s*$")   # pytest's size-over-cap line
SECTION = re.compile(r"^_{3,} (\S.*?) _{3,}$")                     # pytest's per-test failure header
OVER_KEPT = 10                     # overshoot lines a check's tail keeps from its whole output
TOP_DEF = re.compile(r"^(?:async[ \t]+def|def|class)[ \t]+([A-Za-z_]\w*)", re.M)
LEAD_KEY = re.compile(r"""["']?([A-Za-z_][\w.-]*)["']?[ \t]*[:=](?![=/])""")   # `"key":`, `key =`, `key:`
TOP_START = re.compile(r"(?:@|(?:async[ \t]+)?def[ \t]|class[ \t])")
EXIT = {"pushed": 0, "landed": 0, "nothing": 0, "busy": push.BUSY, "env_fault": push.BUSY, "moved": push.KEPT_MOVING,
        "worker_loss": push.BUSY, "script_torn": push.BUSY, "setup_fault": push.BUSY, "rejected": push.REJECTED, "refused": push.REFUSED, "tip_failed": push.CHECKS_FAILED,
        "yielded": 0}


def say(msg: str) -> None:
    try:
        print(f"ttp push: {msg}", flush=True)
    except (OSError, ValueError):   # the log went away; the marker still gets the outcome
        pass


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """git with its output captured. No editor opens (a rebase continues with the message it has),
    and no recorded resolution (rerere) settles a conflict behind the resolver's back."""
    return gitmemo.run(["git", "-C", str(cwd), "-c", "rerere.enabled=false", *args], text=True,
                       capture_output=True, env={**os.environ, "GIT_EDITOR": "true", **(env or {})},
                       timeout=None)


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


def _check_argv(cmd, wt: Path) -> list[str]:
    """How a batch check runs: push.check_argv at a low CPU and IO priority (testslots.low_priority),
    as `ttp push` (push._run_one) and `ttp checks` run theirs."""
    return testslots.low_priority(push.check_argv(cmd, wt))


def _stream(cmd: str | list[str], cwd: Path, env: dict | None = None, timeout: float | None = None,
            group: bool = False, pass_fds: tuple = (), collect: workerloss.Collector | None = None,
            urgent_mark: bool = False) -> tuple[int | None, str, bool]:
    """Run `cmd` (a shell string, or an argv run without a shell), copying its output into this
    process's output (the batch log): (exit code, the last lines of its output, whether it ran out of time and was killed). `group` starts it in a
    process group of its own, which a timeout kills whole. The tail starts with up to OVER_KEPT
    pytest `assert <size> <= <cap>` lines where the size is over, each naming its test, from
    anywhere in the output: a prompt size cap's measured overshoot (conflictfix.cap_failure). `collect`
    gets every line (workerloss.Collector). A check gating the push branch (env TTP_PUSH_MODE `target`)
    runs under a landing mark this process holds until it ends, as push._run_one's do (landmark.py); no
    other command inherits one. `urgent_mark` labels it urgent (Batch._urgent_mark): its `ttp lock`
    waiters then keep the urgent rank a batch's checks have no TTP_TASK to give them."""
    sys.stdout.flush()
    sys.stderr.flush()
    mark = landmark.hold(urgent=urgent_mark) if env and env.get("TTP_PUSH_MODE") == "target" else None
    if env is not None or landmark.ENV in os.environ:
        env = {**{k: v for k, v in (os.environ if env is None else env).items() if k != landmark.ENV},
               **({landmark.ENV: str(mark.path)} if mark else {})}
    try:
        return _streamed(cmd, cwd, env, timeout, group, pass_fds, collect)
    finally:
        if mark:
            mark.close()


def _streamed(cmd, cwd: Path, env: dict | None, timeout: float | None, group: bool, pass_fds: tuple,
              collect: workerloss.Collector | None) -> tuple[int | None, str, bool]:
    proc = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=group, pass_fds=pass_fds)
    tail: deque[str] = deque(maxlen=push.LOG_TAIL)
    over: list[str] = []
    section = [""]

    def pump() -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace")
            tail.append(line.rstrip("\n"))
            if collect is not None:
                collect.feed(line)
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


def _remove_scratch(repo: Path, wt: Path) -> None:
    """Remove an after_push checkout: a scratch clone (cleantree.clone), or a worktree an older version
    registered in `repo`."""
    if (wt / ".git").is_file():
        _remove_worktree(repo, wt)
    else:
        shutil.rmtree(wt, ignore_errors=True)


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
            _remove_scratch(repo, wt)


# Conflicts that need no judgment ---------------------------------------------------------------------

def merge3(ours: str, base: str, theirs: str, py: bool = False, taken_in: bool = False,
           hunks: bool = False, kinds: set | None = None, superseded: bool = False,
           near: bool = False, imports: bool = False, cells: bool = False,
           counters: bool = False) -> str | None:
    """git's three-way merge of the texts, where each conflict in which both sides only added lines
    at one spot (an empty base section) keeps both: ours first, then theirs (in Python, see _seam).
    With `taken_in`, a conflict whose change on our side theirs already holds (_taken_in) takes
    theirs. With `hunks`, a conflict where the two sides changed different lines of the base section
    (git calls adjacent changes a conflict) takes both changes (_hunks). With `superseded`, a conflict
    whose change on our side theirs already makes, comments and spacing aside, takes theirs
    (_superseded). With `near`, both sides' added lines are not kept (by the added or hunks rule)
    when one nearly repeats the other (_near). With `imports`, a Python conflict made only of import
    statements keeps every name each side imports (_imports). With `cells`, a conflict of Markdown
    table rows where the two sides changed different cells takes both (_cells). With `counters`, a
    conflict of lines where both sides only raised numbers and added comment parts adds both raises
    (_counters). None when any other conflict
    remains, or when keeping both could be wrong (_clash). `kinds` collects how each conflict was
    settled ("added", "taken_in", "hunks", "cells"). The conflict markers carry a random tag, so file content
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
        if old and not (taken_in or hunks or superseded or imports or cells or counters):
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
        elif old and imports and py and (both := _imports(mine, old, theirs_lines)) is not None:
            res += both
            how = "imports"
        elif old and cells and (both := _cells(mine, old, theirs_lines)) is not None:
            res += both
            how = "cells"
        elif old and counters and (both := _counters(mine, old, theirs_lines)) is not None:
            res += both
            how = "counters"
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
    side's edits (line diff against old) share no old line, and neither inserts lines inside an edit
    of the other or where the other inserts too (the order would be a guess). An insertion at the
    edge of the other's edit goes before or after it (_edge_ok), every line of both sides as written:
    a block added next to a line the other side changed no longer sends an approval back. None on an
    overlap, or when the lines the two sides added share one (_said: the result would hold it twice),
    or with `near`, when one nearly repeats the other (_near)."""
    def edits(side: list[str]) -> list[tuple[int, int, list[str]]]:
        sm = difflib.SequenceMatcher(None, old, side, autojunk=False)
        return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    a, b = edits(mine), edits(theirs)
    if not a or not b:
        return None
    for i1, i2, x in a:
        for j1, j2, y in b:
            if i1 < j2 and j1 < i2:                     # both changed an old line
                return None
            if (i1 == i2 and not _edge_ok(i1, x, j1, j2, y, old)) or (j1 == j2 and not _edge_ok(j1, y, i1, i2, x, old)):
                return None                             # an insertion where the other edits
    new_a, new_b = [x for *_, new in a for x in new], [x for *_, new in b for x in new]
    if _said(new_a, py) & _said(new_b, py) or near and _near(new_a, new_b, py):
        return None
    out, pos = [], 0
    for i1, i2, new in sorted(a + b, key=lambda e: e[:2]):
        out += old[pos:i1] + new
        pos = i2
    return out + old[pos:]


_CELL_SEP = re.compile(r"(?<!\\)\|")   # a Markdown table's cell separator: a `|` not escaped as `\|`


def _cells(mine: list[str], old: list[str], theirs: list[str]) -> list[str] | None:
    """Both sides' changes of one conflict's Markdown table rows, when they changed different cells:
    the same rows on each side (same count, each a `|`-delimited row with as many cells as its old
    row), and no cell both sides changed to different text. A row only one side changed is taken
    from it. None otherwise: an added or removed row, a cell edited on both sides, or no table."""
    if not (len(mine) == len(old) == len(theirs)):
        return None
    out = []
    for o, b, t in zip(mine, old, theirs):
        if o == t or t == b:
            out.append(o)
            continue
        if o == b:
            out.append(t)
            continue
        rows = [x.rstrip("\r\n") for x in (o, b, t)]
        if not all(r.lstrip().startswith("|") and r.rstrip().endswith("|") for r in rows):
            return None
        co, cb, ct = (_CELL_SEP.split(r) for r in rows)
        if not len(co) == len(cb) == len(ct):
            return None
        row = []
        for x, y, z in zip(co, cb, ct):
            if x != y and z != y and x != z:
                return None
            row.append(z if x == y else x)
        out.append("|".join(row) + o[len(rows[0]):])
    return out


_NUM = re.compile(r"(?<![\w.])\d[\d_]*(?![\w.])")   # an integer literal (not part of a name or a float)


def _counters(mine: list[str], old: list[str], theirs: list[str]) -> list[str] | None:
    """Both sides' raises of the numbers on one conflict's lines, as when two changes each raise a
    test's prompt size cap by what their own text needs: per line, each side kept the code around
    the numbers and only raised numbers, so each number becomes old + both raises, and each side
    only added `; `-separated parts to the line's trailing comment (the reasons), kept with ours
    first. A line only one side changed is taken from it. None otherwise: a lowered number, any
    other code edit, a reworded comment part, or no line where both raised a number."""
    if not (len(mine) == len(old) == len(theirs)):
        return None
    out, raised = [], False
    for o, b, t in zip(mine, old, theirs):
        if o == t or t == b:
            out.append(o)
            continue
        if o == b:
            out.append(t)
            continue
        eol = o[len(o.rstrip("\r\n")):]
        (co, mo), (cb, mb), (ct, mt) = (_code_comment(x.rstrip("\r\n")) for x in (o, b, t))
        spans = [_numbers(x) for x in (co, cb, ct)]
        if not _skeleton(co, spans[0]) == _skeleton(cb, spans[1]) == _skeleton(ct, spans[2]):
            return None
        code, at = [], 0
        for (i, j), (x, y, z) in zip(spans[1], zip(*([c[i:j] for i, j in sp] for c, sp in zip((co, cb, ct), spans)))):
            vx, vy, vz = (int(v.replace("_", "")) for v in (x, y, z))
            if vx < vy or vz < vy or any(v.startswith("0") and v != "0" for v in (x, y, z)):
                return None
            raised |= vx > vy and vz > vy
            v = vx + vz - vy
            code += [cb[at:i], f"{v:_}" if "_" in x + y + z else str(v)]
            at = j
        note = _comment_parts(mo, mb, mt)
        if note is None:
            return None
        out.append("".join(code) + cb[at:] + note + eol)
    return out if raised else None


def _numbers(code: str) -> list[tuple[int, int]]:
    """The spans of the integer literals (_NUM) in `code` outside quoted strings."""
    out, quote, i = [], "", 0
    while i < len(code):
        c = code[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif (m := _NUM.match(code, i)) and not (i and (code[i - 1].isalnum() or code[i - 1] in "_.")):
            out.append(m.span())
            i = m.end()
            continue
        i += 1
    return out


def _skeleton(code: str, spans: list[tuple[int, int]]) -> str:
    """`code` with each number span (_numbers) replaced by a 0."""
    out, at = [], 0
    for i, j in spans:
        out += [code[at:i], "0"]
        at = j
    return "".join(out) + code[at:]


def _code_comment(line: str) -> tuple[str, str]:
    """A line split before its trailing ` # ` comment: the first `#` outside a quoted string, after
    a space. The whole line and no comment when there is none, or a string is still open."""
    quote, i = "", 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "#" and i and line[i - 1] == " ":
            k = len(line[:i].rstrip(" "))
            return line[:k], line[k:]
        i += 1
    return line, ""


def _comment_parts(mine: str, old: str, theirs: str) -> str | None:
    """Three trailing comments merged when each side only added `; `-separated parts to the old one
    (conflictfix.additive, ours first); None for any other change."""
    if mine == theirs or theirs == old:
        return mine
    if mine == old:
        return theirs
    head = re.compile(r"^\s*#\s*")
    if not all(head.match(x) for x in (mine, old, theirs)):
        return None
    lead = head.match(old).group()
    parts = [x[head.match(x).end():].split("; ") for x in (mine, old, theirs)]
    both = conflictfix.additive(parts[0], parts[1], parts[2])
    return None if both is None else lead + "; ".join(both)


def _test_file(path: str) -> bool:
    """Whether `path` is a test file, where a raised number is an allowance (a size cap) both raises add to."""
    parts = path.replace("\\", "/").split("/")
    return any(d in ("tests", "test") for d in parts[:-1]) or parts[-1].startswith("test_")


def _edge_ok(k: int, new: list[str], e1: int, e2: int, edit: list[str], old: list[str]) -> bool:
    """Whether lines `new` inserted before old[k] have one place beside the other side's edit of
    old[e1:e2] into `edit`: not inside it, not where the other inserts too, and at its edge only when
    the edit rewords or deletes lines without adding any (a longer edit may hold an insertion at that
    same spot) and the block cannot as well be read one line into it (its first line repeats the
    edit's first old line, or its last the edit's last: the line diff's placement is a guess)."""
    if not e1 <= k <= e2:
        return True
    if e1 == e2 or e1 < k < e2 or len(edit) not in (0, e2 - e1):
        return False
    return new[0] != old[e1] if k == e1 else new[-1] != old[e2 - 1]


def _import_stmts(lines: list[str]) -> list[tuple[tuple, list[str], int, int]] | None:
    """The import statements that make up `lines`, each as (key, names as written, first line, last
    line): key ("from", level, module) or ("import",), a name `a` or `a as b`. None when the lines
    hold anything else (other code, `;`, a line continuation, a star import, a name twice), hold
    no import statement at all (only comments) or do not parse on their own. Comments may sit among them: names are placed only where the name
    before them reads once in the statement."""
    text = "".join(lines)
    if not text.strip() or any(c in text for c in ";\\"):
        return None
    try:
        body = ast.parse(text).body
    except (SyntaxError, ValueError):
        return None
    out = []
    for node in body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.end_lineno is None:
            return None
        names = [a.name + (f" as {a.asname}" if a.asname else "") for a in node.names]
        if "*" in names or len(set(names)) != len(names):
            return None
        key = ("from", node.level, node.module or "") if isinstance(node, ast.ImportFrom) else ("import",)
        out.append((key, names, node.lineno - 1, node.end_lineno - 1))
    return out or None


def _imports(mine: list[str], old: list[str], theirs: list[str]) -> list[str] | None:
    """One Python conflict made only of import statements, settled by name: the result imports what
    ours does, less the names theirs removed, plus the names theirs added (each statement keyed by
    its module), written in our layout (a name per line where ours lists them so, else inline after
    the name theirs puts before it). Every name each side added stays as it wrote it. None when a
    side holds anything but import statements, a statement theirs extends is gone from ours, or a
    name cannot be placed in one way only."""
    o, b, t = (_import_stmts(x) for x in (mine, old, theirs))
    if o is None or b is None or t is None:
        return None

    def names(stmts: list) -> dict[tuple, list[str]]:
        out: dict[tuple, list[str]] = {}
        for key, ns, _, _ in stmts:
            out.setdefault(key, []).extend(ns)
        return out
    on, bn, tn = names(o), names(b), names(t)
    segs = [["".join(mine[i:j + 1]), key] for key, _, i, j in o]   # each statement of ours, edited in place
    before = mine[:o[0][2]]
    between = [mine[o[k][3] + 1:o[k + 1][2]] for k in range(len(o) - 1)] + [mine[o[-1][3] + 1:]]
    tail: list[str] = []

    def token(name: str) -> str:
        parts = name.split(" as ")
        return r"(?<![\w.])" + r"\s+as\s+".join(re.escape(x) for x in parts) + r"(?![\w.])"

    def inline(seg: str) -> tuple[str, str]:
        cut = re.search(r"\bimport\b", seg)
        return (seg[:cut.end()], seg[cut.end():]) if cut else ("", seg)

    def per_line(seg: str, ns: list[str]) -> list[str] | None:
        rows = seg.splitlines(keepends=True)
        return rows if len(rows) > 1 and all(sum(r.strip() == f"{n}," for r in rows) == 1 for n in ns) else None

    def stmt_names(seg: str) -> list[str]:
        got = _import_stmts([seg])
        return got[0][1] if got and len(got) == 1 else []

    for key in tn.keys() | bn.keys():
        T, B, O = tn.get(key, []), bn.get(key, []), on.get(key, [])
        for r in [n for n in B if n not in T and n in O]:          # theirs removed it
            k = next(k for k, (seg, sk) in enumerate(segs) if sk == key and r in stmt_names(seg))
            seg = segs[k][0]
            if len(stmt_names(seg)) == 1:
                return None
            if rows := per_line(seg, stmt_names(seg)):
                segs[k][0] = "".join(x for x in rows if x.strip() != f"{r},")
                continue
            head, rest = inline(seg)
            hits = list(re.finditer(token(r) + r"\s*,[ \t]*", rest)) or list(re.finditer(r",\s*" + token(r), rest))
            if len(hits) != 1:
                return None
            segs[k][0] = head + rest[:hits[0].start()] + rest[hits[0].end():]
        added = [n for n in T if n not in B and n not in O]
        if added and not O:
            if B or any(n not in added for n in T):
                return None                 # a statement theirs extends is gone from ours
            for tk, _, i, j in t:
                if tk == key:
                    tail += theirs[i:j + 1]
            continue
        for n in added:
            pos = T.index(n)
            prev = next((T[x] for x in range(pos - 1, -1, -1)
                         if any(sk == key and T[x] in stmt_names(seg) for seg, sk in segs)), None)
            k = next(k for k, (seg, sk) in enumerate(segs) if sk == key and (prev is None or prev in stmt_names(seg)))
            seg = segs[k][0]
            anchor = prev or stmt_names(seg)[0]
            if rows := per_line(seg, stmt_names(seg)):
                at = next(x for x, row in enumerate(rows) if row.strip() == f"{anchor},")
                row = rows[at]
                line = row[:len(row) - len(row.lstrip())] + f"{n}," + row[len(row.rstrip("\r\n")):]
                rows.insert(at + 1 if prev else at, line)
                segs[k][0] = "".join(rows)
                continue
            head, rest = inline(seg)
            hits = list(re.finditer(token(anchor), rest))
            if len(hits) != 1:
                return None
            at = hits[0].end() if prev else hits[0].start()
            segs[k][0] = head + rest[:at] + (f", {n}" if prev else f"{n}, ") + rest[at:]
    out = list(before)
    for (seg, _), gap in zip(segs, between):
        out += seg.splitlines(keepends=True) + gap
    out += tail
    got = _import_stmts(out)
    if got is None:
        return None
    want = {key: {n for n in on.get(key, []) if not (n in bn.get(key, []) and n not in tn.get(key, []))}
            | {n for n in tn.get(key, []) if n not in bn.get(key, [])} for key in on.keys() | tn.keys()}
    have = {key: set(ns) for key, ns in names(got).items()}
    # a name bound twice (both sides re-aliased one import, or imported it from two places) is an edit
    bound = [n.split(" as ")[-1].split(".")[0] for _, ns, _, _ in got for n in ns]
    source = [(key, n.split(" as ")[0]) for key, ns, _, _ in got for n in ns]
    if len(set(bound)) != len(bound) or len(set(source)) != len(source):
        return None
    return out if have == {k: v for k, v in want.items() if v} else None


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
           kinds: set | None = None, superseded: bool = False, near: bool = False, imports: bool = False) -> bool:
    """Settle the conflicted `path` of a stopped rebase in `wt`, if it needs no judgment, and write
    the result: True when settled. Ours (stage 2) is the batch head, theirs (stage 3) the entry.
    In a version file every stage first takes the batch head's version, so a version line alone
    never conflicts. A pure addition keeps both sides (merge3), unless in a .py file that leaves a
    top-level def or class name twice where neither side had it twice (it would silently shadow a
    test). `taken_in`, `hunks`, `superseded`, `near`, `imports` and `kinds` are merge3's; a version line taken adds "version" to
    `kinds`. With `hunks`, a Markdown file also settles table rows by cell (merge3's `cells`), and
    a test file numbers both sides raised (merge3's `counters`, _test_file). A file both sides
    created is never settled."""
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
                    superseded=superseded, near=near, imports=imports,
                    cells=hunks and path.lower().endswith(".md"), counters=hunks and _test_file(path))
    if merged is None:
        return False
    if path.endswith(".py") and _duplicates(merged) - _duplicates(o) - _duplicates(t):
        return False
    durable_write(wt / path, merged)
    if kinds is not None:
        kinds |= how
    return True


def release_kind(path: str, cfg: dict | None) -> str | None:
    """Which shared release file `path` is under the delivery.version_bump `cfg` (push.bump_of):
    "version" (a file holding the version), "changeset" (a changeset, or the release changeset) or
    "changelog" (a CHANGELOG.md where a change needs a bump); None for the change's own files. Every
    change of a batch writes these, so their conflicts are the batch's own, never the change's."""
    if not cfg:
        return None
    if path in cfg["files"]:
        return "version"
    cs = cfg.get("changeset_dir") or ""
    if path == cfg.get("changeset_file") or (cs and path.startswith(cs + "/") and path.endswith(".md")):
        return "changeset"
    if os.path.basename(path) == "CHANGELOG.md" and push._under(path, cfg.get("paths") or []):
        return "changelog"
    return None


def release_excludes(cfg: dict | None) -> list[str]:
    """git pathspecs that leave out every release file of `cfg` (release_kind)."""
    if not cfg:
        return []
    out = [f":(exclude){f}" for f in cfg["files"]]
    out += [f":(exclude){x}" for x in (cfg.get("changeset_dir"), cfg.get("changeset_file")) if x]
    return out + [f":(exclude,glob){d + '/' if d else ''}**/CHANGELOG.md" for d in (cfg.get("paths") or [""])]


def _union(ours: str, base: str, theirs: str) -> str:
    """`ours` and then each line `theirs` added to `base` that `ours` lacks: release notes are a list."""
    have, was = set(ours.splitlines()), set(base.splitlines())
    more = [ln for ln in theirs.splitlines() if ln not in was and ln not in have and ln.strip()]
    if not more:
        return ours
    return ours + ("" if not ours or ours.endswith("\n") else "\n") + "\n".join(more) + "\n"


def settle_release(wt: Path, path: str, cfg: dict | None, kinds: set | None = None) -> bool:
    """Settle a stopped rebase's conflict in a changeset or CHANGELOG (release_kind) of `wt`
    mechanically: True when settled. Both sides' notes are kept: a changeset both sides created
    keeps ours at `path` and theirs as a file of its own beside it (changesets stay separate files);
    an edit on both sides merges, or takes ours plus the lines theirs added (_union). A side that
    deleted the file is a real conflict. Adds "release" to `kinds`."""
    if release_kind(path, cfg) not in ("changeset", "changelog"):
        return False
    ours, base, theirs = _show(wt, 2, path), _show(wt, 1, path), _show(wt, 3, path)
    if ours is None or theirs is None:
        return False
    try:
        o, b, t = ours.decode(), (base or b"").decode(), theirs.decode()
    except UnicodeDecodeError:
        return False
    if "\0" in o + b + t:
        return False
    if o == t:
        text = o
    elif base is None and path != cfg.get("changeset_file") and release_kind(path, cfg) == "changeset":
        stem, n = path[:-3], 2
        while (wt / f"{stem}-{n}.md").exists() or _show(wt, 0, f"{stem}-{n}.md") is not None:
            n += 1
        durable_write(wt / f"{stem}-{n}.md", t)
        _git(wt, "add", "--", f"{stem}-{n}.md")
        text = o
    else:
        text = merge3(o, b, t, hunks=True) if base is not None else None
        text = _union(o, b, t) if text is None else text
    durable_write(wt / path, text)
    if kinds is not None:
        kinds.add("release")
    return True


def mechanical_paths(delivery: dict) -> list[str]:
    """`delivery.mechanical_merge_paths` as repo-path globs; unset: MECHANICAL_PATHS; empty: none,
    which turns settle_union off."""
    v = delivery.get("mechanical_merge_paths")
    return list(MECHANICAL_PATHS) if v is None else push.exclude_list(v)


def _union3(ours: str, base: str, theirs: str) -> str | None:
    """`git merge-file --union` of the three texts: each conflicting region keeps ours' lines, then
    theirs', in order. None when git fails."""
    with tempfile.TemporaryDirectory(prefix="ttp-union-") as d:
        names = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(d) / name).write_bytes(text.encode())
            names.append(str(Path(d) / name))
        r = subprocess.run(["git", "merge-file", "-p", "--union", *names], capture_output=True)
    if r.returncode < 0 or r.returncode > 127:
        return None
    try:
        return r.stdout.decode()
    except UnicodeDecodeError:
        return None


def settle_union(wt: Path, path: str, globs: list[str], kinds: set | None = None) -> bool:
    """Settle a stopped rebase's conflict in a shared doc of `wt` (`path` matches one of `globs`,
    delivery.mechanical_merge_paths) when settle could not: both sides' lines are kept, ours then
    theirs in each conflicting region (_union3). True when settled; adds "union" to `kinds`. Only
    lines both sides added are kept so: a region where either side edits or deletes base lines
    (_doc_hunks: a non-empty base section) is a real conflict, as are a side that deleted the file,
    a binary file, and a result that holds a conflict marker (conflictfix._MARKER, a doc's `=`
    heading underline aside)."""
    import fnmatch
    if not any(fnmatch.fnmatchcase(path, g) for g in globs):
        return False
    ours, base, theirs = _show(wt, 2, path), _show(wt, 1, path), _show(wt, 3, path)
    if ours is None or theirs is None:
        return False
    try:
        o, b, t = ours.decode(), (base or b"").decode(), theirs.decode()
    except UnicodeDecodeError:
        return False
    if "\0" in o + b + t or (hunks := _doc_hunks(ours, base or b"", theirs)) is None:
        return False
    if edit := next((h for h in hunks if h["base"]), None):
        say(f"{path}: both sides change line {edit['line']} (not only add lines there); not settled")
        return False
    if (merged := _union3(o, b, t)) is None:
        return False
    if any(conflictfix._MARKER.match(ln) and not conflictfix._marker_ok(path, ln) for ln in merged.splitlines()):
        say(f"{path}: keeping both sides would leave a conflict marker; not settled")
        return False
    durable_write(wt / path, merged)
    if kinds is not None:
        kinds.add("union")
    return True


def own_diff(repo: Path, a: str, b: str, cfg: dict | None, also: list[str] | tuple = ()) -> str:
    """The change a..b makes outside the release files (release_excludes) and the globs `also`,
    without context, blob ids or line numbers: two replays of one change onto different bases give
    the same text."""
    out = _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0", a, b, "--", ".",
               *release_excludes(cfg), *(f":(exclude){g}" for g in also)).stdout
    return "\n".join("@@" if ln.startswith("@@") else ln for ln in out.splitlines() if not ln.startswith("index "))


def _patch_ids(repo: Path, revs: list[str]) -> dict[str, str]:
    """{commit: its `git patch-id --stable`} for the non-merge commits `revs` names, in their order."""
    try:
        patches = _git(repo, "log", "-p", "--no-merges", "--no-color", "--no-ext-diff", "--format=commit %H",
                       *revs, "--").stdout
        out = gitmemo.run(["git", "-C", str(repo), "patch-id", "--stable"], input=patches, text=True,
                          capture_output=True, timeout=None).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {c: pid for pid, c in (ln.split() for ln in out.splitlines() if len(ln.split()) == 2)}


def _idents(repo: Path, *revs: str) -> dict[str, tuple[str, str, frozenset, int]]:
    """{commit: (author and author date, subject, files, committer date)} for the non-merge commits
    `revs` names, in their order."""
    out = _git(repo, "log", "--no-merges", "--name-only", "--date=raw",
               "--format=%x00%H%x00%an <%ae> %ad%x00%ct %s", *revs, "--").stdout
    res, parts = {}, out.split("\0")[1:]
    for k in range(0, len(parts) - 2, 3):
        head, _, files = parts[k + 2].partition("\n")
        when, _, subject = head.partition(" ")
        if when.isdigit():
            res[parts[k]] = (parts[k + 1], subject, frozenset(f for f in files.split("\n") if f), int(when))
    return res


def _rewritten(repo: Path, onto: str, base: str, rest: list[str],
               resolved: dict[str, list[str]] | None = None) -> dict[str, str]:
    """{commit: its landed version} for the commits of `rest` that landed rewritten: a commit of
    base..onto with the same author and author date (a rebase or amend keeps both), the same subject and
    the same files, and a committer date no older than the commit's own. Such a commit landed with a
    conflict resolved in its own review, so neither a trailer nor its patch names it; the landed version
    is the newer one. Every task may commit as one author within one second, so a subject or the files
    alone never match. A commit rewritten after its landing (an amend that adds work) is newer than the
    landed version, so it is replayed, never dropped; one amended before it must still be carried whole by
    the landed version (_carries). `resolved` gets {commit: files} for each one found whose drop relies
    on the landing's resolution of a conflict region, so the batch records it."""
    mine = _idents(repo, "--no-walk=unsorted", *rest)
    theirs: dict[str, list[tuple[str, str, frozenset, int]]] = {}
    for c, (who, subject, files, when) in _idents(repo, "--reverse", f"{base}..{onto}").items():
        theirs.setdefault(who, []).append((c, subject, files, when))   # oldest first wins
    found = {}
    for c in rest:
        if not (m := mine.get(c)):
            continue
        for landed, subject, files, when in theirs.get(m[0], []):
            took: list[str] = []
            if subject == m[1] and files == m[2] and when >= m[3] and _carries(repo, c, landed, m[2], took):
                found[c] = landed
                if took and resolved is not None:
                    resolved[c] = sorted(took)
                break
    return found


def _cdate(repo: Path, rev: str) -> int:
    """The committer date of `rev` (0 when unknown)."""
    out = _git(repo, "log", "-1", "--format=%ct", rev, "--").stdout.strip()
    return int(out) if out.isdigit() else 0


def _held(repo: Path, c: str, onto: str, files: frozenset) -> bool:
    """Whether `onto` already holds all of commit `c`'s work, so replaying it there could only conflict
    with its own landing: `c` merged (three-way, base c^) onto `onto` changes no line of its files outside
    the conflicting regions, and in each such region onto's own lines hold every line `c` adds there,
    verbatim. A file `c` deletes or adds must be gone or there as well."""
    try:
        for path in files:
            mine, got = _blob(repo, c, path), _blob(repo, onto, path)
            if mine == got:
                continue
            if mine is None or got is None:
                return False
            with tempfile.TemporaryDirectory() as d:
                sides = []
                for name, text in (("ours", got), ("base", _blob(repo, f"{c}^", path) or b""), ("theirs", mine)):
                    (Path(d) / name).write_bytes(text)
                    sides.append(str(Path(d) / name))
                r = gitmemo.run(["git", "merge-file", "-p", "--diff3", f"--marker-size={_MARK}", *sides],
                                capture_output=True, timeout=None)
            if r.returncode < 0 or r.returncode > 127:   # 255: an error (binary)
                return False
            regions, gaps = _regions(r.stdout), _gaps(r.stdout, got)
            if gaps is None or len(gaps) != len(regions) or any(
                    Counter(theirs) - Counter(base) - Counter(ours) for ours, base, theirs in regions):
                return False
    except (OSError, subprocess.SubprocessError):
        return False
    return True


_MARK = 40          # conflict marker size: no line of a file is taken for one


def _blob(repo: Path, rev: str, path: str) -> bytes | None:
    """The file `path` at `rev`, or None where it has none."""
    r = gitmemo.run(["git", "-C", str(repo), "cat-file", "blob", f"{rev}:{path}"], capture_output=True,
                    timeout=None)
    return r.stdout if r.returncode == 0 else None


def _carries(repo: Path, c: str, landed: str, files: frozenset, resolved: list[str] | None = None) -> bool:
    """Whether `landed` holds all of commit `c`'s work: `c` merged (three-way, base c^) onto landed^
    gives landed's version of each of its files, outside the regions that conflict there (the
    landing resolved those), and inside each such region the landing's lines account for every line
    `c` adds there (_explained). An amend made before the landing adds work its landed copy lacks, also
    inside a region the landing resolved: that commit is replayed, so the work lands or comes back as a
    conflict, never dropped. The files whose answer relied on a resolved region's looser rewrites go to
    `resolved`; not a file whose every added line is in the landing verbatim or inside a rewritten line
    holding all its words (_holds): nothing can be lost there, so nobody is asked to check it."""
    try:
        for path in files:
            mine, got = _blob(repo, c, path), _blob(repo, landed, path)
            if mine == got:
                continue
            if mine is None or got is None:
                return False
            with tempfile.TemporaryDirectory() as d:
                sides = []
                for name, rev in (("ours", f"{landed}^"), ("base", f"{c}^"), ("theirs", c)):
                    (Path(d) / name).write_bytes(_blob(repo, rev, path) or b"")
                    sides.append(str(Path(d) / name))
                cmd = ["git", "merge-file", "-p", f"--marker-size={_MARK}"]
                r = gitmemo.run([*cmd, *sides], capture_output=True, timeout=None)
                if r.returncode < 0 or r.returncode > 127 or not _fits(r.stdout, got):   # 255: an error (binary)
                    return False
                if r.returncode:    # conflicts: their base side tells what c added in each region
                    r = gitmemo.run([*cmd, "--diff3", *sides], capture_output=True, timeout=None)
                    gaps = _gaps(r.stdout, got) if 0 < r.returncode <= 127 else None
                    regions = _regions(r.stdout)
                    if gaps is None or len(gaps) != len(regions) or not all(
                            _explained(*side, gap) for side, gap in zip(regions, gaps)):
                        return False
                    if resolved is not None and not all(_explained(*side, gap, _holds)
                                                        for side, gap in zip(regions, gaps)):
                        resolved.append(path)   # a line survives only as a looser rewrite: worth a check
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _marked(ln: bytes, ch: bytes) -> bool:
    """Whether `ln` is a conflict marker line of `ch` (marker size _MARK)."""
    return ln.startswith(ch * _MARK) and ln[_MARK:_MARK + 1] in (b" ", b"\n", b"\r", b"")


def _regions(merged: bytes) -> list[tuple[list[bytes], list[bytes], list[bytes]]]:
    """(ours, base, theirs) lines of each conflict region of a diff3-style `merged`."""
    out, sides, at = [], [[], [], []], -1
    for ln in merged.splitlines(keepends=True):
        if at < 0:
            if _marked(ln, b"<"):
                sides, at = [[], [], []], 0
        elif _marked(ln, b">"):
            out.append(tuple(sides))
            at = -1
        elif at == 0 and _marked(ln, b"|"):
            at = 1
        elif at < 2 and _marked(ln, b"="):
            at = 2
        else:
            sides[at].append(ln)
    return out


def _doc_hunks(ours: bytes, base: bytes, theirs: bytes) -> list[dict] | None:
    """Each conflicting region of git's diff3 merge of a file's three sides, as {"line": its first line
    in ours (1-based), "ours", "base", "theirs": each side's line count}: a region with base lines is
    one where a side edits or deletes lines. None when git fails."""
    with tempfile.TemporaryDirectory(prefix="ttp-hunks-") as d:
        sides = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(d) / name).write_bytes(text)
            sides.append(str(Path(d) / name))
        r = subprocess.run(["git", "merge-file", "-p", "--diff3", f"--marker-size={_MARK}", *sides],
                           capture_output=True)
    if not 0 <= r.returncode <= 127:
        return None
    out: list[dict] = []
    line, at = 1, -1
    for ln in r.stdout.splitlines(keepends=True):
        if at < 0:
            if _marked(ln, b"<"):
                out.append({"line": line, "ours": 0, "base": 0, "theirs": 0})
                at = 0
            else:
                line += 1
        elif _marked(ln, b">"):
            at = -1
        elif at == 0 and _marked(ln, b"|"):
            at = 1
        elif at < 2 and _marked(ln, b"="):
            at = 2
        else:
            out[-1][("ours", "base", "theirs")[at]] += 1
            line += at == 0
    return out


def doc_merge_text(recs: list) -> str:
    """One line per shared doc a batch settled (Batch._doc_merge records): file, hunks, both commits."""
    out = []
    for r in recs if isinstance(recs, list) else []:
        if isinstance(r, dict):
            hunks = [h for h in r.get("hunks") or [] if isinstance(h, dict)]
            out.append(f"{r.get('file')}: {len(hunks)} hunk{'' if len(hunks) == 1 else 's'} at line"
                       f"{'' if len(hunks) == 1 else 's'} {', '.join(str(h.get('line')) for h in hunks) or '?'}, "
                       f"{str(r.get('landed') or '?')[:10]}'s lines kept, then {str(r.get('commit') or '?')[:10]}'s")
    return "; ".join(out)


def _explained(ours: list[bytes], base: list[bytes], theirs: list[bytes], got: list[bytes],
               like=None) -> bool:
    """Whether the landing's lines `got` for a conflict region account for every line commit c's side
    (`theirs`) adds over `base` there: each one not in `got` verbatim pairs, one to one, with a similar
    line `got` adds over `ours` that is not c's (a line rewritten while resolving: _paired). A line left
    unpaired is work the landed copy lacks, such as an amend's added line, even when the resolution adds
    as many lines of its own. `like` replaces _similar as the pairing test."""
    new = Counter(theirs) - Counter(base)
    missing = list((new - Counter(got)).elements())
    rewrites = list(((Counter(got) - Counter(ours)) - Counter(theirs)).elements())
    return _paired(missing, rewrites, like or _similar)


_PAIRS_MAX = 40000      # missing x rewritten lines compared at most; more keeps the commit in the replay
_SIMILAR = 0.6          # difflib ratio from which two lines count as one rewritten


def _words(ln: bytes) -> Counter:
    """The words and punctuation marks of line `ln`."""
    return Counter(re.findall(rb"\w+|[^\w\s]", ln))


def _similar(a: bytes, b: bytes) -> bool:
    """Whether line `b` reads as line `a` rewritten: one holds every word and mark of the other, or
    difflib rates them at least _SIMILAR alike. A blank line is like none."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return wa <= wb or wb <= wa or difflib.SequenceMatcher(None, a.strip(), b.strip(), autojunk=False).ratio() >= _SIMILAR


def _holds(a: bytes, b: bytes) -> bool:
    """Whether line `b` holds every word and mark of line `a` (a rewrite that only adds). A blank line is like none."""
    wa = _words(a)
    return bool(wa) and wa <= _words(b)


def _paired(missing: list[bytes], rewrites: list[bytes], like=None) -> bool:
    """Whether each line of `missing` pairs with its own similar line of `rewrites` (`like`, default
    _similar), one to one (a bipartite matching by augmenting paths). Too many lines to compare is no
    pairing."""
    like = like or _similar
    if not missing:
        return True
    if len(missing) > len(rewrites) or len(missing) * len(rewrites) > _PAIRS_MAX:
        return False
    pairs = [[j for j, r in enumerate(rewrites) if like(m, r)] for m in missing]
    owner: dict[int, int] = {}

    def take(i: int, seen: set) -> bool:
        for j in pairs[i]:
            if j not in seen:
                seen.add(j)
                if j not in owner or take(owner[j], seen):
                    owner[j] = i
                    return True
        return False
    return all(take(i, set()) for i in range(len(missing)))


def _fits(merged: bytes, got: bytes) -> bool:
    """Whether `got` is `merged` with each conflict region replaced by any lines: every hunk outside
    the conflicts is there, in order, with nothing else between."""
    return _gaps(merged, got) is not None


def _gaps(merged: bytes, got: bytes) -> list[list[bytes]] | None:
    """The lines of `got` that stand for each conflict region of `merged`, in order, when `got` is
    `merged` with each region replaced by any lines (_fits), else None."""
    segs, cur, inside = [], [], False
    for ln in merged.splitlines(keepends=True):
        if not inside and ln.startswith(b"<" * _MARK) and ln[_MARK:_MARK + 1] in (b" ", b"\n", b"\r", b""):
            segs.append(cur)
            cur, inside = [], True
        elif inside:
            inside = not (ln.startswith(b">" * _MARK) and ln[_MARK:_MARK + 1] in (b" ", b"\n", b"\r", b""))
        else:
            cur.append(ln)
    if inside:
        return None
    segs.append(cur)
    lines = got.splitlines(keepends=True)
    if len(segs) == 1:
        return [] if segs[0] == lines else None
    first, last = segs[0], segs[-1]
    if len(first) + len(last) > len(lines) or lines[:len(first)] != first or lines[len(lines) - len(last):] != last:
        return None
    at, end, gaps = len(first), len(lines) - len(last), []
    for seg in segs[1:-1]:
        k = next((i for i in range(at, end - len(seg) + 1) if lines[i:i + len(seg)] == seg), None)
        if k is None:
            return None
        gaps.append(lines[at:k])
        at = k + len(seg)
    return [*gaps, lines[at:end]]


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
        self.mechanical = mechanical_paths(self.d)   # shared docs settle_union may settle
        self.entries = [e for e in m.get("entries") or [] if isinstance(e, dict)]
        self.wt = p.worktrees / WORKTREE
        self.status: dict[int, str] = {}       # entry index -> status
        self.detail: dict[int, dict] = {}
        self.landed_at: dict[int, str] = {}    # entry index -> its own commit on the branch (pushed or landed)
        self._heads: dict[str, str] | None = None   # push.landed_heads, read once per batch
        self.check_run_dir = m.get("check_run_dir")
        self.checks = {"runs": 0, "seconds": 0.0, "flaky": False}
        self.tip = self.pushed_sha = self.version = None
        self.reach: dict | None = None          # push.reach of what it pushed or found landed
        self.message = ""
        self.rounds = 0
        self.cfg: dict | None = None
        self.target_lock = None
        self.steps = push.Steps(None)           # its steps marked on target_lock: an upgrade goes between them
        self.detail_tip: dict | None = None     # the tip's own failing check, for tip_failed
        self.tip_passed = False                 # the tip passed its checks alone this round
        self.tip_tried = 0                      # leading entries shown not to fix a failing tip (tip_failed)
        self.tip_runs = 0                       # the rerun on the tip (of TIP_RERUNS) that failed last (_tip_ids)
        self.info: dict[int, dict] = {}
        self.settled: dict[int, dict] = {}     # entry index -> the conflicts its replay settled
        self.dropped: dict[int, list] = {}     # entry index -> its commits left out on a landing's conflict resolution
        self.conflicted: set[int] = set()      # entries whose replay hit a conflict it did not settle
        self._settling: tuple[list[str], set, list] = ([], set(), [])   # files, kinds and doc merges of the replay running
        self.rescued: dict[int, str] = {}      # entry index -> its head, replayed again without a blamed entry
        self.unbumped: dict[str, str] = {}     # a head with this batch's version bump -> the replayed head under it
        self.held: dict[int, dict] = {}        # entry index -> the token scope a refused push needs for it
        self.scopes: list[dict] = []           # those refusals: scope, files, entry ids, check (scopehold)
        self.retried = False                   # this round checked the entries after a blamed one (_rescue)
        self.rescues = 0                       # such checks in the batch: at most RESCUES
        self.reran: dict[str, set[str]] = {"head": set(), "tip": set()}   # failing test ids rerun there (_run_ids)
        self.judged: dict[tuple[str, str], bool] = {}   # (head, failing test id) -> new on that head (_judged)
        self.new_ids: dict[str, list[str]] = {}  # a failed check's output tail -> its test ids new on its head (_judged)
        self.flaky: list[dict] = []            # failures whose tests passed on that rerun: {ids, head, cmd}
        self.pre_existing: list[dict] = []     # failures whose tests failed on the tip alone too: {ids, tip, cmd, entry, runs}
        self.fail_rc: dict[str, int] = {}      # a failed check's output tail -> its exit code (_tip_ids)
        self.yielded_to: list[int] = []        # queue ids of urgent approvals the search stopped for (_yield_now)
        self.checks_at: float | None = None    # when this batch's first check began (_check): the yield grace counts from it

    # results

    def _set(self, i: int, status: str, **detail) -> None:
        self.status[i], self.detail[i] = status, detail

    def results(self) -> list[dict]:
        """Each entry's outcome; the detail of one whose replay settled conflicts names them
        ("settled": files, "settled_by": kinds), so a check failure after it can be traced to it, and
        that of one whose commits were left out as landed only by a landing that resolved a conflict in
        their lines names them ("dropped_as_resolved": [{commit, as, files}]: _rewritten). A
        pushed or landed entry names its own commit on the branch ("sha": the replayed head that ends
        with it, or the tip it was found on), which a rebase made different from its reviewed head."""
        out = []
        for i, e in enumerate(self.entries):
            st = "scope_held" if i in self.held else self.status.get(i, "requeued")
            r = {"id": e.get("id"), "task": e.get("task"), "status": st,
                 "detail": {**self.detail.get(i, {}), **self.settled.get(i, {}),
                            **({"dropped_as_resolved": self.dropped[i]} if i in self.dropped else {})}}
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
        self.steps = push.Steps(self.target_lock, say)
        why = push.refusal(self.repo, self.remote, self.branch, push.allow_protected(self.d))
        if why.startswith("refusing"):
            return self._all("refused", "refused", why)
        if why:
            return self._all("requeued", "error", why)
        rnd = 0
        while rnd < rounds:
            rnd += 1
            self.rounds += 1
            self.steps.go("rebase")
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
            except workerloss.Infra as e:
                # The host again (test workers lost twice, each exiting cleanly): every entry goes back
                # to the queue with its approval, retried after a pause (push.WORKER_LOSS_S).
                self.checks["worker_loss"] = {"check": e.check, "tests": e.loss["tests"],
                                              "workers": {str(k): v for k, v in e.loss["workers"].items()}}
                return self._all("requeued", "worker_loss", f"{e}; the batch is held, not failed, and retried later",
                                 keep=("landed", "refused"))
            except syntaxfault.Infra as e:
                # A script outside the change rewritten in place as the check ran it, again on its rerun: the
                # host, never the entries; every one goes back to the queue, retried after syntaxfault.RETRY_S.
                self.checks["script_torn"] = {"check": e.check, "scripts": e.scripts}
                return self._all("requeued", "script_torn", f"{e}; the batch is held, not failed, and retried later",
                                 keep=("landed", "refused"))
            except setupfault.Infra as e:
                # A check that failed before any test ran, again on its reruns (or no free pool slot): the
                # host or its pool, never the entries; every one goes back, retried after setupfault.RETRY_S.
                self.checks["setup_fault"] = {"check": e.check, **e.fault}
                return self._all("requeued", "setup_fault", f"{e}; the batch is held, not failed, and retried later",
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
            return self._close(0, "yielded" if self.yielded_to else "tip_failed" if failed is None else "nothing",
                               carried, failed)
        self.steps.go("push")
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
                self.settled[i].pop("approval_carried", None)
                self._set(i, "conflict", files=self.settled[i]["settled"], cmd=failed[0], tail=failed[1])
            elif n == k + 1 and failed:
                self._set(i, "check_failed", cmd=failed[0], tail=failed[1])
            elif i in self.rescued and outcome == "pushed":
                self._set(i, "pushed")
                self.landed_at[i] = self.rescued[i]
            elif self.yielded_to:
                self._set(i, "requeued", yielded=True)     # the search stopped for an urgent approval
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
        self.info, self.settled, self.conflicted, self.dropped = {}, {}, set(), {}
        csdir = self.cfg["changeset_dir"] if self.cfg else ""
        self._faults: list[str] = []    # where each entry before this one raised, "" for one that did not
        for i, e in enumerate(self.entries):
            try:
                h = self._replay_one(i, e, h, carried, tip, checks, csdir)
                self._faults.append("")
            except HOST_FAULTS:
                raise
            except Exception as ex:     # one entry's fault takes only it out, never the whole batch
                self._replay_fault(i, e, h, carried, ex)
        return carried

    def _replay_one(self, i: int, e: dict, h: str, carried: list[tuple[int, str]], tip: str, checks: list[str],
                    csdir: str) -> str:
        """Replay entry `i` onto the batch head `h` and give it its status; the batch head after it
        (`h` unless it is carried, when it is also added to `carried`)."""
        sha = str(e.get("head") or "")
        if not re.fullmatch(r"[0-9a-f]{7,64}", sha) or \
                _git(self.repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            self._set(i, "refused", message=f"the reviewed head {sha[:10] or '(none)'} is not in {self.repo}")
            return h
        base = _git(self.repo, "merge-base", tip, sha).stdout.strip()
        if not base:
            self._set(i, "refused", message=f"{sha[:10]} shares no history with {self.target}")
            return h
        top = sha
        while top != base and re.search(rf"^{push.BUMP_TRAILER}: ",
                                        _git(self.repo, "log", "-1", "--format=%B", top).stdout, re.M):
            parent = _git(self.repo, "rev-parse", "--verify", "--quiet", f"{top}^").stdout.strip()
            if not parent:
                break
            top = parent
        if i in self.held:   # its push needs a token scope the credential lacks (_hold_scoped)
            return h
        if top == base or (on := self._on_tip(tip, base, top, str(e.get("branch") or ""))):
            say(f"entry {e.get('id')} ({e.get('branch')}): already on {self.target}"
                + (f" (carried by {on[:10]})" if top != base and on != tip else ""))
            self._set(i, "landed")
            self.landed_at[i] = tip if top == base else on
            return h
        subjects = [s for s in _git(self.repo, "log", "--no-merges", "--reverse", "--format=%s",
                                    f"{base}..{top}").stdout.splitlines() if s.strip()]
        brings = bool(csdir) and any(f.endswith(".md") for f in _git(
            self.repo, "diff", "--name-only", "--diff-filter=A", base, top, "--", csdir).stdout.split())
        if csdir and (csfile := self.cfg.get("changeset_file") or "") and not brings:
            # an entry that edits the release changeset itself brings its own changeset
            brings = csfile in _git(self.repo, "diff", "--name-only", base, top, "--", csfile).stdout.split()
        self.info[i] = {"subjects": subjects, "changeset": brings, "top": top, "base": base}
        if not checks and (code := push.code_paths(self.repo, base, top)):
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            self._set(i, "refused", message=f"no checks configured, and this change touches more than "
                                            f"docs ({', '.join(code[:3])}{more}): " + push.NO_CHECKS)
            return h
        if why := push.scalar_problem(self.d.get("push_exclude_paths")):   # never push unguarded
            self._set(i, "refused", message=f"delivery.push_exclude_paths: {why}")
            return h
        if bad := push.excluded(self.repo, tip, top, push.exclude_list(self.d.get("push_exclude_paths"))):
            self._set(i, "refused", message=push.excluded_refusal(bad, self.target))
            return h
        self._settling = ([], set(), [])
        resolved: dict[str, list[str]] = {}
        own, gone = self._landed(h, base, top, resolved, str(e.get("branch") or ""))
        if hits := self._markers([c for c in own if c not in gone]):
            # A commit with a conflict marker never lands: sent back as a non-textual conflict, its
            # fix runs at standard (conflictfix.tier_for) and rewrites the commits.
            say(f"entry {e.get('id')} ({e.get('branch')}): {hits[0]}: a commit adds a conflict marker")
            self._set(i, "conflict", files=[f"(the rebase stopped: {conflictfix.marker_refusal(hits)})"],
                      onto=h, _after=len(carried))
            return h
        if gone:
            left = len(own) - len(gone)
            say(f"entry {e.get('id')} ({e.get('branch')}): {len(gone)} of its {len(own)} commits are on "
                f"{h[:10]} already (" + ", ".join(f"{c[:10]} as {v[:10]}" for c, v in list(gone.items())[:5])
                + (f", +{len(gone) - 5} more" if len(gone) > 5 else "")
                + (f"); replaying the other {left}" if left else ")"))
        if resolved := {c: v for c, v in resolved.items() if c in gone}:
            self.dropped[i] = [{"commit": c, "as": gone[c], "files": v} for c, v in resolved.items()]
            for c, v in resolved.items():
                say(f"entry {e.get('id')}: left out {c[:10]} as landed by {gone[c][:10]}, which resolved a "
                    f"conflict in its lines of {', '.join(v)} and accounts for every line it adds there")
        new, files = (h, None) if gone and len(gone) == len(own) else self._rebase(h, top, drop=set(gone))
        if files is None and self._settling[0]:
            self.settled[i] = self._settled_now()
            if not gone and all(release_kind(f, self.cfg) for f in self._settling[0]) \
                    and own_diff(self.repo, h, new, self.cfg) != own_diff(self.repo, base, top, self.cfg):
                # Only release files conflicted, so the approval holds only while the change itself
                # is the one reviewed; a replay that changed it goes back as a conflict.
                new, files = h, ["(the rebase settled only release files, but the change's own diff "
                                 "is no longer the approved one)"]
                self.settled.pop(i)
            elif "union" in self._settling[1]:
                if why := self._union_lost(gone, base, top, h, new):
                    new, files = h, [f"(the rebase kept both sides' lines of {', '.join(self._union_files())}, "
                                     f"but {why})"]
                    self.settled.pop(i)
                else:
                    self.settled[i]["approval_carried"] = (
                        f"its only conflicts were in shared docs ({', '.join(self._union_files())}), settled by "
                        "keeping the lines both sides added; its own diff outside them is the approved one")
                    say(f"entry {e.get('id')} ({e.get('branch')}): approval carried: "
                        + self.settled[i]["approval_carried"])
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
            return new
        return h

    def _replay_fault(self, i: int, e: dict, h: str, carried: list[tuple[int, str]], ex: Exception) -> None:
        """Entry `i` raised while it was replayed: abort what it left in the worktree and send it
        back as a conflict that is no textual one (its fix rebases it onto `h` by hand), naming the
        error, so the entries after it still go and the error is never every batch's. The batch's
        own fault is raised instead (an error of the whole batch, every entry requeued): the same
        error in every entry so far (two or more), or a worktree it cannot reset."""
        import traceback
        where = traceback.extract_tb(ex.__traceback__)[-1:]
        at = f" at {Path(where[0].filename).name}:{where[0].lineno}" if where else ""
        why = f"{type(ex).__name__}: {ex}{at}"
        key = f"{type(ex).__name__}{at}"
        if self._faults and all(k == key for k in self._faults):
            raise ex
        self._faults.append(key)
        say(f"entry {e.get('id')} ({e.get('branch')}): the batch could not replay it ({why}); sent back, "
            "the rest goes on")
        carried[:] = [c for c in carried if c[0] != i]
        self.settled.pop(i, None)
        self.dropped.pop(i, None)
        self.conflicted.discard(i)
        try:
            if _rebasing(self.wt):
                _git(self.wt, "rebase", "--abort")
            self._checkout(h)
        except Exception as e2:     # a broken worktree is the batch's fault, not the entry's
            raise ex from e2
        self._set(i, "conflict", files=[f"(the rebase could not run in the batch: {why[:400]})"], onto=h,
                  error=why[:2000], _after=len(carried))

    def _settled_now(self) -> dict:
        """The settled detail of the replay just run: files, kinds and, for shared docs, each one's
        resolution ("doc_merges": _doc_merge)."""
        files, kinds, docs = self._settling
        return {"settled": sorted(set(files)), "settled_by": sorted(kinds), **({"doc_merges": docs} if docs else {})}

    def _doc_merge(self, wt: Path, path: str, onto: str) -> dict | None:
        """The record of a stopped rebase's conflict in shared doc `path` (delivery.mechanical_merge_paths),
        read before it is settled: its conflicting hunks (_doc_hunks, at most 20), the entry's commit
        being replayed and the last commit of `onto` that changed the doc. None for another file."""
        import fnmatch
        if not any(fnmatch.fnmatchcase(path, g) for g in self.mechanical):
            return None
        ours, theirs = _show(wt, 2, path), _show(wt, 3, path)
        hunks = _doc_hunks(ours, _show(wt, 1, path) or b"", theirs) if ours is not None and theirs is not None else None
        return {"file": path, "hunks": (hunks or [])[:20],
                "commit": _git(wt, "rev-parse", "--verify", "--quiet", "REBASE_HEAD").stdout.strip(),
                "landed": _git(wt, "rev-list", "-1", onto, "--", path).stdout.strip()}

    def _union_files(self) -> list[str]:
        return sorted(set(self._settling[0])) if self._settling else []

    def _union_lost(self, gone: dict, base: str, top: str, onto: str, new: str) -> str:
        """Why a replay onto `onto` that kept both sides' lines of a shared doc (settle_union) does not
        carry the approval of base..top, or "": commits of it left out as landed, or a change of its own
        diff outside delivery.mechanical_merge_paths and the release files (own_diff), or a line of a
        settled doc the change deletes that the union brought back (_union_revived)."""
        if gone:
            return "some of its commits were left out as landed"
        if own_diff(self.repo, onto, new, self.cfg, self.mechanical) != own_diff(self.repo, base, top, self.cfg,
                                                                                 self.mechanical):
            return "the change's own diff outside them is no longer the approved one"
        import fnmatch
        for f in self._union_files():
            if any(fnmatch.fnmatchcase(f, g) for g in self.mechanical) and (
                    ln := self._union_revived(f, base, top, onto, new)) is not None:
                return f"it brings back a line the change deletes from {f}: {ln[:120]!r}"
        return ""

    def _union_revived(self, path: str, base: str, top: str, onto: str, new: str) -> str | None:
        """A non-blank line base..top deletes from `path` that the replay onto..new holds more often
        than a faithful merge would: the onto side's count, plus the change's own net count of it,
        plus the deletions both sides made (each counted once), or None."""
        out = _git(self.repo, "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0", base, top,
                   "--", path).stdout.splitlines()
        body = out[next((k for k, ln in enumerate(out) if ln.startswith("@@")), len(out)):]   # past the header
        gone = Counter(ln[1:] for ln in body if ln.startswith("-") and ln[1:].strip())
        if not gone:
            return None
        added = Counter(ln[1:] for ln in body if ln.startswith("+"))

        def lines(rev: str) -> Counter:
            r = _git(self.repo, "show", f"{rev}:{path}")
            return Counter(r.stdout.splitlines() if r.returncode == 0 else [])
        at_base, at_onto, got = lines(base), lines(onto), lines(new)
        for ln, n in gone.items():
            both = min(n, max(0, at_base[ln] - at_onto[ln]))
            if got[ln] > at_onto[ln] + added[ln] - n + both:
                return ln
        return None

    def _markers(self, commits: list[str]) -> list[str]:
        """conflictfix.stray_markers of the entry `commits` this batch would replay."""
        return conflictfix.stray_markers(str(self.repo), "--no-walk=unsorted", *commits, timeout=None) if commits else []

    def _on_tip(self, tip: str, base: str, top: str, branch: str = "") -> str:
        """The commit on `tip` that carries the last of base..top's commits when every one is there
        already (_landed), else ""."""
        own, gone = self._landed(tip, base, top, branch=branch)
        if not own or len(gone) < len(own):
            return ""
        order = _git(self.repo, "rev-list", f"{base}..{tip}").stdout.split()   # newest first
        return min(gone.values(), key=lambda c: order.index(c) if c in order else len(order))

    def _landed(self, onto: str, base: str, top: str, resolved: dict[str, list[str]] | None = None,
                branch: str = "") -> tuple[list[str], dict[str, str]]:
        """The commits of base..top (merges aside), and those of them already on `onto`, each with
        the commit there that carries it: one naming it in a REPLAYED trailer (a batch that settled
        its replay, or a squash that carries several), else the landing of another task's approved
        head the entry was built on (push.landed_deps: --onto <tip> <that head>), else one with the same patch (`git patch-id
        --stable`, as git cherry), else a newer rewrite of it (_rewritten: it landed
        conflict-resolved; `resolved` gets those found only through a conflict the landing resolved), else
        a carried copy of another review's commit that landed reworked and that `onto` holds whole
        (_carried_rewrites; `branch` is the entry's own, whose commits are never another's). Replaying such a commit again could only conflict with its own landing. Oldest first; the first
        carrier found wins."""
        own = _git(self.repo, "rev-list", "--reverse", "--no-merges", f"{base}..{top}").stdout.split()
        if not own or base == onto:
            return own, {}
        gone: dict[str, str] = {}
        log = _git(self.repo, "log", "--reverse", "--no-merges", "--format=%x00%H%n%B", f"{base}..{onto}").stdout
        for chunk in log.split("\0")[1:]:
            sha, _, body = chunk.partition("\n")
            for c in re.findall(rf"^{REPLAYED}: ([0-9a-f]{{40,64}})[ \t]*$", body, re.M):
                gone.setdefault(c, sha)
        named = dict(gone)
        if getattr(self, "_heads", None) is None:
            self._heads = push.landed_heads(self.p.db) if getattr(self, "p", None) else {}
        for c, sha in push.landed_deps(self.repo, base, top, onto, self._heads).items():
            gone.setdefault(c, sha)
        if rest := [c for c in own if c not in gone]:
            mine = _patch_ids(self.repo, ["--no-walk=unsorted", *rest])
            theirs = {pid: c for c, pid in reversed(_patch_ids(self.repo, ["--reverse", f"{base}..{onto}"]).items())}
            for c in rest:
                if mine.get(c) in theirs:
                    gone[c] = theirs[mine[c]]
        if rest := [c for c in own if c not in gone]:
            gone.update(_rewritten(self.repo, onto, base, rest, resolved))
        if rest := [c for c in own if c not in gone]:
            gone.update(self._carried_rewrites(onto, rest, named, branch))
        return own, {c: gone[c] for c in own if c in gone}

    def _carried_rewrites(self, onto: str, rest: list[str], named: dict[str, str], branch: str) -> dict[str, str]:
        """{commit: the landing carrying it} for the commits of `rest` an entry carries from another
        review that landed them rewritten (reworked after a conflict or a finding: a new patch, often a
        new subject, so neither patch id nor _rewritten matches the carried copy). A copy is known by the
        author, author date and subject a rebase keeps: shared with a commit a REPLAYED trailer on `onto`
        names (`named`: {commit: the landing naming it}, a committer date no newer than that landing's),
        or with a commit of another branch's approved head whose landing (landed_heads, the review chain)
        is on `onto`. Dropped only when `onto` holds all of the copy's work (_held); one that only partly
        landed is replayed and conflicts as before."""
        mine = _idents(self.repo, "--no-walk=unsorted", *rest)
        key = {(m[0], m[1]): c for c, m in mine.items()}
        found: dict[str, str] = {}
        if named:
            for x, (who, subject, _, _) in _idents(self.repo, "--ignore-missing", "--no-walk=unsorted",
                                                    *named).items():
                c = key.get((who, subject))
                if c and c not in found and _cdate(self.repo, named[x]) >= mine[c][3]:
                    found[c] = named[x]
        heads = self._heads if branch and getattr(self, "p", None) and len(found) < len(rest) else {}
        if heads:   # the entry's own earlier heads are its own work, never carried
            of = push.head_branches(self.p.db, heads)
            heads = {d: sha for d, sha in heads.items() if of.get(d, branch) != branch}
        if heads:
            for x, (who, subject, _, _) in _idents(self.repo, "--ignore-missing", *heads, f"^{onto}").items():
                if (c := key.get((who, subject))) and c not in found:
                    for d, sha in heads.items():
                        if push.descends(self.repo, x, d) and push.descends(self.repo, sha, onto):
                            found[c] = sha
                            break
        return {c: v for c, v in found.items() if _held(self.repo, c, onto, mine[c][2])}

    def _rebase(self, onto: str, top: str, drop: set | frozenset = frozenset()) -> tuple[str, list[str] | None]:
        """Rebase the entry's commits up to `top` onto `onto` in the worktree, settling conflicts that
        need no judgment (the push checks then run on the result, as on any other): (new head, None),
        or (onto, conflicted files) after aborting a real one. The files and kinds settled go to
        self._settling. An entry already on `onto` goes as it is, and one with merges keeps them
        (--rebase-merges): a plain rebase flattens a merge landing and rewrites its commits, so their
        tags drop out. The commits in `drop` (on `onto` already: _landed) are left out of the replay."""
        self.steps.go("rebase")
        wt = self.wt
        version_files = self.cfg["files"] if self.cfg else []
        if push.descends(wt, onto, top):
            self._checkout(top)
            return top, None
        merges = ["--rebase-merges"] if _git(wt, "rev-list", "--merges", "-1", top, f"^{onto}").stdout.strip() else []
        r = _git(wt, "rebase", "--no-keep-empty", *merges, onto, top) if not drop else _git(
            wt, *DROP_CONFIG, "rebase", "--interactive", "--no-keep-empty", *merges, onto, top,
            env={"GIT_SEQUENCE_EDITOR": f"{shlex.quote(sys.executable)} -c {shlex.quote(DROP_TODO)} "
                                        + " ".join(sorted(drop))})
        for _ in range(10000):
            if r.returncode == 0:
                return _git(wt, "rev-parse", "HEAD").stdout.strip(), None
            stopped = _rebasing(wt)
            files = [f for f in _git(wt, "diff", "--name-only", "-z", "--diff-filter=U").stdout.split("\0")
                     if f] if stopped else []
            kinds: set = set()
            docs = [d for f in files if (d := self._doc_merge(wt, f, onto))]
            real = [f for f in files if not settle_release(wt, f, self.cfg, kinds)
                    and not settle(wt, f, version_files, hunks=True, imports=True, kinds=kinds)
                    and not settle_union(wt, f, self.mechanical, kinds)]
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
            self._settling[2].extend(docs)
            if docs:
                say(f"shared docs merged: {doc_merge_text(docs)}")
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
        bumped = _git(self.wt, "rev-parse", "HEAD").stdout.strip()
        self.unbumped[bumped] = h
        return bumped

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

    def _slot(self, at: str) -> checkcap.Slot:
        """The host-wide checks slot for commit `at`. Its wait is between steps (push.Steps), so an upgrade
        past upgrade.push_hold_min may go then; once it is held the checks step is marked."""
        self.steps.between()
        slot = push.check_slot(at, say, checkcap.label(self.p.name, f"push batch {self.id}"),
                               priority=push.priority_key(self.repo, self.remote, self.branch), proj=self.p)
        self.steps.go("checks")
        return slot

    def _check_env(self) -> dict:
        """The env the batch's checks run in (push.check_env), in the project's pinned Python env."""
        return push.check_env("target", self.tip, self.check_run_dir, self.branch,
                              lock=push.runnerenv.lock_of(self.p.config(), self.wt))

    def _check(self, head: str, checks: list[str]) -> tuple[str, str] | None:
        """Check `head` in the worktree: None when it passes, else (command, output tail). The stale
        version guard (push.stale_versions, with `delivery.plugin_version_guard` on) comes first and
        runs no command. A check none of whose failing tests is new on `head` passes (_judged)."""
        if self.checks_at is None:
            self.checks_at = time.time()
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
        try:
            env = self._check_env()
        except push.runnerenv.Unavailable as e:
            say(f"{head[:10]}: {e}")
            return "python env", str(e)
        tree, run = pushpass.tree_of(self.wt, head), []
        for n, cmd in enumerate(todo):
            parts = pushpass.env_parts(self.wt, cmd, env, self.tip or "")
            eid = pushpass.digest(parts)
            if hit := pushpass.passed(self.p, tree, str(cmd), eid):
                say(f"reused pass from {hit.get('batch') or '?'} (tree {tree[:10]}): {cmd}")
                self.checks["reused"] = self.checks.get("reused", 0) + 1
                self._passed(cmd)
            elif carried := self._carried(head, cmd, parts) or self._task_carried(head, cmd, parts, todo[:n], env):
                say(f"{carried}: {cmd}")
                self.checks.setdefault("carried", []).append(f"{carried}: {cmd}")
                self._passed(cmd)
                if why := pushpass.record(self.p, tree, str(cmd), eid, self.id, parts, head, self.tip or "", carried):
                    say(f"could not record the pass of {cmd}: {why}")
            else:
                if why := pushpass.missed(self.p, tree, str(cmd), parts):
                    say(f"not reusing a pass: {why}: {cmd}")
                run.append((cmd, eid, parts))
        if not run:
            return None
        self._runners([c for c, _, _ in run], env)
        suite_out: list[str] = []
        slot = self._slot(head)
        started = time.time()   # a wait for the host-wide slot is not check time
        slow = Path(self.check_run_dir) / slowtests.FILE if self.check_run_dir else None
        if slow:
            slow.unlink(missing_ok=True)   # this head's run only
        try:
            for line in push.sandbox_lines([cmd for cmd, _, _ in run]):
                say(line)
            for cmd, eid, parts in run:
                resume = pushpass.resume_file(self.p, tree, str(cmd), eid) if tree and pushpass.resumable(cmd) else None
                extra = pushpass.resume_env(resume, env) if resume else {}
                if slow and pushpass.resumable(cmd) and not push.remote_host(cmd):
                    extra |= slowtests.env(slow, {**env, **extra})
                if done := pushpass.resumed(resume) if resume else []:
                    say(f"resuming {cmd}: {len(done)} test file(s) passed in an earlier run on tree {tree[:10]}; running the rest")
                if push.remote_host(cmd):
                    say(f"running {cmd}{push.where(cmd)}")
                rc, tail = self._run_check(cmd, {**env, **extra, **slot.env()}, env)
                suite_out.append(tail)
                if fault := push.remote_fault(cmd, rc, self.wt):
                    raise envfault.Fault(fault)   # the check host, not this head: push_phase holds the batch
                if rc != 0:
                    say(f"check failed on {head[:10]}: {cmd}{push.where(cmd)}")
                    self.fail_rc[tail] = rc
                    if self._judged(head, cmd, rc, tail, env, slot.env()):
                        if resume:
                            resume.unlink(missing_ok=True)
                        self._passed(cmd)
                        continue
                    return cmd, tail
                self._passed(cmd)
                if why := pushpass.record(self.p, tree, str(cmd), eid, self.id, parts, head, self.tip or ""):
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

    def _passed(self, cmd) -> None:
        """Note a check that passed (ran, reused or carried) in checks.passed: its script_torn count clears (pushq)."""
        if str(cmd) not in self.checks.setdefault("passed", []):
            self.checks["passed"].append(str(cmd))

    def _run_check(self, cmd, run_env: dict, env: dict) -> tuple[int | None, str]:
        """Run one check in the worktree: (exit code, output tail). A failure whose only failures are lost
        test workers runs once more (workerloss); lost again, every worker exiting cleanly, it raises
        workerloss.Infra: the host, never a failure of the batch's entries. A failure on a transient syntax
        error in a script outside the change runs once more too (syntaxfault); so again, it raises
        syntaxfault.Infra. One that failed before any test ran runs again up to setupfault.RERUNS times (a pool
        with no free slot not at all); still so, it raises setupfault.Infra."""
        collect = workerloss.Collector()

        def once():
            collect.reset()
            return _stream(_check_argv(cmd, self.wt), self.wt, env=run_env, pass_fds=checkdirs.inherited(),
                           collect=collect, urgent_mark=self._urgent_mark())
        reruns = torn = setup = 0
        while True:
            rc, tail, _ = scriptwatch.rerun(once, lambda r: r[0] != 0, None if push.remote_host(cmd) else str(cmd),
                                            self.wt, env, say, on_pass=True)
            if rc == 0 or push.remote_host(cmd):
                return rc, tail
            if found := syntaxfault.transient(collect.text(), self.wt, self.tip):
                if torn:
                    raise syntaxfault.Infra(found, str(cmd))
                torn = 1
                say(syntaxfault.rerun_line(found, str(cmd)))
                time.sleep(syntaxfault.SETTLE_S)
                continue
            if rc != push.BUSY and (fault := setupfault.found(collect.setup, self.wt)):
                if fault["kind"] == "pool" or setup >= setupfault.RERUNS:
                    raise setupfault.Infra(fault, str(cmd))
                setup += 1
                say(setupfault.rerun_line(fault, str(cmd), setup))
                continue
            loss = workerloss.lost(collect.text())
            if reruns >= workerloss.RERUNS:
                if workerloss.infra(loss):
                    raise workerloss.Infra(loss, str(cmd))
                return rc, tail
            if not workerloss.only(loss):
                return rc, tail
            reruns += 1
            self.checks["worker_loss_reruns"] = self.checks.get("worker_loss_reruns", 0) + 1
            say(workerloss.rerun_line(loss, str(cmd)))

    def _carried(self, head: str, cmd, parts: dict) -> str:
        """The record line (verdictcarry.line) of a pass of `cmd` a batch recorded on another tip that
        carries to `head` on this one, or "": the same change, and nothing the check covers changed."""
        exclude = push.exclude_list(self.d.get("push_exclude_paths"))
        for e in pushpass.carriable(self.p, str(cmd), parts)[:CARRY_TRIES]:
            if e["tip"] == self.tip:
                continue   # the same tip: only an exact tree match (pushpass.passed) reuses it
            delta = verdictcarry.carry(self.wt, cmd, e["tip"], e["head"], self.tip or "", head, exclude)
            if delta is not None:
                return verdictcarry.line(e["head"], delta)
        return ""

    def _bump_only(self, head: str) -> str | None:
        """The replayed head under `head` when `head` is it plus this batch's own version bump (_head)
        touching only delivery.version_bump's files and changesets, or `head` itself with no such bump;
        None when the bump commit changed anything else or cannot be read."""
        under = self.unbumped.get(head)
        if under is None:
            return head
        names = verdictcarry._names(self.wt, under, head)
        if names is None or not self.cfg:
            return None
        own = {*(self.cfg.get("files") or ()), *([self.cfg["changeset_file"]] if self.cfg.get("changeset_file") else [])}
        csdir = (self.cfg.get("changeset_dir") or "").strip("/")
        return under if all(f in own or (csdir and f.startswith(csdir + "/")) for f in names) else None

    def _task_carried(self, head: str, cmd, parts: dict, before: list, env: dict) -> str:
        """The record line of a full pass of `cmd` a task's own `ttp checks` recorded (pushpass.task_carriable:
        the same push checks in order, never a targeted run or a single command's suite pass) that carries
        to `head` under verdictcarry's rule, or "". It is judged on the replayed head under this batch's
        version bump (_bump_only): the bump is the batch's own mechanical edit of the version files and a
        changeset, so the change's patch must match the one that passed and the upstream delta must miss
        what the check covers; a batch of several entries has another patch than any one task's."""
        if cmd == STALE or not (under := self._bump_only(head)):
            return ""
        exclude = push.exclude_list(self.d.get("push_exclude_paths"))
        for e in pushpass.task_carriable(self.p, self.wt, [*before, cmd], parts, env)[:CARRY_TRIES]:
            delta = verdictcarry.carry(self.wt, cmd, e["tip"], e["head"], self.tip or "", under, exclude)
            if delta is not None:
                return f"{verdictcarry.line(e['head'], delta)} (the task's own ttp checks run {e.get('run') or '?'})"
        return ""

    def _rerun_ids(self, cmd, rc: int | None, tail: str) -> list[str]:
        """The failing pytest ids of a failed check to rerun: none when the check is not one plain local
        pytest run (pushpass.resumable), it did not exit 1 (tests failed; `rc` None: not known) or pytest
        stopped early (-x, --maxfail, an interrupt: sharedchecks.stopped_early, so later tests never ran),
        its list of failing ids is incomplete (sharedchecks.failing_ids) or it names more than RERUN_IDS."""
        if cmd == STALE or push.remote_host(cmd) or not pushpass.resumable(cmd):
            return []
        if rc not in (1, None) or sharedchecks.stopped_early(tail):
            return []
        ids, complete = sharedchecks.failing_ids(tail)
        if not complete or not ids or len(ids) > RERUN_IDS:
            return []
        return ids

    def _run_ids(self, at: str, cmd, ids: list[str], where: str, env: dict, slot_env: dict) -> tuple[int | None, str]:
        """Run only `ids` of check `cmd` once at commit `at`: the check's own command, its other tests
        deselected by name (pushpass.select_env), killed after delivery.push_rerun_s. (pytest's exit
        code, None when it ran out of time; the output tail)."""
        self.reran.setdefault(where, set()).update(ids)
        self._checkout(at)
        self.checks["reruns"] = self.checks.get("reruns", 0) + 1
        rc, tail, late = _stream(_check_argv(cmd, self.wt), self.wt,
                                 env={**env, **pushpass.select_env(gatejudge.select_rx(ids), env), **slot_env},
                                 timeout=_seconds(self.d.get("push_rerun_s"), RERUN_S), group=True,
                                 pass_fds=checkdirs.inherited(), urgent_mark=self._urgent_mark())
        return (None if late else rc), tail

    def _judged(self, head: str, cmd, rc: int | None, tail: str, env: dict, slot_env: dict) -> bool:
        """A failed check judged as `ttp checks` judges one (gatejudge.judge): its failing tests (_rerun_ids)
        not yet judged at this `head` rerun once there, those failing again on the tip (the base) up to
        TIP_RERUNS times, on this host; a base result saved for the tip on this host counts without a rerun,
        and a quarantined test the batch does not touch is excused at once. True when none is new on
        `head`: each flaky (passed on the rerun: quarantined, the marker's flaky_tests), pre-existing (failed
        on the tip too: the marker's pre_existing) or quarantined. The check then counts as passed in this
        batch only: no pass is recorded for the tree. Else the new ids are kept for the search
        (self.new_ids, _ids_check) and the failure stands; a rerun that cannot tell keeps it too."""
        ids = self._rerun_ids(cmd, rc, tail)
        if not ids:
            return False
        todo = [i for i in ids if (head, i) not in self.judged]
        if todo:
            say(f"judging the {len(todo)} failing test(s) on {head[:10]}: a rerun there, then on the tip "
                f"{(self.tip or '')[:10]} the ones failing again: {' '.join(todo)}")
            try:
                v = self._verdict(head, cmd, todo, env, slot_env)
            finally:
                self._checkout(head)
            for line in gatejudge.lines(v):
                say(line)
            for i in todo:
                self.judged[(head, i)] = i in v["new"] or i in v["busy"]
        new = [i for i in ids if self.judged[(head, i)]]
        if new:
            self.new_ids[tail] = new
            say(f"new on {head[:10]}, the failure stands: {' '.join(new)}")
            return False
        say(f"none of the failing tests is new on {head[:10]}: the batch goes on")
        return True

    def _verdict(self, head: str, cmd, ids: list[str], env: dict, slot_env: dict) -> dict:
        """gatejudge.judge of `ids` of check `cmd` at `head`, its base the tip (checks.judge false: no base and
        no quarantine, only the rerun at `head`). Records the flaky ones (quarantine, flaky_tests) and the
        pre-existing ones (pre_existing)."""
        s, now = gatejudge.settings(self.p.config()), time.time()
        base, where = (self.tip or "") if s["on"] else "", gatejudge.host()
        diff = _git(self.wt, "diff", "--name-only", f"{base}..{head}") if base else None
        changed = diff.stdout.split() if diff is not None and diff.returncode == 0 else None

        def touched(i: str) -> bool:   # the batch touches the test's file (unknown, or judging off: it does)
            f = i.partition("::")[0]
            return changed is None or any(x == f or x.endswith("/" + f) for x in changed)

        def run(at: str, sel: list[str]) -> tuple[int | None, str]:
            if at == "head":
                return self._run_ids(head, cmd, sel, "head", env, slot_env)
            got: tuple[int | None, str] = (None, "")
            for n in range(1, TIP_RERUNS + 1):   # one failing run on the tip makes it pre-existing
                got = self._run_ids(base, cmd, sel, "tip", env, slot_env)
                self.checks["tip_reruns"] = self.checks.get("tip_reruns", 0) + 1
                self.tip_runs = n
                if got[0] != 0:
                    break
            return got
        v = gatejudge.judge(ids, run, self.p.state, base, where, now, touched, tries=0)
        if v["flaky"]:
            self.flaky.append({"ids": v["flaky"], "head": head, "cmd": str(cmd)})
            self.checks["flaky"] = True
        if v["flaky"] and s["on"]:
            gatejudge.quarantine(self.p.state, v["flaky"], {"head": head, "check": str(cmd), "host": where,
                                                            "batch": self.id}, now, s["days"])
        if v["pre_existing"]:
            self.pre_existing.append({"ids": v["pre_existing"], "tip": base, "cmd": str(cmd), "entry": None,
                                      "head": head, "runs": self.tip_runs, "of": TIP_RERUNS})
        return v

    def _ids_check(self, head: str, fail: tuple[str, str], ids: list[str]) -> tuple[str, str] | bool | None:
        """A split probe of `head`: only the tests `ids` new in failure `fail` (_judged), not its whole suite.
        None when they pass, are not there, or none failing is new on `head` (_judged); (command, output
        tail) when one is new there; False when the probe cannot tell (out of time, another exit): the
        full checks decide then."""
        cmd = fail[0]
        env = push.check_env("target", self.tip, self.check_run_dir, self.branch)
        self._runners([cmd], env)
        slot = self._slot(head)
        started = time.time()
        try:
            say(f"split probe of {head[:10]}: only {' '.join(ids)}")
            self.checks["id_probes"] = self.checks.get("id_probes", 0) + 1
            rc, tail = self._run_ids(head, cmd, ids, "probe", env, slot.env())
            if rc in (0, NO_TESTS):
                return None
            if rc != 1:
                say(f"the split probe of {head[:10]} " + ("ran out of time" if rc is None else f"exited {rc}")
                    + ": the full checks decide")
                return False
            self.fail_rc[tail] = rc
            return None if self._judged(head, cmd, rc, tail, env, slot.env()) else (str(cmd), tail)
        finally:
            slot.close()
            self.checks["seconds"] = round(self.checks["seconds"] + time.time() - started, 1)

    def _tip_ids(self, tip: str, fail: tuple) -> tuple[str, str] | bool | None:
        """The search is about to blame an entry for a failed check: rerun only its failing tests
        (_rerun_ids) on `tip`, without the batch, up to TIP_RERUNS times. (cmd, tail) when one rerun fails
        there (the failure is the tip's own, flaky or already failing, not the entry's), True when every
        rerun passes or the tests are not on the tip (the entry's own failure), None when that cannot tell
        (no such ids, ids rerun on the tip in this batch already, a rerun out of time or one pytest ends
        with neither). A failure whose new tests are known (_judged: self.new_ids) is judged on those alone,
        as the others failed on the tip or were excused already. Base results saved for the tip on this
        host (gatejudge) count without a rerun: all passing there, True. The batch log and the marker's
        checks["tip_reruns"] keep the counts."""
        cmd = fail[0]
        ids = self.new_ids.get(fail[1]) or self._rerun_ids(cmd, self.fail_rc.get(fail[1]), fail[1])
        if not ids:
            return None
        saved = gatejudge.saved_base(self.p.state, gatejudge.host(), tip, ids, time.time())
        if len(saved) == len(ids) and not any(saved.values()):
            say(f"they passed on the tip {tip[:10]} when the batch judged them: {' '.join(ids)}")
            return True
        if self.reran["tip"] & set(ids):
            return None
        say(f"rerunning the {len(ids)} failing test(s) on the tip {tip[:10]} alone, up to {TIP_RERUNS} times: "
            f"{' '.join(ids)}")
        try:
            env = self._check_env()
        except push.runnerenv.Unavailable:
            return None
        self._runners([cmd], env)
        slot = self._slot(tip)
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
        todo = [c for c in todo if pushpass.resumable(c)]
        if not todo:
            return False
        try:
            env = self._check_env()
        except push.runnerenv.Unavailable:
            return False
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
                        slot = self._slot(head)
                        started = time.time()
                    say(f"cap probe of {head[:10]}: {cmd}")
                    rc, tail, _ = _stream(_check_argv(cmd, self.wt), self.wt,
                                          env={**env, **pushpass.select_env(rx, env), **slot.env()},
                                          pass_fds=checkdirs.inherited(), urgent_mark=self._urgent_mark())
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
        was flaky: the full head goes. A failing check none of whose failing pytest ids is new at its head
        (gatejudge: flaky, pre-existing on the tip or quarantined) passes there (_judged). When the full
        head's failure names new ids, the search probes each prefix with only those ids (_ids_check) and
        checks the highest prefix they pass on in full once; a full failure there searches below it in
        full. Before an entry is blamed, its failing ids are rerun on the tip alone up to TIP_RERUNS
        times unless the tip's result is saved already (_tip_ids). For entry 1: failing there once, the tip
        is broken (tip_failed); passing every time, entry 1 is blamed; only when that cannot tell is the
        tip checked alone in full. For a later entry: failing there once, the failure is pre-existing
        (_pre_existing): the prefix that passed goes and nothing is blamed; else it is blamed as before.

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
        # A failure whose new tests are known (_judged) is searched on those tests alone (_ids_check), then
        # the longest prefix that passed them is checked in full once; failing there, the search goes below.
        full_lo = 0
        while not bad and hi - lo > 1 and (ids := self.new_ids.get(fails[hi][1])):
            top = hi
            while hi - lo > 1:
                if self._yield_now():
                    return full_lo, heads[full_lo] if full_lo else None, None
                mid = (lo + hi) // 2
                heads[mid] = self._head(tip, carried, mid)
                f = self._ids_check(heads[mid], fails[top], ids)
                if f is None:
                    lo = mid
                elif isinstance(f, tuple):
                    hi, fails[mid] = mid, f
                else:
                    break
            if hi - lo > 1 or lo == full_lo:
                lo = full_lo          # a probe that cannot tell, or nothing new passed them: the full checks decide
                break
            if self._yield_now():
                return full_lo, heads[full_lo] if full_lo else None, None
            say(f"checking the {lo} entr{'y' if lo == 1 else 'ies'} that passed the split probes in full")
            if (f := self._check(heads[lo], checks)) is None:
                full_lo = lo
                break
            hi, fails[lo], lo = lo, f, full_lo
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
            if self._yield_now():
                return lo, heads[lo] if lo else None, None
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
        if hi == 1 and self._yield_now():
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
        # A failure with tests new on its head already failed twice there (_judged): no full rerun.
        rerun = hi == m and fails[m][0] != STALE and fails[m][1] not in self.new_ids
        if rerun and self._yield_now():
            return lo, heads[lo] if lo else None, None
        if rerun:
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
            self._settling = ([], set(), [])
            new, files = self._rebase(h, self.info[i]["top"])
            if files is not None or new == h or ("union" in self._settling[1] and self._union_lost(
                    {}, self.info[i]["base"], self.info[i]["top"], h, new)):
                break
            carry = self.settled.pop(i, {}).get("approval_carried")
            if self._settling[0]:
                self.settled[i] = self._settled_now()
                if carry and "union" in self._settling[1]:
                    self.settled[i]["approval_carried"] = carry
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
        if self._yield_now():
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

    def _yield_now(self) -> bool:
        """Before each further full check of the search for the entry that broke the checks: whether an
        urgent approval (urgent.approvals_waiting: priority 1 or urgent) now waits for this target outside the
        batch. Then the search stops: the prefix known to pass goes, a blamed entry stays blamed, the
        rest is requeued "yielded", and the queue runs the urgent ones alone next (pushq.due), so they
        never wait out the rest of a bisect. The requeued ones are searched anew on the new tip: no entry
        is blamed on a tip it was not checked on, and the urgent ones are never in that search. Only
        approvals made within delivery.push_yield_grace_s of this batch's first check stop it, and it
        stops once at most (_yield_late)."""
        if not self.yielded_to and self._may_yield():
            try:
                skip = self.m.get("yield_skip") if isinstance(self.m.get("yield_skip"), dict) else {}
                skip = set(skip.get("ids") or []) if self.tip and skip.get("tip") == self.tip else set()
                late = self._yield_late()
                self.yielded_to = [i for i in urgent.approvals_waiting(self.p.db, self.target)
                                   if i not in skip and i not in late]
            except Exception as e:      # a queue it cannot read: the search goes on as before
                say(f"cannot read the push queue for urgent approvals: {type(e).__name__}: {e}")
            if self.yielded_to:
                n = len(self.yielded_to)
                say(f"urgent approval{'s' if n > 1 else ''} {', '.join(map(str, self.yielded_to))} "
                    f"wait{'s' if n == 1 else ''}: the search stops here and {'they go' if n > 1 else 'it goes'} next")
        return bool(self.yielded_to)

    def _yield_late(self) -> set[int]:
        """Queue ids of approved rows for this target approved more than delivery.push_yield_grace_s
        (YIELD_GRACE_S) after this batch's checks began: they never stop it, so a stream of urgent
        approvals cannot restart batch after batch; they go first in the next batch (pushq.due), which
        starts as soon as this one ends."""
        if self.checks_at is None:
            return set()
        try:
            grace = max(0.0, float(self.d.get("push_yield_grace_s", YIELD_GRACE_S)))
        except (TypeError, ValueError):
            grace = YIELD_GRACE_S
        return {r["id"] for r in self.p.db.q("SELECT id FROM push_queue WHERE status='approved' AND target=? "
                                              "AND created>?", (self.target, self.checks_at + grace))}

    def _urgent_mark(self) -> bool:
        """Whether this batch's checks run under an urgent landing mark (_stream): one of its entries is
        an urgent approval (urgent.covers_urgent), as pushq.due starts it at once for."""
        try:
            return any(urgent.covers_urgent(self.p.db, self.p.db.task(e.get("task"))) for e in self.entries)
        except Exception as e:          # a queue it cannot read: a routine mark, the checks run as before
            say(f"cannot read the batch's tasks: {type(e).__name__}: {e}")
            return False

    def _may_yield(self) -> bool:
        """Whether this batch may stop its search for an urgent approval at all: never when every entry
        is urgent itself (the ones waiting would only take its place), nor when it carries entries a
        batch before it requeued for one (the marker's "after_yield"), so no two batches in a row stop
        for the same set and the queue cannot loop on yields."""
        if self.m.get("after_yield"):
            return False
        try:
            return not all(urgent.covers_urgent(self.p.db, self.p.db.task(e.get("task"))) for e in self.entries)
        except Exception as e:          # a queue it cannot read: no yield, the search goes on as before
            say(f"cannot read the batch's tasks: {type(e).__name__}: {e}")
            return False

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
    """Step 12: run `delivery.after_push` at the pushed commit, in a fresh detached scratch clone
    (cleantree.clone: never a worktree in the shared .git, whose churn breaks sandboxes starting
    meanwhile), under `delivery.after_push_timeout_s`. Its output goes to the batch log."""
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
    _remove_scratch(repo, wt)
    out = {"status": "ok", "exit": 0, "started": started, "tail": ""}
    try:   # the scratch borrows the repository's objects: it goes as soon as this step ends
        try:
            wt.parent.mkdir(parents=True, exist_ok=True)
            cleantree.clone(repo, wt, sha)
        except (cleantree.Error, OSError, subprocess.SubprocessError) as e:
            say(f"after_push: cannot check out {sha[:10]}: {_last(str(e), 3)}")
            return {"status": "failed", "exit": None, "cmd": "git clone --shared", "started": started,
                    "ended": time.time(), "tail": _last(str(e))}
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
        _remove_scratch(repo, wt)
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
             pre_existing=b.pre_existing or None, yielded_to=b.yielded_to or None)
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
        for x in d.get("dropped_as_resolved") or []:
            if isinstance(x, dict):
                print(f"    left out {str(x.get('commit'))[:10]} as landed by {str(x.get('as'))[:10]}, which resolved "
                      f"a conflict in its lines of {', '.join(map(str, x.get('files') or []))}")
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

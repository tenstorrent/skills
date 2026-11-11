# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Git facts read once, before a write transaction, and reused inside it.

Finishing a run (Daemon.finish_run) judges its hand-off inside one db write transaction: a failed
review's area (reviewcap.area: main_file, touched) and each follow-up's file overlaps and chain
(overlap.find, overlap.chain: branches, _on_push_branch, the base and its files, git grep per symbol)
read git there, and a slow repository held the write lock for over a minute. Within scope(), cached() keeps each such fact by its key: the daemon warms them before the
transaction opens, and the same calls inside it read the memo. A key not warmed (a call the warm-up
did not foresee) runs its git as before; a call that raised is not kept. Outside a scope cached()
only runs the call.

bounded() caps, by time, the git a scope runs: each git read (is_read) gets at most `per_call_s`,
and a scope with `total_s` also ends every git call it makes, reads and writes, by its deadline. A call
started before it gets at least FLOOR_S, or the scope's whole budget if smaller (a scope ends at most
FLOOR_S past its deadline); past it, timeout() starts no new call: it raises Spent, a
subprocess.TimeoutExpired naming the budget, never 0 s, and its caller defers the work to the next tick
as it does for a timeout. The daemon's tick steps run in bounded(TICK_GIT_S) and a finish transaction in
bounded(TX_GIT_S, TX_GIT_S), so one slow or hung git call holds neither past that, whatever its call
site (call sites pass their own timeout through timeout()). A git write outside a transaction keeps
its own timeout: killing it midway can leave a lock or a half-removed worktree. A capped call that
times out raises subprocess.TimeoutExpired as before, and its caller degrades as it does for a git
failure. Outside a bounded scope timeout() returns the call's own timeout.
A git check (_CHECKS: check-ref-format, var, version; no repository read, no lock) is an action's own
check: it gets at least PURE_FLOOR_S past a scope's deadline, and a strict round's slice neither refuses
nor counts it, so slow Python before it never fails it with "timed out after 0 seconds".

run() is the one way runtime code runs git (a guard test holds every helper to it). Within an
attempt() (a finish: its warm-up, then its transaction's rounds) it keeps each read's answer in the
gitreplay memo and answers the same read again from it. Within strict() (one round of the finish
transaction, Daemon._finish_rounds) a read the memo lacks runs live only while the round's live git
stays within its slice; past it the read raises GitMiss before spawning, the transaction rolls back,
the read runs outside the lock and the round runs again. The db audit hook calls on_audit(): a raw
git spawn inside a round counts against the slice; a non-git spawn or a socket connect is the round's
point of no return, after which it never aborts: a later miss past the slice runs no git under the
lock: it raises Spent (naming the slice, never a 0 s timeout) and its caller defers the work to the
next tick (the finish's review read: Daemon.retry_unread_reviews). Pure local git (check-ref-format:
a task_add's branch check, via _CHECKS) is never refused. A stop (Attempt.stop: the daemon got SIGTERM) ends an
abortable round before its next live git, spawn or connect, so a stop never waits out the rounds."""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import stepclock

_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ttp_gitmemo", default=None)
# (per-call seconds for a read, monotonic deadline for every call or None, total seconds) of the bounded() scope
_cap: contextvars.ContextVar[tuple[float, float | None, float] | None] = contextvars.ContextVar("ttp_gitcap", default=None)

TICK_GIT_S = 25.0   # one git read a daemon tick step makes, at most (Daemon._run_step)
TX_GIT_S = 20.0     # all git a finish write transaction makes, together (Daemon._finish_tx)
FINISH_REVIEW_GIT_S = 5.0   # each git read a finish makes to queue its review (Daemon._auto_review): a slow one
#   leaves the review to the next tick (KV_REVIEW_UNREAD), never the finish transaction's whole budget
HOLD_SLICE_S = 1.5  # live git one strict round of a finish transaction may run (strict())
MAX_ROUNDS = 50     # rounds of one finish before its last, which never aborts (Daemon._finish_rounds)
FINISH_WALL_S = 90.0    # nor past this long: the reads it learns between rounds share this wall too
PREFETCH_GIT_S = 60.0   # all git one finish's warm-up (Daemon._prefetch_git) runs, together
PURE_FLOOR_S = 10.0   # a git check (check-ref-format, var, version: no repository read, no lock) gets at least this
FLOOR_S = 2.0   # the least a git call started with its bounded() deadline about to pass gets (or the whole budget)
# git's own options that take the next argument (git -C <dir> <verb> ...)
_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
# Verbs that only read the repository (a scratch GIT_INDEX_FILE aside): bounded() caps them anywhere.
READS = frozenset({"rev-parse", "rev-list", "merge-base", "diff", "log", "show", "ls-files", "check-ignore",
                   "cherry", "patch-id", "symbolic-ref", "cat-file", "for-each-ref", "show-ref", "status",
                   "grep", "name-rev", "describe", "ls-tree", "diff-tree", "merge-tree"})
# `git reflog` reads (show, its default, and the rest) but for these subcommands
_REFLOG_WRITES = frozenset({"expire", "delete"})


def _slice_name(rd) -> str:
    return f"finish round's {rd.slice_s:g} s live-git slice"


class Spent(subprocess.TimeoutExpired):
    """A git call not started: its bounded() scope's whole budget was spent. The caller defers the work
    to the next tick (as it does for a timeout); `timeout` is that budget, never 0."""

    def __init__(self, cmd, timeout: float, budget: str = "") -> None:
        super().__init__(cmd, timeout)
        self.budget = budget or f"{timeout:g} s git budget"

    def __str__(self) -> str:
        return f"Command {self.cmd!r} not started: its {self.budget} was spent"


def is_read(args: tuple | list) -> bool:
    """Whether git `args` (from the verb on) only read the repository: a READS verb, or a reflog read."""
    if not args:
        return False
    if args[0] == "reflog":
        return len(args) == 1 or args[1] not in _REFLOG_WRITES
    return args[0] in READS


@contextlib.contextmanager
def scope(memo: dict | None = None) -> Iterator[dict]:
    """A memo for the calls made within it: `memo` (one filled earlier, e.g. before a transaction),
    else the enclosing scope's, else a new one."""
    m = memo if memo is not None else _memo.get()
    token = _memo.set({} if m is None else m)
    try:
        yield _memo.get()
    finally:
        _memo.reset(token)


def current() -> dict | None:
    """The scope's memo, None outside one."""
    return _memo.get()


def cached(key: tuple, fn: Callable[[], Any]) -> Any:
    """fn()'s value, kept under `key` within a scope; just fn() outside one."""
    m = _memo.get()
    if m is None:
        return fn()
    if key not in m:
        m[key] = fn()
    return m[key]


@contextlib.contextmanager
def bounded(per_call_s: float, total_s: float | None = None) -> Iterator[None]:
    """Cap the git timeout() hands out within: a read at most `per_call_s`; with `total_s`, every call
    ends by that many seconds from now. A nested scope only narrows its enclosing one."""
    outer = _cap.get()
    per = per_call_s if outer is None else min(per_call_s, outer[0])
    end = None if total_s is None else time.monotonic() + total_s
    total = total_s or 0.0
    if outer is not None and outer[1] is not None and (end is None or outer[1] < end):
        end, total = outer[1], outer[2]
    token = _cap.set((per, end, total))
    try:
        yield
    finally:
        _cap.reset(token)


def timeout(own: float | None, args: tuple | list = (), read: bool | None = None) -> float | None:
    """A git call's timeout within the bounded() scope: `own` (its own), capped for a read (`read`; by
    default a call that names no `args`, or whose verb is_read()) and, in a scope with a
    deadline, for any call."""
    cap = _cap.get()
    if cap is None:
        return own
    if read is None:
        read = not args or is_read(args)
    t = own
    if read:
        t = cap[0] if t is None else min(t, cap[0])
    if cap[1] is not None:
        left = cap[1] - time.monotonic()
        if args and args[0] in _CHECKS:   # reads no repository: an action's own check never starves on what is left
            left = max(left, PURE_FLOOR_S)
        if left <= 0:   # past the deadline: no new spawn; the caller defers it to the next tick
            raise Spent(["git", *map(str, args)], cap[2] or FLOOR_S)
        # never a near-0 timeout: at most FLOOR_S past the deadline, never past the scope's own budget
        left = max(left, min(FLOOR_S, cap[2] or FLOOR_S))
        t = left if t is None else min(t, left)
    return t


_cap_timeout = timeout


def git_args(argv: list | tuple) -> tuple[str, ...]:
    """`argv`'s git arguments from its subcommand on: git's own options before it (-C, -c ...) skipped."""
    a = [str(x) for x in argv[1:]]
    i = 0
    while i < len(a) and a[i].startswith("-"):
        i += 2 if a[i] in _OPTS_WITH_VALUE else 1
    return tuple(a[i:])


class GitMiss(BaseException):
    """A git call a strict round may not run under the write lock. A BaseException, so a body's
    `except Exception` fallback cannot swallow it and quietly change a decision. `learn`: the driver
    runs it outside the lock (a memoizable read); else it only retries the round."""

    def __init__(self, argv, kw: dict, own: float | None, read: bool | None, why: str, learn: bool):
        super().__init__(f"{why}: {' '.join(map(str, argv))[:200]}")
        self.argv, self.kw, self.own, self.read, self.why, self.learn = argv, kw, own, read, why, learn


class Attempt:
    """One finish: its warm-up and its transaction's rounds share the memo's volatile answers."""
    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.id = next(Attempt._ids)
        self.round = 0
        self.strict: _Round | None = None
        self.last: _Round | None = None
        self.stop: Callable[[], bool] | None = None   # the daemon is stopping: an abortable round ends

    def stopping(self) -> bool:
        return bool(self.stop and self.stop())


class _Round:
    def __init__(self, slice_s: float, abortable: bool) -> None:
        self.slice_s, self.abortable = slice_s, abortable
        self.live_s, self.live_n, self.hits, self.pnr, self.raw_t0 = 0.0, 0, 0, False, None

    def left(self) -> float:
        return self.slice_s - self.live_s

    def abort(self) -> bool:
        return self.abortable and not self.pnr

    def raw_close(self) -> None:
        if self.raw_t0 is not None:
            self.live_s += time.monotonic() - self.raw_t0
            self.raw_t0 = None


_att: contextvars.ContextVar[Attempt | None] = contextvars.ContextVar("ttp_gitattempt", default=None)
_own: contextvars.ContextVar[bool] = contextvars.ContextVar("ttp_gitown", default=False)
# git verbs outside READS that change nothing: they never make a round's point of no return
_PURE = frozenset({"merge-file", "var", "check-ref-format", "version", "hash-object"})
# Of those, the ones that read no file at all: an action's own check (see PURE_FLOOR_S)
_CHECKS = frozenset({"check-ref-format", "var", "version"})


@contextlib.contextmanager
def attempt(a: Attempt | None = None) -> Iterator[Attempt]:
    """Within: git reads are kept in the memo and answered from it (a's, else a new attempt)."""
    a = a or _att.get() or Attempt()
    token = _att.set(a)
    try:
        yield a
    finally:
        _att.reset(token)


@contextlib.contextmanager
def strict(slice_s: float, abortable: bool = True) -> Iterator[_Round]:
    """One round: live git at most `slice_s` in all; past it a miss raises GitMiss (`abortable`) or
    subprocess.TimeoutExpired without spawning."""
    with attempt() as a:
        a.round += 1
        rd = a.strict = _Round(slice_s, abortable)
        a.last = None
        try:
            yield rd
        finally:
            rd.raw_close()
            a.strict, a.last = None, rd


def no_return() -> None:
    """The open strict round never aborts from here on: its caller changes what a rollback cannot undo
    (the daemon's memory, a process started off the thread). Outside a round: nothing."""
    a = _att.get()
    if a is not None and a.strict is not None:
        a.strict.pnr = True


def tx_note() -> str:
    """The slow-tx log's round figures for the transaction just ended, "" outside a round."""
    a = _att.get()
    rd = a and (a.strict or a.last)
    if not rd:
        return ""
    return f"; rounds {a.round}, live git {rd.live_n} ({rd.live_s:.1f} s), memo hits {rd.hits}"


def _pure(args: tuple) -> bool:
    if not args:
        return True
    if args[0] in _PURE:
        return args[0] != "hash-object" or "-w" not in args
    if args[0] == "remote":
        return len(args) == 1 or args[1] in ("get-url", "-v")
    if args[0] == "config":
        return any(a.startswith(("--get", "-l", "--list")) for a in args[1:])
    return False


def _spawn(argv, t: float | None, kw: dict) -> subprocess.CompletedProcess:
    token = _own.set(True)
    args = git_args(argv)   # a write keeps its own timeout: a tick step's budget never cuts it midway
    hold = stepclock.critical() if args and not is_read(args) and not _pure(args) else contextlib.nullcontext()
    try:
        with hold:
            return subprocess.run(argv, timeout=t, **kw)
    finally:
        _own.reset(token)


def on_audit(event: str, args: tuple) -> None:
    """From the db audit hook: a raw git spawn inside a strict round counts against its slice (past
    it: GitMiss, or TimeoutExpired after the point of no return); a non-git spawn or a socket
    connect is the point of no return. Once the daemon is stopping (Attempt.stop), an abortable round
    ends before its next spawn or connect."""
    a = _att.get()
    rd = a.strict if a else None
    if rd is None or _own.get():
        return
    if event not in ("subprocess.Popen", "socket.connect"):
        return
    if rd.abort() and a.stopping():   # before the spawn or connect: nothing outside the round changed yet
        raise GitMiss(args[1] if event == "subprocess.Popen" and len(args) > 1 else [event], {}, None, None,
                      "the daemon is stopping", learn=False)
    if event == "socket.connect":
        rd.pnr = True
        return
    rd.raw_close()
    argv = args[1] if len(args) > 1 else None
    exe = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
    if Path(str(exe)).name != "git":
        rd.pnr = True
        return
    if isinstance(argv, (list, tuple)) and (args := git_args(argv)) and args[0] in _CHECKS:
        return   # check-ref-format and the like read no repository: never refused, nor counted in the slice
    if rd.left() <= 0:
        if rd.abort():
            raise GitMiss(argv, {}, None, None, "raw git past the round's slice", learn=False)
        raise Spent(argv, rd.slice_s or FLOOR_S, _slice_name(rd))   # the caller defers it
    rd.raw_t0 = time.monotonic()


# pass(): the reads one dispatch pass answers once per repository, by their arguments from the verb
# on. Nothing a pass does between two of them changes their answer: a worktree add, a fork-point ref
# or a lock leaves the repository's own checkout as it was. A call that may move a checkout (MOVES)
# drops that repository's memo; `fetch` drops it too and then counts as done for the pass.
_PASS_READS = frozenset({
    ("rev-parse", "--verify", "--quiet", "HEAD"),
    ("symbolic-ref", "--quiet", "--short", "HEAD"),
    ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
    ("status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=dirty"),
})
_PASS_FETCH = ("fetch", "--quiet", "origin")
MOVES = frozenset({"checkout", "switch", "reset", "rebase", "merge", "commit", "cherry-pick", "revert", "am",
                   "pull", "stash", "branch", "restore", "rm", "mv", "add", "apply", "symbolic-ref", "update-ref",
                   "fetch", "worktree", "submodule", "read-tree", "update-index"})
_pass: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ttp_gitpass", default=None)
_kept: contextvars.ContextVar[bool] = contextvars.ContextVar("ttp_gitkept", default=False)
kept_hits = [0]   # git reads kept_reads() answered without spawning git (Daemon._log_spawns takes it)


@contextlib.contextmanager
def kept_reads() -> Iterator[None]:
    """Within it (outside an attempt), a git read whose answer gitreplay keeps across attempts (every rev
    token a full id, or refs whose values are read from their files) is answered from that memo while
    the refs are unchanged, and kept for the next time. The daemon's tick loop runs in it: its steps
    asked git for the same heads, merge bases and trees every tick, most of its process starts. Worktree
    and index reads, and every other volatile one, still run git each time."""
    token = _kept.set(True)
    try:
        yield
    finally:
        _kept.reset(token)


def _kept_run(argv, kw: dict, t: float | None) -> subprocess.CompletedProcess:
    from . import gitreplay
    k = gitreplay.key(argv, kw, is_read, _OPTS_WITH_VALUE)
    if k is None:
        return _spawn(argv, t, kw)
    e = gitreplay.get(k, None, 0)
    if e is not None and e.rc is not None:
        kept_hits[0] += 1
        return gitreplay.replay(e, argv, kw, t)
    k.pre = gitreplay.classify(k)
    check = bool(kw.pop("check", False))
    r = _spawn(argv, t, kw)
    gitreplay.put(k, r, None, 0)
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r


@contextlib.contextmanager
def dispatch_pass() -> Iterator[dict]:
    """Within it, each _PASS_READS read and one `fetch --quiet origin` per repository run once; the same
    call again gets the kept answer. A daemon dispatch pass starts several runs, and each start read
    the project root's head, branch and status and fetched origin again: on a loaded machine where one
    git spawn takes seconds that cost the pass its step budget after a start or two. A worker that moves
    the root's checkout meanwhile is seen by the next pass. A nested scope shares the outer one's."""
    m = _pass.get()
    token = _pass.set({} if m is None else m)
    try:
        yield _pass.get()
    finally:
        _pass.reset(token)


def _repo_dir(argv, kw: dict) -> str:
    a = [str(x) for x in argv[1:]]
    for i, x in enumerate(a[:-1]):
        if x == "-C":
            return a[i + 1]
        if not x.startswith("-"):
            break
    return str(kw.get("cwd") or ".")


def _moves(args: tuple) -> bool:
    """Whether git `args` may move a checkout or its refs, so a pass memo of its repository is stale."""
    if not args or args[0] not in MOVES:
        return False
    if args[0] == "worktree":   # add, lock and list leave the repository's own checkout alone
        return len(args) > 1 and args[1] in ("move", "repair")
    if args[0] == "update-ref":   # the harness's own refs (fork points) are no branch or HEAD
        refs = [x for x in args[1:] if not x.startswith("-")]
        return not (refs and refs[0].startswith("refs/ttp/"))
    if args[0] == "symbolic-ref":   # its read form names one ref; a second argument sets it
        return len([x for x in args[1:] if not x.startswith("-")]) > 1
    return True


def _pass_run(memo: dict, argv, kw: dict, timeout: float | None, read: bool | None) -> subprocess.CompletedProcess:
    args = git_args(argv)
    repo = _repo_dir(argv, kw)
    if args == _PASS_FETCH and ("fetched", repo) in memo:   # the first fetch's outcome, failure or timeout too
        r = memo[("fetched", repo)]
        if isinstance(r, BaseException):
            raise r
        if kw.get("check") and r.returncode:
            raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
        return r
    key = (repo, args, bool(kw.get("text") or kw.get("universal_newlines")), "input" in kw)
    if args in _PASS_READS and key in memo:
        r = memo[key]
        if kw.get("check") and r.returncode:
            raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
        return r
    keep = args in _PASS_READS and "input" not in kw
    fetch = args == _PASS_FETCH
    check = bool(kw.pop("check", False)) if keep or fetch else False   # a failing read is kept too, then raised
    outcome: Any = None
    try:
        r = _run(argv, timeout=timeout, read=read, **kw)
        outcome = r
    except subprocess.TimeoutExpired as e:
        outcome = e
        raise
    finally:
        if _moves(args):
            for k in [k for k in memo if k[0] == repo]:
                del memo[k]
        if fetch and outcome is not None:   # failed or timed out too: one more try in this pass would do the same
            memo[("fetched", repo)] = outcome
    if fetch and check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    if keep:
        memo[key] = r
        if check and r.returncode:
            raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r


def run(argv: list | tuple, *, timeout: float | None = None, read: bool | None = None,
        **kw: Any) -> subprocess.CompletedProcess:
    """_run() within a dispatch_pass() memo, else as it is."""
    memo = _pass.get()
    if memo is not None and _att.get() is None:
        return _pass_run(memo, argv, kw, timeout, read)
    return _run(argv, timeout=timeout, read=read, **kw)


def _run(argv: list | tuple, *, timeout: float | None = None, read: bool | None = None,
         **kw: Any) -> subprocess.CompletedProcess:
    """The one way a runtime git helper runs git (subprocess.run's arguments): its own `timeout` is
    capped by timeout() for the verb argv names (`read` as there). Within an attempt a read's answer
    is kept and replayed; within a strict round a read's miss runs live only within the slice (a write
keeps its own cap and makes the round's point of no return)."""
    from . import ghroute, gitreplay
    argv = ghroute.push_argv(argv, kw.get("cwd"))   # an https GitHub push with no login here goes over ssh
    args = git_args(argv)
    a = _att.get()
    if a is None:
        if _kept.get() and is_read(args):
            return _kept_run(argv, kw, _cap_timeout(timeout, args, read))
        return _spawn(argv, _cap_timeout(timeout, args, read), kw)
    rd = a.strict
    if rd:
        rd.raw_close()
    k = gitreplay.key(argv, kw, is_read, _OPTS_WITH_VALUE)
    if k is not None:
        e = gitreplay.get(k, a.id, a.round)
        if e is not None:
            if rd:
                rd.hits += 1
            return gitreplay.replay(e, argv, kw, timeout)
    t = _cap_timeout(timeout, args, read)
    check = bool(kw.pop("check", False))
    write = not is_read(args) and not _pure(args)
    pure = bool(args) and args[0] in _CHECKS   # reads no repository: the slice neither binds nor counts it
    slice_binds = False
    if rd and rd.abort() and a.stopping():   # a stop ends the round before its next live git
        raise GitMiss(argv, {**kw, "check": check}, timeout, read, "the daemon is stopping", learn=False)
    if rd and write:
        rd.pnr = True   # a write keeps its own cap: the slice never refuses, binds or kills it
    elif rd and not pure:
        left = rd.left()
        if left <= 0:
            if rd.abort():
                raise GitMiss(argv, {**kw, "check": check}, timeout, read, "past the round's slice",
                              learn=k is not None)
            # no git under the lock past the slice: the caller defers it to the next tick
            raise Spent(list(argv), rd.slice_s or FLOOR_S, _slice_name(rd))
        slice_binds = t is None or t > left
        t = left if slice_binds else t
    if k is not None:
        k.pre = gitreplay.classify(k)
    t0 = time.monotonic()
    try:
        r = _spawn(argv, t, kw)
    except subprocess.TimeoutExpired:
        if rd and not pure:
            rd.live_s += time.monotonic() - t0
            rd.live_n += 1
            if slice_binds and rd.abort():
                raise GitMiss(argv, {**kw, "check": check}, timeout, read, "ran past the round's slice",
                              learn=k is not None) from None
        if k is not None and not (rd and slice_binds):
            gitreplay.put(k, None, a.id, a.round)   # its own timeout: the next round degrades the same
        raise
    finally:
        if write:
            gitreplay.end(a.id)     # its own volatile and once answers may predate the write
    if rd and not pure:
        rd.live_s += time.monotonic() - t0
        rd.live_n += 1
    if k is not None:
        gitreplay.put(k, r, a.id, a.round)
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r

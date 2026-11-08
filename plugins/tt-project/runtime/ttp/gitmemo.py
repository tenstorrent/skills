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
and a scope with `total_s` also ends every git call it makes, reads and writes, by its deadline: past
it, timeout() starts no new call (it raises subprocess.TimeoutExpired at once). The daemon's tick steps run in bounded(TICK_GIT_S) and a finish transaction in
bounded(TX_GIT_S, TX_GIT_S), so one slow or hung git call holds neither past that, whatever its call
site (call sites pass their own timeout through timeout()). A git write outside a transaction keeps
its own timeout: killing it midway can leave a lock or a half-removed worktree. A capped call that
times out raises subprocess.TimeoutExpired as before, and its caller degrades as it does for a git
failure. Outside a bounded scope timeout() returns the call's own timeout.

run() is the one way runtime code runs git (a guard test holds every helper to it). Within an
attempt() (a finish: its warm-up, then its transaction's rounds) it keeps each read's answer in the
gitreplay memo and answers the same read again from it. Within strict() (one round of the finish
transaction, Daemon._finish_rounds) a read the memo lacks runs live only while the round's live git
stays within its slice; past it the read raises GitMiss before spawning, the transaction rolls back,
the read runs outside the lock and the round runs again. The db audit hook calls on_audit(): a raw
git spawn inside a round counts against the slice; a non-git spawn or a socket connect is the round's
point of no return, after which it never aborts: later misses run live within the slice, else raise
subprocess.TimeoutExpired without spawning. A stop (Attempt.stop: the daemon got SIGTERM) ends an
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
# (per-call seconds for a read, monotonic deadline for every call or None) of the bounded() scope
_cap: contextvars.ContextVar[tuple[float, float | None] | None] = contextvars.ContextVar("ttp_gitcap", default=None)

TICK_GIT_S = 25.0   # one git read a daemon tick step makes, at most (Daemon._run_step)
TX_GIT_S = 20.0     # all git a finish write transaction makes, together (Daemon._finish_tx)
HOLD_SLICE_S = 1.5  # live git one strict round of a finish transaction may run (strict())
MAX_ROUNDS = 50     # rounds of one finish before its last, which never aborts (Daemon._finish_rounds)
FINISH_WALL_S = 90.0    # nor past this long: the reads it learns between rounds share this wall too
PREFETCH_GIT_S = 60.0   # all git one finish's warm-up (Daemon._prefetch_git) runs, together
# git's own options that take the next argument (git -C <dir> <verb> ...)
_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"})
# Verbs that only read the repository (a scratch GIT_INDEX_FILE aside): bounded() caps them anywhere.
READS = frozenset({"rev-parse", "rev-list", "merge-base", "diff", "log", "show", "ls-files", "check-ignore",
                   "cherry", "patch-id", "symbolic-ref", "cat-file", "for-each-ref", "show-ref", "status",
                   "grep", "name-rev", "describe", "ls-tree", "diff-tree", "merge-tree"})
# `git reflog` reads (show, its default, and the rest) but for these subcommands
_REFLOG_WRITES = frozenset({"expire", "delete"})


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
    if outer is not None and outer[1] is not None:
        end = outer[1] if end is None else min(end, outer[1])
    token = _cap.set((per, end))
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
        if left <= 0:   # past the deadline: no new spawn
            raise subprocess.TimeoutExpired(["git", *map(str, args)], 0)
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
    if rd.left() <= 0:
        if rd.abort():
            raise GitMiss(argv, {}, None, None, "raw git past the round's slice", learn=False)
        raise subprocess.TimeoutExpired(argv, 0)
    rd.raw_t0 = time.monotonic()


def run(argv: list | tuple, *, timeout: float | None = None, read: bool | None = None,
        **kw: Any) -> subprocess.CompletedProcess:
    """The one way a runtime git helper runs git (subprocess.run's arguments): its own `timeout` is
    capped by timeout() for the verb argv names (`read` as there). Within an attempt a read's answer
    is kept and replayed; within a strict round a read's miss runs live only within the slice (a write
keeps its own cap and makes the round's point of no return)."""
    from . import gitreplay
    args = git_args(argv)
    a = _att.get()
    if a is None:
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
    slice_binds = False
    if rd and rd.abort() and a.stopping():   # a stop ends the round before its next live git
        raise GitMiss(argv, {**kw, "check": check}, timeout, read, "the daemon is stopping", learn=False)
    if rd and write:
        rd.pnr = True   # a write keeps its own cap: the slice never refuses, binds or kills it
    elif rd:
        left = rd.left()
        if left <= 0:
            if rd.abort():
                raise GitMiss(argv, {**kw, "check": check}, timeout, read, "past the round's slice",
                              learn=k is not None)
            raise subprocess.TimeoutExpired(list(argv), 0)
        slice_binds = t is None or t > left
        t = left if slice_binds else t
    if k is not None:
        k.pre = gitreplay.classify(k)
    t0 = time.monotonic()
    try:
        r = _spawn(argv, t, kw)
    except subprocess.TimeoutExpired:
        if rd:
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
    if rd:
        rd.live_s += time.monotonic() - t0
        rd.live_n += 1
    if k is not None:
        gitreplay.put(k, r, a.id, a.round)
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r

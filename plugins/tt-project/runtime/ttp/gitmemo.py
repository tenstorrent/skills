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

bounded() caps, by time, the git a scope runs: each git read (a READS verb) gets at most `per_call_s`,
and a scope with `total_s` also ends every git call it makes, reads and writes, by its deadline (but
FLOOR_S). The daemon's tick steps run in bounded(TICK_GIT_S) and a finish transaction in
bounded(TX_GIT_S, TX_GIT_S), so one slow or hung git call holds neither past that, whatever its call
site (call sites pass their own timeout through timeout()). A git write outside a transaction keeps
its own timeout: killing it midway can leave a lock or a half-removed worktree. A capped call that
times out raises subprocess.TimeoutExpired as before, and its caller degrades as it does for a git
failure. Outside a bounded scope timeout() returns the call's own timeout."""

from __future__ import annotations

import contextlib
import contextvars
import time
from typing import Any, Callable, Iterator

_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ttp_gitmemo", default=None)
# (per-call seconds for a read, monotonic deadline for every call or None) of the bounded() scope
_cap: contextvars.ContextVar[tuple[float, float | None] | None] = contextvars.ContextVar("ttp_gitcap", default=None)

TICK_GIT_S = 45.0   # one git read a daemon tick step makes, at most (Daemon._run_step)
TX_GIT_S = 20.0     # all git a finish write transaction makes, together (Daemon._finish_tx)
FLOOR_S = 1.0       # a call made past its scope's deadline still gets this long
# Verbs that only read the repository (a scratch GIT_INDEX_FILE aside): bounded() caps them anywhere.
READS = frozenset({"rev-parse", "rev-list", "merge-base", "diff", "log", "show", "ls-files", "check-ignore",
                   "cherry", "patch-id", "symbolic-ref", "cat-file", "for-each-ref", "show-ref", "status",
                   "grep", "name-rev", "describe", "ls-tree", "diff-tree", "merge-tree"})


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
    default a call that names no `args`, or whose verb args[0] is in READS) and, in a scope with a
    deadline, for any call."""
    cap = _cap.get()
    if cap is None:
        return own
    if read is None:
        read = not args or args[0] in READS
    t = own
    if read:
        t = cap[0] if t is None else min(t, cap[0])
    if cap[1] is not None:
        left = max(FLOOR_S, cap[1] - time.monotonic())
        t = left if t is None else min(t, left)
    return t

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Git facts read once, before a write transaction, and reused inside it.

Finishing a run (Daemon.finish_run) judges its hand-off inside one db write transaction: a failed
review's area (reviewcap.area: main_file, touched) and each follow-up's file overlaps and chain
(overlap.find, overlap.chain: branches, _on_push_branch, the base and its files, git grep per symbol)
read git there, and a slow repository held the write lock for over a minute. Within scope(), cached() keeps each such fact by its key: the daemon warms them before the
transaction opens, and the same calls inside it read the memo. A key not warmed (a call the warm-up
did not foresee) runs its git as before; a call that raised is not kept. Outside a scope cached()
only runs the call."""

from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Callable, Iterator

_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ttp_gitmemo", default=None)


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

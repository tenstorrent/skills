# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A host-wide pool of test-worker slots, shared by every tt-project project of this account on this host.

checkcap caps how many check runs execute at once; this caps how many test workers they start between
them, since one suite with pytest-xdist's `-n auto` starts one worker per core. Every check command
tt-project runs (`ttp checks`, its pre-stage and the reruns of the test ids a diff adds, review
checks, `ttp push` and the push queue's batch checks) first takes k slots: as many as are free, at
most run_max(), at least one. With none free it waits, saying so, and never fails for it. The slots
are OS file locks (locks.try_take) under checkcap.slot_dir(), so a holder that crashes, is killed or
whose host reboots frees them; no daemon keeps them.

The grant reaches the command as PYTEST_XDIST_AUTO_NUM_WORKERS (what xdist's `-n auto` and
`-n logical` use) and TTP_TEST_SLOTS (for any other runner), and an explicit `-n N` or
`--numprocesses N` above it in a pytest command is lowered to it (clamp()). A `ttp checks` nested in
a check keeps its parent's grant (HELD_ENV) instead of taking more. Checks also run at a lower CPU
and IO priority (low_priority()), so a host's live services win.

Defaults (sizes()): the pool is half the host's usable cores, at least 2, so tests leave the other
half to live work and builds; one run takes at most min(RUN_MAX, half the pool), at least 1, so two
runs always fit side by side and no suite takes the pool. The account settings checks.test_pool and
checks.test_run_max (or TTP_TEST_POOL / TTP_TEST_RUN_MAX) set them; checks.test_pool 0 turns the pool
off. A project's own checks.test_pool / checks.test_run_max may only lower them.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from typing import Callable

from . import checkcap, locks, poll_s, project

RESOURCE = "test-slots"    # check-slots/test-slots.<i>.lock
RUN_MAX = 8                # one run's most workers by default: past about 8 a suite's wall time gains little
SLOTS_ENV = "TTP_TEST_SLOTS"
XDIST_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
HELD_ENV = "TTP_TEST_SLOTS_HELD"   # set for the commands of a run that holds slots: the grant
POLL_S = 5.0
TELL_S = 300.0
NICE = 10
IONICE = ["-c2", "-n7"]    # best-effort, lowest: a live service's IO goes first, yet checks never starve


def _number(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _setting(env: str, key: str) -> int | None:
    for v in (os.environ.get(env), (project.load_account_settings().get("checks") or {}).get(key)):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return None


def sizes(cfg: dict | None = None, cores: int | None = None) -> tuple[int, int] | None:
    """(pool, run_max) on this host, or None when the pool is off (checks.test_pool 0). `cfg` is a
    project's config: its checks.test_pool and checks.test_run_max only lower the host's."""
    c = checkcap.cores() if cores is None else cores
    pool = _setting("TTP_TEST_POOL", "test_pool")
    if pool == 0:
        return None
    pool = max(2, c // 2) if pool is None else pool
    top = _setting("TTP_TEST_RUN_MAX", "test_run_max")
    top = min(RUN_MAX, max(1, pool // 2)) if not top else top
    own = (cfg or {}).get("checks") or {}
    if (n := _number(own.get("test_pool"))):
        pool = min(pool, n)
    if (n := _number(own.get("test_run_max"))):
        top = min(top, n)
    return pool, max(1, min(top, pool))


def _cfg() -> dict | None:
    base = os.environ.get("TTP_PROJECT")
    if not base:
        return None
    try:
        return project.Project(base).config()
    except Exception:   # an unreadable project config only loses its lower limits
        return None


def worker_env(cfg: dict | None) -> dict[str, str]:
    """A worker's environment: tests it runs directly in its shell start at most one run's share."""
    s = sizes(cfg)
    return {XDIST_ENV: str(s[1]), SLOTS_ENV: str(s[1])} if s else {}


def paths(pool: int):
    return locks.slot_paths(checkcap.slot_dir(), RESOURCE, pool)


class Grant:
    """k held slots (none with the pool off: `k` 0); `close()` frees them."""

    def __init__(self, k: int = 0, files=(), waited: float = 0.0):
        self.k, self.files, self.waited = k, list(files), waited

    def env(self) -> dict[str, str]:
        return {XDIST_ENV: str(self.k), SLOTS_ENV: str(self.k), HELD_ENV: str(self.k)} if self.k else {}

    def close(self) -> None:
        for f in self.files:
            f.close()
        self.files = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def acquire(holder: str, tell: Callable[[str], None] | None = None, wait_s: float | None = None,
            cfg: dict | None = None) -> Grant | None:
    """Take this run's test slots (above); waits while none is free, telling why every TELL_S. None
    only once `wait_s` passed with none free (a foreground caller hands over). Nested in a run that
    holds slots, its grant at once; with the pool off, Grant() (no limit, nothing set)."""
    tell = tell or (lambda _line: None)
    if (held := _number(os.environ.get(HELD_ENV))):
        return Grant(held)
    cfg = _cfg() if cfg is None else cfg
    start, told = time.time(), 0.0
    while True:
        s = sizes(cfg)
        if s is None:
            return Grant()
        pool, top = s
        ps, files = paths(pool), []
        try:
            while len(files) < top and (f := locks.try_take(ps, holder, "test slots")) is not None:
                files.append(f)
        except OSError as e:
            for f in files:
                f.close()
            tell(f"test slots unavailable ({e.strerror or e}): running with 1 test worker")
            return Grant(1, waited=time.time() - start)
        waited = time.time() - start
        if files:
            tell(f"test slots: {len(files)} granted of a host pool of {pool} (at most {top} per run) "
                 f"after {waited:.0f} s waiting; tests run with {XDIST_ENV}={len(files)}")
            return Grant(len(files), files, waited)
        if wait_s is not None and waited >= wait_s:
            return None
        if not told or time.time() - told >= TELL_S:
            told = time.time()
            tell(f"waiting for a host-wide test slot: all {pool} are held "
                 f"({checkcap._some(locks.held_labels(ps))})")
        time.sleep(poll_s(POLL_S))


def use() -> str:
    """One status line on the host pool's use, or "" when it is off or idle."""
    s = sizes()
    if s is None:
        return ""
    try:
        held = len(locks.held_labels(paths(s[0])))
    except OSError:
        return ""
    return f"host test slots: {held} of {s[0]} in use (at most {s[1]} per run)" if held else ""


_SEP = re.compile(r"(&&|\|\||[;|\n])")
_PYTEST = re.compile(r"(?<![\w./-])(?:pytest|py\.test)(?![\w-])")
_NPROC = re.compile(r"(?<![\w-])(-n|--numprocesses)(\s*=?\s*)(\d+)(?![\w.])")


def clamp(cmd: str, k: int) -> str:
    """`cmd` with each explicit pytest `-n N` / `--numprocesses N` above `k` lowered to `k`; only after
    a pytest word in the same command, so `head -n 50` or `nice -n 19 pytest` keep theirs."""
    if k <= 0:
        return cmd
    out = []
    for part in _SEP.split(cmd):
        m = _PYTEST.search(part)
        if m:
            tail = _NPROC.sub(lambda x: x.group(0) if int(x.group(3)) <= k else f"{x.group(1)}{x.group(2)}{k}",
                              part[m.end():])
            part = part[:m.end()] + tail
        out.append(part)
    return "".join(out)


def clamp_for(cmd: str, env: dict | None) -> str:
    """clamp() to the grant `env` (default this process's) carries, else `cmd` as it is."""
    k = _number((os.environ if env is None else env).get(HELD_ENV))
    return clamp(cmd, k) if k else cmd


def low_priority(argv: list) -> list:
    """A check command's argv under `nice -n NICE` and `ionice` best-effort lowest where they exist, so a
    host's live services go first; either missing is skipped. checks.nice false (account setting)
    runs it as it is."""
    if (project.load_account_settings().get("checks") or {}).get("nice") is False:
        return list(argv)
    return ((["nice", "-n", str(NICE)] if shutil.which("nice") else [])
            + (["ionice", *IONICE] if shutil.which("ionice") else []) + list(argv))

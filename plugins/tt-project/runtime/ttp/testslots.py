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

One project holds at most project_max() slots at once over all its runs (default twice a run's share,
at most the pool; checks.test_project_max or TTP_TEST_PROJECT_MAX), as per-project lock files beside
the pool's, so a burst of one project's checks leaves room for the others. Waiters are served
first-come (a locks.enqueue ticket each, freed with its process): a waiter whose project is at its
budget steps out of the line, keeping its arrival, so it never holds up other projects. Checks gating
a push to the push branch (`push_key`) never wait behind task checks: with no pool slot free they take
that branch's one reserved slot beyond the pool (RESERVED), which task checks never use.

A project's full checks (`ttp checks`) also hold its suite lock (suite_hold()): a second full run of
the same project waits for the first, other projects never do. It is a file lock, so an exit, a crash
or a reboot frees it.
"""
from __future__ import annotations

import os
import hashlib
import re
import shlex
import shutil
import time
from typing import Callable

from . import checkcap, locks, poll_s, project

RESOURCE = "test-slots"    # check-slots/test-slots.<i>.lock
RUN_MAX = 8                # one run's most workers by default: past about 8 a suite's wall time gains little
SLOTS_ENV = "TTP_TEST_SLOTS"
XDIST_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
HELD_ENV = "TTP_TEST_SLOTS_HELD"   # set for the commands of a run that holds slots: the grant
OWN = "test-slots-project"     # check-slots/test-slots-project.<project id>.<i>.lock: one project's budget
RESERVED = "test-slots-push"   # check-slots/test-slots-push.<branch id>.lock: a push branch's reserved slot
SUITE = "test-suite"           # check-slots/test-suite.<project id>.lock: one project's full checks
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


def project_max(cfg: dict | None = None, s: tuple[int, int] | None = None) -> int:
    """The most slots one project holds at once over all its runs (above); `s` is sizes(cfg)."""
    s = s or sizes(cfg) or (0, 0)
    top = _setting("TTP_TEST_PROJECT_MAX", "test_project_max") or min(s[0], 2 * s[1])
    if (n := _number(((cfg or {}).get("checks") or {}).get("test_project_max"))):
        top = min(top, n)
    return max(s[1], min(top, s[0])) if s[0] else 0


def own_paths(project_name: str | None, n: int) -> list:
    """One project's budget slots; none for no project (its runs only count against the pool)."""
    return locks.slot_paths(checkcap.slot_dir(), f"{OWN}.{locks.group_id(project_name)}", n) if project_name else []


def reserved_path(push_key: str):
    return checkcap.slot_dir() / f"{RESERVED}.{hashlib.sha256(push_key.encode()).hexdigest()[:16]}.lock"


def _project_name() -> str | None:
    base = os.environ.get("TTP_PROJECT")
    try:
        return project.Project(base).name if base else None
    except Exception:   # an unreadable project: its runs count against the pool only
        return None


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


def _take(ps: list, own: list, top: int, holder: str) -> tuple[int, list]:
    """(k, files): up to `top` pool slots, each paired with one of the project's (`own`, when it has a budget)."""
    k, files = 0, []
    while k < top:
        o = None
        if own and (o := locks.try_take(own, holder, "test slots")) is None:
            break
        if (f := locks.try_take(ps, holder, "test slots")) is None:
            if o is not None:
                o.close()
            break
        k, files = k + 1, files + [f] + ([o] if o is not None else [])
    return k, files


def acquire(holder: str, tell: Callable[[str], None] | None = None, wait_s: float | None = None,
            cfg: dict | None = None, project_name: str | None = None, push_key: str = "") -> Grant | None:
    """Take this run's test slots (above); waits while none is free, first-come, telling why every
    TELL_S. None only once `wait_s` passed with none free (a foreground caller hands over). Nested in a
    run that holds slots, its grant at once; with the pool off, Grant() (no limit, nothing set).
    `project_name` (default TTP_PROJECT's) is whose budget it counts against; `push_key` (push.priority_key)
    marks push checks, which take the branch's reserved slot when the pool has none free."""
    tell = tell or (lambda _line: None)
    if (held := _number(os.environ.get(HELD_ENV))):
        return Grant(held)
    cfg = _cfg() if cfg is None else cfg
    name = _project_name() if project_name is None else project_name
    start, told, ticket = time.time(), 0.0, None
    try:
        while True:
            s = sizes(cfg)
            if s is None:
                return Grant()
            pool, top = s
            ps, own = paths(pool), own_paths(name, project_max(cfg, s))
            k, files, why = 0, [], f"all {pool} are held"
            try:
                if own and not push_key and not locks.any_free(own):
                    # At its project's budget: out of the line (keeping its arrival) so others go first.
                    locks.dequeue(ticket)
                    ticket, why = None, f"its project holds its {len(own)} (checks.test_project_max)"
                elif push_key:
                    k, files = _take(ps, own, top, holder)
                    if not k and (f := locks.try_take([reserved_path(push_key)], holder, "push test slot")):
                        k, files = 1, [f]
                else:
                    if ticket is None:
                        ticket = locks.enqueue(checkcap.slot_dir(), RESOURCE, holder, start, group=name)
                    q = locks.queued(checkcap.slot_dir(), RESOURCE)
                    if q and q[0] == ticket:
                        k, files = _take(ps, own, top, holder)
                    elif q:
                        why = f"{q.index(ticket) if ticket in q else len(q)} ahead in the host's line"
            except OSError as e:
                for f in files:
                    f.close()
                tell(f"test slots unavailable ({e.strerror or e}): running with 1 test worker")
                return Grant(1, waited=time.time() - start)
            waited = time.time() - start
            if k:
                tell(f"test slots: {k} granted of a host pool of {pool} (at most {top} per run"
                     + (f", {len(own)} per project" if own else "") + f") after {waited:.0f} s waiting; "
                     f"tests run with {XDIST_ENV}={k}")
                return Grant(k, files, waited)
            if wait_s is not None and waited >= wait_s:
                return None
            if not told or time.time() - told >= TELL_S:
                told = time.time()
                tell(f"waiting for a host-wide test slot: {why} ({checkcap._some(locks.held_labels(ps))})")
            time.sleep(poll_s(POLL_S))
    finally:
        locks.dequeue(ticket)


def suite_hold(project_name: str | None, tell: Callable[[str], None] | None = None,
               wait_s: float | None = None, waiting: Callable[[bool], None] | None = None,
               stop: Callable[[], str] | None = None) -> Grant | None:
    """Hold project `project_name`'s suite lock for its full checks (above), waiting while another of its
    runs holds it; Grant(0) holding it, Grant() with no project or nested in a check, None once `wait_s`
    passed. It never waits for another project's runs. `waiting` and `stop` as for checkcap.acquire:
    `waiting(True)` once the wait starts and `waiting(False)` once it ends; a reason from `stop()` ends
    the wait with checkcap.Cancelled."""
    if not project_name or os.environ.get(HELD_ENV) or os.environ.get(checkcap.HELD_ENV):
        return Grant()
    path = locks.slot_paths(checkcap.slot_dir(), f"{SUITE}.{locks.group_id(project_name)}", 1)
    start, told = time.time(), 0.0
    try:
        while True:
            try:
                f = locks.try_take(path, f"{project_name}: full checks (pid {os.getpid()})", "full checks")
            except OSError:
                return Grant()   # an unwritable slot directory: the checks slot says so
            if f is not None:
                return Grant(0, [f], time.time() - start)
            if stop and (why := stop()):
                raise checkcap.Cancelled(why)
            if wait_s is not None and time.time() - start >= wait_s:
                return None
            if not told or time.time() - told >= TELL_S:
                if not told and waiting:
                    waiting(True)
                told = time.time()
                tell and tell(f"waiting for this project's other full checks to end (one at a time per project): "
                              f"{checkcap._some(locks.held_labels(path))}")
            time.sleep(poll_s(POLL_S))
    finally:
        if told and waiting:
            waiting(False)


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


def over(cmd: str, k: int) -> list[int]:
    """The explicit pytest `-n N` / `--numprocesses N` values in `cmd` above `k` (as clamp() finds them)."""
    if k <= 0:
        return []
    return [int(x.group(3)) for part in _SEP.split(cmd) if (m := _PYTEST.search(part))
            for x in _NPROC.finditer(part[m.end():]) if int(x.group(3)) > k]


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


def clamped(cmd: str, k: int) -> str:
    """clamp(), with a first line that says on stderr which `-n` it lowered (nothing when none)."""
    if not (big := over(cmd, k)):
        return cmd
    note = (f"ttp: pytest -n {', '.join(map(str, sorted(set(big))))} lowered to -n {k}, this run's "
            f"test-worker budget on the host ({SLOTS_ENV}={k})")
    return f"echo {shlex.quote(note)} >&2\n{clamp(cmd, k)}"


def clamp_for(cmd: str, env: dict | None) -> str:
    """clamp() to the grant `env` (default this process's) carries, else `cmd` as it is."""
    k = _number((os.environ if env is None else env).get(HELD_ENV))
    return clamped(cmd, k) if k else cmd


def low_priority(argv: list) -> list:
    """A check command's argv under `nice -n NICE` and `ionice` best-effort lowest where they exist, so a
    host's live services go first; either missing is skipped. checks.nice false (account setting)
    runs it as it is."""
    if (project.load_account_settings().get("checks") or {}).get("nice") is False:
        return list(argv)
    return ((["nice", "-n", str(NICE)] if shutil.which("nice") else [])
            + (["ionice", *IONICE] if shutil.which("ionice") else []) + list(argv))

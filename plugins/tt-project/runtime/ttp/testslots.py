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
or a reboot frees it. Its waiters queue (a locks.enqueue ticket each) and it goes to the most urgent
task priority first (p1 before p2 ...; an obligation owner's 0 before all), then arrival (suite_order);
a running holder is never pre-empted. A waiter without a task priority (no task, a task without one,
an older runtime) ranks as NO_PRIORITY (p3). A waiter ages one priority step per `checks.priority_age_s`
waited (default SUITE_AGE_S; checklane.priority_age_s), never past p1 (AGE_FLOOR; an obligation owner's
0 stays 0), so a waiter of priority p waits at most (p - 1) aging steps plus the runs of those that
arrived before it. Wait lines show each waiter's priority and its rank now (rank_text).
A run holding it is stopped at the checks wall-time cap (checkwall); suite_line shows the holder's
time against that cap and the waiting order (`ttp status`).

Builds (make, ninja, cmake --build) take slots of a second pool, the build pool (BUILDS: same sizes,
same locks and order, its own lock files and settings checks.build_pool, checks.build_run_max and
checks.build_project_max, or TTP_BUILD_POOL / TTP_BUILD_RUN_MAX / TTP_BUILD_PROJECT_MAX). It is the
half of the cores the test pool leaves, so a long build never takes a test slot and a full suite never
holds up a build, and both together stay within the host's cores. A worker's environment carries one
run's build share as MAKEFLAGS=-j<k> and CMAKE_BUILD_PARALLEL_LEVEL=<k> (ninja reads no such variable);
`ttp clip` of a build takes k build slots, sets them for the command, lowers an explicit -j above k (and
gives a bare ninja -j k) through clamp_build(). A build inside a check uses the check's test grant.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
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
SLOTS_KEY = "slots"            # checks.slots: how many full checks of one project and repository run at once
PORTS = "check-ports"          # check-slots/check-ports.<i>.lock: one running check run's port block
# A check run's own ports (port_block): PORT_BLOCKS blocks of PORT_SPAN from PORT_FIRST, 20000-31999,
# below Linux's ephemeral range (32768-60999), so two suites that run at once never pick the same port.
PORT_FIRST, PORT_SPAN, PORT_BLOCKS = 20000, 100, 120
PORT_BASE_ENV, PORT_COUNT_ENV = "TTP_PORT_BASE", "TTP_PORT_COUNT"
POLL_S = 5.0
SUITE_AGE_S = 900.0   # default checks.priority_age_s: a suite waiter gains one priority step per this many seconds waited
NO_PRIORITY = 3      # a ticket without a priority (no task, a task without one, an older runtime) ranks as p3
AGE_FLOOR = 1        # aging never ranks a waiter above p1 (an obligation owner's 0 is its own, not aged to)
TELL_S = 300.0
NICE = 10
IONICE = ["-c3"]    # idle class: a live service's IO always goes first


def _number(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


class Kind:
    """One slot pool: its lock-file names, settings and the words its lines use."""

    def __init__(self, name: str, resource: str, own: str, held_env: str, unit: str):
        self.name, self.resource, self.own, self.held_env, self.unit = name, resource, own, held_env, unit
        self.env_prefix = f"TTP_{name.upper()}_"

    def setting(self, what: str) -> tuple[str, str]:
        """(environment variable, settings key) of `what` (pool, run_max, project_max)."""
        return self.env_prefix + what.upper(), f"{self.name}_{what}"


BUILD_HELD_ENV = "TTP_BUILD_SLOTS_HELD"   # set for a build that holds build slots: the grant
TESTS = Kind("test", RESOURCE, OWN, HELD_ENV, "test worker")
BUILDS = Kind("build", "build-slots", "build-slots-project", BUILD_HELD_ENV, "build job")
CLIP_WAIT_S = 60.0   # `ttp clip` of a build waits this long for a build slot, then runs with one job


def _setting(env: str, key: str) -> int | None:
    for v in (os.environ.get(env), (project.load_account_settings().get("checks") or {}).get(key)):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return None


def sizes(cfg: dict | None = None, cores: int | None = None, kind: Kind = TESTS) -> tuple[int, int] | None:
    """(pool, run_max) on this host, or None when the pool is off (checks.test_pool 0). `cfg` is a
    project's config: its checks.test_pool and checks.test_run_max only lower the host's. `kind`
    BUILDS: the build pool's, from its own build_* settings."""
    c = checkcap.cores() if cores is None else cores
    pool = _setting(*kind.setting("pool"))
    if pool == 0:
        return None
    pool = max(2, c // 2) if pool is None else pool
    top = _setting(*kind.setting("run_max"))
    top = min(RUN_MAX, max(1, pool // 2)) if not top else top
    own = _section(cfg, "checks")
    if (n := _number(own.get(kind.setting("pool")[1]))):
        pool = min(pool, n)
    if (n := _number(own.get(kind.setting("run_max")[1]))):
        top = min(top, n)
    return pool, max(1, min(top, pool))


def project_max(cfg: dict | None = None, s: tuple[int, int] | None = None, kind: Kind = TESTS) -> int:
    """The most slots one project holds at once over all its runs (above); `s` is sizes(cfg)."""
    s = s or sizes(cfg, kind=kind) or (0, 0)
    top = _setting(*kind.setting("project_max")) or min(s[0], 2 * s[1])
    if (n := _number(_section(cfg, "checks").get(kind.setting("project_max")[1]))):
        top = min(top, n)
    return max(s[1], min(top, s[0])) if s[0] else 0


def own_paths(project_name: str | None, n: int, kind: Kind = TESTS) -> list:
    """One project's budget slots; none for no project (its runs only count against the pool)."""
    return (locks.slot_paths(checkcap.slot_dir(), f"{kind.own}.{locks.group_id(project_name)}", n)
            if project_name else [])


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


def makeflags(k: int, current: str | None = None) -> str:
    """MAKEFLAGS (default this process's) with its job count at most `k`: a lower -jN stays, a higher one,
    a bare -j or none becomes -j<k>; its other flags stay."""
    words = (os.environ.get("MAKEFLAGS", "") if current is None else current).split()
    jobs = [m for w in words if (m := re.fullmatch(r"(?:-j|--jobs=?)(\d*)", w))]
    have = [int(m.group(1)) for m in jobs if m.group(1) and int(m.group(1)) > 0]
    rest = [w for w in words if not re.fullmatch(r"(?:-j|--jobs=?)(\d*)", w)]
    return " ".join([f"-j{min([k, *have])}", *rest])


def build_env(k: int) -> dict[str, str]:
    """The variables that cap a build at `k` jobs: make (MAKEFLAGS) and cmake --build (CMAKE_BUILD_PARALLEL_LEVEL)."""
    return {"MAKEFLAGS": makeflags(k), "CMAKE_BUILD_PARALLEL_LEVEL": str(k)} if k else {}


def worker_env(cfg: dict | None) -> dict[str, str]:
    """A worker's environment: tests and builds it runs directly in its shell start at most one run's share."""
    s, b = sizes(cfg), sizes(cfg, kind=BUILDS)
    return {**({XDIST_ENV: str(s[1]), SLOTS_ENV: str(s[1])} if s else {}), **(build_env(b[1]) if b else {})}


def paths(pool: int, kind: Kind = TESTS):
    return locks.slot_paths(checkcap.slot_dir(), kind.resource, pool)


class Grant:
    """k held slots (none with the pool off: `k` 0); `close()` frees them."""

    def __init__(self, k: int = 0, files=(), waited: float = 0.0, kind: Kind = TESTS):
        self.k, self.files, self.waited, self.kind = k, list(files), waited, kind
        self.extra: dict[str, str] = {}   # more variables for the commands, e.g. the run's ports (port_block)

    def env(self) -> dict[str, str]:
        if not self.k:
            return dict(self.extra)
        if self.kind is BUILDS:
            return {**build_env(self.k), BUILD_HELD_ENV: str(self.k), **self.extra}
        return {XDIST_ENV: str(self.k), SLOTS_ENV: str(self.k), HELD_ENV: str(self.k), **self.extra}

    def close(self) -> None:
        for f in self.files:
            f.close()
        self.files = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _take(ps: list, own: list, top: int, holder: str, what: str = "test slots") -> tuple[int, list]:
    """(k, files): up to `top` pool slots, each paired with one of the project's (`own`, when it has a budget)."""
    k, files = 0, []
    while k < top:
        o = None
        if own and (o := locks.try_take(own, holder, what)) is None:
            break
        if (f := locks.try_take(ps, holder, what)) is None:
            if o is not None:
                o.close()
            break
        k, files = k + 1, files + [f] + ([o] if o is not None else [])
    return k, files


def acquire(holder: str, tell: Callable[[str], None] | None = None, wait_s: float | None = None,
            cfg: dict | None = None, project_name: str | None = None, push_key: str = "",
            kind: Kind = TESTS) -> Grant | None:
    """Take this run's test slots (above); waits while none is free, first-come, telling why every
    TELL_S. None only once `wait_s` passed with none free (a foreground caller hands over). Nested in a
    run that holds slots, its grant at once; with the pool off, Grant() (no limit, nothing set).
    `project_name` (default TTP_PROJECT's) is whose budget it counts against; `push_key` (push.priority_key)
    marks push checks, which take the branch's reserved slot when the pool has none free. `kind` BUILDS
    takes build slots instead; a build nested in a check uses the check's test grant."""
    tell = tell or (lambda _line: None)
    what = f"{kind.name} slots"
    for env in (kind.held_env, HELD_ENV):   # HELD_ENV: a build in a check builds on the check's slots
        if (held := _number(os.environ.get(env))):
            return Grant(held, kind=kind)
    cfg = _cfg() if cfg is None else cfg
    name = _project_name() if project_name is None else project_name
    start, told, ticket = time.time(), 0.0, None
    try:
        while True:
            s = sizes(cfg, kind=kind)
            if s is None:
                return Grant(kind=kind)
            pool, top = s
            ps, own = paths(pool, kind), own_paths(name, project_max(cfg, s, kind), kind)
            k, files, why = 0, [], f"all {pool} are held"
            try:
                if own and not push_key and not locks.any_free(own):
                    # At its project's budget: out of the line (keeping its arrival) so others go first.
                    locks.dequeue(ticket)
                    ticket, why = None, f"its project holds its {len(own)} (checks.{kind.setting('project_max')[1]})"
                elif push_key:
                    k, files = _take(ps, own, top, holder, what)
                    if not k and (f := locks.try_take([reserved_path(push_key)], holder, "push test slot")):
                        k, files = 1, [f]
                else:
                    if ticket is None:
                        ticket = locks.enqueue(checkcap.slot_dir(), kind.resource, holder, start, group=name)
                    q = locks.queued(checkcap.slot_dir(), kind.resource)
                    if q and q[0] == ticket:
                        k, files = _take(ps, own, top, holder, what)
                    elif q:
                        why = f"{q.index(ticket) if ticket in q else len(q)} ahead in the host's line"
            except OSError as e:
                for f in files:
                    f.close()
                tell(f"{what} unavailable ({e.strerror or e}): running with 1 {kind.unit}")
                return Grant(1, waited=time.time() - start, kind=kind)
            waited = time.time() - start
            if k:
                tell(f"{what}: {k} granted of a host pool of {pool} (at most {top} per run"
                     + (f", {len(own)} per project" if own else "") + f") after {waited:.0f} s waiting; "
                     + (f"builds run with MAKEFLAGS=-j{k}" if kind is BUILDS else f"tests run with {XDIST_ENV}={k}"))
                return Grant(k, files, waited, kind)
            if wait_s is not None and waited >= wait_s:
                return None
            if not told or time.time() - told >= TELL_S:
                told = time.time()
                tell(f"waiting for a host-wide {kind.name} slot: {why} ({checkcap._some(locks.held_labels(ps))})")
            time.sleep(poll_s(POLL_S))
    finally:
        locks.dequeue(ticket)


def port_block(holder: str):
    """(held lock file, environment) of one free block of the host's check ports, never waiting: the
    block's first port and size as TTP_PORT_BASE and TTP_PORT_COUNT, for a suite that binds fixed ports,
    so check runs that run at once never share one. The lock frees with its run (a crash, a reboot). None
    when all PORT_BLOCKS are held or the slot directory cannot be written: the run then has no own block."""
    paths = locks.slot_paths(checkcap.slot_dir(), PORTS, PORT_BLOCKS)
    try:
        f = locks.try_take(paths, holder, "check ports")
    except OSError:
        return None
    if f is None:
        return None
    i = [str(x) for x in paths].index(str(f.name))
    return f, {PORT_BASE_ENV: str(PORT_FIRST + i * PORT_SPAN), PORT_COUNT_ENV: str(PORT_SPAN)}


def tree_key(top) -> str:
    """The short id of worktree `top` itself (its real path), for checks that run in it (cwd "authoring"):
    they share its files, so two such runs of one worktree never run at once (suite_hold with it as
    `repo`). "" without `top`. Its "t" prefix keeps the lane out of suite_bases, so it never counts
    against the project cap (project_held, cap_turn) beside the run's own repository lane."""
    return "t" + hashlib.sha256(os.path.realpath(str(top)).encode()).hexdigest()[:12] if top else ""


def repo_key(top) -> str:
    """The short id of the git repository at `top` (its common git dir, so all its worktrees share it);
    "" when `top` is not in one."""
    if not top:
        return ""
    try:
        r = subprocess.run(["git", "-C", str(top), "rev-parse", "--git-common-dir"], capture_output=True,
                           text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode or not r.stdout.strip():
        return ""
    common = Path(r.stdout.strip())
    common = common if common.is_absolute() else Path(top) / common
    return hashlib.sha256(os.path.realpath(common).encode()).hexdigest()[:12]


def suite_base(project_name: str, repo: str = "") -> str:
    """The lane resource of project `project_name`'s full checks in repository `repo` (repo_key; "": the
    project's own, for callers that name none): each repository has its own lanes and queue."""
    return f"{SUITE}.{locks.group_id(project_name)}" + (f".{repo}" if repo else "")


def suite_path(project_name: str, repo: str = ""):
    """Project `project_name`'s first suite lane file in repository `repo` (SUITE)."""
    return locks.slot_paths(checkcap.slot_dir(), _suite_resource(project_name, repo), 1)[0]


def _suite_resource(project_name: str, repo: str = "") -> str:
    return suite_base(project_name, repo)


def _lanes_of(base: str) -> list:
    """Every lane file of `base` there is now: its numbered lanes, then its overflow lanes."""
    d = checkcap.slot_dir()
    pat = re.compile(re.escape(base) + r"(?:-x)?\.(\d+)\.lock")
    try:
        found = [x for x in d.iterdir() if pat.fullmatch(x.name)]
    except OSError:
        return []
    return sorted(found, key=lambda x: (x.name.startswith(base + "-x"), int(pat.fullmatch(x.name).group(1))))


def suite_bases(project_name: str) -> list[str]:
    """The project's suite lane resources in use: one per repository, and one per wrapper lock ("w" and
    its checklane.wrap_key), that has lanes or waiters."""
    pre = f"{SUITE}.{locks.group_id(project_name)}"
    pat = re.compile(re.escape(pre) + r"(\.w?[0-9a-f]{12})?(?:-x)?(?:\.\d+\.lock|\.queue)")
    try:
        names = [x.name for x in checkcap.slot_dir().iterdir()]
    except OSError:
        return []
    return sorted({pre + (m.group(1) or "") for n in names if (m := pat.fullmatch(n))})


def lane_paths(project_name: str, n: int | None = None, repo: str = "", overflow: bool = False) -> list:
    """Repository `repo`'s lane files: its `n` numbered lanes (n None: every lane file there is now, of
    every base when `repo` is None), or with `overflow` its MAX_LANES overflow lanes (resource "<base>-x",
    for a cheaper waiter past checks.max_wait_s)."""
    from . import checklane
    if repo is None:
        return [x for b in suite_bases(project_name) for x in _lanes_of(b)]
    base = suite_base(project_name, repo)
    if n is None:
        return _lanes_of(base)
    return locks.slot_paths(checkcap.slot_dir(), base + ("-x" if overflow else ""),
                            checklane.MAX_LANES if overflow else n)


def age_s(cfg: dict | None = None) -> float:
    """Seconds a suite waiter waits per aging step: checks.priority_age_s of `cfg` (the project's
    config), else the account's, else SUITE_AGE_S (checklane.priority_age_s)."""
    from . import checklane
    return checklane.priority_age_s(cfg)


def suite_priority(ticket, now: float | None = None, age: float | None = None) -> float:
    """A suite waiter's rank now: its task priority (NO_PRIORITY without one), one step more urgent
    per `age` seconds it has waited (default age_s()), never past AGE_FLOOR (p1) nor its own priority."""
    prio = locks.ticket_priority(ticket)
    prio = NO_PRIORITY if prio == float("inf") else prio
    age = age_s() if age is None else age
    waited = max(0.0, (time.time() if now is None else now) - locks._arrival(ticket))
    return max(min(prio, AGE_FLOOR), prio - waited // age) if age > 0 else prio


def rank_text(ticket, now: float | None = None, age: float | None = None) -> str:
    """A waiter's task priority and its rank now (suite_priority) for a wait line, e.g. "p4 ranked p2
    (aged)" or "p3 (no task priority) ranked p3"."""
    prio = locks.ticket_priority(ticket)
    rank = suite_priority(ticket, now, age)
    if prio == float("inf"):
        return f"p{NO_PRIORITY} (no task priority) ranked p{rank:g}" + (" (aged)" if rank < NO_PRIORITY else "")
    return f"p{prio:g} ranked p{rank:g}" + (" (aged)" if rank < prio else "")


def _order_key(t, now: float | None = None, age: float | None = None) -> tuple:
    return suite_priority(t, now, age), locks._arrival(t), t.name


def suite_order(project_name: str, q: list | None = None, now: float | None = None, repo: str = "",
                age: float | None = None) -> list:
    """The project's suite waiters in repository `repo` in the order the lanes go to them: by
    suite_priority (aging step `age`, default age_s()), then arrival."""
    q = locks.queued(checkcap.slot_dir(), _suite_resource(project_name, repo)) if q is None else q
    age = age_s() if age is None else age
    return sorted(q, key=lambda t: _order_key(t, now, age))


def project_held(project_name: str) -> list:
    """The lane files of every repository and wrapper lock of the project held right now."""
    return [x for x in lane_paths(project_name, repo=None) if locks._slot_labels([x])]


@contextlib.contextmanager
def _cap_guard(project_name: str, cap: int):
    """Serialize the count-and-take of the project's lanes under its cap (none without one)."""
    if cap <= 0:
        yield
        return
    with open(checkcap.slot_dir() / f"{SUITE}.{locks.group_id(project_name)}.cap.guard", "a+") as g:
        fcntl.flock(g, fcntl.LOCK_EX)
        yield


def cap_turn(project_name: str, cap: int, ticket, slots: int, now: float | None = None,
             age: float | None = None) -> bool:
    """Whether waiter `ticket` may take a lane under the project's cap of `cap` checks at once (0: none):
    fewer than `cap` lanes held over all its repositories and wrapper locks, and `ticket` among the most
    urgent (_order_key) of the waiters a free lane of their own lets start, as many as the cap has room."""
    if cap <= 0:
        return True
    room = cap - len(project_held(project_name))
    if room <= 0:
        return False
    pre = f"{SUITE}.{locks.group_id(project_name)}"
    age = age_s() if age is None else age
    ready = []
    for base in suite_bases(project_name):
        key = base[len(pre) + 1:]
        n = 1 if key.startswith("w") else max(1, int(slots or 1))
        free = n - len([x for x in locks.slot_paths(checkcap.slot_dir(), base, n) if locks._slot_labels([x])])
        if free > 0:
            ready += suite_order(project_name, repo=key, now=now, age=age)[:free]
    if ticket not in ready:
        ready.append(ticket)   # one taking an overflow lane
    ready.sort(key=lambda t: _order_key(t, now, age))
    return ready.index(ticket) < room


def _reclaim(path, tell, state) -> bool:
    """Free lane `path` when its holder is dead: its pid is gone or its heartbeat stopped (the label's
    "hb" and the file untouched for max(HEARTBEAT_DEAD_S, 10 heartbeats)); logged (checklane.log_reclaim)
    and told. Under a guard lock, rechecked, so two waiters never free a lane twice."""
    from . import checklane
    recs = locks.holder_records([path])
    if not recs:
        return False
    h = recs[0]
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    hb = h.get("hb")
    why = ("its holder's process is gone" if h.get("alive") is False else
           f"its holder's heartbeat stopped {_mins(age)} ago"
           if isinstance(hb, (int, float)) and hb > 0 and age > max(checklane.HEARTBEAT_DEAD_S, 10 * hb) else "")
    if not why:
        return False
    try:
        with open(str(path) + ".reclaim", "a+") as g:
            fcntl.flock(g, fcntl.LOCK_EX)
            now = locks.holder_records([path])
            if not now or (now[0].get("pid"), now[0].get("since")) != (h.get("pid"), h.get("since")):
                return False
            path.unlink()
    except OSError:
        return False
    checklane.log_reclaim(state, h, why)
    tell and tell(f"reclaimed a checks lane: {why} ({checklane.holder_text(h, time.time())})")
    return True


def _heartbeat(f, every: float) -> None:
    """Touch held lane file `f` every `every` seconds until it closes (_reclaim reads the mtime)."""
    def beat():
        while not f.closed:
            try:
                os.utime(f.name)
            except OSError:
                pass
            time.sleep(every)
    threading.Thread(target=beat, daemon=True, name="checks-lane-heartbeat").start()


def suite_hold(project_name: str | None, tell: Callable[[str], None] | None = None,
               wait_s: float | None = None, waiting: Callable[[bool], None] | None = None,
               stop: Callable[[], str] | None = None, who: str = "", priority: int | None = None,
               since: float | None = None, slots: int = 1, repo: str = "", cost: dict | None = None,
               max_wait: float = 0.0, state=None, what: str = "full checks", lock: str = "", cap: int = 0,
               worktree: str = "", headroom: Callable[[], bool] | None = None,
               age: float | None = None) -> Grant | None:
    """Hold one of project `project_name`'s `slots` checks lanes in repository `repo` (repo_key) for its
    full checks (above), waiting while its other runs there hold them all or a more urgent waiter is ahead
    (suite_order); Grant(0) holding one, Grant() with no project or nested in a check, None once `wait_s`
    passed. It never waits for another project's runs or another repository's. A lane whose holder is
    dead is reclaimed (_reclaim). A waiter of `cost` (checklane.cost) that has waited `max_wait` seconds
    (0: never) and is cheaper than every holder (checklane.cheaper) takes an overflow lane alongside them.
    `waiting` and `stop` as for checkcap.acquire: `waiting(True)` once the wait starts and `waiting(False)`
    once it ends; a reason from `stop()` ends the wait with checkcap.Cancelled. `who` (e.g. "run 12") goes
    in its label, so a waiting run names the run it waits for; `what`: the commands it runs. `priority`:
    the task's (lower first); `since`: the arrival to queue at (default now), kept by a run that hands
    over. `state`: the project's state folder, for the reclaim log. It says who holds the lanes and its
    place in the queue at once and every checklane.TELL_S.

    `lock`: the run goes behind the wrapper lock of that checklane.wrap_key (checks.wrap_cmd), so it takes
    that lock's one lane, whatever its repository, and never a repository lane or an overflow lane: runs
    behind one wrapper lock go one at a time, and lock-free runs never wait behind them. `cap`: the most of
    the project's checks that run at once over all its lanes (checklane.project_cap; 0: none), the lane
    going to the most urgent waiter (cap_turn). A priority-1 waiter (`priority` <= 1) whose lanes are held
    only by less urgent runs takes an overflow lane beside them while `headroom()` says the host has room,
    never beside a holder in the same `worktree` (one suite per worktree). `age`: the aging step
    (age_s(), checks.priority_age_s; default the account's or SUITE_AGE_S)."""
    from . import checklane
    age = age_s() if age is None else age
    if not project_name or os.environ.get(HELD_ENV) or os.environ.get(checkcap.HELD_ENV):
        return Grant()
    if lock:   # one wrapper lock: its own single lane, never an overflow one
        repo, slots, max_wait, headroom = f"w{lock}", 1, 0.0, None
    slots = max(1, int(slots or 1))
    lanes = lane_paths(project_name, slots, repo)
    extra_lanes = lane_paths(project_name, slots, repo, overflow=True)
    task = os.environ.get("TTP_TASK") or ""
    label = f"{project_name}: full checks ({who + ', ' if who else ''}pid {os.getpid()})"
    try:   # the ticket names its task for suite_line
        ticket = locks.enqueue(checkcap.slot_dir(), _suite_resource(project_name, repo),
                               f"task #{task} {label}" if task.isdigit() else label, since, priority=priority)
    except OSError:
        return Grant()   # an unwritable slot directory: the checks slot says so
    arrival = locks._arrival(ticket)
    fields = {**({"task": task} if task.isdigit() else {}), **({"priority": priority} if priority is not None else {}),
              **({"cost": cost} if cost else {}), **({"lock": lock} if lock else {}),
              **({"worktree": str(worktree)} if worktree else {}), "hb": checklane.HEARTBEAT_S}
    start, told = time.time(), 0.0
    try:
        while True:
            held = [x for x in lanes + extra_lanes if locks._slot_labels([x])]
            for x in held:
                _reclaim(x, tell, state)
            # Free lanes before the queue: a lane freed after this count waits for the next turn, so the
            # queue that decides holds every waiter that arrived before it was freed.
            recs = locks.holder_records(lanes + extra_lanes)
            free = slots - len([x for x in lanes if locks._slot_labels([x])])
            q = suite_order(project_name, repo=repo, age=age)
            f = None
            cheaper = (max_wait > 0 and cost and time.time() - arrival >= max_wait and recs
                       and all(checklane.cheaper(cost, r.get("cost") or {}) for r in recs))
            urgent = (not cheaper and priority is not None and priority <= 1 and recs and headroom is not None
                      and all((r["priority"] if isinstance(r.get("priority"), (int, float)) else NO_PRIORITY) > 1 for r in recs)
                      and not (worktree and any(r.get("worktree") == str(worktree) for r in recs)) and headroom())
            try:
                with _cap_guard(project_name, cap):
                    if free > 0 and (ticket not in q or q.index(ticket) < free):
                        if cap_turn(project_name, cap, ticket, slots, age=age):
                            f = locks.try_take(lanes, label, what, extra=fields)
                    elif (cheaper or urgent) and cap_turn(project_name, cap, ticket, slots, age=age):
                        f = locks.try_take(extra_lanes, label, what, extra={**fields, "overflow": True})
                        held_by = "; ".join(checklane.holder_text(r, time.time()) for r in recs[:3])
                        if f is not None and tell and cheaper:
                            tell(f"took an extra checks lane alongside the holders after {_mins(time.time() - arrival)}: "
                                 f"this run is cheaper than {held_by}")
                        elif f is not None and tell:
                            tell(f"took an extra checks lane beside less urgent runs ({held_by}): this run's task is "
                                 f"priority {priority:g} and the host has room")
            except OSError:
                return Grant()
            if f is not None:
                _heartbeat(f, checklane.HEARTBEAT_S)
                return Grant(0, [f], time.time() - start)
            if stop and (why := stop()):
                raise checkcap.Cancelled(why)
            if wait_s is not None and time.time() - start >= wait_s:
                return None
            if not told or time.time() - told >= checklane.TELL_S:
                if not told and waiting:
                    waiting(True)
                told = time.time()
                pos = (q.index(ticket) + 1) if ticket in q else 1
                rule = ("one at a time behind this wrapper lock (checks.wrap_cmd)" if lock else
                        f"{'one' if slots == 1 else slots} at a time per project and repository, "
                        f"checks.{SLOTS_KEY} sets it")
                rule += f"; at most {cap} of the project's checks at once (checks.test_project_max)" if cap > 0 else ""
                recs = recs or locks.holder_records(project_held(project_name))
                held_by = "; ".join(checklane.holder_text(r, told) for r in recs[:3]) or "a run that just ended"
                tell and tell(f"waiting for this project's other full checks to end ({rule}, most urgent priority "
                              f"first; this run {rank_text(ticket, told, age)}, position {pos} of {max(len(q), pos)}, "
                              f"{pos - 1} waiter(s) ahead, waited {_mins(told - arrival)}), held by {held_by}")
            time.sleep(poll_s(POLL_S))
    finally:
        locks.dequeue(ticket)
        if told and waiting:
            waiting(False)


def suite_running(grant, cap_s: float) -> None:
    """Mark the suite lock `grant` holds as running its commands from now under a wall-time cap of
    `cap_s` seconds (0: none), for suite_line."""
    for f in getattr(grant, "files", None) or []:
        if os.path.basename(getattr(f, "name", "")).startswith(SUITE + "."):
            locks.relabel(f, {"run_since": time.time(), "cap_s": float(cap_s or 0)})


def _mins(s: float) -> str:
    m = int(max(0.0, s) // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def suite_line(project_name: str, now: float | None = None, limit: int = 0, age: float | None = None) -> str:
    """`ttp status`'s line on the project's full checks slot: its holder, how long it has run against
    its wall-time cap, and the waiting order (suite_order, aging step `age`) with each waiter's priority
    and rank (rank_text), with a wait naming the `limit` (lanes per repository) and checks.slots, which
    raises it; "" while it is free and nobody waits."""
    now = time.time() if now is None else now
    age = age_s() if age is None else age
    try:
        recs = locks.holder_records(lane_paths(project_name, None, None))
        q = []
        pre = f"{SUITE}.{locks.group_id(project_name)}"
        for base in suite_bases(project_name):
            q += suite_order(project_name, now=now, repo=base[len(pre) + 1:], age=age)
        q.sort(key=lambda t: _order_key(t, now, age))
    except OSError:
        return ""
    if not recs and not q:
        return ""
    held = []
    for h in recs:
        what = f"#{h['task']}" if h.get("task") else str(h.get("holder") or "a run")
        what += f" p{h['priority']}" if h.get("priority") is not None else ""
        if h.get("run_since"):
            cap = float(h.get("cap_s") or 0)
            what += (f" running {_mins(now - float(h['run_since']))}"
                     + (f" of its {_mins(cap)} cap" if cap else " (no cap)"))
        else:
            what += f" held {_mins(now - float(h.get('since') or now))}, not running yet"
        held.append(what)
    what = "; ".join(held) or "free"
    waits = []
    for t in q:
        try:
            m = re.search(r"task #(\d+)", t.read_text())
        except OSError:
            m = None
        waits.append(f"{'#' + m.group(1) if m else 'a run'} {rank_text(t, now, age)} "
                     f"waited {_mins(now - locks._arrival(t))}")
    return (f"full checks slot: {what}"
            + (f"; waiting in order: {', '.join(waits[:6])}" + (f" +{len(waits) - 6}" if len(waits) > 6 else "")
               + (f" ({limit} at a time per repository; checks.{SLOTS_KEY} raises it)" if limit else "")
               if waits else ""))


def use(kind: Kind = TESTS) -> str:
    """One status line on the host pool's use, or "" when it is off or idle."""
    s = sizes(kind=kind)
    if s is None:
        return ""
    try:
        held = len(locks.held_labels(paths(s[0], kind)))
    except OSError:
        return ""
    return f"host {kind.name} slots: {held} of {s[0]} in use (at most {s[1]} per run)" if held else ""


def use_lines() -> list[str]:
    """use() of the test and the build pool, each only while in use (ttp status, the web feed)."""
    return [line for kind in (TESTS, BUILDS) if (line := use(kind))]


_OPS = ("&&", "||", ";;", "|&", ";", "|", "&", "\n", "(", ")")
_STOP = " \t\n;|&()"
_PYTESTS = ("pytest", "py.test")
_BUILDS = ("make", "gmake", "ninja")
_WRAPPERS = ("nice", "ionice", "time", "env", "nohup", "stdbuf", "timeout", "chrt", "taskset", "ttp", "sandbox", "clip")


def _segments(cmd: str) -> list[list[tuple[int, int, str]]]:
    """The simple commands of shell line `cmd`, each a list of its words as (start, end, unquoted
    text), split at the operators outside quotes, so a '|' or ';' in a quoted -k expression stays
    inside its word. Redirections such as 2>&1 stay words; a comment ends at the line's end. An unquoted
    $(...) stays inside its word (`-j$(nproc)`, `$(nproc)`), and the commands in it follow as segments
    of their own, so `echo $(make -j 64)` still shows its make."""
    segs, cur, inner, i, n = [], [], [], 0, len(cmd)
    while i < n:
        if cmd[i] in " \t":
            i += 1
            continue
        if cmd[i] == "#":
            i = cmd.find("\n", i) if "\n" in cmd[i:] else n
            continue
        if not cmd.startswith("&>", i) and (op := next((o for o in _OPS if cmd.startswith(o, i)), None)):
            segs.append(cur)
            cur, i = [], i + len(op)
            continue
        start, word = i, []
        while i < n and (cmd[i] not in _STOP or (i == start and cmd.startswith("&>", i))):
            c = cmd[i]
            if cmd.startswith((">&", "<&", "&>"), i):
                word.append(cmd[i:i + 2])
                i += 2
            elif c == "\\":
                word.append(cmd[i + 1:i + 2])
                i += 2
            elif cmd.startswith("$(", i):
                j = _close(cmd, i + 1)
                word.append(cmd[i:j + 1])
                inner += [[(a + i + 2, b + i + 2, w) for a, b, w in sub] for sub in _segments(cmd[i + 2:j])]
                i = j + 1
            elif c == "'":
                j = cmd.find("'", i + 1)
                j = n if j < 0 else j
                word.append(cmd[i + 1:j])
                i = j + 1
            elif c == '"':
                i += 1
                while i < n and cmd[i] != '"':
                    if cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                        i += 1
                    word.append(cmd[i])
                    i += 1
                i += 1
            else:
                word.append(c)
                i += 1
        cur.append((start, min(i, n), "".join(word)))
    segs.append(cur)
    return [s for s in segs if s] + inner


def _close(cmd: str, i: int) -> int:
    """The index of the ')' closing the '(' at `i` (len(cmd) when none), past quoted text and nested ()."""
    depth, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            i += 1
        elif c == "'":
            i = cmd.find("'", i + 1)
            i = n if i < 0 else i
        elif c == '"':
            i += 1
            while i < n and cmd[i] != '"':
                i += 2 if cmd[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return i
        i += 1
    return n


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _is_tool(words: list[str], i: int, build: bool) -> bool:
    w = _base(words[i])
    if not build:
        return w in _PYTESTS
    return w in _BUILDS or (w == "cmake" and "--build" in words[i + 1:])


def _runs(words: list[str], i: int) -> bool:
    """Whether word `i` is the command its line runs: only assignments, options, numbers and wrappers
    (nice, env, timeout, ttp sandbox ...) before it, so `pip install ninja` runs no build."""
    return all(re.match(r"[A-Za-z_]\w*=|-|\d", w) or _base(w) in _WRAPPERS for w in words[:i])


def _shell_value(w: str) -> bool:
    """Whether word `w` is a value the shell fills in: $(...), `...` or $VAR."""
    return "$" in w or "`" in w


# ninja calls that build nothing: a tool (-t clean, -t compdb), a dry run, --version, help
_NINJA_INFO = re.compile(r"-t.*|-n|--version|-h|--help")


def _edits(cmd: str, k: int, build: bool) -> list[tuple[int, int, str, int]]:
    """(start, end, new text, old count) for each worker count in `cmd` above `k`: pytest's -n N /
    --numprocesses N after a pytest word (`build` False), or a build's -j N / --jobs N / --parallel N
    after a make, gmake, ninja or `cmake --build` word (`build` True; there a 0, unlimited, or a bare
    make or cmake -j also becomes k, a ninja run as the command with no -j gets -j k unless it only runs
    a tool or prints (-t, -n, --version, --help), and a count the shell fills in ($(nproc), `nproc`,
    $N: old count -1) becomes `-j k` with its flag and value). Words are matched unquoted and only whole
    flag or count words change, so `head -n 50`, `nice -n 19 pytest` and a quoted -k expression keep theirs."""
    out = []
    if k <= 0:
        return out
    short, longs = ("-j", ("--jobs", "--parallel")) if build else ("-n", ("--numprocesses",))
    for seg in _segments(cmd):
        words = [w for _, _, w in seg]
        tool = next((i for i, w in enumerate(words) if _is_tool(words, i, build)), None)
        if tool is None:
            continue
        name = _base(words[tool])
        seen = info = False
        for i in range(tool + 1, len(seg)):
            start, end, w = seg[i]   # a quoted flag or count counts too; its word becomes plain text
            info = info or (name == "ninja" and bool(_NINJA_INFO.fullmatch(w)))
            m = re.fullmatch(rf"({re.escape(short)}|{'|'.join(map(re.escape, longs))})(=?)(.*)", w)
            if not m or (m.group(1) != short and m.group(1) not in longs):
                continue
            flag, eq, num = m.groups()
            dyn = build and _shell_value(num)
            if num and not num.isdigit() and not dyn:
                continue
            seen = True
            nxt = seg[i + 1] if not num and not eq and i + 1 < len(seg) else None
            if nxt and build and _shell_value(nxt[2]):
                out.append((start, nxt[1], f"-j {k}", -1))   # `-j $(nproc)`: the flag and its value
                continue
            if nxt and nxt[2].isdigit():
                start, end, num = nxt
                prefix = ""
            else:
                prefix = flag + eq
            if dyn:
                out.append((start, end, f"-j {k}", -1))   # `-j$(nproc)`, `--jobs=$N`: unknown before it runs
            elif num:
                n = int(num)
                if n > k or (build and n == 0):
                    out.append((start, end, f"{prefix}{k}", n))
            elif build and not eq and name != "ninja":
                out.append((start, end, f"{flag} {k}", 0))   # a bare make or cmake -j: unlimited
        if build and not seen and not info and name == "ninja" and _runs(words, tool):
            out.append((seg[tool][1], seg[tool][1], f" -j{k}", 0))   # ninja's default is every core and then some
    return out


def _apply(cmd: str, edits) -> str:
    for start, end, text, _ in sorted(edits, reverse=True):
        cmd = cmd[:start] + text + cmd[end:]
    return cmd


def over(cmd: str, k: int) -> list[int]:
    """The explicit pytest `-n N` / `--numprocesses N` values in `cmd` above `k` (as clamp() finds them)."""
    return [n for *_, n in _edits(cmd, k, False)]


def clamp(cmd: str, k: int) -> str:
    """`cmd` with each explicit pytest `-n N` / `--numprocesses N` above `k` lowered to `k`; only after
    a pytest word (by its basename: `.venv/bin/pytest`, `python3 -m pytest`) in the same command, so
    `head -n 50` or `nice -n 19 pytest` keep theirs. The rest of the text stays as it is."""
    return _apply(cmd, _edits(cmd, k, False))


def builds(cmd: str) -> bool:
    """Whether `cmd` runs a build tool: make, gmake, ninja or `cmake --build`."""
    return any(_is_tool(words, i, True) and _runs(words, i) for seg in _segments(cmd)
               for words in [[w for *_, w in seg]] for i in range(len(words)))


def clamp_build(cmd: str, k: int) -> str:
    """`cmd` with each build's job count above `k` lowered to `k` (_edits, `build`)."""
    return _apply(cmd, _edits(cmd, k, True))


def clamped(cmd: str, k: int) -> str:
    """clamp() and clamp_build(), with a first line that says on stderr what it lowered (nothing when none)."""
    big, jobs = over(cmd, k), _edits(cmd, k, True)
    if not big and not jobs:
        return cmd
    said = ([f"pytest -n {', '.join(map(str, sorted(set(big))))} lowered to -n {k}"] if big else []) \
        + ([f"build jobs capped at -j {k}"] if jobs else [])
    note = f"ttp: {'; '.join(said)}, this run's test-worker budget on the host ({SLOTS_ENV}={k})"
    return f"echo {shlex.quote(note)} >&2\n{clamp_build(clamp(cmd, k), k)}"


def clamp_for(cmd: str, env: dict | None) -> str:
    """clamp() to the grant `env` (default this process's) carries, else `cmd` as it is."""
    k = _number((os.environ if env is None else env).get(HELD_ENV))
    return clamped(cmd, k) if k else cmd


def _section(cfg: dict | None, name: str) -> dict:
    v = (cfg or {}).get(name)
    return v if isinstance(v, dict) else {}


def nice_level(cfg: dict | None = None) -> int:
    """The nice level 0-19 of check commands: checks.nice (a number; the project's, then the account's),
    else runner.nice, else NICE. A value that is no whole number falls back to the next source."""
    acct = project.load_account_settings().get("checks") or {}
    for v in (_section(cfg, "checks").get("nice"), acct.get("nice"), _section(cfg, "runner").get("nice")):
        if isinstance(v, bool) or v in (None, ""):
            continue
        try:
            return max(0, min(19, int(v)))
        except (TypeError, ValueError):
            continue
    return NICE


def ionice_args(cfg: dict | None = None) -> list:
    """The ionice arguments from checks.ionice (the project's, then the account's): 'idle' is -c3,
    'best-effort' or 'best-effort:N' (N 0-7) is -c2 -n N; an invalid value falls back to the next
    source; unset or invalid is IONICE."""
    acct = project.load_account_settings().get("checks") or {}
    for v in (_section(cfg, "checks").get("ionice"), acct.get("ionice")):
        if not isinstance(v, str):
            continue
        cls, _, lvl = v.strip().lower().partition(":")
        if cls == "idle" and not lvl:
            return ["-c3"]
        if cls == "best-effort" and (not lvl or (lvl.isdigit() and 0 <= int(lvl) <= 7)):
            return ["-c2", "-n", lvl] if lvl else list(IONICE)
    return list(IONICE)


def low_priority(argv: list) -> list:
    """A check command's argv under `nice` (nice_level) and `ionice` (ionice_args) where they exist, so a
    host's live services go first; either missing is skipped. checks.nice false (account setting)
    runs it as it is."""
    if (project.load_account_settings().get("checks") or {}).get("nice") is False:
        return list(argv)
    cfg = _cfg()
    return ((["nice", "-n", str(nice_level(cfg))] if shutil.which("nice") else [])
            + (["ionice", *ionice_args(cfg)] if shutil.which("ionice") else []) + list(argv))

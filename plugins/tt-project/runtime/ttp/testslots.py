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
and IO priority (low_priority(): nice 19, idle IO), so a host's live services win. One project's suites run
at most project_workers() test workers at once between them, and no new suite takes slots while the host's
runnable CPU demand is above its cores (load_guard()); running ones are never stopped.

Defaults (sizes()): the pool is half the host's usable cores, at least 2, so tests leave the other
half to live work and builds; one run takes at most min(RUN_MAX, half the pool), at least 1, so two
runs always fit side by side and no suite takes the pool. Unset, the test pool a run takes from also
grows into spare cores (pool_now: the slots in use plus the cores the CPU demand (checkcap._load) leaves, at most
the host's cores), so the extra lanes an idle host gives (checklane.capacity) get their workers; a busy
host keeps the half, as does checks.load_extra 0. A test grant is also at most the host's live free cores, less one for
each worker granted in the last few seconds, at least WORKERS_FLOOR (live_room; checks.live_cores 0
turns it off), so a run on a busy host starts fewer workers instead of oversubscribing it. The account settings checks.test_pool and
checks.test_run_max (or TTP_TEST_POOL / TTP_TEST_RUN_MAX) set them; checks.test_pool 0 turns the pool
off. A project's own checks.test_pool / checks.test_run_max may only lower them. A full `ttp checks` run
(`full`) waits in line until at least run_min() slots are free in both the pool and its project's
budget, then starts with at least that many, so a suite never runs for half an hour on one or two
workers; with a pool or budget smaller than that it starts with what they can give. Pre-stages, push
checks and builds keep at least one. A worker's own pytest asking for more than one worker takes slots
through `ttp clip` or `ttp sandbox` (direct): at most its TTP_TEST_SLOTS, fewer while others hold them,
held by the test process itself, so they free when it ends or dies.

One project holds at most project_max() slots at once over all its runs, as per-project lock files
beside the pool's, so a burst of one project's checks leaves room for the others. Its default is the
project's checks at once (checklane.project_cap: checks.test_project_max, else its lanes) times one
run's grant, at most the pool, so every check under the cap gets its slots; an explicit slot budget
(checks.test_project_slots or TTP_TEST_PROJECT_SLOTS) wins. Waiters are served urgent first, then first-come (a
locks.enqueue ticket each, freed with its process). A waiter whose project is at its budget steps out
of the host's line, so it never holds up other projects, but keeps its ticket in its project's line
(the "<own>.<project id>" queue): it re-enters the host's line at its first arrival, and a later
arrival of the same project waits until every earlier one is back in the host's line, so a freed slot
goes to the project's earliest waiter, never to whichever polls first. Checks gating
a push to the push branch (`push_key`), and urgent runs on a reserved urgent slot (checkcap.urgent_key), never
wait behind task checks: they take the pool's free slots (up to one run's grant) past the host's line and
their project's budget, and while none is free they hold the line's first place (an owed run's priority), so
the slots that free next are theirs. They draw on the pool: while it is busy they may run with fewer workers.
With checks.priority_beyond_cap true (checkcap.priority_beyond_cap) they instead never run short: with fewer
than one run's grant free in the pool or their project's budget, they take that branch's reserved lane beyond
both (RESERVED), which makes up one run's grant, holds for one push check of the branch at a time and
which task checks never use. While any other run waits for test slots (others_wait: a checks run parked
for them, or a waiter in the host's or its project's line) they run on that lane alone and take none of
the pool's or their project's slots, so back-to-back pushes never keep a waiting run out.

A project's full checks (`ttp checks`) also hold its suite lock (suite_hold()): a second full run of
the same project waits for the first, other projects never do. It is a file lock, so an exit, a crash
or a reboot frees it. Its waiters queue (a locks.enqueue ticket each) and it goes to the most urgent
task priority first (p1 before p2 ...; an obligation owner's 0 before all), then arrival (suite_order);
a running holder is never pre-empted. A waiter without a task priority (no task, a task without one,
an older runtime) ranks as NO_PRIORITY (p3). A waiter ages one priority step per `checks.priority_age_s`
waited (default SUITE_AGE_S; checklane.priority_age_s), never past p2 (AGE_FLOOR; an obligation owner's
0 stays 0), so a p1 or urgent run (checkcap.task_rank) always goes before every routine one, even one that
waited hours. Among equal ranks a native priority goes before an aged one (a native p2 before a p4 aged
to p2), until the aged one has waited FAIR_STEPS steps: then arrival decides (_order_key). Accepted gap:
a steady stream of p1 runs can hold routine runs back; p1s are rare and each runs once. Wait lines show each waiter's priority and its rank now (rank_text).
A run holding it is stopped at the checks wall-time cap (checkwall); checklane.lane_lines shows the
holder's time against that cap and each lane's waiters with what each waits on (`ttp status`).

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

import collections
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import checkcap, checkwait, hostcpu, locks, poll_s, project, repoid

RESOURCE = "test-slots"    # check-slots/test-slots.<i>.lock
RUN_MAX = 8                # one run's most workers by default: past about 8 a suite's wall time gains little
WORKERS_FLOOR = 2          # a run's fewest workers when the host's free cores are short (live_room)
RUN_MIN_KEY, RUN_MIN_ENV = "test_run_min", "TTP_TEST_RUN_MIN"   # a full run's fewest workers (run_min)
SLOTS_ENV = "TTP_TEST_SLOTS"
XDIST_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
HELD_ENV = "TTP_TEST_SLOTS_HELD"   # set for the commands of a run that holds slots: the grant
OWN = "test-slots-project"     # check-slots/test-slots-project.<project id>.<i>.lock: one project's budget
RESERVED = "test-slots-push"   # check-slots/test-slots-push.<branch id>.lock: a push branch's reserved lane
SUITE = "test-suite"           # check-slots/test-suite.<project id>.lock: one project's full checks
SLOTS_KEY = "slots"            # checks.slots: how many full checks of one project and repository run at once
PORTS = "check-ports"          # check-slots/check-ports.<i>.lock: one running check run's port block
# A check run's own ports (port_block): PORT_BLOCKS blocks of PORT_SPAN from PORT_FIRST, 20000-31999,
# below Linux's ephemeral range (32768-60999), so two suites that run at once never pick the same port.
PORT_FIRST, PORT_SPAN, PORT_BLOCKS = 20000, 100, 120
PORT_BASE_ENV, PORT_COUNT_ENV = "TTP_PORT_BASE", "TTP_PORT_COUNT"
POLL_S = 5.0
HEAD_POLL_S = 1.0     # a suite waiter next in line checks this often, so a freed lane goes to it soon
SUITE_AGE_S = 900.0   # default checks.priority_age_s: a suite waiter gains one priority step per this many seconds waited
NO_PRIORITY = 3      # a ticket without a priority (no task, a task without one, an older runtime) ranks as p3
AGE_FLOOR = 2        # aging never ranks a routine waiter above p2: native p1s and urgent runs go first
FAIR_STEPS = 4       # a waiter past this many aging steps (1 h by default) goes by arrival among its rank
TELL_S = 300.0
NICE = 19            # checks run at the lowest CPU priority by default: every other process goes first
PROJECT_WORKERS_MAX = 8       # checks.test_project_workers base: at most this many test workers per project ...
CORES_PER_PROJECT_WORKER = 8  # ... and one per this many cores, at least WORKERS_FLOOR (project_workers)
SPARE_LOAD = 0.75    # unset, a project's test-worker ceiling grows by the cores its load leaves under this x the cores
LOAD_GUARD = 1.0     # default checks.load_guard: no suite starts while the CPU demand is above this x the cores
PUSH_LOAD_WAIT_S, PUSH_LOAD_WAIT_KEY, PUSH_LOAD_WAIT_ENV = 300.0, "push_load_wait_s", "TTP_CHECKS_PUSH_LOAD_WAIT_S"
# (push_load_wait_s) the longest push checks wait under load_guard before their branch's reserved lane
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

    def __init__(self, name: str, resource: str, own: str, held_env: str, unit: str, budget: str):
        self.name, self.resource, self.own, self.held_env, self.unit = name, resource, own, held_env, unit
        self.env_prefix = f"TTP_{name.upper()}_"
        self.budget = budget   # what setting() names a project's slot budget by

    def setting(self, what: str) -> tuple[str, str]:
        """(environment variable, settings key) of `what` (pool, run_max, project_max, project_slots)."""
        return self.env_prefix + what.upper(), f"{self.name}_{what}"


BUILD_HELD_ENV = "TTP_BUILD_SLOTS_HELD"   # set for a build that holds build slots: the grant
# Tests: checks.test_project_max is how many checks run at once (checklane.project_cap), so the slot
# budget has a key of its own; builds keep build_project_max as their slot budget.
TESTS = Kind("test", RESOURCE, OWN, HELD_ENV, "test worker", "project_slots")
BUILDS = Kind("build", "build-slots", "build-slots-project", BUILD_HELD_ENV, "build job", "project_max")
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


def pool_now(cfg: dict | None, pool: int, held: int, cores: int | None = None, load: float | None = None,
             kind: Kind = TESTS) -> int:
    """The test pool a run may take from now: with checks.test_pool unset (account and project), the
    `held` slots in use plus the host's spare cores (its cores less the CPU demand, checkcap._load, which
    counts the running ones), at most its cores, never below the default `pool`; so the half of the
    cores the pool leaves serves more checks only while nothing else keeps it busy. An unreadable load
    average, a set pool, checks.load_extra 0 (checkcap.extra_max) and the build pool keep `pool`."""
    if kind is not TESTS or not checkcap.extra_max() or _setting(*kind.setting("pool")) is not None \
            or _number(_section(cfg, "checks").get(kind.setting("pool")[1])):
        return pool
    c = checkcap.cores() if cores is None else cores
    ld = checkcap._load() if load is None else load
    if ld != ld or ld == float("inf"):
        return pool
    return max(pool, min(c, held + int(max(0.0, c - ld))))


def run_min(cfg: dict | None, run_max: int) -> int:
    """The fewest test slots a full checks run starts with: checks.test_run_min (the project's, then
    TTP_TEST_RUN_MIN or the account's), default max(WORKERS_FLOOR, run_max // 2); at most run_max."""
    n = _number(_section(cfg, "checks").get(RUN_MIN_KEY)) or _setting(RUN_MIN_ENV, RUN_MIN_KEY) \
        or max(WORKERS_FLOOR, run_max // 2)
    return max(1, min(n, run_max))


def live_room(every: list) -> int | None:
    """The test workers a run may start for the host's live free cores (checkcap.free_cores), less one
    for each slot among `every` taken within checkcap.RAMP_S (those workers may not show yet), at least
    WORKERS_FLOOR; None with checks.live_cores 0 or no reading."""
    if not checkcap.live_cores_on() or (free := checkcap.free_cores()) is None:
        return None
    now = time.time()
    young = sum(1 for s in checkcap.held_since(every) if now - s < checkcap.RAMP_S)
    return max(WORKERS_FLOOR, int(free - young))


def project_workers(cfg: dict | None = None, cores: int | None = None, project_name: str | None = None) -> int:
    """The most test workers one project runs at once over all its suites (0: no such ceiling):
    TTP_TEST_PROJECT_WORKERS, else the account's checks.test_project_workers, else the base (the smaller of
    PROJECT_WORKERS_MAX and one per CORES_PER_PROJECT_WORKER cores, at least WORKERS_FLOOR, raised to the
    host-wide checks cap times run_min (admitted_workers) within the test pool) plus the cores the CPU
    demand leaves under SPARE_LOAD x the cores, at most half the cores (spare_workers); a project's own
    checks.test_project_workers only lowers it. So a project's suites take a small share of a busy host
    however many of them run at once, and more of an idle one, and every run the host-wide cap admits
    can start at run_min. The load `project_name`'s own held test slots add is not counted (_own_held):
    its own suites never shrink its ceiling, others' load does, and the host's pool, live free cores
    (live_room) and load guard still bound the total."""
    top = _setting(*TESTS.setting("project_workers"))
    if top is None:
        c = checkcap.cores() if cores is None else cores
        top = max(WORKERS_FLOOR, min(PROJECT_WORKERS_MAX, c // CORES_PER_PROJECT_WORKER), admitted_workers(cfg, c))
        top = max(top, min(c // 2, top + spare_workers(c, _own_held(project_name, c) if project_name else 0)))
    if (mine := _number(_section(cfg, "checks").get("test_project_workers"))) and mine > 0:
        top = min(top, mine) if top else mine
    return max(0, top)


def admitted_workers(cfg: dict | None, c: int) -> int:
    """The test workers the runs the host-wide checks cap admits need to start at run_min each:
    checkcap.limit() x run_min(), at most the test pool (sizes); 0 with the cap off (0) or the pool off."""
    s = sizes(cfg, cores=c)
    n = checkcap.limit()
    return min(s[0], n * run_min(cfg, s[1])) if s and n > 0 else 0


def spare_workers(c: int, own: int = 0) -> int:
    """The cores the host's runnable CPU demand (checkcap.cpu_demand, the load guard's reading: busy cores
    and tasks waiting to run, not tasks waiting on I/O) leaves under SPARE_LOAD x its `c` cores, less `own`,
    the load one worker per slot the asking project holds puts there (never below 0); 0 with
    checks.load_extra 0 or an unreadable reading. A shrink bounds new grants only: holders above it still
    count (_own_free)."""
    if not checkcap.extra_max():
        return 0
    ld = checkcap.cpu_demand()
    if ld != ld or ld == float("inf"):
        return 0
    return max(0, int(c * SPARE_LOAD - max(0.0, ld - own)))


def _own_held(project_name: str, c: int) -> int:
    """How many test slots `project_name` holds now, its budget's and its urgent ones (one worker each)."""
    try:
        return len(locks._slot_labels(own_paths(project_name, c) + urgent_paths(project_name, c)))
    except OSError:
        return 0


def load_guard_factor() -> float:
    """checks.load_guard: TTP_CHECKS_LOAD_GUARD, else the account's setting, else LOAD_GUARD; 0 is off."""
    for v in (os.environ.get("TTP_CHECKS_LOAD_GUARD"), (project.load_account_settings().get("checks") or {}).get("load_guard")):
        if v in (None, "") or isinstance(v, bool):
            continue
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    return LOAD_GUARD


def load_guard() -> str:
    """Why no new suite may start now, or "": the host's runnable CPU demand (checkcap.cpu_demand: busy
    cores and tasks waiting to run, not tasks waiting on I/O) is above checks.load_guard
    (load_guard_factor()) times its cores at every reading over checks.load_sustain_s (checkcap.sustained_load:
    a spike of short-lived processes holds nothing), or its memory is short (hostcpu.mem_short). 0 turns the guard
    off; an unreadable reading never holds a suite. Disk contention is diskgate's limit
    (checks.io_full_max), not this one."""
    factor = load_guard_factor()
    if not factor:
        return ""
    if mem := hostcpu.mem_short():
        return f"the host is {mem}; no new suite starts until memory frees, running ones go on"
    ld, c = checkcap.sustained_load(), checkcap.cores()
    if ld != ld or ld == float("inf") or ld <= factor * c:
        return ""
    return (f"the host's CPU is overloaded: its runnable demand of {ld:.1f} cores is above "
            + (f"its {c} cores" if factor == 1.0 else f"{factor:g} x its {c} cores")
            + " (CPU limit checks.load_guard); no new suite starts until it drops, running ones go on")


LOAD_PASS = "test-slots-load-pass"   # check-slots/test-slots-load-pass.lock: the one push or urgent run past the load guard
LOAD_URGENT = "test-slots-load-urgent"   # the one urgent run (priority 1 or better) past it beside LOAD_PASS's
LOAD_OWED = "test-slots-load-owed"   # the one priority-0 run (owed to the user) past it beside LOAD_PASS's
LOAD_FLOOR = "test-slots-load-floor"   # the one run of any priority past it, so the queue keeps moving
LOAD_GATE = "host load: "   # pool_gate's reason while the load guard holds a run (gate_lanes records it)
STARVED_GATE = " run(s) of its project parked longer go first (checkcap.starved_ahead)"   # its reason's end there
OWED_GATE = " owed run(s) of its project parked go first by priority (checkcap.owed_ahead)"   # its reason's end there
BUDGET_GATE = "its project's test-worker budget: "   # its reason's start while the project's budget holds the run
OWN_RESERVE = "kept for its project's urgent run(s) parked for their test slots"   # pool_gate's reason there


def load_pass_path() -> Path:
    return checkcap.slot_dir() / f"{LOAD_PASS}.lock"


def load_pass_paths(priority: int | None = None, push: bool = False, since: float | None = None) -> list[Path]:
    """The passes a run may take past the load guard, in the order it tries them: a priority-0 run (an
    obligation owed to the user, checkcap.OBLIGATION_PRIORITY) its own one (LOAD_OWED), so a less urgent
    holder of the shared pass never holds it; an urgent run (locks.URGENT_PRIORITY or better) its own one
    (LOAD_URGENT), then a push-queue or urgent run the shared load pass, so two urgent runs of any projects
    start side by side and leave none of them waiting on the other; and any run the floor pass (LOAD_FLOOR)
    while no run parked under the load guard goes before it (floor_ahead), so a host whose load comes from
    other work still starts one check at a time and its queue never stalls while its slots are free. A fixed
    pass count, not urgent runs joining one pass, keeps the guard's bound: at most four suites start past it."""
    d, out = checkcap.slot_dir(), []
    if priority is not None and priority <= checkcap.OBLIGATION_PRIORITY:
        out.append(d / f"{LOAD_OWED}.lock")
    if priority is not None and priority <= locks.URGENT_PRIORITY:
        out.append(d / f"{LOAD_URGENT}.lock")
    if push or (priority is not None and priority <= locks.URGENT_PRIORITY):
        out.append(load_pass_path())
    if not floor_ahead(priority, since):
        out.append(d / f"{LOAD_FLOOR}.lock")
    return out


def floor_ahead(priority: int | None = None, since: float | None = None) -> bool:
    """Whether a run parked for its test slots (checkcap.PARKED) that the load guard alone holds (its lane's
    gate says so: gate_lanes) goes before a run at `priority` that arrived at `since` (default now; its own
    PARKED ticket's, if it holds one): more urgent, or as urgent and older (none: push checks, the least).
    A parked run held by anything else (its project's budget, the pool) keeps no place for the floor pass."""
    d = checkcap.slot_dir()
    try:
        q = locks.queued(d, checkcap.PARKED)
    except OSError:
        return False
    mine = getattr(checkcap._PARKS, "park", None)
    rank = ((locks.ticket_priority(mine), locks._arrival(mine)) if mine in q else
            (math.inf if priority is None else float(priority), time.time() if since is None else since))
    ahead = {ticket_pid(t) for t in q if t != mine and (locks.ticket_priority(t), locks._arrival(t)) < rank}
    if not ahead:
        return False
    return any(r.get("pid") in ahead and str(r.get("gate") or "").startswith(LOAD_GATE)
               for r in locks.holder_records(sorted(d.glob(f"{SUITE}.*.lock"))))


def load_passes(priority: int | None = None, push: bool = False, since: float | None = None) -> bool:
    """Whether a run may pass the load guard: one of its passes (load_pass_paths) is free; acquire takes
    it with its grant."""
    try:
        return locks.any_free(load_pass_paths(priority, push, since))
    except OSError:
        return False


def load_held(priority: int | None = None, push: bool = False, since: float | None = None) -> str:
    """load_guard()'s reason while it holds this run (load_passes: no pass of its free, and this thread's
    run holds none: reserve_load_pass), else ""."""
    why = load_guard()
    return "" if not why or reserved_pass() is not None or load_passes(priority, push, since) else why


_RESERVED = threading.local()   # .grant: the load pass this thread's run took at its gate (reserve_load_pass)


def reserved_pass():
    return getattr(_RESERVED, "grant", None)


def reserve_load_pass(holder: str, priority: int | None = None, push: bool = False,
                      since: float | None = None) -> str:
    """Take this run's load pass (load_pass_paths) the moment its gate passes under the load guard, and
    hold it across its wait for a host-wide checks slot until acquire takes it with its grant: a parked
    run that passed the gate stops counting for floor_ahead (its lane's gate is "", its PARKED ticket
    gone), so without it every other parked run would see the floor free too and take a host-wide slot
    only to wait under the guard. "" once this run holds a pass or the guard is off, else the guard's
    reason (pool_gate's LOAD_GATE one)."""
    if reserved_pass() is not None or not (load := load_guard()):
        return ""
    try:
        f = locks.try_take(load_pass_paths(priority, push, since), holder, "load-guard pass",
                           extra={"priority": priority} if priority is not None else None)
    except OSError:
        f = None
    if f is None:
        return f"{LOAD_GATE}{load}"
    _RESERVED.grant = f
    return ""


def release_load_pass() -> None:
    """Let go of the pass reserve_load_pass took (the run parked again, or its wait ended without
    acquire taking it)."""
    f, _RESERVED.grant = reserved_pass(), None
    if f is not None:
        f.close()


def full_startable(grant, cfg: dict | None, project_name: str | None, holder: str,
                   priority: int | None = None, since: float | None = None, cost: dict | None = None) -> bool:
    """A full run's startable gate for checkcap.acquire (cli._checks_slot_take): pool_room(full=True),
    its reason (pool_gate) recorded on the lanes `grant` holds (gate_lanes). Once it passes under the load
    guard the run takes its load pass at once (reserve_load_pass), so one parked run leaves the park per
    pass; held off again, it lets the pass go."""
    gate = "" if pool_room(cfg, project_name, full=True, priority=priority, since=since, cost=cost) \
        else pool_gate(cfg, project_name, full=True, priority=priority, since=since, cost=cost)
    if gate:
        release_load_pass()
    else:
        gate = reserve_load_pass(holder, priority, since=since)
    return gate_lanes(grant, gate)


def push_load_wait_s(cfg: dict | None = None) -> float | None:
    """The longest push checks (acquire's `push_key`) wait under load_guard: checks.push_load_wait_s (the
    project's, then TTP_CHECKS_PUSH_LOAD_WAIT_S, then the account's), default PUSH_LOAD_WAIT_S. Past it they
    start on their branch's reserved lane alone, one push check per lane at a time. None ("off" or false):
    they wait while the load stays high, as other runs do. A value that is no number of at least 0 falls
    back to the next source."""
    for v in (_section(cfg, "checks").get(PUSH_LOAD_WAIT_KEY), os.environ.get(PUSH_LOAD_WAIT_ENV),
              (project.load_account_settings().get("checks") or {}).get(PUSH_LOAD_WAIT_KEY)):
        if v is False or str(v).strip().lower() in ("off", "false"):
            return None
        if v in (None, "") or isinstance(v, bool):
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if n >= 0 and n != float("inf"):
            return n
    return PUSH_LOAD_WAIT_S


def project_max(cfg: dict | None = None, s: tuple[int, int] | None = None, kind: Kind = TESTS,
                project_name: str | None = None) -> int:
    """The most slots one project holds at once over all its runs (above): tests, its checks at once
    (checklane.project_cap) times one run's grant; builds, twice a run's share. An explicit slot budget
    (checks.test_project_slots, checks.build_project_max) wins, a project's own one only lowering the
    account's; never below one run's grant, never above the pool. Tests: never above project_workers()
    either (`project_name`'s, its own held slots' load not counted), which then also bounds one run's
    grant (acquire). `s` is sizes(cfg)."""
    from . import checklane
    s = s or sizes(cfg, kind=kind) or (0, 0)
    if not s[0]:
        return 0
    env, key = kind.setting(kind.budget)
    mine = _number(_section(cfg, "checks").get(key))
    top = _setting(env, key) or (mine if kind is TESTS else None)
    top = top or (min(s[0], 2 * s[1]) if kind is BUILDS else max(1, checklane.project_cap(cfg)) * s[1])
    if mine:
        top = min(top, mine)
    top = max(s[1], min(top, s[0]))
    if kind is TESTS and (w := project_workers(cfg, project_name=project_name)):
        top = min(top, w)
    return top


def project_max_from(cfg: dict | None = None, s: tuple[int, int] | None = None,
                     project_name: str | None = None) -> str:
    """Which setting sets project_max() for tests, as project_max applies them: "workers" (project_workers:
    the host's per-project ceiling), "pool" (the pool, checks.test_pool), "project" (its own
    checks.test_project_slots), "env" (TTP_TEST_PROJECT_SLOTS), "account" (the account's, which a project's
    own one only lowers), "run" (one run's grant, the floor) or "" (none set: the project cap's default)."""
    s = s or sizes(cfg) or (0, 0)
    if not s[0]:
        return ""
    env, key = TESTS.setting(TESTS.budget)
    mine = _number(_section(cfg, "checks").get(key))
    upper = _number(os.environ.get(env)) if os.environ.get(env) not in (None, "") else None
    acct = _number((project.load_account_settings().get("checks") or {}).get(key))
    top = upper or acct or mine
    src = "env" if upper else "account" if acct else "project" if mine else ""
    if mine and top and mine < top:
        top, src = mine, "project"
    if not top:
        from . import checklane
        top = max(1, checklane.project_cap(cfg)) * s[1]
    w = project_workers(cfg, project_name=project_name)
    bounded = max(s[1], min(top, s[0]))
    if w and w < bounded:
        return "workers"
    if top < s[1]:
        return "run"
    return "pool" if s[0] < top else src


def own_paths(project_name: str | None, n: int, kind: Kind = TESTS) -> list:
    """One project's budget slots; none for no project (its runs only count against the pool)."""
    return (locks.slot_paths(checkcap.slot_dir(), f"{kind.own}.{locks.group_id(project_name)}", n)
            if project_name else [])


def urgent_paths(project_name: str | None, n: int, kind: Kind = TESTS) -> list:
    """One project's urgent slots beyond its budget: one run's grant (`n`), for a priority-1 or better waiter
    (an owed priority-0 run too) whose project's budget is held only by runs less urgent than it (acquire)."""
    return (locks.slot_paths(checkcap.slot_dir(), f"{kind.own}.{locks.group_id(project_name)}-x", n)
            if project_name else [])


def _rank(rec: dict) -> float:
    """A slot holder's task priority from its label; NO_PRIORITY without one (no task, an older runtime)."""
    v = rec.get("priority")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else NO_PRIORITY


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
    """A worker's environment: tests and builds it runs directly in its shell start at most one run's share.
    SLOTS_ENV is the most test workers one of its pytest runs may ask for; a run takes them from the host's
    pool through `ttp clip` or `ttp sandbox` (direct), so a bare `pytest -n auto` starts one (XDIST_ENV 1)."""
    s, b = sizes(cfg), sizes(cfg, kind=BUILDS)
    k = min(s[1], project_max(cfg, s)) if s else 0
    return {**({XDIST_ENV: "1", SLOTS_ENV: str(k)} if s else {}), **(build_env(b[1]) if b else {})}


DIRECT_WAIT_S = 240.0   # a worker's own pytest waits this long for a host test slot, then runs on one worker


def asked(cmd: str, share: int) -> int | None:
    """The most test workers a pytest run in shell line `cmd` starts: its explicit -n N / --numprocesses N,
    `share` for -n auto or logical, 1 with none (or -n 0); None when `cmd` runs no pytest."""
    most = None
    for seg in _segments(cmd):
        words = [w for *_, w in seg]
        if (tool := next((i for i, w in enumerate(words) if _is_tool(words, i, False)), None)) is None:
            continue
        n = 1
        for i in range(tool + 1, len(words)):
            if words[i] == "--":
                break
            if not (m := re.fullmatch(r"(-n|--numprocesses)(=?)(.*)", words[i])):
                continue
            v = m.group(3) or (words[i + 1] if not m.group(2) and i + 1 < len(words) else "")
            n = int(v) if v.isdigit() else share if v in ("auto", "logical") else n
        most = max(most or 0, n, 1)
    return most


def direct(cmd: str, holder: str, tell: Callable[[str], None]) -> Grant | None:
    """A worker's own pytest run (`ttp clip`, `ttp sandbox`) takes its test workers from the host's pool,
    as a `ttp checks` run does: in a worker run (SLOTS_ENV, its share) not inside a run that holds slots,
    for a pytest asking more than one worker (asked), up to that many, at most the share. With too few
    free it starts with fewer; with none it waits (in line, under the load guard and its project's budget,
    as acquire) up to DIRECT_WAIT_S, then runs on one worker without a slot. The caller lowers -n to
    grant.k (clamp) and runs the command with grant.env() and the grant's files (inheritable here), so
    the test process holds the slots and its end or death frees them. None: nothing to take."""
    share = _number(os.environ.get(SLOTS_ENV))
    if not share or os.environ.get(HELD_ENV) or os.environ.get(checkcap.HELD_ENV):
        return None
    if (n := asked(cmd, share) or 0) <= 1:
        return None
    grant = acquire(holder, tell, DIRECT_WAIT_S, most=min(n, share))
    if grant is None:
        tell(f"no host test slot free after {DIRECT_WAIT_S:.0f} s: running on 1 test worker")
        return Grant(1)
    if not grant.k:   # the pool is off
        return None
    for f in grant.files:
        os.set_inheritable(f.fileno(), True)
    return grant


def paths(pool: int, kind: Kind = TESTS):
    return locks.slot_paths(checkcap.slot_dir(), kind.resource, pool)


class Grant:
    """k held slots (none with the pool off: `k` 0); `close()` frees them."""

    def __init__(self, k: int = 0, files=(), waited: float = 0.0, kind: Kind = TESTS):
        self.k, self.files, self.waited, self.kind = k, list(files), waited, kind
        self.extra: dict[str, str] = {}   # more variables for the commands, e.g. the run's ports (port_block)

    def fds(self) -> tuple[int, ...]:
        """The held slot files' descriptors, for a command that should hold them for its life (pass_fds)."""
        return tuple(f.fileno() for f in self.files)

    def env(self) -> dict[str, str]:
        if not self.k:
            return dict(self.extra)
        if self.kind is BUILDS:
            return {**build_env(self.k), BUILD_HELD_ENV: str(self.k), **self.extra}
        return {XDIST_ENV: str(self.k), SLOTS_ENV: str(self.k), HELD_ENV: str(self.k), **self.extra}

    def close(self) -> None:
        guard = _grant_guard(self.kind) if self.files else None
        try:
            for f in self.files:
                f.close()
            self.files = []
        finally:
            guard and guard.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _grant_guard(kind: Kind):
    """The held guard (an open file; closing it lets go) serializing a grant's release (Grant.close)
    with a waiter's count-and-take (acquire), so no waiter sees a grant half released and takes part of
    it while a later one takes the rest; None where the slot directory cannot be written."""
    try:
        g = open(checkcap.slot_dir() / f"{kind.resource}.grant.guard", "a+")
    except OSError:
        return None
    fcntl.flock(g, fcntl.LOCK_EX)
    return g


def _free(ps: list) -> int:
    """How many of the slots `ps` are free right now."""
    return len(ps) - len(locks._slot_labels(ps))


def _own_free(project_name: str | None, own: list, kind: Kind = TESTS) -> int:
    """How many of a project's budget slots `own` it may still take: their number less every slot of its
    budget held, those past a budget that shrank (project_workers) included."""
    span = own_paths(project_name, max(len(own), checkcap.cores()), kind)
    return max(0, len(own) - _held(span))


def _held(paths: list) -> int:
    """How many of `paths` are held now; while a preempt weighs a run (checkcap.FREEING), less those that
    run holds, as if it had stopped (checkcap.preempt's `startable`)."""
    if not (owner := checkcap.FREEING.get()):
        return len(locks._slot_labels(paths))
    n = 0
    for path in paths:
        if locks._slot_labels([path]):
            try:
                n += not checkcap.same_owner(locks.read_label(path), owner)
            except (OSError, ValueError):
                n += 1
    return n


def _take(ps: list, own: list, top: int, holder: str, what: str = "test slots",
          extra: dict | None = None) -> tuple[int, list]:
    """(k, files): up to `top` pool slots, each paired with one of the project's (`own`, when it has a budget)."""
    k, files = 0, []
    while k < top:
        o = None
        if own and (o := locks.try_take(own, holder, what, extra=extra)) is None:
            break
        if (f := locks.try_take(ps, holder, what, extra=extra)) is None:
            if o is not None:
                o.close()
            break
        k, files = k + 1, files + [f] + ([o] if o is not None else [])
    return k, files


def _pass_told(what: str, passed, load: str) -> str:
    return (f"{what}: past the load guard on its "
            + {LOAD_OWED: "pass for runs owed to the user", LOAD_URGENT: "pass for urgent runs",
               LOAD_PASS: "pass for an urgent or push-queue run"}
            .get(Path(passed.name).stem, "floor pass, the one run it admits so the queue moves") + f"; {load}")


@project.ps_snapshot()
def acquire(holder: str, tell: Callable[[str], None] | None = None, wait_s: float | None = None,
            cfg: dict | None = None, project_name: str | None = None, push_key: str = "",
            kind: Kind = TESTS, priority: int | None = None, stop: Callable[[], str] | None = None,
            since: float | None = None, full: bool = False, waits: Callable[[str], None] | None = None,
            most: int | None = None) -> Grant | None:
    """Take this run's test slots (above); waits while none is free, urgent runs of any project first
    (locks.urgent_order: priority 1 or better, by priority then arrival), the rest first-come, telling why every
    TELL_S. None only once `wait_s` passed with none free (a foreground caller hands over). Nested in a
    run that holds slots, its grant at once; with the pool off, Grant() (no limit, nothing set).
    `project_name` (default TTP_PROJECT's) is whose budget it counts against; `push_key` (push.priority_key)
    marks push checks, which get one run's grant with the branch's reserved lane when the pool or the
    project's budget has less free. `kind` BUILDS
    takes build slots instead; a build nested in a check uses the check's test grant. `priority` (the
    task's) orders its project's waiters, most urgent first, then arrival (suite_priority: a waiter ages
    one step per age_s(cfg), checks.priority_age_s); a priority-1 waiter whose project's budget is held
    only by less urgent runs takes one extra run's grant beyond it (urgent_paths) from the pool's free
    slots, so it never waits behind a long, less urgent check and stops none. `stop` as for suite_hold
    (a reason ends the wait with checkcap.Cancelled); `since`: the arrival to queue at (default now),
    kept by a run that hands over or re-executes itself (checkcap.Upgraded). `full` (a full `ttp checks`
    run): it starts only once run_min() slots are free (above). While other runs of its project wait, one
    run takes at most its share of the project's budget (run_share), so up to checklane.project_cap suites
    run side by side. A run that is no nested one takes no test slots while the host is overloaded
    (load_guard), except one run per load pass at a time (load_pass_paths: a priority-0 run's, an urgent or
    push run's, the floor's; it holds the pass with its grant): it waits, telling why and passing the reason
    to `waits` (its lanes' wait note, lane_waits), and `waits` gets "host test slots" again once the load drops. A push check's wait there is bounded
    (push_load_wait_s): past it, it starts on its branch's reserved lane alone (with checks.priority_beyond_cap
    off: on the pool's free slots, past the guard). `most`: a run's most slots below run_max (a worker's own
    pytest -n N, direct)."""
    tell = tell or (lambda _line: None)
    what = f"{kind.name} slots"
    for env in (kind.held_env, HELD_ENV):   # HELD_ENV: a build in a check builds on the check's slots
        if (held := _number(os.environ.get(env))):
            return Grant(held, kind=kind)
    cfg = _cfg() if cfg is None else cfg
    name = _project_name() if project_name is None else project_name
    start, told, ticket, mine, guard = time.time(), 0.0, None, None, None
    arrival = start if since is None else since
    guarded, passed = "", (_RESERVED.__dict__.pop("grant", None) if kind is TESTS else None)
    # Push and urgent checks draw on the pool (no reserved lane beyond it) unless checks.priority_beyond_cap.
    in_pool = bool(push_key) and kind is TESTS and not checkcap.priority_beyond_cap()
    if passed is not None and (load := load_guard()):   # taken at its gate (reserve_load_pass)
        tell(_pass_told(what, passed, load))
    try:
        while True:
            project.ps_cycle()
            if kind is TESTS and passed is None and (load := load_guard()) \
                    and load_passes(priority, bool(push_key), arrival):
                # Over the load guard one run per pass starts (load_pass_paths); its pass goes with its grant.
                if (passed := locks.try_take(load_pass_paths(priority, bool(push_key), arrival), holder,
                                             "load-guard pass",
                                             extra={"priority": priority} if priority is not None else None)):
                    tell(_pass_told(what, passed, load))
            if kind is TESTS and passed is None and (load := load_guard()) and not (
                    in_pool and (bound := push_load_wait_s(cfg)) is not None and time.time() - start >= bound):
                if push_key and not in_pool and (bound := push_load_wait_s(cfg)) is not None \
                        and time.time() - start >= bound \
                        and (f := locks.try_take([reserved_path(push_key)], holder, "push test slots",
                                                 extra={"priority": priority} if priority is not None else None)):
                    # Past the bound a push check starts anyway, on its branch's reserved lane alone (one
                    # run's grant, one push check per lane at a time), taking none of the pool's slots.
                    s = sizes(cfg, kind=kind)
                    k = s[1] if s else 0
                    tell(f"{what}: started after {time.time() - start:.0f} s under the load guard "
                         f"(checks.{PUSH_LOAD_WAIT_KEY} {bound:g} s) on its push branch's reserved lane alone"
                         + (f": {k} granted, tests run with {XDIST_ENV}={k}" if k else "") + f"; {load}")
                    if guarded and waits:
                        waits(SLOT_WAITS)
                    return Grant(k, [f], time.time() - start, kind)
                if not guarded and waits:
                    waits(f"{LOAD_WAITS}{load}")
                if not guarded or time.time() - told >= TELL_S:
                    told = time.time()
                    tell(f"waiting to start: {load}")
                guarded = load
                if stop and (why := stop()):
                    raise checkcap.Cancelled(why)
                if wait_s is not None and time.time() - start >= wait_s:
                    return None
                time.sleep(poll_s(POLL_S))
                continue
            if guarded:
                guarded, told = "", 0.0
                if waits:
                    waits(SLOT_WAITS)
            s = sizes(cfg, kind=kind)
            if s is None:
                return Grant(kind=kind)
            pool, top = s
            top = max(1, min(top, most)) if most else top
            guard = _grant_guard(kind)   # let go once it took its slots or found none (below)
            # Slots in use over every pool file there may be (a pool that shrank leaves holders past its end).
            used = len(locks._slot_labels(paths(max(pool, checkcap.cores()), kind)))
            pool = pool_now(cfg, pool, used, kind=kind)
            room = free = max(0, min(top, pool - used))   # never more than the pool has left
            if kind is TESTS and room and (live := live_room(paths(max(pool, checkcap.cores()), kind))) is not None \
                    and live < room:
                room, sized = live, f", sized to the host's free cores (at least {WORKERS_FLOOR})"
            else:
                sized = ""
            ps, own = paths(pool, kind), ([] if in_pool else
                                          own_paths(name, project_max(cfg, (pool, s[1]), kind, name), kind))
            own_free = _own_free(name, own, kind) if own else 0
            if own:   # a project's test-worker ceiling (project_workers) also bounds one run's grant
                top, room = min(top, len(own)), min(room, len(own))
                if kind is TESTS and not push_key:   # its share while others of its project wait (run_share)
                    room = min(room, run_share(cfg, name, len(own), top, (mine, ticket)))
            # A full run's fewest slots, at most what the pool and its budget can ever give (no deadlock).
            want = min(run_min(cfg, top), pool) if full and kind is TESTS and not push_key else 1
            need = 1
            k, files, why = 0, [], f"all {pool} are held"
            try:
                ahead, extra = [], ({"priority": priority} if priority is not None else None)
                if own and not push_key:
                    # Its place in its project's line (most urgent first, then arrival), kept while it is
                    # out of the host's line; an earlier waiter there goes first.
                    line_of = f"{kind.own}.{locks.group_id(name)}"
                    if mine is None:
                        mine = locks.enqueue(checkcap.slot_dir(), line_of, holder, arrival, priority=priority)
                    step = age_s(cfg)
                    line = sorted(locks.queued(checkcap.slot_dir(), line_of), key=lambda t: _order_key(t, age=step))
                    ahead = line[:line.index(mine)] if mine in line else line
                    if (not ahead and own_free < min(want, len(own)) and priority is not None and priority <= 1
                            and (recs := locks.holder_records(own))
                            and all(_rank(r) > priority for r in recs)
                            and locks.any_free(urgent := urgent_paths(name, top, kind))):
                        own, own_free = urgent, _free(urgent)
                        what = f"{kind.name} slots (urgent: its project's budget is held by less urgent runs)"
                if own:   # never more than its budget has left, holders past a budget that shrank counted
                    room = min(room, own_free)
                short = False   # its project's budget has fewer free than a full run's fewest slots
                if want > 1:
                    need = min(want, len(own)) if own else want
                    if own:
                        mine_free = own_free
                        free, short = min(free, mine_free), mine_free < need
                if own and not push_key and (short or not own_free):
                    # At its project's budget: out of the host's line so others go first, keeping its
                    # place in its project's line.
                    locks.dequeue(ticket)
                    ticket, why = None, (f"its project holds its {len(own)} (checks.{kind.setting(kind.budget)[1]}"
                                         + (", or checks.test_project_max x one run's grant, within"
                                            " checks.test_project_workers" if kind is TESTS else "")
                                         + ")" + (f"; a full run starts with at least {need} free (checks.{RUN_MIN_KEY})"
                                                  if short else ""))
                elif ahead:
                    # An earlier waiter of its project goes back into the host's line first.
                    locks.dequeue(ticket)
                    ticket, why = None, f"{len(ahead)} earlier waiter(s) of its project go first"
                elif in_pool:
                    # From the pool, ahead of the host's line: while none is free it holds the line's first
                    # place (an owed run's priority), so the slots that free next are its.
                    k, files = _take(ps, own, room, holder, what, extra)
                    if not k and ticket is None:
                        ticket = locks.enqueue(checkcap.slot_dir(), kind.resource, holder, arrival, group=name,
                                               priority=checkcap.OBLIGATION_PRIORITY)
                    why = f"all {pool} are held; first in the host's line as push or urgent checks"
                elif push_key and kind is TESTS and others_wait(name) and \
                        (f := locks.try_take([reserved_path(push_key)], holder, "push test slots", extra=extra)):
                    # Others wait for the slots it would take: its branch's reserved lane alone (one run's grant).
                    k, files, what = top, [f], f"{what} (its push branch's reserved lane alone: other runs wait for test slots)"
                elif push_key:
                    k, files = _take(ps, own, room, holder, what, extra)
                    if k < top and (f := locks.try_take([reserved_path(push_key)], holder, "push test slots", extra=extra)):
                        # Short of one run's grant (the pool or its project's budget held by task checks):
                        # the branch's reserved lane makes up the rest, one push check per branch at a time.
                        k, files, what = top, files + [f], f"{what} (with its push branch's reserved lane)"
                else:
                    if ticket is None:
                        ticket = locks.enqueue(checkcap.slot_dir(), kind.resource, holder, arrival, group=name,
                                               priority=priority)
                    q = locks.urgent_order(locks.queued(checkcap.slot_dir(), kind.resource))
                    if q and q[0] == ticket and free < need:
                        # First in line, it keeps its place until a full run's fewest slots are free.
                        why = (f"a full run starts with at least {need} free (checks.{RUN_MIN_KEY}); "
                               f"{free} free now")
                    elif q and q[0] == ticket:
                        k, files = _take(ps, own, max(room, need), holder, what, extra)
                        if k < need:   # another holder took some in the same instant: wait for them again
                            for f in files:
                                f.close()
                            k, files, why = 0, [], f"a full run starts with at least {need} free (checks.{RUN_MIN_KEY})"
                    elif q:
                        why = f"{q.index(ticket) if ticket in q else len(q)} ahead in the host's line"
            except OSError as e:
                for f in files:
                    f.close()
                tell(f"{what} unavailable ({e.strerror or e}): running with 1 {kind.unit}")
                return Grant(1, waited=time.time() - start, kind=kind)
            guard = guard and guard.close()
            waited = time.time() - start
            if k:
                if passed is not None:
                    files, passed = files + [passed], None
                tell(f"{what}: {k} granted of a host pool of {pool} (at most {top} per run"
                     + (f", {len(own)} per project" if own else "") + f") after {waited:.0f} s waiting; "
                     + (f"builds run with MAKEFLAGS=-j{k}" if kind is BUILDS else f"tests run with {XDIST_ENV}={k}")
                     + sized)
                return Grant(k, files, waited, kind)
            if passed is not None:   # no grant yet: the pass is free for another run until its next try
                passed = passed.close()
            if stop and (why := stop()):
                raise checkcap.Cancelled(why)
            # Told before giving up too: a caller whose wait ends on this try (a slow one, or wait_s 0)
            # still learns why it got no slots.
            if not told or time.time() - told >= TELL_S:
                told = time.time()
                tell(f"waiting for a host-wide {kind.name} slot: {why} ({checkcap._some(locks.held_labels(ps))})")
            if wait_s is not None and waited >= wait_s:
                return None
            time.sleep(poll_s(POLL_S))
    finally:
        guard and guard.close()
        passed and passed.close()
        locks.dequeue(ticket)
        locks.dequeue(mine)


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


def _common_dir(top) -> str:
    """The real path of the common git dir of the repository at `top`; "" when `top` is not in one."""
    if not top:
        return ""
    try:
        args = ("rev-parse", "--git-common-dir")
        r = repoid.read(top, args, lambda: subprocess.run(["git", "-C", str(top), *args], capture_output=True,
                                                          text=True, timeout=10))
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode or not r.stdout.strip():
        return ""
    common = Path(r.stdout.strip())
    return os.path.realpath(common if common.is_absolute() else Path(top) / common)


def repo_key(top) -> str:
    """The short id of the git repository at `top` (its common git dir, so all its worktrees share it);
    "" when `top` is not in one."""
    common = _common_dir(top)
    return hashlib.sha256(common.encode()).hexdigest()[:12] if common else ""


def repo_name(top) -> str:
    """The directory name of the git repository at `top` (its main checkout's, or a bare repository's
    without .git, so all its worktrees share it), for its checks lane's label; "" when not in one."""
    common = Path(_common_dir(top) or ".")
    return (common.parent.name if common.name == ".git" else common.name.removesuffix(".git")) if str(common) != "." else ""


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


def suite_priority(ticket, now: float | None = None, age: float | None = None,
                   arrival: float | None = None) -> float:
    """A suite waiter's rank now: its task priority (NO_PRIORITY without one), one step more urgent
    per `age` seconds it has waited (default age_s()) since `arrival` (default its ticket's), never past
    AGE_FLOOR (p2) nor its own priority."""
    prio = locks.ticket_priority(ticket)
    prio = NO_PRIORITY if prio == float("inf") else prio
    age = age_s() if age is None else age
    arrival = locks._arrival(ticket) if arrival is None else arrival
    waited = max(0.0, (time.time() if now is None else now) - arrival)
    return max(min(prio, AGE_FLOOR), prio - waited // age) if age > 0 else prio


def holders_text(recs: list, now: float, full: int = 3) -> str:
    """Every holder for a waiter's line: the first `full` in full (checklane.holder_text), the rest
    by name only ("+2 more: #7, #9"), so no holder is hidden past the project's cap."""
    from . import checklane
    text = "; ".join(checklane.holder_text(r, now) for r in recs[:full]) or "a run that just ended"
    rest = [f"#{r['task']}" if r.get("task") else str(r.get("holder") or "a run") for r in recs[full:]]
    return text + (f"; +{len(rest)} more: {', '.join(rest)}" if rest else "")


def rank_text(ticket, now: float | None = None, age: float | None = None) -> str:
    """A waiter's task priority and its rank now (suite_priority) for a wait line, e.g. "p4 ranked p2
    (aged)" or "p3 (no task priority) ranked p3"."""
    prio = locks.ticket_priority(ticket)
    rank = suite_priority(ticket, now, age)
    if prio == float("inf"):
        return f"p{NO_PRIORITY} (no task priority) ranked p{rank:g}" + (" (aged)" if rank < NO_PRIORITY else "")
    return f"p{prio:g} ranked p{rank:g}" + (" (aged)" if rank < prio else "")


def _order_key(t, now: float | None = None, age: float | None = None, arrival: float | None = None) -> tuple:
    """Rank (suite_priority), then native priority, then arrival: among equal ranks a native p2 goes
    before a waiter that only aged up to p2. One that has waited FAIR_STEPS aging steps goes by arrival
    among its rank (0 sorts it first: it arrived before all who have not), so native p2s never starve it.
    `arrival` overrides the ticket's own (a re-queued waiter keeps its place, waiter_sets)."""
    now = time.time() if now is None else now
    age = age_s() if age is None else age
    arrival = locks._arrival(t) if arrival is None else arrival
    prio = locks.ticket_priority(t)
    native = NO_PRIORITY if prio == float("inf") else prio
    if age > 0 and now - arrival >= FAIR_STEPS * age:
        native = 0
    return suite_priority(t, now, age, arrival), native, arrival, t.name


_CHECK = re.compile(r"\[check ([^\]\s]+)\]")
_PIDNS = re.compile(r"\[pidns (\S+)\]")   # a namespace reads "pid:[4026531836]"


def _ticket_text(t) -> str:
    try:
        return t.read_text()
    except OSError:
        return ""


def ticket_pid(t) -> int | None:
    m = re.match(r"\d+(?:\.\d+)?-(\d+)", t.name)
    return int(m.group(1)) if m else None


def _run_no(text: str) -> int:
    m = re.search(r"\brun (\d+)", text)
    return int(m.group(1)) if m else 0


def waiter_sets(q: list) -> tuple[list, dict, dict, list]:
    """Suite waiter tickets `q` sorted out: (kept, arrival, superseded, dead). One entry per task and
    check (its head and commands, the ticket's "[check ...]"): of several, the latest run's is kept and
    queues at the earliest arrival among them; each older run's is superseded (ticket -> the kept one), as
    a task's next attempt starting the same checks replaces its earlier attempt's. Two of the same run
    count once. A ticket whose pid is gone, judged only in the pid namespace it names ("[pidns ...]"), is
    dead. Read only."""
    ns = locks._pid_ns()
    kept, dead, groups = [], [], {}
    for t in q:
        text = _ticket_text(t)
        pid, tns = ticket_pid(t), _PIDNS.search(text)
        if pid and tns and ns and tns.group(1) == ns and not locks._alive(pid):
            dead.append(t)
            continue
        task, check = re.search(r"task #(\d+)", text), _CHECK.search(text)
        if task and check:
            groups.setdefault((task.group(1), check.group(1)), []).append((_run_no(text), locks._arrival(t), t))
        else:
            kept.append(t)
    arrival = {t: locks._arrival(t) for t in kept}
    superseded = {}
    for g in groups.values():
        g.sort(key=lambda x: (x[0], -x[1], x[2].name))
        keep = g[-1][2]
        kept.append(keep)
        arrival[keep] = min(x[1] for x in g)
        superseded.update({x[2]: keep for x in g if x[0] < g[-1][0]})
    return kept, arrival, superseded, dead


def _ordered(kept: list, arrival: dict, now: float | None = None, age: float | None = None) -> list:
    return sorted(kept, key=lambda t: _order_key(t, now, age, arrival[t]))


def suite_order(project_name: str, q: list | None = None, now: float | None = None, repo: str = "",
                age: float | None = None) -> list:
    """The project's suite waiters in repository `repo` in the order the lanes go to them: by
    suite_priority (aging step `age`, default age_s()), then arrival, one entry per task and check, dead
    ones left out (waiter_sets)."""
    q = locks.queued(checkcap.slot_dir(), _suite_resource(project_name, repo)) if q is None else q
    age = age_s() if age is None else age
    kept, arrival, _, _ = waiter_sets(q)
    return _ordered(kept, arrival, now, age)


def _pending(path) -> bool:
    """Whether a held lane's run still waits for its test slots (suite_hold's `pending`)."""
    try:
        return json.loads(path.read_text() or "{}").get("granted") is False
    except (OSError, ValueError, AttributeError):
        return False


def project_held(project_name: str) -> list:
    """The lane files of every repository and wrapper lock of the project held right now by a run that
    has its test slots: a run still waiting for them does not count against the project's cap."""
    return [x for x in lane_paths(project_name, repo=None) if locks._slot_labels([x]) and not _pending(x)]


def _relabel_lanes(grant, fields: dict) -> None:
    for f in getattr(grant, "files", None) or []:
        if os.path.basename(getattr(f, "name", "")).startswith(SUITE + "."):
            locks.relabel(f, fields)


def slots_granted(grant) -> None:
    """Mark the lanes `grant` (suite_hold's, taken `pending`) holds as running with their test slots,
    since now ("granted_at") and no longer waiting for anything (lane_waits)."""
    _relabel_lanes(grant, {"granted": True, "granted_at": time.time(), "waits": "", "gate": ""})


SLOT_WAITS = "host test slots"   # a lane's waits while its run, past its host-wide checks slot, waits for test slots
LOAD_WAITS = "host load: "   # its prefix while that wait is under the load guard (acquire)
PARKED_WAITS = "host test slots (project budget or pool full)"   # a lane's waits while checkcap.acquire parks it


def gate_lanes(grant, gate: str) -> bool:
    """Record `gate` (pool_gate's reason, "" for none) on the lanes `grant` holds when it changed, so a
    parked run's status names what holds it (checklane._parked); True when nothing holds it."""
    if getattr(grant, "gate", None) != gate:
        grant.gate = gate
        _relabel_lanes(grant, {"gate": gate})
    return not gate


def lane_waits(grant, what: str) -> None:
    """Mark the lanes `grant` holds as waiting for `what` (a host-wide checks slot, host test slots)
    before they run: status says so (checklane.holder_text) and such a holder is no likely hang."""
    _relabel_lanes(grant, {"waits": what})


def admitted_ahead(project_name: str | None, priority: int | None = None) -> bool:
    """Whether a run of the project at least as urgent as `priority` already holds a host-wide checks slot
    and still waits for its test slots: its lane says so (SLOT_WAITS, or LOAD_WAITS under the load guard)
    or its ticket is in the project's line (acquire). It goes first, so pool_room(full=True) gives no other
    run of the project a checks slot only to queue behind it: such runs filled the host-wide slots."""
    if not project_name:
        return False
    mine = NO_PRIORITY if priority is None else priority
    for r in locks.holder_records(lane_paths(project_name, repo=None)):
        w = str(r.get("waits") or "")
        if r.get("granted") is False and _rank(r) <= mine and (w == SLOT_WAITS or w.startswith(LOAD_WAITS)):
            return True
    return any(min(locks.ticket_priority(t), NO_PRIORITY) <= mine
               for t in locks.queued(checkcap.slot_dir(), f"{TESTS.own}.{locks.group_id(project_name)}"))


def project_waits(project_name: str | None, own=()) -> bool:
    """Whether another run of `project_name` waits now: a ticket of its project in the host's checks queue,
    parked for its test slots (checkcap.PARKED), in the host's test-slot line or in its project's line,
    except the asking run's own tickets (`own`)."""
    if not project_name:
        return False
    d, gid, mine = checkcap.slot_dir(), locks.group_id(project_name), {t for t in own if t is not None}
    try:
        grouped = [t for res in (checkcap.RESOURCE, checkcap.PARKED, TESTS.resource) for t in locks.queued(d, res)
                   if locks.ticket_group(t) == gid]
        line = locks.queued(d, f"{TESTS.own}.{gid}")
    except OSError:
        return False
    return any(t not in mine for t in grouped + line)


def run_share(cfg: dict | None, project_name: str | None, budget: int, top: int, own=()) -> int:
    """The most test slots one run of `project_name` takes of its project's `budget` slots: all of them
    alone, else, while another of its runs waits (project_waits), max(run_min, budget // its checks at once
    (checklane.project_cap)), so a second suite starts beside the first instead of waiting for it all."""
    from . import checklane
    if budget <= 0 or not project_waits(project_name, own):
        return budget
    return max(run_min(cfg, top), budget // max(1, checklane.project_cap(cfg)))


def others_wait(project_name: str | None) -> bool:
    """Whether a run waits for test slots now: a checks run parked for them (checkcap.PARKED), or a waiter in
    the host's line or `project_name`'s line (acquire)."""
    d = checkcap.slot_dir()
    try:
        return bool(locks.queued(d, checkcap.PARKED) or locks.queued(d, TESTS.resource)
                    or (project_name and locks.queued(d, f"{TESTS.own}.{locks.group_id(project_name)}")))
    except OSError:
        return False


def pool_room(cfg: dict | None, project_name: str | None, full: bool = False, priority: int | None = None,
              since: float | None = None, cost: dict | None = None) -> bool:
    """Whether a run could take its test slots now: pool_gate finds nothing that holds it."""
    return not pool_gate(cfg, project_name, full, priority, since, cost)


def pool_gate(cfg: dict | None, project_name: str | None, full: bool = False, priority: int | None = None,
              since: float | None = None, cost: dict | None = None) -> str:
    """What holds a run off its test slots now, "" when it could take them (pool_room): the pool is off,
    or it has a slot free (pool_now) with nobody queued for one, and the project's budget (project_max) has
    one free. suite_hold's overflow lanes ask it, so a run takes one only once it can start beside the
    holders. With `full` (a full `ttp checks` run, as acquire's), also only while the host's load lets a
    suite start (load_held: one run per load pass passes) and the pool and the budget have run_min() free, so a
    run whose wait is bounded (checkspill.after_s) takes no host-wide checks slot only to hand it back at
    the bound. A priority-1 run (`priority`) whose project's budget is held only by less urgent runs counts
    its urgent slots instead (urgent_paths), as acquire takes them. A run of the project parked past
    checkcap.starve_s() that arrived before `since` (this run's arrival; default now) goes first
    (checkcap.starved_ahead), unless this run is more urgent and that one's turn (one per
    checkcap.aged_turns() urgent starts) has not come, or this run, of `cost`, is much cheaper than that one's;
    an owed or urgent run is never held by a less urgent one. A less urgent run leaves them to its project's
    parked owed (priority-0) runs whatever its wait (checkcap.owed_ahead). A run leaves `need` slots of the pool to each urgent
    run of another project parked for its test slots ahead of it (urgent_parked: a routine run to every one,
    an urgent run to those more urgent or as urgent and older), so a freed slot goes to the urgent one, not
    to whichever polls first; one its own gate holds otherwise keeps none. A routine run also leaves `need`
    to each urgent run of its own project parked for its test slots that could start, whatever its arrival
    (own_urgent_parked: OWN_RESERVE). The reason names the term that failed, for status (checklane._parked)."""
    if full and (why := load_held(priority, since=since)):
        return f"{LOAD_GATE}{why}"
    if (ahead := checkcap.starved_ahead(project_name, since, priority=priority, cost=cost)):
        return f"{len(ahead)}{STARVED_GATE}"
    if (owed := checkcap.owed_ahead(project_name, priority)):
        return f"{len(owed)}{OWED_GATE}"
    if full and admitted_ahead(project_name, priority):
        return "a run of its project as urgent or more holds a host-wide checks slot and waits for its test slots"
    s = sizes(cfg)
    if s is None:
        return ""
    used = _held(paths(max(s[0], checkcap.cores())))
    pool = pool_now(cfg, s[0], used)
    if used >= pool:
        return f"the host's test pool is full: {used} of {pool} held"
    if (q := locks.queued(checkcap.slot_dir(), TESTS.resource)):
        return f"{len(q)} waiter(s) ahead in the host's test-slot line"
    own = own_paths(project_name, project_max(cfg, (pool, s[1]), project_name=project_name))
    top = min(s[1], len(own)) if own else s[1]
    # As acquire counts a full run's fewest slots (at most what the pool and its budget can ever give),
    # holders past a budget that shrank counted (_own_free).
    need = min(min(run_min(cfg, top), pool), len(own) or pool) if full else 1
    own_free = _own_free(project_name, own) if own else 0
    if (own and priority is not None and priority <= 1 and own_free < need
            and (recs := locks.holder_records(own)) and all(_rank(r) > priority for r in recs)
            and locks.any_free(urgent := urgent_paths(project_name, top))):
        own_free = _free(urgent)
    if own and own_free < need:
        return (f"{BUDGET_GATE}{own_free} of {len(own)} free, a run needs {need}"
                f" (held by {checkcap._some(list(dict.fromkeys(locks.held_labels(own))))})")
    kept = need * urgent_parked(project_name, priority, since, budget=project_max(cfg, (pool, s[1])), need=need,
                                 run_max=s[1], room=pool - used)
    if min(s[1], pool - used - kept) < need:
        return (f"the host's test pool: {pool - used} of {pool} free, a run needs {need}"
                + (f", {kept} kept for {kept // need} urgent run(s) of other projects parked ahead" if kept else ""))
    room = min(own_free if own else pool, pool - used - kept)
    if (n := own_urgent_parked(project_name, priority, room // need)) and room - need * n < need:
        return f"{room} test slot(s) free, a run needs {need}, {need * n} {OWN_RESERVE} ({n})"
    return ""


def own_urgent_parked(project_name: str | None, priority: int | None, fits: int = 0) -> int:
    """How many urgent runs (locks.URGENT_PRIORITY or better) of `project_name` itself parked for their test
    slots now (checkcap.PARKED tickets) a routine run (an int `priority` past locks.URGENT_PRIORITY) leaves
    room to, whatever their arrival, so a freed slot of its project goes to them first. Only those that could
    start: not held by a run of the project parked longer (checkcap.starved_ahead for that ticket), and not
    one of the `fits` most urgent and oldest the free room fits that left it untaken UNTAKEN_S (_untaken):
    some other hold keeps it off them, so slots kept for it would sit idle. 0 for an urgent run or none."""
    if not project_name or not isinstance(priority, int) or priority <= locks.URGENT_PRIORITY:
        return 0
    gid = locks.group_id(project_name)
    try:
        q = locks.queued(checkcap.slot_dir(), checkcap.PARKED)
    except OSError:
        return 0
    mine = sorted((t for t in q if locks._urgent(t) and locks.ticket_group(t) == gid
                   and not checkcap.starved_ahead(project_name, ticket=t)),
                  key=lambda t: (locks.ticket_priority(t), locks._arrival(t)))
    stale = _untaken(mine[:max(0, fits)], seen=_OWN_ROOM_SINCE)
    return sum(1 for t in mine if str(t) not in stale)


def urgent_parked(project_name: str | None, priority: int | None = None, since: float | None = None,
                  budget: int = 0, need: int = 1, run_max: int = 0, room: int | None = None) -> int:
    """How many urgent runs (locks.URGENT_PRIORITY or better) of other projects than `project_name` are parked
    for their test slots now (checkcap.PARKED tickets) ahead of a run at `priority` that arrived at `since`
    (default now): every one for a routine run, else those more urgent, or as urgent and older. A run whose
    own gate (pool_gate's reason its lane records: gate_lanes) is its project's aged turn (STARVED_GATE) or owed runs (OWED_GATE), its
    project's budget (BUDGET_GATE) or the load guard (LOAD_GATE) is not counted: it could not take the slots
    kept for it, so they would sit idle. One whose lane names no reason, or only the pool, still counts.
    With a `budget` (the account's project_max), each project counts at most the runs of `need` slots its
    free budget could start now (its urgent slots too while only less urgent runs hold it, as pool_gate
    counts them): one whose budget is full keeps no pool slots. With the pool's free slots (`room`), one
    the free room fitted (most urgent and oldest first) for UNTAKEN_S keeps none either: some other hold
    keeps it off them (_untaken)."""
    gid, d = locks.group_id(project_name), checkcap.slot_dir()
    try:
        q = locks.queued(d, checkcap.PARKED)
    except OSError:
        return 0
    mine = (float(priority), time.time() if since is None else since) \
        if priority is not None and priority <= locks.URGENT_PRIORITY else None
    ahead = [t for t in q if locks._urgent(t) and locks.ticket_group(t) != gid
             and (mine is None or (locks.ticket_priority(t), locks._arrival(t)) < mine)]
    if not ahead:
        return 0
    held = {r.get("pid") for r in locks.holder_records(sorted(d.glob(f"{SUITE}.*.lock")))
            if (g := str(r.get("gate") or "")).startswith((LOAD_GATE, BUDGET_GATE)) or g.endswith((STARVED_GATE, OWED_GATE))}
    held.discard(None)
    ahead = sorted((t for t in ahead if ticket_pid(t) not in held),
                   key=lambda t: (locks.ticket_priority(t), locks._arrival(t)))
    stale = _untaken(ahead[:max(0, room) // max(1, need)]) if room is not None else set()
    groups = collections.Counter(locks.ticket_group(t) for t in ahead if str(t) not in stale)
    return sum(min(n, _group_free(d, g, budget, run_max) // max(1, need)) if budget and g else n
               for g, n in groups.items())


UNTAKEN_S = 300.0   # a parked urgent run that leaves free pool room untaken this long keeps no slots
_ROOM_SINCE: dict = {}   # _untaken(): each parked ticket this process saw beside free room, and since when
_OWN_ROOM_SINCE: dict = {}   # the same for own_urgent_parked's tickets, so neither caller resets the other's


def _untaken(tickets: list, now: float | None = None, seen: dict | None = None) -> set:
    """Of `tickets` (the parked runs the pool's free room fits now, most urgent and oldest first), those
    (paths, as str) this process has seen so for UNTAKEN_S or longer: a run that could start takes free
    room within a poll, so one that leaves it untaken is held by something else. Room taken or gone
    starts its count again. `seen`: the record to count in (default _ROOM_SINCE)."""
    now = time.time() if now is None else now
    seen = _ROOM_SINCE if seen is None else seen
    keep = {str(t) for t in tickets}
    for k in set(seen) - keep:
        del seen[k]
    return {k for k in keep if now - seen.setdefault(k, now) >= UNTAKEN_S}


def _group_free(d: Path, gid: str, budget: int, run_max: int) -> int:
    """How many test slots the project of group id `gid` could still take under the account's `budget`
    (project_max): the budget less every slot of its budget held, or while only less urgent runs hold
    them, its free urgent slots (urgent_paths, one run's grant: `run_max`)."""
    own = locks.slot_paths(d, f"{TESTS.own}.{gid}", max(budget, checkcap.cores()))
    held = locks.holder_records(own)
    free = max(0, budget - _held(own))
    if held and all(_rank(r) > 1 for r in held):
        free = max(free, _free(locks.slot_paths(d, f"{TESTS.own}.{gid}-x", min(budget, run_max or budget))))
    return free


@contextlib.contextmanager
def _cap_guard(project_name: str, cap: int):
    """Serialize the count-and-take of the project's lanes under its cap (none without one)."""
    if cap <= 0:
        yield
        return
    with open(checkcap.slot_dir() / f"{SUITE}.{locks.group_id(project_name)}.cap.guard", "a+") as g:
        fcntl.flock(g, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _lane_guard(base: str):
    """Serialize the recount-and-take of lane resource `base`'s numbered lanes (suite_hold)."""
    with open(checkcap.slot_dir() / f"{base}.lane.guard", "a+") as g:
        fcntl.flock(g, fcntl.LOCK_EX)
        yield


def _numbered_held(base: str, running: bool = False) -> int:
    """How many of `base`'s numbered lane files are held now, past any count too; with `running` only
    those whose run has its test slots (not _pending)."""
    return sum(1 for x in _lanes_of(base) if not x.name.startswith(base + "-x") and locks._slot_labels([x])
               and not (running and _pending(x)))


def claim_running(grant, project_name: str | None, slots: Callable[[int], int], cap: int = 0,
                  mark: bool = True) -> bool:
    """Mark the lanes `grant` (suite_hold's, taken `pending`) holds as running (slots_granted) once its run
    has its host grants, if that keeps every bound: fewer than `cap` (checklane.project_cap; 0: none) of
    the project's lanes running, unless its lane went past the cap ("over_cap"), and for a lane taken with
    `running`, fewer than `slots(n)` of its repository's numbered lanes running (n of them now). Counted
    and marked under the same guards suite_hold takes its lanes under, so two runs granted at once never
    pass a bound. False, with nothing marked, while a bound is full: the caller hands its grants back
    and waits for room (`mark` False: only says whether there is room now)."""
    lanes = [f for f in getattr(grant, "files", None) or []
             if os.path.basename(getattr(f, "name", "")).startswith(SUITE + ".")]
    recs = {f.name: (locks.holder_records([Path(f.name)]) or [{}])[0] for f in lanes}
    waiting = [f for f in lanes if recs[f.name].get("granted") is False]
    if not project_name or not waiting:
        mark and slots_granted(grant)
        return True
    with contextlib.ExitStack() as guards:
        guards.enter_context(_cap_guard(project_name, cap))
        for base in sorted({_LANE_FILE.sub("", os.path.basename(f.name)) for f in waiting}):
            guards.enter_context(_lane_guard(base))
        counted = {str(x) for x in lane_paths(project_name, repo=None)}   # a worktree lane is not (tree_key)
        if cap > 0 and any(f.name in counted and not recs[f.name].get("over_cap") for f in waiting) \
                and len(project_held(project_name)) >= cap:
            return False
        for f in waiting:
            base = _LANE_FILE.sub("", os.path.basename(f.name))
            if recs[f.name].get("running") and not base.endswith("-x"):
                n = _numbered_held(base, running=True)
                if n >= max(1, int(slots(n) or 1)):
                    return False
        mark and slots_granted(grant)
    return True


_LANE_FILE = re.compile(r"\.\d+\.lock$")


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
        n = 1 if key == "" or key.startswith("w") else max(1, int(slots or 1))   # "": the old one-slot base
        free = n - len([x for x in locks.slot_paths(checkcap.slot_dir(), base, n) if locks._slot_labels([x])])
        if free > 0:
            ready += suite_order(project_name, repo=key, now=now, age=age)[:free]
    if ticket not in ready:
        ready.append(ticket)   # one taking an overflow lane
    ready.sort(key=lambda t: _order_key(t, now, age))
    return ready.index(ticket) < room


def over_cap_room(project_name: str, cap: int) -> bool:
    """Whether a priority-1 waiter may take an overflow lane past the project's full cap of `cap`: the cap
    is full and no lane of the project (pending ones too) is already held past it ("over_cap")."""
    return (cap > 0 and len(project_held(project_name)) >= cap
            and not any(r.get("over_cap") and not r.get("landing")
                        for r in locks.holder_records(lane_paths(project_name, repo=None))))


def landing_room(project_name: str, kind: str = "mark") -> bool:
    """Whether a landing check of `kind` may take a landing lane past the project's lanes and cap. A run under
    a landing mark ("mark": a push batch's or `ttp push`'s checks) has its own lane: only another mark run
    holding one keeps it waiting, never the coordinator's task-less run. The coordinator's run ("coordinator")
    takes one only while no landing lane of the project is held. A lane taken by an older runtime ("landing":
    true) counts as a mark run's."""
    recs = locks.holder_records(lane_paths(project_name, repo=None)) if project_name else []
    held = [r.get("landing") for r in recs if r.get("landing")]
    return bool(project_name) and not (held if kind == "coordinator" else [k for k in held if k != "coordinator"])


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


LANE_ENV = "TTP_CHECKS_LANES"   # the lanes a re-executing `ttp checks` keeps: [[fd, path], ...] (keep_lanes)


def keep_lanes(grant) -> str:
    """The checks lanes `grant` (suite_hold's) holds, made to survive this process's exec: the open
    files become inheritable and the returned text (for LANE_ENV) names them for adopt_lanes. A lane's
    lock belongs to its open file and its label to this pid, which exec keeps, so the lane is never free."""
    kept = []
    for f in getattr(grant, "files", None) or []:
        try:
            os.set_inheritable(f.fileno(), True)
            kept.append([f.fileno(), f.name])
        except (OSError, ValueError):
            pass
    return json.dumps(kept) if kept else ""


def adopt_lanes(spec: str) -> Grant | None:
    """Grant(0) of the lanes keep_lanes() named in `spec`, taken over after the exec (heartbeat again,
    no longer inherited by the commands it starts); None when there are none or one is not the open,
    still locked lane file it names (those that are get closed: the run then queues for a lane again)."""
    try:
        kept = [(int(fd), str(path)) for fd, path in json.loads(spec or "[]")]
    except (ValueError, TypeError):
        return None
    files, ok = [], bool(kept)
    for fd, path in kept:
        try:
            st, own = os.fstat(fd), os.stat(path)
            if (st.st_dev, st.st_ino) != (own.st_dev, own.st_ino):
                ok = False
                continue
            f = os.fdopen(fd, "a+")
            files.append(f)
            f.buffer.raw.name = path   # named as try_take's are (suite_running, the heartbeat read it)
            os.set_inheritable(fd, False)
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # its own lock: taken again at once
        except (OSError, ValueError):
            ok = False
    if not ok:
        for f in files:
            f.close()
        return None
    for f in files:
        _heartbeat(f, _lane_beat())
    return Grant(0, files)


def _lane_beat() -> float:
    from . import checklane
    return checklane.HEARTBEAT_S


@project.ps_snapshot()
def suite_hold(project_name: str | None, tell: Callable[[str], None] | None = None,
               wait_s: float | None = None, waiting: Callable[[bool], None] | None = None,
               stop: Callable[[], str] | None = None, who: str = "", priority: int | None = None,
               since: float | None = None, slots: int | Callable[[int], int] = 1, repo: str = "", cost: dict | None = None,
               max_wait: float = 0.0, state=None, what: str = "full checks", lock: str = "", cap: int = 0,
               cap_note: str = "", worktree: str = "", headroom: Callable[[], bool] | None = None,
               age: float | None = None, pending: bool = False, check: str = "",
               repo_name: str = "", name: str = "", startable: Callable[[], bool] | None = None,
               running: bool = False, landing: bool | str = False) -> Grant | None:
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
    place in the queue at once and every checklane.TELL_S. `check` (its head and commands) names the
    check: a later run of the same task queueing the same check supersedes this wait, which then ends
    with checkcap.Cancelled (waiter_sets). A waiter whose process is gone is dropped from the queue with
    a note; one whose ticket vanished queues again at its place. The waiter next in line checks every
    HEAD_POLL_S, so a freed lane goes to it at once. `repo_name` goes in the lane's label for status.

    `lock`: the run goes behind the wrapper lock of that checklane.wrap_key (checks.wrap_cmd), so it takes
    that lock's one lane, whatever its repository, and never a repository lane or an overflow lane: runs
    behind one wrapper lock go one at a time, and lock-free runs never wait behind them. `cap`: the most of
    the project's checks that run at once over all its lanes (checklane.project_cap; 0: none), the lane
    going to the most urgent waiter (cap_turn); `cap_note` (checklane.cap_note) follows the cap's name in
    the wait line. A priority-1 waiter (`priority` <= 1) whose lanes are held
    only by less urgent runs takes an overflow lane beside them while `headroom()` says the host has room,
    never beside a holder in the same `worktree` (one suite per worktree); with the cap full of less urgent
    runs it takes one past the cap ("over_cap"), one such lane per project at a time (over_cap_room). `age`: the aging step
    (age_s(), checks.priority_age_s; default the account's or SUITE_AGE_S). `pending`: the run takes its
    test slots after the lane, so the lane counts against `cap` only once slots_granted() marks it.
    `name`: the lane's name for status (the repository's directory name, or with `lock` the wrapper
    command's: checklane.lane_labels). `slots` may be a function of the lanes held now (checklane.slots):
    it is read again at every try, and a lane is free only while fewer than that many lane files of the
    repository are held, so a count that fell under a holder's lane number admits nobody past it.
    `startable()` (pool_room): an overflow lane (cheaper, urgent, past the cap) is taken only while it
    says the run can start now, so no extra lane is held by a run that waits for test slots.
    `running` (with `pending`): the repository's lanes bound only running suites: a lane whose run still
    waits for its host grants leaves its place to the next waiter, which queues in those gates beside it
    (most urgent first there), and claim_running checks the bound when a run gets its grants. There an
    urgent or priority-1 waiter (checkcap.is_urgent) takes a lane at once, past its place, the lanes' count
    and the cap: it never waits behind holders that only queue for a host-wide slot, and the host queue,
    where it preempts routine runs, and claim_running decide when it runs."""
    from . import checklane
    age = age_s() if age is None else age
    if not project_name or os.environ.get(HELD_ENV) or os.environ.get(checkcap.HELD_ENV):
        return Grant()
    waiting = checkwait.chain(waiting, "checks lane")   # inside a push check: not its run time
    if lock:   # one wrapper lock: its own single lane, never an overflow one
        repo, slots, max_wait, headroom = f"w{lock}", 1, 0.0, None
    count = slots if callable(slots) else (lambda _held, n=max(1, int(slots or 1)): n)
    base = suite_base(project_name, repo)
    extra_lanes = lane_paths(project_name, 1, repo, overflow=True)
    task = str(checkcap._task_run()[0] or "")   # TTP_TASK, else the adopted one (checkcap.adopt_identity)
    label = f"{project_name}: full checks ({who + ', ' if who else ''}pid {os.getpid()})"
    ns = locks._pid_ns()
    text = ((f"task #{task} {label}" if task.isdigit() else label) + (f" [check {check}]" if check else "")
            + (f" [pidns {ns}]" if ns else ""))   # the ticket names its task for checklane.lane_lines
    resource = _suite_resource(project_name, repo)
    try:
        ticket = locks.enqueue(checkcap.slot_dir(), resource, text, since, priority=priority)
    except OSError:
        return Grant()   # an unwritable slot directory: the checks slot says so
    arrival = locks._arrival(ticket)
    checklane.remember_name(project_name, repo, name or repo_name)
    fields = {**({"task": task} if task.isdigit() else {}), **({"priority": priority} if priority is not None else {}),
              **({"cost": cost} if cost else {}), **({"lock": lock} if lock else {}),
              **({"worktree": str(worktree)} if worktree else {}), **({"granted": False} if pending else {}),
              **({"repo_name": repo_name} if repo_name else {}), **({"running": True} if running and pending and not lock else {}),
              "hb": checklane.HEARTBEAT_S,
              "queued": arrival}   # the holder's queue age, apart from its run time (checklane.holder_times)
    start, told = time.time(), 0.0
    try:
        while True:
            project.ps_cycle()   # pids are read once per poll cycle, never one `ps` each (ps_snapshot)
            mine = [x for x in _lanes_of(base) if not x.name.startswith(base + "-x")]
            held = [x for x in mine + extra_lanes if locks._slot_labels([x])]
            for x in held:
                _reclaim(x, tell, state)
            # Free lanes before the queue: a lane freed after this count waits for the next turn, so the
            # queue that decides holds every waiter that arrived before it was freed.
            n_all = len([x for x in mine if locks._slot_labels([x])])
            only_running = running and pending and not lock
            n_held = len([x for x in mine if locks._slot_labels([x]) and not _pending(x)]) if only_running else n_all
            at_once = only_running and checkcap.is_urgent(priority)
            slots = max(1, int(count(n_held) or 1))
            # Take only from the first `slots` files: with fewer holders, one of them is free. Holders past a
            # shrunk count still count, here and for the cheaper and urgent checks below. Holders still
            # waiting for their grants (`running`) take files past those, one more than are held.
            lanes = lane_paths(project_name, max(slots, n_all + 1) if only_running else slots, repo)
            recs = locks.holder_records(lane_paths(project_name, max(slots, len(mine)), repo) + extra_lanes)
            free = slots - n_held
            every = locks.queued(checkcap.slot_dir(), resource)
            if ticket not in every:   # removed under it: queue again at its place, never past the line
                if locks.dropped(ticket) and locks.run_ended(ticket):   # its run ended (locks.queued)
                    raise checkcap.Cancelled("its run has ended, so its place in the checks queue was dropped")
                locks.dequeue(ticket)
                try:
                    ticket = locks.enqueue(checkcap.slot_dir(), resource, text, arrival, priority=priority)
                except OSError:
                    return Grant()
                every = locks.queued(checkcap.slot_dir(), resource)
            kept, arrivals, superseded, dead = waiter_sets(every)
            for t in dead:
                who_ = _ticket_text(t).split(" [")[0]
                try:
                    t.unlink()
                except OSError:
                    continue
                tell and tell(f"dropped a dead waiter from the checks queue: {who_} (pid {ticket_pid(t)} is gone)")
            if ticket in superseded:
                raise checkcap.Cancelled(f"superseded: {_ticket_text(superseded[ticket]).split(' [')[0]} "
                                         f"queues the same check of the same task")
            q = _ordered(kept, arrivals, age=age)
            pos = q.index(ticket) if ticket in q else len(q)
            f = None
            cheaper = (max_wait > 0 and cost and time.time() - arrival >= max_wait and recs
                       and all(checklane.cheaper(cost, r.get("cost") or {}) for r in recs)
                       and (startable is None or startable()))
            # A priority-1 waiter also weighs the holders of a full project cap: it may start past it.
            p1 = priority is not None and priority <= 1 and headroom is not None
            on_cap = project_held(project_name) if p1 and cap > 0 else []
            pool = recs + (locks.holder_records(on_cap) if len(on_cap) >= cap > 0 else [])
            urgent = (not cheaper and p1 and pool
                      and all((r["priority"] if isinstance(r.get("priority"), (int, float)) else NO_PRIORITY) > 1 for r in pool)
                      and not (worktree and any(r.get("worktree") == str(worktree) for r in pool)) and headroom()
                      and (startable is None or startable()))
            try:
                with _cap_guard(project_name, cap), _lane_guard(base):
                    over = False
                    # Recount under the guard: a run that took a lane since the count above (and left the
                    # queue) is not free room, so a stale count never admits past the bound.
                    free = min(free, slots - _numbered_held(base, only_running)) if free > 0 else free
                    if free > 0 and pos < free:
                        if cap_turn(project_name, cap, ticket, slots, age=age):
                            f = locks.try_take(lanes, label, what, extra=fields)
                        else:
                            over = urgent and over_cap_room(project_name, cap)
                    elif cheaper or urgent:
                        if cap_turn(project_name, cap, ticket, slots, age=age):
                            f = locks.try_take(extra_lanes, label, what,
                                               extra={**fields, "overflow": True, **({"urgent": True} if urgent else {})})
                        else:
                            over = urgent and over_cap_room(project_name, cap)
                    if over:   # one priority-1 run at a time past the project's full cap
                        f = locks.try_take(extra_lanes, label, what,
                                           extra={**fields, "overflow": True, "over_cap": True, "urgent": True})
                    kind = "coordinator" if landing == "coordinator" else "mark" if landing else ""
                    if f is None and kind and landing_room(project_name, kind):
                        # A landing check (a push batch's, or the coordinator's task-less one) never waits behind
                        # the project's task checks: one of each kind at a time takes its own lane past the lanes
                        # and the cap, and a mark run never waits behind the coordinator's (landing_room).
                        f = locks.try_take(extra_lanes, label, what,
                                           extra={**fields, "overflow": True, "over_cap": True, "landing": kind})
                        if f is not None and tell:
                            tell("took the project's landing lane: a task-less or landing check never waits "
                                 "behind its task checks (the host-wide cap still applies)")
                    if f is None and at_once:
                        # Only after the over-cap and landing lanes: a place that only queues it for its host
                        # grants, never held behind holders that are only queued too; claim_running bounds running.
                        f = locks.try_take(lanes, label, what, extra=fields)
                    if f is not None and (cheaper or urgent) and "-x." in os.path.basename(f.name):
                        held_by = "; ".join(checklane.holder_text(r, time.time()) for r in pool[:3])
                        if tell and cheaper:
                            tell(f"took an extra checks lane alongside the holders after {_mins(time.time() - arrival)}: "
                                 f"this run is cheaper than {held_by}")
                        elif tell:
                            past = f" past the project cap of {cap}" if over else ""
                            tell(f"took an extra checks lane beside less urgent runs ({held_by}){past}: this run's "
                                 f"task is priority {priority:g} and the host has room")
            except OSError:
                return Grant()
            if f is not None:
                _heartbeat(f, checklane.HEARTBEAT_S)
                return Grant(0, [f], time.time() - start)
            if stop and (why := stop()):
                raise checkcap.Cancelled(why)
            # A wait that ran out says once what it waited for, though its first try outlasted `wait_s`.
            expired = wait_s is not None and time.time() - start >= wait_s
            if expired and (told or not wait_s):
                return None
            if not told or time.time() - told >= checklane.TELL_S:
                if not told and waiting:
                    waiting(True)
                told = time.time()
                place = pos + 1
                rule = ("one at a time behind this wrapper lock (checks.wrap_cmd)" if lock else
                        f"{'one' if slots == 1 else slots} at a time per project and repository, "
                        f"checks.{SLOTS_KEY} sets it")
                rule += (f"; at most {cap} of the project's checks at once (checks.test_project_max{cap_note})"
                         if cap > 0 else "")
                recs = recs or locks.holder_records(project_held(project_name))
                held_by = holders_text(recs, told)
                tell and tell(f"waiting for this project's other full checks to end ({rule}, most urgent priority "
                              f"first; this run {rank_text(ticket, told, age)}, position {place} of "
                              f"{max(len(q), place)}, {place - 1} waiter(s) ahead, waited {_mins(told - arrival)}), "
                              f"held by {held_by}")
            if expired:
                return None
            time.sleep(poll_s(HEAD_POLL_S if pos < slots else POLL_S))
    finally:
        locks.dequeue(ticket)
        if told and waiting:
            waiting(False)


def suite_running(grant, cap_s: float, log: str = "") -> None:
    """Mark the suite lock `grant` holds as running its commands from now under a wall-time cap of
    `cap_s` seconds (0: none), for checklane.lane_lines, writing to checks log `log` (checkstall reads it)."""
    for f in getattr(grant, "files", None) or []:
        if os.path.basename(getattr(f, "name", "")).startswith(SUITE + "."):
            locks.relabel(f, {"run_since": time.time(), "cap_s": float(cap_s or 0), **({"log": str(log)} if log else {})})


def _mins(s: float) -> str:
    m = int(max(0.0, s) // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


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


def _shell_counts(cmd: str) -> list[tuple[int, str]]:
    """(start, value) for each pytest `-n` / `--numprocesses` in `cmd` whose value the shell fills in
    ($VAR, ${VAR}, $(...), `...`): its count is unknown before it runs, so it may be above 1."""
    out = []
    for seg in _segments(cmd):
        words = [w for _, _, w in seg]
        tool = next((i for i, w in enumerate(words) if _is_tool(words, i, False)), None)
        for i in range(tool + 1, len(seg)) if tool is not None else ():
            if not (m := re.fullmatch(r"(-n|--numprocesses)(=?)(.*)", words[i])):
                continue
            value = m.group(3) or (words[i + 1] if not m.group(2) and i + 1 < len(words) else "")
            if _shell_value(value):
                out.append((seg[i][0], value))
    return out


def unpooled(cmd: str) -> list[str]:
    """The pytest `-n` values in `cmd` that may start more than one worker (an explicit N above 1, or a
    value the shell fills in such as `-n $TTP_TEST_SLOTS`) whose command no `ttp clip` or `ttp sandbox`
    wraps, so no host test slots are taken for them (direct)."""
    wrapped = [(seg[0][0], seg[-1][1]) for seg in _segments(cmd)
               if any(_base(a) == "ttp" and b in ("clip", "sandbox") for (*_, a), (*_, b) in zip(seg, seg[1:]))]
    found = [(start, str(n)) for start, *_, n in _edits(cmd, 1, False)] + _shell_counts(cmd)
    return [v for start, v in sorted(found) if not any(lo <= start <= hi for lo, hi in wrapped)]


def clamp(cmd: str, k: int) -> str:
    """`cmd` with each explicit pytest `-n N` / `--numprocesses N` above `k` lowered to `k`; only after
    a pytest word (by its basename: `.venv/bin/pytest`, `python3 -m pytest`) in the same command, so
    `head -n 50` or `nice -n 19 pytest` keep theirs. The rest of the text stays as it is."""
    return _apply(cmd, _edits(cmd, k, False))


# pytest-xdist's options: without xdist pytest refuses each as an unrecognized argument.
XDIST_OPTS = ("-n", "--numprocesses", "--dist", "--maxprocesses")
XDIST_PROBE = "import importlib.util as u, sys; sys.exit(0 if u.find_spec('xdist') else 3)"


def _python_of(argv: list[str]) -> str | None:
    """The interpreter a direct pytest run `argv` starts: its python word, or a pytest script's
    shebang (through `env`); None when it cannot tell."""
    name = os.path.basename(argv[0])
    if name not in ("pytest", "py.test"):
        return argv[0]
    script = shutil.which(argv[0]) or argv[0]
    try:
        with open(script, "rb") as fh:
            line = fh.readline(512).decode(errors="replace")
    except OSError:
        return None
    words = line[2:].split() if line.startswith("#!") else []
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return words[0] if words else None


def _has_xdist(python: str) -> bool | None:
    """Whether `python` can import xdist; None when the probe cannot tell (no such interpreter, a hang)."""
    try:
        rc = subprocess.run([python, "-c", XDIST_PROBE], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return None
    return True if rc == 0 else False if rc == 3 else None


def drop_xdist(argv: list[str]) -> tuple[list[str], str]:
    """A direct pytest run `argv` (`pytest ...`, `python3 -m pytest ...`) with pytest-xdist's options
    (XDIST_OPTS: -n N, -n auto, -nN, --numprocesses=N, --dist ...) taken out when the interpreter that
    runs it cannot import xdist, and a one-line notice saying so; else `argv` as it is and "". Another
    option's value (`-k "-n"`) and anything after `--` stay."""
    from .hook import PYTEST_VALUE_OPTS, _pytest_args
    args = _pytest_args(list(argv))
    if not args:
        return list(argv), ""
    keep, dropped, i = list(argv[:len(argv) - len(args)]), [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            keep += args[i:]
            break
        if a in XDIST_OPTS:
            dropped += args[i:i + 2]
            i += 2
            continue
        if any(a.startswith(o + "=") for o in XDIST_OPTS) or re.fullmatch(r"-n(?:\d+|auto|logical)", a):
            dropped.append(a)
        else:
            keep.append(a)
            if a in PYTEST_VALUE_OPTS and i + 1 < len(args):
                keep.append(args[i + 1])
                i += 1
        i += 1
    if not dropped or (python := _python_of(list(argv))) is None or _has_xdist(python) is not False:
        return list(argv), ""
    return keep, (f"pytest-xdist is not installed for {python}: dropped {shlex.join(dropped)}; "
                  f"the tests run in one process")


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
    else NICE (the lowest priority; a worker's runner.nice is not the checks'). A value that is no whole
    number falls back to the next source."""
    acct = project.load_account_settings().get("checks") or {}
    for v in (_section(cfg, "checks").get("nice"), acct.get("nice")):
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


def priority_args(cfg: dict | None = None) -> tuple[list, list] | None:
    """(the `nice` argv words, the `ionice` argv words) a check command runs behind, or None when
    checks.nice is false (the project's or the account's): check commands then run at normal priority."""
    acct = project.load_account_settings().get("checks") or {}
    if _section(cfg, "checks").get("nice") is False or acct.get("nice") is False:
        return None
    return ["nice", "-n", str(nice_level(cfg))], ["ionice", *ionice_args(cfg)]


def low_priority(argv: list) -> list:
    """A check command's argv under `nice` (nice_level) and `ionice` (ionice_args) where they exist, so a
    host's live services go first; either missing is skipped. checks.nice false (priority_args)
    runs it as it is."""
    prio = priority_args(_cfg())
    if prio is None:
        return list(argv)
    nice, ionice = prio
    return (nice if shutil.which("nice") else []) + (ionice if shutil.which("ionice") else []) + list(argv)

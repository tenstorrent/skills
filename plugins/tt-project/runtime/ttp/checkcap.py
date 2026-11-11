# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A host-wide cap on how many `ttp checks` runs execute their commands at once.

Many tasks of many projects may each start a full test suite at the same time; together they can
load the host so far that load-sensitive tests time out and every run fails. Each `ttp checks` that
has commands to run first takes one of `limit()` slots under ~/.tt-project/check-slots, shared by
every project of this account on this host. A run over the cap waits its turn in the queue
(locks.enqueue, below), sleeping between tries: no failure, no model run.

Slots and queue tickets are OS file locks (locks.py), so they end with the process that holds them:
a crash, a kill or a reboot never leaves a stale holder.

A slot directory that cannot be written (a worker's sandbox without it among its writable roots,
or file permissions) never runs the checks uncapped: acquire raises Unavailable, which says what to
fix. Workers get the directory as a writable root (daemon.start_run).

The cap is the account setting `checks.host_max` (`ttp config --account checks.host_max N`), or the
environment's TTP_CHECKS_HOST_MAX; 0 turns it off. Unset, it follows the load (default_limit()):
as many checks as the cores left over the measured CPU demand (_load(): /proc/stat or macOS's CPU ticks,
not the load average, which counts tasks waiting on I/O) hold at check_load() each, at least one
slot per CORES_PER_SLOT cores (and at least one), at most one per CORES_PER_SLOT_MAX cores. Our own
running checks count in that load, so the cap shrinks as they load the host; a check keeps its slot
until it ends, and a waiter reads the cap again on every try. Check commands run with HELD_ENV
set, so a `ttp checks` nested in them runs at once instead of waiting for the slot its parent holds.
A check command that waits on an outside CPU pool gives the slot up meanwhile (`ttp checks-yield`: ask_yield,
answered by the run's serve_yield while the command runs) and takes one back in the normal order before its CPU
work (`--resume`); the run's next command never starts without one (Slot.hold_again).

Push checks (the push queue's batches and `ttp push` to the push branch) hold the push lock, so every
approved change waits on them: they never queue behind task checks. Each push branch has one priority
slot of its own (`acquire_priority`), keyed by the remote and branch so it is one per
branch host-wide, across projects. It counts toward the cap: a push holds it while it waits for room, so the
next slot that frees is its, ahead of every waiter, and it starts once the runs on the cap's slots, the
running push checks, those waiting on theirs since before it and the urgent slots' holders leave room (_room,
_counted, _priority_turn). `checks.priority_beyond_cap` true (TTP_CHECKS_PRIORITY_BEYOND_CAP; default off) instead
runs push checks and urgent runs beyond the cap and their reserved test lanes beyond the test pool, as an overflow. `ttp land`'s checks take one too, keyed by the repository's path
and the local branch it moves (push.priority_key with no remote). Task checks keep their cap and their order: the priority slot never
takes one of theirs. It is an OS file lock like the others, so a killed push frees it too. A push waiting
behind another holder of its priority slot waits `checks.push_priority_wait_s` (TTP_CHECKS_PRIORITY_WAIT_S,
default PRIORITY_WAIT_S) at most, then takes an ordinary host-wide turn instead (its caller's `fallback`), so a
holder that is itself stuck or waiting never holds every later push of its branch or lane for hours.

Checks of urgent tasks (priority 1 or better, an `urgent:` spec line: user-ordered and live-impact work,
heal self-fixes, interim mitigations, the review of an urgent change) go next: ahead of every routine
waiter of any project whatever its arrival, the most urgent first, then by project turns (locks.fair_order with
locks.URGENT_FIRST, the default PRIORITY_TURNS). `checks.priority_turns` N (TTP_CHECKS_PRIORITY_TURNS)
instead bounds them to N turns in a row while other checks wait, taking turns by project; 0 serves them
in the fair order only.

A run that has waited `checks.starve_s` (TTP_CHECKS_STARVE_S, default STARVE_S, about one full suite run)
goes ahead of later arrivals at its own priority or a less urgent one, for the next free slot (locks.fair_order's
`aged_before`). More urgent later arrivals keep their precedence under a backlog, but an aged routine run takes
the next turn once aged_turns() urgent ones went (a set checks.priority_turns N, else AGED_TURNS; 0: it goes
first), so it starts within a bound. A run parked outside the queue for its
test slots (acquire's `startable`) holds a PARKED ticket meanwhile, so the wait is visible: once it has
waited that long, later runs of its project at its priority or lower leave it their project's next free test
slots, more urgent ones after aged_turns() of them started (testslots.pool_room, starved_ahead; push checks
count as least urgent, and a less urgent task run leaves its project's next free test slots to the project's
parked priority-0 runs, whatever their arrival: owed_ahead). An owed or urgent run (locks.URGENT_PRIORITY or
better) is never held by a less urgent one, however long that one waited (unless checks.priority_turns is 0:
no urgent precedence). Push checks run on their branch's reserved test lane alone
while any run waits for test slots (testslots.acquire), so a stream of pushes never holds them all.

An urgent run (priority locks.URGENT_PRIORITY or better: an obligation owner, a user-ordered or `urgent:` task)
that would wait with every slot busy (all held, a queue ahead of it, or parked for its test slots) never waits
behind routine runs. `ttp checks` first tries the account's test pool at once (checkspill.pick with no bounded
wait first, cli._checks_slot); with no pool host free it takes a reserved urgent slot (acquire's `urgent`):
`checks.urgent_slots` (TTP_CHECKS_URGENT_SLOTS, default URGENT_SLOTS; 0 turns them off) slots of their own,
as push checks have one per branch, each taking test slots ahead of the queue (testslots' push lane keyed
urgent_key) and past its project's cap of running checks. They count toward the cap: one is taken only while the
cap has room, the next slot that frees ahead of every waiter (with checks.priority_beyond_cap true, beyond the
cap and the load's extras with a reserved test lane, at most that many above the cap at once). Only urgent runs
take one; the cores and disk gates do not hold it (as for push checks).
With those held or the cap full, an urgent waiter that has waited preempt_after_s() since its arrival asks, one live ask at a time
(PREEMPT_ASKED_ENV, kept over a re-execution; again preempt_after_s() after one lapsed), one routine run on a cap slot to step back (preempt): one never
preempted before first, then its own project's, then the least urgent, then the latest granted (it loses the least
work). It ends its command, lets its slots go and queues again at its old arrival, ahead of every later arrival at
its priority or a less urgent one whatever the project turns (a requeued ticket: locks.requeued_place), asking
nobody itself there even when urgent (its grace counts from that old arrival), so it
keeps its place and what it recorded (in a pinned run, only passes on the pinned commit), and the waiter takes
the freed slot: a claim (claims, CLAIM_S) keeps
it from every other waiter while the asker lives and renews it, and a request whose claim lapsed asks nothing.
A waiter parked for its test slots asks only a run whose stop lets it start (preempt's `startable`, FREEING:
its project's budget and the host's test pool counted without that run's slots); else nobody. Its log says
`preempted by urgent #<task>`; its queue entry says `requeued after preemption by ...` until it holds a slot
again (PREEMPTED_ENV, label(), requeued()). Never a push check (their priority slots are no cap slots), nor a
run behind a lock (a wrapper lock, `ttp lock`, `ttp devq`: preemptible()); a holder whose label does not say
it can be asked (an older ttp) is skipped. Never an urgent run either, except that a waiter whose task owns an
open obligation (owed: the slot label's "owed") asks an urgent run that owes nothing, after every routine one,
so an owed run never waits behind urgent work that owes nothing; an owed holder is never asked. `ttp checks
--preempt <run>` writes the same request by hand, logged with who asked (ask_preempt). A freed reserved urgent
slot goes to the urgent waiter that ranks first (URGENT_WAIT tickets: an owed one, then the oldest), not to
whichever polls first. A run whose task's priority changes
while it waits (raised to urgent, or lowered) sees it within RERANK_S (reranked) and re-executes to queue
again at the new priority, keeping its arrival (cli._checks_reexec), so a queued or parked ticket never
keeps a priority its task no longer has.

While the host is lightly loaded (its CPU demand, _load(), under half its cores) up to `checks.load_extra`
slots (TTP_CHECKS_LOAD_EXTRA, default EXTRA_SLOTS; 0 turns it off) open beyond the cap: two below a quarter of the cores, one
below half. A check on one keeps it until it ends, but no new one takes one once the load rises.
They open beyond a default cap's ceiling too. checks.load_extra 0 also keeps a project's checks lanes
per repository and the test pool at their floors (checklane.capacity, testslots.pool_now).

Whatever the cap (unless it is off), a slot of the cap or beyond it is taken only while the host's live free cores cover
one check's price (cores_short: busy cores from /proc/stat over the last few seconds, less the price of
each check granted within RAMP_S), so a saturated host starts no more suites; with no check running one
always starts. The run's test grant then follows the same free cores (testslots.live_room).
`checks.live_cores` 0 (TTP_CHECKS_LIVE_CORES) turns both off. Nor is one taken while the host's disk is
saturated by writes (diskgate: dirty page cache or IO pressure over their limits), even with no check running.

Routine waiters of a project below its fair share of the cap (max(1, cap // the projects running or waiting
checks here), counting the slots it holds) go before those of projects at or over theirs (locks.fair_share):
one project's backlog cannot hold every slot while another waits. A share nobody waits for goes to whoever
waits, and no running check is stopped for a share; urgent and owed waiters keep their places ahead. Each
grant after a wait logs the other projects then over their share ("over_share", record_grant), which
`ttp throughput` shows per project. Accepted gaps: a project's run waiting outside the queue for test slots
(parked) does not count toward the number of projects; an aged run of a project at its share waits for the
under-share projects' turns, at most their shares; runtimes older than this order the queue without shares.

Waiters take turns by project (locks.fair_order); within one, by task priority (1 first;
a spec's `urgent:` line counts as 1, urgent.is_urgent), then arrival; a running check is never stopped.
The checks of a task that is a required owner of an open obligation (obligations.owned_open: an urgent
deliverable owed to the user) queue at OBLIGATION_PRIORITY, ahead of every task priority, until it has its
receipt or is dropped; the ticket and the status line mark them (OBLIGATION_MARK). Within the project
they go first; between projects they count as urgent like priority 1 (above: bounded turns).

A waiter whose checks match ones running now (`same`: the project, tree, commands and purpose) waits
for their result outside the queue, keeping its arrival time; `done` ends any wait once a recorded
pass makes the run needless, and `stop` ends it when the task the checks belong to ended.

Each slot taken is logged in the taker's project state (record_wait: WAITS_LOG, one synced JSON line
with the project, task, priority, queued_at and granted_at, and "cap": the project cap in force at
the grant (checklane.project_cap) when the taker held a project lane); `ttp status` shows that project's median
and p90 wait over the last 24 h (wait_line). Each grant is also logged host-wide (record_grant: GRANTS_LOG
under ~/.tt-project, readable by every run), and a grant after a wait of more than GRANT_TELL_S from its
arrival (queued here, or inherited by a relaunch) says its priority, the obligations behind a boost and
when it arrived (grant_line).
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from . import checkwait, diskgate, hostcpu, locks, obligations, poll_s, project, timefmt, urgent

RESOURCE = "checks"
OBLIGATION_PRIORITY = 0   # an open obligation's required owner: before every task priority (1 and up)
OBLIGATION_MARK = " [owed to the user: goes first]"   # on its queue ticket (an obligation owner or user-ordered urgent)
PRIORITY = "checks-push"   # check-slots/checks-push.<key>.lock: one priority slot per push branch
CORES_PER_SLOT = 16       # the default cap's floor: one slot per this many cores, at least one
CORES_PER_SLOT_MAX = 8    # its ceiling: one slot per this many cores
CHECK_LOAD = 4.0          # the cores one running check keeps busy with the test pool off (check_load())
HELD_ENV = "TTP_CHECKS_SLOT"
COST_LOG = "check-costs.jsonl"   # in slot_dir(): recent runs' measured cores (record_cost, measured_cores)
COST_RECENT = 20
COST_KEEP_S = 7 * 86400.0
COST_MIN_WALL_S = 60.0    # a shorter run is no sample (its ratio is noise)
COST_MAX_BYTES = 16 * 1024
COST_PERCENTILE = 0.8     # measured_cores: the price is this percentile of the recent runs (nearest rank)
COST_MIN_SAMPLES = 5      # fewer recent runs price nothing (check_load's default); at 5 or more the p80 rank
                          # is below the costliest, so no single heavy run sets the price
POLL_S = 5.0
TELL_S = 300.0
# A `ttp checks` in a tool call waits this long for a slot at most, then hands over to detached checks
# (cli.cmd_checks): a full queue can take many minutes, longer than a tool call may block.
FOREGROUND_WAIT_S = 60.0
# The arrival time a foreground run hands to its detached checks, so they keep its place in the queue.
ARRIVED_ENV = "TTP_CHECKS_ARRIVED"
# The runtime version a waiting detached `ttp checks` re-executed itself for (upgraded()): never again for it.
REEXEC_ENV = "TTP_CHECKS_REEXEC"
# The host-wide checks slot a re-executing `ttp checks` was granted and keeps: [fd, path] (keep_slot).
KEPT_ENV = "TTP_CHECKS_KEPT_SLOT"
UPGRADE_SETTLE_S = 10.0   # a newly installed version is adopted once seen this long (an install copies file by file)
EXTRA_SLOTS = 2   # at most this many slots beyond the cap while the host is lightly loaded (extra())
PRIORITY_TURNS = locks.URGENT_FIRST   # urgent checks always first (priority_turns(); N > 0 bounds their run)
PARKED = "checks-parked"   # the queue of runs waiting outside the checks queue for their test slots (acquire)
PUSH_WAIT = "checks-push-wait"   # who waits for a push priority slot another push holds (acquire_priority): status only
BATCH_ENV = "TTP_PUSH_BATCH_ID"   # set by a push batch process (pushq._spawn): its batch id, for status and logs
PRIORITY_WAIT_S = 1800.0   # default checks.push_priority_wait_s: the longest wait behind another priority holder
PRIORITY_WAIT_ENV = "TTP_CHECKS_PRIORITY_WAIT_S"
STARVE_S = 3600.0   # default checks.starve_s: a run waiting this long goes before later arrivals at its priority or lower
AGED_TURNS = 3   # default urgent turns an aged routine run lets go before its own (aged_turns())
STARVE_ENV = "TTP_CHECKS_STARVE_S"
SMALL_TESTS = 500   # default checks.small_tests: a targeted run of fewer tests is short (is_small)
SMALL_ENV = "TTP_CHECKS_SMALL_TESTS"
URGENT = "checks-urgent"   # check-slots/checks-urgent.<n>.lock: the reserved urgent slots (urgent_paths)
URGENT_SLOTS = 1   # default checks.urgent_slots: urgent runs at once above the cap
URGENT_SLOTS_ENV = "TTP_CHECKS_URGENT_SLOTS"
PREEMPT_AFTER_S = 120.0   # default checks.preempt_after_s: an urgent waiter's grace before it asks a routine run
PREEMPT_AFTER_ENV = "TTP_CHECKS_PREEMPT_AFTER_S"
PREEMPT_ASKED_ENV = "TTP_CHECKS_PREEMPT_ASKED"   # this urgent request asked already (kept over a re-execution)
PREEMPTED_ENV = "TTP_CHECKS_PREEMPTED"   # this routine run was preempted: "urgent #<task>" (label())
CLAIM = "claim"   # check-slots/claim.<token>.json: the slot a preempted run frees is its asker's (preempt)
CLAIM_S = 30.0   # a claim lapses this long after its asker last renewed it (it renews while the asked run holds)
CLAIM_MAX_S = 600.0   # and this long after it was made, however often renewed
CLAIM_POLL_S = 0.5   # an asker with a live claim looks this often, so it takes the freed slot at once
PARKED_CLAIM_S = 60.0   # an asker that asked while parked renews its claim parked this long from the ask
HANDOVER_S = 3.0   # a parked asker that still cannot start this long after the asked run let go drops its claim
# The run a preempt would stop: testslots counts the test slots it holds as free (preempt's `startable`).
FREEING: contextvars.ContextVar[dict | None] = contextvars.ContextVar("checkcap_freeing", default=None)
RERANK_ENV = "TTP_CHECKS_RERANKED"   # the priority these waiting checks last re-executed at (reranked)
# The lane wait a re-executing `ttp checks` carries over (cli._checks_slot): {"s", "pool", "at"} as record_wait's
# lane_s, pool_s and lane_at. And a preempted run's cut, read by record_wait: {"at": its slot's grant, "by":
# the asker's project} (cut_at, cut_by: the rerun's wait from its old grant is the preempting project's).
LANE_WAIT_ENV = "TTP_CHECKS_LANE_WAIT"
CUT_ENV = "TTP_CHECKS_CUT"
RERANK_S = 30.0   # a waiting run reads its task's priority again at most this often (reranked)
# A preempted run's rerun resumes each plain local pytest check from the files it passed (cli._checks_resume).
RESUME_ENV = "TTP_CHECKS_RESUME"
SHIELD_PCT = 90   # a run that cannot resume is never asked once its log's last pytest `[ NN%]` is this far (preempt)
# What only this `ttp checks` run reads (its own re-queue, re-execution and label): never passed to the check
# commands it starts (push.check_env), so a `ttp checks` nested in them is not labelled preempted nor kept from
# its own rerank or re-execution.
RUN_ONLY_ENV = (REEXEC_ENV, PREEMPT_ASKED_ENV, PREEMPTED_ENV, RERANK_ENV, RESUME_ENV, LANE_WAIT_ENV, CUT_ENV)
BARE_SLOTS = 1   # default checks.bare_slots: host-wide slots held at once by runs with no project (acquire)
BARE_SLOTS_ENV = "TTP_CHECKS_BARE_SLOTS"
INHERIT_DEPTH = 64   # ancestors read at most for the project run a `ttp checks` was started under (inherited)
NO_INHERIT_ENV = "TTP_TEST_NO_INHERIT"   # tests: never take the identity of the process running them
ADOPTED_ENV = "TTP_CHECKS_ADOPTED"   # the identity a detached run's parent adopted (adopted_env)
IDLE_HOLD_S = 600.0   # default checks.idle_hold_s: a slot held this long with no test running is flagged (line)
IDLE_HOLD_ENV = "TTP_CHECKS_IDLE_HOLD_S"
ACTIVE_SAMPLE_S = 0.5   # holder_activity reads each holder's tests' CPU twice, this far apart
ACTIVE_FRAC = 0.05      # ... and counts them running at this share of one core or more


def slot_dir() -> Path:
    return project.HOME_DIR / "check-slots"


class Unavailable(RuntimeError):
    """The slot directory cannot be written here: the checks must not run without the cap."""


def _unavailable(e: OSError) -> Unavailable:
    return Unavailable(f"host-wide checks slots unavailable: {slot_dir()} is not writable here "
                       f"({e.strerror or e}); not running the checks without the host-wide cap. The run's "
                       f"sandbox or file permissions must allow writing it; hand the task back as blocked "
                       f"and name this directory")


def cores() -> int:
    """This host's cores as checks count them: hostcpu.cores() (the CPU affinity, else os.cpu_count())."""
    return hostcpu.cores()


def default_range() -> tuple[int, int]:
    """The default cap's floor and ceiling on this host's cores."""
    c = cores()
    floor = max(1, c // CORES_PER_SLOT)
    return floor, max(floor, c // CORES_PER_SLOT_MAX)


def check_load() -> float:
    """The 1-minute load one running check adds: one run's cores as the repository lanes count it
    (checklane.run_cores: its test grant, at least the measured cores of a full suite run), else, with
    the test pool off, CHECK_LOAD; never below what recent runs on this host measured (measured_cores:
    the 80th percentile of their user and system CPU, process churn included, once 5 runs measured)."""
    from . import checklane, testslots
    base = float(checklane.run_cores()) if testslots.sizes() else CHECK_LOAD
    return max(base, measured_cores() or 0.0)


def child_cpu_s() -> float:
    """The user and system CPU seconds of this process's children it (or they) waited for so far."""
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_CHILDREN)
    except (ImportError, OSError):
        return 0.0
    return r.ru_utime + r.ru_stime


def record_cost(cpu_s: float, wall_s: float) -> None:
    """Keep one finished checks run's cost on this host for measured_cores: the user and system CPU
    seconds of its commands and everything they waited for (cli: RUSAGE_CHILDREN) over its `wall_s`.
    Runs under COST_MIN_WALL_S are no sample; a failed write only loses one."""
    if wall_s < COST_MIN_WALL_S or cpu_s <= 0:
        return
    path = slot_dir() / COST_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > COST_MAX_BYTES:
            project.durable_write(path, "".join(f"{x}\n" for x in path.read_text().splitlines()[-COST_RECENT:]))
        project.durable_append(path, json.dumps({"ts": time.time(), "cores": round(cpu_s / wall_s, 2),
                                                 "s": round(wall_s, 1)}) + "\n")
    except OSError:
        pass


def measured_cores(now: float | None = None) -> float | None:
    """The COST_PERCENTILE (nearest rank) of the last COST_RECENT checks runs on this host (record_cost)
    within COST_KEEP_S, in cores (CPU seconds per wall second), at most this host's cores; None with
    fewer than COST_MIN_SAMPLES of them. A percentile, not the costliest, so one unusually heavy run
    does not price every project's checks (and hold them for that many free cores) for COST_KEEP_S."""
    now = time.time() if now is None else now
    try:
        lines = (slot_dir() / COST_LOG).read_text().splitlines()
    except OSError:
        return None
    got = []
    for line in lines:
        try:
            r = json.loads(line)
            if now - float(r["ts"]) <= COST_KEEP_S and float(r["cores"]) > 0:
                got.append(float(r["cores"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    got = sorted(got[-COST_RECENT:])
    if len(got) < COST_MIN_SAMPLES:
        return None
    return min(float(cores()), got[max(0, math.ceil(COST_PERCENTILE * len(got)) - 1)])


def default_limit() -> int:
    """The cap with no checks.host_max: the checks the cores left over the CPU demand (_load()) hold
    at check_load() each, within default_range(). An unreadable reading gives the floor."""
    floor, top = default_range()
    fit = int(max(0.0, cores() - _load()) // check_load())
    return min(top, max(floor, fit))


RAMP_S = 30.0   # a check or test worker granted this recently may not show in the busy cores yet
LIVE_ENV = "TTP_CHECKS_LIVE_CORES"


def live_cores_on() -> bool:
    """The live-cores gate (cores_short, testslots.live_room): TTP_CHECKS_LIVE_CORES, else the account's
    checks.live_cores, default on; 0 turns it off."""
    for v in (os.environ.get(LIVE_ENV), (project.load_account_settings().get("checks") or {}).get("live_cores")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return bool(n)
    return True


def free_cores() -> float | None:
    """This host's cores no process keeps busy now: its cores less hostcpu.busy_cores() (/proc/stat over
    the last few seconds, or macOS's CPU ticks), or less the 1-minute load average where neither can be
    read; None with nothing readable."""
    busy = hostcpu.busy_cores()
    if busy is None and ((busy := _loadavg()) != busy or busy == float("inf")):
        return None
    return max(0.0, cores() - busy)


def held_since(paths: list[Path]) -> list[float]:
    """When each slot among `paths` taken right now was taken (its label's since; 0 when unreadable)."""
    out = []
    for path in paths:
        if not locks._slot_labels([path]):
            continue
        try:
            out.append(float(json.loads(path.read_text() or "{}").get("since") or 0))
        except (OSError, ValueError, TypeError, AttributeError):
            out.append(0.0)
    return out


def cores_short(every: list[Path], now: float | None = None) -> str:
    """Why no check may start now for want of cores, else "": one starts only while the host's live
    free cores (free_cores()), less check_load() for each check granted within RAMP_S (its workers may
    not show yet), cover one check's price (check_load()); busy cores count only while the demand lasts
    (sustained_load: a short spike leaves the cores free). With no check running on the host one
    always starts, so checks never starve; checks.live_cores 0 or no reading lets every one start."""
    if not live_cores_on() or not (held := held_since(every)) or (free := free_cores()) is None:
        return ""
    if sustain_s() and (ld := sustained_load(now)) == ld and ld != float("inf"):
        free = max(free, cores() - ld)   # busy cores that did not last (sustained_load) leave room
    now = time.time() if now is None else now
    price = check_load()
    left = free - price * sum(1 for s in held if now - s < RAMP_S)
    if left >= price:
        return ""
    return f"{max(0.0, left):.0f} of {cores()} cores free now, one check takes {price:.0f}"


def _number(v) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _set_limit() -> int | None:
    """TTP_CHECKS_HOST_MAX, else the account's checks.host_max; None when neither is readable."""
    for v in (os.environ.get("TTP_CHECKS_HOST_MAX"), (project.load_account_settings().get("checks") or {}).get("host_max")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return None


def cap_text(n: int) -> str:
    """The host-wide cap `n` with where it comes from, for a wait line: the setting, else the free-core fit."""
    if os.environ.get("TTP_CHECKS_HOST_MAX") not in (None, "") and _set_limit() is not None:
        return f"cap {n} (TTP_CHECKS_HOST_MAX)"
    if _set_limit() is not None:
        return f"cap {n} (checks.host_max)" if n else "no cap (checks.host_max 0)"
    return f"cap {n}: the free-core fit (checks.host_max unset)"


def limit() -> int:
    """Slots on this host: TTP_CHECKS_HOST_MAX, else the account's checks.host_max, else the
    default from the core count and load (default_limit()). 0 means no cap; an unreadable value is the default."""
    n = _set_limit()
    return default_limit() if n is None else n


def _span() -> int:
    """The most cap slots any taker may use: the set cap, else the default's ceiling. A default cap
    that shrank with the load still counts the checks on slots above it."""
    n = _set_limit()
    return default_range()[1] if n is None else n


def extra_max() -> int:
    """Slots that may open beyond the cap: TTP_CHECKS_LOAD_EXTRA, else the account's checks.load_extra,
    else EXTRA_SLOTS; an unreadable value is the default."""
    for v in (os.environ.get("TTP_CHECKS_LOAD_EXTRA"), (project.load_account_settings().get("checks") or {}).get("load_extra")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return EXTRA_SLOTS


def priority_turns() -> int:
    """Priority-1 checks' turns in a row at most while other checks wait: TTP_CHECKS_PRIORITY_TURNS, else
    the account's checks.priority_turns, else PRIORITY_TURNS (locks.URGENT_FIRST: always first, unbounded);
    0 serves them in the fair order only."""
    for v in (os.environ.get("TTP_CHECKS_PRIORITY_TURNS"),
              (project.load_account_settings().get("checks") or {}).get("priority_turns")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return PRIORITY_TURNS


def urgent_slots() -> int:
    """Reserved urgent slots (beyond the cap only with priority_beyond_cap()): TTP_CHECKS_URGENT_SLOTS, else the account's checks.urgent_slots,
    else URGENT_SLOTS; 0 turns them off, an unreadable value is the default."""
    for v in (os.environ.get(URGENT_SLOTS_ENV), (project.load_account_settings().get("checks") or {}).get("urgent_slots")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return n
    return URGENT_SLOTS


BEYOND_CAP_ENV = "TTP_CHECKS_PRIORITY_BEYOND_CAP"


def priority_beyond_cap() -> bool:
    """Whether push checks' priority slots and the reserved urgent slots run beyond the cap, and their reserved
    test lanes beyond the test pool, as an overflow: TTP_CHECKS_PRIORITY_BEYOND_CAP, else the account's
    checks.priority_beyond_cap (true/on/1 or false/off/0). Off by default: they count toward the cap (_room,
    _counted) and draw on the test pool (testslots.acquire), still ahead of every waiter."""
    for v in (os.environ.get(BEYOND_CAP_ENV),
              (project.load_account_settings().get("checks") or {}).get("priority_beyond_cap")):
        if isinstance(v, bool):
            return v
        if (w := str(v if v is not None else "").strip().lower()) in ("1", "true", "on", "yes"):
            return True
        if w in ("0", "false", "off", "no"):
            return False
    return False


def preempt_after_s() -> float:
    """Seconds an urgent waiter waits from its arrival before it asks a routine run for its slot (preempt):
    TTP_CHECKS_PREEMPT_AFTER_S, else the account's checks.preempt_after_s, else PREEMPT_AFTER_S. 0 asks at once;
    an unreadable or negative value is the default."""
    for v in (os.environ.get(PREEMPT_AFTER_ENV), (project.load_account_settings().get("checks") or {}).get("preempt_after_s")):
        if v not in (None, ""):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if 0 <= f < math.inf:
                return f
    return PREEMPT_AFTER_S


_LOCKING = re.compile(r"(^|[\s;&|(])(ttp\s+(lock|devq)|flock)\b")


def preemptible(cmds, lock: str = "") -> bool:
    """Whether a routine run of `cmds` may be asked for its slot (preempt): never one behind a wrapper lock
    (`lock`, checks.wrap_cmd) or whose commands take a lock or a device queue themselves (`ttp lock`, `ttp devq`,
    `flock`): stopping it could leave a device or machine mid-step."""
    return not lock and not any(_LOCKING.search(str(c)) for c in cmds or [])


_RERANK: dict = {"at": 0.0}


def reranked(p, prio: int | None, now: float | None = None) -> int | None:
    """The task's priority read again (task_rank), at most every RERANK_S: the new one once it differs from
    the priority these waiting checks queued at (`prio`: raised, lowered, or now none), else None. Once per
    priority (RERANK_ENV holds the one it last re-executed at, or failed to): its re-execution reads the new
    priority at its start, and a later change re-ranks it again."""
    if p is None:
        return None
    now = time.time() if now is None else now
    if now - _RERANK["at"] < RERANK_S:
        return None
    _RERANK["at"] = now
    new = task_rank(p)[0]
    if new is None or new == prio or os.environ.get(RERANK_ENV) == str(new):
        return None
    return new


def urgent_paths() -> list[Path]:
    return [slot_dir() / f"{URGENT}.{i}.lock" for i in range(urgent_slots())]


def is_urgent(priority: int | None) -> bool:
    """A checks run of this task priority may take a reserved urgent slot (task_rank's: 0 an obligation
    owner or user-ordered urgent task, 1 an urgent one)."""
    return priority is not None and priority <= locks.URGENT_PRIORITY


def urgent_key(slot) -> str:
    """The test lane key (testslots' reserved lane) of the urgent slot `slot` holds, "" for any other."""
    f = getattr(slot, "f", None)
    name = Path(f.name).name if f is not None else ""
    return f"urgent:{name}" if name.startswith(URGENT + ".") else ""


def starve_s() -> float:
    """Seconds a checks run waits at most before it outranks every later arrival (above): TTP_CHECKS_STARVE_S,
    else the account's checks.starve_s, else STARVE_S. Account-level like priority_turns: every waiter on the
    host orders the shared queue by it, so a per-project value would make them disagree on who is next. Zero,
    negative or infinite give the default: the bound is what keeps a routine run from starving."""
    try:
        v = float(os.environ.get(STARVE_ENV)
                  or (project.load_account_settings().get("checks") or {}).get("starve_s") or STARVE_S)
    except (TypeError, ValueError):
        v = STARVE_S
    return v if 0 < v < math.inf else STARVE_S


def priority_wait_s() -> float:
    """Seconds a push waits at most behind another holder of its priority slot before it takes an ordinary
    host-wide turn (acquire_priority's `fallback`): TTP_CHECKS_PRIORITY_WAIT_S, else the account's
    checks.push_priority_wait_s, else PRIORITY_WAIT_S. Zero, negative, infinite or unreadable: the default."""
    try:
        v = float(os.environ.get(PRIORITY_WAIT_ENV)
                  or (project.load_account_settings().get("checks") or {}).get("push_priority_wait_s")
                  or PRIORITY_WAIT_S)
    except (TypeError, ValueError):
        v = PRIORITY_WAIT_S
    return v if 0 < v < math.inf else PRIORITY_WAIT_S


def aged_turns() -> int:
    """How many more urgent runs go before a run that waited starve_s(), before it takes the next turn: a set
    checks.priority_turns N (priority_turns(); 0: none, it goes first), else AGED_TURNS. So urgent runs keep
    their precedence under a backlog and an aged routine run still starts within N urgent turns."""
    n = priority_turns()
    return AGED_TURNS if n == locks.URGENT_FIRST else max(n, 0)


def small_tests() -> int:
    """A targeted run (checklane.cost's "tests") of fewer tests than this is short (is_small): TTP_CHECKS_SMALL_TESTS,
    else the account's checks.small_tests, else SMALL_TESTS; 0 turns it off. Account-level as starve_s: every waiter
    on the host orders the shared queues by it. Negative or unreadable: the default."""
    v = _number(os.environ.get(SMALL_ENV))
    if v is None:
        v = _number((project.load_account_settings().get("checks") or {}).get("small_tests"))
    return SMALL_TESTS if v is None else v


def is_small(cost: dict | None) -> bool:
    """Whether a run of `cost` (checklane.cost) is a short targeted one: fewer than small_tests() tests. Such a
    run goes first among its project's waiters at its priority (locks.SMALL_TICKET) and is held by no parked
    longer one at its priority or a less urgent one (starved_ahead); the host cap (limit) still holds it. One
    that also runs an unnarrowed full suite (checklane.cost's "whole") costs a full run: never short."""
    n = (cost or {}).get("tests")
    return (isinstance(n, int) and not isinstance(n, bool) and 0 <= n < small_tests()
            and not (cost or {}).get("whole"))


def order() -> dict:
    """locks.fair_order's (and my_turn's) ordering arguments for the checks queue: runs with no project last,
    and each project's fair share of the cap (share())."""
    return {"urgent_max": priority_turns(), "aged_before": aged_before(), "aged_turns": aged_turns(),
            "bare_last": True, "share": share()}


def bare_slots() -> int:
    """Host-wide slots runs with no project hold at once: TTP_CHECKS_BARE_SLOTS, else the account's
    checks.bare_slots, else BARE_SLOTS; at least 1. The cap binds only while a project's run waits (_take)."""
    for v in (os.environ.get(BARE_SLOTS_ENV), (project.load_account_settings().get("checks") or {}).get("bare_slots")):
        if v not in (None, "") and (n := _number(v)) is not None:
            return max(n, 1)
    return BARE_SLOTS


def _bare_held(every: list[Path]) -> int:
    """Slots among `every` held now by a run with no project (its label's checks_project empty)."""
    return sum(1 for r in locks.holder_records(every) if "checks_project" in r and not r["checks_project"])


def _project_waits() -> bool:
    """A run of some project waits in the host-wide queue (its ticket has a group)."""
    return any(locks.ticket_group(t) for t in locks.queued(slot_dir(), RESOURCE))


def running_by_project() -> dict[str, int]:
    """{project: checks it runs now on the cap's slots and those load opened beyond it}, from each held
    slot's "checks_project" (acquire's label), else its holder label (an older runtime's: _project); ""
    for runs with no project. The push and urgent slots beyond the cap are no share of it."""
    out: dict[str, int] = {}
    for path in all_paths() if limit() else []:
        if not locks.held_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            h = {}
        h = h if isinstance(h, dict) else {}
        name = (h.get("checks_project") or "") if "checks_project" in h else _project(str(h.get("holder") or ""))
        name = "" if name == "no project" else name
        out[name] = out.get(name, 0) + 1
    return out


def share() -> tuple[int, dict[str, int]] | None:
    """locks.fair_order's `share`: (the cap, {queue group id: slots held}); None with the cap off."""
    n = limit()
    return (n, {locks.group_id(k): v for k, v in running_by_project().items()}) if n else None


def over_share(project_name: str | None) -> list[str]:
    """The other projects that hold more than their fair share of the cap now (locks.share_of over the
    projects running or waiting): what kept a slot from `project_name` while it waited."""
    n = limit()
    if not n:
        return []
    run = running_by_project()
    waiting = {locks.ticket_group(t) for t in locks.queued(slot_dir(), RESOURCE)}
    k = len({locks.group_id(p) for p in run} | waiting | {locks.group_id(project_name)})
    return sorted(p for p, c in run.items() if p != (project_name or "") and c > locks.share_of(n, k))


def aged_before(now: float | None = None) -> float:
    """The arrival time at or before which a waiter has waited past starve_s()."""
    return (time.time() if now is None else now) - starve_s()


def starved_ahead(project_name: str | None, since: float | None = None, now: float | None = None,
                  priority: int | None = None, cost: dict | None = None, ticket: Path | None = None) -> list[Path]:
    """The other parked runs of `project_name` (PARKED tickets) that have waited starve_s() or longer, arrived
    before the asking run (its own PARKED ticket's arrival, else `since`, else now: a newcomer's) and go before
    it: those at its priority or a more urgent one, and a less urgent one only on its turn (_aged_turn: after
    aged_turns() more urgent starts), so urgent runs keep their precedence under a backlog. An owed or urgent
    asking run (locks.URGENT_PRIORITY or better) is never held by a less urgent one, unless aged_turns() is 0
    (checks.priority_turns 0: no urgent precedence). The asking run's priority is its PARKED ticket's, else
    `priority`; none (push checks) is the least urgent.
    A run of `cost` (checklane.cost) is not held by one at its priority or less urgent whose run costs
    CHEAP_FACTOR times as much (checklane.much_cheaper: a targeted run beside full suites), its run's lane
    label's cost by the run id in its ticket; a short one (is_small: shortest first) is held there only by
    short ones (locks.is_small), whatever the others' costs; a landing's ticket always holds it.
    With `ticket`, for that PARKED ticket's run instead of this thread's."""
    now = time.time() if now is None else now
    edge, gid = aged_before(now), locks.group_id(project_name)
    try:
        q = locks.queued(slot_dir(), PARKED)
    except OSError:
        return []
    mine = ticket if ticket is not None else getattr(_PARKS, "park", None)
    me = locks._arrival(mine) if mine in q else now if since is None else since
    rank = locks.ticket_priority(mine) if mine in q else math.inf if priority is None else float(priority)
    aged = [t for t in q if t != mine and locks.ticket_group(t) == gid and locks._arrival(t) <= edge
            and locks._arrival(t) < me]
    if cost and (costly := [t for t in aged if locks.ticket_priority(t) >= rank and not locks.is_landing(t)]):
        from . import checklane
        short = is_small(cost)
        costs = {} if short else checklane.run_costs(project_name)
        aged = [t for t in aged if t not in costly
                or (locks.is_small(t) if short else not checklane.much_cheaper(cost, costs.get(_ticket_run(t))))]
    out = [t for t in aged if locks.ticket_priority(t) <= rank]
    passed = [t for t in aged if t not in out] if rank > 0 else []
    if passed and rank <= locks.URGENT_PRIORITY:
        return out + (passed if aged_turns() <= 0 else [])
    return out + (_aged_turn(project_name, passed, now) if passed else [])


def owed_ahead(project_name: str | None, priority: int | None = None) -> list[Path]:
    """The other parked runs of `project_name` (PARKED tickets) at OBLIGATION_PRIORITY (an obligation owner's
    or a user-ordered urgent task's) that a less urgent asking run leaves its project's next free test slots
    to, whatever their arrival or its own wait: a freed slot goes to them by priority, never to an older
    parked run of lower priority that polls first. starved_ahead never holds them behind a less urgent run,
    so the two never wait on each other; aging still orders runs of one priority and lifts aged routine runs
    above other less urgent ones. The asking run's priority is its PARKED ticket's, else `priority`; push
    checks (none), a landing's ticket and priority 0 are held by none."""
    try:
        q = locks.queued(slot_dir(), PARKED)
    except OSError:
        return []
    mine = getattr(_PARKS, "park", None)
    rank = locks.ticket_priority(mine) if mine in q else math.inf if priority is None else float(priority)
    if rank <= OBLIGATION_PRIORITY or math.isinf(rank) or (mine in q and locks.is_landing(mine)):
        return []
    gid = locks.group_id(project_name)
    return [t for t in q if t != mine and locks.ticket_group(t) == gid
            and locks.ticket_priority(t) <= OBLIGATION_PRIORITY]


def _ticket_run(t: Path) -> str:
    """The run id a ticket's text names ("... (run 42)"), else ''."""
    try:
        m = re.search(r"\(run (\d+)", t.read_text())
    except OSError:
        return ""
    return m.group(1) if m else ""


def _aged_turn(project_name: str | None, aged: list[Path], now: float) -> list[Path]:
    """Those of `aged` (PARKED tickets of `project_name` past starve_s()) whose turn has come: since it aged
    and since its project last started a run at its priority or lower, aged_turns() or more runs of the
    project more urgent than it took a host-wide checks slot (grant_records' queue grants)."""
    n = aged_turns()
    if n <= 0:
        return aged
    s = starve_s()
    rows = [(r["ts"], float(r["priority"])) for r in grant_records(now, now - min(map(locks._arrival, aged)))
            if r.get("project") == project_name and r.get("slot") == "queue"
            and isinstance(r.get("priority"), int)]
    out = []
    for t in aged:
        p = locks.ticket_priority(t)
        start = max([locks._arrival(t) + s] + [ts for ts, pr in rows if pr >= p])
        if sum(1 for ts, pr in rows if pr < p and ts >= start) >= n:
            out.append(t)
    return out


def _loadavg() -> float:
    """The 1-minute load average (inf when unreadable): only a fallback, as it counts tasks waiting on I/O."""
    try:
        return os.getloadavg()[0]
    except OSError:
        return float("inf")


def _load() -> float:
    """The cores in use now as every pacing read counts them (the cap's default, extra slots, the test
    pool, repository lanes, the load guard): the runnable CPU demand measured over a short window
    (hostcpu.demand: /proc/stat on Linux, the kernel's CPU ticks on macOS), which leaves out tasks waiting
    on I/O; the 1-minute load average only where neither can be read (inf when nothing can)."""
    d = hostcpu.demand()
    return _loadavg() if d is None else d


def cpu_demand() -> float:
    """This host's runnable CPU demand in cores for the load guard (testslots.load_guard): _load()."""
    return _load()


SUSTAIN_S = 60.0   # the load guard and the live-cores gate count a CPU demand only once it lasted this long
SUSTAIN_ENV = "TTP_CHECKS_LOAD_SUSTAIN_S"
DEMAND_LOG = "cpu-demand.jsonl"   # check-slots/cpu-demand.jsonl: the demand readings of every run here (sustained_load)
DEMAND_EVERY_S = 5.0   # one reading logged per this many seconds at most
DEMAND_KEEP = 200   # lines kept once the log grows past twice this


def sustain_s() -> float:
    """TTP_CHECKS_LOAD_SUSTAIN_S, else the account's checks.load_sustain_s, else SUSTAIN_S; 0 reads the
    demand now alone."""
    for v in (os.environ.get(SUSTAIN_ENV), (project.load_account_settings().get("checks") or {}).get("load_sustain_s")):
        if v in (None, "") or isinstance(v, bool):
            continue
        try:
            return max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    return SUSTAIN_S


def _demand_log(now: float, d: float) -> list[tuple[float, float]]:
    """The readings logged over the last sustain_s() with `d` (read at `now`) among them: `d` is
    appended unless one was logged within DEMAND_EVERY_S. An unreadable log is empty."""
    path, keep = slot_dir() / DEMAND_LOG, sustain_s()
    got: list[tuple[float, float]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
            ts, v = float(r["ts"]), float(r["d"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if 0 <= now - ts <= keep and v == v:
            got.append((ts, v))
    if not got or now - max(ts for ts, _ in got) >= DEMAND_EVERY_S:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if len(lines) > 2 * DEMAND_KEEP:   # a lost line in a race only shortens the window
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text("".join(json.dumps({"ts": ts, "d": v}) + "\n" for ts, v in got[-DEMAND_KEEP:]))
                os.replace(tmp, path)
            with path.open("a") as f:
                f.write(json.dumps({"ts": now, "d": d}) + "\n")
        except OSError:
            pass
    return got + [(now, d)]


def sustained_load(now: float | None = None) -> float:
    """The CPU demand (cpu_demand()) that lasted: the lowest reading any run on this host logged over the
    last sustain_s() (DEMAND_LOG), once those readings span half of it at least; 0 before they do. So a
    burst of short-lived git, ssh or python processes, or a reading taken once, holds no check: only a
    demand high at every reading of the window does. inf (nothing readable) stays inf; sustain_s() 0 is
    cpu_demand() alone."""
    d = cpu_demand()
    if not (span := sustain_s()) or d != d or d == float("inf"):
        return d
    now = time.time() if now is None else now
    got = _demand_log(now, d)
    if now - min(ts for ts, _ in got) < span / 2:
        return 0.0
    return min(v for _, v in got)


POOL_DIRS_ENV = "TTP_CHECKS_POOL_DIRS"   # where pool runs' folders are looked for (os.pathsep-separated)
POOL_MAX_AGE_S = 720 * 60.0   # an older ttp-check.* folder is a leftover, not a running check (checkspill's bound)


def host_tag() -> str:
    """This machine's short tag (of /etc/machine-id, else its host name) in the folder name of a pool run
    that already holds one of its slots (remotecheck), so pool_runs never counts that run twice."""
    try:
        ident = Path("/etc/machine-id").read_text().strip()
    except OSError:
        ident = ""
    if not ident:
        import socket
        ident = socket.gethostname()
    return hashlib.sha256(ident.encode()).hexdigest()[:8]


def pool_dirs() -> list[Path]:
    if v := os.environ.get(POOL_DIRS_ENV):
        return [Path(x) for x in v.split(os.pathsep) if x]
    return list(dict.fromkeys(Path(x) for x in (os.environ.get("TMPDIR") or "/tmp", "/tmp")))


def pool_runs(now: float | None = None) -> list[Path]:
    """The check runs that run on this host for a `ttp checks` elsewhere, as a project's own pool, a spill
    host or a check host sends them (remotecheck: a ttp-check.* folder of this account, fresh within
    POOL_MAX_AGE_S). Each takes one of this host's slots (_room), so another project's pool queues fairly
    with the checks started here under the same cap and load guard. A run whose `ttp checks` holds a slot
    of this host already (its folder carries host_tag()) is counted once, by that slot."""
    now = time.time() if now is None else now
    mine, uid, out = f"ttp-check.{host_tag()}", os.getuid(), []
    for d in pool_dirs():
        try:
            found = list(d.glob("ttp-check.*"))
        except OSError:
            continue
        for f in found:
            try:
                st = f.stat()
            except OSError:
                continue
            if (f.is_dir() and st.st_uid == uid and now - st.st_mtime < POOL_MAX_AGE_S
                    and not f.name.startswith(mine)):
                out.append(f)
    return out + shard_runs()


SHARD_ENV = "TTP_CHECKS_SHARD"   # set for a shard's command: a shard started in it runs on its slot
SLOT_KEY_ENV = "TTP_CHECKS_SLOT_KEY"   # names the slot a run holds (Slot.env): its first shard runs on it


def shard_dir() -> Path:
    return slot_dir() / "shards"


def shard_runs() -> list[Path]:
    """The shards running here beyond the slot of the `ttp checks` that started them (shard): each counts
    as a pool run, so it takes one of this host's slots from the checks that wait."""
    try:
        found = sorted(shard_dir().glob("run.*"))
    except OSError:
        return []
    return [f for f in found if locks.slot_taken(f)[0]]   # a stale record on a free shard never counts


@contextlib.contextmanager
def shard(holder: str, what: str = "", tell: Callable[[str], None] | None = None,
          project_name: str | None = None, priority: int | None = None):
    """Run one shard of a project's own test pool (behind checks.wrap_cmd, or its local fallback) as a
    check of this host: yields the environment its command gets. Outside a `ttp checks` that holds a slot
    here (a shard started over ssh, or a pool run on its own) it takes a host checks slot under the same
    cap, queue and load guard as `ttp checks`. Inside one (HELD_ENV), its first shard runs on that slot and
    each further one at the same time counts as a pool run (shard_runs) without waiting, so a run that
    waits on its shards never waits on a slot it holds itself. A shard started in a shard runs in its place."""
    if os.environ.get(SHARD_ENV):
        yield {}
        return
    env = {SHARD_ENV: "1"}
    if not os.environ.get(HELD_ENV):
        slot = acquire(holder, what, tell, project_name=project_name, priority=priority)
        try:
            yield env | (slot.env() if slot else {})
        finally:
            if slot:
                slot.close()
        return
    key, f, mine = os.environ.get(SLOT_KEY_ENV, ""), None, None
    if key:   # a run of an older runtime names no slot: its shards run on its slot as before
        f = locks.try_take([shard_dir() / f"cover.{key}"], holder, what)
        for _ in range(5) if f is None else ():
            mine = shard_dir() / f"run.{os.getpid()}.{secrets.token_hex(4)}"
            if (f := locks.try_take([mine], holder, what)) is not None:
                break
            time.sleep(0.05)   # a counter (shard_runs) held the new file for an instant
    try:
        yield env
    finally:
        if mine is not None:
            mine.unlink(missing_ok=True)
        if f is not None:
            f.close()


def extra(n: int | None = None) -> int:
    """Slots open beyond the cap now: two while the CPU demand (_load()) is under a quarter of the
    cores, one under half, none above that or while memory is short (hostcpu.mem_short), with the cap off
    or with checks.load_extra 0."""
    top = extra_max()
    if not (limit() if n is None else n) or not top:
        return 0
    load, c = _load(), cores()
    if hostcpu.mem_short():
        return 0
    return min(top, 2 if load < c / 4 else 1 if load < c / 2 else 0)


class Cancelled(RuntimeError):
    """`stop` ended the wait: the checks' task ended while they waited (the reason is the message)."""


class Upgraded(Exception):
    """A waiting detached `ttp checks` found a newer runtime installed (upgraded(); the message is its
    version): it lets go of what it holds and re-executes itself with it (cli._checks_reexec)."""


class Reranked(Upgraded):
    """A waiting detached `ttp checks` whose task's priority changed (reranked; the message is its new
    priority): it re-executes as Upgraded does, with the same runtime, and queues again at that priority."""


_SEEN: dict = {}   # upgraded(): the installed version it last saw differ, and since when


def _tree(pkg: Path) -> str:
    """A fingerprint of the runtime package's code in `pkg` (each .py file's path, size and mtime), "" if
    unreadable: a fix that lands changes it without a version bump (upgraded)."""
    try:
        files = sorted(f for f in pkg.rglob("*.py") if "__pycache__" not in f.parts)
        return hashlib.sha256("\n".join(f"{f.relative_to(pkg)} {(st := f.stat()).st_size} {st.st_mtime_ns}"
                                         for f in files).encode()).hexdigest()[:12]
    except OSError:
        return ""


_TREE0: dict = {str(Path(__file__).parent): _tree(Path(__file__).parent)}   # the tree this process imported
# upgraded() reads the tree on disk at most this often per package (a waiter asks every second or less):
# the package's directory -> (when it last read it, what it read).
TREE_POLL_S = 15.0
_TREE_AT: dict = {}


def _tree_now(pkg: Path, now: float) -> str:
    """_tree(pkg), read again only once TREE_POLL_S passed since the last read (else that read's answer)."""
    at, tree = _TREE_AT.get(str(pkg), (None, ""))
    if at is None or abs(now - at) >= TREE_POLL_S:
        tree = _tree(pkg)
        _TREE_AT[str(pkg)] = (now, tree)
    return tree


def upgraded(init: Path | None = None, now: float | None = None, settle: float | None = None) -> str:
    """The runtime version now installed where this process imported its code from (`init`: that package's
    __init__.py), when it differs from the version it runs, or the same version with its code changed on
    disk since this process imported it (_tree, read at most every TREE_POLL_S: "<version>+<fingerprint>"),
    and has stayed for `settle` seconds (UPGRADE_SETTLE_S), else "". A version it re-executed for already (REEXEC_ENV) is never returned
    again, so a re-execution that did not take the new code cannot loop."""
    from . import __file__ as own_init, __version__
    now = time.time() if now is None else now
    init = Path(init or own_init)
    try:
        text = init.read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    new = m.group(1) if m else ""
    if new and new == __version__:
        if (was := _TREE0.get(str(init.parent))) is None:   # never read on each call (setdefault would)
            was = _TREE0[str(init.parent)] = _tree(init.parent)
        if was and (now_tree := _tree_now(init.parent, now)) and now_tree != was:
            new = f"{new}+{now_tree}"
    if not new or new == __version__ or new == os.environ.get(REEXEC_ENV):
        _SEEN.clear()
        return ""
    if _SEEN.get("version") != new:
        _SEEN.update(version=new, since=now)
    return new if now - _SEEN["since"] >= (UPGRADE_SETTLE_S if settle is None else settle) else ""


_IDENT: dict = locks.IDENTITY   # adopt_identity() (shared with locks: queue tickets record it): the project run a `ttp checks` with no TTP_TASK was started under


def inherited(pid: int, base: Path | None = None, depth: int = INHERIT_DEPTH) -> dict:
    """The task of the nearest of `pid` and its ancestors that names a project (TTP_PROJECT): with TTP_TASK set
    there and the project existing (and, with `base`, being that one), {"task", "run", "base", "project"}
    (project: its name). {} otherwise: that nearest project context decides, no further ancestor (procfs shows
    each process's starting environment, which can name a task its parent has since left). An unreadable
    process (another user's, no /proc) also ends the walk."""
    from . import jobuse, procowner
    for _ in range(depth):
        if pid <= 1 or (env := procowner.environ(pid)) is None:
            return {}
        if env.get("TTP_PROJECT"):
            there = procowner.base_of(env["TTP_PROJECT"])
            if not env.get("TTP_TASK") or not there or (base is not None and there != procowner.base_of(base)):
                return {}
            p = project.Project(there)
            return {"task": env["TTP_TASK"], "run": env.get("TTP_RUN_ID") or "", "base": str(there),
                    "project": p.name} if p.exists() else {}
        try:
            pid = int((jobuse._stat(pid) or ["", "0"])[1])
        except ValueError:
            return {}
    return {}


def adopt_identity(base: Path | None = None) -> dict:
    """For a `ttp checks` run with no TTP_TASK (a tool that cleared its environment, a temp clone outside the
    project): the task, run and project of the project run it was started under (inherited from its parent
    up; with `base`, its own project's only), which its slot label, priority and queue group then are
    (label, task_rank, queue_name). {} with its own TTP_TASK set or no such run: it queues as a run with no
    project, behind every project's waiter (fair_order's bare_last, bare_slots)."""
    _IDENT.clear()
    if not os.environ.get("TTP_TASK") and not os.environ.get(NO_INHERIT_ENV):
        _IDENT.update(_handed(base) or inherited(os.getppid(), base))
    return dict(_IDENT)


def adopted_env() -> dict:
    """The environment that hands the adopted identity to the background run a detached `ttp checks` starts
    (its parent, and with it the ancestor chain, ends)."""
    return {ADOPTED_ENV: json.dumps(_IDENT)} if _IDENT else {}


def _handed(base: Path | None) -> dict:
    """The identity adopted_env handed over, when its project still exists (and is `base`, with one)."""
    from . import procowner
    try:
        got = json.loads(os.environ.get(ADOPTED_ENV) or "{}")
    except ValueError:
        return {}
    there = procowner.base_of(got.get("base")) if isinstance(got, dict) and got.get("task") else None
    if not there or (base is not None and there != procowner.base_of(base)) or not project.Project(there).exists():
        return {}
    return {k: str(got.get(k) or "") for k in ("task", "run", "base", "project")}


def _task_run() -> tuple[str | None, str | None]:
    """The task and run this process runs for: TTP_TASK and TTP_RUN_ID, else the adopted ones (adopt_identity)."""
    if task := os.environ.get("TTP_TASK"):
        return task, os.environ.get("TTP_RUN_ID")
    return _IDENT.get("task"), _IDENT.get("run")


def queue_name(p) -> str | None:
    """The project a `ttp checks` run queues for host-wide slots as: its own (`p`), else the adopted one
    (adopt_identity); None: a run with no project."""
    return p.name if p else _IDENT.get("project")


def adopted_project():
    """The adopted project (adopt_identity) as a Project, for its task's priority (task_rank); None without."""
    return project.Project(_IDENT["base"]) if _IDENT.get("base") else None


def label(project_name: str | None, what: str = "") -> str:
    """Who holds or waits for a slot, as status lines show it: the project and the task's run."""
    task, run = _task_run()
    who = f"{project_name or 'no project'}" + (f" task #{task} (run {run or '?'})" if task else "")
    return who + requeued() + (f" {what}" if what else "")


def batch_id() -> str:
    """The push batch this process runs for (BATCH_ENV), "" outside one."""
    return (os.environ.get(BATCH_ENV) or "").strip()


_BATCH = re.compile(r"\bpush batch (\S+)")


def batch_of(rec) -> str:
    """The push batch a slot holder's record (its "batch" field) or a label (label(..., "push batch <id>"),
    as a batch labels its checks) names, "" for none."""
    if isinstance(rec, dict):
        if rec.get("batch"):
            return str(rec["batch"])
        rec = rec.get("holder") or ""
    m = _BATCH.search(str(rec or ""))
    return m.group(1).rstrip(",;:)") if m else ""


def owner(task, batch: str = "") -> str:
    """Who a checks run is for, in status words: "#<task>", else "push batch <id>" (a push batch's checks
    have no task by design), else "a run with no task"."""
    return f"#{task}" if task else f"push batch {batch}" if batch else "a run with no task"


def push_rows(now: float | None = None) -> list[dict]:
    """The push batches' checks host-wide, for `ttp checks --slot-status`: each holder of a push priority
    slot (slot "push", state "holding"), each waiter for one (PUSH_WAIT, state "waiting") and each push
    batch waiting for an ordinary turn (slot "queue"), with its batch id (batch_of; None for a push that is
    not a batch, such as `ttp push`), project, label, pid and age. Read only; unreadable gives none."""
    now = time.time() if now is None else now
    out = []
    try:
        for r in locks.holder_records(priority_paths()):
            out.append({"slot": "push", "state": "holding", "batch": batch_of(r) or None,
                        "project": r.get("project") or _project(str(r.get("holder") or "")),
                        "label": str(r.get("holder") or ""), "pid": r.get("pid"),
                        "age_s": round(now - float(r.get("since") or now), 1)})
        for slot, queue in (("push", PUSH_WAIT), ("queue", RESOURCE), ("queue", PARKED)):
            for t in locks.queued(slot_dir(), queue):
                try:
                    text = t.read_text().strip()
                except OSError:
                    continue
                if slot == "queue" and not batch_of(text):
                    continue
                m = re.match(r"\d+(?:\.\d+)?-(\d+)", t.name)
                out.append({"slot": slot, "state": "waiting", "batch": batch_of(text) or None,
                            "project": _project(text), "label": text, "pid": int(m.group(1)) if m else None,
                            "age_s": round(now - locks._arrival(t), 1)})
    except OSError:
        return out
    return out


def push_lines(now: float | None = None, rows: list[dict] | None = None) -> list[str]:
    """push_rows (or `rows`, its answer) as lines for a person."""
    now = time.time() if now is None else now
    return [f"{'holder' if r['state'] == 'holding' else 'waiter'} ({r['slot']} slot): "
            f"{owner(None, r['batch']) if r['batch'] else r['label'] or 'a push'}"
            + (f" of {r['project']}" if r["project"] and r["batch"] else "")
            + f", {'held' if r['state'] == 'holding' else 'waited'} {_dur(r['age_s'])}"
            + (f", pid {r['pid']}" if r["pid"] else "") for r in (push_rows(now) if rows is None else rows)]


def requeued() -> str:
    """What label() adds while a preempted run queues again (PREEMPTED_ENV), "" otherwise. The slot it takes
    back is labelled without it (acquire), and its run drops PREEMPTED_ENV once it holds one (cli)."""
    by = os.environ.get(PREEMPTED_ENV)
    return f" [requeued after preemption by {by}]" if by else ""


def task_priority(p) -> int | None:
    """The priority of the task this process runs for (TTP_TASK) in project `p`; None without one.
    OBLIGATION_PRIORITY while the task is a required owner of an open obligation."""
    return task_rank(p)[0]


def task_rank(p) -> tuple[int | None, list[str]]:
    """(task_priority(p), the ids of the open obligations behind an OBLIGATION_PRIORITY boost, else []).
    An urgent task (urgent.is_urgent) ranks at least urgent.PRIORITY, a user-ordered one (a fresh `urgent:`
    line) urgent.ORDERED_PRIORITY, as an obligation owner."""
    task = _task_run()[0] or ""
    if p is None or not task.isdigit():
        return None, []
    try:
        t = p.db.task(int(task))
        prio = int(t["priority"]) if t and t.get("priority") is not None else None
        if t and (q := urgent.queue_priority(t)) is not None:   # a user-ordered one ORDERED_PRIORITY
            prio = q if prio is None else min(prio, q)
    except Exception:   # an unreadable database only loses the ordering, never the slot
        return None, []
    try:
        owned = list(obligations.owned_open(p.db, int(task))) if t else []
    except Exception:   # unreadable obligations only lose the boost
        return prio, []
    return (OBLIGATION_PRIORITY, owned) if owned else (prio, [])


def paths(n: int | None = None) -> list[Path]:
    return locks.slot_paths(slot_dir(), RESOURCE, limit() if n is None else n)


def all_paths(n: int | None = None) -> list[Path]:
    """The cap's slots and then the ones load may open beyond it (extra()), held or not."""
    n = limit() if n is None else n
    return paths(max(n, _span()) + extra_max()) if n else []


def priority_path(key: str) -> Path:
    """The priority slot of one push branch; `key` names the branch host-wide (push.priority_key)."""
    return slot_dir() / f"{PRIORITY}.{hashlib.sha256(key.encode()).hexdigest()[:16]}.lock"


def priority_paths() -> list[Path]:
    try:
        return sorted(slot_dir().glob(f"{PRIORITY}.*.lock"))
    except OSError:
        return []


def host_holders(n: int | None = None) -> list[str]:
    """The labels of every check run holding a host-wide checks slot right now, each run once: the cap's
    slots and the ones load opened beyond it, the push priority slots, the urgent slots and the shared
    holders. A run seen on several slots (same holder, pid and since) counts once; a taken slot whose label
    does not read counts as "". The one reading the queue-wait report (state's "held") and the slot status
    (hostload) count, so the two never disagree."""
    n = limit() if n is None else n
    if not n:
        return []
    every, seen, out = all_paths(n), set(), []
    for path in every + priority_paths() + urgent_paths():
        if not locks._slot_labels([path]):
            continue
        try:
            h = locks.read_label(path)
        except (OSError, ValueError):
            h = {}
        k = (h.get("holder"), h.get("pid"), h.get("since")) if h else ("unread", path.name)
        if k not in seen:
            seen.add(k)
            out.append(str(h.get("holder") or ""))
    for h in locks.shared_holders(every):
        if (k := (h.get("holder"), h.get("pid"), h.get("since"))) not in seen:
            seen.add(k)
            out.append(str(h.get("holder") or ""))
    return out


def state() -> dict:
    """Who runs checks host-wide now and who waits: {"limit", "running": [labels on cap slots], "queued": [labels],
    "priority": [labels of push checks running on their priority slots], "priority_waiting": [those holding one
    while they wait for room in the cap], "urgent": [labels on reserved urgent slots], "total": every run
    running, "beyond": whether push and urgent runs run beyond the cap (priority_beyond_cap), "held": [every
    slot holder once] (host_holders: what reports count as running)}."""
    n = limit()
    running = locks.held_labels(all_paths(n)) if n else []
    recs = locks.holder_records(priority_paths()) if n else []
    priority = [str(r.get("holder") or "") for r in recs if not r.get("waiting")]
    held_waiting = [str(r.get("holder") or "") for r in recs if r.get("waiting")]
    since = [float(r.get("since") or 0) for r in recs if not r.get("waiting")]
    urgent_held = locks.held_labels(urgent_paths()) if n else []
    queued, ahead, groups, arrived = [], {}, set(), []
    for t in locks.fair_order(slot_dir(), RESOURCE, **order()):
        try:
            queued.append(t.read_text().strip())
        except OSError:
            continue
        arrived.append(locks._arrival(t))
        if (g := locks.ticket_group(t) or t.name) not in groups:   # each project's next waiter
            groups.add(g)
            ahead[_project(queued[-1])] = len(queued) - 1
    parked = []
    for t in locks.queued(slot_dir(), PARKED) if n else []:
        try:
            parked.append((t.read_text().strip(), locks._arrival(t)))
        except OSError:
            continue
    return {"limit": n, "extra": extra(n), "running": running, "queued": queued, "priority": priority,
            "ahead": ahead, "queued_at": arrived, "priority_since": since, "parked": parked,
            "pool": len(pool_runs()) if n else 0, "urgent": urgent_held, "priority_waiting": held_waiting,
            "total": len(running) + len(priority) + len(urgent_held), "beyond": priority_beyond_cap(),
            "held": host_holders(n)}


def _project(holder: str) -> str:
    """The project a holder label names (label())."""
    return holder.split(" task #", 1)[0].split(" ttp checks", 1)[0].split(" push batch ", 1)[0].split(" ttp push", 1)[0]


def idle_hold_s() -> float:
    """Seconds a host-wide slot is held with no test running before status flags it (holder_activity):
    TTP_CHECKS_IDLE_HOLD_S, else the account's checks.idle_hold_s, else IDLE_HOLD_S. Zero, negative,
    infinite or unreadable: the default."""
    try:
        v = float(os.environ.get(IDLE_HOLD_ENV)
                  or (project.load_account_settings().get("checks") or {}).get("idle_hold_s") or IDLE_HOLD_S)
    except (TypeError, ValueError):
        v = IDLE_HOLD_S
    return v if 0 < v < math.inf else IDLE_HOLD_S


def tests_cpu_s(pid: int) -> float | None:
    """The CPU seconds of what process `pid` started (its live descendants', with every reaped child's), not its
    own: the checks process's commands. None when it cannot be read (gone, another PID namespace, no /proc)."""
    from . import jobuse
    tree, own = jobuse._tree(pid), jobuse._stat(pid)
    if not tree or not own:
        return None
    hz = jobuse._hz()
    try:
        reaped = sum(int(x) for x in own[13:15]) / hz
    except (ValueError, IndexError):
        return None
    return reaped + sum(jobuse._cpu(x, hz) for x in tree[1:])


def holder_activity(now: float | None = None, cpu: Callable[[int], float | None] | None = None,
                    sample_s: float = ACTIVE_SAMPLE_S) -> list[dict]:
    """Each holder of a host-wide checks slot (the cap's and the ones load opened beyond it) with whether its
    process tree runs tests now: {"holder", "pid", "held_s", "tests", "idle"}. `tests`: what it started used
    ACTIVE_FRAC of one core or more over `sample_s` (tests_cpu_s read twice), None when that cannot be read;
    `idle`: no test running and held idle_hold_s() or longer, so status flags it. Status only: it stops,
    preempts or asks nothing (accepted gap: one sample, so a suite between steps may read idle)."""
    n = limit()
    recs = [r for r in locks.holder_records(all_paths(n)) if not r.get("shared")] if n else []
    if not recs:
        return []
    cpu = cpu or tests_cpu_s
    pids = [r.get("pid") if isinstance(r.get("pid"), int) else None for r in recs]
    first = [cpu(p) if p else None for p in pids]
    if sample_s > 0 and any(x is not None for x in first):
        time.sleep(sample_s)
    now = time.time() if now is None else now
    bound, out = idle_hold_s(), []
    for r, p, a in zip(recs, pids, first):
        b = cpu(p) if p and a is not None else None
        # a drop: a process left the tree (an orphan of its commands), so they ran
        tests = None if a is None or b is None else b < a or b - a >= max(ACTIVE_FRAC * sample_s, 1e-9)
        held = max(0.0, now - float(r.get("since") or now))
        out.append({"holder": str(r.get("holder") or ""), "pid": p, "held_s": held, "tests": tests,
                    "idle": tests is False and held >= bound})
    return out


def activity_text(a: dict) -> str:
    """One holder of holder_activity, as status says it."""
    what = {True: "tests running", False: "no test running", None: "its processes cannot be read"}[a["tests"]]
    out = f"{a['holder']}: {what}, held {_dur(a['held_s'])}"
    return out + (f"; IDLE: it holds one of the host's checks slots with no test running (over "
                  f"{_dur(idle_hold_s())}); a check that waits on something else gives the slot up meanwhile "
                  "with `ttp checks-yield`" if a["idle"] else "")


PARKED_SHOWN = 5   # parked runs slot status lists (parked_order), in admission order
_LABEL_WHO = re.compile(r"^(?P<project>.*?) task #(?P<task>\d+) \(run (?P<run>[^)]*)\)")


def _pid_identity(pid: int) -> dict | None:
    """The project, task and run live process `pid` checks for, from its own environment: the task it was
    handed with a detached run (ADOPTED_ENV), else inherited's. None when its environment cannot be read
    (gone, another user's, no /proc)."""
    from . import procowner
    env = procowner.environ(pid)
    if env is None:
        return None
    if not env.get("TTP_TASK") and env.get(ADOPTED_ENV):
        try:
            got = json.loads(env[ADOPTED_ENV])
        except ValueError:
            got = {}
        if isinstance(got, dict) and got.get("task"):
            return {k: str(got.get(k) or "") for k in ("project", "task", "run")}
    got = inherited(pid)
    if got:
        return {k: got[k] for k in ("project", "task", "run")}
    there = procowner.base_of(env["TTP_PROJECT"]) if env.get("TTP_PROJECT") else None
    return {"project": there.parent.name if there else "", "task": "", "run": ""}


def _label_identity(holder: str) -> dict:
    """The project, task and run a slot label (label()) names."""
    m = _LABEL_WHO.match(holder or "")
    if m:
        return {"project": m["project"], "task": m["task"], "run": m["run"]}
    return {"project": _project(holder or ""), "task": "", "run": ""}


def slot_holders(read: Callable[[], list[str]] | None = None) -> list[dict]:
    """Each held host-wide checks slot file (the cap's checks.N.lock and the ones load opened beyond it, the
    push priority and the urgent slots) with who holds it now: {"slot", "kind" (cap/extra/push/urgent),
    "project", "task", "run", "pid", "alive", "source"}. A slot is held while its flock is taken; a released
    slot whose file still keeps its last holder's JSON is not listed. The pid is the live flock holder's
    (locks.flock_pids, `read` for its table) and the project, task and run that process's own environment
    names (source "process"). Where that does not read, the file's label serves only when it names that same
    pid (source "label"); with no kernel lock table (macOS) the label is all there is (source "label,
    unchecked"). Status only: it takes, stops or asks nothing."""
    n = limit()
    if not n:
        return []
    cap = set(paths(n))
    every = ([(p, "cap" if p in cap else "extra") for p in all_paths(n)]
             + [(p, "push") for p in priority_paths()] + [(p, "urgent") for p in urgent_paths()])
    taken = [(p, k) for p, k in every if locks._slot_labels([p])]
    if not taken:
        return []
    live = locks.flock_pids([p for p, _ in taken], read)
    out = []
    for path, kind in taken:
        try:
            rec = locks.read_label(path)
        except (OSError, ValueError):
            rec = {}
        mine = rec.get("pid") if isinstance(rec.get("pid"), int) else None
        if live is None:
            pids, unchecked = [mine], True
        else:
            pids, unchecked = live.get(path) or [], False
            if not pids and not locks._slot_labels([path]):
                continue   # released while it was read
        for pid in pids or [None]:
            who, source = (_pid_identity(pid) if pid and not unchecked else None), "process"
            if not who:
                if unchecked or (pid and pid == mine):
                    who, source = _label_identity(str(rec.get("holder") or "")), "label" + (", unchecked" if unchecked else "")
                else:
                    who, source = {"project": "", "task": "", "run": ""}, "unread"
            out.append({"slot": path.name, "kind": kind, **who, "pid": pid,
                        "alive": locks._alive(pid) if pid else None, "source": source})
    return out


def parked_order(first: int = PARKED_SHOWN) -> dict:
    """The host's parked runs (PARKED: waiting outside the checks queue for their test slots) in the order the
    queue admits them (fair_order with order()): {"parked": the first `first` as {"holder", "priority",
    "owed", "tags", "since"}, "total": how many are parked}. Tags: owed (OBLIGATION_PRIORITY), p<priority>,
    landing, small, requeued. Status only."""
    try:
        q = locks.fair_order(slot_dir(), PARKED, **order())
    except OSError:
        q = []
    out = []
    for t in q[:first]:
        try:
            text = t.read_text().strip()
        except OSError:
            continue
        prio = locks.ticket_priority(t)
        owed = prio <= OBLIGATION_PRIORITY
        tags = ((["owed"] if owed else []) + ([f"p{int(prio)}"] if not math.isinf(prio) else [])
                + [name for name, on in (("landing", locks.is_landing(t)), ("small", locks.is_small(t)),
                                         ("requeued", locks.is_requeued(t))) if on])
        out.append({"holder": text.replace(OBLIGATION_MARK, ""), "priority": None if math.isinf(prio) else int(prio),
                    "owed": owed, "tags": tags, "since": locks._arrival(t)})
    return {"parked": out, "total": len(q)}


def slot_lines(held: list[dict], parked: dict, now: float | None = None) -> list[str]:
    """The "host slots" section of the slot status: slot_holders' and parked_order's entries."""
    now = time.time() if now is None else now
    out = ["host slots (held now, by the live lock holder):"]
    for h in held:
        who = (f"{h['project'] or 'no project'}" + (f" task #{h['task']} (run {h['run'] or '?'})" if h["task"] else "")
               if h["source"] != "unread" else "holder's process cannot be read")
        alive = {True: "alive", False: "exited (a child may keep its descriptor)", None: "pid unknown"}[h["alive"]]
        pid = f"pid {h['pid']} {alive}" if h["pid"] else alive
        out.append(f"  {h['slot']} ({h['kind']}): {who}, {pid}"
                   + (f" [from {h['source']}]" if h["source"] not in ("process", "unread") else ""))
    if not held:
        out.append("  none held")
    n, shown = parked["total"], len(parked["parked"])
    out.append(f"host parked queue (admission order{f', first {shown} of {n}' if n > shown else ''}):")
    for i, t in enumerate(parked["parked"], 1):
        tags = f" [{', '.join(t['tags'])}]" if t["tags"] else ""
        out.append(f"  {i}. {t['holder']}{tags}, parked {_dur(max(0.0, now - t['since']))}")
    if not parked["parked"]:
        out.append("  none parked")
    return out


def line(activity: bool = False) -> str:
    """One status line, or "" when nothing runs or waits. With `activity` it also names each slot holder that
    has held its slot idle_hold_s() or longer with no test running (holder_activity)."""
    s = state()
    waiting = s.get("priority_waiting") or []
    if not s["running"] and not s["queued"] and not s["priority"] and not s["parked"] and not s["urgent"] \
            and not waiting:
        return ""
    now = time.time()
    cap = (f"cap {s['limit']}" + (f" +{s['extra']} while the load is low" if s["extra"] else "")
           if s["limit"] else "no cap")
    total = s.get("total", len(s["running"]) + len(s["priority"]) + len(s["urgent"]))
    incl = ", ".join(f"{k} {len(s[v])}" for k, v in (("push", "priority"), ("urgent", "urgent")) if s[v])
    beyond = " beyond the cap" if s.get("beyond") and incl else ""
    out = f"host checks: {total} running of {cap}" + (f" (incl. {incl}{beyond})" if incl else "")
    if s["priority"]:
        out += ", push priority slot: " + _some([_aged(h, t, now, "held") for h, t in
                                                 zip(s["priority"], s["priority_since"] or [0.0] * len(s["priority"]))])
    if waiting:
        out += f", push checks next for a slot: {_some(waiting)}"
    if s["urgent"]:
        out += f", urgent slot{beyond}: {_some(s['urgent'])}"
    if s["queued"]:
        out += f", {len(s['queued'])} queued: " + _some([_aged(h, t, now) for h, t in zip(s["queued"], s["queued_at"])])
    if s["parked"]:
        out += (f", {len(s['parked'])} waiting outside the queue for test slots: "
                + _some([_aged(h, t, now) for h, t in s["parked"]]))
    if s["running"]:
        out += f" (running: {_some(s['running'])})"
    if activity and s["running"] and (idle := [a for a in holder_activity(now) if a["idle"]]):
        out += ("; holding a slot with no test running: " + _some([f"{a['holder']} (held {_dur(a['held_s'])})"
                                                                     for a in idle])
                + " (a check waiting on something else gives its slot up with `ttp checks-yield`)")
    if s["ahead"]:
        out += "; next per project: " + ", ".join(f"{k} {v} ahead" for k, v in list(s["ahead"].items())[:6])
    return out


def _aged(holder: str, at: float, now: float, verb: str = "waited") -> str:
    """'holder (waited 12.0 min)', from a minute on; the bare holder before that or with no time."""
    return f"{holder} ({verb} {_dur(now - at)})" if at and now - at >= 60 else holder


def _some(labels: list[str]) -> str:
    return "; ".join(labels[:4]) + ("; ..." if len(labels) > 4 else "")


class Slot:
    """A held slot (or none: cap off or nested); `close()` releases it. `served`: none is held because
    `done()` ended the wait (acquire): the caller reads the recorded pass, never runs the checks on it."""

    def __init__(self, f=None, served: bool = False, again: dict | None = None):
        self.f = f
        self.served = served
        self.again = again   # acquire's arguments that took it, to take one again after a yield (hold_again)
        self.yield_stem = ""   # its commands may ask it to give the slot up (allow_yield, YIELD_ENV)
        self.yielded = False   # given up while its commands wait on an outside pool (serve_yield)
        self.waiters: set[int] = set()   # the processes that asked it to: once all died, the yield ends
        self.tests = None   # the run's test slots (testslots.Grant), held and freed with this slot
        self.spill = None   # a claim on a spill host the checks run on instead (checkspill.Claim)
        self.pinned: list = []   # claims on the hosts its checks are pinned to (checkspill.admit_pinned)
        self.urgent = False   # a reserved urgent slot beyond the cap (acquire's `urgent`, urgent_key)
        self.since = time.time()   # when it was granted: an older preempt request is not for it (preempted)
        self.fell_back = False   # a push's ordinary turn, taken after its priority slot stayed held (acquire_priority)

    def env(self) -> dict[str, str]:
        """For the check commands' environment: a `ttp checks` or push nested in them runs at once."""
        return (({HELD_ENV: "1", SLOT_KEY_ENV: self.key()} if self.f is not None else {})
                | ({YIELD_ENV: self.yield_stem} if self.yield_stem else {})
                | (self.tests.env() if self.tests else {}))

    def key(self) -> str:
        """This slot's name for its shards (shard): its holder's pid and slot file, kept across exec."""
        return f"{os.getpid()}.{Path(self.f.name).name}" if self.f is not None else ""

    def close(self) -> None:
        self.let_go()
        if self.yield_stem:
            for f in Path(self.yield_stem).parent.glob(Path(self.yield_stem).name + ".*"):
                f.unlink(missing_ok=True)   # its commands' requests and answers (ask_yield)
            self.yield_stem = ""
        if self.tests is not None:
            self.tests.close()
            self.tests = None
        if self.spill is not None:
            self.spill.close()
            self.spill = None
        for c in self.pinned:
            c.close()
        self.pinned = []

    def let_go(self) -> None:
        """Free the host-wide slot alone (its test slots and spill claim stay); a second call frees nothing."""
        if self.f is not None:
            (shard_dir() / f"cover.{self.key()}").unlink(missing_ok=True)
            self.f.close()
            self.f = None

    def allow_yield(self) -> bool:
        """Let its commands give the slot up while they wait (YIELD_ENV in env()): a slot of the cap
        acquire took, never a nested run's, an urgent one or one whose checks run on a spill host."""
        if self.f is None or self.again is None or self.urgent or self.spill is not None:
            return False
        self.yield_stem = self.yield_stem or str(slot_dir() / f"{YIELD}.{os.getpid()}.{secrets.token_hex(4)}")
        return True

    def hold_again(self, tell: Callable[[str], None] | None = None, stop: Callable[[], str] | None = None) -> bool:
        """After a yield, queue for a host-wide slot again in the normal order (a new arrival, its priority
        and project) and hold it; False when `stop()` gave a reason first (then it holds none)."""
        if not self.yielded:
            return True
        try:
            got = acquire(**(self.again or {}), tell=tell, stop=stop)
        except Cancelled:
            return False
        self.f, self.since, self.yielded, self.waiters = got.f if got else None, time.time(), False, set()
        if got is not None:
            got.f = None
        return True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def keep_slot(slot) -> str:
    """The host-wide checks slot `slot` holds, made to survive this process's exec (cli._checks_reexec):
    its open file becomes inheritable and the returned text (for KEPT_ENV) names it for adopt_slot, else "".
    The slot's lock belongs to that open file and its label to this pid, which exec keeps, so no other
    waiter can take it in between; a failed exec or a dead process frees it as any holder's."""
    f = getattr(slot, "f", None)
    if f is None:
        return ""
    try:
        os.set_inheritable(f.fileno(), True)
        return json.dumps([f.fileno(), f.name])
    except (OSError, ValueError):
        return ""


def adopt_slot(spec: str) -> Slot | None:
    """The Slot keep_slot() named in `spec`, taken over after the exec (no longer inherited by the
    commands it starts); None when there is none or the fd is not the open, still locked slot file it
    names (then a matching fd is closed, freeing the slot: the run queues for one again)."""
    try:
        fd, path = json.loads(spec or "null") or (None, None)
        fd, path = int(fd), str(path)
    except (ValueError, TypeError):
        return None
    try:
        st, own = os.fstat(fd), os.stat(path)
    except (OSError, ValueError):
        return None
    if (st.st_dev, st.st_ino) != (own.st_dev, own.st_ino):
        return None   # an fd that is no longer the slot's (exec closed it, the number was reused): not ours
    f = None
    try:
        f = os.fdopen(fd, "a+")
        f.buffer.raw.name = path   # named as try_take's are
        os.set_inheritable(fd, False)
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # its own lock: taken again at once
    except (OSError, ValueError):
        if f is not None:
            f.close()
        return None
    return Slot(f)


@project.ps_snapshot()
def acquire(holder: str, what: str = "", tell: Callable[[str], None] | None = None,
            waiting: Callable[[bool], None] | None = None, max_wait_s: float | None = None,
            since: float | None = None, project_name: str | None = None,
            priority: int | None = None, same: str = "", done: Callable[[], bool] | None = None,
            stop: Callable[[], str] | None = None, state: Path | None = None,
            owns: list[str] | tuple = (), cap: int | None = None, lane_at: float | None = None,
            startable: Callable[[], bool] | None = None,
            parks: Callable[[bool], None] | None = None, urgent: bool = False,
            preemptible: bool = False, small: bool = False, progress: str = "",
            why: Callable[[], str] | None = None, waited: dict | None = None) -> Slot | None:
    """Take a host-wide checks slot, waiting while all are held. Waiters take turns by project
    (`project_name`; none form their own group), within one by task priority (`priority`, 1 first;
    OBLIGATION_PRIORITY before it, its ticket marked OBLIGATION_MARK; none last), then arrival (locks.fair_order),
    priority 1 or better ahead of the others for a bounded run of turns (priority_turns()), so one
    project's burst cannot starve the others' checks. `tell` gets a line
    when the wait starts and every TELL_S while it lasts; `waiting(True)` once it starts and
    `waiting(False)` once it ends. With `max_wait_s` it gives its place back and returns None once
    it waited that long. `since` is the arrival time to queue at (default now). A slot directory that
    cannot be written raises Unavailable: the checks never run uncapped.

    `same` keys these checks (project, tree, commands, purpose): while checks with the same key hold a
    slot it waits for them outside the queue, keeping `since`, and queues again once they end. `done()`
    true (their pass was recorded) ends any wait with no slot held, Slot(served=True); a reason from `stop()`
    ends it with Cancelled. Slots beyond the cap open while the host is lightly loaded (extra()).
    With `state` (a project's state directory) each slot taken is logged there (record_wait); every one is
    logged host-wide (record_grant); `cap` (the project cap the run's lane was held under) goes in
    the project's record, with `waited` (record_wait's lane_s and pool_s). `owns`: the ids behind a boost to OBLIGATION_PRIORITY (task_rank).
    A slot taken more than GRANT_TELL_S after `since` is told with its priority and arrival (grant_line),
    also when a relaunch that inherited `since` takes it at once. While `startable()` is false (the
    run's test slots could not be taken now: testslots.pool_room) it waits outside the queue as for
    `same`, keeping `since`, so it holds no slot that only waits and the next project's waiter takes it;
    `parks(True)` once it parks so and `parks(False)` once it leaves that wait (cli._checks_slot_take
    relabels its lane: testslots.PARKED_WAITS). With `urgent` an urgent run (is_urgent(`priority`)) that would
    wait takes a free reserved urgent slot instead (urgent_paths; Slot.urgent), parked or not, unless the same
    checks run now; with none free it asks the lowest-priority preemptible routine run on a slot of the cap
    to give its slot up (preempt), once a wait, and then takes the first one that frees, in turn or not:
    the freed slot is its own (its claim) and it looks every CLAIM_POLL_S, so it starts within a second of the
    asked run letting go. Asked while parked, it keeps the claim parked; still parked HANDOVER_S after that run
    let go, it drops it and the slot goes to the next waiter. Once its ask lapsed or was dropped, it may ask
    again preempt_after_s() later (never while its earlier request lives).
    `preemptible`: the caller stops its checks when asked (preempted), so its slot's label says it may be
    asked; Slot.since is when it was granted. It asks only once it waited preempt_after_s() from `since`.
    `small` (is_small): its queue and PARKED tickets mark a short run (locks.SMALL_TICKET), which goes first
    among its project's waiters at its priority; the cap and every other rule hold it as any run.
    `progress`: the log of a preemptible run that cannot resume from its passed files, so preempt never asks
    it once it is past SHIELD_PCT."""
    n = limit()
    was_preempted = bool(os.environ.get(PREEMPTED_ENV))   # preempted once: queues at its place, asked again last
    label = {"priority": priority, "checks_project": project_name, "owed": bool(owns),
             **({"preemptible": True} if preemptible else {}), **({"preempted": True} if was_preempted else {}),
             **({"progress_log": progress} if preemptible and progress else {})}
    again = {"holder": holder, "what": what, "project_name": project_name, "priority": priority, "owns": owns,
             "cap": cap, "preemptible": preemptible, "progress": progress}
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    since = time.time() if since is None else since
    held_as = holder.replace(requeued(), "") if requeued() else holder   # a slot it holds is no requeue
    asked, claim = _asked(since), _asked(since, "claim")   # one request asks once, over a re-execution too
    parked_ask = bool(_asked(since, "parked"))   # it asked while parked: it waits parked for that run's slots
    waiting = checkwait.chain(waiting, "host-wide checks slot")   # inside a push check: not its run time
    ps, every = paths(n), all_paths(n)
    queued_as = holder + (OBLIGATION_MARK if priority is not None and priority < 1 else "")
    try:
        twin = _running(every, same)   # before its gate: a twin waiter takes no load pass (_drop_pass)
        parked = not twin and bool(startable and not startable())
        if twin:
            _drop_pass()
        f = (None if parked or twin or locks.queued(slot_dir(), RESOURCE)
             else _take(n, ps, every, held_as, what, same, label=label, mine=claim))
        if f:
            drop_claim(claim)
            locks.mark_served(slot_dir(), RESOURCE, project_name)
            locks.mark_turn(slot_dir(), RESOURCE, None)
            _granted(tell, state, project_name, priority, owns, since, cap=cap, lane_at=lane_at, waited=waited)
            return Slot(f, again=again)
        if f := _urgent_take(urgent, priority, every, held_as, what, same, owns, rank=_urgent_rank(owns, since),
                             n=n, claim=claim):
            drop_claim(claim)
            return _urgent_granted(f, tell, state, project_name, priority, owns, since, cap, lane_at, waited)
        ticket = None if parked else locks.enqueue(slot_dir(), RESOURCE, queued_as, since, group=project_name,
                                                   priority=priority, requeued=was_preempted, small=small)
        uwait = _urgent_wait(urgent and is_urgent(priority) and not twin, None, queued_as, since, owns)
        said = ""   # the limit last told while parked (parked_line): told again when another one holds it
        if parked and tell:   # told once a wait: its pool's room may come and go
            said = _tell_parked(tell, project_name, why)
        park = _park(parked, None, queued_as, since, project_name, priority, small)
        if parked and parks:
            parks(True)
    except OSError as e:
        raise _unavailable(e) from e
    started, told, checked, parked_told, lapsed = time.time(), 0.0, time.time(), parked, 0.0
    try:
        while True:
            if done and ((ticket is None and not parked) or time.time() - checked >= DONE_S):
                checked = time.time()
                if done():
                    return Slot(served=True)
            if stop and (why := stop()):
                raise Cancelled(why)
            twin = _running(every, same)
            was, parked = parked, not twin and bool(startable and not startable())
            if twin:
                _drop_pass()
            try:
                park = _park(parked, park, queued_as, since, project_name, priority, small)
                uwait = _urgent_wait(urgent and is_urgent(priority) and not twin, uwait, queued_as, since, owns)
            except OSError as e:
                raise _unavailable(e) from e
            if parks and parked != was:
                parks(parked)
            if (twin or parked) and ticket is not None:
                # Same checks run now: wait for their result instead of a place a slot frees to. Its test
                # slots cannot be taken now: a slot would only wait for them, so the next waiter takes it.
                locks.dequeue(ticket)
                ticket = None
                if tell and (twin or not parked_told):
                    if twin:
                        tell(f"the same checks run now ({twin}); waiting for their result, keeping this place in the queue")
                    else:
                        said = _tell_parked(tell, project_name, why)
                parked_told = parked_told or parked
            elif tell and parked and parked_told and _limit_name(now_why := _why(why)) != _limit_name(said):
                said = _tell_parked(tell, project_name, why, now_why)   # another limit holds it now: no silent throttling
            elif not twin and not parked and ticket is None:
                try:
                    ticket = locks.enqueue(slot_dir(), RESOURCE, queued_as, since, group=project_name,
                                           priority=priority, requeued=was_preempted, small=small)
                except OSError as e:
                    raise _unavailable(e) from e
                if done and done():
                    return Slot(served=True)
            f = None
            if ticket is not None:
                n = limit() or n   # a default cap follows the load (default_limit())
                ps = paths(n)
                try:
                    f = _take_in_turn(n, ticket, ps, every, held_as, what, same, label, mine=claim)
                except OSError:
                    f = None
            if not f and not twin and (f := _urgent_take(urgent, priority, every, held_as, what, same, owns,
                                                         mine=uwait, n=n, claim=claim)):
                return _urgent_granted(f, tell, state, project_name, priority, owns, since, cap, lane_at, waited)
            if not f and asked and not twin and not parked:   # the slot a preempted run gave up is this run's
                f = _take(n, paths(n), every, held_as, what, same, label=label, mine=claim)
            if claim and not f:
                # Asked while parked, it keeps its claim parked until the asked run lets go; still unable to
                # start HANDOVER_S after that, it drops it, so the freed slot goes to the next waiter now.
                renew_claim(claim, parked, parked_ask)
                if parked and (gone := claim_freed_s(claim)) is not None and gone >= HANDOVER_S:
                    drop_claim(claim)
                    claim = ""
            if not f and asked and not claim_live(claim):
                # Its ask lapsed or was dropped (the asked run kept its slot, or the asker parked): no request
                # lives any more, so preempt_after_s() after that it may ask again, one live ask at a time.
                lapsed = lapsed or time.time()
                if time.time() - lapsed >= preempt_after_s():
                    drop_claim(claim)
                    asked, claim, lapsed = "", "", 0.0
                    os.environ.pop(PREEMPT_ASKED_ENV, None)
            if (not f and not asked and not twin and urgent and is_urgent(priority) and not was_preempted
                    and time.time() - since >= preempt_after_s()):
                # A preempted run queues again at its old arrival, its grace long past: it asks nobody, else it
                # would take a third run's slot ahead of the run that preempted it ([note 46ef17a6f474ab41]).
                # Only a run whose stop lets this one start: parked, its test slots must fit once that run's free.
                claim, parked_ask = secrets.token_hex(6), parked
                if asked := preempt(holder, every, project_name, startable=startable if parked else None,
                                    claim=claim, owed=bool(owns)):
                    os.environ[PREEMPT_ASKED_ENV] = json.dumps({"since": since, "asked": asked, "claim": claim,
                                                                **({"parked": True} if parked else {})})
                    if tell:
                        tell(preempt_line(asked))
                else:
                    claim = ""
            if f:
                locks.mark_served(slot_dir(), RESOURCE, project_name)
                locks.mark_turn(slot_dir(), RESOURCE, ticket)
                _granted(tell, state, project_name, priority, owns, since, bool(told), cap, lane_at, waited)
                return Slot(f, again=again)
            if time.time() - told >= TELL_S:
                if tell and ticket is not None:
                    q = locks.fair_order(slot_dir(), RESOURCE, **order())
                    ahead = q.index(ticket) if ticket in q else 0
                    groups = len({locks.ticket_group(t) for t in q} | {locks.ticket_group(ticket)})
                    tell(f"queued for a host-wide checks slot: {len(locks.held_labels(every))} of {n + extra(n)} running, "
                         f"{cap_text(n)}, {ahead} ahead, {groups} project{'s' if groups != 1 else ''}"
                         + (", first in its project as an open obligation's owner" if queued_as != holder else "")
                         + (f"; waiting for free cores: {short}" if (short := cores_short(every)) else "")
                         + (f"; waiting for the disk: {disk}" if (disk := diskgate.saturated()) else ""))
                    _note_limit(f"waits for a host-wide checks slot: {len(locks.held_labels(every))} running, "
                                f"{cap_text(n)}", project_name)
                if not told and waiting:
                    waiting(True)
                told = time.time()
            if max_wait_s is not None and time.time() - started >= max_wait_s:
                return None
            # A live claim: the asked run's slot is this run's the moment it frees, so it looks often.
            time.sleep(poll_s(CLAIM_POLL_S if claim and claim_live(claim) else POLL_S))
            project.ps_cycle()   # pids are read once per poll cycle, never one `ps` each (ps_snapshot)
    finally:
        locks.dequeue(ticket)
        locks.dequeue(uwait)
        drop_claim(claim)   # taken, or given up: the asked run need not stop for it any more (preempted)
        _park(False, park, "", 0.0, None, None)
        if parked_told:
            _note_limit("")   # parked no more: its limit line leaves status
        if told and waiting:
            waiting(False)


URGENT_LINE = ("urgent checks with every host-wide checks slot busy: running now on a reserved urgent slot beyond "
               "the cap (checks.urgent_slots), with its reserved test lane")
URGENT_IN_CAP_LINE = ("urgent checks: running now on a reserved urgent slot, the next free one of the cap ahead of "
                      "every waiter (checks.urgent_slots), with test slots ahead of the queue")


def _urgent_take(urgent: bool, priority: int | None, every: list[Path], holder: str, what: str, same: str,
                 owns=(), mine: Path | None = None, rank: tuple | None = None, n: int = 0, claim: str = ""):
    """A free reserved urgent slot for an urgent run (acquire's `urgent`), else None: none while an urgent
    waiter ranked before it waits for one (_urgent_ahead; `mine` its own URGENT_WAIT ticket, else its `rank`).
    Unless priority_beyond_cap(), only while the cap `n` has room (_room, `claim` its own preempt claim): it
    takes the next free slot ahead of every waiter, never one beyond the cap."""
    if not urgent or not is_urgent(priority) or _running(every, same) or _urgent_ahead(mine, rank):
        return None
    if not priority_beyond_cap() and _room(n, every, claim) <= 0:
        return None
    return locks.try_take(urgent_paths(), holder, what, key=same,
                          extra={"priority": priority, "urgent": True, "owed": bool(owns)})


URGENT_WAIT = "checks-urgent-wait"   # check-slots queue: the urgent runs that could take a reserved urgent slot now


def _urgent_rank(owns, since: float) -> tuple[float, float]:
    """An urgent waiter's place for a reserved urgent slot: an open obligation's owner first, then arrival
    (its URGENT_WAIT ticket's priority: OBLIGATION_PRIORITY, else locks.URGENT_PRIORITY)."""
    return (float(OBLIGATION_PRIORITY if owns else locks.URGENT_PRIORITY), since)


def _urgent_wait(want: bool, ticket: Path | None, holder: str, since: float, owns) -> Path | None:
    """The URGENT_WAIT ticket an urgent run holds while it could take a reserved urgent slot (acquire: not
    while the same checks run), so a freed one goes to the owed waiter first, then the oldest, not to whichever
    polls first (_urgent_take); dropped once it no longer could."""
    if want and ticket is None:
        return locks.enqueue(slot_dir(), URGENT_WAIT, holder, since, priority=int(_urgent_rank(owns, since)[0]))
    if not want and ticket is not None:
        locks.dequeue(ticket)
        return None
    return ticket


def _urgent_ahead(mine: Path | None, rank: tuple | None = None) -> bool:
    """Whether another live URGENT_WAIT ticket ranks before `mine` (or before `rank` without one)."""
    try:
        q = locks.queued(slot_dir(), URGENT_WAIT)
    except OSError:
        return False
    key = (lambda t: (locks.ticket_priority(t), locks._arrival(t)))
    me = key(mine) if mine is not None and mine in q else rank
    return me is not None and any(t != mine and key(t) < me for t in q)


def _urgent_granted(f, tell, state, project_name, priority, owns, since, cap, lane_at, waited=None) -> Slot:
    """An urgent slot taken: logged as a grant (_granted), told, marked (Slot.urgent). The queue's turns
    are not marked: it took no slot of the cap."""
    _granted(tell, state, project_name, priority, owns, since, cap=cap, lane_at=lane_at, waited=waited)
    if tell:
        tell(URGENT_LINE if priority_beyond_cap() else URGENT_IN_CAP_LINE)
    slot = Slot(f)
    slot.urgent = True
    return slot


PREEMPT = "preempt"
PREEMPT_LINE = ("urgent checks with every host-wide checks slot busy{}: asked {} "
                "to give up its slot and queue again at its place (checks.preempt_after_s)")


def preempt_line(asked: str) -> str:
    """PREEMPT_LINE for the run `asked`; it names the busy urgent slots only where urgent runs may run
    beyond the cap (priority_beyond_cap)."""
    return PREEMPT_LINE.format(" (beyond the cap: an urgent slot too)" if priority_beyond_cap() else "", asked)


class Preempted(BaseException):
    """Raised in a routine run's checks an urgent run asked to give up its slot (preempt, preempted): the
    run ends its command, frees its slot and queues again at its old arrival (cli.cmd_checks). Not an
    Exception, so no handler of a check's failure takes it."""


def _preempt_path(pid: int) -> Path:
    return slot_dir() / f"{PREEMPT}.{pid}.json"


def preempt(by: str, every: list[Path], project_name: str | None = None,
            startable: Callable[[], bool] | None = None, claim: str = "", owed: bool = False) -> str:
    """Ask one routine run holding a slot of the cap (`every`) to give it up: one never preempted before first
    (a preempted one's label says so: acquire's `preempted`), then a run of the asker's project
    (`project_name`), then the lowest priority (none first, then the highest number), the latest granted
    among equals (it loses the least work). Only a holder whose label says it can be asked (acquire's
    `preemptible`), never a reserved slot's; push checks hold priority slots, never these. An urgent run
    (is_urgent) only when the asker is `owed` (its task owns an open obligation) and the holder's label says
    it owes nothing (acquire's "owed" false; an older label without it is skipped), after every routine one:
    an owed run never waits behind urgent work that owes nothing. With `startable` (the asker waits for its test slots) only a run whose stop lets it start:
    `startable()` true with that run's test slots counted free (FREEING), else nobody is asked. With `claim`
    the slot it frees is the asker's (claims): no other waiter takes it while the claim lives, and the request
    lapses with it (preempted). Never a run whose label names a progress log (acquire's `progress`: it cannot
    resume from its passed files) once that log's last pytest `[ NN%]` is SHIELD_PCT or more: a restart from
    zero there loses nearly all its work. Returns the holder asked, or ""."""
    from . import checkwall
    found = []
    for path in every:
        if not locks.held_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if not isinstance(h, dict) or not h.get("preemptible") or h.get("urgent"):
            continue
        if (log := h.get("progress_log")) and (checkwall._percent(log, 0) or 0) >= SHIELD_PCT:
            continue
        band = is_urgent(h.get("priority"))
        if band and not (owed and h.get("owed") is False):
            continue
        pid = int(h.get("ns_pid") or h.get("pid") or 0)
        if pid <= 0 or claim_live(str(_read(_preempt_path(pid)).get("claim") or "")):
            continue   # none, or another asker's live claim waits for its slot already
        prio = h.get("priority")
        again = bool(h.get("preempted")) or "[preempted by " in str(h.get("holder") or "")
        found.append(((not band, not again, bool(project_name) and h.get("checks_project") == project_name,
                       prio is None,
                       prio if isinstance(prio, int) else 0, float(h.get("since") or 0)), pid, path, h))
    for _, pid, path, h in sorted(found, key=lambda x: x[0], reverse=True):
        if startable is not None:
            tok = FREEING.set(h)
            try:
                if not startable():
                    continue
            finally:
                FREEING.reset(tok)
        try:
            if claim:
                _write(_claim_path(claim), {"by": by, "made": time.time(), "at": time.time(), "slot": path.name,
                                            "victim": {k: h.get(k) for k in OWNER_KEYS}, **locks.taker()})
            _preempt_path(pid).write_text(json.dumps({"by": by, "task": os.environ.get("TTP_TASK") or "",
                                                      "project": project_name or "", "at": time.time(),
                                                      **({"claim": claim} if claim else {})}))
        except OSError:
            drop_claim(claim)
            return ""
        return str(h.get("holder") or pid)
    return ""


PREEMPT_LOG = "preempt-asks.jsonl"   # check-slots/preempt-asks.jsonl: who asked by hand (ask_preempt), one line each


def ask_preempt(run: str, by: str, owners: list[dict] | tuple = ()) -> tuple[bool, str]:
    """`ttp checks --preempt <run>`: ask the checks of run `run` (its slot label's run_id, or a slot held by
    one of `owners`: the run's lane holders as `ttp checks --slot-status` names them, checklane.run_owners,
    which also finds a run the daemon relaunched under another run_id) to give up their
    slot of the cap, with the request preempt() writes (no claim: the freed slot goes to the queue's next
    waiter in its order), and log who asked (PREEMPT_LOG). Refused for push checks (a priority slot, or a push
    turn on the cap), a reserved urgent slot's run, an owed holder (its task owns an open obligation; an urgent
    one whose label does not say it owes nothing counts as owed), and one that cannot be asked (behind a lock,
    or an older ttp: preemptible). Returns (asked, the line to print)."""
    for path in all_paths() + urgent_paths() + priority_paths():
        h = _read(path)
        if str(h.get("run_id") or "") != str(run) and not any(same_owner(h, o) for o in owners):
            continue
        if not locks.held_labels([path]):
            continue
        who = str(h.get("holder") or f"run {run}")
        if path.name.startswith(PRIORITY + ".") or str(h.get("command") or "").startswith("push checks"):
            return False, f"not asked: {who} runs push checks, which are never preempted"
        if h.get("urgent") or path.name.startswith(URGENT + "."):
            return False, f"not asked: {who} holds a reserved urgent slot beyond the cap, no slot of the cap"
        if h.get("owed") or (is_urgent(h.get("priority")) and h.get("owed") is not False):
            return False, f"not asked: {who} is owed (its task owns an open obligation, or its label does not say it owes nothing)"
        pid = int(h.get("ns_pid") or h.get("pid") or 0)
        if not h.get("preemptible") or pid <= 0:
            return False, f"not asked: {who} cannot be asked (behind a lock or device queue, or an older ttp)"
        rec = {"by": by, "task": os.environ.get("TTP_TASK") or "", "at": time.time(), "manual": True}
        try:
            _preempt_path(pid).write_text(json.dumps(rec))
            with open(slot_dir() / PREEMPT_LOG, "a") as log:
                log.write(json.dumps({**rec, "victim": who, "run": str(run), "slot": path.name, **locks.taker()}) + "\n")
        except OSError as e:
            return False, f"not asked: {e}"
        return True, f"asked {who} to give up its host-wide checks slot and queue again at its place"
    return False, f"not asked: no checks of run {run} hold a host-wide checks slot"


OWNER_KEYS = ("pid", "pid_start", "ns_pid")   # a slot label's fields that name the process holding it (same_owner)


def same_owner(label: dict, owner: dict) -> bool:
    """Whether slot `label` is held by the process `owner` (a slot label too) names: its pid fields match."""
    return bool(owner.get("pid")) and all(label.get(k) == owner.get(k) for k in OWNER_KEYS)


def _claim_path(token: str) -> Path:
    return slot_dir() / f"{CLAIM}.{token}.json"


def _claim_live(rec: dict, now: float) -> bool:
    """A claim lives while its asker does and renewed it within CLAIM_S, for at most CLAIM_MAX_S."""
    try:
        at, made = float(rec.get("at") or 0), float(rec.get("made") or 0)
    except (TypeError, ValueError):
        return False
    return now - at < CLAIM_S and now - made < CLAIM_MAX_S and locks.pid_alive(rec) is not False


def claims(mine: str = "") -> int:
    """Live claims on a slot a preempted run frees, other than `mine`: each keeps one slot of the cap from
    every waiter but its asker (_room). A lapsed one is removed."""
    n, now = 0, time.time()
    for path in slot_dir().glob(f"{CLAIM}.*.json"):
        if mine and path.name == _claim_path(mine).name:
            continue
        if _claim_live(_read(path), now):
            n += 1
        elif now - _mtime(path) >= CLAIM_S:
            path.unlink(missing_ok=True)
    return n


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def claim_live(token: str) -> bool:
    return bool(token) and _claim_live(_read(_claim_path(token)), time.time())


def renew_claim(token: str, parked: bool, asked_parked: bool = False) -> None:
    """Keep the claim `token` alive while the run it asked still holds its slot and the asker could take
    it (not `parked`); once that run let it go, or while the asker parks, it lapses within CLAIM_S. An asker
    that asked while parked (`asked_parked`: it waits parked for that run's test slots) renews it parked for
    PARKED_CLAIM_S from the ask, time for the asked run to see the request and stop, never longer."""
    path = _claim_path(token)
    rec = _read(path)
    now = time.time()
    if not _claim_live(rec, now) or (parked and not (asked_parked and now - float(rec.get("made") or 0)
                                                      < PARKED_CLAIM_S)):
        return
    slot = slot_dir() / str(rec.get("slot") or "")
    if rec.get("slot") and locks._slot_labels([slot]) and same_owner(_read(slot), rec.get("victim") or {}):
        with contextlib.suppress(OSError):
            _write(path, {**rec, "at": time.time()})


def claim_freed_s(token: str) -> float | None:
    """Seconds since the run the live claim `token` asked let go of its slot (its last renewal), None while
    it still holds it or the claim is gone."""
    rec = _read(_claim_path(token))
    if not token or not _claim_live(rec, time.time()):
        return None
    slot = slot_dir() / str(rec.get("slot") or "")
    if rec.get("slot") and locks._slot_labels([slot]) and same_owner(_read(slot), rec.get("victim") or {}):
        return None
    return max(0.0, time.time() - float(rec.get("at") or 0))


def drop_claim(token: str) -> None:
    if token:
        _claim_path(token).unlink(missing_ok=True)


def _asked(since: float, key: str = "asked") -> str:
    """The holder the request arriving at `since` asked already (PREEMPT_ASKED_ENV), or ""; its claim's
    token with `key` "claim"."""
    try:
        got = json.loads(os.environ.get(PREEMPT_ASKED_ENV) or "{}")
    except ValueError:
        return ""
    return str(got.get(key) or "") if isinstance(got, dict) and got.get("since") == since else ""


def preempted(since: float) -> str:
    """Who asked this process's checks to give up their slot (preempt), granted at `since`, or "":
    "urgent #<task>" when the asker ran for a task, else "urgent <its holder>". A request whose claim
    lapsed or was dropped (its asker died, parked, or took another slot) asks nothing any more."""
    try:
        req = json.loads(_preempt_path(os.getpid()).read_text() or "{}")
    except (OSError, ValueError):
        return ""
    if not isinstance(req, dict) or float(req.get("at") or 0) < since:
        return ""
    if req.get("claim") and not claim_live(str(req["claim"])):
        return ""
    task = str(req.get("task") or "")
    return f"urgent #{task}" if task.isdigit() else f"urgent {req.get('by') or 'run'}"


def preempter() -> str:
    """The project whose urgent run asked this process's checks to give up their slot (preempt), or ""."""
    try:
        req = json.loads(_preempt_path(os.getpid()).read_text() or "{}")
    except (OSError, ValueError):
        return ""
    return str(req.get("project") or "") if isinstance(req, dict) else ""


def clear_preempt() -> None:
    """Remove this process's preempt request: handled, or ended."""
    try:
        _preempt_path(os.getpid()).unlink()
    except OSError:
        pass


YIELD = "yield"   # check-slots/yield.<pid>.<token>.<request id>.req|.ack: a run's commands ask it to give its slot up and back
YIELD_ENV = "TTP_CHECKS_YIELD"   # that stem, for the commands of a run that may yield (Slot.allow_yield)
YIELD_WAIT_S = 60.0   # ask_yield waits this long for the run to answer a yield (it looks every checkwall.POLL_S)


def _read(path: Path) -> dict:
    try:
        got = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write(path: Path, rec: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec))
    os.replace(tmp, path)


def serve_yield(slot, tell: Callable[[str], None] | None = None, stop: Callable[[], str] | None = None) -> None:
    """Answer every pending yield request of `slot`'s commands (ask_yield: one file each, oldest first), called
    while they run: "yield" frees the host-wide slot (Slot.let_go) and adds its waiter, "resume" queues for one
    in the normal order and holds it (hold_again, which `stop()` ends: its command ended); resumes pending
    together take one slot. The slot stays given up while a waiter that asked lives and none asked to resume
    since; once all died, the run takes one again at once for its further work. Each request is answered once,
    in its own answer file, and a slot is freed once."""
    stem = getattr(slot, "yield_stem", "")
    if not stem:
        return
    base = Path(stem)
    reqs = [(float(req.get("at") or 0), f.name, f, req) for f in base.parent.glob(base.name + ".*.req")
            for req in [_read(f)]]
    for _, _, f, req in sorted(reqs, key=lambda r: r[:2]):
        want, pid = req.get("want"), int(req.get("pid") or 0)
        if want == "yield" and (slot.f is not None or slot.yielded):
            if slot.f is not None:
                slot.let_go()
                tell and tell("gave up its host-wide checks slot while its command waits on an outside pool (ttp checks-yield)")
            slot.yielded = True
            slot.waiters.add(pid)
        elif want == "resume" and not slot.hold_again(tell, stop):
            return   # unanswered: its command ended
        f.unlink(missing_ok=True)
        try:
            _write(f.with_suffix(".ack"), {"id": str(req.get("id") or ""), "held": slot.f is not None})
        except OSError:
            pass   # unanswered: the asker ends at its bound or with the run
    slot.waiters = {w for w in slot.waiters if locks._alive(w)}
    if slot.yielded and not slot.waiters:
        slot.hold_again(tell, stop)


def ask_yield(want: str, pid: int | None = None, wait_s: float | None = None) -> str:
    """From a check command: ask the `ttp checks` run it is in (YIELD_ENV) to give its host-wide slot up
    while `pid` (default: this process's parent, the script that waits) waits on an outside pool ("yield",
    answered within YIELD_WAIT_S), or to take one again before CPU work ("resume": waits until it holds one,
    queued in the normal priority order). Each request has its own file, so concurrent askers are all answered.
    "" once answered or when there is no slot to give up (outside a run, cap off, nested, urgent or spilled),
    else why not."""
    stem = os.environ.get(YIELD_ENV) or ""
    if not stem:
        return ""
    owner = int((Path(stem).name.split(".") + ["", ""])[1] or 0)
    rid = secrets.token_hex(6)
    req, ack = Path(f"{stem}.{rid}.req"), Path(f"{stem}.{rid}.ack")
    try:
        _write(req, {"id": rid, "want": want, "pid": pid or os.getppid(), "at": time.time()})
    except OSError as e:
        return f"could not ask: {e}"
    span = YIELD_WAIT_S if wait_s is None else wait_s
    end = None if want == "resume" else time.time() + span
    try:
        while _read(ack).get("id") != rid:
            if not locks._alive(owner):
                return "the checks run that held the slot has ended"
            if end is not None and time.time() >= end:
                return f"no answer from the checks run (pid {owner}) within {span:.0f} s"
            time.sleep(poll_s(0.5))
    finally:
        req.unlink(missing_ok=True)   # an unanswered request is withdrawn: the run never acts on it late
        ack.unlink(missing_ok=True)
    return ""


def _drop_pass() -> None:
    """A run waiting on its twin's checks runs no tests: the load pass its gate took (testslots.full_startable,
    reserve_load_pass) goes back, so another run parked under the load guard may take it. Its gate takes one
    again once the twin is gone."""
    from . import testslots
    testslots.release_load_pass()


def _park(parked: bool, park: Path | None, holder: str, since: float, project_name: str | None,
          priority: int | None, small: bool = False) -> Path | None:
    """The PARKED ticket a run holds while it waits outside the queue for its test slots (acquire), at its
    arrival `since`; dropped once it no longer does."""
    if parked and park is None:
        park = _PARKS.park = locks.enqueue(slot_dir(), PARKED, holder, since, group=project_name, priority=priority,
                                           small=small)
    elif not parked and park is not None:
        _PARKS.park = None
        locks.dequeue(park)
        park = None
    return park


_PARKS = threading.local()   # .park: this thread's PARKED ticket, never in its own starved_ahead


DONE_S = 30.0   # how often a waiter reads whether a recorded pass made its run needless
PARKED_LINE = ("no test slots free for this run (its project's test-worker budget or the host's pool is full, "
               "fewer than checks.test_run_min are free, or the host's load is above checks.load_guard): "
               "waiting outside the queue for a host-wide checks slot, keeping this place")


def _why(why: Callable[[], str] | None) -> str:
    try:
        return str(why() or "") if why else ""
    except Exception:   # a reason that cannot be read costs only the reason, never the wait
        return ""


def _limit_name(why: str) -> str:
    """The limit a parked reason names, without its changing numbers: re-told only when this changes."""
    return re.sub(r"\d+(\.\d+)?", "N", why.split(":", 1)[0])


def _note_limit(line: str, project_name: str | None = None) -> None:
    from . import checkspill
    checkspill.note_limit(project_name or "", line)


def _tell_parked(tell, project_name: str | None, why: Callable[[], str] | None, now: str | None = None) -> str:
    """Tell parked_line with the limit that holds the run now, and show it in status (checkspill.note_limit);
    the limit told."""
    said = _why(why) if now is None else now
    tell(parked_line(said))
    _note_limit(f"waits for its test slots: {said or 'its project budget or the host pool is full'}", project_name)
    return said


def parked_line(why: str = "") -> str:
    """PARKED_LINE, ending with the limit that holds the run now (testslots.pool_gate's reason: its name,
    value and setting) when the waiter knows it (acquire's `why`)."""
    return f"{PARKED_LINE}; limit now: {why}" if why else PARKED_LINE


GRANT_TELL_S = 5.0   # a slot taken longer than this after its arrival is told (grant_line)


def grant_line(priority: int | None, owns, since: float, now: float | None = None) -> str:
    """'host-wide checks slot taken after N min (priority pK[, owner of oX,oY], arrived HH:MM ZONE)'."""
    now = time.time() if now is None else now
    owner = f", owner of {','.join(owns)}" if owns else ""
    try:
        at = timefmt.short(since, timefmt.ambient(), now)
    except Exception:   # an unknown zone only costs the zone, never the line
        at = time.strftime("%H:%M", time.localtime(since))
    prio = f"p{priority}" if priority is not None else "none"
    return (f"host-wide checks slot taken after {max(0.0, now - since) / 60:.1f} min "
            f"(priority {prio}{owner}, arrived {at})")


def _granted(tell, state, project_name, priority, owns, since: float, told: bool = False,
             cap: int | None = None, lane_at: float | None = None, waited: dict | None = None) -> None:
    """Log a slot taken (record_wait, with `waited`'s lane_s and pool_s) and tell it when it waited, here or
    before a relaunch."""
    now = time.time()
    try:   # only the log's note: a failed read never costs the slot
        over = over_share(project_name) if now - since > POLL_S else []
    except Exception:
        over = []
    record_wait(state, project_name, priority, since, now, owns=owns, cap=cap, lane_at=lane_at, over=over,
                **(waited or {}))
    if tell and (told or now - since > GRANT_TELL_S):
        tell(grant_line(priority, owns, since, now))


def _running(every: list[Path], same: str) -> str:
    """The holder of a slot whose checks have the key `same`, or ""."""
    if not same:
        return ""
    for path in every + urgent_paths():
        if not locks.held_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict) and h.get("key") == same:
            return str(h.get("holder") or "")
    return ""


def _room(n: int, every: list[Path], mine: str = "") -> int:
    """Slots a waiter may take: the cap and extra(), less those held, push and urgent holders (_counted),
    pool runs and others' live claims. A live claim `mine` wins the slot its preempted run frees over a
    push waiting on its priority slot: the waiting push is not counted against its asker, and that push's
    turn (_priority_turn) counts the claim, so exactly one of them takes the slot (the claim caused the
    preemption) and the push takes the next one."""
    return (n + extra(n) - len(locks.held_labels(every)) - _counted(claimant=claim_live(mine)) - len(pool_runs())
            - claims(mine))


def _counted(skip: Path | None = None, claimant: bool = False) -> int:
    """Push priority slots (a push waiting on one holds it, so the next free slot is its) and reserved urgent
    slots held now, which count toward the cap unless priority_beyond_cap(). With `skip` (a push's own
    waiting priority slot) only the other pushes that run or waited before it count, so waiting pushes
    of several branches start in arrival order and never block one another. With `claimant` (the asker
    of a live claim, _room) no waiting push counts."""
    if priority_beyond_cap():
        return 0
    n = len(locks.held_labels(urgent_paths()))
    mine = _label(skip) if skip is not None else {}
    for path in priority_paths():
        if path == skip or not locks.held_labels([path]):
            continue
        if claimant:
            n += not _label(path).get("waiting")
            continue
        h = _label(path) if skip is not None else {}
        n += not (h.get("waiting") and (float(h.get("since") or 0), path.name)
                  > (float(mine.get("since") or 0), skip.name))
    return n


def _label(path: Path) -> dict:
    try:
        return locks.read_label(path)
    except (OSError, ValueError):
        return {}


def _take(n: int, ps: list[Path], every: list[Path], holder: str, what: str, same: str, room: int | None = None,
          label: dict | None = None, mine: str = ""):
    """A slot of the cap, else one load opened beyond it, while fewer than cap plus extra() are held.
    The cap's own slots are tried first, so two takers racing never pass the cap with no extra open."""
    room = _room(n, every, mine) if room is None else room
    if room <= 0 or cores_short(every) or diskgate.saturated():
        return None
    if label is not None and "checks_project" in label and not label["checks_project"] \
            and _bare_held(every) >= bare_slots() and _project_waits():
        # a run with no project: at most bare_slots() at once while a project's run waits
        return None
    f = locks.try_take(ps, holder, what, key=same, extra=label)
    if f is None and (x := extra(n)):
        f = locks.try_take(every[n:n + x], holder, what, key=same, extra=label)
    return f


def _take_in_turn(n: int, ticket: Path, ps: list[Path], every: list[Path], holder: str, what: str, same: str,
                  label: dict | None = None, mine: str = ""):
    """locks.take_in_turn over the cap and the slots load opened beyond it."""
    room = _room(n, every, mine)
    if room <= 0 or not locks.my_turn(slot_dir(), RESOURCE, ticket, room, fair=True, **order()):
        return None
    return _take(n, ps, every, holder, what, same, room, label)


def acquire_priority(key: str, holder: str, what: str = "", tell: Callable[[str], None] | None = None,
                     project_name: str | None = None, state: Path | None = None,
                     fallback: Callable[[], Slot] | None = None) -> Slot:
    """Take the priority slot of the push branch `key` (ahead of every task waiting in the queue, then the
    next room in the cap: _priority_turn; beyond it with priority_beyond_cap()), saying so through `tell`. Only another push to the same branch can hold it; then this
    waits for that one alone, priority_wait_s() at most when `fallback` is given: past that it says who
    held it and returns `fallback()` (an ordinary turn), marked `fell_back`. Cap off or nested: no slot,
    run at once. An unwritable slot directory raises Unavailable. With `state` the slot taken is logged
    there as for acquire."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    path = priority_path(key)
    started, told = time.time(), 0.0
    tag = {"slot": "push", **({"batch": b} if (b := batch_id()) else {})}
    ticket = None   # while another push holds the slot: a PUSH_WAIT ticket, so status lists this waiter
    try:
        while True:
            beyond = priority_beyond_cap()
            try:
                f = locks.try_take([path], holder, what, extra=tag if beyond else {**tag, "waiting": True})
                if f and not beyond:
                    locks.dequeue(ticket)   # it holds the slot now: the holder's label lists it
                    ticket = None
                    told = _priority_turn(f, path, n, tell) or told
            except OSError as e:
                raise _unavailable(e) from e
            if f:
                if tell:
                    q = len(locks.queued(slot_dir(), RESOURCE))
                    tell(f"push checks took the push priority slot ("
                         + (f"beyond the host-wide cap of {n}: " if beyond else f"within the host-wide cap of {n}: ")
                         + f"{len(locks.held_labels(all_paths(n)))} task checks running"
                         + (f", {q} queued behind" if q else "") + ")"
                         + (f" after {(time.time() - started) / 60:.1f} min" if told else ""))
                record_wait(state, project_name, None, started, time.time(), "push")
                return Slot(f)
            if fallback is not None and time.time() - started >= (bound := priority_wait_s()):
                if tell:
                    tell(f"the push priority slot stayed held {bound / 60:g} min by "
                         f"{_some(locks.held_labels([path])) or 'unknown'} (checks.push_priority_wait_s): taking an "
                         "ordinary host-wide checks turn instead")
                locks.dequeue(ticket)
                ticket = None
                slot = fallback()
                slot.fell_back = True
                return slot
            if ticket is None:
                try:
                    ticket = locks.enqueue(slot_dir(), PUSH_WAIT, holder, started)
                except OSError:
                    ticket = None   # status only: the wait goes on without it
            if tell and time.time() - told >= TELL_S:
                tell(f"waiting for the push priority slot, held by another push to the same branch: "
                     f"{_some(locks.held_labels([path])) or 'unknown'}")
                told = time.time()
            time.sleep(poll_s(POLL_S))
    finally:
        locks.dequeue(ticket)


def _priority_turn(f, path: Path, n: int, tell: Callable[[str], None] | None = None) -> float:
    """Hold the push priority slot `f` (labelled waiting) until the cap has room for it, then mark it running.
    While it waits it counts toward the cap (_counted), so no other waiter takes the slot that frees next.
    Returns when it was last told about the wait (0.0: no wait). Its slot is let go if the wait is cut short."""
    told = 0.0
    try:
        while True:
            m = limit() or n
            every = all_paths(m)
            if m + extra(m) - len(locks.held_labels(every)) - _counted(path) - len(pool_runs()) - claims() > 0:
                locks.relabel(f, {"waiting": False, "since": time.time()})
                return told
            if tell and time.time() - told >= TELL_S:
                tell(f"push checks hold their push priority slot and take the next free host-wide checks slot ahead of "
                     f"every waiter (cap {m}, checks.priority_beyond_cap off): {_some(locks.held_labels(every))}")
                told = time.time()
            time.sleep(poll_s(POLL_S))
            project.ps_cycle()
    except BaseException:
        f.close()
        raise


# Each slot taken, logged in the taker's project state as one JSON line: {"project", "task", "run",
# "priority", "slot": "queue" | "push", "queued_at", "granted_at"; a push batch's also "batch"} (times in epoch seconds). A slot
# taken at once logs a wait of zero, so the median and p90 describe every checks run, not only the
# ones that queued. The log is bounded like activity.jsonl: past WAITS_MAX_BYTES it rotates to one
# old generation, read as well.
WAITS_LOG, WAITS_OLD, WAITS_LOCK = "checks-slot-waits.jsonl", "checks-slot-waits.1.jsonl", "checks-slot-waits.lock"
WAITS_MAX_BYTES = 256 * 1024
WAITS_WINDOW_S = 24 * 3600


def record_wait(state: Path | None, project_name: str | None, priority: int | None, queued_at: float,
                granted_at: float, slot: str = "queue", owns: list[str] | tuple = (),
                cap: int | None = None, lane_at: float | None = None, over: list[str] | tuple = (),
                lane_s: float | None = None, pool_s: float | None = None) -> None:
    """Append one slot wait to `state`/WAITS_LOG and sync it, and the grant to the host-wide log
    (record_grant). Never raises: a log that cannot be written must not cost the checks their slot.
    No `state`: only the host-wide line is logged. `cap`: the project cap in force (checklane.project_cap,
    0: none), stored as "cap" so throughput judges the wait by it; None (no project lane): no field.
    `lane_at`: when the run got its repository's lane (`ttp checks` queues for it before this slot, from
    the same `queued_at`), stored as "lane_at": only the time after it waited for the host. `over`: the
    other projects over their fair share of the cap when a wait ended (over_share), stored as "over_share".
    `lane_s`: the seconds
    since `queued_at` it waited for its own lanes over every bound and re-execution (a bounded wait gives its lane
    back and takes it again, so lane_at alone dates only the last one), of which `pool_s` the host's test pool held
    (testslots.pool_room refused a lane it could take past them): throughput books only lane_s - pool_s as own.
    A preempted run's rerun (CUT_ENV) stores "cut_at" and "cut_by": its wait from its old grant was the asker's."""
    task, run = _task_run()
    task = task or ""
    rec = {"project": project_name, "task": int(task) if task.isdigit() else None,
           "run": run or None, "priority": priority, "slot": slot,
           "queued_at": round(queued_at, 3), "granted_at": round(granted_at, 3)}
    if not task.isdigit() and (b := batch_id()):
        rec["batch"] = b   # a push batch's checks: no task by design, named by its batch id
    if cap is not None:
        rec["cap"] = int(cap)
    if lane_at is not None:
        rec["lane_at"] = round(lane_at, 3)
    if over:
        rec["over_share"] = list(over)
    if lane_s is not None:
        rec["lane_s"] = round(max(0.0, lane_s), 3)
        rec["pool_s"] = round(min(max(0.0, pool_s or 0.0), rec["lane_s"]), 3)
    try:
        cut = json.loads(os.environ.get(CUT_ENV) or "{}")
        if isinstance(cut, dict) and isinstance(cut.get("at"), (int, float)) and not isinstance(cut["at"], bool):
            rec["cut_at"], rec["cut_by"] = round(float(cut["at"]), 3), str(cut.get("by") or "")
    except ValueError:
        pass
    if state is not None:
        _append(Path(state), WAITS_LOG, WAITS_OLD, WAITS_LOCK, WAITS_MAX_BYTES, rec)
    record_grant(rec, owns)


def _append(d: Path, name: str, old: str, lock: str, cap: int, rec: dict) -> None:
    """Append `rec` as one synced JSON line to `d`/`name`, rotating it to `old` past `cap` bytes. Never raises."""
    line = json.dumps(rec, sort_keys=True) + "\n"
    try:
        with open(d / lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            log = d / name
            try:
                size = log.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(line) > cap:
                os.replace(log, d / old)
            project.durable_append(log, line)
    except OSError:
        pass


# Every slot taken on this host, by any project, as one JSON line in ~/.tt-project/GRANTS_LOG: {"ts" (the
# grant), "project", "task", "run", "slot", "priority", "obligations" (the ids behind a boost), "since" (the
# arrival), "waited_s"}. Any run may read it to see who took slots ahead of it and why. Bounded like
# WAITS_LOG: past GRANTS_MAX_BYTES it rotates to one old generation.
GRANTS_LOG, GRANTS_OLD, GRANTS_LOCK = "checks-slot-grants.jsonl", "checks-slot-grants.1.jsonl", "checks-slot-grants.flock"
GRANTS_MAX_BYTES = 1024 * 1024


def grants_dir() -> Path:
    return project.HOME_DIR


def record_grant(rec: dict, owns: list[str] | tuple = ()) -> None:
    """Append the slot grant `rec` (record_wait's record) to the host-wide GRANTS_LOG. Never raises."""
    row = {"ts": rec["granted_at"], "project": rec["project"], "task": rec["task"], "run": rec["run"],
           "slot": rec["slot"], "priority": rec["priority"], "obligations": list(owns or ()),
           "since": rec["queued_at"], "waited_s": round(max(0.0, rec["granted_at"] - rec["queued_at"]), 3)}
    if rec.get("over_share"):
        row["over_share"] = rec["over_share"]
    d = grants_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    _append(d, GRANTS_LOG, GRANTS_OLD, GRANTS_LOCK, GRANTS_MAX_BYTES, row)


def wait_records(state: Path, now: float | None = None, window_s: float = WAITS_WINDOW_S) -> list[dict]:
    """The records (record_wait's) that `state`'s log holds for slots taken in the last `window_s`,
    with "queued_at" and "granted_at" as floats."""
    return _records(Path(state), (WAITS_OLD, WAITS_LOG), ("queued_at", "granted_at"), now, window_s)


def grant_records(now: float | None = None, window_s: float = WAITS_WINDOW_S) -> list[dict]:
    """The host-wide grants (record_grant's rows, any project) taken in the last `window_s`, with "ts"
    and "since" as floats."""
    return _records(grants_dir(), (GRANTS_OLD, GRANTS_LOG), ("since", "ts"), now, window_s)


def _records(d: Path, names: tuple, keys: tuple[str, str], now: float | None, window_s: float) -> list[dict]:
    """The JSON lines of `d`/`names` whose two times `keys` (arrival, grant) read as floats, the grant
    in the last `window_s`."""
    now = time.time() if now is None else now
    out = []
    for name in names:
        try:
            text = (d / name).read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                r = json.loads(raw)
                q, g = float(r[keys[0]]), float(r[keys[1]])
            except (ValueError, KeyError, TypeError):
                continue
            if now - window_s <= g <= now:
                out.append({**r, keys[0]: q, keys[1]: g})
    return out


def waits(state: Path, now: float | None = None, window_s: float = WAITS_WINDOW_S) -> list[float]:
    """The slot waits in seconds that `state`'s log holds for slots taken in the last `window_s`."""
    return [max(0.0, r["granted_at"] - r["queued_at"]) for r in wait_records(state, now, window_s)]


def wait_stats(state: Path, now: float | None = None) -> dict | None:
    """{"n", "median_s", "p90_s"} of the last 24 h's slot waits in `state`'s log (p90 by nearest
    rank); None with none logged."""
    w = sorted(waits(state, now))
    if not w:
        return None
    mid = len(w) // 2
    median = w[mid] if len(w) % 2 else (w[mid - 1] + w[mid]) / 2
    return {"n": len(w), "median_s": median, "p90_s": w[-(-9 * len(w) // 10) - 1]}


def _dur(s: float) -> str:
    return f"{s:.0f}s" if s < 60 else f"{s / 60:.1f} min"


def wait_line(state: Path, now: float | None = None) -> str:
    """The status line of the project's checks slot waits over the last 24 h, "" with none."""
    st = wait_stats(state, now)
    if not st:
        return ""
    return f"checks slot waits (24 h): {st['n']} taken, median {_dur(st['median_s'])}, p90 {_dur(st['p90_s'])}"


URGENT_WAIT = "urgent_check_wait"   # the daemon's event kind (urgent_waits)
URGENT_WAIT_S = 600.0   # an urgent run waiting this long in a host-wide queue raises one event


def urgent_waits(project_name: str | None, now: float | None = None, min_s: float = URGENT_WAIT_S) -> list[dict]:
    """This project's urgent check runs (a ticket of priority locks.URGENT_PRIORITY or less) waiting `min_s`
    or more in a host-wide queue: the checks queue (RESOURCE), the parked runs' queue (PARKED) or the test
    pool's host line (testslots). Each: queue, ticket (its file name: the same for one run in every queue), holder (the waiter's
    label), waited_s and held_by (who holds what it waits for: the checks slots, or the test pool's slots,
    each label with its count). A parked or test-slot waiter the host load guard holds (testslots.load_held:
    every pass it may take is held) also has load (the guard's reason), and held_by names those passes'
    holders instead, not the pool's. Read only; an unreadable queue gives none."""
    from . import testslots
    gid = locks.group_id(project_name)
    now = time.time() if now is None else now
    s = testslots.sizes(None)
    pool = testslots.paths(s[0]) if s else []
    out = []
    for queue, held in ((RESOURCE, lambda: paths()), (PARKED, lambda: pool),
                        (testslots.TESTS.resource, lambda: pool)):
        try:
            q = [t for t in locks.queued(slot_dir(), queue) if locks.ticket_group(t) == gid and locks._urgent(t)
                 and now - locks._arrival(t) >= min_s]
        except OSError:
            continue
        if not q:
            continue
        labels: dict[str, int] = {}
        for h in locks.held_labels(held()):
            labels[h or "?"] = labels.get(h or "?", 0) + 1
        held_by = [f"{h} (x{n})" if n > 1 else h for h, n in labels.items()]
        for t in q:
            try:
                holder = t.read_text().strip()
            except OSError:
                holder = ""
            w = {"queue": queue, "ticket": t.name, "holder": holder or t.name,
                 "priority": locks.ticket_priority(t), "waited_s": now - locks._arrival(t), "held_by": held_by}
            try:
                if queue != RESOURCE and (load := testslots.load_held(w["priority"], since=locks._arrival(t))):
                    passes = testslots.load_pass_paths(w["priority"], since=locks._arrival(t))
                    w |= {"load": load, "held_by": locks.held_labels(passes)}
            except OSError:
                pass
            out.append(w)
    return out


def urgent_wait_text(w: dict) -> str:
    """One urgent_waits entry in words: the queue, the waiter, how long and what holds it."""
    f = urgent_wait_fold(w)
    return f"urgent checks {f['item']} in {f['group']}; {f['ctx']}"


def urgent_wait_fold(w: dict) -> dict:
    """urgent_wait_text's parts, the event's `fold` data (coordinator.fold_events): one NEW EVENTS line per
    queue names each waiter and what holds the queue now once."""
    held = "; ".join(w["held_by"][:8]) + (f"; and {len(w['held_by']) - 8} more" if len(w["held_by"]) > 8 else "")
    if w.get("load"):   # the load guard, not the pool's slots: name it and its passes' holders
        ctx = (f"held by: the host load guard ({w['load']}); its load passes held by: "
               + (held or "none right now (its next try takes one)"))
    else:
        ctx = "held by: " + (held or 'nothing held right now (the next turn is due)')
    return {"group": f"the host-wide {w['queue']} queue", "ctx": ctx,
            "item": f"{w['holder']} (priority {w['priority']:g}) have waited {w['waited_s'] / 60:.0f} min"}

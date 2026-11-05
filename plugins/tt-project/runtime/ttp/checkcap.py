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
as many checks as the cores left over the 1-minute load average hold at check_load() each, at least one
slot per CORES_PER_SLOT cores (and at least one), at most one per CORES_PER_SLOT_MAX cores. Our own
running checks count in that load, so the cap shrinks as they load the host; a check keeps its slot
until it ends, and a waiter reads the cap again on every try. Check commands run with HELD_ENV
set, so a `ttp checks` nested in them runs at once instead of waiting for the slot its parent holds.

Push checks (the push queue's batches and `ttp push` to the push branch) hold the push lock, so every
approved change waits on them: they never queue behind task checks. Each push branch has one priority
slot of its own beyond the cap (`acquire_priority`), keyed by the remote and branch so it is one per
branch host-wide, across projects. `ttp land`'s checks take one too, keyed by the repository's path
and the local branch it moves (push.priority_key with no remote). Task checks keep their cap and their order: the priority slot never
takes one of theirs. It is an OS file lock like the others, so a killed push frees it too.

Checks of urgent tasks (priority 1 or better, an `urgent:` spec line: user-ordered and live-impact work,
heal self-fixes, interim mitigations, the review of an urgent change) go next: ahead of every routine
waiter of any project whatever its arrival, the most urgent first, then by project turns (locks.fair_order with
locks.URGENT_FIRST, the default PRIORITY_TURNS). `checks.priority_turns` N (TTP_CHECKS_PRIORITY_TURNS)
instead bounds them to N turns in a row while other checks wait, taking turns by project; 0 serves them
in the fair order only.

A routine checks run waits without a bound only behind urgent ones: one that has waited `checks.starve_s`
(TTP_CHECKS_STARVE_S, default STARVE_S, about one full suite run) goes ahead of every later routine arrival
(and of urgent ones under a set checks.priority_turns), for the next free slot (locks.fair_order's `aged_before`). A run parked outside the queue for its
test slots (acquire's `startable`) holds a PARKED ticket meanwhile, so the wait is visible: once it has
waited that long, later runs of its project leave it their project's next free test slots
(testslots.pool_room, starved_ahead), and push checks run on their branch's reserved test lane alone
while any run waits for test slots (testslots.acquire), so a stream of pushes never holds them all.

While the host is lightly loaded (1-minute load average under half its cores) up to `checks.load_extra`
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

import fcntl
import hashlib
import json
import math
import os
import re
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
CHECK_LOAD = 4.0          # the 1-minute load one running check adds with the test pool off (check_load())
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
STARVE_S = 3600.0   # default checks.starve_s: a run waiting this long goes before every later arrival (starve_s())
STARVE_ENV = "TTP_CHECKS_STARVE_S"


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
    """The cap with no checks.host_max: the checks the cores left over the 1-minute load average hold
    at check_load() each, within default_range(). An unreadable load average gives the floor."""
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
    the last few seconds), or less the 1-minute load average where /proc/stat cannot be read; None
    with neither readable."""
    busy = hostcpu.busy_cores()
    if busy is None and ((busy := _load()) != busy or busy == float("inf")):
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
    not show yet), cover one check's price (check_load()). With no check running on the host one
    always starts, so checks never starve; checks.live_cores 0 or no reading lets every one start."""
    if not live_cores_on() or not (held := held_since(every)) or (free := free_cores()) is None:
        return ""
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


def aged_before(now: float | None = None) -> float:
    """The arrival time at or before which a waiter has waited past starve_s()."""
    return (time.time() if now is None else now) - starve_s()


def starved_ahead(project_name: str | None, since: float | None = None, now: float | None = None) -> list[Path]:
    """The other parked runs of `project_name` (PARKED tickets) that have waited starve_s() or longer and arrived
    before the asking run: its own PARKED ticket's arrival, else `since`, else now (a newcomer's)."""
    now = time.time() if now is None else now
    edge, gid = aged_before(now), locks.group_id(project_name)
    try:
        q = locks.queued(slot_dir(), PARKED)
    except OSError:
        return []
    mine = getattr(_PARKS, "park", None)
    me = locks._arrival(mine) if mine in q else now if since is None else since
    return [t for t in q if t != mine and locks.ticket_group(t) == gid and locks._arrival(t) <= edge
            and locks._arrival(t) < me]


def _load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return float("inf")


def extra(n: int | None = None) -> int:
    """Slots open beyond the cap now: two while the 1-minute load average is under a quarter of the
    cores, one under half, none above that, with the cap off or with checks.load_extra 0."""
    top = extra_max()
    if not (limit() if n is None else n) or not top:
        return 0
    load, c = _load(), cores()
    return min(top, 2 if load < c / 4 else 1 if load < c / 2 else 0)


class Cancelled(RuntimeError):
    """`stop` ended the wait: the checks' task ended while they waited (the reason is the message)."""


class Upgraded(Exception):
    """A waiting detached `ttp checks` found a newer runtime installed (upgraded(); the message is its
    version): it lets go of what it holds and re-executes itself with it (cli._checks_reexec)."""


_SEEN: dict = {}   # upgraded(): the installed version it last saw differ, and since when


def upgraded(init: Path | None = None, now: float | None = None, settle: float | None = None) -> str:
    """The runtime version now installed where this process imported its code from (`init`: that package's
    __init__.py), when it differs from the version it runs and has stayed for `settle` seconds
    (UPGRADE_SETTLE_S), else "". A version it re-executed for already (REEXEC_ENV) is never returned
    again, so a re-execution that did not take the new code cannot loop."""
    from . import __file__ as own_init, __version__
    now = time.time() if now is None else now
    try:
        text = Path(init or own_init).read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    new = m.group(1) if m else ""
    if not new or new == __version__ or new == os.environ.get(REEXEC_ENV):
        _SEEN.clear()
        return ""
    if _SEEN.get("version") != new:
        _SEEN.update(version=new, since=now)
    return new if now - _SEEN["since"] >= (UPGRADE_SETTLE_S if settle is None else settle) else ""


def label(project_name: str | None, what: str = "") -> str:
    """Who holds or waits for a slot, as status lines show it: the project and the task's run."""
    task, run = os.environ.get("TTP_TASK"), os.environ.get("TTP_RUN_ID")
    who = f"{project_name or 'no project'}" + (f" task #{task} (run {run or '?'})" if task else "")
    return who + (f" {what}" if what else "")


def task_priority(p) -> int | None:
    """The priority of the task this process runs for (TTP_TASK) in project `p`; None without one.
    OBLIGATION_PRIORITY while the task is a required owner of an open obligation."""
    return task_rank(p)[0]


def task_rank(p) -> tuple[int | None, list[str]]:
    """(task_priority(p), the ids of the open obligations behind an OBLIGATION_PRIORITY boost, else []).
    An urgent task (urgent.is_urgent) ranks at least urgent.PRIORITY, a user-ordered one (a fresh `urgent:`
    line) urgent.ORDERED_PRIORITY, as an obligation owner."""
    task = os.environ.get("TTP_TASK") or ""
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


def state() -> dict:
    """Who runs checks host-wide now and who waits: {"limit", "running": [labels], "queued": [labels],
    "priority": [labels of push checks on their priority slots]}."""
    n = limit()
    running = locks.held_labels(all_paths(n)) if n else []
    priority = locks.held_labels(priority_paths()) if n else []
    since = [float(r.get("since") or 0) for r in locks.holder_records(priority_paths())] if priority else []
    queued, ahead, groups, arrived = [], {}, set(), []
    for t in locks.fair_order(slot_dir(), RESOURCE, urgent_max=priority_turns(), aged_before=aged_before()):
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
            "ahead": ahead, "queued_at": arrived, "priority_since": since, "parked": parked}


def _project(holder: str) -> str:
    """The project a holder label names (label())."""
    return holder.split(" task #", 1)[0].split(" ttp checks", 1)[0]


def line() -> str:
    """One status line, or "" when nothing runs or waits."""
    s = state()
    if not s["running"] and not s["queued"] and not s["priority"] and not s["parked"]:
        return ""
    now = time.time()
    cap = (f"cap {s['limit']}" + (f" +{s['extra']} while the load is low" if s["extra"] else "")
           if s["limit"] else "no cap")
    out = f"host checks: {len(s['running'])} running ({cap})"
    if s["priority"]:
        out += ", push priority slot: " + _some([_aged(h, t, now, "held") for h, t in
                                                 zip(s["priority"], s["priority_since"] or [0.0] * len(s["priority"]))])
    if s["queued"]:
        out += f", {len(s['queued'])} queued: " + _some([_aged(h, t, now) for h, t in zip(s["queued"], s["queued_at"])])
    if s["parked"]:
        out += (f", {len(s['parked'])} waiting outside the queue for test slots: "
                + _some([_aged(h, t, now) for h, t in s["parked"]]))
    if s["running"]:
        out += f" (running: {_some(s['running'])})"
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

    def __init__(self, f=None, served: bool = False):
        self.f = f
        self.served = served
        self.tests = None   # the run's test slots (testslots.Grant), held and freed with this slot
        self.spill = None   # a claim on a spill host the checks run on instead (checkspill.Claim)

    def env(self) -> dict[str, str]:
        """For the check commands' environment: a `ttp checks` or push nested in them runs at once."""
        return ({HELD_ENV: "1"} if self.f is not None else {}) | (self.tests.env() if self.tests else {})

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None
        if self.tests is not None:
            self.tests.close()
            self.tests = None
        if self.spill is not None:
            self.spill.close()
            self.spill = None

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
            parks: Callable[[bool], None] | None = None) -> Slot | None:
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
    the project's record. `owns`: the ids behind a boost to OBLIGATION_PRIORITY (task_rank).
    A slot taken more than GRANT_TELL_S after `since` is told with its priority and arrival (grant_line),
    also when a relaunch that inherited `since` takes it at once. While `startable()` is false (the
    run's test slots could not be taken now: testslots.pool_room) it waits outside the queue as for
    `same`, keeping `since`, so it holds no slot that only waits and the next project's waiter takes it;
    `parks(True)` once it parks so and `parks(False)` once it leaves that wait (cli._checks_slot_take
    relabels its lane: testslots.PARKED_WAITS)."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    since = time.time() if since is None else since
    waiting = checkwait.chain(waiting, "host-wide checks slot")   # inside a push check: not its run time
    ps, every = paths(n), all_paths(n)
    queued_as = holder + (OBLIGATION_MARK if priority is not None and priority < 1 else "")
    try:
        parked = bool(startable and not startable())
        f = (None if parked or locks.queued(slot_dir(), RESOURCE) or _running(every, same)
             else _take(n, ps, every, holder, what, same))
        if f:
            locks.mark_served(slot_dir(), RESOURCE, project_name)
            locks.mark_turn(slot_dir(), RESOURCE, None)
            _granted(tell, state, project_name, priority, owns, since, cap=cap, lane_at=lane_at)
            return Slot(f)
        ticket = None if parked else locks.enqueue(slot_dir(), RESOURCE, queued_as, since, group=project_name,
                                                   priority=priority)
        if parked and tell:   # told once a wait: its pool's room may come and go
            tell(PARKED_LINE)
        park = _park(parked, None, queued_as, since, project_name, priority)
        if parked and parks:
            parks(True)
    except OSError as e:
        raise _unavailable(e) from e
    started, told, checked, parked_told = time.time(), 0.0, time.time(), parked
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
            try:
                park = _park(parked, park, queued_as, since, project_name, priority)
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
                    tell(f"the same checks run now ({twin}); waiting for their result, keeping this place in the queue"
                         if twin else PARKED_LINE)
                parked_told = parked_told or parked
            elif not twin and not parked and ticket is None:
                try:
                    ticket = locks.enqueue(slot_dir(), RESOURCE, queued_as, since, group=project_name,
                                           priority=priority)
                except OSError as e:
                    raise _unavailable(e) from e
                if done and done():
                    return Slot(served=True)
            f = None
            if ticket is not None:
                n = limit() or n   # a default cap follows the load (default_limit())
                ps = paths(n)
                try:
                    f = _take_in_turn(n, ticket, ps, every, holder, what, same)
                except OSError:
                    f = None
            if f:
                locks.mark_served(slot_dir(), RESOURCE, project_name)
                locks.mark_turn(slot_dir(), RESOURCE, ticket)
                _granted(tell, state, project_name, priority, owns, since, bool(told), cap, lane_at)
                return Slot(f)
            if time.time() - told >= TELL_S:
                if tell and ticket is not None:
                    q = locks.fair_order(slot_dir(), RESOURCE, urgent_max=priority_turns(), aged_before=aged_before())
                    ahead = q.index(ticket) if ticket in q else 0
                    groups = len({locks.ticket_group(t) for t in q} | {locks.ticket_group(ticket)})
                    tell(f"queued for a host-wide checks slot: {len(locks.held_labels(every))} of {n + extra(n)} running, "
                         f"{ahead} ahead, {groups} project{'s' if groups != 1 else ''}"
                         + (", first in its project as an open obligation's owner" if queued_as != holder else "")
                         + (f"; waiting for free cores: {short}" if (short := cores_short(every)) else "")
                         + (f"; waiting for the disk: {disk}" if (disk := diskgate.saturated()) else ""))
                if not told and waiting:
                    waiting(True)
                told = time.time()
            if max_wait_s is not None and time.time() - started >= max_wait_s:
                return None
            time.sleep(poll_s(POLL_S))
            project.ps_cycle()   # pids are read once per poll cycle, never one `ps` each (ps_snapshot)
    finally:
        locks.dequeue(ticket)
        _park(False, park, "", 0.0, None, None)
        if told and waiting:
            waiting(False)


def _park(parked: bool, park: Path | None, holder: str, since: float, project_name: str | None,
          priority: int | None) -> Path | None:
    """The PARKED ticket a run holds while it waits outside the queue for its test slots (acquire), at its
    arrival `since`; dropped once it no longer does."""
    if parked and park is None:
        park = _PARKS.park = locks.enqueue(slot_dir(), PARKED, holder, since, group=project_name, priority=priority)
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
             cap: int | None = None, lane_at: float | None = None) -> None:
    """Log a slot taken (record_wait) and tell it when it waited, here or before a relaunch."""
    now = time.time()
    record_wait(state, project_name, priority, since, now, owns=owns, cap=cap, lane_at=lane_at)
    if tell and (told or now - since > GRANT_TELL_S):
        tell(grant_line(priority, owns, since, now))


def _running(every: list[Path], same: str) -> str:
    """The holder of a slot whose checks have the key `same`, or ""."""
    if not same:
        return ""
    for path in every:
        if not locks.held_labels([path]):
            continue
        try:
            h = json.loads(path.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(h, dict) and h.get("key") == same:
            return str(h.get("holder") or "")
    return ""


def _room(n: int, every: list[Path]) -> int:
    return n + extra(n) - len(locks.held_labels(every))


def _take(n: int, ps: list[Path], every: list[Path], holder: str, what: str, same: str, room: int | None = None):
    """A slot of the cap, else one load opened beyond it, while fewer than cap plus extra() are held.
    The cap's own slots are tried first, so two takers racing never pass the cap with no extra open."""
    room = _room(n, every) if room is None else room
    if room <= 0 or cores_short(every) or diskgate.saturated():
        return None
    f = locks.try_take(ps, holder, what, key=same)
    if f is None and (x := extra(n)):
        f = locks.try_take(every[n:n + x], holder, what, key=same)
    return f


def _take_in_turn(n: int, ticket: Path, ps: list[Path], every: list[Path], holder: str, what: str, same: str):
    """locks.take_in_turn over the cap and the slots load opened beyond it."""
    room = _room(n, every)
    if room <= 0 or not locks.my_turn(slot_dir(), RESOURCE, ticket, room, fair=True, urgent_max=priority_turns(),
                                      aged_before=aged_before()):
        return None
    return _take(n, ps, every, holder, what, same, room)


def acquire_priority(key: str, holder: str, what: str = "", tell: Callable[[str], None] | None = None,
                     project_name: str | None = None, state: Path | None = None) -> Slot:
    """Take the priority slot of the push branch `key` (beyond the cap, ahead of every task waiting in
    the queue), saying so through `tell`. Only another push to the same branch can hold it; then this
    waits for that one alone. Cap off or nested: no slot, run at once. An unwritable slot directory
    raises Unavailable. With `state` the slot taken is logged there as for acquire."""
    n = limit()
    if not n or os.environ.get(HELD_ENV):
        return Slot()
    path = priority_path(key)
    started, told = time.time(), 0.0
    while True:
        try:
            f = locks.try_take([path], holder, what)
        except OSError as e:
            raise _unavailable(e) from e
        if f:
            if tell:
                q = len(locks.queued(slot_dir(), RESOURCE))
                tell(f"push checks took the push priority slot (beyond the host-wide cap of {n}: "
                     f"{len(locks.held_labels(all_paths(n)))} task checks running"
                     + (f", {q} queued behind" if q else "") + ")"
                     + (f" after {(time.time() - started) / 60:.1f} min" if told else ""))
            record_wait(state, project_name, None, started, time.time(), "push")
            return Slot(f)
        if tell and time.time() - told >= TELL_S:
            tell(f"waiting for the push priority slot, held by another push to the same branch: "
                 f"{_some(locks.held_labels([path])) or 'unknown'}")
            told = time.time()
        time.sleep(poll_s(POLL_S))


# Each slot taken, logged in the taker's project state as one JSON line: {"project", "task", "run",
# "priority", "slot": "queue" | "push", "queued_at", "granted_at"} (times in epoch seconds). A slot
# taken at once logs a wait of zero, so the median and p90 describe every checks run, not only the
# ones that queued. The log is bounded like activity.jsonl: past WAITS_MAX_BYTES it rotates to one
# old generation, read as well.
WAITS_LOG, WAITS_OLD, WAITS_LOCK = "checks-slot-waits.jsonl", "checks-slot-waits.1.jsonl", "checks-slot-waits.lock"
WAITS_MAX_BYTES = 256 * 1024
WAITS_WINDOW_S = 24 * 3600


def record_wait(state: Path | None, project_name: str | None, priority: int | None, queued_at: float,
                granted_at: float, slot: str = "queue", owns: list[str] | tuple = (),
                cap: int | None = None, lane_at: float | None = None) -> None:
    """Append one slot wait to `state`/WAITS_LOG and sync it, and the grant to the host-wide log
    (record_grant). Never raises: a log that cannot be written must not cost the checks their slot.
    No `state`: only the host-wide line is logged. `cap`: the project cap in force (checklane.project_cap,
    0: none), stored as "cap" so throughput judges the wait by it; None (no project lane): no field.
    `lane_at`: when the run got its repository's lane (`ttp checks` queues for it before this slot, from
    the same `queued_at`), stored as "lane_at": only the time after it waited for the host."""
    task = os.environ.get("TTP_TASK") or ""
    rec = {"project": project_name, "task": int(task) if task.isdigit() else None,
           "run": os.environ.get("TTP_RUN_ID") or None, "priority": priority, "slot": slot,
           "queued_at": round(queued_at, 3), "granted_at": round(granted_at, 3)}
    if cap is not None:
        rec["cap"] = int(cap)
    if lane_at is not None:
        rec["lane_at"] = round(lane_at, 3)
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
    pool's host line (testslots). Each: queue, ticket (its file name: one per wait), holder (the waiter's
    label), waited_s and held_by (who holds what it waits for: the checks slots, or the test pool's slots,
    each label with its count). Read only; an unreadable queue gives none."""
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
            out.append({"queue": queue, "ticket": t.name, "holder": holder or t.name,
                        "priority": locks.ticket_priority(t), "waited_s": now - locks._arrival(t),
                        "held_by": held_by})
    return out


def urgent_wait_text(w: dict) -> str:
    """One urgent_waits entry in words: the queue, the waiter, how long and what holds it."""
    held = "; ".join(w["held_by"][:8]) + (f"; and {len(w['held_by']) - 8} more" if len(w["held_by"]) > 8 else "")
    return (f"urgent checks {w['holder']} (priority {w['priority']:g}) have waited {w['waited_s'] / 60:.0f} min in "
            f"the host-wide {w['queue']} queue; held by: {held or 'nothing held right now (the next turn is due)'}")

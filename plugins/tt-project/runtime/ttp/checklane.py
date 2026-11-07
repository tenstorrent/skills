# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lanes of a project's `ttp checks` runs, so a cheap check never waits hours behind unrelated long ones.

Light checks never queue. A run whose every command is light (a syntax or compile check, a lint or
format check, a whitespace check, JSON or YAML validation: LIGHT_CMDS, plus the project's or account's
`checks.light_cmds`, plus the commands after `--` of a `ttp checks --light`) runs at once, without the
project's checks slot, the host-wide checks slot or test-worker slots (cli._checks_slot). It is still
run, recorded and judged like any check: a failing light check is a failure, never a pass. A command
is light only when each of its simple commands (split at ;, &&, |, ...) starts with a light pattern's
words in order, after only wrappers (env, nice, timeout ...) and with only extra options between them:
`python3 -m py_compile a.py && git diff --check` is light; `black . --check`, `bash script.sh -n` and
`ruff check . && pytest` are not.

Heavy lanes. Other runs take one of the project's `slots()` lanes (testslots.suite_hold), so that many
heavy checks of a project run at once, in task-priority order (#4065's order; one lane keeps the old
one-at-a-time slot). Lanes are per repository under check (testslots.repo_key), so checks of unrelated
repos never share them. `checks.slots` sets it (project, then account, then TTP_CHECKS_SLOTS); unset, it
follows the host now (capacity(), read again at every admission): the runs already holding a lane plus
as many more as the spare cores (cores less the CPU demand, checkcap._load) and the available memory fit,
at one run's cores (its test grant, at least CORES_PER_LANE) and GIB_PER_LANE each, at most the runs
the cores hold; never below default_slots(), its floor: the runs its test pool fits side by side, at
least 2 (each run is isolated: its own clean checkout, scratch, output folder and port block,
testslots.port_block), at most MAX_LANES, lowered only by little available memory. So a loaded or
short-of-memory host admits no run past the floor (checks.load_extra 0 keeps the floor: checkcap). What runs
cannot isolate keeps a lock of its own:
checks run in the authoring worktree (cwd "authoring") also hold that worktree's one lane
(testslots.tree_key). Runs behind checks.wrap_cmd take its wrapper lock's lane instead (below).

Wrapper locks. A run whose commands go behind the project's checks.wrap_cmd (a lock or queue command,
such as a shared device's lock) takes that wrapper lock's own single lane (wrap_key), not a repository
lane: runs behind one wrapper lock go one at a time, and lock-free runs (lint, other trees' tests,
harness checks) run in their repository's lanes beside it instead of waiting behind it. By default
(checks.wrap_lane "before") a run takes that lane before it starts the wrapper, so runs behind a wrapper
that fails instead of waiting never go side by side; one queued on the wrapper's lock then holds the lane
meanwhile. With checks.wrap_lane "after" (for a wrapper that waits for its lock, such as one that picks
its lock per run) it holds no lane while the wrapper queues: a step between the wrapper and each command
(lane_argv, wraplane) takes the lane once the wrapper runs that command, its lock held, and frees it when
the command ends or the step dies.

Project cap. At most project_cap() of a project's checks run at once over all its repository and
wrapper-lock lanes: checks.test_project_max (project, then account, then TTP_TEST_PROJECT_MAX; "auto" in
one skips the later ones, fixed_cap), else the runs the host's test pool fits side by side, at least its lanes per repository (slots()). It counts checks, never test workers: the project's test-worker budget is
this cap times one run's grant (testslots.project_max), so every check under the cap gets its slots, and
a run counts against the cap only once its test slots are granted (testslots.project_held), so a run
still waiting for them never keeps another from its turn. A lane under the cap goes to
the most urgent waiter of the project (task priority, 1 first, then age: testslots.cap_turn). A
priority-1 waiter whose lanes are held only by less urgent runs takes the one overflow lane beside them while
the host has room (headroom(): a free host-wide checks slot, live cores and memory for one run), never beside
a run in its own worktree (one suite per worktree); lane lines and wait events mark that holder.

Bounded wait (testslots.suite_hold). A lane whose holder process is gone, or whose heartbeat (the lane
file's mtime, touched every HEARTBEAT_S by the holder) is older than HEARTBEAT_DEAD_S, is reclaimed and
the reclaim logged (RECLAIM_LOG). A waiter that has waited past `checks.max_wait_s` (default
MAX_WAIT_S; 0 turns it off) and whose run is cheaper than every current holder's (cost(): the median
duration of this command list's recorded passing runs, else its class: light, other, test suite or
build) takes one of as many overflow lanes alongside them, not after. A targeted check (testpick.narrow)
is priced by its scope: its own median, else the full checks' median times its share of their tests, and
its lane label reads 'targeted: N tests' (scope_text).

Visible. A waiter says who holds the lanes (task, command, running for) and its queue position every
TELL_S. Each lane is named for people (lane_labels): 'repository <directory name>', 'wrapper lock
<command>' or, for a waiter whose lane is free but the project is at its cap, 'project cap
(checks.test_project_max)'. `ttp status` and the web app show one line per lane (lane_lines: who runs,
who is held and what each waits on); `ttp status` and the coordinator digest list waits over LONG_WAIT_S
(long_wait_lines), and the
daemon records one `checks_slot_wait` event per wait episode past `checks.wait_event_s` (default
WAIT_EVENT_S; 0 turns it off) naming holder and waiter (wait_events). Only one of them wakes the coordinator
per queue incident (wait_wake: from the first waiter past the threshold until none is); the incident wakes it
again only when a decision is due: its longest waiter past `checks.wait_escalate_s` (default WAIT_ESCALATE_S,
then once per as long again; 0: never), a holder running `checks.wait_hang_factor` (default WAIT_HANG_FACTOR;
0: never) times the median run of its own command list (scope_median_s: a full suite is never timed against
targeted runs) whose log has also stopped growing (checkstall; a likely hang: one still printing results is
named slow under load, with the host's load, and wakes nobody), or a holder whose task has ended (no owner
to fix it).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import statistics
import sys
import time
from pathlib import Path

from . import checkcap, locks, project, testslots

# The default light checks: each a command's leading words (an option word must be present at its place).
LIGHT_CMDS = (
    "python -m py_compile", "python -m compileall", "python -m json.tool", "python -m tabnanny",
    "ruff check", "ruff format --check", "black --check", "isort --check-only", "isort --check",
    "flake8", "pyflakes", "pycodestyle", "git diff --check", "shellcheck", "bash -n", "sh -n",
    "yamllint", "jq", "json_pp", "xmllint --noout", "clang-format --dry-run", "gofmt -l",
    "prettier --check", "markdownlint", "claude plugin validate",
)
CORES_PER_LANE = 8
GIB_PER_LANE = 8
MAX_LANES = 4
MAX_WAIT_S = 300.0
WAIT_EVENT_S = 900.0
WAIT_ESCALATE_S = 7200.0  # a queue incident wakes the coordinator again once its longest wait passes this
WAIT_HANG_FACTOR = 3.0    # a holder running this many times the recent median checks run is a likely hang if stalled
HANG_CPU_IDLE_S = 300.0   # ... and, where the daemon reads its tree's CPU, idle this long (five readings)
INCIDENT_KEY = "checks_wait_incident"   # kv: the open queue incident (wait_wake)
LONG_WAIT_S = 600.0       # ttp status and the digest list a wait longer than this
TELL_S = 60.0             # a waiter says who holds the lanes this often
HEARTBEAT_S = 30.0        # a holder touches its lane file this often
HEARTBEAT_DEAD_S = 300.0  # a lane file untouched this long has a dead holder
DONE_FLAG_S = 300.0       # a lane still held by a finished task's check is flagged this long after the task ended
DONE_GRACE_S = 3600.0     # ... and its own detached checks stopped after this, when no run time is known
FINISHED = ("done", "failed", "cancelled", "exhausted")
RECLAIM_LOG = "checks-lane-reclaims.jsonl"
RECLAIM_KEEP = 50
SLOTS_ENV = "TTP_CHECKS_SLOTS"
# Run classes, cheapest first: the fallback when a command list has no recorded duration.
LIGHT, OTHER, SUITE = 0, 1, 2
CLASS_NAMES = {LIGHT: "light", OTHER: "other", SUITE: "test suite or build"}
_SUITE_WORDS = {"pytest", "py.test", "make", "gmake", "ninja", "ctest", "tox", "nox", "cargo", "npm", "yarn",
                "go", "bazel", "gradle", "mvn"}


def setting(cfg: dict | None, key: str):
    """checks.<key> from the project's config `cfg`, else the account's; None when neither sets it."""
    for src in (cfg or {}, project.load_account_settings()):
        sec = src.get("checks") if isinstance(src, dict) else None
        if isinstance(sec, dict) and sec.get(key) not in (None, ""):
            return sec.get(key)
    return None


def _seconds(cfg: dict | None, key: str, default: float) -> float:
    try:
        v = float(setting(cfg, key))
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def max_wait_s(cfg: dict | None) -> float:
    return _seconds(cfg, "max_wait_s", MAX_WAIT_S)


def wait_event_s(cfg: dict | None) -> float:
    return _seconds(cfg, "wait_event_s", WAIT_EVENT_S)


def priority_age_s(cfg: dict | None) -> float:
    """checks.priority_age_s: seconds a waiter for a lane waits per step it ranks more urgent
    (testslots.suite_priority), default testslots.SUITE_AGE_S. 0 or below is not allowed (aging is what
    keeps a low-priority waiter from starving), nor is an infinite step, so either gives the default."""
    v = _seconds(cfg, "priority_age_s", testslots.SUITE_AGE_S)
    return v if 0 < v < float("inf") else testslots.SUITE_AGE_S


def wait_escalate_s(cfg: dict | None) -> float:
    return _seconds(cfg, "wait_escalate_s", WAIT_ESCALATE_S)


def wait_hang_factor(cfg: dict | None) -> float:
    return _seconds(cfg, "wait_hang_factor", WAIT_HANG_FACTOR)


def _mem_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return None


def default_slots(n_cores: int | None = None, mem_gib: float | None = None, cfg: dict | None = None) -> int:
    """Lanes on this machine: as many runs as the host's test pool fits side by side (testslots.sizes:
    pool // run_max; with the pool off, one per CORES_PER_LANE cores), at least 2, at most MAX_LANES,
    as each run has its own checkout, scratch, output folder and ports (cli._checks_slot). Only little
    available memory (one lane per GIB_PER_LANE GiB) lowers it, to 1 at the least."""
    c = checkcap.cores() if n_cores is None else n_cores
    m = _mem_gib() if mem_gib is None else mem_gib
    s = testslots.sizes(cfg, cores=c)
    n = max(2, min(MAX_LANES, s[0] // s[1] if s else c // CORES_PER_LANE))
    if m is not None:
        n = min(n, int(m // GIB_PER_LANE))
    return max(1, n)


def host_slots(n_cores: int, mem_gib: float | None, load: float | None, running: int) -> int:
    """Free check slots on another host (checkspill.probe), from what it has idle now rather than a small
    fixed count: the runs its idle cores (`n_cores` less the 1-minute `load`, which counts the runs under
    way) and available memory (`mem_gib`, GIB_PER_LANE each) fit at one run's cores (run_cores), at most
    the runs its cores hold less the `running` checks there. An unread load: default_slots less `running`."""
    if load is None:
        return max(0, default_slots(n_cores, mem_gib) - running)
    run = run_cores(None, n_cores)
    n = min(int(max(0.0, n_cores - load) // run), max(1, n_cores // run) - running)
    if mem_gib is not None:
        n = min(n, int(mem_gib // GIB_PER_LANE))
    return max(0, n)


def run_cores(cfg: dict | None = None, n_cores: int | None = None) -> int:
    """One checks run's cores: its test grant (testslots.sizes' run_max), at least CORES_PER_LANE (a
    full required-suite run measured at about that many cores at its peak)."""
    s = testslots.sizes(cfg, cores=n_cores)
    return max(CORES_PER_LANE, s[1] if s else 0)


def capacity(cfg: dict | None = None, held: int = 0, n_cores: int | None = None, load: float | None = None,
             mem_gib: float | None = None, used: int | None = None) -> dict:
    """The lanes of one repository the host runs now with checks.slots unset (above): `held` running plus
    the runs its spare cores (cores less the CPU demand, checkcap._load, which counts the running ones) and
    available memory fit, at one run's cores (`run_cores`: its test grant, testslots.sizes, at least
    CORES_PER_LANE) and GIB_PER_LANE each; at most `top` (the runs the cores hold, and the runs the test pool
    fills at a full grant: testslots.pool_now with `used` of its slots held, over run_max), never below
    `floor` (default_slots()), so no lane opens to start on a grant of one or two workers. An unreadable
    load average fits none, as does checks.load_extra 0 (nothing grows with the load: checkcap.extra_max).
    Returns n and its inputs."""
    c = checkcap.cores() if n_cores is None else n_cores
    m = _mem_gib() if mem_gib is None else mem_gib
    ld = checkcap._load() if load is None else load
    floor = default_slots(c, m, cfg)
    run = run_cores(cfg, c)
    top = c // run
    if s := testslots.sizes(cfg, cores=c):
        if used is None:
            used = len(locks._slot_labels(testslots.paths(max(s[0], c))))
        top = min(top, max(1, testslots.pool_now(cfg, s[0], used, cores=c, load=ld) // s[1]))
    top = max(floor, top)
    fit = min(top, int(max(0.0, c - ld) // run)) if ld == ld and checkcap.extra_max() else 0
    if m is not None:
        fit = min(fit, int(max(0.0, m) // GIB_PER_LANE))
    held = max(0, int(held or 0))
    return {"n": max(floor, min(top, held + fit)), "floor": floor, "top": top, "held": held, "fit": fit,
            "cores": c, "load": ld, "mem_gib": m, "run_cores": run, "run_gib": GIB_PER_LANE}


def _explicit_slots(cfg: dict | None) -> int | None:
    for v in (setting(cfg, testslots.SLOTS_KEY), os.environ.get(SLOTS_ENV)):
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n >= 1:
            return n
    return None


def slots(cfg: dict | None, held: int = 0) -> int:
    """The project's heavy lanes per repository with `held` of them running: checks.slots (project, then
    account), TTP_CHECKS_SLOTS, else capacity()."""
    n = _explicit_slots(cfg)
    return n if n is not None else capacity(cfg, held)["n"]


def slots_from(cfg: dict | None) -> str:
    """Where slots() takes its count, for a wait line: "" when checks.slots or TTP_CHECKS_SLOTS sets it
    (the line's default), else that the host's load sizes it now."""
    return "" if _explicit_slots(cfg) is not None else f"sized from the host's load now (checks.{testslots.SLOTS_KEY} unset)"


def _awaits_grants(rec: dict) -> bool:
    """Whether a lane holder's record is a run taken with `running` still waiting for its host grants:
    testslots.suite_hold does not count such a lane against its repository's lanes (n_held)."""
    return bool(rec.get("running")) and rec.get("granted") is False


def _held_numbered(base: str) -> int:
    """How many of lane resource `base`'s numbered lanes count as held the way suite_hold counts them:
    every held one, less those whose `running` run still waits for its grants."""
    return sum(1 for x in testslots._lanes_of(base) if not x.name.startswith(base + "-x") and locks._slot_labels([x])
               and not _awaits_grants((locks.holder_records([x]) or [{}])[0]))


def held_lanes(project_name: str, repo: str = "") -> int:
    """How many of repository `repo`'s numbered lanes (not overflow ones) are held now (_held_numbered)."""
    return _held_numbered(testslots.suite_base(project_name, repo))


def slots_now(project_name: str, repo: str, cfg: dict | None) -> int:
    """slots() of repository `repo` with its lanes held now."""
    return slots(cfg, held_lanes(project_name, repo))


def slots_note(cfg: dict | None, held: int = 0) -> str:
    """How slots() came to its number, for `ttp status` and the checks_slot_wait event."""
    if (n := _explicit_slots(cfg)) is not None:
        return f"{n} at a time per repository; checks.slots raises it"
    k = capacity(cfg, held)
    mem = f"{k['mem_gib']:.0f} GiB available" if k["mem_gib"] is not None else "memory unknown"
    ld = f"load {k['load']:.1f}" if k["load"] == k["load"] and k["load"] != float("inf") else "load unknown"
    return (f"{k['n']} per repository now, from the host (checks.slots unset): {k['held']} running + {k['fit']} "
            f"more fit ({k['cores']} cores, {ld}, {mem}; one run {k['run_cores']} cores, {k['run_gib']} GiB), "
            f"at least {k['floor']}, at most {k['top']}")


CAP_AUTO = "auto"   # checks.test_project_max "auto": sized from the host, whatever a later source fixes


def fixed_cap(cfg: dict | None) -> tuple[int, str] | None:
    """The fixed checks.test_project_max and where it comes from ("project", "account" or "environment"):
    the first source, in that order, that sets a whole number of at least 1. None when none does, or when
    an earlier one says "auto" (CAP_AUTO): a project's "auto" keeps an account's fixed number off it. A
    layered config (project.layered) carries the account's value as its own: one equal to it reads "account"."""
    own = ((cfg or {}).get("checks") or {}) if isinstance((cfg or {}).get("checks"), dict) else {}
    acct = setting({}, "test_project_max")
    mine = own.get("test_project_max")
    for v, src in ((mine, "account" if acct is not None and str(mine).strip() == str(acct).strip() else "project"),
                   (acct, "account"), (os.environ.get("TTP_TEST_PROJECT_MAX"), "environment")):
        if str(v).strip().lower() == CAP_AUTO:
            return None
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n >= 1:
            return n, src
    return None


def project_cap(cfg: dict | None, fixed: bool = True) -> int:
    """The most of a project's checks that run at once (above): checks.test_project_max (fixed_cap: project,
    then account, then TTP_TEST_PROJECT_MAX; "auto" in one skips the later ones), else the runs the host's
    test pool fits side by side, at least its lanes per repository (slots()) and, with checks.slots unset, the
    runs the host's cores hold (capacity()'s top, whose lanes admit only what the load and memory fit); 0
    (unset with the test pool off): no cap. `fixed` False: that host-sized cap, whatever is set."""
    if fixed and (f := fixed_cap(cfg)):
        return f[0]
    s = testslots.sizes(cfg)
    return max(slots(cfg), s[0] // s[1], capacity(cfg)["top"] if _explicit_slots(cfg) is None else 0) if s else 0


CAP_FROM = {"project": "the project's config", "account": "the account settings", "environment": "TTP_TEST_PROJECT_MAX"}


def cap_note(cfg: dict | None) -> str:
    """For a fixed checks.test_project_max under the host-sized cap (project_cap(fixed=False)): where it is
    set and what the host sizes now, so a stale fixed number holding checks back shows where they wait; ""
    when nothing is fixed or the fixed number is not the lower."""
    f = fixed_cap(cfg)
    if not f or (host := project_cap(cfg, fixed=False)) <= f[0]:
        return ""
    return f" {f[0]}, set in {CAP_FROM[f[1]]}; the host fits {host} now, {CAP_AUTO!r} follows the host"


NAME_SUFFIX = ".name"   # beside a lane's files: its repository's or wrapper command's name (remember_name)
CAP_LABEL = "project cap (checks.test_project_max)"


def wrap_name(wrap) -> str:
    """A wrapper lock's name for its lane's label: checks.wrap_cmd's words, its program by its base name,
    without a closing `--`, at most 60 characters; "" for none."""
    words = [str(w) for w in (wrap or ())]
    while words and words[-1] == "--":
        words.pop()
    if not words:
        return ""
    words[0] = os.path.basename(words[0]) or words[0]
    text = " ".join(words)
    return text if len(text) <= 60 else text[:59] + "…"


def _name_path(project_name: str, key: str) -> Path:
    return checkcap.slot_dir() / (testslots.suite_base(project_name, key) + NAME_SUFFIX)


def remember_name(project_name: str | None, key: str, name: str) -> None:
    """Keep lane `key`'s name (testslots.repo_name or wrap_name) beside its files, for lane_labels:
    written only when it differs, by a rename, so a reader never sees half of it (a cache: no sync)."""
    if not (project_name and key and name):
        return
    path = _name_path(project_name, key)
    try:
        if path.read_text() == name:
            return
    except OSError:
        pass
    try:
        project.durable_write(path, name, sync=False)
    except OSError:
        pass


def group_name(key: str, name: str = "") -> str:
    """What a lane key names when lane_labels has no name for it (its fallback, the one naming rule): a
    wrapper lock, a repository, or an unkeyed group (an older runtime's one lane per project), with `name`
    (the project folder's name for the unkeyed group) beside the key when given. Never empty."""
    if key.startswith("w"):
        return f"wrapper lock {key[1:]} (checks.wrap_cmd, one at a time)"
    if not key:
        return (f"repository {name} (not keyed: an older runtime, one lane per project)" if name
                else "not keyed (no repository key: an older runtime, one lane per project)")
    return f"repository {name} (key {key})" if name and name != key else f"repository {key}"


def _lane_name(project_name: str, key: str, known: dict | None = None) -> str:
    """Lane `key`'s name: the one a run kept beside it (remember_name), else `known[key]` (a holder's
    repo_name, or the caller's name for its own repository or the unkeyed group); "" for none."""
    if key:
        try:
            got = " ".join(_name_path(project_name, key).read_text().split())[:80]
            if got:
                return got
        except OSError:
            pass
    return " ".join(str((known or {}).get(key) or "").split())[:80]


def lane_labels(project_name: str, keys, cfg: dict | None = None, known: dict | None = None) -> dict:
    """Each lane key's label for a person: 'repository <directory name>' (with a short id when two of
    `keys` share a name), 'wrapper lock <command>' (wrap_name), 'not keyed (...)' for an unkeyed
    lane (group_name, with the project folder's name when `known` gives it). A name no run recorded
    (an older runtime took the lane) is `known[key]` (_lane_name), else the project's checks.wrap_cmd
    when its key matches, else the id (group_name)."""
    try:
        from . import push
        own = wrap_key(push.wrap_of(cfg)), wrap_name(push.wrap_of(cfg))
    except ValueError:
        own = "", ""
    names = {}
    for k in keys:
        names[k] = (_lane_name(project_name, k, known)
                    or (own[1] if k.startswith("w") and own[0] and k[1:] == own[0] else ""))
    seen = [n for k, n in names.items() if n and k and not k.startswith("w")]
    out = {}
    for k, n in names.items():
        if not k:
            out[k] = group_name(k, n)
        elif not n:
            out[k] = group_name(k)
        elif k.startswith("w"):
            out[k] = f"wrapper lock {n}"
        else:
            out[k] = f"repository {n}" + (f" ({k[:6]})" if seen.count(n) > 1 else "")
    return out


def _lane_waits_on(project_name: str, base: str, key: str, q: list, cfg: dict | None, label: str) -> list[str]:
    """What each waiter in lane `base`'s queue `q` (in turn order) waits on: the lane itself while its
    lanes are all held; with one free, the project cap only while the project's running checks fill it
    (testslots.cap_turn's count: testslots.project_held), else the host gate a run of the project holding a
    lane waits on (_host_gate: the test-slot pool or the load guard), else nothing (about to start)."""
    held = _held_numbered(base)
    n = 1 if key.startswith("w") else max(1, slots(cfg, held))
    if held >= n:
        return [label for _ in q]
    cap = project_cap(cfg)
    on = CAP_LABEL if cap > 0 and len(testslots.project_held(project_name)) >= cap else \
        _host_gate(project_name) or "nothing (about to start)"
    return [on for _ in q]


HOST_GATE = "the host's gate: "   # _host_gate's prefix (waits()' limit "host")


def _host_gate(project_name: str) -> str:
    """What holds the project's checks below its cap: the host-wide gate (the gate pool_gate recorded, else
    what it waits for: testslots.gate_lanes, lane_waits) of a run of the project that holds a lane and
    still waits for its test slots; "" when none does."""
    for r in locks.holder_records(testslots.lane_paths(project_name, repo=None)):
        if r.get("granted") is False and (r.get("gate") or r.get("waits")):
            return HOST_GATE + " ".join(str(r.get("gate") or r.get("waits")).split())[:200]
    return ""


def _pid_queued(pid, project_name: str = "") -> str:
    """What a lane holder that is not running yet waits on: the host-wide checks slot, the host's
    test-worker slots or its project's test-worker budget (its ticket in their queue), else "" (it is
    starting, or an older runtime holds it)."""
    for res, what in ((checkcap.RESOURCE, "the host-wide checks slot"),
                      (testslots.TESTS.resource, "the host's test-worker slots"),
                      *(((f"{testslots.TESTS.own}.{locks.group_id(project_name)}",
                          f"its project's test-worker budget (checks.{testslots.TESTS.setting(testslots.TESTS.budget)[1]})"),) if project_name else ())):
        for t in _live_tickets(res):
            m = re.match(r"\d+(?:\.\d+)?-(\d+)", t.name)
            if pid and m and int(m.group(1)) == pid:
                return what
    return ""


def _parked(rec: dict) -> str:
    """What a lane holder parked outside the host queue waits on (checkcap.acquire's `parks`, its lane
    labelled testslots.PARKED_WAITS), with the gate that holds it (testslots.gate_lanes), else ""."""
    if rec.get("waits") != testslots.PARKED_WAITS:
        return ""
    return testslots.PARKED_WAITS + ", outside the host queue" + (f"; held by {g}" if (g := rec.get("gate")) else "")


def wrap_key(wrap) -> str:
    """The id of the wrapper lock a run's commands go behind (checks.wrap_cmd as argv words); "" for none.
    The one wrapper-lock detector: any wrapper, however it locks, gets its own single lane. A wrapper
    that fails instead of waiting (`flock -n`, `flock -w 60`, a queue that refuses when busy) is never
    taken to serialize by itself: its runs go one at a time in that lane, never side by side in the
    repository's lanes, so two runs of one project never fail each other's lock."""
    return hashlib.sha256(shlex.join(map(str, wrap)).encode()).hexdigest()[:12] if wrap else ""


LANE_AFTER_WAIT_S = 300.0   # checks.wrap_lane "after": the longest the step waits for the lane, then runs without it


def lane_argv(project_name: str, key: str, name: str = "", wait_s: float = LANE_AFTER_WAIT_S, who: str = "",
              priority: int | None = None) -> tuple[str, ...]:
    """The step (wraplane) that runs between checks.wrap_cmd and a command with checks.wrap_lane "after",
    taking project `project_name`'s lane of wrapper lock `key` (wrap_key) once the wrapper runs it; `name`
    labels the lane (wrap_name). `who` and `priority`: the run's label part and task priority, as
    cli._checks_slot_take gives testslots.suite_hold ("run <run folder>"), so the lane's label is the one
    the detectors read (_run_of, open_precheck). Isolated python (-I): the checkout's files and PYTHONPATH
    never load."""
    runtime = str(Path(__file__).resolve().parent.parent)
    return (sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
            "from ttp.wraplane import main; sys.exit(main(sys.argv[2:]))", runtime,
            "--project", project_name, "--key", key, "--name", name, "--wait-s", f"{wait_s:g}",
            *(("--who", who) if who else ()), *(("--priority", str(priority)) if priority is not None else ()), "--")


def headroom() -> bool:
    """Whether the host has room for a priority-1 run beside the lanes' holders: the host-wide checks cap
    (checks.host_max, with the slots load opens beyond it) has a free slot, the live free cores cover one
    check (checkcap.cores_short) and the available memory one lane (GIB_PER_LANE). Never the load average
    alone: the routine suites the run would start beside keep it high (#5201 waited 12 min first in line
    at a load over the cores with host slots free)."""
    n = checkcap.limit()
    every = checkcap.all_paths(n)
    if n and checkcap._room(n, every) <= 0:
        return False
    if checkcap.cores_short(every):
        return False
    m = _mem_gib()
    return m is None or m >= GIB_PER_LANE


def light_patterns(cfg: dict | None) -> list[list[str]]:
    """LIGHT_CMDS and checks.light_cmds (a list, or one string split at commas and newlines), as words."""
    extra = setting(cfg, "light_cmds")
    if isinstance(extra, str):
        extra = re.split(r"[,\n]", extra)
    pats = list(LIGHT_CMDS) + [str(x) for x in (extra if isinstance(extra, list) else [])]
    return [w for w in (p.split() for p in pats) if w]


def _word(w: str) -> str:
    w = testslots._base(w)
    return "python" if re.fullmatch(r"python[\d.]*", w) else w


def _starts(words: list[str], pat: list[str]) -> bool:
    """Whether `words` start with `pat`'s words in order, with only extra options between them."""
    i = 0
    for w in words:
        if i == len(pat):
            return True
        if (_word(w) if i == 0 else w) == (_word(pat[0]) if i == 0 else pat[i]):
            i += 1
        elif not (i and w.startswith("-")):
            return False
    return i == len(pat)


def is_light(cmd: str, patterns: list[list[str]]) -> bool:
    """Whether every simple command of shell line `cmd` is a light check (after its wrappers)."""
    try:
        segs = testslots._segments(str(cmd))
    except (ValueError, IndexError):
        return False
    if not segs:
        return False
    for seg in segs:
        words = [w for _, _, w in seg]
        i = 0
        while i < len(words) and (re.match(r"[A-Za-z_]\w*=|-|\d", words[i]) or testslots._base(words[i]) in testslots._WRAPPERS):
            i += 1
        words = words[i:]
        if not words or testslots._shell_value(words[0]) or not any(_starts(words, p) for p in patterns):
            return False
    return True


def no_device(cmd, cfg: dict | None, declared: bool = False) -> bool:
    """Whether `cmd` needs no device, so it runs without checks.wrap_cmd (the project's lock or queue
    wrapper) and its lane: `declared` (ttp checks --no-device), or each of its simple commands starts with
    a pattern of checks.no_device_cmds (a list, or one string split at commas and newlines), as is_light."""
    if declared:
        return True
    pats = setting(cfg, "no_device_cmds")
    if isinstance(pats, str):
        pats = re.split(r"[,\n]", pats)
    pats = [w for w in (str(x).split() for x in (pats if isinstance(pats, list) else [])) if w]
    return bool(pats) and is_light(str(cmd), pats)


def all_light(cmds: list, cfg: dict | None, declared=()) -> bool:
    """Whether a run of `cmds` is light: each is light or among `declared` (ttp checks --light)."""
    pats, declared = light_patterns(cfg), {str(c) for c in declared}
    return bool(cmds) and all(str(c) in declared or is_light(str(c), pats) for c in cmds)


def run_class(cmds: list, cfg: dict | None = None) -> int:
    """LIGHT, OTHER or SUITE: the costliest of `cmds` (a test runner or build word anywhere, a shell's -c
    script included, is SUITE)."""
    pats, out = light_patterns(cfg), LIGHT
    for c in cmds:
        if is_light(str(c), pats):
            continue
        out = max(out, SUITE if _SUITE_WORDS & _words(str(c)) else OTHER)
    return out


def _words(c: str, depth: int = 0) -> set[str]:
    """The words of command line `c`, with those of a shell's -c script (`sh -c "pytest ... && claude ..."`,
    `bash -lc ...`) read the same way checkspill.needs_cli reads them."""
    from .checkspill import _SHELLS
    try:
        segs = testslots._segments(c)
    except (ValueError, IndexError):
        return set()
    got = set()
    for seg in segs:
        words = [w for _, _, w in seg]
        got.update(map(_word, words))
        i = 0
        while i < len(words) and (re.match(r"[A-Za-z_]\w*=|-|\d", words[i]) or testslots._base(words[i]) in testslots._WRAPPERS):
            i += 1
        if depth < 3 and i < len(words) and testslots._base(words[i]) in _SHELLS:
            opt = next((j for j in range(i + 1, len(words)) if re.fullmatch(r"-[a-z]*c[a-z]*", words[j])), None)
            if opt is not None and opt + 1 < len(words):
                got |= _words(words[opt + 1], depth + 1)
    return got


def median_s(state: Path | None, key: str) -> float | None:
    """The median wall time of the recorded passing runs of command list `key` (checkwall.record_run)."""
    from . import checkwall
    if state is None or not key:
        return None
    got = []
    try:
        lines = (Path(state) / checkwall.RUNS_LOG).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            r = json.loads(line)
            if r.get("k") == key and float(r["s"]) > 0:
                got.append(float(r["s"]))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return statistics.median(got[-checkwall.RECENT:]) if got else None


def scope(cmds: list) -> dict | None:
    """The narrowed size of `cmds` (testpick.narrow's targeted checks): {"tests": selected, "of": their
    checks' tests}, summed over the targeted ones; None when none is targeted."""
    got = [c.scope for c in cmds if getattr(c, "targeted", False) and isinstance(getattr(c, "scope", None), dict)
           and isinstance(c.scope.get("tests"), int) and isinstance(c.scope.get("of"), int)]
    return {"tests": sum(x["tests"] for x in got), "of": sum(x["of"] for x in got)} if got else None


def scope_text(cmds: list) -> str:
    """`cmds` for a lane's label and status: a targeted check as 'targeted: N tests', others as written."""
    def one(c) -> str:
        n = (scope([c]) or {}).get("tests")
        return str(c) if n is None else f"targeted: {n} test{'' if n == 1 else 's'}"
    return " && ".join(one(c) for c in cmds)


def cost(state: Path | None, key: str, cmds: list, cfg: dict | None = None, full_key: str = "") -> dict:
    """What a run costs, as its lane label keeps it: {"cls": run_class, "est_s": median_s or absent}, and
    for a targeted run (scope) its "tests" and "of". A targeted run's est_s is its own recorded median
    (`key` keeps its selection), else the median of the full checks it narrows (`full_key`) times its share
    of their tests, the whole median while another of `cmds` is a full suite."""
    est, sc = median_s(state, key), scope(cmds)
    if est is None and sc and full_key and (whole := median_s(state, full_key)) is not None:
        full = [c for c in cmds if not getattr(c, "targeted", False) and run_class([c], cfg) == SUITE]
        est = whole if full else whole * sc["tests"] / max(1, sc["of"])
    return {"cls": run_class(cmds, cfg), **({"est_s": round(est, 1)} if est is not None else {}), **(sc or {})}


def _targeted_only(mine: dict, theirs: dict) -> bool:
    """Whether `mine` is a targeted run (its "tests") and `theirs` a full one of its class or costlier."""
    t = theirs.get("cls")
    return (isinstance(mine.get("tests"), int) and not theirs.get("tests")
            and int(mine.get("cls", SUITE)) <= (int(t) if isinstance(t, int) else SUITE))


def cheaper(mine: dict | None, theirs: dict) -> bool:
    """Whether a run of cost `mine` is cheaper than a holder's (`theirs`, its lane label): by both medians
    when both are known, else a targeted run is cheaper than a full one of its class, else by class (a holder
    without one, from an older runtime, is a SUITE run)."""
    if not mine:
        return False
    a, b = mine.get("est_s"), theirs.get("est_s")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a < b
    if _targeted_only(mine, theirs):
        return True
    t = theirs.get("cls")
    return int(mine.get("cls", SUITE)) < (int(t) if isinstance(t, int) else SUITE)


CHEAP_FACTOR = 4.0   # a run this many times cheaper than an aged parked one goes before it (much_cheaper)


def much_cheaper(mine: dict | None, theirs: dict | None) -> bool:
    """Whether a run of cost `mine` costs at most 1/CHEAP_FACTOR of `theirs` (both lane labels' costs): by
    both medians when both are known, else a targeted run of at most that share of its tests against a
    full one of its class. Unknown costs never are."""
    if not mine or not theirs:
        return False
    a, b = mine.get("est_s"), theirs.get("est_s")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a * CHEAP_FACTOR <= b
    return _targeted_only(mine, theirs) and mine["tests"] * CHEAP_FACTOR <= max(1, int(mine.get("of") or 0))


def run_costs(project_name: str | None) -> dict[str, dict]:
    """The cost (lane label "cost") of each run holding one of `project_name`'s checks lanes, by run id."""
    if not project_name:
        return {}
    out = {}
    try:
        for b in testslots.suite_bases(project_name):
            for r in locks.holder_records(testslots._lanes_of(b)):
                if (run := _run_of(r.get("holder"))) and isinstance(r.get("cost"), dict):
                    out[run] = r["cost"]
    except OSError:
        return {}
    return out


def done_flag_s(cfg: dict | None) -> float:
    return _seconds(cfg, "done_holder_flag_s", DONE_FLAG_S)


def done_grace_s(cfg: dict | None, rec: dict) -> float:
    """How long after its task ended a check still holding a lane may run on before it is stopped:
    checks.done_holder_grace_s, else one and a half of the check's usual run time (its label's est_s,
    median_s), else its wall-time cap, else DONE_GRACE_S; never under done_flag_s."""
    v = setting(cfg, "done_holder_grace_s")
    try:
        if v is not None and float(v) >= 0:
            return max(float(v), done_flag_s(cfg))
    except (TypeError, ValueError):
        pass
    est = (rec.get("cost") or {}).get("est_s") if isinstance(rec.get("cost"), dict) else None
    cap = rec.get("cap_s")
    base = (1.5 * float(est) if isinstance(est, (int, float)) and est > 0
            else float(cap) if isinstance(cap, (int, float)) and cap > 0 else DONE_GRACE_S)
    return max(base, done_flag_s(cfg))


def finished_holders(p, cfg: dict | None, now: float | None = None) -> list[dict]:
    """The project's lanes held by a check whose task has ended (FINISHED) for done_flag_s or longer (0:
    off): lane, pid, task, run, status, ended_s (since the task ended, or the lane was taken if later),
    held_s, grace_s (done_grace_s) and stop (ended_s past grace_s). Read only."""
    now = time.time() if now is None else now
    flag = done_flag_s(cfg)
    if flag <= 0:
        return []
    out = []
    for base in testslots.suite_bases(p.name):
        for path in testslots._lanes_of(base):
            for rec in locks.holder_records([path]):
                task = str(rec.get("task") or "")
                if not task.isdigit() or rec.get("alive") is False:
                    continue
                row = p.db.one("SELECT status, updated FROM tasks WHERE id=?", (int(task),))
                if not row or row["status"] not in FINISHED or open_precheck(p.db, task, rec.get("holder")):
                    continue
                since = float(rec.get("since") or now)
                # a check still waiting for a slot refreshes 'since' every cycle: date it by its arrival
                arrived = float(rec.get("queued") or since)
                ended = now - max(float(row["updated"] or 0), arrived)
                if ended < flag:
                    continue
                grace = done_grace_s(cfg, rec)
                out.append({"lane": str(path), "pid": rec.get("pid"), "task": task, "run": _run_of(rec.get("holder")),
                            "precheck": _precheck_of(rec.get("holder")), "status": row["status"], "since": since, "ended_s": ended, "held_s": now - since,
                            "grace_s": grace, "stop": ended >= grace or (bool(_precheck_of(rec.get("holder"))) and not rec.get("run_since")), "command": str(rec.get("command") or "")})
    return out


PRECHECK_LABEL = "precheck:"   # daemon.PRECHECK_LABEL: the review task's label naming its precheck folder
# Review statuses that still read their precheck: queued (Daemon._precheck_holds, _precheck_failed at
# dispatch), running (the reviewer reads its result) and waiting (a reviewer may wait on it). A review past
# them (pushing in the push queue, done, failed) reads it no more.
PRECHECK_READERS = ("queued", "running", "waiting")


def precheck_read(db, name: str) -> bool:
    """Whether a review labelled precheck:<name> is still in PRECHECK_READERS."""
    want = PRECHECK_LABEL + name
    rows = db.q("SELECT labels FROM tasks WHERE kind='review' AND status IN (?,?,?) AND labels LIKE ?",
                (*PRECHECK_READERS, f"%{want}%"))
    for r in rows:
        try:
            if want in json.loads(r["labels"] or "[]"):
                return True
        except (ValueError, TypeError):
            continue
    return False


def open_precheck(db, task: str, holder) -> bool:
    """Whether lane holder label `holder` of task `task` is the daemon's review precheck of that task
    (Daemon._precheck: `ttp checks` run as the code task, its run folder prechecks/t<task>-<head>) and a
    review that reads it (labelled precheck:t<task>-<head>, precheck_read) is still open. Such a check starts as its
    task ends, so it is not a finished task's lane until that review ends too. Its lane label names its
    run folder ("run t<task>-<head>", cli._checks_slot); its slot labels name its run id
    ("run precheck-t<task>-<head>", daemon.precheck_run_id)."""
    m = re.search(r"\brun (?:precheck-)?(t(\d+)-[0-9a-f]{7,40})\b", str(holder or ""))
    if db is None or not m or m.group(2) != str(task):
        return False
    return precheck_read(db, m.group(1))


def stop_finished(p, h: dict) -> dict | None:
    """Stop finished_holders' entry `h` through jobstop.end_checks, only when its pid is the detached
    `ttp checks` of its own run (checks.pid in that run's folder), or the wraplane step under it (holding a
    checks.wrap_lane "after" lane; no other process under it), and that run is its task's: never any other process. None
    when it is not, or it already ended."""
    from . import jobstop, jobuse
    from .cli import _read_checks_pid
    if not (h.get("task") and isinstance(h.get("pid"), int)):
        return None
    if h.get("run"):
        row = p.db.one("SELECT dir, task FROM runs WHERE id=?", (int(h["run"]),))
    elif re.fullmatch(r"t(\d+)-[0-9a-f]{7,40}", str(h.get("precheck") or "")):
        # the daemon's own review precheck: its run folder is state/prechecks/t<task>-<head> (no runs row)
        row = {"dir": str(p.state / "prechecks" / h["precheck"]), "task": h["precheck"][1:].split("-")[0]}
    else:
        return None
    if not row or not row["dir"] or str(row["task"]) != str(h["task"]):
        return None
    pid = _read_checks_pid(Path(row["dir"])).get("pid")
    if not isinstance(pid, int) or (pid != h["pid"] and not (_lane_step(h["pid"]) and h["pid"] in (jobuse._tree(pid) or []))):
        return None
    return jobstop.end_checks(p, Path(row["dir"]),
                              f"task #{h['task']} is {h['status']} and its check held a checks lane "
                              f"{mins(h['ended_s'])} after (grace {mins(h['grace_s'])})", int(h["task"]))


def _lane_step(pid: int) -> bool:
    """Whether process `pid` is a wraplane step (lane_argv), read from its command line."""
    try:
        return b"ttp.wraplane" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def mins(s: float) -> str:
    return testslots._mins(s)


def children(rec: dict) -> int | None:
    """How many live child processes (its whole tree) a lane holder's pid has, or None when this process
    cannot judge it (no pid, another host or PID namespace, no /proc) or the pid is gone."""
    from . import jobuse
    if locks.pid_alive(rec) is not True:
        return None
    tree = jobuse._tree(int(rec["pid"]))
    return None if tree is None else len(tree) - 1


def holder_times(rec: dict, now: float) -> dict:
    """A lane holder's clocks, apart: queued_s (from its arrival in the queue: since the lane was taken
    for an older runtime's label), held_s (from taking the lane), running_s (from its first command,
    the pytest start: suite_running; None while not running yet), and children (children())."""
    def at(k, default):
        try:
            return float(rec.get(k) or default) if default is not None or rec.get(k) else None
        except (TypeError, ValueError):
            return default
    since = at("since", now)
    run = at("run_since", None)
    return {"queued_s": round(now - at("queued", since), 1), "held_s": round(now - since, 1),
            "running_s": round(now - run, 1) if run else None, "children": children(rec)}


def _kids(n: int | None, running: bool) -> str:
    if n is None:
        return "child processes unknown"
    if n == 0:
        return "no child process" + (" (stuck or between commands?)" if running else "")
    return f"{n} child process{'es' if n > 1 else ''} alive"


def holder_text(rec: dict, now: float, cfg: dict | None = None) -> str:
    """"#12 'pytest -q' queued 3 h 0 min, running 14 min, 4 child processes alive" for a waiter's line:
    the holder's queue age and its run time from the pytest start shown apart (holder_times), so a holder
    whose check sat queued for hours does not read as a frozen 'running 0 min'."""
    who = f"#{rec['task']}" if rec.get("task") else str(rec.get("holder") or "a run")
    cmd = str(rec.get("command") or "").strip()
    t = holder_times(rec, now)
    run = (f"running {mins(t['running_s'])}" if t["running_s"] is not None
           else f"has the lane {mins(t['held_s'])}, not running yet"
           + (f" (waiting for {rec['waits']})" if rec.get("waits") else ""))
    over = " (priority-1 run in the urgent extra lane over the lane cap)" if rec.get("urgent") else ""
    return (f"{who}{f' {cmd!r}' if cmd else ''}{over} queued {mins(t['queued_s'])}, {run}, "
            f"{_kids(t['children'], t['running_s'] is not None)}{_stalled(rec, now, cfg)}")


def _stalled(rec: dict, now: float, cfg: dict | None = None) -> str:
    """", STALLED: its log has not grown for ..." for a running holder whose log stopped growing (checkstall)."""
    from . import checkstall
    v = checkstall.view(rec, now, cfg)
    return f", {checkstall.text(v)}" if v and v["stalled"] else ""


def _ticket_task(t: Path) -> str:
    return locks.ticket_task(t)


_WORKTREE = re.compile(r"/worktrees/t(\d+)(?:/|$)")   # a task's worktree: <project folder>/worktrees/t<id>
_RUN_DIR = re.compile(r"/runs/(\d+)/?$")


def _env_task(env: dict | None) -> tuple[str, str]:
    """(task, run) an environment names (TTP_TASK, TTP_RUN_ID or its TTP_RUN_DIR's number), "" each without."""
    env = env or {}
    task = str(env.get("TTP_TASK") or env.get("TTP_TASK_ID") or "")
    run = str(env.get("TTP_RUN_ID") or "")
    if not run and (m := _RUN_DIR.search(str(env.get("TTP_RUN_DIR") or ""))):
        run = m.group(1)
    return (task if task.isdigit() else ""), (run if run.isdigit() else "")


def _run_dir_task(run_dir: str) -> str:
    """The task of the worker run in folder `run_dir` (the TTP_TASK its run.json gives the worker), else ""."""
    try:
        env = json.loads((Path(run_dir) / "run.json").read_text()).get("env")
    except (OSError, ValueError, AttributeError, TypeError):
        return ""
    return _env_task(env if isinstance(env, dict) else {})[0]


def _proc(pid: int | None) -> dict:
    """A live process's command line, working folder, parent pid, session id and environment, each "" or
    None when unreadable (gone, another user's, no /proc)."""
    from . import procowner
    out = {"command": "", "cwd": "", "ppid": None, "sid": None, "env": None}
    if not pid:
        return out
    try:
        out["command"] = " ".join(Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")).strip()
    except OSError:
        pass
    try:
        out["cwd"] = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        out["ppid"], out["sid"] = int(f[1]), int(f[3])
    except (OSError, ValueError, IndexError):
        pass
    out["env"] = procowner.environ(pid)
    return out


def waiter(t: Path) -> dict:
    """Who a checks-lane waiter's ticket `t` is: task, run, pid, command, cwd (its working folder). The
    ticket's text is read once (a waiter that leaves and rejoins the queue between two reads must not lose
    its task); when it names no task, the task comes from the ticket's owner record (the run it waits for),
    the waiter's environment or a parent's in its session (a worker run's TTP_TASK, TTP_RUN_DIR), else its worktree
    (worktrees/t<id>). Only a waiter none of these link to a task is taskless: its pid, command and
    folder then name it (waiter_text)."""
    text = testslots._ticket_text(t)
    m = re.search(r"task #(\d+)", text)
    task, run, pid = (m.group(1) if m else ""), _run_of(text), testslots.ticket_pid(t)
    if task:   # the usual case: no /proc read
        return {"task": task, "run": run, "pid": pid, "command": "", "cwd": "", "label": text}
    me = _proc(pid)
    try:
        rec = json.loads(locks._owner_file(t).read_text())
        rec = rec if isinstance(rec, dict) else {}
    except (OSError, ValueError, AttributeError):
        rec = {}
    owner = str(rec.get("run_dir") or "")
    task = str(rec.get("task_id") or "")   # the ticket's record (locks.waiter_ids), else its run folder's
    task = task if task.isdigit() else (_run_dir_task(owner) if owner else "")
    if not run and str(rec.get("run_id") or "").isdigit():
        run = str(rec["run_id"])
    if not run and (r := _RUN_DIR.search(owner)):
        run = r.group(1)
    up, seen = me, 0
    while not task and up["env"] is not None and seen < 8:   # its own environment, then its parents'
        task, r = _env_task(up["env"])
        run = run or r
        task = task or _run_dir_task(str(up["env"].get("TTP_RUN_DIR") or ""))
        seen += 1
        if task or not up["ppid"] or up["ppid"] <= 1:
            break
        up = _proc(up["ppid"])
        if up["sid"] != me["sid"]:   # a detached waiter's starter is not its task
            break
    if not task and (w := _WORKTREE.search(me["cwd"])):
        task = w.group(1)
    return {"task": task, "run": run, "pid": pid, "command": me["command"], "cwd": me["cwd"], "label": text}


def waiter_text(w: dict, since: float, cfg: dict | None = None, now: float | None = None) -> str:
    """How a wait line names its waiter: "#12", a run no record links to a task "run 7 (no task)"
    (_waiter_name), else "a run with no task (pid 4242 'ttp checks ...' in /path, first queued 12:40 PST)"
    so it can be acted on without a hunt; a gone waiter's command is its ticket label's."""
    if w.get("task") or w.get("run"):
        return _waiter_name(str(w.get("task") or ""), f"run {w['run']}" if w.get("run") else "")
    from . import timefmt
    cmd = str(w.get("command") or "") or " ".join(str(w.get("label") or "").split(": ", 1)[-1].split())
    cmd = cmd if len(cmd) <= 80 else cmd[:77] + "..."
    bits = [f"pid {w['pid']}" if w.get("pid") else "pid unknown", repr(cmd) if cmd else "command unknown",
            f"in {w['cwd']}" if w.get("cwd") else "folder unknown",
            f"first queued {timefmt.short(since, cfg or {}, now)}"]
    return f"a run with no task ({' '.join(bits[:3])}, {bits[3]})"


def waits(project_name: str, cfg: dict | None, now: float | None = None, over_s: float = 0.0) -> list[dict]:
    """The project's waiters for a lane that have waited over `over_s`, in queue order (the aging step
    of `cfg`, checks.priority_age_s — the order the lanes grant): task, waited_s, since (arrival),
    position (1-based), the waiter's run (`run`), who it is (`waiter`: waiter) and its name (`who`: waiter_text), the
    lane's label (lane_labels), what it waits on (the lane, the project cap or a host gate:
    _lane_waits_on), the limit holding it ("lanes", "project_cap", "host" or "" for none), the holders'
    records (of the lane, else of every lane) and how many lanes the repository has now (slots_note)."""
    now = time.time() if now is None else now
    age = priority_age_s(cfg)
    pre = f"{testslots.SUITE}.{locks.group_id(project_name)}"
    out = []
    try:
        bases = testslots.suite_bases(project_name)
        labels = lane_labels(project_name, [b[len(pre) + 1:] for b in bases], cfg)
        for base in bases:
            key = base[len(pre) + 1:]
            q = testslots.suite_order(project_name, now=now, repo=key, age=age)
            on = _lane_waits_on(project_name, base, key, q, cfg, labels[key]) if q else []
            lane_recs = [r for r in locks.holder_records(testslots._lanes_of(base)) if not _awaits_grants(r)] if q else []
            all_recs = locks.holder_records(testslots.project_held(project_name)) if q else []
            note = slots_note(cfg, len([r for r in lane_recs if not r.get("overflow")])) if q else ""
            for i, t in enumerate(q):
                waited = now - locks._arrival(t)
                if waited > over_s:
                    who, since = waiter(t), locks._arrival(t)
                    out.append({"task": who["task"], "run": who["run"], "who": waiter_text(who, since, cfg, now),
                                "waiter": who, "waited_s": waited, "since": since,
                                "position": i + 1, "of": len(q), "lane": labels[key], "waits_on": on[i],
                                "holders": lane_recs if on[i] == labels[key] else all_recs,
                                "limit": "lanes" if on[i] == labels[key] else "project_cap" if on[i] == CAP_LABEL
                                else "host" if on[i].startswith(HOST_GATE) else "", "slots_note": note})
    except OSError:
        return []
    return out


def long_wait_lines(project_name: str, cfg: dict | None, now: float | None = None,
                    over_s: float = LONG_WAIT_S) -> list[str]:
    """One line per task waiting for a checks lane longer than `over_s` (ttp status, the digest)."""
    now = time.time() if now is None else now
    out = []
    for w in waits(project_name, cfg, now, over_s):
        held = "; ".join(holder_text(h, now, cfg) for h in w["holders"][:3]) or "lanes free, about to start"
        out.append(f"checks lane wait: {w['who']} has waited {mins(w['waited_s'])} "
                   f"on {w['waits_on']} (position {w['position']} of {w['of']} in {w['lane']}), behind {held}")
    return out


def wait_events(project_name: str, cfg: dict | None, now: float | None = None) -> list[dict]:
    """The `checks_slot_wait` events due now: one per wait past checks.wait_event_s, keyed by its
    fingerprint (task and arrival: one per wait episode, a hand-over to detached checks keeps it)."""
    limit = wait_event_s(cfg)
    if limit <= 0:
        return []
    now = time.time() if now is None else now
    out = []
    for w in waits(project_name, cfg, now, limit):
        held = "; ".join(holder_text(h, now, cfg) for h in w["holders"][:3]) or "no holder (lanes free, about to start)"
        who = w["who"]
        out.append({"fingerprint": f"checks_slot_wait:{w['task'] or 'run'}:{w['since']:.0f}",
                    "task": int(w["task"]) if w["task"] else None,
                    "waited_s": w["waited_s"], "holders": w["holders"],
                    "text": (f"Checks lane wait: {who} has waited {mins(w['waited_s'])} on {w['waits_on']} "
                             f"(position {w['position']} of {w['of']} in {w['lane']}), behind {held}. Lanes: "
                             f"checks.slots per repository, one per checks.wrap_cmd lock, checks.test_project_max "
                             f"per project; a waiter cheaper than the holders gets a lane after checks.max_wait_s; "
                             f"light checks (ttp checks --light, checks.light_cmds) never wait. This wait was held "
                             f"by " + (f"the per-repository lane limit (checks.slots): {w['slots_note']}."
                                       if w["limit"] == "lanes" else "the project cap (checks.test_project_max)."
                                       if w["limit"] == "project_cap" else f"{w['waits_on']}."))})
    return out


def _waiter_name(task: str, label: str, ticket: str = "") -> str:
    """A lane waiter for a person: its task (#N), else its run (run N, from its label: "run <id>"), else
    its label's command and pid (testslots.suite_hold's "<project>: full checks (pid N)"), else its
    ticket's name, so a wait with no task still says whose it is."""
    if task:
        return f"#{task}"
    if run := _run_of(label):
        return f"run {run} (no task)"
    what = " ".join(str(label or "").split(": ", 1)[-1].split())[:120]
    return f"a run with no task ({what or 'ticket ' + ticket})"


def _holder_key(rec: dict) -> str:
    return f"{rec.get('task') or rec.get('holder') or 'run'}:{rec.get('pid')}:{rec.get('run_since') or rec.get('since')}"


def _hung(h: dict, now: float, cfg: dict | None, marks: dict | None, stat=None) -> bool | None:
    """Whether lane holder `h` looks hung by checkstall's view: its log has not grown for its stall
    threshold and, where the daemon's CPU reading of its tree (`marks`, kv checkstall.KV) is known, the
    tree has used no CPU for HANG_CPU_IDLE_S. A stall watch that is off (checks.stall_s 0) never says
    hung: elapsed time alone says slow, not hung. None for a holder that names no log: nothing shows it
    either hung or working."""
    from . import checkstall
    v = checkstall.view(h, now, cfg, (marks or {}).get(checkstall.key(h)), **({"stat": stat} if stat else {}))
    if v is None:
        return None
    return bool(v["stalled"] and (v["cpu_idle_s"] is None or v["cpu_idle_s"] >= HANG_CPU_IDLE_S))


def wait_wake(events: list[dict], incident: dict | None, cfg: dict | None, now: float,
              median_s: float | None = None, ended=lambda holder: False, marks: dict | None = None,
              stat=None) -> tuple[list[str], dict | None]:
    """Whether this tick's `checks_slot_wait` events (wait_events) wake the coordinator: (the reasons, none
    when they do not, and the queue incident to keep). An incident opens with the first waiter past
    checks.wait_event_s (reason "new") and closes once no waiter is past it (None). Within it, a wake again
    only for: "escalate" (its longest wait past checks.wait_escalate_s, then once per as long again), "hang"
    (a holder running checks.wait_hang_factor times its scope_median_s (`median_s` only without a cost label), that is also
    stalled (_hung: its log still, its CPU idle where `marks` knows it; `stat` is for tests), once per
    holder; such a holder whose log still grows is only kept in the incident's "slow", for wake_text) and "ownerless" (a holder whose task has ended (`ended(holder record)`; a review precheck
    only once no review reading it is open, open_precheck) or whose hold is stale, once per holder). Every other waiter shows in the digest and ttp status only."""
    if not events:
        return [], None
    inc = json.loads(json.dumps(incident)) if isinstance(incident, dict) else {}   # never the caller's lists
    why = [] if inc else ["new"]
    inc.setdefault("start", now)
    inc["waiters"] = len(events)
    head = max(float(e.get("waited_s") or 0) for e in events)
    esc = wait_escalate_s(cfg)
    if esc > 0 and head >= esc and now - float(inc.get("escalated_at") or 0) >= esc:
        inc["escalated_at"] = now
        if "new" not in why:
            why.append("escalate")
    holders = {_holder_key(h): h for e in events for h in e.get("holders") or []}
    factor = wait_hang_factor(cfg)
    # A holder still waiting for a later gate (testslots.lane_waits) is not hung: its clock starts once
    # it has its test slots (granted_at), or with its commands (run_since).
    # Each holder is timed against runs of its own scope (scope_median_s), never a mix of full and targeted.
    def long(h: dict) -> bool:
        return bool(factor > 0 and (base := scope_median_s(h, median_s)) and not h.get("waits")
                    and now - float(h.get("run_since") or h.get("granted_at") or h.get("since") or now)
                    > factor * base)

    inc["slow"] = [k for k, h in holders.items() if long(h) and _hung(h, now, cfg, marks, stat) is False][-20:]
    for name, test in (("hang", lambda h: long(h) and bool(_hung(h, now, cfg, marks, stat))),
                       ("ownerless", lambda h: h.get("stale") or (h.get("task") and ended(h)))):
        seen = list(inc.get(name) or [])
        for k, h in holders.items():
            if k not in seen and test(h):
                seen.append(k)
                if name not in why:
                    why.append(name)
        inc[name] = seen[-20:]
    return why, inc


def scope_median_s(h: dict, median_s: float | None = None) -> float | None:
    """The usual run time a lane holder `h` is timed against: the median of the passing runs of its own
    command list (its label's cost est_s, median_s()). A label with a cost but no est_s (its command list
    has no passing run yet) has none, so its wall time alone flags nothing; only a label without a cost
    (an older runtime) falls back to `median_s`, the project's recent median."""
    c = h.get("cost")
    if isinstance(c, dict) and c:
        est = c.get("est_s")
        return float(est) if isinstance(est, (int, float)) and est > 0 else None
    return float(median_s) if median_s and median_s > 0 else None


def wake_text(why: list[str], events: list[dict], inc: dict, cfg: dict | None, now: float,
              median_s: float | None = None, load=None) -> str:
    """The waking `checks_slot_wait` event's text: the reasons, a slow holder's count with the host's load
    (`load`, default _load_text), then the longest waiter's own event text."""
    head = max(events, key=lambda e: float(e.get("waited_s") or 0))
    holders = {_holder_key(h): h for e in events for h in e.get("holders") or []}
    flagged = [holders[k] for w in ("hang", "slow") if w in why or w == "slow"
               for k in (inc.get(w) or [])[-1:] if k in holders]
    base = scope_median_s(flagged[0], median_s) if flagged else median_s
    over = f"over {wait_hang_factor(cfg):g}x the median run of its own command list ({mins(base or 0)})"
    say = {"new": f"a checks queue incident started ({len(events)} waiting past {mins(wait_event_s(cfg))})",
           "escalate": f"its longest wait passed {mins(wait_escalate_s(cfg))}",
           "hang": f"a holder has run {over} and its log stopped growing, its CPU idle: a likely hang",
           "ownerless": "a holder's task has ended: nobody owns its fix"}
    slow = len(inc.get("slow") or [])
    return (f"Checks queue: {'; '.join(say[w] for w in why)}. "
            + (f"{slow} holder{'s' if slow > 1 else ''} ran {over} but {'are' if slow > 1 else 'is'} still "
               f"progressing (log growing): slow under load ({(load or _load_text)()}), not hung. " if slow else "")
            + f"{head['text']} Further waiters of this "
            f"incident (open {mins(now - float(inc['start']))}) show in the digest's `checks slot wait:` "
            f"lines and ttp status, not as new wakes.")


def _load_text() -> str:
    """The host's CPU demand on its cores for a person ("CPU demand 63.2 cores on 64 cores")."""
    ld, c = checkcap._load(), checkcap.cores()
    return (f"CPU demand {ld:.1f} cores on {c} cores" if ld == ld and ld != float("inf")
            else f"load unknown, {c} cores")


def log_reclaim(state: Path | None, rec: dict, why: str) -> None:
    """Keep a reclaimed lane's holder and why in the project's state (RECLAIM_LOG, last RECLAIM_KEEP)."""
    if state is None:
        return
    path = Path(state) / RECLAIM_LOG
    try:
        if path.exists() and path.stat().st_size > 64 * 1024:
            project.durable_write(path, "".join(f"{x}\n" for x in path.read_text().splitlines()[-RECLAIM_KEEP:]))
        project.durable_append(path, json.dumps({"ts": time.time(), "why": why, "holder": rec}) + "\n")
    except OSError:
        pass


def _live_tickets(base: str) -> list[Path]:
    """Lane resource `base`'s live waiter tickets, read only: a dead one (or one whose waiter's run ended:
    locks._waiter_gone) is skipped, never removed."""
    d = locks.queue_dir(checkcap.slot_dir(), base)
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return []
    return [d / n for n in names if locks._ticket_live(d / n) and not locks._waiter_gone(d / n)]


def _precheck_of(text) -> str:
    """The run folder name (t<task>-<head>) of a daemon review precheck's lane label, else ''."""
    m = re.search(r"\brun (?:precheck-)?(t\d+-[0-9a-f]{7,40})\b", str(text or ""))
    return m.group(1) if m else ""


def run_owners(project_name: str, run) -> list[dict]:
    """The owner fields (checkcap.OWNER_KEYS) of project `project_name`'s lane holders whose run is `run`, as
    lane_status names it: the run folder's name, also for checks the daemon started again under another
    run_id (relaunch-t<task>). `ttp checks --preempt` finds that run's host-wide slot by them."""
    out = []
    for base in testslots.suite_bases(project_name):
        for r in locks.holder_records(testslots._lanes_of(base)):
            if str(run) and _run_of(r.get("holder")) == str(run) and r.get("pid"):
                out.append({k: r.get(k) for k in checkcap.OWNER_KEYS})
    return out


def _run_of(text: str) -> str:
    m = re.search(r"\brun (\d+)", str(text or ""))
    return m.group(1) if m else ""


def lane_status(project_name: str, now: float | None = None, cfg: dict | None = None,
                names: dict | None = None, db=None) -> list[dict]:
    """`ttp checks status`: per lane (a repository's or a wrapper lock's) with a holder or a waiter, its
    id (the key), label (lane_labels), name, holders (task, run, pid, priority, age, alive, command, and
    for one not running yet what it waits on) and its waiters in the order the lanes go to them
    (testslots.suite_order, the aging step of `cfg`), the first one next, each with its task priority
    (None without one), its rank now (testslots.suite_priority) and what it waits on (_lane_waits_on).
    The name is the one its runs recorded (remember_name, else a holder's repo_name), else
    `names[repo key]` (the caller's: its own repository, and the project folder's name for the unkeyed
    group, key ""), else the lane key: never empty. Each holder says why it holds the lane (`why`: its
    label, e.g. "full checks") and, given the project's `db`, its task's status (`task_status`) and
    whether that task has ended (`finished`). Read only: it probes the lock files without holding them
    and never queues, reclaims a lane, removes a dead ticket or writes a file."""
    now = time.time() if now is None else now
    age = priority_age_s(cfg)
    pre = f"{testslots.SUITE}.{locks.group_id(project_name)}"
    bases = testslots.suite_bases(project_name)
    recs = {b: locks.holder_records(testslots._lanes_of(b)) for b in bases}
    known = {b[len(pre) + 1:]: (next((str(r["repo_name"]) for r in recs[b] if r.get("repo_name")), "")
                                or (names or {}).get(b[len(pre) + 1:]) or "") for b in bases}
    labels = lane_labels(project_name, list(known), cfg, known)
    out = []
    for base in bases:
        key = base[len(pre) + 1:]
        lanes = []
        for r in recs[base]:
            t = holder_times(r, now)
            running = t["running_s"]
            lanes.append({"task": _holder_task(r), "run": _run_of(r.get("holder")), "pid": r.get("pid"),
                          "priority": r.get("priority"), "age_s": t["held_s"], "queued_s": t["queued_s"],
                          "alive": r.get("alive"), "children": t["children"],
                          "running_s": running, "cap_s": float(r.get("cap_s") or 0),
                          "command": str(r.get("command") or ""), "overflow": bool(r.get("overflow")), "urgent": bool(r.get("urgent")),
                          "cost": r.get("cost") or {}, "why": _why(r.get("holder")), **_task_state(db, r),
                          "stalled": _stalled(r, now, cfg),
                          **({} if running is not None else {"waits_on": _pid_queued(r.get("pid"), project_name) or _parked(r)
                                                                or "no host queue (starting, or an older runtime)"})})
            # Parked outside the host queue for its test slots (_parked): a waiter that keeps its lane.
            lanes[-1]["parked"] = running is None and lanes[-1]["waits_on"].startswith(testslots.PARKED_WAITS)
        q = testslots.suite_order(project_name, _live_tickets(base), now, age=age)
        on = _lane_waits_on(project_name, base, key, q, cfg, labels[key])
        waiters = []
        for i, t in enumerate(q):
            m = re.match(r"\d+(?:\.\d+)?-(\d+)", t.name)
            prio = locks.ticket_priority(t)
            who = waiter(t)
            waiters.append({"position": i + 1, "task": who["task"], "run": who["run"],
                            "pid": int(m.group(1)) if m else None,
                            "priority": None if prio == float("inf") else prio,
                            "rank": testslots.suite_priority(t, now, age), "label": testslots.rank_text(t, now, age),
                            "waited_s": round(now - locks._arrival(t), 1), "next": i == 0, "waits_on": on[i]})
        if lanes or waiters:
            out.append({"id": key, "label": labels[key], "repo": key, "group": labels[key],
                        "name": _lane_name(project_name, key, known) or key or base,
                        "kind": "wrapper lock" if key.startswith("w") else "repository" if key else "project",
                        "lanes": lanes, "waiters": waiters})
    return out


def _holder_task(rec: dict) -> str:
    """The task a lane holder runs for: its "task" field, else its run record's task_id (locks.run_ids)."""
    task = str(rec.get("task") or rec.get("task_id") or "")
    return task if task.isdigit() else ""


def _why(text) -> str:
    """What a holder label says the lane is for: "task #N proj: full checks (run R, pid P)" -> "full checks"."""
    t = re.sub(r"\s*\((?:run|pid) [^)]*\)\s*$", "", str(text or "")).strip()
    return t.split(": ", 1)[1] if ": " in t else t


def _task_state(db, rec: dict) -> dict:
    task = str(rec.get("task") or "")
    if db is None or not task.isdigit():
        return {}
    row = db.one("SELECT status FROM tasks WHERE id=?", (int(task),))
    if not row:
        return {}
    return {"task_status": row["status"],
            "finished": row["status"] in FINISHED and not open_precheck(db, task, rec.get("holder"))}


def _who(x: dict) -> str:
    return " ".join(w for w in ((f"#{x['task']}" if x.get("task") else "no task"),
                                (f"run {x['run']}" if x.get("run") else ""),
                                (f"pid {x['pid']}" if x.get("pid") else "")) if w)


def _rule(r: dict, cfg: dict | None) -> str:
    if r["kind"] == "wrapper lock":
        return "" if "one at a time" in r["label"] else "one at a time, checks.wrap_cmd"
    return slots_note(cfg, len([h for h in r["lanes"] if not h["overflow"]]))


def lane_lines(project_name: str, cfg: dict | None = None, now: float | None = None) -> list[str]:
    """`ttp status` and the web app's feed: one line per checks lane with a holder or a waiter (lane_status):
    its label, who runs and for how long against the wall-time cap, who holds a lane but is not running
    yet and what it waits on, and who is held in turn order, each with its priority and rank now
    (testslots.rank_text) and the lane or cap it waits on."""
    now = time.time() if now is None else now
    out = []
    try:
        got = lane_status(project_name, now, cfg)
    except OSError:
        return []
    for r in got:
        parts = []
        for h in r["lanes"]:
            who = (f"#{h['task']}" if h["task"] else "a run") + (f" p{h['priority']}" if h["priority"] is not None else "")
            kids = _kids(h["children"], h["running_s"] is not None)
            if h["running_s"] is not None:
                parts.append(f"{who} queued {mins(h['queued_s'])}, running {mins(h['running_s'])}"
                             + (f" of its {mins(h['cap_s'])} cap" if h["cap_s"] else " (no cap)") + f", {kids}{h['stalled']}")
            else:
                parts.append(f"{who} queued {mins(h['queued_s'])}, has a lane {mins(h['age_s'])}, not running yet: "
                             f"waits on {h['waits_on']}, {kids}")
        held = []
        for w in r["waiters"]:
            who = f"#{w['task']}" if w["task"] else "a run"
            held.append(f"{who} {w['label']} {mins(w['waited_s'])}, waits on {w['waits_on']}")
        rule = _rule(r, cfg)
        line = f"checks lane {r['label']}" + (f" ({rule})" if rule else "") + ": " + ("; ".join(parts) or "no holder")
        if held:
            line += f"; held: {', '.join(held[:6])}" + (f" +{len(held) - 6}" if len(held) > 6 else "")
        out.append(line)
    return out


def status_text(project_name: str, cfg: dict | None, here_repo: str = "", now: float | None = None,
                names: dict | None = None, db=None) -> str:
    """lane_status as lines for a person (`ttp checks status`). A lane holder parked outside the host
    queue for its test slots (lane_status's `parked`) is listed with the waiters, never as a holder."""
    now = time.time() if now is None else now
    n = _explicit_slots(cfg)
    lines = [f"checks lanes of {project_name}: "
             + (f"{n} per repository (checks.slots)" if n is not None else "per repository as many as the host "
                "fits now (checks.slots unset; each repository's line says how many)")
             + ", one per wrapper lock "
             f"(checks.wrap_cmd), at most {project_cap(cfg) or 'any number'} at once (checks.test_project_max"
             f"{cap_note(cfg)}); "
             f"light checks never wait"]
    got = lane_status(project_name, now, cfg, names, db)
    if not got:
        lines.append("  no lane held and nobody waiting")
    for r in got:
        # The one naming rule: the row's group (its label, lane_labels; group_name when no run named it).
        where = r["group"] + (" (checks.wrap_cmd, one at a time)"
                              if r["kind"] == "wrapper lock" and "one at a time" not in r["group"] else "")
        lines.append(f"  {where}" + (" (this one)" if r["id"] == here_repo else "")
                     + (f": {slots_note(cfg, len([h for h in r['lanes'] if not h['overflow']]))}"
                        if r["kind"] != "wrapper lock" and _explicit_slots(cfg) is None else ""))
        parked = [h for h in r["lanes"] if h.get("parked")]
        for h in r["lanes"]:
            if h.get("parked"):
                continue
            alive = {True: "alive", False: "DEAD (its lane is reclaimed by the next waiter)"}.get(h["alive"], "alive unknown")
            run = (f", running {h['command']!r} for {mins(h['running_s'])}" if h["running_s"] is not None
                   else f", not running yet: waits on {h['waits_on']}")
            why = f" for {h['why']}" if h.get("why") else ""
            ended = (f", its task is {h['task_status'].upper()} (the lane is reported, and its own detached checks "
                     f"stopped after checks.done_holder_grace_s)") if h.get("finished") else ""
            extra = " (urgent extra lane, priority 1)" if h.get("urgent") else " (extra lane)" if h["overflow"] else ""
            lines.append(f"    holder{extra}: {_who(h)}{why}, {alive}, "
                         f"queued {mins(h['queued_s'])}, held {mins(h['age_s'])}{run}, "
                         f"{_kids(h['children'], h['running_s'] is not None)}{h['stalled']}{ended}")
        if len(parked) == len(r["lanes"]):
            lines.append("    holder: none (the parked waiters below keep their lanes while they wait)" if parked
                         else "    holder: none (a waiter is about to start)")
        for h in parked:
            lines.append(f"    waiter (parked): {_who(h)}{f' for {w}' if (w := h.get('why')) else ''}, "
                         f"queued {mins(h['queued_s'])}, waits on {h['waits_on']}")
        for w in r["waiters"]:
            lines.append(f"    waiter {w['position']}{' (next)' if w['next'] else ''}: {_who(w)}"
                         + f", {w['label']}"
                         + f", waited {mins(w['waited_s'])}, waits on {w['waits_on']}")
        if not r["waiters"] and not parked:
            lines.append("    waiters: none")
    return "\n".join(lines)

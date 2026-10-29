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
follows the machine (default_slots): the runs its test pool fits side by side, at least 2 (each run is
isolated: its own clean checkout, scratch, output folder and port block, testslots.port_block), at most
MAX_LANES, lowered only by little available memory. What runs cannot isolate keeps a lock of its own:
checks run in the authoring worktree (cwd "authoring") also hold that worktree's one lane
(testslots.tree_key). Runs behind checks.wrap_cmd take its wrapper lock's lane instead (below).

Wrapper locks. A run whose commands go behind the project's checks.wrap_cmd (a lock or queue command,
such as a shared device's lock) takes that wrapper lock's own single lane (wrap_key), not a repository
lane: runs behind one wrapper lock go one at a time, and lock-free runs (lint, other trees' tests,
harness checks) run in their repository's lanes beside it instead of waiting behind it.

Project cap. At most project_cap() of a project's checks run at once over all its repository and
wrapper-lock lanes: checks.test_project_max (project, then account, then TTP_TEST_PROJECT_MAX), else the
runs the host's test pool fits side by side, at least its lanes per repository (slots()). It counts checks, never test workers: the project's test-worker budget is
this cap times one run's grant (testslots.project_max), so every check under the cap gets its slots, and
a run counts against the cap only once its test slots are granted (testslots.project_held), so a run
still waiting for them never keeps another from its turn. A lane under the cap goes to
the most urgent waiter of the project (task priority, 1 first, then age: testslots.cap_turn). A
priority-1 waiter whose lanes are held only by less urgent runs takes an overflow lane beside them while
the host has room (headroom()), never beside a run in its own worktree (one suite per worktree).

Bounded wait (testslots.suite_hold). A lane whose holder process is gone, or whose heartbeat (the lane
file's mtime, touched every HEARTBEAT_S by the holder) is older than HEARTBEAT_DEAD_S, is reclaimed and
the reclaim logged (RECLAIM_LOG). A waiter that has waited past `checks.max_wait_s` (default
MAX_WAIT_S; 0 turns it off) and whose run is cheaper than every current holder's (cost(): the median
duration of this command list's recorded passing runs, else its class: light, other, test suite or
build) takes one of as many overflow lanes alongside them, not after.

Visible. A waiter says who holds the lanes (task, command, running for) and its queue position every
TELL_S; `ttp status` and the coordinator digest list waits over LONG_WAIT_S (long_wait_lines), and the
daemon raises one `checks_slot_wait` event per wait episode past `checks.wait_event_s` (default
WAIT_EVENT_S; 0 turns it off) naming holder and waiter (wait_events).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import statistics
import time
from pathlib import Path

from . import checkcap, locks, project, testslots

# The default light checks: each a command's leading words (an option word must be present at its place).
LIGHT_CMDS = (
    "python -m py_compile", "python -m compileall", "python -m json.tool", "python -m tabnanny",
    "ruff check", "ruff format --check", "black --check", "isort --check-only", "isort --check",
    "flake8", "pyflakes", "pycodestyle", "git diff --check", "shellcheck", "bash -n", "sh -n",
    "yamllint", "jq", "json_pp", "xmllint --noout", "clang-format --dry-run", "gofmt -l",
    "prettier --check", "markdownlint",
)
CORES_PER_LANE = 8
GIB_PER_LANE = 8
MAX_LANES = 4
MAX_WAIT_S = 300.0
WAIT_EVENT_S = 900.0
LONG_WAIT_S = 600.0       # ttp status and the digest list a wait longer than this
TELL_S = 60.0             # a waiter says who holds the lanes this often
HEARTBEAT_S = 30.0        # a holder touches its lane file this often
HEARTBEAT_DEAD_S = 300.0  # a lane file untouched this long has a dead holder
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


def slots(cfg: dict | None) -> int:
    """The project's heavy lanes: checks.slots (project, then account), TTP_CHECKS_SLOTS, else default_slots()."""
    for v in (setting(cfg, testslots.SLOTS_KEY), os.environ.get(SLOTS_ENV)):
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n >= 1:
            return n
    return default_slots(cfg=cfg)


def project_cap(cfg: dict | None) -> int:
    """The most of a project's checks that run at once (above): checks.test_project_max (project, then
    account), TTP_TEST_PROJECT_MAX, else the runs the host's test pool fits side by side, at least its lanes
    per repository (slots()); 0 (unset with the test pool off): no cap."""
    own = ((cfg or {}).get("checks") or {}) if isinstance((cfg or {}).get("checks"), dict) else {}
    for v in (own.get("test_project_max"), setting({}, "test_project_max"), os.environ.get("TTP_TEST_PROJECT_MAX")):
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n >= 1:
            return n
    s = testslots.sizes(cfg)
    return max(slots(cfg), s[0] // s[1]) if s else 0


def group_name(key: str) -> str:
    """What a lane base's key names, for status (the one naming rule: lane_status, status_text): a wrapper
    lock, a repository, or an unkeyed group (an older runtime's one lane per project). Never empty."""
    if key.startswith("w"):
        return f"wrapper lock {key[1:]} (checks.wrap_cmd, one at a time)"
    return f"repository {key}" if key else "not keyed (no repository key: an older runtime, one lane per project)"


def wrap_key(wrap) -> str:
    """The id of the wrapper lock a run's commands go behind (checks.wrap_cmd as argv words); "" for none.
    The one wrapper-lock detector: any wrapper, however it locks, gets its own single lane. A wrapper
    that fails instead of waiting (`flock -n`, `flock -w 60`, a queue that refuses when busy) is never
    taken to serialize by itself: its runs go one at a time in that lane, never side by side in the
    repository's lanes, so two runs of one project never fail each other's lock."""
    return hashlib.sha256(shlex.join(map(str, wrap)).encode()).hexdigest()[:12] if wrap else ""


def headroom() -> bool:
    """Whether the host has room for a priority-1 run beside the lanes' holders: its 1-minute load
    average is under half its cores."""
    return checkcap._load() < checkcap.cores() / 2


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


def all_light(cmds: list, cfg: dict | None, declared=()) -> bool:
    """Whether a run of `cmds` is light: each is light or among `declared` (ttp checks --light)."""
    pats, declared = light_patterns(cfg), {str(c) for c in declared}
    return bool(cmds) and all(str(c) in declared or is_light(str(c), pats) for c in cmds)


def run_class(cmds: list, cfg: dict | None = None) -> int:
    """LIGHT, OTHER or SUITE: the costliest of `cmds` (a test runner or build word anywhere is SUITE)."""
    pats, out = light_patterns(cfg), LIGHT
    for c in cmds:
        if is_light(str(c), pats):
            continue
        try:
            words = [_word(w) for seg in testslots._segments(str(c)) for _, _, w in seg]
        except (ValueError, IndexError):
            words = []
        out = max(out, SUITE if _SUITE_WORDS & set(words) else OTHER)
    return out


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


def cost(state: Path | None, key: str, cmds: list, cfg: dict | None = None) -> dict:
    """What a run costs, as its lane label keeps it: {"cls": run_class, "est_s": median_s or absent}."""
    est = median_s(state, key)
    return {"cls": run_class(cmds, cfg), **({"est_s": round(est, 1)} if est is not None else {})}


def cheaper(mine: dict | None, theirs: dict) -> bool:
    """Whether a run of cost `mine` is cheaper than a holder's (`theirs`, its lane label): by both medians
    when both are known, else by class (a holder without one, from an older runtime, is a SUITE run)."""
    if not mine:
        return False
    a, b = mine.get("est_s"), theirs.get("est_s")
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a < b
    t = theirs.get("cls")
    return int(mine.get("cls", SUITE)) < (int(t) if isinstance(t, int) else SUITE)


def mins(s: float) -> str:
    return testslots._mins(s)


def holder_text(rec: dict, now: float) -> str:
    """'#12 running `pytest -q` for 14 min' for a lane's label."""
    who = f"#{rec['task']}" if rec.get("task") else str(rec.get("holder") or "a run")
    cmd = str(rec.get("command") or "").strip()
    since = float(rec.get("run_since") or rec.get("since") or now)
    return f"{who} running {cmd!r} for {mins(now - since)}" if cmd else f"{who} for {mins(now - since)}"


def _ticket_task(t: Path) -> str:
    try:
        m = re.search(r"task #(\d+)", t.read_text())
    except OSError:
        return ""
    return m.group(1) if m else ""


def waits(project_name: str, cfg: dict | None, now: float | None = None, over_s: float = 0.0) -> list[dict]:
    """The project's waiters for a lane that have waited over `over_s`, in queue order (the aging step
    of `cfg`, checks.priority_age_s — the order the lanes grant): task, waited_s, since (arrival),
    position (1-based) and the lane holders' records."""
    now = time.time() if now is None else now
    age = priority_age_s(cfg)
    pre = f"{testslots.SUITE}.{locks.group_id(project_name)}"
    out = []
    try:
        for base in testslots.suite_bases(project_name):
            q = testslots.suite_order(project_name, now=now, repo=base[len(pre) + 1:], age=age)
            recs = locks.holder_records(testslots._lanes_of(base)) if q else []
            if q and not recs:   # its lanes are free: it waits for the project's cap (project_cap)
                recs = locks.holder_records(testslots.project_held(project_name))
            for i, t in enumerate(q):
                waited = now - locks._arrival(t)
                if waited > over_s:
                    out.append({"task": _ticket_task(t), "waited_s": waited, "since": locks._arrival(t),
                                "position": i + 1, "of": len(q), "holders": recs})
    except OSError:
        return []
    return out


def long_wait_lines(project_name: str, cfg: dict | None, now: float | None = None,
                    over_s: float = LONG_WAIT_S) -> list[str]:
    """One line per task waiting for a checks lane longer than `over_s` (ttp status, the digest)."""
    now = time.time() if now is None else now
    out = []
    for w in waits(project_name, cfg, now, over_s):
        held = "; ".join(holder_text(h, now) for h in w["holders"][:3]) or "lanes free, about to start"
        out.append(f"checks slot wait: {'#' + w['task'] if w['task'] else 'a run'} has waited {mins(w['waited_s'])} "
                   f"(position {w['position']} of {w['of']}), behind {held}")
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
        held = "; ".join(holder_text(h, now) for h in w["holders"][:3]) or "no holder (lanes free, about to start)"
        who = f"#{w['task']}" if w["task"] else "a run with no task"
        out.append({"fingerprint": f"checks_slot_wait:{w['task'] or 'run'}:{w['since']:.0f}",
                    "task": int(w["task"]) if w["task"] else None,
                    "text": (f"Checks slot wait: {who} has waited {mins(w['waited_s'])} for its project's checks "
                             f"slot (position {w['position']} of {w['of']}), behind {held}. Lanes: checks.slots "
                             f"per repository, one per checks.wrap_cmd lock, checks.test_project_max per project; "
                             f"a waiter cheaper than the holders gets a lane after checks.max_wait_s; light "
                             f"checks (ttp checks --light, checks.light_cmds) never wait.")})
    return out


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
    """Lane resource `base`'s live waiter tickets, read only: a dead one is skipped, never removed."""
    d = locks.queue_dir(checkcap.slot_dir(), base)
    try:
        names = sorted(n for n in os.listdir(d) if not n.startswith("."))
    except OSError:
        return []
    return [d / n for n in names if locks._ticket_live(d / n)]


def _run_of(text: str) -> str:
    m = re.search(r"\brun (\d+)", str(text or ""))
    return m.group(1) if m else ""


def lane_status(project_name: str, now: float | None = None, cfg: dict | None = None) -> list[dict]:
    """`ttp checks status`: per repository with a held lane or a waiter, its lanes' holders (task, run,
    pid, age, alive, command) and its waiters in the order the lanes go to them (testslots.suite_order,
    the aging step of `cfg`), the first one next, each with its task priority (None without one) and its
    rank now (testslots.suite_priority). Read only: it probes the lock files without holding them and
    never queues, reclaims a lane, removes a dead ticket or writes a file."""
    now = time.time() if now is None else now
    age = priority_age_s(cfg)
    pre = f"{testslots.SUITE}.{locks.group_id(project_name)}"
    out = []
    for base in testslots.suite_bases(project_name):
        lanes = []
        for r in locks.holder_records(testslots._lanes_of(base)):
            since = float(r.get("since") or now)
            lanes.append({"task": str(r.get("task") or ""), "run": _run_of(r.get("holder")), "pid": r.get("pid"),
                          "age_s": round(now - since, 1), "alive": r.get("alive"),
                          "running_s": round(now - float(r["run_since"]), 1) if r.get("run_since") else None,
                          "command": str(r.get("command") or ""), "overflow": bool(r.get("overflow")),
                          "cost": r.get("cost") or {}})
        waiters = []
        for i, t in enumerate(testslots.suite_order(project_name, _live_tickets(base), now, age=age)):
            m = re.match(r"\d+(?:\.\d+)?-(\d+)", t.name)
            prio = locks.ticket_priority(t)
            try:
                label = t.read_text()
            except OSError:
                label = ""
            waiters.append({"position": i + 1, "task": _ticket_task(t), "run": _run_of(label),
                            "pid": int(m.group(1)) if m else None,
                            "priority": None if prio == float("inf") else prio,
                            "rank": testslots.suite_priority(t, now, age), "label": testslots.rank_text(t, now, age),
                            "waited_s": round(now - locks._arrival(t), 1), "next": i == 0})
        if lanes or waiters:
            key = base[len(pre) + 1:]
            out.append({"repo": key, "group": group_name(key), "lanes": lanes, "waiters": waiters})
    return out


def _who(x: dict) -> str:
    return " ".join(w for w in ((f"#{x['task']}" if x.get("task") else "no task"),
                                (f"run {x['run']}" if x.get("run") else ""),
                                (f"pid {x['pid']}" if x.get("pid") else "")) if w)


def status_text(project_name: str, cfg: dict | None, here_repo: str = "", now: float | None = None) -> str:
    """lane_status as lines for a person (`ttp checks status`)."""
    now = time.time() if now is None else now
    n = slots(cfg)
    lines = [f"checks lanes of {project_name}: {n} per repository (checks.slots), one per wrapper lock "
             f"(checks.wrap_cmd), at most {project_cap(cfg) or 'any number'} at once (checks.test_project_max); "
             f"light checks never wait"]
    got = lane_status(project_name, now, cfg)
    if not got:
        lines.append("  no lane held and nobody waiting")
    for r in got:
        lines.append(f"  {r['group']}" + (" (this one)" if r["repo"] == here_repo else ""))
        for h in r["lanes"]:
            alive = {True: "alive", False: "DEAD (its lane is reclaimed by the next waiter)"}.get(h["alive"], "alive unknown")
            run = f", running {h['command']!r} for {mins(h['running_s'])}" if h["running_s"] is not None else ", not running yet"
            lines.append(f"    holder{' (extra lane)' if h['overflow'] else ''}: {_who(h)}, {alive}, "
                         f"held {mins(h['age_s'])}{run}")
        if not r["lanes"]:
            lines.append("    holder: none (a waiter is about to start)")
        for w in r["waiters"]:
            lines.append(f"    waiter {w['position']}{' (next)' if w['next'] else ''}: {_who(w)}"
                         + f", {w['label']}"
                         + f", waited {mins(w['waited_s'])}")
        if not r["waiters"]:
            lines.append("    waiters: none")
    return "\n".join(lines)

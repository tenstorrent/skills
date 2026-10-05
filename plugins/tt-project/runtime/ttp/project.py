# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project layout, configuration and user-level shared state (registry, secrets)."""
from __future__ import annotations

import copy
import difflib
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .db import DB

FOLDER = "tt-project"                     # lives at the root of the user's project, ignored by it
HOME_DIR = Path(os.environ.get("TTP_HOME", Path.home() / ".tt-project"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "core_provider": "claude",
    "providers": {
        # Tiers name a difficulty class, never a model version: aliases resolve at run time and
        # the project (or the user) pins versions only when it has a reason to.
        "claude": {"tiers": {"light": {"model": "opus", "effort": "low"},
                             "standard": {"model": "opus", "effort": "high"},
                             "deep": {"model": "opus", "effort": "max"}}},
        "codex": {"tiers": {"light": {"model": "", "effort": "low"},
                            "standard": {"model": "", "effort": "high"},
                            "deep": {"model": "", "effort": "xhigh"}}},
        "cursor": {"tiers": {"light": {"model": "auto", "effort": ""},
                             "standard": {"model": "auto", "effort": ""},
                             "deep": {"model": "auto", "effort": ""}}},
    },
    "budget": {
        "reserve_pct": 10,              # plan windows: never take the account past 100 - reserve
        "daily_usd": 100.0,             # applied when the plan reports no window (usage-billed)
        "weekly_usd": 200.0,
        "hourly_alarm_x": 4.0,          # spend rate this many times the 7-day hourly norm = runaway
        "max_parallel_workers": 6,          # on a plan, all of them run until the last stretch before the line
        "task_default_usd": {"light": 2.0, "standard": 8.0, "deep": 25.0},
        "run_timeout_s": {"light": 1200, "standard": 3600, "deep": 7200},
        "stall_s": {"light": 900, "standard": 1800, "deep": 2700},
        # Worker and reviewer context is compacted once it nears this many tokens (0 = the agent's
        # own default). Long runs otherwise re-read a huge context on every call. Claude Code
        # accepts 100k to 1M (smaller values become 100k) and compacts about 33k below the window.
        "compact_window_tokens": {"light": 100000, "standard": 150000, "deep": 200000},
        # A worker whose run has re-read this many context tokens (cache reads, summed over its calls)
        # is told once to finish its step and hand the rest on as a follow-up (0 = never).
        "split_reread_tokens": {"light": 2000000, "standard": 4000000, "deep": 8000000},
        # Claude Code keeps a Bash command's output inline up to this many characters (it accepts
        # 4000-128000; its own default is 30000); longer output goes to a file, and the worker gets a
        # short preview and the path. 0 leaves the agent's default.
        "bash_output_max_chars": 12000,
        "exclusive_wait_s": 600,        # an exclusive run waiting for its resource gives up after this
        # A run that ends without reporting its cost is estimated from its tokens at the project's
        # own observed rate; until there is one, this $ per million weighted tokens (set high).
        "estimate_usd_per_mtok": 15.0,
        "max_reboot_losses": 3,         # a task lost to this many host reboots is blocked: it may cause them
        # A run the host took away (reboot, sleep, lost supervisor) after this much spend or time
        # continues its agent session where the provider can; below both, or with `false`, it starts fresh.
        "resume_lost": {"min_usd": 0.5, "min_s": 600},
        # After the host wakes from a sleep, nothing new starts until it has been awake this long, so
        # a laptop's brief maintenance wakes start no runs that the next sleep would cut.
        "wake_settle_s": 300,
    },
    # A waiting task whose `retry_when` probe still says "not yet" sleeps on, but wakes this long
    # after its hand-off whatever the probe says.
    "waiting": {"max_hold_s": 21600},
    "resources": {},                    # shared-slot limits, e.g. {"device": 1}
    "shared_resources": [],             # resources whose slots and pause all the user's projects share
    # model / effort, when set, override the coordinator tier's for the coordinator only.
    # unblock_effort: the least effort of a tricky or blocking turn (coordinator.effort_triggers);
    # "" keeps those at the coordinator's usual effort. A task failing repeat_fails_24h or waiting
    # repeat_waits_24h times in 24 h makes one (0 turns that rule off).
    "coordinator": {"tier": "light", "model": "", "effort": "", "unblock_effort": "high",
                    "repeat_fails_24h": 2, "repeat_waits_24h": 3,
                    "debounce_s": 15, "max_events_per_turn": 40,
                    "max_turns_per_hour": 30, "max_new_tasks_per_day": 200, "idle_wake_s": 3600,
                    "starve_wake_s": 300,
                    # Routine events (task done, follow-ups, notes, normal observations) wait up to
                    # this long for company while every worker slot is busy or runnable work is queued.
                    "batch_s": 300,
                    # The memory in the coordinator's cached system prompt is a snapshot, rebuilt
                    # after this long without a turn (the cache is cold by then) or once the entries
                    # added or retired since outgrow memory_delta_chars; until then they go in the digest.
                    "memory_refresh_s": 3300, "memory_delta_chars": 3000,
                    "turn_budget_usd": 1.0, "turn_timeout_s": 600,
                    "ask_timeout_h": 1,
                    # A task deferred with `start_when` that has not started after this long is raised
                    # to the coordinator once.
                    "defer_max_days": 14,
                    # A queued task waiting on a task in 'review' longer than this is raised to the
                    # coordinator once per review stint; 0 turns it off.
                    "review_stall_s": 14400},
    "notify": {"slack": False, "slack_min_severity": "high", "chat_min_severity": "normal"},
    # code_tasks_may_push: a code task whose spec asks it to land on delivery.push_branch may run
    # `ttp push` itself, so no separate landing task is needed. Off: only review tasks push.
    "delivery": {"draft_prs": True, "review_before_pr": True, "auto_merge_repos": [],
                 "push_allowed": True, "code_tasks_may_push": False},
    # Review tasks run light when the diff under review touches no risky_paths glob and is doc-only
    # or at most light_max_lines non-doc lines; otherwise standard. Only the coordinator picks deep.
    # With `auto`, the daemon queues the review of each finished code task with a routine hand-off
    # whenever delivery has a review step (review_before_pr or a push_branch); auto_notes ends each
    # such review's spec.
    "review": {"light_max_lines": 60, "risky_paths": [], "auto": True, "auto_notes": ""},
    # Each Jev use (e.g. "screen") is switched off once its net saving over window_days, with at
    # least min_calls calls, is not positive; uses.<use> = "on" or "off" forces it (see jevuse.py).
    "jev": {"enabled": "auto", "window_days": 7, "min_calls": 30, "uses": {}},
    "web": {"bind": "127.0.0.1", "port": 0},
    "power": {"keep_awake": "on_ac"},
    # Disk guard: with free space under the project folder below the smaller of min_free_pct of the
    # disk and min_free_gb (either 0 = off), only question and plan tasks start; a machines-list entry's
    # own min_free_gb replaces min_free_gb on that machine (see machines.py). Once tripped, the guard
    # holds until resume_free_gb are free (the threshold when below it; 1.2 times the threshold when
    # at or above the disk's size); null, or a machine's own threshold, resumes at 1.2 times the
    # threshold. Finished tasks'
    # worktrees lose their cache_dirs (null = the built-in list) and are removed once clean with
    # HEAD on a branch and no submodules set up, at least an hour after the task ended, or
    # worktree_retention_days after it (0 = never tidy). One whose only dirty entries are untracked
    # files totalling at most worktree_leftovers_max_mb (0 = never) has them moved to its last run's
    # worktree-leftovers/ first. Branches are never deleted.
    "disk": {"min_free_pct": 5, "min_free_gb": 150, "resume_free_gb": None, "worktree_retention_days": None,
             "cache_dirs": None, "worktree_leftovers_max_mb": 50},
    # Workers and reviewers run with the project's Python venv active (VIRTUAL_ENV, PATH), so a
    # fresh task worktree does not rebuild one. "auto" finds .venv or venv in the project root; a
    # path (absolute, or relative to the project root) names one; "" turns it off. A working
    # directory with a venv of its own keeps that one. A new task worktree gets a symlink to each
    # link_paths entry (relative to the project root) that exists and is git-ignored there and is
    # missing in the worktree, so checks naming `.venv/bin/python` work; [] turns it off.
    "worktree": {"venv": "auto", "link_paths": [".venv"]},
    # A command watcher's known open issue wakes the coordinator again once it was last seen more
    # than rewake_after_h ago (null = never), or every time its observation says "repeat": true.
    "screen": {"wake_min_severity": "normal", "rewake_after_h": 6},
    # A newer tt-project installed with `ttp setup` is merged into this project's own harness by its
    # daemon (`ttp upgrade`, once per release, never during a push), which then restarts keeping workers.
    "upgrade": {"auto": True},
    # Upstream notes (hand-off follow-ups titled `upstream: ...`) are filed in the user's inbox
    # ~/.tt-project/upstream.jsonl. With ingest on, this project's coordinator gets them as events.
    "upstream": {"ingest": False},
    # Worker and reviewer runs, everything they start (detached checks, tests, reference models) and
    # the pushes and push batches the daemon starts run this many nice levels below the daemon
    # (0-19; 0 = normal priority), so they never slow the host's own work. The daemon and the
    # coordinator's turns keep normal priority.
    "runner": {"nice": 10},
}


# project.json keys the runtime reads beyond DEFAULT_CONFIG: identity written at creation, open
# sections, and settings that have no default. A key in neither is reported as unknown.
META_KEYS = {"name", "id", "host", "root", "created", "tt_project_version", "pricing", "restrictions"}
EXTRA_KEYS = {
    "budget": {"max_waits", "hourly_floor_usd", "estimate_weights", "hourly_waste_usd", "hourly_coordinator_usd",
               "max_pace_hold_s"},   # max_pace_hold_s: deprecated and ignored; accepted so old configs stay quiet
    "coordinator": {"max_review_tasks_per_day", "charter_approval_days"},
    "notify": {"slack_poll_s"},
    # push_queue..after_push_timeout_s: the daemon-owned push queue (see PUSH_QUEUE_DEFAULTS).
    "delivery": {"base_ref", "push_branch", "push_checks", "push_rounds", "push_wait_s", "version_bump",
                 "push_queue", "push_batch_s", "push_batch_max", "after_push", "after_push_timeout_s"},
    "jev": {"via", "url", "model"},
}
OPEN_SECTIONS = {"resources"}           # any name below is fine
PROVIDER_KEYS = {"tiers", "plugin_dirs", "worker_isolation", "mcp_servers"}


def _known_keys(path: list[str]) -> set[str] | None:
    """The keys allowed at `path` in project.json, or None when anything goes there."""
    if not path:
        return set(DEFAULT_CONFIG) | META_KEYS
    if path[0] in OPEN_SECTIONS or path[0] in META_KEYS:
        return None
    if path[0] == "providers":
        return {"claude", "codex", "cursor", "fake"} if len(path) == 1 else PROVIDER_KEYS if len(path) == 2 else None
    if len(path) == 1 and isinstance(DEFAULT_CONFIG.get(path[0]), dict):
        return set(DEFAULT_CONFIG[path[0]]) | EXTRA_KEYS.get(path[0], set())
    return None


def unknown_key_hint(dotted: str) -> str | None:
    """'unknown key ... (did you mean ...?)' when no part of the runtime reads `dotted`, else None."""
    parts = dotted.split(".")
    for i, part in enumerate(parts):
        known = _known_keys(parts[:i])
        if known is None:
            return None
        if part not in known:
            near = difflib.get_close_matches(part, sorted(known), n=1, cutoff=0.6)
            guess = ".".join(parts[:i] + near[:1])
            return f"unknown key {dotted}" + (f" (did you mean {guess}?)" if near else "")
    return None


def disk_resume_gb(disk: dict) -> float | None:
    """The disk guard's resume point in GB from a `disk` config section: resume_free_gb as set, or None
    (unset or not a number) for the default rule. The daemon raises it to the guard's threshold."""
    v = disk.get("resume_free_gb")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return None
    return float(v)


def _disk_problems(disk: Any) -> list[str]:
    if not isinstance(disk, dict) or disk.get("resume_free_gb") is None:
        return []
    v, low = disk["resume_free_gb"], disk.get("min_free_gb", DEFAULT_CONFIG["disk"]["min_free_gb"])
    if disk_resume_gb(disk) is None:
        return [f"disk.resume_free_gb: {v!r} is not a number of GB; the guard resumes at 1.2 times its threshold"]
    if isinstance(low, (int, float)) and v < low:
        return [f"disk.resume_free_gb: {v:g} is below disk.min_free_gb {low:g}; the guard's threshold is used "
                f"instead when it is higher"]
    return []


def code_tasks_may_push(cfg: dict) -> bool:
    """Whether code tasks may land their own work with `ttp push`: delivery.code_tasks_may_push is
    true and delivery.push_branch is set."""
    d = cfg.get("delivery") or {}
    return d.get("code_tasks_may_push") is True and bool(str(d.get("push_branch") or "").strip())


# delivery.push_queue on: reviews approve into the daemon's queue, which pushes approved heads in
# batches (a batch starts once the oldest approval waited push_batch_s or push_batch_max are waiting)
# and then runs after_push (commands like push_checks), killed after after_push_timeout_s.
PUSH_QUEUE_DEFAULTS = {"push_queue": False, "push_batch_s": 900, "push_batch_max": 8, "after_push_timeout_s": 1800}
# key: (minimum, whether the minimum itself is allowed, whole numbers only)
PUSH_QUEUE_NUMBERS = {"push_batch_s": (0, True, False), "push_batch_max": (1, True, True),
                      "after_push_timeout_s": (0, False, False)}


def push_queue_number(key: str, v: Any) -> float | int:
    """delivery.<key> (a PUSH_QUEUE_NUMBERS key) as a number in its range; ValueError otherwise."""
    lo, closed, whole = PUSH_QUEUE_NUMBERS[key]
    try:
        n = float(v) if not isinstance(v, bool) else None
    except (TypeError, ValueError):
        n = None
    if n is None or not abs(n) < float("inf") or (whole and n != int(n)) or n < lo or (n == lo and not closed):
        raise ValueError(f"delivery.{key}: {v!r} is not {'a whole number' if whole else 'a number'} "
                         f"{'>=' if closed else '>'} {lo} (default {PUSH_QUEUE_DEFAULTS[key]})")
    return int(n) if whole else n


def push_allowed(d: dict) -> bool:
    """delivery.push_allowed of the delivery settings `d` as `ttp push` reads it: unset allows."""
    v = d.get("push_allowed")
    return v is None or v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


def push_queue_on(cfg: dict) -> bool:
    """Whether reviews approve into the daemon's push queue: delivery.push_queue is true, pushing is
    allowed and delivery.push_branch is set. The prompts and the daemon (pushq.enabled) both ask this."""
    d = cfg.get("delivery") or {}
    return d.get("push_queue") is True and push_allowed(d) and bool(str(d.get("push_branch") or "").strip())


def nice_level(runner: Any) -> tuple[int, str | None]:
    """runner.nice of a `runner` config section as a level 0-19, and a problem line when the value was
    clamped or replaced by the default."""
    default = DEFAULT_CONFIG["runner"]["nice"]
    v = runner.get("nice", default) if isinstance(runner, dict) else default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v != int(v):
        return default, f"runner.nice: {v!r} is not a whole number 0-19; {default} is used"
    if not 0 <= v <= 19:
        n = min(max(int(v), 0), 19)
        return n, f"runner.nice: {v!r} is outside 0-19; {n} is used"
    return int(v), None


def lower_priority(n: int):
    """A Popen preexec_fn that lowers the child's CPU priority by `n` nice levels before it execs,
    so all it starts inherits it; None when `n` is 0. Never stops the child: a failure leaves it
    at the parent's priority, which the caller sees in its effective level. On Linux the I/O
    priority follows the nice level unless set on its own."""
    if n <= 0:
        return None

    def apply() -> None:
        try:
            os.nice(n)
        except OSError:
            pass
    return apply


def renice(pid: int, n: int) -> None:
    """Lower process `pid`'s CPU priority to `n` nice levels below this process, for a child that
    waits for a go before it starts anything (preexec_fn is not safe in the daemon, which has
    threads). Never raises: a failure leaves it at its priority."""
    if n <= 0:
        return
    try:
        os.setpriority(os.PRIO_PROCESS, pid, min(os.nice(0) + n, 19))
    except OSError:
        pass


def config_problems(raw: dict) -> list[str]:
    """Unknown keys, non-command push_checks or after_push, a malformed version_bump and push queue
    settings out of range in a project's own settings, one line each."""
    out: list[str] = []

    def walk(node: dict, path: list[str]) -> None:
        for k, v in node.items():
            hint = unknown_key_hint(".".join(path + [str(k)]))
            if hint:
                out.append(hint)
            elif isinstance(v, dict) and _known_keys(path + [str(k)]) is not None:
                walk(v, path + [str(k)])
    walk(raw, [])
    from .push import bump_problems, check_problems    # push imports this module
    delivery = raw.get("delivery") or {}
    out += [f"delivery.push_checks: {p}" for p in check_problems(delivery.get("push_checks"))]
    out += bump_problems(delivery.get("version_bump"))
    may_push = delivery.get("code_tasks_may_push", False)
    if not isinstance(may_push, bool):
        out.append(f"delivery.code_tasks_may_push: {may_push!r} is not true or false; code tasks do not push")
    queue = delivery.get("push_queue", False)
    if not isinstance(queue, bool):
        out.append(f"delivery.push_queue: {queue!r} is not true or false; reviews push with ttp push")
    for key in PUSH_QUEUE_NUMBERS:
        if key in delivery:
            try:
                push_queue_number(key, delivery[key])
            except ValueError as e:
                out.append(str(e))
    out += [f"delivery.after_push: {p}" for p in check_problems(delivery.get("after_push"))]
    out += _disk_problems(raw.get("disk"))
    if isinstance(raw.get("runner"), dict) and "nice" in raw["runner"]:
        out += [why for why in [nice_level(raw["runner"])[1]] if why]
    return out

def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def hostname() -> str:
    """This machine's short name, restricted to characters that survive markers, ssh and URLs."""
    raw = os.environ.get("TTP_HOST") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "", raw) or "localhost"


# Memory kinds every prompt carries whatever the budget; decisions and facts fill what is left.
PINNED_MEMORY_KINDS = ("restriction", "preference", "resource")
# Memory each prompt carries, in characters of whole entries (see Project.memory_select).
COORDINATOR_MEMORY_CHARS, WORKER_MEMORY_CHARS = 12000, 8000


class Project:
    """A project rooted at <root>/tt-project. `harness/` is its own git repo (charter, memory,
    config, prompts, runtime); `state/` holds the database, run directories and logs."""

    def __init__(self, base: str | Path):
        base = Path(base).resolve()
        self.base = base if base.name == FOLDER else base / FOLDER
        self.root = self.base.parent
        self.harness = self.base / "harness"
        self.state = self.base / "state"
        self.runs = self.state / "runs"
        self.logs = self.state / "logs"
        self.worktrees = self.base / "worktrees"
        self.memory_dir = self.harness / "memory"
        self._db: DB | None = None
        self._last_good: dict | None = None   # project.json as this process last read it valid
        self.config_status = "ok"             # how raw_config last found it (see read_config)

    # layout -----------------------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.harness / "project.json"

    @property
    def charter_path(self) -> Path:
        return self.harness / "CHARTER.md"

    @property
    def memory_index(self) -> Path:
        return self.harness / "MEMORY.md"

    def exists(self) -> bool:
        return self.config_path.is_file()

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.state / "project.db")
        return self._db

    # config -----------------------------------------------------------------------------------
    def raw_config(self) -> dict:
        """The project's own settings (see read_config); `config_status` keeps how they were found."""
        data, self.config_status = self.read_config()
        return data

    def read_config(self) -> tuple[dict, str]:
        """The project's own settings and how they were found. A hand edit that breaks the JSON, or a
        file that goes missing for a moment, keeps the last good copy (on disk, else the one this
        process last read) in force instead of taking the project down or quietly moving its model
        and provider routing to the defaults: "fallback". With neither, an established project (one
        with a database) is "unavailable" and the daemon starts no model work until the file reads
        again; a project still being made has an empty config, "ok"."""
        good = self.state / "project.last-good.json"
        try:
            data = json.loads(self.config_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("project.json is not a JSON object")
        except (OSError, ValueError):
            for read in (lambda: json.loads(good.read_text()), lambda: self._last_good):
                try:
                    kept = read()
                except (OSError, ValueError):
                    continue
                if isinstance(kept, dict):
                    return copy.deepcopy(kept), "fallback"
            return {}, "unavailable" if self.established() else "ok"
        self._last_good = copy.deepcopy(data)
        try:
            # <=: two writes within one filesystem clock tick share an mtime.
            if self.state.is_dir() and (not good.exists() or good.stat().st_mtime <= self.config_path.stat().st_mtime):
                write_json(good, data)
        except OSError:
            pass
        return data, "ok"

    def established(self) -> bool:
        """The project has run: its database exists (never created by asking)."""
        return (self.state / "project.db").is_file()

    def config(self) -> dict:
        return deep_merge(DEFAULT_CONFIG, self.raw_config())

    def set_config(self, dotted: str, value: Any) -> None:
        raw = self.raw_config()
        if self.config_status == "unavailable":
            # Writing the one key would make a project.json of defaults and that key alone.
            raise RuntimeError(f"{self.config_path} is missing or not valid JSON and there is no last good copy: "
                               f"restore it before changing settings")
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        write_json(self.config_path, raw)

    @property
    def name(self) -> str:
        return self.raw_config().get("name", self.root.name)

    # memory -----------------------------------------------------------------------------------
    def commit_harness(self, paths: list[Path], message: str, removed: list[Path] = ()) -> None:
        """Commit just these harness files, so the project's history shows what it learned and when.
        `removed` are files this change took away (their deletion is committed).

        Only the named paths are committed: a harness task may be editing other files right now,
        and its half-done work must not be swept into this commit. Inside a database transaction
        the commit waits until the transaction ends, so git never holds the write lock.
        """
        top = self.harness.resolve()
        rel = [str(Path(x).resolve().relative_to(top)) for x in paths if Path(x).exists()]
        gone = [str(Path(x).resolve().relative_to(top)) for x in removed if not Path(x).exists()]
        if not (rel or gone) or not (self.harness / ".git").exists():
            return
        self.db.after_commit(lambda: self._git_commit(rel, message, gone))

    def _git_commit(self, rel: list[str], message: str, gone: list[str] = ()) -> bool:
        ident = ["-c", "user.name=tt-project", "-c", "user.email=tt-project@localhost"]
        git = ["git", "-C", str(self.harness), *git_fsync_args()]
        try:
            if gone:
                gone = subprocess.run([*git, "ls-files", "--", *gone], capture_output=True, text=True,
                                      timeout=30).stdout.split()
                if gone:
                    subprocess.run([*git, "rm", "-q", "--cached", "--", *gone], check=True, capture_output=True,
                                   timeout=30)
            if rel:
                subprocess.run([*git, "add", "--", *rel], check=True, capture_output=True, timeout=30)
            if not (rel or gone):
                return False
            r = subprocess.run([*git, *ident, "commit", "-q", "-m", message[:200], "--", *rel, *gone],
                               capture_output=True, timeout=30)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def add_memory(self, text: str, kind: str = "fact", title: str | None = None, key: str | None = None,
                   end: dict | None = None) -> Path:
        """One fact per file plus a one-line pointer in MEMORY.md, so the index stays cheap to load.
        A memory written again under the same `key` (a replayed coordinator turn) keeps its one
        file and line. `end` (see ends.from_action) makes it temporary: the daemon retires it then."""
        from .ends import front_matter
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        title = (title or text.splitlines()[0])[:80]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "note"
        tag = f"turn: {key}\n" if key else ""
        same = [f for f in sorted(self.memory_dir.glob(f"{kind}-{slug}*.md"))
                if f"\n{tag}---\n" in f.read_text()] if key else []
        if same:
            path = same[0]
        else:
            # Never a retired entry's name either: a replayed turn that forgot it and then added
            # this one would retire the new entry over the archived copy.
            path = self.memory_dir / f"{kind}-{slug}.md"
            n = 2
            while path.exists() or (self.memory_dir / "archive" / path.name).exists():
                path = self.memory_dir / f"{kind}-{slug}-{n}.md"
                n += 1
            durable_write(path, f"---\nkind: {kind}\ncreated: {_stamp(time.time())}\n{front_matter(end or {})}{tag}"
                                f"---\n{text}\n")
        index = self.memory_index.read_text() if self.memory_index.exists() else ""
        if f"](memory/{path.name})" not in index:
            durable_append(self.memory_index, f"- [{title}](memory/{path.name}) ({kind})\n")
        self.commit_harness([path, self.memory_index], f"memory ({kind}): {title}")
        return path

    def _memory_entries(self) -> list[dict]:
        """Live memory entries, oldest first: `name` (the file stem shown in prompts), `kind`, `line`,
        and `end` (see ends) for a temporary one, whose line says when it ends.
        Ordered by the front matter's `created`, then name; the file's mtime stands in only where
        `created` is missing, so a checkout or copy that touches the files does not reorder them
        (which would change the coordinator's cached prompt)."""
        if not self.memory_dir.is_dir():
            return []
        from .ends import describe, read_front_matter
        out = []
        for p in self.memory_dir.glob("*.md"):
            raw = p.read_text()
            head, body = (raw.split("---", 2)[1:] if raw.startswith("---") and raw.count("---") >= 2
                          else ("", raw))
            m = re.search(r"^kind:\s*(\S+)", head, re.M)
            kind = m.group(1) if m else p.stem.split("-", 1)[0]
            c = re.search(r"^created:\s*(\S+)", head, re.M)
            end = read_front_matter(head)
            ends = f" ({describe(end)})" if end else ""
            out.append({"name": p.stem, "kind": kind, "line": f"[{p.stem}] {body.strip()}{ends}", "end": end,
                        "order": (c.group(1) if c else _stamp(p.stat().st_mtime), p.stem)})
        out.sort(key=lambda e: e.pop("order"))
        return out

    def memory_select(self, limit_chars: int = COORDINATOR_MEMORY_CHARS) -> tuple[list[dict], dict]:
        """The entries a prompt of `limit_chars` gets, whole: every pinned entry (restrictions,
        preferences, resources) first, then the newest decisions and facts that still fit. Pinned
        entries are kept even past the budget. Also returns the usage numbers for the digest."""
        entries = self._memory_entries()
        pinned = [e for e in entries if e["kind"] in PINNED_MEMORY_KINDS]
        used = sum(len(e["line"]) + 1 for e in pinned)
        rest = []
        for e in reversed([e for e in entries if e["kind"] not in PINNED_MEMORY_KINDS]):
            if used + len(e["line"]) + 1 > limit_chars:
                break   # whole entries only, and no older one slips in past a gap
            rest.append(e)
            used += len(e["line"]) + 1
        chosen = pinned + rest[::-1]
        pinned_chars = sum(len(e["line"]) + 1 for e in pinned)
        return chosen, {"entries": len(entries), "shown": len(chosen), "pinned": len(pinned),
                        "chars": sum(len(e["line"]) + 1 for e in entries), "pinned_chars": pinned_chars,
                        "limit": limit_chars, "pinned_over": pinned_chars > limit_chars}

    def memory_text(self, limit_chars: int = COORDINATOR_MEMORY_CHARS) -> str:
        """Pinned entries, then the newest others (newest last), whole and bounded so they never
        crowd the prompt."""
        return "\n".join(e["line"] for e in self.memory_select(limit_chars)[0])

    def _resolve_memory_name(self, name: str, keep: str | None = None) -> tuple[str, bool]:
        """(entry, archived) for `name`. An exact name wins, live or archived, so a replayed forget
        of an archived entry never reaches a live one. Otherwise `name` may be a prefix of exactly
        one entry, live or archived; one ending on a '-' word boundary ('fact-94' of 'fact-94-a',
        not 'fact-940-b') is preferred. Names are title slugs, so nothing beyond the prefix is
        guessed. `keep` (the entry just added) is never a candidate. Ambiguous or missing: refused."""
        if not name:
            raise ValueError("no memory entry named; use the name in [brackets] from MEMORY")
        archive = self.memory_dir / "archive"
        entries = [(f.stem, d == archive) for d in (self.memory_dir, archive) if d.is_dir()
                   for f in sorted(d.glob("*.md")) if f.stem != keep]
        for e in entries:
            if e[0] == name:
                return e
        for found in ([e for e in entries if e[0].startswith(name + "-")],
                      [e for e in entries if e[0].startswith(name)]):
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ValueError(f"memory entry {name!r} is ambiguous; nothing retired. Use one of: "
                                 + ", ".join(f"[{n}]" + (" (archived)" if old else "") for n, old in found))
        raise ValueError(f"no memory entry {name!r}; use the name in [brackets] from MEMORY")

    def forget_memory(self, name: str, keep: str | None = None) -> Path:
        """Retire an entry: its file moves to memory/archive/ and its line leaves MEMORY.md, so no
        prompt carries it again. Forgetting an entry already archived (a replayed turn) is a no-op.
        `keep` names an entry that must survive (the one a `supersedes` adds)."""
        name, archived = self._resolve_memory_name(Path(name.strip().strip("[]")).stem, keep)
        src, dst = self.memory_dir / f"{name}.md", self.memory_dir / "archive" / f"{name}.md"
        if archived:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        fsync_dir(dst.parent)
        fsync_dir(src.parent)
        if self.memory_index.exists():
            lines = self.memory_index.read_text().splitlines(keepends=True)
            durable_write(self.memory_index, "".join(x for x in lines if f"](memory/{name}.md)" not in x))
        self.commit_harness([dst, self.memory_index], f"memory retired: {name}", removed=[src])
        return dst

def _stamp(ts: float) -> str:
    """A memory entry's `created`: UTC to the microsecond, so entries sort in the order they were
    written. Older entries carry the date alone, which sorts before any stamp of the same day."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts % 1 * 1e6):06d}Z"


def fsync_dir(path: Path) -> None:
    """Make a rename or a new file in this directory survive a power cut. Some file systems
    cannot open or sync a directory; they get what the OS gives."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def durable_write(path: Path, text: str | bytes, mode: int | None = None) -> None:
    """Replace `path` with `text` so a power cut leaves the old content or the new, never an empty
    or half-written file: a temporary file in the same directory is written and synced, renamed over
    `path`, and the directory is synced so the rename lasts too. The file keeps its mode unless
    `mode` is given (a new file gets `mode`, else the umask's default)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode() if isinstance(text, str) else text
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if mode is not None else 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def durable_append(path: Path, text: str) -> None:
    """Append `text` and sync it, so an appended line that was reported written survives a power
    cut. A file this creates gets its directory synced as well."""
    path = Path(path)
    new = not path.exists()
    with open(path, "a") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if new:
        fsync_dir(path.parent)


_git_version: tuple[int, ...] | None = None


def git_version() -> tuple[int, ...]:
    """The installed git's version, (0,) when it cannot be told. Asked once per process."""
    global _git_version
    if _git_version is None:
        try:
            out = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
            _git_version = tuple(int(x) for x in m.groups(default="0")) if m else (0,)
        except (OSError, subprocess.SubprocessError):
            _git_version = (0,)
    return _git_version


def git_fsync_config() -> list[tuple[str, str]]:
    """Git settings that make a commit's objects and refs reach the disk before it returns, so a
    power cut cannot leave a commit pointing at empty objects. Given per command or per process,
    never written into anyone's repository config."""
    if git_version() >= (2, 36):
        return [("core.fsync", "committed")]
    return [("core.fsyncObjectFiles", "true")]


def git_fsync_args() -> list[str]:
    return [a for k, v in git_fsync_config() for a in ("-c", f"{k}={v}")]


def git_fsync_env(base: dict[str, str]) -> dict[str, str]:
    """The fsync settings as GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> on top of
    those already in `base` (an environment), for every git a worker runs. Pairs `base` already
    carries are not repeated; a count git would reject is left alone."""
    try:
        n = int(base.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        return {}
    if n < 0:
        return {}
    have = {(base.get(f"GIT_CONFIG_KEY_{i}"), base.get(f"GIT_CONFIG_VALUE_{i}")) for i in range(n)}
    out: dict[str, str] = {}
    for k, v in git_fsync_config():
        if (k, v) in have:
            continue
        out[f"GIT_CONFIG_KEY_{n}"], out[f"GIT_CONFIG_VALUE_{n}"] = k, v
        n += 1
    if out:
        out["GIT_CONFIG_COUNT"] = str(n)
    return out


def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    durable_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


# user-level registry and secrets ------------------------------------------------------------------
def registry_path() -> Path:
    return HOME_DIR / "registry.json"


def load_registry() -> dict:
    try:
        return json.loads(registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"projects": {}}


def register(name: str, entry: dict) -> None:
    reg = load_registry()
    reg.setdefault("projects", {})[name] = {**reg["projects"].get(name, {}), **entry, "updated": time.time()}
    write_json(registry_path(), reg, mode=0o600)


def unregister(name: str) -> None:
    reg = load_registry()
    if reg.get("projects", {}).pop(name, None) is not None:
        write_json(registry_path(), reg)


def secrets_path() -> Path:
    return HOME_DIR / "secrets.json"


def load_secrets() -> dict:
    """Per-user credentials shared by every project of this user on this machine (mode 0600).
    Never copied into a project folder, never printed."""
    try:
        return json.loads(secrets_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_secret(key: str, value: Any) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_DIR, 0o700)
    data = load_secrets()
    data[key] = value
    durable_write(secrets_path(), json.dumps(data), mode=0o600)

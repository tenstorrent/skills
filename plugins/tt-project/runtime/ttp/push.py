# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Guarded push: publish the current commit onto the project's target branch only after the
project's checks passed on exactly the commit being pushed, and never with force. The push runs
those checks itself every time: it never reads the passes `ttp checks` records (cli.CHECK_PASSES),
so a forged record can at most skip a local re-run, never put a change on the branch. Only
`ttp push --own` (publish), which pushes the task's own branch, reuses such a pass on exactly HEAD's
tree with the same commands and environment (_own_pass).

Pushes of one project to one branch take turns: each holds the lock `push:<remote>/<branch>` from
its first fetch to its push, so two reviewers never race each other through rounds. The lock is an
OS file lock (locks.py): a killed push or a reboot frees it. A waiting push waits as long as the
last measured check run takes, with a margin, up to 2 h (`delivery.push_wait_s` overrides it), counted
from when it is first in line: waiters queue beside the lock, an urgent or user-ordered task's first,
then by age (take).
Each check runs at most four times the last measured check run, at least 4 h
(`delivery.push_checks_timeout_s` overrides it, 0 for no limit), counting its run time, wall time less its waits for a grant
and for a test-pool slot (checkwait, `ttp pool-wait`): one that ran past it is ended and fails the push. One still waiting for a
test-pool slot once its wall time less grant waits reaches the limit is ended and exits 75, busy, not failed: the pool's wait, not
the change; one still waiting for a grant at the limit plus the push wait exits 75, queued, not run. Past 4 h of test-pool waits
in all (`delivery.push_pool_wait_s`, 0 for no bound) the check is ended and exits 75, saying how long it waited for the pool; the
push log gives those seconds, live.

With `delivery.version_bump` set, the push also owns the version bump: after each rebase it sets the
listed files one patch version above the tip's and adds a changeset, in one commit of its own. The
bump happens under the lock, so parallel pushes never race for one version."""
from __future__ import annotations

import json
import hashlib
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from . import (checkcap, checkdirs, checkspill, checkwait, checklane, cleantree, envfault, gatejudge, gatetime, gitcache, gitmemo, hoststate, landed, landmark, leakfile, locks, machines, outscan, pooladmit, prguard, prhead, remotecheck, remoteload, repoid, runner, runnerenv,
               sandbox, scriptwatch, settinghold, setupfault, shorttmp, slurmcheck, syntaxfault, testslots, timefmt, workerloss, worktree)
from .budget import DOC_SUFFIXES
from .db import continues_id, dependency_ids, load_result
from .project import (Project, code_tasks_may_push, durable_append, durable_write, git_fsync_env, hostname,
                      load_registry, nice_level, open_append_or_null, own_creates_pr_branch, own_push, push_queue_on,
                      rebase_replace, renice, write_json)
from .project import never_force as never_force_on
from .release import UPGRADE_RESOURCE, _taken as _lock_taken

# Exit codes, distinct so a worker can say why it did not push. BUSY: another push to the same
# branch kept the lock past `delivery.push_wait_s`; the task hands back `waiting`.
REFUSED, CONFLICT, CHECKS_FAILED, KEPT_MOVING, REJECTED, BUSY = 2, 3, 4, 5, 6, 75
DEFAULT_ROUNDS = 3
DEFAULT_WAIT_S = 900       # the lock wait with no measured check run, and its floor otherwise
WAIT_MARGIN_S = 60          # on top of two check runs: the holder may start over once
MAX_DEFAULT_WAIT_S = 7200  # the measured wait's ceiling; only an explicit push_wait_s waits longer
CHECK_LIMIT_FLOOR_S = 4 * 3600   # a push check's time limit with no measured check run, and its floor otherwise
CHECK_LIMIT_FACTOR = 4           # times the last measured check run
POOL_WAIT_S = 4 * 3600          # how long a push check may wait for test-pool slots unless delivery.push_pool_wait_s says
CHECK_POLL_S = 5.0               # how often a timed check's waits for a grant are read (checkwait)
POOL_SAY_S = 60.0                # how often the push log gives a check's live test-pool wait so far
POOL_STATUS_SUFFIX = ".pool"     # beside a detached push's marker: its check's test-pool wait so far
QUEUED = 1075                    # once()'s answer for a check still queued at its outer bound: never a process's code
POOL_STARVED = 1124              # _timed_wait's answer for a check ended at its pool-wait bound: never a process's code
POOL_BUSY = 1175                 # _timed_wait's answer for a check still waiting for a pool slot at its limit: never a process's code
GATE_LINES = 20000                # a failed check's last output lines the tests gate reads its failing ids from (_gate)
QUEUED_RETRY_S = 1800            # a push whose check stayed queued is tried again after this
TIMINGS = "push_checks.json"   # under the project's state: how long the last full check run took
BUMP_TRAILER = "Ttp-Version-Bump"   # marks the bump commit ttp push made, so a rerun replaces it
VERSION_RE = re.compile(r"""(version(?:__)?["']?\s*[:=]\s*["'])(\d+)\.(\d+)\.(\d+)(["'])""", re.I)
PROTECTED = {"HEAD", "main", "master"}
OWN_BRANCH = re.compile(r"ttp/t(\d+)-\S+")   # a task's own branch, as worktree.ensure names it
NAMED_BRANCH = re.compile(r"(?<![\w/.-])ttp/t(\d+)-[\w./-]*[\w-]")   # such a branch named in prose
NOTES_BRANCH = re.compile(r"ttp/t(\d+)-(?:\S+-)?notes")   # a task's notes branch: ttp/t<id>-notes, ...-notes
NO_CHECKS = ("set delivery.push_checks to the commands that must pass on the exact commit before it "
             "is pushed (a list, or one per line, e.g. the repository's test suite); the coordinator "
             "sets it with config_set")


class Check(str):
    """One check command. A plain string runs on every head. The opt-in form
    `{"run": "<cmd>", "if_exists": "<repo path or glob>"}` runs only on a head that has a file
    matching `if_exists` (a directory counts by its files); elsewhere it is skipped as not
    applicable, which is reported and never counted as passed. `"if_changed": "<glob>"` (or a list
    of globs) scopes a check by what it covers: it is skipped on a head whose diff since the push
    target touches no matching path, and a head where every check is skipped that way changes
    nothing any check covers (`outside_scope`). `"purpose": "candidate"` or `"harness"` says which
    `ttp checks --purpose` runs it (untagged: both; `ttp push --own` is candidate); `ttp push` and the
    push queue run every entry. `"host": "<ssh alias>"` runs it on that machine (remotecheck), with
    `"env": {...}` exported where it runs; `"host": "local"` keeps it here when `delivery.checks_host` sets a
    default host (`default_host`, never part of the config form). A local check runs in a process
    sandbox (sandbox.py); `"sandbox": false` runs it without, for a check that needs the host's
    process table. A local check runs in a clean checkout of the commit (cleantree); `"cwd": "authoring"`
    runs it in the worktree being checked instead, for a check that needs build outputs or a venv made
    there, and `ttp checks` labels its pass as not run from a clean tree. `"silent_s": <seconds>` is how long
    `ttp checks` lets it run with no new output before stopping it as hung (checkwall.SILENT_S by default; 0
    turns it off), for a check that is silent longer by design. `"pytest": true` marks a check as one pytest run
    (pytest_runs) the matcher cannot see, such as one behind a wrapper with no `--` before the pytest
    command, so it resumes and records its test times; `false` turns that detection off; left out, it is
    detected from the command. `"spill": true` lets a check spill to the account's test-pool hosts
    (checkspill) even with `"host": "local"`, `false` keeps it here; `"spill_wrap_cmd": "<command>"` is the
    wrapper it runs behind there (default `checks.spill_wrap_cmd`; spill_wrap_of). `"ignore_paths": "<glob or list>"` names
    paths the check cannot be affected by: it decides only whether a pass carries across a rebase
    (verdictcarry), never what the check covers or whether it runs on a fresh head. `wrap` (with_wrap,
    never part of the config form) is the argv a local check runs behind: `checks.wrap_cmd` for the commands after `--`;
    `spill_host` and `remote_wrap` (spill_to, never part of the config form) are the spill host it runs on
    instead and the wrapper it runs behind there; `keep_here` marks a malformed entry that named a host or
    spill key, which never spills."""
    if_exists = ""
    if_changed: tuple[str, ...] = ()
    ignore_paths: tuple[str, ...] = ()
    purpose = ""
    host = ""
    env: tuple[tuple[str, str], ...] = ()
    default_host = ""
    wrap: tuple[str, ...] = ()
    sandbox = True
    cwd = ""
    silent_s: float | None = None
    pytest: bool | None = None
    spill: bool | None = None
    spill_wrap_cmd = ""
    spill_host = ""
    remote_wrap: tuple[str, ...] = ()
    keep_here = False

    def config(self) -> str | dict:
        """The form project.json keeps."""
        if not (self.if_exists or self.if_changed or self.purpose or self.host or self.env or not self.sandbox
                or self.cwd or self.silent_s is not None or self.ignore_paths or self.pytest is not None
                or self.spill is not None or self.spill_wrap_cmd):
            return str(self)
        out: dict = {"run": str(self)}
        if self.if_exists:
            out["if_exists"] = self.if_exists
        if self.if_changed:
            out["if_changed"] = self.if_changed[0] if len(self.if_changed) == 1 else list(self.if_changed)
        if self.purpose:
            out["purpose"] = self.purpose
        if self.host:
            out["host"] = self.host
        if self.env:
            out["env"] = dict(self.env)
        if not self.sandbox:
            out["sandbox"] = False
        if self.cwd:
            out["cwd"] = self.cwd
        if self.silent_s is not None:
            out["silent_s"] = self.silent_s
        if self.ignore_paths:
            out["ignore_paths"] = list(self.ignore_paths)
        if self.pytest is not None:
            out["pytest"] = self.pytest
        if self.spill is not None:
            out["spill"] = self.spill
        if self.spill_wrap_cmd:
            out["spill_wrap_cmd"] = self.spill_wrap_cmd
        return out


CHECK_KEYS = {"run", "if_exists", "if_changed", "purpose", "host", "env", "sandbox", "cwd", "silent_s", "ignore_paths",
              "pytest", "spill", "spill_wrap_cmd"}
PURPOSES = ("candidate", "harness")   # `ttp checks --purpose`; candidate is the default and publication evidence


def _entries(v: Any) -> list:
    """Check entries from a config value: a list, a JSON-encoded list, or one command per line."""
    if isinstance(v, str):
        text = v.strip()
        try:
            v = json.loads(text) if text.startswith("[") else text.splitlines()
        except ValueError:
            v = text.splitlines()
    if isinstance(v, dict):
        v = [v]
    if v and not isinstance(v, (list, tuple)):   # a scalar (true, 5): no entries; scalar_problem names it
        return []
    return [c for c in (v or []) if isinstance(c, dict) or str(c).strip()]


def _check(c: Any) -> Check | None:
    if not isinstance(c, dict):
        return Check(str(c).strip())
    cmd = str(c.get("run") or "").strip()
    if not cmd:
        return None
    out = Check(cmd)
    # Strict: the check skips only when it validly opted in; anything malformed runs it everywhere.
    if not _form_problem(c):
        out.if_exists = _if_exists(c)
        out.if_changed = _if_changed(c)
        out.purpose = c.get("purpose") or ""
        out.host = str(c.get("host") or "").strip()
        out.env = tuple(sorted((c.get("env") or {}).items()))
        out.sandbox = c.get("sandbox") is not False
        out.cwd = "" if c.get("cwd") in (None, "clean") else c["cwd"]
        out.silent_s = c.get("silent_s")
        out.ignore_paths = _globs(c.get("ignore_paths"))
        out.pytest = c.get("pytest")
        out.spill = c.get("spill")
        out.spill_wrap_cmd = str(c.get("spill_wrap_cmd") or "").strip()
    elif {"host", "spill", "spill_wrap_cmd"} & set(map(str, c)):
        out.keep_here = True   # where or behind what it may run elsewhere is unreadable: it never spills
    return out


def with_host(checks: list, default: Any) -> list:
    """`checks` (Checks or plain commands) with `delivery.checks_host` as the host of those that name
    none; a malformed or empty default changes nothing (config_problems names it)."""
    default = default.strip() if isinstance(default, str) else ""
    if not default or remotecheck.host_problem(default):
        return list(checks)
    out = []
    for c in checks:
        if not isinstance(c, Check):
            c = Check(str(c))
        elif not c.host:
            c2 = Check(str(c))
            c2.__dict__.update(c.__dict__)
            c = c2
        if not c.host:
            c.default_host = default
        out.append(c)
    return out


WRAP_KEY = "checks.wrap_cmd"


def wrap_of(cfg: dict | None, repo: Path | str | None = None, p: Project | None = None) -> tuple[str, ...]:
    """`checks.wrap_cmd` as argv words: a project's own lock or queue command that each command given
    after `--` to `ttp checks` (and the ones `ttp push --own` reruns from that record) runs behind, the
    command passed to it as separate arguments. () when unset or empty. ValueError when it is not a
    string or its quoting does not parse; callers fail the checks then, never run the command unwrapped.
    A map `{"cmd": "<command>", "repos": ["<repo key>", ...]}` scopes it to the repositories it serves,
    keyed as delivery.push_checks_by_repo is (repo_named): with `repo` (the checked repository; `p` its
    project) one it does not name gets (), so its checks run unwrapped. No `repo`: the wrapper itself,
    whatever its scope (its lock's identity)."""
    v = ((cfg or {}).get("checks") or {}).get("wrap_cmd") if isinstance((cfg or {}).get("checks"), dict) else None
    repos: list[str] | None = None
    if isinstance(v, dict):
        if bad := set(map(str, v)) - {"cmd", "repos"}:
            raise ValueError(f"{WRAP_KEY} takes only cmd and repos, not {', '.join(sorted(bad))}")
        repos = [v["repos"]] if isinstance(v.get("repos"), str) else v.get("repos")
        if not isinstance(repos, list) or not repos or not all(isinstance(k, str) and k.strip() for k in repos):
            raise ValueError(f"{WRAP_KEY}.repos must be a repo key or a non-empty list of them (as "
                             f"delivery.{BY_REPO} keys: {HARNESS_KEY!r}, a path or a checkout's folder name)")
        v = v.get("cmd")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{WRAP_KEY}.cmd must be a command string")
    if v is None or v == "":
        return ()
    if not isinstance(v, str):
        raise ValueError(f"{WRAP_KEY} must be a command string or a map of cmd and repos, not {type(v).__name__}")
    try:
        words = tuple(shlex.split(v))
    except ValueError as e:
        raise ValueError(f"{WRAP_KEY} {v!r} does not parse: {e}") from e
    if repos is not None and repo is not None and not repo_named(repos, Path(repo), p):
        return ()
    return words


def wrap_problem(wrap: tuple[str, ...], cwd: str | Path, env: dict | None = None) -> str:
    """Why the wrapper `wrap` cannot run in `cwd` ("" when it can): a path (relative to `cwd`) or a
    name on `env`'s PATH that is missing or not executable."""
    if not wrap:
        return ""
    exe = wrap[0]
    if os.sep in exe:
        path = Path(cwd) / exe
        ok = path.is_file() and os.access(path, os.X_OK)
    else:
        ok = shutil.which(exe, path=(env if env is not None else os.environ).get("PATH")) is not None
    return "" if ok else (f"{WRAP_KEY}: the wrapper {exe!r} is missing or not executable, so the commands "
                          f"after `--` were not run (never unwrapped); fix the path or unset {WRAP_KEY}")


SPILL_WRAP_KEY = "checks.spill_wrap_cmd"


def _words(v: Any, key: str) -> tuple[str, ...]:
    if v is None or v == "":
        return ()
    if not isinstance(v, str):
        raise ValueError(f"{key} must be a command string, not {type(v).__name__}")
    try:
        return tuple(shlex.split(v))
    except ValueError as e:
        raise ValueError(f"{key} {v!r} does not parse: {e}") from e


def spill_wrap_of(c: Any, cfg: dict | None) -> tuple[str, ...]:
    """The wrapper check `c` runs behind on a spill host, as argv words: its own `spill_wrap_cmd`, else
    `checks.spill_wrap_cmd`; () for none. A relative path is the project's own, in the copy of the commit
    there (remotecheck). ValueError when it does not parse: the check then never spills."""
    if own := getattr(c, "spill_wrap_cmd", ""):
        return _words(own, f"{str(c)!r}: spill_wrap_cmd")
    checks = (cfg or {}).get("checks")
    return _words(checks.get("spill_wrap_cmd") if isinstance(checks, dict) else None, SPILL_WRAP_KEY)


def spill_to(checks: list, host: str, cfg: dict | None) -> list:
    """`checks` run on spill host `host` (checkspill), each behind its remote wrapper (spill_wrap_of), never
    this machine's checks.wrap_cmd; a check with host "local" that opted in (`spill: true`) goes too."""
    out = []
    for c in checks:
        c2 = Check(str(c))
        if isinstance(c, Check):
            c2.__dict__.update(c.__dict__)
        c2.spill_host, c2.remote_wrap = host, spill_wrap_of(c2, cfg)
        out.append(c2)
    return out


def _ship_plugins(env: dict, py: tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    """A remote check's exports and shipped files (remotecheck, slurmcheck) when its env puts this runtime's
    pytest plugins (pushpass.PLUGIN_DIR: a narrowed check's select plugin, testpick.narrow) on PYTHONPATH:
    the plugins go with the copy of the commit and the entry names that copy, never this host's folder.
    The check keeps its env, so its pass key (cli._command_key) is a run's here."""
    local = str(pushpass.PLUGIN_DIR)
    if local not in env.get("PYTHONPATH", ""):
        return env, py
    env = {**env, "PYTHONPATH": env["PYTHONPATH"].replace(local, f"$PWD/{remotecheck.SHIP_DIR}")}
    return env, py + tuple(str(f) for f in sorted(pushpass.PLUGIN_DIR.glob("*.py")) if str(f) not in py)


WRAP_LANE_KEY = "checks.wrap_lane"
WRAP_LANES = ("before", "after")


def wrap_lane_of(cfg: dict | None) -> str:
    """`checks.wrap_lane`: where a `ttp checks` run behind checks.wrap_cmd takes the wrapper lock's lane
    (checklane). "before" (the default): before it starts the wrapper, so its runs go one at a time even
    when the wrapper fails instead of waiting. "after": a ttp step (checklane.lane_argv) between the
    wrapper and each command takes it once the wrapper runs that command, its own lock held, so a run
    queued on the wrapper's lock holds no lane. ValueError for another value."""
    sec = (cfg or {}).get("checks")
    v = sec.get("wrap_lane") if isinstance(sec, dict) else None
    if v is None or v == "":
        return "before"
    if not isinstance(v, str) or v.strip().lower() not in WRAP_LANES:
        raise ValueError(f"{WRAP_LANE_KEY} must be one of {', '.join(WRAP_LANES)}, not {v!r}")
    return v.strip().lower()


FALLBACK_KEY = "checks.fallback_lane"
UNWRAPPED = "unwrapped"


def fallback_lane_of(cfg: dict | None, cwd: str | Path | None = ".", env: dict | None = None) -> tuple[str, tuple[str, ...], str]:
    """`checks.fallback_lane`: where the commands behind checks.wrap_cmd run while that wrapper's lock is
    paused (`ttp lock`: "held by pause on"). Default "": off, they wait for the pause. A wrapper command
    (parsed as wrap_cmd is; a CPU pool's `ttp lock` for example), or "unwrapped": no wrapper, with
    TTP_NO_DEVICE=1 set (a CPU-only run). Returns (its label, its wrapper argv, why it cannot serve or
    ""): one that does not parse, is not executable here or is checks.wrap_cmd itself cannot, and the
    checks wait for the pause then (never fail). One level only: a paused fallback waits too. With `cwd`
    None (config_problems) the wrapper is not looked for."""
    sec = (cfg or {}).get("checks")
    v = sec.get("fallback_lane") if isinstance(sec, dict) else None
    if v is None or (isinstance(v, str) and not v.strip()):
        return "", (), ""
    if not isinstance(v, str):
        return str(v), (), f"{FALLBACK_KEY} must be a wrapper command or {UNWRAPPED!r}, not {type(v).__name__}"
    if v.strip().lower() == UNWRAPPED:
        return UNWRAPPED, (), ""
    try:
        wrap = tuple(shlex.split(v))
    except ValueError as e:
        return v, (), f"{FALLBACK_KEY} {v!r} does not parse: {e}"
    try:
        if wrap == wrap_of(cfg):
            return v, (), f"{FALLBACK_KEY} is {WRAP_KEY} itself, whose lock is the paused one"
    except ValueError:
        pass
    if cwd is not None and (why := wrap_problem(wrap, cwd, env)):
        return v, (), why.replace(WRAP_KEY, FALLBACK_KEY, 1).split(", so ")[0]
    return v, wrap, ""


def to_fallback(checks: list, wrapped: set, wrap: tuple[str, ...], lane: tuple[str, ...] = ()) -> list:
    """Moves each of `checks` named in `wrapped` (run behind checks.wrap_cmd) onto the fallback lane, in
    place: behind `wrap` (and its lane step `lane`), or, with no `wrap` ("unwrapped"), with no wrapper and
    TTP_NO_DEVICE=1. Returns the commands moved."""
    moved = []
    for c in checks:
        if str(c) in wrapped and isinstance(c, Check):
            c.wrap, c.lane = tuple(wrap), tuple(lane) if wrap else ()
            if not wrap:
                c.env = tuple(sorted({**dict(getattr(c, "env", None) or ()), "TTP_NO_DEVICE": "1"}.items()))
            moved.append(str(c))
    return moved


def with_wrap(checks: list, wrap: tuple[str, ...], lane: tuple[str, ...] = (), skip=lambda c: False) -> list:
    """`checks` with each one that runs on this machine behind `wrap` (check_argv); one with a check
    host runs there unwrapped, as the wrapper is this machine's, and so does one `skip` names (a check
    that needs no device: checklane.no_device). No `wrap`: `checks` unchanged. `lane`
    (checklane.lane_argv, with checks.wrap_lane "after"): the step that runs between the wrapper and
    each command, taking the wrapper lock's lane once the wrapper holds its lock."""
    if not wrap:
        return list(checks)
    out = []
    for c in checks:
        c2 = Check(str(c))
        if isinstance(c, Check):
            c2.__dict__.update(c.__dict__)
        if not remote_host(c2) and not skip(c):
            c2.wrap = tuple(wrap)
            if lane:
                c2.lane = tuple(lane)
        out.append(c2)
    return out


def checks_host_of(v: Any) -> str:
    """A `delivery.checks_host` value: an ssh alias, or "" for this machine."""
    v = "" if v is None else str(v).strip()
    if v and (why := remotecheck.host_problem(v)):
        raise ValueError(why)
    return v


def remote_host(c: Any) -> str:
    """The machine check `c` runs on, or "" for this one: its spill host (spill_to) first."""
    host = getattr(c, "spill_host", "") or getattr(c, "host", "") or getattr(c, "default_host", "")
    return "" if host == remotecheck.LOCAL else host


def where(c: Any) -> str:
    """" (on <host>)" for a remote check, else ""."""
    return f" (on {h})" if (h := remote_host(c)) else ""


def hosts_of(cmds: list) -> dict[str, str]:
    """{command: host} of the remote checks in `cmds`, for the records that name where each ran."""
    return {str(c): h for c in cmds if (h := remote_host(c))}


def remote_fault(c: Any, rc: int | None, cwd: str | Path) -> dict | None:
    """The envfault record when remote check `c` exited remotecheck.FAULT_RC (ssh or the host failed)."""
    if rc != remotecheck.FAULT_RC or not (h := remote_host(c)):
        return None
    return remotecheck.fault(h, str(c), str(cwd))


def harness_plan_problem(cmd: str) -> str | None:
    """Why a `purpose: harness` check is not exactly `ttp check-plan --purpose harness
    (--definition-commit <full hex> | --definition-at-head) --definition-path <repo path>`: its
    coverage must come from a committed definition (pinned, or HEAD's own), never from a selector
    that lists the harness head itself."""
    want = (f"{cmd!r}: a purpose harness check must be `ttp check-plan --purpose harness "
            "--definition-commit <full commit ID> --definition-path <repo path>` (or --definition-at-head "
            "in place of --definition-commit)")
    try:
        words = shlex.split(cmd)
    except ValueError:
        return want
    if len(words) < 2 or posixpath.basename(words[0]) != "ttp" or words[1] != "check-plan":
        return want
    opts: dict[str, str] = {}
    rest = words[2:]
    while rest:
        flag, eq, value = rest.pop(0).partition("=")
        if flag == "--definition-at-head":
            if eq:
                return want
        elif not eq:
            if not rest:
                return want
            value = rest.pop(0)
        if flag not in ("--purpose", "--definition-commit", "--definition-at-head",
                        "--definition-path") or flag in opts:
            return want
        opts[flag] = value
    pinned = opts.get("--definition-commit")
    if (opts.get("--purpose") != "harness" or len(opts) != 3 or "--definition-path" not in opts
            or not ("--definition-at-head" in opts
                    or pinned and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pinned))
            or not _inside(opts["--definition-path"])):
        return want
    return None


def _if_exists(c: dict) -> str:
    """The check's `if_exists`, normalized so 'tests/' and './tests/a.sh' match ls-tree paths."""
    p = str(c.get("if_exists") or "").strip() if isinstance(c.get("if_exists"), str) else ""
    return posixpath.normpath(p) if p else ""


def _if_changed(c: dict) -> tuple[str, ...]:
    """The check's `if_changed` globs (one string or a list), normalized as `_if_exists`."""
    return _globs(c.get("if_changed"))


def _globs(v: Any) -> tuple[str, ...]:
    """Repo-relative globs from one string or a list, normalized as `_if_exists`."""
    globs = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return tuple(posixpath.normpath(g.strip()) for g in globs if isinstance(g, str) and g.strip())


def scalar_problem(v: Any) -> str:
    """Why a list setting's value is a scalar (true, 5) that `_entries` reads as no entries, or ""."""
    return f"{v!r}: not a list" if v and not isinstance(v, (str, list, tuple, dict)) else ""


def check_list(v: Any) -> list[Check]:
    """Check commands from a config value (`_entries`), as `Check`s."""
    return [c for c in map(_check, _entries(v)) if c]


def matches(files: list[str], pattern: str) -> bool:
    """Whether a repo path or glob matches one of `files` (repo-relative); a directory by its files."""
    import fnmatch
    return any(f == pattern or f.startswith(pattern + "/") or fnmatch.fnmatchcase(f, pattern) for f in files)


class ScopeError(Exception):
    """The diff an `if_changed` check is scoped by could not be read: the check fails, never skips."""

    def __init__(self, check: Check, why: str):
        super().__init__(f"{why}; failing {check}")
        self.check = check


class Skipped(str):
    """A `skipped_line`; `scoped` when the check was skipped because the diff is outside its
    `if_changed` scope; `harness_only` when a candidate run left out a `purpose: harness` check."""
    scoped = harness_only = False


def skip_reason(repo: Path, head: str, check: Check, base: str = "") -> str | None:
    """Why `check` does not apply to `head`, or None: it runs. `if_changed` is looked up in the diff
    from `base` (the push target) to `head`; without a base it is not applied, and a diff git cannot
    give raises ScopeError. `if_exists` is looked up in `head`'s files."""
    changed = _changed_reason(repo, head, check, base)
    return changed or _scoped_exists_reason(repo, head, check, base)


def _scoped_exists_reason(repo: Path, head: str, check: Check, base: str) -> str | None:
    missing = _exists_reason(repo, head, check)
    if missing and check.if_changed and base:
        raise ScopeError(check, f"change is inside the check's scope but {missing}")
    return missing


def _changed_reason(repo: Path, head: str, check: Check, base: str) -> str | None:
    globs = getattr(check, "if_changed", ())
    if not (globs and base):
        return None
    diff = _git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", f"{base}...{head}")
    if diff.returncode != 0:
        raise ScopeError(check, f"cannot tell what {head[:10]} changes since {base[:10]} "
                                f"({_tail(diff.stderr) or f'git diff exit {diff.returncode}'})")
    files = [f for f in diff.stdout.splitlines() if f]
    if any(matches(files, g) for g in globs):
        return None
    if not files and _adds_nothing(repo, base, head):
        # An empty self-diff (the tip checked against itself, or a head already in base) proves nothing
        # about scope: the check runs, so a skip is never read as that commit's pass.
        return None
    return f"{head[:10]} changes nothing under {', '.join(globs)} since {base[:10]}"


def _adds_nothing(repo: Path, base: str, head: str) -> bool:
    """`head` has no commit beyond `base` (the same commit, or an ancestor of it). A git error reads as
    adding nothing, so the check runs."""
    if head == base:
        return True
    ahead = _git(repo, "rev-list", "--count", f"{base}..{head}")
    return ahead.returncode != 0 or ahead.stdout.strip() == "0"


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _exists_reason(repo: Path, head: str, check: Check) -> str | None:
    if not getattr(check, "if_exists", ""):
        return None
    ls = _git(repo, "ls-tree", "-r", "--name-only", head)
    if ls.returncode != 0:
        return None   # cannot tell: run it
    if matches(ls.stdout.splitlines(), check.if_exists):
        return None
    return f"{check.if_exists} does not exist at {head[:10]}"


def skipped_line(check: Check, why: str) -> str:
    return f"skipped (not applicable: {why}): {check}"


def outside_scope(checks: list[Check], skipped: list[str]) -> bool:
    """Every check was skipped because the change touches none of its `if_changed` paths: the change
    is outside what any check covers, as a docs-only change is when no checks are set. A check
    skipped by `if_exists` (its file is missing) says nothing about the change, so it keeps the
    head unchecked (NONE_APPLY). A harness check a candidate run leaves out (HARNESS_ONLY) counts neither way."""
    own = [s for s in skipped if not getattr(s, "harness_only", False)]
    return (bool(own) and len(skipped) == len(checks)
            and all(getattr(s, "scoped", False) for s in own))


def check_argv(cmd: str, top: str | Path | None = None, env: dict | None = None, isolate: bool = True,
               commit: str = "") -> list[str]:
    """How a check command runs: first-failure semantics, so `a; b` fails when `a` fails and a
    pipeline fails when any stage does (bash `-e -o pipefail`; plain `sh -e` where bash is missing).
    With `top` (the worktree it runs in), a bare pytest runs with the repository's venv, else the one
    `env` (the check's environment; default this process's) has active (venv_cmd). A check with a host
    (remote_host) runs there instead, on a copy of `commit`, else of the checkout's HEAD (remotecheck; one
    tagged `slurm` as a batch job on that cluster, slurmcheck). A local check runs
    in a process sandbox (sandbox.wrap) unless it opts out or `isolate` is false; sandbox_lines says
    when one cannot."""
    exports = dict(getattr(cmd, "env", ()) or ())
    wrap = tuple(getattr(cmd, "wrap", ()) or ())
    lane = tuple(getattr(cmd, "lane", ()) or ()) if wrap else ()
    isolate = isolate and getattr(cmd, "sandbox", True) is not False
    if slurm := getattr(cmd, "slurm", None):   # the account's Slurm overflow: one batch job (slurmcheck)
        there, py = _ship_plugins(exports, tuple(getattr(cmd, "ship_py", ()) or ()))
        return slurmcheck.argv(str(cmd), slurm, there, commit, getattr(cmd, "time_min", 0), py,
                               dict(getattr(cmd, "literal_env", ()) or ()))
    if host := remote_host(cmd):
        there, py = _ship_plugins(exports, tuple(getattr(cmd, "ship_py", ()) or ()))
        return remotecheck.argv(str(cmd), host, there, commit, tuple(getattr(cmd, "remote_wrap", ()) or ()), py,
                                dict(getattr(cmd, "literal_env", ()) or ()))
    cmd = venv_cmd(cmd, top, env) if top else cmd
    cmd = remotecheck.exports(exports) + testslots.clamp_for(cmd, env)
    bash = shutil.which("bash")
    argv = [bash, "-e", "-o", "pipefail", "-c", cmd] if bash else ["sh", "-e", "-c", cmd]
    # checks.wrap_cmd goes outside the sandbox: a lock or queue script sees the host as it does elsewhere.
    # With checks.wrap_lane "after", the lane step runs inside the wrapper, before the command.
    return [*wrap, *lane, *sandbox.wrap(argv, isolate, env)]


def sandbox_lines(checks: list) -> list[str]:
    """What a run of `checks` says about its process sandbox: why there is none (once), and each
    local check that opted out. Nothing when every local check runs in one."""
    local = [c for c in checks if not remote_host(c)]
    off = [c for c in local if getattr(c, "sandbox", True) is False] if sandbox.opted_in() else []
    lines = [notice for notice in [sandbox.notice()] if notice and len(off) < len(local)]
    return lines + [f"{c}: {sandbox.notice(False)}" for c in off]


VENV_DIRS = (".venv", "venv")
# A pytest run at a command's start: after the line's start or a separator, and VAR=value words,
# `env [VAR=value...]` and `timeout [options] <duration>` wrappers.
_SEG = r"(^|&&|\|\||[;|(\n])"
_LEAD = (r"\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|env|"
         r"timeout(?:\s+-[A-Za-z-]+(?:[= ]\S+)?)*\s+\d+(?:\.\d+)?[smhd]?)\s+)*")
_BARE = r"(?:pytest|py\.test|python3?\s+-m\s+pytest)(?=\s|$|[;&|)])"
_PYTEST_RUN = re.compile(_SEG + "(" + _LEAD + ")" + _BARE)
# A pytest run behind a launcher in one command segment: the first command word after a `--` word,
# past `env [VAR=value...]`: `[<path>/]pytest`, `[<path>/]py.test` or `[<path>/]python[N[.M]] -m pytest`
# (`launcher.py -- .venv/bin/python -m pytest -q tests`). Not in a segment that is itself a pytest run,
# by any of those words (`.venv/bin/pytest -q -- tests/pytest`: its `--` ends pytest's own options), or
# starts with a command printing or searching text (`grep -- pytest`, `echo -- pytest`).
_TEXT_TOOLS = r"\s*(?:echo|printf|grep|egrep|fgrep|rg|ag|git|sed|awk|cat|test|\[|\[\[)(?=\s|$)"
_PYTEST_WORD = (r"(?:[^\s;&|()'\"]*/)?(?:pytest|py\.test|python(?:\d+(?:\.\d+)?)?\s+-m\s+pytest)"
                r"(?=\s|$|[;&|)])")
_PYTEST_WRAPPED = re.compile(
    _SEG + "(?!" + _TEXT_TOOLS + ")(?!" + _LEAD + _PYTEST_WORD + ")"
    r"([^;&|(\n]*?\s--\s+)((?:env(?:\s+[A-Za-z_][A-Za-z0-9_]*=\S*)*\s+)?)"
    "(" + _PYTEST_WORD + ")")


def pytest_runs(cmd: str) -> int:
    """How many pytest runs `cmd` starts: at a segment's start (_PYTEST_RUN) or behind a launcher's
    `--` (_PYTEST_WRAPPED)."""
    return len(_PYTEST_RUN.findall(cmd)) + len(_PYTEST_WRAPPED.findall(cmd))


def venv_python(top: str | Path, env: dict | None = None) -> Path | None:
    """The python of the repository's venv (.venv/ or venv/, with pytest installed in it) in the
    worktree `top`, else in its main checkout (a linked worktree has no copy of an ignored venv), else
    the venv `env` (default this process's environment) has active, or None."""
    return _venv(top, env)[0]


def _checkouts(top: str | Path) -> list[Path]:
    """The worktree `top`, then its main checkout when it is a linked worktree or a scratch clone."""
    bases = [Path(top)]
    args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    r = repoid.read(top, args, lambda: subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True))
    common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    if common and common.resolve() == Path(top).resolve() / ".git":
        common = cleantree.scratch_of(top) or common   # a scratch clone's is its source's
    if common and common.name == ".git" and common.parent not in bases:
        bases.append(common.parent)
    return bases


def _venv(top: str | Path, env: dict | None = None) -> tuple[Path | None, str]:
    """(venv_python, "") or, when the only venv is the main checkout's (or the active one) and it has
    a package installed editable from outside the worktree `top`, (None, why): the worktree's tests
    would import the main checkout's code and could pass whatever the worktree's code does. The active
    venv (VIRTUAL_ENV: a run's is the project's) comes last, so an extra `ttp checks -- pytest` runs in
    the environment the project's checks run in, not with whatever pytest PATH finds first."""
    bases = _checkouts(top)
    active = (os.environ if env is None else env).get("VIRTUAL_ENV") or ""
    cands = [(base, base / d) for base in bases for d in VENV_DIRS]
    if active and Path(active).is_absolute():
        cands.append((bases[-1], Path(active)))
    for base, venv in cands:
        py, bin_ = venv / "bin" / "python", venv / "bin"
        if os.access(py, os.X_OK) and (bin_ / "pytest").exists():
            if base != bases[0] and (src := editable_outside(venv, top, base)):
                return None, (f"the venv {venv} has the project installed editable from {src}, outside "
                              f"this worktree, so its tests would import that code, not this worktree's; "
                              f"pytest runs as written (make a venv in this worktree to use one)")
            return py, ""
    return None, ""


def broken_venvs(top: str | Path, also: list[Path] = ()) -> list[tuple[Path, str]]:
    """The venvs a check may use (.venv/ or venv/ in the worktree `top` or its main checkout, and
    `also`: the project's and the active one) whose python does not run, each with why. A link that
    dangles, or a venv made on another OS (its bin/python points at an interpreter missing here),
    leaves the checks without its packages; venv_python then skips it, and they fail at import."""
    seen, out = set(), []
    for venv in [b / d for b in _checkouts(top) for d in VENV_DIRS] + [Path(v) for v in also]:
        if not (venv.is_symlink() or (venv / "pyvenv.cfg").is_file()):
            continue   # not there, or a directory that is no venv
        real = Path(os.path.realpath(venv))
        if real in seen:
            continue
        seen.add(real)
        py = venv / "bin" / "python"
        try:
            r = subprocess.run([str(py), "-c", "pass"], capture_output=True, text=True, timeout=30)
            why = "" if r.returncode == 0 else \
                f"{py} -c pass exited {r.returncode}: {(r.stderr.strip().splitlines() or [''])[-1][:200]}"
        except FileNotFoundError:
            why = (f"{venv} links to {os.readlink(venv)}, which is missing" if venv.is_symlink() and not real.exists()
                   else f"{py} is missing or points at an interpreter that is missing")
        except (OSError, subprocess.SubprocessError) as e:
            why = f"{py} -c pass failed: {e}"
        if why:
            out.append((venv, why))
    return out


def venv_rebuild_cmd(venv: Path) -> str:
    """The command that makes the venv `venv` again where it points, with the packages of the
    checkout it sits in (an editable install with its test or dev extras, else its requirements.txt,
    else pytest alone)."""
    real, src = Path(os.path.realpath(venv)), venv.parent
    q = shlex.quote
    if (src / "pyproject.toml").is_file() or (src / "setup.py").is_file():
        try:   # the keys of its [project.optional-dependencies] table (no tomllib before Python 3.11)
            text = (src / "pyproject.toml").read_text(errors="replace")
        except OSError:
            text = ""
        table = re.search(r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
        opt = set(re.findall(r"^\s*\"?([A-Za-z0-9_.-]+)\"?\s*=", table.group(1), re.M)) if table else set()
        extras = ",".join(e for e in ("test", "dev") if e in opt)
        what = f"-e {q(str(src) + (f'[{extras}]' if extras else ''))}"
        # declared requirements, read only from [project].dependencies, the chosen extras and the
        # test/dev dependency groups: bounded ones keep their bounds, a group is installed whole
        arr = r"\[(?:\s*(?:\"[^\"]*\"|'[^']*'|#[^\n]*|,|\{[^}]*\}))*\s*\]"

        def table(name):
            m = re.search(rf"^\[{re.escape(name)}\][ \t]*(?:#[^\n]*)?$(.*?)(?=^\[|\Z)", text, re.M | re.S)
            return m.group(1) if m else ""

        def entries(body, key):
            m = re.search(rf"^\s*[\"']?{re.escape(key)}[\"']?\s*=\s*({arr})", body, re.M)
            if not m:
                return []   # one tokenizer: strings first, then comments and inline tables (include-group), which are skipped
            toks = re.findall(r"\"([^\"]*)\"|'([^']*)'|(#[^\n]*|\{[^}]*\})", m.group(1))
            return [a or b for a, b, skip in toks if not skip]

        def bounded(r):
            return "@" not in r and ";" not in r and re.match(r"[A-Za-z0-9_.-]+(\[[^\]]*\])?\s*[<>=!~]", r) \
                and re.search(r"<|~=", r)

        reqs = {r.strip() for r in entries(table("project"), "dependencies") if bounded(r.strip())}
        for e in extras.split(","):
            reqs |= {r.strip() for r in entries(table("project.optional-dependencies"), e) if bounded(r.strip())}
        for g in ("test", "dev"):
            reqs |= {r.strip() for r in entries(table("dependency-groups"), g) if r.strip() and "@" not in r}
        what += "".join(f" {q(b)}" for b in sorted(reqs))
        if (src / "requirements.txt").is_file():
            what += f" -r {q(str(src / 'requirements.txt'))}"
    elif (src / "requirements.txt").is_file():
        what = f"-r {q(str(src / 'requirements.txt'))}"
    else:
        what = "pytest"
    return f"python3 -m venv --clear {q(str(real))} && {q(str(real / 'bin' / 'python'))} -m pip install {what}"


def editable_outside(venv: Path, top: str | Path, main: Path) -> str:
    """A path in the main checkout `main`, outside the worktree `top`, that the venv has a package
    installed editable from (its dist-info direct_url.json, an __editable__ finder's mapping or a
    .pth line), or ""."""
    top, main, venv = Path(top).resolve(), main.resolve(), venv.resolve()

    def outside(path: str) -> bool:
        try:
            q = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return (q == main or main in q.parents) and not (q == top or top in q.parents or venv in q.parents)

    for site in venv.glob("lib/python*/site-packages"):
        for f in site.glob("*.dist-info/direct_url.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict) or not isinstance(d.get("dir_info"), dict):
                continue
            url = str(d.get("url") or "")
            path = unquote(url[7:]) if url.startswith("file://") else ""
            if d["dir_info"].get("editable") and path and outside(path):
                return path
        for f in [*site.glob("__editable__*finder.py"), *site.glob("*.pth")]:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            paths = re.findall(r"""['"](/[^'"]+)['"]""", text) if f.suffix == ".py" else \
                [ln.strip() for ln in text.splitlines() if ln.strip().startswith("/")]
            for path in paths:
                if outside(path) and Path(path).exists():
                    return path
    return ""


# Files above a worktree that pytest picks up: a conftest.py it loads, or a config file that makes
# that directory its rootdir (and so its confcutdir).
PYTEST_FILES = ("conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py")


def nested_pytest(top: str | Path) -> bool:
    """A directory above the worktree `top` has a file pytest would pick up (PYTEST_FILES): a worktree
    nested in a project root that has a conftest.py, say. A plain pytest there may take that directory as
    its rootdir, load its conftest.py and import its package instead of the worktree's."""
    try:
        return any((d / f).is_file() for d in Path(top).resolve().parents for f in PYTEST_FILES)
    except OSError:
        return False


def venv_cmd(cmd: str, top: str | Path, env: dict | None = None) -> str:
    """`cmd` with each bare pytest run (`pytest`, `py.test`, `python[3] -m pytest`) made
    `<venv>/bin/python -m pytest` when the repository has a venv (venv_python); else `cmd` as it is.
    A bare pytest otherwise runs with whatever python is on PATH, which may lack the repo's dependencies.
    When the main checkout's venv is skipped for an editable install from outside the worktree, the
    command stays as written and first prints why to the checks output.
    In a worktree nested below pytest files (nested_pytest), each such run is also kept to the worktree:
    `--rootdir` and `--confcutdir` set to it (unless the command sets them) and it first on PYTHONPATH
    (a PYTHONPATH the run itself sets still wins), so it tests the worktree's code, not the outer one's.
    A run behind a launcher's `--` (_PYTEST_WRAPPED) is changed the same way where it stands, so the launcher
    keeps its own words: a bare one gets the venv python, one naming its own python keeps it, and
    PYTHONPATH goes in as `env PYTHONPATH=...` before it."""
    if not pytest_runs(cmd):
        return cmd
    py, why = _venv(top, env)
    nested = nested_pytest(top)
    if not py and not nested:
        return f"printf '%s\\n' {shlex.quote('ttp: ' + why)} >&2\n{cmd}" if why else cmd
    q = shlex.quote(str(top))
    pre = f" PYTHONPATH={q}${{PYTHONPATH:+:$PYTHONPATH}} " if nested else ""
    flags = "".join(f" --{f}={q}" for f in ("rootdir", "confcutdir") if nested and f"--{f}" not in cmd)

    def run(m: re.Match) -> str:
        bare = m.group(0)[len(m.group(1)) + len(m.group(2)):]
        tail = (f"{shlex.quote(str(py))} -m pytest" if py else bare) + flags
        return f"{m.group(1)}{pre}{m.group(2).lstrip() if pre else m.group(2)}{tail}"

    def wrapped(m: re.Match) -> str:
        # One `env` call with the run's own VAR= words after the worktree's, so they win.
        own, bare = m.group(3), m.group(4)
        tail = (f"{shlex.quote(str(py))} -m pytest" if py and re.fullmatch(_BARE, bare) else bare) + flags
        lead = f"env{pre}{own[3:].lstrip()}" if pre else own
        return f"{m.group(1)}{m.group(2)}{lead}{tail}"

    # Wrapped runs first: a run _PYTEST_RUN rewrote is never scanned again as a launcher's.
    out = _PYTEST_RUN.sub(run, _PYTEST_WRAPPED.sub(wrapped, cmd))
    return f"printf '%s\\n' {shlex.quote('ttp: ' + why)} >&2\n{out}" if why else out


ENV_FIX = ("check command lacks dependencies: point delivery checks at a venv, or pin them in the "
           "checks' Python lock (checks.python_lock; `ttp checks --freeze-lock <file>` writes one)")
_NO_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '?([A-Za-z0-9_.]+)'?")
_NO_PYTEST = re.compile(r"No module named '?pytest'?$|\bpy(?:test|\.test): (?:command )?not found", re.M)


def env_problem(output: str, top: str | Path) -> str | None:
    """Why a failed check failed at import for want of an installed package (an environment problem,
    not the head's code), or None. Said for pytest itself missing, and for a conftest or test module
    that could not be collected because a module the repository does not contain is not installed."""
    if _NO_PYTEST.search(output):
        return f"pytest is not installed for the python the check ran with; {ENV_FIX}"
    if not re.search(r"ImportError while (?:loading conftest|importing test module)|^ERROR collecting|"
                     r"^E\s+(?:ModuleNotFound|Import)Error", output, re.M):
        return None
    missing = []
    for name in dict.fromkeys(m.group(1).split(".")[0] for m in _NO_MODULE.finditer(output)):
        if not _in_repo(name, top):
            missing.append(name)
    if not missing:
        return None
    return f"{', '.join(missing)} not installed for the python the check ran with; {ENV_FIX}"


_TEST_FAILED = re.compile(r"^FAILED \S|^=+ .*\b[1-9]\d* failed\b", re.M)


def test_failures(output: str) -> bool:
    """A test run's output reports tests that ran and failed (pytest's FAILED lines or summary)."""
    return bool(_TEST_FAILED.search(output))


def _in_repo(name: str, top: str | Path) -> bool:
    """The repository has a module or package `name` (its own code, so a failed import is the head's)."""
    r = _git(Path(top), "ls-files")
    if r.returncode != 0:
        return True   # cannot tell: not said to be the environment
    for f in r.stdout.splitlines():
        parts = f.split("/")
        if name in parts[:-1] or parts[-1] in (f"{name}.py", f"{name}.pyi") or \
                parts[-1].startswith(f"{name}.") and parts[-1].endswith((".so", ".pyd")):
            return True
    return False


def runner_wait(cmds: list, repo: Path, env: dict, say: Callable[[str], None], p: Project | None = None) -> int:
    """BUSY when a check's runner is missing on this host (envfault): recorded once for the
    coordinator (in the state of `p`, default TTP_PROJECT's project), said with its retry_when, and
    nothing run or counted against the change; else 0."""
    if p is None and (base := os.environ.get("TTP_PROJECT")):
        p = Project(base)
        p = p if p.exists() else None
    fault = envfault.precheck(cmds, repo, env)
    if not fault:
        if p:
            envfault.recheck(p)
        return 0
    envfault.record(p, fault, os.environ.get("TTP_TASK"))
    say(f"{envfault.line(fault)}; not pushing. Hand off `waiting`; retry_when: {envfault.retry_when(p, fault)}")
    return BUSY


def env_wait(p: Project | None, repo: Path, base: str, head: str, err: Exception, say: Callable[[str], None]) -> int:
    """The checks' pinned Python env could not be made (`err`, runnerenv.Unavailable): CHECKS_FAILED when
    `head` changed checks.python_lock in `repo` from `base` itself (runnerenv.changed_by), else BUSY, a
    host fault recorded and said with its retry_when as runner_wait does: nothing counted against the change."""
    cfg = p.config() if p else {}
    if runnerenv.changed_by(repo, cfg, base, head):
        say(f"{err}; this change edits checks.python_lock; not pushing")
        return CHECKS_FAILED
    fault = runnerenv.fault(runnerenv.lock_of(cfg, repo), err)
    envfault.record(p, fault, os.environ.get("TTP_TASK"))
    say(f"{envfault.line(fault)}; not pushing. Hand off `waiting`; retry_when: {envfault.retry_when(p, fault)}")
    return BUSY


def check_slot(head: str, say: Callable[[str], None], who: str = "", priority: str = "", proj: Project | None = None):
    """A host-wide checks slot (checkcap) held while the push checks run, so `ttp push --own` counts
    toward the same cap as `ttp checks`. Checks gating a push to the push branch (`ttp push` and the
    push queue) pass `priority` (priority_key): they hold the push lock, so they take that branch's
    priority slot beyond the cap instead of queueing behind task checks; `ttp land` passes its local
    branch's, `ttp push --own` its remote's own-branch lane (own_lane_key). Nested in a check that holds
    one already (checkcap.HELD_ENV), it does not wait. Slots it cannot write raise checkcap.Unavailable.
    The slot's wait is logged in the state of `proj` (default: TTP_PROJECT's project)."""
    if proj is None and (base := os.environ.get("TTP_PROJECT")):
        proj = Project(base)
    name = proj.name if proj else None
    state = proj.state if proj else None
    if not who:
        who = checkcap.label(name, "ttp push")

    def turn() -> checkcap.Slot:   # an ordinary turn in the host-wide queue
        prio, owns = checkcap.task_rank(proj)
        # Its slot is taken once its test slots could be (pool_room) and the host's load lets a suite
        # start (load_held: one run per load pass passes the guard): it never holds one while it waits for either.
        cfg = proj.config() if proj else {}
        return checkcap.acquire(who, f"push checks on {head[:12]}", say, project_name=name,
                                priority=prio, state=state, owns=owns,
                                startable=None if os.environ.get(testslots.HELD_ENV)
                                else lambda: not testslots.load_held(prio) and testslots.pool_room(cfg, name))
    # A priority slot another push holds past checkcap.priority_wait_s(): an ordinary turn instead, on the
    # pool's test slots (its reserved lane may be that holder's).
    slot = checkcap.acquire_priority(priority, who, f"push checks on {head[:12]}", say, name, state,
                                     fallback=turn) if priority else turn()
    if getattr(slot, "fell_back", False):
        priority = ""
    # The run's share of the host's test-worker slots, held and freed with the checks slot (testslots);
    # push-priority checks take the branch's reserved test slot when the pool has none free.
    slot.tests = testslots.acquire(who, say, cfg=proj.config() if proj else {}, project_name=name or "",
                                   push_key=priority)
    return slot


def run_slot(run: list, head: str, say: Callable[[str], None], priority: str = "",
             proj: Project | None = None) -> checkcap.Slot:
    """The slot a push's check run holds: check_slot only when one of `run` runs here and is no light
    check (checklane.is_light). Checks run on another host (remote_host) use none of this machine's
    CPU: they take no host-wide checks slot or test slots here and never wait for this machine's load
    guard; they wait instead while their host's demand is near its cores (remoteload.wait), never
    starting there while it is."""
    if proj is None and (base := os.environ.get("TTP_PROJECT")):
        proj = Project(base)
    cfg = proj.config() if proj else {}
    claims: list = []
    if hosts := list(dict.fromkeys(h for c in run if (h := remote_host(c)))):
        bound = testslots.push_load_wait_s(cfg)
        remoteload.wait(hosts, say, bound)
        from . import checkspill   # the host's check slots, claimed as `ttp checks` claims them (bounded here)
        if (claims := checkspill.admit_pinned(proj, hosts, say, "push checks", bound=bound)) is None:
            say(f"no check slot freed on {', '.join(hosts)} in {bound:.0f}s (checks.push_load_wait_s), "
                "starting anyway")
            claims = []
    pats = checklane.light_patterns(cfg)
    if any(not remote_host(c) and not checklane.is_light(str(c), pats) for c in run):
        slot = check_slot(head, say, priority=priority, proj=proj)
    else:
        slot = checkcap.Slot()
    if slot is not None and hasattr(slot, "pinned"):
        slot.pinned = claims
    else:
        for c in claims:
            c.close()
    return slot


def spill_slot(cmds: list, head: str, say: Callable[[str], None], proj: Project | None = None,
               what: str = "push checks") -> tuple[Any, list]:
    """With delivery.checks_spill_first on and spill hosts set, the push checks `cmds` go to a spill host
    before any place here, as `ttp checks` and the push batch send theirs (checkspill.spill_run): (a slot
    holding that host's claim and no slot here, `cmds` sent there; a check that may not spill but is cheap
    runs here beside them), else (None, `cmds`): check_slot then. A check's own "host" stays its own. The
    spill hosts are a speed-up: an error trying them keeps the checks here. Nested in a check that holds a
    slot already (checkcap.HELD_ENV) it never spills. `proj` defaults to TTP_PROJECT's project."""
    if proj is None and (base := os.environ.get("TTP_PROJECT")):
        proj = Project(base)
    if proj is None or not cmds or os.environ.get(checkcap.HELD_ENV):
        return None, cmds
    try:
        claim, out = checkspill.spill_run(proj, proj.config(), cmds, say, f"{what} on {head[:10]}")
    except Exception as e:   # noqa: BLE001
        say(f"spill hosts not tried ({e}): the checks run here")
        return None, cmds
    if claim is None:
        return None, cmds
    slot = checkcap.Slot()
    slot.spill = claim
    return slot, out


def _run_or_here(cmd: Any, repo: Path, env: dict, say: Callable[[str], None], seen: dict,
                 here: Callable[[], Any]) -> tuple[Any, int]:
    """_run_one, and a check spill_slot sent to a spill host that could not run it (BUSY: ssh, the host or
    its pool, never the change) once more here, in the slot `here()` takes: (the check that ran last, its
    exit code). A spill host's fault never holds the push."""
    rc = _run_one(cmd, repo, env, say, seen)
    if rc != BUSY or (was := getattr(cmd, "was", None)) is None:
        return cmd, rc
    say(f"spill host {remote_host(cmd)} could not run {was}: running it here")
    seen.clear()
    try:
        slot = here()
    except checkcap.Unavailable as e:
        say(f"{e}; not pushing")
        return was, CHECKS_FAILED
    with slot:
        return was, _run_one(was, repo, {**env, **slot.env()}, say, seen)


def priority_key(repo: Path, remote: str, branch: str) -> str:
    """The push branch as one name host-wide, whichever project or clone pushes to it: the remote's
    URL (without a trailing .git or /) and the branch; the repository's path when it has no URL."""
    url = _git(repo, "remote", "get-url", remote).stdout.strip() if remote else ""
    url = re.sub(r"(\.git)?/*$", "", url) if url else str(Path(repo).resolve() / remote)
    return f"{url}#{branch}"


# The key of the reserved lane every `ttp push --own` of one remote shares (own_lane_key): no git branch
# name holds a space, so it never names a real branch's lane.
OWN_LANE = "own-branch pushes"


def own_lane_key(repo: Path, remote: str, cfg: dict | None) -> str:
    """The push lane `ttp push --own` takes its checks slot through (check_slot's `priority`): one
    priority slot and reserved test lane per remote shared by every own-branch push, so own pushes
    never wait behind task checks in the host cap or their project's test budget, and run one at a
    time. "" (an ordinary turn, as before) with delivery.own_push_reserved_lane off (false, 0, "off")."""
    v = ((cfg or {}).get("delivery") or {}).get("own_push_reserved_lane")
    if v is False or str(v).strip().lower() in ("0", "false", "off", "no"):
        return ""
    return priority_key(repo, remote, OWN_LANE)


check_lock: Path | None = None   # the lock check_env uses when given none: the project's (`ttp push` sets it)


def check_env(mode: str, tip: str = "", run_dir: str | None = None, branch: str = "",
              lock: Path | None = None) -> dict[str, str]:
    """The environment push_checks run in. TTP_PUSH_MODE says what they gate: `target` (`ttp push`
    or the push queue, to delivery.push_branch), `own` (`ttp push --own`, the task's own branch) or
    `checks` (`ttp checks`, which pushes nothing). TTP_PUSH_TIP is the tip of the branch pushed to,
    as fetched before the checks; empty when there is none (a new branch, or `ttp checks`).
    TTP_PUSH_BRANCH is the name of that branch on the remote (or the local branch `ttp land` moves),
    short, without refs/heads/, so a check can refuse content by where it goes rather than by the
    branch checked out; empty for `ttp checks`. TTP_LEAK_PATTERNS_FILE names the account's local
    leak-pattern file when it has one (leakfile). Every check runs in the pinned Python env built
    from `lock` (default runnerenv.LOCK), the user site off: runnerenv.Unavailable when it cannot be made."""
    output = checkdirs.writable(run_dir if run_dir is not None else os.environ.get("TTP_RUN_DIR"))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and k not in checkcap.RUN_ONLY_ENV}  # a `ttp checks` run's own state stays out of its checks
    if pp := check_pythonpath(os.environ):
        env["PYTHONPATH"] = pp
    env = leakfile.with_path(env)   # the account's leak-pattern file, by path, for a check's temp HOME
    # host-state stand-ins on PATH, after only the pinned env's bin (which holds none of their commands)
    env = hoststate.fake_env(env, Path(output) / "hoststate" if output else None)
    return runnerenv.apply(checkdirs.for_check({**env, "TTP_RUN_DIR": output, "TTP_PUSH_MODE": mode,
                                                "TTP_PUSH_TIP": tip or "", "TTP_PUSH_BRANCH": short_branch(branch)}),
                           lock or check_lock)


def short_branch(ref: str) -> str:
    """`ref` as a short branch name: refs/heads/ dropped."""
    ref = str(ref or "")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def check_pythonpath(env) -> str:
    """`env`'s PYTHONPATH as the checks get it (check_env): without this runtime's folder, which
    tt-project puts there for its own `ttp` (a worker run, the daemon), not for a project's tests,
    which would import the harness's package. The user's own entries stay, in order; "" when none is left."""
    runtime = Path(__file__).resolve().parent.parent
    keep = [e for e in env.get("PYTHONPATH", "").split(os.pathsep)
            if not (e and Path(os.path.realpath(e)) == runtime)]
    return os.pathsep.join(keep) if any(keep) else ""


def exclude_list(v: Any) -> list[str]:
    """`delivery.push_exclude_paths` as repo-relative globs (a list, a JSON list or one per line);
    empty, the default, turns the guard off."""
    return [posixpath.normpath(str(g).strip()) for g in _entries(v) if not isinstance(g, dict)]


def fast_forward_list(v: Any) -> list[str]:
    """`delivery.fast_forward_also` as branch names (a list, a JSON list or one per line); empty, the
    default, turns it off."""
    return list(dict.fromkeys(str(b).strip() for b in _entries(v) if not isinstance(b, dict) and str(b).strip()))


def fast_forward_problem(branch: str, push_branch: str) -> str:
    """Why `branch` may not be in `delivery.fast_forward_also`, or "" when it may. `push_branch` is
    the resolved branch, or the raw setting (`<remote>/<branch>` or `<branch>`)."""
    if branch == push_branch or push_branch.endswith(f"/{branch}"):
        return "it is the push branch itself (delivery.push_branch)"
    if branch.startswith("-") or subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"],
                                                capture_output=True).returncode != 0:
        return "not a valid branch name"
    return ""


def fast_forward_check(v: Any, push_branch: str) -> list[str]:
    """`delivery.fast_forward_also` as config_set takes it; ValueError naming the first bad branch."""
    if v and not isinstance(v, (str, list, tuple)):
        raise ValueError(f"delivery.fast_forward_also: {v!r}: not a list of branch names")
    names = fast_forward_list(v)
    for b in names:
        if why := fast_forward_problem(b, push_branch):
            raise ValueError(f"delivery.fast_forward_also: {b!r}: {why}")
    return names


def fast_forward(repo: Path, remote: str, sha: str, branches: list[str], push_branch: str,
                 say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)) -> list[str]:
    """After `sha` reached `push_branch`, move each of `branches` (delivery.fast_forward_also) on
    `remote` to it, without force and only when its tip there is an ancestor of `sha`. Each outcome
    is read back from the remote with ls-remote: `ff <branch> <sha>`, or `not ff <branch>: <reason>`.
    A `not ff` never undoes the main push; the caller reports it as a warning."""
    out = []
    for b in branches:
        if why := fast_forward_problem(b, push_branch):
            line = f"not ff {b}: {why}"
        elif not (tip := _existing_tip(repo, remote, b)):
            line = f"not ff {b}: {remote}/{b} does not exist or cannot be read"
        elif tip != sha and not descends(repo, tip, sha):
            line = f"not ff {b}: {remote}/{b} at {tip[:10]} is not an ancestor of {sha[:10]}; left as it is"
        else:
            r = _git(repo, "push", remote, f"{sha}:refs/heads/{b}") if tip != sha else None
            ls = _git(repo, "ls-remote", "--heads", push_side(repo, remote), f"refs/heads/{b}")
            now = next((ln.split()[0] for ln in ls.stdout.splitlines() if ln.endswith(f"\trefs/heads/{b}")), "")
            if now == sha:
                line = f"ff {b} {sha}"
            elif r is not None and r.returncode != 0:
                line = f"not ff {b}: the push was rejected: {' / '.join(r.stderr.strip().splitlines()[-3:])[-300:]}"
            else:
                line = f"not ff {b}: {remote}/{b} reads {now[:10] or '(nothing)'} after the push, not {sha[:10]}"
        say(line if line.startswith("ff ") else f"warning: {line}")
        out.append(line)
    return out


def ff_warnings(lines: Any) -> list[str]:
    """The `not ff` and `not mirrored` lines of a recorded fast_forward outcome."""
    return [str(x) for x in lines or [] if str(x).startswith(("not ff ", "not mirrored "))]


MIRROR_TIMEOUT_S = 300   # each network step of one mirror (ls-remote, fetch, push)


def mirror_list(v: Any) -> list[dict]:
    """`delivery.mirror_remotes` as {remote, branch} tables (a list, a JSON list, or `<remote>/<branch>`
    strings); empty, the default, turns it off. Anything else comes back with an empty remote, so
    mirror_problem names it."""
    out: list[dict] = []
    for x in _entries(v):
        if isinstance(x, dict):
            m = {"remote": str(x.get("remote") or "").strip(), "branch": str(x.get("branch") or "").strip()}
        else:
            r, _, b = str(x).strip().partition("/")
            m = {"remote": r, "branch": b} if b else {"remote": "", "branch": r}
        if m not in out:
            out.append(m)
    return out


def mirror_problem(m: dict, push_branch: str) -> str:
    """Why the mirror `m` ({remote, branch}) may not be in `delivery.mirror_remotes`, or "" when it may.
    `push_branch` is the resolved `<remote>/<branch>`, or the raw setting."""
    remote, branch = m.get("remote") or "", m.get("branch") or ""
    if not REMOTE_NAME.fullmatch(remote):
        return "not a {remote, branch} table naming a git remote"
    if f"{remote}/{branch}" == push_branch or remote == "origin" and branch == push_branch:
        return "it is the push branch itself (delivery.push_branch)"
    if branch.startswith("-") or subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"],
                                                capture_output=True).returncode != 0:
        return "not a valid branch name"
    return ""


def mirror_check(v: Any, push_branch: str) -> list[dict]:
    """`delivery.mirror_remotes` as config_set takes it; ValueError naming the first bad entry."""
    if why := scalar_problem(v):
        raise ValueError(f"delivery.mirror_remotes: {why}")
    out = mirror_list(v)
    for m in out:
        if why := mirror_problem(m, push_branch):
            raise ValueError(f"delivery.mirror_remotes: {m['remote']}/{m['branch']}: {why}")
    return out


def _git_net(repo: Path, *args: str, timeout_s: float = MIRROR_TIMEOUT_S) -> subprocess.CompletedProcess:
    """git that talks to a remote: no prompt, no stdin, at most `timeout_s` (exit 124 when it runs past)."""
    if args[:1] == ("push",):
        _git_push_began()
    t = gitmemo.timeout(timeout_s, args)
    try:
        return gitmemo.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=t,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {t:.0f} s")


def _remote_head(repo: Path, remote: str, branch: str) -> tuple[bool, str]:
    """(read, tip) of `branch` on `remote` by ls-remote: tip is "" when the branch does not exist."""
    ls = _git_net(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    tip = next((ln.split()[0] for ln in ls.stdout.splitlines() if ln.endswith(f"\trefs/heads/{branch}")), "")
    return ls.returncode == 0, tip


def mirror(repo: Path, sha: str, mirrors: list[dict], push_branch: str,
           say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)) -> list[str]:
    """After `sha` reached `push_branch`, push it to each of `mirrors` (delivery.mirror_remotes): fetch
    the mirror branch, and push `sha` to it without force only when its tip is an ancestor of `sha` (a
    branch the mirror lacks is created). Each outcome is read back with ls-remote: `mirror <remote>/<branch>
    <sha>`, or `not mirrored <remote>/<branch>: <reason>`, which names both commits when the mirror has
    diverged. A `not mirrored` never undoes the main push; the caller reports it as a warning."""
    out = []
    remotes = _git(repo, "remote").stdout.split()
    for m in mirrors:
        name = f"{m.get('remote')}/{m.get('branch')}"
        try:
            line = _mirror_one(repo, sha, m, push_branch, remotes)
        except Exception as e:      # one mirror's trouble never stops the others or the push
            line = f"not mirrored {name}: {type(e).__name__}: {e}"
        say(line if line.startswith("mirror ") else f"warning: {line}")
        out.append(line)
    return out


def mirror_safely(repo: Path, sha: str, mirrors: list[dict], push_branch: str,
                  say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)) -> list[str]:
    """mirror(), whose failure is only a `not mirrored` line per mirror: the main push stands."""
    try:
        return mirror(repo, sha, mirrors, push_branch, say=say)
    except Exception as e:
        return [f"not mirrored {x.get('remote')}/{x.get('branch')}: {type(e).__name__}: {e}" for x in mirrors]


def _mirror_one(repo: Path, sha: str, m: dict, push_branch: str, remotes: list[str]) -> str:
    remote, branch = m["remote"], m["branch"]
    name = f"{remote}/{branch}"
    if why := mirror_problem(m, push_branch):
        return f"not mirrored {name}: {why}"
    if remote not in remotes:
        return f"not mirrored {name}: there is no git remote named {remote}"
    side = push_side(repo, remote)   # where the push below goes
    read, tip = _remote_head(repo, side, branch)
    if not read:
        return f"not mirrored {name}: {remote} cannot be read; nothing pushed"
    if tip:
        f = _git_net(repo, "fetch", "--no-tags", side, f"+refs/heads/{branch}:{tracking_ref(side, branch)}")
        if f.returncode != 0:
            return f"not mirrored {name}: cannot fetch it ({_tail_line(f.stderr)}); nothing pushed"
        if tip != sha and not descends(repo, tip, sha):
            return f"not mirrored {name}: it is at {tip}, not an ancestor of the pushed {sha}; left as it is"
    r = None
    if tip != sha:   # no leading + and no --force: git itself refuses anything but a fast-forward
        r = _git_net(repo, "push", "--porcelain", remote, f"{sha}:refs/heads/{branch}")
    read, now = _remote_head(repo, side, branch)
    if now == sha:
        return f"mirror {name} {sha}"
    if r is not None and r.returncode != 0:
        return f"not mirrored {name}: the push was rejected: {_tail_line(r.stderr or r.stdout)}"
    return f"not mirrored {name}: it reads {now[:10] or '(nothing)'} after the push, not {sha[:10]}"


def _tail_line(text: str) -> str:
    return " / ".join((text or "").strip().splitlines()[-3:])[-300:]


def run_note(text: str) -> None:
    """A line in the run's notes (progress.md), the feed people read live; nothing outside a run."""
    run_dir = os.environ.get("TTP_RUN_DIR")
    if run_dir:
        try:
            durable_append(Path(run_dir) / "progress.md", f"{timefmt.clock()} {text}\n")
        except OSError:
            pass


def excluded(repo: Path, tip: str, head: str, globs: list[str]) -> list[str]:
    """Files a commit of `head` not yet on `tip` adds or modifies (a deletion is fine) that match
    one of `globs` (`matches`: a path, a directory by its files, or an fnmatch glob whose `*` also
    crosses `/`). Each commit counts, not only the end result: a file added and later deleted would
    still reach the branch's history. Commits whose patch `tip` already has are left out."""
    if not globs:
        return []
    log = _git(repo, "-c", "core.quotePath=false", "log", "--no-merges", "--right-only", "--cherry-pick",
               "--no-renames", "--diff-filter=AMT", "--name-only", "--format=", f"{tip}...{head}")
    return [f for f in dict.fromkeys(log.stdout.splitlines()) if f and any(matches([f], g) for g in globs)]


def excluded_refusal(files: list[str], upstream: str) -> str:
    more = f" and {len(files) - 5} more" if len(files) > 5 else ""
    return (f"this change adds or modifies files that delivery.push_exclude_paths keeps off {upstream} "
            f"({', '.join(files[:5])}{more}); take them out of the commits that add them (a later "
            "delete still leaves them in its history), or publish the branch with `ttp push --own`; "
            "not pushing")


NONE_APPLY = "every check was skipped as not applicable, so nothing checked it"
NONE_APPLY_FIX = "add a delivery.push_checks entry that applies to this change"
OUT_OF_SCOPE = ("every check was skipped by its if_changed scope: this change touches nothing any check "
                "covers, so it goes unchecked")
NONE_APPLY_OWN_FIX = ("run the repository's tests on this commit with `ttp checks -- <cmd>` (`ttp push --own` "
                      "runs the commands it recorded passing there), or " + NONE_APPLY_FIX)
RECORDED = "checks.json"   # in a run's directory, as `ttp checks` writes it (prguard.CHECKS_FILE)


def recorded_checks(run_dir: str | Path | None) -> dict | None:
    """What `ttp checks` last recorded in the run's directory when it passed for the candidate purpose:
    {"head", "commands"}, or None (no run, no record, a failed, harness or malformed one)."""
    try:
        rec = json.loads((Path(run_dir) / RECORDED).read_text()) if run_dir else None
    except (OSError, ValueError):
        return None
    if not (prguard.candidate_record(rec) and rec.get("passed") is True and isinstance(rec.get("head"), str)
            and isinstance(rec.get("commands"), list)):
        return None
    return {"head": rec["head"], "commands": [str(c) for c in rec["commands"] if str(c).strip()]}


def recorded_extras(recorded: dict | None, head: str, checks: list[Check]) -> list[str]:
    """The commands beyond the project's `checks` that `ttp checks -- <cmd>` recorded passing on exactly
    `head`: what `ttp push --own` runs when every project check is skipped there. A record of another
    head, or of nothing but project checks, gives none."""
    if not recorded or not head or recorded.get("head") != head:
        return []
    own = {str(c) for c in checks}
    return [c for c in recorded.get("commands") or [] if c not in own]


HARNESS_ONLY = "purpose harness: it runs only with `ttp checks --purpose harness`, `ttp push` and the push queue"


def applicable(repo: Path, head: str, checks: list[Check], say: Callable[[str], None],
               base: str = "", candidate: bool = False) -> tuple[list[Check], list[str]]:
    """(checks to run on `head`, a `Skipped` line per check that does not apply there, each also said).
    `base` is the push target `if_changed` compares with. Callers treat "none to run" from a non-empty
    list as a failure (NONE_APPLY), never as a pass, unless `outside_scope`. `candidate` (the
    candidate `ttp checks` and `ttp push --own`, never the push branch) also skips every `purpose:
    harness` check (HARNESS_ONLY): its plan belongs to the harness repo, not the candidate's. Raises ScopeError."""
    todo, skipped = [], []
    for c in checks:
        harness_only = candidate and getattr(c, "purpose", "") == "harness"
        changed = None if harness_only else _changed_reason(repo, head, c, base)
        why = HARNESS_ONLY if harness_only else changed or _scoped_exists_reason(repo, head, c, base)
        if why:
            line = Skipped(skipped_line(c, why))
            line.scoped, line.harness_only = bool(changed), harness_only
            skipped.append(line)
            say(line)
        else:
            todo.append(c)
    return todo, skipped


# Shell builtins and keywords a check may start with; anything else must be a program on PATH or a path.
BUILTINS = {".", ":", "[", "[[", "!", "(", "{", "bash", "sh", "cd", "command", "eval", "exec", "exit",
            "export", "false", "for", "if", "set", "source", "test", "true", "type", "ulimit", "umask",
            "unset", "while", "case", "time", "env"}


def _word_end(s: str, i: int) -> int:
    """Index just past the shell word starting at s[i], keeping quotes, `...`, $(...) and ${...}
    whole; -1 when one of them is not closed."""
    depth = 0                                      # open $( / ( inside a $(...)
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'`":
            j = s.find(c, i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < len(s) and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= len(s):
                return -1
            i = j + 1
            continue
        if s.startswith("${", i):
            j = s.find("}", i)
            if j < 0:
                return -1
            i = j + 1
            continue
        if s.startswith("$(", i) or (c == "(" and depth):
            depth += 1
            i += 2 if c == "$" else 1
            continue
        if c == ")" and depth:
            depth -= 1
        elif not depth and (c.isspace() or c in ";&|<>()"):
            return i
        i += 1
    return -1 if depth else i


def check_problem(cmd: str) -> str | None:
    """Why `cmd` cannot be a check command (its first word is no program, path or builtin), or None.
    Leading VAR=value words are skipped; a bare assignment (`h=$(git rev-parse HEAD); ...`) is a
    command of its own, judged by the command it substitutes, if any; a prefix assignment's
    substitution is judged the same way. Arithmetic $((...)) is no command."""
    rest = cmd.strip()
    while m := re.match(r"[A-Za-z_][A-Za-z0-9_]*=", rest):              # leading VAR=value
        end = _word_end(rest, m.end())
        if end < 0:
            return f"{cmd!r} does not parse as a shell command (unclosed quote or substitution)"
        value, rest = rest[m.end():end], rest[end:].lstrip(" \t")
        sub = None if value.lstrip('"').startswith("$((") else \
            re.fullmatch(r'"?\$\((.*)\)"?|"?`(.*)`"?', value, re.S)    # $((...)) is arithmetic
        inner = sub and (sub.group(1) or sub.group(2) or "").strip()
        if inner and (problem := check_problem(inner)):                   # bare or prefix alike
            return problem
        if not rest or rest[0] in ";&|<>\n":
            return None
    try:
        words = shlex.split(rest)
    except ValueError as e:
        return f"{cmd!r} does not parse as a shell command ({e})"
    if not words:
        return None
    first = words[0]
    if "/" in first or first in BUILTINS or shutil.which(first):
        return None
    return f"{cmd!r}: {first!r} is not a program on PATH, a path or a shell builtin; push_checks are commands"


def _form_problem(c: dict) -> str | None:
    """Why a check object is malformed: it takes "run", "if_exists" (a repo-relative path or glob),
    "if_changed" (one such glob or a non-empty list of them) and "purpose" (PURPOSES) and nothing else."""
    cmd = c.get("run")
    if not isinstance(cmd, str) or not cmd.strip():
        return f"{c!r}: a check object needs \"run\": the command"
    extra = sorted(set(map(str, c)) - CHECK_KEYS)
    if extra:
        return (f"{c!r}: unknown key(s) {', '.join(extra)}; a check object takes only run, if_exists, if_changed, "
                "purpose, host, env, sandbox, cwd, silent_s, ignore_paths, pytest, spill and spill_wrap_cmd")
    cond = c.get("if_exists")
    if cond is not None and not _inside(cond):
        return f"{c!r}: if_exists must be a path or glob inside the repository"
    cond = c.get("if_changed")
    if cond is not None and not (_inside(cond) or (isinstance(cond, list) and cond and all(map(_inside, cond)))):
        return f"{c!r}: if_changed must be a path or glob inside the repository, or a list of them"
    cond = c.get("ignore_paths")
    if cond is not None and not (_inside(cond) or (isinstance(cond, list) and cond and all(map(_inside, cond)))):
        return f"{c!r}: ignore_paths must be a path or glob inside the repository, or a list of them"
    purpose = c.get("purpose")
    if purpose is not None and purpose not in PURPOSES:
        return f"{c!r}: purpose must be one of {', '.join(PURPOSES)} (leave it out to run it for both)"
    if "host" in c and (why := remotecheck.host_problem(c["host"])):
        return f"{c!r}: {why}"
    if "env" in c and (why := remotecheck.env_problem(c["env"])):
        return f"{c!r}: {why}"
    if "sandbox" in c and not isinstance(c["sandbox"], bool):
        return f"{c!r}: sandbox must be true or false"
    if "spill" in c and not isinstance(c["spill"], bool):
        return f"{c!r}: spill must be true or false (leave it out for the default: a check with host local stays here)"
    if "spill_wrap_cmd" in c:
        try:
            if not _words(c["spill_wrap_cmd"], "spill_wrap_cmd"):
                return f"{c!r}: spill_wrap_cmd must be a non-empty command"
        except ValueError as e:
            return f"{c!r}: {e}"
    if "pytest" in c and not isinstance(c["pytest"], bool):
        return f"{c!r}: pytest must be true or false (leave it out to detect a pytest run from the command)"
    if "silent_s" in c and (isinstance(c["silent_s"], bool) or not isinstance(c["silent_s"], (int, float))
                            or not 0 <= c["silent_s"] < float("inf")):
        return f"{c!r}: silent_s must be a number of seconds, 0 or more (0 turns the no-output stop off)"
    if "cwd" in c and c["cwd"] not in cleantree.CWDS:
        return f"{c!r}: cwd must be \"authoring\" (run in the worktree being checked) or \"clean\" (the default)"
    if purpose == "harness":
        return harness_plan_problem(cmd.strip())
    return None


def _inside(v: Any) -> bool:
    """`v` is a repo-relative path or glob: not empty, absolute, home-relative or leaving the repo."""
    p = v.strip() if isinstance(v, str) else ""
    norm = posixpath.normpath(p) if p else ""
    return bool(p) and not p.startswith(("/", "~")) and norm not in (".", "..") and not norm.startswith("../")


def _entry_problem(c: Any) -> str | None:
    """Why a check entry cannot be a check: not a command (check_problem) or a malformed object."""
    if not isinstance(c, dict):
        return check_problem(str(c).strip())
    return _form_problem(c) or check_problem(str(c["run"]).strip())


def check_problems(v: Any) -> list[str]:
    if why := scalar_problem(v):
        return [why]
    return [p for p in map(_entry_problem, _entries(v)) if p]


def check_sig(check: Check) -> str:
    return hashlib.sha256(json.dumps(check.config(), sort_keys=True).encode()).hexdigest()[:20]


def applicability_problems(v: Any, repo: Path | None = None, head: str = "HEAD") -> list[str]:
    """Advisory lint only: these warnings never grant permission to skip a check."""
    out = []
    files = _git(repo, "ls-tree", "-r", "--name-only", head) if repo else None
    for check in check_list(v):
        reasons = []
        if re.search(r"\b[0-9a-fA-F]{40}\b", check):
            reasons.append("pins a SHA")
        if re.search(r"/(?:tmp|var/tmp|private/tmp)/[^\s'\"]+", check):
            reasons.append("uses an absolute temp path")
        if not check.if_exists and files and files.returncode == 0:
            missing = [p for p in path_args(check) if not matches(files.stdout.splitlines(), p)]
            if missing:
                reasons.append("missing target at the tip: " + ", ".join(missing))
        if reasons:
            out.append(f"{check!r}: {'; '.join(reasons)}; use if_exists/if_changed to declare applicability")
    return out


def target_missing(repo: Path, head: str, check: Check, rc: int, output: str, base: str = "") -> dict | None:
    """Classify only a missing named target verified against the tested tree. No pass or skip.
    Without a declared scope, nested component targets cover that component; repository-wide
    tests cover all code. An unknown diff is related, so it cannot turn a code failure into a wait.
    """
    if not (rc and ("can't open file" in output and "No such file" in output
                   or rc == 4 and "file or directory not found" in output
                   or rc == 5 and re.search(r"no tests (?:collected|ran)", output, re.I))):
        return None
    files = _git(repo, "ls-tree", "-r", "--name-only", head)
    if files.returncode:
        return None
    targets = [p for p in path_args(check) if not matches(files.stdout.splitlines(), p)]
    if not targets:
        return None
    diff = _git(repo, "diff", "--name-only", "--no-renames", f"{base}...{head}") if base else None
    unrelated = False
    if diff is not None and diff.returncode == 0:
        changed = diff.stdout.splitlines()
        scopes = check.if_changed
        if not scopes:
            components = {p.split("/")[0] for p in targets}
            scopes = tuple(components) if all("/" in p for p in targets) and not components & {"tests", "test"} else ()
        unrelated = not any(matches(changed, g) for g in scopes) if scopes else not any(
            not is_doc(p) for p in changed)
        # Deleting or changing the test itself always belongs to the check.
        unrelated = unrelated and not any(matches(changed, p) for p in targets)
    return {"kind": "target_missing", "sig": check_sig(check), "check": check.config(),
            "targets": targets, "head": head, "base": base, "worktree": str(repo),
            "rc": rc, "output": output[-8000:], "unrelated": unrelated}


def applicability_failure(repo: Path, head: str, check: Check, rc: int, output: str,
                          base: str = "") -> dict | None:
    """Missing target or an explicit selector coverage gap for the exact tested commit.
    A selector can report `check selector: no case for HEAD <40-hex>` (or `no matching case`).
    This records ownership only: every mandatory command remains applicable and no prior tree's
    pass, byte equivalence or selector verdict for another commit can approve this head.
    """
    missing = target_missing(repo, head, check, rc, output, base)
    if missing or not rc:
        return missing
    selected = re.findall(r"(?i)\bno (?:matching )?(?:(?:selector|check|verification) )?"
                          r"case for (?:HEAD|commit)\s*[:=]?\s*([0-9a-f]{40})\b", output)
    if not selected:
        return None
    tested = _git(repo, "rev-parse", "--verify", head + "^{commit}")
    if tested.returncode or tested.stdout.strip() not in {s.lower() for s in selected}:
        return None
    return {"kind": "selector_missing", "sig": check_sig(check), "check": check.config(),
            "targets": [tested.stdout.strip()], "head": head, "base": base, "worktree": str(repo),
            "rc": rc, "output": output[-8000:], "unrelated": True}


def checks_of(v: Any) -> list[str | dict]:
    """The config value of `v`'s checks, rejecting entries that are not commands (e.g. a sentence
    describing the checks) or malformed check objects."""
    bad = check_problems(v)
    if bad:
        raise ValueError("; ".join(bad))
    return [c.config() for c in check_list(v)]

_GLOB = re.compile(r"[*?\[]")
_REDIRECT = re.compile(r"^[0-9&]*(>>?|<)")
# Options whose value is the next word (an output file or setting, not a check target).
VALUE_OPTS = {"--junitxml", "--junit-xml", "--rootdir", "--basetemp", "--confcutdir", "-c", "-o", "-k", "-m",
              "--override-ini", "--cov", "--cov-report", "--cov-config", "--log-file", "--result-log",
              "--html", "--output", "--report", "-p", "--ignore", "--deselect"}


def path_args(cmd: str) -> list[str]:
    """Path-like arguments of a check command (globs, file paths such as pytest targets) relative to
    the repo root, without a pytest `::node` or `[param]` suffix. Options, `VAR=value` words and the
    program itself are skipped, and so are option values (`--opt value`, `--opt=value`) and redirect
    targets (`> out`, `2>out/x`). After `cd <dir>` later paths are taken relative to <dir> (the dir is
    listed too); after a `cd` that cannot be followed (absolute, `~`, `$VAR`, `-`, out of the repo)
    later paths are skipped."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    out: list[str] = []
    cwd: str | None = ""
    prev = None
    for i, w in enumerate(words):
        if prev == "cd":
            prev = w
            if cwd is None or w.startswith(("-", "/", "~")) or "$" in w:
                cwd = None
                continue
            d = posixpath.normpath(posixpath.join(cwd, w))
            if d == ".." or d.startswith("../"):
                cwd = None
                continue
            cwd = "" if d == "." else d
            if cwd:
                out.append(cwd)
            continue
        if prev in VALUE_OPTS or (prev and _REDIRECT.match(prev) and not _REDIRECT.sub("", prev)):
            prev = w
            continue
        prev = w
        if _REDIRECT.match(w):
            continue
        if i == 0 or w.startswith("-") or "=" in w or "$" in w or w in BUILTINS \
                or w in ("&&", "||", ";", "|"):
            continue
        w = w.split("::", 1)[0]
        w = re.sub(r"(\.py)\[[^\]]*\]$", r"\1", w)
        if cwd is None or not w or w.startswith(("/", "~")) or not ("/" in w or _GLOB.search(w)
                                                                                or w.endswith(".py")):
            continue
        w = posixpath.normpath(posixpath.join(cwd, w))
        if w != "." and w != ".." and not w.startswith("../"):
            out.append(w)
    return out


# tt-project runs no whitespace check of its own. A `git diff --check` in push_checks also flags
# captured output (device-run logs keep trailing whitespace); git itself already skips files that
# .gitattributes marks `-diff` or `binary`. This pathspec leaves out the logs too.
LOG_EXCLUDE = "':(exclude)*.log'"


def whitespace_check(base: str) -> str:
    """A `git diff --check` of `base`..HEAD that skips captured `*.log` output, for push_checks."""
    return f"git diff --check {shlex.quote(base)} HEAD -- . {LOG_EXCLUDE}"


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's empty tree: a root commit's base
HARNESS_TEST_DIRS = ("runtime/tests", "bin/tests")   # the harness's own tests, where it has them


def default_harness_checks(repo: Path, base: str = "", purpose: str = "harness") -> list[Check]:
    """The plan `ttp checks --purpose harness` runs when delivery.push_checks has no purpose harness
    check: a whitespace check of `base` (the push target, else HEAD's parent, else the empty tree)
    to HEAD, and pytest of each of the harness's own test folders it has (`if_exists`: a missing one
    is skipped, never passed). All are tagged harness, so the whitespace check alone qualifies a
    head with no tests. A configured purpose harness check replaces the whole plan. `purpose` ""
    leaves them untagged: what a candidate `ttp checks` runs in the harness repo when no repo
    checks are configured for it (HARNESS_UNSET)."""
    if not base:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", "HEAD^{commit}^"],
                           capture_output=True, text=True)
        base = r.stdout.strip() if r.returncode == 0 else EMPTY_TREE
    out = [Check(whitespace_check(base))]
    for d in HARNESS_TEST_DIRS:
        c = Check(f"python3 -m pytest -q {d}")
        c.if_exists = d
        out.append(c)
    for c in out:
        c.purpose = purpose
    return out


# `ttp checks` fast path: off unless a project lists globs in delivery.checks_fast_globs; a diff
# touching only paths they match then skips the heavy checks. The project owns its globs: one that
# covers installed or test-asserted content skips the checks that content needs. Matched with
# fnmatch against the repo-relative path, so `*` also crosses `/`.
FAST_GLOBS: list[str] = []
# Paths that never take the fast path, whatever the configured globs say: code, scripts, manifests,
# build and dependency files and agent-instruction or installed-content markdown. Override with
# delivery.checks_fast_exclude (a list; it replaces these).
FAST_EXCLUDE = ["*.py", "*.sh", "*.c", "*.cc", "*.cpp", "*.h", "*.hpp", "*.js", "*.ts", "*.json", "*.toml",
                "*.yaml", "*.yml", "*.cfg", "*.ini", "*.cmake", "CMakeLists.txt", "*/CMakeLists.txt", "Makefile",
                "*/Makefile", "requirements*.txt", "*/requirements*.txt", "constraints*.txt",
                "*/constraints*.txt", "SKILL.md", "*/SKILL.md", "AGENTS.md", "*/AGENTS.md", "CLAUDE.md",
                "*/CLAUDE.md", "skills/*", "*/skills/*", "prompts/*", "*/prompts/*",
                "tests/*", "*/tests/*", "test/*", "*/test/*", "template/*", "*/template/*", "templates/*",
                "*/templates/*"]


def fast_exclude(cfg: dict | None) -> list[str]:
    v = ((cfg or {}).get("delivery") or {}).get("checks_fast_exclude")
    return FAST_EXCLUDE if v is None else [str(g) for g in v] if isinstance(v, list) else FAST_EXCLUDE


def fast_globs(cfg: dict | None) -> list[str]:
    v = ((cfg or {}).get("delivery") or {}).get("checks_fast_globs")
    return [str(g) for g in v] if isinstance(v, list) else list(FAST_GLOBS)


def fast_path(repo: Path, base: str, head: str, globs: list[str], exclude: list[str] | None = None) -> bool:
    """True when `base`..`head` changes at least one path and every one matches `globs` and none
    matches `exclude` (default FAST_EXCLUDE), which always wins."""
    import fnmatch
    if not base or not globs:
        return False
    r = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", "-z", "--no-renames", base, head],
                       capture_output=True, text=True)
    paths = [x for x in r.stdout.split("\0") if x]
    bad = FAST_EXCLUDE if exclude is None else exclude
    return r.returncode == 0 and bool(paths) and all(
        any(fnmatch.fnmatchcase(x, g) for g in globs) and not any(fnmatch.fnmatchcase(x, g) for g in bad)
        for x in paths)


def fast_checks(base: str) -> list[str]:
    """The checks of the fast path: the whitespace check and a CRLF check on the added lines."""
    q = shlex.quote(base)
    return [whitespace_check(base),
            f"! git diff {q} HEAD -- . {LOG_EXCLUDE} | grep -q \"$(printf '^+.*\\r$')\""]


def unexcluded_log_checks(checks: list[str]) -> list[str]:
    """Checks running `git diff --check` that do not leave out `*.log` files: committed run logs
    would fail them. They stay as configured; `ttp doctor` names them."""
    out = []
    for cmd in checks:
        try:
            words = shlex.split(cmd)
        except ValueError:
            continue
        after = words[words.index("git") + 1:] if "git" in words else []
        if "diff" in after and "--check" in after and not any(
                w.startswith((":!", ":^", ":(exclude")) and "*.log" in w for w in after):
            out.append(str(cmd))
    return out


def unmatched_paths(repo: Path, remote: str, branch: str, checks: list[str]) -> tuple[str, list[str]]:
    """(ref, ["cmd: path", ...]) for check path args that match no file on the push branch: the
    remote-tracking ref, which `ttp push` rebases onto, else the local branch. ref is "" when
    neither can be read."""
    for ref, full in ((f"{remote}/{branch}", f"refs/remotes/{remote}/{branch}"),
                      (branch, f"refs/heads/{branch}")):
        ls = _git(repo, "ls-tree", "-r", "--name-only", full)
        if ls.returncode == 0:
            break
    else:
        return "", []
    files = ls.stdout.splitlines()
    out = []
    for cmd in checks:
        if getattr(cmd, "if_exists", "") and not matches(files, cmd.if_exists):
            continue  # not applicable there: it is skipped, not failed
        gone: list[str] = []
        for a in path_args(cmd):
            if any(a.startswith(g + "/") for g in gone):
                continue  # under a missing `cd` dir, already reported
            if not matches(files, a):
                gone.append(a)
                out.append(f"{cmd!r}: {a!r}")
    return ref, out


def _git(repo: Path, *args: str, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run git; when not quiet its output goes to stderr, keeping stdout for the verdict."""
    if args[:1] == ("push",):
        _git_push_began()
    t = gitmemo.timeout(None, args)   # none of its own; capped within a daemon tick step or finish transaction
    if quiet:
        run = lambda: gitcache.run(repo, args, lambda: gitmemo.run(   # noqa: E731
            ["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=t), t)
        return repoid.read(repo, args, run) if repoid.identity(args) else run()
    return gitmemo.run(["git", "-C", str(repo), *args], text=True, stdout=2, timeout=t)


def _fetch(repo: Path, remote: str, branch: str) -> str:
    """The remote branch's current tip, or "" when it cannot be read. The explicit refspec also
    works where the remote's configured refspec does not cover the branch."""
    ref = tracking_ref(remote, branch)
    if _git(repo, "fetch", remote, f"+refs/heads/{branch}:{ref}", quiet=False).returncode != 0:
        return ""
    return _git(repo, "rev-parse", "--verify", "--quiet", ref).stdout.strip()


def tracking_ref(remote: str, branch: str) -> str:
    """Where a fetch of `branch` from `remote` is kept: refs/remotes/<remote>/<branch> for a remote's
    name, a ref of its own for a URL (delivery.own_push_remote), which no ref name may hold."""
    if not url_like(remote):
        return f"refs/remotes/{remote}/{branch}"
    return f"refs/ttp/url/{hashlib.sha256(remote.encode()).hexdigest()[:12]}/{branch}"


def _existing_tip(repo: Path, remote: str, branch: str) -> str:
    """The remote branch's tip, fetched, or "" when the remote has no such branch or cannot be read.
    Read where a push to `remote` goes (push_side)."""
    side = push_side(repo, remote)
    ls = _git(repo, "ls-remote", "--heads", side, f"refs/heads/{branch}")
    if ls.returncode != 0 or not any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines()):
        return ""
    return _fetch(repo, side, branch)


def target(p: Project, repo: Path) -> tuple[str, str]:
    """(remote, branch) to push to: `delivery.push_branch` only. `delivery.base_ref` is where work
    starts, which may be the default branch, so it is never a push target."""
    d = p.config().get("delivery") or {}
    ref = str(d.get("push_branch") or "").strip()
    if not ref:
        raise ValueError("no target branch: set delivery.push_branch")
    return _split_target(ref, _git(repo, "remote").stdout.split())


def _split_target(ref: str, remotes: list[str]) -> tuple[str, str]:
    """(remote, branch) of a `delivery.push_branch` value, given the repo's remotes: `<remote>/<branch>`
    when its first part is one of them, else the whole value is a branch of origin."""
    remote, _, rest = ref.partition("/")
    if not (rest and remote in remotes):
        remote, rest = "origin", ref
    return remote, rest[len("refs/heads/"):] if rest.startswith("refs/heads/") else rest


def ref_name(repo: Path, ref: str) -> str:
    """`ref` (branch, remote/branch, refs/heads/... or refs/remotes/...) as remote/branch, on origin
    when it names no remote of `repo`."""
    ref = ref.strip().removeprefix("refs/remotes/")
    remote, _, rest = ref.partition("/")
    if not (rest and remote in _git(repo, "remote").stdout.split()):
        remote, rest = "origin", ref
    return f"{remote}/{rest.removeprefix('refs/heads/')}"


def intended(p: Project, repo: Path, pushed: str) -> str:
    """The branch work pushed to `pushed` (remote/branch) is meant to reach when that is another
    one: the first of `delivery.push_branch` and `delivery.base_ref` naming a different branch, as
    remote/branch; "" when neither does (a commit hash as base_ref names no branch)."""
    d = p.config().get("delivery") or {}
    for key in ("push_branch", "base_ref"):
        ref = str(d.get(key) or "").strip()
        if ref and not re.fullmatch(r"[0-9a-f]{7,64}", ref) and (name := ref_name(repo, ref)) != pushed:
            return name
    return ""


REACH_FETCH_S = 60   # reach() runs after a push landed, foreign_base() before --detach starts: never held up long


def _fetch_bounded(repo: Path, remote: str, branch: str) -> str:
    """_fetch with no prompt, no stdin and at most REACH_FETCH_S: "" when it times out."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        if subprocess.run(["git", "-C", str(repo), "fetch", remote, f"+refs/heads/{branch}:{tracking_ref(remote, branch)}"],
                          stdin=subprocess.DEVNULL, stdout=2, text=True, env=env,
                          timeout=REACH_FETCH_S).returncode != 0:
            return ""
    except subprocess.TimeoutExpired:
        return ""
    return _git(repo, "rev-parse", "--verify", "--quiet", tracking_ref(remote, branch)).stdout.strip()


def reach(p: Project, repo: Path, pushed: str, sha: str | None) -> dict | None:
    """Whether `sha`, pushed to `pushed`, is on the branch it is meant to reach (intended) as well:
    {"ref", "on" (None when that branch cannot be read), "behind" (commits of `sha` it lacks)}; None
    when no other branch is named. Fetches that branch."""
    ref = intended(p, repo, pushed)
    if not (ref and sha):
        return None
    remote, _, branch = ref.partition("/")
    tip = _fetch_bounded(repo, remote, branch)
    if not tip:
        return {"ref": ref, "on": None, "behind": None}
    n = _git(repo, "rev-list", "--count", f"{tip}..{sha}").stdout.strip()
    return {"ref": ref, "on": _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode == 0,
            "behind": int(n) if n.isdigit() else None}


def reach_words(r: dict | None) -> str:
    """What `reach` found, to follow "pushed <sha> to <branch>": never lets a push read as landed on a
    branch it has not reached."""
    if not isinstance(r, dict) or not r.get("ref"):
        return ""
    if r.get("on"):
        return f", also on {r['ref']}"
    if r.get("on") is None:
        return f", not yet seen on {r['ref']} (it could not be read)"
    n = r.get("behind")
    return f", not yet on {r['ref']}" + (f" ({r['ref']} is {n} commit{'s' if n != 1 else ''} behind)"
                                         if isinstance(n, int) and n > 0 else "")


def landing(sha: str | None, pushed: str, r: dict | None = None, version: str | None = None) -> str:
    """A push in words for the user: the short sha, version, the exact branch it went to and, when the
    work is meant to reach another branch, whether it is there (reach_words)."""
    return (f"pushed {(sha or '?')[:7]}" + (f" as {version}" if version else "")
            + (f" to {pushed}" if pushed else "") + reach_words(r))


def push_branch_problem(push_branch: Any, repo: Path | None = None, allow: bool = False) -> str:
    """Why `delivery.push_branch` can never be pushed to, whatever the change, or "" when it may be:
    it names a branch `refusal` always refuses (PROTECTED), or `repo` (the code repo) has no git
    remote, or none of the name the push would use. Local git only: the remote's default branch is
    refused where the remote is asked (refusal, pushq._refusal). Without `repo` (or outside a git
    repo) only the name is checked, its first part read as a remote or as part of the branch.
    `ttp doctor` and the config check report it; reviews of such a project are review only, except on
    an existing local branch of a repo with no remote, where they land with `ttp land` (land.py). `allow`
    (allow_protected) lets main and master through."""
    ref = str(push_branch or "").strip()
    if not ref:
        return ""
    r = _git(repo, "remote") if repo is not None else None
    remotes = r.stdout.split() if r is not None and r.returncode == 0 else None
    if remotes == []:
        if lands_locally(ref, repo, allow):
            return (f"delivery.push_branch: the code repo has no git remote, so {ref} is never pushed; it is a "
                    f"local branch, and reviews land on it with `ttp land`")
        return (f"delivery.push_branch: the code repo has no git remote, so {ref} can never be pushed to; "
                f"reviews are review only (add a remote, or clear delivery.push_branch)")
    if remotes is None:
        names = [_split_target(ref, [])[1], _split_target(ref, [ref.partition("/")[0]])[1]]
    else:
        remote, branch = _split_target(ref, remotes)
        if remote not in remotes:
            return (f"delivery.push_branch: {ref} pushes to {remote}, which is not a git remote of the code "
                    f"repo (it has {', '.join(remotes)}), so it can never be pushed to; reviews are review only")
        names = [branch]
    hit = next((b for b in names if b in PROTECTED and not (allow and b != "HEAD")), "")
    if hit:
        return (f"delivery.push_branch: {ref} is {hit}, a branch ttp push always refuses, so every review push "
                f"would fail; reviews are review only and never fall back to a raw `git push` to it (name a "
                f"branch of its own, or, only on the user's word, set delivery.allow_protected_push_branch)")
    return ""


def lands_locally(push_branch: Any, repo: Path | None, allow: bool = False) -> bool:
    """Whether `push_branch` names an existing local branch of `repo` (the code repo), which has no git
    remote (main/master only with `allow`): reviews land on it with `ttp land` (land.py), so
    push_branch_problem's line for it is information for `ttp doctor`, not a config problem."""
    ref = str(push_branch or "").strip()
    if not ref or repo is None or not remote_less(repo):
        return False
    branch = _split_target(ref, [])[1]
    return (branch not in PROTECTED or (allow and branch != "HEAD")) and bool(_local_tip(repo, branch))


LOCAL_HARNESS = ("nothing to push: this is the project's harness, a local git repo with no remote. "
                 "A commit here is already delivered: the daemon reads the harness from this repo. "
                 "Hand off with the commit's hash; never copy harness files into a code branch to publish them.")


def local_harness(p: Project, repo: Path) -> bool:
    """Whether `repo` is the project's own harness repo and has no git remote: a commit there is
    delivered as it is, and there is nowhere to push it."""
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    try:
        same = bool(top) and Path(top).resolve() == p.harness.resolve()
    except OSError:
        return False
    return same and not _git(repo, "remote").stdout.split()


def own_target(p: Project, repo: Path) -> tuple[str, str]:
    """(remote, branch) for `ttp push --own`: the checked-out branch, published under the same name
    on the remote of `delivery.push_branch` (else origin). A `ttp/t<id>-...` branch must be this
    task's (inside a run), or one the task carries as its branch or continues (_carries: a fix on a
    finished task's PR); any other named branch (one a spec names, e.g. <user>/feature-x) may go
    too. Both only as a fast-forward (publish, ff_only: own_ff_only). A task labelled
    `pr_branch:<branch>` on its own ttp/t<id>-... branch (the PR's branch was held by another
    worktree) publishes its head onto that branch instead, and so does a review covering that task,
    from that task's branch or from the pr_branch itself checked out (_reviewed finds the task); a
    review whose own spec has one `pr_branch:` line publishes the covered task's branch onto it.
    Never a detached HEAD, main/master, the
    push branch or the branch work starts from; publish also refuses the remote's default branch.
    A notes branch (is_notes_branch) is published under its own name whatever pr_branch the task carries.
    Another of the task's own ttp/t<id>-... branches than the one its run was given (_switched) is
    refused, unless the run was given a branch not its own that it was not told to deliver on
    (_fresh): that fresh branch goes under its own name.
    Another task's ttp/t<id>-... branch the task only inherited is refused (foreign_pr_branch).
    With `--own-branch` (OWN_BRANCH_ENV) the task's own checked-out ttp/t<id>-... branch is published
    under its own name, never onto a pr_branch it carries.
    In a repo with no git remote the remote is LOCAL: publish moves the local branch (local_refusal).
    delivery.own_push_remote, when set, is the remote instead; unset, a remote that is someone's local
    working copy or another project's is refused (local_remote_problem)."""
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        raise ValueError("--own publishes the checked-out branch, not a detached HEAD")
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    fresh = bool(m and task and m.group(1) == task and _fresh(p, task, branch))
    as_is = bool(m and task and m.group(1) == task and (fresh or os.environ.get(OWN_BRANCH_ENV)))
    # The branch the dispatcher built it on (delivery_source: its pr_branch or rebases: record, or the
    # one of a task it continues), as its own branch or with that branch itself checked out. Its own
    # record as written first, so an invalid one is refused below rather than skipped.
    onto = (_pr_branch(p, task) or pr_branch_of(p, _task_row(p, task))) if task and not as_is else ""
    notes = is_notes_branch(p, branch)   # publishes itself, never onto a pr_branch
    if task and not as_is and not notes and (named := worktree.named_branch(_task_row(p, task) or {})) in PROTECTED:
        raise ValueError(f"--own never pushes to a shared branch ({named}): this task's pr_branch or "
                         f"rebases: record names it")
    if m and task and m.group(1) == task and not notes and not as_is and (why := _switched(p, task, branch)):
        raise ValueError(why)
    carried = bool(onto and not notes and (m and m.group(1) == task or branch == onto))
    if m and task and m.group(1) != task and not carried and not notes:
        # A review publishes the code task it covers onto that task's pr_branch, as the task would:
        # from that task's own branch, or from the pr_branch itself checked out (a task's ttp/t<id>-...
        # branch another task carries as its pr_branch, e.g. one publishing a finished task's work).
        reviewed = _reviewed(p, task)
        onto = next((b for t in reviewed
                     if (b := pr_branch_of(p, t)) and (str(t["id"]) == m.group(1) or b == branch)), "")
        # Or, from the reviewed task's own branch, onto the one branch a `pr_branch:` line of the
        # review's own spec names (the code task carried no label; the review delivers it there).
        # Accepted gap: a branch still checked out in a done task's kept worktree is not freed here
        # or in worktree.py; the error names the holding worktree, and worktree retention clears it.
        if not onto and any(str(t["id"]) == m.group(1) for t in reviewed):
            named = {g.group("branch") for g in worktree.SPEC_PR_BRANCH.finditer(
                str((_task_row(p, task) or {}).get("spec") or ""))}
            onto = named.pop() if len(named) == 1 else ""
        carried = bool(onto)
    if not carried and not notes and task and rebase_replace(p.config()) != "lease" and any(
            r and worktree.rebased_branch(r) == branch for r in [_task_row(p, task)] + _reviewed(p, task)):
        raise ValueError(f"{branch} is the branch this task rebases: a rebase cannot fast-forward it, and "
                         f"under delivery.rebase_replace new_branch (the default) --own never replaces it; "
                         f"publish the rebase from the task's own new branch")
    if carried:
        if not worktree.valid_branch(onto):
            raise ValueError(f"this task's pr_branch label ({onto!r}) is not one valid branch name; "
                             f"not publishing")
        branch = onto
    elif m and task and m.group(1) != task and not _carries(p, task, m.group(1), branch):
        raise ValueError(f"--own publishes only this task's own branch (ttp/t{task}-...), not {branch}, task "
                         f"#{m.group(1)}'s branch, which this task does not carry: not pushing. If the work "
                         f"must go onto {branch}, {pr_branch_route(branch)}")
    d = p.config().get("delivery") or {}
    remote, shared = target(p, repo) if str(d.get("push_branch") or "").strip() else ("origin", "")
    if configured := own_push_remote(d, repo):
        remote = configured
    elif remote_less(repo):
        remote = LOCAL
    elif why := local_remote_problem(p, repo, remote):
        raise ValueError(why)
    if carried and remote != LOCAL:
        remote = _pr_head(repo, branch, remote)
    if task and not notes and (why := foreign_pr_branch(p, task, branch, f"{remote}/{branch}")):
        raise ValueError(why)
    base = str(d.get("base_ref") or "").strip()
    if branch in PROTECTED or branch in (shared, base, base.partition("/")[2]):
        raise ValueError(f"--own never pushes to a shared branch ({branch})")
    mine = OWN_BRANCH.fullmatch(branch)
    if carried and not (mine and mine.group(1) == task):
        local = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
        if not local and not _on_remote(repo, remote, branch):
            raise ValueError(f"this task's pr_branch {branch} exists neither here nor on {remote}: --own "
                             f"publishes onto an existing branch only, never creates one")
    return remote, branch


def _pr_head(repo: Path, branch: str, remote: str) -> str:
    """The remote a pr_branch goes to: the open PR's head repository when that is a fork other than
    `remote` (prhead.fork_remote, which refuses the fork's main, master and default branch), else
    `remote`, saying why when gh could not tell."""
    fork, note = prhead.fork_remote(repo, branch, remote)
    if note:
        print(f"ttp push: {note}" if fork else f"ttp push: {note}; pushing to {remote} as before", file=sys.stderr)
    return fork or remote


OWN_REMOTE_KEY = "delivery.own_push_remote"


def url_like(remote: str) -> bool:
    """Whether `remote` is a URL or path git pushes to directly rather than a remote's name:
    scheme://..., scp-like host:path, or a filesystem path."""
    return bool(re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", remote) or re.match(r"[^/]+:", remote)
                or remote.startswith(("/", "./", "../", "~")))


def push_sides(repo: Path, remote: str) -> list[str]:
    """Where `git push <remote>` goes, to ask whether a branch or a pushed commit is there: each push
    URL of a remote whose push URL is not its fetch URL (remote.<name>.pushurl, e.g. fetching from an
    upstream and pushing to a fork, or a pushInsteadOf rewrite), as `git remote get-url --push --all`
    gives them; `git ls-remote <name>` and `git fetch <name>` would ask the fetch URL. Else `remote`
    itself (one URL, a URL or path, LOCAL, or no such remote), so its tracking refs stay as they are."""
    if not remote or remote == LOCAL or url_like(remote):
        return [remote]
    pushes = _git(repo, "remote", "get-url", "--push", "--all", remote)
    fetch = _git(repo, "remote", "get-url", remote)
    urls = pushes.stdout.split() if pushes.returncode == 0 else []
    if not urls or fetch.returncode != 0 or urls == [fetch.stdout.strip()]:
        return [remote]
    return urls


def push_side(repo: Path, remote: str) -> str:
    """Where reads of a pushed branch on `remote` go (its tip, a pushed commit): the first of
    push_sides. With several push URLs `git push` pushes to each and fails when one refuses, so a
    push that went through is on the first; asking whether a branch exists checks every one."""
    return push_sides(repo, remote)[0]


def _repo_keys(repo: Path) -> tuple[list[Path], str]:
    """The paths `repo` is known by (its top level, and the main checkout a worktree belongs to) and
    the main checkout's folder name: what a delivery.own_push_remote map is keyed by."""
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    paths = [Path(x).resolve() for x in (top, str(Path(common).parent) if common else "") if x]
    main = Path(common).parent if common and Path(common).name == ".git" else Path(top or repo)
    return paths, main.name


BY_REPO = "push_checks_by_repo"
HARNESS_KEY = "harness"   # the push_checks_by_repo key of the project's own harness repo
HARNESS_UNSET = "no repo checks configured for the harness repo"


def harness_repo(p: Project | None, repo: Path) -> bool:
    """Whether `repo` (its checkout or any worktree of it) is the project's own harness repo: it
    shares the harness's git directory. A harness with no git repo of its own never matches."""
    if p is None or not (p.harness / ".git").exists():
        return False
    mine, theirs = (_git(r, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
                    for r in (repo, p.harness))
    return bool(mine and theirs) and Path(mine).resolve() == Path(theirs).resolve()


def _names(k: str, paths: list[Path], name: str, p: Project | None) -> bool:
    """Whether repo key `k` (not `harness`) names the repository known by `paths` and `name` (_repo_keys):
    its main checkout's folder name, or a path (absolute, ~, or relative to the project root)."""
    path, root = Path(k).expanduser(), p.root if p is not None else None
    return k == name or ("/" in k or k.startswith("~")) \
        and (path if path.is_absolute() or root is None else root / path).resolve() in paths


def repo_named(keys: list[str], repo: Path, p: Project | None = None) -> bool:
    """Whether any of `keys` names `repo` as a delivery.push_checks_by_repo key would: `harness` for the
    project's own harness repo, a path, or its main checkout's folder name."""
    if HARNESS_KEY in keys and harness_repo(p, repo):
        return True
    paths, name = _repo_keys(repo)
    return any(k != HARNESS_KEY and _names(k, paths, name, p) for k in keys)


def repo_checks(d: dict, repo: Path, p: Project | None = None) -> tuple[Any, str]:
    """The check list for `repo` (delivery settings `d`) and where it came from. The
    delivery.push_checks_by_repo entry that names it: `harness` for the project's own harness repo,
    else the repo's path (absolute, or relative to the project root) or its main checkout's folder
    name, as own_push_remote keys them. Else, for the harness repo, only push_checks' `purpose:
    harness` entries (HARNESS_UNSET): the code repo's other push_checks are not its checks. Else
    delivery.push_checks, as for a single-repo project."""
    by = d.get(BY_REPO)
    harness = harness_repo(p, repo)
    if isinstance(by, dict) and by:
        if harness and HARNESS_KEY in by:
            return by[HARNESS_KEY], f"delivery.{BY_REPO}.{HARNESS_KEY}"
        paths, name = _repo_keys(repo)
        for k, v in by.items():
            if k != HARNESS_KEY and _names(k, paths, name, p):
                return v, f"delivery.{BY_REPO}[{k!r}]"
    if harness:
        return [c.config() for c in check_list(d.get("push_checks")) if c.purpose == "harness"], HARNESS_UNSET
    return d.get("push_checks"), "delivery.push_checks"


def all_checks(d: dict) -> list[Check]:
    """Every configured check: delivery.push_checks and each push_checks_by_repo list."""
    by = d.get(BY_REPO)
    return check_list(d.get("push_checks")) + [c for v in (by.values() if isinstance(by, dict) else ())
                                              for c in check_list(v)]


def checks_config(d: dict) -> Any:
    """The check settings a fingerprint covers: delivery.push_checks alone (as before) unless
    push_checks_by_repo is set, then both."""
    by = d.get(BY_REPO)
    return [d.get("push_checks"), by] if by else d.get("push_checks")


def by_repo_problems(v: Any) -> list[str]:
    """Why a delivery.push_checks_by_repo value is malformed, one line each: not a map from a repo key
    (`harness`, a repo path or a folder name) to a check list, or an entry push_checks would refuse."""
    if v is None:
        return []
    if not isinstance(v, dict):
        return [f"{v!r}: not a map from a repo (\"{HARNESS_KEY}\", a repo path or a folder name) to its check list"]
    out = []
    for k, x in v.items():
        if not isinstance(k, str) or not k.strip():
            out.append(f"{k!r}: not a repo key (\"{HARNESS_KEY}\", a repo path or a folder name)")
        elif not isinstance(x, (str, list)):
            out.append(f"{k}: {x!r} is not a check list")
        else:
            out += [f"{k}: {why}" for why in check_problems(x)]
    return out


def checks_by_repo_of(v: Any) -> dict:
    """A delivery.push_checks_by_repo value, each list as push_checks stores it; ValueError if malformed."""
    if bad := by_repo_problems(v):
        raise ValueError("; ".join(bad))
    return {k: checks_of(x) for k, x in (v or {}).items()}


def own_push_remote(d: dict, repo: Path) -> str:
    """delivery.own_push_remote for `repo` (delivery settings `d`): where every own-branch publish
    goes instead of the push branch's remote (else origin). A remote's name or a URL; or a map from a
    repo (its path, or the main checkout's folder name; "*" for any other) to one. "" when unset or
    no entry covers `repo`. Raises ValueError on a value of another kind, or a name `repo` has no
    remote by, so nothing is pushed to a remote nobody chose."""
    v = d.get("own_push_remote")
    if isinstance(v, dict) and v:
        paths, name = _repo_keys(repo)
        hit = next((x for k, x in v.items() if k != "*" and (k == name or ("/" in k or k.startswith("~"))
                                                              and Path(k).expanduser().resolve() in paths)), None)
        v = v.get("*") if hit is None else hit
    if v is None or v == "" or v == {}:
        return ""
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{OWN_REMOTE_KEY}: {v!r} is neither a git remote's name, a URL nor a map of them; "
                         f"not pushing")
    v = v.strip()
    if not url_like(v) and v not in _git(repo, "remote").stdout.split():
        raise ValueError(f"{OWN_REMOTE_KEY} names remote {v}, which this repo has no remote by "
                         f"(`git remote add {v} <url>`, or set the URL itself); not pushing")
    return v


def _local_path(url: str, repo: Path) -> Path | None:
    """The filesystem path a remote's URL names (a path or file:// URL), else None (a network remote)."""
    if url.startswith("file://"):
        return Path(unquote(url[len("file://"):]))
    if not url or url_like(url) and not url.startswith(("/", "./", "../", "~")):
        return None
    path = Path(url).expanduser()
    return path if path.is_absolute() else repo / path


def _owning_project(path: Path) -> tuple[str, Path] | None:
    """(name, folder) of the registered project on this machine (`ttp list`) whose folder holds
    `path` most closely, or None."""
    best = None
    for name, e in (load_registry().get("projects") or {}).items():
        if not (isinstance(e, dict) and e.get("dir")) or e.get("host") not in (None, hostname()):
            continue
        try:
            d = Path(e["dir"]).resolve()
        except OSError:
            continue
        if path.is_relative_to(d) and (best is None or len(d.parts) > len(best[1].parts)):
            best = (name, d)
    return best


def local_remote_problem(p: Project, repo: Path, remote: str) -> str:
    """Why `ttp push --own` must not publish to `remote` while delivery.own_push_remote is unset, or
    "": its push URL is a local path (or file:// URL) that is a non-bare working tree or lies inside
    another registered project's folder, i.e. someone else's copy rather than a shared remote.
    Network remotes and other local paths (e.g. a bare repo of this project's own) pass."""
    url = _git(repo, "remote", "get-url", "--push", remote).stdout.strip()
    path = _local_path(url, repo)
    if path is None or not path.exists():
        return ""
    path = path.resolve()
    owner = _owning_project(path)
    if owner and owner[1] != p.root.resolve():
        what = f"inside project {owner[0]}'s folder"
    elif _git(path, "rev-parse", "--is-bare-repository").stdout.strip() == "false":
        what = "a working copy, not a shared remote"
    else:
        return ""
    return (f"{remote} is a local path ({path}), {what}: --own does not publish there unless "
            f"{OWN_REMOTE_KEY} names it. Set {OWN_REMOTE_KEY} to the remote or URL task branches are "
            f"published to; nothing pushed")


LOCAL = "local"   # own_target's remote in a code repo with no git remote: publishing moves the local branch
OWN_BRANCH_ENV = "TTP_PUSH_OWN_BRANCH"   # `ttp push --own-branch`: the own branch under its own name
LAND_ENV = "TTP_PUSH_LAND"   # `ttp push --land`: a task lands its own branch though its spec has no landing step


def say_target(repo: Path, remote: str, branch: str) -> None:
    """Print where `ttp push --own` publishes, before it pushes anything."""
    here = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() or "HEAD"
    where = f"local branch {branch}" if remote == LOCAL else f"{remote}/{branch}"
    print(f"ttp push: --own target: {where}" + (f" (from {here})" if here != branch else ""), flush=True)


def foreign_owner(p: Project, row: dict | None, branch: str) -> int | None:
    """The id of the other task whose ttp/t<id>-... `branch` is, when `row` (a task, or one about to
    be added) only inherits it, else None. It does not when it, or a task it continues (failed,
    cancelled or blocked work it took over), owns the branch or has a `pr_branch:` line naming it in
    its spec (worktree.SPEC_PR_BRANCH), or is the daemon's fix of open findings on a PR the project
    opened from that branch (`pr_fix:` with `pr_branch:<branch>`), or carries both `pr_branch:<branch>` and
    a continues:<owner> link, wherever it stands (publishkind's conversion of a task that also took
    over failed work keeps that work as its first link). A branch of no known task is
    nobody's here: None."""
    m = OWN_BRANCH.fullmatch(branch or "")
    if not m or not row:
        return None
    owner, seen, t = int(m.group(1)), set(), row
    while t and t.get("id") not in seen:
        seen.add(t.get("id"))
        labels = _labels(t)
        if t.get("id") == owner or worktree.spec_names_pr_branch(str(t.get("spec") or ""), branch) \
                or (f"pr_branch:{branch}" in labels and any(x.startswith("pr_fix:") for x in labels)) \
                or {f"pr_branch:{branch}", f"continues:{owner}"} <= set(labels):
            return None
        c = continues_id(t)
        t = p.db.task(c) if c is not None else None
    return owner if p.db.task(owner) else None


def foreign_pr_branch(p: Project, task: str, branch: str, where: str) -> str:
    """Why `ttp push --own` by task `task` must not publish onto `branch` (at `where`), or "": it is
    another task's branch (foreign_owner) the task, and every code task it reviews, only inherited (a
    pr_branch label, a task it continues or a branch its worktree started on). The answer says how
    to deliver instead: on the task's own branch, under its own name."""
    t = _task_row(p, task)
    if not t:
        return ""
    owners = [foreign_owner(p, r, branch) for r in [t] + _reviewed(p, task)]
    if not owners or None in owners:
        return ""
    own = own_name(t)
    return (f"--own would push to {where}, task #{owners[0]}'s branch, which task #{task} only inherited "
            f"(a pr_branch label, a task it continues or the branch its worktree started on) and its spec "
            f"names in no `pr_branch:` line: not pushing, another task's branch is not this task's to "
            f"change. Deliver on this task's own branch instead: commit on {own} (`git switch -c {own}` "
            f"if it is not checked out) and run `ttp push --own-branch --detach`, which publishes it under "
            f"its own name. If the work must go onto {branch}, hand off blocked saying so: the coordinator "
            f"adds a task whose spec has the line `pr_branch: {branch}`")


def own_name(t: dict) -> str:
    """Task row `t`'s own ttp/t<id>-... branch: its row's branch when that is its own, else the name
    worktree.ensure gives it."""
    own = str(t.get("branch") or "")
    if not ((mo := OWN_BRANCH.fullmatch(own)) and mo.group(1) == str(t["id"])):
        own = f"ttp/t{t['id']}-{worktree.slug(str(t.get('title') or ''))}"
    return own


def remote_less(repo: Path) -> bool:
    """Whether `repo` is a git repo with no remote at all (`git remote` lists none)."""
    r = _git(repo, "remote")
    return r.returncode == 0 and not r.stdout.split()


def _local_tip(repo: Path, branch: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()


def _holder(repo: Path, branch: str) -> str:
    """The other worktree that has local branch `branch` checked out, or "" (none, or `repo` itself)."""
    top, cur = Path(repo).resolve(), ""
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = line[9:]
        elif line == f"branch refs/heads/{branch}" and Path(cur).resolve() != top:
            return cur
    return ""


def local_refusal(repo: Path, branch: str) -> str:
    """Why `ttp push --own` in a repo with no git remote must not move local branch `branch` to HEAD,
    or "": that branch is the published copy there. Never main/master, only as a fast-forward, and
    when another worktree has it checked out only while that worktree has no uncommitted changes
    (_move_local then updates its files with the branch)."""
    if branch in PROTECTED:
        return f"refusing to move the local branch {branch}"
    if branch != _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() \
            and (held := _holder(repo, branch)):
        dirty = _git(Path(held), "status", "--porcelain", "--untracked-files=no")
        if dirty.returncode != 0 or dirty.stdout.strip():
            return (f"no git remote, and {branch} is checked out in another worktree ({held}) with "
                    f"uncommitted changes: fast-forwarding it there would mix them with this work; "
                    f"not publishing. Commit or discard them there, or publish onto a branch no "
                    f"worktree holds, e.g. this task's own")
    tip = _local_tip(repo, branch)
    if tip and _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode != 0:
        return (f"local branch {branch} ({tip[:10]}) is not an ancestor of HEAD: with no git remote --own "
                f"only fast-forwards it, never rewrites it. A rebase delivers on a branch of its own")
    return ""


def _move_local(repo: Path, branch: str, head: str, say: Callable[[str], None]) -> int:
    """Publish `head` in a repo with no git remote: fast-forward local branch `branch` to it, guarded
    by the tip it had (another writer in between makes git refuse). A branch another worktree has
    checked out is fast-forwarded there (`merge --ff-only`), so that worktree's files follow it."""
    if branch == _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip():
        say(f"pushed {head[:10]} to local branch {branch}: no git remote, so the branch is the published "
            f"copy and it is already there")
        return 0
    if why := local_refusal(repo, branch):   # again: it may have changed during the checks
        say(why)
        return REJECTED
    if held := _holder(repo, branch):
        wt = Path(held)
        if _git(wt, "symbolic-ref", "--quiet", "HEAD").stdout.strip() != f"refs/heads/{branch}":
            say(f"could not move local branch {branch}: the worktree {held} no longer has it checked out")
            return REJECTED
        r = _git(wt, "merge", "--ff-only", "--quiet", head)
        if r.returncode != 0:
            say(f"could not fast-forward {branch} in the worktree {held}: {(r.stderr or r.stdout).strip()}")
            return REJECTED
        say(f"pushed {head[:10]} to local branch {branch} (no git remote: the local branch is the published "
            f"copy), fast-forwarded in the worktree {held} that has it checked out")
        return 0
    old = _local_tip(repo, branch) or "0" * len(head)
    r = _git(repo, "update-ref", "-m", "ttp push --own", f"refs/heads/{branch}", head, old)
    if r.returncode != 0:
        say(f"could not move local branch {branch}: {r.stderr.strip()}")
        return REJECTED
    say(f"pushed {head[:10]} to local branch {branch} (no git remote: the local branch is the published copy)")
    return 0


def own_base(p: Project, repo: Path, remote: str) -> str | None:
    """The tip of the shared branch work leaves from (delivery.push_branch) that `ttp push --own`
    compares with: fetched, or the local branch of that name in a repo with no git remote. A worktree
    of another repository, whose remote has no such branch, compares with that remote's own default
    branch instead (_default_base). None when there is none."""
    try:
        tgt = target(p, repo)
    except ValueError:
        return None
    if remote == LOCAL:
        return _local_tip(repo, tgt[1]) or None
    return _fetch(repo, *tgt) or _default_base(repo, *tgt) or None


def _default_base(repo: Path, remote: str, branch: str) -> str:
    """The tip of `remote`'s default branch (its HEAD), fetched, when `remote` has no branch `branch`:
    the repo is not the one the push branch belongs to. "" when it has that branch (its fetch failed
    for another reason), names no default branch, or cannot be read."""
    ls = _git(repo, "ls-remote", "--symref", remote, "HEAD", f"refs/heads/{branch}")
    lines = ls.stdout.splitlines()
    if ls.returncode != 0 or any(ln.endswith(f"\trefs/heads/{branch}") for ln in lines):
        return ""
    head = next((ln.split()[1].removeprefix("refs/heads/") for ln in lines
                 if ln.startswith("ref: refs/heads/") and ln.endswith("\tHEAD")), "")
    return _fetch(repo, remote, head) if head else ""


def no_checks_hint(repo: Path) -> str:
    """NO_CHECKS, plus the delivery.push_checks_by_repo key that gives `repo` checks of its own."""
    return (f"{NO_CHECKS}; for this repository alone, set delivery.push_checks_by_repo"
            f"[{_repo_keys(repo)[1]!r}] instead")


def own_ff_only(branch: str) -> bool:
    """Whether `ttp push --own` to `branch` must fast-forward the remote's before its checks: every
    branch but the running task's own ttp/t<id>-... one (any of them outside a run)."""
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    return not (m and (not task or m.group(1) == task))


def _task_row(p: Project, task: str | None) -> dict | None:
    try:
        return p.db.task(int(task or ""))
    except (ValueError, TypeError):
        return None


def _labels(t: dict | None) -> list[str]:
    try:
        return [str(x) for x in json.loads((t or {}).get("labels") or "[]")]
    except (ValueError, TypeError):
        return []


def _pr_branch(p: Project, task: str | None) -> str:
    """The branch task `task` is recorded as delivering onto (worktree.recorded_pr_branch: its label or
    the latest `pr_branch:` line an update appended to its spec), as written, or ""."""
    t = _task_row(p, task)
    return (worktree.recorded_pr_branch(t) if t else None) or ""


def is_notes_branch(p: Project, branch: str) -> bool:
    """Whether `branch` is a task's notes branch: it matches NOTES_BRANCH and is not that task's own
    code branch (the branch recorded on its row, else ttp/t<id>-<slug(title)>, as worktree.ensure
    names it), so a code task titled e.g. 'Update release notes' still delivers as code."""
    m = NOTES_BRANCH.fullmatch(branch)
    if not m:
        return False
    t = _task_row(p, m.group(1))
    if not t:
        return True
    return branch != (str(t.get("branch") or "") or f"ttp/t{t['id']}-{worktree.slug(str(t.get('title') or ''))}")


def _switched(p: Project, task: str, branch: str) -> str:
    """Why `ttp push --own` must not publish the checked-out `branch`, one of task `task`'s own
    ttp/t<id>-... branches, or "": the worktree left the branch the task's run was given (its row's
    branch) for another of its own. Publishing it would push onto the task's pr_branch, or a
    carried branch another task owns, work that was never on the branch the task delivers from."""
    t = _task_row(p, task)
    given = str((t or {}).get("branch") or "")
    if not given or given == branch:
        return ""
    onto = _pr_branch(p, task)
    to = f", which it publishes onto {onto}" if onto and onto != given else ""
    return (f"this worktree has {branch} checked out, but task #{task} delivers from {given}{to}: "
            f"--own publishes neither. Switch back to {given} (bring the commits over, e.g. "
            f"`git checkout {given} && git merge --ff-only {branch}`) and rerun")


def _fresh(p: Project, task: str, branch: str) -> bool:
    """Whether `branch`, one of task `task`'s own ttp/t<id>-... branches, is a fresh one the task made
    off a branch its run was given that is not its own and that it was not told to deliver on (e.g.
    one inherited through `continues`, when its spec asked for a new branch): neither that branch
    nor its pr_branch is named in a `pr_branch:` line of its spec, and it is no fix of a PR's
    findings (`pr_fix:`). It publishes under its own name, never onto either, and _switched does
    not apply."""
    t = _task_row(p, task) or {}
    given = str(t.get("branch") or "")
    mg = OWN_BRANCH.fullmatch(given)
    if not given or given == branch or (mg and mg.group(1) == task):
        return False
    onto = _pr_branch(p, task)
    spec = str(t.get("spec") or "")
    return not any(b and worktree.spec_names_pr_branch(spec, b) for b in (given, onto)) \
        and not any(x.startswith("pr_fix:") for x in _labels(t))


def _carries(p: Project, task: str, owner: str, branch: str) -> bool:
    """Whether task `task` may publish task `owner`'s ttp/t<id>-... `branch` with --own: it carries
    it as its branch or `pr_branch:` label, or continues `owner` (a fix on a finished task's PR)."""
    t = _task_row(p, task)
    if not t:
        return False
    labels = _labels(t)
    return str(t.get("branch") or "") == branch or worktree.recorded_pr_branch(t) == branch \
        or f"continues:{owner}" in labels


def _may_land(p: Project, task: str, owner: str, branch: str) -> bool:
    """Whether task `task` may land `branch`, task `owner`'s ttp/t<id>-... branch: its own, one the
    task carries as its branch, the change a review checks (reviews push in the change's worktree)
    or a task it depends on or continues."""
    if owner == task:
        return True
    try:
        t = p.db.task(int(task))
    except (ValueError, TypeError):
        return False
    if not t:
        return False
    if t.get("kind") == "review" or str(t.get("branch") or "") == branch:
        return True
    try:
        refs = [str(x) for x in json.loads(t.get("depends_on") or "[]")]
        refs += [str(x).partition(":")[2] for x in json.loads(t.get("labels") or "[]")
                 if str(x).startswith(("auto_review:", "continues:"))]
    except (ValueError, TypeError):
        return False
    return owner in refs


def _on_remote(repo: Path, remote: str, branch: str) -> bool:
    """Whether a push to `remote` finds `branch` there, so it creates none: on each of its push URLs."""
    def has(side: str) -> bool:
        ls = _git(repo, "ls-remote", "--heads", side, f"refs/heads/{branch}")
        return ls.returncode == 0 and any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())
    return all(has(side) for side in push_sides(repo, remote))


def own_landing(cfg: dict) -> str:
    """How work lands while own pushes are off (delivery.own_push): through the push queue or `ttp push`."""
    d = cfg.get("delivery") or {}
    to = str(d.get("push_branch") or "").strip()
    if not to:
        return "set delivery.push_branch: with no push branch and own pushes off, nothing is pushed"
    return (f"its review approves it for the push queue, which lands it on {to}" if push_queue_on(cfg) else
            f"plain `ttp push` lands it on {to}")


def own_gate(p: Project, repo: Path, remote: str, branch: str) -> None:
    """Refuse (ValueError) a push that publishes `branch` on `remote` as with --own when
    delivery.own_push forbids it: "off" refuses every one, "existing_only" (also unset) one the remote
    does not have yet (or cannot be asked about). Unset still lets a code task create its own branch
    (own_pr_branch): draft PRs are published that way. A code repo with no git remote (LOCAL) moves
    only a local branch, which pushes nothing, so it is never refused here."""
    if remote == LOCAL:
        return
    cfg = p.config()
    mode = own_push(cfg)
    unset = (cfg.get("delivery") or {}).get("own_push") is None
    if mode == "off":
        raise ValueError(f"own pushes are off in this project (delivery.own_push = off): it pushes to no "
                         f"remote branch but delivery.push_branch, so {remote}/{branch} is not pushed. Never "
                         f"publish it another way (no `git push`): {own_landing(cfg)}")
    if mode == "existing_only" and not own_pr_branch(p, cfg, branch) and not _on_remote(repo, remote, branch):
        setting = ("delivery.own_push is unset, which reads as existing_only; only the user sets it to on"
                   if unset else "delivery.own_push = existing_only")
        raise ValueError(f"{remote} has no branch {branch}, and this project lets `ttp push --own` publish "
                         f"only onto a branch the remote already has ({setting}): not "
                         f"creating it. Never create it another way (no `git push`): "
                         f"{own_route(p, cfg, branch) or work_route(p, branch) or own_landing(cfg)}")


def own_route(p: Project, cfg: dict, branch: str) -> str:
    """The route a code task refused creating `branch` (another task's branch it carries) still has, or
    "": publishing its own ttp/t<id>-... branch under its own name, which own_pr_branch lets it create
    (delivery.own_push unset, draft PRs on). An explicit existing_only, a review or no run has none."""
    task = os.environ.get("TTP_TASK")
    t = _task_row(p, task) if task and own_creates_pr_branch(cfg) else None
    if not t or str(t.get("kind") or "") != "code" or (own := own_name(t)) == branch:
        return ""
    return (f"deliver on this task's own branch instead, which may be created: commit on {own} (`git switch "
            f"-c {own}` if it is not checked out), run `ttp push --own-branch --detach` and open the PR "
            f"from {own}")


def pr_branch_route(branch: str) -> str:
    """How work gets onto another task's existing `branch`: only a task whose spec has a `pr_branch:`
    line naming it publishes a fast-forward there with `ttp push --own`."""
    return (f"hand off blocked naming {branch}: the coordinator then adds a task whose spec has the line "
            f"`pr_branch: {branch}` and whose title has no 'rebase' (that would deliver on a new branch), "
            f"and that task's `ttp push --own` fast-forwards the existing branch (task_update cannot set a "
            f"running task's pr_branch)")


def work_route(p: Project, branch: str) -> str:
    """The route a running task that is neither a code task nor a review, refused publishing `branch`
    the remote lacks, has, or "": it creates no branch, but a task with a `pr_branch:` line can deliver
    onto the one other task's branch its spec names (or any existing branch)."""
    task = os.environ.get("TTP_TASK")
    t = _task_row(p, task) if task else None
    if not t or (kind := str(t.get("kind") or "work")) in ("code", "review"):
        return ""
    named = {m.group(0) for m in NAMED_BRANCH.finditer(str(t.get("spec") or ""))
             if m.group(1) != task and m.group(0) != branch}
    if len(named) == 1:
        onto = named.pop()
        return f"a {kind} task creates no branch. If its work must go onto {onto}, which its spec names, " \
               f"{pr_branch_route(onto)}"
    return f"a {kind} task creates no branch. If its work must go onto another task's existing branch, " \
           f"{pr_branch_route('<branch>')}"


def own_pr_branch(p: Project, cfg: dict, branch: str) -> bool:
    """Whether `branch` is the running code task's own ttp/t<id>-... branch (no notes branch) that it may
    create on the remote with delivery.own_push unset (own_creates_pr_branch): the documented way a
    draft PR's branch is first published. Never another task's branch, never from a review (reviews
    once created the branches of the code tasks they covered this way), and never outside a run."""
    task = os.environ.get("TTP_TASK")
    m = OWN_BRANCH.fullmatch(branch)
    if not (task and m and m.group(1) == task and own_creates_pr_branch(cfg)) or is_notes_branch(p, branch):
        return False
    return str((_task_row(p, task) or {}).get("kind") or "") == "code"


def resolve(p: Project, repo: Path, own: bool = False) -> tuple[str, str, bool]:
    """(remote, branch, own) for `ttp push`, each publish as with --own passed through own_gate."""
    remote, branch, own = _resolve(p, repo, own)
    if own:
        own_gate(p, repo, remote, branch)
    return remote, branch, own


def _resolve(p: Project, repo: Path, own: bool = False) -> tuple[str, str, bool]:
    """(remote, branch, own) for `ttp push`. `--own` is own_target. Otherwise the target is
    `delivery.push_branch`, but inside a run a worktree on another task's ttp/t<id>-... branch is
    refused (it would land that task's work: _may_land), a notes branch (is_notes_branch) is published
    under its own name as with --own, and with no push_branch set this task's
    own branch, once on the remote, is the default target (published as with --own). A branch based
    on a branch outside the flow work lands by (foreign_base) is never rebased onto the push target:
    once on the remote as a fast-forward it is pushed there as with --own, else it is refused. Inside a
    run, the task's own ttp/t<id>-... branch lands on the push branch only when its spec asks it to
    land (_told_to_land) or `--land` (LAND_ENV) says so; else it is refused, naming `--own`. A code
    task's run never lands on the push branch while delivery.code_tasks_may_push is off (code_run_landing)."""
    if own:
        return (*own_target(p, repo), True)
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    if m and task and not _may_land(p, task, m.group(1), branch):
        raise ValueError(f"this worktree has task #{m.group(1)}'s branch {branch} checked out, not "
                         f"task #{task}'s own (ttp/t{task}-...): ttp push lands only this task's work. "
                         f"Run it in this task's own worktree")
    if is_notes_branch(p, branch):
        remote, own_branch = own_target(p, repo)
        print(f"ttp push: {branch} is a notes branch; publishing it under its own name as with --own, "
              f"never rebased onto the push branch or a pr_branch", file=sys.stderr)
        return remote, own_branch, True
    d = p.config().get("delivery") or {}
    if branch and (why := pr_branch_guard(p, branch, task, d)):
        raise ValueError(why)
    if not str(d.get("push_branch") or "").strip() and m and m.group(1) == task:
        remote, own_branch = own_target(p, repo)
        if _on_remote(repo, remote, own_branch):
            print(f"ttp push: no delivery.push_branch; {remote}/{branch} is this task's own branch, "
                  f"publishing it as with --own", file=sys.stderr)
            return remote, own_branch, True
    remote, push_to = target(p, repo)
    if branch and (other := foreign_base(p, repo, remote, push_to, branch)):
        base, carried = other
        upstream = f"{remote}/{push_to}"
        why = (f"{branch} is based on {base}, not on {upstream} or the base work starts from: rebasing it "
               f"onto {upstream} would carry {carried} commit(s) it has over that base with it")
        if published(repo, remote, push_to) == branch:
            try:
                ok = own_target(p, repo) == (remote, branch)
            except ValueError:
                ok = False
            if ok:
                print(f"ttp push: {why}; {remote}/{branch} is already on {remote}, so it is pushed there "
                      f"as a fast-forward, with no rebase (as with --own)", file=sys.stderr)
                return remote, branch, True
        raise ValueError(f"{why}. Publish it on its own branch with `ttp push --own`, or rebase only "
                         f"its own commits (`git rebase --onto {upstream} {base}`) and rerun")
    if task and (why := code_run_landing(p, task, f"{remote}/{push_to}", d)):
        raise ValueError(why)
    if m and task and m.group(1) == task:
        if os.environ.get(LAND_ENV):
            say = (f"ttp push: --land: task #{task} lands its own branch {branch} on {remote}/{push_to} "
                   f"(explicit override of the landing guard)")
            print(say, file=sys.stderr)
            run_note(say)
        elif not _told_to_land(p, task, d):
            raise ValueError(f"task #{task}'s spec does not ask it to land on {remote}/{push_to}, and a plain "
                             f"`ttp push` from its own branch {branch} would. Publish {branch} under its own name "
                             f"with `ttp push --own --detach`; only if the spec does ask to land on the push "
                             f"branch, say so with `ttp push --land --detach`")
        elif why := untested_landing(p, repo, task, d):
            raise ValueError(why)
    return remote, push_to, False


def code_run_landing(p: Project, task: str, upstream: str, d: dict) -> str:
    """Why task `task`'s run must not push to the push branch `upstream`, or "": a code task lands
    there itself only with delivery.code_tasks_may_push on. Off, only a review (or the push queue it
    hands the change to) pushes there, whatever the spec says, and `--land` does not override it;
    the run's `git` wrapper (gitguard.py) refuses a raw `git push` there the same way."""
    try:
        t = p.db.task(int(task))
    except (ValueError, TypeError):
        return ""
    if not t or t.get("kind") != "code" or code_tasks_may_push(p.config()):
        return ""
    own = (" To publish the branch under its own name, run `ttp push --own --detach`."
           if own_push({"delivery": d}) != "off" else "")
    return (f"task #{task} is a code task and delivery.code_tasks_may_push is off: code tasks never push to "
            f"{upstream}, whatever the spec says, and `--land` does not change that. Commit, run `ttp checks` "
            f"and hand off; the review lands the change.{own}")


def untested_landing(p: Project, repo: Path, task: str, d: dict) -> str:
    """Why a code task's worker must not land its own branch on the push branch yet, or "": plain
    `ttp push` is delivery, so from a code task's run it needs `ttp checks` passed on exactly HEAD
    in this worktree (this run's or an earlier run's of the task, prguard.task_pass). A review, the
    push queue (no task) and other kinds are not gated here; `--land` (LAND_ENV) overrides it."""
    try:
        t = p.db.task(int(task))
    except (ValueError, TypeError):
        return ""
    if not t or t.get("kind") != "code":
        return ""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if prguard.task_pass(p, task, head, str(repo)):
        return ""
    own = (f" To publish the branch under its own name instead, run `ttp push --own --detach`."
           if own_push({"delivery": d}) != "off" else "")
    return (f"task #{task} is a code task and `ttp checks` has not passed on this commit ({head[:10]}) in "
            f"this worktree with no uncommitted changes: plain `ttp push` delivers to the push branch, so "
            f"it lands only tested work. Run `ttp checks` (committed), then rerun.{own} An explicit "
            f"`ttp push --land --detach` overrides this and is logged")


def _told_to_land(p: Project, task: str, d: dict) -> bool:
    """Whether task `task` may land its own branch on the push branch with a plain `ttp push`: its
    spec or title has a landing step (settinghold.lands; a `no_push:` line never does), its pr_branch
    label names the push branch, or it is a review. Not gated: a task the project has no row of
    (nothing to read), or own pushes off (delivery.own_push: the push branch is the only one)."""
    if own_push({"delivery": d}) == "off":
        return True
    try:
        t = p.db.task(int(task))
    except (ValueError, TypeError):
        return True
    if not t or t.get("kind") == "review":
        return True
    if pr_branch_of(p, t) and not labelled_elsewhere(p, t, d):
        return True
    return settinghold.lands(t, str(d.get("push_branch") or ""))


def _commit_of(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()


def _carried(repo: Path, ref: str) -> int:
    """How many commits HEAD has that `ref` does not: what a rebase onto `ref` would carry."""
    out = _git(repo, "rev-list", "--count", "HEAD", f"^{ref}")
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else -1


def base_candidates(p: Project, repo: Path, remote: str, push_to: str, branch: str) -> tuple[list[str], list[str]]:
    """(configured, other) refs that may be the checked-out `branch`'s base. Configured: the push
    target, `delivery.base_ref` (or the base worktrees start from without it), the flow work lands
    by. Other: the branch's upstream and the remote's default branch (else its main or master).
    Never the branch's own copy on the remote, nor another task's ttp/t<id>-... branch: that is
    work landing by the same flow, not a base."""
    configured = [f"{remote}/{push_to}"]
    d = p.config().get("delivery") or {}
    for ref in (str(d.get("base_ref") or "").strip(), worktree.named_base(p)):
        if ref:
            name = ref_name(repo, ref)
            configured.append(name if _commit_of(repo, name) else ref)
    other = [_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip()]
    default = _git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD").stdout.strip()
    other += [default] if default else [f"{remote}/main", f"{remote}/master"]
    mine = {branch, f"{remote}/{branch}"}

    def keep(ref: str) -> bool:
        tail = ref.partition("/")[2] if ref.startswith(f"{remote}/") else ref
        return bool(ref) and ref not in mine and not OWN_BRANCH.fullmatch(tail) and bool(_commit_of(repo, ref))
    configured = list(dict.fromkeys(r for r in configured if r and r not in mine and _commit_of(repo, r)))
    return configured, [r for r in dict.fromkeys(other) if keep(r) and r not in configured]


def real_base(p: Project, repo: Path, remote: str, push_to: str, branch: str) -> tuple[str, int]:
    """(ref, carried) of the checked-out branch's nearest base among base_candidates: the one HEAD
    has the fewest commits over (_carried); on a tie a configured one, the push target first."""
    configured, other = base_candidates(p, repo, remote, push_to, branch)
    best = ("", -1)
    for ref in configured + other:
        n = _carried(repo, ref)
        if n >= 0 and (best[1] < 0 or n < best[1]):
            best = (ref, n)
    return best


def foreign_base(p: Project, repo: Path, remote: str, push_to: str, branch: str) -> tuple[str, int] | None:
    """(base, carried) when the checked-out `branch` is based on a branch outside the flow work lands
    by (real_base is no configured candidate): rebasing it onto the push target would carry that
    base's commits too, and conflict or land them unreviewed. None otherwise: when HEAD already
    descends from the push target's tip (on the flow, whatever else it merged in), or when the push
    target cannot be fetched within REACH_FETCH_S (the push itself then fetches it, or refuses):
    `ttp push --detach` runs this in the foreground, so a stalled fetch never holds up its start."""
    if branch == push_to or not (tip := _fetch_bounded(repo, remote, push_to)):
        return None
    if _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
        return None
    base, carried = real_base(p, repo, remote, push_to, branch)
    if not base or base in base_candidates(p, repo, remote, push_to, branch)[0]:
        return None
    return base, carried


def behind(repo: Path, remote: str, branch: str) -> str:
    """Why pushing HEAD to remote/branch would not be a fast-forward, or "" when the branch is new
    there or its tip is an ancestor of HEAD. Fails closed when the remote cannot be read. Read where
    the push goes (push_side)."""
    side = push_side(repo, remote)
    ls = _git(repo, "ls-remote", "--heads", side, f"refs/heads/{branch}")
    if ls.returncode != 0:
        return f"cannot reach {remote}: {ls.stderr.strip()}"
    tip = next((ln.split("\t")[0] for ln in ls.stdout.splitlines()
                if ln.endswith(f"\trefs/heads/{branch}")), "")
    if not tip:
        return ""
    tip = _fetch(repo, side, branch)   # the latest tip, should it have moved since
    if not tip:
        return f"cannot fetch {remote}/{branch} to compare with HEAD"
    if _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode != 0:
        return (f"{remote}/{branch} ({tip[:10]}) is not an ancestor of HEAD: --own only fast-forwards "
                "a branch that is not this task's own ttp/t<id>-... one, never rewrites it (a rebase delivers "
                "on its own new branch unless delivery.rebase_replace is lease)")
    return ""


def delivered_pr(p: Project, task: dict, head: str) -> str | None:
    """The URL of the task's PR when it already carries `head` (a full hash), else None. The PR's
    head as pr-watch last read it decides, and a PR it read as no longer open does not count; before
    pr-watch read it, a `ttp push --own` of this task that pushed exactly `head` does. A review of
    such a head is review only: the work is delivered, so a pass publishes nothing more."""
    url = str(task.get("pr_url") or "").strip()
    key = prguard.pr_key(url) if url and head else None
    if not key:
        return None
    state = ((p.db.kv("pr_signatures", {}) or {}).get(url) or {}).get("state")
    if state and state != "OPEN":
        return None
    seen = ((p.db.kv(prguard.HEADS_KEY, {}) or {}).get(key) or {}).get("sha")
    if seen:
        return url if seen == head else None
    folder = p.state / DETACHED
    for marker in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        m = _read(marker)
        if (m.get("own") and m.get("status") == "pushed" and m.get("sha") == head
                and str(m.get("task") or "") == str(task.get("id"))):
            return url
    return None


PR_VIEW_TIMEOUT_S = 60


def _pr_view(p: Project, url: str) -> dict | None:
    """gh's view of the PR at `url` (head ref and commit, state, changed files), or None."""
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", "headRefName,headRefOid,state,files"],
                             capture_output=True, text=True, timeout=PR_VIEW_TIMEOUT_S, cwd=p.root,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pr_change(p: Project, url: str) -> dict | None:
    """A change published as a PR, read from the hosting service rather than the project root's git
    (it may live in another repository): {"ref": head branch, "sha": head commit, "changes": lines
    changed per file, as worktree.diff_lines}. None when it cannot be read, is no longer open or
    changes nothing."""
    pr = _pr_view(p, url)
    if not pr or str(pr.get("state") or "OPEN") != "OPEN" or not pr.get("headRefOid"):
        return None
    changes: dict[str, int | None] = {}
    for f in pr.get("files") or []:
        if isinstance(f, dict) and f.get("path"):
            try:
                changes[str(f["path"])] = int(f.get("additions") or 0) + int(f.get("deletions") or 0)
            except (TypeError, ValueError):
                changes[str(f["path"])] = None
    if not changes:
        return None
    return {"ref": str(pr.get("headRefName") or ""), "sha": str(pr["headRefOid"]), "changes": changes}


REVIEW_ONLY_LABEL = "review_only"   # a review whose change must not reach the push branch (kept_off)
LAND_LABEL = "lands_local"   # a review that lands with `ttp land` on a local push branch (no git remote)
# A spec keeps its change off the push branch only with an explicit marker line, never by its prose:
# free text cannot tell a ban from a sentence that reports, quotes or conditions one.
_NO_PUSH_MARKER = re.compile(r"^[ \t]*no_push:[ \t]*(.*?)[ \t]*$", re.M)


def kept_off(task: dict, changes: dict | None, d: dict, p: Project | None = None) -> str:
    """Why a code task's change must not reach `delivery.push_branch`, or "" when it may: every path
    of its diff since the base (`changes`, worktree.diff_lines) matches `delivery.push_exclude_paths`,
    or its own or (with `p`) the nearest task it continues (a fix of a review's findings, a `continue:`
    task) says so: a hand-off that sets `no_push` (true or the reason), or a spec with a line starting
    `no_push:` (the reason follows; false/no/0 does not count). An explicit false (hand-off `no_push`
    false, or `no_push: false`) on the nearest task ends the walk: it may reach the branch. Prose in the spec or hand-off summary
    is never read as a ban. Its review is then review only: no `ttp push` and no push-queue approval."""
    globs = exclude_list(d.get("push_exclude_paths"))
    if changes and globs and all(any(matches([f], g) for g in globs) for f in changes):
        return "every file it changes is one delivery.push_exclude_paths keeps off the push branch"
    seen: set = set()
    first = task
    while task and task.get("id") not in seen:
        seen.add(task.get("id"))
        why = _no_push_of(task, "its" if task is first else f"#{task['id']} (which it continues): its")
        if why is not None:
            return why
        c = continues_id(task) if p else None
        task = p.db.task(c) if c is not None else None
    return ""


def _no_push_of(task: dict, whose: str) -> str | None:
    """kept_off's reason from `task`'s own hand-off or spec marker (`whose` starts it), "" when it
    explicitly says no_push false (ends the walk), or None when it says nothing."""
    result = load_result(task.get("result"))
    flag = result.get("no_push")
    explicit = flag is False or flag == 0 or str(flag).strip().lower() in ("false", "no", "0")
    if flag not in (None, False, "", 0) and not explicit:
        return f"{whose} hand-off says it must not reach the push branch" + (
            f" ({str(flag)[:200]})" if isinstance(flag, str) else "")
    for m in _NO_PUSH_MARKER.finditer(str(task.get("spec") or "")):
        why = m.group(1).strip("`\"' ")
        if why.lower() not in ("false", "no", "0"):
            return f"{whose} spec says it must not reach the push branch" + (f" ({why[:200]})" if why else "")
        explicit = True
    return "" if explicit else None


def pr_branch_of(p: Project, task: dict | None) -> str:
    """The branch a task delivers onto (delivery_source): the one its own records name (its pr_branch,
    or the branch it rebases: `rebases:<branch>`, replaced under a lease), or that of the task it
    continues (a fix of a failed review's findings builds on that task's work), or "".
    A task labelled worktree.NO_PR_BRANCH carries none, whatever the tasks it continues carry."""
    return pr_branch_source(p, task)[1]


def pr_branch_source(p: Project, task: dict | None) -> tuple[dict | None, str]:
    """(the task whose record gives `task` its branch, that branch) as delivery_source finds them."""
    return delivery_source(p, task)[:2]


def delivery_source(p: Project, task: dict | None) -> tuple[dict | None, str, bool]:
    """(the task whose record gives `task` its branch, that branch, whether it is a rebase of it): the
    one resolver of the branch a task delivers onto, for the dispatcher's routing and `ttp push --own`
    alike. `task` itself (worktree.delivery_branch: the latest of an appended `pr_branch:` line, its
    pr_branch label and its `rebases:` label), else the task it continues, and so on; (None, "", False)
    when it carries none (also a cleared record: NO_PR_BRANCH or `pr_branch: ""`). A rebase is read
    so only under delivery.rebase_replace `lease`; under `new_branch` (the default) the task delivers
    onto its own branch (rebase False), never replacing the one it rebases."""
    seen: set[int] = set()
    lease = rebase_replace(p.config()) == "lease"
    while task and task["id"] not in seen:
        seen.add(task["id"])
        found = worktree.delivery_branch(task)
        if found is not None and found[1] and not lease:
            if own := str(task.get("branch") or ""):
                return task, own, False
        elif found is not None:
            return (task, *found) if found[0] else (None, "", False)
        c = continues_id(task)
        task = p.db.task(c) if c is not None else None
    return None, "", False


def own_lease(p: Project, branch: str) -> bool:
    """Whether `ttp push --own` publishes onto `branch` as a rebase: the running task, or a code task
    the running review covers, delivers onto it by a `rebases:` record (delivery_source). Such a push
    replaces the branch only when every commit it has beyond the tip the rebase started from is in
    HEAD, and then under a lease on the tip it read (publish); never as a plain force push, and it
    never creates the branch. Never outside a run, never for `--own-branch`."""
    return own_rebase(p, branch) is not None


def own_rebase(p: Project, branch: str) -> dict | None:
    """The task whose `rebases:` record makes `ttp push --own` publish onto `branch` as a rebase
    (own_lease), or None."""
    task = os.environ.get("TTP_TASK")
    if not task or os.environ.get(OWN_BRANCH_ENV):
        return None
    for r in [_task_row(p, task)] + _reviewed(p, task):
        if r and (found := delivery_source(p, r))[2] and found[1] == branch:
            return found[0]
    return None


def unkept(repo: Path, head: str, tip: str, start: str = "") -> list[str]:
    """The commits of `tip` (the remote branch a rebase replaces) that `head` lacks, by patch
    (`git cherry`): those beyond `start` (the tip the rebase started from, when recorded), else all of
    them. Replacing the branch with `head` would lose each one. ["?"] when git cannot tell."""
    out = _git(repo, "cherry", head, tip, *([start] if start else []))
    if out.returncode != 0:
        return ["?"]
    return [ln.split()[1] for ln in out.stdout.splitlines() if ln.startswith("+ ")]


def never_published(repo: Path, d: dict, branch: str, timeout: float = 30) -> bool:
    """Whether `branch` exists neither as a local branch of `repo` nor on the remote of
    `delivery.push_branch` (`d`; else origin; delivery.own_push_remote when set), so `ttp push --own`
    cannot publish onto it (own_target
    never creates a carried branch). False whenever git cannot tell (not a repo, remote unreachable)."""
    def git(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return gitmemo.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                               timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.SubprocessError):
            return None
    if not worktree.valid_branch(branch):
        return False
    r = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if r is None or r.returncode not in (0, 1) or r.stdout.strip():
        return False
    remotes = git("remote")
    if remotes is None or remotes.returncode != 0:
        return False
    if not remotes.stdout.split():
        return True   # no remote: the local branch is the only place it could be
    ref = str(d.get("push_branch") or "").strip()
    remote = _split_target(ref, remotes.stdout.split())[0] if ref else "origin"
    try:
        remote = own_push_remote(d, repo) or remote
    except ValueError:
        return False
    ls = git("ls-remote", "--heads", push_side(repo, remote), f"refs/heads/{branch}")
    return ls is not None and ls.returncode == 0 and not any(
        ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())


def pr_elsewhere(p: Project, task: dict | None, d: dict) -> str:
    """The branch a task delivers onto (pr_branch_of) when it is not `delivery.push_branch`, else "".
    Its work goes to that PR with `ttp push --own`: a plain push or a push-queue approval would
    rebase it onto the push branch, which may be unrelated to it. "" while own pushes are off
    (delivery.own_push): the push branch is then the only one, and the task lands there."""
    return "" if own_push({"delivery": d}) == "off" else labelled_elsewhere(p, task, d)


def labelled_elsewhere(p: Project, task: dict | None, d: dict) -> str:
    """The branch a task's labels deliver onto (pr_branch_of) when it is not `delivery.push_branch`,
    whatever delivery.own_push says, else ""."""
    onto = pr_branch_of(p, task)
    ref = str(d.get("push_branch") or "").strip()
    if not onto or not ref:
        return ""
    # Its branch part as the push reads it: `team/x` is branch team/x of origin unless team is a remote.
    # Remotes unknown (not a git repo): either reading, as push_branch_problem does.
    r = _git(p.root, "remote")
    tries = [r.stdout.split()] if r.returncode == 0 else [[], [ref.partition("/")[0]]]
    return "" if onto in {_split_target(ref, remotes)[1] for remotes in tries} else onto


REVIEW_TITLE = re.compile(r"\s*Review #(\d+)\b")   # a review's title names the task it covers


def _reviewed(p: Project, task: str | None) -> list[dict]:
    """The code tasks review `task` covers (its dependencies, auto_review: label and the `Review #<id>`
    its title starts with), or []."""
    t = _task_row(p, task)
    if not t or t.get("kind") != "review":
        return []
    ids = [i for i in dependency_ids(t) if i is not None]
    ids += [int(x[12:]) for x in _labels(t) if x.startswith("auto_review:") and x[12:].isdigit()]
    ids += [int(m.group(1))] if (m := REVIEW_TITLE.match(str(t.get("title") or ""))) else []
    return [r for r in (_task_row(p, str(i)) for i in dict.fromkeys(ids)) if r]


def pr_branch_guard(p: Project, branch: str, task: str | None, d: dict) -> str:
    """Why a plain `ttp push` of the checked-out `branch` must not land on the push branch, or "": the
    task whose work it is (the ttp/t<id>-... branch's owner, else the running task or a code task the
    running review covers whose pr_branch is `branch`) delivers onto another branch (pr_elsewhere)."""
    m = OWN_BRANCH.fullmatch(branch)
    rows = [_task_row(p, m.group(1))] if m else [_task_row(p, task)] + _reviewed(p, task)
    for t in rows:
        onto = pr_elsewhere(p, t, d)
        if onto and (m or onto == branch):
            src = pr_branch_source(p, t)[0] or t
            whose = "its own label" if src["id"] == t["id"] else f"the label of #{src['id']}, which it continues"
            return (f"task #{t['id']} delivers onto pr_branch {onto} ({whose}), not {d['push_branch']}: a "
                    f"plain `ttp push` would rebase it onto {d['push_branch']}. Publish it with "
                    f"`ttp push --own --detach`. If it should land on {d['push_branch']} instead, the label "
                    f"is wrong: hand off blocked saying so; the coordinator clears it with task_update "
                    f"pr_branch \"\" on a task not yet started, or a redo: task_add with continues #{t['id']} "
                    f"and pr_branch \"\"")
    return ""


def allow_protected(d: dict | None) -> bool:
    """`delivery.allow_protected_push_branch` (off by default): whether `ttp push` and the push queue
    may publish to the push branch when it is main, master or the remote's default branch. Only the
    user's word turns it on; the checks, the rebase before the push and the no-force rule still apply."""
    v = (d or {}).get("allow_protected_push_branch")
    return v is True or (isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on"))


PROTECTED_HINT = ("only the user's word lets reviewed work go there (delivery.allow_protected_push_branch); "
                  "until then reviews are review only: never fall back to a raw `git push` to that branch")


def refusal(repo: Path, remote: str, branch: str, allow: bool = False) -> str:
    """Why `branch` on `remote` must not be pushed to, or "" when it may. Fails closed when the
    remote cannot be asked for its default branch. `allow` (allow_protected, for the push branch
    only) lets main, master and the remote's default branch through; a detached HEAD never."""
    if branch in PROTECTED and not (allow and branch != "HEAD"):
        return f"refusing to push to {remote}/{branch}" + (f": {PROTECTED_HINT}" if branch != "HEAD" else "")
    ls = _git(repo, "ls-remote", "--symref", remote, "HEAD")
    if ls.returncode != 0:
        return f"cannot reach {remote}: {ls.stderr.strip()}"
    for line in ls.stdout.splitlines():
        if not allow and line.startswith("ref: ") and line[5:].split("\t")[0] == f"refs/heads/{branch}":
            return f"refusing to push to {remote}/{branch}, the remote's default branch: {PROTECTED_HINT}"
    return ""


REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BACKUP_TIMEOUT_S = 300


def backup_problem(d: dict) -> str:
    """Why `delivery.backup_remote` in the delivery settings `d` cannot be used, or "" when it is off
    (unset or empty) or names a remote. It names a git remote, never a branch: main/master, the push
    branch, the base_ref and anything with a slash (a remote/branch) are refused."""
    v = d.get("backup_remote")
    if v is None or v == "":
        return ""
    if not isinstance(v, str):
        return f"delivery.backup_remote: {v!r} is not a git remote's name; nothing is backed up"
    v = v.strip()
    shared = {str(d.get(k) or "").strip() for k in ("push_branch", "base_ref")} - {""}
    if v in PROTECTED or v in shared or v in {s.partition("/")[2] for s in shared}:
        return (f"delivery.backup_remote: {v!r} names a branch (main, master, the push branch or the "
                f"base_ref), not a git remote; nothing is backed up")
    if not REMOTE_NAME.fullmatch(v):
        return f"delivery.backup_remote: {v!r} is not a git remote's name; nothing is backed up"
    return ""


def backup(repo: Path, remote: str, branch: str, timeout_s: float = BACKUP_TIMEOUT_S) -> tuple[str, str]:
    """Push a finished task's own branch (ttp/t<id>-...) to the same name on `remote`, fast-forward
    only: never with force, never another branch. Returns (outcome, detail): "pushed" (head), "not_ff"
    (the remote's copy has commits the local branch lacks; left alone), "refused" (not a task branch
    or no such remote), "gone" (the branch no longer exists) or "failed" (git's error)."""
    if not OWN_BRANCH.fullmatch(branch) or branch in PROTECTED:
        return "refused", f"{branch} is not a task branch (ttp/t<id>-...)"
    if remote not in _git(repo, "remote").stdout.split():
        return "refused", f"there is no git remote named {remote}"
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()
    if not head:
        return "gone", f"branch {branch} no longer exists"
    ref = f"refs/heads/{branch}"
    try:   # no leading + and no --force: git itself refuses anything but a fast-forward
        from . import ghroute
        out = subprocess.run(ghroute.push_argv(["git", "-C", str(repo), "push", "--porcelain", remote, f"{ref}:{ref}"]),
                             text=True, capture_output=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return "failed", f"git push to {remote} timed out after {timeout_s:.0f} s"
    if out.returncode == 0:
        return "pushed", head
    if any(ln.startswith("!") and "[rejected]" in ln for ln in out.stdout.splitlines()):
        return "not_ff", f"{remote}/{branch} has commits {branch} lacks"
    err = (out.stderr.strip() or out.stdout.strip())[-300:]
    return "failed", " ".join(x for x in (err, ghroute.push_hint(err)) if x)


def is_doc(path: str) -> bool:
    """A documentation file: prose by its suffix, or anything under a `docs/` or `doc/` folder."""
    return path.lower().endswith(DOC_SUFFIXES) or any(d in ("docs", "doc") for d in path.split("/")[:-1])


def code_paths(repo: Path, tip: str, head: str = "HEAD") -> list[str]:
    """The files the change at `head` touches since it left `tip` that are not docs. --no-renames
    lists a moved file under both names, so moving code into docs/ still counts as code."""
    diff = _git(repo, "diff", "--name-only", "--no-renames", f"{tip}...{head}")
    if diff.returncode != 0:
        return ["(the diff could not be read)"]
    return [f for f in diff.stdout.splitlines() if f.strip() and not is_doc(f)]


MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")


def _version(repo: Path, rev: str, path: str) -> str:
    """The `version` in the JSON file `path` at `rev`, or "" when it has none or cannot be read."""
    show = _git(repo, "show", f"{rev}:{path}")
    try:
        v = json.loads(show.stdout).get("version") if show.returncode == 0 else ""
    except (ValueError, AttributeError):
        return ""
    return str(v or "")


def pushed_version(p: Project, repo: Path, rev: str) -> str | None:
    """The version `delivery.version_bump` reads at `rev` (its first file), or None when it is not
    set, malformed or has no version there (TTP_PUSHED_VERSION is then empty)."""
    try:
        cfg = bump_of((p.config().get("delivery") or {}).get("version_bump"))
    except ValueError:
        return None
    if not cfg:
        return None
    show = _git(repo, "show", f"{rev}:{cfg['files'][0]}")
    m = VERSION_RE.search(show.stdout) if show.returncode == 0 else None
    return ".".join(m.group(2, 3, 4)) if m else None


def version_guard(d: dict) -> bool:
    """Whether `delivery.plugin_version_guard` is on (only `true`; off by default)."""
    return (d or {}).get("plugin_version_guard") is True


def stale_versions(repo: Path, tip: str) -> list[str]:
    """Plugins under `plugins/<name>/` whose content HEAD changes since `tip` while a manifest
    keeps tip's version, as "plugins/<name> <version>". Used only with `delivery.plugin_version_guard`
    on, for a repository of plugins whose installs upgrade by version. Two changes that each bumped to the same
    version rebase onto each other cleanly; the second must take the next version, or installs
    that only upgrade to a strictly newer one skip it."""
    diff = _git(repo, "diff", "--name-only", "--no-renames", tip, "HEAD")
    names = sorted({"/".join(f.split("/")[:2]) for f in diff.stdout.splitlines()
                    if f.startswith("plugins/") and f.count("/") >= 2})
    out = []
    for d in names:
        for m in MANIFESTS:
            v = _version(repo, tip, f"{d}/{m}")
            if v and v == _version(repo, "HEAD", f"{d}/{m}"):
                out.append(f"{d} {v}")
                break
    return out


def rounds_of(v: Any) -> int:
    """`delivery.push_rounds` as an integer of at least 1; ValueError when it is not an integer."""
    if v is None or v == "":
        return DEFAULT_ROUNDS
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}")
    try:
        return max(1, int(v))
    except ValueError:
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}") from None


def wait_of(v: Any) -> float:
    """`delivery.push_wait_s` as seconds of at least 0; ValueError when it is not a number."""
    if v is None or v == "":
        return float(DEFAULT_WAIT_S)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"delivery.push_wait_s must be a number of seconds, not {v!r}")
    try:
        return max(0.0, float(v))
    except ValueError:
        raise ValueError(f"delivery.push_wait_s must be a number of seconds, not {v!r}") from None


def check_limit_of(v: Any, last_s: float | None) -> float | None:
    """`delivery.push_checks_timeout_s` as the seconds one push check may run, None for no limit (0).
    Unset: CHECK_LIMIT_FACTOR times the last measured check run, at least CHECK_LIMIT_FLOOR_S.
    ValueError when it is not a number."""
    if v is None or v == "":
        return max(float(CHECK_LIMIT_FLOOR_S), CHECK_LIMIT_FACTOR * (last_s or 0.0))
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"delivery.push_checks_timeout_s must be a number of seconds, not {v!r}")
    try:
        f = float(v)
    except ValueError:
        raise ValueError(f"delivery.push_checks_timeout_s must be a number of seconds, not {v!r}") from None
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"delivery.push_checks_timeout_s must be 0 or more seconds, not {v!r}")
    return f or None


def pool_wait_of(v: Any) -> float | None:
    """`delivery.push_pool_wait_s` as the seconds one push check may wait for test-pool slots in all before
    it is ended (_timed_wait), None for no bound (0). Unset: POOL_WAIT_S. ValueError when it is not a number."""
    if v is None or v == "":
        return float(POOL_WAIT_S)
    try:
        f = float(v) if not isinstance(v, bool) and isinstance(v, (int, float, str)) else float("nan")
    except ValueError:
        f = float("nan")
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"delivery.push_pool_wait_s must be 0 or more seconds, not {v!r}")
    return f or None


def last_check_s(p: Project) -> float | None:
    """Seconds the project's last full, passing check run took, or None when none was measured."""
    try:
        v = json.loads((p.state / TIMINGS).read_text()).get("last_s")
    except (OSError, ValueError, AttributeError):
        return None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return float(v) if ok else None


def record_check_s(p: Project, seconds: float) -> None:
    """Runs between passing checks and the push, so a failed write is logged and never stops it."""
    try:
        write_json(p.state / TIMINGS, {"last_s": round(seconds, 1), "at": time.time()})
        gatetime.record(p.state, seconds)
    except OSError as e:
        print(f"ttp push: could not record the check time in {p.state / TIMINGS}: {e}", file=sys.stderr)


def default_wait(last_s: float | None) -> float:
    """The lock wait when `delivery.push_wait_s` is unset: long enough for the holder to run its
    checks twice (it starts over when the branch moved), never under DEFAULT_WAIT_S and never over
    MAX_DEFAULT_WAIT_S."""
    if last_s is None:
        return float(DEFAULT_WAIT_S)
    return min(float(MAX_DEFAULT_WAIT_S), max(float(DEFAULT_WAIT_S), 2 * last_s + WAIT_MARGIN_S))


def bump_of(v: Any) -> dict | None:
    """`delivery.version_bump` as {"files", "changeset_dir", "package", "paths"}, or None when unset;
    ValueError when it is malformed. `files` are repo-relative files holding the version; `paths`
    (default: the folder the files share) are where a change counts as one that needs a bump."""
    if v is None or v == "" or v == {}:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise ValueError(f"delivery.version_bump must be an object with `files`, not {v!r}") from None
    if not isinstance(v, dict):
        raise ValueError(f"delivery.version_bump must be an object with `files`, not {v!r}")
    unknown = set(v) - {"files", "changeset_dir", "changeset_file", "package", "paths"}
    if unknown:
        raise ValueError(f"delivery.version_bump: unknown keys {', '.join(sorted(unknown))}")

    def rel_list(key: str) -> list[str]:
        items = v.get(key) or []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
            raise ValueError(f"delivery.version_bump.{key} must be a list of repo-relative paths")
        out = [x.strip().strip("/") for x in items]
        if any(x.startswith("..") or x.startswith("~") or not x for x in out) or any(x.startswith("/") for x in items):
            raise ValueError(f"delivery.version_bump.{key} must be a list of repo-relative paths")
        return out
    files = rel_list("files")
    if not files:
        raise ValueError("delivery.version_bump.files must list the files that hold the version")
    paths = rel_list("paths") or [os.path.commonpath(files) if len(files) > 1 else os.path.dirname(files[0])]
    for key in ("changeset_dir", "changeset_file", "package"):
        if v.get(key) is not None and not isinstance(v[key], str):
            raise ValueError(f"delivery.version_bump.{key} must be a string")
    csfile = (v.get("changeset_file") or "").strip().strip("/")
    if csfile and (csfile.startswith("..") or csfile.startswith("~") or str(v.get("changeset_file")).strip().startswith("/")
                   or not csfile.endswith(".md")):
        raise ValueError("delivery.version_bump.changeset_file must be a repo-relative .md path")
    return {"files": files, "paths": paths, "package": (v.get("package") or "").strip(),
            "changeset_file": csfile, "changeset_dir": (v.get("changeset_dir") or "").strip().strip("/")}


def bump_problems(v: Any) -> list[str]:
    try:
        bump_of(v)
    except ValueError as e:
        return [str(e)]
    return []


def _under(path: str, dirs: list[str]) -> bool:
    return any(not d or path == d or path.startswith(d + "/") for d in dirs)


def _drop_own_bump(repo: Path) -> None:
    """Drop the bump commits an earlier round or run of ttp push left on top of HEAD; the next
    rebase lands on a tip that may have taken that version, so the bump is made again after it."""
    while re.search(rf"^{BUMP_TRAILER}: ", _git(repo, "log", "-1", "--format=%B").stdout, re.M):
        if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD~1").returncode != 0 \
                or _git(repo, "reset", "-q", "--hard", "HEAD~1").returncode != 0:
            return


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "change"


def needs_bump(repo: Path, tip: str, cfg: dict, head: str = "HEAD") -> bool:
    """Whether `head` changes something under `cfg["paths"]` since `tip`, other than the version
    files and changesets themselves."""
    files, csdir = cfg["files"], cfg["changeset_dir"]
    changed = _git(repo, "diff", "--name-only", "--no-renames", tip, head).stdout.split()
    return any(_under(f, cfg["paths"]) and f not in files and not (csdir and _under(f, [csdir]))
               for f in changed)


def next_version(repo: Path, tip: str, files: list[str]) -> str | None:
    """One patch version above the highest version `files` hold at `tip`; None when none holds one
    there (the files are new: their version is the change's own)."""
    at_tip = []
    for f in files:
        show = _git(repo, "show", f"{tip}:{f}")
        m = VERSION_RE.search(show.stdout) if show.returncode == 0 else None
        if m:
            at_tip.append(tuple(int(x) for x in m.group(2, 3, 4)))
    if not at_tip:
        return None
    major, minor, patch = max(at_tip)
    return f"{major}.{minor}.{patch + 1}"


def set_version(repo: Path, files: list[str], new: str, package: str = "") -> str:
    """Write version `new` over the first version each of `files` holds in the work tree, and
    return the package name (`package`, else the first JSON file's `name`). ValueError, writing
    nothing, when a file holds no version or a JSON file's first version is not its `version`."""
    texts = {}
    for f in files:            # all checked before any is written, so a refusal leaves the tree clean
        path = repo / f
        try:
            text = path.read_text()
        except OSError:
            text = ""
        if not VERSION_RE.search(text):
            raise ValueError(f"delivery.version_bump: {f} holds no version to bump")
        text = VERSION_RE.sub(lambda m: f"{m.group(1)}{new}{m.group(5)}", text, count=1)
        if f.endswith(".json"):
            try:
                doc = json.loads(text)
                ok = doc.get("version") == new
            except (ValueError, AttributeError):
                doc, ok = {}, False
            if not ok:
                raise ValueError(f"delivery.version_bump: the first version in {f} is not its top-level `version`")
            package = package or str(doc.get("name") or "")
        texts[path] = text
    for path, text in texts.items():
        durable_write(path, text)
    return package


def release_changeset(repo: Path, csfile: str, package: str, lines: list[str]) -> None:
    """Add `lines` as `- ` bullets to the one release changeset `csfile` (delivery.version_bump.
    changeset_file), skipping those it already holds, or create it; then stage it."""
    cs = repo / csfile
    cs.parent.mkdir(parents=True, exist_ok=True)
    lines = list(dict.fromkeys(lines))
    if cs.is_file():
        text = cs.read_text().rstrip("\n")
        have = {ln.strip() for ln in text.splitlines()}
        durable_write(cs, text + "".join(f"\n- {s}" for s in lines if f"- {s}" not in have) + "\n")
    else:
        durable_write(cs, f'---\n"{package}": patch\n---\n\n`{package}`:\n\n'
                      + "\n".join(f"- {s}" for s in lines) + "\n")
    _git(repo, "add", "--", csfile)


def bump(repo: Path, tip: str, cfg: dict, say: Callable[[str], None]) -> int:
    """After a rebase onto `tip`: when the change touches `cfg["paths"]`, set every file in
    `cfg["files"]` one patch version above the highest version they hold at tip, add a changeset
    for the change when it brings none, and commit both as one `<package>: X.Y.Z (<subject>)`
    commit. 0 when done or not needed; REFUSED when a file holds no version to bump."""
    files, csdir = cfg["files"], cfg["changeset_dir"]
    if not needs_bump(repo, tip, cfg):
        return 0
    new = next_version(repo, tip, files)
    if new is None:
        return 0
    try:
        package = set_version(repo, files, new, cfg["package"])
    except ValueError as e:
        say(f"{e}; not pushing")
        return REFUSED
    log = _git(repo, "log", "--no-merges", "--reverse", "--format=%s", f"{tip}..HEAD").stdout.splitlines()
    subjects = [re.sub(rf"^{re.escape(package)}: ", "", s) if package else s for s in log if s.strip()]
    subject = subjects[-1] if subjects else "update"
    title = f"{package}: {new} ({subject})" if package else f"{new} ({subject})"
    added, wrote = [], False
    if csdir:
        added = [f for f in _git(repo, "diff", "--name-only", "--diff-filter=A", tip, "HEAD", "--",
                                 csdir).stdout.split() if f.endswith(".md")]
        csfile = cfg.get("changeset_file") or ""
        if csfile and csfile in _git(repo, "diff", "--name-only", tip, "HEAD", "--", csfile).stdout.split():
            added = [csfile]     # the batch brings its own edit of the release changeset
        if not added and package and csfile:
            release_changeset(repo, csfile, package, subjects)
            wrote = True
        elif not added and package:
            branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            name = _slug(branch.rsplit("/", 1)[-1] if branch and branch != "HEAD" else subject)
            body = (f"`{package}`: {subjects[0]}." if len(subjects) == 1
                    else f"`{package}`:\n\n" + "\n".join(f"- {s}" for s in subjects))
            cs = repo / csdir / f"{_slug(package)}-{name}.md"
            cs.parent.mkdir(parents=True, exist_ok=True)
            if cs.is_file():     # an earlier push of this branch: add what is new to its changeset
                text = cs.read_text().rstrip("\n")
                durable_write(cs, text + "".join(f"\n- {s}" for s in subjects if s not in text) + "\n")
            else:
                durable_write(cs, f'---\n"{package}": patch\n---\n\n{body}\n')
            _git(repo, "add", "--", str(cs.relative_to(repo)))
            wrote = True
    _git(repo, "add", "--", *files)
    if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return 0                 # the change already sits one version above the tip, with its changeset
    if _git(repo, "commit", "-q", "-m", f"{title}\n\n{BUMP_TRAILER}: {new}").returncode != 0:
        say("delivery.version_bump: the bump commit failed; not pushing")
        return REFUSED
    say(f"bumped {package or 'the version'} to {new}" + (" with a changeset" if wrote else ""))
    return 0


def lock_name(remote: str, branch: str) -> str:
    return f"push:{remote}/{branch}"


def lock_resource(remote: str, branch: str) -> str:
    """The lock's resource name; the branch is quoted so its slashes stay in one name."""
    return "push:" + quote(f"{remote}/{branch}", safe="")


def lock_paths(p: Project, remote: str, branch: str) -> list[Path]:
    """The lock file of pushes to remote/branch."""
    return locks.slot_paths(p.state / "locks", lock_resource(remote, branch), 1)


def free_probe(p: Project) -> str:
    """A `retry_when` that exits 0 once the push lock is free. The harness runs it in the project
    root, maybe without the harness on its PATH, so it names the project's own `ttp`."""
    ttp = p.harness / "bin" / "ttp"
    return f"{shlex.quote(str(ttp)) if ttp.is_file() else 'ttp'} push --free"


STALL_S = 30   # the lock free this long while a waiter ahead does not take it: the next one takes it


def _priority(p: Project) -> int | None:
    try:
        return checkcap.task_priority(p)
    except Exception:   # an unreadable task only loses the ordering, never the wait
        return None


def take(p: Project, remote: str, branch: str, wait_s: float, poll_s: float = 1.0,
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
         who: str | None = None):
    """The held push lock of remote/branch, waiting for it; None when it stayed busy.
    Waiters queue beside the lock (locks.enqueue, with the task's priority): when it frees, the first
    live one in turn takes it, an urgent or user-ordered task's (locks.my_turn) before the rest, each
    in arrival order; a dead waiter's ticket drops out of the queue. `wait_s` counts from when this
    waiter is first in line, so one behind others is never failed for their turns; one that stays
    first in line while the lock stays busy gives up after `wait_s`. A waiter ahead that leaves the
    lock free for STALL_S loses its turn to the next. `wait_s` 0: one try, no queue (the push queue's
    batch). Inside a run the wait is recorded, so it does not count against the run's wall clock.
    `who` labels the holder (default: the task and run, else the pid)."""
    paths = lock_paths(p, remote, branch)
    locks_dir, resource = p.state / "locks", lock_resource(remote, branch)
    who = who or (f"task #{os.environ['TTP_TASK']} (run {os.environ.get('TTP_RUN_ID') or '?'})"
                  if os.environ.get("TTP_TASK") else f"pid {os.getpid()}")
    if wait_s <= 0 or not locks.queued(locks_dir, resource):
        f = locks.try_take(paths, who, "ttp push")
        if f or wait_s <= 0:
            return f
    name = lock_name(remote, branch)
    started = time.time()
    try:
        ticket = locks.enqueue(locks_dir, resource, who, since=started, priority=_priority(p))
    except OSError:   # no queue: plain polling, in no fixed order
        ticket = None
    mine = ticket
    waiting = len(locks.queued(locks_dir, resource)) - 1 if ticket else 0
    say(f"waiting up to {wait_s:.0f} s, counted once first in line, for {name} (held by "
        f"{', '.join(locks.holders_now(paths, p)) or 'another push'}"
        + (f"; {waiting} other push(es) waiting" if waiting > 0 else "") + ")")
    run_dir = Path(os.environ["TTP_RUN_DIR"]) if os.environ.get("TTP_RUN_DIR") else None
    key = f"push:{os.getpid()}:{started}"
    if run_dir:
        locks.record_wait(run_dir, key, started, None)
    f, first_at, free_since = None, None, None
    try:
        while True:
            if ticket is not None and locks.dropped(ticket):
                ticket = None
            first = ticket is None or locks.my_turn(locks_dir, resource, ticket, 1)
            now = time.time()
            if first and first_at is None:
                first_at = now
                if ticket is not None and now - started >= poll_s:
                    say(f"first in line for {name} after {now - started:.0f} s")
            elif not first:
                first_at = None   # a more urgent waiter came in ahead: its turn is not this one's wait
            free_since = (free_since or now) if not first and locks.any_free(paths) else None
            if first or (free_since and now - free_since >= STALL_S):
                f = locks.try_take(paths, who, "ttp push")
            if f or (first_at is not None and now - first_at >= wait_s):
                break
            time.sleep(max(0.0, poll_s if first_at is None else min(poll_s, wait_s - (now - first_at))))
    finally:
        locks.dequeue(mine)
        if run_dir:
            locks.record_wait(run_dir, key, started, time.time())
    if f:
        say(f"got {name} after {time.time() - started:.0f} s")
        return f
    say(f"{name} stayed busy for {wait_s:.0f} s (held by {', '.join(locks.holders_now(paths, p)) or 'another push'}); "
        f"hand the task back as waiting with retry_when: {free_probe(p)}")
    return None


UPGRADE_WAIT_S = 900   # a push step waits at most this long for an automatic upgrade that runs now


class Steps:
    """The steps of a push (a rebase, the checks, the push itself) named in the label of its held branch
    lock: past upgrade.push_hold_min an automatic upgrade waits only for a push inside a step
    (release.push_hold), so it starts between steps, never in one. A step marks itself first and then
    reads the upgrade lock; the upgrade takes that lock first and then reads the marks, so the two never
    both go: a step that finds an upgrade running unmarks itself and waits for it (UPGRADE_WAIT_S at most)."""

    def __init__(self, lock, say: Callable[[str], None] = lambda m: None, poll_s: float = 1.0):
        name = getattr(lock, "name", None)
        self.lock = lock if isinstance(name, str) else None
        self.up = Path(name).parent / f"{UPGRADE_RESOURCE}.0.lock" if self.lock else None
        self.say, self.poll_s = say, poll_s

    def _upgrading(self) -> bool:
        return _lock_taken(self.up)

    def go(self, step: str) -> None:
        if not self.lock:
            return
        waited = 0.0
        while True:
            locks.relabel(self.lock, {"step": step, "step_since": time.time()})
            if not self._upgrading() or waited >= UPGRADE_WAIT_S:
                return
            self.between()
            if not waited:
                self.say(f"an automatic tt-project upgrade is running; the {step} step waits for it")
            while self._upgrading() and waited < UPGRADE_WAIT_S:
                time.sleep(self.poll_s)
                waited += self.poll_s

    def between(self) -> None:
        if self.lock:
            locks.relabel(self.lock, {"step": ""})


def push(repo: Path, remote: str, branch: str, checks: list[str], rounds: int = DEFAULT_ROUNDS,
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
         hold: Callable[[], Any] | None = None, version_bump: dict | None = None,
         timed: Callable[[float], None] | None = None, exclude: list[str] | None = None,
         allow: bool = False, check_run_dir: str | None = None, guard: bool = False,
         landed: dict[str, str] | None = None) -> int:
    """Rebase HEAD onto remote/branch, run `checks` on the result, and push it if the remote did not
    move meanwhile; if it did, start over, at most `rounds` times. With no checks only a change that
    touches nothing but docs goes through, and none that adds or modifies a file `exclude` matches
    (delivery.push_exclude_paths, `excluded`). `hold` takes the push lock once the quick refusals
    passed: it returns the held lock, or None when it stayed busy (BUSY). `version_bump` (bump_of)
    bumps the version after each rebase; `timed` gets the seconds of each full, passing check run.
    `allow` (allow_protected) lets it publish to main, master or the remote's default branch.
    `landed` (landed_heads) maps landed approved heads: a HEAD built on one rebases --onto it."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        say("uncommitted changes; commit first")
        return REFUSED
    why = refusal(repo, remote, branch, allow)
    if why:
        say(why)
        return REFUSED
    lock = hold() if hold else None
    if hold and lock is None:
        return BUSY
    global last_pushed, last_tip, last_carried
    last_pushed = last_tip = None
    last_carried = []
    keep = published(repo, remote, branch)
    if keep:
        # The rebase would rewrite a branch the remote already has, and a later `ttp push --own` of
        # it would be rejected; it runs on a detached HEAD and the branch stays as it was.
        say(f"{keep} is on {remote}; rebasing a detached copy, the branch stays as it is")
        _git(repo, "switch", "--detach", "--quiet")
    try:
        return _rounds(repo, remote, branch, checks, rounds, say, version_bump, timed, keep, exclude, check_run_dir,
                       guard, landed, Steps(lock, say))
    finally:
        if keep:
            if Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip(), "rebase-merge").is_dir():
                _git(repo, "rebase", "--abort")
            if _git(repo, "switch", "--quiet", keep).returncode != 0:
                say(f"could not switch back to {keep}; HEAD is detached, the branch is unchanged")
        if lock is not None:
            lock.close()


last_pushed: str | None = None   # the commit the last push() pushed; HEAD may be back on its branch
last_tip: str | None = None      # the branch's tip that last push() pushed onto
last_carried: list[str] = []     # verdictcarry.line of each check pass the last push() carried across a rebase
last_reach: dict | None = None   # reach() of the last run(): whether it is on the branch it is meant to reach


def published(repo: Path, remote: str, target_branch: str) -> str:
    """The checked-out branch when the remote has it and its tip there is an ancestor of HEAD (it was
    published, e.g. with `ttp push --own`), else "". Not the push target itself: rebasing that onto
    its own tip rewrites nothing."""
    name = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not name or name == target_branch:
        return ""
    ls = _git(repo, "ls-remote", "--heads", push_side(repo, remote), f"refs/heads/{name}")
    tip = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ""
    if tip and _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
        return name
    return ""


def _reused(repo: Path, head: str, todo: list, run_dir: str | None, reuse: Callable[[str, list, list, list], str]) -> str:
    """What `reuse` says of a recorded pass of `todo` on `head`'s tree under the environment they
    would run in here (pushpass.check_envs, and the check_env_parts they hash), or "" (none, or it
    could not be told)."""
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").stdout.strip()
    try:
        parts = pushpass.check_env_parts(repo, todo, check_env("own", "", run_dir)) if tree else []
        return reuse(tree, todo, [pushpass.digest(x) for x in parts], parts) if tree else ""
    except (OSError, ValueError, runnerenv.Unavailable):
        return ""


def publish(repo: Path, remote: str, branch: str, checks: list[str],
            say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
            hold: Callable[[], Any] | None = None, timed: Callable[[float], None] | None = None,
            base: str | None = None, ff_only: bool = False, recorded: dict | None = None,
            check_run_dir: str | None = None, reuse: Callable[[str, list, list], str] | None = None,
            host: str = "", lease: bool = False, start: str = "",
            start_ref: str = "", never_force: bool = False, cfg: dict | None = None,
            p: Project | None = None) -> int:
    """`ttp push --own`: run `checks` on HEAD as it is and push it to remote/branch, the task's own
    branch. No rebase and no version bump, so the pushed commit is the one reviewed; without force,
    so the remote takes only a fast-forward of what it has. Without checks, as for `ttp push`, only
    a docs-only change since `base` (the project's push target) may go. `ff_only` (a branch that is
    not this task's own `ttp/t<id>-...` one) refuses before the checks unless it fast-forwards the remote's.
    A HEAD the remote branch already has as its tip is reported as pushed, with no checks or push.
    When every check is skipped on HEAD, the extra commands `recorded` (recorded_checks) passing on
    exactly HEAD run instead, and must pass again (or be reused); with none, nothing checked it and it is refused,
    unless every check was skipped by its `if_changed` scope (`outside_scope`, compared with `base`).
    `reuse(tree, commands, envs, parts)` names a pass `ttp checks` recorded of exactly those commands on
    HEAD's tree under the same environment (pushpass.check_envs, hashing the check_env_parts `parts`),
    or gives "": with one, nothing runs again.
    `host` (delivery.checks_host) is where those recorded commands run, as the project's checks do;
    ones run here go behind `cfg`'s checks.wrap_cmd (wrap_of), as `ttp checks` ran them.
    `lease` (own_lease: a rebase of `branch`) replaces the remote branch instead of fast-forwarding it.
    It is refused when the remote tip has a commit HEAD lacks (by patch) beyond `start`, the tip the
    rebase started from (worktree.rebase_start, kept at `start_ref`; without one, any such commit):
    work pushed to the branch after the rebase began is never lost; the task rebases again onto the
    new tip. Then it
    goes only with `--force-with-lease` on the tip read before the checks, so a branch that moves
    during them is not overwritten either. One the remote lacks is refused (a rebase never creates
    its branch). `never_force` (delivery.never_force) refuses a lease push outright."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if lease and never_force:
        say(f"not replacing {remote}/{branch} under a lease: delivery.never_force is true, so nothing is "
            f"force-pushed (delivery.rebase_replace lease needs it off); deliver the rebase on a new branch")
        return REFUSED
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        say("uncommitted changes; commit first")
        return REFUSED
    local = remote == LOCAL and remote_less(repo)
    why = local_refusal(repo, branch) if local else \
        refusal(repo, remote, branch) or (behind(repo, remote, branch) if ff_only else "")
    if why:
        say(why)
        return REFUSED
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    read = _existing_tip(repo, remote, branch) if not local else ""
    if not local and head and read == head:
        # Already published (e.g. by the change a review covers): nothing to check or push again.
        say(f"{head[:10]} is already {remote}/{branch}'s tip; nothing to push")
        return 0
    if lease and not local and not read:
        say(f"{remote} has no branch {branch} (or it cannot be read): a rebase replaces an existing branch "
            f"under a lease and never creates one; not pushing")
        return REFUSED
    if lease and not local and (lost := unkept(repo, head, read, start)):
        more = f" and {len(lost) - 5} more" if len(lost) > 5 else ""
        since = f"since the rebase started from {start[:10]}" if start else "(no rebase start is recorded)"
        say(f"{remote}/{branch} has {len(lost)} commit(s) {since} that HEAD lacks: "
            f"{', '.join(c[:10] for c in lost[:5])}{more}; replacing the branch would lose them, so it is "
            f"not pushed. Rebase again onto {read[:10]} ({remote}/{branch}'s tip) so HEAD holds them; if HEAD "
            f"already holds them in another form, record that tip as the start: "
            f"git update-ref {start_ref or worktree.REBASE_START_REF + '<task id>'} {read}")
        return REJECTED
    if not checks and (code := code_paths(repo, base) if base else ["(no push target to compare with)"]):
        more = f" and {len(code) - 3} more" if len(code) > 3 else ""
        say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}{more}): "
            + no_checks_hint(repo))
        return REFUSED
    lock = hold() if hold else None
    if hold and lock is None:
        return BUSY
    Steps(lock, say).go("own push")    # its checks and push, one step: no rebase to pause between
    try:
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        try:
            todo, skipped = applicable(repo, head, checks, say, base or "", candidate=True)
        except ScopeError as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        extras = False
        if checks and not todo:
            todo = extras = with_host(recorded_extras(recorded, head, checks), host)
            if todo:
                try:
                    wrap = wrap_of(cfg, repo, p)
                except ValueError as e:
                    say(f"{e}; not pushing")
                    return CHECKS_FAILED
                todo = extras = with_wrap(todo, wrap, skip=lambda c: checklane.no_device(c, cfg))
                if any(getattr(c, "wrap", None) for c in todo) and (why := wrap_problem(wrap, repo)):
                    say(f"{why}; not pushing")
                    return CHECKS_FAILED
            if not todo and outside_scope(checks, skipped):
                say(f"{head[:10]}: {OUT_OF_SCOPE}")
            elif not todo:
                say(f"{head[:10]}: {NONE_APPLY}; not pushing: {NONE_APPLY_OWN_FIX}")
                return CHECKS_FAILED
        if todo and reuse and (hit := _reused(repo, head, todo, check_run_dir, reuse)):
            say(f"{head[:10]}: reused the pass of {len(todo)} check(s) {hit} on this tree with the same "
                f"commands and environment; not running them again"
                + (" (every project check was skipped; these are the commands after `--`)" if extras else ""))
            todo = []
        elif extras:
            say(f"{head[:10]}: every project check was skipped; running the {len(todo)} command(s) "
                f"`ttp checks` recorded passing on this commit: {'; '.join(todo)}")
        tip = (_local_tip(repo, branch) if local else _existing_tip(repo, remote, branch)) if todo else ""
        if todo and (why := runner_wait(todo, repo, check_env("own", tip, check_run_dir, branch), say)):
            return why
        try:
            # Spill first (delivery.checks_spill_first): a spill host before the own-branch lane here.
            slot, todo = spill_slot(todo, head, say, p)
            slot = slot or run_slot(todo, head, say, priority=own_lane_key(repo, remote, cfg))
        except checkcap.Unavailable as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        with slot:
            started = time.time()
            env = {**check_env("own", tip, check_run_dir, branch), **slot.env()}
            for line in sandbox_lines(todo):
                say(line)
            excused = False
            for cmd in todo:
                cmd, rc = _run_or_here(cmd, repo, env, say, seen := {},
                                       lambda: check_slot(head, say, priority=own_lane_key(repo, remote, cfg)))
                if rc != 0:
                    if rc == CHECKS_FAILED:
                        rc = _gate_here(cmd, repo, env, say, head, base or "", seen, p,
                                        lambda: check_slot(head, say, priority=own_lane_key(repo, remote, cfg)))
                    if rc == 0:
                        excused = True   # its reruns are no check time
                        continue
                    if rc != BUSY:
                        say(f"check failed on {head[:10]}: {cmd}{where(cmd)}; not pushing")
                    return rc
        if todo and timed and not excused:
            timed(time.time() - started)
        if _git(repo, "rev-parse", "HEAD").stdout.strip() != head:
            say(f"HEAD moved off {head[:10]} during the checks; not pushing")
            return REFUSED
        if local:
            return _move_local(repo, branch, head, say)
        if lease and not local:
            # Never a plain force push: only while the remote still has the tip read before the checks.
            if _git(repo, "push", f"--force-with-lease=refs/heads/{branch}:{read}", remote,
                    f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
                say(f"pushed {head[:10]} to {remote}/{branch}, replacing {read[:10]} (a rebase, under a lease)")
                return 0
            say(f"push to {remote}/{branch} was rejected: it no longer has {read[:10]}, the tip the lease was "
                f"taken on (someone pushed to it since); not overwritten")
            return REJECTED
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            say(f"pushed {head[:10]} to {remote}/{branch}")
            return 0
        say(f"push to {remote}/{branch} was rejected (never forced: it must fast-forward what the remote has; "
            f"only a rebase under delivery.rebase_replace lease replaces a branch)")
        return REJECTED
    finally:
        if lock is not None:
            lock.close()


RERERE = ("-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=true")
MAX_REPLAYED = 50   # merge stops a rebase may get past on recorded resolutions


def _learn_merges(repo: Path, merges: list[list[str]]) -> None:
    """Record each merge's conflict resolution with git rerere, as git's rerere-train does: redo the
    merge in a scratch worktree, then show rerere the merge's committed result. The record lives in
    the repository's shared rr-cache, where the rebase finds it."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ttp-push-rerere-") as tmp:
        scratch = Path(tmp) / "wt"
        if _git(repo, "worktree", "add", "-q", "--detach", "--no-checkout", str(scratch), "HEAD").returncode:
            return
        try:
            for commit, first, *others in merges:
                if _git(scratch, "checkout", "-q", "--detach", "-f", first).returncode:
                    continue
                if _git(scratch, *RERERE, "merge", "-q", "--no-commit", "--no-ff", *others).returncode:
                    _git(scratch, *RERERE, "rerere")
                    _git(scratch, "checkout", "-q", commit, "--", ".")
                    _git(scratch, *RERERE, "rerere")
                _git(scratch, "merge", "--abort")
                _git(scratch, "reset", "-q", "--hard")
        finally:
            _git(repo, "worktree", "remove", "--force", str(scratch))
            _git(repo, "worktree", "prune")


def descends(repo: Path, tip: str, head: str) -> bool:
    """Whether `head` already has `tip` in its history, so it lands on it as a fast-forward."""
    return _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0


REPLAYED = "Ttp-Replayed-From"   # batch.REPLAYED: names each commit a squash carries, for ttp landed


def squash_merge(repo: Path, tip: str, head: str = "HEAD") -> str:
    """One commit on `tip` whose tree is the merge of `head` with `tip`, when that merge is clean:
    the landing for a branch with merges whose rebase conflicts only because a commit after a merge
    edits lines the merge resolved. Its body lists the branch's commits, each also as a trailer
    naming it, so ttp landed and the push queue find them landed. `tip` itself when the merge adds
    nothing; "" when it conflicts. Made in a scratch worktree: `repo` is left as it is."""
    import tempfile
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"{head}^{{commit}}").stdout.strip()
    if not head:
        return ""
    with tempfile.TemporaryDirectory(prefix="ttp-push-squash-") as tmp:
        scratch = Path(tmp) / "wt"
        if _git(repo, "worktree", "add", "-q", "--detach", str(scratch), tip).returncode:
            return ""
        try:
            if _git(scratch, "-c", "rerere.enabled=false", "merge", "-q", "--no-commit", "--no-ff",
                    head).returncode:
                return ""
            tree = _git(scratch, "write-tree").stdout.strip()
        finally:
            _git(repo, "worktree", "remove", "--force", str(scratch))
            _git(repo, "worktree", "prune")
    if not tree:
        return ""
    if tree == _git(repo, "rev-parse", f"{tip}^{{tree}}").stdout.strip():
        return tip
    commits = [ln.split(" ", 1) for ln in _git(repo, "log", "--reverse", "--no-merges", "--format=%H %s",
                                               head, f"^{tip}").stdout.splitlines()]
    subject = _git(repo, "log", "-1", "--format=%s", head).stdout.strip()
    if subject.startswith("Merge ") and commits:
        subject = commits[-1][-1]
    body = ("Squashed onto the target: its rebase conflicted where a commit after a merge edits lines "
            "the merge resolved, and its merge with the target is clean. It carries:\n\n"
            + "".join(f"- {c[-1]}\n" for c in commits)
            + "\n" + "".join(f"{REPLAYED}: {c[0]}\n" for c in commits))
    return _git(repo, "commit-tree", tree, "-p", tip, "-m", subject, "-m", body).stdout.strip()


LANDED_HEADS_MAX = 200   # the newest landed push queue rows landed_heads maps


def landed_heads(db) -> dict[str, str]:
    """{approved head: the commit it landed as} of the newest push queue rows that landed
    (LANDED_HEADS_MAX): the landed-task mapping landed_deps reads. A head the same review approved
    on the same branch before that landing (sent back as a conflict or failed checks, then reworked)
    maps to the landing too: a branch built on that older head carries copies the landing replaced.
    A row's own landing wins over a later one. {} when the queue cannot be read."""
    try:
        rows = db.q("SELECT l.id = o.id AS own, o.head, COALESCE(l.landed_sha, l.pushed_sha) AS sha "
                    "FROM (SELECT * FROM push_queue WHERE status IN ('pushed','landed') "
                    "AND COALESCE(landed_sha, pushed_sha) IS NOT NULL ORDER BY id DESC LIMIT ?) l "
                    "JOIN push_queue o ON o.task = l.task AND o.branch = l.branch AND o.id <= l.id "
                    "ORDER BY own DESC, l.id DESC, o.id DESC", (LANDED_HEADS_MAX,))
    except Exception:   # noqa: BLE001 - no mapping only loses the drop, never a push
        return {}
    out: dict[str, str] = {}
    for r in rows:
        head, sha = str(r.get("head") or ""), str(r.get("sha") or "")
        if re.fullmatch(r"[0-9a-f]{40,64}", head) and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            out.setdefault(head, sha)
    return out


def head_branches(db, heads) -> dict[str, str]:
    """{head: the branch its push queue row names} for the `heads`; {} when the queue cannot be read."""
    heads = [h for h in heads if re.fullmatch(r"[0-9a-f]{40,64}", h or "")]
    try:
        return {str(r["head"]): str(r["branch"] or "") for k in range(0, len(heads), 500)
                for r in db.q(f"SELECT head, branch FROM push_queue WHERE head IN "
                              f"({','.join('?' * len(heads[k:k + 500]))})", tuple(heads[k:k + 500]))}
    except Exception:   # noqa: BLE001 - no branch only loses the drop, never a push
        return {}


def dependency_heads(repo: Path, base: str, top: str, onto: str, heads: dict[str, str]) -> dict[str, str]:
    """{approved head: landed commit} of the `heads` (landed_heads) inside base..top whose landing is
    on `onto`: a branch built on another task's approved head before that task landed. Its commits up
    to that head are the dependency's, already on `onto`; a dependency that landed rebased and changed
    (a review fix rewording or reworking a commit) leaves its old copies unmatched by patch or author
    date, so only this mapping drops them."""
    if not heads or not base:
        return {}
    mine = set(_git(repo, "rev-list", f"{base}..{top}").stdout.split())
    return {d: sha for d, sha in heads.items() if d in mine and descends(repo, sha, onto)}


def landed_deps(repo: Path, base: str, top: str, onto: str, heads: dict[str, str]) -> dict[str, str]:
    """{commit: the landed commit carrying it} for the non-merge commits of base..top under a
    dependency_heads head: a replay leaves them out, as a rebase --onto <tip> <that head> would."""
    out: dict[str, str] = {}
    for d, sha in dependency_heads(repo, base, top, onto, heads).items():
        for c in _git(repo, "rev-list", "--no-merges", f"{base}..{d}").stdout.split():
            out.setdefault(c, sha)
    return out


def _dependency(repo: Path, tip: str, heads: dict[str, str], say: Callable[[str], None]) -> str:
    """The landed dependency head (dependency_heads) HEAD's rebase onto `tip` starts after (--onto
    <tip> <head>): the one every other such head is under. "" when there is none, or when several are
    on separate lines (then none covers the others' commits, and the rebase goes as before)."""
    base = _git(repo, "merge-base", tip, "HEAD").stdout.strip()
    deps = dependency_heads(repo, base, "HEAD", tip, heads)
    top = next((d for d in deps if all(o == d or descends(repo, o, d) for o in deps)), "")
    if top:
        n = _git(repo, "rev-list", "--count", "--no-merges", f"{base}..{top}").stdout.strip()
        say(f"HEAD is built on approved head {top[:10]}, which landed as {deps[top][:10]}: rebasing with "
            f"--onto, leaving out its {n} commit(s)")
    return top


def _rebase(repo: Path, tip: str, upstream: str = "") -> bool:
    """Rebase HEAD onto `tip`; with `upstream` (a landed dependency's approved head: _dependency) only
    the commits after it (--onto <tip> <upstream>). A branch with merge commits (a review combining
    several branches) keeps them (--rebase-merges), and the conflicts they resolved are resolved again
    the same way rather than coming back; a conflict nothing resolved still stops it. False on a
    conflict, with the rebase left for the caller to abort. HEAD already on `tip` stays as it is (a
    fast-forward): rebasing it could only rewrite its commits."""
    if descends(repo, tip, "HEAD"):
        return True
    onto = ["--onto", tip, upstream] if upstream else [tip]
    merges = [line.split() for line in _git(repo, "rev-list", "--merges", "--parents", "HEAD", f"^{tip}",
                                            *([f"^{upstream}"] if upstream else [])).stdout.splitlines()]
    if not merges:
        return _git(repo, "rebase", *onto, quiet=False).returncode == 0
    _learn_merges(repo, merges)
    if _git(repo, *RERERE, "rebase", "--rebase-merges", *onto, quiet=False).returncode == 0:
        return True
    editor = {**os.environ, "GIT_EDITOR": "true"}   # keep each recreated merge's own message
    for _ in range(MAX_REPLAYED):
        if not Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip(), "rebase-merge").is_dir() \
                or _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip():
            return False   # no rebase stopped, or a conflict no recorded resolution covers
        if subprocess.run(["git", "-C", str(repo), *RERERE, "rebase", "--continue"], env=editor,
                          text=True, stdout=2).returncode == 0:
            return True
    return False


def _land_squashed(repo: Path, tip: str, upstream: str, say: Callable[[str], None]) -> bool:
    """After a conflicting rebase of a branch with merges (aborted), put HEAD on squash_merge's commit
    when the branch's merge with `tip` is clean. False when the branch has no merges or the merge
    conflicts too: a real conflict, left to the caller."""
    if not _git(repo, "rev-list", "--merges", "-1", "HEAD", f"^{tip}").stdout.strip():
        return False
    squashed = squash_merge(repo, tip)
    if not squashed or _git(repo, "reset", "-q", "--hard", squashed).returncode:
        return False
    say(f"rebase onto {upstream} conflicts where a commit after a merge edits lines the merge resolved, "
        f"but its merge with {upstream} is clean: landing that merge as one commit {squashed[:10]} on {upstream}")
    return True


def leak_step(repo: Path, tip: str, say: Callable[[str], None]) -> str | None:
    """The secret-shape scan of what this push sends (tip..HEAD), run as its own first step, before the
    rebase and the checks: a literal in the outgoing history fails in seconds, not after the smoke run
    when the pre-push hook refuses it. A scan that cannot run is said and left to that hook."""
    started = time.time()
    try:
        why = outscan.history_problem(repo, tip, "HEAD", outscan._project())
    except Exception as e:
        say(f"leak scan of the outgoing commits could not run ({type(e).__name__}: {str(e)[:200]}); "
            "the pre-push hook still scans them")
        return None
    if why:
        outscan.record("ttp push", why)
        return why
    say(f"leak scan: the outgoing commits hold no secret shape ({time.time() - started:.1f}s)")
    return None


def _rounds(repo: Path, remote: str, branch: str, checks: list[str], rounds: int,
            say: Callable[[str], None], version_bump: dict | None = None,
            timed: Callable[[float], None] | None = None, keep: str = "",
            exclude: list[str] | None = None, check_run_dir: str | None = None, guard: bool = False,
            landed: dict[str, str] | None = None, steps: Steps | None = None) -> int:
    global last_pushed
    upstream = f"{remote}/{branch}"
    passes: dict[str, tuple[str, str]] = {}   # check -> (tip, head) it last passed on in this push
    steps = steps or Steps(None)
    for rnd in range(1, rounds + 1):
        steps.go("rebase")
        tip = _fetch(repo, remote, branch)
        if not tip:
            say(f"cannot fetch {upstream}")
            return REFUSED
        if version_bump:
            _drop_own_bump(repo)
        if not checks and (code := code_paths(repo, tip)):
            # Checked before the rebase, so a refused push leaves the branch as it was.
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}{more}): "
                + NO_CHECKS)
            return REFUSED
        if bad := excluded(repo, tip, "HEAD", exclude or []):
            say(excluded_refusal(bad, upstream))
            return REFUSED
        if rnd == 1 and (leak := leak_step(repo, tip, say)):
            say(f"{leak}; not pushing")
            return CHECKS_FAILED
        # A head built on a landed dependency's approved head leaves out its stale copies (--onto);
        # a conflict in its own commits then goes the usual way.
        ok = bool(dep := _dependency(repo, tip, landed or {}, say)) and _rebase(repo, tip, dep)
        if dep and not ok:
            _git(repo, "rebase", "--abort")
            say(f"the rebase --onto {upstream} {dep[:10]} conflicts; rebasing onto {upstream} as before")
        if not ok and not _rebase(repo, tip):
            _git(repo, "rebase", "--abort")
            if not _land_squashed(repo, tip, upstream, say):
                if keep:
                    # Rebasing the branch by hand would rewrite what the remote already has.
                    say(f"rebase onto {upstream} conflicts; {keep} is already on {remote}, so do not rebase it: "
                        f"merge {upstream} into it (`git fetch {remote} {branch} && git merge {upstream}`), "
                        "resolve keeping both sides' intents, commit, then rerun")
                else:
                    say(f"rebase onto {upstream} conflicts; resolve it keeping both sides' intents, then rerun")
                return CONFLICT
        if version_bump and (r := bump(repo, tip, version_bump, say)):
            return r
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if guard and (stale := stale_versions(repo, tip)):
            say(f"{', '.join(stale)}: this change edits the plugin but keeps the version already on "
                f"{upstream}; bump it past that (in every manifest, plus a changeset where the "
                "repository wants one), commit, then rerun; not pushing")
            return CHECKS_FAILED
        try:
            todo, skipped = applicable(repo, head, checks, say, tip)
        except ScopeError as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        if checks and not todo and outside_scope(checks, skipped):
            say(f"{head[:10]}: {OUT_OF_SCOPE}")
        elif checks and not todo:
            say(f"{head[:10]}: {NONE_APPLY}; not pushing: {NONE_APPLY_FIX}")
            return CHECKS_FAILED
        # A pass from an earlier round carries when the rebase changed nothing the check covers.
        run, carried = [], []
        for cmd in todo:
            was = passes.get(str(cmd))
            delta = verdictcarry.carry(repo, cmd, *was, tip, head, exclude or []) if was else None
            if delta is None:
                run.append(cmd)
            else:
                carried.append(f"{verdictcarry.line(was[1], delta)}: {cmd}")
                say(carried[-1])
        steps.between()   # waiting for a checks runner or slot: an upgrade may go now
        if run and (why := runner_wait(run, repo, check_env("target", tip, check_run_dir, branch), say)):
            return why
        try:
            # Spill first (delivery.checks_spill_first): a spill host before the push priority slot here.
            slot, run = spill_slot(run, head, say)
            slot = slot or run_slot(run, head, say, priority=priority_key(repo, remote, branch))
        except checkcap.Unavailable as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        with slot:
            steps.go("checks")
            started = time.time()
            env = {**check_env("target", tip, check_run_dir, branch), **slot.env()}
            for line in sandbox_lines(run):
                say(line)
            excused = set()
            for cmd in run:
                cmd, rc = _run_or_here(cmd, repo, env, say, seen := {},
                                       lambda: check_slot(head, say, priority=priority_key(repo, remote, branch)))
                if rc != 0:
                    if rc == CHECKS_FAILED:
                        rc = _gate_here(cmd, repo, env, say, head, tip, seen, None,
                                        lambda: check_slot(head, say, priority=priority_key(repo, remote, branch)))
                    if rc == 0:
                        excused.add(str(cmd))
                        continue
                    if rc != BUSY:
                        say(f"check failed on {head[:10]}: {cmd}{where(cmd)}; not pushing")
                    return rc
        if run and not carried and timed and not excused:
            timed(time.time() - started)
        # A check the tests gate excused did not pass: no later round carries it.
        passes = {str(cmd): (tip, head) for cmd in todo if str(cmd) not in excused}
        steps.go("push")
        if _fetch(repo, remote, branch) != tip:
            say(f"{upstream} moved during the checks; round {rnd + 1}")
            continue
        # Without --force the remote refuses anything that is not a fast-forward of what we tested on.
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            last_pushed, last_tip = head, tip
            last_carried[:] = carried
            say(f"pushed {head[:10]} to {upstream}")
            return 0
        if _fetch(repo, remote, branch) == tip:
            say(f"push to {upstream} was rejected")
            return REJECTED
        say(f"{upstream} moved before the push; round {rnd + 1}")
    say(f"{upstream} kept moving for {rounds} rounds; not pushing")
    return KEPT_MOVING


def _kill_tree(pid: int) -> None:
    """SIGKILL process `pid` and every descendant /proc shows (Linux; elsewhere only `pid`)."""
    kids: dict[int, list[int]] = {}
    for d in Path("/proc").glob("[0-9]*"):
        try:
            kids.setdefault(int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]),
                            []).append(int(d.name))
        except (OSError, IndexError, ValueError):
            continue
    todo, tree = [pid], []
    while todo:
        tree.append(todo.pop())
        todo += kids.get(tree[-1], [])
    for q in tree:
        try:
            os.kill(q, signal.SIGKILL)
        except OSError:
            pass


# A local check's group leader: it ends the check's group should the push die before the check ends.
CHECK_GROUP = (sys.executable, "-I", str(Path(__file__).with_name("checkgroup.py")))


def _end_group(pgid: int) -> None:
    """SIGKILL what is left of an ended check's process group `pgid` (its pid, started in a session of its
    own): a background process it left behind ends with it, as no namespace reaps it (sandbox.py)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:   # nothing left, or not allowed: nothing of the check's to end
        pass


def _copy_out(stream, collect: workerloss.Collector, keep: deque | None = None) -> None:
    """A check's output into this process's output (the push log), each line also to `collect` (and its
    last lines to `keep`, for the tests gate: _gate)."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode(errors="replace")
            collect.feed(line)
            if keep is not None:
                keep.append(line)
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
    finally:
        stream.close()


def _timed_wait(proc: subprocess.Popen, waits: Path, cmd: Any, say: Callable[[str], None]) -> int:
    """Wait for check `proc`; its exit code. Its time limit (check_limit_s) counts its run time: its wall time
    since it started less its waits for a grant (a `ttp lock`, a checks lane, a host-wide checks slot) and for a
    test-pool slot (`ttp pool-wait`, in the check or any of its shards) that it recorded in `waits` (checkwait),
    each kept once seen (checkwait.Seen: a waiter that died open still waited). Run past the limit, it is ended
    within a poll (CHECK_POLL_S): 124, timed out, with its elapsed time. Still waiting for a pool slot once its
    wall time less its grant waits reached the limit, it is ended: POOL_BUSY, busy, never a failure (a pool that
    stayed full, not the change), so a push never waits on a full pool much past the limit. Still waiting for a
    grant once its limit plus the push's own wait bound
    (check_queue_s) have gone by since it started, not counting its pool waits, it is ended without having run:
    QUEUED, a transient answer, never a failure. Waiting for a test-pool slot past pool_wait_s in all, it is
    ended: POOL_STARVED, saying how long it waited for the pool (no free slot, never a failure: setupfault). While it waits for a pool slot the push log gives the
    seconds so far every POOL_SAY_S, and pool_status (a detached push's marker side file) has them for
    `ttp push --result`; at the end the log gives the seconds it waited in all."""
    started = time.time()
    pool_s, pooling, told = 0.0, False, None
    seen_grants, seen_pool = checkwait.Seen(), checkwait.Seen()   # a wait once counted stays counted
    try:
        while True:
            now = time.time()
            granted, queued = checkwait.grants(waits, started, now)
            granted = seen_grants.read(granted, now - started)
            pool_s, pooling = checkwait.pool(waits, started, now)
            pool_s = seen_pool.read(pool_s, now - started)
            _pool_status(pool_s, pooling, now)
            if pooling and pool_s >= 1 and (told is None or now - told >= POOL_SAY_S):
                told = now
                say(f"check waiting for a test-pool slot: {pool_s:.0f} s so far, not counted as run time: "
                    f"{cmd}{where(cmd)}")
            elif not pooling:
                told = None
            if check_limit_s is None:
                try:
                    return proc.wait(timeout=CHECK_POLL_S)
                except subprocess.TimeoutExpired:
                    continue
            ran = now - started - granted
            run_s = ran - pool_s   # its run time: a wait for a pool slot is the pool's, not the change's
            if pooling and pool_wait_s and pool_s >= pool_wait_s:
                _kill_tree(proc.pid)
                proc.wait()
                say(f"check waited {pool_s:.0f} s for a test-pool slot, past its bound of {pool_wait_s:.0f} s, "
                    f"and was ended: {cmd}{where(cmd)} (delivery.push_pool_wait_s; unset: "
                    f"{POOL_WAIT_S / 3600:.0f} h; every pool host was busy or held that long)")
                return POOL_STARVED
            if queued and now - started - pool_s >= check_limit_s + check_queue_s:
                _kill_tree(proc.pid)
                proc.wait()
                say(f"check still waiting for a grant (a lock, a checks lane or a checks slot) after "
                    f"{(now - started) / 60:.0f} min and was ended before it ran: {cmd}{where(cmd)}")
                return QUEUED
            if not queued and run_s >= check_limit_s:
                _kill_tree(proc.pid)
                proc.wait()
                say(f"check ran past its limit of {check_limit_s:.0f} s and was ended: timed out after "
                    f"{now - started:.0f} s: {cmd}{where(cmd)} ({run_s:.0f} s counted; {pool_s:.0f} s waiting for a "
                    f"test-pool slot and {granted:.0f} s waiting for a grant not counted; "
                    "delivery.push_checks_timeout_s; unset: 4x the last measured check run, at least 4 h)")
                return 124
            if not queued and pooling and ran >= check_limit_s:
                _kill_tree(proc.pid)
                proc.wait()
                say(f"check still waiting for a test-pool slot at its limit of {check_limit_s:.0f} s and was ended, "
                    f"busy, not failed: {cmd}{where(cmd)} ({pool_s:.0f} s waiting for a test-pool slot, {run_s:.0f} s "
                    "run; every pool host was busy or held)")
                return POOL_BUSY
            try:
                return proc.wait(timeout=min(CHECK_POLL_S, max(check_limit_s - (ran if pooling else run_s), 0.05)))
            except subprocess.TimeoutExpired:
                pass
    finally:
        pool_s = max(pool_s, checkwait.pool(waits, started)[0])
        _pool_status(pool_s, False, time.time())
        if pool_s >= 1:
            say(f"check waited {pool_s:.0f} s for a test-pool slot, not counted as run time: {cmd}{where(cmd)}")


def _pool_status(seconds: float, waiting: bool, now: float) -> None:
    """Write a check's pool-wait seconds so far to pool_status, for `ttp push --result` while the push runs."""
    if pool_status is None or (not waiting and seconds < 1):
        return
    try:
        durable_write(pool_status, json.dumps({"s": round(seconds), "waiting": waiting, "at": now}))
    except OSError:
        pass


def pool_status_words(marker: Path) -> str:
    """The running push's pool wait as `ttp push --result` shows it ('' when its checks waited for none)."""
    try:
        st = json.loads(marker.with_suffix(POOL_STATUS_SUFFIX).read_text())
        s = float(st.get("s") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    if st.get("waiting"):
        return f"; check waiting for a test-pool slot, {s:.0f} s so far"
    return f"; check waited {s:.0f} s for a test-pool slot" if s >= 1 else ""


def _run_one(cmd: Any, repo: Path, env: dict, say: Callable[[str], None], seen: dict | None = None) -> int:
    """Run one push check: 0 when it passes, CHECKS_FAILED when it fails, BUSY when it exits 75 itself (busy: a
    lock, a test pool or a tests gate's rerun gave it no slot, so it judged nothing), when its check host
    could not run it (an environment fault, recorded as runner_wait records a missing runner) or its
    test workers were lost again on its one rerun, each exiting cleanly (workerloss): the host too.
    A pass or failure fills `seen` with the check's own exit code and its last output lines (the tests gate: _gate)."""
    if remote_host(cmd):
        say(f"running {cmd}{where(cmd)}")
    collect = workerloss.Collector()
    kept: deque = deque(maxlen=GATE_LINES)

    def once() -> int:
        collect.reset()
        kept.clear()
        sys.stdout.flush()
        sys.stderr.flush()
        waits = Path(tempfile.mkdtemp(prefix="check-waits-", dir=env.get("TTP_RUN_DIR") or None)).resolve()
        # A landing's check (to the push branch, or `ttp land`) runs under a landing mark: a `ttp lock`
        # in it goes ahead of non-landing waiters (landmark.py). No other check inherits one.
        mark = landmark.hold() if env.get("TTP_PUSH_MODE") == "target" else None
        try:
            argv = testslots.low_priority(check_argv(cmd, repo, env))
            guard = hoststate.Guard(env, argv=argv)   # the real crontab and units, compared after a local check
            # own session: its group is ended once it ends (_end_group), or by checkgroup if this push dies first
            proc = subprocess.Popen([*CHECK_GROUP, str(os.getpid()), "--", *argv], cwd=repo,
                                    env={**{k: v for k, v in env.items() if k != landmark.ENV}, checkwait.ENV: str(waits),
                                         **({landmark.ENV: str(mark.path)} if mark else {}),
                                         # its run-time limit, for a sharded pool run's admission (pooladmit)
                                         **({pooladmit.LIMIT_ENV: f"{time.time():.0f}:{check_limit_s:.0f}"}
                                            if check_limit_s is not None else {})},
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, pass_fds=checkdirs.inherited(),
                                    start_new_session=True)
        except BaseException:
            if mark:
                mark.close()
            raise
        reader = threading.Thread(target=_copy_out, args=(proc.stdout, collect, kept), daemon=True)
        reader.start()
        try:
            rc = _timed_wait(proc, waits, cmd, say)
            _end_group(proc.pid)   # what it left running ends first: nothing writes after the compare
            changed, said = guard.end()
            for line in said:
                say(line)
            return CHECKS_FAILED if changed and rc == 0 else rc
        except BaseException:   # stopped (run_detached's SIGTERM): the check ends with it, as run() did
            proc.kill()
            proc.wait()
            _end_group(proc.pid)   # the command under checkgroup too, before the compare
            guard.stopped(say)   # a change made before the stop: restored and reported, not the next baseline
            raise
        finally:
            _end_group(proc.pid)
            reader.join(timeout=10)   # a child that left the group may keep the pipe open; do not wait on it
            shutil.rmtree(waits, ignore_errors=True)
            if mark:
                mark.close()
    reruns = setup_reruns = 0
    while True:
        rc = scriptwatch.rerun(once, bool, None if remote_host(cmd) else str(cmd), repo, env, say, on_pass=True)
        if rc == 0:
            if seen is not None:
                seen.update(rc=0, text="".join(kept))
            return 0
        if rc in (QUEUED, POOL_BUSY):   # it never ran, or waited for a full pool at its limit: not the change
            say(f"{'queued, not run' if rc == QUEUED else 'busy, not failed'}: {cmd}{where(cmd)}; not pushing. "
                "Hand off `waiting`; retry_when: "
                f"test \"$(date +%s)\" -ge {int(time.time()) + QUEUED_RETRY_S}")
            return BUSY
        # Failed before any test ran (its work dir gone, a bad archive, no free pool slot): the host or its pool,
        # not the change. A setup fault runs again up to setupfault.RERUNS times; a starved pool waits.
        fault = setupfault.starved() if rc == POOL_STARVED else (
            None if rc == BUSY or remote_host(cmd) else setupfault.found(collect.setup, repo))
        if fault:
            if fault["kind"] == "setup" and setup_reruns < setupfault.RERUNS:
                setup_reruns += 1
                say(setupfault.rerun_line(fault, str(cmd), setup_reruns))
                continue
            say(f"{setupfault.line(fault, str(cmd))}; not pushing. Hand off `waiting`; retry_when: "
                f"test \"$(date +%s)\" -ge {int(time.time()) + setupfault.RETRY_S}")
            return BUSY
        # A test worker lost mid-run is the host, not the change: once more (workerloss).
        if remote_host(cmd) or reruns >= workerloss.RERUNS or not workerloss.only(loss := workerloss.lost(collect.text())):
            break
        reruns += 1
        say(workerloss.rerun_line(loss, str(cmd)))
    # A syntax error in a script outside the change, whole again now: one rewritten in place as it ran.
    # Once more after a settle; again so, the host's fault: the head is neither passed nor failed (syntaxfault).
    if not remote_host(cmd) and (torn := syntaxfault.transient(collect.text(), repo, env.get("TTP_PUSH_TIP"))):
        say(syntaxfault.rerun_line(torn, str(cmd)))
        time.sleep(syntaxfault.SETTLE_S)
        rc = scriptwatch.rerun(once, bool, str(cmd), repo, env, say, on_pass=True)
        if rc == 0:
            if seen is not None:
                seen.update(rc=0, text="".join(kept))
            return 0
        if rc in (QUEUED, POOL_BUSY):
            say(f"{'queued, not run' if rc == QUEUED else 'busy, not failed'}: {cmd}{where(cmd)}; not pushing. "
                "Hand off `waiting`; retry_when: "
                f"test \"$(date +%s)\" -ge {int(time.time()) + QUEUED_RETRY_S}")
            return BUSY
        if torn := syntaxfault.transient(collect.text(), repo, env.get("TTP_PUSH_TIP")):
            say(f"{syntaxfault.line(torn, str(cmd))}; not pushing. Hand off `waiting`; retry_when: "
                f"test \"$(date +%s)\" -ge {int(time.time()) + syntaxfault.RETRY_S}")
            return BUSY
    if reruns and workerloss.infra(loss := workerloss.lost(collect.text())):
        say(f"{workerloss.line(loss, str(cmd))}; not pushing. Hand off `waiting`; retry_when: "
            f"test \"$(date +%s)\" -ge {int(time.time()) + WORKER_LOSS_S}")
        return BUSY
    if fault := remote_fault(cmd, rc, repo):
        p = None
        if base := os.environ.get("TTP_PROJECT"):
            p = Project(base)
            p = p if p.exists() else None
        envfault.record(p, fault, os.environ.get("TTP_TASK"))
        say(f"{envfault.line(fault)}; not pushing. Hand off `waiting`; retry_when: {envfault.retry_when(p, fault)}")
        return BUSY
    if rc == BUSY:   # its own "busy" (a lock, a test pool or a gate rerun that admitted it nowhere): not the change
        say(f"busy, not failed: {cmd}{where(cmd)} exited 75 (no free slot, lock or pool); not pushing. Hand off "
            f"`waiting`; retry_when: test \"$(date +%s)\" -ge {int(time.time()) + QUEUED_RETRY_S}")
        return BUSY
    if seen is not None:
        seen.update(rc=rc, text="".join(kept))
    return CHECKS_FAILED


def _gated(cmd: Any) -> bool:
    """Whether the tests gate (_gate) may judge check `cmd`: a local check that is no Slurm batch and runs
    pytest once (pushpass.resumable), or a script whose pytest runs are not known (no `"pytest"` key, no pytest
    in its words): its output tells, and its reruns select by the select plugin in PYTEST_ADDOPTS."""
    if remote_host(cmd) or getattr(cmd, "slurm", None):
        return False
    said = getattr(cmd, "pytest", None)
    return pushpass.resumable(cmd) or (said is None and pytest_runs(cmd) == 0
                                       and not any(w in cmd for w in pushpass._OWN_ENV))


def _gate(cmd: Any, repo: Path, env: dict, say: Callable[[str], None], head: str, target: str, seen: dict,
          p: Project | None = None) -> int:
    """The base-vs-head tests gate for a push check that failed (`seen`: _run_one's exit code and output), as
    the push queue's batch and `ttp checks` judge one (gatejudge.judge): its failing pytest ids
    (gatejudge.judgeable) rerun once at `head`, those failing again once at its merge base with `target` (the
    push target's tip), in a temp checkout on this same host. 0 when none is new on the head (each flaky,
    quarantined, or pre-existing: failing the same way on the base), each named; BUSY when a rerun found no
    slot; else CHECKS_FAILED, the failure as it was. A failing id matching delivery.push_hard_tests (the leak
    scan) is never excused. `p` (else TTP_PROJECT): its settings, base results and quarantine."""
    if p is None and (at := os.environ.get("TTP_PROJECT")):
        p = Project(at)
    if p is None or not p.exists() or not _gated(cmd):
        return CHECKS_FAILED
    cfg = p.config()
    ids = gatejudge.judgeable(seen.get("rc"), seen.get("text") or "", cfg)
    if not ids:
        return CHECKS_FAILED
    s, now, host_ = gatejudge.settings(cfg), time.time(), gatejudge.host()
    r = _git(repo, "merge-base", target, head) if target else None
    mb = r.stdout.strip() if r is not None and r.returncode == 0 else ""
    diff = _git(repo, "diff", "--name-only", f"{mb}..{head}") if mb else None
    changed = diff.stdout.split() if diff is not None and diff.returncode == 0 else None

    def touched(i: str) -> bool:   # the change touches the test's file (unknown: it does)
        f = i.partition("::")[0]
        return changed is None or any(x == f or x.endswith("/" + f) for x in changed)

    say(f"judging the {len(ids)} failing test(s) on {head[:10]}: a rerun there, then on the base "
        f"{mb[:10] or '(none)'} the ones failing again, on this host: {' '.join(ids)}")

    def run(at: str, sel: list[str]) -> tuple[int | None, str]:
        tree = None
        try:
            where_ = repo
            if at == "base":
                tree = cleantree.Checkout(repo, mb, env.get("TTP_RUN_DIR") or None)
                worktree.prepare(p, tree.path)
                where_ = tree.path
            got: dict = {}
            rc = _run_one(cmd, Path(where_), {**env, "PWD": str(where_), **gatejudge.select_env(sel, env)}, say, got)
            return (BUSY if rc == BUSY else got.get("rc")), got.get("text") or ""
        except (cleantree.Error, OSError, subprocess.SubprocessError) as e:
            say(f"the rerun on the {at} could not tell: {e}")
            return None, ""
        finally:
            if tree is not None:
                tree.close()
    v = gatejudge.judge(ids, run, p.state, mb, host_, now, touched, s["busy_tries"], s["busy_wait_s"])
    rx = (cfg.get("delivery") or {}).get("push_hard_tests") or batch.HARD_TESTS
    hard = [i for i in v["pre_existing"] + v["quarantined"] if _hard(rx, i)]
    for k in ("pre_existing", "quarantined"):
        v[k] = [i for i in v[k] if i not in hard]
    v["new"] = v["new"] + hard
    for line in gatejudge.lines(v):
        say(line)
    if hard:
        say(f"never excused (delivery.push_hard_tests): {' '.join(hard)}")
    if v["flaky"] and s["on"]:
        new = gatejudge.quarantine(p.state, v["flaky"], {"head": head, "check": str(cmd), "host": host_,
                                                         "task": os.environ.get("TTP_TASK") or ""}, now, s["days"])
        if new:   # one fix note per newly quarantined test
            # pushq._event's row, written here: pushq imports push (and landed) at import time, and a
            # sibling import inside a function on the push path is refused
            p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), "daemon", gatejudge.EVENT, "normal",
                    gatejudge.note_text(new, head, str(cmd))[:6000], "queued", None))
    if gatejudge.blocks(v):
        say(f"new on {head[:10]}, the failure stands: {' '.join(v['new'])}")
        return CHECKS_FAILED
    if gatejudge.undecided(v):
        return BUSY
    say(f"none of the failing tests is new on {head[:10]}: {cmd}{where(cmd)} does not block the push")
    return 0


def _gate_here(cmd: Any, repo: Path, env: dict, say: Callable[[str], None], head: str, target: str, seen: dict,
               p: Project | None, here: Callable[[], Any]) -> int:
    """_gate for a push check that failed; one spill_slot sent to a spill host is judged as its local
    original (`was`, as the push batch's Batch._judged does), its reruns here in the slot `here()` takes."""
    was = getattr(cmd, "was", None)
    if was is None or not getattr(cmd, "spilled", False):
        return _gate(cmd, repo, env, say, head, target, seen, p)
    try:
        slot = here()
    except checkcap.Unavailable as e:
        say(f"{e}; not pushing")
        return CHECKS_FAILED
    with slot:
        return _gate(was, repo, {**env, **slot.env()}, say, head, target, seen, p)


def _hard(rx: str, test_id: str) -> bool:
    try:
        return bool(re.search(rx, test_id, re.I))
    except (re.error, TypeError):
        return bool(re.search(batch.HARD_TESTS, test_id, re.I))


def run(p: Project, repo: Path, own: bool = False, recorded: dict | None = None, after: bool = True) -> int:
    if os.environ.get("TTP_CHECK_OUTPUT_DIR"):
        return _run(p, repo, own, recorded, after)
    recorded = recorded or recorded_checks(os.environ.get("TTP_RUN_DIR"))
    return in_check_scratch(p, lambda: _run(p, repo, own, recorded, after), repo=repo)


def scratch_need(p: Project, repo: Path) -> int | None:
    """The scratch the push checks of `repo` plan on the RAM disk (shorttmp.need of its HEAD), None to
    keep it under /tmp."""
    try:
        d = p.config().get("delivery") or {}
        return shorttmp.need(repo, "HEAD", check_list(repo_checks(d, Path(repo), p)[0]))
    except (OSError, ValueError, TypeError):
        return None


def in_check_scratch(p: Project, fn: Callable[[], int], what: str = "ttp push", repo: Path | None = None) -> int:
    """fn() with the checks' output going to a scratch directory of the project's own (checkdirs),
    as `ttp push` and `ttp land` run their checks of `repo`; CHECKS_FAILED when it cannot be set up."""
    if os.environ.get("TTP_CHECK_OUTPUT_DIR"):
        return fn()
    output = None
    guard = None
    previous = dict(os.environ)
    try:
        output = checkdirs.new(p.state, need=scratch_need(p, repo) if repo else None)
        guard = checkdirs.take(output)
        if guard is None:
            raise OSError("check scratch is busy")
        env = checkdirs.environment(output, previous)
        if previous.get("TTP_RUN_DIR"):
            # Foreground bookkeeping belongs to the worker; only check subprocesses
            # receive the independent report directory through check_env().
            env["TTP_RUN_DIR"] = previous["TTP_RUN_DIR"]
        os.environ.clear()
        os.environ.update(env)
        os.environ['TTP_CHECKS_TMP_FD'] = str(guard.fileno())
        return fn()
    except OSError as e:
        print(f"{what}: {e}", file=sys.stderr)
        return CHECKS_FAILED
    finally:
        os.environ.clear()
        os.environ.update(previous)
        if guard is not None:
            guard.close()
        if output is not None:
            checkdirs.finish(output)


def _own_pass(p: Project, tree: str, cmds: list, envs: list, parts: list | None = None) -> str:
    """Where and when `ttp checks` recorded passing exactly `cmds` (or more starting with them) on
    `tree` under `envs`, for `ttp push --own` to reuse, or "". With none but one under another
    environment, says which of `parts` (pushpass.check_env_parts) it differs in."""
    hit = cli._recorded_pass(p, tree, cmds, "candidate", envs)
    if not hit and parts and (why := cli._env_miss(p, tree, cmds, parts)):
        print(f"ttp push: not reusing a pass: {why}", file=sys.stderr)
    return f"`ttp checks` recorded in run {hit.get('run') or '?'} at {timefmt.long(float(hit.get('ts') or 0), p)}" \
        if hit else ""


def _run(p: Project, repo: Path, own: bool = False, recorded: dict | None = None, after: bool = True) -> int:
    """`ttp push` for a project: target, checks and rounds come from its `delivery` config. `own`
    publishes the task's own branch instead (own_target, publish), with the checks `recorded` (by
    default those of the run's `ttp checks`) for when every project check is skipped. A push to the
    push branch then runs `delivery.after_push` (run_after); with `after` False it is left in
    `pending_after` for the caller (run_detached runs it once its outcome is written)."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    if local_harness(p, repo):
        print(f"ttp push: {LOCAL_HARNESS}", file=sys.stderr)
        return 0
    try:
        output = checkdirs.writable(os.environ.get("TTP_CHECK_OUTPUT_DIR") or os.environ.get("TTP_RUN_DIR"))
    except OSError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return CHECKS_FAILED
    # none: only a docs-only change may go (_rounds)
    checks = with_host(check_list(repo_checks(d, repo, p)[0]), d.get("checks_host"))
    try:
        remote, branch, own = resolve(p, repo, own)
        rounds = rounds_of(d.get("push_rounds"))
        explicit = d.get("push_wait_s")
        wait_s = default_wait(last_check_s(p)) if explicit in (None, "") else wait_of(explicit)
        limit_s = check_limit_of(d.get("push_checks_timeout_s"), last_check_s(p))
        pool_s = pool_wait_of(d.get("push_pool_wait_s"))
        version_bump = bump_of(d.get("version_bump"))
        if not own and (why := scalar_problem(d.get("push_exclude_paths"))):   # never push unguarded
            raise ValueError(f"delivery.push_exclude_paths: {why}")
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    global last_ff, last_reach, last_after, pending_after, check_limit_s, check_queue_s, pool_wait_s
    last_ff, last_reach, last_after, pending_after = [], None, None, None
    check_limit_s, check_queue_s, pool_wait_s = limit_s, wait_s, pool_s
    global check_lock
    check_lock = runnerenv.lock_of(p.config(), repo)
    try:   # the checks' pinned Python env, made first (runnerenv): no check runs without it
        if checks:
            print(f"ttp push: {runnerenv.describe(runnerenv.ensure(check_lock))}", file=sys.stderr)
    except runnerenv.Unavailable as e:
        tracked = _git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{branch}").stdout.strip()
        return env_wait(p, repo, tracked, "HEAD", e, lambda m: print(f"ttp push: {m}", file=sys.stderr))
    if own:   # delivery.push_exclude_paths guards the push branch only
        say_target(repo, remote, branch)
        # The docs-only test, if_changed scopes and the project's own checks may all compare with the
        # shared branch this work leaves from: always fetch it (cheap), so none sees a stale base.
        base = own_base(p, repo, remote)
        # A rebase replaces the branch it rebases, keeping what it gained since the rebase started.
        rebase = own_rebase(p, branch)
        lease, start = rebase is not None, worktree.rebase_start(repo, rebase["id"]) if rebase else ""
        start_ref = f"{worktree.REBASE_START_REF}{rebase['id']}" if rebase else ""
        rc = publish(repo, remote, branch, checks, hold=lambda: take(p, remote, branch, wait_s),
                     timed=lambda s: record_check_s(p, s), base=base,
                     ff_only=own_ff_only(branch) and not lease, lease=lease, start=start, start_ref=start_ref,
                     recorded=recorded or recorded_checks(os.environ.get("TTP_RUN_DIR")), check_run_dir=output,
                     reuse=lambda tree, cmds, envs, parts: _own_pass(p, tree, cmds, envs, parts), host=d.get("checks_host") or "",
                     never_force=never_force_on(p.config()), cfg=p.config(), p=p)
    else:
        rc = push(repo, remote, branch, checks, rounds, hold=lambda: take(p, remote, branch, wait_s),
                  version_bump=version_bump, timed=lambda s: record_check_s(p, s),
                  exclude=exclude_list(d.get("push_exclude_paths")), allow=allow_protected(d), check_run_dir=output,
                  guard=version_guard(d), landed=landed_heads(p.db))
        if rc == 0 and last_pushed and (also := fast_forward_list(d.get("fast_forward_also"))):
            last_ff = fast_forward(repo, remote, last_pushed, also, branch)
        if rc == 0 and last_pushed and (mirrors := mirror_list(d.get("mirror_remotes"))):
            last_ff = last_ff + mirror_safely(repo, last_pushed, mirrors, f"{remote}/{branch}")
            for line in ff_warnings(last_ff):
                if line.startswith("not mirrored "):
                    run_note(f"ttp push: pushed {last_pushed[:10]} to {remote}/{branch}, but {line}")
        if rc == 0 and last_pushed:   # a push queue batch runs its own (batch.run_batch), never this
            pending_after = (last_pushed, f"{remote}/{branch}", last_tip or "")
    if rc == 0 and remote != LOCAL:   # no remote: nothing to fetch the intended branch from
        sha = (None if own else last_pushed) or _git(repo, "rev-parse", "HEAD").stdout.strip()
        try:
            last_reach = reach(p, repo, f"{remote}/{branch}", sha)
        except Exception as e:      # a fact for the notice; never changes what the push did
            print(f"ttp push: cannot tell whether it is on the branch it is meant to reach: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
        if last_reach:
            print(f"ttp push: {landing(sha, f'{remote}/{branch}', last_reach)}", file=sys.stderr)
    if after and pending_after:
        last_after = run_after(p, repo)
    return rc


def run_after(p: Project, repo: Path) -> dict | None:
    """`delivery.after_push` for the push run() left in `pending_after` (batch.after_ordinary_push):
    its outcome, or None when after_push is not set. Its failure is a warning; the push stands."""
    global pending_after
    if not pending_after:
        return None
    sha, target, tip = pending_after
    pending_after = None
    try:
        out = batch.after_ordinary_push(p, repo, sha, target, tip)
    except Exception as e:   # never changes what the push did
        out = {"status": "failed", "exit": None, "tail": f"{type(e).__name__}: {e}"}
        print(f"ttp push: after_push: {type(e).__name__}: {e}", file=sys.stderr)
    if out and out.get("status") != "ok":
        print(f"ttp push: warning: {after_words(out)}; the push stands", file=sys.stderr)
    return out


check_limit_s: float | None = None   # the time limit of each check of the current run() (check_limit_of)
check_queue_s: float = 0.0   # how much longer than check_limit_s a check may wait for a grant (the run()'s push wait)
pool_wait_s: float | None = None   # how long a check may wait for test-pool slots in all (pool_wait_of)
pool_status: Path | None = None    # a detached push's file for its check's live test-pool wait (_pool_status)
last_ff: list[str] = []   # the fast_forward outcome of the last run(): `ff`/`not ff` and `mirror`/`not mirrored` lines
last_after: dict | None = None   # the after_push outcome of the last run() (batch.after_ordinary_push)
pending_after: tuple[str, str, str] | None = None   # (sha, target, tip) whose after_push run() left to run


def after_words(a: dict) -> str:
    """An after_push outcome in a few words: `after_push ok`, `after_push failed (exit 2) in <cmd>`."""
    how = {"failed": f"failed (exit {a.get('exit')})", "timeout": "timed out"}.get(a.get("status"), a.get("status"))
    return f"after_push {how}" + (f" in `{a['cmd']}`" if a.get("cmd") and a.get("status") != "ok" else "") + (
        f" (receipt {a['receipt']})" if a.get("receipt") and a.get("status") != "ok" else "")


def free(p: Project, repo: Path) -> int:
    """`ttp push --free`: 0 when no push of this project holds a push lock, 1 while one does, 2 when
    there is no target. Read-only and instant, for a waiting task's `retry_when`. It checks every
    push lock of the project: the harness runs the probe in the project root, whose git remotes
    may resolve `delivery.push_branch` differently from the worktree that pushes."""
    try:
        target(p, repo)
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    held = [x for x in sorted((p.state / "locks").glob("push:*.lock")) if not locks.any_free([x])]
    if not held:
        return 0
    print(f"ttp push: a push holds its turn: {', '.join(locks.holders_now(held, p)) or 'another push'}",
          file=sys.stderr)
    return 1


# Detached push ------------------------------------------------------------------------------------
# `ttp push --detach` runs the same push in a process of its own and returns at once, so a worker
# whose tool calls are capped shorter than the checks can hand off `waiting` and read the outcome
# later. The process writes its outcome into a marker under the project's state. It holds a run lock
# (an OS file lock, inherited from the launcher so it is held from the first instant) for its whole
# life: a marker whose run lock is free and that holds no outcome belongs to a push that died (a
# crash, a kill, a reboot), and the probe or the daemon then records it as failed. Its push lock goes
# the same way.
#
# A sandbox that runs each command in a PID namespace of its own (Codex's on Linux) kills every
# process it holds the moment the command ends, a new session or not: the push would die before
# it logged a line. The daemon gives its runs its own namespace in TTP_PIDNS; a launcher that finds
# itself in another one queues the push and the daemon starts it outside the sandbox.
DETACHED = "pushes"        # under the project's state: <id>.json (marker) and <id>.log per push
LOG_TAIL = 20
WORKER_LOSS_S = 900   # a check whose test workers were lost twice (workerloss) runs again after this
KEEP_S = 30 * 86400        # finished markers and logs older than this go when the next push starts
QUEUED_S = 900             # a queued push the daemon has not started by then is recorded as failed
# The environment a queued push takes from the run that queued it; the rest is the daemon's.
QUEUE_ENV = ("PATH", "VIRTUAL_ENV", "TTP_TASK", "TTP_TASK_ID", "TTP_RUN_ID", "TTP_PYTHON", "GIT_CEILING_DIRECTORIES",
             OWN_BRANCH_ENV, LAND_ENV)
# A detached push tries again by itself, model-free, at most `delivery.push_retries` times in all
# (DEFAULT_RETRIES with none set), for two causes ttp can clear: the remote branch moved on during its
# checks so the push was refused as not a fast-forward (NON_FF: fetch, rebase, the checks again at the
# rebased commit, push; never forced), and a host reboot that cut it short (REBOOT: queued again and
# started by the daemon, which fetches, rebases and checks as any push does). The marker lists each
# retry's cause and attempt (`retries`, `attempt`); a rebase conflict or a failed check stops it.
DEFAULT_RETRIES = 2
NON_FF, REBOOT = "non_fast_forward", "reboot"


def _run_lock(marker: Path) -> Path:
    """The run lock of a detached push. Its name starts with `push:`, so an upgrade waits for it
    (release.push_in_flight) and `ttp push --free` counts it, also while it waits for its turn."""
    return marker.parent.parent / "locks" / f"push:run-{marker.stem}.0.lock"


def _own_ttp(p: Project | None = None) -> str:
    """The `ttp` of this runtime, so the probe runs the code that wrote the marker."""
    ttp = Path(__file__).resolve().parents[2] / "bin" / "ttp"
    if ttp.is_file():
        return shlex.quote(str(ttp))
    return free_probe(p).rsplit(" push", 1)[0] if p else "ttp"


def result_probe(marker: Path, p: Project | None = None) -> str:
    """A `retry_when` that exits 0 once the detached push of `marker` finished (or died), 1 before."""
    return f"{_own_ttp(p)} push --result {shlex.quote(str(marker))}"


def _read(marker: Path) -> dict:
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _forget_lock(marker: Path) -> None:
    """Remove a finished push's run lock file; the probe reads a missing one as free."""
    try:
        _run_lock(marker).unlink()
    except OSError:
        pass


def _prune(folder: Path, now: float) -> int:
    """Remove finished markers (and their logs and lock files) older than KEEP_S; how many went."""
    n = 0
    for marker in folder.glob("*.json"):
        try:
            old = now - marker.stat().st_mtime > KEEP_S
        except OSError:
            continue
        if old and _read(marker).get("status") not in ("running", "queued"):
            n += 1
            for f in (marker, marker.with_suffix(".log"), marker.with_suffix(POOL_STATUS_SUFFIX),
                      marker.with_suffix(GIT_PUSH_SUFFIX), _run_lock(marker)):
                try:
                    f.unlink()
                except OSError:
                    pass
    return n


def _tail(path: str | None, n: int = LOG_TAIL) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:]) if path else ""
    except OSError:
        return ""


def pid_ns() -> str | None:
    """This process's PID namespace ("pid:[4026531836]"); None where there is no /proc."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def cgroup() -> str | None:
    """This process's control group(s); None where there is no /proc."""
    try:
        return Path("/proc/self/cgroup").read_text().strip() or None
    except OSError:
        return None


def _boxed() -> bool:
    """Whether this process runs in a PID namespace or a cgroup other than its run's: one that may
    end with the command, and take every process started in it along."""
    run_ns, run_cg = os.environ.get("TTP_PIDNS"), os.environ.get("TTP_CGROUP")
    return bool(run_ns) and pid_ns() not in (None, run_ns) or bool(run_cg) and cgroup() not in (None, run_cg)


def _log_line(log: Path, text: str) -> None:
    """Append a line to a push's log and sync it, so the log of a push that dies is never empty."""
    line = f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n"
    with open_append_or_null(log, "ab", line) as out:
        out.write(line.encode())
        out.flush()
        if out.name != os.devnull:   # refused (open_append_or_null): the null device takes no fsync
            os.fsync(out.fileno())


def retries_of(v: Any) -> int:
    """`delivery.push_retries` (or a marker's `retry_max`) as an integer of at least 0; DEFAULT_RETRIES
    when unset or not an integer, so a typo never turns the retries off nor fails the push."""
    if isinstance(v, bool) or not isinstance(v, (int, str)) or v == "":
        return DEFAULT_RETRIES
    try:
        return max(0, int(v))
    except ValueError:
        return DEFAULT_RETRIES


def _retries(m: dict) -> list[dict]:
    """The retries a marker records; [] when its `retries` is not a list (a hand-edited or damaged
    marker), so neither the daemon's tick nor the probe fails on it."""
    v = m.get("retries")
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def retry_words(m: dict) -> str:
    """The retries of a detached push in a few words ('' with none): `attempt 3 of at most 3, retried
    after non_fast_forward, reboot`."""
    done = _retries(m)
    if not done:
        return ""
    return (f"attempt {len(done) + 1} of at most {retries_of(m.get('retry_max')) + 1}, retried after "
            + ", ".join(str(r.get("cause")) for r in done))


def _note_retry(marker: Path, m: dict, cause: str, detail: str) -> dict:
    """Record a retry of cause `cause` in `m` (the marker as read) and write it."""
    done = _retries(m)
    done.append({"cause": cause, "attempt": len(done) + 2, "at": time.time(), "detail": detail})
    m.update(retries=done, attempt=len(done) + 1, retry_cause=cause)
    write_json(marker, m)
    return m


SCRATCH_HOW = {"reused": "the short temp dir recorded when it was queued",
               "made": "a short temp dir made at start: the recorded one was missing or not this user's",
               "long": "long fallback: no short temp dir could be made"}


def _start(p: Project, marker: Path, m: dict, lock, env: dict, nice: int = 0) -> int:
    """Start the push process of `marker`, holding `lock` (its run lock, taken by the caller), `nice`
    levels below this process, write the marker as running, then give the go. Returns the pid."""
    log = Path(m["log"])
    env = {k: v for k, v in env.items() if k not in ("TTP_RUN_DIR", "TTP_PIDNS", "TTP_CGROUP")}   # the run ends first
    env = machines.mask_devices(env)   # its checks run as the worker's would: no direct device opens
    output = checkdirs.restore(p.state, m.get("check_run_dir"))
    guard = checkdirs.take(output)
    if guard is None:
        raise OSError("check scratch is busy")
    try:
        # A push queued from a worker's sandbox recorded a short scratch that may exist only in there.
        tmp, how = checkdirs.short(output, lambda: scratch_need(p, Path(m["repo"])))
        _log_line(log, f"check scratch: {tmp or output / checkdirs.SCRATCH} ({SCRATCH_HOW[how]})")
        env = checkdirs.environment(output, env)
        # The guard is the check scratch's lifetime lock: the checks inherit it on purpose (checkdirs), so
        # their scratch outlives no process that uses it. The run lock is the push process's alone
        # (keep_run_lock_here).
        env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), TTP_PROJECT=str(p.base),
                   TTP_CHECKS_TMP_FD=str(guard.fileno()), **{RUN_LOCK_FD_ENV: str(lock.fileno())})
        _log_line(log, f"starting the push process for marker {marker}")
        with open_append_or_null(log, "ab") as out:
            child = subprocess.Popen([sys.executable, "-m", "ttp", "push", "--marker", str(marker),
                                      *(["--own"] if m.get("own") else [])],
                                     cwd=m["repo"], env=env, stdin=subprocess.PIPE, stdout=out,
                                     stderr=subprocess.STDOUT, start_new_session=True,
                                     pass_fds=(lock.fileno(), guard.fileno()))
        # The label names the push process, not this launcher, which exits (or the daemon, which restarts):
        # the stale-lock reaper (lockreap) never takes a live push for a left-running process of its run.
        locks.label_pid(lock, child.pid, via="push")
        renice(child.pid, nice)
        m.update(status="running", pid=child.pid, started=time.time(), boot=runner.boot_id(),
                 check_report_dir=str(checkdirs.reports(output)))
        write_json(marker, m)
        child.stdin.close()
        return child.pid
    except BaseException:
        checkdirs.finish(output)
        raise
    finally:
        guard.close()
        checkdirs.cleanup(output, whole=True)


def detach(p: Project, repo: Path, own: bool = False) -> int:
    """Start `ttp push` for `repo` in a process of its own and print its marker and probe. The quick
    refusals (pushing not allowed, no target, uncommitted changes, no checks for a code change) answer at once, without a marker.
    From inside a sandbox that would kill that process with the command, the daemon starts it."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if local_harness(p, top):
        print(f"ttp push: {LOCAL_HARNESS}", file=sys.stderr)
        return 0
    try:
        remote, branch, own = resolve(p, top, own)
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    if own:
        say_target(top, remote, branch)
    if _git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("ttp push: uncommitted changes; commit first", file=sys.stderr)
        return REFUSED
    if not check_list(repo_checks(d, top, p)[0]):   # refuse now, not after the task handed off waiting
        base = own_base(p, top, remote)
        if code := code_paths(top, base) if base else ["(no push target to compare with)"]:
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            print(f"ttp push: no checks configured, and this change touches more than docs "
                  f"({', '.join(code[:3])}{more}): " + no_checks_hint(top), file=sys.stderr)
            return REFUSED
    task = os.environ.get("TTP_TASK")
    rid = (f"t{task}-" if task else "") + time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    folder = p.state / DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    base, n = rid, 1
    while (folder / f"{rid}.json").exists():   # two pushes from one process within a second
        n += 1
        rid = f"{base}-{n}"
    marker = folder / f"{rid}.json"
    log = marker.with_suffix(".log")
    _prune(folder, time.time())
    m = {"id": rid, "repo": str(top), "head": _git(top, "rev-parse", "HEAD").stdout.strip(),
         "target": f"{remote}/{branch}", "remote": remote, "branch": branch,
         "log": str(log), "lock": str(_run_lock(marker)),
         "task": task, "run": os.environ.get("TTP_RUN_ID"), "own": own,
         "retry_max": retries_of(d.get("push_retries")), "attempt": 1,
         # what the daemon needs to start it again after a reboot, as for a queued push
         "env": {k: os.environ[k] for k in os.environ if k in QUEUE_ENV or k.startswith("GIT_CONFIG_")}}
    try:
        m["check_run_dir"] = str(checkdirs.new(p.state, marker, scratch_need(p, top)))
        m["check_report_dir"] = str(checkdirs.reports(Path(m["check_run_dir"])))
    except OSError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    if own and (rec := recorded_checks(os.environ.get("TTP_RUN_DIR"))):
        m["recorded_checks"] = rec   # the push process has a different TTP_RUN_DIR
    if _boxed():
        m.update(status="queued", queued=time.time())
        _log_line(log, "queued for the daemon: this command's sandbox would end a push started here")
        write_json(marker, m)
        print(f"ttp push: queued; the daemon starts it outside this command's sandbox, pushing {top} "
              f"to {remote}/{branch}")
    else:
        lock = locks.try_take([_run_lock(marker)], f"detached push {rid}", "ttp push --detach")
        if lock is None:
            print(f"ttp push: the run lock of {rid} is taken", file=sys.stderr)
            return REFUSED
        try:
            pid = _start(p, marker, m, lock, dict(os.environ))
        finally:
            lock.close()             # the child holds the run lock from here on
        print(f"ttp push: started in the background (pid {pid}), pushing {top} to {remote}/{branch}")
    print(f"marker: {marker}")
    print(f"log: {log}")
    print(f"retry_when: {result_probe(marker, p)}")
    return 0


class _Stopped(Exception):
    pass


GIT_PUSH_SUFFIX = ".gitpush"   # beside a detached push's marker once its process began a `git push` (stop_own)
_marker_here: Path | None = None   # the marker of this process when it is a detached push (run_detached)
_git_pushed = False   # this process began a `git push`: from here on a stop signal never interrupts it


def _git_push_began() -> None:
    """Mark that this process begins a `git push`, first in itself (so _stop no longer raises) and then
    beside its marker (so `ttp push --stop` refuses it): a push to a remote is never interrupted, and once
    one began the rest of the run (its outcome, a retry after a rejection) is short."""
    global _git_pushed
    _git_pushed = True
    if _marker_here is not None:
        try:
            _marker_here.with_suffix(GIT_PUSH_SUFFIX).touch()
        except OSError:
            pass


def _stop(sig, _frame) -> None:
    if _git_pushed:   # os.write: a print here could re-enter a write the signal interrupted
        os.write(2, f"ttp push: {signal.Signals(sig).name} after a git push began; not interrupting it\n".encode())
        return
    raise _Stopped(f"stopped by {signal.Signals(sig).name}")


RUN_LOCK_FD_ENV = "TTP_PUSH_RUN_LOCK_FD"   # the fd of its run lock, which a detached push process inherits


def keep_run_lock_here() -> None:
    """Make the run lock the push process inherited (RUN_LOCK_FD_ENV) close-on-exec, so it is held by this
    process alone: nothing it starts (git, a check, a hook, a shell) gets it, and a process one of those
    leaves running never makes a push that died look alive."""
    fd = os.environ.pop(RUN_LOCK_FD_ENV, "")
    if fd.isdigit():
        try:
            os.set_inheritable(int(fd), False)
        except OSError:
            pass


def run_detached(p: Project, repo: Path, marker: Path, own: bool = False) -> int:
    """The detached process: wait for the launcher's go, push, and write the outcome into `marker`."""
    keep_run_lock_here()
    print(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} detached push process {os.getpid()} up, "
          f"waiting for the go", flush=True)
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass
    print(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} detached push, marker {marker}", flush=True)
    global pool_status, _marker_here
    pool_status = marker.with_suffix(POOL_STATUS_SUFFIX)
    _marker_here = marker
    m = _read(marker)
    if m.get("status") == "running":
        m["alive"] = time.time()   # it began: a death from here on is not one at startup
        write_json(marker, m)
    reason = None
    output = None
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        output = checkdirs.restore(p.state, m.get("check_run_dir"))
        if os.environ.get("TTP_RUN_DIR") != str(checkdirs.reports(output)):
            raise OSError("TTP_RUN_DIR is missing or differs from the queued check output directory")
        rec = m.get("recorded_checks")
        rc, reason = _run_retrying(p, repo, marker, own, rec if isinstance(rec, dict) else None)
    except Exception as e:     # recorded as a failure, never left as "running"
        print(f"ttp push: {type(e).__name__}: {e}", file=sys.stderr)
        reason = str(e) if isinstance(e, _Stopped) else None
        rc = 1
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)   # the outcome gets written
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    sha = version = None
    if rc == 0:
        sha = (None if own else last_pushed) or _git(top, "rev-parse", "HEAD").stdout.strip() or None
    if rc == 0 and not own:
        version = pushed_version(p, top, sha or "HEAD")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.fsync(sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    m = _read(marker) or {"id": marker.stem, "pid": os.getpid(), "log": str(marker.with_suffix(".log"))}
    m.update(status="pushed" if rc == 0 else "failed", exit=rc, sha=sha, version=version, ended=time.time())
    if rc == 0 and not own and last_ff:
        m["fast_forward"] = last_ff
    if rc == 0 and not own and last_carried:
        m["carried"] = last_carried
    after = rc == 0 and not own and pending_after and after_set(p)
    if after:
        m["after_push"] = {"status": "running", "pid": os.getpid(), "started": time.time()}
    if rc == 0 and last_reach:
        m["reach"] = last_reach
    if reason:
        m["reason"] = reason
    write_json(marker, m)
    _forget_lock(marker)       # the outcome is written: a missing lock file reads as finished
    if after:   # with the push lock and the run lock let go, so upgrades never wait for a deploy
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_DFL)   # its receipt records a kill (pushq.tend_receipts)
        a = run_after(p, repo) or {"status": "skipped", "reason": "delivery.after_push is not set"}
        m = _read(marker) or m
        m["after_push"] = a
        write_json(marker, m)
    if output is not None:
        checkdirs.release(output)
    return rc


def _run_retrying(p: Project, repo: Path, marker: Path, own: bool, rec: dict | None) -> tuple[int, str | None]:
    """run(), and again while it is refused as not a fast-forward because the remote branch moved on
    since this attempt began (moved_on): HEAD is rebased onto the new tip and run() checks the rebased
    commit before it pushes. The push branch's own rounds (_rounds) already do this for a move during
    the checks; this covers `--own`, whose publish never rebases. At most the marker's retry_max
    retries, counting a reboot's requeue. (exit code, the reason to record or None)."""
    m = _read(marker)
    remote, branch = str(m.get("remote") or ""), str(m.get("branch") or "")
    done = _retries(m)
    if done and done[-1].get("cause") == REBOOT and _rebasing(repo):
        print("ttp push: aborting the rebase the reboot cut short", file=sys.stderr, flush=True)
        _git(repo, "rebase", "--abort")
    while True:
        start = _existing_tip(repo, remote, branch) if own and remote and remote != LOCAL else ""
        rc = run(p, repo, own, rec, after=False)
        tip = moved_on(p, repo, remote, branch, start) if rc == REJECTED and own else ""
        if not tip:
            return rc, None
        m = _read(marker)
        n, limit = len(_retries(m)), retries_of(m.get("retry_max"))
        if n >= limit:
            why = (f"{NON_FF}: {remote}/{branch} moved on to {tip[:10]}; already retried {n} time(s), "
                   f"the most delivery.push_retries allows")
            print(f"ttp push: {why}; not pushing", file=sys.stderr, flush=True)
            return rc, why
        if not _rebase(repo, tip):
            _git(repo, "rebase", "--abort")
            why = (f"{NON_FF}: rebase onto {remote}/{branch} ({tip[:10]}) conflicts; resolve it keeping "
                   f"both sides' intents, then rerun")
            print(f"ttp push: {why}", file=sys.stderr, flush=True)
            return CONFLICT, why
        _note_retry(marker, m, NON_FF, f"{remote}/{branch} moved from {start[:10]} to {tip[:10]}")
        print(f"ttp push: {remote}/{branch} moved on to {tip[:10]} during the push: rebased onto it, "
              f"retrying ({retry_words(_read(marker))})", file=sys.stderr, flush=True)


def moved_on(p: Project, repo: Path, remote: str, branch: str, start: str) -> str:
    """The remote branch's new tip when a push of HEAD was refused only because the branch moved on
    since `start`, its tip when the attempt began: HEAD then fast-forwarded `start`, the branch only
    fast-forwarded from it (nobody rewrote it) and HEAD lacks the new tip, so a rebase onto it keeps
    everything. '' otherwise: a rewritten branch (a task that rebased its own), a refusal for another
    cause, a lease push (own_rebase: never retried, never forced) or no remote."""
    if not start or not branch or remote in ("", LOCAL) or own_rebase(p, branch) is not None:
        return ""
    tip = _existing_tip(repo, remote, branch)
    if not tip or tip == start or descends(repo, tip, "HEAD"):
        return ""
    return tip if descends(repo, start, "HEAD") and descends(repo, start, tip) else ""


def _rebasing(repo: Path) -> bool:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip() or ".")
    return any((git_dir / d).is_dir() for d in ("rebase-merge", "rebase-apply"))


def after_set(p: Project) -> bool:
    """Whether `delivery.after_push` names a command (a malformed value counts: it fails loudly)."""
    v = (p.config().get("delivery") or {}).get("after_push")
    try:
        return bool(scalar_problem(v) or check_list(v))
    except (TypeError, ValueError):
        return True


def _dead_reason(m: dict) -> str:
    if m.get("boot") and not runner.same_boot(m["boot"]):
        return "the push process ended without writing an outcome: the host rebooted while it ran"
    if m.get("boot") and not m.get("alive"):   # written by a version that marks the start
        return ("the push process ended without writing an outcome before it began: it was probably "
                "killed with the command that started it (a sandbox or tool that ends background processes)")
    return "the push process ended without writing an outcome (killed, crashed or rebooted)"


def _rebooted(m: dict) -> bool:
    return bool(m.get("boot")) and not runner.same_boot(m["boot"])


def _restartable(m: dict) -> bool:
    """Whether a marker holds what the daemon needs to start its push again (one written before
    push_retries existed has no env)."""
    return bool(m.get("repo") and m.get("log") and m.get("check_run_dir")) and isinstance(m.get("env"), dict)


def _spent(m: dict) -> str:
    n = len(_retries(m))
    if not (_rebooted(m) and _restartable(m)) or n < retries_of(m.get("retry_max")):
        return ""
    return f"; {REBOOT}: already retried {n} time(s), the most delivery.push_retries allows"


def _requeue(marker: Path, m: dict) -> bool:
    """Queue again, for the daemon to start (_tend_one), the push of `marker` that a host reboot cut
    short, while its retries last. True when it is queued (or another process is handling it right now:
    its run lock is taken), False to record it as failed."""
    if not (_rebooted(m) and _restartable(m)):
        return False
    if len(_retries(m)) >= retries_of(m.get("retry_max")):
        return False
    lock = locks.try_take([_run_lock(marker)], f"detached push {m.get('id')}", "requeue after a reboot")
    if lock is None:
        return True
    with lock:
        m = _read(marker)
        if m.get("status") != "running" or not _rebooted(m):
            return True
        for k in ("pid", "alive", "started"):
            m.pop(k, None)
        m.update(status="queued", queued=time.time())
        m = _note_retry(marker, m, REBOOT, "the host rebooted while it ran")
        try:
            _log_line(Path(m["log"]), f"the host rebooted while this push ran: queued again for the daemon "
                                      f"({retry_words(m)})")
        except OSError:
            pass
    return True


def _settle(marker: Path) -> dict:
    """The marker of a push that cannot run any more recorded as failed: one that runs with its run
    lock free died; one queued longer than QUEUED_S was never started. Others come back as read, and
    so does a push queue batch's (batch.py): it lets go of its run lock before its after_push and
    writes its marker after, so only the push queue judges it (pushq.finalize), never this."""
    m = _read(marker)
    if m.get("kind") == "batch":
        return m
    if m.get("status") == "running" and locks.any_free([_run_lock(marker)]):
        m = _read(marker)      # read after the lock: the push writes its outcome before letting go
        if m.get("status") == "running" and _requeue(marker, m):
            return _read(marker)
        if m.get("status") == "running":
            m.update(status="failed", exit=None, ended=time.time(), reason=_dead_reason(m) + _spent(m))
            write_json(marker, m)
            _forget_lock(marker)
    elif m.get("status") == "queued" and time.time() - float(m.get("queued") or 0) > QUEUED_S:
        lock = locks.try_take([_run_lock(marker)], f"detached push {m.get('id')}", "expire")
        if lock is not None:   # taken: the daemon is starting it right now
            with lock:
                m = _read(marker)
                if m.get("status") == "queued":
                    m.update(status="failed", exit=None, ended=time.time(),
                             reason=f"the daemon did not start the queued push within {QUEUED_S // 60} min")
                    write_json(marker, m)
            _forget_lock(marker)
    return m


def tend(p: Project) -> list[str]:
    """The daemon's part, each tick: start the queued pushes and clear the stale markers: one whose
    push died (its run lock free, no outcome written: a killed run, a crash, a reboot) or that was
    queued and never started is recorded as failed, so nothing counts or waits on it as running any
    more, and finished markers older than KEEP_S go (_prune). Returns a line per marker it cleared,
    for the daemon's log."""
    folder = p.state / DETACHED
    cleared: list[str] = []
    for marker in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        before = _read(marker)
        try:
            m = _tend_one(p, marker)
        except Exception as e:   # a malformed marker: recorded as failed, never aborts the daemon's tick
            _malformed(marker, e)
            m = _read(marker)
        if len(_retries(m)) > len(_retries(before)):
            cleared.append(f"requeued push marker {marker.name} after a host reboot ({retry_words(m)})")
        before = before.get("status")
        if before in ("running", "queued") and m.get("status") == "failed":
            task = f" of task {m['task']}" if m.get("task") else ""
            cleared.append(f"cleared stale push marker {marker.name}{task} ({m.get('target') or 'no target'}): "
                           f"{m.get('reason') or 'recorded as failed'}")
    if folder.is_dir():
        gone = _prune(folder, time.time())
        if gone:
            cleared.append(f"removed {gone} finished push marker(s) older than {KEEP_S // 86400} days")
    return cleared


def _tend_one(p: Project, marker: Path) -> dict:
    m = _settle(marker)
    landed.record_push(p, marker, m)
    if m.get("status") != "queued" or m.get("kind") == "batch":
        return m
    lock = locks.try_take([_run_lock(marker)], f"detached push {m.get('id')}", "ttp push --detach (daemon)")
    if lock is None:
        return m
    try:
        m = _read(marker)
        if m.get("status") != "queued":
            return m
        env = {**os.environ, **(m.get("env") or {})}
        env.update(git_fsync_env(env))
        try:
            # A worker's push runs niced like the worker it came from (one started there inherits it).
            _start(p, marker, m, lock, env, nice_level(p.config().get("runner"))[0])
        except OSError as e:   # its worktree went, say: recorded, never retried each tick
            m.update(status="failed", exit=None, ended=time.time(),
                     reason=f"the daemon could not start the push: {e}")
            write_json(marker, m)
    finally:
        lock.close()
    return _read(marker)


def _malformed(marker: Path, e: Exception) -> None:
    """Record a queued push the daemon could not handle as failed, so it is skipped from now on."""
    m = _read(marker)
    if m.get("status") != "queued":
        return
    m.update(status="failed", exit=None, ended=time.time(),
             reason=f"the daemon could not start the push: malformed marker ({type(e).__name__}: {e})")
    try:
        write_json(marker, m)
    except OSError:
        pass
    _forget_lock(marker)


NOT_PUSHED = 3   # `--result --pushed`: finished without pushing


def result(marker: Path, pushed_only: bool = False) -> int:
    """`ttp push --result <marker>`, the probe: 0 once the push finished (pushed, failed or died),
    1 while it runs or waits for the daemon, 2 when there is no such marker. A push whose run lock is
    free but that wrote no outcome died; it is recorded as failed here, so nobody waits for it.
    With `pushed_only` (`--pushed`): 0 only when the push landed, 1 while it runs, 3 when it finished
    without pushing, so an on_pass_cmd tests the exit code and needs no grep."""
    marker = Path(marker)
    if not _read(marker):
        print(f"ttp push: no detached push marker at {marker}", file=sys.stderr)
        return REFUSED
    if _read(marker).get("kind") == "batch":     # the push queue's batch process (batch.py)
        return batch.summary(marker, pushed_only)
    try:
        m = _settle(marker)
        since = {"queued": "queued", "running": "started"}.get(m.get("status"))
        took = time.time() - float(m.get(since) or time.time()) if since else 0
    except (TypeError, ValueError) as e:   # a malformed marker: a clear verdict, not a traceback
        _malformed(marker, e)   # best effort: the verdict does not depend on the rewrite
        m = {**_read(marker), "status": "failed", "exit": None,
             "reason": f"malformed marker ({type(e).__name__}: {e})"}
        took = 0
    retried = f"; {retry_words(m)}" if retry_words(m) else ""
    if m.get("status") == "running":
        print(f"ttp push: still running (pid {m.get('pid')}, {took:.0f} s{retried}{pool_status_words(marker)}); "
              f"log: {m.get('log')}")
        return 1
    if m.get("status") == "queued":
        print(f"ttp push: queued for the daemon ({took:.0f} s{retried}); log: {m.get('log')}")
        return 1
    if retried:
        print(f"ttp push: {retry_words(m)}")
    if m.get("status") == "pushed":
        v = f", version {m['version']}" if m.get("version") else ""
        print(f"ttp push: pushed {m.get('sha')} to {m.get('target')}{v}{reach_words(m.get('reach'))}")
        for line in m.get("fast_forward") or []:
            print(f"ttp push: {'warning: ' if line in ff_warnings([line]) else ''}{line}")
        a = m.get("after_push") if isinstance(m.get("after_push"), dict) else {}
        if a.get("status") == "running" and _pid_alive(a.get("pid")):
            print("ttp push: after_push is still running; the daemon reports how it ends")
        elif a.get("status") == "running":
            print("ttp push: warning: after_push stopped before it finished; the push stands")
        elif a.get("status") == "ok":
            print("ttp push: after_push ok")
        elif a.get("status") and a.get("status") != "skipped":
            print(f"ttp push: warning: {after_words(a)}; the push stands")
    else:
        why = m.get("reason") or f"exit {m.get('exit')}"
        print(f"ttp push: not pushed ({why}); log: {m.get('log')}")
        tail = _tail(m.get("log"))
        if tail:
            print(tail)
        return NOT_PUSHED if pushed_only else 0
    return 0


def unpushed(marker: Path) -> str:
    """Why the finished push of `marker` did not land (as `--result` reads it), or '' when it pushed,
    still runs, finished with nothing to push or its marker cannot be read: the daemon then wakes a
    task waiting on it at its own tier, as for failed checks. A batch whose process ended without an
    outcome (killed, crashed or rebooted) did not land."""
    m = _read(Path(marker))
    if m.get("kind") == "batch":
        if not m.get("outcome") and not batch.alive(Path(marker)):
            m = _read(Path(marker))   # it may have finished between the two reads
            if not m.get("outcome"):
                return "batch ended without an outcome (killed, crashed or rebooted)"
        out = m.get("outcome")
        return f"batch {out}" + (f" ({m['message']})" if m.get("message") else "") \
            if out and out not in ("pushed", "landed", "nothing") else ""
    if m.get("status") in (None, "pushed", "running", "queued"):
        return ""
    return str(m.get("reason") or f"exit {m.get('exit')}")


def pushed(marker: Path) -> bool:
    """Whether the finished push of `marker` landed its commits: a batch whose outcome is pushed or
    landed, or a single push with status pushed. Running, queued, nothing to push, failed, refused or
    an unreadable marker: False."""
    m = _read(Path(marker))
    if m.get("kind") == "batch":
        return m.get("outcome") in ("pushed", "landed")
    return m.get("status") == "pushed"


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def running(p: Project) -> list[dict]:
    """Markers of this project's detached pushes that are still running or queued. A running one whose
    run lock does not open (locks.free_state None) counts, with `lock_unknown`."""
    out = []
    for marker in sorted((p.state / DETACHED).glob("*.json")):
        m = _read(marker)
        free = locks.free_state(_run_lock(marker)) if m.get("status") == "running" else True
        if m.get("status") == "queued" or m.get("status") == "running" and not free:
            out.append({**m, "marker": str(marker), **({"lock_unknown": True} if free is None else {})})
    return out


STOP_TERM_S = 15.0   # stop_own: how long a push has to end after SIGTERM before its group gets SIGKILL
NOT_STOPPED = 1      # stop_own: the push began its `git push` (or did not end) and was left running


def stop_own(p: Project, marker: Path, why: str = "") -> int:
    """`ttp push --stop <marker>` (and `ttp detach --stop` on a push marker): end this task's own detached
    push while it waits for its turn or runs its checks, so the push train can take its head, and remove
    its marker, log and lock file. Refused (REFUSED) for a marker outside this project's pushes folder,
    a push queue batch, and a push of another task or with no task. A queued push is recorded as not
    started (jobstop.end_push). A running one gets SIGTERM to its process alone: it ends its lock wait or
    its checks (their group leader ends them with it) and writes its outcome; SIGKILL to its group follows
    after STOP_TERM_S only while it has not begun a `git push`. A push that began one is never signalled
    and keeps its marker: NOT_STOPPED. Only what the push itself held is freed (its process ends, so its
    run lock and any branch lock go with it); a push that already ended is left as it is (0)."""
    say = lambda m: print(f"ttp push: {m}", file=sys.stderr)   # noqa: E731
    marker = Path(marker).expanduser().resolve()
    folder = (p.state / DETACHED).resolve()
    mine = os.environ.get("TTP_PROJECT")
    if mine and Path(mine).expanduser().resolve() != p.base.resolve():
        say(f"refused: {marker} is a push of the project at {p.base}, not of this run's project ({mine})")
        return REFUSED
    if marker.parent != folder or marker.suffix != ".json":
        say(f"refused: {marker} is not a detached push marker of this project ({folder}/<id>.json)")
        return REFUSED
    m = _read(marker)
    if not m:
        say(f"no detached push marker at {marker}")
        return REFUSED
    if m.get("kind") == "batch":
        say("refused: that is the push queue's batch, not a task's push")
        return REFUSED
    me = os.environ.get("TTP_TASK") or ""
    if not me or str(m.get("task") or "") != me:
        say(f"refused: the push {marker.stem} is task #{m.get('task') or '?'}'s, "
            + (f"not task #{me}'s" if me else "and only its own task's run may stop it"))
        return REFUSED
    why = why or f"task #{me} withdrew its own push"
    side = marker.with_suffix(GIT_PUSH_SUFFIX)
    m = _settle(marker)
    if m.get("status") not in ("queued", "running"):
        say(f"the push {marker.stem} already ended ({m.get('status') or 'no status'}); nothing to stop")
        return 0
    if m.get("status") == "running" and side.exists():
        say(f"not stopped: the push {marker.stem} is in its git push, which is never interrupted; "
            f"its outcome comes from {result_probe(marker, p)}")
        return NOT_STOPPED
    if m.get("status") == "queued":
        if jobstop.end_push(p, marker, why, int(me)) is None and _read(marker).get("status") in ("queued", "running"):
            say(f"not stopped: the daemon is starting the push {marker.stem} right now; run this again")
            return NOT_STOPPED
    else:
        run_lock, pid = _run_lock(marker), m.get("pid")
        if not (isinstance(pid, int) and pid > 1) or str(marker) not in jobstop._argv(pid):
            say(f"not stopped: pid {pid} of the push {marker.stem} is not its push process")
            return NOT_STOPPED
        sent = []
        for sig, group in ((signal.SIGTERM, False), (signal.SIGKILL, True)):
            if group and side.exists():
                break   # it began its git push meanwhile: never interrupted
            try:
                os.killpg(pid, sig) if group else os.kill(pid, sig)
                sent.append(sig)
            except OSError:
                pass
            deadline = time.monotonic() + STOP_TERM_S
            while not locks.any_free([run_lock]) and time.monotonic() < deadline:
                time.sleep(0.1)
            if locks.any_free([run_lock]):
                break
        for sig in sent:
            detachjob.signalled(marker, sig, pid, "push.stop_own", why, by="push", group=sig == signal.SIGKILL,
                             state=p.state, task=int(me))
        if not locks.any_free([run_lock]):
            say(f"not stopped: the push {marker.stem} " + ("began its git push, which is never interrupted"
                if side.exists() else f"did not end within {STOP_TERM_S:.0f} s of SIGTERM"))
            return NOT_STOPPED
        if _read(marker).get("status") == "pushed":
            say(f"not removed: the push {marker.stem} pushed before it ended; its marker stays for its result")
            return NOT_STOPPED
    for f in (marker, marker.with_suffix(".log"), marker.with_suffix(POOL_STATUS_SUFFIX), side, _run_lock(marker)):
        try:
            f.unlink()
        except OSError:
            pass
    print(f"ttp push: stopped the push {marker.stem} (task #{me}) and removed its marker: {why}")
    return 0


def status_lines(p: Project) -> list[str]:
    """`ttp status`: a line per running or queued detached push that has retried, naming its attempt
    and the causes, so the coordinator and the user see a push that tries again by itself."""
    out = []
    for m in running(p):
        words = "; ".join(x for x in (retry_words(m), "run lock state unknown (its file does not open here)"
                                      if m.get("lock_unknown") else "") if x)
        if words:
            task = f"task #{m['task']}, " if m.get("task") else ""
            out.append(f"  push {m.get('id') or Path(m['marker']).stem} {m.get('status')} "
                       f"({task}{m.get('target') or 'no target'}): {words}")
    return out


# Last, not at the top: batch imports push and reads its constants at import time. Loaded with push,
# never inside a function: a push that outlives an upgrade of runtime/ must not import a newer batch
# that calls into this older push (after_push would fail on a missing attribute). cli and pushpass
# (for `ttp push --own` reusing a `ttp checks` pass) likewise: pushpass imports push at import time.
from . import batch, cli, jobstop, pushpass, verdictcarry  # noqa: E402
from . import detach as detachjob  # noqa: E402  (this module has its own `detach` function)

# --- project-local imports below ---

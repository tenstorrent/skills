# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Waits that heal themselves, model-free: the checks behind Daemon.probe_waiting and the hand-off it
records, kept here as plain functions of a probe, its output and the project.

- A `ttp devq probe <runner> <id>` that finds its job pending with no runner alive exits
  DEVQ_RUNNER_DOWN_RC; the daemon restarts that runner itself (`ttp devq start`), rate-limited per
  runner, and the task sleeps on (devq_runner).
- A probe that can never pass is named in one event per wait (never_passes): its command is not
  found, it names a run dir or worktree that is gone, its devq job is unknown to the runner, or it
  printed the same output for max_hold_s while a task it names has ended.
- A queued task whose start_when keeps saying "not yet" while it names a device or serving resource
  is raised once: a probe waiting on a dead resource hides the outage (watched_resources)."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from .devq import RUNNER_DOWN_RC as DEVQ_RUNNER_DOWN_RC   # pending, no runner alive

RESTART_EVERY_S = 600         # the daemon restarts one runner at most this often
RESTARTS_PER_DAY = 6          # default of device.runner_restarts_per_day
RESTART_FAILS_REPORTED = 2    # restarts failed in a row before one low line and a self-fix task
RUNNER_RESTARTS_KEY = "devq_restarts"   # kv: runner -> {last, day, n, fails, error, reported}
OUTPUT_CHARS = 400
START_STALE_S = 3 * 3600      # default of waiting.start_resource_stale_s
WATCHED_RESOURCE_RE = re.compile(r"device|serv", re.I)
TASK_REF_RE = re.compile(r"(?:#|\blanded:#?|--task[ =])([0-9]+)\b")
GIT_HISTORY_RE = re.compile(r"\bgit\b[^|;&]*\b(?:log|rev-list|reflog|show|branch|cherry)\b")


def _words(probe: str) -> list[str]:
    try:
        return shlex.split(str(probe or ""))
    except ValueError:
        return str(probe or "").split()


def devq_runner(probe: str) -> str | None:
    """The runner a `ttp devq probe <runner> <id>` retry_when waits on, else None (any other probe, or
    one chained with more commands: only the plain probe's exit 3 means a runner is down)."""
    w = _words(probe)
    if len(w) == 5 and Path(w[0]).name == "ttp" and w[1:3] == ["devq", "probe"]:
        return w[3]
    return None


def greps_history(probe: str) -> bool:
    """Whether a probe searches commit messages or branch logs (`git log --grep`, `git log | grep`,
    `git branch ... | grep`). A squash, rebase or reworded landing never matches it, so it may wait
    forever: `landed:#<id>` checks the landing itself."""
    probe = str(probe or "")
    return bool(GIT_HISTORY_RE.search(probe)) and "grep" in probe


def clip(text: str, n: int = OUTPUT_CHARS) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else "…" + s[-(n - 1):]


def gone_paths(p, probe: str) -> list[str]:
    """Paths the probe names under one of the project's run dirs or worktrees whose run dir or worktree
    no longer exists: a marker there can never appear. A missing file in a live run dir is the
    normal wait and is not listed."""
    out = []
    for root in (p.runs, p.worktrees):
        root = str(root).rstrip("/") + "/"
        for m in re.finditer(re.escape(root) + r"([^/\s'\";|&)]+)", str(probe or "")):
            top = Path(root + m.group(1))
            if not top.exists() and str(top) not in out:
                out.append(str(top))
    return out


def task_refs(db, probe: str, own: int) -> list[int]:
    """Other tasks a probe names: `#N`, `landed:#N`, `--task N`, or a run dir of theirs."""
    ids = {int(m.group(1)) for m in TASK_REF_RE.finditer(str(probe or ""))}
    for m in re.finditer(r"/runs/([0-9]+)\b", str(probe or "")):
        r = db.one("SELECT task FROM runs WHERE id=?", (int(m.group(1)),))
        if r and r["task"]:
            ids.add(int(r["task"]))
    ids.discard(own)
    return sorted(ids)


def never_passes(p, probe: str, rc=None, output: str = "") -> str:
    """Why a probe can never pass, in plain words, or "" when it may yet."""
    if rc == 127:
        return "its command is not found (exit 127)"
    gone = gone_paths(p, probe)
    if gone:
        return f"it names {gone[0]}, a run dir or worktree that no longer exists"
    if "unknown to the runner" in str(output or ""):
        return "its devq job is unknown to its runner"
    return ""


def ended_refs(db, probe: str, own: int) -> list[str]:
    """`#N (status)` for each task the probe names that has ended."""
    from .db import TERMINAL_TASK_STATES
    out = []
    for tid in task_refs(db, probe, own):
        t = db.task(tid)
        if t and t["status"] in TERMINAL_TASK_STATES:
            out.append(f"#{tid} ({t['status']})")
    return out


def watched_resources(cfg: dict, names) -> list[str]:
    """The device or serving resources among `names`: a device lock or runner of the project's config,
    or a name that says device or serv(er/ing)."""
    dev = cfg.get("device") or {}
    known = {str(x) for x in dev.get("locks") or []} | {str(x) for x in (dev.get("runners") or {})}
    return sorted(n for n in names if n in known or WATCHED_RESOURCE_RE.search(n))

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
  is raised once: a probe waiting on a dead resource hides the outage (watched_resources).
- A probe that waits over ssh only for an end marker file, with no check that the job's driver still
  lives, is flagged (end_marker_only): a driver killed by a reboot or power cycle never writes it.
- A wait on any probe other than `ttp lock --probe` shows the probe's own latest line, with when it was
  read, before the worker's hand-off words, labelled with their time (live_note): a holder the
  worker named may have ended long ago.
- A probe that greps a section header a status command always prints (`tt-device-mcp status | grep -q
  RUNNING`) is flagged (header_grep): it matches whether or not a job is listed under it."""
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


LIVE_CHARS = 160              # a probe's line in a wait note
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def last_line(text: str, n: int = LIVE_CHARS) -> str:
    """A probe output's last non-empty line, trimmed, without control characters, at most n characters."""
    for ln in reversed(_ANSI_RE.sub("", str(text or "")).splitlines()):
        ln = " ".join(_CTRL_RE.sub(" ", ln).split())
        if ln:
            return ln if len(ln) <= n else ln[:n - 1] + "…"
    return ""


def live_note(said: str, said_at, out: str, read_at, where) -> str:
    """A wait note for a probe that is not `ttp lock --probe`: the probe's latest line and when it was
    read, then the worker's hand-off words (`said`) labelled with their time; with no line, those words
    marked as possibly stale."""
    from . import timefmt

    def at(ts) -> str:
        return timefmt.short(float(ts), where) if isinstance(ts, (int, float)) and ts > 0 else ""
    line, since = last_line(out), at(said_at)
    note = f"worker's note{f' from {since}' if since else ''}"
    if line:
        read = at(read_at)
        return f"{line}{f' (read {read})' if read else ''}" + (f"; {note}: {said}" if said else "")
    return f"{said} ({note}, may be stale)" if said else f"its probe's condition ({note}, may be stale)"


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


# An end marker a job writes when it ends (`test -e x.rc`, `[ -f out/done ]`), tested over ssh.
_SSH_RE = re.compile(r"(?:^|[\s;&|(`'\"])(?:\S*/)?ssh\s")
_END_TEST_RE = re.compile(r"(?:\btest\s+|\[\[?\s+)-[efs]\s+[\"']?([^\s\"';&|)\]]+)")
_END_NAME_RE = re.compile(r"(?:\.rc|\.exit\w*|\.status|done|finish\w*|complete\w*|ended|\.end)$", re.I)
# What tells a dead driver from a slow one: its pid, its boot, its scheduler or ttp's own check.
_LIVENESS_RE = re.compile(r"kill\s+-0|\bpgrep\b|\bpidof\b|\bps\s|/proc/|boot_id|\.pid\b|\bdetach\s+--check\b"
                          r"|\bdevq\s+probe\b|\bsystemctl\b|\bsqueue\b|\bsacct\b|\bscontrol\b|\bflock\b")
END_MARKER_HINT = ("it waits over ssh only for an end marker file, with no check that the job's driver still "
                   "lives: a driver killed by a reboot or power cycle never writes it, and the wait sleeps on to "
                   "its timer. Start the job with `ttp detach --remote` and wait with `ttp detach --check --host "
                   "<alias> <dir>/<name>` (add `--rows <file> --after <n>` to also wake on a new result row)")


def end_marker_only(probe: str | None) -> str:
    """END_MARKER_HINT when a probe tests an end marker over ssh and nothing that tells a dead driver
    from a running one, else "". A flag only, never a refusal."""
    probe = str(probe or "")
    if not _SSH_RE.search(probe) or _LIVENESS_RE.search(probe):
        return ""
    return END_MARKER_HINT if any(_END_NAME_RE.search(m) for m in _END_TEST_RE.findall(probe)) else ""


# A grep whose whole pattern is one section header word a status listing always prints, such as the
# device broker's RUNNING or QUEUED: the header shows with no job under it too, so the grep always matches.
# Only a probe reading the broker's status listing: in a Slurm pipeline (squeue -o %T) the word is a job's state.
HEADER_WORDS = ("RUNNING", "QUEUED", "PENDING")
_HEADER_GREP_RE = re.compile(r"\b[ef]?grep((?:\s+-[A-Za-z]+)*)\s+(?:-e\s+)?([\"']?)\^?(" + "|".join(HEADER_WORDS)
                             + r")\$?\2(?=$|[\s;&|)'\"])", re.I)
_STATUS_LISTING_RE = re.compile(r"\b(?:tt-device-mcp|\S*broker\S*)(?:\s+[^\s|;&'\"]+)*?\s+status\b", re.I)
_SLURM_RE = re.compile(r"\b(?:squeue|sacct|scontrol|sinfo)\b")
HEADER_GREP_HINT = ("it greps `{word}`, a section header the status output prints even when no job is under it, "
                    "so it always matches: test the job rows instead (a row under the section, or the jobs' "
                    "status in the broker's JSON job list); never grep a header word that is always printed")


def header_grep(probe: str | None) -> str:
    """HEADER_GREP_HINT when a probe reading the device broker's status listing greps only a section
    header word (as written, or in any case with -i), else "". A grep fed by a Slurm command is not
    flagged. A flag only, never a refusal."""
    probe = str(probe or "")
    if not _STATUS_LISTING_RE.search(probe):
        return ""
    for m in _HEADER_GREP_RE.finditer(probe):
        feed = re.split(r"&&|\|\||[;\n]", probe[:m.start()])[-1]   # the pipeline this grep reads
        if _SLURM_RE.search(feed):
            continue
        if m[3].isupper() or "i" in m[1].replace("-", ""):
            return HEADER_GREP_HINT.format(word=m[3])
    return ""


_ALWAYS_RE = re.compile(r"(?:(?:/usr)?/bin/)?true|:|exit(?:\s+0)?")


def always_passes(probe: str | None) -> bool:
    """Whether a probe passes whatever it waits for: only `true`, `:` or `exit 0` steps (joined by && or ;)."""
    parts = [x.strip() for x in re.split(r"&&|;", str(probe or ""))]
    return bool(str(probe or "").strip()) and all(not x or _ALWAYS_RE.fullmatch(x) for x in parts) and any(parts)


def probe_flag(probe: str | None) -> str:
    """The first flag for a wait probe that may wait wrongly (end_marker_only, header_grep), else ""."""
    return end_marker_only(probe) or header_grep(probe)


def never_passes(p, probe: str, rc=None, output: str = "") -> str:
    """Why a probe can never pass, in plain words, or "" when it may yet."""
    if rc == 127:
        return "its command is not found (exit 127)"
    if rc in (None, 2):   # dash's exit for a syntax error; None: the wait just started
        from .ends import SH, sh_syntax_error
        from .landed import probe_command
        err = sh_syntax_error(probe_command(probe, p))
        if err:
            return f"{SH}, which runs it, cannot parse it ({err}): it must be POSIX sh, not bash-only syntax"
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

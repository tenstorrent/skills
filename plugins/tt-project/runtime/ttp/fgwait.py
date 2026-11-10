"""A worker's foreground wait holds its worker slot (owner's agent msg #223, "make it efficient").

A worker that runs a remote test in the foreground (`ttp lock <box> -- ...`, a test script over ssh)
or polls something slow keeps its worker slot for as long as it waits, and on a workstation those
slots are few. Run that work detached (`ttp detach <name> [--lock <box>] -- <command>`) and hand off
`waiting` with the `retry_when` it prints: the slot frees at once, the job's log and exit code stay in
the run folder, and the task resumes once the job ends.

- reminder(): the PreToolUse hook's note (never a refusal) on such a command in a worker run.
- watch(): `ttp lock` in a run, once its foreground command has run FG_WAIT_S, appends one update to the
  run's steer.md and a progress line naming the `ttp detach` command to use, so the worker reads it as
  soon as the call returns, and its coordinator sees a run that held its slot waiting.
- detach_line(): that command.
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import time
from pathlib import Path

# A foreground command run under `ttp lock` this long in a worker run gets the steer note: past "a few
# minutes", where the worker prompt says to run it detached.
FG_WAIT_S = 180
FG_WAIT_ENV = "TTP_FG_WAIT_S"   # seconds, overriding FG_WAIT_S (tests)

# `ttp lock <res...> -- <cmd>` run in the foreground (a probe or `--json` answer returns at once).
_LOCK_RE = re.compile(r"(?:^|[\s;&|(])ttp\s+lock\s+(?![^\n;&|]*--(?:probe|json)\b)[^\n;&|]*?\s--\s+\S")
# An ssh command up to its end (quoted strings whole, so `ssh box 'cd x && pytest'` is one), and a test or
# script run in it.
_SSH_RE = re.compile(r"(?:^|[\s;&|(])ssh\s(?:'[^']*'|\"[^\"]*\"|[^\n;&|'\"])*")
_REMOTE_TEST_RE = re.compile(r"\bpytest\b|\bmake\b|\bninja\b|\bctest\b|\S\.sh\b")
# Already detached, or a command that waits on its own detached run.
_DETACHED_RE = re.compile(r"\bttp\s+(?:detach|checks|push)\b|\bsetsid\b|\bnohup\b")


def _worker_run() -> bool:
    from . import coordtools
    return bool(os.environ.get("TTP_RUN_DIR") and os.environ.get("TTP_TASK")) \
        and not os.environ.get("TTP_DETACHED") and os.environ.get(coordtools.ROLE_ENV) != "coordinator"


def holds_slot(cmd: str) -> str | None:
    """What kind of slot-holding wait `cmd` is: "remote" (a foreground `ttp lock ... --` or a test over
    ssh), "poll" (runner.is_wait: a long sleep, a sleep-poll loop, `tail -f`), or None."""
    if not isinstance(cmd, str) or _DETACHED_RE.search(cmd):
        return None
    if _LOCK_RE.search(cmd) or any(_REMOTE_TEST_RE.search(m.group()) for m in _SSH_RE.finditer(cmd)):
        return "remote"
    from . import runner
    return "poll" if runner.is_wait(cmd) else None


def reminder(cmd: str) -> str | None:
    """The hook's reminder for a slot-holding wait in a worker run (the command still runs)."""
    if not _worker_run():
        return None
    kind = holds_slot(cmd)
    if not kind:
        return None
    what = ("a remote test run in the foreground (`ttp lock <box> -- ...`, a test over ssh)" if kind == "remote"
            else "a poll or sleep-wait")
    return (f"tt-project: {what} holds your worker slot for as long as it waits. If it may take more than a few "
            "minutes, start it with `ttp detach <name> [--lock <box>] -- <command>` and hand off `waiting` with "
            "the `retry_when` it prints: your slot frees and you resume once the job ends. "
            "This is a reminder: the command still runs.")


def detach_line(locks: list[str], cmd: list[str]) -> str:
    """The `ttp detach` command that runs `cmd` under `locks` without holding the worker's slot."""
    name = re.sub(r"[^\w.-]+", "-", Path(cmd[0]).name if cmd else "job").strip("-")[:40] or "job"
    return " ".join(["ttp detach", name, *(f"--lock {shlex.quote(r)}" for r in locks), "--",
                     shlex.join(cmd)])


def note(run_dir: Path, locks: list[str], cmd: list[str], waited_s: float, key: str) -> str:
    """Tell the run (steer.md, once per `key`; progress.md) that this foreground command holds its slot."""
    from .coordinator import _append_update
    from .project import append_text
    from . import timefmt
    text = (f"`ttp lock {','.join(locks)}` has run its command in the foreground for {waited_s / 60:.0f} min, "
            "holding your worker slot while it waits. When this call returns, and for any later remote test "
            f"run or slow poll: start it detached, `{detach_line(locks, cmd)}`, and hand off `waiting` with the "
            "`retry_when` it prints; your slot frees and you resume once the job ends, its log and exit code "
            "kept in your run folder.")
    try:
        _append_update(run_dir / "steer.md", text, f"fgwait-{key}")
        append_text(run_dir / "progress.md",
                    f"{timefmt.clock()} ttp lock {','.join(locks)}: foreground command running "
                    f"{waited_s / 60:.0f} min, holding the worker slot\n")
    except OSError:
        pass
    return text


def watch(run_dir: Path | None, locks: list[str], cmd: list[str], after: float | None = None) -> threading.Timer | None:
    """In a worker run, a timer that calls note() once `cmd` has run `after` seconds; the caller cancels
    it once the command ends. None outside a worker run or in a detached job (that wait frees no slot)."""
    if run_dir is None or not _worker_run():
        return None
    if after is None:
        try:
            after = float(os.environ.get(FG_WAIT_ENV) or FG_WAIT_S)
        except ValueError:
            after = FG_WAIT_S
    started, key = time.time(), f"{os.getpid()}-{int(time.time())}"
    t = threading.Timer(after, lambda: note(run_dir, locks, cmd, time.time() - started, key))
    t.daemon = True
    t.start()
    return t

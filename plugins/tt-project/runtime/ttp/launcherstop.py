# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp detach --stop NAME` for a driver a project's own launcher started, not `ttp detach`.

Such a launcher records its driver's pid in RESULTS_DIR/NAME.pid (the first word of its first line;
an optional `start=<token>`, runner.proc_start's, pins the process) and stops it cooperatively once a
file STOP appears in RESULTS_DIR. With no detached job recorded for NAME, `ttp detach --stop` looks for
NAME.pid in the folder of a path it was given, else in $TTP_RUN_DIR and the task's folders t<id>,
task<id> and task-<id> in the project root (results_dirs); nothing else is searched.

A live pid counts as that launcher's only while it runs as this user, is the process the file pins
(its start token, else it started no later than the file was last written: a pid reused afterwards
started later) and, where its environment names a tt-project project, names this one. Then stop()
writes RESULTS_DIR/STOP, waits STOP_WAIT_S for it to exit, then sends SIGTERM (to its process group
when it leads one), waits TERM_WAIT_S and sends SIGKILL, checking before each signal that the pid is
still the same process. A pid that is gone or reused is reported already stopped; no STOP file is
written then, so a later launch does not stop at once.
"""
from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path

from .jobstop import _stat
from .runner import proc_start

STOP_FILE = "STOP"
STOP_WAIT_S = 60.0
TERM_WAIT_S = 15.0
KILL_WAIT_S = 2.0
WRITE_SLACK_S = 2.0   # /proc start times are derived from the boot time: allow a little clock skew
TASK_DIRS = ("t{}", "task{}", "task-{}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def results_dirs(what: str, run_dir: str | None, project: str | None, task: str | None) -> tuple[str, list[Path]]:
    """(NAME, folders to look in for NAME.pid) for `ttp detach --stop <what>`: a path's own folder, or
    for a bare name $TTP_RUN_DIR and the task's folders in the project root (the TTP_PROJECT's parent)."""
    if "/" in what:
        f = Path(what).expanduser().absolute()
        return (f.stem if f.suffix in (".rc", ".pid") else f.name), [f.parent]
    name = what[:-3] if what.endswith(".rc") else what
    dirs = [Path(run_dir)] if run_dir else []
    if project and task and task.isdigit():
        root = Path(project).resolve().parent
        dirs += [root / d.format(task) for d in TASK_DIRS]
    return name, dirs


def find(name: str, dirs: list[Path]) -> Path | None:
    """The first NAME.pid among dirs, else None."""
    if not NAME_RE.fullmatch(name):
        return None
    return next((d / f"{name}.pid" for d in dirs if (d / f"{name}.pid").is_file()), None)


def read(pidfile: Path) -> tuple[int, str | None]:
    """(pid, start token or None) from the pid file's first line; ValueError when it names no pid."""
    w = (pidfile.read_text(errors="replace")[:4096].strip().splitlines() or [""])[0].split()
    if not w or not w[0].isdigit() or int(w[0]) <= 1:
        raise ValueError(f"{pidfile} names no pid")
    return int(w[0]), next((x.split("=", 1)[1] for x in w[1:] if x.startswith("start=")), None)


def _started_at(pid: int) -> float | None:
    """When pid started, in seconds since the epoch (Linux /proc), else None."""
    try:
        btime = next(float(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime"))
        return btime + int(proc_start(pid) or "") / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return None


def _project_of(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for x in raw.split(b"\0"):
        if x.startswith(b"TTP_PROJECT="):
            return x.split(b"=", 1)[1].decode(errors="replace") or None
    return None


def identify(pidfile: Path, project: str | None) -> tuple[str, int, str]:
    """(state, pid, detail) for the pid file: ("live", pid, start token), ("gone", pid, why) or
    ("refused", pid, why)."""
    pid, token = read(pidfile)
    written = pidfile.stat().st_mtime
    st = _stat(pid)
    now = proc_start(pid)
    if st is None or now is None:
        return "gone", pid, "its process is gone"
    if st[1] != os.getuid():
        return "refused", pid, f"pid {pid} runs as another user"
    if token is not None and now != token:
        return "gone", pid, f"pid {pid} is now another process (start token {now}, the file pins {token})"
    if token is None:
        at = _started_at(pid)
        if at is None:
            return "refused", pid, f"the start time of pid {pid} cannot be read, so it cannot be told from a reused pid"
        if at > written + WRITE_SLACK_S:
            return "gone", pid, f"pid {pid} is now another process (it started after the pid file was written)"
    there = _project_of(pid)
    if there and project and Path(there).resolve() != Path(project).resolve():
        return "refused", pid, f"pid {pid} belongs to another tt-project project ({there})"
    return "live", pid, now


def _same(pid: int, token: str) -> bool:
    return _stat(pid) is not None and proc_start(pid) == token


def _wait(pid: int, token: str, s: float) -> bool:
    """Whether pid stopped being that process within s seconds."""
    deadline = time.monotonic() + s
    while _same(pid, token):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _signal(pid: int, token: str, sig: int) -> None:
    if not _same(pid, token):
        return
    try:
        if (st := _stat(pid)) and st[0] == pid:
            os.killpg(pid, sig)   # it leads its group: its driver's children go with it
        else:
            os.kill(pid, sig)
    except OSError:
        pass


def stop(pidfile: Path, pid: int, token: str, why: str) -> dict:
    """Write RESULTS_DIR/STOP and wait for the launcher's process to exit, then SIGTERM, then SIGKILL."""
    stop_file = pidfile.parent / STOP_FILE
    if not stop_file.exists():
        stop_file.write_text(f"{why} at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
    name = pidfile.stem
    if _wait(pid, token, STOP_WAIT_S):
        return {"how": "stop_file", "left": False,
                "text": f"{name}: stopped; its launcher (pid {pid}) exited after {stop_file} was written"}
    _signal(pid, token, signal.SIGTERM)
    if _wait(pid, token, TERM_WAIT_S):
        return {"how": "sigterm", "left": False,
                "text": f"{name}: stopped; pid {pid} ignored {stop_file} for {STOP_WAIT_S:g}s and ended on SIGTERM"}
    _signal(pid, token, signal.SIGKILL)
    if _wait(pid, token, KILL_WAIT_S):
        return {"how": "sigkill", "left": False,
                "text": f"{name}: stopped; pid {pid} ignored {stop_file} and SIGTERM and was killed (SIGKILL)"}
    return {"how": "left", "left": True,
            "text": f"{name}: pid {pid} still runs after {stop_file}, SIGTERM and SIGKILL"}

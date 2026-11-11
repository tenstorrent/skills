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
still the same process (never this command or one of its ancestors). A pid that is gone or reused
is reported already stopped; no STOP file is written then, so a later launch does not stop at once.
"""
from __future__ import annotations

import os
import re
import signal
import time
from pathlib import Path

from .jobstop import _stat
from .project import FOLDER, durable_write
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


def _ancestors() -> set[int]:
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return out


def identify(pidfile: Path, project: str | None) -> tuple[str, int, str]:
    """(state, pid, detail) for the pid file: ("live", pid, start token), ("gone", pid, why) or
    ("refused", pid, why)."""
    pid, token = read(pidfile)
    written = pidfile.stat().st_mtime
    st = _stat(pid)
    now = proc_start(pid)
    if st is None or now is None:
        return "gone", pid, "its process is gone"
    if pid in _ancestors():
        return "refused", pid, f"pid {pid} is this command or one of its ancestors"
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
    if not os.path.lexists(stop_file):   # one there (even a symlink planted as STOP) is left as it is
        durable_write(stop_file, f"{why} at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
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


# A launcher that keeps no NAME.pid ------------------------------------------------------------------
# A project's own launcher may lay its jobs out its own way: a job folder, no pid file, and a stop command
# of its own. The project registers that stop in project.json, `detach.launcher_stops`: {glob: command}.
# The glob matches the job folder's path relative to the project root (an absolute glob: the whole path),
# one folder level per `*`, any number of levels for a `**` level. The command (a string split as a shell
# would, or a list; never run by a shell) gets the job folder as its last argument and runs from the
# project root. `ttp detach --stop <job folder>` then runs it (registered_stop), logs the stop in the
# project's job-signal stream and events (detached_job_stopped, as jobstop.end does) and reports which of
# the job's processes (this user's, with the job folder as their cwd, in their command line or in their
# environment) are still left. A folder outside the project root, inside another tt-project project's
# folder, or with a process of another project is refused; nothing runs then. One exception: a launcher
# that keeps its jobs in the user's own state folder (say ~/.local/state/<tool>) registers an absolute
# glob (`~` allowed) for them; a folder outside the project root is accepted only when such a glob
# matches it, it lies strictly inside this user's home (symlinks followed), below the glob's literal
# prefix, which itself lies strictly inside the home, and it and every folder up to the home are this
# user's (_outside_root). Another user's files, system paths and the home folder itself stay refused.
CONFIG_KEY = "detach.launcher_stops"
CMD_TIMEOUT_S = 300.0
GONE_WAIT_S = 15.0
PROCS_MAX = 256


def _glob_parts(pat: list[str], parts: list[str]) -> bool:
    import fnmatch
    if not pat:
        return not parts
    if pat[0] == "**":
        return any(_glob_parts(pat[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pat[0]) and _glob_parts(pat[1:], parts[1:])


def registered(stops, folder: Path, root: Path) -> tuple[str, list[str]] | None:
    """(glob, command argv) of the first `detach.launcher_stops` entry whose glob matches the job folder,
    else None (also for a malformed entry, which is skipped)."""
    import shlex
    if not isinstance(stops, dict):
        return None
    try:
        rel = folder.relative_to(root).parts
    except ValueError:
        rel = None
    for glob, cmd in stops.items():
        if not isinstance(glob, str) or not glob.strip():
            continue
        g = Path(glob).expanduser()
        if g.is_absolute():
            hit = any(_glob_parts(list(x[1:]), list(folder.parts[1:])) for x in (g.parts, _resolved_glob(g)))
        else:
            hit = rel is not None and _glob_parts([x for x in g.parts if x != "."], list(rel))
        if not hit:
            continue
        try:
            argv = shlex.split(cmd) if isinstance(cmd, str) else [str(x) for x in cmd] if isinstance(cmd, list) else []
        except ValueError:
            argv = []
        if argv:
            return glob, argv
    return None


_WILD = ("*", "?", "[")


def _resolved_glob(g: Path) -> tuple[str, ...]:
    """The parts of an absolute glob with its literal prefix (the levels before the first wildcard)
    resolved, symlinks followed, as the job folder it is matched against is."""
    n = next((i for i, x in enumerate(g.parts) if any(c in x for c in _WILD)), len(g.parts))
    return (*Path(*g.parts[:n]).resolve().parts, *g.parts[n:])


def _outside_root(folder: Path, glob: str | None, project: Path) -> str | None:
    """Why a job folder outside the project root may not be stopped, else None. Only a folder an absolute
    registered glob matched (folder already resolved), strictly inside this user's home and below the
    glob's literal prefix, itself strictly inside the home, with it and every folder up to the home this
    user's, and inside no other tt-project project's folder."""
    if not glob or not Path(glob).expanduser().is_absolute():
        return "no absolute glob in " + CONFIG_KEY + " names it"
    home = Path(os.path.expanduser("~")).resolve()
    if home == Path(home.anchor):
        return f"the home folder is {home}"
    if home not in folder.parents:
        return f"it is not inside your home folder {home}"
    g = _resolved_glob(Path(glob).expanduser())
    prefix = Path(*g[:next((i for i, x in enumerate(g) if any(c in x for c in _WILD)), len(g))])
    if home not in prefix.parents:
        return f"its glob {glob!r} reaches {prefix}, not a folder strictly inside your home {home}"
    if prefix != folder and prefix not in folder.parents:
        return f"it is not below its glob's folder {prefix}"
    uid = os.getuid()
    for a in (folder, *folder.parents):
        try:
            owner = a.stat().st_uid
        except OSError as e:
            return f"{a} cannot be read ({e.strerror})"
        if owner != uid:
            return f"{a} is not yours (owner uid {owner})"
        if a == home:
            break
        if a != project and (a / "state" / "project.db").is_file():
            return f"it is inside another tt-project project ({a})"
        if (a / FOLDER / "state" / "project.db").is_file():
            return f"it is inside another tt-project project ({a / FOLDER})"
    return None


def foreign(folder: Path, root: Path, project: Path, glob: str | None = None) -> str | None:
    """Why this job folder is not this project's to stop, else None. glob: the registered glob that
    matched it (registered()), which lets a folder outside the project root in (_outside_root)."""
    if folder != root and root not in folder.parents:
        if why := _outside_root(folder, glob, project):
            return f"{folder} is outside the project root {root}, and {why}"
        return None
    own = (project / "state", project / "state.noindex")      # state.noindex: macOS (noindex.py)
    if folder in (root, project, *own, *(d / "runs" for d in own)):
        return f"{folder} is the project's own folder, not a job's"
    for a in (folder, *folder.parents):
        if a == root:
            break
        if a != project and (a / "state" / "project.db").is_file():
            return f"{folder} is inside another tt-project project ({a})"
        if (a / FOLDER / "state" / "project.db").is_file():     # a nested project's own root
            return f"{folder} is inside another tt-project project ({a / FOLDER})"
    return None


def job_procs(folder: Path) -> dict[int, str]:
    """{pid: start token} of this user's processes the job folder names: their cwd is in it, or a word of
    their command line or a value of their environment is that folder or a path in it. Never this
    command or one of its ancestors; at most PROCS_MAX."""
    me, uid, out = _ancestors(), os.getuid(), {}
    f = str(folder)
    inside = lambda s: s == f or s.startswith(f + "/")
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return {}
    for pid in pids:
        if pid in me or len(out) >= PROCS_MAX:
            continue
        st = _stat(pid)
        if st is None or st[1] != uid:
            continue
        hit = False
        try:
            hit = inside(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            pass
        try:
            if not hit:
                hit = any(inside(w.decode(errors="replace")) for w in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"))
            if not hit:
                hit = any(inside(x.split(b"=", 1)[-1].decode(errors="replace"))
                          for x in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"))
        except OSError:
            pass
        if hit and (tok := proc_start(pid)) is not None:
            out[pid] = tok
    return out


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()[:120]
    except OSError:
        return "?"


def registered_stop(p, folder: Path, glob: str, argv: list[str], root: Path, why: str,
                    task: int | None = None) -> dict:
    """Run the registered stop command on the job folder, wait up to GONE_WAIT_S for the job's processes
    to end, and log the stop (detach.signalled's stream and an events row). {"ok", "rc", "left", "text"}:
    ok only when the command exited 0 and none of the job's processes is left. Refused (nothing run, ok
    False, "refused" set) when a process of the job belongs to another tt-project project."""
    import json
    import subprocess
    from .jobstop import EVENT
    from .project import durable_append
    from .detach import SIGNALS
    before = job_procs(folder)
    for pid in before:
        there = _project_of(pid)
        if there and Path(there).resolve() != Path(p.base).resolve():
            return {"ok": False, "refused": True, "rc": None, "left": [],
                    "text": f"pid {pid} of {folder} belongs to another tt-project project ({there}); nothing was run"}
    cmd = [*argv, str(folder)]
    try:
        r = subprocess.run(cmd, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=CMD_TIMEOUT_S)
        rc, tail = r.returncode, (r.stdout + r.stderr).strip()[-400:]
    except subprocess.TimeoutExpired:
        rc, tail = None, f"it ran over {CMD_TIMEOUT_S:g}s and was ended"
    except OSError as e:
        rc, tail = None, f"it could not start: {e}"
    deadline = time.monotonic() + GONE_WAIT_S
    while True:
        left = {pid: tok for pid, tok in {**before, **job_procs(folder)}.items() if _same(pid, tok)}
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    ok = rc == 0 and not left
    name = folder.name
    text = (f"{name}: " + ("stopped" if ok else "NOT stopped") + f" by its registered stop ({CONFIG_KEY} {glob!r}: "
            f"{' '.join(cmd)[:200]}, exit {rc if rc is not None else '-'})"
            + (f"; {len(before)} process(es) of the job ran before, none is left" if ok
               else f"; still running: {', '.join(f'{pid} [{_cmdline(pid)}]' for pid in sorted(left)) or 'none'}")
            + (f"; its output: {tail}" if tail and not ok else ""))
    now = time.time()
    row = {"ts": now, "job": name, "rc": str(folder), "pid": None, "group": False, "signal": "launcher_stop",
           "reason": "ttp detach --stop", "why": why, "by": "ttp", "by_pid": os.getpid(),
           "by_cmd": " ".join(cmd)[:200], "task": task, "exit": rc, "left": sorted(left), "ok": ok}
    try:
        durable_append(p.state / SIGNALS, json.dumps(row) + "\n")
    except OSError:
        pass
    out = {"task": task, "job": name, "folder": str(folder), "glob": glob, "command": cmd, "exit": rc,
           "before": sorted(before), "left": sorted(left), "why": why, "text": text}
    try:
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (now, "daemon", EVENT, f"launcher_stop:{folder}:{now}", "low" if ok else "normal", text,
                json.dumps(out), "handled", task))
    except Exception:   # the audit row is best effort, as detach.signalled's: never the stop's outcome
        pass
    return {"ok": ok, "rc": rc, "left": sorted(left), "text": text}

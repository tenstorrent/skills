# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Local `ttp detach` jobs (cli.cmd_detach): start them, queue them for the daemon, tell their state.

A job writes <name>.log, then <name>.rc with its exit code, in the run's folder. Its wrapper writes
<name>.started the moment it runs and holds <name>.lock (locks.job_lock) while any of its processes
lives, so a job gone without an .rc is told apart from one still running.

A sandbox that runs each command in a PID namespace or a cgroup of its own can end every process the
command started once it returns, a new session or not: the job would die before its first line. As
for `ttp push --detach`, a launcher that finds itself boxed (push._boxed) queues the job under
state/detach-queue, with <name>.queued next to its .rc, and the daemon starts it outside the sandbox
(tend). A job gone without an exit code gets <name>.lost holding why, so its probe and the next run
read the reason, never a bare "gone".

Each request carries an id, which <name>.queued names too. The queue folder's .lock serializes
publishing a request (enqueue), settling one (_settle) and telling whether one is pending (pending):
a newer request that arrives while the daemon starts an older one is never deleted with it, and a
marker whose request is gone (a crash between the two writes) is cleared instead of hiding the result.

A job gets a temp dir of its own (scratch), with TMPDIR, TMP and TEMP pointing at it: the run's own
tmp dir, which a fenced run's environment names, goes when that run ends, while the job still runs
(a compiler then loses its scratch partway through). The wrapper removes it when the job ends; tend
sweeps those of jobs gone without doing so (killed, or the host restarted). The temp dir itself is a
short one (shorttmp), so a socket bound beneath it stays within the AF_UNIX path limit; the job's
record under the state names it in its `path` file. Without a usable short root the record is the
temp dir.

A job started with `ttp detach --resumable` also gets <name>.resume.json: its argv, cwd and
environment (secrets left out, as for a queued job) and how often the daemon relaunched it. When a
host reboot cut it short, the daemon starts the same command again on the next boot (relaunch),
model-free, while its task still waits on it, up to RELAUNCH_CAP times; past that the task wakes as
before. Only a job that is safe to run again from the start opts in.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import locks, machines, shorttmp
from .project import FOLDER, Project, durable_write, nice_level, renice, write_json

QUEUE = "detach-queue"   # under the project's state: <run>-<name>.json per job a sandboxed command queued
SCRATCH = "detach-tmp"   # under the project's state: <run>-<name>/ is a job's temp dir while it runs
QUEUED_S = 900           # a queued job the daemon has not started by then is recorded as lost
RELAUNCH_CAP = 3         # default relaunches of a resumable job after reboots (budget.max_job_relaunches)
FAILED_RC = 4            # `ttp detach --check`: every job ended, but one with an exit code other than 0 or none
CHECK_RE = re.compile(r"\bdetach\s+--check\b")


def failed_check(probe: str, rc) -> bool:
    """Whether a probe's exit says the detached jobs it checks ended and one failed (FAILED_RC), not
    that the probe itself is broken: the daemon then runs the hand-off's on_pass_cmd or wakes the task
    with the job's exit code, never as a passed probe."""
    return rc == FAILED_RC and bool(CHECK_RE.search(probe or ""))
def _checked(probe: str, cwd=None) -> list[Path]:
    """The local .rc paths a probe's `detach --check` names, relative to `cwd` (none for a --host one)."""
    words = shlex.split(probe or "")
    for i, w in enumerate(words):
        if w == "--check":
            out = []
            for a in words[i + 1:]:
                if a.startswith("-") or a in ("&&", "||", ";", "|"):
                    break
                out.append(Path(cwd or ".") / a)
            return out
    return []


def failed_why(probe: str, rc, cwd=None) -> str:
    """How a failed check reads: "job rc=N" (the first failed job's exit code, read from the rc files the
    probe's `detach --check` names, relative to `cwd`), else "probe exit 4": 4 is the probe's, not the job's."""
    try:
        for path in _checked(probe, cwd):
            st, why = state(path)
            if st == "ended" and why != "0":
                return f"job rc={why}"
    except (OSError, ValueError):
        pass
    return f"probe exit {rc}"


# The first line of a failed job's log that names its failure, case-sensitive so ordinary words do not
# match: a Python traceback, any word ending in Error (ValueError, Error:), the word error or ERROR, and
# FAIL or FAILED (pytest, ctest). Lower-case "fail"/"failed" is left out: "0 failed" is a pass.
ERROR_RE = re.compile(r"Traceback \(most recent call last\)|\b\w*Error\b|\berror\b|\bERROR\b|\bFAIL(?:ED)?\b")
ERROR_SCAN_BYTES = 8 * 1024 * 1024   # first_error reads at most this much of a log's start
LOG_EXCERPT_CHARS = 4000             # failed_logs' whole text, every job's together


def first_error(log: Path) -> tuple[int, str] | None:
    """The first line of `log` ERROR_RE matches, with its line number. Raises OSError."""
    from .project import clean_line
    seen = 0
    with open(log, "rb") as f:
        for n, raw in enumerate(f, 1):
            line = raw.decode(errors="replace")
            if ERROR_RE.search(line):
                return n, clean_line(line.rstrip("\n"), 300)
            seen += len(raw)
            if seen >= ERROR_SCAN_BYTES:
                return None
    return None


def failed_logs(probe: str, cwd=None, limit: int = LOG_EXCERPT_CHARS) -> str:
    """For each job a probe's local `detach --check` names that failed or is gone: its <name>.log's last
    20 lines and its first error line (first_error), all within `limit` chars, so a woken run need not
    look for them; one short line for a log that is missing or unreadable. '' when none applies."""
    from .project import file_tail
    try:
        jobs = [rc for rc in _checked(probe, cwd) if (st := state(rc))[0] == "lost"
                or st[0] == "ended" and st[1] != "0"]
    except (OSError, ValueError):
        return ""
    if not jobs:
        return ""
    each = max(limit // len(jobs), 400)
    parts = []
    for rc in jobs:
        log = rc.with_suffix(".log")
        try:
            hit = first_error(log)
            tail = file_tail(log, 20, max(each - 450, 200), 200)
        except FileNotFoundError:
            parts.append(f"{log.name}: no log at {log}")
            continue
        except OSError as e:
            parts.append(f"{log.name}: its log could not be read ({type(e).__name__}: {e.strerror or e})"[:300])
            continue
        first = (f"first error line (line {hit[0]}): {hit[1]}" if hit
                 else "no line matches Traceback, *Error, error, ERROR or FAIL")
        parts.append(f"{log.name} ({log}), last lines:\n{tail or '(empty)'}\n{first}")
    text = "\n".join(parts)
    return text if len(text) <= limit else text[:limit - 1] + "…"


# Environment a queued job does not take from the run that queued it: the run's sandbox markers, and
# secrets, which stay off disk (the daemon's own environment supplies them).
_SKIP_ENV = ("TTP_PIDNS", "TTP_CGROUP")
_SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|_KEY$|AUTH", re.I)
# With TTP_ACTIVITY_LOG and TTP_ACTIVITY_ID set (unset before the job runs), the job's end is appended
# to that project's activity log (activity.py).
WRAPPER = ('o="$1" r="$2" s="$3" t="$4" k="$5"; shift 5; a=${TTP_ACTIVITY_LOG-} i=${TTP_ACTIVITY_ID-}; '
           'unset TTP_ACTIVITY_LOG TTP_ACTIVITY_ID; echo $$ >"$s"; "$@" >"$o" 2>&1 </dev/null; c=$?; '
           'rm -rf "$t" "$k"; echo $c >"$r.tmp"; mv "$r.tmp" "$r"; [ -z "$a" ] || '
           'printf \'{"end": %s, "ev": "job_end", "id": "%s", "rc": %s}\\n\' "$(date +%s)" "$i" "$c" >>"$a" 2>/dev/null')
_TMP_VARS = ("TMPDIR", "TMP", "TEMP")


def started(rc: Path) -> Path:
    return Path(rc).with_suffix(".started")


def queued(rc: Path) -> Path:
    return Path(rc).with_suffix(".queued")


def lost(rc: Path) -> Path:
    return Path(rc).with_suffix(".lost")


def resume_file(rc: Path) -> Path:
    return Path(rc).with_suffix(".resume.json")


def scratch(rc: Path) -> Path:
    """The record of the job's own temp dir, under the project's state for a job of a run (keyed by
    run and job name, so tend can tell whose it is), else next to its .rc."""
    rc = Path(rc)
    if rc.parent.parent.name == "runs":
        return rc.parent.parent.parent / SCRATCH / f"{rc.parent.name}-{rc.stem}"
    return rc.with_suffix(".tmp")


def tmp_dir(record: Path) -> Path:
    """The temp dir a job's record names, else the record itself."""
    try:
        named = (Path(record) / "path").read_text().strip()
    except OSError:
        return Path(record)
    return Path(named) if shorttmp.owned(named) else Path(record)


def _drop(record: Path) -> None:
    shorttmp.remove(tmp_dir(record))
    shutil.rmtree(record, ignore_errors=True)


def start(rc: Path, log: Path, cmd: list, cwd: str, env: dict, lock) -> int:
    """Start the job's wrapper in a session of its own, holding `lock` (its job lock, which the
    caller took and closes after), with a fresh temp dir of its own. Returns its pid."""
    record = scratch(rc)
    if record.exists() and not record.is_symlink():   # left by an earlier start of it that never ran
        _drop(record)
    record.mkdir(mode=0o700, parents=True)
    tmp = shorttmp.make() or record
    if tmp != record:
        try:
            durable_write(record / "path", f"{tmp}\n")
        except OSError:   # unrecorded, nothing would remove it: use the record itself
            shorttmp.remove(tmp)
            tmp = record
    env = {**env, **{var: str(tmp) for var in _TMP_VARS}, "TTP_DETACHED": "1"}
    proc = subprocess.Popen(["/bin/sh", "-c", WRAPPER, "sh", str(log), str(rc), str(started(rc)), str(tmp),
                             str(record), *cmd],
                            cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            pass_fds=(lock.fileno(),))
    _children.append(proc)
    return proc.pid


# Wrappers this process started (start): a long-lived one, the daemon (tend, relaunch), reaps each that
# ended on its next tick (reap_children), so none stays a zombie with the daemon as its parent.
_children: list = []


def reap_children() -> int:
    """Reap the started wrappers that ended (non-blocking); how many."""
    left = [c for c in _children if c.poll() is None]
    n = len(_children) - len(left)
    _children[:] = left
    return n


def enqueue(state: Path, rc: Path, log: Path, cmd: list, cwd: str, env: dict, *, checks: dict | None = None) -> Path:
    """Queue the job for the daemon to start, replacing a request of the same job not started yet;
    returns the queue file. The request and its marker are published together, under the lock."""
    rc = Path(rc)
    q = Path(state) / QUEUE / f"{rc.parent.name}-{rc.stem}.json"
    keep = _kept_env(env)
    rid = uuid.uuid4().hex
    with _locked(q.parent):
        write_json(q, {"rc": str(rc), "log": str(log), "command": cmd, "cwd": cwd, "env": keep, "id": rid,
                       "queued": time.time(), **({"checks": checks} if checks is not None else {})}, mode=0o600)
        durable_write(queued(rc), f"{q}\n{rid}\n")
    return q


def _kept_env(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in _SKIP_ENV and not _SECRET_ENV.search(k)}


def save_resume(rc: Path, log: Path, cmd: list, cwd: str, env: dict, boot: str) -> None:
    """Record what `relaunch` needs to start a resumable job again: written before the job starts."""
    write_json(resume_file(rc), {"command": list(cmd), "cwd": cwd, "env": _kept_env(env), "log": str(log),
                                 "boot": boot, "relaunches": 0}, mode=0o600)


def read_resume(rc: Path) -> dict | None:
    """The job's resume record, or None for a job that did not opt in (or a malformed record)."""
    try:
        m = json.loads(resume_file(rc).read_text())
    except (OSError, ValueError):
        return None
    ok = isinstance(m, dict) and isinstance(m.get("command"), list) and m["command"] and \
        isinstance(m.get("cwd"), str) and isinstance(m.get("env") or {}, dict)
    return m if ok else None


def relaunch(rc: Path, boot: str, nice: int = 0) -> tuple[str, int]:
    """Start a resumable job a reboot cut short again, with the command, cwd and environment it was
    started with (secrets from the caller's own environment). Its old log is kept as <name>.log.<n>.
    Returns ("", pid) once started, else (why not, 0)."""
    rc = Path(rc)
    m = read_resume(rc)
    if m is None:
        return "it has no resume record", 0
    if rc.exists():
        return "it already ended", 0
    lock = locks.try_take([locks.job_lock(rc)], f"detached job {rc.stem}", " ".join(map(str, m["command"])))
    if lock is None:
        return "it is still running", 0
    try:
        # Counted on this boot already, by a daemon that died before starting it: counted once.
        again = m.get("boot") == boot and bool(m.get("relaunches"))
        n = int(m.get("relaunches") or 0) + (0 if again else 1)
        log = Path(m.get("log") or rc.with_suffix(".log"))
        if log.exists():
            log.replace(log.with_name(f"{log.name}.{n}"))
        lost(rc).unlink(missing_ok=True)
        started(rc).unlink(missing_ok=True)
        # Counted before it starts: a crash here never relaunches it past the cap.
        if not again:
            write_json(resume_file(rc), {**m, "boot": boot, "cut_boot": m.get("boot"), "relaunches": n,
                                         "relaunched": time.time()}, mode=0o600)
        pid = start(rc, log, [str(c) for c in m["command"]], m["cwd"],
                    machines.mask_devices({**os.environ, **(m.get("env") or {})}), lock)
        # Before the lock goes: a daemon that dies after this still finds the job to stop
        # (running_pid; the wrapper's <name>.started covers the instant before).
        _restamp(rc, pid, boot)
    except (OSError, ValueError, TypeError) as e:
        return f"it could not start: {e}", 0
    finally:
        lock.close()
    renice(pid, nice)
    return "", pid


def relaunch_started(rc: Path) -> bool:
    """Whether the job `relaunch` counted on this boot started (it clears <name>.started and <name>.rc
    is absent before it starts): it runs, or its wrapper ran. False after a crash between counting
    it and starting it."""
    rc = Path(rc)
    return rc.exists() or started(rc).exists() or not locks.any_free([locks.job_lock(rc)])


def stop(rc: Path, pid: int, why: str) -> None:
    """End a job `relaunch` just started (pid, its wrapper, leads a session and process group of its
    own) before it writes an exit code, and record why in <name>.lost: its probe then reads it ended.
    The why goes first: a reader that sees its lock go free before it would record a guess instead."""
    _mark_lost(Path(rc), why)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass   # already gone


def running_pid(rc: Path, this_boot) -> int:
    """The pid of a job relaunched on this boot (its registry entry names a boot `this_boot` accepts,
    else its resume record does and <name>.started names it) that still runs (holds its lock), else 0:
    the one `stop` may end."""
    rc = Path(rc)
    if locks.any_free([locks.job_lock(rc)]):
        return 0
    j = _entry(rc)
    pid = j.get("pid") if this_boot(j.get("boot")) else None
    if not (isinstance(pid, int) and pid > 1) and this_boot((read_resume(rc) or {}).get("boot")):
        # A daemon that died between starting it and restamping the registry: the wrapper's own pid.
        try:
            pid = int(started(rc).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pid = None
    return pid if isinstance(pid, int) and pid > 1 else 0


def _restamp(rc: Path, pid: int, boot: str) -> None:
    """Point the run's registry entry of the job at its new process and boot (state reads the boot);
    a relaunched job's record is open again."""
    try:
        with registry(rc.parent) as jobs:
            for j in jobs:
                if isinstance(j, dict) and j.get("rc") == str(rc):
                    j.update(pid=pid, boot=boot, started=time.time(), queued=False, status="running")
                    for k in ("exit", "ended", "why"):
                        j.pop(k, None)
    except OSError:
        pass


# A run's detached.json records each local job `ttp detach` started (cli.cmd_detach): its start fields
# and status "running" (or "queued"), and once it is over status "ended" with its exit code (exit, None
# when the .rc holds no number) and end time (the .rc's), or "died" (exit None) with why and when it was
# found gone. The daemon closes them each tick (close_records), `ttp detach --reap` closes one at once;
# both by state(), which reads liveness from the job's lock, never its pid, so a pid reused by another
# process never keeps a record open and a driver still alive (any of its processes) is never closed.
# Every write goes through registry(), one flock per run, so a close and a new job's entry never race.
REGISTRY = "detached.json"
CLOSED = ("ended", "died")
CLOSE_DAYS = 14   # the runs whose records the daemon's sweep looks at
CLOSE_EVERY_S = 60


@contextmanager
def registry(run_dir: Path):
    """The run's detached.json entries as a list to change in place, written back (durably) if changed."""
    reg = Path(run_dir) / REGISTRY
    with open(reg.with_name(REGISTRY + ".flock"), "a") as f:   # never <name>.lock: a job's own lock
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            jobs = json.loads(reg.read_text())
        except (OSError, ValueError):
            jobs = []
        jobs = jobs if isinstance(jobs, list) else []
        before = json.dumps(jobs)
        yield jobs
        if json.dumps(jobs) != before:
            durable_write(reg, json.dumps(jobs, indent=1))


def close(rc: Path, now: float | None = None) -> dict | None:
    """Close the record of the job at this .rc once state() says it is over: "ended" once its .rc
    exists, "died" once it is gone without one. The record (closed now or before: idempotent), or None
    while the job runs or is queued, or when no record names it."""
    rc = Path(rc)
    e = _entry(rc)
    if not e or e.get("status") in CLOSED:
        return e or None
    st, why = state(rc)
    if st not in ("ended", "lost"):
        return None
    if st == "ended":
        try:
            end = rc.stat().st_mtime
        except OSError:
            end = time.time() if now is None else now
        upd = {"status": "ended", "exit": int(why) if re.fullmatch(r"-?[0-9]+", why) else None, "ended": end}
    else:
        try:
            end = lost(rc).stat().st_mtime
        except OSError:
            end = time.time() if now is None else now
        upd = {"status": "died", "exit": None, "ended": end, "why": why}
    out = None
    with registry(rc.parent) as jobs:
        for j in jobs:
            if isinstance(j, dict) and j.get("rc") == str(rc):
                if j.get("status") not in CLOSED:
                    j.update(upd)
                out = j
    return out


def close_records(p: Project, now: float | None = None) -> int:
    """The daemon's sweep (tend, at most once a minute): close every open record of the runs of the last
    CLOSE_DAYS days whose job is over. How many it closed."""
    now = time.time() if now is None else now
    mark = p.state / "detach-closed.at"
    try:
        if now - mark.stat().st_mtime < CLOSE_EVERY_S:
            return 0
    except OSError:
        pass
    mark.touch()
    n = 0
    for d in sorted(p.runs.glob("*/" + REGISTRY)) if p.runs.is_dir() else []:
        try:
            if now - d.stat().st_mtime > CLOSE_DAYS * 86400:
                continue
            jobs = json.loads(d.read_text())
        except (OSError, ValueError):
            continue
        for j in jobs if isinstance(jobs, list) else []:
            if isinstance(j, dict) and j.get("rc") and j.get("status") not in CLOSED:
                try:
                    n += bool(close(Path(j["rc"]), now))
                except OSError:
                    continue
    return n


_held = threading.local()


@contextmanager
def _locked(folder: Path):
    """Hold the queue folder's lock (briefly: never while a job starts). Re-entrant within a thread,
    so a nested take never deadlocks on the thread's own lock."""
    key = str(folder)
    held = _held.__dict__.setdefault("folders", set())
    if key in held:
        yield
        return
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)


def _marker(rc: Path) -> tuple[Path | None, str | None]:
    """The queue file and request id <name>.queued names; (None, None) without one."""
    try:
        lines = queued(rc).read_text().split("\n")
    except OSError:
        return None, None
    return (Path(lines[0].strip()) if lines[0].strip() else None), ((lines[1].strip() if len(lines) > 1 else "") or None)


def _request_id(q: Path) -> str | None:
    try:
        m = json.loads(q.read_text())
    except (OSError, ValueError):
        return None
    return m.get("id") if isinstance(m, dict) else None


def pending(rc: Path) -> bool:
    """Whether a queued request of this job waits for the daemon (or is being started). A marker
    whose queue file is gone is an orphan (its request was consumed or never published): it is
    removed, so the job's own result answers again."""
    rc = Path(rc)
    q, _ = _marker(rc)
    if q is None:
        return False
    try:
        with _locked(q.parent):
            q, _ = _marker(rc)
            if q is None:
                return False
            if q.exists():
                return True
            queued(rc).unlink(missing_ok=True)
            return False
    except OSError:
        return q.exists()


def withdraw(rc: Path) -> None:
    """Drop the job's pending request (a foreground launch replaces it)."""
    q, _ = _marker(rc)
    if q is None:
        return
    with _locked(q.parent):
        q.unlink(missing_ok=True)
        queued(rc).unlink(missing_ok=True)


def _settle(q: Path, rc: Path, rid: str | None, lost_why: str = "") -> None:
    """Remove the request the daemon consumed, and its marker, only while they still name it: a
    newer request queued meanwhile stays for the next tick. `lost_why` records why it did not start,
    again only for a request not replaced."""
    with _locked(q.parent):
        if q.exists() and _request_id(q) != rid:
            return   # replaced: the newer request and its marker stay
        if lost_why:
            _mark_lost(rc, lost_why)
        q.unlink(missing_ok=True)
        if _marker(rc)[1] == rid:
            queued(rc).unlink(missing_ok=True)


def _markable(rc: Path) -> Path | None:
    """The absolute .rc path when a marker may go next to it: inside a project's state (its runs, the
    daemon's own jobs), the calling run's folder ($TTP_RUN_DIR) or a run folder that records detached
    jobs (detached.json). Else None, with a warning: a relative path a probe names resolves against
    its cwd, and a stray <name>.lost would land in the caller's checkout."""
    p = Path(os.path.abspath(Path(rc).expanduser()))
    real = p.parent.resolve()
    run = os.environ.get("TTP_RUN_DIR")
    up = [real, *real.parents]
    if (any(a.name == "state" and a.parent.name == FOLDER for a in up) or (real / "detached.json").is_file()
            or (run and Path(run).resolve() in up)):
        return p
    print(f"ttp detach: not marking {p.with_suffix('.lost')}: outside the project state and run folders",
          file=sys.stderr)
    return None


def _mark_lost(rc: Path, why: str) -> None:
    rc = _markable(rc)
    if rc is None:
        return
    try:
        durable_write(lost(rc), why + "\n")
    except OSError:
        pass


def _mark_lost_first(rc: Path, why: str) -> str:
    """Record `why` in <name>.lost unless a reason is already there, and return the one that stands:
    a guess made from the outside never replaces a verdict written meanwhile (stop, remote_verdict)."""
    rc = _markable(rc)
    if rc is None:
        return why
    target = lost(rc)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        durable_write(tmp, why + "\n")
        os.link(tmp, target)   # atomic create-if-absent: never half-written, never over another
        return why
    except FileExistsError:
        try:
            return target.read_text().strip() or why
        except OSError:
            return why
    except OSError:   # no hard links here: the plain write, as before
        if not target.exists():
            _mark_lost(rc, why)
        return why
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def tend(p: Project) -> None:
    """The daemon's part, each tick: start the queued jobs, and record as lost those queued too long
    ago or that cannot start. The .lost goes before the queue entry, so a probe never sees neither."""
    # Completion normally removes scratch; SIGKILL, a launcher crash or a reboot cannot.
    # The inherited lifetime lock protects live checks across daemon restarts and pid namespaces.
    from . import checkdirs, checkreap
    reap_children()
    checkreap.sweep(p.state)   # first, so the scratch whose lock a leftover held can go below
    checkdirs.recover(p.state, p.runs)
    sweep(p.state)
    close_records(p)
    folder = p.state / QUEUE
    for q in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            m = json.loads(q.read_text())
            rc, log = Path(m["rc"]), Path(m["log"])
        except (OSError, ValueError, KeyError, TypeError):
            q.unlink(missing_ok=True)
            continue
        rid, why = m.get("id"), ""
        if "checks" in m:
            from .cli import _read_checks_pid
            # A restart after launch but before settlement must not expire checks already
            # started, even when the daemon was down longer than QUEUED_S.
            if rid and _read_checks_pid(rc.parent).get("req") == rid:
                _settle(q, rc, rid)
                continue
        elif _superseded(rc, m):
            _settle(q, rc, rid)
            continue
        if time.time() - float(m.get("queued") or 0) > QUEUED_S:
            why = (f"the daemon did not start it within {QUEUED_S // 60} min of it being queued "
                   f"(was the daemon down?)")
        elif "checks" in m:
            # Replacement checks must cancel their earlier host pid here, never in the tool's
            # PID namespace. An earlier checks.rc is removed only after that process ends. The
            # request stays queued while they start, so a crash here starts them again.
            from argparse import Namespace
            from .cli import _checks_start
            try:
                c = m["checks"]
                lost(rc).unlink(missing_ok=True)
                pid = _checks_start(Namespace(cmd=c["cmd"], fresh=c["fresh"], full=c.get("full", False),
                                              purpose=c.get("purpose")), rc.parent, c["worktree"],
                                    c["head"], machines.mask_devices({**os.environ, **(m.get("env") or {})}), rid or "")
                renice(pid, nice_level(p.config().get("runner"))[0])
            except (OSError, ValueError, TypeError, KeyError) as e:
                why = f"the daemon could not start the checks: {e}"
        elif not (lost(rc).exists() or rc.exists()):
            lock = locks.try_take([locks.job_lock(rc)], f"detached job {rc.stem}", " ".join(m["command"]))
            if lock is not None:   # else it already runs
                try:
                    pid = start(rc, log, list(m["command"]), m["cwd"], machines.mask_devices({**os.environ, **(m.get("env") or {})}), lock)
                    renice(pid, nice_level(p.config().get("runner"))[0])
                except (OSError, ValueError, TypeError) as e:   # its folder went, say: never retried each tick
                    why = f"the daemon could not start it: {e}"
                finally:
                    lock.close()
        _settle(q, rc, rid, why)


def _superseded(rc: Path, m: dict) -> bool:
    """Whether a queued job already runs, or the daemon relaunched it since it was queued (a
    --resumable job queued before a reboot): its request is spent, never a lost job, however old."""
    r = read_resume(rc)
    if r is not None and float(r.get("relaunched") or 0) >= float(m.get("queued") or 0) > 0:
        return True
    return locks.job_lock(rc).exists() and not locks.any_free([locks.job_lock(rc)])


def sweep(state: Path) -> None:
    """Remove the temp dirs of jobs that ended or are gone: the wrapper removes its own when the job
    ends, but not when it is killed or the host restarts. A job that still runs holds its lock."""
    folder = Path(state) / SCRATCH
    for d in sorted(folder.iterdir()) if folder.is_dir() else []:
        m = re.fullmatch(r"([0-9]+)-([A-Za-z0-9._-]{1,64})", d.name)
        if not m or d.is_symlink() or not d.is_dir():
            continue
        rc = Path(state) / "runs" / m[1] / f"{m[2]}.rc"
        if rc.exists() or lost(rc).exists() or locks.any_free([locks.job_lock(rc)]):
            _drop(d)


def _entry(rc: Path) -> dict:
    try:
        jobs = json.loads((rc.parent / "detached.json").read_text())
    except (OSError, ValueError):
        return {}
    return next((j for j in jobs if isinstance(j, dict) and j.get("rc") == str(rc)), {}) \
        if isinstance(jobs, list) else {}


def recorded(rc: Path) -> bool:
    """Whether anything records a detached job at this .rc path: its exit code, its loss, its start,
    its entry in detached.json, a queued request or its held lock. False for a path no `ttp detach`
    wrote, which state() would call lost (and mark so)."""
    rc = Path(rc)
    return (rc.exists() or lost(rc).exists() or started(rc).exists() or bool(_entry(rc)) or pending(rc)
            or not locks.any_free([locks.job_lock(rc)]))


def series(rc: Path) -> tuple[str, str] | None:
    """A series driver's own dir at this path less its .rc (<dir>/pid, optional <dir>/EXIT holding
    its exit code), as state() reads a job: ("ended", code, "?" when EXIT holds none) once EXIT
    exists, ("running", "") while the pid lives, ("lost", why) once it is dead. None for no such dir."""
    rc = Path(rc).expanduser()
    d = rc.with_suffix("") if rc.suffix == ".rc" else rc
    if not d.is_dir():
        return None
    if (d / "EXIT").exists():
        try:
            code = "".join((d / "EXIT").read_text(errors="replace").split())[:16]
        except OSError:
            code = ""
        return "ended", code if code.isdigit() else "?"
    try:
        pid = int((d / "pid").read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "lost", f"series driver pid {pid} is dead with no EXIT; see {d / 'log'}"
    except PermissionError:
        pass
    return "running", ""


def state(rc: Path) -> tuple[str, str]:
    """("ended", exit code), ("running" or "queued", ""), or ("lost", why). The first look at a lost
    job records why in <name>.lost."""
    rc = Path(rc)
    for _ in range(2):   # a job that ends between the looks wrote its .rc before its lock went free
        if rc.exists():
            return "ended", rc.read_text().strip() or "?"
        if lost(rc).exists():
            return "lost", lost(rc).read_text().strip() or "no reason recorded"
        if pending(rc):
            return "queued", ""
        if not locks.any_free([locks.job_lock(rc)]):
            return "running", ""
    from .runner import boot_id, same_boot
    boot = _entry(rc).get("boot")
    known = bool(boot) and "unknown" not in (boot, boot_id())   # a boot id recorded at launch and readable now
    if known and not same_boot(boot):
        why = "the driver died with the host reboot: the host restarted while it ran (its boot id changed)"
    elif not started(rc).exists():
        why = ("it never started: it was killed with the command that ran `ttp detach` (a sandbox or tool "
               "that ends the processes a command starts)")
    elif known:
        why = "it was killed after it started, before it wrote an exit code (same boot: it crashed or was killed)"
    else:
        why = ("it was killed after it started, before it wrote an exit code, or the host restarted: the boot "
               "id could not be read, so which is unknown")
    return "lost", _mark_lost_first(rc, why)


# A local job that drives a job on another host itself (`ttp detach <name> --remote-host <alias>
# [--remote-pid <pid or remote file holding it>] [--remote-marker <remote path>] -- <command...>`) records
# that host's boot id at start in its detached.json entry ("remote"). The keeper of an exclusive run's
# hold (hold.watch) asks the host now and then, read-only and bounded, whether the remote side still
# lives: a changed boot id (the host reset) or a remote pid gone with no completion marker ends the
# local job (stop) with that verdict in <name>.lost, so its hold goes before the job's own deadline.
# A host it cannot reach proves nothing: the job and its hold stay. Run by POSIX sh over ssh, with the
# pid (or the remote file holding it) as $1 and the marker as $2, either "" when not given.
REMOTE_TIMEOUT_S = 30
GONE_CONFIRM = 2   # probes in a row that read the remote pid dead with no marker before the job is ended
_REMOTE_LIVE = r'''pid=$1 m=$2
case $pid in "~/"*) pid=$HOME${pid#\~} ;; esac
case $m in "~/"*) m=$HOME${m#\~} ;; esac
%s
echo "boot $b"
if [ -n "$m" ] && [ -e "$m" ]; then echo "marker"; fi
case $pid in
  ""|*[!0-9]*) if [ -n "$pid" ] && [ -s "$pid" ]; then pid=$(tr -cd 0-9 <"$pid" | head -c 12); else pid=; fi ;;
esac
if [ -n "$pid" ] && [ "$pid" -gt 1 ]; then
  if kill -0 "$pid" 2>/dev/null || ps -p "$pid" >/dev/null 2>&1; then echo "alive $pid"; else echo "dead $pid"; fi
fi
echo "end"
'''


def _remote_sh(host: str, pid: str = "", marker: str = "") -> dict | None:
    """What the remote host says of a job's remote side: {"boot", "marker" (bool), "alive" / "dead"
    (the pid)}; None when ssh fails, times out or the answer is cut short."""
    from .cli import REMOTE_BOOT_SH
    script = _REMOTE_LIVE % REMOTE_BOOT_SH
    remote = "sh -c " + " ".join(shlex.quote(x) for x in (script, "sh", pid or "", marker or ""))
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={REMOTE_TIMEOUT_S // 2}",
                            host, remote], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=REMOTE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.splitlines()
    if r.returncode != 0 or "end" not in lines:
        return None
    out: dict = {"marker": "marker" in lines}
    for ln in lines:
        key, _, val = ln.partition(" ")
        if key in ("boot", "alive", "dead"):
            out[key] = val.strip()
    return out


def remote_start(host: str) -> str:
    """The boot id of the host a local job drives, read as it starts; "" when the host does not answer."""
    return (_remote_sh(host) or {}).get("boot") or ""


def remote_verdict(rc: Path, memo: dict) -> str | None:
    """Why a local job's remote side is dead (its host reset, or its remote pid is gone with no completion
    marker on GONE_CONFIRM probes in a row), else None: alive, finished (its marker is there), no remote
    contract, or the host did not answer. `memo` keeps what earlier probes of this job learned."""
    c = _entry(Path(rc)).get("remote")
    if not isinstance(c, dict) or not c.get("host"):
        return None
    host = str(c["host"])
    seen = _remote_sh(host, str(c.get("pid") or ""), str(c.get("marker") or ""))
    if seen is None:
        return None   # unreachable is no proof of death: keep it until its deadline
    if seen["marker"]:
        memo.pop("dead", None)
        return None
    boot = c.get("boot") or memo.get("boot")
    if not boot and seen.get("boot"):
        memo["boot"] = seen["boot"]   # unreachable at start: the first answer names its boot
    elif boot and seen.get("boot") and seen["boot"] != boot:
        return f"remote reset: {host} restarted (its boot id changed) while the job ran"
    if not seen.get("dead"):
        memo.pop("dead", None)
        return None
    memo["dead"] = memo.get("dead", 0) + 1
    if memo["dead"] < GONE_CONFIRM:
        return None
    mark = f" and its completion marker {c['marker']} is missing" if c.get("marker") else ""
    return f"remote driver gone: pid {seen['dead']} on {host} is dead{mark}"


def end_remote(rc: Path, why: str) -> None:
    """End a local job whose remote side is dead: kill its wrapper's process group and record why."""
    rc = Path(rc)
    pid = _entry(rc).get("pid")
    if not (isinstance(pid, int) and pid > 1):
        try:
            pid = int(started(rc).read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pid = 0
    if pid > 1 and not locks.any_free([locks.job_lock(rc)]):
        stop(rc, pid, why)
    else:
        _mark_lost(rc, why)


# A job started with `ttp detach --remote` keeps its .log and .rc on that host, so a host that stops
# answering later (it reset, or its network went) would hide a result it already had. Its probe,
# `ttp detach --check --host <alias> --mirror <run folder> <dir>/<name>`, copies what each answer says
# into that folder: <name>.remote.log (the last MIRROR_TAIL_BYTES of its log, sent only when the log's
# size changed), <name>.remote.json (host, remote path, log size, time, and the end once known) and, once
# it ended, <name>.remote.rc with its exit code. When ssh then fails, the probe reads the mirrored end
# (exit 0 or 4) instead of 255. An exclusive run's hold keeper refreshes the run's mirrors as it probes
# (mirror_refresh). Run by POSIX sh over ssh before the check, with the job's path as $1 and the log
# size already mirrored as $3.
MIRROR_TAIL_BYTES = 64 * 1024
_REMOTE_TAIL = r'''q=$1 z=$3
case $q in "~"|"~/"*) q=$HOME${q#\~} ;; esac
q=${q%%/}; q=${q%%.rc}
l=$q.log; [ -f "$l" ] || l=$q/log
if [ -f "$l" ]; then
  s=$(wc -c <"$l" | tr -d " ")
  if [ "$s" = "$z" ]; then echo "tail same $s"
  elif command -v base64 >/dev/null 2>&1; then echo "tail $s"; tail -c %d "$l" | base64; echo "tail-end"
  fi
fi
'''


def mirror_name(pre: str) -> str:
    """A remote job's name from its path, <dir>/<name> or <dir>/<name>.rc."""
    return Path(pre[:-3] if pre.endswith(".rc") else pre.rstrip("/")).name


def mirror_read(d, name: str, host: str, pre: str) -> dict | None:
    """The mirror of this remote job in folder `d`, None when there is none or it is another job's."""
    try:
        m = json.loads((Path(d) / f"{name}.remote.json").read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) and m.get("host") == host and m.get("path") == pre else None


def mirror_save(d, name: str, host: str, pre: str, *, tail: bytes | None = None, size=None,
                end: dict | None = None) -> None:
    """Record what a remote job's answer said in folder `d`: its log tail and size, and its end
    ({"rc": code} or {"gone": why}), which is kept once known."""
    d = Path(d)
    m = mirror_read(d, name, host, pre) or {"host": host, "path": pre}
    if tail is not None:
        durable_write(d / f"{name}.remote.log", tail[-MIRROR_TAIL_BYTES:])
    if size is not None:
        m["size"] = str(size)
    if end and not m.get("end"):
        m["end"] = end
        if "rc" in end:
            durable_write(d / f"{name}.remote.rc", f"{end['rc']}\n")
    m["at"] = int(time.time())
    write_json(d / f"{name}.remote.json", m)


def _split_tail(out: str) -> tuple[list[str], tuple | None]:
    """The answer's lines less its tail block, and the tail: (size, bytes), (size, None) when the log
    is unchanged, None when the answer has none."""
    import base64
    lines, tail, i = out.splitlines(), None, 0
    rest: list[str] = []
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("tail same "):
            tail = (ln.split()[-1], None)
        elif ln.startswith("tail ") and "tail-end" in lines[i + 1:]:
            j = lines.index("tail-end", i + 1)
            try:
                tail = (ln.split()[-1], base64.b64decode("".join(lines[i + 1:j])))
            except ValueError:
                tail = None
            i = j
        elif ln.strip():
            rest.append(ln)
        i += 1
    return rest, tail


def mirror_probe(host: str, pre: str, d=None, rows: str | None = None):
    """One ssh call for a remote job: the rows count (with `rows`), its log tail when mirroring into
    folder `d`, and the check's verdict (cli._REMOTE_CHECK). (r, lines): r is None or exits 255 when
    ssh failed; lines are its answer less the tail, which is mirrored into `d` already."""
    from .cli import _REMOTE_CHECK, _REMOTE_ROWS, _ssh_sh
    name = mirror_name(pre)
    seen = (mirror_read(d, name, host, pre) or {}).get("size", "") if d else ""
    # With a mirror the verdict is read before the tail and printed after it, so a job that ends between
    # the two cannot leave a tail missing its last lines; the verdict stays the last line.
    check = f'v=$(\n{_REMOTE_CHECK}\n)\n' if d else _REMOTE_CHECK
    script = ((_REMOTE_ROWS if rows is not None else "") + check
              + (_REMOTE_TAIL % MIRROR_TAIL_BYTES + 'printf "%s\\n" "$v"\n' if d else ""))
    r = _ssh_sh(host, script, [pre, rows or "", seen] if d else [pre, rows] if rows is not None else [pre])
    if r is None or r.returncode == 255:
        return r, []
    lines, tail = _split_tail(r.stdout)
    if d and tail:
        try:
            mirror_save(d, name, host, pre, tail=tail[1], size=tail[0])
        except OSError:
            pass
    return r, lines


def mirror_end(answer: str) -> dict | None:
    """The end a check's verdict line names, for the mirror: {"rc": code} or {"gone": word}, else None."""
    word, _, rest = answer.partition(" ")
    if word == "rc":
        return {"rc": rest.strip() or "?"}
    if word in ("gone", "boot", "series"):
        return {"gone": answer.strip()}
    return None


def mirror_refresh(run_dir) -> None:
    """Refresh the mirrors of a run's remote jobs not known to have ended (the hold keeper, now and then)."""
    for meta in sorted(Path(run_dir).glob("*.remote.json")):
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(m, dict) or m.get("end") or not m.get("host") or not m.get("path"):
            continue
        r, lines = mirror_probe(str(m["host"]), str(m["path"]), run_dir)
        if r is None or r.returncode or not lines:
            continue
        end = mirror_end(lines[-1])
        if end:
            try:
                mirror_save(run_dir, mirror_name(str(m["path"])), str(m["host"]), str(m["path"]), end=end)
            except OSError:
                pass

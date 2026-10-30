"""Runs the tt-project tests (tests/test_tt_project_*.py) in forked worker processes, with no plugin.

Those files hold well over a thousand tests that mostly wait on subprocesses (git, the CLI, the
runner), so one process spends most of its time idle. When a session collects at least
MIN_ITEMS of them, the main process forks TTP_TEST_JOBS workers after collection. The default is
half the CPUs, at most 4, so a laptop stays usable; on CI (the CI variable set) it is one per CPU, at
most 8. Off CI the workers run at a lower priority (nice +10, where the system has it). Each worker takes the next test from a shared queue and runs it with pytest's own
runtest protocol; the main process replays every report through pytest's hooks, so the terminal
output, --durations, the summary and the exit status are those of an ordinary run. All other
collected tests form one unit that a single worker runs in order, as before.

Every collected test still runs exactly once. A worker that dies fails the tests it took and did not
finish. TTP_TEST_JOBS=1 (or -x, --maxfail, --pdb, -s, --stepwise, xdist) runs everything in-process.

A session that runs tt-project tests also fails if, at its end, a process whose command line names
the session's own temp root (basetemp) is still alive: those are processes a test detached and never
reaped. The guard lists them and stops them first, so a failure leaves nothing behind. Only this
session's basetemp counts, so concurrent sessions on the host never trip it. `reap_tmp_processes`
gives a test the same reaping for its own tmp_path.

Every run ends its summary with one line of wall time and CPU time (user + system, of the main
process and every child it waited for: workers and the processes the tests started), e.g.
`ttp-suite: wall 312.4s cpu 1840.2s workers 4`, so a slower or costlier suite shows in push logs. A
full-suite run (MIN_ITEMS or more tt-project tests) also prints its SLOWEST slowest test calls just above
that line (`ttp-suite: slowest 15 test calls:`, then `ttp-suite:   61.20s <test id>` each), so slow or
wall-clock-bound tests can be ranked from any run's log.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

MIN_ITEMS = 50
SLOWEST = 15           # the slowest test calls a full-suite run lists
MAX_AUTO_JOBS = 8      # on CI
MAX_LOCAL_JOBS = 4     # anywhere else: half the CPUs, at most this
WORKER_NICE = 10
_SHARDED = "test_tt_project_"
# tt-project's db.tx() refuses a git network command (ls-remote, fetch, push) started inside a write
# transaction while this is set: every test, and every process it starts, runs under that guard.
os.environ.setdefault("TTP_TX_GUARD", "1")
os.environ.setdefault("TTP_ALLOW_VOLATILE_BASE", "1")   # the suite runs on tmpfs on purpose
os.environ.setdefault("TTP_DEVICE_NODES", "")   # no host device node changes what a test sees; tests set their own


def _jobs(config, items) -> int:
    raw = os.environ.get("TTP_TEST_JOBS", "").strip()
    opt = config.option
    if (not hasattr(os, "fork") or opt.collectonly or getattr(opt, "usepdb", False) or opt.maxfail
            or getattr(opt, "capture", "fd") == "no" or getattr(opt, "stepwise", False)
            or getattr(opt, "stepwise_skip", False) or getattr(opt, "numprocesses", None)
            or os.environ.get("PYTEST_XDIST_WORKER")):
        return 1
    if raw:
        return max(1, int(raw)) if raw.isdigit() else 1
    if sum(1 for i in items if i.path.name.startswith(_SHARDED)) < MIN_ITEMS:
        return 1
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    return _auto_jobs(cpus, os.environ)


def _on_ci(env) -> bool:
    return env.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _auto_jobs(cpus: int, env) -> int:
    """The default worker count: one per CPU up to MAX_AUTO_JOBS on CI, else half the CPUs up to
    MAX_LOCAL_JOBS, so a run on a laptop or a shared host leaves room for everything else; never more
    than the host-wide test slots tt-project granted the run (TTP_TEST_SLOTS). A run that holds its grant
    (TTP_TEST_SLOTS_HELD: `ttp checks`) uses all of it, so a granted slot never sits idle."""
    slots = env.get("TTP_TEST_SLOTS", "").strip()
    cap = int(slots) if slots.isdigit() and int(slots) > 0 else cpus
    if env.get("TTP_TEST_SLOTS_HELD", "").strip().isdigit() and slots.isdigit() and int(slots) > 0:
        return max(1, min(cpus, cap))
    if _on_ci(env):
        return max(1, min(MAX_AUTO_JOBS, cpus, cap))
    return max(1, min(MAX_LOCAL_JOBS, cpus // 2, cap))


def _suite_line(wall: float, cpu: float, workers: int) -> str:
    return f"ttp-suite: wall {wall:.1f}s cpu {cpu:.1f}s workers {workers}"


def _slowest_lines(reports, n: int = SLOWEST) -> list[str]:
    """The `n` slowest test calls of a full-suite run's `reports` (MIN_ITEMS or more tt-project tests),
    slowest first; [] for a smaller run."""
    calls = [r for r in reports if getattr(r, "when", None) == "call" and isinstance(getattr(r, "duration", None), float)]
    if sum(1 for r in calls if Path(r.nodeid.split("::", 1)[0]).name.startswith(_SHARDED)) < MIN_ITEMS:
        return []
    top = sorted(calls, key=lambda r: -r.duration)[:n]
    return [f"ttp-suite: slowest {len(top)} test calls:"] + [f"ttp-suite: {r.duration:8.2f}s {r.nodeid}" for r in top]


@pytest.hookimpl(trylast=True)   # after pytest's own tmpdir plugin made the factory _use_tmpfs patches
def pytest_configure(config):
    config._ttp_started = time.monotonic()
    _use_tmpfs(config)


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, config):
    started = getattr(config, "_ttp_started", None)
    if started is None or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    try:
        import resource
        cpu = sum(r.ru_utime + r.ru_stime for r in (resource.getrusage(resource.RUSAGE_SELF),
                                                     resource.getrusage(resource.RUSAGE_CHILDREN)))
    except ImportError:   # no resource module (Windows): this process's own CPU time only
        cpu = time.process_time()
    for line in _slowest_lines([r for reps in terminalreporter.stats.values() for r in reps]):
        terminalreporter.write_line(line)
    terminalreporter.write_line(_suite_line(time.monotonic() - started, cpu, getattr(config, "_ttp_workers", 1)))


_STARTED = time.time()   # this session's start (reset at sessionstart): older processes are never its leaks


def _seconds(etime: str) -> int | None:
    """`ps` etime ([[dd-]hh:]mm:ss) in seconds, or None."""
    days, _, rest = etime.rpartition("-")
    try:
        total = 0
        for part in rest.split(":"):
            total = total * 60 + int(part)
        return total + int(days or 0) * 86400
    except ValueError:
        return None


def _ps() -> list[tuple[int, int, int, float, str]]:
    """(pid, ppid, pgid, start time, command line) of every process on the host; `ps` as on Linux and
    macOS. The start time is to the second (etime)."""
    # -ww: whole command lines, never cut to the terminal width (COLUMNS) as without a terminal
    r = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,etime=,command="], capture_output=True,
                       text=True, timeout=60)
    now, rows = time.time(), []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        age = _seconds(parts[3]) if len(parts) == 5 else None
        if age is not None and all(x.isdigit() for x in parts[:3]):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), now - age, parts[4]))
    return rows


def _ancestors(rows) -> set[int]:
    """This process and its ancestors."""
    parent = {row[0]: row[1] for row in rows}
    mine, pid = set(), os.getpid()
    while pid > 0 and pid not in mine:
        mine.add(pid)
        pid = parent.get(pid, 0)
    return mine


def leaked_processes(root: str | Path, since: float | None = None) -> list[tuple[int, int, str]]:
    """(pid, pgid, command line) of live processes whose command line names `root` (the path itself or
    anything below it) and that started after `since` (default: this session's start), other than
    this process and its ancestors. Processes from before the session (the shell that started pytest
    with --basetemp=root, say) are never counted: etime has whole seconds, so a process counts only from
    a second after `since`."""
    rows = _ps()
    mine = _ancestors(rows)
    since = _STARTED if since is None else since
    names = re.compile(re.escape(str(root).rstrip(os.sep)) + r"(?=[/\s'\"]|$)")
    return [(pid, pgid, cmd) for pid, _, pgid, start, cmd in rows
            if pid not in mine and start >= since + 1 and names.search(cmd)]


def reap(found: list[tuple[int, int, str]], root: str | Path, grace: float = 3.0) -> list[tuple[int, int, str]]:
    """Stop the processes in `found` and their process groups: TERM, up to `grace` s, then KILL. A group
    this process or an ancestor belongs to is never signalled, only the process. Returns those of
    `found` still alive afterwards."""
    rows = _ps()
    mine = _ancestors(rows)
    ours = {row[2] for row in rows if row[0] in mine} | {os.getpgrp()}
    left = found
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, pgid, _ in left:
            try:
                if pgid > 1 and pgid not in ours:
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + grace
        while True:
            left = [proc for proc in leaked_processes(root) if proc[0] in {f[0] for f in found}]
            if not left or time.time() >= deadline:
                break
            time.sleep(0.1)
        if not left:
            break
    return left


@pytest.fixture
def reap_tmp_processes():
    """Returns reap(root): stops every process whose command line names `root` (and its group), and
    returns those still alive after TERM and KILL, normally []. For a test that detaches processes:
    call it from a finalizer that also runs when the test fails."""
    return lambda root: reap(leaked_processes(root), root)


@pytest.fixture
def leaked_tmp_processes():
    """The session guard's finder (leaked_processes), for tests of the guard itself."""
    return leaked_processes


def pytest_sessionstart(session):
    global _STARTED
    _STARTED = time.time()


def pytest_sessionfinish(session, exitstatus):
    if not any(i.path.name.startswith(_SHARDED) for i in session.items):
        return
    root = getattr(getattr(session.config, "_tmp_path_factory", None), "_basetemp", None)
    if root is None:   # no test used a temp path
        return
    found = leaked_processes(root)
    if not found:
        return
    left = reap(found, root)
    lines = [f"tests left {len(found)} process(es) running that name this session's temp root {root} "
             f"(stopped now{'' if not left else f'; {len(left)} still alive'}):"]
    lines += [f"  pid {pid} (group {pgid}): {cmd[:300]}" for pid, pgid, cmd in found]
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    for line in lines:
        if tr:
            tr.write_line(line, red=True)
        else:
            print(line, file=sys.stderr)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _units(items) -> list[list]:
    """Each tt-project test is a unit of its own; every other test goes in one unit, kept in order."""
    rest = [i for i in items if not i.path.name.startswith(_SHARDED)]
    return ([rest] if rest else []) + [[i] for i in items if i.path.name.startswith(_SHARDED)]


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    if session.testsfailed and not session.config.option.continue_on_collection_errors:
        return None   # pytest's own loop reports the collection errors
    units = _units(session.items)
    jobs = min(_jobs(session.config, session.items), len(units))
    if jobs < 2 or len(units) * 4 > 60000:   # the queue must fit in a pipe's buffer
        return None
    session.config._ttp_workers = jobs
    _run_forked(session, units, jobs)
    return True


def _send(fd: int, kind: str, **kw) -> None:
    data = (json.dumps({"kind": kind, **kw}) + "\n").encode()
    while data:
        data = data[os.write(fd, data):]


class _Forward:
    """In a worker: sends what the main process's reporters need over the worker's pipe."""

    def __init__(self, config, fd: int):
        self.config, self.fd = config, fd

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logstart(self, nodeid, location):
        _send(self.fd, "start", nodeid=nodeid, location=list(location))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        _send(self.fd, "report", data=data)

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logfinish(self, nodeid, location):
        _send(self.fd, "finish", nodeid=nodeid, location=list(location))

    @pytest.hookimpl(trylast=True)
    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        cat = warning_message.category
        _send(self.fd, "warning", message=str(warning_message.message), when=when, nodeid=nodeid,
              category=f"{cat.__module__}.{cat.__qualname__}", filename=str(warning_message.filename),
              lineno=warning_message.lineno, location=list(location) if location else None)


def _worker(session, units, work_r: int, out_w: int) -> None:
    config = session.config
    tr = config.pluginmanager.get_plugin("terminalreporter")
    if tr is not None:   # the main process prints; a worker's reporter writes nowhere
        from _pytest._io import TerminalWriter
        tr._tw = TerminalWriter(open(os.devnull, "w"))
    config.pluginmanager.register(_Forward(config, out_w), "ttp-forward")

    def take():
        raw = os.read(work_r, 4)
        if len(raw) < 4:
            return None
        n = struct.unpack("!I", raw)[0]
        _send(out_w, "take", unit=n)
        return units[n]

    cur = take()
    while cur:
        nxt = take()   # the next item decides which fixtures this one's teardown keeps
        for j, item in enumerate(cur):
            nextitem = cur[j + 1] if j + 1 < len(cur) else (nxt[0] if nxt else None)
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        cur = nxt


def _warning(msg: dict):
    mod, _, name = msg["category"].rpartition(".")
    try:
        cat = getattr(importlib.import_module(mod), name)
    except Exception:
        cat = Warning
    if not (isinstance(cat, type) and issubclass(cat, Warning)):
        cat = Warning
    return warnings.WarningMessage(cat(msg["message"]), cat, msg["filename"], msg["lineno"])


NUMBERED_DIR_TRIES = 1000


def _make_numbered_dir(root: Path, prefix: str, mode: int = 0o700) -> Path:
    """pytest's make_numbered_dir, for workers sharing one temp root: pytest's gives up after 10 lost
    races (mkdir EEXIST: another worker took the number its scan picked), which a test with many
    short parameters lost under load. This retries a lost race up to NUMBERED_DIR_TRIES times and
    raises any other mkdir error at once, with its errno."""
    import _pytest.pathlib as pl
    for _ in range(NUMBERED_DIR_TRIES):
        path = root.joinpath(f"{prefix}{max(map(pl.parse_num, pl.find_suffixes(root, prefix)), default=-1) + 1}")
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            continue
        pl._force_symlink(root, prefix + "current", path)
        return path
    raise OSError(f"could not create numbered dir with prefix {prefix} in {root} after {NUMBERED_DIR_TRIES} tries")


def _share_numbered_dirs() -> None:
    """Makes tmp_path's numbered folders safe for workers sharing one temp root (no-op on a pytest
    without these internals)."""
    import _pytest.pathlib as pl
    import _pytest.tmpdir as td
    if all(hasattr(pl, n) for n in ("parse_num", "find_suffixes", "_force_symlink")) and hasattr(td, "make_numbered_dir"):
        td.make_numbered_dir = _make_numbered_dir


def _run_forked(session, units, jobs: int) -> None:
    config, hook = session.config, session.config.hook
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        factory.getbasetemp()   # made once here, so every worker shares the session's temp root
        _share_numbered_dirs()
    work_r, work_w = os.pipe()
    os.write(work_w, b"".join(struct.pack("!I", n) for n in range(len(units))))
    os.close(work_w)
    sys.stdout.flush()
    sys.stderr.flush()
    workers: dict[int, dict] = {}
    for _ in range(jobs):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(r)
                for other in workers.values():
                    os.close(other["fd"])
                if not _on_ci(os.environ):
                    try:
                        os.nice(WORKER_NICE)   # the tests and what they start yield to other work
                    except (AttributeError, OSError):
                        pass
                _worker(session, units, work_r, w)
            except BaseException:   # noqa: BLE001 - the main process reports what this worker left
                code = 1
            finally:
                os._exit(code)
        os.close(w)
        workers[pid] = {"fd": r, "buf": b"", "taken": [], "open": None}
    os.close(work_r)
    finished: set[str] = set()
    sel = selectors.DefaultSelector()
    for pid, wk in workers.items():
        os.set_blocking(wk["fd"], False)
        sel.register(wk["fd"], selectors.EVENT_READ, pid)

    def handle(wk, msg):
        kind = msg["kind"]
        if kind == "take":
            wk["taken"].append(msg["unit"])
        elif kind == "start":
            wk["open"] = msg["nodeid"]
            hook.pytest_runtest_logstart(nodeid=msg["nodeid"], location=tuple(msg["location"]))
        elif kind == "report":
            rep = hook.pytest_report_from_serializable(config=config, data=msg["data"])
            if isinstance(rep.longrepr, list):   # a skip's (path, line, reason), a list after JSON
                rep.longrepr = tuple(rep.longrepr)
            hook.pytest_runtest_logreport(report=rep)
        elif kind == "finish":
            wk["open"] = None
            finished.add(msg["nodeid"])
            hook.pytest_runtest_logfinish(nodeid=msg["nodeid"], location=tuple(msg["location"]))
        elif kind == "warning":
            hook.pytest_warning_recorded.call_historic(kwargs=dict(
                warning_message=_warning(msg), when=msg["when"], nodeid=msg["nodeid"],
                location=tuple(msg["location"]) if msg["location"] else None))

    def read(pid, wk, exited=False) -> None:
        """Hands on what the worker wrote; at EOF, or once it exited and nothing is left (a process a
        test started may still hold the pipe open), its pipe is closed and the worker reaped."""
        while True:
            try:
                chunk = os.read(wk["fd"], 1 << 16)
            except BlockingIOError:
                chunk = None
            if chunk:
                *lines, wk["buf"] = (wk["buf"] + chunk).split(b"\n")
                for line in lines:
                    handle(wk, json.loads(line))
                continue
            if chunk is None and not exited:
                return
            sel.unregister(wk["fd"])
            os.close(wk["fd"])
            if "status" not in wk:
                wk["status"] = os.waitpid(pid, 0)[1]
            return

    try:
        while sel.get_map():
            for key, _ in sel.select(timeout=1.0):
                read(key.data, workers[key.data])
            for pid, wk in workers.items():
                if "status" in wk or wk["fd"] not in {k.fd for k in sel.get_map().values()}:
                    continue
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    wk["status"] = status
                    read(pid, wk, exited=True)
    except BaseException:
        for pid in workers:
            try:
                if "status" not in workers[pid]:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except OSError:
                pass
        raise
    # A worker that died (a crash, a kill) fails every test it took and did not finish.
    from _pytest.reports import TestReport
    for pid, wk in workers.items():
        for n in wk["taken"]:
            for item in units[n]:
                if item.nodeid in finished:
                    continue
                why = (f"the test worker process {pid} ended (wait status {wk.get('status')}) before this "
                       "test finished")
                if wk["open"] != item.nodeid:
                    hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
                hook.pytest_runtest_logreport(report=TestReport(
                    item.nodeid, item.location, {}, "failed", why, "call", duration=0.0))
                hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
                finished.add(item.nodeid)
    lost = [i for u in units for i in u if i.nodeid not in finished]
    for item in lost:   # taken by no worker: every worker died before reaching it
        hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        hook.pytest_runtest_logreport(report=TestReport(
            item.nodeid, item.location, {}, "failed", "no test worker was left to run this test", "call",
            duration=0.0))
        hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    if session.shouldfail:
        raise session.Failed(session.shouldfail)
    if session.shouldstop:
        raise session.Interrupted(session.shouldstop)


# On Linux the session's temp files go to a RAM disk when one is free: the suite writes gigabytes of
# short-lived git repositories, and on disk that costs about a third of its CPU and wall time.
TMPFS = "/dev/shm"
TMPFS_MIN_FREE = 2 << 30


def _tmpfs_root(given_basetemp, environ, platform: str = sys.platform, path: str = TMPFS,
                min_free: int = TMPFS_MIN_FREE, mounts: str = "/proc/self/mounts") -> str | None:
    """The tmpfs folder to hold this session's temp files, or None to keep pytest's default: not
    Linux, on CI, a temp root the caller chose (--basetemp, PYTEST_DEBUG_TEMPROOT, TMPDIR/TMP/TEMP,
    or a `ttp checks` run, whose TMPDIR is scratch reclaimed with its allocation), TTP_TEST_TMPFS=0,
    or no writable tmpfs at `path` with `min_free` bytes free."""
    if (not platform.startswith("linux") or given_basetemp is not None
            or any(environ.get(k) for k in ("PYTEST_DEBUG_TEMPROOT", "TMPDIR", "TMP", "TEMP",
                                            "TTP_CHECK_OUTPUT_DIR", "TTP_CHECKS_TMP_FD"))
            or any(environ.get(k, "").strip().lower() not in ("", "0", "false") for k in ("CI", "GITHUB_ACTIONS"))
            or environ.get("TTP_TEST_TMPFS", "").strip() == "0"):
        return None
    try:
        real = os.path.realpath(path)
        with open(mounts, encoding="utf-8") as f:
            fstype = {p[1]: p[2] for p in (line.split() for line in f) if len(p) > 2}.get(real)
        st = os.statvfs(real)
    except OSError:
        return None
    if fstype != "tmpfs" or not os.access(real, os.W_OK | os.X_OK) or st.f_bavail * st.f_frsize < min_free:
        return None
    return real


def _use_tmpfs(config) -> None:
    """Puts the session's base temp under the tmpfs _tmpfs_root picks, in this process only: an
    exported PYTEST_DEBUG_TEMPROOT would also move the temp root of a nested pytest that a test
    starts. pytest still numbers, retains and removes the folders as usual. config._ttp_tmpfs tells
    the _short_tmp fixture the same root."""
    factory = getattr(config, "_tmp_path_factory", None)
    root = _tmpfs_root(getattr(factory, "_given_basetemp", None), os.environ) if factory is not None else None
    config._ttp_tmpfs = root
    if root is None:
        return
    import tempfile
    default = factory.getbasetemp

    def getbasetemp():
        if factory._basetemp is not None:
            return factory._basetemp
        saved, tempfile.tempdir = tempfile.tempdir, root   # pytest's temp root is tempfile.gettempdir()
        try:
            return default()
        finally:
            tempfile.tempdir = saved
    factory.getbasetemp = getbasetemp

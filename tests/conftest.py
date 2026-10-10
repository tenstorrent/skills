"""Runs the tt-project tests (tests/test_tt_project_*.py) in forked worker processes, with no plugin.

Those files hold well over a thousand tests that mostly wait on subprocesses (git, the CLI, the
runner), so one process spends most of its time idle. When a session collects at least
MIN_ITEMS of them, the main process forks TTP_TEST_JOBS workers (default: up to 8, one per CPU)
after collection. Each worker takes the next test from a shared queue and runs it with pytest's own
runtest protocol; the main process replays every report through pytest's hooks, so the terminal
output, --durations, the summary and the exit status are those of an ordinary run. All other
collected tests form one unit that a single worker runs in order, as before.

Every collected test still runs exactly once. A worker that dies fails the tests it took and did not
finish. TTP_TEST_JOBS=1 (or -x, --maxfail, --pdb, -s, --stepwise, xdist) runs everything in-process.
"""

from __future__ import annotations

import importlib
import json
import os
import selectors
import signal
import struct
import sys
import warnings

import pytest

MIN_ITEMS = 50
MAX_AUTO_JOBS = 8
_SHARDED = "test_tt_project_"


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
    return max(1, min(MAX_AUTO_JOBS, cpus))


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


def _run_forked(session, units, jobs: int) -> None:
    config, hook = session.config, session.config.hook
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        factory.getbasetemp()   # made once here, so every worker shares the session's temp root
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

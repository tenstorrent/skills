# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin the push queue loads (`-p ttp_pytest_resume`) to resume a check cut short.

TTP_PYTEST_RESUME names a file of test files, one per line, whose every selected test passed in an
earlier run of the same check on the same tree (pushpass). This run deselects them and appends each
further file once all its selected tests passed. A file with a failed, errored or rerun test is never
written. It acts only in the pytest process it was loaded into: it takes its load and record out of
the environment, so a pytest a test starts runs without it. Off under pytest-xdist, whose
workers report files out of order."""
from __future__ import annotations

import os

import pytest

ENV = "TTP_PYTEST_RESUME"
NAME = "ttp_pytest_resume"
TESTS_FAILED = 1
NO_TESTS = 5   # pytest's exit status when nothing was selected


def _strip_env() -> str:
    """The record file, with the plugin's load and record taken out of the environment children inherit."""
    path = os.environ.pop(ENV, "")
    words = os.environ.get("PYTEST_ADDOPTS", "").split()
    kept = [w for i, w in enumerate(words) if not (w == "-p" and words[i + 1:i + 2] == [NAME]
                                                   or w == NAME and words[i - 1:i] == ["-p"])]
    if kept:
        os.environ["PYTEST_ADDOPTS"] = " ".join(kept)
    else:
        os.environ.pop("PYTEST_ADDOPTS", None)
    return path   # PYTHONPATH keeps this folder: an xdist worker imports the plugin it was given


def _file(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


class Resume:
    def __init__(self, path: str):
        self.path = path
        self.left: dict[str, int] = {}
        self.bad: set[str] = set()
        self.skipped = 0

    def done(self) -> set[str]:
        try:
            with open(self.path) as f:
                text = f.read()
        except OSError:
            return set()
        lines = text.split("\n")
        return {line for line in lines[:-1] if line}   # the last part has no newline: a cut write

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config, items):
        done = self.done()
        gone = [i for i in items if _file(i.nodeid) in done]
        if gone:
            items[:] = [i for i in items if _file(i.nodeid) not in done]
            config.hook.pytest_deselected(items=gone)
            self.skipped = len({_file(i.nodeid) for i in gone})
        for i in items:
            self.left[_file(i.nodeid)] = self.left.get(_file(i.nodeid), 0) + 1

    def pytest_report_collectionfinish(self, config, items):
        if self.skipped:
            return f"ttp: resuming: {self.skipped} test file(s) passed in an earlier run on this tree; not run again"

    def pytest_runtest_logreport(self, report):
        f = _file(report.nodeid)
        if f not in self.left:
            return
        if report.outcome not in ("passed", "skipped"):
            self.bad.add(f)
        if report.when != "teardown":
            return
        self.left[f] -= 1
        if self.left[f] == 0 and f not in self.bad:
            with open(self.path, "a") as out:
                out.write(f + "\n")
                out.flush()
                os.fsync(out.fileno())

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        status = int(session.exitstatus)   # after other plugins (a coverage floor) set theirs
        if status == TESTS_FAILED and not self.bad:
            # The session failed though no test did: its record would let a later run skip the failure.
            try:
                os.unlink(self.path)
            except OSError:
                pass
            return
        if self.skipped and status == NO_TESTS and not self.left:
            session.exitstatus = 0   # every file passed before: nothing left to run is a pass


def pytest_configure(config):
    path = _strip_env()
    xdist = hasattr(config, "workerinput") or bool(getattr(config.option, "numprocesses", None)) \
        or getattr(config.option, "dist", "no") not in ("no", None)
    if path and not xdist:
        config.pluginmanager.register(Resume(path), "ttp-pytest-resume")

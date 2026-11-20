# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin the push queue and `ttp checks` load (`-p ttp_pytest_durations`, slowtests.env) to
keep a check's slowest tests.

TTP_PYTEST_DURATIONS names the JSON file to write. A test's time is the sum of its setup, call and
teardown durations, as pytest's --durations counts them, from the reports the main process sees
(xdist or forked workers send theirs there). At the session's end the TTP_PYTEST_DURATIONS_TOP
slowest (default 30) are written to the file, merged with what an earlier pytest run of the same
check left in it: {"tests": [{"id", "s"}], "count": tests timed}. With TTP_PYTEST_DURATIONS_LIVE
also set, each test's start ({"id", "start": epoch}) and end ({"id", "s"}) are appended to that file
as they happen, so a run killed part way (a timeout) still shows what ended and what was running.
It selects, skips and changes nothing else. It acts only in the pytest process it was loaded into: it takes its load and file out
of the environment, so a pytest a test starts runs without it."""
from __future__ import annotations

import json
import os
import time

ENV, TOP_ENV, NAME, TOP = "TTP_PYTEST_DURATIONS", "TTP_PYTEST_DURATIONS_TOP", "ttp_pytest_durations", 30
LIVE_ENV = "TTP_PYTEST_DURATIONS_LIVE"


def _strip_env() -> tuple[str, str, str]:
    """(file, top, live file), with the plugin's load and settings taken out of the environment children inherit."""
    path, top, live = os.environ.pop(ENV, ""), os.environ.pop(TOP_ENV, ""), os.environ.pop(LIVE_ENV, "")
    words = os.environ.get("PYTEST_ADDOPTS", "").split()
    kept = [w for i, w in enumerate(words) if not (w == "-p" and words[i + 1:i + 2] == [NAME]
                                                   or w == NAME and words[i - 1:i] == ["-p"])]
    if kept:
        os.environ["PYTEST_ADDOPTS"] = " ".join(kept)
    else:
        os.environ.pop("PYTEST_ADDOPTS", None)
    return path, top, live


class Durations:
    def __init__(self, path: str, top: str, live: str = ""):
        self.path, self.live, self.times = path, live, {}
        try:
            self.top = max(int(top or TOP), 1)
        except ValueError:
            self.top = TOP

    def pytest_runtest_logreport(self, report):
        self.times[report.nodeid] = self.times.get(report.nodeid, 0.0) + float(getattr(report, "duration", 0) or 0)

    def _append(self, row: dict) -> None:
        if not self.live:
            return
        try:
            with open(self.live, "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:   # a timing note never fails the run
            pass

    def pytest_runtest_logstart(self, nodeid, location):
        self._append({"id": nodeid, "start": round(time.time(), 3)})

    def pytest_runtest_logfinish(self, nodeid, location):
        self._append({"id": nodeid, "s": round(self.times.get(nodeid, 0.0), 3)})

    def pytest_sessionfinish(self, session, exitstatus):
        if not self.times:
            return
        times, count = dict(self.times), len(self.times)
        try:
            with open(self.path) as f:
                old = json.load(f)
            times = {str(t["id"]): float(t["s"]) for t in old.get("tests", [])} | times
            count += int(old.get("count", 0))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        rows = sorted(times.items(), key=lambda kv: -kv[1])[:self.top]
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"tests": [{"id": k, "s": round(v, 3)} for k, v in rows], "count": count}, f)
            os.replace(tmp, self.path)
        except OSError:   # a timing note never fails the run
            try:
                os.unlink(tmp)
            except OSError:
                pass


def pytest_configure(config):
    path, top, live = _strip_env()
    if path and not hasattr(config, "workerinput"):   # an xdist worker's reports reach the controller
        config.pluginmanager.register(Durations(path, top, live), "ttp-pytest-durations")

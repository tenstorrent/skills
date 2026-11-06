"""A test worker lost mid-run is the host, not the change under test.

The forked test runner (tests/conftest.py) fails each test a worker took and did not finish with
'the test worker process N ended (wait status S) before this test finished', and each test no worker
took with 'no test worker was left to run this test'. When those are a failed check's only failures,
`ttp checks` runs it once more (RERUNS caps it, so a real crash cannot loop). A second loss in which
every worker exited cleanly (wait status 0, which no test verdict produces) is an infrastructure
fault: reported as such, never as a failure of the change. A worker killed or crashed twice (a
signal, a non-zero exit) may be the change's own doing and stays a failure.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path

from . import setupfault

RERUNS = 1
MARKER = "worker_loss.json"   # in a run's directory: its checks stopped at a lost test worker
# Only a lost test's own report counts: its failure section, a '_ name _' header line followed by
# just the runner's message. A test's assertion diff or a short-summary line quoting it never does.
_ENDED = re.compile(r"^_+ (.+?) _+\nthe test worker process (\d+) ended \(wait status (-?\d+|None)\) before this "
                    r"test finished$", re.M)
_LEFT = re.compile(r"^_+ (.+?) _+\nno test worker was left to run this test$", re.M)
_SUMMARY = re.compile(r"^(?:=+ )?((?:\d+ \w+, )*\d+ (?:failed|passed|errors?)\b.*?) in [0-9.]+s", re.M)
_COUNT = re.compile(r"(\d+) (failed|errors?)\b")
_MESSAGE = re.compile(r"(?:the test worker process \d+ ended \(wait status (?:-?\d+|None)\) before this test finished"
                      r"|no test worker was left to run this test)$")
KEPT = 20000   # a Collector keeps at most this many lost tests' sections


class Collector:
    """What lost() reads from a check's output as it streams, for a runner that keeps only its last
    lines: each lost test's failure section (its header and the runner's line) and the last lines
    (the pytest summary). reset() before each run of the check."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.sections: list[str] = []
        self.prev = ""
        self.tail: deque[str] = deque(maxlen=5)
        self.setup = setupfault.Scan()

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if _MESSAGE.match(line) and len(self.sections) < KEPT:
            self.sections.append(f"{self.prev}\n{line}")
        self.prev = line
        self.tail.append(line)
        self.setup.feed(line)

    def text(self) -> str:
        return "\n".join([*self.sections, *self.tail])


class Infra(Exception):
    """Test workers lost again on the rerun of `check`, every one exiting cleanly (infra): raised by the
    push queue's check runner, whose batch then goes back to the queue, held, never failed."""

    def __init__(self, loss: dict, check: str):
        super().__init__(line(loss, check))
        self.loss, self.check = loss, check


def lost(output: str) -> dict | None:
    """What a check's output says of lost test workers, or None when it names none: `workers` maps
    each lost worker's pid to its wait status, `tests` counts the tests lost, and `other` the
    failures and errors besides them (None when no pytest summary line counts them)."""
    ended = {test: (int(pid), status) for test, pid, status in _ENDED.findall(output)}
    left = len(set(_LEFT.findall(output)) - set(ended))
    if not ended and not left:
        return None
    workers = {pid: (None if status == "None" else int(status)) for pid, status in ended.values()}
    summary = _SUMMARY.findall(output)
    other = None
    if summary:
        failed = sum(int(n) for n, _ in _COUNT.findall(summary[-1]))
        other = max(0, failed - len(ended) - left)
    return {"workers": workers, "tests": len(ended) + left, "other": other}


def only(loss: dict | None) -> bool:
    """The lost workers' tests are the check's only failures: what a rerun may settle."""
    return bool(loss) and loss["other"] == 0


def infra(loss: dict | None) -> bool:
    """A loss no test verdict explains: the only failures, and no worker ended by a signal or a
    non-zero exit (what a crashing test, or the change, can cause)."""
    return only(loss) and all(s == 0 for s in loss["workers"].values())


def _workers(loss: dict) -> str:
    w = loss["workers"]
    if not w:
        return "no worker was left to take them"
    return ", ".join(f"process {pid} (wait status {s})" for pid, s in sorted(w.items()))


def rerun_line(loss: dict, check: str) -> str:
    return (f"a test worker was lost mid-run ({loss['tests']} tests not run: {_workers(loss)}); an "
            f"infrastructure fault, not a test verdict: running {check} once more")


def line(loss: dict, check: str) -> str:
    return (f"infrastructure fault: test workers were lost mid-run again on the rerun of {check} "
            f"({loss['tests']} tests not run: {_workers(loss)}); not a failure of the change under test")


def write_marker(run_dir: str | Path | None, loss: dict, check: str) -> None:
    if not run_dir:
        return
    from .project import write_json
    try:
        write_json(Path(run_dir) / MARKER, {"check": check, "tests": loss["tests"],
                                            "workers": {str(k): v for k, v in loss["workers"].items()},
                                            "ts": time.time()})
    except OSError:
        pass


def clear(run_dir: str | Path | None) -> None:
    """A later run of the checks did not stop at a lost worker: an older marker no longer applies."""
    if run_dir:
        try:
            (Path(run_dir) / MARKER).unlink(missing_ok=True)
        except OSError:
            pass


def marker(run_dir: Path) -> dict | None:
    """The worker loss a run's checks stopped at, or None."""
    try:
        m = json.loads((Path(run_dir) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or not isinstance(m.get("workers"), dict):
        return None
    return {"check": str(m.get("check") or "?"), "tests": int(m.get("tests") or 0),
            "workers": {int(k): v for k, v in m["workers"].items() if str(k).isdigit()}, "other": 0}

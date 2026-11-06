# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check that failed before any test ran: the host or its test pool, not the change under test.

A check that copies the head to a test pool, or waits for one, can fail before its first test: the head or
work dir it copies from is gone (`cd: <absolute dir>: No such file or directory`), the archive it sends is
empty or corrupt (tar's `This does not look like a tar archive` or `Unexpected EOF in archive`, gzip's `not
in gzip format` or `unexpected end of file`, rsync's `rsync error:`), or no pool had a free slot (`no free
slot`, `no <name> pool free`, or the push's own pool-wait bound, push.POOL_STARVED). found() names such a
fault in a failed check's output only when no test verdict is in it (VERDICT: a pytest count or result line,
a TAP `ok`/`not ok`, an upper-case PASS or FAIL word, a Python traceback): a check whose tests ran and failed
stays a failure. The runners (`ttp push`, `ttp land`, `ttp checks`, the push queue) rerun a setup fault up
to RERUNS times; still so, or a pool with no free slot at once, it is an infrastructure fault: no verdict,
never passed and never failed (`ttp push` and `ttp land` exit BUSY, `ttp checks` 75 with infra_fault INFRA
in checks.json, the push queue holds the batch and tries it again after RETRY_S).
"""
from __future__ import annotations

import re
from pathlib import Path

RERUNS = 2          # reruns of a check that failed in setup before it is an infrastructure fault
RETRY_S = 600       # a push stopped by one is tried again after this
INFRA = "no-test-ran"
KEPT = 5            # fault lines kept for the log
_SIGNS = (("setup", re.compile(r"\bcd: (?P<path>/[^:\n]*): No such file or directory")),
          ("setup", re.compile(r"\btar: (?:This does not look like a tar archive|Unexpected EOF in archive)"
                               r"|\bgzip: stdin: (?:not in gzip format|unexpected end of file)|^rsync error: ")),
          ("pool", re.compile(r"\bno (?:free )?(?:[\w-]+ )?(?:pool|slot)s? (?:is |was )?(?:free|available|left)\b"
                              r"|\bno free (?:test[- ])?(?:pool|slot|host)s?\b", re.I)))
VERDICT = re.compile(r"\b\d+ (?:passed|failed|errors?|xfailed|xpassed)\b|^(?:PASSED|FAILED|ERROR) \S+::"
                     r"|^(?:not )?ok \d+\b|\b(?:PASS|PASSED|FAIL|FAILED)\b|^Traceback \(most recent call last\)")


class Scan:
    """What found() reads from a check's output as it streams: whether a test verdict showed, and the
    first KEPT fault lines with their kind and the missing dir they name (None for none)."""

    def __init__(self) -> None:
        self.verdict = False
        self.faults: list[tuple[str, str, str | None]] = []

    def feed(self, line: str) -> None:
        if self.verdict:
            return
        line = line.rstrip("\r\n")
        if VERDICT.search(line):
            self.verdict = True
            return
        for kind, sign in _SIGNS:
            if (m := sign.search(line)) and len(self.faults) < KEPT:
                self.faults.append((kind, line.strip()[:300], m.groupdict().get("path")))
                return


def scan(text: str) -> Scan:
    s = Scan()
    for line in (text or "").splitlines():
        s.feed(line)
    return s


def _inside(path: str, repo: Path | str | None) -> bool:
    if repo is None:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(repo).resolve())
    except (OSError, ValueError):
        return True


def found(s: Scan, repo: Path | str | None) -> dict | None:
    """The setup fault a failed check's output (Scan) shows, or None: `kind` "pool" when a pool had no
    free slot (the check waits, never reruns at once), else "setup", and its `lines`. A dir missing
    inside the repository is the change's own doing, never one; any test verdict makes none."""
    if s.verdict:
        return None
    faults = [(k, ln) for k, ln, path in s.faults if not (path and _inside(path, repo))]
    if not faults:
        return None
    return {"kind": "pool" if any(k == "pool" for k, _ in faults) else "setup", "lines": [ln for _, ln in faults]}


def starved() -> dict:
    """The fault of a check the push ended at its pool-wait bound (push.POOL_STARVED)."""
    return {"kind": "pool", "lines": ["the check waited past its pool-wait bound (delivery.push_pool_wait_s)"]}


def _what(fault: dict) -> str:
    return ("no test pool had a free slot" if fault["kind"] == "pool" else "its setup failed") + \
        f" before any test ran ({fault['lines'][0]})"


def rerun_line(fault: dict, check: str, n: int) -> str:
    return (f"{check}: {_what(fault)}; an infrastructure fault, not a test verdict: running it again "
            f"({n} of {RERUNS})")


def line(fault: dict, check: str) -> str:
    again = "" if fault["kind"] == "pool" else f" after {RERUNS} reruns"
    return (f"infrastructure fault: {check}: {_what(fault)}{again}; no test ran, so the change is neither "
            "passed nor failed")


class Infra(Exception):
    """A check that failed before any test ran, still so after its reruns (or a pool with no free slot):
    raised by the push queue's check runner, whose batch then goes back to the queue, held, never failed."""

    def __init__(self, fault: dict, check: str):
        super().__init__(line(fault, check))
        self.fault, self.check = fault, check

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keep large command output out of a model's context.

A long run re-reads its whole context on every call, so a 3,000-line test log read once is paid
for again on every later turn. The full output goes to a file; the model gets a short excerpt and
the path. Test output is cut to its failures: which tests failed, why, and the totals line.
"""
from __future__ import annotations

import re

MAX_LINES = 40          # an excerpt of ordinary output: this many lines, head and tail
MAX_LINE_CHARS = 300    # each line is cut to this many characters
MAX_FAILURE_LINES = 60  # failures-only output: at most this many lines

# pytest (-q, -v, -ra) and unittest: the lines that say what failed and why
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+\S|^(?:FAIL|ERROR):\s+\S|^E\s{2,}\S|^\S+\.py:\d+: \w*(?:Error|Exception)\b")
_TOTALS_RE = re.compile(r"^=*\s*(?:\d+ (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
                        r"[\d\w ,]*)+ in [\d.]+\s*s|^(?:Ran \d+ tests? in |FAILED \(|OK\b)")


def _cut(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " …"


def is_test_output(text: str) -> bool:
    """Whether `text` ends like a pytest or unittest run."""
    return any(_TOTALS_RE.search(ln.strip()) for ln in text.splitlines()[-15:])


def failures(text: str, limit: int = MAX_FAILURE_LINES) -> str:
    """The failing tests, their error lines and the totals line of a test run; "" when it shows none."""
    lines = text.splitlines()
    picked = [ln for ln in lines if _FAILED_RE.search(ln)]
    totals = [ln for ln in lines[-15:] if _TOTALS_RE.search(ln.strip())]
    if not picked:
        return ""
    # Each failing test is listed twice by pytest (its section and the short summary): keep one.
    seen: set[str] = set()
    out = [ln for ln in picked if not (ln in seen or seen.add(ln))]
    if len(out) > limit:
        out = out[:limit - 1] + [f"… {len(out) - limit + 1} more failure lines"]
    return "\n".join(_cut(ln) for ln in out + totals[-1:])


def excerpt(text: str, max_lines: int = MAX_LINES) -> str:
    """The head and tail of `text`, at most `max_lines` lines, saying how many were left out."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return "\n".join(_cut(ln) for ln in lines)
    head = max_lines // 4
    tail = max_lines - head
    return "\n".join([*(_cut(ln) for ln in lines[:head]), f"… {len(lines) - max_lines} lines left out …",
                      *(_cut(ln) for ln in lines[-tail:])])


def summary(text: str, path: str = "", max_lines: int = MAX_LINES) -> str:
    """What a model should read of a command's output: a test run's failures, else an excerpt, then
    where the full output is."""
    short = failures(text) if is_test_output(text) else ""
    short = short or excerpt(text, max_lines)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    shown = short.count("\n") + 1 if short else 0
    if path and shown < lines:
        short += f"\n(full output, {lines} lines: {path})"
    return short

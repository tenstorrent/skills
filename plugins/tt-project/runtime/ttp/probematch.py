# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A waiting hand-off's `probe_match`: words in its `retry_when` probe's output that let the daemon end
a confirm or watch wait with no model run.

    "probe_match": {"pass": "PASSED", "fail": ["FAILED", "Traceback"], "wait": "still running"}

Each of `pass`, `fail` and `wait` (any one or more) is a string or a list of strings, found as plain,
case-sensitive substrings (never regular expressions: no pattern can stall the daemon) in the last
OUT_CHARS of the probe's output. Only a probe that exits 0 is read; exactly one kind must match:

- wait: the exit 0 counts as "not yet", so the task sleeps on; a timer wake on the same output
  re-defers with the wake gate's back-off (waitgate), up to its caps.
- pass: the hand-off's `on_pass` is recorded model-free, `{probe_out}` in its summary or message
  filled with the output's tail (a hand-off that also names an `on_pass_cmd` runs that command).
- fail: the task wakes at its own tier with the output's tail.
- none or several kinds: the usual probe-passed wake, which says what matched.

The daemon notes each settled wait in the task's notes (the run's progress.md).
"""
from __future__ import annotations

KEY = "probe_match"
KINDS = ("pass", "fail", "wait")
OUT_CHARS = 4000          # of the probe's output searched
FILL_CHARS = 1500         # of its tail put in for {probe_out}
WORD_CHARS, WORDS_MAX = 200, 8
FILL = "{probe_out}"


def problem(raw) -> str:
    """Why a hand-off's probe_match cannot be used, or ""."""
    if not isinstance(raw, dict) or not raw:
        return "probe_match must be an object naming pass, fail or wait"
    if bad := [k for k in raw if k not in KINDS]:
        return f"probe_match has unknown keys: {', '.join(str(k)[:40] for k in bad[:4])} (only pass, fail, wait)"
    for k, v in raw.items():
        words = [v] if isinstance(v, str) else v
        if not isinstance(words, list) or not words or len(words) > WORDS_MAX:
            return f"probe_match {k} must be a string or a list of 1-{WORDS_MAX} strings"
        if any(not isinstance(w, str) or not w.strip() or len(w) > WORD_CHARS for w in words):
            return f"probe_match {k} words must be non-blank strings of at most {WORD_CHARS} chars"
    return ""


def matched(raw, out: str | None) -> list[str]:
    """The kinds whose words the output contains (none for an unusable probe_match)."""
    if problem(raw):
        return []
    text = str(out or "")[-OUT_CHARS:]
    return [k for k in KINDS if k in raw and any(w in text for w in ([raw[k]] if isinstance(raw[k], str) else raw[k]))]


def verdict(prev: dict, out: str | None) -> str | None:
    """pass, fail or wait when exactly one kind of the hand-off's probe_match matches, else None."""
    if not isinstance(prev, dict) or KEY not in prev:
        return None
    got = matched(prev[KEY], out)
    return got[0] if len(got) == 1 else None


def unclear(prev: dict, out: str | None) -> str:
    """What a probe_match found when it decided nothing, for the wake ("" without one)."""
    if not isinstance(prev, dict) or KEY not in prev:
        return ""
    if why := problem(prev[KEY]):
        return f"; {why}"
    got = matched(prev[KEY], out)
    return (f"; its output matched {' and '.join(got)} of its probe_match, so it is unclear" if got
            else "; its output matched none of its probe_match")


def tail(out: str | None, n: int = FILL_CHARS) -> str:
    return str(out or "").strip()[-n:]


def fill(on_pass: dict, out: str | None) -> dict:
    """The on_pass with {probe_out} in its summary and message replaced by the output's tail."""
    got = dict(on_pass)
    for k in ("summary", "message"):
        if isinstance(got.get(k), str) and FILL in got[k]:
            got[k] = got[k].replace(FILL, tail(out) or "(no output)")
    return got

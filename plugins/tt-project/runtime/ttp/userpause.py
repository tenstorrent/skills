# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Holds only the user can lift, and holds only time lifts.

A user pause is a probe that only tests for a marker file the user's word creates (by default one named
`user-*`, e.g. `test -e state/user-resume-tuning || exit 1`; coordinator.user_pause_marker, a regex
searched in the tested path, changes it): file tests of user markers joined by `||` or `&&`, or an `||`
of a user marker with other plain file tests (the user's word still ends it), optionally ending in
`|| exit 1`. A marker AND-ed or OR-ed with anything else (ssh, a command, a pipe) is a live probe. No
model turn can make a user pause pass, and the daemon already wakes or requeues the task once it does.
A probe whose top-level `&&` chain has a user-marker file test as a term (`test -e state/user-x && cmd`,
optionally ending in `|| exit 1`) also cannot pass while that marker is missing: user_gate names those
markers and user_gated says whether one is missing now (the progress check and wait_stale skip it then).
is_probe is the one classifier: the blocker sweep carries such a hold without an outcome, the waiting
check leaves it out of wait_stale and the progress check raises no probe stall for it. A time-only deferral (a `start_after` with no `start_when`)
also ends on its own; the sweep carries it the same way. Both stay listed for the user.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import anchors
from .db import deferral, load_result

DEFAULT_MARKER = r"(?:^|/)user-[\w.-]+$"
_PATH = r"(?:[^\s;|&`()<>$]|\$(?=[\w{]))+"   # a plain path: a variable may expand, nothing runs
_TERM = re.compile(rf"\s*(?:test\s+-[ef]\s+(?P<p1>{_PATH})|\[\s+-[ef]\s+(?P<p2>{_PATH})\s+\])\s*")
_OP = re.compile(r"(\|\||&&)")
_TAIL = re.compile(r"\s*\|\|\s*exit\s+1\s*;?\s*$|\s*;?\s*$")


def settings(where) -> dict:
    """Project settings from `where`: a settings dict, a Project, or anything else (no settings)."""
    if isinstance(where, dict):
        return where
    cfg = getattr(where, "config", None)
    try:
        return cfg() if callable(cfg) else {}
    except Exception:  # an unreadable project.json must not break a sweep
        return {}


def marker(cfg: dict | None) -> re.Pattern:
    """The user-pause marker pattern: coordinator.user_pause_marker, or the default when unset or invalid."""
    raw = ((cfg or {}).get("coordinator") or {}).get("user_pause_marker")
    if isinstance(raw, str) and raw.strip():
        try:
            return re.compile(raw)
        except re.error:
            pass
    return re.compile(DEFAULT_MARKER)


def is_probe(probe: object, cfg: dict | None = None) -> bool:
    """Whether `probe` is a user pause: plain file tests only, every one a user marker (any of `||`
    and `&&` between them), or an `||` chain with at least one user marker, optionally ending in
    `|| exit 1`."""
    text = _TAIL.sub("", str(probe or ""), count=1)
    parts = _OP.split(text)
    terms = [_TERM.fullmatch(t) for t in parts[0::2]]
    if not text.strip() or not all(terms):
        return False
    rx = marker(cfg)
    user = [bool(rx.search((m.group("p1") or m.group("p2")).strip("'\""))) for m in terms]
    return all(user) or (any(user) and set(parts[1::2]) <= {"||"})


def _top_level(text: str) -> list[str] | None:
    """`text` split at top-level `&&`, `||`, `;`, `&` and newlines into [term, op, term, ...], or None
    when it cannot be read safely (an open quote or group, a comment)."""
    out, cur, depth, quote, i = [], [], 0, "", 0
    while i < len(text):
        c, two = text[i], text[i:i + 2]
        if quote:
            if c == "\\" and quote == '"':
                cur.append(text[i:i + 2])
                i += 2
                continue
            quote = "" if c == quote else quote
        elif c == "\\":
            cur.append(text[i:i + 2])
            i += 2
            continue
        elif c in "'\"`":
            quote = c
        elif c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
            if depth < 0:
                return None
        elif c == "#" and (not cur or cur[-1][-1:].isspace()):
            return None   # a comment may hide a term
        elif depth == 0 and two in ("&&", "||"):
            out += ["".join(cur), two]
            cur, i = [], i + 2
            continue
        elif depth == 0 and (c in ";\n" or c == "&" and text[i - 1:i] not in "<>" and text[i + 1:i + 2] != ">"):
            out += ["".join(cur), c]
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if quote or depth:
        return None
    return out + ["".join(cur)]


def user_gate(probe: object, cfg: dict | None = None) -> list[str]:
    """The user-marker paths `probe` requires: each a file test term of a top-level chain joined only by
    `&&` (optionally ending in `|| exit 1`), so the probe fails while any of them is missing. Empty when
    another operator (`||`, `;`, `&`) could let it pass without them."""
    text = _TAIL.sub("", str(probe or ""), count=1)
    parts = _top_level(text) if text.strip() else None
    if not parts or set(parts[1::2]) - {"&&"}:
        return []
    rx = marker(cfg)
    got = []
    for t in parts[0::2]:
        m = _TERM.fullmatch(t)
        if m and rx.search(path := (m.group("p1") or m.group("p2")).strip("'\"")):
            got.append(path)
    return got


def user_gated(probe: object, cfg: dict | None = None, root: Path | str | None = None) -> bool:
    """Whether `probe` cannot pass until the user's word: a user pause (is_probe), or a probe that
    requires a user marker (user_gate) that is missing now (relative paths from `root`, where probes
    run). A path that still holds a variable after expansion is unknown: not gated."""
    if is_probe(probe, cfg):
        return True
    for raw in user_gate(probe, cfg):
        path = os.path.expanduser(os.path.expandvars(raw))
        if "$" in path:
            continue
        p = Path(path)
        if not p.is_absolute():
            if root is None:
                continue
            p = Path(root) / p
        if not p.exists():
            return True
    return False


def probe_of(task: dict, cfg: dict | None = None) -> str | None:
    """The user-pause probe that is `task`'s only hold, or None. Blocked: its `when:` anchor. Waiting
    (or queued after a waiting hand-off, unless only `start_after` defers it now): its retry_when.
    Deferred: its start_when."""
    if task.get("status") == "blocked":
        a = anchors.anchor(task)
        return a[1] if a and a[0] == "when" and is_probe(a[1], cfg) else None
    if task.get("status") not in ("queued", "waiting"):
        return None
    when = deferral(task).get("when")
    if when:
        return when if is_probe(when, cfg) else None
    if time_only(task):   # deferred by start_after alone: an earlier wait's probe no longer holds it
        return None
    prev = load_result(task.get("result"))
    rw = prev.get("retry_when") if prev.get("status") == "waiting" else None
    return rw if is_probe(rw, cfg) else None


def time_only(task: dict, now: float | None = None) -> bool:
    """A queued task deferred only by `start_after`, still ahead."""
    now = time.time() if now is None else now
    if task.get("status") != "queued":
        return False
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (TypeError, ValueError):
        return False
    return (isinstance(labels, list) and any(isinstance(lb, str) and lb.startswith("start_after:") for lb in labels)
            and not deferral(task).get("when") and float(task.get("not_before") or 0) > now)

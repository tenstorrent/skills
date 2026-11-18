# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A routine needs_review hand-off gets its review at once.

A task that hands off `needs_review` with nothing else to decide (no follow-ups, findings or notes,
normal severity: the daemon marks it `plain`) has the review the daemon queues for it anyway; waiting for
the review sweep (`review.sweep_after_s`, 300 s) only delays it. `at_once` says when the review is queued
in the hand-off's own step. `review.at_once` false turns it off, and the sweep stays the fallback."""
from __future__ import annotations


def enabled(cfg: dict) -> bool:
    return bool(((cfg or {}).get("review") or {}).get("at_once", True))


def at_once(cfg: dict, new: str, plain: bool, result: dict | None = None) -> bool:
    """Whether a hand-off that moved `task` to status `new` queues its review now: a plain needs_review
    hand-off, with review.at_once on. Whether the task has a change to review is the daemon's one code
    test (Daemon._code_change), which gates the review itself; the task's kind is no gate here. One that
    asks a question of the coordinator is its to read first, never queued here."""
    asks = isinstance(result, dict) and bool(result.get("question") or result.get("reason"))
    return new == "review" and plain and not asks and enabled(cfg)


def hold_for_precheck(cfg: dict) -> bool:
    """Whether a review waits for the checks the daemon started on its head (daemon.precheck_holds).
    Off by default: the review runs alongside the checks, and the pass is reused once the review approves
    that head unchanged (verdictcarry, reviewreuse). `review.hold_for_precheck` true brings the hold back."""
    return bool(((cfg or {}).get("review") or {}).get("hold_for_precheck", False))

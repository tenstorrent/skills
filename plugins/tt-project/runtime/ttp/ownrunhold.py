# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hold spec-change and probe-change wakes while a task's own `ttp checks` or `ttp push` run.

A task that handed off `waiting` on its own checks or push sleeps on that wait. A spec edit (`wake_now`)
or a probe answer that changed would start a model turn that can only hand the same wait back: the
checks or push are still running and settle model-free when they end (daemon._probe_passed applies the
pending spec change then). `hold` says why such a wake waits, or None when it may start: the wait is not
on an own run, the run is dead or lost (a real fault), the update is urgent, or `waiting.hold_own_runs`
is false."""
from __future__ import annotations

from typing import Any

from . import waitgate


def enabled(cfg: dict) -> bool:
    return bool(((cfg or {}).get("waiting") or {}).get("hold_own_runs", True))


def hold(p, cfg: dict, result: Any, urgent: bool = False) -> str | None:
    """Why a spec-change or probe-change wake of a task with hand-off `result` waits for its own
    checks or push to end, else None."""
    if urgent or not isinstance(result, dict) or not enabled(cfg):
        return None
    why = waitgate.own_checks(p, cfg, result)
    return f"{why}; the wake waits until it ends" if why else None

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""How often a wait on state the daemon can read is polled, and how long an urgent task's timer may run.

- Polling: a waiting task's `retry_when` and a deferred task's `start_when` run model-free every
  `waiting.poll_s` (POLL_S, kept between MIN_POLL_S and MAX_POLL_S). A pass wakes the task at once,
  whatever later retry the worker chose (Daemon.probe_waiting); a "not yet" makes no model call. A free
  lock, a finished `ttp detach` job or `ttp checks` run, a landing and any other probe answer the same way.
- Urgent cap: an urgent task (urgent.is_urgent) whose wait has such a probe sleeps at most
  `waiting.urgent_retry_cap_s` (URGENT_RETRY_CAP_S; 0: off) before its timer runs out, at the hand-off
  and at every re-sleep (Daemon._hold_waiting). The timer running out re-checks the probe: still "not
  yet" puts it back to sleep with no model call. A bare clock gate (`test "$(date +%s)" -ge <epoch>`) is
  the worker's own deadline: no early check can make it pass sooner, so it is not capped. Other tasks keep
  their `retry_after_s` backoff and are still released early by a passing poll.
- Ordered cap: a task of an open user order never goes longer than ORDERED_CAP_S between probe runs
  or timer checks, urgent or not (Daemon._probe_gap, `capped`).
- Events: the end of what a probe waits on runs the probe at once (waitend)."""
from __future__ import annotations

POLL_S = 90.0                 # default waiting.poll_s
MIN_POLL_S, MAX_POLL_S = 20.0, 180.0   # the bounds; MAX_POLL_S is daemon.PROBE_EVERY_S, its freshness unit
URGENT_RETRY_CAP_S = 300.0    # default waiting.urgent_retry_cap_s
ORDERED_CAP_S = 120.0         # the longest a task of an open user order sleeps between probe runs or timer checks


def _num(cfg: dict, key: str, default: float) -> float:
    try:
        v = (cfg.get("waiting") or {}).get(key)
        return default if v is None else float(v)
    except (TypeError, ValueError, AttributeError):
        return default


def poll_s(cfg: dict) -> float:
    """Seconds between model-free runs of a wait's probe."""
    return min(max(_num(cfg, "poll_s", POLL_S), MIN_POLL_S), MAX_POLL_S)


def urgent_cap_s(cfg: dict) -> float:
    """waiting.urgent_retry_cap_s (0: off)."""
    return max(_num(cfg, "urgent_retry_cap_s", URGENT_RETRY_CAP_S), 0.0)


def capped(cfg: dict, task: dict | None, result: dict, retry_s: float, ordered: bool = False) -> float:
    """`retry_s` (the hand-off's retry_after_s, already bounded), capped for an urgent task, or one of an
    open user order (`ordered`, ORDERED_CAP_S), whose wait has a probe that is not a bare clock gate."""
    from . import urgent
    from .daemon import time_gate
    cap = min(urgent_cap_s(cfg) or ORDERED_CAP_S, ORDERED_CAP_S) if ordered else urgent_cap_s(cfg)
    probe = result.get("retry_when")
    if not cap or not (ordered or urgent.is_urgent(task)) or not isinstance(probe, str) or not probe.strip() \
            or time_gate(probe) is not None:
        return retry_s
    return min(retry_s, cap)

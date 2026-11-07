# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""When a daily dollar cap is reached at the current burn rate, worked out once, model-free, with the
cap's reset in view: 'at $86/h: not reached before the reset at 08:00 PST' or 'at $400/h: reaches
the cap at 07:47 PST'. A cap reached after its reset is never reached in that period, so no
reach time later than the reset is ever printed. Without a known reset (a rolling 24 h) the line
says so and projects nothing. The coordinator quotes this line from STATE instead of computing one.

The rate: the slope of the recent readings of a running total (`trail`, at most TRAIL_S old), else
the period's average since its start."""
from __future__ import annotations

from . import timefmt

HOUR = 3600.0
TRAIL_S = HOUR          # the readings the rate is measured over
TRAIL_STEP_S = 300.0    # at most one reading kept per this long
TRAIL_MIN_S = 900.0     # a slope over less than this is too noisy: the period's average instead


def trail(prev, spent: float, now: float, start: float | None = None) -> list[list[float]]:
    """`prev` (a saved trail of [ts, usd]) with this reading added: readings before `start` (the
    period's start), older than TRAIL_S, above `spent` (a new period or a correction) or not
    readable are dropped; one reading per TRAIL_STEP_S."""
    out: list[list[float]] = []
    for r in prev if isinstance(prev, list) else []:
        try:
            ts, usd = float(r[0]), float(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if now - TRAIL_S <= ts < now and usd <= spent and (start is None or ts >= start):
            out.append([ts, usd])
    if not out or now - out[-1][0] >= TRAIL_STEP_S:
        out.append([now, round(spent, 2)])
    return out


def rate(readings, spent: float, now: float, start: float | None = None) -> float | None:
    """Dollars per hour: the slope from the oldest reading in `readings` to `spent` now once they span
    TRAIL_MIN_S, else `spent` over the time since `start`; None when neither can be measured."""
    rows = [r for r in readings or [] if isinstance(r, (list, tuple)) and len(r) == 2]
    if rows and now - float(rows[0][0]) >= TRAIL_MIN_S:
        return max(spent - float(rows[0][1]), 0.0) / ((now - float(rows[0][0])) / HOUR)
    if start is not None and now - start >= TRAIL_MIN_S:
        return max(spent, 0.0) / ((now - start) / HOUR)
    return None


def line(spent: float, cap: float, per_h: float | None, reset_at: float | None, now: float, where) -> str:
    """When `cap` is reached from `spent` at `per_h` dollars an hour, against its next reset
    `reset_at`, with times in the zone of `where` (a zone name, Project or settings). "" for an unset
    cap (0)."""
    cap = float(cap or 0)
    if cap <= 0:
        return ""
    reset = f"the reset at {timefmt.short(reset_at, where, now)}" if reset_at else ""
    if spent >= cap:
        return f"cap reached: new work waits until {reset}" if reset else \
            "cap reached, reset time unknown (rolling 24 h)"
    if per_h is None:
        return "no burn rate measured yet: no projection"
    at = f"at ${per_h:,.0f}/h" if per_h >= 10 else f"at ${per_h:.2f}/h"
    if not reset:
        return f"{at}: reset time unknown (rolling 24 h), no projection"
    if per_h <= 0:
        return f"{at}: not reached before {reset}"
    reach = now + (cap - spent) / per_h * HOUR
    if reach >= reset_at:
        return f"{at}: not reached before {reset}"
    return f"{at}: reaches the cap at {timefmt.short(reach, where, now)}, before {reset}"

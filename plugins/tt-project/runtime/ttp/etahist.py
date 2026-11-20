# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Statistics behind the ETA estimate (eta.py), model-free: percentiles of the project's own history,
a spec-size bucket, and the bounded adjustments for the host's current load and the queue's depth.

- pct(): linear-interpolated percentile (q=0.5 is the median eta.py always used; q=0.9 the conservative
  time's basis).
- size_of(): short / medium / long by spec length, the proxy for how much work a task of one kind holds.
- load_factor(), depth_factor(): a multiplier of at least 1 (never shrinks an estimate) and at most
  MAX_LOAD_X / MAX_DEPTH_X, so one busy minute cannot move an ETA far. Reading the host's load is
  read-only (hostload.loads()); `TTP_ETA_LOAD` overrides it for tests (`off`, or a load per core).
"""
from __future__ import annotations

import math
import os

MIN_SIZE = 5           # samples of one (kind, size) before it replaces the (kind, tier) samples
SIZE_EDGES = (1500, 4000)   # spec characters: below the first is short, below the second medium
SIZES = ("short", "medium", "long")
MAX_LOAD_X = 1.5       # the most the host load adds to a run leg
MAX_DEPTH_X = 2.0      # the most the queue depth adds to a queue-wait leg
P90 = 0.9


def pct(xs, q: float) -> float | None:
    """The q-quantile (0..1, out of range clamped) of the finite numbers in `xs` by linear
    interpolation; None for none. q=0.5 equals statistics.median."""
    v = sorted(float(x) for x in xs or [] if isinstance(x, (int, float)) and not isinstance(x, bool)
               and math.isfinite(x))
    if not v:
        return None
    q = 0.0 if not isinstance(q, (int, float)) or q != q else min(1.0, max(0.0, float(q)))
    k = (len(v) - 1) * q
    lo = math.floor(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def size_of(n) -> str:
    """The size bucket of a spec of `n` characters (a spec text works too); a bad length is medium."""
    if isinstance(n, str):
        n = len(n)
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n != n or n < 0:
        return SIZES[1]
    return SIZES[0] if n < SIZE_EDGES[0] else SIZES[1] if n < SIZE_EDGES[1] else SIZES[2]


def load_factor(per_core) -> float:
    """The run-leg multiplier for a host load per core: 1.0 at or below one busy process per core, then
    +0.25 per extra, at most MAX_LOAD_X. None or a bad number: 1.0."""
    if not isinstance(per_core, (int, float)) or isinstance(per_core, bool) or per_core != per_core:
        return 1.0
    return min(MAX_LOAD_X, max(1.0, 1.0 + 0.25 * (float(per_core) - 1.0)))


def depth_factor(queued, running) -> float:
    """The queue-wait multiplier for `queued` tasks waiting against `running` workers: 1.0 up to one
    queued task per running worker, then +1 per further worker's worth, at most MAX_DEPTH_X."""
    try:
        q, r = max(0, int(queued)), max(1, int(running))
    except (TypeError, ValueError):
        return 1.0
    return min(MAX_DEPTH_X, max(1.0, 1.0 + (q - r) / r))


def host_load() -> float | None:
    """This host's 1-minute load per core, or None where it cannot be read or `TTP_ETA_LOAD=off`."""
    raw = os.environ.get("TTP_ETA_LOAD")
    if raw is not None:
        try:
            v = float(raw)
        except ValueError:
            return None   # `off` or anything unreadable: no adjustment
        return v if math.isfinite(v) and v >= 0 else None
    from . import hostload
    ld = hostload.loads()
    return ld[0] / max(1, os.cpu_count() or 1) if ld else None

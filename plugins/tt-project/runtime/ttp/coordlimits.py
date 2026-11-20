# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Bound coordinator wall time after its provider and reasoning effort are resolved."""
from __future__ import annotations

import math

HARD_CEILING_S = 3600
LEGACY_DEFAULT_S = 600   # the flat turn_timeout_s every coordinator turn had before effort sizing
TURN_TIMEOUTS_S = {
    "claude": {"low": 300, "medium": 600, "high": 900, "xhigh": 1200, "max": 1500, "ultra": 1500},
    "codex": {"low": 300, "medium": 600, "high": 1200, "xhigh": 1800, "max": 2400, "ultra": 2400},
    "cursor": {"low": 300, "medium": 600, "high": 1200, "xhigh": 1800, "max": 2400, "ultra": 2400},
}


def positive(value, default: float) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else default
    except (ValueError, TypeError):
        return default


def turn_timeout(cfg: dict, provider: str, effort: str, prompt_bytes: int) -> float:
    """Base timeout, first set wins: the project's own turn_timeouts_s.<provider>.<effort>, then a
    legacy flat turn_timeout_s, then the built-in table. Then the prompt allowance, then the ceiling."""
    c = cfg.get("coordinator") or {}
    table = c.get("turn_timeouts_s") or {}
    row = table.get(provider) if isinstance(table, dict) else None
    explicit = positive(row.get(effort), 0) if isinstance(row, dict) else 0
    legacy = positive(c.get("turn_timeout_s"), 0)
    # Old projects stored the old flat default (600); that is no choice, so effort sizing still applies.
    legacy = 0 if legacy == LEGACY_DEFAULT_S else legacy
    base = explicit or legacy or TURN_TIMEOUTS_S.get(provider, {}).get(effort, LEGACY_DEFAULT_S)
    # Input size is only a modest allowance; reasoning effort is the main driver.
    extra = math.ceil(max(0, prompt_bytes - 32000) / 8000) * 60
    ceiling = min(HARD_CEILING_S, positive(c.get("turn_timeout_ceiling_s"), HARD_CEILING_S))
    return min(ceiling, base + extra)

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The per-wake dollar cap (--max-budget-usd) of a new coordinator session's first wake, model-free.

That wake carries the session's seed (coordsession.seed: the whole system prompt, context, memory,
order ledger and chat), which can cost more than one wake's cap by itself. Under the bare cap it
fails with error_max_budget_usd and the next wake seeds again, paying for the seed twice. So it gets
the per-wake cap plus the expected seed cost:

- expected(): the dearest seed of the last HISTORY new-session wakes of this project (same provider,
  same model where there are any), failed ones included. A past wake's seed is the context it wrote
  to the prompt cache plus its uncached input, priced by budget.estimate_cost, never more than the
  wake booked; never its whole cost, which also holds the wake's own work under the cap raised here,
  so each raise would feed the next. A wake that reported no cache writes is skipped. Without such
  a wake, the seed prompt's size at the model's prices (budget.estimate_cost: this project's own rate
  on the provider, else the configured fallback).
- cap(): the raise never goes past what the dollar caps and the hourly coordinator limit leave
  (turnsize.headroom), and never lowers the per-wake cap. A resumed wake is not raised here.

The project and account caps stay hard, and the wake's whole cost is booked to the coordinator as
before, so the spend guards count it."""
from __future__ import annotations

import json

HISTORY = 3
CHARS_PER_TOKEN = 4


def _seeds(db, cfg: dict, provider: str, model: str | None) -> list[float]:
    from .budget import estimate_cost
    got = []
    for r in db.q("SELECT model, cost_usd, input_tokens, cache_write_tokens, note FROM runs WHERE role='coordinator' "
                  "AND task IS NULL AND provider=? AND (? IS NULL OR model=?) AND status!='running' AND cost_usd>0 "
                  "AND cache_write_tokens>0 AND note LIKE '%\"new\"%' ORDER BY id DESC LIMIT 50",
                  (provider, model, model)):
        try:
            session = (json.loads(r["note"] or "{}").get("turn") or {}).get("session") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(session, dict) and session.get("mode") == "new":
            seed = estimate_cost(db, cfg, provider, r["model"] or "",
                                 {"cache_write": r["cache_write_tokens"] or 0, "input": r["input_tokens"] or 0})
            got.append(min(seed, float(r["cost_usd"])))
            if len(got) >= HISTORY:
                break
    return got


def expected(db, cfg: dict, provider: str, model: str, chars: int) -> tuple[float, str]:
    """(dollars, basis) a new session's seed is expected to cost: "history" or "estimate"."""
    seeds = _seeds(db, cfg, provider, model or None) or _seeds(db, cfg, provider, None)
    if seeds:
        return round(max(seeds), 2), "history"
    from .budget import estimate_cost
    tokens = max(int(chars), 0) // CHARS_PER_TOKEN
    return round(estimate_cost(db, cfg, provider, model or "", {"cache_write": tokens}), 2), "estimate"


def cap(budget_usd: float, seed_usd: float, room: float | None) -> float:
    """The first wake's cap: budget_usd plus seed_usd, the raise held within `room` (None: no limit
    known), never below budget_usd."""
    raise_by = max(float(seed_usd), 0.0)
    if room is not None:
        raise_by = min(raise_by, max(float(room) - float(budget_usd), 0.0))
    return round(float(budget_usd) + raise_by, 2)

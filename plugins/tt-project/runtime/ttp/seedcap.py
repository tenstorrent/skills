# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The per-wake dollar cap (--max-budget-usd) of a new coordinator session's first wake, model-free.

That wake carries the session's seed (coordsession.seed: the whole system prompt, context, memory,
order ledger and chat), which can cost more than one wake's cap by itself. Under the bare cap it
fails with error_max_budget_usd and the next wake seeds again, paying for the seed twice. So it gets
the per-wake cap plus the expected seed cost:

- expected(): the dearest seed of the last HISTORY new-session wakes of this project (same provider,
  same model where there are any), failed ones included. A past wake's seed is the context it wrote
  to the prompt cache plus its uncached input, at that wake's own booked rate: its cost times their
  share of its weighted tokens (budget.TOKEN_WEIGHTS; a cache write at a 1h lifetime weighs
  CACHE_WRITE_1H). Never the project-wide rate of budget.estimate_cost, which averages mostly workers
  booked far below a coordinator wake. Never its whole cost either, which also holds the wake's own
  work under the cap raised here, so each raise would feed the next. Part of its cache writes are
  its own work too, so the seed counts at most SEED_FACTOR times that wake's own seeded prompt size
  (turn.session.seed_chars: system, context and prompt with the seed; an older row without it,
  turn.prompt_bytes, the size before the seed; neither, this wake's): the cap then stays bounded
  however much each wake spends, and a seed that grew with chat and the handover note is not clipped.
  A wake that reported no cache writes is skipped. Without such a wake, the seed prompt's size at
  the model's prices (budget.estimate_cost: this project's own rate on the provider, else the
  configured fallback).
- cap(): the raise never goes past what the project and account dollar caps leave
  (turnsize.dollar_room), and never lowers the per-wake cap. The hourly coordinator limit does not
  bound it: after a few capped wakes it left no room, so a seed over the bare cap failed on every
  try and no wake ever resumed (no session spend guard, user order). A resumed wake is not raised here.

The per-wake cap itself, with coordinator.session.wake_budget_usd unset, is sized to the session
(wake_usd): WAKE_FACTOR times the dearest of the last WAKES resumed wakes that ended ok (same
provider, same model where there are any), never below the sized turn budget and never past what
the dollar caps leave. Only ok wakes count, so a capped wake never raises the next cap by itself.
With no such wake yet (a fresh project), NO_HISTORY_USD: a measured session wake costs about $0.6 to
$2.4, over the $1 default turn budget most times. A wake that still hits its cap is not lost: its
session is kept and the next wake reruns its batch resuming it (coordsession.fail), at RERUN_FACTOR
times the capped wake's cap (rerun_usd), within the dollar caps only, so a rerun never repeats the
cap that stopped it.

The project and account caps stay hard, and the wake's whole cost is booked to the coordinator as
before, so the spend guards count it."""
from __future__ import annotations

import json

HISTORY = 3
CHARS_PER_TOKEN = 4
CACHE_WRITE_1H = 2.0   # a 1h cache write costs 2x input (cachettl.py); the 5m weight is TOKEN_WEIGHTS'
# A past seed counts at most this many times its wake's prompt size in tokens: room for the agent's own
# system prompt and tool definitions, which that size leaves out, and a denser text than CHARS_PER_TOKEN,
# but not a wake's whole work. Calibrated on one live new-session wake, against the size before its seed
# (turn.prompt_bytes): it wrote 2.6 times that size / 4 to the cache. Its seeded size was not recorded,
# so the ratio to seed_chars, which includes the seed, is lower by an unmeasured amount.
SEED_FACTOR = 3.0
WAKES = 20
WAKE_FACTOR = 2.0   # room for a wake twice as dear as the dearest recent one
NO_HISTORY_USD = 3.0   # the per-wake cap before any resumed wake ended ok: over the dearest measured wake
RERUN_FACTOR = 2.0   # a capped wake's rerun gets this many times the cap that stopped it


def _size(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _seed(r, turn: dict, cfg: dict, chars: int) -> float:
    """Run row r's seed: its cache writes and uncached input at its own booked rate, at most
    SEED_FACTOR times its prompt size (the larger of turn.session.seed_chars and turn.prompt_bytes,
    else `chars`), never more than it booked."""
    from .budget import TOKEN_WEIGHTS
    w = {**TOKEN_WEIGHTS, **((cfg.get("budget") or {}).get("estimate_weights") or {})}
    if turn.get("cache_ttl") == "1h":
        w["cache_write"] = CACHE_WRITE_1H * w["input"]
    n = {k: float(r[f"{k}_tokens"] or 0) for k in ("input", "output", "cache_read", "cache_write")}
    units = sum(n[k] * w[k] for k in n)
    if units <= 0:
        return 0.0
    session = turn.get("session")
    size = max(_size(turn.get("prompt_bytes")),
               _size(session.get("seed_chars") if isinstance(session, dict) else None))
    size = size if size > 0 else int(chars)
    seed = n["cache_write"] * w["cache_write"] + n["input"] * w["input"]
    if size > 0:
        seed = min(seed, SEED_FACTOR * size / CHARS_PER_TOKEN * w["cache_write"])
    cost = float(r["cost_usd"])
    return min(cost * seed / units, cost)


def _seeds(db, cfg: dict, provider: str, model: str | None, chars: int = 0) -> list[float]:
    got = []
    for r in db.q("SELECT cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, note "
                  "FROM runs WHERE role='coordinator' "
                  "AND task IS NULL AND provider=? AND (? IS NULL OR model=?) AND status!='running' AND cost_usd>0 "
                  "AND cache_write_tokens>0 AND note LIKE '%\"new\"%' ORDER BY id DESC LIMIT 50",
                  (provider, model, model)):
        try:
            turn = json.loads(r["note"] or "{}").get("turn") or {}
            session = turn.get("session") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(session, dict) and session.get("mode") == "new":
            got.append(_seed(r, turn, cfg, chars))
            if len(got) >= HISTORY:
                break
    return got


def seed_of(db, cfg: dict, provider: str, r) -> float:
    """A past new-session wake's seed (_seed: its cache writes plus uncached input at its own booked
    rate, at most SEED_FACTOR times its prompt size, never more than it booked); r is a runs row with
    cost_usd, the four token columns and note."""
    try:
        turn = json.loads(r["note"] or "{}").get("turn") or {}
    except (ValueError, AttributeError):
        turn = {}
    return _seed(r, turn if isinstance(turn, dict) else {}, cfg, 0)


def expected(db, cfg: dict, provider: str, model: str, chars: int) -> tuple[float, str]:
    """(dollars, basis) a new session's seed is expected to cost: "history" or "estimate"."""
    seeds = _seeds(db, cfg, provider, model or None, chars) or _seeds(db, cfg, provider, None, chars)
    if seeds:
        return round(max(seeds), 2), "history"
    from .budget import estimate_cost
    tokens = max(int(chars), 0) // CHARS_PER_TOKEN
    return round(estimate_cost(db, cfg, provider, model or "", {"cache_write": tokens}), 2), "estimate"


def _wake_costs(db, provider: str, model: str | None) -> list[float]:
    got = []
    for r in db.q("SELECT cost_usd, note FROM runs WHERE role='coordinator' AND task IS NULL AND provider=? "
                  "AND (? IS NULL OR model=?) AND status='ok' AND cost_usd>0 AND note LIKE '%\"resume\"%' "
                  "ORDER BY id DESC LIMIT 100", (provider, model, model)):
        try:
            session = (json.loads(r["note"] or "{}").get("turn") or {}).get("session") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(session, dict) and session.get("mode") == "resume":
            got.append(float(r["cost_usd"]))
            if len(got) >= WAKES:
                break
    return got


def wake_usd(db, provider: str, model: str) -> float:
    """The per-wake cap a session wake's measured cost calls for: WAKE_FACTOR times the dearest of the
    last WAKES resumed wakes that ended ok (this model's, else the provider's), NO_HISTORY_USD with none."""
    costs = _wake_costs(db, provider, model or None) or _wake_costs(db, provider, None)
    return round(WAKE_FACTOR * max(costs), 2) if costs else NO_HISTORY_USD


def rerun_usd(state) -> float:
    """The per-wake cap the rerun of a wake that hit its dollar cap gets: RERUN_FACTOR times that cap
    (coordsession.fail's capped_usd), 0 for any other wake."""
    st = state if isinstance(state, dict) else {}
    try:
        capped = float(st.get("capped_usd") or 0) if st.get("rerun") else 0.0
    except (TypeError, ValueError):
        capped = 0.0
    return round(RERUN_FACTOR * capped, 2) if capped > 0 else 0.0


def cap(budget_usd: float, seed_usd: float, room: float | None) -> float:
    """The first wake's cap: budget_usd plus seed_usd, the raise held within `room` (None: no limit
    known), never below budget_usd."""
    raise_by = max(float(seed_usd), 0.0)
    if room is not None:
        raise_by = min(raise_by, max(float(room) - float(budget_usd), 0.0))
    return round(float(budget_usd) + raise_by, 2)

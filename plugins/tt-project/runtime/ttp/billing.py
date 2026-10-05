# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which spend was billed: the one rule every dollar total (project caps, budget gate, budget line,
global daily cap) uses to leave out spend a plan paid for.

Spend is billed only if the account it was spent on was billed by use when it was spent. An account
was on a plan at a time when it reported plan windows around then, so this is decided per account
and per time, never from the account the provider is on now: spend on a plan account stays plan
spend after a switch to a usage-billed account drops that plan's windows, and spend of a
usage-billed account stays billed after a switch back to a plan.

An account's plan time is made of spans of its readings, each from PLAN_GRACE_S before a reading to
PLAN_GRACE_S after the last one before a gap longer than READING_GAP_S in which PLAN_LAPSE_RUNS paid
runs of the provider ended without a reading of it (it was not in use, or not on a plan, then).
When those runs were its own or of no recorded account (a plan that expired, an API key under the
same label), the span ends at that reading without grace. The first span reaches back
PLAN_MEMORY_S, or to the last of PLAN_LAPSE_RUNS or more paid runs that ended in that time without a
reading. A run started inside a span is plan spend to its end, so work in flight at a switch keeps
the plan it started on; a run still going is judged by when it started.

Readings recorded without an account belong to whichever account was in use then, so they cover
every account's spend. Spend recorded without an account (ledger rows from before accounts were
recorded, if the runs they came from could not tell) is plan spend when any of the provider's
accounts was on a plan then, and billed otherwise.

Works on a plain sqlite3 connection, so it can read other projects' databases read-only.
"""
from __future__ import annotations

import bisect
import sqlite3
from typing import Iterable, Mapping

HOUR, DAY = 3600.0, 86400.0
# Spend this long before or after a plan reading of its account still counts as plan-billed.
PLAN_GRACE_S = HOUR
# A plan provider's windows arrive with each run or from a meter read every few minutes. A gap in an
# account's readings longer than this, in which this many paid runs ended, ends its plan span.
READING_GAP_S = 30 * 60
PLAN_LAPSE_RUNS = 2
# Readings this far back still say an account was on a plan (it may idle between them).
PLAN_MEMORY_S = 7 * DAY

Spans = dict[tuple[str, str], list[tuple[float, float]]]


def _q(conn: sqlite3.Connection, sql: str, args: Iterable = ()) -> list[tuple]:
    return [tuple(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def _count(ends: list[tuple[float, str, float]], keys: list[float], a: float, b: float | None,
           acct: str | None = None) -> list[float]:
    """End times of the runs in `ends` that ended after `a` and before `b` (None: no bound), only
    those of `acct` or of no recorded account when it is given."""
    lo = bisect.bisect_right(keys, a)
    hi = len(keys) if b is None else bisect.bisect_left(keys, b)
    return [e for e, ac, _ in ends[lo:hi] if acct is None or not acct or not ac or ac == acct]


def plan_spans(conn: sqlite3.Connection, since: float, provider: str | None = None) -> Spans:
    """(provider, account) -> the times its spend was plan-billed, for spend from `since` on.
    Account '' holds the spans of readings recorded without an account."""
    lookback = since - PLAN_MEMORY_S
    readings: dict[tuple[str, str], list[float]] = {}
    for prov, acct, ts in _q(conn, "SELECT DISTINCT provider, COALESCE(account,''), ts FROM snapshots "
                                   "WHERE ts>=? AND (? IS NULL OR provider=?) ORDER BY ts",
                             (lookback, provider, provider)):
        readings.setdefault((prov or "", acct), []).append(float(ts))
    paid: dict[str, list[tuple[float, str, float]]] = {}
    started: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for prov, acct, start, end, status, cost in _q(
            conn, "SELECT provider, COALESCE(account,''), started, ended, status, cost_usd FROM runs "
                  "WHERE ended>=? AND (? IS NULL OR provider=?) ORDER BY ended", (lookback, provider, provider)):
        if status == "ok" and float(cost or 0) > 0:
            paid.setdefault(prov or "", []).append((float(end), acct, float(start or end)))
        started.setdefault((prov or "", acct), []).append((float(start or end), float(end)))
    out: Spans = {}
    for (prov, acct), ts in readings.items():
        ends = paid.get(prov, [])
        keys = [e for e, _, _ in ends]
        # Before its first reading an account was on its plan, unless paid runs ended then without
        # one (it was not in use, or billed by use): then only since the last of them.
        before = _count(ends, keys, ts[0] - PLAN_MEMORY_S, ts[0] - PLAN_GRACE_S)
        start = before[-1] + 1e-3 if len(before) >= PLAN_LAPSE_RUNS else ts[0] - PLAN_MEMORY_S
        spans = []
        for prev, nxt in zip(ts, ts[1:] + [None]):
            if nxt is not None and (nxt - prev <= READING_GAP_S or
                                    len(_count(ends, keys, prev, nxt)) < PLAN_LAPSE_RUNS):
                continue
            lapsed = len(_count(ends, keys, prev, nxt, acct)) >= PLAN_LAPSE_RUNS
            spans.append([start, prev + (0.0 if lapsed else PLAN_GRACE_S)])
            start = (nxt or 0.0) - PLAN_GRACE_S
        # A run started while its account was on a plan runs on it to its end, after a switch too.
        runs = [r for (p, a), rs in started.items() if p == prov and (a == acct or not acct) for r in rs]
        for span in spans:
            span[1] = max([span[1]] + [e for s0, e in runs if span[0] <= s0 <= span[1]])
        out[(prov, acct)] = [(a, b) for a, b in spans]
    return out


def _covered(spans: list[tuple[float, float]], ts: float) -> bool:
    return any(a <= ts <= b for a, b in spans)


def is_plan(spans: Spans, provider: str, account: str, ts: float) -> bool:
    """Whether spend of `provider` on `account` at `ts` was paid for by a plan."""
    if account:
        return _covered(spans.get((provider, account), []), ts) or _covered(spans.get((provider, ""), []), ts)
    return any(_covered(s, ts) for (p, _), s in spans.items() if p == provider)


def billed_by_account(conn: sqlite3.Connection, since: float, until: float | None = None,
                      provider: str | None = None, exclude: Mapping[str, float] | None = None,
                      estimated_only: bool = False, running_at: float | None = None) -> dict[tuple[str, str], float]:
    """Billed spend in [since, until) per (provider, account). `exclude` maps provider -> time up to
    which its rows are left out. With `running_at`, runs still going add what they were priced at
    so far, when their account was billed by use when they started."""
    from .db import counted_spend     # db imports this module; which ledger rows count is decided there
    where, args = counted_spend(since, until, provider, exclude, estimated_only)
    rows = [(p or "", a, float(ts), float(usd or 0)) for p, a, ts, usd in _q(
        conn, f"SELECT provider, COALESCE(account,''), ts, usd FROM ledger WHERE {where}", args)]
    if running_at is not None:
        rows += [(p or "", a, float(st or running_at), float(usd or 0)) for p, a, st, usd in _q(
            conn, "SELECT provider, COALESCE(account,''), started, cost_usd FROM runs WHERE status='running' "
                  "AND (? IS NULL OR provider=?)", (provider, provider))]
    spans = plan_spans(conn, min([since] + [ts for _, _, ts, _ in rows]), provider)
    out: dict[tuple[str, str], float] = {}
    for p, a, ts, usd in rows:
        if usd and not is_plan(spans, p, a, ts):
            out[(p, a)] = out.get((p, a), 0.0) + usd
    return out

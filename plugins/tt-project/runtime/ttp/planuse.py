# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Plan-window usage for the efficiency reports (`ttp stats --landing`, `ttp audit`): a provider on a
subscription plan (Codex on a prepaid plan, Claude on a Max plan) is paid for by its usage windows, so
its list-price dollars are virtual. The reports show them apart as virtual and show what counts: the
points the plan's windows rose over the period (also per landed change). Usage-billed spend (Claude
on an Enterprise login, an API key) stays in dollars.

A run is plan spend by the rule the caps use (billing.py): the provider's declared kind
(providers.<name>.billing `plan` or `usage`), else whether its account was on a plan when it started.
Window points are read from the plan readings (snapshots): per account and window, the rises between
consecutive readings in the period, a new period (its reset moved on, or a drop) counting from 0.
The readings are the whole account's, the user's own use and other projects' included, so the
points are an upper bound on this project's share.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import billing

LABELS = {"five_hour": "5h", "5h": "5h", "seven_day": "7d", "7d": "7d"}
# A reading whose reset time moved on by more than this started a new window period.
RESET_SLACK_S = 120.0


class Classifier:
    """Tells plan runs from billed runs for spend started from `since` on."""

    def __init__(self, db: Any, since: float, cfg: Mapping | None = None) -> None:
        self.cfg = cfg or {}
        self.spans = billing.plan_spans(db.conn, since)

    def kind(self, provider: str) -> str:
        return billing.declared(self.cfg, provider) if self.cfg else "auto"

    def is_plan(self, provider: str | None, account: str | None, started: float | None) -> bool:
        prov = provider or ""
        k = self.kind(prov)
        if k != "auto":
            return k == "plan"
        return bool(started) and billing.is_plan(self.spans, prov, account or "", float(started))


def window_points(db: Any, provider: str, since: float, until: float) -> dict[str, float]:
    """{window label: points its windows rose in [since, until)}, summed over the provider's accounts."""
    rows = db.q("SELECT ts, COALESCE(account,'') account, window, utilization, resets_at FROM snapshots "
                "WHERE provider=? AND ts<? ORDER BY ts", (provider, until))
    last: dict[tuple, Any] = {}
    out: dict[str, float] = {}
    for r in rows:
        key = (r["account"], r["window"])
        prev, last[key] = last.get(key), r
        if float(r["ts"]) < since or prev is None:
            continue
        util, before = float(r["utilization"] or 0), float(prev["utilization"] or 0)
        new_period = (r["resets_at"] and prev["resets_at"]
                      and float(r["resets_at"]) > float(prev["resets_at"]) + RESET_SLACK_S) or util < before
        rise = util if new_period else util - before
        if rise > 0:
            label = LABELS.get(r["window"], r["window"])
            out[label] = out.get(label, 0.0) + rise
    return {k: round(v, 1) for k, v in sorted(out.items())}


def summary(db: Any, since: float, until: float, cfg: Mapping | None = None,
            landed: int | None = None, cls: Classifier | None = None) -> list[dict]:
    """Per provider with plan spend in [since, until): {provider, virtual_usd, runs, windows, per_landed}.
    `windows` maps a window label to the points it rose; `per_landed` divides them by `landed`."""
    cls = cls or Classifier(db, since, cfg)
    got: dict[str, dict] = {}
    for r in db.q("SELECT provider, account, started, cost_usd FROM runs WHERE started>=? AND started<?",
                  (since, until)):
        if cls.is_plan(r["provider"], r["account"], r["started"]):
            g = got.setdefault(r["provider"] or "?", {"virtual_usd": 0.0, "runs": 0})
            g["virtual_usd"] += float(r["cost_usd"] or 0)
            g["runs"] += 1
    out = []
    for prov in sorted(got):
        pts = window_points(db, prov, since, until)
        out.append({"provider": prov, "virtual_usd": round(got[prov]["virtual_usd"], 2), "runs": got[prov]["runs"],
                    "windows": pts,
                    "per_landed": {k: round(v / landed, 2) for k, v in pts.items()} if landed else None})
    return out


def text(rows: list[dict]) -> str:
    """'codex on a plan: 7d +12 points, 5h +40 points (1.5 and 5 per landed change; whole account);
    3 runs $3.20 virtual (list price, not billed)'; '' with no plan spend."""
    parts = []
    for r in rows:
        w = ", ".join(f"{k} +{v:g} points" for k, v in r["windows"].items()) or "no window readings"
        per = r.get("per_landed")
        per_s = (" (" + ", ".join(f"{k} {v:g}" for k, v in per.items()) + " per landed change; whole account)"
                 if per else " (whole account)" if r["windows"] else "")
        parts.append(f"{r['provider']} on a plan: {w}{per_s}; {r['runs']} run{'s' if r['runs'] != 1 else ''} "
                     f"${r['virtual_usd']:.2f} virtual (list price, not billed)")
    return "; ".join(parts)

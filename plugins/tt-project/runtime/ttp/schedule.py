# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recurring work. Three kinds, cheapest first:
- `command`: a deterministic script (no model). Its stdout lines become observations.
- `watcher`: a built-in probe (pull requests, CI, logs) — also no model.
- `llm`: a scheduled task for a worker (daily review, audits). Budgeted per day, off when the
  governor says optional work must wait.

A schedule that was missed while the machine slept runs once on wake, never once per missed slot.
An llm schedule skipped because the budget gate holds optional work keeps its period open and
retries every half hour (or its own period, if shorter) until the gate allows it.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

from .db import DB

_EVERY = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_every(spec: str | int) -> int:
    if isinstance(spec, int):
        return spec
    if str(spec).strip().isdigit():   # a bare number is seconds
        return int(str(spec).strip())
    m = _EVERY.match(str(spec))
    if not m:
        raise ValueError(f"bad interval {spec!r}; use e.g. 5m, 1h, 1d or seconds")
    return int(m.group(1)) * _UNIT[m.group(2)]


def next_run(every_s: int, at: str | None, after: float) -> float:
    """Next fire time strictly after `after`. `at` ("HH:MM", local) anchors day-scale schedules."""
    if at and every_s >= 86400:
        hh, mm = (int(x) for x in at.split(":"))
        base = datetime.fromtimestamp(after).replace(hour=hh, minute=mm, second=0, microsecond=0)
        while base.timestamp() <= after:
            base += timedelta(seconds=every_s)
        return base.timestamp()
    return after + every_s


def upsert(db: DB, name: str, kind: str, every: str | int, at: str | None = None, enabled: bool = True,
           budget_usd_day: float | None = None, description: str = "", payload: dict | None = None) -> None:
    every_s = parse_every(every)
    existing = db.one("SELECT * FROM schedules WHERE name=?", (name,))
    nxt = existing["next_run"] if existing and existing["next_run"] else next_run(every_s, at, time.time())
    db.x("INSERT INTO schedules(name,kind,every_s,at,enabled,budget_usd_day,description,payload,next_run) "
         "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, every_s=excluded.every_s, "
         "at=excluded.at, enabled=excluded.enabled, budget_usd_day=excluded.budget_usd_day, "
         "description=excluded.description, payload=excluded.payload",
         (name, kind, every_s, at, int(enabled), budget_usd_day, description, json.dumps(payload or {}), nxt))


def due(db: DB, now: float | None = None) -> list[dict]:
    now = now or time.time()
    return db.q("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run",
                (now,))


BUDGET_RETRY_S = 1800


def budget_skipped(status: str | None) -> bool:
    """The budget gate held this run back; it is owed, not done."""
    return bool(status) and status.startswith("skipped: budget ")


def mark_ran(db: DB, sched: dict, status: str, now: float | None = None) -> None:
    now = now or time.time()
    if budget_skipped(status):
        # Keep last_run so the period still counts as not run, and retry soon instead of a full period on.
        db.x("UPDATE schedules SET last_status=?, next_run=? WHERE name=?",
             (status, now + min(BUDGET_RETRY_S, sched["every_s"]), sched["name"]))
        return
    db.x("UPDATE schedules SET last_run=?, last_status=?, next_run=? WHERE name=?",
         (now, status, next_run(sched["every_s"], sched["at"], now), sched["name"]))


def failing(status: str | None) -> bool:
    """A run that did nothing because the schedule itself is broken, not because it chose to skip."""
    return bool(status) and (status == "no command" or status.startswith("error"))


def broken(db: DB) -> list[dict]:
    """Enabled schedules whose last run failed, for `ttp status` and the web app."""
    return [r for r in db.q("SELECT name, kind, last_run, last_status FROM schedules WHERE enabled=1 ORDER BY name")
            if failing(r["last_status"])]


def broken_line(db: DB) -> str:
    """The broken schedules in one line, or ""."""
    rows = broken(db)
    return ("schedules failing: " + "; ".join(f"{r['name']} ({r['last_status'][:80]})" for r in rows)) if rows else ""


def waiting_line(db: DB) -> str:
    """Enabled schedules held back by the budget gate, in one line, or ""."""
    rows = [r["name"] for r in db.q("SELECT name, last_status FROM schedules WHERE enabled=1 ORDER BY name")
            if budget_skipped(r["last_status"])]
    return ("schedules waiting for budget: " + ", ".join(rows)) if rows else ""


def spent_today(db: DB, name: str) -> float:
    row = db.one("SELECT COALESCE(SUM(usd),0) s FROM ledger WHERE ts>=? AND source=?",
                 (time.time() - 86400, f"schedule:{name}"))
    return float(row["s"]) if row else 0.0


def with_costs(db: DB) -> list[dict]:
    """Schedules annotated with their last-7-day cost, for the web app's recurring pane."""
    rows = db.q("SELECT * FROM schedules ORDER BY name")
    week = time.time() - 7 * 86400
    for r in rows:
        c = db.one("SELECT COALESCE(SUM(usd),0) s, COUNT(*) n FROM ledger WHERE ts>=? AND source=?",
                   (week, f"schedule:{r['name']}"))
        r["cost_7d"] = round(float(c["s"]), 2)
        r["payload"] = json.loads(r["payload"] or "{}")
    return rows

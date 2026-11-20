# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Routine defaults: the daemon settles three routine coordinator triggers itself, without a model call.

- A project's own machine change about to expire (within a tenth of its span) whose task is still open,
  or whose until-probe still says not yet, is renewed by its original span (machine_ledger.Probes._renew):
  while its until-task or until-probe holds, and at most RENEW_MAX times for one kept open only by the
  task that made it; it never runs an undo. A no-op undo still closes the entry
  (machine_ledger.auto_close_rule); anything else still pages.
- A task at its run cap is held, blocked, with its summary (runcap.check); it wakes the coordinator
  only when the same task reaches its cap again.
- A new probe stall re-runs its probe first (progress.check): a pass ends the stall, and only a probe
  that still says not yet wakes the coordinator.

Each default applied is recorded here (the project db's KV_KEY), shown once in the next coordinator
digest (digest_lines), in the daily review (audit_line) and in `ttp audit fixes`. One switch turns them
all off: coordinator.routine_defaults (default true); false wakes the coordinator as before."""
from __future__ import annotations

import time
from typing import Any

KEY = "routine_defaults"     # coordinator.<KEY>: false turns every routine default off
KV_KEY = "routine_defaults"  # the applied defaults, newest last
KEEP = 300                   # records kept
RENEW_MAX = 2                # renewals of one machine change before it pages
RENEW_MIN_S, RENEW_MAX_S = 3600, 86400   # one renewal: the entry's own span, within these
SHOWN = 8


def on(cfg: dict | None) -> bool:
    return (((cfg or {}).get("coordinator") or {}).get(KEY, True)) is not False


def record(db, kind: str, key: str, what: str, undo: str = "", now: float | None = None) -> None:
    """Log one applied default: `kind` (renew, run_cap, stall), the item's `key`, what was done and its undo."""
    recs = list(db.kv(KV_KEY, []) or [])
    recs.append({"ts": time.time() if now is None else now, "kind": kind, "key": key, "what": what[:300],
                 "undo": undo[:200]})
    db.set_kv(KV_KEY, recs[-KEEP:])


def entries(db, since: float = 0.0, until: float | None = None) -> list[dict]:
    until = time.time() + 1 if until is None else until
    return [r for r in db.kv(KV_KEY, []) or [] if isinstance(r, dict) and since <= float(r.get("ts") or 0) < until]


def seen(db, kind: str, key: str) -> bool:
    """Whether a default was already applied to this item: the same item back wakes the coordinator."""
    return any(r.get("kind") == kind and r.get("key") == key for r in entries(db))


def _line(r: dict) -> str:
    return f"- {r['kind']} {r['key']}: {r['what']}" + (f" (undo: {r['undo']})" if r.get("undo") else "")


def digest_lines(db, since: float, now: float | None = None) -> list[str]:
    """The defaults applied since `since` (the last coordinator turn's start): shown once."""
    rows = entries(db, since, now)
    if not rows:
        return []
    return ([f"## Routine defaults applied since your last turn: {len(rows)} (no wake; the same item back "
             f"wakes you; off: coordinator.{KEY} false)"]
            + [_line(r) for r in rows[-SHOWN:]]
            + ([f"- and {len(rows) - SHOWN} more (`ttp audit fixes`)"] if len(rows) > SHOWN else []))


def audit_line(db, since: float, now: float | None = None) -> str:
    rows = entries(db, since, now)
    if not rows:
        return ""
    by: dict[str, int] = {}
    for r in rows:
        by[r["kind"]] = by.get(r["kind"], 0) + 1
    return "routine defaults (no coordinator wake): " + ", ".join(f"{k} {n}" for k, n in sorted(by.items()))


def fixes_text(db, since: float, zone: Any, now: float | None = None) -> str:
    from . import timefmt
    rows = entries(db, since, now)
    return "\n".join([f"routine defaults: {len(rows)}"]
                     + [f"  {timefmt.long(float(r['ts']), zone)} {_line(r)[2:]}" for r in rows]) if rows else ""

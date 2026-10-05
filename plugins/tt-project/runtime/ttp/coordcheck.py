# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev's 'routine or needs thought?' check on a coordinator turn: one more trigger for a high-effort turn.

It runs only on a turn the rules (coordinator.effort_triggers) left below coordinator.unblock_effort,
where its `coord_effort` use is allowed (see jevuse; `jev.uses.coord_effort` = "off" turns it off).
Jev reads a compact summary of what the turn will see and rates four reasons a turn may need thought;
any one at THRESHOLD or above makes it 'needs_thought' and the turn runs at unblock_effort. Every other
outcome, an error included, leaves the rules' choice; JevOutOfFunds reaches the caller for its alert.

Each call is logged in jev_calls with its verdict, the effort it led to and whether that escalated
the turn, and once the turn ends (`settle`) with what the turn did. A needs_thought call is counted as
saving one low-effort turn (the one that would have failed or handed off to a high one) when its turn
acted; it is wrong when its turn did nothing. A routine call saves nothing; it is wrong when its turn
failed or had actions rejected.
"""
from __future__ import annotations

import time

from . import jevuse
from .db import DB

JEV_USE = "coord_effort"
THRESHOLD = 0.5
SUMMARY_CHARS = 4000
EVENT_CHARS = 300
REASONS = {
    "stuck": ("Is work stuck: a failure, a block, a repeated problem or something waiting with no way forward?",
              "A failure, blocked or repeatedly waiting work, a stall, or an error to work around.",
              "Progress as planned: finished tasks, routine reports, nothing stuck."),
    "decision": ("Does the turn have to make a real judgement call or trade-off?",
                 "Choosing between options, weighing risk, cost or priorities, or answering a hard question.",
                 "Only bookkeeping: queue the obvious next step, acknowledge, record."),
    "conflict": ("Are instructions, results or goals conflicting or unclear?",
                 "Contradicting instructions or results, an unclear spec, or surprising evidence.",
                 "Everything is consistent and clear."),
    "risk": ("Could a wrong move here be costly or hard to undo?",
             "Spend, deletions, pushes, releases, shared machines or anything irreversible is at stake.",
             "Cheap, reversible steps only."),
}


def summary(db: DB, event_ids: list[int], wake_due: str | None = None) -> str:
    """What the turn will see, compactly: its new events and the counts that make a turn hard."""
    lines = [f"wake: {wake_due}"] if wake_due else []
    if event_ids:
        rows = db.q(f"SELECT kind, severity, text FROM events WHERE id IN ({','.join('?' * len(event_ids))}) "
                    "ORDER BY id", list(event_ids))
        lines += [f"event {r['kind']} [{r['severity']}]: {' '.join(str(r['text']).split())[:EVENT_CHARS]}"
                  for r in rows]
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    lines.append("tasks: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"))
    asks = db.one("SELECT COUNT(*) n FROM messages WHERE kind='ask' AND handled=0")["n"]
    rejected = len(db.kv("rejected_actions", []) or [])
    fails = int(db.kv("coordinator_failures", 0) or 0)
    lines.append(f"open asks {asks}, actions rejected last turn {rejected}, failed turns in a row {fails}")
    return "\n".join(lines)[:SUMMARY_CHARS]


def check(db: DB, cfg: dict, jev, text: str, rules_effort: str, high_effort: str) -> dict | None:
    """Jev's verdict on a turn the rules leave at `rules_effort`: {"verdict", "reason", "effort",
    "escalated", "jev_call"}, or None when the check is off or Jev gave no answer. Any error but
    JevOutOfFunds is the caller's to treat as None."""
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, JEV_USE):
        return None
    ans = jev.decide(text, {k: {"type": "noul", "instructions": q, "criteria": {"true": t, "false": f}}
                            for k, (q, t, f) in REASONS.items()}, purpose=JEV_USE, timeout=10.0)
    if ans is None:
        return None
    ps = {}
    for k in REASONS:
        try:
            ps[k] = round(float((ans.get(k) or {}).get("noul")), 2)
        except (TypeError, ValueError):
            pass
    if not ps:
        return None
    top = max(ps, key=ps.get)
    thought = ps[top] >= THRESHOLD
    effort = high_effort if thought else rules_effort
    out = {"verdict": "needs_thought" if thought else "routine", "reason": f"{top} {ps[top]:.2f}",
           "p": ps, "rules_effort": rules_effort, "effort": effort, "escalated": effort != rules_effort}
    out["jev_call"] = jevuse.record(db, JEV_USE, out, getattr(jev, "last_cost", 0.0),
                                    avoided_usd=jevuse.mean_turn_cost(db, cfg) if thought else 0.0)
    return out


def settle(db: DB, call_id: int, verdict: str, status: str, actions: list | None, problems: list[str]) -> None:
    """Log what the checked turn did next to its call, and whether the verdict held up."""
    kinds: dict[str, int] = {}
    for a in actions or []:
        k = str(a.get("type") if isinstance(a, dict) else a)
        kinds[k] = kinds.get(k, 0) + 1
    did = ", ".join(f"{k} x{n}" if n > 1 else k for k, n in sorted(kinds.items())) or "no actions"
    note = f"turn {status}; {did}" + (f"; {len(problems)} rejected" if problems else "")
    if verdict == "needs_thought":
        if status != "ok":   # unscored: a failed turn says nothing about the verdict
            db.x("UPDATE jev_calls SET note=? WHERE id=? AND outcome IS NULL", (note[:300], call_id))
            return
        jevuse.resolve(db, call_id, bool(kinds), note, now=time.time())
    else:
        jevuse.resolve(db, call_id, status == "ok" and not problems, note, now=time.time())

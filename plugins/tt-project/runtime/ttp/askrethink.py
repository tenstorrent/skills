# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Ask rethink: an open ask that holds work for long goes back to the coordinator to decide.

An open ask whose `blocking` is restriction, irreversible or human, that holds at least one blocked
task (its anchor `waits:ask:<id>`, or the task's blocked reason cites `ask <id>`), and that has been
open longer than coordinator.ask_rethink_h (default 2; 0 off) raises one KIND event. Its kind is a
'stuck' effort trigger (coordinator.EFFORT_EVENT_TRIGGERS), so the turn it wakes is a tricky one
and either decides the ask itself or records why only the user can (the blocker sweep's
`human_indispensable`). Each ask raises it once, across daemon restarts (kv RAISED_KEY). A review,
merge, access, funds or spend ask is never raised: only the user can answer those. The blocker sweep
flags, rather than carries, a `still_blocked` on a task such an ask holds (held_tasks). A task held on
a user-only setting (settinghold.py) counts for none: only the user may turn the setting on.
"""
from __future__ import annotations

import json
import re
import time

from .db import DB

KIND = "ask_rethink"
RAISED_KEY = "ask_rethink_raised"   # kv: {ask id: ts raised}, for asks still open
REASONS = ("restriction", "irreversible", "human")
DEFAULT_H = 2.0
_REF = "blocking:"   # prguard.BLOCKING_REF


def hours(cfg: dict | None) -> float:
    """coordinator.ask_rethink_h: how long an ask may hold work before it is raised (0: off)."""
    try:
        v = ((cfg or {}).get("coordinator") or {}).get("ask_rethink_h", DEFAULT_H)
        return max(0.0, float(DEFAULT_H if v is None else v))
    except (TypeError, ValueError):
        return DEFAULT_H


def _cites(reason: str, ask_id: int) -> bool:
    return bool(re.search(rf"\bask\s*#?{int(ask_id)}(?!\d)", reason or "", re.I))


def held(db: DB, cfg: dict | None, now: float | None = None) -> dict[int, dict]:
    """{ask id: {"ask": row, "tasks": [task ids], "age_h"}} for each open ask past the threshold that
    may be rethought and holds at least one blocked task."""
    from .settinghold import held_key
    from .waits import _held_ask
    now = time.time() if now is None else now
    h = hours(cfg)
    if h <= 0:
        return {}
    asks = [a for a in db.q("SELECT id, ts, text, ref FROM messages WHERE direction='out' AND kind='ask' "
                            "AND handled=0 AND ts<=? ORDER BY id", (now - h * 3600,))
            if (a["ref"] or "").startswith(_REF) and a["ref"][len(_REF):] in REASONS]
    if not asks:
        return {}
    blocked = [t for t in db.q("SELECT id, status, labels, blocked_reason FROM tasks WHERE status='blocked' ORDER BY id")
               if not held_key(t)]
    out = {}
    for a in asks:
        tasks = [t["id"] for t in blocked if _held_ask(t) == a["id"] or _cites(t["blocked_reason"], a["id"])]
        if tasks:
            out[a["id"]] = {"ask": a, "tasks": tasks, "age_h": (now - float(a["ts"])) / 3600}
    return out


def held_tasks(db: DB, cfg: dict | None, now: float | None = None) -> dict[int, int]:
    """{task id: ask id} for each blocked task such an ask holds."""
    return {t: aid for aid, x in held(db, cfg, now).items() for t in x["tasks"]}


def text(ask_id: int, tasks: list[int], age_h: float, question: str = "", blocking: str = "") -> str:
    s = (f"Ask {ask_id} has blocked tasks {', '.join(f'#{t}' for t in tasks)} for {age_h:.0f}h. Decide it "
         f"yourself by the least-disruptive way unless only the user can; then use the blocker sweep's "
         f"human_indispensable with a why. To decide: act, resolve the ask, memory_add the decision, notify "
         f"low with what you chose and that a reply reverses it.")
    if question:
        s += f" The question ({blocking or 'blocking'}) was: {' '.join(question.split())[:300]}"
    return s


def raise_due(db: DB, cfg: dict | None, now: float | None = None) -> list[int]:
    """Queue one KIND event per ask that newly qualifies; returns their ask ids."""
    now = time.time() if now is None else now
    found = held(db, cfg, now)
    with db.tx():
        raised = db.kv(RAISED_KEY, {}) or {}
        open_ids = {str(r["id"]) for r in db.q("SELECT id FROM messages WHERE kind='ask' AND handled=0")}
        keep = {k: v for k, v in raised.items() if k in open_ids}
        new = [aid for aid in found if str(aid) not in keep]
        for aid in new:
            x = found[aid]
            question = x["ask"]["text"].split("\n")[0]
            db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
                 (now, "daemon", KIND, "normal",
                  text(aid, x["tasks"], x["age_h"], question, x["ask"]["ref"][len(_REF):]),
                  json.dumps({"ask": aid, "tasks": x["tasks"]}), "queued"))
            keep[str(aid)] = now
        if keep != raised:
            db.set_kv(RAISED_KEY, keep)
    return new

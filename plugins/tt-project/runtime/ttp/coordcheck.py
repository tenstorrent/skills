# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Jev's 'routine or needs thought?' check on a coordinator turn: one more trigger for a high-effort turn.

It runs only on a turn the rules (coordinator.effort_triggers) left below coordinator.unblock_effort,
where its `coord_effort` use is allowed (see jevuse; `jev.uses.coord_effort` = "off" turns it off),
and only when the turn has something worth escalating that Jev has not raised a turn for already
(`worth_asking`): a new failed or blocked task, a high or critical event, a user message, or an external
wait older than coordinator.jev_wait_h. Otherwise the call is skipped, logged in the turn's note as
skipped and not in jev_calls, so it is neither charged nor scored.
Jev reads a compact summary of what the turn will see and rates four reasons a turn may need thought;
any one at coordinator.jev_threshold (THRESHOLD) or above makes it 'needs_thought' and the turn runs at
unblock_effort; the events and messages it raised for are remembered (RAISED_KEY) and never raise
another turn. Every other outcome, an error included, leaves the rules' choice; JevOutOfFunds reaches
the caller for its alert.

Each call is logged in jev_calls with its verdict, the effort it led to and whether that escalated
the turn, and once the turn ends (`settle`) with what the turn did. A needs_thought call is counted as
saving one low-effort turn (the one that would have failed or handed off to a high one) when its turn
took an action beyond routine bookkeeping (ROUTINE_ACTIONS), less what the raised turn cost above a
routine one. When its turn took only routine actions, a low-effort turn would have acted the same: the
call is scored 'no change' and saves nothing, and that extra cost counts as a negative saving. It is
wrong when its turn did nothing, with the same negative saving. A turn that failed, was lost, shut down or logged out
leaves the call unscored with no saving. A routine call saves nothing; it is wrong when its turn
failed or had actions rejected.
"""
from __future__ import annotations

import json
import time

from . import coordinator as coord
from . import jevuse
from .db import DB

JEV_USE = "coord_effort"
THRESHOLD = 0.7       # default of coordinator.jev_threshold
WAIT_H = 6.0          # default of coordinator.jev_wait_h
WORTH_KINDS = ("task_failed", "task_blocked")
RAISED_KEY = "coord_check_raised"   # kv: event ("e<id>"), message ("m<id>") and wait stint
                                    # ("w<task>:<stint start>") keys Jev raised a turn for
RAISED_KEEP = 500
SUMMARY_CHARS = 4000
# What a routine (low-effort) turn does: queue the obvious next step, acknowledge, record. A raised turn
# that took only these acted as a low-effort turn would have.
ROUTINE_ACTIONS = frozenset(("noop", "reply", "notify", "resolve", "task_add", "task_update", "memory_add"))
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


def _num(cfg: dict, key: str, default: float) -> float:
    try:
        return float((cfg.get("coordinator") or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def worth_asking(db: DB, cfg: dict, event_ids: list[int], msg_ids: list[int] | None = None,
                 now: float | None = None) -> tuple[list[str], str]:
    """The keys of what makes the turn worth Jev's call ("e<id>" events, "m<id>" messages,
    "w<task>:<stint start>" waits) that Jev has not raised a turn for yet, and why: a new task_failed
    or task_blocked event, a high or critical event, a user message, or a task_waiting event of a task
    whose stint of external waits began over coordinator.jev_wait_h ago (keyed by the stint, so a task
    that keeps re-waiting raises once per stint; the rules raise a changed wait reason). ([], why not)
    when there is none."""
    now = time.time() if now is None else now
    keys: list[str] = [f"m{i}" for i in msg_ids or []]
    why = {"user message"} if keys else set()
    rows = db.q(f"SELECT id, kind, severity, task FROM events WHERE id IN ({','.join('?' * len(event_ids))}) "
                "ORDER BY id", list(event_ids)) if event_ids else []
    wait_s = _num(cfg, "jev_wait_h", WAIT_H) * 3600
    for r in rows:
        key = f"e{r['id']}"
        if r["kind"] in WORTH_KINDS:
            why.add(r["kind"])
        elif r["severity"] in coord.EFFORT_SEVERITIES:
            why.add(f"{r['severity']} event")
        elif (r["kind"] == "task_waiting" and r["task"]
              and (stint := coord.wait_stint(db, r["task"], now, coord.live_max(cfg))[1])
              and now - stint[-1]["ts"] >= wait_s):
            why.add(f"waiting over {wait_s / 3600:g} h")
            key = f"w{r['task']}:{stint[-1]['ts']}"   # one raise per stint, however often it re-waits
        else:
            continue
        if key not in keys:
            keys.append(key)
    if not keys:
        return [], "no failed or blocked task, high event, user message or long wait"
    raised = set(db.kv(RAISED_KEY, []) or [])
    new = [k for k in keys if k not in raised]
    return (new, ", ".join(sorted(why))) if new else ([], "already raised for these events")


def check(db: DB, cfg: dict, jev, text: str, rules_effort: str, high_effort: str,
          event_ids: list[int] | None = None, msg_ids: list[int] | None = None) -> dict | None:
    """Jev's verdict on a turn the rules leave at `rules_effort`: {"verdict", "reason", "effort",
    "escalated", "jev_call"}, {"verdict": "skipped", "reason"} (no call: nothing worth asking about,
    see worth_asking), or None when the check is off or Jev gave no answer. Any error but
    JevOutOfFunds is the caller's to treat as None."""
    if jev is None or not jev.enabled() or not jevuse.allowed(db, cfg, JEV_USE):
        return None
    keys, why = worth_asking(db, cfg, list(event_ids or []), msg_ids)
    if not keys:
        return {"verdict": "skipped", "reason": why, "jev_call": None, "escalated": False}
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
    thought = ps[top] >= _num(cfg, "jev_threshold", THRESHOLD)
    effort = high_effort if thought else rules_effort
    out = {"verdict": "needs_thought" if thought else "routine", "reason": f"{top} {ps[top]:.2f}",
           "p": ps, "rules_effort": rules_effort, "effort": effort, "escalated": effort != rules_effort}
    out["jev_call"] = jevuse.record(db, JEV_USE, out, getattr(jev, "last_cost", 0.0),
                                    avoided_usd=jevuse.mean_turn_cost(db, cfg) if thought else 0.0,
                                    changed=out["escalated"])
    if thought:
        raised = [k for k in db.kv(RAISED_KEY, []) or [] if k not in keys] + keys
        db.set_kv(RAISED_KEY, raised[-RAISED_KEEP:])
    return out


def routine_turn_cost(db: DB, cfg: dict, effort: str, now: float | None = None) -> float:
    """The mean cost of a finished coordinator turn at `effort` (the rules' choice) over the window:
    what the turn would have cost had Jev not raised it."""
    now = time.time() if now is None else now
    row = db.one("SELECT AVG(cost_usd) c FROM runs WHERE role='coordinator' AND status!='running' "
                 "AND cost_usd>0 AND effort=? AND started>=?", (effort, now - jevuse.window_s(cfg)))
    return float(row["c"]) if row and row["c"] else jevuse.mean_turn_cost(db, cfg, now)


def extra_cost(db: DB, cfg: dict, call_id: int, turn_cost: float, now: float | None = None) -> float:
    """What a raised turn cost above a routine one: its cost minus the mean turn at the call's
    rules_effort (0 when the call did not raise the turn)."""
    row = db.one("SELECT decision FROM jev_calls WHERE id=?", (call_id,))
    try:
        dec = json.loads(row["decision"]) if row else {}
    except (TypeError, ValueError):
        dec = {}
    if not isinstance(dec, dict) or not dec.get("escalated"):
        return 0.0
    return max(0.0, float(turn_cost or 0) - routine_turn_cost(db, cfg, str(dec.get("rules_effort") or ""), now))


def unscored(db: DB, call_id: int, status: str) -> None:
    """A turn that failed, was lost, shut down or logged out says nothing about the verdict: the call
    stays unscored and claims no saving."""
    db.x("UPDATE jev_calls SET avoided_usd=0, note=? WHERE id=? AND outcome IS NULL",
         (f"turn {status}; unscored", call_id))


def settle(db: DB, call_id: int, verdict: str, status: str, actions: list | None, problems: list[str],
           extra_usd: float = 0.0) -> None:
    """Log what the checked turn did next to its call, and whether the verdict held up. `extra_usd` is
    what the raised turn cost above a routine one (extra_cost): it comes off the call's saving, and a
    wrong call is charged it as a negative saving."""
    kinds: dict[str, int] = {}
    for a in actions or []:
        k = str(a.get("type") if isinstance(a, dict) else a)
        kinds[k] = kinds.get(k, 0) + 1
    did = ", ".join(f"{k} x{n}" if n > 1 else k for k, n in sorted(kinds.items())) or "no actions"
    note = f"turn {status}; {did}" + (f"; {len(problems)} rejected" if problems else "")
    if verdict == "needs_thought":
        if status != "ok":   # unscored: a failed turn says nothing about the verdict
            db.x("UPDATE jev_calls SET avoided_usd=0, note=? WHERE id=? AND outcome IS NULL", (note[:300], call_id))
            return
        right = bool(kinds)
        if right and set(kinds) <= ROUTINE_ACTIONS:   # acted as a low-effort turn would have
            right = jevuse.NO_CHANGE
        if jevuse.resolve(db, call_id, right, note, now=time.time()) and extra_usd > 0:
            if right is True:
                db.x("UPDATE jev_calls SET avoided_usd=avoided_usd-? WHERE id=?", (float(extra_usd), call_id))
            else:
                db.x("UPDATE jev_calls SET avoided_usd=? WHERE id=?", (-float(extra_usd), call_id))
    else:
        jevuse.resolve(db, call_id, status == "ok" and not problems, note, now=time.time())

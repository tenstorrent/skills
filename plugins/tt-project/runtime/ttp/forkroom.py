# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Room for a forked run: a task run starting from a copy of the coordinator's session (ctxfork.py)
pays for that copy on its first call, so a task with little budget left could spend it all there and
stop before doing any work.

At dispatch (Daemon._dispatch), model-free: the first call is estimated from the first-call token
counts recorded by this provider's (and model's) recent forked runs, priced at the project's own rate
(budget.estimate_cost), else from the coordinator session's size as a cache write (a run on another
model: its last turns'). When the budget left minus that estimate is under floor(), the task's
budget is raised by the shortfall, once per task, within what the dollar caps leave
(turnsize.dollar_room): the caps themselves are never raised, and the max-runs and no-progress stops
stay. Each raise is one handled KIND event."""
from __future__ import annotations

import json
import time

KIND = "fork_budget_raise"
FLOOR_USD = 2.0   # what a run needs past its first call to read its task, change something and hand off
RUNS = 20         # recent forked runs whose first calls set the estimate


def _first_calls(db, provider: str, model: str | None) -> list[dict]:
    from . import ctxfork
    got = []
    for r in db.q("SELECT note FROM runs WHERE provider=? AND (? IS NULL OR model=?) AND role IN ('worker','reviewer') "
                  "AND note LIKE '%\"fork\"%' ORDER BY id DESC LIMIT 200", (provider, model, model)):
        try:
            note = json.loads(r["note"] or "{}")
        except ValueError:
            continue
        if not isinstance(note, dict):
            continue
        tel = note.get("cache_telemetry") or {}
        if (note.get(ctxfork.KEY) or {}).get("how") == "fork" and int(tel.get("first_context") or 0) > 0:
            read = int(tel.get("first_cache_read") or 0)
            got.append({"cache_read": read, "cache_write": max(int(tel["first_context"]) - read, 0)})
            if len(got) >= RUNS:
                break
    return got


def estimate(db, cfg: dict, provider: str, model: str | None, tail: int = 0) -> tuple[float, str]:
    """(dollars, basis) a forked run's first call is expected to cost: "last turns" (a run on another
    model copies only `tail` tokens), "history" or "session size"."""
    from . import coordsession
    from .budget import estimate_cost
    if tail:
        return round(estimate_cost(db, cfg, provider, model or "", {"cache_write": tail}), 2), "last turns"
    calls = _first_calls(db, provider, model) or _first_calls(db, provider, None)
    if calls:   # one price for the largest call seen, an upper bound on each
        top = {k: max(c[k] for c in calls) for k in ("cache_read", "cache_write")}
        return round(estimate_cost(db, cfg, provider, model or "", top), 2), "history"
    size = int(float((db.kv(coordsession.KEY) or {}).get("context_tokens") or 0))
    return round(estimate_cost(db, cfg, provider, model or "", {"cache_write": size}), 2), "session size"


def floor(budget: float, est: float) -> float:
    """What a forked run needs left past its first call: FLOOR_USD (half the budget, for a smaller one),
    and at least twice that call, since each later call reads the copied session again."""
    return max(min(FLOOR_USD, budget / 2), 2 * est)


def _copies(p, cfg: dict, task: dict, provider: str, model: str | None, now: float) -> int | None:
    """What this run copies of the coordinator's session, by ctxfork.prepare's own checks: None for no
    fork (a blind review, a retry whose copy failed, a model, provider or session that cannot take it),
    else the last turns' tokens of a run on another model, 0 for the whole session."""
    from . import blindreview, ctxfork
    from .providers import get_provider
    if ctxfork.mode(cfg) != "fork" or blindreview.is_blind_task(task):
        return None
    try:
        prov = get_provider(provider)
        tail = ctxfork._other_model_tail(p, cfg, prov, model)
        if ("" if tail else ctxfork._model_gap(p, model)) or ctxfork._role_missed(p, task["id"]) \
                or ctxfork._fork_gap(p, cfg, prov, provider, "", None, now) \
                or ctxfork._fork_rejected(p, task["id"], "fork"):
            return None
        return tail
    except Exception:
        return None


def fit(p, cfg: dict, task: dict, provider: str, gate=None, now: float | None = None) -> dict:
    """The task, its budget raised so a forked run keeps floor() past its first call (see above)."""
    from . import topup, turnsize
    db, now = p.db, time.time() if now is None else now
    tier = ((((cfg.get("providers") or {}).get(provider) or {}).get("tiers") or {}).get(task.get("tier")) or {})
    model = task.get("model") or tier.get("model") or None   # a pin, else its tier's
    if not task.get("budget_usd") or topup.spent(task) \
            or db.one("SELECT id FROM events WHERE kind=? AND task=?", (KIND, task["id"])) \
            or (tail := _copies(p, cfg, task, provider, model, now)) is None:
        return task
    left = float(task["budget_usd"]) - float(task.get("spent_usd") or 0)
    est, basis = estimate(db, cfg, provider, model, tail)
    room = turnsize.dollar_room(dict(gate.numbers)) if gate is not None else None
    add = floor(float(task["budget_usd"]), est) - (left - est)
    if room is not None:   # the start commits what is left, and the raise, under the dollar caps
        add = min(add, room - max(left, 0.5))
    if add < 0.01:
        return task
    after = round(float(task["budget_usd"]) + add, 2)
    text = (f"#{task['id']} {task['title'][:100]}: a forked run's first call is about ${est:.2f} ({basis}) and "
            f"${left:.2f} was left: budget raised to ${after:.2f}; the dollar caps still apply")
    db.update_task(task["id"], budget_usd=after)
    db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", KIND, "low", text,
          json.dumps({"estimate": est, "basis": basis, "before": task["budget_usd"], "after": after}), "handled",
          task["id"]))
    return {**task, "budget_usd": after}

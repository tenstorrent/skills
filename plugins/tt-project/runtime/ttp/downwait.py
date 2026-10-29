# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A task waiting on a host that is down: a fast retry and the machine's recovery at once, model-free
(Daemon._host_down).

A waiting task's `retry_when` (or a queued one's `start_when`) that exits 255 while ssh confirms its host
does not answer (Daemon._own_255) is waiting on a down host, not on a timer:

- Fast retry (gap): it is probed again BACKOFF_S[0] (15 s) later, doubling with each such answer up to
  BACKOFF_S[-1] (120 s), instead of every PROBE_EVERY_S; any other answer returns it to that interval. The
  task is due as soon as its probe passes.
- Recovery (plan, act), once per down episode of a machine (a machines.json alias the probe's ssh target
  names; other hosts get only the fast retry and the network outage alert, netwait.py):
  - its `recovery_owner` is another project: nothing is run here; that project gets one high note at
    once (its daemon decides);
  - a pause on the machine or on the fix's `resource` (any `ttp pause`, a user's too, and a fleet hold),
    that resource's lock held, or an open entry in the machine change ledger (`ttp machines change
    add`) on the machine: nothing is run; the daemon log says why. A pause is never lifted;
  - an enabled heal check of this project on the machine (heal.machine_of) with a `fix`: its schedule
    runs at once, its healthy back-off (`healthy_every`) ended. The check then decides as always: its
    grace, `max_fixes` per `window_h`, `suppress`, its lock and pauses, known outages and escalation;
  - none: one `host_down` event names the machine and its recovery owner (or that it has none), so the
    coordinator hears of it at once instead of the wait polling silently.
  The episode ends when the host answers again (any probe naming it exits other than 255, or the
  daemon's ssh check reaches it) or after EPISODE_S, when a still-down host is acted on again (the heal
  check's max_fixes per window still caps its fixes).
"""
from __future__ import annotations

import json
import time
from typing import Any

BACKOFF_S = (15, 30, 60, 120)   # probe gaps after 1, 2, 3, 4+ confirmed-down answers in a row
EPISODE_S = 3600                 # a down episode acted on is acted on again after this
KEY = "host_down"                # kv: alias -> {"since", "action", "why"}
EVENT = "host_down"


def gap(n: int, normal: float) -> float:
    """The probe interval after n confirmed-down answers in a row (0: the normal one)."""
    if n <= 0:
        return normal
    return min(float(BACKOFF_S[min(n, len(BACKOFF_S)) - 1]), normal)


def machine_of(host: str, known: dict) -> str | None:
    """The machines.json alias an ssh target names (`alias` or `user@alias`), else None."""
    h = str(host or "").rsplit("@", 1)[-1]
    return h if h and h in known else None


def _covers(name: str, alias: str) -> bool:
    return name == alias or name.startswith((alias + "-", alias + ":"))


def blocker(db: Any, alias: str, resource: str, lock_busy=None) -> str:
    """Why nothing may be run on `alias` now ("" when nothing stands in the way): a pause on it or on
    `resource`, `resource`'s lock held (lock_busy(resource) -> True), or an open machine change on it."""
    try:
        paused = db.paused_resources()
    except Exception:
        paused = {}
    for name, why in sorted(paused.items()):
        if _covers(name, alias) or (resource and name == resource):
            by = (why or {}).get("by") if isinstance(why, dict) else None
            return f"{name} is paused" + (f" by {by}" if by else "")
    if resource and lock_busy is not None and lock_busy(resource):
        return f"lock {resource} is held"
    from . import machine_ledger
    try:
        open_ = machine_ledger.changes([alias])
    except Exception:
        open_ = []
    if open_:
        c = open_[0]
        return f"machine change #{c.get('id')} is open on it: {str(c.get('what') or '')[:120]}"
    return ""


def plan(db: Any, alias: str, project: str, known: dict, checks: list, lock_busy=None) -> dict:
    """What to do about down machine `alias`: {"action": "owner"|"blocked"|"fix"|"none", ...}.
    `checks`: this project's heal checks, [(schedule row, heal spec)] (heal.checks)."""
    from . import heal
    m = known.get(alias) or {}
    owner = str(m.get("recovery_owner") or "")
    if owner and owner != project:
        return {"action": "owner", "owner": owner}
    mine = [(r, s) for r, s in checks if heal.machine_of(s, known) == alias]
    fixer = next(((r, s) for r, s in mine if s.get("fix")), None)
    if fixer is None:
        return {"action": "none", "owner": owner, "checks": [r["name"] for r, _ in mine]}
    r, s = fixer
    why = blocker(db, alias, str(s.get("resource") or ""), lock_busy)
    if why:
        return {"action": "blocked", "schedule": r["name"], "why": why}
    return {"action": "fix", "schedule": r["name"]}


def episodes(db: Any) -> dict:
    v = db.kv(KEY, {}) or {}
    return v if isinstance(v, dict) else {}


def clear(db: Any, alias: str) -> bool:
    """The host answers again: its episode ends. True when one was open."""
    eps = episodes(db)
    if alias not in eps:
        return False
    eps.pop(alias)
    db.set_kv(KEY, eps)
    return True


def act(db: Any, alias: str, tid: int, project: str, known: dict, checks: list, now: float | None = None,
        lock_busy=None, send=None) -> dict | None:
    """Once per episode of down machine `alias` (task `tid` found it): run plan()'s action. The heal
    schedule is made due now (and its healthy back-off ended); `send(owner, text)` notes another
    owner; an event is raised for no recovery. Returns the plan acted on, None within an episode."""
    from . import heal
    now = time.time() if now is None else now
    eps = episodes(db)
    ep = eps.get(alias)
    if ep and now - float(ep.get("since") or 0) < EPISODE_S and ep.get("action") != "blocked":
        return None
    got = plan(db, alias, project, known, checks, lock_busy)
    if ep and ep.get("action") == "blocked" and got["action"] == "blocked" \
            and now - float(ep.get("since") or 0) < EPISODE_S:
        return None   # still blocked: said once
    eps[alias] = {"since": now, "action": got["action"], "task": tid,
                  **({"why": got["why"]} if got.get("why") else {})}
    db.set_kv(KEY, eps)
    if got["action"] == "fix":
        heal.end_streak(db, got["schedule"])
        db.x("UPDATE schedules SET next_run=? WHERE name=? AND (next_run IS NULL OR next_run>?)",
             (now, got["schedule"], now))
    elif got["action"] == "owner" and send is not None:
        send(got["owner"], f"Machine {alias} is down: ssh does not reach it while task #{tid} of {project} "
                           f"waits on it. You are its recovery owner; {project} runs nothing on it.")
    elif got["action"] == "none":
        who = f"its recovery owner {got['owner']}" if got["owner"] else "it has no recovery owner (ttp machines set " \
                                                                        f"{alias} --owner <project>)"
        checks_ = ", ".join(got["checks"]) or "none"
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
             "VALUES(?,?,?,?,?,?,?,?,?)",
             (now, "daemon", EVENT, f"{EVENT}:{alias}:{now:.0f}", "normal",
              f"Machine {alias} is down: ssh does not reach it while #{tid} waits on it, and no recovery is "
              f"declared here (heal checks on it with a fix: none; checks: {checks_}); {who}. Decide: add a heal "
              f"check with a fix for it, page its owner, or re-point the wait.",
              json.dumps({"alias": alias, "owner": got["owner"]}), "queued", tid))
    return got

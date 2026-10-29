# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Severity of daemon events that need no user decision, so they wake the coordinator only when it matters.

A high event wakes the coordinator at once as a tricky turn. Three daemon events used to be high every
time though most carry no decision:

- `continue_unqueued` (a done task's `continue:` follow-up no task took up): normal, and it rides
  along (RIDE_ALONG) with the next coordinator turn that is due anyway, at the latest RIDE_ALONG_MAX_S
  after it was raised. It is a queued event, so it is never dropped and survives restarts. High when
  the done task is urgent (`urgent`).
- `task_blocked` raised by the daemon or the push queue (`raise_blocked`): high when the task is
  urgent or its hold waits on an ask (the user decides), else normal. The same task blocked again for
  the same reason within REPEAT_WINDOW_S raises nothing new: it is counted on the queued event, or,
  once a turn read that, on one queued ride-along event.
- `machine_change_overdue` (machine_ledger.route): normal, high only for a change on a live serving
  machine (`serving_machine`) that is past its end with no until-probe or one that has not passed
  (`change_severity`). Each project hears of one change once per episode: its own event and pages
  relayed from other projects' daemons share one fingerprint (`change_fingerprint`).

Urgent (`urgent`): priority 1, an `urgent:` line in the spec, or a required owner of an open obligation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import anchors, obligations

RIDE_ALONG = "ride_along"        # event data flag: starts no turn by itself (rides_along)
RIDE_ALONG_MAX_S = 3600          # a ride-along event waits at most this long for a turn
REPEAT_WINDOW_S = 6 * 3600       # task_blocked: the same (task, reason) again within this is a repeat
URGENT_LINE = re.compile(r"^\s*urgent:", re.I | re.M)
SERVING_TAG = re.compile(r"^(serving|live|server)$", re.I)   # machines.json tags of a live serving machine


def urgent(db, task: dict | None) -> bool:
    """Whether `task` is priority 1, has an `urgent:` spec line or owns an open obligation."""
    if not task:
        return False
    if int(task.get("priority") or 3) == 1 or URGENT_LINE.search(str(task.get("spec") or "")):
        return True
    try:
        return bool(obligations.owned_open(db, int(task["id"])))
    except Exception:   # an unreadable obligation store must not lose the event: keep it high
        return True


def rides_along(e: dict) -> bool:
    """Whether queued event `e` (with its `data`) waits for a turn something else starts."""
    if e.get("severity") not in ("low", "normal") or not e.get("data"):
        return False
    try:
        data = json.loads(e["data"]) if isinstance(e["data"], str) else e["data"]
    except ValueError:
        return False
    return isinstance(data, dict) and data.get(RIDE_ALONG) is True


def continue_severity(db, task: dict) -> str:
    return "high" if urgent(db, task) else "normal"


def _reason_key(reason: str) -> str:
    norm = re.sub(r"\d+", "#", " ".join(str(reason).lower().split()))   # counts and times do not make it new
    return hashlib.sha256(norm.encode()).hexdigest()[:12]


def blocked_fingerprint(tid: int, reason: str) -> str:
    return f"task_blocked:{tid}:{_reason_key(reason)}"


def raise_blocked(db, task: dict, reason: str, text: str, now: float | None = None) -> str:
    """Record task `task` blocked for `reason` as a queued task_blocked event (call inside the caller's
    transaction). Returns "high", "normal", "counted" (a repeat added to the queued event) or "ride"
    (a repeat queued as one ride-along event)."""
    now = time.time() if now is None else now
    tid = int(task["id"])
    fp = blocked_fingerprint(tid, reason)
    anchor = anchors.anchor(task)
    high = urgent(db, task) or bool(anchor and anchor[0] == "ask")
    prev = None if high else db.one(
        "SELECT id, status, data, text FROM events WHERE kind='task_blocked' AND task=? AND fingerprint=? AND ts>=? "
        "ORDER BY id DESC LIMIT 1", (tid, fp, now - REPEAT_WINDOW_S))
    if prev:
        try:
            data = json.loads(prev["data"] or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        n = int(data.get("repeats") or 0) + 1
        if prev["status"] == "queued":
            base = str(data.get("text") or prev["text"])
            db.x("UPDATE events SET data=?, text=? WHERE id=?",
                 (json.dumps({**data, "repeats": n, "last": now, "text": base}),
                  f"{base} (blocked again {n}x for the same reason within {REPEAT_WINDOW_S // 3600} h)", prev["id"]))
            return "counted"
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
             (now, f"task:{tid}", "task_blocked", fp, "normal",
              f"{text} (blocked again for the same reason within {REPEAT_WINDOW_S // 3600} h)",
              json.dumps({RIDE_ALONG: True, "repeats": 1, "repeat_of": prev["id"], "last": now, "text": text}),
              "queued", tid))
        return "ride"
    sev = "high" if high else "normal"
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, f"task:{tid}", "task_blocked", fp, sev, text, "queued", tid))
    return sev


def serving_machine(entry: dict | None) -> bool:
    """Whether a machines.json entry is tagged as a live serving machine (`serving`, `live` or `server`)."""
    tags = (entry or {}).get("tags") or []
    return any(SERVING_TAG.match(str(t).strip()) for t in (tags if isinstance(tags, list)
                                                          else str(tags).replace(",", " ").split()))


def change_severity(c: dict, now: float, serving: bool) -> str:
    """High only for change `c` on a live serving machine, past its end (its time, or its until-task
    ended), whose until-probe has not passed or that has none; else normal."""
    past = float(c.get("expires") or now + 1) <= now or bool(c.get("task_ended"))
    probe_ok = bool(c.get("probe")) and bool(c.get("probe_passed")) and not c.get("probe_job_failed")
    return "high" if serving and past and not probe_ok else "normal"


def change_fingerprint(key: str, episode: float) -> str:
    """One fingerprint per change and episode, whatever the page step or who relayed it."""
    return f"{key}@{int(episode)}"

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The daily self-review never stops silently. A model-free check on the daemon tick (Daemon.check_self_review)
looks at the `daily-review` schedule and its last review receipt: its last run that ended done with a summary.

- Missing schedule (never created by new, connect or upgrade, or removed with no record of the user
  doing it): restored from the template.
- Turned off with no record of the user doing it (the coordinator outside a user turn, or no record):
  turned back on. Off or removed by the user (web app, a user turn, a harness/schedules.json edit that changes it)
  is left alone: no event, no wake (record_off).
- Enabled, but no receipt for over OVERDUE_S while work happened since: a run is queued at once at
  priority 2, past the debounce, idle guard and precheck, and one 'daily review overdue: <cause>'
  event wakes the coordinator with the cause found (skips, a held or failed run, one with no receipt).
  The budget gate is not overridden: it is named as the cause instead.

A healthy project costs a few queries and no model call. `line` is the one status line for
`ttp status` and the web header.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from .db import DB
from . import schedule as sched
from . import timefmt

NAME = "daily-review"
OVERDUE_S = 26 * 3600      # no receipt for this long, while work happened, is overdue
GRACE_S = 2 * 3600         # a run queued this recently is on its way, not held
FORCE_GAP_S = 12 * 3600    # at most one forced run per this long
SOURCE = "daemon:self-review"
OFF_KEY = "self_review_off"        # kv: {"how": "off"|"removed", "by", "user", "at"}: the last switch-off
STATE_KEY = "self_review_state"    # kv: {"since", "cause", "event_at", "forced": {"task", "at"}}: an open episode
TEMPLATE = Path(__file__).resolve().parents[2] / "template" / "recurring.json"
# Used only when the template cannot be read; test_the_fallback_matches_the_template keeps them equal.
FALLBACK = {"name": NAME, "kind": "llm", "every": "1d", "at": "04:30", "enabled": True, "budget_usd_day": 4.0,
            "description": "Assess progress, spend, quality and harness friction; propose at most 5 improvements.",
            "payload": {"prompt": "daily-review.md", "tier": "standard", "kind": "work", "priority": 4,
                        "skip_if_idle": True}}


def template_entry() -> dict:
    try:
        for s in json.loads(TEMPLATE.read_text()):
            if s.get("name") == NAME:
                return s
    except (OSError, ValueError):
        pass
    return FALLBACK


def record_off(db: DB, name: str, enabled: bool | None, by: str, user: bool, now: float | None = None) -> None:
    """Record a switch of schedule `name` (daily-review only): `enabled` True clears the record, False
    is a switch-off, None a removal. `user`: the user did it (their web app, their turn, a harness commit)."""
    if name != NAME:
        return
    if enabled:
        db.x("DELETE FROM kv WHERE key=?", (OFF_KEY,))
        return
    db.set_kv(OFF_KEY, {"how": "off" if enabled is False else "removed", "by": by, "user": bool(user),
                        "at": time.time() if now is None else now})


def _task(db: DB, done: bool) -> dict | None:
    """The latest daily-review run task: the latest done one with a summary (its receipt), or the latest at all."""
    rows = db.q("SELECT id, status, result, blocked_reason, created, updated FROM tasks WHERE origin='schedule' "
                "AND labels=?" + (" AND status='done'" if done else "") + " ORDER BY id DESC LIMIT 20",
                (json.dumps([NAME]),))
    if not done:
        return rows[0] if rows else None
    return next((r for r in rows if _summary(r)), None)


def _summary(t: dict) -> str:
    try:
        res = json.loads(t.get("result") or "{}")
    except (TypeError, ValueError):
        return ""
    return str(res.get("summary") or "").strip() if isinstance(res, dict) else ""


def _ended(db: DB, t: dict) -> float:
    r = db.one("SELECT MAX(ended) e FROM runs WHERE task=? AND ended IS NOT NULL", (t["id"],))
    return float(r["e"]) if r and r["e"] else float(t["updated"] or t["created"] or 0)


def active_since(db: DB, since: float) -> bool:
    """Whether the user wrote or a worker ran or ended, other than a daily review's own, since `since`
    (the harness's own posts, such as the project-created brief, are no activity)."""
    return bool(db.one("SELECT id FROM messages WHERE direction='in' AND ts>? AND COALESCE(provenance,'')!='system' "
                       "LIMIT 1", (since,))
                or db.one("SELECT runs.id FROM runs LEFT JOIN tasks ON tasks.id=runs.task WHERE runs.role!='coordinator' "
                          "AND (runs.started>? OR runs.ended>?) AND NOT (COALESCE(tasks.origin,'')='schedule' AND "
                          "COALESCE(tasks.labels,'')=?) LIMIT 1", (since, since, json.dumps([NAME]))))


def skip_cause(status: str) -> str:
    s = str(status or "")
    if s.startswith("skipped: budget "):
        return f"the budget gate held it ({s[len('skipped: budget '):]})"
    if s.endswith(": daily budget used"):
        return "its own daily budget (budget_usd_day) was used up"
    if s.endswith(": previous run still open"):
        return "its previous run was still open"
    if s.startswith("skipped: precheck: "):
        return f"its precheck skipped it ({s[len('skipped: precheck: '):]})"
    if "on unchanged evidence" in s:
        return "the debounce skipped it on unchanged evidence"
    if s == "skipped: nothing happened since the last run":
        return "the idle guard skipped it, though work happened since the last review"
    return s


def assess(db: DB, now: float, created: float | None, held: Callable[[dict], str] | None = None) -> dict:
    """{"state", "last", "cause", "open", "row"}. state: ok (a receipt within OVERDUE_S), idle (nothing
    happened since the last receipt), young (none yet in a project under OVERDUE_S old), due (a run
    queued under GRACE_S ago), user-off, missing, off, overdue. Read-only; `held(task)` names what holds a
    queued run (the daemon's gates), else its status says."""
    row = db.one("SELECT * FROM schedules WHERE name=?", (NAME,))
    off = db.kv(OFF_KEY) or {}
    done = _task(db, done=True)
    last = _ended(db, done) if done else None
    out = {"state": "ok", "last": last, "cause": "", "open": None, "row": row, "off": off}
    if (row is None or not row["enabled"]) and off.get("user") and off.get("how") == ("removed" if row is None else "off"):
        return {**out, "state": "user-off"}
    if row is None:
        return {**out, "state": "missing", "cause": "its schedule was missing (never created, or removed with no "
                                                     "record of the user doing it)"}
    if not row["enabled"]:
        return {**out, "state": "off", "cause": "its schedule was off with no record of the user turning it off"
                + (f" (turned off by {off['by']})" if off.get("by") else "")}
    base = last if last is not None else float(created or now)
    if now - base < OVERDUE_S:
        return {**out, "state": "ok" if last is not None else "young"}
    if not active_since(db, base):
        return {**out, "state": "idle"}
    latest = _task(db, done=False)
    if latest and latest["status"] not in ("done", "failed", "cancelled"):
        out["open"] = latest
        if now - float(latest["created"] or 0) < GRACE_S:
            return {**out, "state": "due"}
        why = (held(latest) if held else "") or (f"{latest['status']}: {latest['blocked_reason']}"
                                                  if latest["blocked_reason"] else latest["status"])
        return {**out, "state": "overdue", "cause": f"its run #{latest['id']} is held ({why})"}
    if latest and (done is None or latest["id"] > done["id"]):
        if latest["status"] == "done":
            return {**out, "state": "overdue",
                    "cause": f"its last run #{latest['id']} ended done without a review receipt (no summary)"}
        return {**out, "state": "overdue", "cause": f"its last run #{latest['id']} ended {latest['status']}"
                + (f" ({latest['blocked_reason'][:160]})" if latest["blocked_reason"] else "")}
    skipped = sched.skips(db, NAME, limit=50, since=base)
    if skipped:
        return {**out, "state": "overdue", "cause": f"{len(skipped)} trigger{'s' if len(skipped) != 1 else ''} "
                f"skipped since; the latest: {skip_cause(skipped[0]['reason'])}"}
    if int(row["every_s"] or 0) > OVERDUE_S:
        return {**out, "state": "overdue", "cause": f"its period is {int(row['every_s']) / 3600:g} h, over a day"}
    return {**out, "state": "overdue", "cause": "it has not triggered since" if row["last_run"] is None
            or float(row["last_run"]) <= base else f"its last trigger said: {row['last_status'] or 'nothing'}"}


def restore(p: Any, row: dict | None, off: dict) -> str:
    """Put back a missing schedule from the template, or turn an off one back on, and write the
    schedules file when the project keeps one. Returns what it did, or why it could not."""
    db, home = p.db, timefmt.home(p)
    try:
        sched.before_change(p)
    except ValueError as e:
        return f"not restored: {e}"
    if row is None:
        s = template_entry()
        sched.upsert(db, NAME, s["kind"], s["every"], s.get("at"), True, s.get("budget_usd_day"),
                     s.get("description", ""), s.get("payload", {}), home)
        did = "restored from the template"
    else:
        sched.set_enabled(db, NAME, True, home=home)
        did = "turned back on"
    record_off(db, NAME, True, "daemon", False)
    try:
        sched.write_file(p, f"schedule {NAME}: {did} by the self-review check")
    except Exception as e:   # the database holds it; the file catches up on the next change
        return f"{did} (harness/{sched.FILE} not written: {type(e).__name__}: {e})"
    return did


def tick(p: Any, now: float, created: float | None, held: Callable[[dict], str],
         force: Callable[[dict], str]) -> dict:
    """One check: repair, assess, queue a forced run and raise or clear the episode's event.
    `force(row)` queues a run and returns the schedule status ("queued" or why not)."""
    db = p.db
    a = assess(db, now, created, held)
    repaired = ""
    if a["state"] in ("missing", "off"):
        repaired = f"{a['cause']}; {restore(p, a['row'], a['off'])}"
        a = assess(db, now, created, held)
        if a["state"] in ("missing", "off"):   # not restored (a broken schedules file): report that
            a = {**a, "state": "overdue"}
    st = db.kv(STATE_KEY) or {}
    if a["state"] in ("ok", "idle", "young", "user-off"):
        if st:
            db.x("DELETE FROM kv WHERE key=?", (STATE_KEY,))
            db.x("UPDATE events SET status='done' WHERE source=? AND status IN ('new','queued')", (SOURCE,))
        return {**a, "repaired": repaired}
    if a["state"] == "due":
        return {**a, "repaired": repaired}
    cause = "; ".join(x for x in (repaired, a["cause"] if a["cause"] not in repaired else "") if x)
    forced = st.get("forced") or {}
    if a["open"] is None and a["row"] is not None and now - float(forced.get("at") or 0) >= FORCE_GAP_S:
        status = force(a["row"])
        latest = _task(db, done=False)
        if status == "queued" and latest:
            forced = {"task": latest["id"], "at": now}
            cause += f"; queued run #{latest['id']} now (priority 2)"
        else:
            forced = {"at": now}
            cause += f"; could not queue a run ({status})"
    if not st or now - float(st.get("event_at") or 0) >= OVERDUE_S:
        last = f"last receipt {(now - a['last']) / 3600:.0f} h ago" if a["last"] else "no receipt yet"
        text = f"daily review overdue: {cause} ({last})"
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
             (now, SOURCE, "observation", f"self-review-overdue:{int(st.get('since') or now)}", "normal",
              text[:4000], "queued"))
        st, raised = {"since": st.get("since") or now, "event_at": now}, True
    else:
        raised = False
    db.set_kv(STATE_KEY, {**st, "cause": cause, "forced": forced})
    return {**a, "state": "overdue", "cause": cause, "repaired": repaired, "raised": raised}


def line(db: DB, now: float, created: float | None) -> str:
    """'self-review: last ran N h ago', or overdue with the cause the daemon found."""
    a = assess(db, now, created)
    ago = (lambda: f"{(now - a['last']) / 3600:.0f} h ago") if a["last"] else (lambda: "")
    if a["state"] == "user-off":
        off = a["off"]
        return f"self-review: {'removed' if off.get('how') == 'removed' else 'off'} by the user ({off.get('by')})"
    if a["state"] in ("ok", "idle"):
        return f"self-review: last ran {ago()}" + (" (nothing happened since)" if a["state"] == "idle" else "")
    if a["state"] == "young":
        return "self-review: not run yet"
    if a["state"] == "due":
        return f"self-review: run #{a['open']['id']} {a['open']['status']}" + (f"; last ran {ago()}" if a["last"] else "")
    cause = (db.kv(STATE_KEY) or {}).get("cause") or a["cause"]
    return f"self-review: overdue ({'last ran ' + ago() if a['last'] else 'never ran'}): {cause}"

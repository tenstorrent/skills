"""Hold escalation: ordered work held by a rule that is no safety gate proceeds on its own.

A task the user ordered (blockledger._ordered) that is blocked, waiting or deferred longer than
`coordinator.hold_escalate_min` (default 5; 0 off) is classified like the blocked-time ledger does
(blockledger.safety_of). A safety hold is never cleared: it raises one event per hold episode, none for a
held resource or a pause the user set. Any other hold raises one `hold_escalate` event naming the task,
the rule and the minutes, and, `coordinator.hold_proceed_min` (default 5; 0 escalate only) later, is
cleared unless a safety cite was added meanwhile. A deferral is an outside wait when it starts
itself: a `start_when` probe, or a start_after whose why or probe names a safety class by the same
classifier the deferral check uses (convgate.stored_class). It is shown, never raised. A hold escalates once, though it is recorded again. A hold on an open task, an ask or a resource is left
alone: the root blocker is the thing to name. selfblock.raise_due runs this once a minute."""
from __future__ import annotations

import json
import re
import time

from . import blockledger, convgate, unblock
from .db import DB, deferral, without_deferral
from .project import DEFAULT_CONFIG

KIND = "hold_escalate"
KEY = "holdescalate:episodes"   # kv: {"<task>:<hold start>": {"at": escalated, "rule", "safe"}}
KEEP_S = 7 * 86400
DEFAULTS = {k: DEFAULT_CONFIG["coordinator"][k] for k in ("hold_escalate_min", "hold_proceed_min")}
OUTSIDE = "an outside wait"   # a deferral on a start_when probe: it starts itself once the outside condition holds
QUIET = ("held resource", "a pause or hold the user set", OUTSIDE)   # shown in the ledger, no wake
OPEN = re.compile(r"\bask:\d+|\blanded\b", re.I)   # waits on a user answer or another task's landing


def _cfg(db: DB) -> dict:
    from . import selfblock
    coord = (selfblock._raw_config(db) or {}).get("coordinator")
    return {**DEFAULTS, **{k: v for k, v in (coord if isinstance(coord, dict) else {}).items() if k in DEFAULTS}}


def _hold(db: DB, t: dict, now: float) -> dict | None:
    """{kind, start, reason} of the task's hold, or None when it is not one this module handles."""
    if t["status"] in ("blocked", "waiting"):
        eps = unblock._task_episodes(db, t, now)
        seg = eps[-1]["segments"][-1] if eps and eps[-1]["open"] else None
        ev = db.one("SELECT text FROM events WHERE task=? AND kind=? ORDER BY ts DESC, id DESC LIMIT 1",
                    (t["id"], f"task_{t['status']}"))
        reason = t["blocked_reason"] or blockledger._why((ev and ev["text"]) or "", t["id"], t["title"])
        return {"kind": t["status"], "start": seg["start"] if seg else float(t["updated"] or now), "reason": reason}
    if t["status"] == "queued" and (d := deferral(t)):
        reason = " ".join(str(d.get(k) or "") for k in ("why", "when")).strip() or "start_after"
        return {"kind": "deferred", "start": d.get("since") or float(t["created"] or now), "reason": reason,
                "probe": bool(d.get("when")), "cls": convgate.stored_class(t)}
    return None


def _on_open_task(db: DB, t: dict) -> bool:
    try:
        deps = json.loads(t["depends_on"] or "[]")
    except (TypeError, ValueError):
        return False
    return any((d := db.task(int(i))) and d["status"] not in unblock.ENDED for i in deps if str(i).isdigit())


def _event(db: DB, now: float, task: dict, text: str, data: dict) -> None:
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
         (now, "daemon", KIND, f"{KIND}:{task['id']}:{data['start']:.0f}:{data['stage']}", "normal", text,
          json.dumps(data), "queued", task["id"]))


def _proceed(db: DB, t: dict, h: dict, rule: str, mins: int, now: float) -> bool:
    """Requeue the task (its attempt cap and a running run stand) and say so in its spec; False when refused."""
    if db.requeue(t["id"], by="holdescalate"):
        return False
    labels = without_deferral(json.loads(t["labels"] or "[]"))
    line = f"auto-proceeded past {rule} after {mins} min (holdescalate)"
    db.update_task(t["id"], labels=labels, not_before=None, spec=f"{t['spec'] or ''}\n{line}".strip())
    db.set_kv(f"holdescalate:proceeded:{t['id']}", {"rule": rule, "minutes": mins, "kind": h["kind"], "ts": now})
    blockledger.append(db, "auto-proceed", t["id"], 0, f"{rule} ({h['kind']}, {mins} min)", source="holdescalate")
    return True


def _again(db: DB, t: dict, rule: str) -> bool:
    """True when this rule already let the task proceed and it has not run since: the rule put it straight back
    (a spent task budget), so another proceed would only loop."""
    prev = db.kv(f"holdescalate:proceeded:{t['id']}")
    return bool(prev and prev.get("rule") == rule and not db.one(
        "SELECT 1 FROM runs WHERE task=? AND started>?", (t["id"], float(prev.get("ts") or 0))))


def _prior(db: DB, state: dict, t: dict, rule: str) -> str | None:
    """The key of an earlier episode of this task under the same rule that no run has followed: a hold that
    is re-recorded (its start moves) is still the one hold and escalates once."""
    for key, v in state.items():
        if key.startswith(f"{t['id']}:") and v.get("rule") == rule and not db.one(
                "SELECT 1 FROM runs WHERE task=? AND started>?", (t["id"], float(v.get("at") or 0))):
            return key
    return None


def run(db: DB, cfg: dict | None, now: float) -> list[dict]:
    """Escalate and clear due holds; returns what it did: {task, rule, minutes, safety, did}."""
    cfg = {**DEFAULTS, **(cfg if cfg is not None else _cfg(db))}
    esc_s, proceed_s = float(cfg["hold_escalate_min"]) * 60, float(cfg["hold_proceed_min"]) * 60
    if esc_s <= 0:
        return []
    ordered = blockledger._ordered(db)
    state = {k: v for k, v in (db.kv(KEY, {}) or {}).items()
             if isinstance(v, dict) and now - float(v.get("at") or 0) < KEEP_S}
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status IN ('blocked','waiting','queued') ORDER BY id"):
        if t["id"] not in ordered or not (h := _hold(db, t, now)) or now - h["start"] < esc_s:
            continue
        if OPEN.search(h["reason"]) or _on_open_task(db, t):
            continue
        rule = blockledger.rule_of(h["reason"])
        safety = OUTSIDE if h.get("probe") or h.get("cls") else blockledger.safety_of(f"{rule} {h['reason']}", "")
        mins = int((now - h["start"]) / 60)
        ep = f"{t['id']}:{h['start']:.0f}"
        if ep not in state and (prior := _prior(db, state, t, rule)):
            ep = prior
        seen = state.get(ep)
        if safety == "convenience" and seen is None and _again(db, t, rule):
            continue   # it proceeded past this rule and the rule put it straight back: one try per run, no loop
        if safety != "convenience":
            if seen is None and safety not in QUIET:
                _event(db, now, t, f"#{t['id']} {t['title'][:80]} held {mins} min by {rule} (safety: {safety})",
                       {"task": t["id"], "rule": rule, "safety": safety, "start": h["start"], "stage": "safety"})
            if seen is None:
                state[ep] = {"at": now, "rule": rule, "safe": True}
                out.append({"task": t["id"], "rule": rule, "minutes": mins, "safety": safety, "did": "shown"})
            continue
        if seen is None or seen.get("safe"):   # the hold was safety until a cite changed; its proceed clock starts now
            at = now
            state[ep] = {"at": at, "rule": rule, "safe": False}
            at_hhmm = time.strftime("%H:%M", time.localtime(at + proceed_s))
            tail = (f"; proceeds at {at_hhmm} unless you cite a safety gate" if proceed_s > 0
                    else "; it needs a safety cite or a fix")
            _event(db, now, t, f"#{t['id']} {t['title'][:80]} held {mins} min by {rule} (no safety gate named){tail}",
                   {"task": t["id"], "rule": rule, "safety": safety, "start": h["start"], "stage": "escalate",
                    "proceeds_at": at + proceed_s if proceed_s > 0 else None})
            out.append({"task": t["id"], "rule": rule, "minutes": mins, "safety": safety, "did": "escalated"})
        elif proceed_s > 0 and now - float(seen["at"]) >= proceed_s and not seen.get("done"):
            seen["done"] = True
            if _proceed(db, t, h, rule, mins, now):
                out.append({"task": t["id"], "rule": rule, "minutes": mins, "safety": safety, "did": "proceeded"})
    if state != (db.kv(KEY, {}) or {}):
        db.set_kv(KEY, state)
    return out

"""A worker's normal `ttp notify` from a run that ended blocked, waiting, failed or needs_review goes
to the coordinator first, so it cannot duplicate or contradict what the coordinator then tells the user.
It is queued as a ride-along `worker_notify` event (pagesev.RIDE_ALONG: it joins the turn the run's
hand-off starts, never one of its own) and kept in a kv list. The coordinator takes it up by an ask,
reply or notify naming the task (#<id>), or drops it with a task_update whose text starts DROPPED
(handled). Otherwise, once notify.hold_s has passed, post_due posts it unchanged and model-free. The
list lives in kv and each post removes its entry in the same transaction, so a daemon restart during
the hold neither loses nor doubles it."""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import pagesev

KIND = "worker_notify"
KV = "worker_notify_hold"   # kv: [{event, task, text, severity, due}] notifies held for the coordinator
HELD_ENDS = ("blocked", "waiting", "failed", "needs_review")
DROPPED = "notify dropped:"
HOLD_S = 900
_TASK_REF = re.compile(r"#(\d+)\b")


def hold_s(cfg: dict) -> float:
    try:
        return max(0.0, float((cfg.get("notify") or {}).get("hold_s", HOLD_S)))
    except (TypeError, ValueError):
        return float(HOLD_S)


def _entries(db) -> list[dict]:
    v = db.kv(KV, [])
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def _due(e: dict) -> float:
    try:
        return float(e.get("due") or 0)
    except (TypeError, ValueError):
        return 0.0   # a damaged entry is posted now, never left to stop every tick


def hold(db, task: dict, text: str, run: str, end: str, hold_for: float, now: float | None = None) -> bool:
    """Hold alert `text` of task `task`'s run `run` (ended `end`) for the coordinator. Call inside the
    run end's transaction: a rollback drops both the event and the entry. False if already held."""
    now = time.time() if now is None else now
    fp = f"{KIND}:{run}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    with db.tx():
        if db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return False
        _hold(db, task, text, end, hold_for, now, fp)
    return True


def _hold(db, task: dict, text: str, end: str, hold_for: float, now: float, fp: str) -> None:
    mins = max(1, round(hold_for / 60))
    body = (f"Worker notify of #{task['id']} (its run ended {end}), held for you, not yet sent: {text}\n"
            f"Posted to the user unchanged in {mins} min unless you ask, reply or notify naming #{task['id']} "
            f"first, or drop it with task_update #{task['id']} text '{DROPPED} <why>'.")
    eid = db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
               (now, f"worker:{task['id']}", KIND, fp, "normal", body[:4000],
                json.dumps({pagesev.RIDE_ALONG: True, "task": task["id"], "text": text}), "queued", task["id"]))
    db.set_kv(KV, _entries(db) + [{"event": eid, "task": task["id"], "text": text, "severity": "normal",
                                    "due": now + hold_for}])


def _close(db, keep: list[dict], gone: list[dict]) -> None:
    db.set_kv(KV, keep)
    for e in gone:
        db.x("UPDATE events SET status='handled' WHERE id=? AND status='queued'", (e.get("event"),))


def handled(db, tasks) -> int:
    """The coordinator took up (or dropped) the held notifies of `tasks`: none of them is posted."""
    tasks = {int(t) for t in tasks}
    with db.tx():
        entries = _entries(db)
        gone = [e for e in entries if e.get("task") in tasks]
        if gone:
            _close(db, [e for e in entries if e not in gone], gone)
    return len(gone)


def named(text: str) -> set[int]:
    return {int(m) for m in _TASK_REF.findall(str(text or ""))}


def post_due(db, now: float | None = None) -> int:
    """Post each held notify whose hold ended, unchanged, and close its coordinator item."""
    now = time.time() if now is None else now
    with db.tx():
        entries = _entries(db)
        due = [e for e in entries if _due(e) <= now]
        if not due:
            return 0
        for e in due:
            text = str(e.get("text") or "")
            if text and not db.one("SELECT id FROM messages WHERE direction='out' AND kind='alert' AND text=? "
                                   "AND ts>?", (text, now - 86400)):
                db.post("out", text, chat=None, kind="alert", severity=e.get("severity") or "normal")
        _close(db, [e for e in entries if e not in due], due)
    return len(due)

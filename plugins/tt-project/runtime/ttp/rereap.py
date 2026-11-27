"""Re-reading a run the reap quarantined (Daemon._quarantine_run) whose hand-off is on disk.

A reap cut off twice quarantines its run: the run and its task fail, though the run dir may hold a
finished exit.json and a complete result.json. Once, model-free, such a run is put back as running
with its reap marked lean, so the normal reap tick finishes it from those files. The worker is never
run again, and a run whose result.json is missing or not a hand-off stays quarantined."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import batchrun

KEY = "rereaped"   # kv: run id -> when its quarantined end was re-read; each run once
WINDOW_S = 7 * 86400   # sweep re-reads runs quarantined this recently
PER_TICK = 2
REF = "reap-quarantine:"


def check(p, run_id: int) -> tuple[dict | None, str]:
    """(the run's row, "") when its quarantined end can be re-read, else (None, why not)."""
    from .daemon import HANDOFF_STATES, _read_result
    db = p.db
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if not r:
        return None, f"no run {run_id}"
    if not db.one("SELECT 1 FROM messages WHERE direction='out' AND ref=?", (f"{REF}{run_id}",)):
        return None, f"run {run_id} was not quarantined"
    if (db.kv(KEY) or {}).get(str(run_id)):
        return None, f"run {run_id}'s end was already re-read once"
    t = db.task(r["task"]) if r["task"] else None
    if r["status"] != "failed" or r["role"] == "coordinator" or not t or t["status"] != "failed" \
            or db.one("SELECT 1 FROM runs WHERE task=? AND id>?", (r["task"], run_id)):
        return None, f"run {run_id} is not the quarantined last run of a failed task"
    run_dir = Path(r["dir"]) if r["dir"] else p.runs / str(run_id)
    if _read_result(run_dir / "exit.json") is None:
        return None, f"run {run_id} has no exit.json"
    if (_read_result(run_dir / "result.json") or {}).get("status") not in HANDOFF_STATES:
        return None, f"run {run_id}'s result.json is missing or not a complete hand-off"
    return r, ""


def reread(p, run_id: int) -> str:
    """Put quarantined run `run_id` back for the reap tick to finish from its files: "" when done,
    else why not. What _abandon_run booked is taken back (finish_run books the run's cost whole), from
    the ledger source it used, and the task_failed event it queued is closed. The mark stands in for an
    earlier daemon's cut-off reap, which _reap_mark answers with a lean reap."""
    from .daemon import REAP_KEY
    db, now = p.db, time.time()
    with db.tx():   # checked inside, so the CLI and the sweep never both re-read one run
        r, why = check(p, run_id)
        if not r:
            return why
        if cost := r["cost_usd"] or 0:
            booked = db.one("SELECT source FROM ledger WHERE ts=? AND usd=? AND provider=? ORDER BY id DESC",
                            (r["ended"], cost, r["provider"]))
            db.spend(r["provider"], -cost, booked["source"] if booked else f"task:{r['task']}",
                     account=r["account"] or "", estimated=bool(r["cost_estimated"]), ts=now)
            db.x("UPDATE tasks SET spent_usd=COALESCE(spent_usd,0)-? WHERE id=?", (cost, r["task"]))
            batchrun.book(db, r["task"], -cost)
        db.x("UPDATE runs SET status='running', ended=NULL WHERE id=?", (run_id,))
        db.update_task(r["task"], status="running", blocked_reason=None)
        db.x("UPDATE events SET status='handled' WHERE kind='task_failed' AND task=? AND status='queued'", (r["task"],))
        marks = db.kv(REAP_KEY) or {}
        db.set_kv(REAP_KEY, {**marks, str(run_id): {"pid": 0, "started": 0, "lean": False, "at": now}})
        db.set_kv(KEY, {**(db.kv(KEY) or {}), str(run_id): now})
    return ""


def sweep(p, log: Callable[[object, str], None]) -> None:
    """Re-read, at most PER_TICK a tick, the runs quarantined within WINDOW_S that can be."""
    since, done = time.time() - WINDOW_S, 0
    for m in p.db.q("SELECT DISTINCT ref FROM messages WHERE direction='out' AND ts>=? AND ref LIKE ?",
                    (since, f"{REF}%")):
        run_id = m["ref"][len(REF):]
        if done >= PER_TICK:
            break
        try:   # a re-read never holds up reaping
            if not run_id.isdigit() or check(p, int(run_id))[0] is None:
                continue
            done += 1
            why = reread(p, int(run_id))
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
        log(p, f"run {run_id}: " + (f"quarantined end not re-read: {why}" if why else "quarantined end re-read from its files"))

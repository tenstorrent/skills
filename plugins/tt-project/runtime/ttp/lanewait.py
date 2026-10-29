# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Task-to-task waits, model-free (Daemon.probe_deferred runs both each tick).

- Early start: a queued task whose start_when is exactly `landed:#X` starts once X's change (X or a
  task carrying it on, landed.carriers) has an approved or batched push queue row, without waiting
  through the batch and the landing. Its start_when goes; START_FROM names the row's head (the
  approved head), which worktree.ensure branches a new worktree from instead of the base, and
  EARLY names X and the row. A held row (a missing token scope) does not count. Only a task that
  branches from that head takes it (worktree.branches_early: a worktree of its own, no carried
  branch, PR clone or continued head); any other (plan, ops, review, harness, work unless
  worktree.kinds lists it) would run on code without X, so it waits for the real landing.
- Rebase note: once that row is sent back (conflict, failed checks, refused, cancelled) the task
  gets a note in its spec, and in a running run's steer.md, to rebase onto the branch once X's
  change lands; it is never cancelled. EARLY then goes, as it does once the row is pushed or landed.
- `lane_wait`: a queued code or work task that has waited on another task's lane (start_when
  `landed:#`/`ttp landed --task`, depends_on, waits_end:) longer than `waiting.lane_wait_event_s`
  (600; 0: never) raises one event, naming both tasks, the holder's status and the hours so far. It
  wakes the coordinator once per wait and is cleared (handled) once the task starts or no longer
  waits on a task. A start_when with no task in it (a secret, a sign-in, a cadence: the user's say)
  or a start_after still ahead is no lane wait. Known gap, accepted: a probe naming a task and
  something else counts as a lane wait."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import landed
from .db import TERMINAL_TASK_STATES, deferral, without_deferral

START_FROM = "start_from:"   # + sha: a new worktree branches from it (worktree.early_head)
EARLY = "early_start:"       # + "<X>:<push_queue row>": started on X's approved head
SENT_BACK = ("conflict", "check_failed", "refused", "cancelled")
KIND = "lane_wait"
KEY = "lane_waits"           # kv: waiting task id -> {"since", "event"}
WAIT_S = 600                 # default of waiting.lane_wait_event_s
KINDS = ("code", "work")


def batched(db, when: str | None) -> tuple[int, int, str] | None:
    """(X, push queue row id, head) of the newest approved or batched row carrying X's change, for a
    start_when that is exactly `landed:#X`; else None."""
    m = landed.LANDED_PROBE.fullmatch(when or "")
    if not m:
        return None
    x = int(m.group(1))
    ids = sorted(set(landed.carriers(db, x)) | {x})
    row = db.one(f"SELECT id, head FROM push_queue WHERE status IN ('approved','batched') AND task IN "
                 f"({','.join('?' * len(ids))}) ORDER BY id DESC LIMIT 1", tuple(ids))
    return (x, row["id"], row["head"]) if row and row["head"] else None


def early_labels(labels: list, got: tuple[int, int, str]) -> list:
    """`labels` without the deferral and any earlier early start, plus START_FROM and EARLY."""
    x, row, head = got
    keep = [lb for lb in without_deferral(labels)
            if not (isinstance(lb, str) and lb.startswith((START_FROM, EARLY)))]
    return keep + [f"{START_FROM}{head}", f"{EARLY}{x}:{row}"]


def label_value(task: dict, prefix: str) -> str | None:
    try:
        labels = json.loads(task.get("labels") or "[]")
    except (ValueError, TypeError):
        return None
    return next((lb[len(prefix):] for lb in labels if isinstance(lb, str) and lb.startswith(prefix)), None)


def rebase_notes(db, now: float | None = None) -> list[int]:
    """Gives each open task started early (EARLY) whose push queue row was sent back a rebase note,
    once, and drops EARLY from it then or once the row shipped. Returns the tasks noted."""
    noted = []
    for t in db.q(f"SELECT * FROM tasks WHERE status NOT IN (?,?,?) AND labels LIKE '%\"{EARLY}%'",
                  TERMINAL_TASK_STATES):
        x, _, row = (label_value(t, EARLY) or "").partition(":")
        r = db.one("SELECT status, head FROM push_queue WHERE id=?", (int(row),)) if row.isdigit() else None
        if r and r["status"] not in SENT_BACK + ("pushed", "landed"):
            continue   # still approved, batched or held: wait on
        labels = [lb for lb in json.loads(t["labels"] or "[]") if not (isinstance(lb, str) and lb.startswith(EARLY))]
        if r is None or r["status"] in ("pushed", "landed"):
            db.update_task(t["id"], labels=labels)
            continue
        note = (f"Rebase note: #{x}'s change was sent back from the push queue ({r['status']}). This task "
                f"started from its approved head {str(r['head'])[:12]} before it landed. Keep working; once "
                f"#{x}'s change lands, rebase your branch onto the push branch (keep both sides' intents), "
                f"and do not deliver #{x}'s commits as your own.")
        db.update_task(t["id"], labels=labels, spec=(t["spec"] or "").rstrip() + "\n\n" + note + "\n")
        _tell(db, t["id"], note, f"rebase-note-{t['id']}-{row}")
        noted.append(t["id"])
    return noted


def _tell(db, tid: int, note: str, key: str) -> None:
    from .coordinator import _append_update

    def tell():
        for r in db.q("SELECT dir FROM runs WHERE task=? AND status='running' AND dir IS NOT NULL", (tid,)):
            _append_update(Path(r["dir"]) / "steer.md", note, key)
    db.after_commit(tell)


def threshold(cfg: dict) -> float:
    """waiting.lane_wait_event_s; 0 turns the event off."""
    return float((cfg.get("waiting") or {}).get("lane_wait_event_s", WAIT_S) or 0)


def holders(db, t: dict, unmet: list, now: float) -> list[tuple[int, str]]:
    """(holder id, how) of each other task queued task `t` waits on; [] when it waits on something
    else too that is not a task (a start_when naming none, a start_after ahead)."""
    from .worktree import lineage_hold
    when = deferral(t).get("when")
    refs = [int(r) for r in landed.task_refs(when) if r.isdigit()]
    if (when and not refs) or (t["not_before"] or 0) > now:
        return []
    out = [(r, f"start_when landed:#{r}") for r in refs]
    out += [(d, "depends_on") for d in unmet if isinstance(d, int)]
    if (h := lineage_hold(t)) is not None and (row := db.task(h)) and row["status"] in ("queued", "running", "waiting"):
        out.append((h, "waits_end"))
    # A failed, cancelled or missing holder is a dead wait, raised on its own (dead_dependency,
    # missing_task_ref, deferral_probe_broken).
    return [x for x in dict.fromkeys(out) if x[0] != t["id"]
            and (h := db.task(x[0])) and h["status"] not in ("failed", "cancelled")]


def check(db, cfg: dict, now: float) -> None:
    """Raises KIND once per queued code or work task waiting on another task's lane past the
    threshold; clears it once the task no longer waits so."""
    wait_s = threshold(cfg)
    state = db.kv(KEY, {}) or {}
    new: dict = {}
    if wait_s:
        rows = db.q(f"SELECT * FROM tasks WHERE status='queued' AND kind IN ({','.join('?' * len(KINDS))})", KINDS)
        unmet = db.unmet_dependencies(rows) if rows else {}
        for t in rows:
            held = holders(db, t, unmet.get(t["id"]) or [], now)
            if not held:
                continue
            prev = state.get(str(t["id"])) or {}
            since = float(deferral(t).get("since") or t["created"] or now)
            got = {"since": prev.get("since") or since, "event": prev.get("event")}
            if got["event"] is None and now - got["since"] >= wait_s:
                got["event"] = _raise(db, t, held, now - got["since"])
            new[str(t["id"])] = got
    for k, v in state.items():
        if k not in new and v.get("event"):
            db.x("UPDATE events SET status='handled' WHERE id=? AND status='queued'", (v["event"],))
    if new != state:
        db.set_kv(KEY, new)


def _raise(db, t: dict, held: list[tuple[int, str]], waited: float) -> int:
    from .startgate import push_state
    parts = []
    for hid, how in held[:6]:
        h = db.task(hid)
        queued = push_state(db, f"landed:#{hid}") if how.startswith("start_when") else None
        parts.append(f"#{hid} {h['title'][:80] if h else '(missing)'} ({h['status'] if h else 'missing'}"
                     f"{'; ' + queued if queued else ''}; via {how})")
    text = (f"#{t['id']} {t['title'][:120]} has waited {waited / 3600:.1f} h to start on another task's lane: "
            + "; ".join(parts) + ". Decide whether it can start now (re-point or clear its start_when or "
            "depends_on, or start it from the holder's head) or should keep waiting.")
    return db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), "daemon", KIND, f"{KIND}:{t['id']}:{time.time():.0f}", "normal", text,
                 json.dumps({"task": t["id"], "holders": [h for h, _ in held], "waited_s": round(waited)}),
                 "queued", t["id"]))

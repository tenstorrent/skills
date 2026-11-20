# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp task <project> rejudge <id>`: the daemon's hand-off checks run again on a finished task's stored
hand-off at the current harness version, so a record an older or buggy check marked wrongly is put right.

It reruns what Daemon._finish_worker runs on a done hand-off, with the same code: the requested-cells
check (cells.check and cells.prefix on the task's `cells:` line) and the project's own hand-off checks
(handoffcheck.run, project.json `worker.handoff_checks`), on the stored hand-off without the daemon's
marks. A failing project check leaves the record as it is. Else, when the cells verdict changed, the
summary's prefix and the daemon's keys (cells.FIELD's seal `cells_checked`, cells.NO_HOP) are rewritten
to what the daemon stores today; a mark the cells still earn is kept or updated, never dropped, so a
task is never passed as complete on a failing check, done or failed (a failed task gets no new mark).
A cleared mark also drops the daemon's `continue:` follow-up for the missing cells: its hop is cancelled
when it never started, else kept and reported, and a proposal still waiting for the coordinator is
marked handled. The before and after go into a
`task_rejudged` event and the result's REJUDGED key. The task's status never changes. A second run finds
the same verdict and changes nothing; a dry run writes nothing.

A cells mark another rule put on the record (a project's own daemon rule, or an older daemon, with no
`cells_checked` seal) is found against the worker's own hand-off, the result.json in the task's last
run's folder: when that file is the stored hand-off (same status and fields) but its summary lacks the
stored summary's 'partial: N of M cells' or 'cells unreported:' start, the start is a daemon mark. It is
dropped only when the cells check here finds every requested cell of the task's `cells:` line reported:
the worker's summary and follow-ups come back, the `continue:` follow-ups the rule added with a `cells:`
line go (their hops and proposals as above), and so do the keys it added holding a cells finding (a dict
with `missing`).
Without that file, or with a check that still marks or requests no cells, the record stays as it is."""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from . import __version__, cells, handoffcheck
from .db import dump_result, load_result

EVENT = "task_rejudged"
REJUDGED = "rejudged"     # in the task's result: the last rejudge that changed it (ts, version, before)
STATUSES = ("done", "failed")
DAEMON_KEYS = ("cells_checked", cells.NO_HOP)
CONTINUE = re.compile(r"(?i)^\s*continue\s*:")
OTHER_MARK = re.compile(rf"(?:{re.escape(cells.PARTIAL)} \d+ of \d+ cells\b|{re.escape(cells.UNREPORTED)})")


def _keys(result: dict) -> dict:
    return {k: result[k] for k in DAEMON_KEYS if k in result}


def _show(keys: dict) -> str:
    return ", ".join(f"{k}={json.dumps(v)[:200]}" for k, v in keys.items()) or "(none)"


def _other_mark(db, task_id: int, result: dict) -> dict | None:
    """The worker's own hand-off when the stored one carries a cells mark another rule added to it (see the
    module doc): {"own", "fups" (the stored follow-ups without the added ones), "dropped" (their titles),
    "keys" (the added keys holding a cells finding)}; else None."""
    summary = str(result.get("summary") or "")
    if not OTHER_MARK.match(summary):
        return None
    r = db.one("SELECT dir FROM runs WHERE task=? AND role!='coordinator' AND dir IS NOT NULL "
               "ORDER BY id DESC LIMIT 1", (task_id,))
    try:
        own = json.loads((Path(r["dir"]) / "result.json").read_text()) if r else None
    except (OSError, ValueError):
        return None
    if not isinstance(own, dict) or OTHER_MARK.match(str(own.get("summary") or "")) \
            or any(result.get(k) != v for k, v in own.items() if k not in ("summary", "followups")):
        return None
    mine = [f for f in own.get("followups") or [] if isinstance(f, dict)]
    stored = [f for f in result.get("followups") or [] if isinstance(f, dict)]
    added = [f for f in stored if f not in mine and CONTINUE.match(str(f.get("title") or ""))
             and cells.LINE.search(str(f.get("spec") or ""))]
    return {"own": own, "fups": mine + [f for f in stored if f not in mine and f not in added],
            "dropped": [str(f.get("title") or "") for f in added],
            "keys": [k for k, v in result.items() if k not in own and isinstance(v, dict) and "missing" in v]}


def rejudge(p, cfg: dict, task_id: int, dry_run: bool = False, why: str = "") -> tuple[list[str], int]:
    """(lines to print, exit code): 0 when the record matches the checks (changed or already so), 1 when
    a project hand-off check still fails (nothing changed). ValueError for a task it cannot rejudge."""
    db = p.db
    t = db.task(task_id)
    if not t:
        raise ValueError(f"no task #{task_id} in {p.name}")
    if t["status"] not in STATUSES:
        raise ValueError(f"rejudge needs a done or failed task; #{task_id} is {t['status']}")
    result = load_result(t.get("result"))
    if not result:
        raise ValueError(f"task #{task_id} has no stored hand-off to rejudge")
    summary = str(result.get("summary") or "")
    sealed = bool(result.get("cells_checked"))
    old_mark = cells.mark_of(summary) if sealed else ""   # a worker's own 'partial:' text is not the daemon's
    bare = summary[len(old_mark):]
    title = cells.hop_title(t)
    fups = result.get("followups") if isinstance(result.get("followups"), list) else []
    own_fups = [f for f in fups if not (sealed and isinstance(f, dict) and str(f.get("title") or "") == title)]
    other = None if sealed else _other_mark(db, task_id, result)
    if other:
        bare, own_fups = str(other["own"].get("summary") or ""), other["fups"]
        old_mark = summary[:len(summary) - len(bare)] if bare and summary.endswith(bare) else summary
    # The hand-off as the worker wrote it: what the daemon's checks saw when the run ended.
    hand = {k: v for k, v in result.items() if k not in (*DAEMON_KEYS, REJUDGED, *(other or {}).get("keys", ()))}
    hand["summary"] = bare
    if "followups" in result:
        hand["followups"] = own_fups
    lines = [f"#{task_id} {t['title']}: rejudged at harness {__version__}" + (" (dry run: nothing written)"
                                                                               if dry_run else "")]
    with tempfile.TemporaryDirectory(prefix="ttp-rejudge-") as tmp:
        checked = handoffcheck.run(p, cfg, t, hand, Path(tmp))
    for b in checked.get("broken") or []:
        lines.append(f"hand-off check '{b['name']}' is broken ({b['why']}); not counted, as the daemon does")
    if bad := checked.get("failed"):
        lines.append(f"hand-off check '{bad['name']}' still fails, so the record is left as it is:\n"
                     f"{handoffcheck.event_tail(bad['output'])}")
        return lines, 1
    # Every task it accepts gets the cells verdict: a failed task (a partial one requeued, its run reaped)
    # keeps the mark its done hand-off earned. The daemon marks only done hand-offs, so a failed task
    # with no mark gets none.
    chk = cells.check(t.get("spec"), hand) if t["kind"] != "review" else None
    new_mark = cells.prefix(chk)
    if other and (new_mark or not chk):
        # Dropped only when this check finds every requested cell reported: else the rule's mark stays.
        other, old_mark, bare, own_fups = None, "", summary, fups
    if chk and not new_mark:
        lines.append(f"cells: all {len(chk['requested'])} requested cells reported")
    if new_mark == old_mark or (new_mark and not old_mark and t["status"] != "done"):
        lines.append("the checks give the verdict already on record; nothing to change")
        return lines, 0
    after = {k: v for k, v in result.items() if k not in (*DAEMON_KEYS, *(other or {}).get("keys", ()))}
    titles = [title, *(other or {}).get("dropped", ())]
    hops_kept: list[str] = []
    cancel: list[dict] = []
    if new_mark:
        # Still partial or unreported (new detail): the mark stays, updated; its hop and proposals stay.
        after.update(_keys(result), cells_checked=True)
        after["summary"] = summary if not sealed and cells.marked(summary) else new_mark + bare
    else:
        after["summary"] = bare
        if "followups" in result:
            after["followups"] = own_fups
        for h in db.q(f"SELECT id, title, status FROM tasks WHERE parent=? AND title IN "
                      f"({','.join('?' * len(titles))}) ORDER BY id", (task_id, *titles)):
            started = db.one("SELECT 1 FROM runs WHERE task=?", (h["id"],))
            if h["status"] == "queued" and not started:
                cancel.append(h)
            elif h["status"] != "cancelled":
                hops_kept.append(f"kept #{h['id']} {h['title']} ({h['status']}"
                                 f"{', started' if started and h['status'] == 'queued' else ''}): "
                                 f"rejudge leaves a hop that started alone")
    before_keys, after_keys = _keys(result), _keys(after)
    reason = (why.strip() + "; " if why.strip() else "") + \
        f"the checks at harness {__version__} give {new_mark.strip() or 'every requested cell finished'}"
    lines += [f"- summary: {summary[:300]}", f"+ summary: {after['summary'][:300]}",
              f"- keys: {_show(before_keys)}", f"+ keys: {_show(after_keys)}"]
    if after.get("followups") != result.get("followups"):
        lines += [f"- followup: {x}" for x in ((other or {}).get("dropped") or [title])]
    lines += [f"- key: {k}" for k in (other or {}).get("keys", ())]
    lines += [f"{'would cancel' if dry_run else 'cancelled'} #{h['id']} {h['title']} (queued, never started)"
              for h in cancel] + hops_kept
    if dry_run:
        return lines, 0
    now = time.time()
    after[REJUDGED] = {"ts": now, "version": __version__, "before": old_mark.strip()[:300]}
    gone: list[int] = []
    with db.tx():
        db.update_task(task_id, result=dump_result(after))
        for h in cancel:
            # Only while it still never started: a dispatch between the read and this write wins.
            if db.conn.execute("UPDATE tasks SET status='cancelled', blocked_reason=?, updated=? WHERE id=? "
                               "AND status='queued' AND NOT EXISTS (SELECT 1 FROM runs WHERE task=?)",
                               (f"cancelled by rejudge of #{task_id}: {reason}"[:500], now, h["id"],
                                h["id"])).rowcount:
                gone.append(h["id"])
        if not new_mark:
            for x in titles:
                db.x("UPDATE events SET status='handled' WHERE task=? AND kind='followup_proposed' "
                     "AND status='queued' AND instr(text, ?) > 0", (task_id, x))
        db.x("INSERT INTO events(ts,source,kind,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?)",
             (now, "cli", EVENT, "low",
              f"#{task_id} {t['title']} rejudged: {old_mark.strip() or '(no mark)'} -> "
              f"{new_mark.strip() or '(no mark)'} ({reason})"[:1500],
              json.dumps({"before": {"prefix": old_mark.strip()[:300], "keys": before_keys},
                          "after": {"prefix": new_mark.strip(), "keys": after_keys},
                          "reason": reason, "version": __version__, "cancelled": gone}),
              "handled", task_id))
    lost = [h for h in cancel if h["id"] not in gone]
    lines += [f"#{h['id']} started meanwhile: kept" for h in lost]
    return lines, 0

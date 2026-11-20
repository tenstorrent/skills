"""An open machine-ledger entry whose undo resumes one of this project's resource pauses
(`ttp resume <project> --resource <r>`) while that pause is gone: the pause was lifted, or vanished, and
the entry still describes it as live, so work it kept off (test-pool spills, locks) may be back on the
machine. The daemon checks this each time it routes the ledger (Daemon.route_machines) and raises one
normal alert per entry, keyed `pause-gone:<entry id>`; it clears once the entry closes or the pause is
back. KEY keeps the entries alerted on (alerts.holds reads it), so a repeat check never alerts again."""
from __future__ import annotations

import json
import os
import shlex
from typing import Any, Callable

from . import alerts, ends, machine_ledger
from .db import PAUSE_REMOVED_KIND

KEY = "pause_gone"   # kv: {entry id: resource} of the open ledger entries alerted on
KIND = "pause-gone"  # alert key prefix (alerts.STATUS_CONDITIONS)
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}


def resumed_resource(undo: Any, project: str) -> str | None:
    """The resource undo command `undo` resumes in project `project` (`ttp resume <project> --resource <r>`,
    also inside a chain or after `ttp lock ... --`), else None."""
    try:
        lex = shlex.shlex(str(undo or ""), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    for i, w in enumerate(words[:-1]):
        if os.path.basename(w) != "ttp" or words[i + 1] != "resume":
            continue
        args, j = [], i + 2
        while j < len(words) and words[j] not in SEPARATORS:
            args.append(words[j])
            j += 1
        names, res, k = [], None, 0
        while k < len(args):
            a = args[k]
            if a == "--resource" and k + 1 < len(args):
                res, k = args[k + 1], k + 2
                continue
            if a.startswith("--resource="):
                res = a.split("=", 1)[1]
            elif not a.startswith("-"):
                names.append(a)
            k += 1
        if res and names[:1] == [project]:
            return res
    return None


def _removed_line(db, res: str, where) -> str:
    """Who removed pause `res` last, from its PAUSE_REMOVED_KIND event, or ""."""
    for row in db.q("SELECT ts, data FROM events WHERE kind=? ORDER BY id DESC LIMIT 50", (PAUSE_REMOVED_KIND,)):
        try:
            d = json.loads(row["data"] or "{}")
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("resource") in (res, "*"):
            break
    else:
        return " (no event records who removed it)"
    return (f" (removed {ends.shown(float(row['ts']), where)} by {d.get('by') or 'unknown'} via "
            f"{d.get('via') or 'unknown'}: {d.get('reason') or 'no reason given'})")


def check(p, alert: Callable[..., None]) -> list[int]:
    """Alert once on each open ledger entry whose undo resumes a pause of project `p` that is gone, and
    clear the alerts of those whose entry closed or whose pause is back. Returns the ids alerting now."""
    db = p.db
    live = db.paused_resources()
    gone = {}
    for c in machine_ledger.changes():
        res = resumed_resource(c.get("undo"), p.name)
        if res and res not in live and isinstance(c.get("id"), int):
            gone[str(c["id"])] = (c, res)
    seen = db.kv(KEY) or {}
    for cid in sorted(set(seen) - set(gone)):
        with db.tx():
            cur = db.kv(KEY) or {}
            cur.pop(cid, None)
            db.set_kv(KEY, cur or None)
        alerts.close_key(db, f"{KIND}:{cid}", "the entry closed or the pause is back",
                         text="Cleared: " + alerts.CLEARED_TEXT[KIND].format(arg=cid))
    for cid, (c, res) in sorted(gone.items()):
        if cid in seen:
            continue
        with db.tx():
            db.set_kv(KEY, {**(db.kv(KEY) or {}), cid: res})
        alert(f"{KIND}:{cid}",
              f"Machine change #{cid} on {c.get('alias')} is still open and its undo resumes the pause of "
              f"`{res}` in this project, but that pause is gone{_removed_line(db, res, p)}. Work the pause kept "
              f"off may run there again. Close the entry (`ttp machines change close {cid}`) if the pause was "
              f"meant to end, else pause `{res}` again.", severity="normal")
    return sorted(int(c) for c in gone)

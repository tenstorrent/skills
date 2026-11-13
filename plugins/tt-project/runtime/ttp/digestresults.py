"""Task results and the last push batch in the coordinator's digest (coordinator.digest_results).

Each task named in this wake's events or messages, or finished in the last few hours, gets a short
summary of its last result and the path of its result file, so the coordinator need not look them
up. An entry goes out once per change: one the session already holds (same task, status, update and
result) is left out of a resumed wake. The seen list resets when the session is new or compacted,
or the last wake's digest was never acknowledged, and the whole list is always in the digest's
uncollapsed text, which such wakes get. A task whose whole hand-off is in this wake's events is left
out. One line names the last push batch, its outcome and, on a failure, what failed."""
from __future__ import annotations

import hashlib
import json
import re

from . import coordsession, leadmode, spans
from .db import load_result, task_outcome

KEY = "task_results"    # the coordinator.DIGEST_SEEN_KEY entry: {"session", "ids": {task id: hash}, "push"}
CAP = 6000              # chars (about 1.5k tokens) the entries may add to one digest
SUMMARY = 300           # chars of a result's summary
WINDOW_S = 6 * 3600     # finished tasks this recent are candidates; the seen list keeps them to once
KEEP = 300              # seen ids kept
NAMED_RE = re.compile(r"(?<!msg )(?<!message )(?:#|\btask:)(\d+)\b")
HEAD = "## Task results (named this wake or recently finished; each once per change; full result at its path)"
CLEAN = ("pushed", "landed", "nothing")


def on(cfg: dict) -> bool:
    """coordinator.digest_results: on unless set off."""
    return str((cfg.get("coordinator") or {}).get("digest_results", True)).strip().lower() not in (
        "false", "off", "no", "0")


def of(value) -> bool:
    """The config_set validator of coordinator.digest_results."""
    got = str(value).strip().lower()
    if got not in ("true", "on", "yes", "1", "false", "off", "no", "0"):
        raise ValueError("coordinator.digest_results: give true or false")
    return got in ("true", "on", "yes", "1")


def named(text: str) -> set[int]:
    """Task ids `text` names as #N or task:N (never 'msg #N')."""
    return {int(n) for n in NAMED_RE.findall(text or "")}


def _session(db, seen: dict | None) -> str | None:
    """The session the seen list belongs to, or None when the session may not hold what it saw: no
    acknowledged digest on the baseline `seen`, or a compacted history to restore."""
    state = db.kv(coordsession.KEY)
    if not isinstance(state, dict) or not state:
        return "none"   # no session: the stored baseline is all there is
    got = state.get(coordsession.DIGEST)
    if state.get("restore") or not isinstance(got, dict) or got.get("seen") != coordsession.fingerprint(seen):
        return None
    return f"{state.get('id')}:{len(state.get('compactions') or [])}"


def _hash(t: dict) -> str:
    return hashlib.sha256(json.dumps([t["id"], t["status"], t["updated"], t["result"]]).encode()).hexdigest()[:12]


def _entry(db, t: dict, tag: str, clip) -> str:
    r = load_result(t["result"])
    run = db.one("SELECT dir FROM runs WHERE task=? AND dir IS NOT NULL ORDER BY id DESC LIMIT 1", (t["id"],))
    path = f"{run['dir']}/result.json" if run and run["dir"] else "(no run)"
    push = db.one("SELECT status, COALESCE(landed_sha, pushed_sha) AS sha FROM push_queue WHERE task=? "
                  "ORDER BY id DESC LIMIT 1", (t["id"],))
    summary = clip(r.get("summary") or leadmode.spec_result(t.get("spec")) or t.get("blocked_reason") or "", SUMMARY)
    lines = [f"- #{t['id']} {task_outcome(t)}: {clip(t['title'], 90)}"]
    if summary:
        lines.append("  " + spans.wrap(summary, f"result of #{t['id']}", tag))
    lines.append(f"  result: {path}" + (f"; push {push['status']}" + (f" {push['sha'][:10]}" if push["sha"] else "")
                                        if push else ""))
    return "\n".join(lines)


def push_line(db, clip) -> str:
    """The last push batch: its outcome, pass or fail, and on a failure what its marker says failed."""
    b = db.one("SELECT id, outcome, marker FROM push_batches ORDER BY started DESC LIMIT 1")
    if not b:
        return ""
    if not b["outcome"]:
        return f"- last push batch {b['id']}: running"
    if b["outcome"] in CLEAN:
        return f"- last push batch {b['id']}: {b['outcome']}, checks passed"
    why = ""
    try:
        why = str(json.loads(open(b["marker"]).read()).get("message") or "")
    except (OSError, TypeError, ValueError):
        pass
    return f"- last push batch {b['id']}: {b['outcome']}, failed" + (f": {clip(why, 200)}" if why else "")


def lines(p, events: list[dict], said: str, seen: dict | None, now: float, tag: str,
          handoffs: set[str]) -> tuple[list[str], list[str], dict]:
    """(the section this wake shows, the whole section, what to store as seen). `events`: this wake's
    events; `said`: their text and this wake's messages'; `handoffs`: event sources whose whole
    hand-off is in this wake's events."""
    from .coordinator import clip
    db = p.db
    ids = named(" ".join(f"{e['source']} {e['text'] or ''}" for e in events) + " " + said)
    rows = []
    if ids:
        rows = db.q(f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))}) AND result IS NOT NULL "
                    "AND result != '' ORDER BY updated DESC", sorted(ids))
    rows += [t for t in db.q("SELECT * FROM tasks WHERE status IN ('done','failed','cancelled') AND updated>? "
                             "ORDER BY updated DESC LIMIT 30", (now - WINDOW_S,)) if t["id"] not in ids]
    rows = [t for t in rows if f"task:{t['id']}" not in handoffs]
    before = (seen or {}).get(KEY)
    session = _session(db, seen)
    kept = before.get("ids") if isinstance(before, dict) and session and before.get("session") == session else None
    old = kept if isinstance(kept, dict) else {}
    store = {"session": session, "ids": dict(old)}
    body, whole, size, left = [], [], 0, []
    for t in rows:
        h = _hash(t)
        if size >= CAP:
            left.append(t["id"])
            continue
        e = _entry(db, t, tag, clip)
        size += len(e)
        whole.append(e)
        if old.get(str(t["id"])) != h:
            body.append(e)
        store["ids"][str(t["id"])] = h
    if len(store["ids"]) > KEEP:
        store["ids"] = dict(sorted(store["ids"].items(), key=lambda kv: int(kv[0]))[-KEEP:])
    more = [f"- {len(left)} more, over the size cap: " + ", ".join(f"#{i}" for i in left)] if left else []
    push = push_line(db, clip)
    store["push"] = push
    shown_push = [push] if push and (not isinstance(kept, dict) or push != before.get("push")) else []
    if not whole and not push:
        return [], [], store
    full = [HEAD] + whole + more + ([push] if push else [])
    if body or more or shown_push:
        return [HEAD] + body + more + shown_push, full, store
    return [f"{HEAD}: none new since your last wake"], full, store

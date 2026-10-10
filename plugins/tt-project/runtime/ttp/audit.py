# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Self-efficiency audit, for the daily review: what the last day cost the user and the budget that
the project could have saved. Model-free; the review grades it.

- every ask sent (its question, blocking reason, recommendation, the user's answer and how long it
  took), asks the ask gate refused, and answers that handed the decision back ("decide yourself");
- worker runs that failed (a reboot, sleep, lost network or a resume is not a failure) and tasks
  run again after a failed attempt;
- coordinator turns that decided nothing (no action but `noop`, or an escalate), and idle and
  starve wakes (woken by the idle or idle-slot timer alone, with no message or event), with their cost;
- spend per finished task: the workers' own, and the coordinator's day spread over them;
- tasks blocked longer than `blocked_h` hours now;
- review loops: per change whose review moved in the window, its review rounds and their spend
  plus the fixes they asked for (a review's `auto_review:<task>` and `continues:<review>` labels);
- overrides held longer than `blocked_h`: resource pauses and active mutes, and temporary instructions
  whose end only a model can judge that were listed as possibly over a day or more ago.

An empty day is one line, NOTHING."""
from __future__ import annotations

import json
import time
from typing import Any

from . import budget, ends, unblock
from .db import DB
from .project import Project

WINDOW_S = 86400
BLOCKED_H = 12.0   # review.blocked_long_h: a task blocked this long is listed
NOTHING = "nothing to grade"
TOP = 5   # rows listed per section; the rest are counted


def _note(raw: Any) -> dict:
    try:
        note: Any = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3] + "..."


def _dur(s: float) -> str:
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min"


def asks(db: DB, since: float, now: float) -> list[dict]:
    """Asks sent since `since`, with their text, recommendation and answer (unblock.asks links them)."""
    from .coordinator import _REC_NOTE, _ask_question
    rows = unblock.asks(db, since, now)
    if not rows:
        return []
    msgs = {m["id"]: m for m in db.q(f"SELECT id, ts, text FROM messages WHERE id IN ({','.join('?' * len(rows))})",
                                     [r["id"] for r in rows])}
    out = []
    for r in rows:
        m = msgs.get(r["id"]) or {"ts": now, "text": ""}
        rec = m["text"].split(_REC_NOTE, 1)[1].split("\n\n")[0] if _REC_NOTE in m["text"] else ""
        out.append({**r, "question": _short(_ask_question(m["text"])), "recommendation": _short(rec, 100),
                    "wait_s": (r["answered_at"] - m["ts"]) if r.get("answered_at") else None,
                    "open_s": None if r.get("answered_at") else now - m["ts"]})
    return out


def rejected_asks(db: DB, since: float) -> list[str]:
    """ask_user actions the ask gate refused since `since`: the reason each got."""
    out = []
    for e in db.q("SELECT text, data FROM events WHERE kind='rejected_actions' AND ts>=? ORDER BY id", (since,)):
        try:
            items = json.loads(e["data"]) if e["data"] else None
        except ValueError:
            items = None
        if not isinstance(items, list):   # recorded before the list was kept: one joined line
            items = [e["text"]] if str(e["text"]).startswith("ask_user:") else []
        out += [str(x) for x in items if str(x).startswith("ask_user:")]
    return out


def failed_runs(db: DB, since: float) -> list[dict]:
    """Worker runs that ended since `since` in a failure of their own (budget.wasted: what the
    runaway guard counts as waste), most costly first."""
    rows = db.q("SELECT r.id, r.task, r.status, r.cost_usd, r.note, t.title FROM runs r LEFT JOIN tasks t "
                "ON t.id=r.task WHERE r.role!='coordinator' AND r.ended>=? AND r.status NOT IN ('ok','running')",
                (since,))
    return sorted([r for r in rows if budget.wasted(r)], key=lambda r: -float(r["cost_usd"] or 0))


def retried_tasks(db: DB, since: float) -> list[dict]:
    """Tasks run since `since` after at least one failed attempt."""
    return db.q("SELECT id, title, attempts, status, spent_usd FROM tasks WHERE attempts>1 AND id IN "
                "(SELECT task FROM runs WHERE role!='coordinator' AND started>=?) ORDER BY spent_usd DESC", (since,))


def coordinator_turns(db: DB, since: float) -> dict:
    """Coordinator turns since `since`: all, those that decided nothing, idle wakes and starve wakes,
    with costs. A turn decided nothing when its note says `decided` 0 (turns before that was logged
    are not counted; an escalated turn decided nothing itself); an idle or starve wake was due to the
    idle timer or the idle-slot (starve) timer with no message or event in its batch."""
    rows = db.q("SELECT note, cost_usd FROM runs WHERE role='coordinator' AND status='ok' AND started>=?", (since,))
    out = {"n": len(rows), "usd": 0.0, "noop": 0, "noop_usd": 0.0, "idle": 0, "idle_usd": 0.0,
           "starve": 0, "starve_usd": 0.0, "logged": 0}
    for r in rows:
        note, usd = _note(r["note"]), float(r["cost_usd"] or 0)
        out["usd"] += usd
        due = "starve" if note.get("wake_due") == "starve" else "idle" if note.get("wake_due") else None
        if due and not note.get("messages") and not note.get("events"):
            out[due] += 1
            out[f"{due}_usd"] += usd
        if "decided" in note:
            out["logged"] += 1
            if not note["decided"]:
                out["noop"] += 1
                out["noop_usd"] += usd
    return out


def finished(db: DB, since: float) -> list[dict]:
    """Tasks that ended (done or failed) since `since`, most costly first."""
    return db.q("SELECT id, title, status, spent_usd FROM tasks WHERE status IN ('done','failed') AND updated>=? "
                "ORDER BY spent_usd DESC", (since,))


def blocked_long(db: DB, now: float, hours: float) -> list[dict]:
    """Tasks blocked now for more than `hours`, longest first."""
    rows = [r for r in unblock.inventory(db, now) if r["kind"] == "blocked"
            and r["state_s"] is not None and r["state_s"] > hours * 3600]
    return sorted(rows, key=lambda r: -r["state_s"])


def _label(labels: Any, prefix: str) -> int | None:
    try:
        vals = json.loads(labels or "[]")
    except ValueError:
        return None
    for v in vals if isinstance(vals, list) else []:
        if isinstance(v, str) and v.startswith(prefix) and v[len(prefix):].isdigit():
            return int(v[len(prefix):])
    return None


def review_loops(db: DB, since: float) -> list[dict]:
    """Per change whose review chain moved since `since`, most costly first: {change, title, rounds,
    review_usd, fix_usd}. A chain is a first review (`auto_review:<change>`) and the reviews that
    continue it (`continues:<review>`); the fixes are the other tasks those reviews checked."""
    reviews = {r["id"]: r for r in db.q("SELECT id, labels, spent_usd, updated FROM tasks WHERE kind='review'")}
    chains: dict[int, list[dict]] = {}
    for r in reviews.values():
        root, seen = r, {r["id"]}
        while (prev := _label(root["labels"], "continues:")) in reviews and prev not in seen:
            seen.add(prev)
            root = reviews[prev]
        chains.setdefault(root["id"], []).append(r)
    out = []
    for root_id, chain in chains.items():
        change = _label(reviews[root_id]["labels"], "auto_review:")
        if change is None or not any(float(r["updated"] or 0) >= since for r in chain):
            continue
        fixes = {_label(r["labels"], "auto_review:") for r in chain} - {change, None}
        fix_usd = sum(float(t["spent_usd"] or 0) for t in db.q(
            f"SELECT spent_usd FROM tasks WHERE id IN ({','.join('?' * len(fixes))})", list(fixes))) if fixes else 0.0
        task = db.task(change) or {}
        out.append({"change": change, "title": task.get("title") or "", "rounds": len(chain),
                    "review_usd": sum(float(r["spent_usd"] or 0) for r in chain), "fix_usd": fix_usd})
    return sorted(out, key=lambda x: -(x["review_usd"] + x["fix_usd"]))


def overrides(p: Project | None, db: DB, now: float, hours: float) -> list[str]:
    """Overrides held longer than `hours`: resource pauses, active mutes, and temporary instructions whose
    end only a model can judge, listed as possibly over at least ends.RECHECK_S ago."""
    from .screen import mutes
    out = []
    for name, v in sorted(db.paused_resources(shared=False).items()):
        age = now - float(v.get("since") or now)
        if age > hours * 3600:
            out.append(f"resource {name} paused {_dur(age)} by {v.get('by') or 'unknown'}"
                       + (f" ({_short(v['reason'], 80)})" if v.get("reason") else ""))
    for m in mutes(db, now):
        age = now - float(m.get("since") or now)
        if age > hours * 3600:
            out.append(f"mute of {m.get('source')} {str(m.get('match'))[:60]!r} held {_dur(age)}, "
                       f"ends in {_dur(max(0.0, float(m.get('until') or now) - now))}")
    if p is not None:
        listed, broken = db.kv(ends.LISTED_KEY) or {}, db.kv(ends.BROKEN_KEY) or {}
        for item in ends.temporaries(p):
            first = listed.get(item["key"])
            if item["key"] in broken or (first is not None and now - float(first) >= ends.RECHECK_S):
                out.append(f"{item['what']} possibly over ({_short(ends.describe(item['end']), 100)}"
                           + (f"; its probe is broken: {_short(broken[item['key']], 60)}" if item["key"] in broken else "")
                           + ")")
    return out


def _more(rows: list, shown: int = TOP) -> str:
    return f"; and {len(rows) - shown} more" if len(rows) > shown else ""


def lines(db: DB, now: float | None = None, window_s: float = WINDOW_S, blocked_h: float = BLOCKED_H,
          p: Project | None = None) -> list[str]:
    """The daily review's 'Self-efficiency audit' lines for the last `window_s`; [NOTHING] on an empty
    day. With the project `p`, its temporary instructions are checked too."""
    now = time.time() if now is None else now
    since = now - window_s
    out: list[str] = []
    sent = asks(db, since, now)
    for a in sent:
        rec = f"; recommended: {a['recommendation']}" if a["recommendation"] else ""
        if a["answered"]:
            how = f"answered after {_dur(a['wait_s'])}: \"{a['answer']}\""
            how += f" (handed back: \"{a['handback']}\")" if a["handback"] else ""
        else:
            how = f"no answer yet, open {_dur(a['open_s'])}"
        out.append(f"ask {a['id']} ({a['blocking']}): \"{a['question']}\"{rec}; {how}")
    refused = rejected_asks(db, since)
    if refused:
        out.append(f"asks refused by the ask gate: {len(refused)}: "
                   + "; ".join(_short(x, 140) for x in refused[:TOP]) + _more(refused))
    back = [a for a in sent if a["handback"]]
    if sent:
        out.append(f"asks: {len(sent)} sent, {sum(a['answered'] for a in sent)} answered, {len(back)} handed back")
    fails = failed_runs(db, since)
    if fails:
        usd = sum(float(r["cost_usd"] or 0) for r in fails)
        out.append(f"failed worker runs: {len(fails)}, ${usd:.2f}: " + ", ".join(
            f"run {r['id']} task #{r['task']} {_short(r['title'] or '', 50)} {r['status']} ${float(r['cost_usd'] or 0):.2f}"
            for r in fails[:TOP]) + _more(fails))
    again = retried_tasks(db, since)
    if again:
        out.append(f"tasks run again after a failed attempt: {len(again)}: " + ", ".join(
            f"#{t['id']} {_short(t['title'], 50)} (attempt {t['attempts']}, {t['status']}, ${float(t['spent_usd'] or 0):.2f})"
            for t in again[:TOP]) + _more(again))
    turns = coordinator_turns(db, since)
    if turns["n"]:
        noop = (f"{turns['noop']} decided nothing (${turns['noop_usd']:.2f})" if turns["logged"] else
                "turns that decided nothing: not logged")
        part = f" of {turns['logged']} logged" if turns["logged"] and turns["logged"] < turns["n"] else ""
        out.append(f"coordinator turns: {turns['n']}, ${turns['usd']:.2f}; {noop}{part}; "
                   f"{turns['idle']} idle wakes (${turns['idle_usd']:.2f}), "
                   f"{turns['starve']} starve wakes (${turns['starve_usd']:.2f})")
    done = finished(db, since)
    if done:
        work = sum(float(t["spent_usd"] or 0) for t in done)
        out.append(f"finished tasks: {len(done)} ({sum(t['status'] == 'done' for t in done)} done), workers ${work:.2f} "
                   f"(${work / len(done):.2f} each), coordinator ${turns['usd']:.2f} in the window "
                   f"(${turns['usd'] / len(done):.2f} per finished task); most costly: " + ", ".join(
                       f"#{t['id']} {_short(t['title'], 50)} {t['status']} ${float(t['spent_usd'] or 0):.2f}"
                       for t in done[:3]))
    stuck = blocked_long(db, now, blocked_h)
    if stuck:
        out.append(f"blocked longer than {blocked_h:g} h now: {len(stuck)}: " + ", ".join(
            f"#{r['task']} {_short(r['title'], 50)} {_dur(r['state_s'])}" for r in stuck[:TOP]) + _more(stuck))
    loops = review_loops(db, since)
    if loops:
        rev, fix = sum(x["review_usd"] for x in loops), sum(x["fix_usd"] for x in loops)
        out.append(f"review loops: {len(loops)} changes, reviews ${rev:.2f} + fixes ${fix:.2f} "
                   f"(${(rev + fix) / len(loops):.2f} per change); most costly: " + ", ".join(
                       f"#{x['change']} {_short(x['title'], 50)} {x['rounds']} round{'s' if x['rounds'] > 1 else ''} "
                       f"${x['review_usd']:.2f} + fixes ${x['fix_usd']:.2f}" for x in loops[:3]))
    held = overrides(p, db, now, blocked_h)
    if held:
        out.append(f"overrides held long: {len(held)}: " + "; ".join(held[:TOP]) + _more(held))
    return out or [NOTHING]

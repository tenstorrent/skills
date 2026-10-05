# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Unblocking quality, for the daily review: how long stuck work stays stuck, how many asks the user
handed back ("decide yourself"), and how the coordinator's effort was split.

Stuck time: an episode starts when a task is handed off blocked, waiting or for review (a queued
task behind a dead dependency is set blocked too) and ends when the task next moves forward: a run
of it starts (a blocked or review task only; a waiting task's wakes keep waiting), it is done,
failed, requeued or pushed, or its status left the stuck state (cancelled, re-pointed). Consecutive
stuck hand-offs are one episode. An episode still open counts with its age so far.

Handed-back asks: a user message answers an ask when it names it (`#<id>`) after it was sent, or
replies in its chat thread. The answer's part about that ask is matched against HANDBACK, a short
phrase list of answers that give the decision back to the project.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .db import DB

WINDOWS = (("24 h", 86400), ("7 d", 7 * 86400))
STUCK = {"task_blocked": "blocked", "task_waiting": "waiting", "task_review": "review"}
MOVED = frozenset({"task_done", "task_failed", "task_queued", "task_requeued", "push_queued"})
ENDED = frozenset({"done", "failed", "cancelled", "pushing"})   # a status that ends any episode
ANSWER_MAX_AGE_S = 14 * 86400   # a mention of an ask id later than this is not its answer

# Answers that hand the decision back: the ask should not have been sent.
HANDBACK = tuple(re.compile(p, re.I) for p in (
    r"\bdecide (it |that |this )?(for )?yourself\b",
    r"\bdecide (it |that |this )?on your own\b",
    r"\byou (can |should |may )?decide\b",
    r"\b(it'?s |that'?s )?your (call|choice|decision)\b",
    r"\bup to you\b",
    r"\b(just|simply) (do|fix|handle) it\b",
    r"\buse your (own |best )?(judge?ment|discretion)\b",
    r"\bwhatever you (think|prefer|decide)\b",
    r"\b(do|go with) what(ever)? you think\b",
    r"\bno need to ask\b",
    r"\b(don'?t|do not|never|shouldn'?t|should not) (need to )?ask( me)?\b",
    r"\b(stop|quit) asking\b",
    r"\bwhy (are you |did you |do you )?ask(ing)?\b",
    r"\b(don'?t|do not|never) (get|stay|be) (blocked|stuck)\b",
    r"\bunblock (yourself|itself|efficiently)\b",
    r"\b(you )?(don'?t|do not) need my (ok|okay|approval|answer|input|permission)\b",
    r"\bhandle it yourself\b",
    r"\bi trust (you|your)\b",
))
_MENTION = re.compile(r"(?<![\w/])#(\d+)\b")
_NOT_ASK = re.compile(r"\b(task|pr|issue|run|pull request)\s*$", re.I)


def handback(text: str) -> str | None:
    """The phrase in an answer that hands the decision back, or None."""
    text = (text or "").replace("’", "'")
    for rx in HANDBACK:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None


def answer_part(text: str, ask_id: int, ask_ids: set[int]) -> str | None:
    """The part of a message about ask `ask_id`: from its `#id` to the next ask id it names, or
    None when it does not name it. A `#id` right after "task" or "PR" names something else."""
    found = [m for m in _MENTION.finditer(text or "") if int(m.group(1)) in ask_ids
             and not _NOT_ASK.search(text[:m.start()])]
    for i, m in enumerate(found):
        if int(m.group(1)) == ask_id:
            end = next((n.start() for n in found[i + 1:] if int(n.group(1)) != ask_id), len(text))
            return text[m.start():end]
    return None


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    k = max(0, min(len(sorted_vals) - 1, -(-int(q * 100) * len(sorted_vals) // 100) - 1))
    return sorted_vals[k]


def episodes(db: DB, since: float, now: float | None = None) -> list[dict]:
    """Stuck episodes that started at or after `since`: {task, title, kind, start, end, open, s}."""
    now = time.time() if now is None else now
    kinds = tuple(STUCK) + tuple(MOVED)
    ids = [r["task"] for r in db.q(
        f"SELECT DISTINCT task FROM events WHERE task IS NOT NULL AND ts>=? AND kind IN "
        f"({','.join('?' * len(STUCK))})", (since, *STUCK))]
    out: list[dict] = []
    for tid in ids:
        task = db.task(tid)
        if not task:
            continue
        marks = [(e["ts"], e["kind"]) for e in db.q(
            f"SELECT ts, kind FROM events WHERE task=? AND kind IN ({','.join('?' * len(kinds))})", (tid, *kinds))]
        marks += [(r["started"], "run") for r in db.q(
            "SELECT started FROM runs WHERE task=? AND role!='coordinator' AND started IS NOT NULL", (tid,))]
        marks.sort()
        ep: dict | None = None
        for ts, kind in marks:
            if kind in STUCK:
                if ep is None:
                    ep = {"task": tid, "title": task["title"], "kind": STUCK[kind], "start": ts, "mode": STUCK[kind]}
                ep["mode"] = STUCK[kind]
            elif ep and (kind in MOVED or (kind == "run" and ep["mode"] != "waiting")):
                ep["end"] = ts
                out.append(ep)
                ep = None
        if ep:
            status, upd = task["status"], float(task["updated"] or 0)
            left = status in ENDED or (ep["mode"] != "waiting" and status not in ("blocked", "review"))
            if left and upd >= ep["start"]:
                ep["end"] = upd
            out.append(ep)
    for ep in out:
        ep["open"] = "end" not in ep
        ep["s"] = max(0.0, (ep["end"] if not ep["open"] else now) - ep["start"])
        ep.pop("mode", None)
    return [ep for ep in out if ep["start"] >= since]


def _dur(s: float) -> str:
    return f"{s / 60:.0f} min" if s < 3600 else f"{s / 3600:.1f} h" if s < 4 * 86400 else f"{s / 86400:.1f} d"


def stuck_line(eps: list[dict]) -> str:
    """count, still open, median, p90 and the longest of some episodes."""
    if not eps:
        return "none"
    vals = sorted(e["s"] for e in eps)
    top = max(eps, key=lambda e: e["s"])
    opened = sum(e["open"] for e in eps)
    return (f"{len(eps)} ({opened} still open), median {_dur(_pct(vals, 0.5))}, p90 {_dur(_pct(vals, 0.9))}, "
            f"longest #{top['task']} {top['title'][:70]} {_dur(top['s'])}{' (still open)' if top['open'] else ''}")


def asks(db: DB, since: float, now: float | None = None) -> list[dict]:
    """Asks sent since `since`: {id, blocking, answered, handback (the phrase or None)}."""
    now = time.time() if now is None else now
    sent = db.q("SELECT id, ts, ref, ext_id FROM messages WHERE direction='out' AND kind='ask' AND ts>=? ORDER BY id",
                (since,))
    if not sent:
        return []
    every = {r["id"] for r in db.q("SELECT id FROM messages WHERE direction='out' AND kind='ask'")}
    replies = db.q("SELECT ts, text, ref FROM messages WHERE direction='in' AND kind='user' AND ts>=? ORDER BY id",
                   (sent[0]["ts"],))
    out = []
    for a in sent:
        ref = a["ref"] or ""
        row = {"id": a["id"], "blocking": ref.split(":", 1)[1] if ref.startswith("blocking:") else "unset",
               "answered": False, "handback": None}
        for m in replies:
            if not a["ts"] < m["ts"] <= a["ts"] + ANSWER_MAX_AGE_S:
                continue
            part = answer_part(m["text"], a["id"], every)
            if part is None and a["ext_id"] and m["ref"] == a["ext_id"]:
                part = m["text"]   # a reply in the ask's chat thread
            if part is None:
                continue
            row["answered"] = True
            row["handback"] = row["handback"] or handback(part)
        out.append(row)
    return out


def asks_line(rows: list[dict]) -> str:
    if not rows:
        return "no asks sent"
    back = [r for r in rows if r["handback"]]
    answered = sum(r["answered"] for r in rows)
    line = (f"{len(rows)} sent, {answered} with a linked answer, {len(back)} handed back "
            f"({100 * len(back) / len(rows):.0f}% of asks)")
    if back:
        line += ": " + ", ".join(f"#{r['id']} ({r['blocking']}; \"{r['handback']}\")" for r in back)
    return line


def turns_line(db: DB, since: float) -> str:
    """The coordinator's high/low turn split and, where its turns log them, escalations and triggers.
    A note's `triggers` list counts each trigger on its own; an "escalated: <why>" entry counts
    under "escalated". Notes without that list fall back to `trigger` or `unblock`."""
    rows = db.q("SELECT effort, note FROM runs WHERE role='coordinator' AND started>=?", (since,))
    if not rows:
        return "no coordinator turns"
    split: dict[str, int] = {}
    escalated, logged, why = 0, False, {}
    for r in rows:
        split[r["effort"] or "default"] = split.get(r["effort"] or "default", 0) + 1
        note = _note(r["note"])
        keys = [k for k in note if "escalat" in str(k)]
        logged = logged or bool(keys)
        escalated += any(bool(note[k]) for k in keys)
        for t in _triggers(note):
            why[t] = why.get(t, 0) + 1
    line = f"{len(rows)} turns: " + ", ".join(f"{n} {k}" for k, n in sorted(split.items(), key=lambda kv: -kv[1]))
    line += f"; {escalated} escalated low to high" if logged else "; escalations not logged"
    if why:
        line += "; high-effort triggers: " + ", ".join(f"{k} {n}" for k, n in sorted(why.items(), key=lambda kv: -kv[1])[:5])
    return line


def _note(raw: Any) -> dict:
    try:
        note: Any = json.loads(raw or "{}")
    except ValueError:
        return {}
    return note if isinstance(note, dict) else {}


def _triggers(note: dict) -> list[str]:
    """A turn's trigger labels, each cut to 40 chars; "escalated: <why>" becomes "escalated"."""
    trigs = note.get("triggers")
    if isinstance(trigs, list):
        return ["escalated" if str(t).startswith("escalated") else str(t)[:40] for t in trigs if t]
    trig = note.get("trigger") or note.get("unblock")
    return [str(trig)[:40]] if trig else []


def triggers_line(db: DB, since: float) -> str:
    """Coordinator turns per trigger label, the raised vs routine share with their cost, and the
    `escalations` counts (routine turns escalated, escalations refused)."""
    rows = db.q("SELECT note, cost_usd FROM runs WHERE role='coordinator' AND started>=?", (since,))
    if not rows:
        return "no coordinator turns"
    per: dict[str, int] = {}
    raised = [0, 0.0]
    routine = [0, 0.0]
    for r in rows:
        trigs = _triggers(_note(r["note"]))
        side = raised if trigs else routine
        side[0] += 1
        side[1] += float(r["cost_usd"] or 0)
        for t in trigs:
            per[t] = per.get(t, 0) + 1
    n = len(rows)
    line = (f"raised {raised[0]} ({100 * raised[0] / n:.0f}%, ${raised[1]:.2f}), "
            f"routine {routine[0]} ({100 * routine[0] / n:.0f}%, ${routine[1]:.2f})")
    if per:
        line += "; per trigger: " + ", ".join(f"{k} {v}" for k, v in sorted(per.items(), key=lambda kv: (-kv[1], kv[0])))
    esc = db.kv("escalations", {}) or {}
    line += f"; escalations: {int(esc.get('n', 0))} (refused {int(esc.get('refused', 0))})"
    return line


def lines(db: DB, now: float | None = None) -> list[str]:
    """The daily review's 'Unblocking quality' lines."""
    now = time.time() if now is None else now
    out = []
    for label, span in WINDOWS:
        eps = episodes(db, now - span, now)
        for kind in STUCK.values():
            out.append(f"stuck {kind}, {label}: {stuck_line([e for e in eps if e['kind'] == kind])}")
    for label, span in WINDOWS:
        out.append(f"asks, {label}: {asks_line(asks(db, now - span, now))}")
    out.append(f"coordinator, 24 h: {turns_line(db, now - 86400)}")
    out.append(f"coordinator triggers, 24 h: {triggers_line(db, now - 86400)}")
    return out

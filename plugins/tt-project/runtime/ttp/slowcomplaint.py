# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Speed complaints: a user message saying work is slow, stuck or waiting starts a measured throughput
review the same tick, model-free (throughput.py), instead of a prose answer.

`detect` matches a short generic word list (WORDS, whole words) or 'again' with a delay word
(AGAIN_DELAY). At a coordinator turn's start the daemon calls `on_messages` with the turn's new
messages: on an inbound user message that matches it runs `throughput.review(apply=True)`, queues one
priority 1 task listing the slow lanes it could not change itself (at most one open at a time: later
lanes are added to it; a lane a closed one listed waits for new evidence or a cooldown, _followups),
and returns the result as a digest section, so the turn reports numbers and changes. A second
complaint the same day runs it again; the result lists the earlier changes (throughput.recent)."""
from __future__ import annotations

import re
import time

from . import throughput
from .db import DB, TERMINAL_TASK_STATES
from .lanewait import _tell

WORDS = ("slow", "slower", "slowly", "stuck", "waiting", "taking forever", "takes forever",
         "throughput", "asap", "in the queue", "in queue", "queued for", "queue is", "hours", "sitting idle", "idle for", "too long", "faster", "speed up", "sluggish")
AGAIN_DELAY = ("again", "still")
DELAY = ("hour", "hours", "day", "days", "long", "wait", "waiting", "yet", "minutes")
_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in WORDS) + r")\b")
TITLE = "throughput: "   # follow-up tasks are titled `throughput: <lane> ...`
SECTION = "throughput"


def detect(text: str) -> str:
    """Why `text` is a speed complaint ('word <w>' or 'again + delay'), or ''."""
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    if m := _RE.search(low):
        return f"word '{m.group(1)}'"
    words = set(re.findall(r"[a-z]+", low))
    if words & set(AGAIN_DELAY) and words & set(DELAY):
        return "again + delay"
    return ""


def complaints(db: DB, msgs: list[dict]) -> dict[int, str]:
    """{message id: why} for the inbound user messages in `msgs` that complain about speed. Rows with
    no `text` (the daemon's turn-sizing query) get theirs read here."""
    ids = [int(m["id"]) for m in msgs]
    if not ids:
        return {}
    rows = db.q(f"SELECT id, direction, kind, text FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
    out = {}
    for m in rows:
        if (m["direction"] or "in") != "in" or (m["kind"] or "user") == "finding":
            continue
        if why := detect(m["text"] or ""):
            out[int(m["id"])] = why
    return out


FACTOR = 1.5          # a lane's waits or p90 grown this much since it was last listed is new evidence
COOLDOWN_S = 86400    # a closed throughput task holds its lanes this long on unchanged evidence (the 24 h window)
_LISTED = re.compile(r"lane (\S+) slow: (\d+) wait\(s\), median \S+, p90 (\S+?)s\b")


def _line(k: dict, ln: dict) -> str:
    """One lane's evidence line; _LISTED reads it back from the task's spec."""
    return (f"lane {k['lane']} slow: {ln.get('count')} wait(s), median {ln.get('median_s')}s, p90 {ln.get('p90_s')}s"
            f" (priority 1 p90 {ln.get('p1_p90_s')}s); limit {k['key']}={ln.get('limit')}. `ttp throughput "
            f"--apply` did not change it: {k['why']}.")


def _num(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _grown(old: tuple[float, float], ln: dict) -> bool:
    (n0, p0), n, p = old, _num(ln.get("count")), _num(ln.get("p90_s"))
    return (n0 > 0 and n >= FACTOR * n0) or (p0 > 0 and p >= FACTOR * p0)


def _followups(db: DB, rep: dict, msg_ids: list[int], now: float | None = None) -> dict:
    """At most one open throughput task: the slow lanes it kept go into one new priority 1 task, or, while
    one is open, into that task's spec (and its running worker's steer.md). A lane last listed in a closed
    task (cancelled, failed or done: its 24 h window still holds the old waits) is held for COOLDOWN_S
    unless its waits or p90 grew by FACTOR. Returns {"opened": id|None, "listed": (id, lanes)|None,
    "held": [(lane, task id)]}."""
    now = time.time() if now is None else now
    rows = db.q("SELECT id, status, updated, spec FROM tasks WHERE origin='daemon' AND title LIKE ? "
                "ORDER BY id DESC LIMIT 200", (f"{TITLE}%",))
    open_ = min((r["id"] for r in rows if r["status"] not in TERMINAL_TASK_STATES), default=None)
    out, new, cite = {"opened": None, "listed": None, "held": []}, [], ", ".join("#" + str(i) for i in msg_ids)
    for k in rep.get("kept") or []:
        ln = next((x for x in rep["lanes"] if x["lane"] == k["lane"]), {})
        prev = next(((r, m) for r in rows for m in reversed(_LISTED.findall(r["spec"] or "")) if m[0] == k["lane"]),
                    None)
        if prev and prev[0]["status"] not in TERMINAL_TASK_STATES:
            continue   # listed in an open task already
        if prev and now - float(prev[0]["updated"] or 0) < COOLDOWN_S \
                and not _grown((_num(prev[1][1]), _num(prev[1][2])), ln):
            out["held"].append((k["lane"], prev[0]["id"]))
            continue
        new.append((k, ln))
    if not new:
        return out
    lines = "\n".join("- " + _line(k, ln) for k, ln in new)
    if open_:
        t = db.one("SELECT spec FROM tasks WHERE id=?", (open_,))
        note = (f"Throughput update (speed complaint {cite}): also over threshold, find what holds these lanes "
                f"too:\n{lines}")
        db.update_task(open_, spec=(t["spec"] or "").rstrip() + "\n\n" + note + "\n")
        _tell(db, open_, note, f"throughput-{open_}-{int(now)}")
        out["listed"] = (open_, [k["lane"] for k, _ in new])
        return out
    names = [k["lane"] for k, _ in new]
    title = (f"{TITLE}{names[0]} waits over threshold; {new[0][0]['key'] or 'no setting'} not raised" if len(new) == 1
             else f"{TITLE}{', '.join(names)} waits over threshold; not raised")[:200]
    spec = (f"urgent: a user's speed complaint (message {cite}) found {len(new)} lane(s) slow:\n{lines}\n"
            "Find what holds each lane and change it (a setting, a default, a missing lever), measured before "
            "and after with `ttp throughput --json`. Lanes found slow while this task is open are added here.")
    out["opened"] = db.add_task(title, spec, kind="code", priority=1, origin="daemon")
    return out


def on_messages(p, cfg: dict, msgs: list[dict], now: float | None = None, gates: dict | None = None) -> str:
    """The digest section for a turn whose messages include a speed complaint, or ''."""
    now = time.time() if now is None else now
    hit = complaints(p.db, msgs)
    if not hit:
        return ""
    trigger = "speed complaint " + ", ".join(f"#{i} ({w})" for i, w in hit.items())
    try:
        rep = throughput.review(p, cfg, do_apply=True, trigger=trigger, gates=gates, now=now)
        fu = _followups(p.db, rep, list(hit), now)
    except Exception as e:   # the turn still runs; it sees the review failed and why
        return (f"## Speed complaint ({trigger})\nThe throughput review failed: {type(e).__name__}: {e}"[:600]
                + "\nRun `ttp throughput --apply` in a task and report its numbers.")
    return (f"## Speed complaint: measured throughput review ({trigger})\n{throughput.text(rep)}\n"
            + (f"Queued priority 1 follow-up #{fu['opened']}\n" if fu["opened"] else "")
            + (f"Added lane(s) {', '.join(fu['listed'][1])} to the open follow-up #{fu['listed'][0]}\n"
               if fu["listed"] else "")
            + (f"Not reopened (unchanged evidence since a closed follow-up): "
               f"{', '.join(f'{ln} (#{t})' for ln, t in fu['held'])}\n" if fu["held"] else "")
            + "Reply with these numbers and changes, not prose alone.")

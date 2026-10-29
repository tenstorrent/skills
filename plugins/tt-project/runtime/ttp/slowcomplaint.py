# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Speed complaints: a user message saying work is slow, stuck or waiting starts a measured throughput
review the same tick, model-free (throughput.py), instead of a prose answer.

`detect` matches a short generic word list (WORDS, whole words) or 'again' with a delay word
(AGAIN_DELAY). At a coordinator turn's start the daemon calls `on_messages` with the turn's new
messages: on an inbound user message that matches it runs `throughput.review(apply=True)`, queues one
priority 1 task per slow lane it could not change itself (one open task per lane, never a second),
and returns the result as a digest section, so the turn reports numbers and changes. A second
complaint the same day runs it again; the result lists the earlier changes (throughput.recent)."""
from __future__ import annotations

import re
import time

from . import throughput
from .db import DB

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


def _followups(db: DB, rep: dict, msg_ids: list[int]) -> list[int]:
    """One priority 1 task per slow lane it kept, unless an open one for the lane exists."""
    out = []
    for k in rep.get("kept") or []:
        title = f"{TITLE}{k['lane']} waits over threshold; {k['key'] or 'no setting'} not raised"[:200]
        if db.one("SELECT 1 FROM tasks WHERE title LIKE ? AND status NOT IN ('done','failed','cancelled')",
                  (f"{TITLE}{k['lane']} %",)):
            continue
        ln = next((x for x in rep["lanes"] if x["lane"] == k["lane"]), {})
        spec = (f"urgent: a user's speed complaint (message {', '.join('#' + str(i) for i in msg_ids)}) found lane "
                f"{k['lane']} slow: {ln.get('count')} wait(s), median {ln.get('median_s')}s, p90 {ln.get('p90_s')}s"
                f" (priority 1 p90 {ln.get('p1_p90_s')}s); limit {k['key']}={ln.get('limit')}. `ttp throughput "
                f"--apply` did not change it: {k['why']}. Find what holds this lane and change it (a setting, a "
                f"default, a missing lever), measured before and after with `ttp throughput --json`.")
        out.append(db.add_task(title, spec, kind="code", priority=1, origin="daemon"))
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
        tasks = _followups(p.db, rep, list(hit))
    except Exception as e:   # the turn still runs; it sees the review failed and why
        return (f"## Speed complaint ({trigger})\nThe throughput review failed: {type(e).__name__}: {e}"[:600]
                + "\nRun `ttp throughput --apply` in a task and report its numbers.")
    return (f"## Speed complaint: measured throughput review ({trigger})\n{throughput.text(rep)}\n"
            + (f"Queued priority 1 follow-up(s): {', '.join('#' + str(t) for t in tasks)}\n" if tasks else "")
            + "Reply with these numbers and changes, not prose alone.")

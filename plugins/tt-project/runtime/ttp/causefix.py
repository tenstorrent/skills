# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Named a cause without a fix: a coordinator `reply` or `notify` that names a limit, lock, queue,
cap, slot or setting as the cause of slowness or a wait is backed in the same batch by the change
that fixes it (a task_add, a task_update of priority, spec, resources or status, a config_set or a
charter_update). Telling the user "it waits on the lock" and queuing nothing leaves them to repeat
the complaint.

Model-free and flag-only: `check` runs after a batch is applied and never rejects anything. Each hit
writes a `cause_without_fix` event (a log entry, already handled) and a record; the next digest's
Course check carries one line naming the replies (`digest_lines`), shown once. A reply that says why
there is no fix (NO_FIX: "no fix because", "no fix:", "not fixing", "already fixed") passes. The pattern is
narrow: a cause word (CAUSE) and a slowness word (SLOW) in one sentence, at most NEAR words apart.
A reply about an old cause is judged by the same words, so it passes by saying it is fixed."""
from __future__ import annotations

import json
import re
import time

from .db import DB

KEY = "cause_flags"   # kv: [{"msg", "ts", "kind", "cause", "text", "turn", "shown"}], newest last
EVENT = "cause_without_fix"
KEEP_S = 7 * 86400
KEEP_N = 100
DAY = 86400
NEAR = 6   # words at most between the cause word and the slowness word
TEXT_CHARS = 80
CAUSE = re.compile(r"\b(?:limits?|limited|locks?|locked|queues?|queueing|queuing|caps?|capped|slots?|"
                   r"serializ(?:e|es|ed|ing)|serialis(?:e|es|ed|ing))\b", re.I)
SLOW = re.compile(r"\b(?:wait(?:s|ed|ing)?|slow(?:s|ed|er|ly|ing|ness)?|hours?|blocked|blocking)\b", re.I)
NO_FIX = re.compile(r"\bno\s+fix(?:\s+is)?\s+(?:because|needed|since)\b|\bno\s+fix\s*:|\bnot\s+fixing\b|"
                    r"\bno\s+change\s+(?:needed|because)\b|"
                    r"\b(?:already|was|were|is|are|been|now)\s+fixed\b|\bfixed\s+in\s+\S", re.I)
FIX_TYPES = ("task_add", "config_set", "charter_update")
FIX_FIELDS = ("priority", "spec", "resources", "status")   # a task_update changing one of these is a fix
WORD = re.compile(r"[\w'’-]+")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def names_cause(text: str) -> str:
    """The sentence of `text` naming a cause of slowness ('' for none), unless `text` says why there is
    no fix."""
    text = (text or "").replace("’", "'")
    if NO_FIX.search(text):
        return ""
    for s in _sentences(text):
        words = WORD.findall(s)
        cause = [i for i, w in enumerate(words) if CAUSE.fullmatch(w)]
        slow = [i for i, w in enumerate(words) if SLOW.fullmatch(w)]
        if any(abs(c - w) - 1 <= NEAR for c in cause for w in slow):
            return " ".join(s.split())
    return ""


def is_fix(a: dict) -> bool:
    t = (a or {}).get("type")
    return t in FIX_TYPES or (t == "task_update" and any(a.get(f) not in (None, "", []) for f in FIX_FIELDS))


def _records(db: DB) -> list[dict]:
    got = db.kv(KEY, []) or []
    return [r for r in got if isinstance(r, dict) and str(r.get("msg", "")).isdigit()]


def check(db: DB, sent: list[tuple[int, str, str]], applied: list[dict], turn=None,
          now: float | None = None) -> list[int]:
    """After a batch: flag each sent (message id, kind, text) naming a cause when no `applied` action
    fixes anything. A coordinator turn (`turn` set) marks records from earlier turns shown (its digest
    carried them); a turn-less apply (a chain hop between turns) marks nothing, so a flag waits for the
    next turn's Course check. Returns the flagged message ids."""
    now = time.time() if now is None else now
    recs = _records(db)
    for r in recs:
        if turn is not None and r.get("turn") != turn:
            r["shown"] = True
    hits = [] if any(is_fix(a) for a in applied) else \
        [(mid, kind, s) for mid, kind, text in sent if (s := names_cause(text))]
    have = {int(r["msg"]) for r in recs}
    flagged = []
    for mid, kind, s in hits:
        if mid in have:
            continue   # the same turn applied again
        clip = s if len(s) <= TEXT_CHARS else s[:TEXT_CHARS - 1] + "…"
        recs.append({"msg": mid, "ts": now, "kind": kind, "text": clip, "turn": turn, "shown": False})
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
             (now, "coordinator", EVENT, f"{EVENT}:{mid}", "low",
              f"{kind} #{mid} named a cause of slowness with no fix in its batch: {clip}",
              json.dumps({"message": mid, "kind": kind}), "handled"))
        flagged.append(mid)
    db.set_kv(KEY, [r for r in recs if now - float(r.get("ts") or 0) <= KEEP_S][-KEEP_N:])
    return flagged


def digest_lines(db: DB, now: float | None = None) -> list[str]:
    """One Course check line for the replies of the last turn that named a cause and queued no fix;
    [] when there are none."""
    now = time.time() if now is None else now
    got = [r for r in _records(db) if not r.get("shown") and now - float(r.get("ts") or 0) <= DAY]
    if not got:
        return []
    said = "; ".join(f"{r.get('kind') or 'reply'} #{r['msg']}: \"{r.get('text') or ''}\"" for r in got[:3])
    more = f" and {len(got) - 3} more" if len(got) > 3 else ""
    return [f"- named a cause without a fix: {said}{more} | queue the fix now (a task, a priority or "
            "resources change, a config_set), or say `no fix because <why>` in a reply"]

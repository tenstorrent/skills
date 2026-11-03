# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Announced a charter change without sending it: a coordinator `reply` or `notify` that tells the chat,
in the first person and present or future tense, that it is adding, changing or removing a charter
item ("I'm adding the Restrictions item", "I'll record that in the charter", "adding it to the
charter") is backed by a `charter_update` in the same batch, applied or refused. Without one the
chat waits on a change that never reaches the charter, and a task that needs it blocks and asks again.

Model-free and flag-only: `check` runs after a batch is applied and never rejects anything. Each hit
queues one EVENT for the next coordinator turn, quoting the reply and asking it to send the change or
tell the chat it is not being made. A refused charter_update raises no EVENT: its charter_retry event
names the batch's announcing replies instead (`quote_lines`).

The matcher is narrow (`announces`): a change verb in the first person present or future ("I'm
adding", "I will record", "let me add", a clause opening with "Adding"), then a charter object
(charter, Restrictions, Policies, a Goals or Policy item) at most NEAR words on, in one clause.
Skipped: past tense ("I added", "I've recorded"), negations ("I'm not adding", "I won't"),
questions, offers conditional on the user ("once you confirm, I'll add"), quoted text (the charter
quoted back, in quotes, backticks or `>` lines), a task or memory about the charter ("I'm adding a
task to update the charter") and "update you"."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .db import DB

EVENT = "charter_unsent"
NEAR = 6   # words at most between the change verb and the charter object, in its clause
TEXT_CHARS = 300
_VERB_ING = r"(?:adding|recording|putting|writing|updating|changing|removing|dropping|amending|editing|" \
            r"retiring|replacing|inserting)"
_VERB = r"(?:add|record|put|write|update|change|remove|drop|amend|edit|retire|replace|insert)"
_ADV = r"(?:(?:now|also|just|right\s+now|then|go\s+ahead\s+and)\s+)?"
ANNOUNCE = re.compile(
    rf"\b(?:(?:i|we)\s*(?:'m|\s+am|'re|\s+are)\s+{_ADV}{_VERB_ING}"
    rf"|(?:i|we)\s*(?:'ll|\s+will|\s+shall|(?:'m|\s+am|'re|\s+are)\s+going\s+to)\s+{_ADV}{_VERB}"
    rf"|let\s+me\s+{_VERB})\b"
    rf"|(?:^|[,;:—–]\s*|\s-\s)(?:now\s+|also\s+)?{_VERB_ING}\b", re.I)
OBJECT = re.compile(r"\b(?:charter|restrictions?|policies|(?:polic(?:y|ies)|goals?)\s+(?:item|section|list))\b",
                    re.I)
# Words between the verb and the object that make the charter something else's subject.
NOT_THE_CHANGE = re.compile(r"\b(?:tasks?|workers?|followups?|memory|you|the\s+user)\b", re.I)
CONDITIONAL = re.compile(r"\b(?:if|once|when|after|should|unless)\s+(?:you|the\s+user)\b|\bwant\s+me\s+to\b|"
                         r"\b(?:shall|should)\s+i\b|\bwith\s+your\s+(?:ok|okay|yes|approval)\b", re.I)
QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”|`[^`\n]*`|^\s*>.*$", re.M)
WORD = re.compile(r"[\w'’-]+")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def announces(text: str) -> str:
    """The sentence of `text` announcing a charter change in the first person present or future, '' for
    none (see the module docstring for what is skipped)."""
    text = QUOTED.sub(" ", (text or "").replace("’", "'"))
    for s in _sentences(text):
        if s.rstrip().endswith("?") or CONDITIONAL.search(s):
            continue
        for m in ANNOUNCE.finditer(s):
            rest = re.split(r"[;:()—–]", s[m.end():], maxsplit=1)[0]
            obj = OBJECT.search(rest)
            if not obj or len(WORD.findall(rest[:obj.start()])) > NEAR:
                continue
            if NOT_THE_CHANGE.search(rest[:obj.start()]):
                continue
            return " ".join(s.split())
    return ""


def has_update(actions: list) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "charter_update" for a in actions or [])


def announcing(actions: list) -> list[tuple[str, str]]:
    """(kind, sentence) of each reply or notify in `actions` that announces a charter change."""
    return [(a["type"], s) for a in actions or []
            if isinstance(a, dict) and a.get("type") in ("reply", "notify") and (s := announces(str(a.get("text") or "")))]


def _clip(s: str) -> str:
    return s if len(s) <= TEXT_CHARS else s[:TEXT_CHARS - 1] + "…"


def quote_lines(said: list[tuple[str, str]]) -> str:
    """The charter_retry event's line naming the batch's replies that announced the change, '' for none."""
    if not said:
        return ""
    return (" This batch also told the chat the change was being made: "
            + "; ".join(f"{kind}: \"{_clip(s)}\"" for kind, s in said[:3])
            + ". If the change does not go in this turn, tell that chat it is not made yet, and why.")


def check(db: DB, sent: list[tuple[int, str, str]], actions: list, turn=None, now: float | None = None) -> list[int]:
    """After a batch: queue one EVENT per sent (message id, kind, text) announcing a charter change when
    the batch (`actions`, applied or refused) carries no charter_update. The same message applied again
    raises nothing twice. Returns the flagged message ids."""
    if has_update(actions):
        return []
    now = time.time() if now is None else now
    flagged = []
    for mid, kind, text in sent:
        if not (s := announces(text)):
            continue
        fp = f"{EVENT}:{mid}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        if db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            continue
        db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status) VALUES(?,?,?,?,?,?,?,?)",
             (now, "harness", EVENT, fp, "normal",
              f"{kind} #{mid} told the chat a charter change is being made, but no charter_update was in its "
              f"batch: \"{_clip(s)}\". Send that charter_update now, or tell the chat it is not being made.",
              json.dumps({"message": mid, "kind": kind, "turn": turn}), "queued"))
        flagged.append(mid)
    return flagged

"""What a task that answers a chat (`reply_chat`) posts there, and when it posts nothing.

The chat gets the hand-off's `message` verbatim when it is present and non-empty, else its `summary`
(less a trailing AI-authorship tag, which db.post drops from every message to the user: aitag.for_user).
The chat sees nothing else of the run: not its tool output, not its final reply. A done hand-off whose
text is empty, or only describes the run ("returned them as the message for the user") with no content
of its own, delivers nothing: `undelivered` names why, and the daemon sends the task to review instead
of posting it, so the coordinator sees that the user got nothing. The check runs on every reply_chat worker's done hand-off, a
code task's too (it then waits in review for the coordinator); a landed reply from the push queue posts
the `message` the worker's hand-off already passed it with.

A hand-off from a run on the chat provider (GLM) is data: its text posts only as the Opus write-up
(writeup.py), which stamps it; `guard` refuses any other, on every path that posts it.
"""
from __future__ import annotations

import hashlib
import re

FIELD = "message"
# A code task's done reply held until its review passes and the push queue ships it (pushq.post_held):
# the chat never hears "shipped" while the review and landing are still ahead.
HELD = "reply_held"
# A hand-off from a run on the chat provider (GLM) posts only as the Opus write-up (writeup.py): `BY` is that
# step's stamp, `CHAT_RUN` marks the hand-off as such a run's.
BY = "message_by"
CHAT_RUN = "chat_run"
WRITEUP = "opus-writeup"

# Phrases of a run describing its own delivery, not delivering. Narrow on purpose: each names the
# reply itself as an object ("them as the message", "the steps to the user", "is above"), never a
# plain mention of the user or of sending, a comparison ("it is above the target") or a named
# deliverable's state ("the release note is ready to send").
_OBJ = r"(?:it|them|this|that|these|those|the\s+(?:\w+\s+){0,3}?(?:message|reply|answer|steps|text|result|instructions)s?)"
_SELF = re.compile(
    r"\b(?:returned|sent|relayed|posted|delivered|passed|gave|handed)\s+" + _OBJ
    + r"\s+(?:\w+\s+){0,2}?(?:to|for|as)\s+(?:the|this\s+task'?s)\s+(?:user|chat|message|reply|answer|result)\b"
    r"(?:\s+(?:to|for)\s+the\s+(?:user|chat)\b)?"
    r"|\b(?:sent|gave|returned)\s+the\s+user\s+" + _OBJ + r"(?=[\s.,;:]|$)"
    r"|\bas\s+(?:the|this\s+task'?s)\s+(?:message|reply|answer|result)\s+(?:to|for)\s+the\s+(?:user|chat)\b"
    r"|\b(?:it|they|message|reply|answer|steps|text|instructions)\s+(?:is|are)\s+(?:written|shown|given|listed)\s+"
    r"(?:out\s+)?above\b"
    r"|\b(?:message|reply|answer|steps|text|instructions)\s+(?:is|are)\s+above\s*(?:[.!]|$)"
    r"|\bwritten\s+out\s+above\b"
    r"|\b(?:message|reply|answer|steps|text|instructions)\s+(?:is|are)\s+ready\s+to\s+(?:send|relay|post|paste)\b"
    r"|(?:^|[;,]\s*)ready\s+to\s+(?:send|relay|post|paste)\s*[.!]?\s*$",
    re.I)
# What still marks content once the self-descriptions are cut out: a colon before more text, a numbered
# item ("1)", "2."), or a list of three or more parts split by semicolons.
_CONTENT = re.compile(r":\s+\w|(?:^|\s)\d+[.)]\s+\w|;[^;]*\w[^;]*;")
# Past this length, or with code or several lines, a text carries content of its own.
_SHORT = 500


def text(result) -> str:
    """The text a reply posts: `message` when present and non-empty, else `summary`."""
    if not isinstance(result, dict):
        return ""
    msg = result.get(FIELD)
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    return str(result.get("summary") or "").strip()


def digest(text) -> str:
    """The hash a write-up's stamp carries for the message it covers."""
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:16]


def guard(result) -> str:
    """Why the chat must not get this hand-off's text ("" when it may): the run was on the chat provider and
    the text carries no valid opus-writeup stamp (the stamp names the step and the hash of the message it covers)."""
    if not isinstance(result, dict) or not result.get(CHAT_RUN):
        return ""
    by = result.get(BY)
    msg = result.get(FIELD)
    if isinstance(by, dict) and by.get("step") == WRITEUP and isinstance(msg, str) and msg.strip() \
            and by.get("sha") == digest(msg):
        return ""
    return "its run was on the chat provider and no Opus write-up of its message was made"


def undelivered(result) -> str:
    """Why a done reply delivers nothing ("" when it delivers): no text, a short text that only
    describes the run, or text a chat-provider run wrote that Opus did not write up (guard)."""
    if why := guard(result):
        return why
    t = text(result)
    if not t:
        return "the hand-off has no message or summary"
    if len(t) > _SHORT or "`" in t or len([ln for ln in t.splitlines() if ln.strip()]) > 2:
        return ""
    if (m := _SELF.search(t)) and not _CONTENT.search(_SELF.sub(" ", t)):
        which = FIELD if isinstance(result.get(FIELD), str) and result[FIELD].strip() else "summary"
        return f"its {which} only describes the run ({m.group(0)!r}) and holds no deliverable"
    return ""


def prompt_block(task: dict) -> str:
    """The worker prompt's line for a task that answers a chat."""
    if not task.get("reply_chat"):
        return ""
    return ("## Reply to the chat\n"
            f"This task answers a chat. The daemon posts result.json `{FIELD}` there verbatim (else `summary`); "
            "the chat sees nothing else of your run, not your tool output or final reply. Put the deliverable "
            f"itself in `{FIELD}`: the full answer, steps or text asked for, ready to read. A `{FIELD}` that only "
            "describes your run (\"returned them as the message\") is not posted: the task goes to review. The chat "
            "is the user's own: no AI-authorship tag.")

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text: a line like
'drafts in my earlier messages, ready to send' points the reader back instead of repeating the
drafts, so the harness refuses it model-free.

`backrefs` finds the phrases that stand in for content: an explicit back-reference ('see above', 'in
my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item word
(draft/ask/request/command) that is the subject of 'ready to send', 'still open' or 'still pending'
in the same clause, not negated, or follows 'pending your'. A phrase is fine only when the message
itself carries the content: quoted material (a blockquote, a fenced block, an inline code span, a
quoted span of 30+ chars, or, for an ask, the '- ask <id>:' lines pendingask appends) in or after it
or in the message's later numbered parts (before it too, unless it points to earlier messages), or,
when the phrase's line ends in ':', non-empty lines after it. A passing line or list item with plain
lines after it is not its text; a phrase inside a short quoted span is only named. `refused` turns
the findings into a coordinator reply or notify rejection. Daemon-built lines were audited and carry
their pending item's full text: ask reminders quote the question (coordinator.defaults,
askrethink.text), `ttp status` shows each open ask whole (cli.status_text) and worker notifies carry
their body (daemon.relay_worker_notifies)."""
from __future__ import annotations

import re

from .pointers import _has_content

_EARLIER = r"\b(?:in|from|of|see)\s+(?:my|the|our)\s+(?:earlier|previous|prior|last)\s+messages?\b"
_POINT_BACK = re.compile(
    rf"{_EARLIER}|\bsee above\b|\bmentioned above\b"
    r"|\bdrafts?\s+above\b|\bas\s+drafted\b|\bthe\s+drafts?\s+(?:I|we)\s+(?:sent|posted|shared|wrote)\b",
    re.I)
_ITEM = r"asks?|requests?|commands?|drafts?|questions?"
_STATE = r"ready\s+to\s+send|still\s+open|still\s+pending|pending\s+your"
_SPAN = r"(?:(?!,\s*(?:and|but|so|while)\b)[^.;:\n]){0,120}"   # one clause: no . ; : , and/but or line break
_STATUS_PHRASE = re.compile(   # the item is the subject; only 'pending your <item>' names it after
    rf"\b(?:(?:{_ITEM})\b{_SPAN}\b(?:{_STATE})\b|pending\s+your\s+(?:{_ITEM})\b)", re.I)
_NEGATED = re.compile(r"\b(?:no|none|nothing|not|zero|neither|nor)\b|(?<![\w.])0(?![\w.])", re.I)
_OPENS = re.compile(r"(?:^|[\s(\[])['\"`‘“][^'\"`’”\n]{0,40}$")   # a quote mark opening a span, not an apostrophe
_CLOSES = re.compile(r"^[^'\"`’”\n]{0,40}['\"`’”](?!\w)")
_ASK_LINE = re.compile(r"^\s*- ask\s+\d+:", re.M)   # pendingask's expansion: the text of asks, nothing else
_QUOTED = re.compile(r"^\s*>|^```|`[^`\n]+`|\"[^\"]{30,}\"|“[^”]{30,}”", re.M)


def backrefs(text: str, later: list[str] | tuple = ()) -> list[str]:
    """The phrases standing in for a pending item's content, each once, in order; [] for a plain
    status message that names nothing pending (`later`: the text's later numbered parts)."""
    out: list[str] = []
    text = str(text or "")
    tail = "\n".join(t or "" for t in later)
    found = [(m, True) for m in _POINT_BACK.finditer(text)] + [(m, False) for m in _STATUS_PHRASE.finditer(text)]
    for m, back in sorted(found, key=lambda f: f[0].start()):
        cut = max((x.end() for x in re.finditer(r"[.;:,](?=\s|$)|\n", text[:m.start()])), default=0)   # not 'v1.0'
        clause = (text[cut:m.start()]   # an aside ', not sent yet,' never negates
                  + re.sub(r",[^,]*,", " ", m.group(0)))
        if (not back and _NEGATED.search(clause)) or (   # named as an example: 'see above' in quotes
                _OPENS.search(text[:m.start()]) and _CLOSES.search(text[m.end():])):
            continue
        eol = text.find("\n", m.end())
        line_end = text[m.end():eol if eol >= 0 else len(text)].rstrip()
        after = text[eol + 1 if eol >= 0 else len(text):] + "\n" + tail
        item = re.search(_ITEM, m.group(0), re.I)
        quoted = _QUOTED.search(text[m.start():] + "\n" + tail) or (
            not re.fullmatch(_EARLIER, m.group(0), re.I) and _QUOTED.search(text[:m.start()])) or (
            item and item.group(0).lower().startswith(("ask", "question")) and _ASK_LINE.search(text))
        if (quoted or (line_end.endswith(":") and _has_content(after))
                or re.match(r":\s*(?:none|nothing|no|0)\b", line_end, re.I)):
            continue
        p = " ".join(m.group(0).split()) if back else \
            f"the {item.group(0).lower()} named as pending, without its text"
        if p not in out:
            out.append(p)
    return out


def refused(text: str, what: str, later: list[str] | tuple = ()) -> str:
    """Why a coordinator `what` (reply, notify, urgent reply) is refused for pointing back at a
    pending item instead of quoting it, else ''."""
    gone = backrefs(text, later)
    if not gone:
        return ""
    return (f"{what} rejected: {', '.join(gone)}: quote the draft/ask/request/command in full "
            f"in the message itself; never point to earlier messages")

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text: a line like
'drafts in my earlier messages, ready to send' points the reader back instead of repeating the
drafts, so the harness refuses it model-free.

`backrefs` finds the phrases that stand in for content: an explicit back-reference ('see above',
'in my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item
word (draft/ask/request/command) said, in the same clause and not negated, to be 'ready to send',
'still open', 'still pending' or 'pending your ...'. A phrase is fine only when the message itself
carries the content: quoted material (a blockquote, a fenced block, a quoted span of 30+ chars, or
the open-ask texts pendingask appends as '- ask <id>:' lines) after it or in the message's later
numbered parts (before it too, unless it points to earlier messages), or, when the phrase's line
ends in ':', non-empty lines after it. A passing line or list item with plain lines after it is
not its text; a phrase inside a short quoted span is only named. `refused` turns the findings into a coordinator reply or notify
rejection. Daemon-built lines were audited and carry their pending item's full text: ask reminders
quote the question (coordinator.defaults, askrethink.text), `ttp status` shows each open ask whole
(cli.status_text) and worker notifies carry their body (daemon.relay_worker_notifies)."""
from __future__ import annotations

import re

from .pointers import _has_content

_EARLIER = r"\b(?:in|from|of)\s+(?:my|the|our)\s+(?:earlier|previous|prior|last)\s+messages?\b"
_POINT_BACK = re.compile(
    rf"{_EARLIER}|\bsee above\b|\bmentioned above\b"
    r"|\bdrafts?\s+above\b|\bas\s+drafted\b|\bthe\s+drafts?\s+(?:I|we)\s+(?:sent|posted|shared|wrote)\b",
    re.I)
_ITEM = r"asks?|requests?|commands?|drafts?|questions?|reminders?|instructions?"
_STATE = r"ready\s+to\s+send|still\s+open|still\s+pending|pending\s+your"
_STATUS_PHRASE = re.compile(   # item and state in one clause: no . ; : or line break between them
    rf"\b(?:(?:{_ITEM})\b[^.;:\n]{{0,120}}\b(?:{_STATE})\b|(?:{_STATE})\b[^.;:\n]{{0,120}}\b(?:{_ITEM})\b)", re.I)
_NEGATED = re.compile(r"\b(?:no|none|nothing|not|zero|neither|nor)\b", re.I)
_OPENS = re.compile(r"(?:^|[\s(\[])['\"`‘“][^'\"`’”\n]{0,40}$")   # a quote mark opening a span, not an apostrophe
_CLOSES = re.compile(r"^[^'\"`’”\n]{0,40}['\"`’”](?!\w)")
_QUOTED = re.compile(r"^\s*>|^\s*- ask\s+\d+:|^```|\"[^\"]{30,}\"|“[^”]{30,}”", re.M)


def backrefs(text: str, later: list[str] | tuple = ()) -> list[str]:
    """The phrases standing in for a pending item's content, each once, in order; [] for a plain
    status message that names nothing pending (`later`: the text's later numbered parts)."""
    out: list[str] = []
    text = str(text or "")
    tail = "\n".join(t or "" for t in later)
    found = [(m, True) for m in _POINT_BACK.finditer(text)] + [(m, False) for m in _STATUS_PHRASE.finditer(text)]
    for m, back in sorted(found, key=lambda f: f[0].start()):
        clause = text[max(text.rfind(c, 0, m.start()) for c in ".;:\n") + 1:m.end()]
        if (not back and _NEGATED.search(clause)) or (   # named as an example: 'see above' in quotes
                _OPENS.search(text[:m.start()]) and _CLOSES.search(text[m.end():])):
            continue
        eol = text.find("\n", m.end())
        line_end = text[m.end():eol if eol >= 0 else len(text)].rstrip()
        after = text[eol + 1 if eol >= 0 else len(text):] + "\n" + tail
        quoted = _QUOTED.search(text[m.end():] + "\n" + tail) or (
            not re.fullmatch(_EARLIER, m.group(0), re.I) and _QUOTED.search(text[:m.start()]))
        if quoted or (line_end.endswith(":") and _has_content(after)):
            continue
        p = " ".join(m.group(0).split()) if back else \
            f"the {re.search(_ITEM, m.group(0), re.I).group(0).lower()} named as pending, without its text"
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

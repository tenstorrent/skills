# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text: a line like
'drafts in my earlier messages, ready to send' points the reader back instead of repeating the
drafts, so the harness refuses it model-free.

`backrefs` finds the phrases that stand in for content: an explicit back-reference ('see above',
'in my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item
word (draft/ask/request/command) said to be 'ready to send', 'still open' or 'pending'. A phrase
is fine when the message itself carries the content: quoted material anywhere (a blockquote, a
fenced block, a quoted span, or the open-ask texts pendingask appends as '- ask <id>:' lines) or
non-empty lines after the phrase. `refused` turns the findings into a coordinator reply or notify
rejection. Daemon-built lines were audited and carry their pending item's full text: ask reminders
quote the question (coordinator.defaults, askrethink.text), `ttp status` shows each open ask whole
(cli.status_text) and worker notifies carry their body (daemon.relay_worker_notifies)."""
from __future__ import annotations

import re

from .pointers import _has_content

_POINT_BACK = re.compile(
    r"\b(?:in|from|of)\s+(?:my|the|our)\s+(?:earlier|previous|prior|last)\s+messages?\b"
    r"|\bsee above\b|\bmentioned above\b"
    r"|\bdrafts?\s+above\b|\bas\s+drafted\b|\bthe\s+drafts?\s+(?:I|we)\s+(?:sent|posted|shared|wrote)\b",
    re.I)
_ITEM = r"asks?|requests?|commands?|drafts?|questions?|reminders?|instructions?"
_STATUS_PHRASE = re.compile(
    rf"\b(?:(?:{_ITEM})\b[\s\S]{{0,120}}\b(?:ready\s+to\s+send|still\s+open|still\s+pending|"
    rf"is\s+pending|pending\s+your)\b"
    rf"|(?:ready\s+to\s+send|still\s+open|still\s+pending|is\s+pending|pending\s+your)\b"
    rf"[\s\S]{{0,120}}\b({_ITEM})\b)", re.I)
_QUOTED = re.compile(r"^\s*>|^\s*- ask\s+\d+:|^```|\"[^\"]{30,}\"|“[^”]{30,}”", re.M)


def backrefs(text: str) -> list[str]:
    """The phrases standing in for a pending item's content, each once, in order; [] for a plain
    status message that names nothing pending."""
    out: list[str] = []
    text = str(text or "")
    for m in _POINT_BACK.finditer(text):
        eol = text.find("\n", m.end())
        after = text[eol + 1 if eol >= 0 else len(text):]
        if not (_QUOTED.search(text) or _has_content(after)) and (p := " ".join(m.group(0).split())) not in out:
            out.append(p)
    for m in _STATUS_PHRASE.finditer(text):
        eol = text.find("\n", m.end())   # only lines after the phrase's own line count as its text
        if not (_QUOTED.search(text) or _has_content(text[eol + 1 if eol >= 0 else len(text):])):
            out.append(f"the {m.group(1) if m.lastindex else 'item'} named as pending, without its text")
            break
    return out


def refused(text: str, what: str) -> str:
    """Why a coordinator `what` (reply, notify, urgent reply) is refused for pointing back at a
    pending item instead of quoting it, else ''."""
    gone = backrefs(text)
    if not gone:
        return ""
    return (f"{what} rejected: {', '.join(gone)}: quote the draft/ask/request/command in full "
            f"in the message itself; never point to earlier messages")

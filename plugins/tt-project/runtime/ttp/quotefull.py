# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text (user orders of
2026-11-10 and 11-26): a line like 'drafts in my earlier messages, ready to send' points the reader
back instead of repeating the drafts, so the harness refuses it model-free.

`dangling` finds the phrases that stand in for content: an explicit back-reference ('see above',
'in my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item
word (draft/ask/request/command) said to be 'ready to send', 'still open' or 'pending'. A phrase is
fine when the message itself carries the content: quoted material anywhere (a blockquote, a fenced
block, a quoted span, or the open-ask texts pendingask appends as '- ask <id>:' lines) or
non-empty lines after the phrase. `refused` turns the findings into a coordinator reply or notify
rejection. Audit of daemon-built lines (2026-11-26): ask reminders quote the question
(coordinator.defaults, askrethink.text), `ttp status` shows each open ask whole (cli.status_text)
and worker notifies carry their body (daemon.relay_worker_notifies); none names a pending item
without its text."""
from __future__ import annotations

import re

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
_TAG = re.compile(r"^\W*authored\s+by\s+ai\W*$", re.I)
_QUOTED = re.compile(r'^\s*>|^\s*- ask\s+\d+:|^```|"[^"]{30,}"|“[^”]{30,}”', re.M)


def _content(text: str) -> bool:
    return any(re.search(r"\w", ln) and not _TAG.match(ln) for ln in text.splitlines())


def _answered(text: str, at: int) -> bool:
    """Whether the message itself carries the content: quoted material anywhere, or non-empty
    lines after the phrase's own line (the draft texts pasted under a passing line, or the ask
    text pendingask appended)."""
    eol = text.find("\n", at)
    return bool(_QUOTED.search(text)) or _content(text[eol + 1 if eol >= 0 else len(text):])


def dangling(text: str) -> list[str]:
    """The phrases standing in for a pending item's content, each once, in order; [] for a plain
    status message that names nothing pending."""
    out: list[str] = []
    text = str(text or "")
    for m in _POINT_BACK.finditer(text):
        if not _answered(text, m.end()) and (phrase := " ".join(m.group(0).split())) not in out:
            out.append(phrase)
    m = _STATUS_PHRASE.search(text)
    if m and not _answered(text, m.start()):
        out.append(f"the {m.group(1) if m.lastindex else 'item'} named as pending, without its text")
    return out


def refused(text: str, what: str) -> str:
    """Why a coordinator `what` (reply, notify, urgent reply) is refused for pointing back at a
    pending item instead of quoting it, else ''."""
    gone = dangling(text)
    if not gone:
        return ""
    return (f"{what} rejected: {', '.join(gone)}: quote the draft/ask/request/command in full "
            f"in the message itself; never point to earlier messages")

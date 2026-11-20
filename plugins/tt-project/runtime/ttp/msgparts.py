# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Chat replies and notifies that fit the channel: never cut silently.

A chat relay shows at most LIMIT characters of one message. A longer text is posted whole as ordered
parts, each under LIMIT and marked `(1/N) `, `(2/N) ` and so on. Parts end at a paragraph break,
else at a line break, else at a sentence end; a hard cut only splits one paragraph that is too long by
itself. Joining the parts without their markers gives back the text exactly: the history holds it once.

A text over MAX_CHARS is no message but a document: the coordinator's `reply` or `notify` is rejected
(`too_long`) naming the limit and its length, so its next turn shortens it or points to a file. A
worker's hand-off posted to its chat has nobody to reject it to: it is cut at a boundary near MAX_CHARS
with a line saying how much more there is and where (`cut`).
"""
from __future__ import annotations

import re

LIMIT = 6000              # chars a chat relay shows of one message
MAX_PARTS = 4             # parts a reply or notify may take before it is a document, not a message
MAX_CHARS = LIMIT * MAX_PARTS
_PART = re.compile(r"\((\d+)/\d+\) ")
_SENTENCE = re.compile(r"[.!?][\"')\]]*\s+")


def _marker(i: int, n: int) -> str:
    return f"({i}/{n}) "


def _cut(text: str, room: int) -> int:
    """Where the first part of `text` ends: the last paragraph break within `room`, else line break,
    else sentence end (each only past half of it, so no part is needlessly short), else `room`."""
    if len(text) <= room:
        return len(text)
    head = text[:room]
    for sep in ("\n\n", "\n"):
        at = head.rfind(sep)
        if at >= room // 2:
            return at + len(sep)
    ends = [m.end() for m in _SENTENCE.finditer(head) if m.end() >= room // 2]
    return ends[-1] if ends else room


def chunks(text: str, room: int) -> list[str]:
    """`text` in pieces of at most `room` chars, cut as `_cut` says; they join back to `text`."""
    out: list[str] = []
    while text:
        at = _cut(text, room)
        out.append(text[:at])
        text = text[at:]
    return out


def split(text: str, limit: int = LIMIT) -> list[str]:
    """The messages that deliver `text`: itself when it fits, else marked parts, each within `limit`."""
    if len(text) <= limit:
        return [text]
    n = 2
    while True:   # the marker's width depends on the count of parts it numbers
        pieces = chunks(text, limit - len(_marker(n, n)))
        if len(_marker(len(pieces), len(pieces))) <= len(_marker(n, n)):
            break
        n = len(pieces)
    return [_marker(i, len(pieces)) + s for i, s in enumerate(pieces, 1)]


def continued(text: str) -> bool:
    """Whether `text` is a later part (2/N on) of a message split says."""
    return bool((m := _PART.match(text or "")) and m.group(1) != "1")


def too_long(text: str, what: str) -> str:
    """Why a coordinator `what` (reply, notify) is refused for its length, else ""."""
    if len(text) <= MAX_CHARS:
        return ""
    return (f"{what} rejected: {len(text)} chars is over the {MAX_CHARS}-char cap ({MAX_PARTS} chat messages "
            f"of {LIMIT}); shorten it, or put the long part in a file or task and say where it is")


def cut(text: str, where: str) -> str:
    """`text` cut near MAX_CHARS at a boundary, saying how much more there is and `where` it is."""
    if len(text) <= MAX_CHARS:
        return text
    def note(n: int) -> str:
        return f"\n\n[{n} more chars not posted here; the full text is in {where}]"
    head = text[:_cut(text, MAX_CHARS - len(note(len(text))))]
    return head + note(len(text) - len(head))


def post(db, text: str, **kw) -> list[int]:
    """Post `text` as split says, all parts or none; the message ids in order."""
    with db.tx():
        return [db.post("out", part, **kw) for part in split(text)]

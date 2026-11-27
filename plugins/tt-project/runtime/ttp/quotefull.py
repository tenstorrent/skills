# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text: a line like
'drafts in my earlier messages, ready to send' points the reader back instead of repeating the
drafts, so the harness refuses it model-free.

`backrefs` finds the phrases that stand in for content: an explicit back-reference ('see above', 'in
my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item word
(draft/ask/request/command) that awaits the user ('ready to send', 'pending your', 'for you to',
'please run' and the like after it; 'still open' or 'still pending' after an ask) in the same clause, not negated.
A pull request, a PR or a draft PR is no such item. A phrase is fine only when the text is attached
to it: quoted material (a blockquote, a fenced block, an inline code span, a quoted span of 30+
chars) later in its own sentence or as the first lines after it (or in the message's later numbered
parts), the '- ask <id>:' line pendingask appends for that ask, or, for a status phrase, text after a
colon on its line; when the line ends in ':', non-empty lines after it. Quoted text elsewhere in
the message never counts. A phrase inside a short quoted span is only named. `refused` turns
the findings into a coordinator reply or notify rejection. Daemon-built lines were audited and carry
their pending item's full text: ask reminders quote the question (coordinator.defaults,
askrethink.text), `ttp status` shows each open ask whole (cli.status_text) and worker notifies carry
their body (daemon.relay_worker_notifies)."""
from __future__ import annotations

import re

from .pointers import has_content

_EARLIER = r"\b(?:in|from|of|see)\s+(?:my|the|our)\s+(?:earlier|previous|prior|last)\s+messages?\b"
_POINT_BACK = re.compile(
    rf"{_EARLIER}|\bsee above\b|\bdrafts?\s+above\b|\bas\s+drafted\s+(?:earlier|before|above)\b"
    r"|\bthe\s+drafts?\s+(?:I|we)\s+(?:sent|posted|shared|wrote)\b",
    re.I)
_SPAN = r"(?:(?!,\s*(?:and|but|so|while)\b)[^.;:\n]){0,120}"   # one clause: no . ; : , and/but or line break
_ITEM = r"(?<!pull\s)(?<!PR\s)(?<!\()(?:asks?|requests?|commands?|drafts?|questions?)\b(?!\s+PRs?\b|\s+pull\s+request|\s*\))"
_AWAITS = r"ready\s+to\s+send|pending\s+your|(?:for\s+you\s+to|please)\s+(?:run|approve|review|reply|answer|confirm|decide)\b"
_STATUS_PHRASE = re.compile(   # an item that awaits the user; 'pending your <item>' names it after
    rf"\b(?:{_ITEM}{_SPAN}\b(?:{_AWAITS})|(?<!pull\s)asks?\b{_SPAN}\bstill\s+(?:open|pending)\b"
    rf"|pending\s+your\s+{_ITEM})", re.I)
_NEGATED = re.compile(r"\b(?:no|none|nothing|not|zero|neither|nor)\b|(?<![\w.])0(?![\w.])", re.I)
_OPENS = re.compile(r"(?:^|[\s(\[])['\"`‘“][^'\"`’”\n]{0,40}$")   # a quote mark opening a span, not an apostrophe
_CLOSES = re.compile(r"^[^'\"`’”\n]{0,40}['\"`’”](?!\w)")
_BLOCK = r"^\s*>|^```|\"[^\"]{30,}\"|“[^”]{30,}”"   # quoted text a back-reference accepts
_BLOCKS = re.compile(_BLOCK, re.M)
_QUOTED = re.compile(_BLOCK + r"|`[^`\n]+`", re.M)
_STOP = re.compile(r"[.;!?](?=\s|$)|\n")


def _attached(text: str, start: int, after: str, ids: list[str], back: bool) -> bool:
    """Whether quoted text belongs to the phrase at `start`: in its own sentence, as the first lines
    after it, or (an ask) its '- ask <id>:' line. A back-reference takes no inline code span: a
    command is not the earlier text."""
    quoted = _BLOCKS if back else _QUOTED
    q = quoted.search(text, start)
    if q and not _STOP.search(text[start:q.start()]):
        return True
    first = next((ln for ln in after.splitlines() if ln.strip() and not re.fullmatch(r"\s*\(\d+/\d+\)\s*", ln)), "")
    return bool(quoted.search(first)) or bool(
        ids and all(re.search(rf"^\s*- ask\s+{i}:", text, re.M) for i in ids))


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
        ids = re.findall(r"\b\d+\b", m.group(0)) if item and item.group(0).lower().startswith("ask") else []
        if (_attached(text, m.start(), after, ids, back) or (line_end.endswith(":") and has_content(after))
                or (not back and re.match(r":\s*\S", line_end))):
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

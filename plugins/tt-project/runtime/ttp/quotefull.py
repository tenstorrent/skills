# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A mention of a pending ask, request, draft or command carries its full text: a line like
'drafts in my earlier messages, ready to send' points the reader back instead of repeating the
drafts, so the harness refuses it model-free.

`backrefs` finds the phrases that stand in for content: an explicit back-reference ('see above', 'in
my earlier messages', 'drafts above', 'as drafted earlier', 'the draft I sent'), and an item word
(draft/ask/request/command) that awaits the user ('ready to send', 'pending your', 'for you to',
a request to the user to act, after it; 'still open' or 'still pending' after an ask) in the same clause, not negated.
A pull request, a PR or a draft PR is no such item. A phrase is fine only when the text is attached
to it: quoted material (a blockquote, a fenced block, an inline code span, a quoted span of 30+
chars) later in its own sentence or as the first line after it (or in the message's later numbered
parts; a code span there only as the whole line or after a closing ':'), or, for a status phrase,
text after a colon on its line, or non-empty lines after a closing ':'. Quoted text elsewhere in
the message never counts. An ask named by id is pendingask's: it runs first and appends the ask in
full unless the text already quotes it. 'In my earlier message' counts only with a pending item in
its sentence. A phrase inside a short quoted span is only named. `refused` turns
the findings into a coordinator reply or notify rejection. Daemon-built lines were audited and carry
their pending item's full text: ask reminders quote the question (coordinator.defaults,
askrethink.text), `ttp status` shows each open ask whole (cli.status_text) and worker notifies carry
their body (daemon.relay_worker_notifies)."""
from __future__ import annotations

import re

from . import pendingask
from .pointers import has_content, part_of

_EARLIER = r"\b(?:in|from|of|see)\s+(?:my|the|our)\s+(?:earlier|previous|prior|last)\s+messages?\b"
_POINT_BACK = re.compile(
    rf"{_EARLIER}|\bsee above\b(?!\s+in\s+the\b)|\bdrafts?\s+above\b|\bas\s+drafted\s+(?:earlier|before|above)\b"
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
_BARE = re.compile(_EARLIER.replace("|see", ""), re.I)   # points back only with a pending item in its sentence
_PENDING = re.compile(rf"\b{_ITEM}|{_AWAITS}", re.I)


def _attached(text: str, start: int, after: str, back: bool) -> bool:
    """Whether quoted text belongs to the phrase at `start`: in its own sentence, or as the first line
    after it (a block or long quote; a code span only as the whole line). A
    back-reference takes no inline code span: a command is not the earlier text."""
    q = (_BLOCKS if back else _QUOTED).search(text, start)
    if q and not _STOP.search(text[start:q.start()]):
        return True
    first = next((ln for ln in after.splitlines() if ln.strip() and not part_of(ln)), "")
    return bool(_BLOCKS.search(first) or (not back and re.fullmatch(r"\s*`[^`\n]+`\s*", first)))


def backrefs(text: str, later: list[str] | tuple = (), open_asks: list[dict] | tuple = ()) -> list[str]:
    """The phrases standing in for a pending item's content, each once, in order; [] for a plain
    status message that names nothing pending (`later`: the text's later numbered parts; `open_asks`:
    those pendingask.expand, run first, could match by their words)."""
    out: list[str] = []
    text = str(text or "")
    tail = "\n".join(t or "" for t in later)
    head, added = pendingask.appended(text)
    found = [(m, True) for m in _POINT_BACK.finditer(text)] + [(m, False) for m in _STATUS_PHRASE.finditer(text)]
    for m, back in sorted(found, key=lambda f: f[0].start()):
        if m.start() >= head:   # an appended ask's own words are its text, not a pointer
            continue
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
        lo = max((x.end() for x in _STOP.finditer(text[:m.start()])), default=0)
        hi = _STOP.search(text, m.end())
        if not back and (pendingask.refs(m.group(0)) or (   # pendingask.expand, run first, appended it or saw it quoted
                item and item.group(0).lower() in ("ask", "asks")   # ... or matched it by its words and appended it
                and (hit := pendingask.match(text[lo:hi.start() if hi else head], list(open_asks)))
                and {int(a["id"]) for a in hit} <= added)):
            continue
        if back and _BARE.fullmatch(m.group(0)):   # 'in my earlier message I said ...' with nothing pending
            if not _PENDING.search(text[lo:hi.start() if hi else len(text)]) and not line_end.endswith(":"):
                continue
        if (_attached(text, m.start(), after, back) or (not back and line_end.endswith(":") and has_content(after))
                or (not back and re.match(r":\s*\S", line_end))):
            continue
        p = " ".join(m.group(0).split()) if back else \
            f"the {item.group(0).lower()} named as pending, without its text"
        if p not in out:
            out.append(p)
    return out


def refused(text: str, what: str, later: list[str] | tuple = (), open_asks: list[dict] | tuple = ()) -> str:
    """Why a coordinator `what` (reply, notify, urgent reply) is refused for pointing back at a
    pending item instead of quoting it, else ''."""
    gone = backrefs(text, later, open_asks)
    if not gone:
        return ""
    return (f"{what} rejected: {', '.join(gone)}: quote the draft/ask/request/command in full "
            f"in the message itself; never point to earlier messages")

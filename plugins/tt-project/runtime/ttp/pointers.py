# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replies that point to what is not there: a coordinator `reply` or `notify` that sends the reader
to content further on ("follow-up below", "see below", "details below", "as follows:") and has none
is refused, naming the phrase, so its next turn sends the whole text.

Model-free; coordinator.apply runs it on every reply and notify, the per-wake turn's and the
persistent session's alike, whatever the provider. A pointer is (POINTER):
- "below" after "see", after a word saying something was put there ("listed below", "shown below")
  or after a word naming a section ("details below", "follow-up below", "steps below"), unless a
  number, an article or a comparison word follows it ("below the cap", "below 30 GB");
- any other "below" at a clause end ("it is below.", "more on that below:") or a line end;
- "as follows", and "the following" with at most three words then a colon.
A bare colon ending the text is no pointer by itself ("ack: Brief:" quotes a line).
A "below" pointer is answered by a later non-empty line (a paragraph, a list item, a heading) or by
text right after its colon on the same line ("follow-up below: #12 lands today"); "as follows" and
"the following ...:" by any text after them. A line that is only the AI-authorship tag is no content.
A reply sent as numbered parts in one batch ("(1/3) ...", "Part 2 of 3: ...") is checked as a whole:
a later part's lines answer an earlier part's pointer (`later_parts`)."""
from __future__ import annotations

import re

_SECTION = (r"details?|follow[- ]?ups?|steps?|list|section|notes?|table|summary|plan|options?|items?|more|breakdown|"
            r"reasons?|answers?|questions?|changes?|findings?|links?|numbers|figures|results?|commands?|"
            r"instructions|context|status")
_PUT = (r"see|listed|shown|given|described|detailed|explained|outlined|summari[sz]ed|included|noted|linked|"
        r"quoted|written|attached|set\s+out|spelled\s+out|laid\s+out|further|right|just")
# Not a pointer when one of these follows "below": a comparison ("below the cap", "below 30 GB").
_COMPARE = (r"\s+(?:the|a|an|its|their|our|your|his|her|this|that|these|those|zero|half|par|average|normal|"
            r"target|budget|limit|threshold|cap|baseline|expected|[\d$~<>=%+-])")
POINTER = re.compile(
    rf"\b(?:{_PUT}|{_SECTION})\s+below\b(?!{_COMPARE})"
    r"|\bbelow(?=\s*(?:[:.;!?)\]]|$))"
    r"|\bas\s+follows\b"
    r"|\bthe\s+following(?:\s+[\w-]+){0,3}\s*:",
    re.I | re.M)
_TAG = re.compile(r"^\W*authored\s+by\s+ai\W*$", re.I)
_PART = re.compile(r"^\s*(?:[(\[]\s*(?:part\s+)?(\d+)\s*(?:/|of)\s*(\d+)\s*[)\]]"
                   r"|part\s+(\d+)\s*(?:/|of)\s*(\d+)\b|(\d+)/(\d+)(?=[\s:.)]))", re.I)


def part_of(text: str) -> tuple[int, int] | None:
    """(i, n) when `text` opens with a part marker ("(1/3)", "[2/3]", "Part 2 of 3", "2/3:"), else None."""
    m = _PART.match(text or "")
    if not m:
        return None
    i, n = (int(g) for g in next(pair for pair in (m.groups()[k:k + 2] for k in (0, 2, 4)) if pair[0]))
    return (i, n) if 0 < i <= n else None


def later_parts(text: str, nexts: list[str]) -> list[str]:
    """The texts in `nexts` (the same kind and chat, in send order) that continue `text` as later
    numbered parts of one message; [] when `text` is no part or is the last one."""
    me = part_of(text)
    if not me or me[0] >= me[1]:
        return []
    out, at = [], me[0]
    for t in nexts:
        nx = part_of(t)
        if nx and nx[1] == me[1] and nx[0] > at:
            out.append(t)
            at = nx[0]
    return out


def _has_content(text: str) -> bool:
    return any(re.search(r"\w", ln) and not _TAG.match(ln) for ln in text.splitlines())


def dangling(text: str, later: list[str] | tuple = ()) -> list[str]:
    """The pointers in `text` with nothing after them, in order, each once (`later`: the text's later
    parts, whose lines count as after it)."""
    text = text or ""
    tail = "\n".join(t or "" for t in later)
    out: list[str] = []
    for m in POINTER.finditer(text):
        eol = text.find("\n", m.end())
        line_rest = text[m.end():] if eol < 0 else text[m.end():eol]
        after = (text[eol + 1:] if eol >= 0 else "") + "\n" + tail
        if "below" in m.group(0).lower():
            ok = bool(re.match(r"\s*\)?\s*:\s*\S", line_rest)) or _has_content(after)
        else:
            ok = _has_content(line_rest) or _has_content(after)
        if not ok and (phrase := " ".join(m.group(0).split())) not in out:
            out.append(phrase)
    return out


def refused(text: str, what: str, later: list[str] | tuple = ()) -> str:
    """Why a coordinator `what` (reply, notify) is refused for pointing to content it lacks, else ""."""
    gone = dangling(text, later)
    if not gone:
        return ""
    said = ", ".join(f"'{g}'" for g in gone)
    return (f"{what} rejected: it points to content that is not in it: {said} with nothing after it (no later "
            f"paragraph, list or heading); add that part or drop the pointer, and send the whole {what} again")

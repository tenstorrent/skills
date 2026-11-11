"""A reminder of a pending user action quotes it in full (user msg #2424): the user should never have to
scroll back for what 'ask 297' or 'the command above' meant.

`full` renders an open ask as the user reads it: its id and its stored text verbatim (the question, the
least-disruptive or reversible route, the recommendation and the timer note the ask was sent with).
`expand` is the model-free check on an outgoing reply or notify: an open ask the text names only by its
id gets its full text appended, and a 'still waiting on you' that names no open ask it can match is
flagged for the next turn. It never rejects or drops the text."""
from __future__ import annotations

import re

from . import reask

HEADER = "\n\nStill open, in full:"
# 'ask 297', 'asks 12 and 14', 'ask #297': an ask id, never a task (#297 alone reads as a task).
_REF = re.compile(r"\basks?\s+#?(\d+)((?:\s*(?:,|and|&|/)\s*#?\d+)*)", re.I)
_MORE = re.compile(r"\d+")
# Words that say the user still owes something, with no ask id next to them.
_WAITING = re.compile(
    r"\b(?:still\s+)?(?:waiting|waits?|blocked|held)\s+(?:on|for)\s+(?:you|your)\b"
    r"|\bneeds?\s+(?:you|your\s+(?:yes|answer|approval|go-ahead|word|reply|decision|ok))\b"
    r"|\b(?:pending|awaiting)\s+your\b|\bover to you\b|\byour\s+(?:turn|move|call)\b", re.I)
_QUOTED_CHARS = 120   # a reply already carrying this much of the ask's text quotes it


def _flat(s: str) -> str:
    return " ".join((s or "").split())


def full(ask: dict) -> str:
    """'ask <id>: <its text>', whitespace collapsed to one line, never clipped."""
    return f"ask {ask['id']}: {_flat(ask.get('text') or '')}"


def refs(text: str) -> list[int]:
    """The ask ids `text` names, in order, once each."""
    out: list[int] = []
    for m in _REF.finditer(text or ""):
        for n in [m.group(1)] + _MORE.findall(m.group(2) or ""):
            if int(n) not in out:
                out.append(int(n))
    return out


def waiting(text: str) -> bool:
    return bool(_WAITING.search(text or ""))


def quoted(text: str, ask: dict) -> bool:
    """Whether `text` already carries the ask's opening words (its question)."""
    head = _flat(ask.get("text") or "").lower()[:_QUOTED_CHARS]
    return bool(head) and head in _flat(text).lower()


def _match(text: str, asks: list[dict]) -> list[dict]:
    """The open asks a reference-free 'waiting on you' most likely means: the only one open, else those
    sharing at least two content words with the text."""
    if len(asks) == 1:
        return asks
    said = reask.words(text)
    return [a for a in asks if len(said & reask.words(a.get("text") or "")) >= 2]


def expand(text: str, open_asks: list[dict], what: str = "reply") -> tuple[str, list[str]]:
    """(`text` with each open ask it names but does not quote appended in full, notes for the next turn).
    A 'waiting on you' naming no ask gets the open ask(s) it matches; with none to match, or naming an
    ask that is not open, the text goes unchanged and a note says so."""
    by_id = {int(a["id"]): a for a in open_asks}
    ids = refs(text)
    hit = [by_id[i] for i in ids if i in by_id]
    gone = [i for i in ids if i not in by_id]
    notes: list[str] = []
    if not hit and waiting(text):
        hit = _match(text, open_asks)
        if not hit:
            notes.append(f"pendingask: your {what} says the user still owes something but names no open ask it "
                         "could match" + (f" (ask {', '.join(map(str, gone))} is not open)" if gone else "")
                         + "; it went as written. Quote the pending question or command in full in the text")
    add = [a for a in hit if not quoted(text, a)]
    if add:
        text = text.rstrip() + HEADER + "".join(f"\n- {full(a)}" for a in add)
        notes.append(f"pendingask: your {what} named ask {', '.join(str(a['id']) for a in add)} without its text; "
                     "the full text was appended. Quote a pending question or command in full yourself")
    return text, notes

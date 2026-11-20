"""Provenance spans: untrusted text the harness shows a model, enclosed as data (spotlighting).

Each span gets a label naming where its text came from and a delimiter carrying a tag that is new for
every turn or prompt (`new_tag`), so text written before that turn cannot know the tag and cannot close
its span early or open a fake one: any marker character inside the text is shown as a plain bracket
first. The prompts say once that enclosed text is data. The user's verbatim words are never enclosed.
"""
from __future__ import annotations

import re
import secrets

OPEN, CLOSE = "⟦", "⟧"   # ⟦ ⟧: the delimiter's brackets, never left inside a span's text
_PLAIN = str.maketrans({OPEN: "[", CLOSE: "]"})
_LABEL = re.compile(r"[^\w .,:#/'()+-]")


def new_tag() -> str:
    """A fresh delimiter tag: 8 hex characters from the OS's random source."""
    return secrets.token_hex(4)


def clean(text) -> str:
    """`text` with every delimiter bracket shown as a plain one, so it can neither close nor open a span."""
    return str(text or "").translate(_PLAIN)


def wrap(text, source: str, tag: str) -> str:
    """`text` enclosed as data from `source` under this turn's `tag`. Clip `text` before wrapping, never
    after: a cut that drops the closing marker leaves the span open."""
    label = _LABEL.sub("", clean(source).replace("\n", " "))[:80].strip() or "unknown"
    return f"{OPEN}{tag} data: {label}{CLOSE} {clean(text)} {OPEN}/{tag}{CLOSE}"


def strip(text: str) -> str:
    """`text` with every span's delimiters and label taken out, its contents left in place."""
    o, c = re.escape(OPEN), re.escape(CLOSE)
    return re.sub(rf"{o}[0-9a-f]{{8}} data: [^{c}]*{c} | {o}/[0-9a-f]{{8}}{c}", "", text)


def unwrap(text: str, tag: str) -> list[tuple[str, str]]:
    """The (label, text) of each span in `text` under `tag`, in order (for tests and checks)."""
    o, c = re.escape(OPEN), re.escape(CLOSE)
    pat = re.compile(rf"{o}{re.escape(tag)} data: ([^{c}]*){c} (.*?) {o}/{re.escape(tag)}{c}", re.S)
    return pat.findall(text)

"""One place that joins a clipped title to its body for alert and feed lines."""
from __future__ import annotations

import re


def clip(text: str, cap: int) -> str:
    """`text` with whitespace normalised, cut at a word boundary with an ellipsis when over `cap`."""
    s = " ".join(str(text or "").split())
    if len(s) <= cap:
        return s
    cut = s[:max(cap - 1, 1)]
    if not s[len(cut)].isspace():
        head = cut.rsplit(" ", 1)[0]
        cut = head if head else cut
    # never end inside an open parenthesis or on a dangling separator
    if cut.count("(") > cut.count(")"):
        cut = cut[:cut.rindex("(")]
    return cut.rstrip(" ,;:-–(") + "…"


_STOP = frozenset("a an the to of in on at for and or is was were be by with".split())


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def restates(title: str, body: str, clipped: bool = True) -> bool:
    """Whether the body already carries the title's words (so the title adds nothing).

    `clipped` says the title was cut mid-word, which allows its last word to be a prefix."""
    t, b = _words(title), _words(body)
    if not t or len(b) < len(t) * 0.5:
        return False
    if b[:len(t)] == t:
        return True
    k = len(t)
    if clipped and k <= len(b) and b[:k - 1] == t[:-1] and b[k - 1].startswith(t[-1]):
        return True
    # a short leading label ("triage:") may be skipped; most content words appear early in the body
    if title.split()[0].endswith(":") and k > 3:
        t = t[1:]
        if b[:len(t)] == t:
            return True
    t = [w for w in t if w not in _STOP]
    if len(t) < 3:
        return False
    early = set(b[:2 * len(t)])
    return sum(w in early for w in t) >= 0.6 * len(t)


def join(title: str, body: str, cap: int = 80, prefix: str = "", sep: str = ": ") -> str:
    """`prefix` + title + sep + body; the body alone (after the prefix) when it restates the title,
    and a title over `cap` characters clipped at a word boundary with an ellipsis."""
    t = " ".join(str(title or "").split())
    b = " ".join(str(body or "").split())
    if not t or restates(t[:cap], b, len(t) > cap and not t[cap].isspace()) or restates(t, b, False) or restates(clip(t, cap), b, False):
        return f"{prefix}{b}"
    return f"{prefix}{clip(t, cap)}{sep}{b}"

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A refused Restrictions charter_update that cites a user message: a filled resend, and the chat told.

A coordinator often puts the cite inside `text` ("... (user msg #12)") and leaves `source` unset, so the
gate reads the item as unsourced and refuses it with a long error. When a charter_update to Restrictions
is refused and its `source` names a message, or its `text` cites one (restriction_source.CITE_RE),
template() reads that message from the chat log and fills the same change ready to resend: the same
section, quote and replaces, `source` {"msg": N, "quote": "<its exact words>"} and `text` without cites
(the gate adds the cite from `source`). The refusal and the charter_retry event put that template first
and the reason after it in one line (coordinator.apply, _charter_retry_now).

The template is a suggestion only: the gate checks the resend as it checks any (restriction_source.check
reads the quote against the log). Nothing is made up: with no readable cited message (unknown, not the
user's own, no verbatim words) there is no template, just the one-line reason.

alert() tells the chat the cited message came from (the relayer's chat) that the item was refused: the item,
the reason in one line, and that the coordinator was handed the template. Once per item and refusal cause
(kv ALERTS_KEY); landed() clears it with one low line to that chat once the item lands.

Only a source refusal is templated (SOURCE_REFUSAL: no `source`, or one the gate could not check): a filled
`source` fixes nothing else, so any other refusal (an ambiguous `replaces`, a missing approval, a contradiction,
a held class, a queued change) keeps its full error.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from . import relay, restriction_source

ALERTS_KEY = "charter_refusal_alerts"   # kv: {item key: {"chat", "msg", "causes", "ts", "item"}}
ALERTS_KEEP = 50
QUOTE_MAX = 400   # a longer message is quoted by its sentence that best matches the item
_DATED = re.compile(r"\s*\(added [^)]*\)$", re.I)
_NUM = re.compile(r"#(\d+)")
_SENTENCE = re.compile(r"(?<=[.;!?])\s+|\n+")
SOURCE_REFUSAL = re.compile(r"^charter_update rejected: (?:a Restrictions item needs `source`|`source`|`text` cites|"
                            r"(?:quote|message|origin) check failed)")
_CAUSE_NOISE = re.compile(r"\d+|\"[^\"]*\"|'[^'\s][^']*'|`[^`]*`")


def _ws(s: str) -> str:
    return " ".join((s or "").split())


def restrictions(section: str) -> bool:
    return _DATED.sub("", " ".join(str(section or "").lstrip("#").split())).lower().startswith("restriction")


def cited_msg(a: dict) -> int | None:
    """The user message a charter_update names: its `source.msg`, else the first cite in its `text`."""
    try:
        got = restriction_source._parse(a.get("source"))
        return int(str(got.get("msg") or "").lstrip("#"))
    except (ValueError, TypeError):
        pass
    m = restriction_source.CITE_RE.search(str(a.get("text") or ""))
    return int(_NUM.search(m.group()).group(1)) if m else None


def strip_cites(text: str) -> str:
    """`text` without its source cites; a line that held only a cite is dropped."""
    out = []
    for line in str(text or "").split("\n"):
        kept = restriction_source.bare(line).rstrip()
        if kept.strip(" -*") or not line.strip():
            out.append(kept)
    return "\n".join(out).strip()


def _own(db, mid: int) -> tuple[str, str | None] | None:
    """(the verbatim words, the chat) of user message `mid`, else None: not inbound, not the user's (a
    finding or the harness's own), or no verbatim words."""
    row = db.one("SELECT text, context, chat, kind FROM messages WHERE id=? AND direction='in' "
                 "AND COALESCE(provenance,'')!='system'", (mid,))
    if row is None or row["kind"] != "user":
        return None
    own = relay.split(row["text"] or "", row["context"])[0]
    return (own, row["chat"]) if own.strip() else None


def _quote(own: str, sent: str, text: str) -> str:
    """The words of `own` to quote: the quote sent when it is in them, else all of them, or for a long
    message its sentence sharing the most words with `text`."""
    loose = restriction_source._loose
    if len(sent.strip()) >= 3 and loose(sent) in loose(own):
        return sent.strip()
    if len(own) <= QUOTE_MAX:
        return own.strip()
    want = set(relay._words(text)) - relay._COMMON
    parts = [s.strip() for s in _SENTENCE.split(own) if len(s.strip()) >= 3]
    return max(parts, key=lambda s: len(want & set(relay._words(s))), default=own.strip())


def template(db, a: dict, err: str) -> tuple[dict, int, str | None] | None:
    """(the filled charter_update, the cited message id, its chat) for a Restrictions change refused for its
    source (`err`, SOURCE_REFUSAL) that names a readable user message (module doc), else None."""
    if not (restrictions(a.get("section") or "") and SOURCE_REFUSAL.match(str(err or ""))) or (
            restriction_source.TURN_SOURCE[:60] in str(err)):   # this turn's words: no older message fills it
        return None
    mid = cited_msg(a)
    got = _own(db, mid) if mid is not None else None
    if got is None:
        return None
    own, chat = got
    try:
        sent = str(restriction_source._parse(a.get("source")).get("quote") or "")
    except ValueError:
        sent = ""
    text = strip_cites(a.get("text") or "")
    filled = {k: v for k, v in a.items() if k not in ("text", "source")}
    filled.update(text=text, source=json.dumps({"msg": mid, "quote": _quote(own, sent, text)}, ensure_ascii=False))
    return filled, mid, chat


def reason(err: str) -> str:
    """The refusal in one line: its first clause, without the 'charter_update rejected:' lead."""
    s = re.sub(r"^(?:charter_update(?: rejected)?:\s*)+", "", _ws(err), flags=re.I)
    quoted = [m.span() for m in re.finditer(r'"[^"]*"|`[^`]*`', s)]   # a quoted item's own '.' ends nothing
    cut = next((m.start() for m in re.finditer(r"[.;:](?=\s)", s)
                if m.start() >= 25 and not any(a < m.start() < b for a, b in quoted)), len(s))
    s = s[:cut]
    if s.count("(") > s.count(")"):
        s = s[:s.rfind("(")].rstrip(" ,")
    return s if len(s) <= 200 else s[:199].rstrip() + "…"


def item_key(a: dict) -> str:
    """One Restrictions change, whatever its cites: the same change resent from its template keeps it."""
    parts = ("restrictions", strip_cites(a.get("text") or ""), a.get("quote") or "", a.get("replaces") or "")
    return hashlib.sha256("\n".join(restriction_source._loose(str(x)) for x in parts).encode()).hexdigest()[:16]


def refusal(filled: dict, mid: int, why: str) -> str:
    """The refusal the coordinator reads: the template first, the reason after it in one line."""
    return (f"refused; resend this, filled from the user's message #{mid} (the gate checks its quote again): "
            f"{json.dumps(filled, ensure_ascii=False)} -- why: {why}")


def alert(db, a: dict, mid: int, chat: str | None, why: str) -> int | None:
    """Tell `chat` (the cited message's) that the item was refused, once per item and cause (module doc)."""
    if not chat:
        return None
    key = item_key(a)
    cause = _ws(_CAUSE_NOISE.sub(" ", why.lower()))[:120]
    item = _ws(strip_cites(a.get("text") or "")) or f"removing {_ws(a.get('quote') or '')!r}"
    item = item if len(item) <= 160 else item[:159].rstrip() + "…"
    with db.tx():
        have = db.kv(ALERTS_KEY, {}) or {}
        rec = have.get(key) or {"causes": []}
        if cause in rec["causes"]:
            return None
        sent = db.post("out", f"A charter Restrictions item from your message #{mid} was refused: \"{item}\". Why: "
                              f"{why}. The coordinator was handed a filled retry template to resend it.",
                       chat=chat, kind="alert", severity="normal", ref=f"charter-refused:{key}")
        have[key] = {"chat": chat, "msg": mid, "causes": (rec["causes"] + [cause])[-10:], "ts": time.time(),
                     "item": item}
        db.set_kv(ALERTS_KEY, dict(sorted(have.items(), key=lambda x: x[1].get("ts", 0))[-ALERTS_KEEP:]))
    return sent


def landed(db, a: dict) -> int | None:
    """The item landed: clear its refusal alert, told as one low line to the same chat."""
    if not restrictions(a.get("section") or ""):
        return None
    key = item_key(a)
    with db.tx():
        have = db.kv(ALERTS_KEY, {}) or {}
        rec = have.pop(key, None)
        if rec is None:
            return None
        db.set_kv(ALERTS_KEY, have)
        return db.post("out", f"Cleared: the charter Restrictions item from your message #{rec['msg']} is now in "
                              f"the charter: \"{rec['item']}\".", chat=rec["chat"], kind="resolved",
                       severity="low", ref=f"charter-refused:{key}")


# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Agent mode's ask gate: no permission ask for what a logged order already covers.

With relay.AGENT_KEY on, the owner's own chat agent's words are the owner's. An ask_user seeking permission
for a step that the user or that agent already ordered in a logged inbound message is needless: the
coordinator sends the change itself (a Restrictions charter_update with `source`, the agent's words as
origin relayer) or acts. refusal() is model-free and runs only in agent mode; with it off nothing changes.

What it refuses: a restriction ask (blocking restriction, or classify restriction_change) or an irreversible
ask worded as a permission question (_PERMISSION_RE), when the ask cites a logged inbound message's id or
shares a phrase (relay.SHINGLE words, relay.CONTENT of them not common) with its words, verbatim or relayed.
Ids and phrases the charter already holds (a ban's own cite and words) do not count: quoting the ban asked
about is no order. What still goes to the owner: funds, spend, review, merge, access (credential) and human
(another person's request) asks, an ask in a relay.HELD class (a step that could affect people badly, one
that speaks as the owner, money or a credential), and an ask whose `not_covered` says what the order does
not cover.
"""
from __future__ import annotations

import re
import time

from . import relay

DAYS = 7   # how far back a logged order makes a permission ask needless
SCAN = 200   # inbound messages read, newest first
_PERMISSION_RE = re.compile(r"\b(?:(?:may|can|could|shall|should) (?:i|we)|ok(?:ay)? to|permission|allow\w*|approv\w*|"
                            r"go[- ]ahead|proceed|lift\w*|loosen\w*|exception)\b", re.I)
_CITE_RE = re.compile(r"\b(?:user\s+)?(?:msgs?|messages?)\s*#?(\d+)", re.I)


def _content(phrase: tuple[str, ...]) -> bool:
    return sum(w not in relay._COMMON for w in phrase) >= relay.CONTENT


def order(p, said: str, now: float | None = None) -> tuple[int, str, bool] | None:
    """(message id, the words of it the ask repeats or "", whether they are the owner's agent's and not the
    user's verbatim words) of the newest logged inbound message of the last DAYS that `said` cites or shares
    a phrase with, ids and phrases of the charter aside; else None."""
    charter = p.charter_path.read_text() if p.charter_path.exists() else ""
    known = {int(x) for x in re.findall(r"#(\d+)", charter)}
    known_phrases = relay._shingles(relay._words(charter))
    mine = relay._shingles(relay._words(said))
    cites = {int(x) for x in _CITE_RE.findall(said)} - known
    since = (now or time.time()) - DAYS * 86400
    for m in p.db.q("SELECT id, text, context FROM messages WHERE direction='in' AND kind IN ('user','finding') "
                    "AND COALESCE(provenance,'')!='system' AND ts>=? ORDER BY id DESC LIMIT ?", (since, SCAN)):
        if m["id"] in known:
            continue
        own, ctx = relay.split(m["text"] or "", m["context"])
        for words, agent in ((own, False), (ctx, True)):
            hit = sorted(h for h in (mine & relay._shingles(relay._words(words))) - known_phrases if _content(h))
            if hit:
                return m["id"], " ".join(hit[0]), agent
        if m["id"] in cites and (own or ctx):
            return m["id"], "", not own
    return None


def refusal(p, cfg: dict, a: dict, text: str, least: str, not_covered_min: int) -> tuple[str, str] | None:
    """(refusal label, why) for a needless permission ask in agent mode (module doc), else None."""
    if not relay.agent_orders(cfg) or a.get("blocking") not in ("irreversible", "restriction"):
        return None
    if len(str(a.get("not_covered") or "").strip()) >= not_covered_min:
        return None
    said = f"{text} {a.get('recommendation') or ''} {least}"
    if relay.held_class(said):
        return None
    restr = a.get("blocking") == "restriction" or a.get("classify") == "restriction_change"
    if not restr and not _PERMISSION_RE.search(text):
        return None
    got = order(p, said)
    if not got:
        return None
    mid, words, agent = got
    whose = relay.AGENT if agent else f"the user's own words ({relay.VERBATIM})"
    src = (f'{{"origin": "relayer", "msg": {mid}, "quote": "<its words>"}}' if agent
           else f'{{"msg": {mid}, "quote": "<their words>"}}')
    act = ("send the Restrictions change yourself: charter_update section Restrictions with `source` " + src
           + " (quote them exactly), and `quote` plus `text` to loosen or narrow the item, `excepts` for a scoped "
           "exception, or `both_hold` with an end (`expires` or `until`) for a temporary one" if restr else
           "act on it (task_add or the action), memory_add the decision and notify at severity low")
    return ("already ordered",
            f"message #{mid} already ordered this ({whose}" + (f': "{words}"' if words else "") + f"), and with "
            f"{relay.AGENT_KEY} on that order is the owner's: never ask the owner for permission to do what they "
            f"or their agent ordered. Instead, {act}. A refusal of that change names its own fix: apply it and "
            f"resend. Only a step that could affect people badly, speaks as the owner, or needs money or a "
            f"credential goes to the owner. If the order does not cover this ask, resend with not_covered saying "
            f"what differs (at least {not_covered_min} characters)")

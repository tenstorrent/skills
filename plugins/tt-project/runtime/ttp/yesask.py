# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A short yes answering an ask, read with the ask it answers.

"yes of course. don't ask stupid questions" names no change by itself: the ask before it does. A
Restrictions change cited to such a yes ({msg: <the yes>, quote}) takes its scope from that ask's
question, so the charter gate (restriction_source.check, coordinator._scoped_exception) reads the
yes and the ask together instead of refusing the change for its citation. Only a user's own message
counts as the yes (or the owner's agent's in agent mode, which check() reads as before); the ask is
the coordinator's own outbound ask that the yes resolved (unblock.RESOLVED_KEY), else the newest ask
before it in the same chat, at most ASK_MAX_AGE_S older.
"""
from __future__ import annotations

import re

ASK_MAX_AGE_S = 7 * 86400
# Assent at the start of the first sentence; a hedge in that sentence makes it no plain yes.
_YES_RE = re.compile(r"^\W*(?:yes|yep|yeah|yup|sure|of course|ok|okay|absolutely|approved?|go ahead|go for it|"
                     r"do it|please do|y)\b", re.I)
_HEDGE_RE = re.compile(r"\b(?:but|unless|if|only|except|wait|later|not yet|hold off|instead)\b|\?", re.I)


def is_yes(words: str) -> bool:
    """Whether `words` open with a plain yes (its first sentence assents and hedges nothing)."""
    first = re.split(r"[.!;\n]", (words or "").strip(), maxsplit=1)[0]
    return bool(_YES_RE.search(first)) and not _HEDGE_RE.search(first)


def answered_ask(db, mid: int) -> dict | None:
    """The outbound ask (id, text) that user message `mid` answers: one resolved with it, else the
    newest ask before it in its chat, at most ASK_MAX_AGE_S older; None if none."""
    from . import unblock
    for aid, rec in sorted((db.kv(unblock.RESOLVED_KEY, {}) or {}).items(), key=lambda x: -int(x[0])):
        if mid in [int(m) for m in (rec or {}).get("messages") or []]:
            row = db.one("SELECT id, text FROM messages WHERE id=? AND kind='ask' AND direction='out'", (int(aid),))
            if row:
                return dict(row)
    me = db.one("SELECT chat, ts FROM messages WHERE id=?", (mid,))
    if not me:
        return None
    row = db.one("SELECT id, text FROM messages WHERE kind='ask' AND direction='out' AND id<? AND chat IS ? "
                 "AND ts>=? ORDER BY id DESC LIMIT 1", (mid, me["chat"], float(me["ts"]) - ASK_MAX_AGE_S))
    return dict(row) if row else None


def scope(db, mid: int, own: str) -> str:
    """The question of the ask user message `mid` answers, when its own words `own` are a plain yes;
    else "". Those are the words the yes approves."""
    if not is_yes(own):
        return ""
    ask = answered_ask(db, mid)
    if not ask:
        return ""
    from .coordinator import _ask_question
    return _ask_question(ask["text"] or "").strip()


def reply_to(db, aid: int) -> int | None:
    """The user's plain-yes reply to outbound ask `aid` (a `source` that names the ask, not the
    answer): the message that resolved it, else the first user message after it in its chat."""
    from . import relay, unblock
    ask = db.one("SELECT chat FROM messages WHERE id=? AND kind='ask' AND direction='out'", (aid,))
    if not ask:
        return None
    ids = [int(m) for m in ((db.kv(unblock.RESOLVED_KEY, {}) or {}).get(str(aid)) or {}).get("messages") or []]
    if not ids:
        row = db.one("SELECT id FROM messages WHERE direction='in' AND kind='user' AND id>? AND chat IS ? "
                     "ORDER BY id LIMIT 1", (aid, ask["chat"]))
        ids = [row["id"]] if row else []
    for r in relay.recent(db, ids) if ids else []:
        if is_yes(relay.split(r["text"] or "", r["context"])[0]) and (answered_ask(db, r["id"]) or {}).get("id") == aid:
            return r["id"]
    return None

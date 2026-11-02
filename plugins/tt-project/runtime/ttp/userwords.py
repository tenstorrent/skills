# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The user's latest words on a topic, model-free, from the project's recorded messages.

`search` returns the newest of the user's own messages (inbound, kind user, never a system row;
only the verbatim part of a relayed one, never the relayer's context) matching a topic, with the
message id, time and the quoted sentence. `ttp said <words>` runs it read-only for the coordinator and
workers. `digest_lines` adds, for each blocked task, open ask and refused action of the last turn,
at most PER_ITEM lines 'User's latest words on this: #id date "quote"', so a turn sees what the user
said later before it refuses, blocks or asks again on an older rule. Nothing matches, nothing shown.

Match: content words (reask.words, with GENERIC words dropped) and hard anchors (reask.anchors).
A query (the command) matches a sentence holding every one of its words, or its anchors with half
of its words. A topic (the digest) matches a sentence sharing an anchor and one more word, or
MIN_SHARED words that are at least COVER of the topic's or of the sentence's words.
"""
from __future__ import annotations

import sqlite3
import time

from . import reask, timefmt

USER_SQL = ("SELECT id, ts, text, context FROM messages WHERE direction='in' AND kind='user' AND ts>=? "
            "AND COALESCE(provenance,'')!='system' AND COALESCE(channel,'')!='system' ORDER BY id DESC LIMIT ?")
SCAN = 3000          # newest user messages read per lookup
DAYS = 120.0         # the digest's look-back
PER_ITEM = 3         # lines per blocked item
ITEMS = 8            # blocked items looked up per turn
MIN_SHARED = 3
COVER = 0.3
QUOTE = 220
# Words every task, ask or refusal carries: they tell no topic apart.
GENERIC = {reask._stem(w) for w in """task tasks code work run runs fix fixes fixed blocked block wait waiting
waits project projects coordinator worker workers change changes reject rejected refused gate action actions
turn turns resend sent send""".split()}
HEADER = ("## The user's latest words on blocked items (newest first, from their recorded messages: the newest "
          "word on a topic wins over an older rule or reading; apply it, or say why it does not cover the item)")


def _words(text: str) -> set[str]:
    return reask.words(text) - GENERIC


def _quote(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= QUOTE else text[:QUOTE - 3].rstrip() + "..."


def _topic_hit(tw: set[str], ta: set[str], s: str) -> int:
    """Strength of sentence `s` on a topic of words `tw` and anchors `ta`; 0 when it does not match."""
    sw, sa = _words(s), reask.anchors(s)
    named = {a for a in ta if not a.startswith("task:")}   # its own task id names nothing else
    if named and sa and not named & sa:
        return 0   # both name things, none the same: about something else
    shared = tw & sw
    if ta & sa and len(shared - {a.split(":", 1)[1] for a in ta}) >= 1:
        return 10 + len(shared)
    if len(shared) >= MIN_SHARED and (len(shared) >= COVER * len(tw) or len(shared) >= COVER * len(sw)):
        return len(shared)
    return 0


def _query_hit(qw: set[str], qa: set[str], s: str) -> int:
    sw, sa = reask.words(s), reask.anchors(s)
    if qw and qw <= sw:
        return len(qw) + 2 * len(qa & sa)
    if qa and qa & sa and len(qw & sw) * 2 >= len(qw):
        return len(qw & sw) + 2 * len(qa & sa)
    return 0


def _rows(con, since: float) -> list:
    return con.execute(USER_SQL, (since, SCAN)).fetchall()


def _match(rows, hit, limit: int, skip: set[int] | frozenset = frozenset()) -> list[dict]:
    from .relay import split
    out = []
    for r in rows:   # newest first
        mid, ts, text, context = r[0], r[1], r[2], r[3]
        if mid in skip:
            continue
        said = split(text or "", context)[0]
        best, quote = 0, ""
        for s in reask._sentences(said):
            n = hit(s)
            if n > best:
                best, quote = n, s
        if best:
            out.append({"id": mid, "ts": ts, "quote": _quote(quote), "text": said, "score": best})
            if len(out) >= limit:
                break
    return out


def search(con, query: str, limit: int = 5, since: float = 0.0) -> list[dict]:
    """The newest user messages matching `query` (every content word, or its anchors with half of
    them), newest first: id, ts, quote (the best sentence), text (the verbatim words), score.
    `con`: a sqlite3 connection or a DB."""
    qw, qa = reask.words(query), reask.anchors(query)
    if not qw and not qa:
        return []
    return _match(_rows(con, since), lambda s: _query_hit(qw, qa, s), limit)


def on_topic(con, topic: str, limit: int = PER_ITEM, since: float = 0.0, skip=frozenset(), rows=None) -> list[dict]:
    """The newest user messages on a topic (a task title and reason, an ask, a refusal): see the module doc."""
    tw, ta = _words(topic), reask.anchors(topic)
    if len(tw) < 2 and not ta:
        return []
    return _match(_rows(con, since) if rows is None else rows, lambda s: _topic_hit(tw, ta, s), limit, skip)


def open_ro(db_path) -> sqlite3.Connection:
    """The project database read-only (a run may not write it)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)


def line(m: dict, where) -> str:
    return f'User\'s latest words on this: #{m["id"]} {timefmt.today(m["ts"], where)} "{m["quote"]}"'


def items(db, rejected: list[str]) -> list[tuple[str, str]]:
    """(label, topic) of each blocked task, open ask and refused action, at most ITEMS."""
    out = [(f"task #{t['id']}", f"{t['title']}\n{t['blocked_reason'] or ''}\n#{t['id']}")
           for t in db.q("SELECT id, title, blocked_reason FROM tasks WHERE status='blocked' ORDER BY priority, id")]
    from .coordinator import _ask_question
    out += [(f"ask #{a['id']}", _ask_question(a["text"] or ""))
            for a in db.q("SELECT id, text FROM messages WHERE kind='ask' AND handled=0 ORDER BY id DESC LIMIT 10")]
    out += [("refused action", str(x)[:600]) for x in rejected]
    return out[:ITEMS]


def digest_lines(p, rejected: list[str], skip: list[int] | None = None, now: float | None = None) -> list[str]:
    """The digest's section: per blocked item (items), up to PER_ITEM of the user's newest matching
    messages, `skip` (this turn's new messages, shown whole) left out; [] when nothing matches."""
    db = p.db
    todo = items(db, rejected)
    if not todo:
        return []
    now = time.time() if now is None else now
    rows = [(r["id"], r["ts"], r["text"], r["context"]) for r in db.q(USER_SQL, (now - DAYS * 86400, SCAN))]
    if not rows:
        return []
    out = []
    for label, topic in todo:
        for m in on_topic(None, topic, rows=rows, skip=set(skip or [])):
            out.append(f"- {label}: {line(m, p)}")
    return [HEADER, *out] if out else []

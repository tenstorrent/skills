# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Old user orders, re-read by intent: model-free. An order given for a reason (an overloaded machine, a
spend spike, a release week) must stop shaping work once that reason is gone, not be read literally
forever.

- Stored with a reason: memory_add and an obligation's order keep the coordinator's `reason` (why the
  user gave it) and the `context` (the cited user message's own words, context_for) beside the order
  (Project.add_memory front matter; the order's promise record). A read-only end or probe is the
  entry's own `expires` / `until_probe` (ends.py), which retires it by itself.
- scan: memory entries that cite a user message (`user msg #N`), are preferences or standing rules, are
  older than AGE_S and have no end of their own, plus open orders older than AGE_S, whose reason is
  unknown (none recorded), over (every task it names is finished) or unchecked (prose naming no task:
  the coordinator judges it by intent). A reason naming a task still open holds. Restrictions are never listed:
  they loosen only on the user's word. Each item lists the holds citing it: a blocked task's reason, a
  deferred task's `why` and an open ask, naming the entry, its rule id (rulecite), its `oN` or one of
  its user messages. An open order no hold cites is listed only when its reason is over or unchecked.
- digest_lines: the 'Re-read by intent' section, on a blocker sweep (held items) and once per RECHECK_S
  per item (the daily review), at most MAX_LINES lines. coursecheck flags each held item every turn.
"""
from __future__ import annotations

import json
import re
import time

AGE_S = 86400               # an order younger than this is never listed
RECHECK_S = 86400           # an item is listed again after this long (daily)
MAX_LINES = 6
CHARS = 300                 # a stored reason or context is cut to this
KV = "old_orders"           # kv: {"at": ts, "items": [item]} of the last scan (coursecheck reads it)
LISTED_KEY = "old_orders_listed"   # kv: {item key: ts} it was last listed in the digest
HEADER = ("## Re-read by intent (old user orders whose reason is over or unknown: read each by its intent and "
          "context; retire one whose reason is gone, `memory_forget` or the obligation with a `why`, and say so; "
          "a Restriction only on the user's word)")
_USER_MSG = re.compile(r"\b(?:user|owner)(?:'s)?\s+(?:msgs?|messages?)\s+((?:#\d+\b(?:\s*(?:,|and|or|&)\s*)?)+)", re.I)
_ANY_MSG = re.compile(r"\b(?:msgs?|messages?)\s+#(\d+)\b", re.I)
_OID = re.compile(r"(?<![\w-])(o\d+)\b")
_FINISHED = {"done", "cancelled", "failed", "exhausted", "landed", "abandoned"}


def one_line(text) -> str:
    """`text` as one front-matter-safe line of at most CHARS characters ("" for none)."""
    return " ".join(str(text or "").split()).replace("---", "—")[:CHARS]


def user_msgs(text: str) -> list[int]:
    """The user messages `text` cites (`user msg #N`, `owner's msgs #N and #M`), in order."""
    out = [int(n) for m in _USER_MSG.finditer(text or "") for n in re.findall(r"#(\d+)", m.group(1))]
    return list(dict.fromkeys(out))


def clean_context(p, context: str) -> str:
    """`context` (harness-attached user words), or "" when it matches a declared forbidden-data pattern
    or a pattern is invalid: dropped, never refused (the caller's own text is checked as before) and
    never written anywhere."""
    if not context:
        return ""
    from .forbidden import matches
    try:
        return "" if matches(p, context) else context
    except ValueError:
        return ""


def context_for(db, text: str, message: int | None = None) -> str:
    """The user's own words behind an order: `message` (else the first user message `text` cites),
    with its date, as one line; "" when none is on record."""
    ids = [message] if message else user_msgs(text)
    for n in ids:
        row = db.one("SELECT ts, text FROM messages WHERE id=? AND direction='in'", (int(n),))
        if row and (row["text"] or "").strip():
            day = time.strftime("%Y-%m-%d", time.gmtime(float(row["ts"] or 0)))
            return one_line(f'user msg #{n} ({day}): "{one_line(row["text"])[:240]}"')
    return ""


def _state(p, reason: str) -> str:
    """'unknown' (no reason), 'over (#1: done)' (every task it names finished), 'unchecked' (a prose
    reason naming no task: only the coordinator can judge it by intent) or "" (a task it names is open)."""
    if not reason:
        return "unknown"
    from .ends import task_states
    states = task_states(p, reason)
    if not states:
        return "unchecked"
    parts = [s.split(": ", 1)[-1] for s in states.split("; ")]
    return f"over ({states})" if all(s in _FINISHED for s in parts) else ""


def _holds(db) -> list[dict]:
    from .db import deferral
    out = [{"what": f"blocked #{r['id']}", "text": r["blocked_reason"] or ""}
           for r in db.q("SELECT id, blocked_reason FROM tasks WHERE status='blocked'")]
    for r in db.q("SELECT id, labels FROM tasks WHERE status IN ('queued', 'waiting') AND labels LIKE '%start_%'"):
        why = deferral(dict(r)).get("why")
        if why:
            out.append({"what": f"deferred #{r['id']}", "text": why})
    out += [{"what": f"ask #{r['id']}", "text": r["text"] or ""}
            for r in db.q("SELECT id, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0")]
    return [h for h in out if h["text"]]


def _cites(hold: str, names: list[str], msgs: list[int]) -> bool:
    if any(n and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", hold) for n in names):
        return True
    return bool(msgs) and bool({int(x) for x in _ANY_MSG.findall(hold)} & set(msgs))


def scan(p, now: float | None = None) -> list[dict]:
    """The old orders whose reason is over or unknown (see the module doc), held ones first, oldest
    first; also kept in kv KV for coursecheck."""
    from . import obligations
    from .project import _stamp_ts
    from .rulecite import _sha4
    now = time.time() if now is None else now
    db, holds, items = p.db, _holds(p.db), []
    for e in p._memory_entries():
        if e["kind"] == "restriction" or e["end"] or not (e["standing"] or e["kind"] == "preference"):
            continue
        msgs = user_msgs(e["body"])
        created = _stamp_ts(e["created"])
        if not msgs or now - created < AGE_S or not (state := _state(p, e.get("reason") or "")):
            continue
        names = [e["name"], f"M-{_sha4(e['name'])}"]
        items.append({"key": e["name"], "what": f"memory [{e['name']}]", "text": one_line(e["body"])[:160],
                      "msgs": msgs, "reason": e.get("reason") or "", "state": state, "context": e.get("context") or "",
                      "age_s": now - created,
                      "held": [h["what"] for h in holds if _cites(h["text"], names, msgs)][:4]})
    for o in obligations.items(db):
        pr = o.get("promise") or {}
        if not pr.get("order") or not obligations._open(o) or now - float(o.get("created") or now) < AGE_S:
            continue
        if not (state := _state(p, pr.get("reason") or "")):
            continue
        held = [h["what"] for h in holds if _cites(h["text"], [o["id"]], [int(pr["order"])])][:4]
        if held or state != "unknown":
            items.append({"key": o["id"], "what": f"order {o['id']}", "text": one_line(o.get("text"))[:160],
                          "msgs": [int(pr["order"])], "reason": pr.get("reason") or "", "state": state,
                          "context": pr.get("context") or "", "age_s": now - float(o["created"]), "held": held})
    items.sort(key=lambda i: (not i["held"], -i["age_s"]))
    db.set_kv(KV, {"at": now, "items": items})
    return items


def _age(s: float) -> str:
    return f"{int(s // 86400)}d" if s >= 86400 else f"{int(s // 3600)}h"


def line(i: dict) -> str:
    why = f"reason {i['state']}{': ' + i['reason'] if i['reason'] else ''}"
    ctx = f"; context: {i['context']}" if i["context"] else ""
    held = f"; cited by {', '.join(i['held'])}" if i["held"] else ""
    return f"- {i['what']} ({_age(i['age_s'])} old): {i['text']} | {why}{ctx}{held}"


def digest_lines(p, event_ids: list[int] | None = None, now: float | None = None) -> list[str]:
    """The 'Re-read by intent' section: on a blocker sweep in this batch, the held items; and each item
    not listed for RECHECK_S (the daily review). Scans every turn so coursecheck reads fresh holds."""
    from .sweep import SWEEP_KIND
    now = time.time() if now is None else now
    db = p.db
    items = scan(p, now)
    sweep = bool(event_ids) and bool(db.q(
        f"SELECT 1 FROM events WHERE kind=? AND id IN ({','.join('?' * len(event_ids))}) LIMIT 1",
        (SWEEP_KIND, *event_ids)))
    listed = {k: v for k, v in (db.kv(LISTED_KEY, {}) or {}).items() if k in {i["key"] for i in items}}
    due = [i for i in items if (sweep and i["held"]) or now - float(listed.get(i["key"]) or 0) >= RECHECK_S]
    due.sort(key=lambda i: (not i["held"], float(listed.get(i["key"]) or 0), -i["age_s"]))
    due = due[:MAX_LINES]
    for i in due:
        listed[i["key"]] = now
    db.set_kv(LISTED_KEY, listed)
    if not due:
        return []
    more = len(items) - len(due)
    return [HEADER, *map(line, due)] + ([f"- (+{more} more not shown here)"] if more > 0 else [])


def held(db) -> list[dict]:
    """The last scan's items some hold cites (coursecheck)."""
    return [i for i in ((db.kv(KV, {}) or {}).get("items") or []) if i.get("held")]

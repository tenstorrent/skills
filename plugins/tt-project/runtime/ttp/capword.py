# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A dollar cap the user set in their own words, applied inside a run.

A run may not set a user-only setting (coordinator.NEEDS_USER): text it reads could steer it. A cap
change the user ordered in chat is still theirs, though, and should not wait for someone to type
`ttp config` by hand. So `ttp config KEY VALUE --user-msg ID` in a run applies a project's daily or
7-day dollar cap (KEYS) only when message ID is the user's own (inbound, kind user, not posted by the
harness), from the last MAX_AGE_S, and its verbatim words (relay.split: never a relayer's context)
bind the exact value to that cap in one clause (_grants). A later recorded set of the same key by the
user or the coordinator wins: an older message never overwrites it. The setter is recorded as the user, with that message id. Every
other user-only key still changes only in a coordinator turn that carries the user's yes.
"""
from __future__ import annotations

import re
import time

KEYS = {"budget.daily_usd": ("daily", "day", "per day", "a day", "24h", "24 h"),
        "budget.weekly_usd": ("weekly", "week", "7-day", "7 day", "7 days", "7d")}
MAX_AGE_S = 14 * 86400
# A dollar amount: '$' before it or a dollar word after it; never a '#' id, never inside a word.
_NUM = re.compile(r"(?<![#\w.,$])(\$\s*)?(\d[\d,]*(?:\.\d+)?)(\s*k\b)?(\s*(?:usd|dollars?|bucks)\b)?", re.I)
_CAP = re.compile(r"\b(?:cap|caps|budget|limit|spend|spending)\b")
_VERB = re.compile(r"\b(?:set|raise|raising|lower|bump|increase|reduce|change|make|put|move)\b")
# A limit the value must not pass, a negation or a past value: no grant of that value.
_NOT = re.compile(r"n['’]t\b|\b(?:not|no|never|nor|without|dont|above|below|over|under|beyond|exceed\w*|than|"
                  r"at most|at least|was|were|used to|had)\b")
_CLAUSE = re.compile(r"[;!?\n]|\.(?=\s|$)|,(?=\s)")


def _key_token(low: str, key: str) -> bool:
    """The key's own name as a whole token: 'global_daily_usd' never names budget.daily_usd."""
    return any(re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", low) for n in (key, key.split(".")[-1]))


def _names_key(low: str, key: str) -> bool:
    if _key_token(low, key):
        return True
    if "global" in low:   # the global daily cap is another key: only its literal name above counts
        return False
    return any(re.search(rf"(?<![\w-]){re.escape(w)}\b", low) for w in KEYS[key])


def _grants(words: str, key: str) -> tuple[bool, set[float]]:
    """Whether any clause of `words` names `key`, and the dollar values a clause binds to it: the clause
    names the cap (its key name, or its period word with cap/budget/limit or a set verb) and the amount
    carries '$' or a dollar word (a bare number only beside the key's own name), with no negation, no
    above/below limit, no past value and no second dollar amount in that clause."""
    named, out = False, set()
    for clause in _CLAUSE.split(words.lower()):
        if not _names_key(clause, key):
            continue
        named = True
        token = _key_token(clause, key)
        if _NOT.search(clause) or not (token or _CAP.search(clause) or _VERB.search(clause)):
            continue
        found = set()
        for m in _NUM.finditer(clause):
            if not (m.group(1) or m.group(4) or token):
                continue
            try:
                found.add(float(m.group(2).replace(",", "")) * (1000 if m.group(3) else 1))
            except ValueError:
                continue
        if len(found) == 1:   # two dollar amounts in one clause: which is the cap is unclear, so neither
            out |= found
    return named, out


def problem(db, key: str, value, msg_id, now: float | None = None) -> str | None:
    """Why message `msg_id` does not grant setting `key` to `value` inside a run, or None when it does."""
    if key not in KEYS:
        return f"--user-msg applies only {', '.join(sorted(KEYS))}; {key} waits for a coordinator turn with the user's yes"
    try:
        mid, amount = int(str(msg_id).lstrip("#")), float(value)
    except (TypeError, ValueError):
        return f"--user-msg needs a message id and a number for {key}"
    row = db.one("SELECT ts, text, provenance FROM messages WHERE id=? AND direction='in' AND kind='user'", (mid,))
    if not row or (row.get("provenance") or "") == "system":
        return f"message #{mid} is not the user's own message"
    now = time.time() if now is None else now
    if now - float(row["ts"]) > MAX_AGE_S:
        return f"message #{mid} is older than {MAX_AGE_S // 86400} days; the coordinator asks the user again"
    from . import configwho, relay
    words = relay.split(row.get("text") or "")[0]
    named, values = _grants(words, key)
    if not named:
        return f"message #{mid}'s own words do not name {key}"
    if amount not in values:
        return (f"message #{mid}'s own words do not name the value {value} as {key} (one clause: the cap, "
                f"a $ amount, no negation or above/below limit)")
    rec = configwho.get(db, key) or {}
    if rec.get("by") in ("user", "coordinator") and float(rec.get("ts") or 0) > float(row["ts"]):
        return f"the {rec['by']} set {key} again after message #{mid}; that later value stands"
    return None

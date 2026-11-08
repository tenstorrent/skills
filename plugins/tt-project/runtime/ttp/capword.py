# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A dollar cap the user set in their own words, applied inside a run.

A run may not set a user-only setting (coordinator.NEEDS_USER): text it reads could steer it. A cap
change the user ordered in chat is still theirs, though, and should not wait for someone to type
`ttp config` by hand. So `ttp config KEY VALUE --user-msg ID` in a run applies a project's daily or
7-day dollar cap (KEYS) only when message ID is the user's own (inbound, kind user, not posted by the
harness), from the last MAX_AGE_S, and its verbatim words (relay.split: never a relayer's context)
name both that cap and the exact value. A later recorded set of the same key by the user wins: an
older message never overwrites it. The setter is recorded as the user, with that message id. Every
other user-only key still changes only in a coordinator turn that carries the user's yes.
"""
from __future__ import annotations

import re
import time

KEYS = {"budget.daily_usd": ("daily", "day", "per day", "a day", "24h", "24 h"),
        "budget.weekly_usd": ("weekly", "week", "7-day", "7 day", "7 days", "7d")}
MAX_AGE_S = 14 * 86400
_NUM = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b)?", re.I)


def _amounts(text: str) -> set[float]:
    out = set()
    for m in _NUM.finditer(text):
        try:
            out.add(float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1))
        except ValueError:
            continue
    return out


def _names_key(words: str, key: str) -> bool:
    low = words.lower()
    if key in low or key.split(".")[-1] in low:
        return True
    if "global" in low:   # the global daily cap is another key: only its literal name above counts
        return False
    return any(re.search(rf"(?<![\w-]){re.escape(w)}\b", low) for w in KEYS[key])


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
    if not _names_key(words, key):
        return f"message #{mid}'s own words do not name {key}"
    if amount not in _amounts(words):
        return f"message #{mid}'s own words do not name the value {value}"
    rec = configwho.get(db, key) or {}
    if rec.get("by") == "user" and float(rec.get("ts") or 0) > float(row["ts"]):
        return f"the user set {key} again after message #{mid}; that later value stands"
    return None

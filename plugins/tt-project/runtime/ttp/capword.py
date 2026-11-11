# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A dollar cap the user set themselves in chat, applied inside a run.

A run may not set a user-only setting (coordinator.NEEDS_USER): text it reads could steer it. A cap
change the user wrote in chat is still theirs, though, and should not wait for someone to type
`ttp config` by hand. So `ttp config KEY VALUE --user-msg ID` in a run applies a project's daily or
7-day dollar cap (KEYS) only on a structured grant: message ID is the user's own (inbound, kind user,
not posted by the harness), from the last MAX_AGE_S, and its verbatim words (relay.split: never a
relayer's context) carry the setting itself, the cap's full key and that one value (`budget.weekly_usd
400`, `budget.weekly_usd=$400`, as `ttp config` takes it; grants()). Prose about a cap ('raise the
weekly cap to $400') grants nothing: no gate reads the meaning of the user's wording (user msg #2116),
so such an order goes to the coordinator, which sets the cap in a turn that carries the user's yes. A
later recorded set of the same key by the user or the coordinator, or a later message of the user's
giving the key another value, wins: an older message never overwrites the user's newest word. The setter is recorded as the user, with that message id. Every other user-only key
still changes only in a coordinator turn that carries the user's yes.
"""
from __future__ import annotations

import re
import time

KEYS = ("budget.daily_usd", "budget.weekly_usd")
MAX_AGE_S = 14 * 86400


def grants(words: str, key: str) -> set[float]:
    """The values `words` set `key` to: its full dotted name as a whole token, then '=', ':' or a space,
    an optional '$' and a plain number (`budget.daily_usd 250`, `budget.daily_usd: $250.5`)."""
    out = set()
    for m in re.finditer(rf"(?<![\w.]){re.escape(key)}\s*[=:]?\s*\$?(\d+(?:\.\d+)?)(?![\w.,]?\d)(?!\w)", words or ""):
        out.add(float(m.group(1)))
    return out


def problem(db, key: str, value, msg_id, now: float | None = None) -> str | None:
    """Why message `msg_id` does not grant setting `key` to `value` inside a run, or None when it does."""
    if key not in KEYS:
        return f"--user-msg applies only {', '.join(KEYS)}; {key} waits for a coordinator turn with the user's yes"
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
    values = grants(relay.split(row.get("text") or "")[0], key)
    if values != {amount}:
        return (f"message #{mid}'s own words do not carry the setting `{key} {value}`"
                + (" alone" if amount in values else "")
                + "; a cap the user ordered in words is set by the coordinator in a turn that carries their yes: "
                  "name the value in your hand-off")
    # The user's newest word wins: a later message of theirs setting this key to another value (or to
    # several) means the older message grants nothing.
    for later in db.q("SELECT id, text, provenance FROM messages WHERE direction='in' AND kind='user' "
                      "AND (ts > ? OR (ts = ? AND id > ?)) AND ts <= ? AND text LIKE ? ORDER BY ts DESC, id DESC",
                      (row["ts"], row["ts"], mid, now, f"%{key}%")):
        if (later.get("provenance") or "") == "system":
            continue
        said = grants(relay.split(later.get("text") or "")[0], key)
        if said and said != {amount}:
            return (f"message #{later['id']} set {key} to {', '.join(f'{v:g}' for v in sorted(said))} later; "
                    "that value stands")
    rec = configwho.get(db, key) or {}
    if rec.get("by") in ("user", "coordinator") and float(rec.get("ts") or 0) > float(row["ts"]):
        return f"the {rec['by']} set {key} again after message #{mid}; that later value stands"
    return None

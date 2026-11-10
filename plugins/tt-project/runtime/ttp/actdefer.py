# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Coordinator actions a git timeout deferred, applied again on the next tick.

coordinator.apply checks a task_add or task_update against git (its branch, the head it continues).
Under a spent time budget (gitmemo: a finish transaction's deadline, a round's slice) that check can
time out. A timeout says nothing about the action, so apply never rejects it for one: when the action
wrote nothing yet, it is kept here (kv, committed with the turn, so a restart keeps it) and the
daemon's tick applies it again (retry), outside any finish transaction. After MAX_TRIES timeouts it is
rejected as before, saying it timed out.

The replay is the same apply as the first one: the action keeps its turn's context (the user messages
the turn was started for, whether it was a user turn), so a task_add citing one of those messages is
still recorded as the user's order, and a task_update's `waits_on ask:new` is stored re-pointed to the
ask its turn sent (apply does that once the turn's asks are known: repoint, drop)."""

from __future__ import annotations

import time

KEY = "deferred_actions"
TYPES = frozenset({"task_add", "task_update"})
MAX_TRIES = 5


def deferrable(action_type: str | None, tries: int, wrote: bool) -> bool:
    """Whether an action of `action_type` whose git check timed out on try `tries` is kept for a retry:
    only one that wrote nothing yet, so applying it again repeats nothing."""
    return action_type in TYPES and not wrote and tries < MAX_TRIES


def defer(db, action: dict, chat: str | None, why: str, tries: int, messages: list[int] | None = None,
          user_turn: bool = False) -> tuple[int, str]:
    """Keep `action` for the next tick with its turn's context (`messages`, `user_turn`: as apply got
    them); its id and the note for the coordinator's next digest."""
    items = list(db.kv(KEY, []) or [])
    n = max([int(x.get("id") or 0) for x in items] + [0]) + 1
    items.append({"id": n, "action": dict(action), "chat": chat, "tries": tries, "why": why[:300], "at": time.time(),
                  "messages": [int(m) for m in messages or []], "user_turn": bool(user_turn)})
    db.set_kv(KEY, items)
    return n, (f"{action.get('type')}: deferred, not rejected: a git check timed out ({why[:200]}); the daemon "
               f"applies it again on its next tick")


def repoint(db, item_id: int, waits_on: str) -> None:
    """Store deferred action `item_id`'s `waits_on` as `waits_on` (its turn's `ask:new` resolved)."""
    db.set_kv(KEY, [{**x, "action": {**x["action"], "waits_on": waits_on}}
                    if isinstance(x, dict) and x.get("id") == item_id and isinstance(x.get("action"), dict) else x
                    for x in db.kv(KEY, []) or []])


def drop(db, item_id: int) -> None:
    db.set_kv(KEY, [x for x in db.kv(KEY, []) or [] if not (isinstance(x, dict) and x.get("id") == item_id)])


def pending(db) -> list[dict]:
    return [x for x in db.kv(KEY, []) or [] if isinstance(x, dict) and isinstance(x.get("action"), dict)]


def retry(p) -> list[str]:
    """Apply each deferred action again, each in its own transaction with its removal. Its problems join
    the last turn's rejections and its notes the last turn's notes (the next digest shows both). One
    line per action for the log."""
    from . import coordinator as coord
    db, out = p.db, []
    for item in pending(db):
        with db.tx():
            left = [x for x in db.kv(KEY, []) or [] if not (isinstance(x, dict) and x.get("id") == item["id"])]
            if len(left) == len(db.kv(KEY, []) or []):
                continue   # applied already
            db.set_kv(KEY, left)
            notes, reply = db.kv(coord.NOTES_KEY, []) or [], db.kv(coord.REJECTED_REPLY_KEY, False)
            problems = list(coord.apply(p, [item["action"]], default_chat=item.get("chat"),
                                        user_turn=bool(item.get("user_turn")),
                                        messages=[int(m) for m in item.get("messages") or []] or None,
                                        deferred_tries=int(item.get("tries") or 0) + 1))
            db.set_kv(coord.NOTES_KEY, list(notes) + list(db.kv(coord.NOTES_KEY, []) or []))
            db.set_kv(coord.REJECTED_REPLY_KEY, reply)
            if problems:
                rejected = list(db.kv(coord.REJECTED_KEY, []) or [])
                db.set_kv(coord.REJECTED_KEY, rejected + problems)
                if not rejected:
                    db.set_kv(coord.REJECTED_AT_KEY, time.time())
        out.append(f"deferred {item['action'].get('type')} {item['id']}: "
                   + ("; ".join(problems)[:300] if problems else "applied"))
    return out

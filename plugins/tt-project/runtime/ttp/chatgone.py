# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Relay chats that stopped reading, and the warning for messages that reached no reader.

A chat whose `ttp listen` is not running (its listen-<chat>.pid lock is missing or names a dead
process) and that left a message unread longer than relay.gone_after_s is gone: it no longer counts
as a reader of asks and alerts, and one low status line in the feed says so until its next read.
The web app is a reader too: each poll from a visible tab moves the 'web' chat's read cursor.
"""
from __future__ import annotations

import os

from .db import SEVERITY_RANK, chat_floor
from .project import zombie

KEY = "chat-silent"        # alert key prefix: chat-silent:<chat id>
KV = "chats_silent"        # kv: chat id -> its last_read when it was reported gone
GONE_AFTER_S = 86400.0     # relay.gone_after_s default (0: a chat never counts as gone)
SAY_CHAT_PREFIX = "say-"   # a chat `ttp say` without --chat registered for its caller (cli.say_chat)
SAY_FROM = "say_chat_from:"   # kv: a say chat's read cursor at creation (cli._register_say_chat)


def read_from(db, chat: str) -> int:
    """The newest message a chat had not read when it was made: a say chat starts with its cursor at
    the newest message, which is no read, so only a later `ttp listen` read past an older one counts."""
    return int(db.kv(SAY_FROM + chat, 0) or 0) if chat.startswith(SAY_CHAT_PREFIX) else 0


def listener_alive(p, chat: str) -> bool:
    try:
        pid = int((p.state / f"listen-{chat}.pid").read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return not zombie(pid)


def _read_at(db, chat: dict) -> float:
    """When the chat last read: the time of the newest message it read, else when it connected."""
    row = db.one("SELECT ts FROM messages WHERE id=?", (int(chat["last_read"] or 0),))
    return float(row["ts"] if row else chat["created"] or 0)


def chats(p, db, cfg: dict, now: float) -> list[dict]:
    """Every relay chat (not the web app) with its read cursor, whether a listener runs, and `gone`."""
    after = float((cfg.get("relay") or {}).get("gone_after_s", GONE_AFTER_S) or 0)   # 0: never gone
    out = []
    for c in db.q("SELECT id, label, created, last_read, min_severity FROM chats WHERE id!='web' ORDER BY id"):
        floor = SEVERITY_RANK.get(chat_floor(c["min_severity"], cfg["notify"].get("chat_min_severity")), 1)
        sevs = [s for s, r in SEVERITY_RANK.items() if r >= floor]
        alive = listener_alive(p, c["id"])
        # Silent: a message it should read has waited longer than gone_after_s.
        old = None if alive or after <= 0 else db.one(
            f"SELECT id FROM messages WHERE direction='out' AND id>? AND ts<? AND (chat=? OR (chat IS NULL AND "
            f"channel!='quiet' AND severity IN ({','.join('?' * len(sevs))}))) LIMIT 1",
            (int(c["last_read"] or 0), now - after, c["id"], *sevs))
        out.append({"id": c["id"], "label": c["label"] or c["id"], "read_id": int(c["last_read"] or 0),
                    "from": read_from(db, c["id"]),
                    "read_at": _read_at(db, c), "floor": floor, "listener": alive, "gone": bool(old)})
    return out


def holds(db, chat: str) -> bool:
    """The gone line lasts until the chat reads again (its cursor moves past the one reported)."""
    seen = (db.kv(KV, {}) or {}).get(chat)
    row = db.one("SELECT last_read FROM chats WHERE id=?", (chat,))
    return seen is not None and bool(row) and int(row["last_read"] or 0) <= int(seen)


def check(p, db, cfg: dict, now: float, alert, short) -> None:
    """Daemon step: one low status line per gone chat, kept until its next read."""
    old = db.kv(KV, {}) or {}
    seen = {k: v for k, v in old.items() if holds(db, k)}
    for c in chats(p, db, cfg, now):
        if c["gone"] and c["id"] not in seen:
            alert(f"{KEY}:{c['id']}", f"chat {c['label']} has not read since {short(c['read_at'])} (message "
                  f"#{c['read_id']}); asks and alerts reach the web app and desktop alerts", severity="low")
            seen[c["id"]] = c["read_id"]
    if seen != old:
        db.set_kv(KV, seen)

"""Relayer chats (an agent chat that relays a project's messages to the user) kept cheap.

Every line `ttp listen` prints wakes the relayer, and each wake re-reads its whole chat. So the
listener wakes it only for what is meant for the user (replies, asks, alerts), batches broadcast
alerts over a short window (relay.batch_s), and skips routine updates, which stay in the web app
and `ttp status`. Each chat id has one listener lease (`claim`): a relayer listening on another
chat's id would take that chat's replies, so it is refused, and `conflicts` names it in `ttp status`
and the coordinator digest.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

BATCH_S = 120.0            # relay.batch_s default: how long a broadcast alert waits for company
LEASE_S = 600.0            # relay.lease_s default: a chat's listener lease outlives its listener this long
CONFLICTS_KEY = "relay_conflicts"   # kv: chat id -> the last relayer refused on it or taking it over
SHOWN_S = 86400.0          # status and the digest show a conflict this long
URGENT = ("high", "critical")


def holder_id() -> str:
    """Who is listening: the relayer chat's own session (TTP_RELAYER_ID, else the agent host's session
    id) on its machine; '' when the host gives none."""
    sid = os.environ.get("TTP_RELAYER_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    return f"{os.environ.get('TTP_CLIENT_HOST') or socket.gethostname()}/{sid[:12]}" if sid else ""


def _lease_path(p, chat: str) -> Path:
    return p.state / f"listen-{chat}.lease"


def read_lease(p, chat: str) -> dict:
    try:
        v = json.loads(_lease_path(p, chat).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_lease(p, chat: str, lease: dict) -> None:
    from .project import durable_write
    try:
        durable_write(_lease_path(p, chat), json.dumps(lease), sync=False)   # a lost heartbeat only shortens it
    except OSError:
        pass


def claim(p, db, chat: str, holder: str, live: bool, now: float | None = None) -> tuple[str, int | None]:
    """One relayer per chat id. Returns ('', rewind) to go ahead, or (refusal, None).

    The first relayer that connects or listens owns the chat. Another is refused while the lease
    holder listens or listened within relay.lease_s, so a relayer never takes another chat's replies.
    After that it may take over (a resumed relayer with a new session); its owner coming back wins
    the chat back, and `rewind` is the read mark at the takeover: what the other one took is
    delivered again. An unnamed listener (no session id) goes ahead only on a free chat."""
    now = now or time.time()
    lease = read_lease(p, chat)
    cur = lease.get("holder", "")
    fresh = live or now - float(lease.get("seen") or 0) < lease_s(p.config())
    if cur and cur != holder and fresh and (not holder or holder != lease.get("owner")):
        note_conflict(db, chat, holder or "a listener with no session id", cur, "refused", now)
        return (f"chat {chat} is another relayer's ({cur}, last listening {(now - float(lease.get('seen') or now)) / 60:.0f} "
                f"min ago). Listen only on your own chat id: `ttp connect <project>` prints one."), None
    if not holder:
        return "", None
    rewind = None
    lease.setdefault("owner", holder)
    if holder == lease["owner"]:
        rewind = (lease.pop("taken", None) or {}).get("at")
    elif cur and cur != holder and "taken" not in lease:
        row = db.one("SELECT last_read FROM chats WHERE id=?", (chat,))
        lease["taken"] = {"from": cur, "at": int(row["last_read"] or 0) if row else 0, "ts": now}
        note_conflict(db, chat, holder, cur, "took", now)
    lease.update(holder=holder, seen=now)
    write_lease(p, chat, lease)
    return "", rewind


def touch(p, chat: str, holder: str, now: float | None = None) -> None:
    """The listener's heartbeat: keeps its lease while it runs."""
    lease = read_lease(p, chat)
    if holder and lease.get("holder") == holder:
        lease["seen"] = now or time.time()
        write_lease(p, chat, lease)


def note_conflict(db, chat: str, by: str, holder: str, kind: str, now: float) -> None:
    try:
        seen, _ = db.kv_dict(CONFLICTS_KEY)
        was = seen.get(chat) if isinstance(seen.get(chat), dict) else {}
        n = int(was.get("n") or 0) + 1 if was.get("by") == by and now - float(was.get("ts") or 0) < SHOWN_S else 1
        seen = {c: v for c, v in seen.items() if isinstance(v, dict) and now - float(v.get("ts") or 0) < SHOWN_S}
        seen[chat] = {"by": by, "holder": holder, "kind": kind, "ts": now, "n": n}
        db.set_kv(CONFLICTS_KEY, seen)
    except Exception:
        pass   # best effort: a busy database never stops a listener


def conflicts(db, now: float | None = None) -> list[str]:
    """One line per chat id another relayer tried to listen on in the last day, naming the one to close."""
    now = now or time.time()
    seen, _ = db.kv_dict(CONFLICTS_KEY)
    out = []
    for c, v in sorted(seen.items()):
        if not isinstance(v, dict) or now - float(v.get("ts") or 0) >= SHOWN_S:
            continue
        did = (f"was refused {v.get('n', 1)}x" if v.get("kind") == "refused"
               else f"took it over from {v.get('holder')}; its owner gets the taken messages again when it listens")
        out.append(f"a second relayer ({v.get('by')}) listened on chat {c}, which {v.get('holder')} uses, and {did}: "
                   f"close that relayer chat, or connect it to its own chat id (one relayer per chat)")
    return out


def wakes(m, chat: str) -> str:
    """'now' (print at once), 'batch' (print within the batch window) or '' (routine: skipped)."""
    if m["chat"] == chat or m["kind"] == "ask" or m["severity"] in URGENT:
        return "now"
    return "batch" if m["kind"] == "alert" else ""


def batch_s(cfg: dict, given: float | None) -> float:
    if given is not None:
        return max(0.0, given)
    return _relay_num(cfg, "batch_s", BATCH_S)


def lease_s(cfg: dict) -> float:
    return _relay_num(cfg, "lease_s", LEASE_S)


def _relay_num(cfg: dict, key: str, default: float) -> float:
    try:
        return max(0.0, float((cfg.get("relay") or {}).get(key, default)))
    except (TypeError, ValueError, AttributeError):
        return default

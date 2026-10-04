# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Slack as a notification and inbox channel, with the standard library only.

One Slack app per user, shared by all of that user's projects. The bot DMs the user; messages
the user sends to the bot are polled from that DM (no public endpoint, no websocket, works from
any on-prem box). Routing between projects:
- the bot posts every project message as `[<project>] ...`;
- a reply in that message's thread belongs to that project;
- a top-level DM starting with `<project>:` goes to that project;
- a top-level DM without a prefix goes to the only project, or is answered with a list of names.
Each daemon only consumes messages routed to its own project, so several projects on several
machines can share one bot without a central server.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://slack.com/api/"
THREAD_WINDOW_S = 7 * 86400   # replies to posts older than this are not looked for
THREAD_SCAN_S = 60            # how often all posts of that window are checked for new replies
MAX_PAGES = 20


class SlackError(Exception):
    def __init__(self, msg: str, method: str = "", code: str | None = None):
        super().__init__(msg)
        self.method = method
        self.code = code   # Slack's `error` from an ok=false reply; None for HTTP and network errors


# chat.postMessage errors caused by the message itself: retrying the same message cannot succeed.
# Auth, rate-limit and service errors are left out so an outage never drops a message.
REJECTED_MESSAGE = frozenset({
    "no_text", "msg_too_long", "msg_blocks_too_long", "invalid_blocks", "invalid_blocks_format",
    "invalid_attachments", "too_many_attachments", "invalid_metadata_format",
    "invalid_metadata_schema", "metadata_too_large", "markdown_text_conflict"})


class Slack:
    def __init__(self, bot_token: str, user_id: str | None = None, user_email: str | None = None):
        self.token = bot_token
        self.user_id = user_id
        self.user_email = user_email
        self._dm: str | None = None

    def call(self, method: str, **params: Any) -> dict:
        data = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v)
                                       for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(API + method, data=data, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/x-www-form-urlencoded"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    out = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(int(e.headers.get("Retry-After", "5")))
                    continue
                raise SlackError(f"HTTP {e.code} on {method}", method) from None
            if not out.get("ok"):
                raise SlackError(f"{method}: {out.get('error')}", method, out.get("error"))
            return out
        raise SlackError(f"{method}: rate limited", method)

    def resolve_user(self) -> str:
        if not self.user_id:
            if not self.user_email:
                raise SlackError("no Slack user id or email configured")
            self.user_id = self.call("users.lookupByEmail", email=self.user_email)["user"]["id"]
        return self.user_id

    def dm_channel(self) -> str:
        if not self._dm:
            self._dm = self.call("conversations.open", users=self.resolve_user())["channel"]["id"]
        return self._dm

    def post(self, project: str, text: str, thread_ts: str | None = None) -> str:
        body = text if thread_ts else f"[{project}] {text}"
        return self.call("chat.postMessage", channel=self.dm_channel(), text=body[:39000],
                         thread_ts=thread_ts, unfurl_links=False)["ts"]

    @staticmethod
    def rejected_message(e: Exception) -> bool:
        """Whether Slack refused this particular message, as opposed to being unreachable."""
        return isinstance(e, SlackError) and e.method == "chat.postMessage" and e.code in REJECTED_MESSAGE

    def poll(self, oldest: str) -> tuple[list[dict], list[dict]]:
        """Top-level messages the user wrote in the DM after `oldest`, oldest first, and every post
        after `oldest` (thread parents). A backlog longer than one page is read whole."""
        posts = self._pages("conversations.history", channel=self.dm_channel(), oldest=oldest)
        uid = self.resolve_user()
        top = [m for m in posts if m.get("user") == uid and m.get("thread_ts") in (None, m["ts"])]
        return sorted(top, key=lambda m: float(m["ts"])), posts

    def recent_posts(self) -> list[dict]:
        """The DM's posts of the last THREAD_WINDOW_S. A reply never moves its parent, so threads with
        new replies are looked for among all of these, not only among posts after the read cursor."""
        return self._pages("conversations.history", channel=self.dm_channel(),
                           oldest=f"{time.time() - THREAD_WINDOW_S:.6f}")

    def new_replies(self, parents: list[dict], read: dict[str, str], floor: str) -> list[tuple[str, list[dict]]]:
        """(parent ts, replies oldest first) for each thread with replies after its own read position,
        `read[parent ts]`, or `floor` for a thread not read yet. Every thread keeps its own position,
        so a reply is never skipped because a newer message elsewhere was read first."""
        ch, out = self.dm_channel(), []
        for parent in {m["ts"]: m for m in parents}.values():
            pos = read.get(parent["ts"], floor)
            if parent.get("reply_count") and float(parent.get("latest_reply", "0")) > float(pos):
                reps = [r for r in self._pages("conversations.replies", channel=ch, ts=parent["ts"], oldest=pos)
                        if r["ts"] != parent["ts"] and float(r["ts"]) > float(pos)]
                if reps:
                    out.append((parent["ts"], sorted(reps, key=lambda r: float(r["ts"]))))
        return out

    def message(self, ts: str) -> dict | None:
        """The DM message with this ts, top-level or in a thread, as Slack has it now; None if none."""
        for m in self.call("conversations.replies", channel=self.dm_channel(), ts=ts, oldest=ts, latest=ts,
                           inclusive=True, limit=10).get("messages", []):
            if m.get("ts") == ts:
                return m
        return None

    def _pages(self, method: str, **params: Any) -> list[dict]:
        msgs: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            page = self.call(method, limit=200, cursor=cursor, **params)
            msgs += page.get("messages", [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return msgs


def from_config(cfg: dict) -> Slack | None:
    """The user's Slack client when this project uses Slack, else None."""
    if not cfg.get("notify", {}).get("slack"):
        return None
    from .project import load_secrets
    sec = load_secrets().get("slack") or {}
    return Slack(sec["bot_token"], sec.get("user_id"), sec.get("user_email")) if sec.get("bot_token") else None


PREFIX = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]{0,62})\s*:\s*(.+)$", re.S)
BOT_TAG = re.compile(r"^\[([A-Za-z0-9][A-Za-z0-9._-]{0,62})\] ")


def projects_in_dm(history: list[dict]) -> list[str]:
    """Every project that has posted in this DM, so each daemon knows its siblings on other
    machines without a shared server. The first name alphabetically answers ambiguous messages."""
    return sorted({m.group(1) for h in history if h.get("bot_id") and (m := BOT_TAG.match(h.get("text", "")))})


def route(msg: dict, project: str, own_threads: set[str], all_projects: list[str]) -> str | None:
    """Return the message text if it belongs to `project`, else None."""
    text = (msg.get("text") or "").strip()
    thread = msg.get("thread_ts")
    if thread and thread != msg.get("ts"):
        return text if thread in own_threads else None
    m = PREFIX.match(text)
    if m and m.group(1).lower() in {p.lower() for p in all_projects}:
        return m.group(2).strip() if m.group(1).lower() == project.lower() else None
    return text if len(all_projects) == 1 and all_projects[0] == project else None

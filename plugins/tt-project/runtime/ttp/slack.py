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


class SlackError(Exception):
    pass


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
                raise SlackError(f"HTTP {e.code} on {method}") from None
            if not out.get("ok"):
                raise SlackError(f"{method}: {out.get('error')}")
            return out
        raise SlackError(f"{method}: rate limited")

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

    def poll(self, oldest: str) -> list[dict]:
        """Messages the user wrote in the DM after `oldest` (top level and in threads), oldest first."""
        ch = self.dm_channel()
        uid = self.resolve_user()
        msgs = self.call("conversations.history", channel=ch, oldest=oldest, limit=100).get("messages", [])
        out = [m for m in msgs if m.get("user") == uid]
        for parent in msgs:   # replies live in threads; fetch only threads with new activity
            if parent.get("reply_count") and float(parent.get("latest_reply", "0")) > float(oldest):
                reps = self.call("conversations.replies", channel=ch, ts=parent["ts"], oldest=oldest)
                out += [r for r in reps.get("messages", [])[1:] if r.get("user") == uid]
        return sorted(out, key=lambda m: float(m["ts"]))


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

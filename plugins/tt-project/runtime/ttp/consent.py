# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The questions tt-project asks a user once, per user and machine (`~/.tt-project/consent.json`).

`notifier`: install the desktop notifier on this computer. `tunnel`: open local view tunnels (SSH
local forwards that let this computer view a remote project's web app). Each is asked once, by the
agent in the user's session; the answer is recorded with `ttp consent <topic> yes|no` and never
asked again: a yes lets the step run from then on, a no keeps it off until the user records a yes.
A user who already has the thing (an installed notifier, a kept tunnel service) said yes. A user
who runs the command at a terminal themselves (stdin a TTY) says yes by doing it. Tunnels that
expose this computer to others (reverse forwards, public binds) still need their own ask each time.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import project

TOPICS = {
    "notifier": "Install the tt-project desktop notifier on this computer (native alerts for all your projects)?",
    "tunnel": ("Open local view tunnels (SSH local forwards, kept up as a user service) so this computer can show "
               "remote projects' web apps?"),
}
ANSWERS = ("yes", "no")
NEEDS_ANSWER = 3   # exit code: not done, the user's one answer is missing or was no


def path() -> Path:
    return project.HOME_DIR / "consent.json"


def _load() -> dict:
    try:
        data = json.loads(path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _already(topic: str) -> str | None:
    """What says this user already chose yes without being asked: an installed notifier, a kept tunnel."""
    home = Path.home()
    if topic == "tunnel":
        kept = sorted([*(home / "Library" / "LaunchAgents").glob("com.tt-project.tunnel.*.plist"),
                       *(home / ".config" / "systemd" / "user").glob("com.tt-project.tunnel.*.service")])
        return f"kept tunnel {kept[0].name}" if kept else None
    if topic == "notifier":
        for f in (home / "Library" / "LaunchAgents" / "com.tt-project.notifier.plist",
                  home / ".config" / "systemd" / "user" / "tt-project-notifier.service"):
            if f.is_file():
                return f"installed notifier {f.name}"
    return None


def record(topic: str, answer: str, how: str = "the user's answer") -> dict:
    if topic not in TOPICS or answer not in ANSWERS:
        raise ValueError(f"consent: topic is one of {', '.join(TOPICS)} and the answer yes or no")
    data = _load()
    data[topic] = {"answer": answer, "ts": time.time(), "how": how}
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    project.write_json(path(), data)
    return data[topic]


def answer(topic: str) -> str:
    """'yes', 'no' or 'ask' (never asked). Something the user already has counts as a recorded yes."""
    got = _load().get(topic) or {}
    if got.get("answer") in ANSWERS:
        return got["answer"]
    why = _already(topic)
    if why:
        record(topic, "yes", why)
        return "yes"
    return "ask"


def interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def gate(topic: str, cmd: str) -> str | None:
    """None when `topic` may go ahead (a recorded yes, or the user running `cmd` at a terminal, which
    records one); else what to do instead: ask once, or respect the recorded no."""
    a = answer(topic)
    if a == "yes":
        return None
    if a == "ask" and interactive():
        record(topic, "yes", f"ran `{cmd}` at a terminal")
        return None
    if a == "no":
        return (f"the user said no to this ({TOPICS[topic]}): do not ask again. Only the user's own later "
                f"word changes it: `ttp consent {topic} yes`, then `{cmd}`")
    return (f"asked once per user, never yet: ask the user {TOPICS[topic]!r}, then record the answer with "
            f"`ttp consent {topic} yes` or `ttp consent {topic} no` (never asked again), and on a yes run `{cmd}`")

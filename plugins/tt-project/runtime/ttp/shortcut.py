# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The plain `/tt-project` command in Claude Code. Claude Code lists every plugin skill under the plugin's
namespace (`/tt-project:tt-project`), so `ttp setup` adds a thin personal skill, `<claude config>/skills/
tt-project/SKILL.md`, that the slash menu lists as `/tt-project` and that loads the plugin's skill. It is
user-invoked only (`disable-model-invocation`), so the model still sees one tt-project skill, not two.
Setup rewrites it on every install, leaves a user's own file of that name untouched, and
`ttp setup --no-shortcut` removes it again."""
from __future__ import annotations

from pathlib import Path

MARK = "<!-- ttp-managed:"

TEXT = """---
name: tt-project
description: "Start, connect to, or talk with a long-running tt-project (shortcut for the tt-project plugin's main skill)."
argument-hint: "[message or request for tt-project]"
disable-model-invocation: true
---

<!-- ttp-managed: written by `ttp setup`, which updates it on every install; `ttp setup --no-shortcut` removes it. -->

Load the tt-project plugin's main skill now and follow it, with the request below unchanged. In
Claude Code it is `tt-project:tt-project` (use the Skill tool); in other hosts it is the plugin's own
`tt-project` skill. If it is not available, say that the tt-project plugin is not installed or not
enabled here, and stop.

Request: $ARGUMENTS
"""


def path() -> Path:
    from .providers.claude import claude_config_dir
    return claude_config_dir() / "skills" / "tt-project" / "SKILL.md"


def ours(f: Path) -> bool:
    try:
        return MARK in f.read_text(errors="replace")
    except OSError:
        return False


def install() -> str:
    """Write or refresh the shortcut. One line for setup to print, empty when nothing changed."""
    f = path()
    root = f.parents[2]
    if not root.is_dir():       # no Claude Code for this user
        return ""
    if f.exists() and not ours(f):
        return f"left your own {f} as it is: /tt-project runs it; the plugin skill stays /tt-project:tt-project"
    if f.exists() and f.read_text(errors="replace") == TEXT:
        return ""
    from .project import durable_write
    durable_write(f, TEXT)
    return f"Claude Code: /tt-project is ready ({f})"


def remove() -> str:
    f = path()
    if not f.exists():
        return ""
    if not ours(f):
        return f"left {f} as it is: it is not the one ttp setup wrote"
    f.unlink()
    try:
        f.parent.rmdir()
    except OSError:
        pass
    return f"removed the /tt-project shortcut ({f}); the plugin skill stays /tt-project:tt-project"

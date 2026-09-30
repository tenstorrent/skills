# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider hook endpoint: `python -m ttp.hook <event>`, with the hook payload on stdin.

Claude Code calls it after every tool use. If the coordinator has changed the task since the
worker started (it appends to `$TTP_RUN_DIR/steer.md`), the new part is handed to the worker as
added context, once. Workers on providers without hooks read the same file between steps.

It fails open: any error prints nothing, and the run goes on unchanged.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

STEER_FILE = "steer.md"
OFFSET_FILE = "steer.offset"


def unread_update(run_dir: Path) -> tuple[str, int]:
    """The part of steer.md this run has not seen yet, and the offset that marks it seen. The caller
    marks it only once the text is handed over (a repeat is harmless, a loss is not)."""
    steer = run_dir / STEER_FILE
    if not steer.exists():
        return "", 0
    data = steer.read_bytes()
    try:
        seen = int((run_dir / OFFSET_FILE).read_text())
    except (OSError, ValueError):
        seen = 0
    if len(data) <= seen:
        return "", seen
    return data[seen:].decode("utf-8", errors="replace").strip(), len(data)


def mark_seen(run_dir: Path, offset: int) -> None:
    (run_dir / OFFSET_FILE).write_text(str(offset))


def post_tool_use(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        return None, None
    text, offset = unread_update(Path(run_dir))
    if not text:
        return None, None
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "Update for your task from the project coordinator. Where it differs from "
                             "the spec, it wins:\n" + text}}, lambda: mark_seen(Path(run_dir), offset)


HANDLERS = {"PostToolUse": post_tool_use}


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    handler = HANDLERS.get(event or payload.get("hook_event_name") or "")
    if not handler:
        return 0
    try:
        out, delivered = handler(payload)
        if out:
            json.dump(out, sys.stdout)
            sys.stdout.flush()
        if delivered:
            delivered()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

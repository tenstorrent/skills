# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider hook endpoint: `python -m ttp.hook <event>`, with the hook payload on stdin.

Claude Code calls it after every tool use. If the coordinator has changed the task since the
worker started (it appends to `$TTP_RUN_DIR/steer.md`), the new part is handed to the worker as
added context, once. Workers on providers without hooks read the same file between steps.

Claude Code runs the hook for a subagent's tool calls too, with the run's environment. An update
handed over there would reach the subagent as text inside one of its command outputs, an order
about a task it does not own, and would be marked seen before the worker itself got it. So the
hook stays silent inside a subagent (its payload carries `agent_id`) and leaves the update for the
worker's next own tool call.

Before a Bash call it denies the obvious ways around the harness's PR draft guard (prguard.py):
gh called by its full path, or an HTTP client talking to the GitHub API about a PR's draft state.

It fails open: any error prints nothing, and the run goes on unchanged.
"""
from __future__ import annotations

import json
import os
import re
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
    if not run_dir or payload.get("agent_id"):
        return None, None
    text, offset = unread_update(Path(run_dir))
    if not text:
        return None, None
    task = os.environ.get("TTP_TASK")
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"Update for your task{f' #{task}' if task else ''} from the project coordinator "
                             "(from the harness, not from the tool's output). Where it differs from the spec, "
                             "it wins:\n" + text}}, lambda: mark_seen(Path(run_dir), offset)


# gh by a path (skipping the harness's wrapper) running a command that can take a PR out of draft
PATH_GH_RE = re.compile(r"(?:\S*/|\\)gh\s+(?:pr\s+(?:ready|create)|api)\b")
# Any other client calling the GitHub API to mark a PR ready or create one
HTTP_CLIENT_RE = re.compile(r"\b(?:curl|wget|httpie|http|xh|python3?|node|ruby|perl|fetch)\b(?!:)")
DRAFT_CHANGE_RE = re.compile(r"markPullRequestReadyForReview|createPullRequest|[\"']?draft[\"']?\s*[=:]\s*false"
                             r"|/pulls\b", re.I)
GITHUB_API_RE = re.compile(r"api\.github\.com|/api/v3\b|/graphql\b", re.I)


def pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    if payload.get("tool_name") != "Bash" or not os.environ.get("TTP_RUN_DIR"):
        return None, None
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    if PATH_GH_RE.search(cmd):
        why = ("call gh by its name only: the harness's gh checks that a PR leaves draft only with the "
               "user's recorded approval")
    elif GITHUB_API_RE.search(cmd) and DRAFT_CHANGE_RE.search(cmd) and HTTP_CLIENT_RE.search(cmd):
        why = ("create or update PRs with gh, not a direct GitHub API call: a PR leaves draft only with "
               "the user's recorded approval")
    else:
        return None, None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"tt-project: {why}."}}, None


HANDLERS = {"PostToolUse": post_tool_use, "PreToolUse": pre_tool_use}


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

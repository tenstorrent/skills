# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A deterministic stand-in agent for tests and dry runs: no model, no network, no cost.

Coordinator runs echo a scripted action list (TTP_FAKE_ACTIONS, JSON) or, by default, reply to
every user message and queue one task per message. Worker runs write result.json and exit.
With TTP_FAKE_SESSIONS set to a directory, each run saves a session transcript there, and
`--resume <id>` continues one (or fails to start, like a real agent, when it is gone).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import register
from .base import Provider, RunUsage


@register
class Fake(Provider):
    name = "fake"

    def credential_files(self) -> list[str]:
        return [f] if (f := os.environ.get("TTP_FAKE_CREDENTIALS")) else []

    def binary(self):
        return sys.executable

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        return [sys.executable, "-m", "ttp.providers.fake", role], {}

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id] if session_id else []

    def session_saved(self, session_id: str, cwd: str, env: dict | None = None) -> bool:
        d = os.environ.get("TTP_FAKE_SESSIONS")
        return bool(d and session_id and (Path(d) / f"{session_id}.jsonl").is_file())

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage(cost_usd=0.0)
        try:
            data = json.loads(Path(output_path).read_text() or "{}")
        except (OSError, ValueError):
            data = {}
        u.final_text = json.dumps(data)
        u.structured = data if isinstance(data, dict) else None
        u.cost_usd = float(data.get("_cost", 0.0)) if isinstance(data, dict) else 0.0
        u.output_tokens = int(data.get("_output_tokens", 0)) if isinstance(data, dict) else 0
        u.session_id = str(data.get("_session") or "") if isinstance(data, dict) else ""
        return u


def _main(role: str, args: list[str] | None = None) -> int:
    import uuid
    args = args or []
    prompt = sys.stdin.read()
    sessions = os.environ.get("TTP_FAKE_SESSIONS")
    session = args[args.index("--resume") + 1] if "--resume" in args[:-1] else ""
    if session and not (sessions and (Path(sessions) / f"{session}.jsonl").is_file()):
        sys.stderr.write(f"No conversation found with session ID: {session}\n")
        return 1
    session = session or os.environ.get("TTP_FAKE_SESSION") or str(uuid.uuid4())
    if sessions:
        with open(Path(sessions) / f"{session}.jsonl", "a") as f:
            f.write(json.dumps({"role": role, "prompt": prompt}) + "\n")
    cost = float(os.environ.get("TTP_FAKE_COST", "0"))
    if role == "coordinator":
        scripted = os.environ.get("TTP_FAKE_ACTIONS")
        if scripted:
            out = {"actions": json.loads(scripted)}
        else:
            actions = []
            for line in prompt.splitlines():
                if line.startswith("- [user message via"):
                    chat = line.split("chat=")[1].split("]")[0]
                    text = line.split("] ", 1)[1]
                    actions.append({"type": "reply", "chat": chat, "text": f"ack: {text}"})
                    actions.append({"type": "task_add", "title": f"do: {text}"[:80], "spec": text, "tier": "light",
                                    "reply_chat": chat})
            out = {"actions": actions or [{"type": "noop"}]}
    else:
        run_dir = Path(os.environ["TTP_RUN_DIR"])
        result = json.loads(os.environ.get("TTP_FAKE_RESULT") or "null") or {
            "status": "done", "summary": f"fake {role} finished", "followups": []}
        (run_dir / "result.json").write_text(json.dumps(result))
        out = {"ok": True}
    out["_cost"] = cost
    out["_session"] = session
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1] if len(sys.argv) > 1 else "worker", sys.argv[2:]))

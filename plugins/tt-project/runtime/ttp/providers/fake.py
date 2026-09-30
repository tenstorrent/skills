# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A deterministic stand-in agent for tests and dry runs: no model, no network, no cost.

Coordinator runs echo a scripted action list (TTP_FAKE_ACTIONS, JSON) or, by default, reply to
every user message and queue one task per message. Worker runs write result.json and exit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import register
from .base import Provider, RunUsage


@register
class Fake(Provider):
    name = "fake"

    def binary(self):
        return sys.executable

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        return [sys.executable, "-m", "ttp.providers.fake", role], {}

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage(cost_usd=0.0)
        try:
            data = json.loads(Path(output_path).read_text() or "{}")
        except (OSError, ValueError):
            data = {}
        u.final_text = json.dumps(data)
        u.structured = data if isinstance(data, dict) else None
        u.cost_usd = float(data.get("_cost", 0.0)) if isinstance(data, dict) else 0.0
        return u


def _main(role: str) -> int:
    import os
    prompt = sys.stdin.read()
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
        (run_dir / "result.json").write_text(json.dumps(
            {"status": "done", "summary": f"fake {role} finished", "followups": []}))
        out = {"ok": True}
    out["_cost"] = cost
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1] if len(sys.argv) > 1 else "worker"))

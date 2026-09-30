# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI Codex CLI (`codex exec --json`). Codex reports tokens but no cost, has no budget flag,
and exposes plan windows through `codex app-server` (`account/rateLimits/read`, no model tokens).
Cost is therefore an estimate from a price table the project can edit; the runner enforces the
run's dollar budget mid-flight from streamed token counts."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import register
from .base import LIMIT_RE, Provider, RunUsage

# $ per million tokens: (input, cached input, output). Estimates only; the project may override
# them in project.json under pricing.codex.<model>. Unknown models use the "default" row.
PRICES = {"default": (4.0, 0.4, 20.0)}


@register
class Codex(Provider):
    name = "codex"
    binaries = ("codex",)

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        argv = [self.binary() or "codex", "exec", "--json", "-C", cwd, "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["-c", "approval_policy=never"]
        if read_only:
            argv += ["-s", "read-only"]
        else:
            argv += ["-s", "workspace-write"]
            if not restrictions.get("no_internet"):
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if schema:
            fd, path = tempfile.mkstemp(prefix="ttp-schema-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(schema, f)
            argv += ["--output-schema", path]
        argv += ["-"]
        return argv, {}

    def _events(self, output_path: Path):
        try:
            with open(output_path, errors="replace") as f:
                for line in f:
                    if line.startswith("{"):
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
        except FileNotFoundError:
            return

    def _tokens(self, output_path: Path) -> tuple[int, int, int, str]:
        inp = cached = out = 0
        last = ""
        for ev in self._events(output_path):
            t = ev.get("type", "")
            if t == "turn.completed":
                u = ev.get("usage") or {}
                inp += int(u.get("input_tokens") or 0)
                cached += int(u.get("cached_input_tokens") or 0)
                out += int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
            elif t.startswith("item.") and (ev.get("item") or {}).get("type") == "agent_message":
                last = (ev.get("item") or {}).get("text") or last
        return inp, cached, out, last

    def _price(self, model: str, inp: int, cached: int, out: int, prices: dict | None = None) -> float:
        table = {**PRICES, **(prices or {})}
        pin, pcached, pout = table.get(model) or table["default"]
        return ((inp - cached) * pin + cached * pcached + out * pout) / 1e6

    def cost_so_far(self, output_path):
        inp, cached, out, _ = self._tokens(output_path)
        return self._price("", inp, cached, out)

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        inp, cached, out, last = self._tokens(output_path)
        u = RunUsage(input_tokens=inp - cached, cache_read_tokens=cached, output_tokens=out, final_text=last,
                     estimated=True)
        u.cost_usd = self._price("", inp, cached, out)
        errors = [ev for ev in self._events(output_path) if ev.get("type") in ("turn.failed", "error")]
        if errors:
            u.error = json.dumps(errors[-1])[:400]
        if last.strip().startswith("{"):
            try:
                u.structured = json.loads(last)
            except ValueError:
                pass
        blob = u.error + " " + (Path(stderr_path).read_text(errors="replace")[-2000:]
                                if stderr_path and Path(stderr_path).exists() else "")
        if LIMIT_RE.search(blob):
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        return u

    def account(self) -> str:
        try:
            auth = json.loads(Path(os.path.expanduser("~/.codex/auth.json")).read_text())
        except (OSError, ValueError):
            return ""
        mode = auth.get("auth_mode") or ("api_key" if auth.get("OPENAI_API_KEY") else "chatgpt")
        return f"codex ({mode})"

    def meter(self) -> list:
        """Plan windows via the app-server protocol; spends no model tokens."""
        exe = self.binary()
        if not exe:
            return []
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "tt-project", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
        try:
            out = subprocess.run([exe, "app-server"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        from ..budget import Window
        wins = []
        for line in out.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") != 2:
                continue
            rl = (msg.get("result") or {}).get("rateLimits") or {}
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not w:
                    continue
                mins = int(w.get("windowDurationMins") or 0)
                name = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m"
                wins.append(Window("codex", name, float(w.get("usedPercent") or 0), w.get("resetsAt"),
                                   rl.get("planType") or ""))
        return wins

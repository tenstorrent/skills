# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Cursor CLI (`agent -p`, also installed as `cursor-agent`). No cost field, no budget flag and no
effort flag: reasoning depth is part of the model name, so tiers map to model names. Usage tokens
are read when the build reports them; cost is estimated from the project's price table."""
from __future__ import annotations

import json
from pathlib import Path

from . import register
from .base import LIMIT_RE, Provider, RunUsage

PRICES = {"default": (3.0, 0.3, 15.0)}


@register
class Cursor(Provider):
    name = "cursor"
    binaries = ("agent", "cursor-agent")

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        argv = [self.binary() or "agent", "-p", "--output-format", "json", "--trust", "--workspace", cwd]
        if model:
            argv += ["--model", model]
        if not read_only:
            argv += ["--force"]
        return argv, {}

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage(estimated=True)
        text = Path(output_path).read_text(errors="replace") if Path(output_path).exists() else ""
        data = None
        for line in reversed(text.strip().splitlines()):
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        if data:
            u.final_text = data.get("result") or ""
            u.session_id = data.get("session_id") or ""
            usage = data.get("usage") or {}
            u.input_tokens = int(usage.get("inputTokens") or 0)
            u.output_tokens = int(usage.get("outputTokens") or 0)
            u.cache_read_tokens = int(usage.get("cacheReadTokens") or 0)
            u.cache_write_tokens = int(usage.get("cacheWriteTokens") or 0)
            if data.get("is_error"):
                u.error = str(data.get("result"))[:400]
        pin, pcached, pout = PRICES["default"]
        u.cost_usd = (u.input_tokens * pin + u.cache_read_tokens * pcached + u.output_tokens * pout) / 1e6
        blob = u.error + " " + (Path(stderr_path).read_text(errors="replace")[-2000:]
                                if stderr_path and Path(stderr_path).exists() else "")
        if LIMIT_RE.search(blob):
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        if u.final_text.strip().startswith("{"):
            try:
                u.structured = json.loads(u.final_text)
            except ValueError:
                pass
        return u

    def account(self) -> str:
        return "cursor"

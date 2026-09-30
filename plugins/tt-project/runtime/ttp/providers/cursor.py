# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Cursor CLI (`agent -p`, also installed as `cursor-agent`). No cost field, no budget flag and no
effort flag: reasoning depth is part of the model name, so tiers map to model names. Usage tokens
are read when the build reports them; cost is estimated from the project's price table.

Builds that offer `--output-format stream-json` stream one event per line, ending with the same
result object `json` prints alone. Streaming keeps the output file growing while the agent works,
so the runner's stall guard sees progress and its budget check sees spend before the run ends."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import register
from .base import AUTH_RE, LIMIT_RE, Provider, RunUsage, cli_output, price_row, stderr_tail

PRICES = {"default": (3.0, 0.3, 15.0)}
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")


@register
class Cursor(Provider):
    name = "cursor"
    binaries = ("agent", "cursor-agent")
    login_hint = "run `agent login` there"
    isolate_read_only = True

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        exe = self.binary() or "agent"
        help_text = cli_output(exe, "--help")
        fmt = "stream-json" if "stream-json" in help_text else "json"
        argv = [exe, "-p", "--output-format", fmt, "--trust", "--workspace", cwd]
        if model:
            argv += ["--model", model]
        if not read_only:
            argv += ["--force"]
        elif re.search(r"--mode\b[\s\S]{0,400}?\bask\b", help_text):   # its choices may wrap
            argv += ["--mode", "ask"]   # answers only: no edits and no commands
        return argv, {}

    def _scan(self, output_path: Path) -> dict:
        """The result object, the usage it (or any earlier event) reported, the last assistant text
        and how many characters the agent produced so far."""
        seen = {"result": None, "usage": dict.fromkeys(USAGE_KEYS, 0), "reported": False, "last": "",
                "session_id": "", "chars": 0, "events": 0}
        streamed = dict.fromkeys(USAGE_KEYS, 0)
        try:
            with open(output_path, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    seen["events"] += 1
                    seen["session_id"] = ev.get("session_id") or seen["session_id"]
                    kind = ev.get("type")
                    if kind == "assistant":
                        content = (ev.get("message") or {}).get("content") or []
                        text = "".join(c.get("text") or "" for c in content if isinstance(c, dict))
                        seen["last"] = text or seen["last"]
                        seen["chars"] += len(text)
                    elif kind == "tool_call":
                        seen["chars"] += len(json.dumps(ev.get("tool_call") or {}))
                    usage = ev.get("usage")
                    if kind in ("result", None) and "result" in ev:
                        seen["result"] = ev
                        if isinstance(usage, dict):
                            seen["usage"] = {k: int(usage.get(k) or 0) for k in USAGE_KEYS}
                            seen["reported"] = True
                    elif isinstance(usage, dict):
                        for k in USAGE_KEYS:
                            streamed[k] += int(usage.get(k) or 0)
        except FileNotFoundError:
            pass
        if not seen["reported"] and any(streamed.values()):
            # Per-message usage only counts when no result totals arrived, or it would count twice.
            seen["usage"], seen["reported"] = streamed, True
        return seen

    def _price(self, usage: dict) -> float:
        pin, pcached, pout = price_row(PRICES, self.prices, self.model)
        return (usage["inputTokens"] * pin + usage["cacheReadTokens"] * pcached + usage["outputTokens"] * pout) / 1e6

    def cost_so_far(self, output_path):
        seen = self._scan(output_path)
        if seen["reported"]:
            return self._price(seen["usage"])
        if not seen["events"]:
            return None
        # No usage yet: a floor of what the agent wrote (~4 characters a token) at the output rate.
        # It under-counts the context each call reads, but a run writing without end still trips it.
        return self._price({**dict.fromkeys(USAGE_KEYS, 0), "outputTokens": seen["chars"] // 4})

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage(estimated=True)
        seen = self._scan(Path(output_path))
        data = seen["result"]
        u.session_id = seen["session_id"]
        u.input_tokens = seen["usage"]["inputTokens"]
        u.output_tokens = seen["usage"]["outputTokens"]
        u.cache_read_tokens = seen["usage"]["cacheReadTokens"]
        u.cache_write_tokens = seen["usage"]["cacheWriteTokens"]
        # A run killed before its result keeps the last thing it said.
        u.final_text = (data.get("result") or "") if data else seen["last"]
        if data and (data.get("is_error") or data.get("subtype") not in (None, "success")):
            u.error = str(data.get("result") or data.get("subtype"))[:400]
        err = stderr_tail(stderr_path)
        if not data and err.strip():
            u.error = err.strip()[-400:]   # a failed run prints no result, only its reason on stderr
        # Only reported usage is priced: with none, the daemon books the run's elapsed budget share.
        u.cost_usd = self._price(seen["usage"])
        blob = u.error + " " + err
        if AUTH_RE.search(blob) and not u.output_tokens:
            u.auth_failed = True
        elif LIMIT_RE.search(blob):
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        if u.final_text.strip().startswith("{"):
            try:
                u.structured = json.loads(u.final_text)
            except ValueError:
                pass
        return u

    def account(self) -> str:
        return "cursor"

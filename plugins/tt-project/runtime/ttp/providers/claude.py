# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code (`claude -p`). Every run streams JSON: a `rate_limit_event` carrying the account's
plan windows (utilization as a 0..1 fraction plus reset times), per-message usage, and a final
`result` with the reported cost, the last message and any schema-validated output."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import register
from ..budget import Window
from .base import AUTH_RE, LIMIT_RE, Provider, RunUsage


@register
class Claude(Provider):
    name = "claude"
    binaries = ("claude",)

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        argv = [self.binary() or "claude", "-p", "--output-format", "stream-json", "--verbose", "--no-chrome"]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        if budget_usd:
            argv += ["--max-budget-usd", f"{float(budget_usd):.2f}"]
        if read_only:
            # No command execution, no web, user/project settings ignored: a decision-only turn.
            argv += ["--restricted", "--permission-mode", "dontAsk", "--disallowedTools", "Edit", "Write",
                     "NotebookEdit"]
        else:
            argv += ["--permission-mode", "bypassPermissions"]
        if schema:
            argv += ["--json-schema", json.dumps(schema)]
        denied = []
        if restrictions.get("no_internet"):
            denied += ["WebFetch", "WebSearch"]
        if denied and not read_only:
            argv += ["--disallowedTools", *denied]
        env = {"CLAUDE_CODE_ENABLE_CFC": "0"}
        return argv, env

    def _events(self, output_path: Path):
        try:
            with open(output_path, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            yield json.loads(line)
                        except ValueError:
                            continue
        except FileNotFoundError:
            return

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage()
        last_text = ""
        est_in = est_out = est_cr = est_cw = 0
        for ev in self._events(output_path):
            t = ev.get("type")
            if t == "rate_limit_event":
                u.extra["windows"] = windows_from_event(ev)
            elif t == "assistant":
                msg = ev.get("message") or {}
                us = msg.get("usage") or {}
                est_in += int(us.get("input_tokens") or 0)
                est_out += int(us.get("output_tokens") or 0)
                est_cr += int(us.get("cache_read_input_tokens") or 0)
                est_cw += int(us.get("cache_creation_input_tokens") or 0)
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_text = block.get("text") or last_text
            elif t == "system" and ev.get("subtype") == "init":
                u.session_id = ev.get("session_id", "")
                u.extra["model"] = ev.get("model", "")
            elif t == "result":
                usage = ev.get("usage") or {}
                u.cost_usd = float(ev.get("total_cost_usd") or 0.0)
                u.input_tokens = int(usage.get("input_tokens") or 0)
                u.output_tokens = int(usage.get("output_tokens") or 0)
                u.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
                u.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
                u.final_text = ev.get("result") if isinstance(ev.get("result"), str) else json.dumps(ev.get("result"))
                u.structured = ev.get("structured_output")
                if ev.get("is_error"):
                    u.error = f"{ev.get('subtype')}: {str(ev.get('result'))[:300]}"
                u.extra["subtype"] = ev.get("subtype")
                u.session_id = ev.get("session_id", u.session_id)
        if not u.final_text and not u.cost_usd:
            # Killed before the result line: fall back to what the stream showed.
            u.final_text = last_text
            u.input_tokens, u.output_tokens = est_in, est_out
            u.cache_read_tokens, u.cache_write_tokens = est_cr, est_cw
            u.estimated = True
        for ev in self._events(output_path):
            if ev.get("type") == "assistant" and ev.get("error") == "authentication_failed":
                u.auth_failed = True
        blob = u.final_text + " " + u.error
        if stderr_path and Path(stderr_path).exists():
            blob += " " + Path(stderr_path).read_text(errors="replace")[-2000:]
        if not u.cost_usd and AUTH_RE.search(blob):
            u.auth_failed = True
        if LIMIT_RE.search(blob) and (u.error or not u.cost_usd) and not u.auth_failed:
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        return u

    def account(self) -> str:
        try:
            data = json.loads(Path(os.path.expanduser("~/.claude.json")).read_text())
        except (OSError, ValueError):
            return ""
        acct = data.get("oauthAccount") or {}
        who = acct.get("emailAddress") or ""
        org = acct.get("organizationName") or ""
        bill = acct.get("billingType") or data.get("billingType") or ""
        return " | ".join(x for x in (who, org if org and who not in org else "", bill) if x)


def windows_from_event(ev: dict) -> list[dict]:
    info = ev.get("rate_limit_info") or {}
    out = []
    for name, w in (info.get("unifiedWindows") or {}).items():
        util = w.get("utilization")
        if util is None:
            continue
        util = float(util)
        out.append({"window": name, "utilization": util * 100 if util <= 1.0 else util,
                    "resets_at": w.get("resetsAt")})
    return out


def as_windows(extra_windows: list[dict], account: str) -> list[Window]:
    return [Window("claude", w["window"], w["utilization"], w.get("resets_at"), account) for w in extra_windows]

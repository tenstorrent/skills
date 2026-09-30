# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code (`claude -p`). Every run streams JSON: a `rate_limit_event` carrying the account's
plan windows (utilization as a 0..1 fraction plus reset times), per-message usage, and a final
`result` with the reported cost, the last message and any schema-validated output."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import register
from ..budget import Window
from .base import AUTH_RE, LIMIT_RE, Provider, RunUsage

EXCLUDE_DYNAMIC = "--exclude-dynamic-system-prompt-sections"
_FLAGS: dict[str, bool] = {}   # CLI flag support, probed once per daemon from `claude --help`
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@register
class Claude(Provider):
    name = "claude"
    binaries = ("claude",)
    login_hint = "run `claude` there and use /login"

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
        if not read_only:
            argv += ["--settings", json.dumps(hook_settings())]
            # Workers start in many different worktrees; with the per-directory sections out of the
            # system prompt, it stays cached across them (measured: ~30% fewer cache-write tokens).
            if self.supports(EXCLUDE_DYNAMIC):
                argv.append(EXCLUDE_DYNAMIC)
        env = {"CLAUDE_CODE_ENABLE_CFC": "0"}
        return argv, env

    def supports(self, flag: str) -> bool:
        if flag not in _FLAGS:
            try:
                out = subprocess.run([self.binary() or "claude", "--help"], capture_output=True, text=True,
                                     timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            _FLAGS[flag] = flag in out
        return _FLAGS[flag]

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

    def plugin_args(self, dirs: list[str]) -> list[str]:
        out: list[str] = []
        for d in dirs:
            out += ["--plugin-dir", d]
        return out

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage()
        last_text = ""
        saw_result = False
        rejected = ""
        # One API message streams as several events (one per content block), each repeating its
        # usage: count every message id once, at its highest reading.
        per_msg: dict[str, dict[str, int]] = {}
        for n, ev in enumerate(self._events(output_path)):
            t = ev.get("type")
            if t == "rate_limit_event":
                u.extra["windows"] = windows_from_event(ev)
                info = ev.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    rejected = f"rate limit rejected: {info.get('rateLimitType') or 'unknown'}"
            elif t == "assistant":
                msg = ev.get("message") or {}
                us = msg.get("usage") or {}
                seen = per_msg.setdefault(str(msg.get("id") or f"event-{n}"), {})
                for k in USAGE_KEYS:
                    seen[k] = max(seen.get(k, 0), int(us.get(k) or 0))
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_text = block.get("text") or last_text
            elif t == "system" and ev.get("subtype") == "init":
                u.session_id = ev.get("session_id", "")
                u.extra["model"] = ev.get("model", "")
            elif t == "result":
                saw_result = True
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
        if not saw_result:
            # Ended before the result line (killed, lost): the tokens the stream showed are all there
            # is. The cost is left to the daemon, which prices them at this project's observed rate.
            u.final_text = last_text
            u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens = (
                sum(m[k] for m in per_msg.values()) for k in USAGE_KEYS)
            u.estimated = True
        for ev in self._events(output_path):
            if ev.get("type") == "assistant" and ev.get("error") == "authentication_failed":
                u.auth_failed = True
        stderr = ""
        if stderr_path and Path(stderr_path).exists():
            stderr = Path(stderr_path).read_text(errors="replace")[-2000:]
        # Not final_text: a killed worker's last message may just be discussing a 401 or a rate limit.
        blob = u.error + " " + stderr
        if not u.cost_usd and AUTH_RE.search(blob):
            u.auth_failed = True
        hit = LIMIT_RE.search(blob)
        if (rejected or hit) and (u.error or not u.cost_usd) and not u.auth_failed:
            u.limited, u.limit_note = True, rejected or hit.group(0)
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


def hook_settings() -> dict:
    """Route tool-use events through `ttp.hook`, so coordinator updates reach a running worker.

    Passed as JSON on the command line: nothing is written to the user's settings files, and one
    project never changes another's behavior.
    """
    runtime = str(Path(__file__).resolve().parents[2])
    cmd = f"{shlex.quote(sys.executable)} -m ttp.hook PostToolUse"
    return {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd,
                                                                  "timeout": 20}]}]},
            "env": {"PYTHONPATH": runtime}}


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

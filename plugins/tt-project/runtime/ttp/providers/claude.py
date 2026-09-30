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
APPEND_SYSTEM = "--append-system-prompt"
APPEND_SYSTEM_FILE = "--append-system-prompt[-file]"   # how `--help` names the (unlisted) file form
_FLAGS: dict[str, bool] = {}   # CLI flag support, probed once per daemon from `claude --help`
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# Claude Code's bundled skills, which workers have never used: listed by name only (still callable),
# which saves ~2.1k tokens a call. "off" and disableBundledSkills both add tokens instead.
BUNDLED_SKILLS = ("dataviz", "update-config", "keybindings-help", "code-review", "simplify",
                  "fewer-permission-prompts", "loop", "schedule", "claude-api", "workflow-authoring", "run",
                  "init", "security-review")
# Scheduling, remote and orchestration tools a headless worker must not use; their definitions
# alone are ~3.7k tokens a call.
WORKER_DENIED_TOOLS = ("Workflow", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger",
                       "PushNotification", "DesignSync")


@register
class Claude(Provider):
    name = "claude"
    binaries = ("claude",)
    login_hint = "run `claude` there and use /login"

    def credential_files(self) -> list[str]:
        return [str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")) / ".credentials.json")]

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
        if not read_only:
            denied = list(WORKER_DENIED_TOOLS)
            if restrictions.get("no_internet"):
                denied += ["WebFetch", "WebSearch"]
            argv += ["--disallowedTools", *denied]
            argv += ["--settings", json.dumps(worker_settings())]
            # Workers start in many different worktrees; with the per-directory sections out of the
            # system prompt, it stays cached across them (measured: ~30% fewer cache-write tokens).
            if self.supports(EXCLUDE_DYNAMIC):
                argv.append(EXCLUDE_DYNAMIC)
        # A headless run kills its background tasks when it exits, so a job started that way dies
        # with the worker and the run ends with no hand-off. Without them, long jobs are detached.
        env = {"CLAUDE_CODE_ENABLE_CFC": "0", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
        return argv, env

    def compact_env(self, tokens: int) -> dict[str, str]:
        # Honoured by headless stream-json runs (checked live: a `compact_boundary` event follows).
        # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE alone did not compact there.
        return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(int(tokens))} if tokens and tokens > 0 else {}

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

    def append_system_args(self, path: Path) -> list[str]:
        if self.supports(APPEND_SYSTEM_FILE):
            return ["--append-system-prompt-file", str(path)]
        if self.supports(APPEND_SYSTEM):
            return [APPEND_SYSTEM, Path(path).read_text()]
        return []

    def isolation_args(self) -> list[str]:
        # Flag settings (the harness hook) and --plugin-dir load whatever the sources are.
        return ["--strict-mcp-config", "--setting-sources", "project,local"]

    def mcp_servers(self, names: list[str], dirs: list[str]) -> tuple[dict, list[str]]:
        # Claude Code's own scopes, most specific first: local (per directory, in the user's
        # config), project (`.mcp.json`), user. `claude mcp get` has no machine-readable output.
        user_cfg = _read_json(claude_config_path())
        scopes = [((user_cfg.get("projects") or {}).get(d) or {}).get("mcpServers") for d in dirs]
        scopes += [_read_json(Path(d) / ".mcp.json").get("mcpServers") for d in dirs]
        scopes.append(user_cfg.get("mcpServers"))
        found: dict = {}
        for name in names:
            for scope in scopes:
                if isinstance(scope, dict) and isinstance(scope.get(name), dict):
                    found[name] = scope[name]
                    break
        return found, [n for n in names if n not in found]

    def with_mcp_config(self, argv: list[str], path: Path) -> list[str]:
        # --mcp-config takes several values, so a flag must follow it, never a positional argument.
        i = argv.index("--strict-mcp-config") if "--strict-mcp-config" in argv else len(argv)
        return argv[:i] + ["--mcp-config", str(path)] + argv[i:]

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


def claude_config_path() -> Path:
    """Where Claude Code keeps user- and local-scope MCP servers."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() / ".claude.json" if base else Path.home() / ".claude.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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


def worker_settings() -> dict:
    """The hook, plus a leaner context: bundled skills by name only, no auto-memory (the project
    keeps its own). Skills from a project's plugin dirs stay fully listed."""
    return {**hook_settings(), "skillOverrides": {name: "name-only" for name in BUNDLED_SKILLS},
            "autoMemoryEnabled": False}


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

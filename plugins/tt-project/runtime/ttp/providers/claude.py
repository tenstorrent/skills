# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Claude Code (`claude -p`). Every run streams JSON: a `rate_limit_event` carrying the account's
plan windows (utilization as a 0..1 fraction plus reset times), per-message usage, and a final
`result` with the reported cost, the last message and any schema-validated output.

Write fence (probed on CLI 2.1.285; Linux measured, macOS from the docs). Workers run with
bypassPermissions and no OS sandbox, so they are unfenced today.

| Question | Linux | macOS |
| :- | :- | :- |
| Fence Bash writes to a list of dirs | yes, `sandbox.enabled` + `sandbox.filesystem.allowWrite`; needs `bubblewrap` and `socat` | yes, Seatbelt, nothing to install |
| Sandbox missing a dependency | runs unsandboxed with a warning (measured); `sandbox.failIfUnavailable` refuses to start instead (measured, exit 1) | n/a |
| `allowUnsandboxedCommands: false` | drops the `dangerouslyDisableSandbox` retry (docs); no help when the sandbox could not start (measured) | same (docs) |
| Edit deny rules under bypassPermissions | hold for Write/Edit and plain shell redirects and `cp` (measured), but a write from `python3 -c` passed (measured): a check, not a fence | same rules (docs) |
| Unix socket under `state/` from the sandbox | allowed unless the optional seccomp filter is installed, then `network.allowAllUnixSockets` (docs) | only paths in `network.allowUnixSockets` (docs) |

File tools (Read, Edit, Write) run outside the sandbox and follow permission rules only, so a fence
needs both the sandbox and Edit deny rules. `--setting-sources` drops a source's sandbox and Edit
entries; `--settings` keeps them.

Credential reads: a worker's `--settings` denies `Read(//<project>/tt-project/state/web.token)`
(worker_settings); under bypassPermissions it refused both the Read tool and a plain `cat` of the
file (measured, CLI 2.1.296). The Bash hook also refuses a command naming `state/web.token`. Like
the Edit rules (a `python3 -c` write passed those), a check and not a fence; reads from an
interpreter were not probed."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import register
from ..budget import Window
from .base import (AUTH_RE, LIMIT_RE, MAX_ARG_BYTES, MAX_CACHE_BREAKPOINTS, Provider, RunUsage, cap_cache_breakpoints,
                   cli_output, find_binary, request_rejected, status_check, strict_schema, drop_nulls)

EXCLUDE_DYNAMIC = "--exclude-dynamic-system-prompt-sections"
APPEND_SYSTEM = "--append-system-prompt"
APPEND_SYSTEM_FILE = "--append-system-prompt[-file]"   # how `--help` names the (unlisted) file form
SYSTEM_PROMPT = "--system-prompt"
SYSTEM_PROMPT_FILE = "--system-prompt[-file]"   # likewise for the replacing flag
INPUT_FORMAT = "--input-format"
# The prompt cache lifetimes the CLI takes in CLAUDE_CODE_PROMPT_CACHE_TTL. Unset, it picks 1 h on a
# plan within its limits and 5 min otherwise (API key, or a plan past its limits).
CACHE_TTLS = ("5m", "1h")
_FLAGS: dict[str, bool] = {}   # CLI flag support, probed once per daemon from `claude --help`
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# Claude Code's bundled skills, which workers have never used: listed by name only (still callable),
# which saves ~2.1k tokens a call. "off" and disableBundledSkills both add tokens instead. Then the
# skills of its built-in plugins, and the Anthropic skills a claude.ai account brings: by name only
# they cut a worker's first call by another ~2.1k tokens (CLI 2.1.296). No worker called Skill in
# the 14 days to 2026-10-24. Plugin skills (tt-project's own, a project's plugin_dirs) stay listed.
# --disable-slash-commands is no way out: it drops the Skill tool, and every plugin skill with it.
BUNDLED_SKILLS = ("dataviz", "update-config", "keybindings-help", "code-review", "simplify",
                  "fewer-permission-prompts", "loop", "schedule", "claude-api", "workflow-authoring", "run",
                  "init", "security-review", "deep-research", "design", "design-sync", "verify", "debug",
                  "batch", "doctor", "run-skill-generator", "plugin-authoring",
                  *(f"anthropic-skills:{name}" for name in ("docs", "docx", "google-workspace", "import-memory",
                                                            "morning", "pdf", "pptx", "skill-creator", "xlsx")))
# What a worker or reviewer may use, passed as --tools: the built-in tools workers called in the
# 14 days to 2026-10-24 (~4,500 runs: Bash, Read, Edit, Write, the web tools in 8 runs; never Monitor,
# TaskStop, NotebookEdit, subagents or scheduling), Skill for the plugin skills, and ToolSearch, which
# loads MCP tools on demand: without it every MCP tool's full schema is sent on every call (measured:
# 13.7k -> 28.3k tokens with three user servers). With nothing to defer it added no tokens
# (measured), so it costs a worker without MCP servers nothing. MCP tools are not built-in, so --tools never
# drops them; StructuredOutput comes with --json-schema. The allowlist replaces the old denylist
# (WORKER_DENIED_TOOLS, kept for a CLI without --tools) and cuts ~0.6k tokens a call.
TOOLS = "--tools"
# The user's claude.ai connectors load in every run that is not isolated (--strict-mcp-config drops them
# too): their deferred tool names and instructions, also for those that need a login. Measured on the
# first call of a 'reply OK' worker run (CLI 2.1.296, 17 connectors, 1 logged in): 16.5k tokens -> 14.5k
# with ENABLE_CLAUDEAI_MCP_SERVERS=false, which keeps the user's, project's and plugins' own servers.
# In the 14 days to 2026-10-24 no worker of three projects (~6,700 runs) called a connector's tool.
CONNECTORS_OFF = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
CONNECTOR_PREFIXES = ("claude.ai", "claude_ai_", "mcp__claude_ai_")   # server name, its tools' prefix
WORKER_TOOLS = ("Bash", "Read", "Edit", "Write", "Skill", "ToolSearch")
WEB_TOOLS = ("WebFetch", "WebSearch")
# Scheduling, remote and orchestration tools a headless worker must not use; their definitions
# alone are ~3.7k tokens a call. Then tools no worker used in ~1,000 runs, or that would step outside
# the harness (its own worktrees, other sessions). Measured on the first call of a 'reply OK' worker
# run, CLI 2.1.285: 10.6k tokens -> 9.3k without the notebook/worktree/listing/findings tools, 7.9k
# also without subagents ("Task" in the init event, "Agent" in tool calls).
WORKER_DENIED_TOOLS = ("Workflow", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger",
                       "PushNotification", "DesignSync", "NotebookEdit", "EnterWorktree", "ExitWorktree",
                       "ListAgents", "ReportFindings", "Task", "Agent", "SendMessage")


@register
class Claude(Provider):
    name = "claude"
    binaries = ("claude",)
    api_host, api_base_env = "api.anthropic.com", "ANTHROPIC_BASE_URL"
    # Measured (CLI 2.1.285, its system prompt and latest messages): three on a run's first call and
    # four on every later one (after a tool result or a schema retry). A user block's own mark on top
    # made those later calls fail with a 400 (found 5), losing the turn.
    # CLI 2.1.296 used three; giving the fourth to that mark (charter and memory as their own block)
    # was tried and measured: coordinator cost per turn rose and the hit rate barely moved. So all
    # four stay reserved and the stable part ends the system prompt.
    own_cache_breakpoints = 4
    login_hint = "run `claude` there and use /login"
    efforts = ("low", "medium", "high", "xhigh", "max")
    steer_hook = True   # worker_settings() routes PostToolUse through ttp.hook

    def credential_files(self) -> list[str]:
        return [str(claude_config_dir() / ".credentials.json")]

    def login_check(self) -> bool | None:
        b = self.binary()
        if not b or "status" not in cli_output(b, "auth", "--help"):
            return None   # a CLI without `claude auth status`
        got = status_check([b, "auth", "status", "--json"])
        if got is None:
            return None
        rc, text = got
        try:
            return bool(json.loads(text[text.index("{"):text.rindex("}") + 1]).get("loggedIn"))
        except ValueError:
            return rc == 0

    caps_budget = True   # --max-budget-usd

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions,
              disallowed_tools=(), denied_reads=(), web=True):
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
            argv += ["--json-schema", json.dumps(strict_schema(schema))]
        if not read_only:
            web = web and not restrictions.get("no_internet")
            if self.supports(TOOLS):
                argv += [TOOLS, ",".join([*WORKER_TOOLS, *(WEB_TOOLS if web else ())])]
                denied = []
            else:
                denied = list(WORKER_DENIED_TOOLS) + ([] if web else list(WEB_TOOLS))
            denied += [t for t in disallowed_tools if t not in denied]   # the project's own denylist
            if denied:
                argv += ["--disallowedTools", *denied]
            argv += ["--settings", json.dumps(worker_settings(denied_reads))]
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
        # CLAUDE_AUTOCOMPACT_PCT_OVERRIDE alone did not compact there. The CLI clamps the value to
        # 100000-1000000 (https://code.claude.com/docs/en/env-vars); it is clamped here too, so the
        # run's recorded environment shows the window that applies.
        if not tokens or tokens <= 0:
            return {}
        return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(min(max(int(tokens), 100_000), 1_000_000))}

    def cap_output(self, argv: list[str], chars: int) -> list[str]:
        # `bashOutputMaxChars` (CLI 2.1.285): longer output of a successful command is saved to a file
        # and the model gets a short preview plus the path. The CLI clamps it to 4000-128000.
        if not chars or chars <= 0 or "--settings" not in argv:
            return argv
        i = argv.index("--settings") + 1
        try:
            settings = json.loads(argv[i])
        except (IndexError, ValueError):
            return argv
        settings["bashOutputMaxChars"] = min(max(int(chars), 4000), 128000)
        return [*argv[:i], json.dumps(settings), *argv[i + 1:]]

    def cache_env(self, ttl: str) -> dict[str, str]:
        return {"CLAUDE_CODE_PROMPT_CACHE_TTL": ttl} if ttl in CACHE_TTLS else {}

    def cached_input(self, stable: str, rest: str, ttl: str, log=None) -> tuple[list[str], str] | None:
        # The CLI passes a user block's own cache_control through (checked live, CLI 2.1.285: a second
        # call that changed only the last block read the first from the cache; without the mark it
        # missed). Its lifetime must match the CLI's (a longer one may not follow a shorter one), so it
        # is only set together with cache_env(ttl). The mark counts against own_cache_breakpoints.
        if (ttl not in CACHE_TTLS or self.own_cache_breakpoints >= MAX_CACHE_BREAKPOINTS
                or not self.supports(INPUT_FORMAT)):
            return None
        blocks = cap_cache_breakpoints(
            [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral", "ttl": ttl}},
             {"type": "text", "text": rest}],
            MAX_CACHE_BREAKPOINTS - self.own_cache_breakpoints, source="claude cached input", log=log)
        if not any(isinstance(b, dict) and "cache_control" in b for b in blocks):
            return None   # no breakpoint to spare: `stable` ends the system prompt, under the CLI's own
        message = {"type": "user", "message": {"role": "user", "content": blocks}}
        return [INPUT_FORMAT, "stream-json"], json.dumps(message) + "\n"

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
        return self._inline(APPEND_SYSTEM, path) if self.supports(APPEND_SYSTEM) else []

    def system_prompt_args(self, path: Path) -> list[str]:
        """Arguments that make the text in `path` the whole system prompt; [] when it cannot go on
        the command line, and the text then leads the prompt."""
        if self.supports(SYSTEM_PROMPT_FILE):
            return ["--system-prompt-file", str(path)]
        return self._inline(SYSTEM_PROMPT, path)

    @staticmethod
    def _inline(flag: str, path: Path) -> list[str]:
        # A CLI without the file flag takes the text as one argument. Linux refuses one longer than
        # 128 KiB (E2BIG: the agent never starts), so longer text leads the prompt instead.
        text = Path(path).read_text()
        return [flag, text] if len(text.encode()) <= MAX_ARG_BYTES else []

    def write_fence(self) -> str:
        gap = "bypassPermissions without the Bash sandbox"
        if sys.platform.startswith("linux"):
            missing = [b for b in ("bwrap", "socat") if not find_binary(b)]
            gap += f"; the sandbox would need {' and '.join(missing)}" if missing else ""
        return gap

    def isolation_args(self) -> list[str]:
        # Flag settings (the harness hook) and --plugin-dir load whatever the sources are.
        return ["--strict-mcp-config", "--setting-sources", "project,local"]

    def keep_connectors(self, argv: list[str], deny: list[str]) -> list[str] | None:
        # Not strict, so the account's connectors load; every server in `deny` is kept out twice:
        # deniedMcpServers in the run's flag settings stops it starting at all, and a whole-server
        # --disallowedTools rule (mcp__<server>) hides its tools (both checked live, CLI 2.1.296).
        argv = list(argv)
        tail = ["-"] if argv[-1:] == ["-"] else []
        argv = argv[:len(argv) - len(tail)]
        if "--settings" in argv:
            i = argv.index("--settings") + 1
            try:
                settings = json.loads(argv[i])
            except ValueError:
                return None   # a settings file, not JSON: stay strict rather than lose either
        else:
            settings, i = {}, None
        if deny:
            settings = {**settings, "deniedMcpServers": [*(settings.get("deniedMcpServers") or []),
                                                         *({"serverName": n} for n in deny)]}
        if i is None:
            argv += ["--settings", json.dumps(settings)]
        else:
            argv[i] = json.dumps(settings)
        from ..mcpgap import tool_name
        if deny:   # one comma-separated value; --disallowedTools takes several, so a flag follows it
            argv += ["--disallowedTools", ",".join(dict.fromkeys(f"mcp__{tool_name(n)}" for n in deny))]
        return argv + ["--setting-sources", "project,local"] + tail

    def mcp_server_names(self, dirs: list[str]) -> list[str]:
        return list(dict.fromkeys(n for scope in self._scopes(dirs) if isinstance(scope, dict) for n in scope))

    def _scopes(self, dirs: list[str]) -> list:
        # Claude Code's own scopes, most specific first: local (per directory, in the user's
        # config), project (`.mcp.json`), user. `claude mcp get` has no machine-readable output.
        user_cfg = _read_json(claude_config_path())
        scopes = [((user_cfg.get("projects") or {}).get(d) or {}).get("mcpServers") for d in dirs]
        scopes += [_read_json(Path(d) / ".mcp.json").get("mcpServers") for d in dirs]
        scopes.append(user_cfg.get("mcpServers"))
        return scopes

    def mcp_servers(self, names: list[str], dirs: list[str]) -> tuple[dict, list[str]]:
        scopes = self._scopes(dirs)
        found: dict = {}
        for name in names:
            for scope in scopes:
                if isinstance(scope, dict) and isinstance(scope.get(name), dict):
                    found[name] = scope[name]
                    break
        return found, [n for n in names if n not in found]

    def connector_env(self, keep: bool) -> dict[str, str]:
        return {} if keep else dict(CONNECTORS_OFF)

    def is_connector(self, name: str) -> bool:
        return str(name).lower().startswith(CONNECTOR_PREFIXES)

    def with_mcp_config(self, argv: list[str], path: Path) -> list[str]:
        # --mcp-config takes several values, so a flag must follow it, never a positional argument.
        # A run that keeps connectors (keep_connectors) has --setting-sources in place of the strict flag.
        flag = next((f for f in ("--strict-mcp-config", "--setting-sources") if f in argv), None)
        i = argv.index(flag) if flag else len(argv)
        return argv[:i] + ["--mcp-config", str(path)] + argv[i:]

    def session_args(self, session_id: str) -> list[str]:
        return ["--session-id", session_id] if session_id and self.supports("--session-id") else []

    def resume_args(self, session_id: str) -> list[str]:
        return ["--resume", session_id] if session_id and self.supports("--resume") else []

    def session_saved(self, session_id: str, cwd: str, env: dict | None = None) -> bool:
        # Transcripts live per working directory, under the directory's path with every character
        # but letters and digits as "-" (long paths are shortened, so look in the others too). A run
        # given its own CLAUDE_CONFIG_DIR (another account) kept them there, not in the daemon's.
        name = f"{session_id}.jsonl"
        if not session_id or "/" in session_id:
            return False
        own = (env or {}).get("CLAUDE_CONFIG_DIR")
        for base in dict.fromkeys(([Path(own).expanduser()] if own else []) + [claude_config_dir()]):
            projects = base / "projects"
            if (projects / re.sub(r"[^a-zA-Z0-9]", "-", cwd) / name).is_file() or any(projects.glob(f"*/{name}")):
                return True
        return False

    def session_paths(self, session_id: str, cwd: str, env: dict | None = None) -> list[Path]:
        if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return []
        own = (env or {}).get("CLAUDE_CONFIG_DIR")
        out: list[Path] = []
        for base in dict.fromkeys(([Path(own).expanduser()] if own else []) + [claude_config_dir()]):
            projects = base / "projects"
            out += [*projects.glob(f"*/{session_id}.jsonl"), *projects.glob(f"*/{session_id}/**/*.jsonl")]
        return out

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
        typed: list[str] = []   # assistant `error` events and a failed result's errors and message
        # One API message streams as several events (one per content block), each repeating its
        # usage: count every message id once, at its highest reading.
        per_msg: dict[str, dict[str, int]] = {}
        reported: dict[str, set[str]] = {}
        cache_keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        cache_complete = False
        for n, ev in enumerate(self._events(output_path)):
            t = ev.get("type")
            if t == "rate_limit_event":
                u.extra["windows"] = windows_from_event(ev)
                u.extra["credits"] = credits_from_event(ev)
                info = ev.get("rate_limit_info") or {}
                if info.get("status") == "rejected":
                    rejected = f"rate limit rejected: {info.get('rateLimitType') or 'unknown'}"
            elif t == "assistant":
                msg = ev.get("message") or {}
                if ev.get("error"):
                    typed.append(f"{ev['error']}: " + " ".join(str(b.get("text") or "") for b in msg.get("content") or []
                                                         if isinstance(b, dict) and b.get("type") == "text"))
                us = msg.get("usage") or {}
                ident = str(msg.get("id") or f"event-{n}")
                seen = per_msg.setdefault(ident, {})
                reported.setdefault(ident, set()).update(k for k in cache_keys
                                                        if isinstance(us.get(k), int) and us[k] >= 0)
                for k in USAGE_KEYS:
                    seen[k] = max(seen.get(k, 0), int(us.get(k) or 0))
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        last_text = block.get("text") or last_text
                    elif isinstance(block, dict) and block.get("type") == "tool_use" \
                            and self.is_connector(str(block.get("name") or "")):
                        # Its workers then keep the connectors (Daemon._connectors_needed).
                        used = u.extra.setdefault("connectors_used", [])
                        name = str(block["name"]).split("__")[1]
                        if name not in used:
                            used.append(name)
            elif t == "system" and ev.get("subtype") == "init":
                u.session_id = ev.get("session_id", "")
                u.extra["model"] = ev.get("model", "")
            elif t == "result":
                saw_result = True
                usage = ev.get("usage") or {}
                cache_complete = all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in cache_keys)
                u.cost_usd = float(ev.get("total_cost_usd") or 0.0)
                u.input_tokens = int(usage.get("input_tokens") or 0)
                u.output_tokens = int(usage.get("output_tokens") or 0)
                u.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
                u.cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
                u.final_text = ev.get("result") if isinstance(ev.get("result"), str) else json.dumps(ev.get("result"))
                u.structured = drop_nulls(ev.get("structured_output"))
                if ev.get("is_error"):
                    u.error = f"{ev.get('subtype')}: {str(ev.get('result'))[:300]}"
                    typed += [str(e) for e in ev.get("errors") or []] + [str(ev.get("result") or "")]
                else:
                    typed = []   # the CLI got past the errors it streamed
                u.extra["subtype"] = ev.get("subtype")
                u.session_id = ev.get("session_id", u.session_id)
        if not saw_result:
            # Ended before the result line (killed, lost): the tokens the stream showed are all there
            # is. The cost is left to the daemon, which prices them at this project's observed rate.
            u.final_text = last_text
            u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens = (
                sum(m[k] for m in per_msg.values()) for k in USAGE_KEYS)
            u.estimated = True
            cache_complete = bool(per_msg) and all(set(cache_keys) <= keys for keys in reported.values())
        # CLI turns/message IDs are not HTTP request counts (retries can be hidden).
        # Keep missing request metadata unknown rather than inventing a denominator.
        u.extra["cache_telemetry"] = {"complete": cache_complete, "requests": None,
                                      "source": "result" if saw_result else "messages"}
        # API messages and the first one's prompt (the prefix every later call re-reads): `ttp stats`
        prompts = [t for t in (sum(m.get(k, 0) for k in cache_keys) for m in per_msg.values()) if t]
        u.extra["cache_telemetry"].update(calls=len(prompts), first_context=prompts[0] if prompts else 0)
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
        # Only a run that failed and spent nothing: a priced run's error was not the request.
        cause = next((t for t in typed if request_rejected(t)), "")
        if cause and (u.error or not saw_result) and not (u.cost_usd or u.output_tokens or u.auth_failed or u.limited):
            u.request_error, u.error = True, cause[:400]
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

    def account_kind(self) -> str:
        try:
            data = json.loads(Path(os.path.expanduser("~/.claude.json")).read_text())
        except (OSError, ValueError):
            return ""
        return str((data.get("oauthAccount") or {}).get("billingType") or data.get("billingType") or "")


def claude_config_dir() -> Path:
    """Where Claude Code keeps credentials and session transcripts."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


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
    """Route tool-use events through `ttp.hook`, so coordinator updates reach a running worker and
    Bash calls cannot get around the PR draft guard.

    Passed as JSON on the command line: nothing is written to the user's settings files, and one
    project never changes another's behavior.
    """
    runtime = str(Path(__file__).resolve().parents[2])
    cmd = f"{shlex.quote(sys.executable)} -m ttp.hook"
    return {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{cmd} PostToolUse",
                                                                  "timeout": 20}]}],
                      # Denies Bash calls that would get around the harness's PR draft guard.
                      "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"{cmd} PreToolUse",
                                                                    "timeout": 20}]}]},
            "env": {"PYTHONPATH": runtime}}


def worker_settings(denied_reads=()) -> dict:
    """The hook, plus a leaner context: bundled skills by name only, no auto-memory (the project
    keeps its own). Skills from a project's plugin dirs stay fully listed. Each absolute path in
    `denied_reads` gets a Read deny rule (`//` starts an absolute path in a rule); deny rules hold
    under bypassPermissions."""
    out = {**hook_settings(), "skillOverrides": {name: "name-only" for name in BUNDLED_SKILLS},
           "autoMemoryEnabled": False}
    if denied_reads:
        out["permissions"] = {"deny": [f"Read(/{p})" for p in denied_reads if os.path.isabs(p)]}
    return out


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


def credits_from_event(ev: dict) -> dict:
    """The event's overage fields as a credits.parse reading: paid credits are open only when
    `overageStatus` is 'allowed', closed for any other status or a disabled reason, and unknown
    (None) when the event carries neither."""
    from ..credits import parse
    info = ev.get("rate_limit_info") or {}
    status, reason = info.get("overageStatus"), info.get("overageDisabledReason")
    has = None
    if isinstance(status, str) and status:
        has = status == "allowed" and not reason
    elif isinstance(reason, str) and reason:
        has = False
    reading = parse({"credits": {"hasCredits": has}})
    reading["overage"] = {"status": status if isinstance(status, str) else None,
                          "using": info.get("isUsingOverage") if isinstance(info.get("isUsingOverage"), bool) else None,
                          "disabled_reason": reason if isinstance(reason, str) else None}
    return reading


def as_windows(extra_windows: list[dict], account: str) -> list[Window]:
    return [Window("claude", w["window"], w["utilization"], w.get("resets_at"), account) for w in extra_windows]

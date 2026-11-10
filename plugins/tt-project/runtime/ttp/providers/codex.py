# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI Codex CLI (`codex exec --json`). Codex reports tokens but no cost, has no budget flag,
and exposes plan windows through `codex app-server` (`account/rateLimits/read`, no model tokens).
Cost is therefore an estimate from a price table the project can edit. `codex exec --json` prints
tokens only when a turn completes (one turn per run), so the runner's mid-run budget check and a
cut-off run's price read the running totals in the session's rollout (token_count events).
The same events carry the account's plan windows (`rate_limits`), so a running run reports them
with each model call; the app-server meter, read every few minutes, covers the time between.

Write fence (from the docs; Codex was not installed where this was probed). Workers run in
`danger-full-access` by default: no sandbox. A project that sets `providers.codex.write_fence: true`, or
has the no_internet restriction, runs them in `workspace-write` (fence_args): they write their cwd, temp
dirs and `writable_args` roots. Coordinator turns likewise drop `-s read-only` for `danger-full-access`
unless the same key is on (read_only_args); the daemon applies their output only through its action gates
and logs a turn that changed files (coordwrites).

| Question | Linux | macOS |
| :- | :- | :- |
| Fence writes to a list of dirs | yes, `sandbox_workspace_write.writable_roots`; uses `bwrap` from PATH, else a bundled helper that needs unprivileged user namespaces (startup warning if it can't) | yes, Seatbelt |
| Unix socket under `state/` from the sandbox | blocked by default; allow it with `permissions.<name>.network.unix_sockets` or `dangerously_allow_all_unix_sockets` | same |

Reads are not fenced, and the roots today include all of the project's state. Codex has no read
deny rules, so the web token is guarded only by the Bash hook (when it runs) refusing a command
that names `state/web.token`.

Harness hook (measured on codex-cli 0.160). Codex sends the same PreToolUse/PostToolUse payload as
Claude Code (`tool_name` "Bash", `tool_input.command`) and takes the same deny and added-context
replies. File edits come as `tool_name` "apply_patch" with the patch text in `tool_input.command`, MCP
calls as `mcp__<server>__<tool>` with the tool's arguments, and the end of a turn as a Stop event
(`stop_hook_active`, no tool) that takes Claude Code's `{"decision": "block", "reason": ...}` and goes
on with the reason as the next prompt. So workers get `ttp.hook` through `-c hooks.*` keys, never a file under `~/.codex`. Codex
runs a hook only once its hash is trusted, and silently skips a `-c` hook that is not, so the hook
needs `--dangerously-bypass-hook-trust`. That flag trusts every hook of the run, so it is passed
only when no other hook source exists (hook_sources); otherwise the worker gets coordinator updates
from `ttp note` and `ttp steer` (cli.print_update), which leave them to the hook in a run where it runs.

Per-run plugins: not possible. Codex loads a plugin only from its install cache under
`$CODEX_HOME/plugins/cache` (`codex plugin add` copies it there); enabling a local marketplace's
plugin with `-c` alone loaded none of its skills (measured), and no `-c` key adds a skill folder.
An installed plugin does load from a whole `-c plugins={...}` table, also in a run that ignores
config.toml (measured on 0.160): isolated workers get tt-project's own skills that way
(own_plugin_args)."""
from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import register
from .claude import STOP_HOOK_TIMEOUT_S   # the same allowance for the hand-off checks at a stop
from .base import (AUTH_RE, LIMIT_RE, MAX_ARG_BYTES, Provider, RunUsage, cli_output, price_row, request_rejected,
                   status_check, stderr_tail, strict_schema, drop_nulls, head80, input_hash, isolated_module_argv, jsonl_from,
                   CMD_MAX)

# $ per million tokens: (input, cached input, output). Estimates only; the project may override
# them in project.json under pricing.codex.<model>. Unknown models use the "default" row.
PRICES = {"default": (4.0, 0.4, 20.0)}
# A rollout's plan-window reading stands in for the meter's only this fresh (seconds).
ROLLOUT_WINDOWS_FRESH_S = 180
# The run's developer instructions are looked for in this many of its rollout's first lines, by
# this long a prefix (characters) of the text the run passed.
ROLLOUT_HEAD_LINES = 200
INSTRUCTIONS_PREFIX = 4096
# Rollout items only a model reply writes: once one is there, the first request's context is complete.
MODEL_ITEMS = ("reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call")
# Tools a decision-only turn does without, switched off when this build lists them as features.
READ_ONLY_OFF = ("shell_tool", "unified_exec", "web_search_request")
HOOK_TRUST_BYPASS = "--dangerously-bypass-hook-trust"
IGNORE_USER_CONFIG = "--ignore-user-config"
IGNORE_RULES = "--ignore-rules"
# A config.toml line that may define a hook: a hooks table or key, or a plugin (plugins bundle hooks).
# Only the fallback where no TOML parser loads or the file does not parse (see config_has_hooks).
HOOK_CONFIG_RE = re.compile(r"^\s*(?:\[\[?\s*[\"']?(?:hooks|plugins)\b|[\"']?(?:hooks|plugins)[\"']?\s*[.=])", re.M)
# User config the run cannot reach its model without (its provider, endpoint or login): ignoring it
# would break every run.
PROVIDER_CONFIG_RE = re.compile(r"^\s*(?:\[\s*)?[\"']?(?:model_providers?|cli_auth_credentials_store|forced_login_method|"
                                r"openai_base_url|chatgpt_base_url)\b", re.M)
# A command the harness's hook denied (logged by codex_core::tools::router): its text is the
# command's, so it says nothing about the run's login or limits.
HOOK_DENIAL_RE = re.compile(r"^.*Command blocked by PreToolUse hook.*$\n?", re.M)
# This plugin's name in Codex's install cache (`plugins/cache/<marketplace>/<name>/<version>/`).
OWN_PLUGIN = "tt-project"
# What a plugin may bring besides skills: an isolated run keeps out a copy that brings any of these.
PLUGIN_EXTRAS = ("hooks", "mcpServers", "apps")
PLUGIN_EXTRA_FILES = ("hooks", "hooks.json", ".mcp.json", ".app.json")


@register
class Codex(Provider):
    name = "codex"
    binaries = ("codex",)
    final_events = ("turn.completed", "turn.failed")   # base.stream_ended
    api_host, api_base_env = "api.openai.com", "OPENAI_BASE_URL"
    login_hint = "run `codex login` there"
    efforts = ("minimal", "low", "medium", "high", "xhigh")
    isolate_read_only = True
    # Codex sends the thread id as the request's prompt_cache_key: one `codex exec` per coordinator
    # turn read 0 cached tokens on most turns with a byte-identical prefix (measured on 0.160).
    cache_key_per_session = True
    coord_session = "agent"   # `codex exec ... resume <id> -`; size and compaction from the rollout (session_context)
    session_format = "codex-rollout"   # sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl (fork_session)
    mcp_by_args = True   # `-c mcp_servers=...` (codexmcp.py): Codex has no per-run MCP config file
    # Whether build() left the harness hook out only because the user's config.toml defines hooks or
    # plugins; isolation_args() then adds it, as an isolated run ignores that file.
    _hook_held_by_user_config = ""

    def credential_files(self) -> list[str]:
        return [str(codex_home() / "auth.json")]

    def login_check(self) -> bool | None:
        b = self.binary()
        if not b or "status" not in cli_output(b, "login", "--help"):
            return None   # a CLI without `codex login status`
        got = status_check([b, "login", "status"])
        return None if got is None else got[0] == 0

    def resume_args(self, session_id: str) -> list[str]:
        # `codex exec [options] resume <SESSION_ID> -`: the daemon puts these last, just before the
        # trailing "-", so the exec options stay ahead of the subcommand and the prompt is on stdin.
        if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return []
        exe = self.binary() or "codex"
        return ["resume", session_id] if re.search(r"^\s*resume\b", cli_output(exe, "exec", "--help"), re.M) else []

    def session_saved(self, session_id: str, cwd: str, env: dict | None = None) -> bool:
        # Rollouts are saved as sessions/YYYY/MM/DD/rollout-<time>-<id>.jsonl under CODEX_HOME (the
        # run's own, when it had one). Not in the official docs: if the layout moves, this finds
        # nothing and the lost run starts fresh, as before.
        if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return False
        own = (env or {}).get("CODEX_HOME")
        homes = ([Path(own).expanduser()] if own else []) + [codex_home()]
        return any(any((h / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl")) for h in dict.fromkeys(homes))

    def session_paths(self, session_id: str, cwd: str, env: dict | None = None) -> list[Path]:
        if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return []
        own = (env or {}).get("CODEX_HOME")
        homes = ([Path(own).expanduser()] if own else []) + [codex_home()]
        return [f for h in dict.fromkeys(homes) for f in (h / "sessions").glob(f"*/*/*/rollout-*-{session_id}.jsonl")]

    def fork_session(self, session_id: str, cwd: str, env: dict | None, new_id: str) -> str:
        # `codex fork` is interactive only and `codex exec` has none (0.160), so a copy that `exec resume
        # <new id>` continues: the rollout's whole lines up to its last finished turn (a task_complete
        # event: a wake still running leaves a partial turn) with the old id swapped for the new one (session
        # meta, thread and turn records), the meta's cwd set to the run's, written owner-only as today's
        # rollout under the run's CODEX_HOME. The rollout layout is not in the official docs: if it
        # moves, the session is not found and the run takes the disk block (ctxfork.py).
        if not re.fullmatch(r"[A-Za-z0-9-]+", new_id or "") or not re.fullmatch(r"[A-Za-z0-9-]+", session_id or ""):
            return "bad session id"
        src = self.session_paths(session_id, cwd, env)
        if not src:
            return "the session's rollout is not on disk"
        raw = max(src, key=lambda x: x.stat().st_mtime).read_text(errors="replace")
        rows: list[dict] = []
        for line in raw.split("\n")[:-1]:   # a line not yet ended by a newline is partial
            try:
                row = json.loads(line.replace(session_id, new_id))
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        done = [i for i, r in enumerate(rows) if r.get("type") == "event_msg"
                and isinstance(r.get("payload"), dict) and r["payload"].get("type") == "task_complete"]
        if not done:
            return "the session has no finished turn to start from"
        rows = rows[:done[-1] + 1]
        meta = next((r for r in rows if r.get("type") == "session_meta" and isinstance(r.get("payload"), dict)), None)
        if meta is None:
            return "the session's rollout has no session_meta line"
        if cwd:
            meta["payload"]["cwd"] = cwd
        own = (env or {}).get("CODEX_HOME")
        now = datetime.now(timezone.utc)
        folder = (Path(own).expanduser() if own else codex_home()) / "sessions" / now.strftime("%Y/%m/%d")
        from ..project import durable_write
        durable_write(folder / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl",
                      "".join(json.dumps(r) + "\n" for r in rows), mode=0o600)
        return ""

    def own_instructions(self, session_id: str, cwd: str, env: dict | None, text: str) -> str:
        # `codex exec resume` keeps the developer messages saved in the rollout and takes no new
        # `-c developer_instructions` (0.160), and re-sends them after a compaction (each compacted
        # row's histories hold them too). A worker forked from a coordinator without tools so ran under
        # "This session has no tools" and did no work. The run's text replaces the harness's own blocks
        # in every developer message, wherever it is nested; Codex's own blocks (they start with "<",
        # as <skills_instructions>) stay.
        src = self.session_paths(session_id, cwd, env)
        if not src:
            return ""   # nothing here to rewrite: the resume finds no session either, or one Codex keeps elsewhere
        path = max(src, key=lambda x: x.stat().st_mtime)
        hits = [0]

        def swap(node):
            if isinstance(node, list):
                return [swap(x) for x in node]
            if not isinstance(node, dict):
                return node
            if node.get("role") == "developer" and isinstance(node.get("content"), list):
                content, mine = [], False
                for part in node["content"]:
                    if isinstance(part, dict) and isinstance(part.get("text"), str) \
                            and not part["text"].lstrip().startswith("<"):
                        hits[0] += 1
                        if mine:   # one harness block per message: the run's text once
                            continue
                        part, mine = {**part, "text": text}, True
                    content.append(part)
                return {**node, "content": content}
            return {k: swap(v) for k, v in node.items()}

        out = []
        for line in path.read_text(errors="replace").split("\n"):
            try:
                row = json.loads(line)
            except ValueError:
                out.append(line)   # a partial or blank line stays as it was
                continue
            out.append(json.dumps(swap(row)) if isinstance(row, (dict, list)) else line)
        if not hits[0]:
            return "the session holds no developer instructions to replace"
        from ..project import durable_write
        durable_write(path, "\n".join(out), mode=0o600)
        return ""

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions,
              disallowed_tools=(), denied_reads=(), web=True):
        exe = self.binary() or "codex"
        argv = [exe, "exec", "--json", "-C", cwd, "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["-c", "approval_policy=never"]
        if read_only:
            # A decision-only turn: no user config, MCP servers or execpolicy rules, as Claude's --restricted.
            argv += self._ignore_user_config() + ([IGNORE_RULES] if self._exec_flag(IGNORE_RULES) else [])
            argv += ["-s", "read-only"]
            features = cli_output(exe, "features", "list")
            for feature in READ_ONLY_OFF:
                if re.search(rf"^{feature}\b", features, re.M):
                    argv += ["-c", f"features.{feature}=false"]
        else:
            argv += ["-s", "workspace-write"]
            if not restrictions.get("no_internet"):
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
            argv += self._hook_for(cwd, user_config=True)
        if schema:
            argv += ["--output-schema", schema_file(strict_schema(schema))]
        argv += ["-"]
        return argv, {}

    def append_system_args(self, path: Path) -> list[str]:
        # `developer_instructions` adds to Codex's built-in instructions; `model_instructions_file`
        # would replace them. `-c` values parse as TOML, so the text goes in as a TOML string.
        # https://developers.openai.com/codex/config-reference
        arg = "developer_instructions=" + toml_string(Path(path).read_text())
        return ["-c", arg] if len(arg.encode()) <= MAX_ARG_BYTES else []

    def instructions_recorded(self, output_path, argv, expected, env=None):
        # Codex records the developer_instructions it took (append_system_args) near the top of the
        # session's rollout, as a developer message starting with the text. A CLI that ignored the
        # -c key would run without the harness's system prompt; Codex reads `-c` values as TOML, so
        # the message holds the unescaped text, as system.md has it.
        if not expected or "resume" in argv or not any(str(a).startswith("developer_instructions=") for a in argv):
            return None   # the text led the prompt, or a resumed session's rollout holds the first run's
        sid = next((str(ev.get("thread_id") or "") for ev in self._events(Path(output_path))
                    if ev.get("type") == "thread.started"), "")
        if not sid:
            return {"state": "no_session"}
        paths = self.session_paths(sid, "", env)
        if not paths:
            return {"state": "no_rollout"}
        want = expected.replace("\r\n", "\n")[:INSTRUCTIONS_PREFIX]
        version, answered, lines = "", False, 0
        try:
            with open(max(paths, key=lambda f: f.stat().st_mtime), "rb") as f:
                for raw in f:
                    if lines >= ROLLOUT_HEAD_LINES or not raw.endswith(b"\n"):
                        break   # past the head, or a line still being written
                    lines += 1
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    pay = rec.get("payload") if isinstance(rec, dict) else None
                    if not isinstance(pay, dict):
                        continue
                    if rec.get("type") == "session_meta":
                        version = str(pay.get("cli_version") or "")[:40]
                    elif rec.get("type") == "response_item" and pay.get("role") == "developer":
                        parts = pay.get("content") if isinstance(pay.get("content"), list) else []
                        if any(isinstance(c, dict) and isinstance(c.get("text"), str)
                               and c["text"].replace("\r\n", "\n").startswith(want) for c in parts):
                            return {"state": "recorded", "cli_version": version}
                    elif rec.get("type") == "response_item" and (pay.get("role") == "assistant"
                                                                  or pay.get("type") in MODEL_ITEMS):
                        answered = True   # the model already answered: the first request's context is all here
                    elif rec.get("type") == "event_msg" and pay.get("type") == "token_count":
                        answered = True
        except OSError:
            return {"state": "unreadable"}
        done = answered or lines >= ROLLOUT_HEAD_LINES
        return {"state": "missing" if done else "pending", "cli_version": version}

    def compact_args(self, tokens: int) -> list[str]:
        # `model_auto_compact_token_limit`: history is compacted once it reaches this many tokens.
        return ["-c", f"model_auto_compact_token_limit={int(tokens)}"] if tokens and tokens > 0 else []

    def writable_args(self, dirs, restrictions=None):
        # workspace-write only lets the worker write its cwd; result.json, `ttp note`, `ttp lock`
        # and commits in a worktree (whose git metadata lives in the main repository) are elsewhere.
        return ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(d) for d in dirs])] if dirs else []

    def plugin_dirs_why(self, isolated: bool) -> str:
        return ("Codex loads plugins only from those installed in its home (`codex plugin add`)"
                + ("; worker_isolation also keeps out the plugins installed in your config.toml, all but "
                   "tt-project's own." if isolated else "."))

    def isolation_args(self) -> list[str]:
        # The user's config.toml holds their MCP servers, plugins, hooks and profiles.
        out = self._ignore_user_config()
        if out:
            out += own_plugin_args()   # tt-project's own skills, which the ignored config enabled
        if out and self._hook_held_by_user_config:
            out += self._hook_for(self._hook_held_by_user_config, user_config=False)
        return out

    def mcp_args(self, names, dirs, argv, environ=None):
        # The servers go in as `-c` (codexmcp.py); a worker still reading config.toml gets the user's
        # other servers turned off.
        from . import codexmcp
        isolated = IGNORE_USER_CONFIG in argv
        users = [] if isolated else codexmcp.user_server_names(_read(codex_home() / "config.toml"))
        return codexmcp.run_args(names, dirs, users, isolated, inherited=environ)

    def mcp_check(self, names: list[str], dirs: list[str], isolated: bool) -> tuple[dict[str, bool] | None, list[str]]:
        """The servers a worker would get, as `codex mcp list --json` reads its arguments ({name:
        enabled}; None if it did not answer), and the names it cannot give. Offline, no sign-in:
        an isolated worker's view is read with an empty CODEX_HOME, as it ignores config.toml."""
        import tempfile
        from . import codexmcp
        isolated = isolated and bool(self._ignore_user_config())
        args, _, unknown = self.mcp_args(names, dirs, [IGNORE_USER_CONFIG] if isolated else [])
        with tempfile.TemporaryDirectory(prefix="ttp-codex-mcp-") as home:
            env = {**os.environ, **({"CODEX_HOME": home} if isolated else {})}
            try:
                r = subprocess.run([self.binary() or "codex", *args, "mcp", "list", "--json"], env=env,
                                   capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                return None, unknown
        if r.returncode != 0 or not r.stdout.strip().startswith("["):
            return None, unknown
        return codexmcp.parse_list(r.stdout), unknown

    def _exec_flag(self, flag: str) -> bool:
        return bool(re.search(rf"(?<![\w-]){re.escape(flag)}\b", cli_output(self.binary() or "codex", "exec", "--help")))

    def _ignore_user_config(self) -> list[str]:
        if not self._exec_flag(IGNORE_USER_CONFIG) or PROVIDER_CONFIG_RE.search(_read(codex_home() / "config.toml")):
            return []
        return [IGNORE_USER_CONFIG]

    def harness_hook(self, argv):
        return any(str(a).startswith("hooks.PreToolUse=") for a in argv)   # hook_config_args, when _hook_for adds it

    def hooks_supported(self) -> bool:
        """This build runs hooks (the `hooks` feature is on) and takes the trust bypass flag."""
        features = cli_output(self.binary() or "codex", "features", "list")
        return bool(re.search(r"^hooks\s.*\btrue\s*$", features, re.M)) and self._exec_flag(HOOK_TRUST_BYPASS)

    def _hook_for(self, cwd: str, user_config: bool) -> list[str]:
        """The harness hook and the trust bypass, when the hook would be the run's only hook source;
        [] otherwise (or on a build without hooks)."""
        self._hook_held_by_user_config = ""
        if not self.hooks_supported():
            return []
        if hook_sources(cwd, user_config=user_config):
            if user_config and not hook_sources(cwd, user_config=False):
                self._hook_held_by_user_config = cwd
            return []
        return [HOOK_TRUST_BYPASS, *hook_config_args()]

    def coord_tools(self, argv, cwd, settings, full, web=True):
        # A session coordinator's tools (coordtools.session_tool_args for Claude): every call goes
        # through ttp.hook's CoordPreToolUse (and with full tools the CoordPostToolUse guard and audit),
        # under the same trust-bypass rule as a worker's hook (_hook_for). The shell and the web come
        # back only with that hook: without it nothing refuses a push to the push branch or a call past
        # the tool timeout (coordtools.full_refusal), so the turn keeps its read-only tool set and
        # coord_tools_note names why.
        self.coord_tools_note = ""
        gap, others = self._coord_hook_gap(cwd, user_config=IGNORE_USER_CONFIG not in argv)
        if gap:
            self.coord_tools_note = (f"codex: hooks cannot run here ({', '.join(others)}): the coordinator has no shell"
                                     if others else "codex: this build runs no hooks: the coordinator has no shell")
            return argv, {}
        back = {f"features.{f}=false" for f in READ_ONLY_OFF if f != "web_search_request" or (full and web)}
        out = []
        for a in argv:
            if a in back and out and out[-1] == "-c":
                out.pop()
            else:
                out.append(a)
        from ..coordtools import HOOK_EVENT, POST_EVENT
        hooks = [("PreToolUse", "*", HOOK_EVENT, 20)] + ([("PostToolUse", "*", POST_EVENT, 20)] if full else [])
        i = len(out) - 1 if out and out[-1] == "-" else len(out)
        out[i:i] = [HOOK_TRUST_BYPASS, *hook_config_args(hooks)]
        return out, dict(settings.get("env") or {})

    def _coord_hook_gap(self, cwd: str, user_config: bool) -> tuple[str, list[str]]:
        """Why ttp's hook cannot run in a coordinator wake in `cwd` ("" when it can), and the other hook
        sources that keep it out ([] for a build without hooks). coord_tools and coord_tool_set share it."""
        if not self.hooks_supported():
            return "this Codex build runs no hooks", []
        others = hook_sources(cwd, user_config=user_config)
        return ("another hook source is set for Codex here, so ttp's hooks cannot run" if others else ""), others

    def coord_tool_set(self, cwd):
        # As coord_tools gives them: build's read-only turn reads config.toml unless _ignore_user_config.
        gap, _ = self._coord_hook_gap(cwd, user_config=not self._ignore_user_config())
        return ("none", f"{gap}: no shell, web or file edits") if gap else ("full", "")

    def write_fence(self, restrictions=None) -> str:
        return ""   # workspace-write: Seatbelt on macOS, bubblewrap or the bundled helper on Linux

    fence_optional = True   # workspace-write only on request (providers.codex.write_fence) or under no_internet

    def fence_args(self, argv, dirs, cwd="", on=True, restrictions=None, freed=()):
        if on or (restrictions or {}).get("no_internet"):
            return super().fence_args(argv, dirs, cwd, on, restrictions, freed)
        if "workspace-write" not in argv:
            return argv, False
        argv = list(argv)
        argv[argv.index("workspace-write")] = "danger-full-access"
        i = next((k for k, w in enumerate(argv) if w == "sandbox_workspace_write.network_access=true"), None)
        if i is not None and argv[i - 1] == "-c":
            del argv[i - 1:i + 1]
        return argv, False

    def read_only_args(self, argv, on=False, restrictions=None):
        # `-s read-only` only with the opt-in fence or no_internet; the daemon's action gates apply the output.
        i = next((k for k in range(1, len(argv)) if argv[k] == "read-only" and argv[k - 1] == "-s"), None)
        if on or (restrictions or {}).get("no_internet") or i is None:
            return argv
        return argv[:i] + ["danger-full-access"] + argv[i + 1:]

    def launch_sandbox(self, argv):
        i = next((k for k, w in enumerate(argv) if w in ("-s", "--sandbox")), None)
        mode = argv[i + 1] if i is not None and i + 1 < len(argv) else ""
        return super().launch_sandbox(argv) or ("" if mode in ("", "danger-full-access") else f"codex-{mode}")

    def fenced(self, on=True, restrictions=None):
        return on or bool((restrictions or {}).get("no_internet"))

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
                # output_tokens already includes reasoning; reasoning_output_tokens is a breakdown.
                out += int(u.get("output_tokens") or 0)
            elif t.startswith("item.") and (ev.get("item") or {}).get("type") == "agent_message":
                last = (ev.get("item") or {}).get("text") or last
        return inp, cached, out, last

    def _reasoning(self, output_path: Path) -> int | None:
        """The reasoning share of the run's output tokens, or None when no turn reported one."""
        got = None
        for ev in self._events(output_path):
            r = (ev.get("usage") or {}).get("reasoning_output_tokens") if ev.get("type") == "turn.completed" else None
            if isinstance(r, int) and not isinstance(r, bool):
                got = (got or 0) + r
        return got

    def _price(self, inp: int, cached: int, out: int) -> float:
        pin, pcached, pout = price_row(PRICES, self.prices, self.model)
        return ((inp - cached) * pin + cached * pcached + out * pout) / 1e6

    def _rollout_tokens(self, output_path: Path, since: float | None = None,
                        env: dict | None = None) -> tuple[int, int, int] | None:
        """(input, cached, output) tokens this run spent so far, from the newest
        event_msg/token_count `total_token_usage` in its session's rollout. That total covers the
        whole session, so a resumed run subtracts the last total written before `since` (its start).
        Read incrementally: the runner asks every few seconds while the rollout grows."""
        return self._read_rollout(output_path, since, env)[0]

    def _read_rollout(self, output_path: Path, since: float | None = None, env: dict | None = None):
        """(`_rollout_tokens`, (stamp, rate_limits) of the newest token_count event carrying them),
        read in one incremental pass."""
        sid = next((str(ev.get("thread_id") or "") for ev in self._events(output_path)
                    if ev.get("type") == "thread.started"), "")
        paths = self.session_paths(sid, "", env)
        if not paths:
            return None, None
        seen = self.__dict__.setdefault("_rollouts", {})
        try:
            path = max(paths, key=lambda f: f.stat().st_mtime)
            offset, base, last, limits = seen.get((str(path), since), (0, None, None, None))
            with open(path, "rb") as f:
                f.seek(offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break   # a line still being written: read it on the next call
                    offset += len(raw)
                    if b"token_count" not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if isinstance(rec["payload"].get("rate_limits"), dict):   # also on an event with no info
                            limits = (_stamp(rec.get("timestamp")), rec["payload"]["rate_limits"])
                        total = rec["payload"]["info"]["total_token_usage"]
                        tok = tuple(int(total.get(k) or 0)
                                    for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
                    if since is not None and _stamp(rec.get("timestamp")) < since:
                        base = tok
                    else:
                        last = tok
        except OSError:
            return None, None
        seen[(str(path), since)] = (offset, base, last, limits)
        if last is None:
            return None, limits
        return (tuple(max(a - b, 0) for a, b in zip(last, base)) if base else last), limits

    def session_context(self, session_id, since, env=None):
        # `codex exec --json` gives one usage per turn, summed over its calls; the rollout's token_count
        # events carry each call's own (last_token_usage, input counting the cached part), and a
        # "compacted" record marks a history Codex compacted.
        paths = self.session_paths(session_id, "", env)
        if not paths:
            return None
        first = last = 0
        compacted = False
        try:
            with open(max(paths, key=lambda f: f.stat().st_mtime), "rb") as f:
                for raw in f:
                    if b'"token_count"' not in raw and b'"compacted"' not in raw:
                        continue
                    try:
                        rec = json.loads(raw)
                        if since is not None and _stamp(rec.get("timestamp")) < since:
                            continue
                        if rec.get("type") == "compacted":
                            compacted = True
                            continue
                        call = rec["payload"]["info"]["last_token_usage"]
                        n = int(call.get("input_tokens") or 0) + int(call.get("output_tokens") or 0)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue
                    first, last = first or n, n
        except OSError:
            return None
        return {"first_context": first, "last_context": last, "compacted": compacted} if last or compacted else None

    def activity(self, output_path, offset=0):
        """`codex exec --json`: item.started is a call, item.completed its result (a command that
        exited non-zero or an item that failed is an error); an item only seen completed is both."""
        lines, offset = jsonl_from(Path(output_path), offset, (b'"item.started"', b'"item.completed"'))
        events = []
        for ev in lines:
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            kind = item.get("type") or item.get("item_type") or ""
            if kind in ("agent_message", "reasoning", "todo_list", "error", ""):
                continue
            if kind == "command_execution":
                head = item.get("command") or ""
            elif kind == "file_change":
                head = " ".join(str(c.get("path") or "") for c in item.get("changes") or [] if isinstance(c, dict))
            elif kind == "mcp_tool_call":
                kind, head = f"{item.get('server') or ''}.{item.get('tool') or ''}", item.get("arguments") or ""
            else:
                head = item.get("query") or ""
            call = {"kind": "call", "id": str(item.get("id") or ""), "tool": kind, "input_head": head80(head),
                    "is_error": None, "ts": None, "hash": input_hash(kind, head),
                    "command": str(head)[:CMD_MAX] if kind == "command_execution" else ""}
            if ev.get("type") == "item.started":
                events.append({**call, "payloads": _payloads(item)})
            else:
                code = item.get("exit_code")
                failed = item.get("status") == "failed" or (isinstance(code, int) and code != 0)
                events.append({**call, "kind": "result", "is_error": failed})
        return events, offset

    def windows_so_far(self, output_path, since=None, env=None, now=None):
        # The newest reading a model call wrote to the rollout, while fresh; else none, and the
        # gate keeps the meter's (an unreadable stamp is never fresh).
        limits = self._read_rollout(Path(output_path), since, env)[1]
        now = time.time() if now is None else now
        if not limits or not abs(now - limits[0]) <= ROLLOUT_WINDOWS_FRESH_S:
            return []
        return [dict(w, read_at=min(limits[0], now)) for w in _rollout_windows(limits[1])]

    def cut_off_cost(self, output_path, since=None, env=None):
        # Codex prints usage only on turn.completed, and `codex exec` is one turn; its rollout has
        # running token totals, so a run stopped mid-turn is priced at the tokens it spent.
        got = self._rollout_tokens(Path(output_path), since, env)
        return self._price(*got) + self._price(*self._subagent_tokens(output_path, since, env)) if got else None

    def cost_so_far(self, output_path, since=None, env=None):
        inp, cached, out, _ = self._tokens(output_path)
        return max(self._price(inp, cached, out), self.cut_off_cost(output_path, since, env) or 0.0)

    def add_subagent_usage(self, usage, output_path, since=None, env=None) -> None:
        sub = self._subagent_tokens(output_path, since, env)
        if any(sub):
            usage.input_tokens += sub[0] - sub[1]
            usage.cache_read_tokens += sub[1]
            usage.output_tokens += sub[2]
            usage.cost_usd += self._price(*sub)
            usage.extra["subagent_tokens"] = list(sub)

    def _subagent_tokens(self, output_path, since=None, env=None) -> tuple[int, int, int]:
        """(input, cached, output) tokens of the sub-agents the run's thread spawned (the multi_agent
        feature), at any depth: `turn.completed` counts only the run's own thread, and each sub-agent
        writes its own rollout whose session_meta names its parent_thread_id. Each one's newest
        total_token_usage, less its last total before `since` (a sub-agent a resumed run continues).
        Read from the parent rollout's day folder and the next (a sub-agent started past midnight UTC);
        priced at the run's model. The layout is not in the official docs: if it moves, this finds none."""
        sid = next((str(ev.get("thread_id") or "") for ev in self._events(Path(output_path))
                    if ev.get("type") == "thread.started"), "")
        days: set[Path] = set()
        for f in self.session_paths(sid, "", env):
            days.add(f.parent)
            try:
                nxt = datetime.strptime("/".join(f.parent.parts[-3:]), "%Y/%m/%d") + timedelta(days=1)
                days.add(f.parent.parent.parent.parent / nxt.strftime("%Y/%m/%d"))
            except ValueError:
                pass
        parent_of: dict[str, tuple[str, Path]] = {}
        heads = self.__dict__.setdefault("_rollout_heads", {})   # a rollout's first line never changes
        for day in days:
            for f in day.glob("rollout-*.jsonl"):
                if str(f) in heads:
                    if heads[str(f)]:
                        parent_of[heads[str(f)][0]] = (heads[str(f)][1], f)
                    continue
                heads[str(f)] = None
                try:
                    with open(f, "rb") as fh:
                        head = fh.readline()
                    if not head.endswith(b"\n"):
                        del heads[str(f)]   # still being written: read it next time
                        continue
                    if b"parent_thread_id" not in head:
                        continue
                    meta = json.loads(head).get("payload") or {}
                    up = meta.get("parent_thread_id") or (((meta.get("source") or {}).get("subagent") or {})
                                                          .get("thread_spawn") or {}).get("parent_thread_id")
                    if up and meta.get("id"):
                        parent_of[str(meta["id"])] = (str(up), f)
                        heads[str(f)] = (str(meta["id"]), str(up))
                except (OSError, ValueError, AttributeError):
                    continue
        mine, grew = {sid}, bool(sid)
        while grew:
            grew = False
            for child, (up, _) in parent_of.items():
                if up in mine and child not in mine:
                    mine.add(child)
                    grew = True
        total = [0, 0, 0]
        for child in mine - {sid}:
            got = _rollout_total(parent_of[child][1], since)
            total = [a + b for a, b in zip(total, got)]
        return tuple(total)

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        inp, cached, out, last = self._tokens(output_path)
        u = RunUsage(input_tokens=inp - cached, cache_read_tokens=cached, output_tokens=out, final_text=last,
                     estimated=True)
        u.cost_usd = self._price(inp, cached, out)
        if (reasoning := self._reasoning(output_path)) is not None:
            u.extra["thinking_tokens"] = reasoning   # part of output_tokens (effortab.report)
        events, errors = 0, []
        cache_reports = []
        for ev in self._events(output_path):
            events += 1
            if ev.get("type") == "thread.started":
                u.session_id = str(ev.get("thread_id") or "")   # what `codex exec resume` takes
            elif ev.get("type") == "turn.completed":
                report = ev.get("usage") or {}
                cache_reports.append(all(isinstance(report.get(k), int) and report[k] >= 0
                                         for k in ("input_tokens", "cached_input_tokens"))
                                     and report.get("cached_input_tokens", 0) <= report.get("input_tokens", 0))
                errors = []   # transient errors Codex retried are reported as "error" events too
            elif ev.get("type") in ("turn.failed", "error"):
                errors.append(ev)
        u.extra["cache_telemetry"] = {"complete": bool(cache_reports) and all(cache_reports),
                                      "requests": None, "source": "turns"}
        err = HOOK_DENIAL_RE.sub("", stderr_tail(stderr_path))
        if errors:
            u.error = json.dumps(errors[-1])[:400]
        elif not events and err.strip():
            u.error = err.strip()[-400:]   # it failed before starting a session: only stderr says why
        if last.strip().startswith("{"):
            try:
                u.structured = drop_nulls(json.loads(last))
            except ValueError:
                pass
        if not cache_reports and isinstance(u.structured, dict) and isinstance(u.structured.get("actions"), list):
            # Cut off before its turn completed (the runner's budget check) after a whole JSON answer:
            # the daemon may apply it (Daemon._finish_coordinator). A cut-off message never parses.
            u.answer = u.structured
        blob = u.error + " " + err
        if AUTH_RE.search(blob) and not u.output_tokens:
            u.auth_failed = True
        elif LIMIT_RE.search(blob):
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        # Only the failed turn's own error event, on a run that wrote nothing: never stderr alone.
        if errors and request_rejected(u.error) and not (u.cost_usd or u.output_tokens or u.auth_failed or u.limited):
            u.request_error = True
        return u

    def account(self) -> str:
        """`codex (<mode>) <id>`: the login mode plus a short opaque hash of the account, so two
        logins of one mode tell apart. Never the email or a token."""
        auth = _auth()
        if auth is None:
            return ""
        who = _account_id(auth)
        return f"codex ({_mode(auth)})" + (f" {hashlib.sha256(who.encode()).hexdigest()[:10]}" if who else "")

    def account_kind(self) -> str:
        auth = _auth()
        return "" if auth is None else f"codex ({_mode(auth)})"

    def meter(self) -> list:
        """Plan windows via the app-server protocol; spends no model tokens."""
        exe = self.binary()
        if not exe:
            return []
        from ..budget import Window
        from ..credits import parse
        msg = app_server_call([exe, "app-server"], "account/rateLimits/read")
        if msg is None:
            return []
        res = msg.get("result") or {}
        self.last_credits = parse(res)
        rl, allowed = res.get("rateLimits") or {}, res.get("ordinaryUsageAllowed", True)
        wins = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if not w:
                continue
            mins = int(w.get("windowDurationMins") or 0)
            util = float(w.get("usedPercent") or 0)
            # Keyed like the readings its runs report, so the gate matches them to the account.
            wins.append(Window("codex", _window_name(mins), 100.0 if not allowed else util, w.get("resetsAt"),
                               self.account()))
        return wins

    def consume_reset(self, credit_id: str, key: str) -> dict | None:
        """`account/rateLimitResetCredit/consume`: resets the plan windows at once. Its outcome is
        passed on as it is (credits.classify judges it); no answer, or a timeout, connection or 5xx
        error, is a transport error, which the idempotency key makes safe to retry."""
        from ..credits import transport_error
        exe = self.binary()
        if not exe:
            return {"transport": True, "error": "codex not found"}
        msg = app_server_call([exe, "app-server"], "account/rateLimitResetCredit/consume",
                              {"creditId": credit_id, "idempotencyKey": key})
        if msg is None:
            return {"transport": True, "error": "no answer from `codex app-server` (did not start or timed out)"}
        if "error" in msg:
            return {"transport": transport_error(msg["error"]), "error": json.dumps(msg["error"])[:300]}
        return {"outcome": str((msg.get("result") or {}).get("outcome") or "")}

    def resolved_session(self, argv, cwd, env=None):
        # The app-server's `thread/start` answers with the model and reasoning effort the session
        # resolved from the same config and `-c` overrides; an ephemeral thread starts no turn and
        # spends nothing. `codex exec`'s own stream does not report them before its first turn.
        exe = self.binary()
        if not exe:
            return None
        msg = app_server_call([exe, "app-server", *session_config_args(argv)], "thread/start",
                              {"cwd": cwd, "ephemeral": True}, cwd=cwd, env=env)
        if msg is None:
            return {"error": "no answer from `codex app-server`"}
        if "error" in msg or not isinstance(msg.get("result"), dict) or not msg["result"].get("model"):
            return {"error": "thread/start: " + json.dumps(msg.get("error") or msg.get("result"))[:300]}
        res = msg["result"]
        return {"model": str(res.get("model") or ""), "effort": str(res.get("reasoningEffort") or "")}


def _rollout_total(path: Path, since: float | None) -> tuple[int, int, int]:
    """A rollout's newest (input, cached, output) total_token_usage, less its last total before `since`."""
    base, last = (0, 0, 0), (0, 0, 0)
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b"token_count" not in raw or not raw.endswith(b"\n"):
                    continue
                try:
                    rec = json.loads(raw)
                    total = rec["payload"]["info"]["total_token_usage"]
                    tok = tuple(int(total.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if since is not None and _stamp(rec.get("timestamp")) < since:
                    base = tok
                else:
                    last = tok
    except OSError:
        return (0, 0, 0)
    return tuple(max(a - b, 0) for a, b in zip(last, base)) if last != (0, 0, 0) else (0, 0, 0)

def codex_home() -> Path:
    """Where Codex keeps its login and saved sessions."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def _auth() -> dict | None:
    """CODEX_HOME's auth.json, or None when it is missing or unreadable."""
    try:
        auth = json.loads((codex_home() / "auth.json").read_text())
    except (OSError, ValueError):
        return None
    return auth if isinstance(auth, dict) else None


def _mode(auth: dict) -> str:
    return str(auth.get("auth_mode") or ("api_key" if auth.get("OPENAI_API_KEY") else "chatgpt"))


def _account_id(auth: dict) -> str:
    """A stable id of the logged-in account, to be hashed: the ChatGPT account id (from `tokens`, else
    the id token's claims), else the API key itself. Empty when there is none."""
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    if tokens.get("account_id"):
        return str(tokens["account_id"])
    tok = tokens.get("id_token")
    if isinstance(tok, str) and tok.count(".") == 2:
        try:
            body = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            info = claims.get("https://api.openai.com/auth") or {}
            if who := (info.get("chatgpt_account_id") if isinstance(info, dict) else None) or claims.get("sub"):
                return str(who)
        except (ValueError, AttributeError):
            pass
    return str(auth.get("OPENAI_API_KEY") or "")


def _window_name(mins: int) -> str:
    return "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m"


def _rollout_windows(limits) -> list[dict]:
    """A token_count event's `rate_limits` as plan windows, shaped like the meter's: the account's
    own limit only (a `limit_id` naming another limit is skipped), and nothing if any window is
    malformed. A reached limit reads full, as the meter reads a refused account."""
    if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "", "codex"):
        return []
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    full, wins = bool(limits.get("rate_limit_reached_type")), []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if w is None:
            continue
        if not isinstance(w, dict):
            return []
        mins, util, resets = w.get("window_minutes"), w.get("used_percent"), w.get("resets_at")
        if not (num(mins) and mins > 0 and num(util) and 0 <= util <= 100 and (resets is None or num(resets))):
            return []
        wins.append({"window": _window_name(int(mins)), "utilization": 100.0 if full else float(util),
                     "resets_at": resets})
    return wins


def _stamp(text) -> float:
    """A rollout line's ISO timestamp as epoch seconds; unreadable counts as now (booked to the run)."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


# `codex exec` options that change which model or effort a session resolves. The hook keys and the
# instructions do not, and `developer_instructions` may be too long to pass twice.
SESSION_KEYS_SKIP = ("hooks.", "developer_instructions=")


def session_config_args(argv: list[str]) -> list[str]:
    """The `-c` overrides of a `codex exec` command line, with its `-m` as `-c model=...`, for an
    app-server that should resolve the same session."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--config") and i + 1 < len(argv):
            if not argv[i + 1].startswith(SESSION_KEYS_SKIP):
                out += ["-c", argv[i + 1]]
            i += 2
        elif a in ("-m", "--model") and i + 1 < len(argv):
            out += ["-c", "model=" + toml_string(argv[i + 1])]
            i += 2
        else:
            i += 1
    return out


def app_server_call(argv: list[str], method: str, params: dict | None = None, cwd: str | None = None,
                    env: dict | None = None, timeout_s: float = 25) -> dict | None:
    """The reply to one request to a `codex app-server` started with `argv`, after the protocol's
    initialize handshake; None when it cannot start or does not answer in time. The server answers
    asynchronously, so stdin stays open until the reply arrives (closing it early loses it)."""
    import select
    import time
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1, cwd=cwd, env=None if env is None else {**os.environ, **env})
    except OSError:
        return None
    try:
        for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"clientInfo": {"name": "tt-project", "version": "1"}}},
                  {"jsonrpc": "2.0", "method": "initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": method, **({"params": params} if params is not None else {})}):
            proc.stdin.write(json.dumps(m) + "\n")
            proc.stdin.flush()
        # Raw reads: a buffered readline could swallow the reply select() then never reports.
        deadline, fd, buf = time.time() + timeout_s, proc.stdout.fileno(), b""
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            *lines, buf = (buf + chunk).split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    return msg
    except (OSError, ValueError):
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return None


def hook_sources(cwd: str, user_config: bool = True) -> list[str]:
    """Files besides the harness's own `-c` keys that may give a run in `cwd` hooks Codex would
    otherwise ask the user to trust: `hooks.json` in Codex's home, its `config.toml` when the run
    loads it (`user_config`), and a `.codex/` folder in `cwd` or any folder above it. Hooks or
    plugins in a config.toml count, as plugins can bundle hooks (see config_has_hooks)."""
    home = codex_home()
    found = [str(home / "hooks.json")] if (home / "hooks.json").exists() else []
    if user_config and config_has_hooks(_read(home / "config.toml")):
        found.append(str(home / "config.toml"))
    for d in [Path(cwd), *Path(cwd).parents]:
        dot = d / ".codex"
        if dot.resolve() == home.resolve():
            continue   # Codex's own home (often ~/.codex), handled above
        if (dot / "hooks.json").exists():
            found.append(str(dot / "hooks.json"))
        if config_has_hooks(_read(dot / "config.toml")):
            found.append(str(dot / "config.toml"))
    return found


def own_plugin_args() -> list[str]:
    """`-c` keys that enable tt-project's installed copy in a run that ignores config.toml, so an
    isolated worker keeps the plugin's skills, as a run reading the config does. Only a copy that
    brings nothing but skills (no hooks, MCP servers or apps: those would undo the isolation, and
    the hook trust bypass would trust its hooks). Of several marketplaces, the one whose copy is
    this runtime's version, then the one config.toml enables. [] when Codex has no such copy (the
    skills stay readable by `ttp skill`)."""
    cache = codex_home() / "plugins" / "cache"
    try:
        markets = sorted(d.parent.name for d in cache.glob(f"*/{OWN_PLUGIN}") if d.is_dir())
    except OSError:
        return []
    usable = [m for m in markets if _skills_only(cache / m / OWN_PLUGIN)]
    if not usable:
        return []
    config = _read(codex_home() / "config.toml")
    enabled = [m for m in usable
               if re.search(rf"^\s*\[?\s*plugins\.[\"']{re.escape(f'{OWN_PLUGIN}@{m}')}[\"']", config, re.M)]
    from .. import __version__
    from ..codexmarket import cached
    pick = min(usable, key=lambda m: (__version__ not in cached(codex_home(), m), m not in enabled))
    key = f"{OWN_PLUGIN}@{pick}"
    return ["-c", f"plugins={{{toml_string(key)}={{enabled=true}}}}"]


def _skills_only(plugin: Path) -> bool:
    """Whether every installed version of a plugin has a manifest and brings nothing but skills."""
    try:
        versions = [v for v in plugin.iterdir() if v.is_dir()]
        for v in versions:
            manifest = json.loads((v / ".codex-plugin" / "plugin.json").read_text())
            if not isinstance(manifest, dict) or any(k in manifest for k in PLUGIN_EXTRAS) \
                    or any((v / f).exists() for f in PLUGIN_EXTRA_FILES):
                return False
    except (OSError, ValueError):
        return False
    return bool(versions)


def config_has_hooks(config_toml: str) -> bool:
    """Whether a Codex config.toml may give a run hooks: a top-level `hooks` table or key, or a
    plugin entry not set `enabled = false` (plugins can bundle hooks). Parsed with tomllib (3.11+)
    or tomli; loaded by name, since the runtime supports 3.9. Without a parser, or on a file that
    does not parse, HOOK_CONFIG_RE decides, so a doubtful line counts: a false find only keeps the
    harness hook off."""
    for name in ("tomllib", "tomli"):
        try:
            data = importlib.import_module(name).loads(config_toml)
        except ImportError:
            continue
        except Exception:   # tomllib raises TOMLDecodeError (a ValueError); be cautious on any other
            break
        plugins = data.get("plugins")
        enabled = [e for e in plugins.values() if not (isinstance(e, dict) and e.get("enabled") is False)] \
            if isinstance(plugins, dict) else plugins
        return "hooks" in data or bool(enabled)
    return bool(HOOK_CONFIG_RE.search(config_toml))


def shell_script(command: str) -> str:
    """The script of a `bash -lc '<script>'` command line as `codex exec --json` reports it (what the
    Bash hook gets as its command); the line itself when it is no such wrapper."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh") and words[1] in ("-c", "-lc"):
        return words[2]
    return command


def _payloads(item: dict) -> list[dict]:
    """The hook-shaped calls of one started item (Provider.activity "payloads")."""
    kind = item.get("type") or item.get("item_type") or ""
    if kind == "command_execution":
        return [{"tool_name": "Bash", "tool_input": {"command": shell_script(str(item.get("command") or ""))}}]
    if kind == "file_change":
        return [{"tool_name": "Edit", "tool_input": {"file_path": str(c.get("path") or "")}}
                for c in item.get("changes") or [] if isinstance(c, dict)]
    if kind == "mcp_tool_call":
        return [{"tool_name": f"mcp__{item.get('server') or ''}__{item.get('tool') or ''}",
                 "tool_input": item.get("arguments")}]
    return []


# The tool calls the harness hook checks first, as claude.hook_settings does: shell calls, file edits (Codex
# edits files only through apply_patch) and MCP connector calls (`mcp__<server>__<tool>`). A regex, as
# measured on codex-cli 0.160.
PRE_TOOL_MATCHER = "Bash|apply_patch|mcp__.*"


# A worker's hooks, as (Codex event, matcher or None, ttp.hook event, timeout) rows.
WORKER_HOOKS = (("PreToolUse", PRE_TOOL_MATCHER, "PreToolUse", 20), ("PostToolUse", "*", "PostToolUse", 20),
                ("Stop", None, "Stop", STOP_HOOK_TIMEOUT_S))


def hook_config_args(hooks=WORKER_HOOKS) -> list[str]:
    """`-c` keys that route shell, edit and MCP calls, every tool result and the end of a turn (Stop: the
    hand-off guard) through `ttp.hook` (see the module docstring); `hooks`: (Codex event, matcher or
    None, ttp.hook event, timeout) rows, a worker's by default. PYTHONPATH is set in the command
    itself, so the worker's own commands keep theirs."""
    runtime = str(Path(__file__).resolve().parents[2])
    cmd = f"env PYTHONPATH={shlex.quote(runtime)} {shlex.join(isolated_module_argv('ttp.hook', runtime))}"
    out = []
    for event, matcher, handler, timeout in hooks:
        hook = f"{{type=\"command\",command={toml_string(f'{cmd} {handler}')},timeout={timeout}}}"
        entry = f"[{{{'' if matcher is None else f'matcher={toml_string(matcher)},'}hooks=[{hook}]}}]"
        out += ["-c", f"hooks.{event}={entry}"]
    return out


def toml_string(text: str) -> str:
    """`text` as a TOML basic string: quotes, backslashes and control characters escaped."""
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def schema_file(schema: dict) -> str:
    """One file per distinct schema, reused across runs, so coordinator turns leave no temp files."""
    text = json.dumps(schema, sort_keys=True)
    # A per-user directory: in the shared temp dir another user could create the file first.
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ttp"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cache / f"ttp-schema-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    if not path.exists() or path.read_text(errors="replace") != text:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    return str(path)

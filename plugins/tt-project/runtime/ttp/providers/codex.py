# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI Codex CLI (`codex exec --json`). Codex reports tokens but no cost, has no budget flag,
and exposes plan windows through `codex app-server` (`account/rateLimits/read`, no model tokens).
Cost is therefore an estimate from a price table the project can edit. Tokens arrive only when a
turn completes, so the runner's mid-run budget check sees completed turns only.

Write fence (from the docs; Codex was not installed where this was probed). Workers run in
`workspace-write`, so they are fenced: they write their cwd, temp dirs and `writable_args` roots.

| Question | Linux | macOS |
| :- | :- | :- |
| Fence writes to a list of dirs | yes, `sandbox_workspace_write.writable_roots`; uses `bwrap` from PATH, else a bundled helper that needs unprivileged user namespaces (startup warning if it can't) | yes, Seatbelt |
| Unix socket under `state/` from the sandbox | blocked by default; allow it with `permissions.<name>.network.unix_sockets` or `dangerously_allow_all_unix_sockets` | same |

Reads are not fenced, and the roots today include all of the project's state.

Harness hook (measured on codex-cli 0.160). Codex sends the same PreToolUse/PostToolUse payload as
Claude Code (`tool_name` "Bash", `tool_input.command`) and takes the same deny and added-context
replies, so workers get `ttp.hook` through `-c hooks.*` keys, never a file under `~/.codex`. Codex
runs a hook only once its hash is trusted, and silently skips a `-c` hook that is not, so the hook
needs `--dangerously-bypass-hook-trust`. That flag trusts every hook of the run, so it is passed
only when no other hook source exists (hook_sources); otherwise the worker reads steer.md between
steps, as before.

Per-run plugins: not possible. Codex loads a plugin only from its install cache under
`$CODEX_HOME/plugins/cache` (`codex plugin add` copies it there); enabling a local marketplace's
plugin with `-c` alone loaded none of its skills (measured), and no `-c` key adds a skill folder."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import register
from .base import AUTH_RE, LIMIT_RE, Provider, RunUsage, cli_output, price_row, status_check, stderr_tail

# $ per million tokens: (input, cached input, output). Estimates only; the project may override
# them in project.json under pricing.codex.<model>. Unknown models use the "default" row.
PRICES = {"default": (4.0, 0.4, 20.0)}
# Tools a decision-only turn does without, switched off when this build lists them as features.
READ_ONLY_OFF = ("shell_tool", "unified_exec", "web_search_request")
HOOK_TRUST_BYPASS = "--dangerously-bypass-hook-trust"
IGNORE_USER_CONFIG = "--ignore-user-config"
IGNORE_RULES = "--ignore-rules"
# A config.toml line that may define a hook: a hooks table or key, or a plugin (plugins bundle hooks).
HOOK_CONFIG_RE = re.compile(r"^\s*(?:\[\[?\s*[\"']?(?:hooks|plugins)\b|[\"']?(?:hooks|plugins)[\"']?\s*[.=])", re.M)
# User config the run cannot reach its model without: ignoring it would break every run.
PROVIDER_CONFIG_RE = re.compile(r"^\s*(?:\[\s*)?[\"']?model_providers?\b", re.M)
# Linux refuses one argument longer than 128 KiB; longer system text leads the prompt instead.
MAX_ARG_BYTES = 120_000


@register
class Codex(Provider):
    name = "codex"
    binaries = ("codex",)
    api_host, api_base_env = "api.openai.com", "OPENAI_BASE_URL"
    login_hint = "run `codex login` there"
    isolate_read_only = True
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

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
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

    def compact_args(self, tokens: int) -> list[str]:
        # `model_auto_compact_token_limit`: history is compacted once it reaches this many tokens.
        return ["-c", f"model_auto_compact_token_limit={int(tokens)}"] if tokens and tokens > 0 else []

    def writable_args(self, dirs):
        # workspace-write only lets the worker write its cwd; result.json, `ttp note`, `ttp lock`
        # and commits in a worktree (whose git metadata lives in the main repository) are elsewhere.
        return ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(d) for d in dirs])] if dirs else []

    def isolation_args(self) -> list[str]:
        # The user's config.toml holds their MCP servers, plugins, hooks and profiles.
        out = self._ignore_user_config()
        if out and self._hook_held_by_user_config:
            out += self._hook_for(self._hook_held_by_user_config, user_config=False)
        return out

    def _exec_flag(self, flag: str) -> bool:
        return bool(re.search(rf"(?<![\w-]){re.escape(flag)}\b", cli_output(self.binary() or "codex", "exec", "--help")))

    def _ignore_user_config(self) -> list[str]:
        if not self._exec_flag(IGNORE_USER_CONFIG) or PROVIDER_CONFIG_RE.search(_read(codex_home() / "config.toml")):
            return []
        return [IGNORE_USER_CONFIG]

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

    def write_fence(self) -> str:
        return ""   # workspace-write: Seatbelt on macOS, bubblewrap or the bundled helper on Linux

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

    def _price(self, inp: int, cached: int, out: int) -> float:
        pin, pcached, pout = price_row(PRICES, self.prices, self.model)
        return ((inp - cached) * pin + cached * pcached + out * pout) / 1e6

    def cost_so_far(self, output_path):
        inp, cached, out, _ = self._tokens(output_path)
        return self._price(inp, cached, out)

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        inp, cached, out, last = self._tokens(output_path)
        u = RunUsage(input_tokens=inp - cached, cache_read_tokens=cached, output_tokens=out, final_text=last,
                     estimated=True)
        u.cost_usd = self._price(inp, cached, out)
        events, errors = 0, []
        for ev in self._events(output_path):
            events += 1
            if ev.get("type") == "thread.started":
                u.session_id = str(ev.get("thread_id") or "")   # what `codex exec resume` takes
            elif ev.get("type") == "turn.completed":
                errors = []   # transient errors Codex retried are reported as "error" events too
            elif ev.get("type") in ("turn.failed", "error"):
                errors.append(ev)
        err = stderr_tail(stderr_path)
        if errors:
            u.error = json.dumps(errors[-1])[:400]
        elif not events and err.strip():
            u.error = err.strip()[-400:]   # it failed before starting a session: only stderr says why
        if last.strip().startswith("{"):
            try:
                u.structured = drop_nulls(json.loads(last))
            except ValueError:
                pass
        blob = u.error + " " + err
        if AUTH_RE.search(blob) and not u.output_tokens:
            u.auth_failed = True
        elif LIMIT_RE.search(blob):
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
        """Plan windows via the app-server protocol; spends no model tokens. The server answers
        asynchronously, so stdin stays open until the reply arrives (closing it early loses it)."""
        exe = self.binary()
        if not exe:
            return []
        import select
        import time
        from ..budget import Window
        try:
            proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError:
            return []
        rl, allowed = {}, True
        try:
            for m in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "tt-project", "version": "1"}}},
                      {"jsonrpc": "2.0", "method": "initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}):
                proc.stdin.write(json.dumps(m) + "\n")
                proc.stdin.flush()
            deadline = time.time() + 25
            while time.time() < deadline:
                ready, _, _ = select.select([proc.stdout], [], [], 1)
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 2:
                    res = msg.get("result") or {}
                    rl, allowed = res.get("rateLimits") or {}, res.get("ordinaryUsageAllowed", True)
                    break
        except (OSError, ValueError):
            return []
        finally:
            proc.terminate()
        wins = []
        for key in ("primary", "secondary"):
            w = rl.get(key)
            if not w:
                continue
            mins = int(w.get("windowDurationMins") or 0)
            name = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m"
            util = float(w.get("usedPercent") or 0)
            # Keyed like the readings its runs report, so the gate matches them to the account.
            wins.append(Window("codex", name, 100.0 if not allowed else util, w.get("resetsAt"), self.account()))
        return wins


def codex_home() -> Path:
    """Where Codex keeps its login and saved sessions."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def hook_sources(cwd: str, user_config: bool = True) -> list[str]:
    """Files besides the harness's own `-c` keys that may give a run in `cwd` hooks Codex would
    otherwise ask the user to trust: `hooks.json` in Codex's home, its `config.toml` when the run
    loads it (`user_config`), and a `.codex/` folder in `cwd` or any folder above it. Hooks or
    plugins in a config.toml count, as plugins can bundle hooks. Read without a TOML parser, so a
    doubtful line counts too: a false find only keeps the hook off, as before."""
    home = codex_home()
    found = [str(home / "hooks.json")] if (home / "hooks.json").exists() else []
    if user_config and HOOK_CONFIG_RE.search(_read(home / "config.toml")):
        found.append(str(home / "config.toml"))
    for d in [Path(cwd), *Path(cwd).parents]:
        dot = d / ".codex"
        if dot.resolve() == home.resolve():
            continue   # Codex's own home (often ~/.codex), handled above
        if (dot / "hooks.json").exists():
            found.append(str(dot / "hooks.json"))
        if HOOK_CONFIG_RE.search(_read(dot / "config.toml")):
            found.append(str(dot / "config.toml"))
    return found


def hook_config_args() -> list[str]:
    """`-c` keys that route Bash calls and every tool result through `ttp.hook` (see the module
    docstring). PYTHONPATH is set in the command itself, so the worker's own commands keep theirs."""
    runtime = str(Path(__file__).resolve().parents[2])
    cmd = f"env PYTHONPATH={shlex.quote(runtime)} {shlex.quote(sys.executable)} -m ttp.hook"
    out = []
    for event, matcher in (("PreToolUse", "Bash"), ("PostToolUse", "*")):
        entry = f"[{{matcher={toml_string(matcher)},hooks=[{{type=\"command\",command={toml_string(f'{cmd} {event}')},timeout=20}}]}}]"
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


def strict_schema(schema):
    """`--output-schema` is enforced in strict mode: every object closed and every property
    required. Optional properties become nullable instead; drop_nulls() undoes that on the way back."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if "items" in out:
        out["items"] = strict_schema(out["items"])
    props = schema.get("properties")
    if schema.get("type") == "object" and isinstance(props, dict):
        required = set(schema.get("required") or [])
        out["properties"] = {}
        for name, sub in props.items():
            sub = strict_schema(sub)
            if name not in required:
                sub = _nullable(sub)
            out["properties"][name] = sub
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: dict) -> dict:
    # A repeated "null" in `type` (a property already nullable in the source schema) makes the
    # API reject the whole schema as "invalid schema keyword".
    t = sub.get("type")
    sub = dict(sub)
    types = (t if isinstance(t, list) else [t]) if t else []
    sub["type"] = types if "null" in types else [*types, "null"]
    if "enum" in sub and None not in sub["enum"]:
        sub["enum"] = [*sub["enum"], None]
    return sub


def drop_nulls(value):
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value


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

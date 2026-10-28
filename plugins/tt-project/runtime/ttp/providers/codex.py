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

Reads are not fenced, and the roots today include all of the project's state. Codex has no read
deny rules, so the web token is guarded only by the Bash hook (when it runs) refusing a command
that names `state/web.token`.

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
from .base import (AUTH_RE, LIMIT_RE, MAX_ARG_BYTES, Provider, RunUsage, cli_output, price_row, request_rejected,
                   status_check, stderr_tail, strict_schema, drop_nulls)

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
# User config the run cannot reach its model without (its provider, endpoint or login): ignoring it
# would break every run.
PROVIDER_CONFIG_RE = re.compile(r"^\s*(?:\[\s*)?[\"']?(?:model_providers?|cli_auth_credentials_store|forced_login_method|"
                                r"openai_base_url|chatgpt_base_url)\b", re.M)
# A command the harness's hook denied (logged by codex_core::tools::router): its text is the
# command's, so it says nothing about the run's login or limits.
HOOK_DENIAL_RE = re.compile(r"^.*Command blocked by PreToolUse hook.*$\n?", re.M)


@register
class Codex(Provider):
    name = "codex"
    binaries = ("codex",)
    api_host, api_base_env = "api.openai.com", "OPENAI_BASE_URL"
    login_hint = "run `codex login` there"
    efforts = ("minimal", "low", "medium", "high", "xhigh")
    isolate_read_only = True
    # Codex sends the thread id as the request's prompt_cache_key: one `codex exec` per coordinator
    # turn read 0 cached tokens on most turns with a byte-identical prefix (measured on 0.160).
    cache_key_per_session = True
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
        try:
            auth = json.loads(Path(os.path.expanduser("~/.codex/auth.json")).read_text())
        except (OSError, ValueError):
            return ""
        mode = auth.get("auth_mode") or ("api_key" if auth.get("OPENAI_API_KEY") else "chatgpt")
        return f"codex ({mode})"

    def account_kind(self) -> str:
        return self.account()

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
            name = "5h" if mins == 300 else "7d" if mins == 10080 else f"{mins}m"
            util = float(w.get("usedPercent") or 0)
            # Keyed like the readings its runs report, so the gate matches them to the account.
            wins.append(Window("codex", name, 100.0 if not allowed else util, w.get("resetsAt"), self.account()))
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

def codex_home() -> Path:
    """Where Codex keeps its login and saved sessions."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


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

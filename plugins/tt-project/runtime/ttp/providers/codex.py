# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""OpenAI Codex CLI (`codex exec --json`). Codex reports tokens but no cost, has no budget flag,
and exposes plan windows through `codex app-server` (`account/rateLimits/read`, no model tokens).
Cost is therefore an estimate from a price table the project can edit. Tokens arrive only when a
turn completes, so the runner's mid-run budget check sees completed turns only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import register
from .base import AUTH_RE, LIMIT_RE, Provider, RunUsage, cli_output, price_row, stderr_tail

# $ per million tokens: (input, cached input, output). Estimates only; the project may override
# them in project.json under pricing.codex.<model>. Unknown models use the "default" row.
PRICES = {"default": (4.0, 0.4, 20.0)}
# Tools a decision-only turn does without, switched off when this build lists them as features.
READ_ONLY_OFF = ("shell_tool", "unified_exec", "web_search_request")


@register
class Codex(Provider):
    name = "codex"
    binaries = ("codex",)
    login_hint = "run `codex login` there"
    isolate_read_only = True

    def credential_files(self) -> list[str]:
        return [str(Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")) / "auth.json")]

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions):
        exe = self.binary() or "codex"
        argv = [exe, "exec", "--json", "-C", cwd, "--skip-git-repo-check"]
        if model:
            argv += ["-m", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += ["-c", "approval_policy=never"]
        if read_only:
            argv += ["-s", "read-only"]
            features = cli_output(exe, "features", "list")
            for feature in READ_ONLY_OFF:
                if re.search(rf"^{feature}\b", features, re.M):
                    argv += ["-c", f"features.{feature}=false"]
        else:
            argv += ["-s", "workspace-write"]
            if not restrictions.get("no_internet"):
                argv += ["-c", "sandbox_workspace_write.network_access=true"]
        if schema:
            argv += ["--output-schema", schema_file(strict_schema(schema))]
        argv += ["-"]
        return argv, {}

    def writable_args(self, dirs):
        # workspace-write only lets the worker write its cwd; result.json, `ttp note`, `ttp lock`
        # and commits in a worktree (whose git metadata lives in the main repository) are elsewhere.
        return ["-c", "sandbox_workspace_write.writable_roots=" + json.dumps([str(d) for d in dirs])] if dirs else []

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
            if ev.get("type") == "turn.completed":
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
            wins.append(Window("codex", name, 100.0 if not allowed else util, w.get("resetsAt"), rl.get("planType") or ""))
        return wins


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
    t = sub.get("type")
    sub = dict(sub)
    sub["type"] = [*(t if isinstance(t, list) else [t]), "null"] if t else ["null"]
    if "enum" in sub:
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

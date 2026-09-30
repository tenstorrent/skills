# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What every agent harness adapter provides. Adapters build a command line and read results;
they never run anything themselves (the detached runner does), so they stay easy to test."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Services start with a minimal PATH; agent CLIs usually live in the user's own bin directories.
EXTRA_BIN_DIRS = ["~/.local/bin", "~/.npm-global/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin",
                  "~/.bun/bin", "~/.cargo/bin"]

AUTH_RE = re.compile(r"(authentication_failed|failed to authenticate|authentication required|oauth (session|token) (expired|invalid)|"
                     r"not logged in|please (run )?/?login|invalid api key|unauthorized|(status|http|error)[ :=]*401\b)", re.I)

LIMIT_RE = re.compile(r"(usage limit|limit reached|rate limit|quota exceeded|out of credits|"
                      r"insufficient (credits|balance|funds)|spend(ing)? limit)", re.I)


def find_binary(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
        for d in EXTRA_BIN_DIRS:
            c = Path(os.path.expanduser(d)) / n
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
    return None


def service_path() -> str:
    dirs = [os.path.expanduser(d) for d in EXTRA_BIN_DIRS] + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(d for d in dirs if os.path.isdir(d)))


@dataclass
class RunUsage:
    cost_usd: float = 0.0
    estimated: bool = False          # True when computed from token counts, not reported
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    final_text: str = ""             # the agent's last message
    structured: dict | None = None   # schema-validated output, when requested and supported
    auth_failed: bool = False        # the provider is logged out or its credentials are invalid
    limited: bool = False            # the provider refused for quota/limit reasons
    limit_note: str = ""
    error: str = ""
    session_id: str = ""
    extra: dict = field(default_factory=dict)


class Provider:
    name = "base"
    binaries: tuple[str, ...] = ()
    login_hint = "log in to the agent CLI there"   # how the user fixes "logged out" on this provider
    model = ""                       # the run's model and the project's price rows, for providers
    prices: dict = {}                # whose cost is estimated from tokens (see use())
    # Read-only turns run from scratch_dir(), not the project: this agent otherwise loads the
    # project's AGENTS.md or rules from its working directory into a decision-only turn.
    isolate_read_only = False

    def use(self, model: str = "", prices: dict | None = None) -> "Provider":
        """Price this run's tokens with `model` and the project's `pricing.<provider>` rows."""
        self.model, self.prices = model or "", dict(prices or {})
        return self

    def binary(self) -> str | None:
        return find_binary(*self.binaries)

    def credential_files(self) -> list[str]:
        """Files a login writes; a change to one ends a logged-out pause without waiting it out."""
        return []

    def credentials_stamp(self) -> str:
        """Cheap fingerprint (path, mtime, size) of credential_files(); "" when this provider has none."""
        parts = []
        for f in self.credential_files():
            try:
                st = os.stat(f)
                parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{f}:-")
        return "|".join(parts)

    def available(self) -> bool:
        return self.binary() is not None

    def build(self, *, role: str, model: str, effort: str, cwd: str, budget_usd: float | None,
              read_only: bool, schema: dict | None, restrictions: dict) -> tuple[list[str], dict]:
        """Return (argv, extra_env) for a headless run whose prompt arrives on stdin."""
        raise NotImplementedError

    def parse(self, output_path: Path, stderr_path: Path | None = None) -> RunUsage:
        raise NotImplementedError

    def plugin_args(self, dirs: list[str]) -> list[str]:
        """Arguments that load extra skill plugins for one run; [] when the agent cannot."""
        return []

    def append_system_args(self, path: Path) -> list[str]:
        """Arguments that add the text in `path` to the agent's system prompt, where a provider
        caches it across runs; [] when the agent cannot, and the text then leads the prompt."""
        return []

    def isolation_args(self) -> list[str]:
        """Arguments that keep the user's own MCP servers, plugins, hooks and settings out of a
        worker (the project's approved plugins and the harness hook still load); [] if unsupported."""
        return []

    def mcp_servers(self, names: list[str], dirs: list[str]) -> tuple[dict, list[str]]:
        """The named MCP server definitions from the user's own agent config, looked up for the
        directories `dirs`, and the names not found. They may hold secrets: never log them."""
        return {}, list(names)

    def with_mcp_config(self, argv: list[str], path: Path) -> list[str]:
        """`argv` that also loads the MCP servers in the config file `path`; unchanged if unsupported."""
        return argv

    def writable_args(self, dirs: list[str]) -> list[str]:
        """Arguments that let a sandboxed worker also write `dirs` (run dir, project state, git
        metadata); [] when the agent has no write sandbox."""
        return []

    def cost_so_far(self, output_path: Path) -> float | None:
        """Mid-run spend, when the provider streams it and does not enforce a budget itself."""
        return None

    def account(self) -> str:
        """Who pays: an email/org/plan label. Never a secret."""
        return ""

    def meter(self) -> list:
        """Plan-window utilization for the whole account, when the provider exposes it."""
        return []


_CLI_OUTPUT: dict[tuple, str] = {}


def cli_output(*argv: str) -> str:
    """What a quick CLI query (`--help`, a feature list) prints, cached per process; "" when it
    cannot run. Flags differ between CLI versions, so adapters check before using newer ones."""
    if argv not in _CLI_OUTPUT:
        try:
            out = subprocess.run(list(argv), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20)
            _CLI_OUTPUT[argv] = out.stdout + out.stderr
        except (OSError, subprocess.SubprocessError):
            _CLI_OUTPUT[argv] = ""
    return _CLI_OUTPUT[argv]


def scratch_dir(key: str) -> str:
    """An empty per-project directory outside any repository, for turns that need nothing on disk."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ttp" / "scratch"
    path = base / hashlib.sha256(key.encode()).hexdigest()[:16]
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(path)


def stderr_tail(stderr_path: Path | None, chars: int = 2000) -> str:
    try:
        return Path(stderr_path).read_text(errors="replace")[-chars:] if stderr_path else ""
    except OSError:
        return ""


def price_row(defaults: dict, overrides: dict, model: str) -> tuple[float, float, float]:
    """($ per million input, cached input, output tokens) for `model`: the project's
    `pricing.<provider>` rows win over the adapter's, and unknown models use "default"."""
    rows = {k: v for k, v in (overrides or {}).items() if isinstance(v, (list, tuple)) and len(v) == 3}
    table = {**defaults, **rows}
    return tuple(float(x) for x in (table.get(model) or table["default"]))


def last_json_object(text: str) -> dict | None:
    """The last top-level {...} in free text; used when a provider cannot enforce a schema."""
    import json
    depth, start, found = 0, None, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                found = (start, i + 1)
    if not found:
        return None
    try:
        return json.loads(text[found[0]:found[1]])
    except ValueError:
        return None

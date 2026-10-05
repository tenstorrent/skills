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
from urllib.parse import urlparse

# Services start with a minimal PATH; agent CLIs usually live in the user's own bin directories.
EXTRA_BIN_DIRS = ["~/.local/bin", "~/.npm-global/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin",
                  "~/.bun/bin", "~/.cargo/bin"]

AUTH_RE = re.compile(r"(authentication_failed|failed to authenticate|authentication required|oauth (session|token) (expired|invalid)|"
                     r"not logged in|please (run )?/?login|invalid api key|unauthorized|(status|http|error)[ :=]*401\b)", re.I)

LIMIT_RE = re.compile(r"(usage limit|limit reached|rate limit|quota exceeded|out of credits|"
                      r"insufficient (credits|balance|funds)|spend(ing)? limit)", re.I)


# The API takes at most this many cache_control breakpoints per request; one more fails the whole
# call with a 400 ("A maximum of 4 blocks with cache_control may be provided").
MAX_CACHE_BREAKPOINTS = 4
_TRIM_LOGGED: set[str] = set()


def cap_cache_breakpoints(blocks, limit: int = MAX_CACHE_BREAKPOINTS, source: str = "",
                          log=None) -> list:
    """Copies of `blocks` (content blocks, stable prefix first) with at most `limit` cache_control
    marks: the earliest are kept, as they cover the most stable prefix, and the later, more volatile
    ones are dropped. Logs once per `source` through `log` when it drops one. Never raises: on input
    it cannot read, every mark is dropped."""
    try:
        limit, kept, dropped, out = max(int(limit), 0), 0, 0, []
        for b in blocks:
            if isinstance(b, dict) and "cache_control" in b:
                if kept < limit:
                    kept += 1
                else:
                    b = {k: v for k, v in b.items() if k != "cache_control"}
                    dropped += 1
            out.append(b)
    except Exception:
        try:
            return [{k: v for k, v in b.items() if k != "cache_control"} if isinstance(b, dict) else b
                    for b in blocks]
        except Exception:
            return []
    if dropped and source not in _TRIM_LOGGED and log is not None:
        _TRIM_LOGGED.add(source)
        try:
            log(f"prompt cache: {source} had {kept + dropped} cache breakpoints with room for {limit}; "
                f"dropped {dropped} from the latest blocks")
        except Exception:
            pass
    return out


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
    # The API host a run needs to resolve: after a run could not reach it, new runs wait until it
    # resolves (Daemon.net_held). api_base_env names the variable that points the CLI elsewhere.
    api_host = ""
    api_base_env = ""
    login_hint = "log in to the agent CLI there"   # how the user fixes "logged out" on this provider
    model = ""                       # the run's model and the project's price rows, for providers
    prices: dict = {}                # whose cost is estimated from tokens (see use())
    # Read-only turns run from scratch_dir(), not the project: this agent otherwise loads the
    # project's AGENTS.md or rules from its working directory into a decision-only turn.
    isolate_read_only = False
    # Cache breakpoints the agent itself places on one request, at most (on any call of a run, not
    # only the first): runtime-added marks get what is left of MAX_CACHE_BREAKPOINTS.
    own_cache_breakpoints = 0

    def reach_host(self, env: dict | None = None) -> str:
        """The host this provider's runs reach its API at: the base URL's host when its variable is
        set, else api_host. Empty: nothing to check."""
        base = (os.environ if env is None else env).get(self.api_base_env, "") if self.api_base_env else ""
        if base:
            return urlparse(base if "://" in base else "https://" + base).hostname or ""
        return self.api_host

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

    def login_check(self) -> bool | None:
        """Whether the agent CLI is logged in, asked without a model call (its own status command):
        True or False, or None when this CLI has no such check (a run then checks the login)."""
        return None

    def build(self, *, role: str, model: str, effort: str, cwd: str, budget_usd: float | None,
              read_only: bool, schema: dict | None, restrictions: dict) -> tuple[list[str], dict]:
        """Return (argv, extra_env) for a headless run whose prompt arrives on stdin."""
        raise NotImplementedError

    def parse(self, output_path: Path, stderr_path: Path | None = None) -> RunUsage:
        raise NotImplementedError

    def plugin_args(self, dirs: list[str]) -> list[str]:
        """Arguments that load extra skill plugins for one run; [] when the agent cannot."""
        return []

    def session_args(self, session_id: str) -> list[str]:
        """Arguments that make a new run's agent session use `session_id`, so the session is known
        before the agent writes anything; [] when the agent cannot."""
        return []

    def resume_args(self, session_id: str) -> list[str]:
        """Arguments that continue the saved agent session `session_id` instead of starting a new
        one; [] when the agent cannot, and a lost run then starts fresh."""
        return []

    def session_saved(self, session_id: str, cwd: str, env: dict | None = None) -> bool:
        """Whether the transcript of `session_id`, run in `cwd` with the environment `env` (the run's
        own, which may point the agent at another config directory), is still on disk to resume."""
        return False

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

    def write_fence(self) -> str:
        """Why this provider's workers can write outside their writable dirs, as built today; ""
        when an OS sandbox fences them. Shown by `ttp doctor`; changes nothing."""
        return "no write sandbox"

    def compact_env(self, tokens: int) -> dict[str, str]:
        """Environment that makes the agent compact its context near `tokens`; {} when it has no
        such switch or `tokens` is 0."""
        return {}

    def compact_args(self, tokens: int) -> list[str]:
        """Arguments that make the agent compact its context near `tokens`, for agents whose switch
        is a flag or config override rather than an environment variable; [] if none or 0."""
        return []

    def cap_output(self, argv: list[str], chars: int) -> list[str]:
        """`argv` with the agent keeping a command's output inline only up to `chars` characters (the
        rest in a file it is pointed to); `argv` unchanged when it has no such setting or `chars` is 0."""
        return argv

    def cache_env(self, ttl: str) -> dict[str, str]:
        """Environment that sets the prompt cache lifetime (`5m`, `1h`); {} when the agent has no
        such switch or `ttl` is not one it takes."""
        return {}

    def cached_input(self, stable: str, rest: str, ttl: str, log=None) -> tuple[list[str], str] | None:
        """Arguments and stdin for a prompt sent as two blocks with a cache breakpoint after
        `stable`, so a change in `rest` alone re-reads `stable` from the cache; None when the agent
        cannot mark one or has no breakpoint to spare (the caller then puts `stable` in the system
        prompt as before). Blocks go through cap_cache_breakpoints; `log` takes its trim note."""
        return None

    def streams(self, argv: list[str]) -> bool:
        """Whether a run started with `argv` writes its output as it goes, so an empty output means
        the agent did nothing (rather than that it had not finished)."""
        return True

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


def status_check(argv: list[str], timeout_s: float = 30) -> tuple[int, str] | None:
    """Exit code and output of a quick, model-free status command; None when it cannot run."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.returncode, out.stdout + out.stderr


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

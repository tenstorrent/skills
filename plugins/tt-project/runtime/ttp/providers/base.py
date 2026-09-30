# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What every agent harness adapter provides. Adapters build a command line and read results;
they never run anything themselves (the detached runner does), so they stay easy to test."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Services start with a minimal PATH; agent CLIs usually live in the user's own bin directories.
EXTRA_BIN_DIRS = ["~/.local/bin", "~/.npm-global/bin", "~/bin", "/opt/homebrew/bin", "/usr/local/bin",
                  "~/.bun/bin", "~/.cargo/bin"]

AUTH_RE = re.compile(r"(authentication_failed|failed to authenticate|oauth (session|token) (expired|invalid)|"
                     r"not logged in|please (run )?/?login|invalid api key|unauthorized|401)", re.I)

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

    def binary(self) -> str | None:
        return find_binary(*self.binaries)

    def available(self) -> bool:
        return self.binary() is not None

    def build(self, *, role: str, model: str, effort: str, cwd: str, budget_usd: float | None,
              read_only: bool, schema: dict | None, restrictions: dict) -> tuple[list[str], dict]:
        """Return (argv, extra_env) for a headless run whose prompt arrives on stdin."""
        raise NotImplementedError

    def parse(self, output_path: Path, stderr_path: Path | None = None) -> RunUsage:
        raise NotImplementedError

    def cost_so_far(self, output_path: Path) -> float | None:
        """Mid-run spend, when the provider streams it and does not enforce a budget itself."""
        return None

    def account(self) -> str:
        """Who pays: an email/org/plan label. Never a secret."""
        return ""

    def meter(self) -> list:
        """Plan-window utilization for the whole account, when the provider exposes it."""
        return []


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

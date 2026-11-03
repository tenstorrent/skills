"""sandbox.fence_freed_commands: a project's own host-reaching commands (a script in its harness bin/ that
wraps ssh, say) that the Claude Code Bash sandbox frees by name as it frees ssh (providers/claude.py
FENCE_EXCLUDED), and that the worker hook checks as it checks ssh (hook.fenced_ssh). The project lists
them in project.json, never in those template-owned lines: a local edit there turns each upgrade whose
template changes them into a merge task.

Each entry is a command name (`"my-ssh"`) or `{"command": "my-ssh", "env": ["MY_SSH_OPTS"]}`, which also
frees `MY_SSH_OPTS=... my-ssh ...`. Only a name that is an executable file in the harness's bin/ counts
(workers cannot write there): any other entry is left out, with one daemon log line saying why. Names the
plugin ships there (ttp, git, gh) and shells or wrappers are refused: freeing one frees all it runs.
The daemon reads the key at each worker start and passes the names to the hook (TTP_FENCE_FREED)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ENV = "TTP_FENCE_FREED"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What freeing would free every command of: the harness's own wrappers, shells and command wrappers
# (hook.SHELLS and hook.WRAPPERS).
REFUSED = {"ttp", "git", "gh", "eval", "sh", "bash", "dash", "zsh", "ksh", "env", "nohup", "setsid", "nice",
           "exec", "command", "time", "stdbuf", "ionice", "timeout", "xargs", "sudo"}


def commands(base: Path | str, value: Any) -> tuple[list[tuple[str, tuple[str, ...]]], list[str]]:
    """The project's (tt-project folder `base`) fence_freed_commands `value`: ([(name, env var names)],
    ["entry: why" left out])."""
    freed: dict[str, tuple[str, ...]] = {}
    refused: list[str] = []
    if value in (None, [], ""):
        return [], refused
    if not isinstance(value, list):
        return [], [f"{json.dumps(value)[:60]}: not a list of command names"]
    bin_dir = Path(base) / "harness" / "bin"
    for entry in value:
        name, env = (entry, []) if isinstance(entry, str) else \
            (entry.get("command"), entry.get("env", [])) if isinstance(entry, dict) else (None, [])
        env = [env] if isinstance(env, str) else env
        shown = json.dumps(entry)[:60]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            refused.append(f"{shown}: not a command name (a file name in the harness's bin/, no path)")
        elif name in REFUSED:
            refused.append(f"{name}: a shell, wrapper or harness command (freeing it frees all it runs)")
        elif not ((bin_dir / name).is_file() and os.access(bin_dir / name, os.X_OK)):
            refused.append(f"{name}: no executable {bin_dir / name}")
        elif not isinstance(env, list) or not all(isinstance(v, str) and VAR_RE.fullmatch(v) for v in env):
            refused.append(f"{shown}: env is not a list of variable names")
        else:
            freed[name] = tuple(dict.fromkeys([*freed.get(name, ()), *env]))
    return list(freed.items()), refused


def patterns(freed) -> list[str]:
    """excludedCommands patterns for `freed` ([(name, env var names)]), in the built-in ones' form."""
    return [p for name, env in freed for p in (f"{name} *", *(f"{v}=* {name} *" for v in env))]


def names() -> set[str]:
    """The project's freed command names for this run, as the daemon passed them to the hook."""
    return {n for n in os.environ.get(ENV, "").split() if NAME_RE.fullmatch(n) and n not in REFUSED}

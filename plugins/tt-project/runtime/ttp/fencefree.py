"""sandbox.fence_freed_commands: a project's own host-reaching commands (a script in its harness bin/ that
wraps ssh, say) that the Claude Code Bash sandbox frees by name as it frees ssh (providers/claude.py
FENCE_EXCLUDED), and that the worker hook checks as it checks ssh (hook.fenced_ssh). The project lists
them in project.json, never in those template-owned lines: a local edit there turns each upgrade whose
template changes them into a merge task.

Each entry is a command name (`"my-ssh"`); an older `{"command": "my-ssh", "env": [...]}` entry frees
the name alone. The Claude sandbox frees no line that starts with an assignment (measured on CLI
2.1.296: `BAZ=1 my-ssh *` did not free `BAZ=1 my-ssh y`, nor `FOO=* my-ssh *` `FOO=x my-ssh y`, while
`my-ssh *` freed `my-ssh y`), so an env part adds no pattern and gets one daemon log line. `*` matches
across words (`my-ssh * end` freed `my-ssh a b end`). Only a name that is an executable file in the
harness's bin/ counts (workers cannot write there): any other entry is left out, with one daemon log
line saying why. Names the plugin ships there (ttp, git, gh) and shells or wrappers
are refused: freeing one frees all it runs.
The daemon reads the key at each worker start and passes the names to the hook (TTP_FENCE_FREED)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ENV = "TTP_FENCE_FREED"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
# What freeing would free every command of: the harness's own wrappers, shells and command wrappers
# (hook.SHELLS and hook.WRAPPERS).
REFUSED = {"ttp", "git", "gh", "eval", "sh", "bash", "dash", "zsh", "ksh", "env", "nohup", "setsid", "nice",
           "exec", "command", "time", "stdbuf", "ionice", "timeout", "xargs", "sudo"}


def commands(base: Path | str, value: Any) -> tuple[list[str], list[str]]:
    """The project's (tt-project folder `base`) fence_freed_commands `value`: ([freed command names],
    ["entry: why" left out or ignored])."""
    freed: list[str] = []
    refused: list[str] = []
    if value in (None, [], ""):
        return [], refused
    if not isinstance(value, list):
        return [], [f"{json.dumps(value)[:60]}: not a list of command names"]
    bin_dir = Path(base) / "harness" / "bin"
    for entry in value:
        name, env = (entry, None) if isinstance(entry, str) else \
            (entry.get("command"), entry.get("env")) if isinstance(entry, dict) else (None, None)
        shown = json.dumps(entry)[:60]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            refused.append(f"{shown}: not a command name (a file name in the harness's bin/, no path)")
        elif name in REFUSED:
            refused.append(f"{name}: a shell, wrapper or harness command (freeing it frees all it runs)")
        elif not ((bin_dir / name).is_file() and os.access(bin_dir / name, os.X_OK)):
            refused.append(f"{name}: no executable {bin_dir / name}")
        else:
            if env:
                refused.append(f"{name} env {json.dumps(env)[:60]}: ignored, the Claude sandbox never frees a command "
                               f"line that starts with an assignment ({name} alone stays freed)")
            if name not in freed:
                freed.append(name)
    return freed, refused


def patterns(freed) -> list[str]:
    """excludedCommands patterns for `freed` (command names), in the built-in ones' form."""
    return [f"{name} *" for name in freed]


def names() -> set[str]:
    """The project's freed command names for this run, as the daemon passed them to the hook."""
    return {n for n in os.environ.get(ENV, "").split() if NAME_RE.fullmatch(n) and n not in REFUSED}

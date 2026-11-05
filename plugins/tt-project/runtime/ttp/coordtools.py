# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Read-only tools and the per-wake budget of a coordinator in session mode (coordinator.session.mode
"on"). With the mode off or unset, nothing here changes how a coordinator turn launches.

Tools: Read (and Grep/Glob where the CLI has them) on the project folder, and Bash for a fixed list of
read-only `ttp` commands, `ttp hostcheck` among them (hostcheck.py: git log/show, lock holders and the
other host checks). No write tool, no free shell. Three fences hold it: the CLI's tool list, its
allow and deny rules under the dontAsk permission mode (anything not allowed is refused), and a
PreToolUse hook (pre_tool_use) that denies every call outside the list, also a Bash line with a shell
operator, redirect or substitution.

Budget: a wake stops at whichever comes first of coordinator.session.wake_budget_usd (the CLI's
--max-budget-usd) and wake_budget_s (the run's timeout); there is no tool-call cap. Unset, each is
what the stateless turn it replaces gets: the sized turn budget (coordinator.turn_budget_usd, default
$1) and the effort-sized turn timeout (coordlimits.turn_timeout). Why: a wake is started by exactly the
events, gates and hourly cap that start a stateless turn today, one wake per turn, so a per-wake
ceiling equal to the per-turn ceiling keeps a project's daily ceiling at or under the stateless
design. A typical wake costs less than its ceiling: the session's cached context is read, not resent.
A wake that hits either limit ends with what it has; that non-ok end takes the stateless rerun path."""
from __future__ import annotations

import os
import re
import shlex
import sys

from .coordlimits import positive

HOOK_EVENT = "CoordPreToolUse"
DENIED_ENV = "TTP_COORD_DENIED_READS"
FILE_TOOLS = ("Read", "Grep", "Glob")
CLI_TOOLS = ("Read", "Bash")   # Grep and Glob are not in every CLI: --tools names only these two
# `ttp <words...>` prefixes a session coordinator may run, each followed by any plain arguments.
# "*" is one project name: the CLI rules name the coordinator's own, the hook takes any (all read-only).
READ_COMMANDS = (("status",), ("task", "*", "show"), ("task", "*", "list"), ("said",), ("eta",), ("landed",),
                 ("stats",), ("hostcheck",), ("machines", "list"), ("machines", "activity"), ("machines", "status"))
# Anything a shell would act on: operators, redirects, substitutions, globs, comments, escapes.
SHELL_META = re.compile(r"[;&|<>`$\\(){}*?\[\]!#\n\r]")


def session(cfg: dict) -> dict:
    s = (cfg.get("coordinator") or {}).get("session")
    return s if isinstance(s, dict) else {}


def session_on(cfg: dict) -> bool:
    """coordinator.session.mode == "on"; missing, malformed or anything else is off."""
    return str(session(cfg).get("mode") or "").strip().lower() == "on"


def _rules(project: str) -> list[str]:
    out = []
    for words in READ_COMMANDS:
        line = " ".join(project if w == "*" else w for w in ("ttp", *words))
        out += [f"Bash({line})", f"Bash({line} *)"]
    return out


def hook_settings(denied_reads=()) -> dict:
    """The coordinator's PreToolUse hook (every tool goes through it) and the files it never reads,
    as --settings JSON."""
    from pathlib import Path
    runtime = str(Path(__file__).resolve().parents[1])
    cmd = f"{shlex.quote(sys.executable)} -m ttp.hook {HOOK_EVENT}"
    deny = [str(p) for p in denied_reads if os.path.isabs(p)]
    return {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 20}]}]},
            "permissions": {"deny": [f"Read(/{p})" for p in deny]},
            "env": {"PYTHONPATH": runtime, DENIED_ENV: os.pathsep.join(deny)}}


def session_tool_args(cfg: dict, *, project: str = "", root: str = "", denied_reads=()) -> list[str]:
    """The CLI arguments that give a session coordinator its read-only tools ([] with the mode off).
    They take the place of the stateless turn's `--tools ""`."""
    if not session_on(cfg):
        return []
    import json
    allow = [f"Read(/{os.path.abspath(root)}/**)"] if root else []
    deny = [f"Read(/{p})" for p in denied_reads if os.path.isabs(p)]
    args = ["--tools", ",".join(CLI_TOOLS), "--allowedTools", *allow, *_rules(project or "*")]
    if deny:   # one comma-separated value, so a later flag is no rule
        args += ["--disallowedTools", ",".join(deny)]
    return args + ["--settings", json.dumps(hook_settings(denied_reads))]


def _denied_read(path: str, denied: list[str], search: bool = False) -> bool:
    """Whether reading `path` reads a denied file; a search (Grep, Glob) also when it would walk into one."""
    try:
        real = os.path.realpath(path)
        files = [os.path.realpath(d) for d in denied]
    except (OSError, ValueError):
        return True
    return any(real == f or search and f.startswith(real.rstrip(os.sep) + os.sep) for f in files)


def bash_refusal(cmd: str) -> str | None:
    """Why the coordinator may not run `cmd` (None: a listed read-only `ttp` command)."""
    if SHELL_META.search(cmd):
        return "a session coordinator runs one plain read-only command: no shell operators, redirects or substitutions"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "the command does not parse"
    if argv[:1] != ["ttp"]:
        return ("a session coordinator runs only read-only ttp commands (status, task <project> show|list, said, eta, "
                "landed, stats, machines list|activity|status, hostcheck)")
    for words in READ_COMMANDS:
        head = argv[1:1 + len(words)]
        if len(head) == len(words) and all(w == "*" and not h.startswith("-") or h == w for w, h in zip(words, head)):
            return None
    return f"`ttp {' '.join(argv[1:3])}` is not one of the coordinator's read-only commands"


def pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    """Deny every coordinator tool call outside its read-only list; no opinion on the rest, so the
    CLI's own allow rules still apply. Any error denies."""
    from .hook import _deny
    try:
        tool = str(payload.get("tool_name") or "")
        args = payload.get("tool_input") or {}
        if tool in FILE_TOOLS:
            denied = [d for d in os.environ.get(DENIED_ENV, "").split(os.pathsep) if d]
            search = tool != "Read"
            path = str(args.get("file_path") or args.get("path") or (os.getcwd() if search else ""))
            if path and _denied_read(path, denied, search):
                return _deny("that file holds a credential; a coordinator never reads it"), None
            return None, None
        if tool == "Bash":
            why = bash_refusal(str(args.get("command") or ""))
            return (_deny(why) if why else None), None
        return _deny(f"a session coordinator has read-only tools only; {tool or 'this tool'} is not one"), None
    except Exception as e:   # fail closed
        return _deny(f"the coordinator tool check failed ({type(e).__name__})"), None


def wake_budget(cfg: dict, turn_usd: float | None, turn_timeout_s: float) -> tuple[float | None, float]:
    """(dollars, seconds) one session wake may use: wake_budget_usd and wake_budget_s where set and
    positive, else the stateless turn's own (module doc)."""
    s = session(cfg)
    usd = positive(s.get("wake_budget_usd"), 0) or turn_usd
    secs = positive(s.get("wake_budget_s"), 0) or turn_timeout_s
    return usd, secs

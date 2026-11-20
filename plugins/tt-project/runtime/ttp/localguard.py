# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A project's own tool guards (project.json `guards.local`), the supported place for a rule the
built-in guards do not have, so no project edits runtime/ files (an edited runtime/ttp/hook.py makes
`ttp upgrade` conflict and leaves the project on an old release).

`guards.local` lists Python files by path, relative to the project's harness folder (absolute and `~`
also work), e.g. `["guards/no_prod_restart.py"]`. Keep them outside runtime/ and prompts/: upstream
ships nothing elsewhere, so an upgrade never touches or conflicts on them. Each module defines
`refusal(cmd: str) -> str | None`, asked about every shell command (the reason it refuses, else None),
and may define `tool_refusal(tool: str, tool_input: dict) -> str | None` for every other tool call (file
edits, connector calls). Keep them fast and side-effect free: they run on each tool call.

hook.local_guard asks them after the built-in guards, from every tool check the harness runs:
- the worker hook (hook.pre_tool_use): Claude Code on every tool; Codex on what its hook matches
  (shell, apply_patch edits with the patch text as tool_input, mcp__ connector calls: its other tools,
  such as web search, are not asked); Cursor through its per-run hooks.json (cursorhook.translate, the
  call in Claude's shape: Shell is Bash, every file edit is Write, MCP:<tool> is mcp__cursor__<tool>);
- the runner-side tool guard (streamguard) of runs the hook does not reach: it asks hook.pre_tool_use
  about each call read from the run's output and stops the run on a refusal, once the call has started;
- a session coordinator's tool check (coordtools.pre_tool_use), read-only and full tools, its Read,
  Grep and Glob calls included.
A refusal blocks the call and is logged to the run's refusal log with kind `local:<file name>`. A listed
guard is a safety rule and is never skipped: a file that is missing, fails to import, has no `refusal`,
or raises (or calls sys.exit) refuses every call it would check (a module that does not load: every
shell command), naming itself and the error. A listed guard file is edited, moved or named in a shell
command only by a harness task's run (a full-tools coordinator included: it gives the change to one).

One case cannot refuse: a project.json that does not read and has no last good copy (project.py
read_config "unavailable") lists no guards, so none is asked. Refusing every call instead would also stop
the runs that could fix it; the daemon starts no new run then and says so in its config alert, naming
the local guards, and `ttp doctor` reports it as a problem. Otherwise `ttp doctor` reports each listed guard and whether it loads."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from pathlib import Path

KEY = "guards.local"
_cache: dict[tuple[str, int, int], object] = {}


def harness_dir() -> Path | None:
    base = os.environ.get("TTP_PROJECT")
    return Path(base) / "harness" if base else None


def paths(cfg: dict, harness: Path | None) -> tuple[list[Path], str | None]:
    """The listed guard files, and why the setting itself is unusable (then every shell command is refused)."""
    section = cfg.get("guards")
    if section and not isinstance(section, dict):
        return [], "guards is not an object with a `local` list"
    raw = (section or {}).get("local")
    if not raw:
        return [], None
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        return [], f"{KEY} is not a list of file paths"
    out = []
    for x in raw:
        p = Path(os.path.expanduser(x.strip()))
        if not p.is_absolute():
            if harness is None:
                return [], f"{KEY} names {x}, a relative path, outside a project"
            p = harness / p
        out.append(p)
    return out, None


def names_guard(cfg: dict, harness: Path | None, tool: str, tool_input: dict, cwd: str) -> Path | None:
    """The listed guard file this call edits (an edit tool on it) or names (a shell command with its
    path as listed or in full), else None: like the gh pre-hooks, guards change only in a harness task."""
    files, _ = paths(cfg, harness)
    if not files:
        return None
    if tool == "Bash":
        cmd = str(tool_input.get("command") or "")
        raw = [x.strip() for x in ((cfg.get("guards") or {}).get("local") or [])]
        return next((f for f, x in zip(files, raw) if _names(cmd, x) or _names(cmd, str(f))), None)
    name = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if not name:
        return None
    path = os.path.realpath(os.path.join(cwd, os.path.expanduser(name)))
    return next((f for f in files if os.path.realpath(f) == path), None)


def _names(cmd: str, path: str) -> bool:
    """`cmd` names `path` as whole path components: `a.py` matches `rm a.py` and `rm ./a.py`, never
    `data.py` or `a.py.bak`."""
    return bool(path) and re.search(r"(?<![\w.~-])" + re.escape(path) + r"(?![\w.-])", cmd) is not None


def load(path: Path):
    """The guard module at `path` (re-read once it changes); raises on any failure."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _cache:
        name = "ttp_localguard_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("not a Python file")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "refusal", None)):
            raise AttributeError("defines no refusal(cmd)")
        _cache[key] = mod
    return _cache[key]


def _broken(path: Path, e: BaseException) -> str:
    return (f"the project's local guard {path.name} ({KEY}) failed ({type(e).__name__}: {str(e)[:200]}), so the "
            f"calls it checks are refused until it is fixed or taken out of {KEY}")


def refusal(cfg: dict, tool: str, tool_input: dict, harness: Path | None = None) -> tuple[str, str] | None:
    """(kind, reason) of the first listed guard that refuses this tool call, else None."""
    files, bad = paths(cfg, harness if harness is not None else harness_dir())
    if bad:
        return ("local", f"{bad}; shell commands are refused until it is fixed") if tool == "Bash" else None
    cmd = str((tool_input or {}).get("command") or "")
    for f in files:
        kind = f"local:{f.name}"
        try:
            mod = load(f)
        except (Exception, SystemExit) as e:   # not loaded: its tool_refusal is unknown, its refusal(cmd) is not
            if tool == "Bash":
                return kind, _broken(f, e)
            continue
        check = getattr(mod, "tool_refusal", None)
        try:
            if tool == "Bash":
                why = mod.refusal(cmd)
            elif callable(check):
                why = check(tool, dict(tool_input or {}))
            else:
                continue
        except (Exception, SystemExit) as e:   # a guard's sys.exit is its failure, not the hook's exit
            return kind, _broken(f, e)
        if why:
            return kind, f"{str(why).rstrip('.')} (the project's local guard {f.name})"
    return None


UNREAD = ("project.json does not read and there is no last good copy, so its local guards ({key}), if it "
          "lists any, are not asked on tool calls until it reads again").format(key=KEY)


def report(cfg: dict, harness: Path | None, status: str = "ok") -> list[str]:
    """`ttp doctor` lines: each listed guard and whether it loads; a problem line starts with 'problem'.
    `status`: how project.json read (project.read_config)."""
    if status == "unavailable":
        return [f"problem: {UNREAD}"]
    files, bad = paths(cfg, harness)
    if bad:
        return [f"problem: {bad}; every shell command of the project's runs is refused"]
    out = []
    for f in files:
        under = harness is not None and f.is_relative_to(harness) and f.relative_to(harness).parts[:1] in (
            ("runtime",), ("prompts",), ("template",))
        try:
            load(f)
            out.append(f"local guard {f}: loads" + ("; problem: it sits in a folder `ttp upgrade` merges, "
                                                     "move it out of runtime/ and prompts/" if under else ""))
        except (Exception, SystemExit) as e:
            out.append(f"problem: local guard {f} does not load ({type(e).__name__}: {str(e)[:200]}); "
                       f"its calls are refused")
    return out

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What a run that ended without a valid hand-off did outside its own folders, found model-free.

A run that exits non-zero or writes no result.json says nothing about its side effects, and the retry
or fallback run that follows starts blind: it may report 'nothing ran' over a run that overwrote files
elsewhere. When such a run ends, the daemon reads its transcript (output.jsonl, in the format of
whichever provider ran it: Claude Code and the chat bridge's stream-json, Codex's `exec --json`
items, Cursor's tool_call events) and the hook's log of rewritten commands (refusals.jsonl, kind
`rewrite`, which ran), and lists every write-like shell command's paths and every file edit (Write,
Edit, MultiEdit, NotebookEdit, apply_patch, a Codex file_change, an MCP write tool) that lies outside
the run's working directory and run dir. A shell command counts when it redirects into a file or runs
a writing verb; any command naming a path under the home's config or credential folders counts too.
The list (paths only, never a command's text or a file's contents) goes to the run dir's
outside.json, to the next run of the task as one prompt block, and to the coordinator as an
`outside_writes` event, high when a path is under those folders."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FILE = "outside.json"
KIND = "outside_writes"
MAX_PATHS = 15   # named in the prompt block and the event; the rest are counted
# Under the home: config and credential folders and files (a path there makes the event high)
SENSITIVE = (".config", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials",
             ".gitconfig", ".pypirc", ".npmrc", ".local/share/keyrings", ".claude", ".codex", ".cursor")
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path", "target", "destination", "dest", "source", "target_file")
MCP_WRITE_RE = re.compile(r"write|edit|create|move|rename|delete|remove|patch|upload|mkdir|copy", re.I)
PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
# A path in shell text: ~ or $HOME first, or absolute after a word break (not s/a/b/ or a URL's //host)
SHELL_PATH_RE = re.compile(r"""(?:^|(?<=[\s'"=<>(]))(?:(?:~|\$\{?HOME\}?|\$\{?TTP_RUN_DIR\}?)?/[^\s'"`;|&<>()]*"""
                           r"""|(?:~|\$\{?HOME\}?)(?=[\s'"`;|&<>()]|$))""")
REDIRECT_RE = re.compile(r"(?<![0-9&<>])(?:[0-9]?>>?|&>>?)\s*(?!&)([^\s;|&<>()'\"]+|'[^']+'|\"[^\"]+\")")
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&<>()]+)")
CD_RE = re.compile(r"(?:^|[;&|(]\s*|\n\s*)cd\s+([^\s;|&<>()]+)")
WRITE_RE = re.compile(r"\b(?:rm|mv|cp|ln|mkdir|rmdir|touch|chmod|chown|install|rsync|dd|truncate|tee|shred|unlink|"
                      r"ssh-keygen|scp|unzip|patch|wget)\b|\bcurl\b[^|;&\n]*\s-o\b|\bsed\s+(?:-\w+\s+)*-i|\bperl\s+-\w*i|\bgit\s+(?:-C\s+\S+\s+)?"
                      r"(?:config|clone|init|worktree|checkout|reset|commit|stash|apply)\b|open\([^)]*['\"][wax]\+?b?['\"]|"
                      r"\.write_(?:text|bytes)\(|\bshutil\.|\bos\.(?:remove|replace|rename|unlink|chmod|makedirs|mkdir)\(")
HARMLESS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/", "/dev/tty", "/proc/self/")


def _expand(path: str, home: str, cwd: str, run_dir: str, base: str | None = None) -> str:
    p = path.strip("'\"")
    p = re.sub(r"^\$\{?TTP_RUN_DIR\}?", run_dir, p)
    p = re.sub(r"^\$\{?HOME\}?", home, p)
    if p == "~" or p.startswith("~/"):
        p = home + p[1:]
    if not p.startswith("/"):
        p = os.path.join(base or cwd, p)
    return os.path.normpath(p)


def _inside(path: str, roots: list[str]) -> bool:
    if path.startswith(HARMLESS) or path in HARMLESS:
        return True
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def sensitive(path: str, home: str) -> bool:
    """`path` is under one of the home's config or credential folders (SENSITIVE)."""
    h = os.path.normpath(home)
    return any(path == f"{h}/{s}" or path.startswith(f"{h}/{s}/") for s in SENSITIVE)


def _shell_paths(cmd: str, home: str, cwd: str, run_dir: str) -> list[str]:
    """The paths a shell command names: its absolute and home paths but its `cd` targets, and its
    redirection and tee targets (a relative one after `cd <dir>` resolves under that dir)."""
    cds = [m.group(1) for m in CD_RE.finditer(cmd)]
    base = _expand(cds[-1], home, cwd, run_dir) if cds else None
    dirs = {_expand(d, home, cwd, run_dir) for d in cds}   # where it went, not what it touched
    out = [p for m in SHELL_PATH_RE.finditer(cmd) if (p := _expand(m.group(0), home, cwd, run_dir)) not in dirs]
    out += [_expand(m.group(1), home, cwd, run_dir, base) for r in (REDIRECT_RE, TEE_RE) for m in r.finditer(cmd)]
    return [p for p in out if len(p) > 1]


def _patch_paths(text: str) -> list[str]:
    return [(m.group(1) or m.group(2)).strip() for m in PATCH_RE.finditer(text or "")]


def _arg_paths(args) -> list[str]:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []
    return [v for k in PATH_KEYS if isinstance(v := args.get(k), str) and v]


def calls(run_dir: Path) -> list[tuple[str, str, str]]:
    """(tool, 'shell' or 'file', command text or path) of each call in the run's transcript, in any
    shipped provider's format, plus the hook's rewritten commands."""
    out: list[tuple[str, str, str]] = []

    def tool_use(name: str, inp: dict) -> None:
        if not isinstance(inp, dict):
            return
        if isinstance(inp.get("command"), str) and name not in EDIT_TOOLS and name != "apply_patch":
            out.append((name, "shell", inp["command"]))
        elif name in EDIT_TOOLS or (name.startswith("mcp__") and MCP_WRITE_RE.search(name.split("__")[-1])):
            out.extend((name, "file", p) for p in _arg_paths(inp))
        elif name == "apply_patch":
            text = next((v for k in ("input", "patch", "command") if isinstance(v := inp.get(k), str)), "")
            out.extend((name, "file", p) for p in _patch_paths(text))

    for ev in _jsonl(run_dir / "output.jsonl"):
        content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
        for b in content if isinstance(content, list) else []:   # Claude Code, the chat bridge, Cursor text
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_use(str(b.get("name") or ""), b.get("input") or {})
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and ev.get("type") in ("item.started", "item.completed"):   # Codex
            kind = item.get("type") or item.get("item_type") or ""
            if kind == "command_execution" and isinstance(item.get("command"), str):
                out.append(("shell", "shell", item["command"]))
            elif kind == "file_change":
                out += [("file_change", "file", str(c["path"])) for c in item.get("changes") or []
                        if isinstance(c, dict) and c.get("path")]
            elif kind == "mcp_tool_call" and MCP_WRITE_RE.search(str(item.get("tool") or "")):
                out += [(f"{item.get('server') or ''}.{item.get('tool')}", "file", p)
                        for p in _arg_paths(item.get("arguments"))]
        tc = ev.get("tool_call") if ev.get("type") == "tool_call" and isinstance(ev.get("tool_call"), dict) else None
        for name, body in (tc or {}).items():   # Cursor: {"shellToolCall": {"args": {...}}, ...}
            args = body.get("args") if isinstance(body, dict) and isinstance(body.get("args"), dict) else {}
            if isinstance(args.get("command"), str):
                out.append((name, "shell", args["command"]))
            elif re.search(r"write|edit|delete|patch", name, re.I):
                out += [(name, "file", p) for p in _arg_paths(args)]
    for ev in _jsonl(run_dir / "refusals.jsonl"):   # the hook's log: a rewritten command ran
        if ev.get("kind") == "rewrite" and isinstance(ev.get("rewritten"), str):
            out.append(("hook rewrite", "shell", ev["rewritten"]))
    return out


def _jsonl(path: Path):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                if line.lstrip().startswith("{"):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        yield ev
    except OSError:
        return


def scan(run_dir: Path, cwd: str, home: str | None = None) -> list[dict]:
    """The run's calls that touched paths outside `cwd` and `run_dir`: [{"tool", "path"}], each path
    once, in call order."""
    home = os.path.normpath(home or os.path.expanduser("~"))
    rd = str(run_dir)
    roots = [os.path.realpath(x) for x in (cwd, rd) if x] + [os.path.normpath(x) for x in (cwd, rd) if x]
    found: dict[str, str] = {}
    for tool, kind, what in calls(Path(run_dir)):
        if kind == "file":
            paths = [_expand(what, home, cwd, rd)]
        else:
            named = _shell_paths(what, home, cwd, rd)
            writes = bool(WRITE_RE.search(what) or REDIRECT_RE.search(what))
            paths = [p for p in named if writes or sensitive(p, home)]
        for p in paths:
            if not _inside(p, roots):
                found.setdefault(p, tool)
    return [{"tool": t, "path": p} for p, t in found.items()]


def _names(found: list[dict]) -> str:
    text = ", ".join(f"{f['path']} ({f['tool']})" for f in found[:MAX_PATHS])
    return text + (f", and {len(found) - MAX_PATHS} more" if len(found) > MAX_PATHS else "")


def record(run_dir: Path, run_id: int, cwd: str, home: str | None = None) -> dict | None:
    """Scan a run that ended without a valid hand-off and write its outside.json; the record, or None
    when it touched nothing outside its folders."""
    home = os.path.normpath(home or os.path.expanduser("~"))
    found = scan(run_dir, cwd, home)
    if not found:
        return None
    rec = {"run": run_id, "found": found, "high": any(sensitive(f["path"], home) for f in found)}
    from .project import durable_write
    durable_write(Path(run_dir) / FILE, json.dumps(rec, indent=1))
    return rec


def event_text(task: dict, rec: dict) -> str:
    return (f"#{task['id']} {task['title']}: run {rec['run']} ended without a hand-off and touched "
            f"{len(rec['found'])} path(s) outside its folders: {_names(rec['found'])}. Its next run is told; "
            "check the effect" + (" (home config or credential folders)" if rec["high"] else ""))


def prompt_block(run_dir: Path) -> str:
    """The retry's block for the failed run whose outside.json is in `run_dir`, or ''."""
    try:
        rec = json.loads((Path(run_dir) / FILE).read_text())
        found = [f for f in rec["found"] if isinstance(f, dict) and f.get("path")]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not found:
        return ""
    return (f"\nRun {rec.get('run')}, the earlier run of this task, ended without a hand-off and did these outside "
            f"your folders: {_names(found)}. Check their effect (paths only: never print a credential file's "
            "contents), report it in your hand-off, and do not repeat them.\n")

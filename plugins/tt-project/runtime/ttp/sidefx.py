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
Text that names no local path is left out first: a grep-like pattern, an awk or sed regex literal, a
'path' with regex anchors or escapes, a comparison such as `>=`, an ssh remote command and a heredoc's
body (data, unless a shell or interpreter runs it); so is
Claude Code's saved large tool output (~/.claude/projects/*/tool-results), which the model only reads.
The list (paths only, never a command's text or a file's contents) goes to the run dir's
outside.json, to the next run of the task as one prompt block (still named when a later run also ends
without a hand-off and touches nothing), and to the coordinator as an `outside_writes` event, high
on a credential store or a probable write there in a call no guard refused (record). Each path
carries its class (classify_paths: probable write, executed, read or mentioned only, with the reason)
and its call's status (items: started, completed with its exit code, or refused by which guard)."""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

from .hook import _quoting

FILE = "outside.json"
# The `guard` of a call no guard refused: the hooks block only protected and credential paths (pathguard,
# hook.py), so a write elsewhere, such as a tee into /tmp, runs, and this run-end scan is what records it.
SEEN_BY = "recorded by the run-end scan; no guard blocks writes outside protected paths"
KIND = "outside_writes"
MAX_PATHS = 15   # named in the prompt block and the event; the rest are counted
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path", "target", "destination", "dest", "source", "target_file")
MCP_WRITE_RE = re.compile(r"write|edit|create|move|rename|delete|remove|patch|upload|mkdir|copy", re.I)
PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
# A path in shell text: ~ or $HOME first, or absolute after a word break (not s/a/b/ or a URL's //host)
SHELL_PATH_RE = re.compile(r"""(?:^|(?<=[\s'"=<>(]))(?:(?:~|\$\{?HOME\}?|\$\{?TTP_RUN_DIR\}?)?/[^\s'"`;|&<>()]*"""
                           r"""|(?:~|\$\{?HOME\}?)(?=[\s'"`;|&<>()]|$))""")
REDIRECT_RE = re.compile(r"(?<![0-9&<>=-])(?:[0-9]?>>?|&>>?)(?![=&])\s*(?!&)([^\s;|&<>()'\"]+|'[^']+'|\"[^\"]+\")")
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&<>()]+)")
CD_RE = re.compile(r"(?:^|[;&|(]\s*|\n\s*)cd\s+([^\s;|&<>()]+)")
WRITE_RE = re.compile(r"\b(?:rm|mv|cp|ln|mkdir|rmdir|touch|chmod|chown|install|rsync|dd|truncate|tee|shred|unlink|"
                      r"ssh-keygen|scp|unzip|patch|wget|chgrp)\b|\bfind\b[^;&|\n]*\s-delete\b|\bcurl\b[^|;&\n]*\s-o\b|\bsed\b[^;&|\n]*\s(?:-[A-Za-z]*i|--in-place)|\bperl\b[^;&|\n]*\s-\w*i|\bgit\s+(?:-C\s+\S+\s+)?"
                      r"(?:config|clone|init|worktree|checkout|reset|commit|stash|apply)\b|open\([^)]*['\"][wax]\+?b?['\"]|"
                      r"\.write_(?:text|bytes)\(|\bshutil\.|\bos\.(?:remove|replace|rename|unlink|chmod|makedirs|mkdir)\(")
# Text in a shell command that names no local path (_local_text)
GREPS = {"grep", "egrep", "fgrep", "rg", "ag"}   # their pattern is blanked
PROGS = {"awk", "gawk", "mawk", "nawk", "sed"}   # their program's regex literals are blanked
SHELLS = {"sh", "bash", "zsh", "dash"}           # a -c script is read the same way
VALUE_OPTS = {"-A", "-B", "-C", "-m", "-f", "-g", "-t", "-d", "-D", "-F", "-v", "-l", "--file", "--glob", "--type",
              "--max-count"}   # take the next word
PROG_ONLY_OPTS = {"-F", "-v", "-l"}   # flags in grep, but take the next word in awk or sed
PATTERN_OPTS = {"-e", "--regexp", "--expression"}
SSH_VALUE_OPTS = set("bcDEeFIiJLlmOoPpRSWw")
REGEX_LITERAL_RE = re.compile(r"(?:^|(?<=[\s(!,~;{&|]))/(?:[^/\\\n]|\\.)+/")
REGEX_PATH_RE = re.compile(r"\^|\\[dDwWsSbB.(){}+*?|]|\$(?![\w{(])")   # anchors or escapes: pattern text, not a path
# Words before a command that leave the next word in command position, with their options that take a value
# (a short option's letter, or a long option written apart from its value)
PREFIXES = {"sudo": set("ugChprtUD") | {"--user", "--group", "--chdir", "--host", "--prompt", "--role", "--type",
                                        "--close-from", "--other-user", "--command-timeout"},
            "env": set("uCS") | {"--unset", "--chdir", "--split-string"},
            "xargs": set("IinPLldsEae") | {"--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars",
                                           "--process-slot-var"},
            "time": set("fo") | {"--format", "--output"}, "nice": set("n") | {"--adjustment"},
            "timeout": set("sk") | {"--signal", "--kill-after"}, "nohup": set(), "exec": set("a"), "command": set(),
            "builtin": set(), "!": set(), "if": set(), "then": set(), "else": set(), "elif": set(), "while": set(),
            "until": set(), "do": set(), "{": set()}
GIT_OPTS = set("Cc") | {"--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}   # before `git grep`
LOCAL_TEXT_MAX = 50000   # longer commands (a big heredoc) are read as they are
TOOL_OUTPUT_RE = re.compile(r"/\.claude/projects/[^/]+/tool-results(?:/|$)")
HARMLESS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/", "/dev/tty", "/proc/self/")
# A heredoc operator and its end word (not a here-string <<<, nor a shift such as $((1<<2)))
HEREDOC_RE = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.@%+-]*))")
ASSIGN_RE = re.compile(r"[A-Za-z_]\w*=.*", re.S)


def _expand(path: str, home: str, cwd: str, run_dir: str, base: str | None = None) -> str:
    p = path.strip("'\"")
    p = re.sub(r"^\$\{?TTP_RUN_DIR\}?", run_dir, p)
    p = re.sub(r"^\$\{?HOME\}?", home, p)
    if p == "~" or p.startswith("~/"):
        p = home + p[1:]
    if not p.startswith("/"):
        p = os.path.join(base or cwd, p)
    return os.path.normpath(p)


def _operators(cmd: str, start: int, stop: int | None = None):
    """The heredoc operators in cmd[start:stop] that are not quoted, escaped or in a comment (hook._quoting)."""
    for m in HEREDOC_RE.finditer(cmd, start, len(cmd) if stop is None else stop):
        first = cmd.rfind("\n", 0, m.start()) + 1
        last = cmd.find("\n", m.start())
        if not _quoting(cmd[first:len(cmd) if last < 0 else last])[m.start() - first][0]:
            yield m


def _heredocs(cmd: str) -> str:
    """`cmd` with each heredoc's body taken out: the text it feeds a command is data, not commands run or
    paths written. A body fed to a shell or an interpreter (`bash <<EOF`, `python3 - <<EOF`) runs, so it
    stays as that command's `-c` script. A heredoc whose end line is missing is kept as it is."""
    out, pos = [], 0
    while (m := next(_operators(cmd, pos), None)):
        nl = cmd.find("\n", m.end())
        if nl < 0:
            break
        line, at, parts, ok = cmd.rfind("\n", 0, m.start()) + 1, nl + 1, [], True
        for o in _operators(cmd, m.start(), nl):   # each body follows the line, in order
            word = o.group(2) or o.group(3) or o.group(4)
            end = re.compile("^" + ("\t*" if o.group(1) else "") + re.escape(word) + "$", re.M).search(cmd, at)
            if not end:
                ok = False
                break
            seg = re.split(r"[;&|(]", cmd[line:o.start()])[-1].split()
            name = next((w for w in seg if not ASSIGN_RE.fullmatch(w) and w not in PREFIXES), "").rsplit("/", 1)[-1]
            parts.append((o, f"-c {shlex.quote(cmd[at:end.start()])}" if name in SHELLS | INTERPS else ""))
            at = end.end() + 1
        if not ok:
            break
        out.append(cmd[pos:m.start()])
        k = m.start()
        for o, rep in parts:
            out.append(cmd[k:o.start()] + rep)
            k = o.end()
        out.append(cmd[k:nl + 1])
        pos = min(at, len(cmd))
    return "".join(out) + cmd[pos:]


def _tokens(cmd: str) -> list[str] | None:
    """`cmd`'s words and operators as the shell splits them (a heredoc's body out), or None when it cannot."""
    try:
        lex = shlex.shlex(_heredocs(cmd), posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
        return list(lex)
    except ValueError:
        return None


def _inside(path: str, roots: list[str]) -> bool:
    if path.startswith(HARMLESS) or path in HARMLESS:
        return True
    return any(path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


def _tool_output(path: str, home: str) -> bool:
    """`path` is Claude Code's saved large tool output, which the model only reads."""
    h = os.path.normpath(home)
    return path.startswith(f"{h}/.claude/projects/") and bool(TOOL_OUTPUT_RE.match(path, len(h)))


def home_entry(path: str, home: str) -> bool:
    """`path` is below the home in a hidden entry (config, credentials, shell startup files, ~/.local), in ~/bin or
    in ~/Library/LaunchAgents: what every later session reads or runs. The harness's own ~/.tt-project is judged
    by its own rules."""
    h = os.path.normpath(home)
    rel = path[len(h) + 1:] if path.startswith(h + "/") else ""
    top = rel.split("/", 1)[0]
    return (top.startswith(".") and top not in (".", "..", ".tt-project")) or top == "bin" \
        or (rel + "/").lower().startswith("library/launchagents/")


def sensitive(path: str, home: str) -> bool:
    """`path` is in a hidden or bin entry of the home (home_entry) or a credential store (credguard.read_refusal,
    ~/Library/Keychains among them), not saved tool output."""
    from . import credguard
    return not _tool_output(path, home) and (home_entry(path, home) or bool(credguard.read_refusal(path, home=home)))


def _local_text(cmd: str, depth: int = 0) -> str:
    """`cmd` with the words that name no local path blanked: a grep-like pattern, an awk or sed program's
    regex literals and an ssh remote command (the remote host's paths); a `sh -c` script is read the same
    way; a heredoc's body is taken out (_heredocs). Text the shell cannot split is kept as it is."""
    cmd = _heredocs(cmd)
    if len(cmd) > LOCAL_TEXT_MAX or (toks := _tokens(cmd)) is None:
        return cmd
    spans, at = [], 0   # each word's place in cmd (None when quoting changed its text)
    for t in toks:
        i = cmd.find(t, at) if t else -1
        spans.append((i, i + len(t)) if i >= 0 else None)
        at = i + len(t) if i >= 0 else at
    new: dict[int, str] = {}
    punct = [bool(t) and all(c in "();<>|&\n" for c in t) for t in toks]   # an empty argument ('') is a word
    commands = _commands(toks, punct)
    for i, t in enumerate(toks):
        name = t.rsplit("/", 1)[-1]
        if punct[i] or name not in GREPS | PROGS | SHELLS | {"ssh"} or i not in commands:
            continue
        j, words, explicit = i + 1, [], False
        while j < len(toks) and not punct[j]:
            w = toks[j]
            if name == "ssh":
                if w.startswith("-") and len(w) == 2 and w[1] in SSH_VALUE_OPTS:
                    j += 1
                elif not w.startswith("-"):   # the host: what follows runs there
                    new.update((k, "") for k in range(j + 1, len(toks)) if not any(punct[j + 1:k + 1]))
                    break
            elif name in SHELLS:
                if re.fullmatch(r"-\w*c", w) and j + 1 < len(toks) and not punct[j + 1] and depth < 3:
                    new[j + 1] = _local_text(toks[j + 1], depth + 1)
                    break
            elif w.split("=", 1)[0] in PATTERN_OPTS:
                explicit = True
                if "=" in w:
                    new[j] = w.split("=", 1)[0] + "=" + _blank(w.split("=", 1)[1], name)
                elif j + 1 < len(toks):
                    j += 1
                    new[j] = _blank(toks[j], name)
            elif w in ("-f", "--file"):   # the patterns or program come from a file
                explicit = True
                j += 1
            elif w in VALUE_OPTS and (name in PROGS or w not in PROG_ONLY_OPTS):
                j += 1
            elif not w.startswith("-") or w == "-":
                words.append(j)
            j += 1
        if words and not explicit and name in GREPS | PROGS:
            new[words[0]] = _blank(toks[words[0]], name)
    out = cmd
    for k in sorted(new, reverse=True):
        if spans[k] and new[k] != toks[k]:
            out = out[:spans[k][0]] + new[k] + out[spans[k][1]:]
    return out


def _commands(toks: list[str], punct: list[bool]) -> set[int]:
    """The indexes of the words in command position: the first word of each command (after ; | & ( <( or
    a newline), past VAR=val, sudo, env, xargs, time, `timeout <duration>` and the like and their options;
    and the `grep` of `git [options] grep`. A redirect's target and a command's arguments are not, so an
    argument ending in /rg or /sed is no grep or sed."""
    out, at_cmd, opts, skip, wait, git = set(), True, None, False, False, False
    for i, t in enumerate(toks):
        if punct[i]:
            redirect = any(c in "<>" for c in t) and not t.endswith("(")   # <( and >( start a command
            at_cmd, opts, skip, wait, git = (at_cmd, opts, True, wait, git) if redirect \
                else (True, None, False, False, False)
        elif skip:   # a redirect's target or an option's value
            skip = False
        elif not at_cmd:
            continue
        elif opts is not None and t.startswith("-") and len(t) > 1:
            skip = (len(t) == 2 and t[1] in opts) or t in opts   # an option that takes the next word
        elif git:
            if t == "grep":
                out.add(i)
            at_cmd, opts, git = False, None, False
        elif wait:   # timeout's duration
            wait = False
        elif re.fullmatch(r"[A-Za-z_]\w*=.*", t, re.S):
            continue
        elif t in PREFIXES:
            opts, wait = PREFIXES[t], t == "timeout"
        elif t == "git":
            opts, git = GIT_OPTS, True
        else:
            out.add(i)
            at_cmd, opts = False, None
    return out


def _blank(pattern: str, tool: str) -> str:
    """A grep-like pattern goes whole; an awk or sed program keeps all but its regex literals."""
    return "" if tool in GREPS else REGEX_LITERAL_RE.sub("", pattern)


def _shell_paths(cmd: str, home: str, cwd: str, run_dir: str) -> list[str]:
    """The paths a shell command names: its absolute and home paths but its `cd` targets, and its
    redirection and tee targets (a relative one after `cd <dir>` resolves under that dir)."""
    cds = [m.group(1) for m in CD_RE.finditer(cmd)]
    base = _expand(cds[-1], home, cwd, run_dir) if cds else None
    dirs = {_expand(d, home, cwd, run_dir) for d in cds}   # where it went, not what it touched
    out = [p for m in SHELL_PATH_RE.finditer(cmd) if not REGEX_PATH_RE.search(m.group(0))
           and (p := _expand(m.group(0), home, cwd, run_dir)) not in dirs]
    out += [_expand(m.group(1), home, cwd, run_dir, base) for r in (REDIRECT_RE, TEE_RE) for m in r.finditer(cmd)]
    return [p for p in out if len(p) > 1 and not _tool_output(p, home)]


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
    out = [c for ev in _jsonl(run_dir / "output.jsonl") for c in event_calls(ev)]
    for ev in _jsonl(run_dir / "refusals.jsonl"):   # the hook's log: a rewritten command ran
        if ev.get("kind") == "rewrite" and isinstance(ev.get("rewritten"), str):
            out.append(("hook rewrite", "shell", ev["rewritten"]))
    return out


def event_calls(ev: dict) -> list[tuple[str, str, str]]:
    """calls() of one transcript event: the commands and file writes the agent issued in it, never tool
    output or text."""
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
    return out


# What a blocked call's result starts with when a guard stopped it before it ran: a hook's refusal or a
# permission denial. Only these, at the start of the result, never a word in a command's own output.
DENY_RE = re.compile(r"\s*(?:PreToolUse:\w+ hook error:|\w+ operation blocked by hook|Permission to use \S+ has been "
                     r"denied|The user doesn't want to (?:proceed|take this action))")
EXIT_RE = re.compile(r"\s*Exit code (\d+)")


def _result_text(content) -> str:
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return str(content or "")


def _fork_prompt(ev: dict) -> bool:
    """Whether `ev` is the user line that hands a forked run its task (ctxfork.FORK_HEAD in its own text,
    never in a tool's output): the lines before it are the coordinator's inherited session."""
    from .ctxfork import FORK_HEAD
    msg = ev.get("message") if ev.get("type") == "user" and isinstance(ev.get("message"), dict) else None
    c = msg.get("content") if msg else None
    if isinstance(c, str):
        return FORK_HEAD in c
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" and FORK_HEAD in
                                       str(b.get("text") or "") for b in c)


def own_events(run_dir: Path) -> list[dict]:
    """The run's own transcript events: those after a fork's task prompt (_fork_prompt; the inherited
    coordinator lines and their tool calls before it are not this run's), without replayed ones."""
    evs = list(_jsonl(Path(run_dir) / "output.jsonl"))
    cut = next((i for i, ev in enumerate(evs) if _fork_prompt(ev)), -1)
    return [ev for ev in evs[cut + 1:] if not ev.get("isReplay")]


def items(run_dir: Path, own_only: bool = False) -> list[dict]:
    """calls() with each call's status: [{"tool", "kind", "what", "status", "exit", "guard"}], status
    `started` (no result seen), `completed` (exit: its code, or None when the provider gives none) or
    `refused` (guard: which guard stopped it before it ran: the deny text Claude Code puts at the start of
    the result, Codex's declined approval, Cursor's rejection, or the hook's own refusals.jsonl record).
    `own_only`: only the run's own calls (own_events), never a fork's inherited ones."""
    out: list[dict] = []
    by_id: dict[str, list[dict]] = {}
    line = 0

    def add(ev: dict, key: str, text: str = "") -> list[dict]:
        new = [{"tool": t, "kind": k, "what": w, "status": "started", "exit": None, "guard": "", "text": text,
                "line": line} for t, k, w in event_calls(ev)]
        out.extend(new)
        if key:
            by_id.setdefault(key, []).extend(new)
        return new

    for ev in own_events(run_dir) if own_only else _jsonl(Path(run_dir) / "output.jsonl"):
        line = ev.get("_n") or 0
        if isinstance(ev.get("_shell"), str):   # the fake provider's command
            add({"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": ev["_shell"]}}]}}, "")
        content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
        for b in content if isinstance(content, list) else []:   # Claude Code, the chat bridge
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name, inp = str(b.get("name") or ""), b.get("input") or {}
                text = str(inp.get("command") or "") if name == "Bash" else f"{name} {json.dumps(inp)[:400]}"
                add({"message": {"content": [b]}}, f"c:{b.get('id') or ''}", text[:500])
            elif b.get("type") == "tool_result":
                res = _result_text(b.get("content"))
                for it in by_id.get(f"c:{b.get('tool_use_id') or ''}", []):
                    if DENY_RE.match(res):
                        it.update(status="refused", guard=res.strip().split("\n", 1)[0][:80])
                    else:
                        m = EXIT_RE.match(res) if b.get("is_error") else None
                        it.update(status="completed", exit=int(m.group(1)) if m else None if b.get("is_error") else 0)
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and ev.get("type") in ("item.started", "item.completed"):   # Codex
            key = f"x:{item.get('id') or ''}"
            got = by_id.get(key) if item.get("id") else None
            got = got if got is not None else add({"type": ev["type"], "item": item}, key if item.get("id") else "")
            if ev["type"] == "item.completed":
                for it in got:
                    if item.get("status") == "declined":
                        it.update(status="refused", guard="Codex approval declined")
                    else:
                        code = item.get("exit_code")
                        it.update(status="completed", exit=code if isinstance(code, int) else None)
        tc = ev.get("tool_call") if ev.get("type") == "tool_call" and isinstance(ev.get("tool_call"), dict) else None
        if tc:   # Cursor: started, then completed with its result
            key = f"u:{ev.get('call_id') or ''}"
            got = by_id.get(key) if ev.get("call_id") else None
            got = got if got is not None else add(ev, key if ev.get("call_id") else "")
            body = next(iter(tc.values()), {})
            res = body.get("result") if isinstance(body, dict) and isinstance(body.get("result"), dict) else None
            if ev.get("subtype") == "completed" and res is not None:
                for it in got:
                    if "rejected" in res:
                        it.update(status="refused", guard="Cursor rejected the call")
                    else:
                        code = (res.get("success") or {}).get("exitCode") if isinstance(res.get("success"), dict) else None
                        it.update(status="completed", exit=code if isinstance(code, int) else 0 if "success" in res else None)
    for ev in _jsonl(Path(run_dir) / "refusals.jsonl"):   # the hook's log
        if ev.get("kind") == "rewrite" and isinstance(ev.get("rewritten"), str):   # the rewritten command ran
            out.append({"tool": "hook rewrite", "kind": "shell", "what": ev["rewritten"], "status": "started",
                        "exit": None, "guard": "", "text": "", "line": 0})
        elif isinstance(ev.get("command"), str):   # refused before it ran: the first unrefused call it names
            it = next((i for i in out if i["text"] == ev["command"] and i["status"] != "refused"
                       and not (i["status"] == "completed" and i["exit"] == 0)), None)
            if it:
                it.update(status="refused", guard=f"tt-project hook ({ev.get('kind') or 'refusal'})")
    for it in out:
        it.pop("text")
    return out


STRENGTH = {"write": 3, "executed": 2, "read": 1, "mentioned": 0}
LABEL = {"write": "probable write", "executed": "executed", "read": "read", "mentioned": "mentioned only"}
LISTERS = ("ls argument", "stat argument")   # classify_paths' reason for a path these list


def _rank(cls: str, why: str) -> tuple[int, bool]:
    """Which use of a path is kept: the stronger class, then any use over a listing (names only)."""
    return STRENGTH[cls], not why.startswith(LISTERS)


REMOVERS = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp", "truncate", "shred", "unlink"}
COPIERS = {"cp", "mv", "install", "rsync", "scp", "ln"}
READERS = {"cat", "head", "tail", "less", "more", "ls", "stat", "wc", "diff", "cmp", "file", "md5sum", "sha256sum",
           "find", "test", "[", "jq", "sort", "uniq", "cut", "readlink", "realpath", "du", "tree"} | GREPS | PROGS
INTERPS = {"python", "python3", "node", "perl", "ruby", "php"}
OUTPUT_OPTS = {"-o", "--output", "--output-file", "--output-document", "--out"}
NO_OUTPUT_OPTS = {"ps", "pgrep", "ss", "lsof", "ssh", "scp", "sftp", "journalctl", "grep", "ttp"}   # their -o: a format or an option
CODE_WRITE_RE = re.compile(r"""open\([^)]*['"][wax]|\.write(?:_text|_bytes)?\(|unlink|remove\(|rmtree|rename\(|"""
                           r"""replace\(|mkdir|chmod|writeFile|appendFile|copyfile|shutil\.(?:copy|move)""")


def _own_writes(name: str, code: str, home: str, cwd: str, run_dir: str, base: str | None) -> bool:
    """Inline Python whose every write the Python prover (pyproof) places under the run's own folders (the
    working folder, the run folder): its other paths are then only text it holds (a replacement string)."""
    from . import pyproof
    return name.startswith("python") and pyproof.harmless(code, cwd, {"HOME": home, "TTP_RUN_DIR": run_dir}, [base or cwd])


def commands(cmd: str, depth: int = 0) -> list[str]:
    """Each simple command `cmd` runs, as its words without redirects, an argument holding a space (quoted
    text: a commit message, an echo's) as `_` and an empty one as '' (a `bash -c` script's own commands in
    its place): what a check for a command's words reads, so a tool's name inside a quoted argument is no
    run of it, nor is a heredoc's body (_heredocs). [cmd] when the shell cannot split it."""
    if (toks := _tokens(cmd)) is None:
        return [cmd]
    out: list[str] = []

    def flush(words: list[str]) -> None:
        k = next((k for k, w in enumerate(words[:-2]) if w.rsplit("/", 1)[-1] in SHELLS
                  and re.fullmatch(r"-\w*c", words[k + 1])), None) if depth < 3 else None
        if k is not None:
            out.extend(commands(words[k + 2], depth + 1))
        elif words:
            out.append(" ".join("_" if re.search(r"\s", w) else w or "''" for w in words))

    words, skip = [], False
    for t in toks:
        if t and all(c in "();<>|&\n" for c in t):
            if any(c in "<>" for c in t) and not t.endswith("("):
                skip = True   # a redirect: its target is no word of the command
            else:
                flush(words)
                words = []
        elif skip:
            skip = False
        else:
            words.append(t)
    flush(words)
    return out


# an `s` or `y` command's pattern and replacement are text: `s/x/C8 wrote/` names no `w` command
SED_BODY_RE = re.compile(r"(?<![A-Za-z_])([sy])([^\w\s\\])(?:\\.|(?!\2).)*\2(?:\\.|(?!\2).)*\2")
# a command's address (`/warn/d`, `1,/x/!d`): its regex is text too, so it is cut down to a bare `/`
_SED_ADDR = r"(?:/(?:\\.|[^/\n])*/I?|[\d$]+)"
SED_ADDR_RE = re.compile(r"(^|[;{}\n])(\s*)" + _SED_ADDR + r"(?:\s*,\s*" + _SED_ADDR + ")?")
# a `w` command (after an address), or an `s` command's `w` flag, and its file: the rest of the line
SED_WRITE_RE = re.compile(r"(?:^|[;{}\n/\d$]|\x00[gpiImMe\d]*)\s*[wW]\s*([^\n]*)")


def _sed_writes(script: str) -> list[str]:
    """The files a sed script's `w` commands and `s///w` flags write."""
    script = SED_ADDR_RE.sub(r"\1\2/", SED_BODY_RE.sub("\x00", script))
    return [m.group(1).strip() for m in SED_WRITE_RE.finditer(script)]


def _sed_operands(args: list[str]) -> tuple[bool, list[str], list[str], list[str]]:
    """(in place, scripts, -f script files, file operands) of sed's arguments: its script is no file."""
    inplace, scripts, sfiles, files, k = False, [], [], [], 0
    while k < len(args):
        a = args[k]
        if a == "--":
            files += args[k + 1:]
            break
        if a.startswith("--"):
            opt, eq, val = a.partition("=")
            inplace = inplace or opt == "--in-place"
            if opt in ("--expression", "--file", "--line-length"):
                if not eq:
                    k += 1
                    val = args[k] if k < len(args) else ""
                if opt != "--line-length":
                    (scripts if opt == "--expression" else sfiles).append(val)
        elif a.startswith("-") and a != "-":
            for n, c in enumerate(a[1:], 2):
                if c == "i":
                    inplace = True
                    break   # the rest is its backup suffix
                if c in "efl":
                    val = a[n:]
                    if not val:
                        k += 1
                        val = args[k] if k < len(args) else ""
                    if c != "l":
                        (scripts if c == "e" else sfiles).append(val)
                    break
        elif not scripts and not sfiles:
            scripts.append(a)
        else:
            files.append(a)
        k += 1
    return inplace, scripts, sfiles, files


def classify_paths(cmd: str, home: str, cwd: str, run_dir: str, depth: int = 0) -> dict[str, tuple[str, str]]:
    """{path: (class, why)} for the paths a shell command names: `write` (a redirect, tee or output-flag
    target, a writing verb's target, a copy's destination, inline code that writes), `executed` (the
    command word, a script an interpreter runs), `read` (an input redirect, a reader's or copy's source)
    or `mentioned` (any other argument); the strongest per path. A `bash -c` script is read the same way,
    its reasons naming it; a heredoc's body is data (_heredocs). {} when the shell cannot split it."""
    if (toks := _tokens(cmd)) is None:
        return {}
    punct = [bool(t) and all(c in "();<>|&\n" for c in t) for t in toks]   # an empty argument ('') is a word
    out: dict[str, tuple[str, str]] = {}
    base: str | None = None

    def put(word: str, cls: str, why: str, target: bool = False) -> None:
        found = [m.group(0) for m in SHELL_PATH_RE.finditer(word) if not REGEX_PATH_RE.search(m.group(0))]
        if not found and target and word and not word.startswith("-"):
            found = [word]   # a relative target, under the last cd
        for w in found:
            p = _expand(w, home, cwd, run_dir, base)
            if p not in out or _rank(cls, why) > _rank(*out[p]):
                out[p] = (cls, why)

    starts = _commands(toks, punct)
    for i, t in enumerate(toks):   # in order, so a relative path resolves under the cd before it
        if punct[i] and i + 1 < len(toks) and not punct[i + 1] and not t.endswith("("):
            if t.endswith("&") and re.fullmatch(r"\d+-?|-", toks[i + 1]):
                pass   # `2>&1`, `>&2`, `2>&-`: a file descriptor copied or closed, no file
            elif ">" in t:
                put(toks[i + 1], "write", "redirect target", True)
            elif "<" in t:
                put(toks[i + 1], "read", "input redirect")
        if i not in starts:
            continue
        name = toks[i].rsplit("/", 1)[-1]
        args, j = [], i + 1
        while j < len(toks) and not punct[j]:
            args.append(toks[j])
            j += 1
        put(toks[i], "executed", "run as a command")
        if name == "cd":
            base = _expand(args[0], home, cwd, run_dir) if args else None
            continue
        if name in SHELLS:
            for k, a in enumerate(args[:-1]):
                if re.fullmatch(r"-\w*c", a) and depth < 3:
                    for p, (cls, why) in classify_paths(args[k + 1], home, cwd, run_dir, depth + 1).items():
                        if p not in out or _rank(cls, why) > _rank(*out[p]):
                            out[p] = (cls, f"{why} (inner command of {name} {a})")
                    break
            continue
        k, paths, tdir = 0, [], None
        while k < len(args):
            a = args[k]
            flag, _, val = a.partition("=")
            if flag in OUTPUT_OPTS and name not in NO_OUTPUT_OPTS:
                put(val if val else args[k + 1] if k + 1 < len(args) else "", "write", f"{name} {flag}", True)
                k += 1 if val else 2
                continue
            if name in COPIERS and flag in ("-t", "--target-directory"):
                tdir = val or (args[k + 1] if k + 1 < len(args) else "")
                k += 1 if val else 2
                continue
            if name in INTERPS and a in ("-c", "-e") and k + 1 < len(args):
                code = args[k + 1]
                writes = CODE_WRITE_RE.search(code) and not _own_writes(name, code, home, cwd, run_dir, base)
                cls, why = ("write", f"{name} {a} inline code that writes") if writes \
                    else ("mentioned", f"in {name} {a} inline code")
                put(code, cls, why)
                k += 2
                continue
            if name == "dd" and flag in ("of", "if"):
                put(val, "write" if flag == "of" else "read", f"dd {flag}=", True)
            elif not a.startswith("-"):
                paths.append(a)
            k += 1
        if name in INTERPS and paths:
            put(paths[0], "executed", f"script {name} runs")
            for a in paths[1:]:
                put(a, "mentioned", f"argument to {name}")
        elif name == "tee":
            for a in paths:
                put(a, "write", "tee target", True)
        elif name in REMOVERS:
            for a in paths:
                put(a, "write", f"{name} target", True)
        elif name in COPIERS:
            if tdir is not None:
                put(tdir, "write", f"{name} -t directory", True)
            elif paths:
                put(paths.pop(), "write", f"{name} destination", True)
            for a in paths:
                put(a, "write" if name == "mv" else "read", f"{name} source")
        elif name == "sed" and (sed := _sed_operands(args))[0]:
            _, scripts, sfiles, files = sed
            for a in files:
                put(a, "write", "sed -i target", True)
            for a in sfiles:
                put(a, "executed", "sed -f script")
            for f in (f for a in scripts for f in _sed_writes(a)):   # a `w file` command writes that file
                put(f, "write", "sed -i script with a w command", True)
        elif name in ("source", "."):
            for a in paths[:1]:
                put(a, "executed", f"{name} runs it")
        else:
            if name in PROGS - {"sed"}:   # an awk program's own `print > "/file"`
                for m in (m for a in paths for m in REDIRECT_RE.finditer(a)):
                    put(m.group(1), "write", f"{name} program's output file")
            for a in paths:
                put(a, "read" if name in READERS else "mentioned", f"{name} argument")
    return out


def _jsonl(path: Path):
    """The JSON objects of a jsonl file, each with its physical line number as `_n`."""
    try:
        with open(path, errors="replace") as f:
            for n, line in enumerate(f, 1):
                if line.lstrip().startswith("{"):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        ev["_n"] = n
                        yield ev
    except OSError:
        return


def _classes(it: dict, home: str, cwd: str, rd: str) -> dict[str, tuple[str, str]]:
    """{path: (class, why)} of one items() call, for the paths scan() lists."""
    if it["kind"] == "file":
        return {_expand(it["what"], home, cwd, rd): ("write", f"{it['tool']} edit")}
    what = _local_text(it["what"])
    named = _shell_paths(what, home, cwd, rd)
    if _tokens(what) is None:   # unsplit: any write-like text makes each path it names a probable write
        writes = bool(WRITE_RE.search(what) or REDIRECT_RE.search(what))
        return {p: ("write", "redirect or tee target") if writes else ("mentioned", "named")
                for p in named if writes or sensitive(p, home)}
    cls = classify_paths(what, home, cwd, rd)
    for c in commands(what):   # a writing verb classify_paths does not model (git clone, unzip) writes what it names
        c = " ".join(w for w in c.split() if not ASSIGN_RE.fullmatch(w))
        if WRITE_RE.search(c):
            for p in _shell_paths(c, home, cwd, rd):
                if cls.get(p, ("mentioned",))[0] == "mentioned":
                    cls[p] = ("write", "named by a writing command")
    if any(v[0] == "write" and "$" in p for p, v in cls.items()):   # a write through a variable may hit any path named
        for p in named:
            if cls.get(p, ("mentioned",))[0] == "mentioned":
                cls[p] = ("write", "redirect or tee target")
    writes = any(v[0] == "write" for v in cls.values())
    return {p: cls.get(p) or ("mentioned", "named") for p in named if writes or sensitive(p, home)}


def scan(run_dir: Path, cwd: str, home: str | None = None) -> list[dict]:
    """The run's calls that touched paths outside `cwd` and `run_dir`: [{"tool", "path", "class", "why",
    "status", "exit", "guard"}], each path once, in call order, from its strongest call that a guard did
    not refuse (class and why: classify_paths(); status, exit and guard: items(); a call no guard refused
    names SEEN_BY as its guard)."""
    home = os.path.normpath(home or os.path.expanduser("~"))
    rd = str(run_dir)
    roots = [os.path.realpath(x) for x in (cwd, rd) if x] + [os.path.normpath(x) for x in (cwd, rd) if x]
    found: dict[str, dict] = {}
    for it in items(Path(run_dir)):
        for p, (cls, why) in _classes(it, home, cwd, rd).items():
            if _inside(p, roots):
                continue
            f = {"tool": it["tool"], "path": p, "class": cls, "why": why,
                 **{k: it[k] for k in ("status", "exit", "guard")}}
            if f["status"] != "refused":
                f["guard"] = SEEN_BY   # no hook blocks a write outside the protected paths: say who saw it
            old = found.get(p)
            if old is None or (old["status"] != "refused", *_rank(old["class"], old["why"])) \
                    < (f["status"] != "refused", *_rank(cls, why)):
                found[p] = f
    return list(found.values())


def touches(run_dir: Path, path: str, home: str, cwd: str = "", own_only: bool = False) -> list[dict]:
    """Each items() call naming `path` (own_only: only the run's own calls), with its class and why for
    it: what credwatch reads to tell a run that wrote a credential file from one that only named, read
    or tried to."""
    rd = str(run_dir)
    out = []
    for it in items(Path(run_dir), own_only):
        if it["kind"] == "file":
            cls = {_expand(it["what"], home, cwd or rd, rd): ("write", f"{it['tool']} edit")}
        else:
            cls = classify_paths(_local_text(it["what"]), home, cwd or rd, rd)
        if path in cls:
            out.append({**it, "class": cls[path][0], "why": cls[path][1]})
    return out


def _state(f: dict) -> str:
    if f.get("status") == "refused":
        return f"stopped by {f.get('guard') or 'a guard'} before it ran"
    if f.get("status") == "completed":
        return "completed" + (f", exit {f['exit']}" if f.get("exit") is not None else "")
    return "started, never completed" if f.get("status") == "started" else ""


def _names(found: list[dict]) -> str:
    def one(f: dict) -> str:
        bits = [f["tool"]] + ([f"{LABEL.get(f['class'], f['class'])}: {f.get('why')}"] if f.get("class") else []) \
            + ([_state(f)] if _state(f) else [])
        return f"{f['path']} ({'; '.join(bits)})"
    text = ", ".join(one(f) for f in found[:MAX_PATHS])
    return text + (f", and {len(found) - MAX_PATHS} more" if len(found) > MAX_PATHS else "")


def record(run_dir: Path, run_id: int, cwd: str, home: str | None = None) -> dict | None:
    """Scan a run that ended without a valid hand-off and write its outside.json; the record, or None
    when it touched nothing outside its folders. High for a sensitive path in a call a guard did not
    refuse before it ran: whatever its class on a credential store (credguard.read_refusal), else on a
    probable write outside a cache (a top entry named like `.cache`). A read, run or `source` of a
    startup file or a ~/.local program is no harm, nor is an `ls` or `stat` of a folder that holds a credential."""
    from . import credguard
    home = os.path.normpath(home or os.path.expanduser("~"))
    found = scan(run_dir, cwd, home)
    if not found:
        return None

    def harm(f: dict) -> bool:
        cache = "cache" in f["path"][len(home) + 1:].split("/", 1)[0].lower()
        listing = f["why"].startswith(LISTERS)   # names only: a folder holding a credential file is no read of it
        return credguard.read_refusal(f["path"], search=not listing, home=home) or f["class"] == "write" and not cache
    rec = {"run": run_id, "found": found,
           "high": any(f["status"] != "refused" and sensitive(f["path"], home) and harm(f) for f in found)}
    from .project import durable_write
    durable_write(Path(run_dir) / FILE, json.dumps(rec, indent=1))
    return rec


def event_text(task: dict, rec: dict) -> str:
    return (f"#{task['id']} {task['title']}: run {rec['run']} ended without a hand-off and touched "
            f"{len(rec['found'])} path(s) outside its folders: {_names(rec['found'])}. Its next run is told; "
            "check the effect" + (" (home config or credential folders)" if rec["high"] else ""))


def last_block(run_dirs: list[Path]) -> str:
    """The retry's block for the task's ended runs `run_dirs`, newest first: the newest one's outside.json,
    or, while the newer runs also ended without a hand-off and touched nothing, an older one's; or ''."""
    from .hook import HANDOFF_STATES, RESULT_FILE
    for n, d in enumerate(run_dirs):
        if (Path(d) / FILE).exists():
            return prompt_block(Path(d), latest=n == 0)
        try:
            if json.loads((Path(d) / RESULT_FILE).read_text()).get("status") in HANDOFF_STATES:
                return ""   # it handed off: what came before it is reported
        except (OSError, ValueError, AttributeError):
            pass
    return ""


def prompt_block(run_dir: Path, latest: bool = True) -> str:
    """The retry's block for the failed run whose outside.json is in `run_dir`, or ''."""
    try:
        rec = json.loads((Path(run_dir) / FILE).read_text())
        found = [f for f in rec["found"] if isinstance(f, dict) and f.get("path")]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    if not found:
        return ""
    which = "the earlier run" if latest else "an earlier run"
    return (f"\nRun {rec.get('run')}, {which} of this task, ended without a hand-off and did these outside "
            f"your folders: {_names(found)}. Check their effect (paths only: never print a credential file's "
            "contents), report it in your hand-off, and do not repeat them.\n")


# Effects on other projects: what a run did to another project of this machine (its registry row, its daemon, its
# unit, its harness checkout), checked at the hand-off against a snapshot taken at the spawn. A hand-off claiming
# no side effects is checked against these and against the writes outside its folders (scan()).
PEERS = "peers-before.json"
NO_EFFECTS_RE = re.compile(
    r"\b(?:no (?:other |further |external )?side[- ]?effects?|nothing (?:else |outside [^.;\n]{1,80}?)?(?:was )?"
    r"(?:touched|changed|modified|affected)|touched nothing|no changes? (?:were made )?outside)", re.I)
OUTSIDE_RE = re.compile(r"outside (?:of )?(?:the |its |my )?[`'\"]?((?:~|/)[^\s`'\";,]+)")
UNIT_STOP_RE = re.compile(r"\bsystemctl\b[^;&|\n]*\b(stop|disable|kill|restart|mask|reset-failed)\b[^;&|\n]*?"
                          r"\btt-project-([\w.@-]+?)(?:\.service)?(?=[\s;&|'\"]|$)")
GIT_REWRITE_RE = re.compile(r"\bgit\b[^;&|\n]*\b(checkout|reset|clean|restore|switch|stash)\b")


def _peers(own_base) -> dict:
    """{name: {"dir", "daemon"}} of the other projects registered on this machine; `daemon`: its pid file
    names a live process."""
    from . import project
    own = os.path.realpath(str(own_base))
    out = {}
    for name, e in (project.load_registry().get("projects") or {}).items():
        if not (isinstance(e, dict) and e.get("dir") and e.get("host") in (None, project.hostname())):
            continue
        base = os.path.join(str(e["dir"]), project.FOLDER)
        if os.path.realpath(base) == own:
            continue
        alive, pid = False, None
        try:
            pid = int(Path(base, "state", "daemon.pid").read_text().strip())
            os.kill(pid, 0)
            alive = True
        except (OSError, ValueError):
            pass
        out[name] = {"dir": str(e["dir"]), "daemon": alive, **({"pid": pid} if alive else {}),
                     **({"made_by": e["made_by"]} if isinstance(e.get("made_by"), dict) else {})}
    return out


def _own_scratch(was: dict, own_name: str, task) -> bool:
    """A project this run's task made itself (`ttp new` records made_by) under the account's scratch folder."""
    from . import project
    by = was.get("made_by") or {}
    if task is None or by.get("project") != own_name or by.get("task") != task:
        return False
    root = os.path.realpath(str(project.HOME_DIR / "scratch")) + os.sep
    return os.path.realpath(str(was.get("dir") or "")).startswith(root)


READERS = frozenset({"cat", "ls", "grep", "rg", "head", "tail", "less", "wc", "stat", "find", "file", "echo",
                     "printf", "jq", "test", "[", "diff", "du", "tree", "realpath", "readlink", "basename", "dirname"})


def _made_it(calls: list[tuple[str, str]], name: str, was: dict) -> bool:
    """One of the run's own calls ([(kind, what)]) could have removed project `name`'s registry row or stopped its
    daemon: a command (no plain reader) naming it, its folder or its daemon's pid, or a write of the registry. A
    change another run made in the same window (it created and removed its own scratch project) is not this
    run's."""
    marks = {name, str(was.get("dir") or "").rstrip("/")} | ({str(was["pid"])} if was.get("pid") else set())
    marks.discard("")
    for kind, what in calls:
        if kind == "file":
            if os.path.basename(what.rstrip("/")) == "registry.json":
                return True
            continue
        words = what.split()
        if not words or os.path.basename(words[0]) in READERS:
            continue
        if any(w.rstrip("/") in marks or w.rstrip("/").startswith(tuple(m + "/" for m in marks if "/" in m))
               or os.path.basename(w) == "registry.json" for w in words[1:]):
            return True
    return False


def _run_task(run_dir) -> int | None:
    try:
        got = (json.loads((Path(run_dir) / "run.json").read_text()).get("env") or {}).get("TTP_TASK")
        return int(got) if str(got or "").isdigit() else None
    except (OSError, ValueError, AttributeError):
        return None


def snapshot_peers(run_dir: Path, own_base) -> None:
    """At a run's spawn: the other projects of this machine as they are now (peers-before.json)."""
    from .project import durable_write
    durable_write(Path(run_dir) / PEERS, json.dumps(_peers(own_base)))


def peer_effects(run_dir: Path, own_base, own_name: str) -> list[str]:
    """What the run did to another project of this machine: its registry row gone, its daemon stopped since the
    spawn (peers-before.json; only by one of the run's own commands, _made_it; not a scratch project the run's
    task made, _own_scratch), a call that stopped or disabled another project's unit, a git checkout, reset or
    clean in another project's folder (a call no guard refused). One line each."""
    out: list[str] = []
    try:
        before = json.loads((Path(run_dir) / PEERS).read_text())
    except (OSError, ValueError):
        before = {}
    now = _peers(own_base)
    task = _run_task(run_dir)
    own = [it for it in items(Path(run_dir), own_only=True) if it["status"] != "refused"]
    calls = [("file", it["what"]) if it["kind"] == "file" else ("shell", c)
             for it in own if it["kind"] in ("file", "shell")
             for c in (commands(it["what"]) if it["kind"] == "shell" else [it["what"]])]
    for name, was in sorted(before.items()):
        if name not in now and _own_scratch(was, own_name, task):
            continue   # its own scratch project, removed again
        gone = name not in now
        if not (gone or (was.get("daemon") and not now[name]["daemon"])) or not _made_it(calls, name, was):
            continue   # unchanged, or changed by another run's or a person's command in the same window
        out.append(f"project {name}'s registry row was removed" if gone else f"project {name}'s daemon was stopped")
    folders = {n: os.path.join(e["dir"], "tt-project") for n, e in {**before, **now}.items()}
    for it in own:
        if it["kind"] != "shell":
            continue
        for cmd in commands(it["what"]):   # commands run, not quoted text or a heredoc's body
            for m in UNIT_STOP_RE.finditer(cmd):
                if m.group(2) != own_name:
                    out.append(f"`systemctl {m.group(1)}` of another project's unit tt-project-{m.group(2)}")
            if (g := GIT_REWRITE_RE.search(cmd)):
                for n, f in sorted(folders.items()):
                    if re.search(re.escape(f) + r"(?:/|\b)", cmd):
                        out.append(f"`git {g.group(1)}` in project {n}'s folder {f}")
    return list(dict.fromkeys(out))


def claim_check(run_dir: Path, result: dict, cwd: str, own_base, own_name: str,
                home: str | None = None) -> tuple[list[str], list[str]]:
    """(the run's effects on other projects, its hand-off's false 'no side effects' claims). A claim in its
    summary or message ('no side effects', 'nothing outside X touched') is false when the run's own records
    show an effect on another project or a write outside its working folder, run dir and the X it names."""
    effects = peer_effects(run_dir, own_base, own_name)
    res = result if isinstance(result, dict) else {}
    text = "\n".join(str(res.get(k) or "") for k in ("summary", "message", "conclusion"))
    false = []
    for m in NO_EFFECTS_RE.finditer(text):
        claim = text[max(0, text.rfind("\n", 0, m.start()) + 1):][:240].split("\n", 1)[0]
        extra = [x.group(1) for x in OUTSIDE_RE.finditer(claim)]
        writes = [f["path"] for f in scan(Path(run_dir), cwd, home)
                  if f["class"] == "write" and f["status"] != "refused"
                  and not _inside(f["path"], [os.path.normpath(os.path.expanduser(x)) for x in extra])]
        why = effects + ([f"wrote outside its folders: {', '.join(writes[:5])}" + (f" and {len(writes) - 5} more" if len(writes) > 5 else "")]
                         if writes else [])
        if why:
            false.append((claim.strip(), "; ".join(why)))
        break   # one claim per hand-off is enough to mark it
    return effects, false

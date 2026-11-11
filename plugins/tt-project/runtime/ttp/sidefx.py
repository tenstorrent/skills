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
'path' with regex anchors or escapes, a comparison such as `>=`, and an ssh remote command; so is
Claude Code's saved large tool output (~/.claude/projects/*/tool-results), which the model only reads.
The list (paths only, never a command's text or a file's contents) goes to the run dir's
outside.json, to the next run of the task as one prompt block (still named when a later run also ends
without a hand-off and touches nothing), and to the coordinator as an
`outside_writes` event, high when a path is under those folders in a call no guard refused. Each path
carries its class (classify_paths: probable write, executed, read or mentioned only, with the reason)
and its call's status (items: started, completed with its exit code, or refused by which guard)."""
from __future__ import annotations

import json
import os
import re
import shlex
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
REDIRECT_RE = re.compile(r"(?<![0-9&<>=-])(?:[0-9]?>>?|&>>?)(?![=&])\s*(?!&)([^\s;|&<>()'\"]+|'[^']+'|\"[^\"]+\")")
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&<>()]+)")
CD_RE = re.compile(r"(?:^|[;&|(]\s*|\n\s*)cd\s+([^\s;|&<>()]+)")
WRITE_RE = re.compile(r"\b(?:rm|mv|cp|ln|mkdir|rmdir|touch|chmod|chown|install|rsync|dd|truncate|tee|shred|unlink|"
                      r"ssh-keygen|scp|unzip|patch|wget)\b|\bcurl\b[^|;&\n]*\s-o\b|\bsed\s+(?:-\w+\s+)*-i|\bperl\s+-\w*i|\bgit\s+(?:-C\s+\S+\s+)?"
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


def _tool_output(path: str, home: str) -> bool:
    """`path` is Claude Code's saved large tool output, which the model only reads."""
    h = os.path.normpath(home)
    return path.startswith(f"{h}/.claude/projects/") and bool(TOOL_OUTPUT_RE.match(path, len(h)))


def sensitive(path: str, home: str) -> bool:
    """`path` is under one of the home's config or credential folders (SENSITIVE), not saved tool output."""
    h = os.path.normpath(home)
    return not _tool_output(path, home) and any(path == f"{h}/{s}" or path.startswith(f"{h}/{s}/") for s in SENSITIVE)


def _local_text(cmd: str, depth: int = 0) -> str:
    """`cmd` with the words that name no local path blanked: a grep-like pattern, an awk or sed program's
    regex literals and an ssh remote command (the remote host's paths); a `sh -c` script is read the same
    way. Text the shell cannot split is kept as it is."""
    if len(cmd) > LOCAL_TEXT_MAX:
        return cmd
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
        toks = list(lex)
    except ValueError:
        return cmd
    spans, at = [], 0   # each word's place in cmd (None when quoting changed its text)
    for t in toks:
        i = cmd.find(t, at) if t else -1
        spans.append((i, i + len(t)) if i >= 0 else None)
        at = i + len(t) if i >= 0 else at
    new: dict[int, str] = {}
    punct = [all(c in "();<>|&\n" for c in t) for t in toks]
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

    def add(ev: dict, key: str, text: str = "") -> list[dict]:
        new = [{"tool": t, "kind": k, "what": w, "status": "started", "exit": None, "guard": "", "text": text}
               for t, k, w in event_calls(ev)]
        out.extend(new)
        if key:
            by_id.setdefault(key, []).extend(new)
        return new

    for ev in own_events(run_dir) if own_only else _jsonl(Path(run_dir) / "output.jsonl"):
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
                        "exit": None, "guard": "", "text": ""})
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
REMOVERS = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp", "truncate", "shred", "unlink"}
COPIERS = {"cp", "mv", "install", "rsync", "scp", "ln"}
READERS = {"cat", "head", "tail", "less", "more", "ls", "stat", "wc", "diff", "cmp", "file", "md5sum", "sha256sum",
           "find", "test", "[", "jq", "sort", "uniq", "cut", "readlink", "realpath", "du", "tree"} | GREPS | PROGS
INTERPS = {"python", "python3", "node", "perl", "ruby", "php"}
OUTPUT_OPTS = {"-o", "--output", "--output-file", "--output-document", "--out"}
CODE_WRITE_RE = re.compile(r"""open\([^)]*['"][wax]|\.write(?:_text|_bytes)?\(|unlink|remove\(|rmtree|rename\(|"""
                           r"""replace\(|mkdir|chmod|writeFile|appendFile|copyfile|shutil\.(?:copy|move)""")


def classify_paths(cmd: str, home: str, cwd: str, run_dir: str, depth: int = 0) -> dict[str, tuple[str, str]]:
    """{path: (class, why)} for the paths a shell command names: `write` (a redirect, tee or output-flag
    target, a writing verb's target, a copy's destination, inline code that writes), `executed` (the
    command word, a script an interpreter runs), `read` (an input redirect, a reader's or copy's source)
    or `mentioned` (any other argument); the strongest per path. A `bash -c` script is read the same way,
    its reasons naming it. {} when the shell cannot split it."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
        toks = list(lex)
    except ValueError:
        return {}
    punct = [all(c in "();<>|&\n" for c in t) for t in toks]
    out: dict[str, tuple[str, str]] = {}
    base: str | None = None

    def put(word: str, cls: str, why: str, target: bool = False) -> None:
        found = [m.group(0) for m in SHELL_PATH_RE.finditer(word) if not REGEX_PATH_RE.search(m.group(0))]
        if not found and target and word and not word.startswith("-"):
            found = [word]   # a relative target, under the last cd
        for w in found:
            p = _expand(w, home, cwd, run_dir, base)
            if p not in out or STRENGTH[cls] > STRENGTH[out[p][0]]:
                out[p] = (cls, why)

    starts = _commands(toks, punct)
    for i, t in enumerate(toks):   # in order, so a relative path resolves under the cd before it
        if punct[i] and i + 1 < len(toks) and not punct[i + 1] and not t.endswith("("):
            if ">" in t:
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
                        if p not in out or STRENGTH[cls] > STRENGTH[out[p][0]]:
                            out[p] = (cls, f"{why} (inner command of {name} {a})")
                    break
            continue
        k, paths, tdir = 0, [], None
        while k < len(args):
            a = args[k]
            flag, _, val = a.partition("=")
            if flag in OUTPUT_OPTS:
                put(val if val else args[k + 1] if k + 1 < len(args) else "", "write", f"{name} {flag}", True)
                k += 1 if val else 2
                continue
            if name in COPIERS and flag in ("-t", "--target-directory"):
                tdir = val or (args[k + 1] if k + 1 < len(args) else "")
                k += 1 if val else 2
                continue
            if name in INTERPS and a in ("-c", "-e") and k + 1 < len(args):
                code = args[k + 1]
                cls, why = ("write", f"{name} {a} inline code that writes") if CODE_WRITE_RE.search(code) \
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
        elif name == "sed" and any(re.fullmatch(r"-\w*i\S*|--in-place\S*", a) for a in args):
            for a in paths:
                put(a, "write", "sed -i target")
        elif name in ("source", "."):
            for a in paths[:1]:
                put(a, "executed", f"{name} runs it")
        else:
            for a in paths:
                put(a, "read" if name in READERS else "mentioned", f"{name} argument")
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


def _classes(it: dict, home: str, cwd: str, rd: str) -> dict[str, tuple[str, str]]:
    """{path: (class, why)} of one items() call, for the paths scan() lists."""
    if it["kind"] == "file":
        return {_expand(it["what"], home, cwd, rd): ("write", f"{it['tool']} edit")}
    what = _local_text(it["what"])
    named = _shell_paths(what, home, cwd, rd)
    writes = bool(WRITE_RE.search(what) or REDIRECT_RE.search(what))
    cls = classify_paths(what, home, cwd, rd)
    return {p: cls.get(p) or ("write", "redirect or tee target") if writes else cls.get(p) or ("mentioned", "named")
            for p in named if writes or sensitive(p, home)}


def scan(run_dir: Path, cwd: str, home: str | None = None) -> list[dict]:
    """The run's calls that touched paths outside `cwd` and `run_dir`: [{"tool", "path", "class", "why",
    "status", "exit", "guard"}], each path once, in call order, from its strongest call that a guard did
    not refuse (class and why: classify_paths(); status, exit and guard: items())."""
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
            old = found.get(p)
            if old is None or ((old["status"] == "refused", -STRENGTH[old["class"]])
                               > (f["status"] == "refused", -STRENGTH[cls])):
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
    when it touched nothing outside its folders. High for a sensitive path in any call a guard did not
    refuse before it ran, whatever its class: the class only words it."""
    home = os.path.normpath(home or os.path.expanduser("~"))
    found = scan(run_dir, cwd, home)
    if not found:
        return None
    rec = {"run": run_id, "found": found,
           "high": any(sensitive(f["path"], home) and f["status"] != "refused" for f in found)}
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

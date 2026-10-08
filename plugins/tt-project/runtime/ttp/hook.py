# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider hook endpoint: `python -m ttp.hook <event>`, with the hook payload on stdin.

Claude Code calls it after every tool use. If the coordinator has changed the task since the
worker started (it appends to `$TTP_RUN_DIR/steer.md`), the new part is handed to the worker as
added context, once. Workers on providers without hooks read the same file between steps.

Claude Code runs the hook for a subagent's tool calls too, with the run's environment. An update
handed over there would reach the subagent as text inside one of its command outputs, an order
about a task it does not own, and would be marked seen before the worker itself got it. So the
hook stays silent inside a subagent (its payload carries `agent_id`) and leaves the update for the
worker's next own tool call.

Before a Bash call it denies the obvious ways around the harness's PR draft guard (prguard.py):
gh called by a path or from a variable, a gh further down PATH (`which -a`, `hash -p`, or a PATH
that no longer starts with the run's own or `env -i` before a gh that writes: pr ready, pr create
without --draft, api with a write method or a GraphQL mutation, or a gh it cannot show is a read),
an HTTP client sending a GitHub API request that creates a PR or takes one out of draft (also from
a body the same command writes), and a script file run by the command that does any of these. It
also denies `ttp say` and the web app's /api/say: a run must not post as the user. It checks only
what runs: heredocs and echo/printf/cat text written to files that the command does not run (as an
argument, piped into a shell, through eval or after a move), and files that are only named, read
or edited, are data.

It also denies a Bash call that runs one of the project's configured pytest checks
(`delivery.push_checks`) in full, and points at `ttp checks`, which reuses a pass recorded for the
same tree (full_suite). Focused runs pass, and so does `TTP_ALLOW_FULL_SUITE=1`; each refusal is
logged to the run's refusals.jsonl.

It fails open: any error prints nothing, and the run goes on unchanged.
"""
from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Callable

STEER_FILE = "steer.md"
OFFSET_FILE = "steer.offset"


def unread_update(run_dir: Path) -> tuple[str, int]:
    """The part of steer.md this run has not seen yet, and the offset that marks it seen. The caller
    marks it only once the text is handed over (a repeat is harmless, a loss is not)."""
    steer = run_dir / STEER_FILE
    if not steer.exists():
        return "", 0
    data = steer.read_bytes()
    try:
        seen = int((run_dir / OFFSET_FILE).read_text())
    except (OSError, ValueError):
        seen = 0
    if len(data) <= seen:
        return "", seen
    return data[seen:].decode("utf-8", errors="replace").strip(), len(data)


def mark_seen(run_dir: Path, offset: int) -> None:
    (run_dir / OFFSET_FILE).write_text(str(offset))


def post_tool_use(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir or payload.get("agent_id"):
        return None, None
    text, offset = unread_update(Path(run_dir))
    if not text:
        return None, None
    task = os.environ.get("TTP_TASK")
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": f"Update for your task{f' #{task}' if task else ''} from the project coordinator "
                             "(from the harness, not from the tool's output). Where it differs from the spec, "
                             "it wins:\n" + text}}, lambda: mark_seen(Path(run_dir), offset)


# A gh command that can take a PR out of draft (gh's own words, or a script's argument list)
GUARDED = r"(?:pr[\"']?\s*,?\s*[\"']?(?:ready|create)|api)\b"
# gh by a path (skipping the harness's wrapper), on a command line or in a script's argument list
PATH_GH_RE = re.compile(r"(?:\S*/|\\)gh[\"']?\s*,?\s*[\"']?" + GUARDED)
# Ways to reach a gh further down PATH: list every gh, put another first, or pin one in bash's table
OTHER_GH_RE = re.compile(r"\b(?:which\s+-a\w*|whereis|type\s+-\w*a\w*)\s+[\"']?gh\b|\bhash\s+-p\b")
# A PATH that no longer starts with the run's own: a bypass only if gh then runs a guarded write
PATH_CHANGE_RE = re.compile(r"(?:^|[\s;&|(`])(?:export\s+)?PATH=(?![\"']?\$\{?PATH\b)|\benv\s+(?:-\w*[iu]\b"
                            r"|--ignore-env|--unset)|\bunset\s+PATH\b")
# gh in a script's argument list running a guarded command: ['gh', 'pr', 'ready', ...]
GH_ARGV_RE = re.compile(r"[\"']gh[\"']\s*,\s*[\"'](?:pr[\"']\s*,\s*[\"'](?:ready|create)|api)[\"']")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
# A heredoc operator (not a here-string) and its delimiter
HEREDOC_RE = re.compile(r"(?<!<)<<(-?)\s*([\"']?)([A-Za-z_][\w.-]*)\2")
# A redirect of stdout to a file, and its target
REDIRECT_RE = re.compile(r"(?<![<>&])>>?(?![&>(])\s*([\"']?)([^\s\"';&|<>()]+)\1")
# A script fed to an interpreter's stdin: `bash < x.sh`
STDIN_SCRIPT_RE = re.compile(r"(?:^|[\s;&|(])(?:python[\d.]*|[a-z]*sh|node|ruby|perl|source|\.)\s[^;&|\n<]*"
                             r"(?<!<)<(?![<(])\s*[\"']?([^\s\"';&|<>()]+)")
# Commands whose quoted arguments are only data (printed or read), never run
INERT = {"echo", "printf", "cat"}
INERT_RE = re.compile(r"\b(?:echo|printf|cat)\b")
GH_WORD_RE = re.compile(r"(?<![\w./-])gh(?![\w.-])")
GUARDED_RE = re.compile(r"(?<![\w./-])" + GUARDED)
# A variable in command position running a guarded gh command: `G=...; $G pr ready 7`
VAR_GH_RE = re.compile(r"(?:^|[\s;&|(`])[\"']?\$\{?\w+\}?[\"']?\s+(?:pr\s+(?:ready|create)|api\s+(?:graphql|-X|--method"
                       r"|\S*pulls))\b")
# Any other client calling the GitHub API to mark a PR ready or create one
HTTP_CLIENT_RE = re.compile(r"\b(?:curl|wget|httpie|http|xh|python3?|node|ruby|perl|fetch|requests|urllib\w*|httpx"
                            r"|aiohttp|axios|octokit|Octokit)\b(?!:)")
GITHUB_API_RE = re.compile(r"api\.github\.com|/api/v3\b|/graphql\b|\bGITHUB_(?:API|GRAPHQL)_URL\b", re.I)
# A token gh would use, sent by hand to a GitHub API path
TOKEN_API_RE = re.compile(r"\b(?:GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)\b")
API_PATH_RE = re.compile(r"/repos/|\bgraphql\b", re.I)
DRAFT_CHANGE_RE = re.compile(r"markPullRequestReadyForReview|createPullRequest|[\"']?draft[\"']?\s*[=:]\s*false",
                             re.I)
PULLS_RE = re.compile(r"/pulls\b")
# A request that writes (reading a PR is fine): curl/wget/httpie flags, or a client's post/patch/put
WRITE_RE = re.compile(r"(?:^|\s)(?:-X\s*|--request[\s=]+|--method[\s=]+)[\"']?(?:POST|PATCH|PUT)\b"
                      r"|(?:^|\s)(?:-d|--data(?:-\w+)?|--json|-F|--form|--post-data|--post-file)(?:[\s=]|$)"
                      r"|\b(?:POST|PATCH|PUT)\s+\S*(?:api\.github|/repos/)|method\s*[=:]\s*[\"'](?:POST|PATCH|PUT)"
                      r"|\.(?:post|patch|put|request)\s*\(", re.I)
INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|bash|sh|dash|zsh|ksh|node|nodejs|deno|bun|ruby|perl|tsx|ts-node)$")
WRAPPERS = {"env", "nohup", "setsid", "nice", "exec", "command", "time", "stdbuf", "ionice", "timeout", "xargs",
            "sudo"}
# Shell words that come before a command without being one: `{ gh ...; }`, `then gh ...`, `! gh ...`
RESERVED = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until"}
# A file's text run through a shell's -c or eval: `eval "$(cat a.sh)"`
CAT_SUBST_RE = re.compile(r"\$\(\s*(?:cat\s+|<\s*)[\"']?([^\s\"')]+)|`\s*cat\s+[\"']?([^\s\"'`]+)")
# Commands that copy or rename a file: what runs the new name runs the old one's text
COPIES = {"mv", "cp", "ln", "install"}
OPTS_WITH_VALUE = {"-u", "--unset", "-C", "--chdir", "-s", "--signal", "-k", "--kill-after", "-n", "--adjustment"}
SCRIPT_MAX_BYTES = 1 << 20


def _api_draft_change(text: str, full: str = "") -> bool:
    """An HTTP client sending a request to the GitHub API that creates a PR or takes one out of draft.
    The client and the API are looked for in `text` (what runs); the draft change may also sit in a
    body the same command writes (`full`) when the request sends data: `curl ... -d @q.json`."""
    api = GITHUB_API_RE.search(text) or (TOKEN_API_RE.search(text) and API_PATH_RE.search(text))
    write = WRITE_RE.search(text)
    change = DRAFT_CHANGE_RE.search(text) or (write and (PULLS_RE.search(text) or DRAFT_CHANGE_RE.search(full)))
    return bool(api and change and HTTP_CLIENT_RE.search(text))


def _quoting(line: str) -> list[tuple[bool, int]]:
    """Per character of `line`: whether it is quoted (or escaped, or in a comment), and the depth
    of parentheses around it."""
    out, q, depth, esc = [], "", 0, False
    for i, c in enumerate(line):
        inert = True
        if q == "#" and c == "\n":
            q = ""
        if esc:
            esc = False
        elif q == "#":
            pass
        elif q == "'":
            q = "" if c == "'" else q
        elif c == "\\":
            esc = True
        elif q == '"':
            q = "" if c == '"' else q
        elif c in "'\"":
            q = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t\n;&|("):
            q = "#"
        else:
            inert = False
            depth += c == "("
            depth = max(0, depth - (c == ")"))
        out.append((inert, depth))
    return out


def _pieces(line: str) -> list[tuple[int, int, str]]:
    """The top-level simple commands of one line: (start, end, the separator after it)."""
    flags, out, start, i = _quoting(line), [], 0, 0
    while i < len(line):
        c = line[i]
        if not flags[i][0] and flags[i][1] == 0 and c in ";&|" and line[i - 1:i] not in ("<", ">") \
                and line[i + 1:i + 2] != ">":
            sep = line[i:i + 2] if line[i + 1:i + 2] in (c, "&") and c != ";" else c
            out.append((start, i, sep))
            i = start = i + len(sep)
            continue
        i += 1
    out.append((start, len(line), ""))
    return out


def _piece_at(line: str, pos: int) -> tuple[str, str]:
    """The top-level simple command around `pos` in `line`, and the separator after it."""
    return next((line[s:e], sep) for s, e, sep in _pieces(line) if s <= pos <= e)


def _first_word(piece: str) -> str:
    segs = _segments(piece)
    return os.path.basename(segs[0][0]) if segs else ""


@functools.lru_cache(maxsize=4)
def _ran(commands: str) -> frozenset[str]:
    """Names of the script files `commands` runs: as an argument, on an interpreter's stdin
    (`bash < x`, `cat x | bash`), as the text of a `sh -c`/eval string (`eval "$(cat x)"`), or
    under another name it is copied or moved to first (`mv x y; bash y`)."""
    segments = _all_segments(commands)
    ran = [w for argv in segments for w in _run_files(argv)] + STDIN_SCRIPT_RE.findall(commands)
    for line in commands.split("\n"):
        pieces = _pieces(line)
        for (s, e, sep), (s2, e2, _) in zip(pieces, pieces[1:]):
            nxt = _segments(line[s2:e2])
            if sep == "|" and nxt and _reads_stdin_script(nxt[0]):
                ran += [w for argv in _segments(line[s:e]) for w in argv[1:] if not w.startswith("-")]
    for argv in segments:
        name = os.path.basename(argv[0])
        if name == "eval" or name in SHELLS:
            ran += [a or b for a, b in CAT_SUBST_RE.findall(" ".join(argv[1:]))]
    names = {os.path.basename(w) for w in ran}
    copies = [[os.path.basename(w) for w in argv[1:] if not w.startswith("-")] for argv in segments
              if os.path.basename(argv[0]) in COPIES]
    grew = True
    while grew:
        grew = False
        for *sources, target in (c for c in copies if len(c) > 1):
            if target in names and not names.issuperset(sources):
                names.update(sources)
                grew = True
    return frozenset(names)


def _data_only(piece: str, sep: str, commands: str) -> bool:
    """`piece` is an echo/printf/cat whose output is shown or written to a file that `commands`
    does not run afterwards, and that runs nothing inside."""
    if _first_word(piece) not in INERT or sep.startswith("|") \
            or re.search(r"\$\(|`|[<>]\(", re.sub(r"'[^']*'", "", piece)):
        return False
    return not any(os.path.basename(m[2]) in _ran(commands) for m in REDIRECT_RE.finditer(piece))


def _split(text: str) -> tuple[str, str]:
    """(what runs as shell commands, what to scan) for a command line or a script. Heredoc bodies
    are commands only when a shell reads them; bodies an echo/cat only writes to a file are
    dropped from both, and so are the quoted arguments of echo, printf and cat: data, not run."""
    lines, bodies, pending = [], [], []
    for line in text.replace("\\\n", " ").split("\n"):
        if pending:
            delim, dash, k = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            else:
                bodies[k][1].append(line)
            continue
        lines.append(line)
        flags = _quoting(line) if "<<" in line else []
        for m in HEREDOC_RE.finditer(line):
            if not flags[m.start()][0]:
                pending.append((m[3], m[1] == "-", len(bodies)))
                bodies.append((len(lines) - 1, [], m.start()))
    commands = "\n".join(lines)
    scan = list(lines)
    for i, line in enumerate(lines):
        stripped = line
        for s, e, sep in reversed(_pieces(line) if INERT_RE.search(line) else []):
            piece = line[s:e]
            if _data_only(piece, sep, commands):
                flags = _quoting(piece)
                kept = "".join(c if not flags[j][0] else "''" if j == 0 or not flags[j - 1][0] else ""
                               for j, c in enumerate(piece))
                stripped = stripped[:s] + kept + stripped[e:]
        scan[i] = stripped
    run = list(scan)
    for i, body, pos in bodies:
        piece, sep = _piece_at(lines[i], pos)
        if _first_word(piece) in SHELLS:
            run += body
        if not _data_only(piece, sep, commands):
            scan += body
    return "\n".join(run), "\n".join(scan)


def _gh_write(argv: list[str]) -> bool:
    """gh `argv` can take a PR out of draft: pr ready, pr create without --draft, or api with a
    write method or a GraphQL mutation (reads are fine)."""
    args, i = [], 1
    while i < len(argv):     # gh's -R/--repo works before the subcommand too: gh pr -R a/b ready 7
        if argv[i] in ("-R", "--repo"):
            i += 2
            continue
        if not argv[i].startswith("--repo="):
            args.append(argv[i])
        i += 1
    if args[:2] == ["pr", "ready"]:
        return True
    if args[:2] == ["pr", "create"]:
        return not any(a in ("-d", "--draft") or re.fullmatch(r"--draft=(?!false|0)\S*", a, re.I) for a in args)
    if args[:1] != ["api"]:
        return False
    method, fields, endpoint, rest = "", False, "", args[1:]
    for i, a in enumerate(rest):
        value = rest[i + 1] if i + 1 < len(rest) else ""
        if a in ("-X", "--method"):
            method = value
        elif re.match(r"-X\w|--method=", a):
            method = a.split("=", 1)[-1] if "=" in a else a[2:]
        elif re.match(r"-[fF]|--(?:raw-)?field\b|--input\b", a):
            fields = True
        elif not a.startswith("-") and not endpoint and rest[i - 1:i] not in (["-X"], ["--method"], ["-H"],
                                                                            ["--header"], ["-q"], ["--jq"]):
            endpoint = a
    if endpoint == "graphql":
        # A query from a variable or a command substitution may be a mutation: `-f query="$Q"`
        return any(re.search(r"\bmutation\b|=@|^--input|^query=.*\$", a) for a in rest)
    return (method or ("POST" if fields else "GET")).upper() not in ("GET", "HEAD")


def _all_segments(commands: str, depth: int = 0) -> list[list[str]]:
    """The simple commands in `commands`, and those in the strings it hands to `sh -c` or eval."""
    out = []
    for argv in _segments(commands):
        out.append(argv)
        name = os.path.basename(argv[0])
        inner = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
                     None) if name in SHELLS else " ".join(argv[1:]) if name == "eval" else None
        if inner and depth < 3:
            out += _all_segments(inner, depth + 1)
    return out


def _gh_writes(commands: str, text: str) -> bool:
    """gh may run a guarded write: a gh in command position in `commands` (or a `sh -c`/eval string
    there) writes, or `text` names gh with a guarded word and has more gh words than the parsed gh
    calls, all reads, account for (gh inside inline code, or where the parser does not see it)."""
    calls = [argv for argv in _all_segments(commands) if os.path.basename(argv[0]) == "gh"]
    if any(_gh_write(argv) for argv in calls):
        return True
    return bool(GUARDED_RE.search(text)) and len(GH_WORD_RE.findall(text)) > len(calls)


def draft_bypass(text: str, path_changed: bool = False) -> str | None:
    """Why `text` (a command line or a script it runs) gets around the gh draft guard, else None.
    `path_changed`: the command line that runs this script changes PATH (`PATH=/x bash s.sh`)."""
    full = text
    commands, text = _split(text)
    if PATH_GH_RE.search(text):
        return ("call gh by its name only: the harness's gh checks that a PR leaves draft only with the "
                "user's recorded approval")
    if (GUARDED_RE.search(text) and ((OTHER_GH_RE.search(text) and GH_WORD_RE.search(text)) or VAR_GH_RE.search(text))
            or (path_changed or PATH_CHANGE_RE.search(text)) and (GH_ARGV_RE.search(text) or _gh_writes(commands, text))):
        return ("call gh by its name, with PATH as the run set it: the harness's gh comes first and checks "
                "that a PR leaves draft only with the user's recorded approval")
    if _api_draft_change(text, full):
        return ("create or update PRs with gh, not a direct GitHub API call: a PR leaves draft only with "
                "the user's recorded approval")
    return None


def _segments(cmd: str) -> list[list[str]]:
    """The simple commands in `cmd`, each without its leading variable assignments and wrappers
    (env, nohup, timeout 60, `ttp lock res --` ...): what actually runs first in each."""
    cmd = "".join(";" if c == "\n" and not inert else c for c, (inert, _) in zip(cmd, _quoting(cmd)))
    try:
        import shlex
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = cmd.split()
    out, seg, target = [], [], False
    for w in words + [";"]:
        if target and w != ";":   # a redirect's file (or heredoc delimiter), not a command
            target = False
        elif w and set(w) <= set("&<>") and set(w) & set("<>"):
            target = True
        elif w and set(w) <= set(";&|()<>"):
            target = False
            if seg:
                out.append(_strip_prefix(seg))
            seg = []
        else:
            seg.append(w)
    return [s for s in out if s]


def _strip_prefix(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        w = argv[i]
        if w in OPTS_WITH_VALUE:   # env -u NAME, timeout -s KILL, nice -n 5 ...
            i += 2
        elif re.match(r"^\w+=", w) or w.startswith("-") or re.fullmatch(r"\d+[smhd]?", w) \
                or os.path.basename(w) in WRAPPERS or w in RESERVED:
            i += 1
        elif w == "ttp" and argv[i + 1:i + 2] in (["lock"], ["clip"], ["detach"]) and "--" in argv[i:]:
            i = argv.index("--", i) + 1
        else:
            break
    return argv[i:]


def _interpreter_args(argv: list[str]) -> str | None:
    """For an interpreter, the script file it runs, "" when it runs inline code (`-c`, `-m`) and
    "-" when it reads its script from stdin; None for any other command."""
    first, *rest = argv
    name = os.path.basename(first)
    if not INTERPRETER_RE.match(name):
        return None
    shell = re.match(r"[a-z]*sh$", name)
    inline = ("-c", "-m") if shell or name.startswith("python") else ("-e", "-E", "--eval", "-p", "--print")
    skip = False
    for a in rest:
        if skip:     # bash -o errexit, bash -O extglob: an option's value, not the script
            skip = False
        elif a in inline:
            return ""
        elif a == "-":
            return "-"
        elif shell and a in ("-o", "+o", "-O", "+O"):
            skip = True
        elif not a.startswith(("-", "+")):
            return a
    return "-"


def _reads_stdin_script(argv: list[str]) -> bool:
    return _interpreter_args(argv) == "-"


def _run_files(argv: list[str]) -> list[str]:
    """The script file a simple command runs: `python3 x.py`, `bash -e x.sh`, `source x`, `./x`.
    `-c` and `-m` run no file (inline code is on the command line already), nor does `-` (stdin)."""
    first, *rest = argv
    name = os.path.basename(first)
    script = _interpreter_args(argv)
    if script is not None:
        return [script] if script not in ("", "-") else []
    if name in ("source", "."):
        return rest[:1]
    return [first] if "/" in first else []


def _scripts(segments: list[list[str]], cwd: str) -> list[Path]:
    """Script files the commands run (see _run_files) that exist and are small enough to read."""
    found = []
    for argv in segments:
        for c in _run_files(argv):
            f = Path(os.path.expanduser(c))
            f = f if f.is_absolute() else Path(cwd) / f
            try:
                if f.is_file() and f.stat().st_size <= SCRIPT_MAX_BYTES:
                    found.append(f)
            except OSError:
                pass
    return found


def pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    if payload.get("tool_name") != "Bash" or not os.environ.get("TTP_RUN_DIR"):
        return None, None
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    commands, scan = _split(cmd)
    segments = _segments(commands)
    why = draft_bypass(cmd)
    if not why and (any(os.path.basename(s[0]) == "ttp" and s[1:2] == ["say"] for s in segments)
                    or (re.search(r"/api/say\b", cmd) and HTTP_CLIENT_RE.search(cmd))):
        # What a run posts as the user could count as the user's approval (pr_approve).
        why = ("a run must not post messages as the user; report with `ttp note` and the hand-off, "
                   "tell the user with `ttp notify \"<text>\"`, "
                   "and reach another project with `ttp note --to <project> \"<text>\"`")
    if not why:
        path_changed = bool(PATH_CHANGE_RE.search(scan))
        for f in _scripts(segments, str(payload.get("cwd") or os.getcwd())):
            try:
                why = draft_bypass(f.read_text(errors="replace"), path_changed)
            except OSError:
                continue
            if why:
                why = f"{f.name}: {why}"
                break
    if not why:
        why = full_suite(cmd, segments, str(payload.get("cwd") or os.getcwd()))
    if not why:
        return None, None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"tt-project: {why}."}}, None


# Full runs of the project's required checks go through `ttp checks`, which reuses a pass recorded
# for the same tree. Set to 1 for the run, or put `TTP_ALLOW_FULL_SUITE=1` before the command.
FULL_SUITE_OK = "TTP_ALLOW_FULL_SUITE"
FULL_SUITE_OK_RE = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?" + FULL_SUITE_OK + r"=[\"']?1\b")
# Each refusal, one JSON line in the run's directory, so they can be counted.
REFUSALS_FILE = "refusals.jsonl"
# pytest options that take the next word as their value (`--opt=value` carries its own)
PYTEST_VALUE_OPTS = {"-k", "-m", "-p", "-c", "-o", "-r", "-n", "-W", "--tb", "--deselect", "--ignore", "--ignore-glob",
                     "--rootdir", "--confcutdir", "--basetemp", "--junitxml", "--junit-xml", "--maxfail",
                     "--durations", "--durations-min", "--timeout", "--log-level", "--log-file", "--log-cli-level",
                     "--cov", "--cov-report", "--cov-config", "--pythonwarnings", "--import-mode", "--capture",
                     "--override-ini", "--dist", "--color", "--code-highlight", "--junit-prefix", "--count",
                     "--reruns", "--randomly-seed", "--html", "--config-file", "--inifile"}
# pytest options that select part of what the targets name, or run none of it
PYTEST_SELECT_OPTS = {"-k", "-m", "--lf", "--last-failed", "--sw", "--stepwise", "--stepwise-skip", "--deselect",
                      "--sw-skip", "--ignore", "--ignore-glob", "--co", "--collect-only", "--collectonly", "--fixtures",
                      "--fixtures-per-test", "--markers", "-h", "--help", "--version", "-V", "--setup-plan",
                      "--setup-only", "--trace-config", "--count", "--lfnf", "--last-failed-no-failures"}
PYTHON_VALUE_OPTS = {"-W", "-X", "-Q"}


def _pytest_args(argv: list[str]) -> list[str] | None:
    """The arguments of a pytest run (`pytest`, `py.test`, `python -m pytest`), else None."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    if name in ("pytest", "py.test"):
        return argv[1:]
    if not re.fullmatch(r"python[\d.]*", name):
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-m":
            return argv[i + 2:] if argv[i + 1:i + 2] == ["pytest"] else None
        if a in PYTHON_VALUE_OPTS:
            i += 2
        elif a.startswith("-") and a != "-":
            i += 1
        else:
            return None   # a script or stdin: not pytest itself
    return None


def _pytest_parts(args: list[str], rel: str) -> tuple[set[str], set[tuple[str, str]]]:
    """(the paths a pytest run names, relative to the repository's top, "." when it names none;
    its selecting options as (option, value)). An option not known to take a value is a flag: its
    value then counts as one more path, which can only make a run look narrower, never wider."""
    paths: set[str] = set()
    select: set[tuple[str, str]] = set()
    i = 0
    while i < len(args):
        a = args[i]
        opt, eq, val = a.partition("=")
        if a.startswith("-") and not a.startswith("--") and len(a) > 2:
            # bundled short options: `-xk expr`, `-kexpr`, `-qxkfoo`; a value option takes the rest
            for j, c in enumerate(a[1:], 1):
                o = "-" + c
                if not c.isalnum():
                    select.add(("?", a))   # unclear: count as narrower, so it passes
                    break
                if o in PYTEST_VALUE_OPTS:
                    val = a[j + 1:]
                    if not val:
                        val = args[i + 1] if i + 1 < len(args) else ""
                        i += 1
                    if o in PYTEST_SELECT_OPTS:
                        select.add((o, val))
                    break
                if o in PYTEST_SELECT_OPTS:
                    select.add((o, ""))
        elif a.startswith("-") and a != "-":
            if not eq and opt in PYTEST_VALUE_OPTS:
                val = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if opt in PYTEST_SELECT_OPTS:
                select.add((opt, val))
        elif "::" in a:
            select.add(("::", a))   # a node id: one test or class
        else:
            paths.add(a)
        i += 1
    return {posixpath.normpath(posixpath.join(rel, p)) for p in (paths or {"."})}, select


def _covers(run: set[str], check: set[str]) -> bool:
    """Whether a run naming `run` also runs everything a check naming `check` runs: each of the
    check's paths is one of the run's, or under one of its directories."""
    return all(any(c == r or r == "." or c.startswith(r.rstrip("/") + "/") for r in run) for c in check)


def _repo_rel(cwd: str) -> tuple[str, str]:
    """(the git top of `cwd`, `cwd` relative to it); ("", ".") outside a repository."""
    d = Path(cwd)
    for top in (d, *d.parents):
        if (top / ".git").exists():
            return str(top), os.path.relpath(d, top)
    return "", "."


def _relativize(args: list[str], top: str) -> list[str]:
    """Absolute paths under the repository's top as repo-relative ones."""
    return [os.path.relpath(a, top) if top and os.path.isabs(a) and (a + "/").startswith(top.rstrip("/") + "/")
            else a for a in args]


CD_COMMANDS = ("cd", "pushd")


def _cd_target(argv: list[str]) -> str | None:
    """The directory a `cd`/`pushd` goes to as written, or None when it is not a literal path
    (a variable, `-`, `~`, a substitution, no argument)."""
    args = [a for a in argv[1:] if a not in ("-L", "-P", "-e", "-@", "--")]
    if len(args) != 1 or not args[0] or args[0] == "-" or args[0].startswith(("~", "+", "-")) \
            or re.search(r"[$`*?\[]", args[0]):
        return None
    return args[0]


def _pytest_dirs(segments: list[list[str]], cwd: str | None, subshell: bool = False):
    """(each pytest run's arguments, the directory it runs in) in order, following each `cd`/`pushd`
    before it from `cwd`. The directory is None once it is unclear (a non-literal target, `popd`, a
    relative target from an unknown start): such a run is not judged. With `subshell` (the command
    has a `(`), a `cd` may sit inside `( ... )` or `$( ... )`, so every run after one is unclear."""
    for seg in segments:
        if os.path.basename(seg[0]) in CD_COMMANDS:
            t = _cd_target(seg)
            cwd = None if t is None or subshell or (cwd is None and not os.path.isabs(t)) \
                else os.path.normpath(os.path.join(cwd or "/", t))
        elif os.path.basename(seg[0]) == "popd":
            cwd = None
        else:
            args = _pytest_args(seg)
            if args is not None:
                yield args, cwd


def _check_runs() -> list[tuple[str, set[str], set[tuple[str, str]]]]:
    """(the check, its paths, its selecting options) for each pytest run in the project's configured
    checks (`delivery.push_checks`, what `ttp checks` runs). Read fresh: a project may change them."""
    base = os.environ.get("TTP_PROJECT")
    if not base:
        return []
    from .project import Project
    from .push import check_list
    p = Project(base)
    if not p.exists():
        return []
    out = []
    top = "/top"   # checks run from the repository's top; a cd out of it makes the run unclear
    for c in check_list((p.config().get("delivery") or {}).get("push_checks")):
        for args, d in _pytest_dirs(_segments(str(c)), top, "(" in str(c)):
            if d is None or not (d + "/").startswith(top + "/"):
                continue
            paths, select = _pytest_parts(args, os.path.relpath(d, top))
            out.append((str(c), paths, select))
    return out


def full_suite(cmd: str, segments: list[list[str]], cwd: str) -> str | None:
    """Why `cmd` is refused when it runs one of the project's configured pytest checks in full,
    else None. A run counts as full when it names every path the check names (or a directory above
    them) and selects nothing the check does not (`-k`, `-m`, `--lf`, a node id ...). Anything
    unclear passes: refusing a focused run costs more than missing a full one. Each refusal is
    logged to the run's refusals.jsonl."""
    if os.environ.get(FULL_SUITE_OK) == "1" or FULL_SUITE_OK_RE.search(cmd):
        return None
    runs = list(_pytest_dirs(segments, cwd, "(" in cmd))
    if not runs:
        return None
    checks = _check_runs()
    if not checks:
        return None
    home = _repo_rel(cwd)[0]
    for args, d in runs:
        if d is None:
            continue
        top, rel = _repo_rel(d)
        if top != home:   # another repository, or none: not this project's checks
            continue
        paths, select = _pytest_parts(_relativize(args, top), rel)
        for check, cpaths, cselect in checks:
            if select <= cselect and _covers(paths, cpaths):
                _log_refusal(cmd, check)
                return (f"this runs the project's full check `{check}`. While you work, run only the tests "
                        "you changed (a file, `-k <expr>`, `file::test`); run the full checks once, committed, "
                        "with `ttp checks` (it reuses a pass already recorded for the same tree; "
                        "`ttp checks --detach` when they take long). If you truly need it here, put "
                        f"`{FULL_SUITE_OK}=1` before the command")
    return None


def _log_refusal(cmd: str, check: str) -> None:
    import time
    try:
        with open(Path(os.environ["TTP_RUN_DIR"]) / REFUSALS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "full_suite", "check": check,
                                "command": cmd[:500]}) + "\n")
    except OSError:
        pass


HANDLERS = {"PostToolUse": post_tool_use, "PreToolUse": pre_tool_use}


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    handler = HANDLERS.get(event or payload.get("hook_event_name") or "")
    if not handler:
        return 0
    try:
        out, delivered = handler(payload)
        if out:
            json.dump(out, sys.stdout)
            sys.stdout.flush()
        if delivered:
            delivered()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

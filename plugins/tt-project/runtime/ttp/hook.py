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
without --draft, api with a write method or a GraphQL mutation), an HTTP client sending a GitHub
API request that creates a PR or takes one out of draft, and a script file run by the command that
does any of these. It also denies `ttp say` and the web app's /api/say: a run must not post as the
user. It checks only what runs: heredocs and echo/printf/cat text written to files that the
command does not run, and files that are only named, read or edited, are data.

It fails open: any error prints nothing, and the run goes on unchanged.
"""
from __future__ import annotations

import functools
import json
import os
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
WRAPPERS = {"env", "nohup", "setsid", "nice", "exec", "command", "time", "stdbuf", "ionice", "timeout", "xargs"}
OPTS_WITH_VALUE = {"-u", "--unset", "-C", "--chdir", "-s", "--signal", "-k", "--kill-after", "-n", "--adjustment"}
SCRIPT_MAX_BYTES = 1 << 20


def _api_draft_change(text: str) -> bool:
    """An HTTP client sending a request to the GitHub API that creates a PR or takes one out of draft."""
    api = GITHUB_API_RE.search(text) or (TOKEN_API_RE.search(text) and API_PATH_RE.search(text))
    change = DRAFT_CHANGE_RE.search(text) or (PULLS_RE.search(text) and WRITE_RE.search(text))
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
    """Names of the script files `commands` runs, as an argument or on an interpreter's stdin."""
    return frozenset(os.path.basename(w) for w in [w for argv in _all_segments(commands) for w in _run_files(argv)]
                     + STDIN_SCRIPT_RE.findall(commands))


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
    dropped, and so are the quoted arguments of echo, printf and cat: data, not run."""
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
    run, scan = list(lines), list(lines)
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
    args = argv[1:]
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
        return any(re.search(r"\bmutation\b|=@|^--input", a) for a in rest)
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


def _gh_writes(commands: str) -> bool:
    """A gh in command position in `commands` (or a `sh -c`/eval string there) runs a guarded write."""
    return any(os.path.basename(argv[0]) == "gh" and _gh_write(argv) for argv in _all_segments(commands))


def draft_bypass(text: str) -> str | None:
    """Why `text` (a command line or a script it runs) gets around the gh draft guard, else None."""
    commands, text = _split(text)
    if PATH_GH_RE.search(text):
        return ("call gh by its name only: the harness's gh checks that a PR leaves draft only with the "
                "user's recorded approval")
    if (GUARDED_RE.search(text) and ((OTHER_GH_RE.search(text) and GH_WORD_RE.search(text)) or VAR_GH_RE.search(text))
            or PATH_CHANGE_RE.search(text) and (GH_ARGV_RE.search(text) or _gh_writes(commands))):
        return ("call gh by its name, with PATH as the run set it: the harness's gh comes first and checks "
                "that a PR leaves draft only with the user's recorded approval")
    if _api_draft_change(text):
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
                or os.path.basename(w) in WRAPPERS:
            i += 1
        elif w == "ttp" and argv[i + 1:i + 2] == ["lock"] and "--" in argv[i:]:
            i = argv.index("--", i) + 1
        else:
            break
    return argv[i:]


def _run_files(argv: list[str]) -> list[str]:
    """The script file a simple command runs: `python3 x.py`, `bash -e x.sh`, `source x`, `./x`.
    `-c` and `-m` run no file (inline code is on the command line already), nor does `-` (stdin)."""
    first, *rest = argv
    name = os.path.basename(first)
    if INTERPRETER_RE.match(name):
        inline = ("-c", "-m") if re.match(r"python|[a-z]*sh$", name) else ("-e", "-E", "--eval", "-p", "--print")
        for a in rest:
            if a in inline or a == "-":
                break
            if not a.startswith("-"):
                return [a]
        return []
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
    segments = _segments(_split(cmd)[0])
    why = draft_bypass(cmd)
    if not why and (any(os.path.basename(s[0]) == "ttp" and s[1:2] == ["say"] for s in segments)
                    or (re.search(r"/api/say\b", cmd) and HTTP_CLIENT_RE.search(cmd))):
        # What a run posts as the user could count as the user's approval (pr_approve).
        why = "a run must not post messages as the user; report with `ttp note` and the hand-off"
    if not why:
        for f in _scripts(segments, str(payload.get("cwd") or os.getcwd())):
            try:
                why = draft_bypass(f.read_text(errors="replace"))
            except OSError:
                continue
            if why:
                why = f"{f.name}: {why}"
                break
    if not why:
        return None, None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"tt-project: {why}."}}, None


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

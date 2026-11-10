# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider hook endpoint: `python -m ttp.hook <event>`, with the hook payload on stdin.

Claude Code calls it after every tool use. Codex (where its hooks are on) sends the same payload
fields and takes the same replies; providers/codex.py passes it per run. If the coordinator has changed the task since the
worker started (it appends to `$TTP_RUN_DIR/steer.md`), the new part is handed to the worker as
added context, once. Workers on providers without hooks get it printed by `ttp note` and `ttp steer`
(take_update); either way steer.offset marks it read and steer.reads records when. The hook's first
tool event in a run writes HOOKED_FILE there: from then on `ttp note` and `ttp steer` leave updates
to the hook (Codex may run without it, so this is known per run, not per provider). A Bash call's
PreToolUse comes before the command, so the marker exists before a worker's first `ttp note` runs.

Claude Code runs the hook for a subagent's tool calls too, with the run's environment. An update
handed over there would reach the subagent as text inside one of its command outputs, an order
about a task it does not own, and would be marked seen before the worker itself got it. So the
hook stays silent inside a subagent (its payload carries `agent_id`) and leaves the update for the
worker's next own tool call.

After a failed tool call (Claude Code's PostToolUseFailure, which replaces PostToolUse there) it hands
over the same update and hand-off problems, and, when the error is the Bash sandbox's mount race
(MOUNT_RACE: bwrap could not bind a path that vanished while it started), the hint to re-run the
command unchanged; `ttp audit` counts the runs that hit it. In a sandboxed run, an ssh that failed with
`kex_exchange_identification` gets the hint that a sudo ProxyCommand cannot run there (KEX_NO_NEW_PRIVS_HINT).

The hand-off guard: after a tool call that changed `$TTP_RUN_DIR/result.json` (any tool: it compares
the file's size and mtime with the last one checked), the hook parses it as the daemon does and, if
it is no hand-off (not JSON, not an object, or no hand-off status), hands the worker the error with
its line and column, once per version of the file. A valid done or needs_review hand-off of a code task
with no branch on record (NO_BRANCH_ENV) that names no PR, `published` change or top-level `branch` +
`head` gets a warning naming those fields, since the daemon could queue no review of it. A Stop hook (Claude Code and Codex, task runs only: the
daemon sets HANDOFF_GUARD_ENV) refuses to end a run whose result.json is missing or no hand-off, with
a one-line reason; it lets the run end when `stop_hook_active` is set or after MAX_STOP_BLOCKS
refusals, so it never loops, and stays silent in a subagent and outside a task run. On a valid hand-off
it runs the project's hand-off checks (handoffcheck.at_stop) and refuses the stop with a failed check's
output, so the same session fixes it, within the task's send-back cap.

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

A PATH set in front of a gh call is fine when it visibly keeps the run's harness bin dir first: its
literal path, or `$TTP_PROJECT/harness/bin` (not in single quotes) where the text names TTP_PROJECT
only as a plain expansion and sets no variable by a built name (`eval`, `declare`, `export "$n=..."`,
os.environ). A test can so put a fake gh behind the wrapper. Known gap: a script that sets TTP_PROJECT
some other way it cannot see.

Outside a harness task it denies Edit/Write calls (and Codex apply_patch calls naming them) on the
harness's bin/gh and the project's gh.d pre-hooks (prguard.run_gh_hooks), and Bash calls naming their path (`$TTP_PROJECT/harness/bin/gh`,
`$TTP_PROJECT/harness/bin/gh.d`, or gh.d together with the bin dir), as the web token check does:
any mention.

In a run it denies `git push` with git called by a path, which skips the run's bin/git: its push
branch guard (gitguard.py) and its leak scan (outscan.py).

It denies kill, pkill and killall aimed at processes outside the task's own process tree, also through
wrappers, `sh -c` and ssh (killguard.py); that check fails closed on a command it cannot parse.

It also denies a Bash call that runs one of the project's configured pytest checks
(`delivery.push_checks`) in full, and points at `ttp checks`, which reuses a pass recorded for the
same tree (full_suite). Focused runs pass, and so does `TTP_ALLOW_FULL_SUITE=1`; each refusal is
logged to the run's refusals.jsonl.

A Bash call that would print more than WIDE_READ_LINES lines of one file (sed -n with a big range,
cat of a large file) gets a reminder to grep first and read narrow ranges, as added context; it is
never denied (wide_read). `ttp stats --tools` shows how much of the runs' tool output is file reads.

A run that holds no device (TTP_NO_DEVICE=1) on a machine tagged `device` is refused device work: a
reset, flash or power tool, a device node path, or a change to the device mask (TT_VISIBLE_DEVICES,
`env -i`), also through wrappers and `sh -c`; a device `ttp lock` (refused itself on a held or paused
device) or `ttp devq` passes (device_command). Nothing runs in a namespace sandbox: tests run as they
are, with the device mask set. Each refusal is logged to refusals.jsonl.

Otherwise it fails open: any error prints nothing, and the run goes on unchanged.
"""
from __future__ import annotations

import functools
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

STEER_FILE = "steer.md"
OFFSET_FILE = "steer.offset"
READS_FILE = "steer.reads"
HOOKED_FILE = "steer.hooked"   # this run's hook hands updates over (see the module docstring)
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")


def hooked(run_dir: Path) -> bool:
    """Whether this run's hook has run, so it, not `ttp note` or `ttp steer`, hands updates over."""
    try:
        return (run_dir / HOOKED_FILE).exists()
    except OSError:
        return False


def mark_hooked(run_dir: Path) -> None:
    """Record that this run's hook runs (once; a lost marker only lets `ttp note` print an update)."""
    if not hooked(run_dir):
        (run_dir / HOOKED_FILE).touch()


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
    """Mark steer.md read up to `offset`, and record when (READS_FILE, `<epoch> <offset>` lines): the
    daemon's live check (steerack) raises an update still unread after steer.ack_after_s."""
    (run_dir / OFFSET_FILE).write_text(str(offset))
    with open(run_dir / READS_FILE, "a") as f:
        f.write(f"{time.time():.0f} {offset}\n")


def update_context(text: str) -> str:
    """How an unread update is handed to the worker, by the hook or by `ttp note` / `ttp steer`."""
    task = os.environ.get("TTP_TASK")
    return (f"Update for your task{f' #{task}' if task else ''} from the project coordinator "
            "(from the harness, not from the tool's output). Where it differs from the spec, "
            "it wins:\n" + text)


def take_update(run_dir: Path) -> str:
    """The unread update as handed to the worker, marked read; "" when there is none. For runs
    whose hook does not run: `ttp note` and `ttp steer` print it (cli.print_update)."""
    text, offset = unread_update(run_dir)
    if not text:
        return ""
    mark_seen(run_dir, offset)
    return update_context(text)


RESULT_FILE = "result.json"
CHECKED_FILE = "result.checked"   # the size and mtime of the result.json version last checked
STOP_BLOCKS_FILE = "stop.blocks"  # how many times the Stop hook refused to end this run
# Set to 1 by the daemon for a task's worker or reviewer run, which must end with a hand-off.
HANDOFF_GUARD_ENV = "TTP_HANDOFF_GUARD"
MAX_STOP_BLOCKS = 2
# Set to 1 by the daemon for a code task's worker run with no branch on record (its project root is not
# a git repository): its hand-off must name the change for the daemon to queue its review.
NO_BRANCH_ENV = "TTP_NO_BRANCH_ON_RECORD"
# The statuses the daemon takes as a hand-off (daemon.HANDOFF_STATES; a test keeps them equal).
HANDOFF_STATES = ("done", "blocked", "failed", "needs_review", "waiting")


def handoff_problem(path: Path) -> str | None:
    """Why `path` is no hand-off the daemon would take (it reads it with json.loads, as
    daemon._read_result does), None when it is one."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return f"there is no {RESULT_FILE} in the run dir"
    except (OSError, UnicodeDecodeError) as e:
        return f"{RESULT_FILE} cannot be read ({e})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return f"{RESULT_FILE} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}"
    if not isinstance(data, dict):
        return f"{RESULT_FILE} must hold one JSON object, not {type(data).__name__}"
    if data.get("status") not in HANDOFF_STATES:
        return (f"{RESULT_FILE} status {data.get('status')!r} is not a hand-off state "
                f"({', '.join(HANDOFF_STATES)})")
    if "then" in data:
        from . import thenwaits
        if why := thenwaits.problem(data):
            return f"{RESULT_FILE}: {why}"
    if data.get("status") == "done" and isinstance(data.get("followups"), list):
        from . import chaincap
        if wait := next((f for f in data["followups"] if isinstance(f, dict) and chaincap.waits_only(f)), None):
            return (f"its `continue:` follow-up {str(wait.get('title'))[:120]!r} only waits (checks, a detached job "
                    f"or a landing), which costs a new run: {chaincap.WAITS_ONLY_HINT} instead")
    return None


def handoff_warning(path: Path) -> str | None:
    """Why the daemon could queue no review for this hand-off, None when it can (or need not): a code
    task with no branch on record (NO_BRANCH_ENV) handing off done or needs_review with no PR, no
    valid `published` entry and no valid top-level `branch` + `head`. A branch or sha named only in
    the summary does not count: the daemon never reads prose."""
    if os.environ.get(NO_BRANCH_ENV) != "1":
        return None
    from . import extchange
    from .prguard import PR_URL_RE
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") not in ("done", "needs_review"):
        return None
    if isinstance(data.get("pr"), str) and PR_URL_RE.search(data["pr"]):
        return None
    pub, pub_why = extchange.parse(data.get(extchange.FIELD))
    top, top_why = extchange.top_level(data) if pub is None and not pub_why else (None, "")
    if pub or top:
        return None
    why = pub_why and f" (`{extchange.FIELD}` ignored: {pub_why})" or top_why and f" (ignored: {top_why})" or ""
    return (f"this task has no branch on record, so the daemon can queue no review of your change{why}. If it "
            f"changed code, name it in {RESULT_FILE}: `\"{extchange.FIELD}\": {{\"repo\": ..., \"branch\": ..., "
            f"\"head\": ...}}`, or top-level `\"branch\"` + `\"head\"` (a 7-40 digit sha; optional `\"repo\"`). "
            f"A branch or sha named only in the summary is not read")


def _result_change(run_dir: Path) -> tuple[str | None, Callable[[], None] | None]:
    """The problem with result.json when it changed since the last check, and a callback that marks
    this version checked; (None, None) when it is absent or unchanged."""
    try:
        st = (run_dir / RESULT_FILE).stat()
    except OSError:
        return None, None
    stamp = f"{st.st_mtime_ns} {st.st_size}"
    try:
        if (run_dir / CHECKED_FILE).read_text() == stamp:
            return None, None
    except OSError:
        pass
    return handoff_problem(run_dir / RESULT_FILE), lambda: (run_dir / CHECKED_FILE).write_text(stamp)


def _quiet(check: Callable[[Path], str | None], path: Path) -> str | None:
    try:
        return check(path)
    except Exception:   # a warning fails open
        return None


def mark_untrusted(payload: dict, run_dir: str) -> None:
    """Record that a task run (a subagent's call too) read web, connector or downloaded text (untrusted.py)."""
    try:
        from . import coordtools, untrusted
        tool = str(payload.get("tool_name") or "")
        if not os.environ.get("TTP_TASK") or os.environ.get(coordtools.ROLE_ENV) == "coordinator":
            return
        cmd = str((payload.get("tool_input") or {}).get("command") or "") if tool == "Bash" else ""
        if src := untrusted.read_source(tool, _all_segments(_split(cmd)[0]) if cmd else None):
            untrusted.mark(run_dir, src)
    except Exception:   # fails open, as the hook does
        pass


def post_tool_use(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if run_dir:
        mark_untrusted(payload, run_dir)
    if not run_dir or payload.get("agent_id"):
        return None, None
    parts, marks = [], []
    text, offset = unread_update(Path(run_dir))
    if text:
        parts.append(update_context(text))
        marks.append(lambda: mark_seen(Path(run_dir), offset))
    try:
        problem, checked = _result_change(Path(run_dir))
    except Exception:   # the guard fails open; the update above still goes out
        problem, checked = None, None
    if checked:
        marks.append(checked)
    if problem:
        parts.append(f"tt-project: your hand-off is not valid yet: {problem}. The daemon would take this run as "
                     f"ending without a hand-off; fix $TTP_RUN_DIR/{RESULT_FILE} before you stop.")
    elif checked and (warning := _quiet(handoff_warning, Path(run_dir) / RESULT_FILE)):
        parts.append(f"tt-project: {warning}.")
    if not marks:
        return None, None
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(parts)}} \
        if parts else None
    return out, lambda: [mark() for mark in marks]


def stop(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    """Refuse to end a task run with no valid hand-off, at most MAX_STOP_BLOCKS times; or one whose
    hand-off fails one of the project's hand-off checks, within its send-back cap (handoffcheck.at_stop)."""
    run_dir = os.environ.get("TTP_RUN_DIR")
    if (not run_dir or os.environ.get(HANDOFF_GUARD_ENV) != "1" or not os.environ.get("TTP_TASK")
            or payload.get("agent_id")):
        return None, None
    rd = Path(run_dir)
    problem = handoff_problem(rd / RESULT_FILE)
    if not problem:
        from . import handoffcheck
        try:
            why = handoffcheck.at_stop(rd)   # its own count bounds it, also when stop_hook_active is set
        except Exception:   # a broken check or hook never holds the run: the daemon's check is the gate
            why = None
        return ({"decision": "block", "reason": why} if why else None), None
    if payload.get("stop_hook_active"):
        return None, None
    try:
        blocks = int((rd / STOP_BLOCKS_FILE).read_text())
    except (OSError, ValueError):
        blocks = 0
    if blocks >= MAX_STOP_BLOCKS:
        return None, None
    return ({"decision": "block",
             "reason": f"tt-project: {problem}. Write your hand-off to $TTP_RUN_DIR/{RESULT_FILE} "
                       f"(the Handoff section of your instructions), then stop."},
            lambda: (rd / STOP_BLOCKS_FILE).write_text(str(blocks + 1)))


# A gh command that can take a PR out of draft (gh's own words, or a script's argument list)
GUARDED = r"(?:pr[\"']?\s*,?\s*[\"']?(?:ready|create)|api)\b"
# gh by a path (skipping the harness's wrapper), on a command line or in a script's argument list
PATH_GH_RE = re.compile(r"(?:\S*/|\\)gh[\"']?\s*,?\s*[\"']?" + GUARDED)
# Ways to reach a gh further down PATH: list every gh, put another first, or pin one in bash's table
OTHER_GH_RE = re.compile(r"\b(?:which\s+-a\w*|whereis|type\s+-\w*a\w*)\s+[\"']?gh\b|\bhash\s+-p\b")
# A PATH that no longer starts with the run's own: a bypass only if gh then runs a guarded write
PATH_CHANGE_RE = re.compile(r"(?:^|[\s;&|(`])(?:export\s+)?PATH=(?![\"']?\$\{?PATH\b)|\benv\s+(?:-\w*[iu]\b"
                            r"|--ignore-env|--unset)|\bunset\s+PATH\b|\bcommand\s+(?:-\w+\s+)*-\w*p")
# The run's harness bin dir at the head of a new PATH, as `$TTP_PROJECT/harness/bin` (or `${TTP_PROJECT}`)
HARNESS_BIN_VARS = ("$TTP_PROJECT/harness/bin", "${TTP_PROJECT}/harness/bin")
# TTP_PROJECT as a plain expansion; named any other way (set, unset, a default) its value may not be the run's
PROJECT_EXPANSION_RE = re.compile(r"\$TTP_PROJECT(?!\w)|\$\{TTP_PROJECT\}")
# Ways to set a variable whose name is built at run time (TTP_PROJECT spelled in pieces), or a child's env
DYNAMIC_SET_RE = re.compile(r"\b(?:eval|declare|typeset|read|readarray|mapfile|environ|putenv|setenv)\b|\benv\s*="
                            r"|\bprintf\s+-v\b|\b(?:export|local)\s+(?:-\w+\s+)*[\"']?\$")
# The harness's gh wrapper and its gh.d pre-hooks, named in a command (also as gh.d after a cd to the bin dir)
HARNESS_GH_RE = re.compile(r"(?<![\w.-])harness/+bin/+gh(?:\.d)?(?![\w.-])")
HARNESS_BIN_RE = re.compile(r"(?<![\w.-])harness/+bin(?![\w.-])")
GH_D_RE = re.compile(r"(?<![\w.-])gh\.d(?![\w.-])")
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
# xargs options whose value is the rest of the word or else the next one (`-a FILE`, `-I {}`), and its long
# ones that take the next word when they carry no `=` (getopt takes an unambiguous prefix too)
XARGS_VALUE_LETTERS = "adEILnPs"
XARGS_LONG_WITH_VALUE = ("--arg-file", "--delimiter", "--max-args", "--max-procs", "--max-chars", "--process-slot-var")
SCRIPT_MAX_BYTES = 1 << 20


def _api_draft_change(text: str, full: str = "") -> bool:
    """An HTTP client sending a request to the GitHub API that creates a PR or takes one out of draft.
    The client and the API are looked for in `text` (what runs); the draft change may also sit in a
    body the same command writes (`full`) when the request sends data: `curl ... -d @q.json`."""
    api = GITHUB_API_RE.search(text) or (TOKEN_API_RE.search(text) and API_PATH_RE.search(text))
    write = WRITE_RE.search(text)
    change = DRAFT_CHANGE_RE.search(text) or (write and (PULLS_RE.search(text) or DRAFT_CHANGE_RE.search(full)))
    return bool(api and change and HTTP_CLIENT_RE.search(text))


def api_ref_write(text: str) -> bool:
    """An HTTP client sending a write to a GitHub API route that moves refs, tags, releases or file
    contents (credguard), or a GraphQL ref mutation."""
    from .credguard import REF_MUTATION_RE, REF_URL_RE
    api = GITHUB_API_RE.search(text) or (TOKEN_API_RE.search(text) and API_PATH_RE.search(text))
    write = WRITE_RE.search(text) or re.search(r"(?:-X\s*|--request[\s=]+|--method[\s=]+|method\s*[=:]\s*)[\"']?"
                                               r"DELETE\b|\.delete\s*\(", text, re.I)
    change = (write and REF_URL_RE.search(text)) or REF_MUTATION_RE.search(text)
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


def _project_trusted(text: str) -> bool:
    """`text` names TTP_PROJECT only as a plain expansion and sets no variable by a built name, so
    `$TTP_PROJECT` there is the run's own."""
    return text.count("TTP_PROJECT") == len(PROJECT_EXPANSION_RE.findall(text)) and not DYNAMIC_SET_RE.search(text)


def _keeps_harness_first(text: str, pos: int, trusted: bool) -> bool:
    """The `PATH=` value starting at `pos` in `text` visibly starts with the run's harness bin dir:
    its literal path, or `$TTP_PROJECT/harness/bin` outside single quotes when `trusted`."""
    quote = text[pos:pos + 1] if text[pos:pos + 1] in ("'", '"') else ""
    rest = text[pos + len(quote):]
    base = os.environ.get("TTP_PROJECT", "")
    heads = [posixpath.join(base, "harness", "bin")] if posixpath.isabs(base) else []
    if trusted and quote != "'":
        heads += HARNESS_BIN_VARS
    for head in heads:
        if rest.startswith(head):
            tail = rest[len(head):].removeprefix("/")
            if quote and tail[:1] == quote:
                tail = tail[1:]                 # past the closing quote: only ':' or the word's end
            elif quote:
                return tail[:1] == ":"
            if tail[:1] == ":" or not tail or tail[0].isspace() or tail[0] in ";&|)`":
                return True
    return False


def path_change(text: str, trusted: bool | None = None) -> bool:
    """`text` changes PATH so that the run's harness bin dir may no longer come first: a `PATH=` whose
    value does not visibly start with it (_keeps_harness_first; appending to $PATH is fine), `env -i`,
    `env -u` or `unset PATH`. `trusted`: `$TTP_PROJECT` is the run's (default: judged from `text`)."""
    trusted = _project_trusted(text) if trusted is None else trusted
    return any(not (m.group().endswith("PATH=") and _keeps_harness_first(text, m.end(), trusted))
               for m in PATH_CHANGE_RE.finditer(text))


def draft_bypass(text: str, path_changed: bool = False, trusted: bool = True) -> str | None:
    """Why `text` (a command line or a script it runs) gets around the gh draft guard, else None.
    `path_changed`: the command line that runs this script changes PATH (`PATH=/x bash s.sh`).
    `trusted`: that command line leaves TTP_PROJECT alone, so the script's `$TTP_PROJECT` is the run's."""
    full = text
    commands, text = _split(text)
    if PATH_GH_RE.search(text):
        return ("call gh by its name only: the harness's gh checks that a PR leaves draft only with the "
                "user's recorded approval")
    if (GUARDED_RE.search(text) and ((OTHER_GH_RE.search(text) and GH_WORD_RE.search(text)) or VAR_GH_RE.search(text))
            or (path_changed or path_change(text, trusted and _project_trusted(full)))
            and (GH_ARGV_RE.search(text) or _gh_writes(commands, text))):
        return ("call gh by its name, with PATH as the run set it: the harness's gh comes first and checks "
                "that a PR leaves draft only with the user's recorded approval")
    if _api_draft_change(text, full):
        return ("create or update PRs with gh, not a direct GitHub API call: a PR leaves draft only with "
                "the user's recorded approval")
    if api_ref_write(text):
        from .credguard import REF_WHY
        return REF_WHY.removeprefix("refused: ")
    return None


def git_push_bypass(segments: list[list[str]]) -> str | None:
    """Why a Bash call that pushes with git called by a path (around the run's bin/git, gitguard.py)
    is refused in a run: it would skip the push branch guard and the leak scan; else None."""
    from . import gitguard
    pushes = [s for s in segments if os.path.basename(s[0]) == "git" and "/" in s[0]
              and gitguard.split(s[1:])[1] in ("push", *gitguard.HOOKLESS_PUSH)]
    if not pushes:
        return None
    try:
        if forced := next((w for s in pushes if (w := gitguard.never_force_refusal(s[1:], dict(os.environ), s[0]))), ""):
            return forced
        branch, why = gitguard.guarded_branch(dict(os.environ))
    except Exception:
        branch, why = "", ""
    if branch:
        return f"{why}. git called by its path skips the run's push guard: push with plain `git` from PATH"
    return ("git called by its path skips the run's leak scan of what a push sends (outscan.py): push with "
            "plain `git` from PATH" if os.environ.get("TTP_TASK") else None)


def _segments(cmd: str, strip: bool = True) -> list[list[str]]:
    """The simple commands in `cmd`, each without its leading variable assignments and wrappers
    (env, nohup, timeout 60, `ttp lock res --` ...): what actually runs first in each. `strip`
    False keeps them."""
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
                out.append(_strip_prefix(seg) if strip else seg)
            seg = []
        else:
            seg.append(w)
    return [s for s in out if s]


def _strip_prefix(argv: list[str], ttp_wrappers: list[list[str]] | None = None) -> list[str]:
    """`argv` without its wrappers; each `ttp lock|clip|detach|sandbox|secret env ... --` stripped is added to
    `ttp_wrappers` when given."""
    i, xargs = 0, False
    while i < len(argv):
        w = argv[i]
        if xargs and w.startswith("-") and w != "-":   # xargs -a FILE, --arg-file FILE, -0I {} ...
            i += 2 if _xargs_takes_next(w) else 1
        elif w in OPTS_WITH_VALUE:   # env -u NAME, timeout -s KILL, nice -n 5 ...
            i += 2
        elif re.match(r"^\w+\+?=", w) or w.startswith("-") or re.fullmatch(r"\d+[smhd]?", w) \
                or os.path.basename(w) in WRAPPERS or w in RESERVED:
            xargs = os.path.basename(w) == "xargs"
            i += 1
        elif w == "ttp" and (argv[i + 1:i + 2] in (["lock"], ["clip"], ["detach"], ["sandbox"])
                             or argv[i + 1:i + 3] == ["secret", "env"]) and "--" in argv[i:]:
            end = argv.index("--", i)
            if ttp_wrappers is not None:
                ttp_wrappers.append(argv[i + 1:end])
            i = end + 1
        else:
            break
    return argv[i:]


def _xargs_takes_next(opt: str) -> bool:
    """Whether xargs option word `opt` takes the next word as its value."""
    if opt.startswith("--"):
        return len(opt) > 2 and "=" not in opt and any(o.startswith(opt) for o in XARGS_LONG_WITH_VALUE)
    for j, c in enumerate(opt[1:], 1):
        if c in XARGS_VALUE_LETTERS:
            return j == len(opt) - 1
        if c in "eil":   # -e[EOF], -i[R], -l[N]: an optional value, only in the same word
            return False
    return False


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


def _log_bypass(cmd: str) -> None:
    """Record a denied draft-guard bypass in the gh guard log as the run's refused 'ready' call, so
    pr-watch counts it as the run's doing, never as the user's own approval. The run dir is
    <state>/runs/<id>."""
    from . import prguard
    prguard.log_call(Path(os.environ["TTP_RUN_DIR"]).parent.parent, [("ready", [])], ["hook-denied", cmd],
                     refused=True)


# The project's web app token (state/web.token), named in a command
WEB_TOKEN_RE = re.compile(r"state/+web\.token\b")
# The per-user secrets store by path, and the runtime calls that hand back its values: a tool call's
# output stays in the run's log and its agent's transcript, so a run tests a key with
# `ttp secret check` and gives one to a command with `ttp secret env` (also `--secret`) instead.
SECRET_FILE_RE = re.compile(r"(?:\.tt-project|TTP_HOME\}?)/+secrets\.json\b")
SECRET_CALL_RE = re.compile(r"(?<!def )\b(?:credentials|load_secrets|env_for)\s*\(|\bendpoint\.saved\s*\(|\bsecrets_path\s*\(|\bsecretstore\.(?:load|BACKENDS|_\w+_load)\b")


def secret_read(cmd: str, segments: list[list[str]], cwd: str) -> str | None:
    """Why a Bash call that reads the secrets store is refused, else None: a command naming the store's
    path, or an interpreter (inline code, stdin or a script it runs) calling the runtime's readers."""
    why = ("a run must never read stored secrets (`ttp secret`): what it prints stays in the run's log and "
           "transcript. Test a saved endpoint key with `ttp secret check <alias>` (reachable, auth ok or "
           "auth failed), give one to a command with `ttp secret env <alias> -- <command>` (a secret stored with "
           "`ttp secret set` or `generate`: `ttp secret env --secret <name>[:<key>]=<VAR> -- <command>`), "
           "and call the secrets file 'the secrets store' in text")
    if SECRET_FILE_RE.search(cmd):
        return why
    try:
        from .project import secrets_path
        from .secretstore import ENV_VAR, command_words
        if any(w and w in cmd for w in (str(secrets_path()), ENV_VAR, *command_words())):
            return why   # also the keychain item or this machine's fetch command, run directly
    except Exception:
        pass
    runs = [s for s in segments if s and _interpreter_args(s) is not None]
    if runs and SECRET_CALL_RE.search(cmd):
        return why
    for f in _scripts(segments, cwd) if runs else []:
        try:
            if SECRET_CALL_RE.search(f.read_text(errors="replace")):
                return why
        except OSError:
            continue
    return None


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PATCH_TOOLS = {"apply_patch"}   # Codex's file edits: one patch text naming each file it changes
# A file a patch names (codex-cli 0.160: `*** Add File: p`, `*** Update File: p`, `*** Delete File: p`, `*** Move to: p`),
# read loosely (case, spacing) so a header Codex might still take is never missed
PATCH_FILE_RE = re.compile(r"^[ \t]*\*\*\*[ \t]*(?:(?:Add|Update|Delete)[ \t]+File|Move[ \t]+to)[ \t]*:[ \t]*(.*?)[ \t\r]*$",
                           re.M | re.I)
HARNESS_GH_WHY = ("the harness's gh wrapper (bin/gh) and the project's gh pre-hooks (bin/gh.d) change only in a "
                  "harness task: other runs never edit, move or disable them, and a command naming their path is "
                  "refused (call gh by its name; read them with the Read tool)")


# Tools of Claude Code's full standard set (every run has it, user msg #2302) that would step outside
# the harness: refused by name, inside subagents too, with the harness's own way instead.
_WAIT_WAY = ("a headless run ends when its task does, so the CLI's own schedule never wakes it: hand off "
             "`waiting` with a `retry_when`, start long work with `ttp detach`, and ask for a recurring job "
             "in a follow-up")
HARNESS_TOOLS = {
    "CronCreate": _WAIT_WAY, "CronDelete": _WAIT_WAY, "ScheduleWakeup": _WAIT_WAY,
    "RemoteTrigger": ("a remote agent runs outside the project's spend caps and guards: put the work in a "
                      "follow-up task, or start a subagent with the Agent tool"),
    "EnterWorktree": ("a run works in the worktree the harness made for it; another repository: your own "
                      "`git worktree` of it, used by path"),
    "ExitWorktree": "a run works in the worktree the harness made for it",
    "PushNotification": "a run tells the user with `ttp notify \"<text>\"`, which the project's settings route",
}
SEARCH_TOOLS = ("Grep", "Glob")
_GLOB_CHARS = re.compile(r"[*?[{]")


def harness_tool(tool: str) -> str | None:
    why = HARNESS_TOOLS.get(tool)
    return f"{tool} is refused here: {why}" if why else None


def search_scope(tool: str, args: dict, cwd: str) -> str | None:
    """Why a Grep or Glob call would search from /, the home folder or a folder above it (the worker
    rule: search the working directory, the project root and paths the charter or memory names)."""
    if tool not in SEARCH_TOOLS:
        return None
    starts = [str(args.get("path") or "") or cwd]
    pattern = str(args.get("pattern") or "") if tool == "Glob" else ""
    if pattern.startswith(("/", "~")):   # a Glob pattern names its own start: its part before the first wildcard
        parts = pattern.split("/")
        starts.append("/".join(parts[:next((i for i, x in enumerate(parts) if _GLOB_CHARS.search(x)), len(parts))])
                      or "/")
    home = os.path.realpath(os.path.expanduser("~"))
    for start in starts:
        path = os.path.expanduser(start)
        real = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        if real == "/" or real == home or home.startswith(real.rstrip(os.sep) + os.sep):
            return (f"{tool} would search all of {start}: search your working directory, the project root and "
                    "paths your charter or memory names, never / or the home folder (slow, and it can raise "
                    "privacy prompts)")
    return None


def _git_op_in_progress(path: str) -> bool:
    """Whether the repository holding `path` is in a merge, rebase or cherry-pick (a conflict to resolve)."""
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    from . import gitmemo
    try:
        gdir = gitmemo.run(["git", "-C", d or "/", "rev-parse", "--absolute-git-dir"], capture_output=True,
                           text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(gdir) and any(os.path.exists(os.path.join(gdir, n)) for n in
                              ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"))


def reviewer_write(payload: dict) -> str | None:
    """Why a review's file-tool write is refused: a review never edits the change it judges, so its Edit,
    Write and NotebookEdit calls write only in its run directory or a temporary folder, or in a
    repository with a merge or rebase in progress (resolving its conflict is the one edit a review makes)."""
    tool = str(payload.get("tool_name") or "")
    if os.environ.get("TTP_ROLE") != "reviewer" or tool not in EDIT_TOOLS:
        return None
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    path = os.path.expanduser(str(args.get("file_path") or args.get("notebook_path") or ""))
    if not path:
        return None
    real = os.path.realpath(path if os.path.isabs(path) else os.path.join(str(payload.get("cwd") or os.getcwd()), path))
    import tempfile
    roots = [os.environ.get("TTP_RUN_DIR", ""), tempfile.gettempdir(), "/tmp"]
    if any(r and (real + os.sep).startswith(os.path.realpath(r).rstrip(os.sep) + os.sep) for r in roots):
        return None
    if _git_op_in_progress(real):
        return None
    return ("a review never edits the change it judges: write notes and scratch files in your run directory "
            "($TTP_RUN_DIR); the one edit a review makes in the worktree, resolving a rebase or merge conflict, "
            "is made while that rebase or merge is in progress")


def _harness_task() -> bool:
    """This run works on a harness task of TTP_PROJECT (False when that cannot be read)."""
    task, base = os.environ.get("TTP_TASK", ""), os.environ.get("TTP_PROJECT", "")
    if not task.isdigit() or not base:
        return False
    try:
        from .project import Project
        p = Project(base)
        t = p.db.task(int(task)) if (p.state / "project.db").is_file() else None
    except Exception:
        return False
    return bool(t) and t.get("kind") == "harness"


def harness_gh_edit(payload: dict) -> str | None:
    """Why an Edit/Write tool call may not change the file it names: the run's harness bin/gh or a file
    in bin/gh.d, outside a harness task."""
    inp = payload.get("tool_input") or {}
    name = str(inp.get("file_path") or inp.get("notebook_path") or "")
    base = os.environ.get("TTP_PROJECT", "")
    if not name or not base:
        return None
    path = os.path.realpath(os.path.join(str(payload.get("cwd") or os.getcwd()), os.path.expanduser(name)))
    gh = os.path.join(os.path.realpath(os.path.join(base, "harness", "bin")), "gh")
    if path not in (gh, gh + ".d") and not path.startswith(gh + ".d" + os.sep) and not HARNESS_GH_RE.search(name):
        return None
    return None if _harness_task() else HARNESS_GH_WHY


def patch_files(payload: dict) -> list[str]:
    """Each file a Codex apply_patch call names (add, update, delete and move targets) in its patch text
    (tool_input itself, or its command, input or patch)."""
    inp = payload.get("tool_input")
    text = inp if isinstance(inp, str) else next((v for k in ("command", "input", "patch")
                                                  if isinstance(v := (inp or {}).get(k), str)), "")
    return PATCH_FILE_RE.findall(text)


def harness_gh_patch(payload: dict) -> str | None:
    """harness_gh_edit for each file a Codex apply_patch call names."""
    for name in patch_files(payload):
        if why := harness_gh_edit({**payload, "tool_input": {"file_path": name}}):
            return why
    return None


# Commands the Claude Code fence frees by name (providers/claude.py FENCE_EXCLUDED, also after `timeout N`):
# only a simple command; a redirect to or from a file, a substitution anywhere in the line, a shell
# wrapper, `&&`, `;` or a pipe keep the whole line sandboxed, where they cannot reach a host (measured).
# A project adds its own in project.json (sandbox.fence_freed_commands, fencefree), never here.
FENCE_FREED = {"ssh", "scp", "sftp", "rsync"}
SUBST_RE = re.compile(r"\$\(|`")
FILE_REDIRECT_RE = re.compile(r"<|>(?!&)")   # `2>&1` and `>&2` duplicate a descriptor: fine
SHELL_C_RE = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
ASSIGN_RE = re.compile(r"[A-Za-z_]\w*\+?=")   # `FOO=x ssh ...`: no such line is freed (measured, CLI 2.1.296)


def _freed_in(argv: list[str], depth: int = 0) -> str | None:
    """The ssh-like command `argv` runs: itself, or one in its `bash -c` or `eval` script."""
    from . import fencefree
    name = os.path.basename(argv[0])
    if name in FENCE_FREED or name in fencefree.names():
        return name
    if name not in SHELLS and name != "eval" or depth > 2:
        return None
    script = " ".join(argv[1:]) if name == "eval" else \
        next((argv[i + 1] for i in range(1, len(argv) - 1) if SHELL_C_RE.fullmatch(argv[i])), None)
    return script and next((h for s in _segments(script) if (h := _freed_in(s, depth + 1))), None)


def fenced_ssh(segments: list[list[str]], cmd: str) -> str | None:
    """Why a fenced worker's command `cmd` (its simple commands: `segments`) that runs ssh (or scp,
    sftp, rsync, or a project's sandbox.fence_freed_commands) would stay in the fence: beside other commands, with a redirect to or from a file,
    with a substitution, after a leading `VAR=value` assignment, or in a `bash -c` not run by `ttp lock ... --`.
    None outside such a run (TTP_FENCE_BY_NAME) or for a simple command."""
    if not os.environ.get("TTP_FENCE_BY_NAME"):
        return None
    hit, form, assign = None, "", ""
    for raw in _segments(cmd, strip=False):
        wrappers: list[list[str]] = []
        s = _strip_prefix(raw, wrappers)
        if s and (h := _freed_in(s)):
            hit = hit or h
            if (m := ASSIGN_RE.match(raw[0])) and not assign:
                assign = (f"A leading `{m[0].rstrip('+=')}=...` before {h} keeps the line in the write fence too: "
                          f"the sandbox frees no line that starts with an assignment. Drop the prefix and pass the "
                          f"value as an option of the command (e.g. `ssh -o Name=value host cmd`), or set it inside "
                          f"the quoted remote command (`ssh host 'FOO=x cmd'`).")
            if os.path.basename(s[0]) != h and not any(w[:1] == ["lock"] for w in wrappers):
                form = form or f"`{os.path.basename(s[0])} -c` wrapper"
    if not hit:
        return None
    if len(segments) > 1:
        form = "`&&`, `;` or pipe"
    elif SUBST_RE.search(cmd):
        form = "`$(...)` or backtick substitution"
    elif FILE_REDIRECT_RE.search("".join(" " if q else c for c, (q, _) in zip(cmd, _quoting(cmd)))):
        form = "redirect to or from a file"
    if not form:
        return assign and (f"{hit} cannot reach another host from this line. {assign} There host names do not "
                           f"resolve and 127.0.0.1 is the sandbox's own loopback, so a refusal is not a host or "
                           f"tunnel outage.")
    return (
        f"{hit} reaches another host only as a simple command on its own. The sandbox frees `{hit} ...` "
        f"(also after `timeout N` or `ttp lock <res> --`) by name, but this line's {form} keeps the whole "
        f"line in the write fence, as does any `<`, `<<`, `> file`, `$(...)`, backtick, `bash -c` wrapper, "
        f"`&&`, `;` or pipe (`2>&1` is fine). There host names do not resolve and 127.0.0.1 is the "
        f"sandbox's own loopback (only forwarded host ports answer), so a refusal is not a host or tunnel "
        f"outage. Put a pipe inside the quoted remote command "
        f"(`ssh host 'cmd | tail -20'`), or write the line into a local wrapper script (e.g. "
        f"`ssh host 'bash -s' < remote.sh > out.txt`) and run `ttp lock <res> -- bash wrapper.sh`: a plain "
        f"`bash wrapper.sh` stays fenced" + (f". {assign}" if assign else ""))


def lock_privileged(cmd: str) -> str | None:
    """Why a worker's `ttp lock <res> -- sudo ...` (or su, doas, pkexec, run0; also in a `sh -c` string) is
    refused: `ttp lock` itself refuses it (privlock.py), and a privileged command goes through `ttp host-run`.
    A lock inside `ttp host-run -- ...` is the daemon's, under host-run's allowlist (hostrun.unlocked)."""
    from . import privlock
    if not privlock.sandboxed_run():
        return None   # an unsandboxed run may run sudo itself
    for seg in _segments(cmd, strip=False):
        i = next((j for j, w in enumerate(seg) if os.path.basename(w) == "ttp" and seg[j + 1:j + 2] == ["lock"]), None)
        if i is not None and any(os.path.basename(w) == "ttp" and seg[j + 1:j + 2] == ["host-run"]
                                 for j, w in enumerate(seg[:i])):
            continue
        inner = privlock.lock_command(seg[i:]) if i is not None else None
        if inner and (why := privlock.refusal(inner)):
            return why
    return None


# An argv element or command string naming an ssh-like command in a non-shell script (`["ssh", host]`)
QUOTED_FREED_RE = re.compile(r"""["'](?:ssh|scp|sftp|rsync)(?:["']|\s)""")


def _script_reaches_host(text: str) -> str | None:
    """The ssh-like command (FENCE_FREED) a script's text runs: a shell command (also in `bash -c`,
    after wrappers or `ttp lock ... --`), or a quoted argv element or command string, else None."""
    commands, _ = _split(text)
    lines = "\n".join(line for line in commands.split("\n") if not line.lstrip().startswith("#"))
    hit = next((h for s in _segments(lines) if (h := _freed_in(s))), None)
    m = hit or QUOTED_FREED_RE.search(lines)
    return hit or (m and re.sub(r"""["'\s]""", "", m[0]))


def fenced_script(segments: list[list[str]], cmd: str, cwd: str) -> str | None:
    """A hint for a fenced worker's command `cmd` (its simple commands: `segments`) that runs a script
    calling ssh (or scp, sftp, rsync) inside the fence: any line but a simple `ttp lock <res> -- <script>`
    (in a call by itself, no redirect to a file, substitution or subshell). fenced_ssh covers ssh named
    on the line itself. None outside such a run (TTP_FENCE_BY_NAME), for `ttp detach` (the job runs
    elsewhere) or when no script the line runs names one."""
    if not os.environ.get("TTP_FENCE_BY_NAME"):
        return None
    raws = _segments(cmd, strip=False)
    wrappers: list[list[str]] = []
    for raw in raws:
        _strip_prefix(raw, wrappers)
    if any(w[:1] == ["detach"] for w in wrappers):
        return None
    unquoted = "".join(" " if q else c for c, (q, _) in zip(cmd, _quoting(cmd)))
    if len(segments) == 1 and not SUBST_RE.search(cmd) and not FILE_REDIRECT_RE.search(unquoted) \
            and "(" not in unquoted and any(w[:1] == ["lock"] for w in wrappers):
        return None
    gdir = cwd
    for s in segments:
        if os.path.basename(s[0]) == "cd" and len(s) > 1:
            gdir = os.path.join(gdir, os.path.expanduser(s[1]))
        for f in _scripts([s], gdir):
            try:
                hit = _script_reaches_host(f.read_text(errors="replace"))
            except OSError:
                continue
            if hit:
                name = os.path.basename(s[0])
                run = f"{name} {f}" if _interpreter_args(s) is not None else \
                    f"bash {f}" if name in ("source", ".") else str(f)
                return (f"{f.name} runs {hit}, which reaches another host only from a simple command on its own. "
                        f"Run from this line (a plain script run, cd, `&&`, `;`, a pipe, a redirect to a file, "
                        f"`$(...)` or a subshell) it stays in the sandbox's write fence, where host names do not "
                        f"resolve, so an unresolved host is not an outage. Run it in a call by itself as "
                        f"`ttp lock <res> -- {run}` (absolute path, nothing else on the line; put a pipe or "
                        f"redirect inside the script), and the next step in another call")
    return None


# git's network commands the fence frees by name (FENCE_EXCLUDED); git runs ssh for an SSH remote.
GIT_NET = {"fetch", "pull", "push", "ls-remote", "clone"}
SSH_URL_RE = re.compile(r"^(?:(?:git\+)?ssh://|[\w.-]+@[\w.-]+:)")   # an argument: `HEAD:main` is a refspec
SSH_REMOTE_RE = re.compile(r"^(?:(?:git\+)?ssh://|[\w.-]+@[\w.-]+:|[\w.-]+:(?!//))")   # a configured URL
URL_RE = re.compile(r"^[a-z][\w+.-]*://")


def _ssh_remote(gdir: str) -> bool:
    """`gdir`'s repository has a remote with an SSH URL (True when that cannot be read: the refusal's
    advice works either way)."""
    try:
        r = subprocess.run(["git", "-C", gdir, "config", "--get-regexp", r"^remote\..*\.(push)?url$"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    if r.returncode not in (0, 1):
        return True
    return any(SSH_REMOTE_RE.match(line.split(" ", 1)[-1].strip()) for line in r.stdout.splitlines())


def fenced_git(segments: list[list[str]], cwd: str) -> str | None:
    """Why a fenced worker's git network command (fetch, pull, push, ls-remote, clone, remote or
    submodule update) against an SSH remote would fail: beside other commands, or after git options
    such as `-C` (the fence's patterns match `git fetch ...` only). None outside such a run, for an
    explicit non-SSH URL, or where the repository has no SSH remote."""
    if not os.environ.get("TTP_FENCE_BY_NAME"):
        return None
    from . import gitguard
    gdir = cwd
    for s in segments:
        if os.path.basename(s[0]) == "cd" and len(s) > 1:
            gdir = os.path.join(gdir, os.path.expanduser(s[1]))
        if os.path.basename(s[0]) != "git":
            continue
        opts, sub, rest = gitguard.split(s[1:])
        if not (sub in GIT_NET or sub in ("remote", "submodule") and rest[:1] == ["update"]):
            continue
        if len(segments) < 2 and not opts:
            continue
        urls = [a for a in rest if SSH_URL_RE.match(a) or URL_RE.match(a)]
        if urls:
            ssh = any(SSH_URL_RE.match(a) for a in urls)
        else:
            d = next((opts[i + 1] for i, o in enumerate(opts[:-1]) if o == "-C"), "")
            ssh = _ssh_remote(os.path.join(gdir, os.path.expanduser(d)))
        if ssh:
            name = f"git {sub}" + (" update" if sub in ("remote", "submodule") else "")
            return (f"`{name}` over SSH reaches the host only when it runs on its own as `{name} ...`: inside "
                    f"`&&`, `;` or a pipe, or after git options such as `-C`, it stays in the write fence, where "
                    f"host names do not resolve. `cd` in a separate call, then run `{name} ...` as a command by "
                    f"itself (under `timeout N` is fine), and the next step in another call")
    return None


def _deny(why: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"tt-project: {why}."}}


def pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    if not os.environ.get("TTP_RUN_DIR"):
        return None, None
    from . import pathguard, untrusted
    if why := untrusted.mark_refusal(str(payload.get("tool_name") or ""), payload.get("tool_input") or {}):
        return _deny(why), None   # the run's untrusted-input mark, marked or not
    if why := pathguard.refusal(payload):   # a write into a credential folder, on any tool
        return _deny(why), None
    if why := harness_tool(str(payload.get("tool_name") or "")):
        return _deny(why), None
    if payload.get("tool_name") in EDIT_TOOLS | PATCH_TOOLS:
        why = harness_gh_edit(payload) if payload["tool_name"] in EDIT_TOOLS else harness_gh_patch(payload)
        why = why or reviewer_write(payload) or local_guard(payload)
        return (_deny(why) if why else None), None
    if payload.get("tool_name") in ("Read", "Grep", "Glob"):   # a credential store's contents (credguard)
        from . import credguard
        args, cwd = payload.get("tool_input") or {}, str(payload.get("cwd") or os.getcwd())
        why = credguard.tool_refusal(str(payload["tool_name"]), args, cwd)
        if not why:   # a run reading untrusted input: a credential file outside its worktree
            from . import untrusted
            why = untrusted.tool_refusal(str(payload["tool_name"]), args, cwd)
        why = why or search_scope(str(payload["tool_name"]), args if isinstance(args, dict) else {}, cwd)
        why = why or local_guard(payload)
        return (_deny(why) if why else None), None
    if str(payload.get("tool_name") or "").startswith("mcp__"):   # a connector post to others (aitag)
        from . import aitag
        why = aitag.tool_problem(str(payload["tool_name"]), payload.get("tool_input"), aitag.project_config())
        why = why or local_guard(payload)
        return (_deny(why) if why else None), None
    if payload.get("tool_name") != "Bash":
        why = local_guard(payload)
        return (_deny(why) if why else None), None
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    commands, scan = _split(cmd)
    segments = _segments(commands)
    why = draft_bypass(cmd)
    bypass = bool(why)
    if not why and (any(os.path.basename(s[0]) == "ttp" and s[1:2] == ["say"] for s in segments)
                    or (re.search(r"/api/say\b", cmd) and HTTP_CLIENT_RE.search(cmd))):
        # What a run posts as the user could count as the user's approval (pr_approve).
        why = ("a run must not post messages as the user; report with `ttp note` and the hand-off, "
                   "tell the user with `ttp notify \"<text>\"`, "
                   "and reach another project with `ttp note --to <project> \"<text>\"`")
    if not why and WEB_TOKEN_RE.search(cmd):
        # Whoever holds it acts as the user in the web app; the Read deny rule covers the file tools.
        why = ("the web token is the user's credential and a run must not read it; any command naming its "
               "path is refused, so write a commit message with `git commit -F <file>` or call it 'the web token file'")
    if not why and (HARNESS_GH_RE.search(cmd) or HARNESS_BIN_RE.search(cmd) and GH_D_RE.search(cmd)) \
            and not _harness_task():
        why = HARNESS_GH_WHY
    if not why and "***" in cmd:   # a patch in the command (`apply_patch <<EOF`), relative paths too
        why = harness_gh_patch(payload)
    if not why:
        why = secret_read(cmd, segments, str(payload.get("cwd") or os.getcwd()))
    if not why:
        from . import credguard
        why = credguard.bash_refusal(cmd, _all_segments(commands))
    if not why:   # a run reading untrusted input: no foreign push, write or credential read
        from . import untrusted
        why = untrusted.bash_refusal(_all_segments(commands), _segments(commands, strip=False),
                                     str(payload.get("cwd") or os.getcwd()))
    if not why:
        why = git_push_bypass(segments)
    if not why:
        why = fenced_ssh(segments, commands) or fenced_git(segments, str(payload.get("cwd") or os.getcwd()))
    if not why:
        why = lock_privileged(cmd)
    if not why and os.environ.get("TTP_NO_USER_SITE") == "1":   # usersite: a host of several projects
        from . import usersite
        if why := usersite.refusal(commands):
            _log_refusal(cmd, kind="user_site")
            try:
                usersite.flag(cmd)
            except Exception:   # the refusal stands; only the coordinator's count is lost
                pass
    if not why:
        from . import killguard
        why = killguard.refusal(cmd, str(payload.get("cwd") or os.getcwd()))
    if not why:   # a curl or wget post to others (aitag)
        from . import aitag
        why = aitag.http_problem(segments, str(payload.get("cwd") or os.getcwd()))
    if not why:
        path_changed, trusted = path_change(scan), _project_trusted(cmd)
        for f in _scripts(segments, str(payload.get("cwd") or os.getcwd())):
            try:
                why = draft_bypass(f.read_text(errors="replace"), path_changed, trusted)
            except OSError:
                continue
            if why:
                why = f"{f.name}: {why}"
                bypass = True
                break
    if bypass:
        _log_bypass(cmd)
    if not why:
        why = idle_wait(cmd)
    if not why:
        why = full_suite(cmd, segments, str(payload.get("cwd") or os.getcwd()))
    if not why:
        why = too_many_workers(cmd)
    if not why:
        why = too_many_build_jobs(cmd)
    if not why:
        why = device_command(commands, cmd)
    if not why:
        why = local_guard(payload)
    if not why:
        from . import fgwait
        cwd = str(payload.get("cwd") or os.getcwd())
        hint = "\n\n".join(h for h in (fenced_script(segments, commands, cwd), wide_read(commands, cwd),
                                        fgwait.reminder(cmd)) if h)
        out = {"hookEventName": "PreToolUse", **({"additionalContext": hint} if hint else {})}
        return ({"hookSpecificOutput": out} if len(out) > 1 else None), None
    return _deny(why), None


def local_guard(payload: dict) -> str | None:
    """Why one of the project's own guards (project.json guards.local, localguard.py) refuses this tool
    call, logged to the refusal log with kind local:<file>; asked after the built-in guards."""
    from . import aitag, localguard
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    try:
        cfg = aitag.project_config()
    except Exception as e:   # unreadable settings: the listed guards cannot be found, so none is skipped silently
        return f"the project's settings could not be read for its local guards ({type(e).__name__})" \
            if tool == "Bash" else None
    cwd, harness = str(payload.get("cwd") or os.getcwd()), localguard.harness_dir()
    edits = [("Edit", {"file_path": n}) for n in patch_files(payload)] if tool in PATCH_TOOLS else \
        [(tool, args)] if tool == "Bash" or tool in EDIT_TOOLS else []
    if (f := next((g for t, a in edits if (g := localguard.names_guard(cfg, harness, t, a, cwd))), None)) \
            and not _harness_task():
        return (f"the project's local guard {f.name} ({localguard.KEY}) changes only in a harness task: other runs "
                f"never edit, move or disable it, and a command naming its path is refused (read it with the Read tool)")
    hit = localguard.refusal(cfg, tool, args)
    if not hit:
        return None
    if os.environ.get("TTP_RUN_DIR"):
        _log_refusal(str(args.get("command") or "") if tool == "Bash" else f"{tool} {json.dumps(args)[:400]}",
                     kind=hit[0], reason=hit[1])
    return hit[1]


def idle_wait(cmd: str) -> str | None:
    """In a chat-route run (runner.WAITS_IDLE_ENV), a wait call (runner.is_wait: a sleep of 30 s or
    more, a sleep-poll loop, `tail -f`, `watch`) is refused: waits filling most of its stall_s end the
    run as stalled and rerun it on another provider, paying twice for the same result."""
    if os.environ.get("TTP_WAITS_IDLE") != "1":
        return None
    from . import runner
    if not runner.is_wait(cmd):
        return None
    return ("this run must not wait in a tool call (a sleep of 30 s or more, a sleep-poll loop, `tail -f`, "
            "`watch`): long waits end it as stalled and rerun it elsewhere. Hand off `waiting` with a "
            "`retry_when` that exits 0 once the wait is over, such as `ttp checks --result <run dir>` for "
            "detached checks or the one `ttp detach` printed. A commit message naming such a command: "
            "use `git commit -F <file>`")


# A command that prints more than this many lines of one file gets a reminder (never a refusal):
# every line read stays in the context and is paid for again on each later call.
WIDE_READ_LINES = 300
# Line counts stop here: a bigger file is "more than this", which is all the reminder needs.
COUNT_MAX_BYTES = 32 << 20
# stdout sent to a file (`> f`, `>> f`, `1> f`, `&> f`), not `2>&1` or `2> f`
STDOUT_REDIRECT_RE = re.compile(r"(?:^|[^\d<>&])>|(?:^|\s)1>|&>")
SED_RANGE_RE = re.compile(r"^\s*(\d+|\$)\s*(?:,\s*(\d+|\$|\+\d+)\s*)?p\s*$")


def _line_count(path: str, cwd: str) -> int | None:
    f = Path(os.path.expanduser(path))
    f = f if f.is_absolute() else Path(cwd) / f
    try:
        if not f.is_file():
            return None
        n, ends_nl = 0, True
        with open(f, "rb") as fh:
            read = 0
            while read < COUNT_MAX_BYTES:
                b = fh.read(1 << 20)
                if not b:
                    break
                read += len(b)
                n += b.count(b"\n")
                ends_nl = b.endswith(b"\n")
        return n + (not ends_nl)
    except OSError:
        return None


def _sed_lines(args: list[str], cwd: str) -> list[tuple[str, int]]:
    """(file, lines printed) of `sed -n 'a,bp' file`: only print-only scripts are counted."""
    quiet, scripts, files, i = False, [], [], 0
    while i < len(args):
        a = args[i]
        short = re.fullmatch(r"-([nEsrzu]*)(e?)", a)
        if a.startswith(("-i", "--in-place")):
            return []
        if a in ("--quiet", "--silent") or short and "n" in short[1]:
            quiet = True
        if a == "--expression" or short and short[2]:
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 1
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-"):
            pass
        elif not scripts:
            scripts.append(a)
        else:
            files.append(a)
        i += 1
    if not quiet or len(files) != 1:
        return []
    ranges = []
    for part in ";".join(scripts).replace("\n", ";").split(";"):
        if not part.strip():
            continue
        m = SED_RANGE_RE.match(part)
        if not m:
            return []
        ranges.append(m.groups())
    if not ranges:
        return []
    n = _line_count(files[0], cwd)
    if n is None:
        return []
    out = 0
    for a, b in ranges:
        start = n if a == "$" else int(a)
        end = start if b is None else n if b == "$" else start + int(b[1:]) if b.startswith("+") else int(b)
        out += max(0, min(end, n) - start + 1)
    return [(files[0], out)]


def _count_arg(args: list[str]) -> tuple[str | None, list[str]]:
    """head/tail: the -n value as written (default "10") and the file arguments."""
    count, files, i = "10", [], 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--lines"):
            count = args[i + 1] if i + 1 < len(args) else count
            i += 1
        elif a.startswith("--lines="):
            count = a.split("=", 1)[1]
        elif re.fullmatch(r"-n[+-]?\d+", a):
            count = a[2:]
        elif re.fullmatch(r"-\d+", a):
            count = a[1:]
        elif a in ("-c", "--bytes", "-f", "--follow", "-F") or a.startswith(("-c", "--bytes")):
            return None, []
        elif not a.startswith("-"):
            files.append(a)
        i += 1
    return count, files


def _printed(argv: list[str], cwd: str) -> list[tuple[str, int]]:
    """(file, about how many of its lines the command prints) for sed -n, cat, nl, head and tail."""
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == "sed":
        return _sed_lines(args, cwd)
    if name in ("cat", "nl"):
        return [(f, n) for f in args if not f.startswith("-") and f != "-"
                for n in [_line_count(f, cwd)] if n is not None]
    if name in ("head", "tail"):
        count, files = _count_arg(args)
        out = []
        for f in files if count is not None else []:
            n = _line_count(f, cwd)
            m = re.fullmatch(r"([+-]?)(\d+)", count)
            if n is None or not m:
                continue
            k = int(m[2])
            want = (n - k if m[1] == "-" else k) if name == "head" else (n - k + 1 if m[1] == "+" else k)
            out.append((f, max(0, min(n, want))))
        return out
    return []


def wide_read(commands: str, cwd: str) -> str | None:
    """A reminder when a command shows more than WIDE_READ_LINES lines of one file: sed -n with a
    big range, cat or nl of a large file, head/tail with a large count. Output piped on or written
    to a file is not shown, so it does not count."""
    worst = ("", 0)
    try:
        for line in commands.split("\n"):
            for s, e, sep in _pieces(line):
                piece = line[s:e]
                bare = re.sub(r"'[^']*'|\"[^\"]*\"", "", piece)
                if sep.startswith("|") or STDOUT_REDIRECT_RE.search(bare) or re.search(r"\bttp\s+clip\b", bare):
                    continue
                for argv in _segments(piece)[:1]:
                    for f, n in _printed(argv, cwd):
                        if n > worst[1]:
                            worst = (f, n)
    except Exception:
        return None
    if worst[1] <= WIDE_READ_LINES:
        return None
    f, n = worst
    return (f"tt-project: this prints about {n} lines of {f}, and every line stays in your context, paid for "
            "again on each later call. Find the part you need with `grep -n` first, then read narrow ranges "
            "(`sed -n 'a,bp'`, or Read with offset and limit); do not re-read a range you already have. "
            "This is a reminder: the command still runs.")


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


def _inline_code(argv: list[str]) -> str | None:
    """The code of `python -c CODE` (also `-Ic CODE`, `-cCODE`), else None."""
    if not argv or not re.fullmatch(r"python[\d.]*", os.path.basename(argv[0])):
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if not a.startswith("-") or a in ("-", "--"):
            return None   # a script or stdin
        if a.startswith("--"):   # --check-hash-based-pycs MODE, --version ...
            i += 2 if a == "--check-hash-based-pycs" else 1
            continue
        for j, c in enumerate(a[1:], 1):
            if c == "c":
                return a[j + 1:] or (argv[i + 1] if i + 1 < len(argv) else None)
            if c in "mWXQ":   # -m MOD runs a module; -W/-X/-Q take the rest of the word or the next one
                if c == "m":
                    return None
                i += 0 if a[j + 1:] else 1
                break
        i += 1
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
    checks (`delivery.push_checks` and push_checks_by_repo, what `ttp checks` runs). Read fresh: a
    project may change them."""
    base = os.environ.get("TTP_PROJECT")
    if not base:
        return []
    from .project import Project
    from .push import all_checks
    p = Project(base)
    if not p.exists():
        return []
    out = []
    top = "/top"   # checks run from the repository's top; a cd out of it makes the run unclear
    for c in all_checks(p.config().get("delivery") or {}):
        for args, d in _pytest_dirs(_segments(str(c)), top, "(" in str(c)):
            if d is None or not (d + "/").startswith(top + "/"):
                continue
            paths, select = _pytest_parts(args, os.path.relpath(d, top))
            out.append((str(c), paths, select))
    return out


def too_many_workers(cmd: str) -> str | None:
    """Why `cmd` is refused when a pytest in it asks for more workers (`-n N`) than the run's test-worker
    budget (TTP_TEST_SLOTS, which the daemon gives worker runs: testslots.worker_env), or for more than
    one outside `ttp clip` / `ttp sandbox`, which take them from the host's test pool (testslots.direct)
    unless the run already holds slots; else None."""
    from . import testslots
    try:
        k = int(os.environ.get(testslots.SLOTS_ENV) or 0)
    except ValueError:
        return None
    if not k or re.search(r"\bttp\s+checks\b", cmd):
        return None   # `ttp checks` lowers it itself
    how = (f"`ttp clip -- ttp sandbox -- <pytest ...>`, which takes up to {k} test workers from the host's "
           f"test slots (fewer, or a short wait, while other runs hold them)")
    if big := testslots.over(cmd, k):
        return (f"pytest -n {max(big)} is above this run's test-worker budget of {k} on this shared host. "
                f"Use -n {k} or fewer (or `-n auto`) through {how}")
    if not os.environ.get(testslots.HELD_ENV) and (bare := testslots.unpooled(cmd)):
        return (f"pytest -n {bare[0]} outside `ttp clip` or `ttp sandbox` holds none of this shared host's "
                f"test slots. Run it as {how}, or with no -n")
    return None


def build_share() -> int:
    """This run's build share k: CMAKE_BUILD_PARALLEL_LEVEL and MAKEFLAGS' -j<k> as testslots.worker_env
    sets them together, else 0 (no build pool, or the variables are the user's own)."""
    try:
        k = int(os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL") or 0)
    except ValueError:
        return 0
    return k if k > 0 and f"-j{k}" in os.environ.get("MAKEFLAGS", "").split() else 0


def too_many_build_jobs(cmd: str) -> str | None:
    """Why `cmd` is refused when a make, gmake, ninja or `cmake --build` in it asks for more jobs than the
    run's build share (build_share()): an explicit -j N above it (it overrides MAKEFLAGS), a -j the shell
    fills in ($(nproc), $N), an unlimited `make -j` or a ninja with no -j (ninja reads no MAKEFLAGS). A build run through `ttp clip` (it takes
    build slots and lowers -j itself), inside `ttp checks` or under a held slot grant is not refused."""
    from . import testslots
    k = build_share()
    if not k or re.search(r"\bttp\s+checks\b", cmd) or any(
            os.environ.get(v) for v in (testslots.HELD_ENV, testslots.BUILD_HELD_ENV)):
        return None
    clipped = [(seg[0][0], seg[-1][1]) for seg in testslots._segments(cmd)
               if any(testslots._base(a) == "ttp" and b == "clip" for (*_, a), (*_, b) in zip(seg, seg[1:]))]
    big = [n for start, _, _, n in testslots._edits(cmd, k, True)
           if not any(lo <= start <= hi for lo, hi in clipped)]
    if not big:
        return None
    asked = (f"-j {max(big)}" if max(big) > 0 else
             "a job count the shell fills in (`-j$(nproc)`, `-j $N`), so its size is unknown" if -1 in big else
             "unlimited jobs (`make -j` with no number, or ninja with no -j)")
    return (f"a build with {asked} is above this run's build share of {k} jobs on this shared host. "
            f"Use -j {k} or fewer, or run it as `ttp clip -- <build>` (it takes the host's build slots and "
            f"caps the jobs itself)")


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
                _log_refusal(cmd, kind="full_suite", check=check)
                return (f"this runs the project's full check `{check}`. While you work, run only the tests "
                        "you changed (a file, `-k <expr>`, `file::test`); run the full checks once, committed, "
                        "with `ttp checks` (it reuses a pass already recorded for the same tree; "
                        "`ttp checks --detach` when they take long). If you truly need it here, put "
                        f"`{FULL_SUITE_OK}=1` before the command")
    return None


def _log_refusal(cmd: str, kind: str, **extra: str) -> None:
    import time
    try:
        with open(Path(os.environ["TTP_RUN_DIR"]) / REFUSALS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, **{k: v[:500] for k, v in extra.items()},
                                "command": cmd[:500]}) + "\n")
    except OSError:
        pass


# Device commands a run that holds no device (TTP_NO_DEVICE=1) is refused outside a device `ttp lock` or
# `ttp devq` (device_command): reset, flash and power tools by program name, each with the argument words
# that make a call one (None: any call).
DEVICE_TOOLS = {"tt-smi": re.compile(r"-r[\d,]*|--reset(?:=.*)?|-{1,2}glx_reset\w*"), "tt-flash": None,
                "tt-topology": None, "ipmitool": re.compile(r"power|reset")}
# Programs that only list, stat or search for a device node path (a word naming it is a pattern or a
# path to walk), never open the device
NODE_LISTERS = {"ls", "dir", "vdir", "exa", "eza", "tree", "stat", "test", "[", "readlink", "realpath", "lsof",
                "fuser", "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "fdfind",
                "locate", "git"}
DECLARE = {"export", "unset", "declare", "typeset", "readonly", "local"}


def _device_lock(words: list[str]) -> bool:
    """`ttp lock <resource>[,...] ...` takes a device lock (ends in `-device`, or config device.locks)."""
    res = next((w for w in words[1:] if not w.startswith("-")), "")
    names = [n for n in res.split(",") if n]
    if any(n.endswith("-device") for n in names):
        return True
    try:
        from .locks import device_locks
        from .project import Project
        p = Project(os.environ["TTP_PROJECT"])
        return bool(set(names) & device_locks(p.config()))
    except Exception:
        return False


def _device_use(commands: str, nodes: list[str], depth: int = 0) -> tuple[str, str] | None:
    """(the command, what makes it device work) for the first simple command of `commands` (or of a
    `sh -c` string there) that resets, flashes or power-cycles a device (DEVICE_TOOLS), names a device
    node (`nodes`) or changes or drops the device mask (machines.DEVICE_MASK, `env -i`), outside a
    device `ttp lock` or `ttp devq`; None when there is none."""
    from .machines import DEVICE_MASK
    mask = "|".join(map(re.escape, DEVICE_MASK))
    locked = False
    for raw in _segments(commands, strip=False):
        wrappers: list[list[str]] = []
        argv = _strip_prefix(raw, wrappers)
        if any(w[:1] == ["lock"] and _device_lock(w) for w in wrappers) or argv[:2] == ["ttp", "devq"]:
            locked = True
            continue
        shown, pre = " ".join(raw)[:120], raw[:len(raw) - len(argv)]
        name = os.path.basename(argv[0]) if argv else ""
        if name in DEVICE_TOOLS and (DEVICE_TOOLS[name] is None
                                     or any(DEVICE_TOOLS[name].fullmatch(w) for w in argv[1:])):
            return shown, f"runs the device tool {name}"
        named = [v for w in argv for v in (w, w.partition("=")[2]) if v]   # `if=<node>`, `--file=<node>`
        if name not in NODE_LISTERS and any(v == p or v.startswith(p.rstrip("/") + "/") for v in named for p in nodes):
            return shown, "opens a device node"
        envs = [i for i, w in enumerate(pre) if os.path.basename(w) == "env"]
        declared = name in DECLARE and any(re.fullmatch(rf"(?:{mask})(?:\+?=.*)?", w) for w in argv[1:])
        if mask and (declared or any(re.fullmatch(rf"(?:{mask})\+?=.*|--unset=(?:{mask})", w) for w in pre)
                     or any(a in ("-u", "--unset") and b in DEVICE_MASK for a, b in zip(pre, pre[1:]))):
            return shown, "changes the device mask (" + ", ".join(DEVICE_MASK) + ")"
        if envs and any(re.fullmatch(r"-[\w]*i\w*|--ignore-environment|-", w) for w in pre[envs[0] + 1:]):
            return shown, "drops the device mask with the rest of the environment"
        if name in SHELLS and depth < 3:
            inner = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)), None)
            if inner and (hit := _device_use(inner, nodes, depth + 1)):
                return hit
    if not locked and (m := next((m for p in nodes if (m := re.search(rf"[<>]&?\s*{re.escape(p)}(?![\w.-])", commands))), None)):
        return m[0], "redirects to or from a device node"
    return None


def device_command(commands: str, cmd: str = "") -> str | None:
    """Why the call is refused: a run that holds no device (TTP_NO_DEVICE=1) on a machine tagged `device`
    runs device work (_device_use) outside a device `ttp lock` (which itself refuses a held or paused
    device) or `ttp devq`. Nothing runs in a namespace: this refusal and the TT_VISIBLE_DEVICES mask are
    the device safety. It guards against accidents, not a determined bypass (a script file or inline
    code is not read), and only refuses new calls. A project adds its own refusals in guards.local."""
    if os.environ.get("TTP_NO_DEVICE", "").strip() != "1":
        return None
    from . import machines
    try:
        if not machines.device_host():
            return None
        hit = _device_use(commands, machines.device_nodes())
    except Exception:
        return None
    if not hit:
        return None
    if os.environ.get("TTP_RUN_DIR"):
        _log_refusal(cmd or commands, kind="no_device")
    return (f"this run holds no device (TTP_NO_DEVICE=1) on a device host, and `{hit[0]}` {hit[1]}. Device work "
            f"goes through the device broker: `ttp lock <device> -- <cmd>` (refused while the device is held "
            f"or paused) or `ttp devq`")


# Claude Code's Bash sandbox (bwrap) binds each fenced path that exists when the command starts; one
# that another command or a worktree removal deletes before bwrap mounts it fails the whole command
# (measured: config.lock placeholders of concurrent sandboxes, removed `.git/worktrees/<name>` entries).
MOUNT_RACE = re.compile(r"bwrap: Can't (?:get type of source(?: path)?|find source path|bind mount) "
                        r"[^\n]{0,600}?: No such file or directory")
MOUNT_RACE_HINT = ("tt-project: transient sandbox mount race (a path the sandbox binds vanished while it "
                   "started), not a fault of your command: re-run the same command unchanged.")

NO_NEW_PRIVS = re.compile(r"no new privileges|sudo\.conf is owned by uid \d+|effective uid is not 0", re.I)
NO_NEW_PRIVS_HINT = ("tt-project: your sandbox cannot gain privileges, so sudo fails inside it. A command the project "
                     "allows runs on the host through the daemon: `ttp host-run -- <command...>` (config "
                     "runner.host_commands, set by the user or the coordinator). Not listed: name the exact "
                     "command in your hand-off.")
# An ssh whose ProxyCommand runs sudo dies before any banner where nothing can gain privileges, and ssh
# then blames the host. Hinted only in a run the daemon started sandboxed (it sets TTP_SANDBOXED=1 there):
# unsandboxed (the default), sudo works and the host really refused.
KEX_FAIL = re.compile(r"kex_exchange_identification", re.I)
SANDBOXED_ENV = "TTP_SANDBOXED"
KEX_NO_NEW_PRIVS_HINT = ("tt-project: this run is sandboxed, so nothing in it can gain privileges (no_new_privs). "
                         "If that host's ssh ProxyCommand runs sudo, sudo dies at once and ssh says "
                         "'kex_exchange_identification: Connection closed by remote host': the host is not "
                         "refusing. `ttp ssh-preflight <host>` exits 13 for this case. Run that ssh from a "
                         "model-free command schedule or pre-step, or use a ProxyCommand without sudo or a "
                         "ControlMaster opened outside the sandbox (tt-project-harness "
                         "references/ssh-preflight.md).")


def post_tool_use_failure(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    """A failed tool call: the hint for a sandbox mount race, and any coordinator update or hand-off
    problem PostToolUse would have delivered (Claude Code sends no PostToolUse for a failed call)."""
    out, delivered = post_tool_use(payload)
    parts = [out["hookSpecificOutput"]["additionalContext"]] if out else []
    if MOUNT_RACE.search(str(payload.get("error") or "")):
        parts.insert(0, MOUNT_RACE_HINT)
    if NO_NEW_PRIVS.search(str(payload.get("error") or "")):
        parts.insert(0, NO_NEW_PRIVS_HINT)
    if KEX_FAIL.search(str(payload.get("error") or "")) and os.environ.get(SANDBOXED_ENV, "").strip() == "1":
        parts.insert(0, KEX_NO_NEW_PRIVS_HINT)
    if not parts:
        return None, delivered
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                   "additionalContext": "\n\n".join(parts)}}, delivered


HANDLERS = {"PostToolUse": post_tool_use, "PostToolUseFailure": post_tool_use_failure,
            "PreToolUse": pre_tool_use, "Stop": stop}


def _coord_pre_tool_use(payload: dict):
    try:
        from . import coordtools
    except Exception as e:   # fails closed, unlike the worker hooks
        return _deny(f"the coordinator tool check could not load ({type(e).__name__})"), None
    return coordtools.pre_tool_use(payload)


COORD_EVENT = "CoordPreToolUse"   # a session coordinator's tools (coordtools.py)
HANDLERS[COORD_EVENT] = _coord_pre_tool_use


def _coord_post(failed: bool):
    def handle(payload: dict):
        from . import coordtools
        return coordtools.post_tool_use(payload, failed)
    return handle


# A full-tools session coordinator's audit log of its mutating calls (coordtools.post_tool_use).
HANDLERS.update(CoordPostToolUse=_coord_post(False), CoordPostToolUseFailure=_coord_post(True))


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        if event != COORD_EVENT:
            return 0
        payload = {}   # the coordinator's check fails closed: no tool named is a denied tool
    handler = HANDLERS.get(event or payload.get("hook_event_name") or "")
    if not handler:
        return 0
    if (event or payload.get("hook_event_name")) in TOOL_EVENTS and (run_dir := os.environ.get("TTP_RUN_DIR")):
        try:
            mark_hooked(Path(run_dir))
        except OSError:
            pass
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

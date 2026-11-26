# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider hook endpoint: `python -m ttp.hook <event>`, with the hook payload on stdin.

Claude Code calls it after every tool use. Codex (where its hooks are on) sends the same payload
fields and takes the same replies; providers/codex.py passes it per run. If the coordinator has changed the task since the
worker started (it appends to `$TTP_RUN_DIR/steer.md`), the new part is handed to the worker as
added context, once (a chat provider's mini-swe-agent run gets it after a command's output from its command
layer, glmenv.with_update). Workers on providers without hooks get it printed by `ttp note` and `ttp steer`
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
over the same update and hand-off problems.

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
import itertools
import json
import operator
import os
import posixpath
import re
import shlex
import signal
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


def steer_hooked(run_dir: Path, provider: str, spec: dict | None = None) -> bool:
    """Whether this run's update hook hands steer.md over: the run's own marker (HOOKED_FILE; Codex
    and Cursor workers may run the hook, a chat provider's mini-swe-agent run writes it from its
    command layer, glmenv.with_update) or its provider's steer_hook class flag — never the class flag
    for a run with a harness: it inherits it from Claude Code runs, but its agent has no hook."""
    if spec is None:
        try:
            spec = json.loads((run_dir / "run.json").read_text())
        except (OSError, ValueError):
            spec = {}
        spec = spec if isinstance(spec, dict) else {}
    if spec.get("harness"):
        return hooked(run_dir)
    try:
        from .providers import get_provider
        return hooked(run_dir) or bool(getattr(get_provider(provider), "steer_hook", False))
    except Exception:
        return hooked(run_dir)


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
STOP_JOBS_FILE = "stop.jobs"      # the live jobs a done hand-off left unnamed, as last told to the worker
# Set to 1 by the daemon for a task's worker or reviewer run, which must end with a hand-off.
HANDOFF_GUARD_ENV = "TTP_HANDOFF_GUARD"
MAX_STOP_BLOCKS = 2
# Set to 1 by the daemon for a code task's worker run with no branch on record (its project root is not
# a git repository): its hand-off must name the change for the daemon to queue its review.
NO_BRANCH_ENV = "TTP_NO_BRANCH_ON_RECORD"
# The statuses the daemon takes as a hand-off (daemon.HANDOFF_STATES; a test keeps them equal).
HANDOFF_STATES = ("done", "blocked", "failed", "needs_review", "waiting")


def handoff_problem(path: Path, lane: bool | None = None) -> str | None:
    """Why `path` is no hand-off the daemon would take (it reads it with json.loads, as
    daemon._read_result does), None when it is one. A ponytail-lane review (`lane`, default its run's
    ponytail.ENV) hands off done only with a readable lane verdict beside it."""
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
    from . import ponytail
    if data.get("status") == "done" and (ponytail.required() if lane is None else lane) and \
            (why := ponytail.lane_problem(path.parent)) and not why.startswith(ponytail.FAILED):
        return (f"{why}: run the ponytail lane (skill tt-ponytail-review, in a fresh-context subagent) and "
                f"write its verdict to {ponytail.FILE} in the run dir first")
    if why := ponytail.midline_problem(str(data.get("summary") or "")):
        return f"{RESULT_FILE}: {why}"
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


def live_job_warning(run_dir: Path) -> tuple[str | None, Callable[[], None] | None]:
    """Before a `done` hand-off is taken: the live detached jobs of this run it does not name
    (jobstop.names_job), which the daemon stops once it is done, with how to keep or stop each; and a
    callback recording that this set was told, so the same set is told once. (None, None) when none."""
    from . import jobstop
    try:
        data = json.loads((run_dir / RESULT_FILE).read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict) or data.get("status") != "done":
        return None, None
    left = sorted(str(rc) for rc in jobstop._live_jobs(run_dir) if not jobstop.names_job(data, rc))
    try:
        if not left or (run_dir / STOP_JOBS_FILE).read_text() == "\n".join(left):
            return None, None
    except OSError:
        pass
    jobs = "; ".join(f"{Path(rc).stem} ({Path(rc).parent.name}/{Path(rc).stem})" for rc in left)
    return (f"tt-project: your hand-off is `done` while {len(left)} detached job(s) of this run still run and "
            f"no retry_when, on_pass_cmd or next_step names them: {jobs[:800]}. Once a done hand-off is taken "
            f"the daemon stops each such job. To keep one, hand off `waiting` with a retry_when naming "
            f"<run>/<name> (or name it in next_step); to end one, `ttp detach --stop <name> --why ...`. If they "
            f"may be stopped, stop again as you are."), lambda: (run_dir / STOP_JOBS_FILE).write_text("\n".join(left))


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


SSH_ARG_OPTS = set("bBcDEeFIiJLlmOopQRSWw")   # ssh options that take a value


LONG_RUN = 256       # characters in a run _words hands shlex as a short mark
RUN_MARK = "\ue000"  # brackets a mark's number; a command holding it is read as is


def _words(cmd: str, punctuation: str, commenters: str = "#") -> list[str]:
    """shlex's posix words of `cmd`, split at whitespace and at runs of `punctuation`, in time linear in
    its length: shlex grows a word one character at a time (quadratic in the word's length, minutes for a
    2 MB argument), so each run of LONG_RUN or more characters shlex reads alike in every state (none a
    space, quote, backslash, punctuation or comment character) is read as a short mark and put back in
    the words after. Raises ValueError as shlex does (an unclosed quote)."""
    import shlex
    runs: dict[str, str] = {}
    if RUN_MARK not in cmd:
        mark = lambda run: runs.setdefault(run, f"{RUN_MARK}{len(runs)}{RUN_MARK}")
        plain = "[^" + re.escape(" \t\r\n'\"\\" + punctuation + commenters) + "]"
        cmd = re.sub(f"(?<!{plain}){plain}{{{LONG_RUN},}}", lambda m: mark(m[0]), cmd)
        cmd = _mark_quoted(cmd, commenters, mark)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=punctuation)
    lex.whitespace_split = True
    lex.commenters = commenters
    words = list(lex)
    back = {m: run for run, m in runs.items()}
    for i, w in enumerate(words):
        while runs and RUN_MARK in w:   # a quote's mark may hold a mark of its own
            w = re.sub(f"{RUN_MARK}\\d+{RUN_MARK}", lambda m: back[m[0]], w)
        words[i] = w
    return words


def _mark_quoted(cmd: str, commenters: str, mark) -> str:
    """`cmd` with the text of each quote of LONG_RUN or more characters read as mark(text as shlex reads it, a
    backslash dropped only before `"` or a backslash inside double quotes): shlex grows a word one character at a time."""
    out, i, n = [], 0, len(cmd)
    outside = re.compile("[" + re.escape("'\"\\" + commenters) + "]")
    while m := outside.search(cmd, i):
        c, j = m[0], m.start() + 1
        if c == "\\":
            j += 1
        elif c in commenters:
            j = n if (k := cmd.find("\n", j)) < 0 else k
        else:
            inside = re.compile("'" if c == "'" else '["\\\\]')
            while (q := inside.search(cmd, j)) and q[0] == "\\":
                j = q.start() + 2
            if not q:
                break   # an unclosed quote: shlex raises on it
            j = q.end()
            body = cmd[m.end():q.start()]
            if len(body) >= LONG_RUN:
                body = mark(body if c == "'" else re.sub(r'\\([\\"])', r"\1", body))
            out.append(cmd[i:m.end()] + body + c)
            i = j
            continue
        out.append(cmd[i:j])
        i = j
    out.append(cmd[i:])
    return "".join(out)


def ssh_targets(cmd: str) -> list[str]:
    """The hosts an ssh, scp or rsync command in this shell line runs on or copies to or from
    (user@ and ssh:// stripped; local paths and option values skipped)."""
    try:
        words = _words(cmd, ";&|()")
    except ValueError:
        return []
    found, i = [], 0
    while i < len(words):
        prog = posixpath.basename(words[i])
        if prog not in ("ssh", "scp", "rsync") or (i and words[i - 1] in ("echo", "printf", "grep", "which", "man", "type")):
            i += 1
            continue
        j = i + 1
        while j < len(words) and words[j][:1] not in ";&|()":
            w = words[j]
            if w.startswith("-"):
                if prog == "ssh" and len(w) == 2 and w[1] in SSH_ARG_OPTS:
                    j += 1
            elif prog == "ssh":
                found.append(w)
                break
            elif ":" in w and not w.startswith("/"):
                found.append(w.split(":", 1)[0])
            j += 1
        i = j
    out = []
    for h in found:
        h = h.split("://", 1)[-1].rsplit("@", 1)[-1].split(":", 1)[0].split("/", 1)[0].strip("[]")
        if h and h not in out and re.fullmatch(r"[\w.-]+", h):
            out.append(h)
    return out


def record_ssh(payload: dict, run_dir: str) -> None:
    """A Bash call that ran ssh, scp or rsync on a host: the host is a resource the run used
    (onhost), so a failure there counts against it and is never filed as a local fault."""
    if payload.get("tool_name") != "Bash":
        return
    try:
        if hosts := ssh_targets(str((payload.get("tool_input") or {}).get("command") or "")):
            from . import onhost
            onhost.record(run_dir, hosts, "ssh")
    except Exception:
        pass


EXIT_CODE = re.compile(r"(?i)\bexit(?:ed)?(?: with)? (?:code|status) (\d+)")


def _device_plan(payload: dict) -> str:
    """A GLM device run under an Opus plan (deviceplan.py): why this command may not run, '' when it may."""
    from . import deviceplan, sidefx
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cmd = str(args.get("command") or "")
    if payload.get("tool_name") != "Bash":   # a tool reading a device file reads it as `cat` would
        paths = [str(args[k]) for k in sidefx.PATH_KEYS if args.get(k)]
        if payload.get("tool_name") in ("Glob", "LS") or not deviceplan.DEVICE_PATH.search(" ".join(paths)):
            return ""
        cmd = shlex.join(["cat", *paths])
    # a step to take first, a redirect or a hand-back to Opus, as deviceplan.hook_refusal decided it
    return deviceplan.hook_refusal(cmd, str(payload.get("cwd") or "")) or ""


def _gate_stop(payload: dict, run_dir: str) -> str:
    """A failed gate command of a device run on the chat model ends the run as a hand-back to Opus
    (glmguard.gate_stop); what the model reads then, '' when the run goes on. A failed call is one whose
    payload carries an error (PostToolUseFailure) or a non-zero exit code in its response."""
    if payload.get("tool_name") != "Bash":
        return ""
    from . import glmguard
    if glmguard.glm_run() is None:
        return ""
    resp = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    err = str(payload.get("error") or "")
    rc = next((resp[k] for k in ("exit_code", "exitCode", "returncode", "returnCode") if isinstance(resp.get(k), int)),
              None)
    if rc is None and err:
        m = EXIT_CODE.search(err)
        rc = int(m.group(1)) if m else 1
    out = err or "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr"))
    cmd = str((payload.get("tool_input") or {}).get("command") or "")
    from . import deviceplan
    deviceplan.log_step(run_dir, cmd, rc, out)
    why = glmguard.gate_stop(run_dir, cmd, rc, out)
    return f"{why}: the run stops here and the task goes back to Opus with this command and its output" if why else ""


def post_tool_use(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    run_dir = os.environ.get("TTP_RUN_DIR")
    if run_dir:
        mark_untrusted(payload, run_dir)
        record_ssh(payload, run_dir)
        if stopped := _gate_stop(payload, run_dir):
            return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": stopped}}, None
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
    hand-off fails one of the project's hand-off checks, within its send-back cap (handoffcheck.at_stop);
    or, once per set of jobs, a `done` one leaving live detached jobs unnamed (live_job_warning)."""
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
        if why:
            return {"decision": "block", "reason": why}, None
        try:
            why, told = live_job_warning(rd)   # told once per set of unnamed jobs: a second stop is taken
        except Exception:   # never holds the run: the daemon keeps a job the hand-off mentions (jobstop)
            why, told = None, None
        return ({"decision": "block", "reason": why} if why else None), told
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
# Commands whose quoted arguments are only data (printed, searched for or recorded), never run; also
# `git commit` and ttp's message calls (as pathguard._blank_messages decides). Their output may go on only into
# READERS.
INERT = {"echo", "printf", "cat", "grep", "egrep", "fgrep", "rg"}
INERT_RE = re.compile(r"\b(?:echo|printf|cat|e?grep|fgrep|rg|git|ttp)\b")
READERS = INERT - {"echo", "printf"} | {"head", "tail", "sort", "uniq", "wc", "cut", "less", "more"}
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
TOKEN_API_RE = re.compile(r"\b(?:TTP_GH_HELD_\w+|GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)\b")
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
# sudo's and doas's options that take a value (sudo -u root, -p prompt, -g grp; doas -u, -C); its -n, -s and -k
# take none (`sudo -n reboot` runs reboot)
SUDO_VALUE_OPTS = set("ugpChDrRtUTa")
SUDO_LONG_VALUE = {"user", "group", "prompt", "host", "close-from", "chdir", "role", "type", "other-user",
                   "command-timeout", "chroot"}
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


# Per _quoting state (unquoted, in '', in "", in a comment): a run of characters that changes nothing
QUOTING_RUNS = {"": re.compile(r"[^'\"\\#()]+"), "'": re.compile(r"[^']+"), '"': re.compile(r'[^"\\]+'),
                "#": re.compile(r"[^\n]+")}


# A `#` starts a comment where bash starts a new word: after one of these characters, unquoted and
# unescaped (`a\ #b` is one word; a backslash-newline is skipped), redirect operators included (`echo a>#b`
# comments out `#b`). Not after the `)` of a paren that continues a word ($(...), <(...), a=(...), @(...)):
# the `#` joins it. Never inside (( )) or $(( )): bash reads no comment in arithmetic. A known gap:
# `f()#c` and `if(true)#c` are comments in bash, read here as words.
COMMENT_START_AFTER = " \t\n;&|()<>"
WORD_START_AFTER = " \t\n;&|()"   # a `(` here opens a subshell or (( )); anywhere else it continues a word
# A case or esac in command position: a `)` after an open case, back to the innermost `(`, ends a pattern
CASE_RE = re.compile(r"(?:^|[;&|({\n])(?:[ \t]*(?:then|do|else|!)(?=[ \t]))*[ \t]*(case|esac)(?=[ \t\n;&|()<>]|$)")


def _quoting(line: str) -> list[tuple[bool, int]]:
    """Per character of `line`: whether it is quoted (or escaped, or in a comment), and the depth
    of parentheses around it. A run of characters that changes neither is copied in one step, so a long
    word costs no per-character loop."""
    out, q, depth, esc, i = [], "", 0, False, 0
    opens, in_word = [], False   # per open `(`: (index, whether it continues a word); `in_word`: the last `)` closed one
    cases = [(m.start(1), 1 if m[1] == "case" else -1) for m in CASE_RE.finditer(line)] if "case" in line else []
    while i < len(line):
        if not esc and (m := QUOTING_RUNS[q].match(line, i)):
            out.extend([(q != "", depth)] * (m.end() - i))
            i = m.end()
            continue
        c = line[i]
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
        elif c == "#" and ((j := _read_before(line, out, i)) < 0 or (not out[j][0] and line[j] in COMMENT_START_AFTER
                                                                 and not (line[j] == ")" and in_word)
                                                                 and not any(line[k + 1] == "(" for k, _ in opens))):
            q = "#"
        else:
            inert = False
            if c == "(":
                j = _read_before(line, out, i)
                opens.append((i, not (j < 0 or (not out[j][0] and line[j] in WORD_START_AFTER))))
                depth += 1
            elif c == ")":
                start = opens[-1][0] if opens else -1
                if sum(n for k, n in cases if start < k < i and not out[k][0]) > 0:
                    in_word = False   # a case pattern's `)`: it closes no paren
                else:
                    in_word = opens.pop()[1] if opens else False
                    depth = max(0, depth - 1)
        out.append((inert, depth))
        i += 1
    return out


def _read_before(line: str, out: list[tuple[bool, int]], i: int) -> int:
    """Index of the character bash reads before line[i], skipping backslash-newlines (-1: none)."""
    j = i - 1
    while j > 0 and line[j] == "\n" and line[j - 1] == "\\" and out[j][0]:
        j -= 2
    return j


def _comment_starts(line: str, flags: list[tuple[bool, int]]) -> list[int]:
    """Indexes of the `#`s where _quoting starts a comment: inert, while what bash reads before it is not."""
    return [i for i, c in enumerate(line) if c == "#" and flags[i][0]
            and ((j := _read_before(line, flags, i)) < 0 or not flags[j][0])]


# A character that can start a quoted, escaped or comment run (_quoting)
QUOTE_START_RE = re.compile(r"['\"\\\\#]")


def _quoted_runs(text: str) -> list[tuple[int, int, bool]]:
    """(start, end, quoted) of each run of `text` that is all quoted or all unquoted (_quoting), found
    without a per-character Python loop."""
    if not QUOTE_START_RE.search(text):
        return [(0, len(text), False)] if text else []
    out, i = [], 0
    for quoted, run in itertools.groupby(_quoting(text), key=operator.itemgetter(0)):
        n = len(list(run))
        out.append((i, i + n, quoted))
        i += n
    return out


SEPARATOR_RE = re.compile(r"[;&|\n]")


def _pieces(line: str) -> list[tuple[int, int, str]]:
    """The top-level simple commands of `line` (a line break outside quotes ends one too): (start, end, the
    separator after it)."""
    flags, out, start, i = _quoting(line), [], 0, 0
    while m := SEPARATOR_RE.search(line, i):   # only a separator character can end a command
        i = m.start()
        c = line[i]
        if not flags[i][0] and flags[i][1] == 0 and c in ";&|\n" and line[i - 1:i] not in ("<", ">") \
                and line[i + 1:i + 2] != ">":
            sep = line[i:i + 2] if line[i + 1:i + 2] in (c, "&") and c not in ";\n" else c
            out.append((start, i, sep))
            i = start = i + len(sep)
            continue
        i += 1
    out.append((start, len(line), ""))
    return out


def _piece_at(line: str, pos: int) -> tuple[str, str]:
    """The top-level simple command around `pos` in `line`, and the separator after it."""
    return next((line[s:e], sep) for s, e, sep in _pieces(line) if s <= pos <= e)


@functools.lru_cache(maxsize=64)
def _first_argv(piece: str) -> tuple[str, ...]:
    """The first simple command of `piece` (_segments), cached: tokenizing a long quoted word is slow."""
    return tuple(next(iter(_segments(piece)), []))


def _first_word(piece: str) -> str:
    argv = _first_argv(piece)
    return os.path.basename(argv[0]) if argv else ""


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


def _chain(line: str, pieces: list[tuple[int, int, str]], k: int) -> list[str]:
    """The commands `pieces[k]` of `line` pipes its output into (`|` or `|&`, never `||`)."""
    out = []
    while pieces[k][2] in ("|", "|&") and k + 1 < len(pieces):
        k += 1
        out.append(line[pieces[k][0]:pieces[k][1]])
    return out


def _runs_inside(piece: str) -> bool:
    """`piece` runs a command of its own: a `$(...)` or backtick outside single quotes, `<(`/`>(`, or `rg --pre`."""
    from . import pathguard
    return bool(pathguard._substitutions(piece) or re.search(r"[<>]\(", re.sub(r"'[^']*'", "", piece))
                or re.search(r"(?:^|\s)--pre(?:=|\s|$)", piece) and _first_word(piece) == "rg")


@functools.lru_cache(maxsize=64)
def _unquoted(text: str) -> str:
    """`text` with its quoted (and escaped, and comment) characters as spaces."""
    return "".join(" " * (e - b) if q else text[b:e] for b, e, q in _quoted_runs(text))


def _data_only(piece: str, sep: str, commands: str, after: list[str] | tuple = ()) -> bool:
    """`piece` only prints, searches for or records text (INERT, `git commit`, ttp's message calls), runs nothing
    inside, pipes on (`after`) only into READERS, and writes no file that `commands` runs afterwards. Fail closed:
    a variable set before it (`GIT_EDITOR=...`), or a PATH, alias or function `commands` sets, may make it run."""
    from . import pathguard
    argv = list(_first_argv(piece))
    name = _first_word(piece)
    if not (name in INERT or name == "git" and argv[1:2] == ["commit"] or name == "ttp"
            and pathguard._message_pieces(piece + sep) and pathguard._blank_messages(commands) != commands) \
            or _runs_inside(piece) or sep in ("|", "|&") and not (
                after and all(_first_word(p) in READERS and not _runs_inside(p) for p in after)):
        return False
    unquoted = _unquoted(commands)
    if re.match(r"\s*[A-Za-z_]\w*\+?=", piece) or any(w in unquoted for w in ("PATH", "alias", "function", "(")) and re.search(
            rf"\bPATH\+?=|\balias\b|\bfunction\s+{re.escape(name)}\b|(?<![\w-]){re.escape(name)}\s*\(\s*\)", unquoted):
        return False
    return not any(os.path.basename(m[2]) in _ran(commands) for m in REDIRECT_RE.finditer(piece))


def _data_body(lines: list[str], i: int, pos: int) -> bool:
    """The heredoc at `pos` of `lines[i]` is only data: a quoted delimiter (no expansion) on a _data_only piece
    (`cat > t.md <<'EOF'`, `git commit -F - <<'EOF'`)."""
    line = lines[i]
    pieces = _pieces(line)
    k = next(k for k, (s, e, _) in enumerate(pieces) if s <= pos <= e)
    return bool(HEREDOC_RE.match(line, pos)[2]) and _data_only(line[pieces[k][0]:pieces[k][1]], pieces[k][2],
                                                                "\n".join(lines), _chain(line, pieces, k))


def _into_shell(line: str, pieces: list[tuple[int, int, str]], k: int) -> bool:
    """`pieces[k]` of `line` pipes into a shell (`echo 'tt-smi -r 0' | bash`): what it prints runs."""
    return pieces[k][2] in ("|", "|&") and k + 1 < len(pieces) \
        and _first_word(line[pieces[k + 1][0]:pieces[k + 1][1]]) in SHELLS


def _fed_shell_texts(text: str, pieces: list, is_shell) -> list[tuple[int, str, str]]:
    """(piece index, the text it prints, the piece it pipes into) for each echo or printf piece of
    `text` (split into `pieces`) that pipes into a piece `is_shell` accepts: what it prints runs as
    commands, into a local shell (`echo cmd | bash`) or one fed over ssh."""
    out = []
    for k, (s, e, _) in enumerate(pieces):
        if _first_word(text[s:e]) not in ("echo", "printf"):
            continue
        for nxt in _chain(text, pieces, k):
            if is_shell(nxt):   # every shell piece in the chain it pipes through, as deviceplan read it
                argv = next(iter(_segments(text[s:e])), [])
                out.append((k, " ".join(w for w in argv[1:] if not w.startswith("-"))
                                    .replace("\\n", "\n"), nxt))
    return out


def _blank(piece: str) -> str:
    """`piece` with each quoted span holding whitespace (text, not a word) replaced by `''`; a one-word span (a
    path, a flag) and a redirection's target stay."""
    out = []
    for j, k, quoted in _quoted_runs(piece):
        span = piece[j:k]
        text = quoted and re.search(r"\s", span) and not piece[:j].rstrip(" \t").endswith(("<", ">"))
        out.append("''" if text else span)
    return "".join(out)


class _Body(tuple):
    """(a heredoc's line index, its body lines, the `<<` column), unpacked as such, and `.end`: the line that
    closes it as written (tabs included), None while it is unended."""
    end: str | None = None


def _heredocs(text: str) -> tuple[list[str], list[tuple[int, list[str], int]]]:
    """(the lines that are not heredoc bodies, each body as (its line's index, its lines, the `<<` column))."""
    lines, bodies, pending = [], [], []
    for line in text.replace("\\\n", " ").split("\n"):
        if pending:
            delim, dash, k = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
                bodies[k].end = line
            else:
                bodies[k][1].append(line)
            continue
        lines.append(line)
        flags = _quoting(line) if "<<" in line else []
        for m in HEREDOC_RE.finditer(line):
            if not flags[m.start()][0]:
                pending.append((m[3], m[1] == "-", len(bodies)))
                bodies.append(_Body((len(lines) - 1, [], m.start())))
    return lines, bodies


def _heredoc_readers(text: str):
    """(argv, the body's lines) for each heredoc body of `text` and each command that reads it: the command
    the heredoc is on and the ones it pipes into (`cat <<EOF | ssh box bash` is read by cat, ssh)."""
    lines, bodies = _heredocs(text)
    for i, body, pos in bodies:
        ps = _pieces(lines[i])
        k = next(k for k, (s, e, _) in enumerate(ps) if s <= pos <= e)
        for piece in (lines[i][ps[k][0]:ps[k][1]], *_chain(lines[i], ps, k)):
            if argv := list(_first_argv(piece)):
                yield argv, body


def _split(text: str, strip: bool = True) -> tuple[str, str]:
    """(what runs as shell commands, what to scan) for a command line or a script. Heredoc bodies
    are commands only when a shell reads them; bodies only data (_data_only) are dropped from both,
    and so are the quoted text arguments of _data_only commands (a commit message, a grep pattern,
    an echo naming a command runs none). What echo or printf pipes into a shell runs.
    With `strip` False those arguments stay in what runs, so a quote spanning lines stays whole."""
    lines, bodies = _heredocs(text)
    commands = "\n".join(lines)
    pieces = _pieces(commands) if INERT_RE.search(commands) else []
    stripped, fed = commands, []
    feeds = {k: text for k, text, _ in _fed_shell_texts(commands, pieces, lambda p: _first_word(p) in SHELLS)}
    for k in reversed(range(len(pieces))):
        s, e, sep = pieces[k]
        piece = commands[s:e]
        if _data_only(piece, sep, commands, _chain(commands, pieces, k)):
            stripped = stripped[:s] + _blank(piece) + stripped[e:]
        elif k in feeds:
            fed.append(feeds[k])
    scan = [stripped]
    run = [stripped if strip else commands] + fed
    for i, body, pos in bodies:
        line = lines[i]
        ps = _pieces(line)
        k = next(k for k, (s, e, _) in enumerate(ps) if s <= pos <= e)
        piece, sep = line[ps[k][0]:ps[k][1]], ps[k][2]
        if _first_word(piece) in SHELLS or _into_shell(line, ps, k):
            run += body
        if not _data_only(piece, sep, commands, _chain(line, ps, k)):
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


def _segments(cmd: str, strip: bool = True, commenters: str = "#") -> list[list[str]]:
    """The simple commands in `cmd`, each without its leading variable assignments and wrappers
    (env, nohup, timeout 60, `ttp lock res --` ...): what actually runs first in each. `strip`
    False keeps them; `commenters` "" reads a `#` as part of its word."""
    # A line break ends a command; it stays a line break too, so a `#` comment (a script's #! line) ends there
    flags = _quoting(cmd)
    cmd = re.sub("\n", lambda m: "\n" if flags[m.start()][0] else ";\n;", cmd)
    try:
        words = _words(cmd, ";&|()<>", commenters)
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


def _sudo_opts(args: list[str]) -> int:
    """How many words of `args` (what follows sudo or doas) are its own options."""
    i = 0
    while i < len(args) and args[i].startswith("-") and len(args[i]) > 1:
        a = args[i]
        if a == "--":
            return i + 1
        if a.startswith("--"):
            i += a[2:] in SUDO_LONG_VALUE
        else:
            for j, ch in enumerate(a[1:], 2):
                if ch in SUDO_VALUE_OPTS:
                    i += not a[j:]
                    break
        i += 1
    return i


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
            if not w.startswith("-"):
                xargs = os.path.basename(w) == "xargs"
            i += 1
            if os.path.basename(w) == "sudo":
                i += _sudo_opts(argv[i:])
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
    from_stdin, skip = False, False
    for a in rest:
        if skip:     # bash -o errexit, bash -O extglob: an option's value, not the script
            skip = False
        elif a in inline:
            return ""
        elif a == "-":
            return "-"
        elif shell and a == "-s":   # bash -s x reads its script from stdin; x is $1, not a script
            from_stdin = True
        elif shell and a in ("-o", "+o", "-O", "+O"):
            skip = True
        elif not a.startswith(("-", "+")):
            return "-" if from_stdin else a
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


# The project's web app token (state/web.token, or under state.noindex on macOS), named in a command
WEB_TOKEN_RE = re.compile(r"state(?:\.noindex)?/+web\.token\b")
# The per-user secrets store by path, and the runtime calls that hand back its values: a tool call's
# output stays in the run's log and its agent's transcript, so a run tests a key with
# `ttp secret check` and gives one to a command with `ttp secret env` (also `--secret`) instead.
SECRET_FILE_RE = re.compile(r"(?:\.tt-project|TTP_HOME\}?)/+secrets\.json\b")
SECRET_CALL_RE = re.compile(r"(?<!def )\b(?:credentials|load_secrets|env_for)\s*\(|\bendpoint\.saved\s*\(|\bsecrets_path\s*\(|\bsecretstore\.(?:load|BACKENDS|_\w+_load)\b")


def store_refusal(cmd: str, cwd: str) -> str | None:
    """Why a Bash command naming a credential store is refused, else None: the web token's path, the secrets
    store (secret_read) and the credential folders (credguard). The guards' one reading of the words of a
    command, which pyproof asks of each path a snippet opens."""
    from . import credguard
    commands, _ = _split(cmd)
    if WEB_TOKEN_RE.search(cmd):
        # Whoever holds it acts as the user in the web app; the Read deny rule covers the file tools.
        return ("the web token is the user's credential and a run must not read it; any command naming its "
                "path is refused, so write a commit message with `git commit -F <file>` or call it 'the web token file'")
    return secret_read(cmd, _segments(commands), cwd) or credguard.bash_refusal(cmd, _all_segments(commands), cwd=cwd)


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


# Tools of Claude Code's full standard set that would step outside
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


def _scratch_roots() -> list[str]:
    """Where a review may write: its run directory and the temporary folders."""
    import tempfile
    return [os.environ.get("TTP_RUN_DIR", ""), tempfile.gettempdir(), "/tmp"]


def reviewer_write(payload: dict) -> str | None:
    """Why a review's file-tool write is refused: a review never edits the change it judges, so its Edit,
    Write and NotebookEdit calls (and each file a Codex apply_patch names) write only in its run
    directory or a temporary folder, or in a repository with a merge or rebase in progress (resolving
    its conflict is the one edit a review makes)."""
    tool = str(payload.get("tool_name") or "")
    if os.environ.get("TTP_ROLE") != "reviewer" or tool not in EDIT_TOOLS | PATCH_TOOLS:
        return None
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    names = patch_files(payload) if tool in PATCH_TOOLS else [str(args.get("file_path") or args.get("notebook_path") or "")]
    roots = _scratch_roots()
    cwd = str(payload.get("cwd") or os.getcwd())
    for name in filter(None, names):
        path = os.path.expanduser(name)
        real = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))
        if any(r and (real + os.sep).startswith(os.path.realpath(r).rstrip(os.sep) + os.sep) for r in roots):
            continue
        if not _git_op_in_progress(real):
            break
    else:
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


def _deny(why: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"tt-project: {why}."}}


# The call being judged, named in every refusal record (_log_refusal): its id (the provider's tool call
# id) and stage: "before" from the hook (the call has not run), "started" from the runner's stream guard.
CALL = {"id": "", "stage": "before"}


# The refusal the current pre_tool_use call logs: whether a guard logged it itself (full_suite, no_device,
# user_site, a local guard), and else its kind, `provenance` for a guard on untrusted input, `worker_role`
# for the others. localfault.GUARD_KINDS counts these two as the run's own fault, not its resource's.
_refusal = {"logged": False, "kind": "worker_role"}


def _provenance(why: str | None) -> str | None:
    """`why` of a guard on where the run's input came from (untrusted): its refusal is logged as provenance."""
    if why:
        _refusal["kind"] = "provenance"
    return why


def _refusal_command(payload: dict) -> str:
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool = str(payload.get("tool_name") or "")
    return str(args.get("command") or "") if tool == "Bash" else f"{tool} {json.dumps(args, default=str)[:400]}"


def _proven(payload: dict) -> dict:
    """The Bash call the guards judge: its literal loops and once-set variables written out (glmguard.literal_view),
    then each inline Python snippet pyproof proves harmless written as `pass` (the guards read words, and such a
    snippet's words do nothing); the call as is otherwise."""
    args = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(args, dict) or not isinstance(args.get("command"), str):
        return payload
    from . import glmguard, pyproof
    cwd = str(payload.get("cwd") or os.getcwd())
    try:   # loops over literal words or globs, and variables set once to a literal, read as their values
        cmd = glmguard.literal_view(args["command"], cwd) or args["command"]
    except Exception:
        cmd = args["command"]
    try:
        new = pyproof.rewrite(cmd, cwd)
    except Exception:   # no proof: the call as written
        new = None
    new = new or (cmd if cmd != args["command"] else None)
    return {**payload, "tool_input": {**args, "command": new}} if new else payload


def _staged(payload: dict, judged: dict) -> dict | None:
    """The hook's reply for a Bash call of a GLM run the guards refuse only for an expansion they cannot read: each
    `$(...)` the rules pass runs alone first and its output is written in (glmguard.staged_parts, written_in); the
    literal command is judged by every rule and, if it passes, runs as judged; a harmful one is refused
    with its reason. None when it cannot be staged: the up-front refusal stands."""
    from . import glmguard
    args, cmd = payload.get("tool_input"), (judged.get("tool_input") or {}).get("command")
    cwd = str(payload.get("cwd") or os.getcwd())
    if glmguard.glm_run() is None or not isinstance(args, dict) or not isinstance(cmd, str) or glmguard.HIDDEN_WHY not in (
            glmguard.shell_refusal(cmd, cwd, os.environ.get("TTP_RUN_DIR", "")) or ""):
        return None
    if (parts := glmguard.staged_parts(args["command"])) is not None:   # as written: a loop runs it once
        cmd = args["command"]
    elif (parts := glmguard.staged_parts(cmd)) is None:   # else with a literal loop written out (_proven)
        return None
    outputs, left, run_dir = [], 10.0, os.environ.get("TTP_RUN_DIR", "")
    for *_, body, _, there in parts:   # judged and run with the literal `cd` before it, as one command
        body = f"cd {shlex.quote(there)} && {{ {body}\n}}" if there else body
        out, _ = _pre_tool_use(_proven({**payload, "tool_input": {**args, "command": body}}))
        if ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
            return out
        start = time.monotonic()   # one budget well under the hook's own limit; the whole group ends with it
        try:
            # own session: the group is killed whole (killpg) when the 10 s budget runs out
            proc = subprocess.Popen(["bash", "-c", body], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        except OSError:
            return None
        try:
            outputs.append(proc.communicate(timeout=left)[0])
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return _deny(glmguard.refused(run_dir, "an expansion the guard must run first took over 10 s"))
        left -= time.monotonic() - start
    if (new := glmguard.written_in(cmd, parts, outputs)) == cmd:
        return None
    if why := glmguard.secret_in("\n".join([*outputs, new])):
        return _deny(glmguard.refused(run_dir, f"{why}, which it may print"))
    staged = _proven({**payload, "tool_input": {**args, "command": new}})
    if glmguard.HIDDEN_WHY in (glmguard.shell_refusal(staged["tool_input"]["command"], cwd, run_dir) or ""):
        return None   # still unreadable once expanded: the up-front refusal stands, recorded once
    out, _ = _pre_tool_use(staged)
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("permissionDecision") == "deny" or spec.get("updatedInput"):
        return out
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": staged["tool_input"]}}


def pre_tool_use(payload: dict, stage: str = "before") -> tuple[dict | None, None]:
    """The tool hook's verdict on one call; a refusal no guard logged itself is logged to the run's
    refusals.jsonl as worker_role or provenance, so localfault counts it apart from the resource."""
    if not os.environ.get("TTP_RUN_DIR"):
        return None, None
    CALL.update(id=str(payload.get("tool_use_id") or payload.get("call_id") or payload.get("id") or ""), stage=stage)
    _refusal.update(logged=False, kind="worker_role")
    judged = _proven(payload)
    out = _staged(payload, judged) if payload.get("tool_name") == "Bash" else None
    out, _ = (out, None) if out else _pre_tool_use(judged)
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("permissionDecision") == "deny" and not _refusal["logged"]:
        _log_refusal(_refusal_command(payload), kind=_refusal["kind"],
                     reason=str(spec.get("permissionDecisionReason") or ""))
    return out, None


def _pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    from . import glmguard, pathguard, untrusted
    if glmguard.glm_run() is not None and (d := glmguard.outward(payload)):   # a draft for Opus, the run goes on
        return _deny(glmguard.drafted(os.environ.get("TTP_RUN_DIR"), d)), None
    if glmguard.glm_run() is not None and (why := glmguard.tool_refusal(payload)):   # every route to the chat model
        # a redirect (the reason and the allowed way; the run goes on) or a hand-back to Opus (no model call)
        return _deny(glmguard.refused(os.environ.get("TTP_RUN_DIR"), why)), None
    if why := _device_plan(payload):
        return _deny(why), None
    if why := _provenance(untrusted.mark_refusal(str(payload.get("tool_name") or ""), payload.get("tool_input") or {})):
        return _deny(why), None   # the run's untrusted-input mark, marked or not
    if why := pathguard.refusal(payload):   # a write into a credential folder or a protected path, on any tool
        from . import ruleguard
        ruleguard.fire("protected_paths", why)
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
            why = _provenance(untrusted.tool_refusal(str(payload["tool_name"]), args, cwd))
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
    if not why and (HARNESS_GH_RE.search(cmd) or HARNESS_BIN_RE.search(cmd) and GH_D_RE.search(cmd)) \
            and not _harness_task():
        why = HARNESS_GH_WHY
    if not why and "***" in cmd:   # a patch in the command (`apply_patch <<EOF`), relative paths too
        why = harness_gh_patch(payload)
    if not why:
        why = store_refusal(cmd, str(payload.get("cwd") or os.getcwd()))
    if not why:   # the run's own credentials and proxy stay in the run (runguard.py)
        from . import runguard
        why = runguard.refusal(cmd, _all_segments(commands) + _segments(commands, strip=False))
    if not why:   # a run reading untrusted input: no foreign push, write or credential read
        from . import untrusted
        why = _provenance(untrusted.bash_refusal(_all_segments(commands), _segments(commands, strip=False),
                                                 str(payload.get("cwd") or os.getcwd())))
    if not why:
        why = git_push_bypass(segments)
    if not why:   # the project's own rules: Slurm nodes, landing pushes, user-ordered undos (ruleguard.py)
        from . import aitag, ruleguard
        why = ruleguard.bash_refusal(cmd, _all_segments(commands), aitag.project_config(),
                                     str(payload.get("cwd") or os.getcwd()))
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
    if not why:   # a PR body's form, and gh writes where the first gh is not the wrapper (ghhold.py)
        why = gh_form(segments, str(payload.get("cwd") or os.getcwd()))
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
        why = held_machine(cmd)
    if not why:
        why = local_guard(payload)
    if not why:
        from . import fgwait
        cwd = str(payload.get("cwd") or os.getcwd())
        hint = "\n\n".join(h for h in (wide_read(commands, cwd),
                                        fgwait.reminder(cmd)) if h)
        out = {"hookEventName": "PreToolUse", **({"additionalContext": hint} if hint else {})}
        return ({"hookSpecificOutput": out} if len(out) > 1 else None), None
    return _deny(why), None


def gh_form(segments: list[list[str]], cwd: str) -> str | None:
    """Why a gh command in `segments` must not run, checked before it runs on every provider whose tool
    hook runs, so a gh reached around the wrapper is held to the same rule: a PR body's form
    (prguard.form_problem, the wrapper's own rule), and, in a run whose first gh was not the wrapper,
    any gh write (ghhold.marked_refusal)."""
    from . import ghhold, prguard
    if why := ghhold.marked_refusal(segments):
        return why
    for s in segments:
        if s and os.path.basename(s[0]) == "gh":
            try:
                why = prguard.form_problem(s[1:], cwd)
            except Exception:   # the wrapper checks it again when it runs
                why = None
            if why:
                return why
    return None


def held_machine(cmd: str) -> str | None:
    """Why a command that reaches a machine held machine-wide or under a known outage may not
    run (heldwork.ssh_refusal): no ssh, scp or rsync to a machine the user took out of service."""
    from . import heldwork
    return heldwork.ssh_refusal(cmd)


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
    one outside `ttp clip` / `ttp sandbox` / a local `ttp detach` (testslots.detach_argv), which take them from
    the host's test pool (testslots.direct) unless the run already holds slots; else None."""
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
        return (f"pytest -n {bare[0]} outside `ttp clip`, `ttp sandbox` or a local `ttp detach` holds none of "
                f"this shared host's test slots. Run it as {how}, as a local `ttp detach` job, or with no -n")
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
    _refusal["logged"] = True
    try:
        with open(Path(os.environ["TTP_RUN_DIR"]) / REFUSALS_FILE, "a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "id": CALL["id"], "stage": CALL["stage"],
                                **{k: v[:500] for k, v in extra.items()}, "command": cmd[:500]}) + "\n")
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


def post_tool_use_failure(payload: dict) -> tuple[dict | None, Callable[[], None] | None]:
    """A failed tool call: any coordinator update or hand-off problem PostToolUse would have delivered
    (Claude Code sends no PostToolUse for a failed call)."""
    out, delivered = post_tool_use(payload)
    parts = [out["hookSpecificOutput"]["additionalContext"]] if out else []
    if not parts:
        return None, delivered
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure",
                                   "additionalContext": "\n\n".join(parts)}}, delivered


def session_start(payload: dict):
    """After the agent compacted a task run's context (SessionStart, source "compact"), its role and task
    again (rolerestore.py), so a summary written under a copied coordinator session does not make the
    run act as the coordinator."""
    run_dir = os.environ.get("TTP_RUN_DIR")
    if payload.get("source") != "compact" or not run_dir:
        return None, None
    from . import rolerestore
    return rolerestore.hook_output(Path(run_dir)), None


HANDLERS = {"PostToolUse": post_tool_use, "PostToolUseFailure": post_tool_use_failure,
            "PreToolUse": pre_tool_use, "SessionStart": session_start, "Stop": stop}


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

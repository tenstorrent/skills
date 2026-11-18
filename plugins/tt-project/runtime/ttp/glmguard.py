# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free guard on the side effects of a run on the code arm's GLM side.

A GLM-arm run (codearm.py) builds what its spec decided and hands the result back; it does not talk to a
person or another project, write the project's records or touch anything outside its worktree. A prompt
rule cannot hold that for a cheaper model, so the harness enforces it in code, with no model call, on
every harness the arm runs:

- `ttp` (cli.glm_gate) runs only the commands in SUBS_ALLOWED: checks, push, reads of its own task and
  the lock and detach it needs. `note`, `say`, `ask`, `notify`, `upstream`, task, obligation, memory and
  config writes, and every other command are refused.
- The hook (hook.pre_tool_use, Claude Code and any provider with hooks) and the command layer of a harness
  without hooks (glmenv, for mini-swe-agent) ask tool_refusal() before each call: a `ttp` command the
  list does not allow, a raw `git push`, a `gh` write, an HTTP post, ssh or mail, a database or state
  write, a command that edits the guard's own environment, a file write outside the worktree and run
  folder, and every connector tool.

A refused attempt ends the run as a hand-back to Opus: hit() writes the run's glm_guard.json and a
`needs_opus` result.json (reason `guard: <what>`), the runner stops the agent at its next look and the
daemon requeues the task on Opus without spending an attempt (codearm.bounce_reason). The guard applies to
runs where active() holds: the daemon sets TTP_CODE_ARM_GUARD for a run whose arm is glm and writes MARK in
its run folder, so a run that unsets the variable is still guarded. Other runs are not touched."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ENV = "TTP_CODE_ARM_GUARD"
MARK = "glm_guard.on"        # in the run folder: the daemon wrote it before launch
HIT = "glm_guard.json"       # in the run folder: {"why", "ts"}; the runner stops the agent when it exists
KEPT = "result.pre-guard.json"   # a hand-off the run wrote before the guard replaced it

# What a GLM-arm run may run: its checks and push, reads of its own task, and the lock and detach commands
# a build or test needs. Everything else changes state or reaches someone outside the hand-off.
SUBS_ALLOWED = frozenset({
    "push", "checks", "checks-shard", "checks-yield", "pool-wait", "pool-admit", "pool-shard", "install-check",
    "clip", "hours", "alive", "landed", "lock", "detach", "eta", "list", "find", "overview", "said", "runs", "task",
    "paths"})
TASK_ACTIONS = frozenset({"show", "list"})
GH_READS = frozenset({"view", "list", "diff", "status", "checks"})
REACH = frozenset({"ssh", "scp", "sftp", "mail", "mailx", "sendmail", "mutt", "nc", "ncat", "telnet", "sqlite3"})
HTTP = frozenset({"curl", "wget", "http", "https", "httpie"})
HTTP_DATA = re.compile(r"(?:^|\s)(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s+(?:POST|PUT|PATCH|DELETE)|-d\b|--data\S*|"
                       r"-F\b|--form\S*|-T\b|--upload-file|--post-\S+)", re.I)
# A command that edits what identifies the run: the guard's own variable, or the run's identity. Only where a shell
# reads an assignment (the start of a command, after a wrapper such as sudo or env, or a -c or eval script), so
# `echo TTP_RUN_DIR=$TTP_RUN_DIR` is a read, while `TTP_RUN_DIR=x cmd`, `export`, `env`, `unset`, `read` and
# `printf -v` of one are edits.
_IDS = r"(?:TTP_CODE_ARM_GUARD|TTP_RUN_DIR|TTP_TASK|TTP_PROJECT|TTP_RUN_ID)"
_AT = (r"(?:^|[;&|({}\n`!]|\$\(|\b(?:then|do|else|elif|if|while|until)\s|(?:-c|eval)\s+['\"]?)\s*"
       r"(?:(?:sudo|exec|command|nohup|time|nice|xargs|builtin|doas)\s+(?:-\S+\s+)*)*")
TAMPER = re.compile(
    _AT + r"(?:(?:unset|export|declare|typeset|local|readonly|env|set|read|mapfile|readarray|for)\b[^;|&\n]*\b"
    + _IDS + r"\b|printf\s+-v\s*" + _IDS + r"\b|(?:[A-Za-z_]\w*=\S*\s+)*" + _IDS + r"\+?=)"
    r"|\benv\s+-i\b|\bglm_guard\.(?:on|json)\b", re.M)
# A shell redirection (`2>&1`, `>out.log`, `<in`): its fd digit and target are no words of a ttp call.
REDIRECT = re.compile(r"(?<![\w$])\d*(?:&>>?|>>?|<<?<?|>&|<&)\s*(?:&?\d+-?|[^\s;|&<>()]+)?")
STATE_DB = re.compile(r"\bproject\.db\b|\bmachine-workers\.json\b|\b\.tt-project/(?:locks|machines)\b")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "ApplyPatch"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS", "TodoWrite", "Task", "WebSearch", "WebFetch"}


def active(env=None) -> bool:
    """Whether this process is a run on the GLM arm: the variable, or the marker in its run folder."""
    e = os.environ if env is None else env
    if e.get(ENV) == "1":
        return True
    rd = e.get("TTP_RUN_DIR") or ""
    return bool(rd) and (Path(rd) / MARK).exists()


def env_for(note: dict | None) -> dict[str, str]:
    """The run environment's flag for a run whose arm is glm (empty otherwise): `note` is the run note."""
    from . import codearm
    arm = (note or {}).get(codearm.KEY)
    return {ENV: "1"} if isinstance(arm, dict) and arm.get("run") == "glm" else {}


def mark(run_dir) -> None:
    """The daemon's marker for a run on the arm (read by active() where the variable is gone)."""
    from .project import durable_write
    durable_write(Path(run_dir) / MARK, "1")


def hit(run_dir, why: str) -> None:
    """End the run as a hand-back to Opus: record the refusal and replace its hand-off with a `needs_opus`
    one (a hand-off it already wrote is kept beside it). The runner stops the agent when HIT exists;
    the daemon requeues the task on Opus. Never raises: a guard that cannot write still refuses."""
    if not run_dir:
        return
    d = Path(run_dir)
    why = why.strip()[:280]
    try:
        from .project import durable_write
        res = d / "result.json"
        if res.exists() and not (d / KEPT).exists():
            try:
                durable_write(d / KEPT, res.read_bytes())
            except OSError:
                pass
        durable_write(res, json.dumps({"status": "blocked", "needs_opus": f"guard: {why}",
                                       "summary": f"needs Opus: guard: {why}"}))
        durable_write(d / HIT, json.dumps({"why": why, "ts": time.time()}))
    except OSError:
        pass


def hit_reason(run_dir) -> str:
    """The reason the guard ended the run in `run_dir`, '' when it did not."""
    try:
        got = json.loads((Path(run_dir) / HIT).read_text())
    except (OSError, ValueError, TypeError):
        return ""
    return str(got.get("why") or "guard") if isinstance(got, dict) else "guard"


def cli_refusal(cmd: str, action: str = "") -> str | None:
    """Why a GLM-arm run may not run `ttp <cmd> [<action>]`, else None."""
    if cmd in SUBS_ALLOWED and (cmd != "task" or action in TASK_ACTIONS):
        return None
    what = f"ttp {cmd}" + (f" {action}" if cmd == "task" and action else "")
    return (f"`{what}` is not allowed to a run on the GLM arm (it changes records or reaches a person or another "
            f"project outside the hand-off); hand the task back to Opus")


def _ttp_args(seg: list[str], project: str) -> list[str] | None:
    """The words after `ttp` in a command segment (an optional leading project name dropped), None for a
    segment that is not a ttp call."""
    if not seg:
        return None
    name = os.path.basename(seg[0])
    args = seg[1:] if name == "ttp" else seg[seg.index("-m") + 2:] if "-m" in seg[:-1] and \
        seg[seg.index("-m") + 1] in ("ttp", "ttp.cli") else None
    if args is None:
        return None
    words = [w for w in args if not w.startswith("-")]
    if len(words) > 1 and words[0] not in SUBS_ALLOWED and project and words[0] == project:
        words = words[1:]   # `ttp <this project> <command>`: any other project's name is refused as a command
    return words


def _own_name(env) -> str:
    """This run's project name (the first word of `ttp <project> <command>`), '' when it cannot be read."""
    base = str(env.get("TTP_PROJECT") or "")
    if not base:
        return ""
    try:
        from .project import Project
        return str(Project(base).name or "")
    except Exception:
        return os.path.basename(base)


def _git_word(args: list[str]) -> str:
    """The git subcommand of `args` (what follows `git`), its global options and their values skipped."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and "=" not in a:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a
    return ""


def _roots(cwd: str, run_dir: str) -> list[str]:
    out = []
    for x in (cwd, run_dir):
        if x:
            out += [os.path.normpath(x), os.path.realpath(x)]
    return out


def shell_refusal(cmd: str, cwd: str, run_dir: str = "", env=None) -> str | None:
    """Why a shell command of a GLM-arm run is refused (before it runs), else None."""
    from . import hook, sidefx
    e = os.environ if env is None else env
    if TAMPER.search(cmd):
        return "a command may not edit the run's identity or the guard's own files"
    if STATE_DB.search(cmd):
        return "a run on the GLM arm may not read or write the project's database or machine records by hand"
    commands, _ = hook._split(cmd)
    segs = hook._all_segments(commands) + hook._segments(commands, strip=False)
    project = _own_name(e)
    plain, _ = hook._split(REDIRECT.sub(" ", cmd))   # `ttp checks 2>&1`: the 2 is no word of the call
    for seg in hook._all_segments(plain) + hook._segments(plain, strip=False):
        words = _ttp_args(seg, project)
        if words and (why := cli_refusal(words[0], words[1] if len(words) > 1 else "")):
            return why
    for seg in segs:
        if not seg:
            continue
        name = os.path.basename(seg[0])
        words = _ttp_args(seg, project)
        if words is not None:
            continue   # read above, without its redirections
        elif name == "git" and _git_word(seg[1:]) == "push":
            return "a run on the GLM arm publishes only with `ttp push` (after its checks), never `git push`"
        elif name == "gh":
            args = [w for w in seg[1:] if not w.startswith("-")]
            if not (args[1:2] and args[1] in GH_READS) and not (args[:1] == ["api"] and not hook._gh_write(seg)
                                                                 and not HTTP_DATA.search(" ".join(seg[1:]))):
                return "a run on the GLM arm does not post to GitHub (comments, reviews, PRs, issues)"
        elif name in HTTP and HTTP_DATA.search(" ".join(seg[1:])):
            return "a run on the GLM arm does not send data to a service (an HTTP post)"
        elif name in REACH:
            return f"`{name}` reaches another host, person or database: not for a run on the GLM arm"
    home = os.path.expanduser("~")
    roots = _roots(cwd, run_dir)
    for path, (cls, why) in sidefx.classify_paths(cmd, home, cwd or os.getcwd(), run_dir).items():
        if cls == "write" and not sidefx._inside(path, roots):
            return f"it writes outside its worktree: {path} ({why})"
    return None


def tool_refusal(payload: dict, env=None) -> str | None:
    """Why a hook payload (the tool name, its input, the cwd) of a GLM-arm run is refused, else None: the
    Bash command checks above, a file write outside the worktree and run folder, any connector tool."""
    from . import sidefx
    e = os.environ if env is None else env
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd, run_dir = str(payload.get("cwd") or os.getcwd()), str(e.get("TTP_RUN_DIR") or "")
    if tool == "Bash":
        return shell_refusal(str(args.get("command") or ""), cwd, run_dir, e)
    if tool.startswith("mcp__"):
        return "a connector tool reaches a person or a service outside the hand-off"
    if tool in WRITE_TOOLS:
        home, roots = os.path.expanduser("~"), _roots(cwd, run_dir)
        paths = sidefx._arg_paths(args) + sidefx._patch_paths(json.dumps(args) if tool == "apply_patch" else
                                                              str(args.get("input") or args.get("patch") or ""))
        for p in paths:
            full = sidefx._expand(p, home, cwd, run_dir)
            if not sidefx._inside(full, roots):
                return f"it writes outside its worktree: {full}"
    return None

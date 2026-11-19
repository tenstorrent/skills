# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free guard on the side effects of a run on the code arm's GLM side.

A GLM-arm run (codearm.py) builds what its spec decided and hands the result back; it does not talk to a
person or another project, write the project's records or touch anything outside its worktree. A prompt
rule cannot hold that for a cheaper model, so the harness enforces it in code, with no model call, on
every harness the arm runs:

- `ttp` (cli.glm_gate) runs only the commands in SUBS_ALLOWED: checks, push, reads of its own task, the
  lock and detach it needs, and a plain `ttp note "<text>"`, which goes only to the run's own task log
  (progress.md); the coordinator decides what reaches a chat. Every route from a note to a person or
  another project (NOTE_ROUTES: --to, --reply and the flags that go with them), `say`, `ask`, `notify`,
  `upstream`, task, obligation, memory and config writes, and every other command are refused.
- The hook (hook.pre_tool_use, Claude Code and any provider with hooks) and the command layer of a harness
  without hooks (glmenv, for mini-swe-agent) ask tool_refusal() before each call: a `ttp` command the
  list does not allow, a raw `git push`, a `gh` write, an HTTP post, ssh or mail, a database or state
  write, a command that edits the guard's own environment, a file write outside the worktree and run
  folder, and every connector tool.

A refused attempt ends the run as a hand-back to Opus: hit() writes the run's glm_guard.json and a
`needs_opus` result.json (reason `guard: <what>`), the runner stops the agent at its next look and the
daemon requeues the task on Opus without spending an attempt (codearm.bounce_reason). The guard applies to
runs where active() holds: the daemon sets TTP_CODE_ARM_GUARD for a run whose arm is glm and writes MARK in
its run folder, so a run that unsets the variable is still guarded. Other runs are not touched.

Every run on the chat model, whatever route sent it there (run_env: the chat provider, a code arm's GLM run, a
routine coordinator lane), also gets two rules; the daemon sets RUN_ENV and writes RUN_MARK:
- human interaction stays on Opus: an outward post (outward: `ttp notify`, `say`, `ask`, `ttp note --to/--reply`,
  a `gh` write, an HTTP post, mail, any connector tool) is refused and recorded as a draft (DRAFTS: channel,
  target, text, why, command), and the run goes on; after the run the daemon hands the drafts to the
  coordinator's Opus session, which reviews each and sends or drops it (daemon.relay_glm_drafts);
- on a device task (its resources name a device or machine), the first failed gate command (a `check:` or
  `gate:` line of the spec, `ttp checks`, `ttp lock`, a broker status or preflight) ends the run as a hand-back
  to Opus with the command and its output (gate_stop); any other failed command does not."""
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
    "paths", "note", "advise"})   # advise: refused unless the run's glmassist switches are on
TASK_ACTIONS = frozenset({"show", "list"})
# `ttp note` flags that send a note on, or change a record beyond the run's own log: refused on the GLM arm.
NOTE_ROUTES = ("--to", "--reply", "--urgent", "--severity", "--confirms", "--status")
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
# Every run on the chat model (the code arm, a GLM work route, a routine coordinator lane, a bench run as a task):
# human interaction stays on Opus, so its outward posts become drafts an Opus step reviews and sends.
SCRATCH = "tmp"               # in the run folder: the run's own scratch folder, its TMPDIR (TMP, TEMP)
SCRATCH_VARS = ("TMPDIR", "TMP", "TEMP")
SCRATCH_LINE = ("Keep scratch files in $TMPDIR (a folder of your own in your run folder) or your worktree, never in"
                " /tmp: the guard refuses writes anywhere else.")
_SCRATCH_REF = re.compile(r"\$(?:\{(TMPDIR|TMP|TEMP)\}|(TMPDIR|TMP|TEMP)(?!\w))")
_SCRATCH_SET = re.compile(r"(?<![\w${])(?:TMPDIR|TMP|TEMP)(?!\w)")   # the command names one itself (sets it)
RUN_ENV = "TTP_GLM_RUN"       # "1", or DEVICE for a run on a device task
RUN_MARK = "glm_run.json"     # in the run folder: {"device", "gates"}, read where the variable is gone
DEVICE = "device"
DRAFTS = "outbound.jsonl"     # in the run folder: one {channel, target, text, why, command, ts} per refused post
GATE = "gate"
OUTPUT_TAIL = 2000
OUT_SUBS = frozenset({"notify", "say", "ask"})
OUT_NOTE = ("--to", "--reply")
MAIL = frozenset({"mail", "mailx", "sendmail", "mutt"})
GATE_SUBS = frozenset({"checks", "lock"})
BUSY = 75                     # `ttp lock` and the checks queue: busy, not failed
BROKER = re.compile(r"(?i)\bbroker\b.*\b(?:status|preflight)\b|\bpreflight\b")
GATE_LINE = re.compile(r"(?im)^\s*[-*]?\s*(?:check|gate)\s*:\s*`?([^`\n]+?)`?\s*$")
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


def scratch_env(run_dir, env=None) -> dict[str, str]:
    """TMPDIR, TMP and TEMP for a run under the GLM guards: `env`'s own short temp dir (a fenced run's, from
    shorttmp) when it has one, else the run folder's scratch folder, made here. The guard lets writes go there."""
    tmp = (env or {}).get("TMPDIR") or ""
    if not _own_tmp(tmp, str(run_dir)):
        tmp = str(Path(run_dir) / SCRATCH)
        Path(tmp).mkdir(exist_ok=True)
    return {var: tmp for var in SCRATCH_VARS}


def _own_tmp(tmp: str, run_dir: str) -> bool:
    """Whether `tmp` is a run's own scratch folder: its run folder's, or a short temp dir shorttmp made."""
    from . import shorttmp
    return bool(tmp) and ((bool(run_dir) and os.path.normpath(tmp) == os.path.normpath(Path(run_dir) / SCRATCH))
                          or shorttmp.owned(tmp))


def mark(run_dir) -> None:
    """The daemon's marker for a run on the arm (read by active() where the variable is gone)."""
    from .project import durable_write
    durable_write(Path(run_dir) / MARK, "1")


def hit(run_dir, why: str, kind: str = "guard", detail: str = "") -> None:
    """End the run as a hand-back to Opus: record the refusal and replace its hand-off with a `needs_opus`
    one (a hand-off it already wrote is kept beside it). The runner stops the agent when HIT exists;
    the daemon requeues the task on Opus. `kind`: "guard" for a refused side effect, GATE for a failed gate
    command on a device run, whose output tail is `detail`. Never raises: a guard that cannot write still
    refuses."""
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
        durable_write(res, json.dumps({"status": "blocked", "needs_opus": f"{kind}: {why}",
                                       "summary": f"needs Opus: {kind}: {why}",
                                       **({"gate_output": detail[-OUTPUT_TAIL:]} if detail else {})}))
        durable_write(d / HIT, json.dumps({"why": why, "kind": kind, "ts": time.time(),
                                           **({"output": detail[-OUTPUT_TAIL:]} if detail else {})}))
    except OSError:
        pass


def hit_reason(run_dir) -> str:
    """The reason the guard ended the run in `run_dir`, '' when it did not."""
    try:
        got = json.loads((Path(run_dir) / HIT).read_text())
    except (OSError, ValueError, TypeError):
        return ""
    return str(got.get("why") or "guard") if isinstance(got, dict) else "guard"


def note_route(flags) -> str:
    """The first of `flags` (option words of a `ttp note` call, argparse prefixes included) that routes the
    note past the run's own log, else ''."""
    for w in flags:
        opt = str(w).split("=", 1)[0]
        if len(opt) > 2 and opt.startswith("--") and any(o.startswith(opt) for o in NOTE_ROUTES):
            return next(o for o in NOTE_ROUTES if o.startswith(opt))
    return ""


def cli_refusal(cmd: str, action: str = "", flags=()) -> str | None:
    """Why a GLM-arm run may not run `ttp <cmd> [<action>]` with option words `flags`, else None."""
    if cmd == "note" and (route := note_route(flags)):
        return (f"`ttp note {route}` is not allowed to a run on the GLM arm: its notes go only to its own task log, "
                f"and the coordinator decides what reaches a person, a chat or another project")
    if cmd in SUBS_ALLOWED and (cmd != "task" or action in TASK_ACTIONS):
        return None
    what = f"`ttp {cmd}" + (f" {action}" if cmd == "task" and action else "") + "`" if cmd else "a ttp call naming no command"
    return (f"{what} is not allowed to a run on the GLM arm (it changes records or reaches a person or another "
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


def _gh_posts(seg: list[str]) -> bool:
    """Whether the `gh` call `seg` writes: anything but a read subcommand or a read-only `gh api`."""
    from . import hook
    args = [w for w in seg[1:] if not w.startswith("-")]
    return not (args[1:2] and args[1] in GH_READS) and not (args[:1] == ["api"] and not hook._gh_write(seg)
                                                         and not HTTP_DATA.search(" ".join(seg[1:])))


def _scratch(run_dir: str, env) -> str:
    """The run's scratch folder as the guard reads it: its TMPDIR when that is its own, else the run folder's."""
    tmp = str(env.get("TMPDIR") or "")
    return tmp if _own_tmp(tmp, run_dir) else (str(Path(run_dir) / SCRATCH) if run_dir else "")


def _roots(cwd: str, run_dir: str, scratch: str = "") -> list[str]:
    out = []
    for x in (cwd, run_dir, scratch):
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
        act = words[2:3] if words and words[0] == "task" else (words or [])[1:2]   # `ttp task <project> <action>`
        if words and (why := cli_refusal(words[0], act[0] if act else "", [w for w in seg if w.startswith("--")])):
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
        elif name == "gh" and _gh_posts(seg):
            return "a run on the GLM arm does not post to GitHub (comments, reviews, PRs, issues)"
        elif name in HTTP and HTTP_DATA.search(" ".join(seg[1:])):
            return "a run on the GLM arm does not send data to a service (an HTTP post)"
        elif name in REACH:
            return f"`{name}` reaches another host, person or database: not for a run on the GLM arm"
    home, tmp = os.path.expanduser("~"), _scratch(run_dir, e)
    roots = _roots(cwd, run_dir, tmp)
    # $TMPDIR (TMP, TEMP) is read as its value in the run's environment, or as /tmp where the command sets it itself
    told = bool(_SCRATCH_SET.search(cmd))
    paths = _SCRATCH_REF.sub(lambda m: "/tmp" if told else str(e.get(m.group(1) or m.group(2)) or "/tmp"), cmd)
    for path, (cls, why) in sidefx.classify_paths(paths, home, cwd or os.getcwd(), run_dir).items():
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
        home, roots = os.path.expanduser("~"), _roots(cwd, run_dir, _scratch(run_dir, e))
        paths = sidefx._arg_paths(args) + sidefx._patch_paths(json.dumps(args) if tool == "apply_patch" else
                                                              str(args.get("input") or args.get("patch") or ""))
        for p in paths:
            full = sidefx._expand(p, home, cwd, run_dir)
            if not sidefx._inside(full, roots):
                return f"it writes outside its worktree: {full}"
    return None


def run_env(provider: str, note, device: bool = False) -> dict[str, str]:
    """The flag for a run on the chat model, whatever route sent it there (empty for any other run): every
    run on the chat provider or whose note routes it there (glm_work, a tier route, a model pick), a code
    arm's GLM run and a coordinator lane or shadow turn. `device`: its task names a device or machine, so a
    failed gate command stops it (gate_stop)."""
    from . import coordroute, route
    routed = note.get(route.KEY) if isinstance(note, dict) else None
    on = (provider == route.TO or (isinstance(routed, dict) and routed.get("to") == route.TO)
          or bool(env_for(note)) or bool(coordroute.guard_env(note)))
    return {RUN_ENV: DEVICE if device else "1"} if on else {}


def device_task(task: dict | None) -> bool:
    """Whether `task` is device work: its resources name a device or machine."""
    for k in ("resources", "resources_read"):
        raw = (task or {}).get(k)
        try:
            got = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            got = raw
        if got:
            return True
    return False


def gate_lines(spec: str) -> list[str]:
    """The commands of the `check:` and `gate:` lines of a task spec (or an arm's plan)."""
    return [" ".join(m.split()) for m in GATE_LINE.findall(spec or "")][:20]


def mark_run(run_dir, device: bool, gates=()) -> None:
    """The daemon's marker for a run on the chat model (read by glm_run() where the variable is gone)."""
    from .project import durable_write
    durable_write(Path(run_dir) / RUN_MARK, json.dumps({"device": bool(device), "gates": list(gates)}))


def glm_run(env=None) -> dict | None:
    """{"device", "gates"} for a run on the chat model, None for any other run."""
    e = os.environ if env is None else env
    rd = str(e.get("TTP_RUN_DIR") or "")
    try:
        got = json.loads((Path(rd) / RUN_MARK).read_text()) if rd else None
    except (OSError, ValueError):
        got = None
    if isinstance(got, dict):
        gates = got.get("gates") if isinstance(got.get("gates"), list) else []
        return {"device": bool(got.get("device")) or e.get(RUN_ENV) == DEVICE, "gates": [str(g) for g in gates]}
    if e.get(RUN_ENV) in ("1", DEVICE) or active(e):
        return {"device": e.get(RUN_ENV) == DEVICE, "gates": []}
    return None


def _value(seg: list[str], *names: str) -> str:
    """The value of the first of option `names` in `seg` (`--x v` or `--x=v`), '' when absent."""
    for i, w in enumerate(seg):
        for n in names:
            if w == n and i + 1 < len(seg):
                return seg[i + 1]
            if w.startswith(n + "=") and n.startswith("--"):
                return w.split("=", 1)[1]
    return ""


def _outward_seg(seg: list[str], project: str) -> dict | None:
    name = os.path.basename(seg[0])
    words = _ttp_args(seg, project)
    if words is not None:
        if words and words[0] in OUT_SUBS:
            return {"channel": words[0], "target": _value(seg, "--chat") or "user", "text": " ".join(words[1:]),
                    "why": f"`ttp {words[0]}` reaches a person"}
        flags = [w for w in seg if w.startswith("--")]
        if words and words[0] == "note" and (route := note_route(flags)) in OUT_NOTE:
            target = _value(seg, route)
            return {"channel": "note", "target": target, "text": " ".join(w for w in words[1:] if w != target),
                    "why": f"`ttp note {route}` reaches another project or a person"}
        return None
    if name == "gh" and _gh_posts(seg):
        return {"channel": "github", "target": " ".join(w for w in seg[1:4] if not w.startswith("-")),
                "text": _value(seg, "--body", "-b", "--title", "-t"), "why": "a `gh` write posts to GitHub"}
    if name in HTTP and HTTP_DATA.search(" ".join(seg[1:])):
        url = next((w for w in seg[1:] if "://" in w), "")
        return {"channel": "http", "target": url, "text": _value(seg, "-d", "--data", "--data-raw", "-F", "--form"),
                "why": "an HTTP post sends data to a service"}
    if name in MAIL:
        return {"channel": "mail", "target": " ".join(w for w in seg[1:] if not w.startswith("-"))[:200], "text": "",
                "why": "mail reaches a person"}
    return None


def outward_shell(cmd: str, env=None) -> dict | None:
    """The draft of the first outward post in shell command `cmd` (a `ttp notify`, `say` or `ask`, a
    `ttp note --to/--reply`, a `gh` write, an HTTP post, mail), None when it posts nothing."""
    from . import hook
    e = os.environ if env is None else env
    project = _own_name(e)
    plain, _ = hook._split(REDIRECT.sub(" ", cmd))
    for seg in hook._all_segments(plain) + hook._segments(plain, strip=False):
        if seg and (d := _outward_seg(seg, project)):
            return {**d, "command": cmd[:OUTPUT_TAIL]}
    return None


def outward(payload: dict, env=None) -> dict | None:
    """The draft of a hook payload's outward post (a Bash command as outward_shell, any connector tool), else None."""
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool == "Bash":
        return outward_shell(str(args.get("command") or ""), env)
    if tool.startswith("mcp__"):
        target = next((str(args[k]) for k in ("channel", "channel_id", "to", "issueKey", "issue_key", "page_id",
                                               "recipient", "user") if args.get(k)), "")
        text = next((str(args[k]) for k in ("text", "body", "message", "content", "comment") if args.get(k)), "")
        return {"channel": tool, "target": target, "text": text or json.dumps(args)[:OUTPUT_TAIL],
                "why": "a connector tool reaches a person or a service"}
    return None


def outward_cli(cmd: str, text: str = "", to: str = "", reply: str = "", chat: str = "") -> dict | None:
    """The draft of `ttp <cmd>` when it posts outward (the command layer of a run without hooks), else None."""
    if cmd in OUT_SUBS:
        return {"channel": cmd, "target": chat or "user", "text": text, "why": f"`ttp {cmd}` reaches a person"}
    if cmd == "note" and (to or reply):
        return {"channel": "note", "target": to or reply, "text": text,
                "why": f"`ttp note {'--to' if to else '--reply'}` reaches another project or a person"}
    return None


def drafted(run_dir, d: dict) -> str:
    """Record draft `d` in the run's DRAFTS (an identical one once) and return the refusal the model reads."""
    if run_dir:
        from .project import durable_append
        f = Path(run_dir) / DRAFTS
        key = (d.get("channel"), d.get("target"), d.get("text"))
        try:
            seen = any((x.get("channel"), x.get("target"), x.get("text")) == key
                       for x in (json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()))
        except (OSError, ValueError):
            seen = False
        if not seen:
            durable_append(f, json.dumps({**d, "ts": time.time()}) + "\n")
    return (f"not sent: {d.get('why')}, and a run on the chat model hands outward posts off. It is recorded as a "
            f"draft ({DRAFTS} in the run folder); after the run an Opus step reviews it and sends or drops it. "
            f"Carry on with the task and do not retry the post")


def drafts(run_dir) -> list[dict]:
    """The drafts a run recorded."""
    try:
        lines = (Path(run_dir) / DRAFTS).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def is_gate(cmd: str, gates=(), env=None) -> bool:
    """Whether shell command `cmd` is a gate: a `check:`/`gate:` line of the spec, `ttp checks`, `ttp lock`,
    or a broker status or preflight."""
    from . import hook
    norm = " ".join(cmd.split())
    if any(g and (norm == g or norm.startswith(g + " ")) for g in gates):
        return True
    if BROKER.search(cmd):
        return True
    project = _own_name(os.environ if env is None else env)
    plain, _ = hook._split(REDIRECT.sub(" ", cmd))
    return any((w := _ttp_args(seg, project)) and w[0] in GATE_SUBS
               for seg in hook._all_segments(plain) + hook._segments(plain, strip=False) if seg)


def gate_stop(run_dir, cmd: str, rc, output: str, env=None) -> str | None:
    """On a device run on the chat model, a gate command that failed (`rc` neither 0 nor busy) ends the run
    as a hand-back to Opus with the command and its output; returns why, None when the run goes on."""
    g = glm_run(env)
    if not g or not g["device"] or rc in (0, BUSY, None) or not is_gate(cmd, g["gates"], env):
        return None
    why = f"a gate failed (exit {rc}): `{' '.join(cmd.split())[:160]}`"
    hit(run_dir, why, GATE, output or "")
    return why

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The tools and the per-wake budget of a coordinator in session mode (coordinator.session.mode
"on"). Claude takes the hooks below through --settings; an agent without tool_hooks gets them through
its own hook config (Provider.coord_tools: Codex runs HOOK_EVENT and POST_EVENT per call), and a
whole-wake guard (wake_snapshot, wake_guard) puts back protected files after any such wake. With the mode off or unset, nothing here changes how a coordinator turn launches: the stateless
turn has no tools.

Full tools (coordinator.session.tools "full", the default): a session wake (a new or resumed session)
gets a worker's tools (providers/claude.WORKER_TOOLS and its web tools) under a worker's guards: the
worker PreToolUse checks (hook.pre_tool_use: the PR draft and gh guards, no posting as the user, no
credential reads, the kill guard, the device-run broker rule, no direct full suite) run on every Bash
and file edit, the run's PATH starts with the harness's gh and git wrappers, and it runs unsandboxed
as every coordinator turn does. On top of them (full_refusal): no direct write to the project's
charter, memory, project.json or state database (Edit/Write deny rules and the hook; the denial names
the action or command that applies the same checks), no raw `git push` and no landing `ttp push` (a
--land, --own, --detach or --move-tag push, or a plain one: code goes to a worker task, its review and
the push queue; a channel tag moves only in a release cut task after its candidate checks; `ttp push
--queue`, `--result` and `--free` stay), and no foreground command past coordinator.session.tool_timeout_s
(TOOL_TIMEOUT_S; the CLI's own Bash timeout is set to it too): longer work goes to `ttp detach` or a
worker. Every call that is not a read (Bash off the read-only list, Edit, Write) is logged with its
exit to the run's AUDIT file and its progress.md, which the web app shows (post_tool_use). The Bash
checks read the command text, so a command that builds a protected path at run time (a variable, a glob,
a script file) passes them; around each Bash call a guard compares the charter, memory and project.json
with a snapshot taken before it (snapshot, guard), puts back what changed outside ttp's own writes (which
project.protected_log records) and reports it in the tool result and as an event. The state database is
not compared, as the daemon writes it all the time: only the command-text refusal covers it. A push built
at run time is refused again at run time: `ttp push` (cli.cmd_push) and the harness's git
wrapper (gitguard.main) refuse a landing or any `git push` in a coordinator run (coordinator_run: FULL_ENV,
or ROLE_ENV, which the daemon sets for every run). A coordinator run also holds no device
(TTP_NO_DEVICE=1), so the worker device-command refusals (hook.device_command) bind it (heavy device work
goes to a worker). Event text stays untrusted data (later hardening: #6095, #6098). A stateless turn
(also a session's stateless fallback) never gets them: the fallback keeps the read-only set below.

Read-only tools ("read_only", any value but "full"): Read (and Grep/Glob where the CLI has them) on the project folder, and Bash for a fixed list of
read-only `ttp` commands, `ttp hostcheck` among them (hostcheck.py: git log/show, lock holders and the
other host checks). No write tool, no free shell. Three fences hold it: the CLI's tool list, its
allow and deny rules under the dontAsk permission mode (anything not allowed is refused), and a
PreToolUse hook (pre_tool_use) that denies every call outside the list, also a Bash line with a shell
operator, redirect or substitution. The one other tool it allows is StructuredOutput, the CLI's
own tool for the turn's --json-schema answer, without which the turn has no structured result; the
hook denies an answer with an action coord.apply would refuse, with the reason (coordprecheck.py).

Budget: a wake stops at whichever comes first of coordinator.session.wake_budget_usd (the CLI's
--max-budget-usd) and wake_budget_s (the run's timeout); there is no tool-call cap. Unset, each is
what the stateless turn it replaces gets: the sized turn budget (coordinator.turn_budget_usd, default
$1) and the effort-sized turn timeout (coordlimits.turn_timeout). Why: a wake is started by exactly the
events, gates and hourly cap that start a stateless turn today, one wake per turn, so a per-wake
ceiling equal to the per-turn ceiling keeps a project's daily ceiling at or under the stateless
design. A typical wake costs less than its ceiling: the session's cached context is read, not resent.
A wake that hits either limit ends with what it has; that non-ok end takes the stateless rerun path."""
from __future__ import annotations

import json
import os
import re
import shlex
import time

from .coordlimits import positive

HOOK_EVENT = "CoordPreToolUse"
DENIED_ENV = "TTP_COORD_DENIED_READS"
FILE_TOOLS = ("Read", "Grep", "Glob")
CLI_TOOLS = ("Read", "Bash")   # Grep and Glob are not in every CLI: --tools names only these two
# The CLI's tool for a --json-schema answer: a turn ends only through it, and it writes nothing.
ANSWER_TOOL = "StructuredOutput"
# `ttp <words...>` prefixes a session coordinator may run, each followed by any plain arguments.
# "*" is one project name: the CLI rules name the coordinator's own, the hook takes any (all read-only).
READ_COMMANDS = (("status",), ("task", "*", "show"), ("task", "*", "list"), ("said",), ("eta",), ("landed",),
                 ("stats",), ("hostcheck",), ("machines", "list"), ("machines", "activity"), ("machines", "status"))
FULL_ENV = "TTP_COORD_FULL_TOOLS"   # "1": the hook applies full_refusal, not the read-only list
ROLE_ENV = "TTP_ROLE"   # the run's role, set for every run (daemon.start_run): "coordinator" in a coordinator's
# `ttp push` forms that land or move nothing (cli.cmd_push): the only ones a coordinator may run
PUSH_READS = (("--queue",), ("--free",), ("--result", "*"), ("--result", "*", "--pushed"), ("--help",), ("-h",))
PUSH_REFUSAL = ("a coordinator lands nothing and moves no tag: code changes go on a work branch through a worker "
                "task, its review and the push queue (the review lands them), and a channel tag moves only in a "
                "release cut task once its candidate checks pass. Queue a task (`ttp task <project> add ...`)")


def coordinator_run(env=None) -> bool:
    """Whether this process runs in a coordinator run (its tools, or a command one of them started)."""
    env = os.environ if env is None else env
    return env.get(FULL_ENV) == "1" or env.get(ROLE_ENV) == "coordinator"
PROTECTED_ENV = "TTP_COORD_PROTECTED"   # the files no tool writes directly, os.pathsep-separated
TIMEOUT_ENV = "TTP_COORD_TOOL_TIMEOUT_S"
POST_EVENT, FAIL_EVENT = "CoordPostToolUse", "CoordPostToolUseFailure"
AUDIT = "tool-audit.jsonl"   # in the run dir: one line per mutating tool call
GUARD_DIR = "coord-guard"   # in the run dir: each Bash call's snapshot of the protected files (snapshot, guard)
GUARD_EVENT = "coord_protected_write"   # the event of a protected file guard put back
PUT_BACK_DIR = "coord-put-back"   # in a project's state folder: content guard put back, by its sha256
PUT_BACK_LOG = "coord-put-back.jsonl"   # in a project's state folder: one line per put-back (guard)
TOOL_TIMEOUT_S = 300   # a foreground command's limit by default
MAX_TOOL_TIMEOUT_S = 600   # the CLI's own Bash ceiling
MODES = ("full", "read_only")
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "eval", "ssh", "su", "sudo", "script"}
# Anything a shell would act on: operators, redirects, substitutions, globs, comments, escapes.
SHELL_META = re.compile(r"[;&|<>`$\\(){}*?\[\]!#\n\r]")


def session(cfg: dict) -> dict:
    s = (cfg.get("coordinator") or {}).get("session")
    return s if isinstance(s, dict) else {}


def session_on(cfg: dict, provider: str | None = None) -> bool:
    """coordinator.session.mode == "on" (unset: the default, coordsession.DEFAULTS); anything else is off.
    With `provider`, also off for a provider that keeps no session (coordsession.keeps_session)."""
    from .coordsession import DEFAULTS, keeps_session
    if provider is not None and not keeps_session(provider):
        return False
    mode = session(cfg).get("mode")
    return str(DEFAULTS["mode"] if mode is None else mode).strip().lower() == "on"


def tools_mode(cfg: dict) -> str:
    """coordinator.session.tools: "full" when unset or "full", else "read_only" (an unknown value fails closed)."""
    v = session(cfg).get("tools")
    return "full" if v is None or str(v).strip().lower() == "full" else "read_only"


def tools_mode_of(value) -> str:
    """The config_set validator of coordinator.session.tools."""
    mode = str(value).strip().lower()
    if mode not in MODES:
        raise ValueError(f"coordinator.session.tools: give one of {', '.join(MODES)}")
    return mode


def tool_timeout_s(cfg: dict) -> int:
    """A foreground command's limit: coordinator.session.tool_timeout_s, else TOOL_TIMEOUT_S, at most the CLI's."""
    return int(min(positive(session(cfg).get("tool_timeout_s"), TOOL_TIMEOUT_S), MAX_TOOL_TIMEOUT_S))


def protected_paths(p) -> list[str]:
    """The project files a coordinator changes only through actions and the ttp CLI."""
    return [str(x) for x in (p.charter_path, p.memory_index, p.harness / "memory", p.config_path,
                             p.state / "project.db")]


def _rules(project: str) -> list[str]:
    out = []
    for words in READ_COMMANDS:
        line = " ".join(project if w == "*" else w for w in ("ttp", *words))
        out += [f"Bash({line})", f"Bash({line} *)"]
    return out


def hook_settings(denied_reads=(), *, full: bool = False, protected=(), timeout_s: int = TOOL_TIMEOUT_S) -> dict:
    """The coordinator's PreToolUse hook (every tool goes through it) and the files it never reads,
    as --settings JSON; `full`: also its audit hooks, the protected files' Edit deny rules and the
    CLI's Bash timeout."""
    from pathlib import Path
    runtime = str(Path(__file__).resolve().parents[1])
    from .providers.base import isolated_module_argv
    cmd = shlex.join(isolated_module_argv("ttp.hook", runtime))   # never imports from the project folder
    deny = [str(p) for p in denied_reads if os.path.isabs(p)]
    hook = lambda event: [{"matcher": "*", "hooks": [{"type": "command", "command": f"{cmd} {event}", "timeout": 20}]}]  # noqa: E731
    out = {"hooks": {"PreToolUse": hook(HOOK_EVENT)}, "permissions": {"deny": [f"Read(/{p})" for p in deny]},
           "env": {"PYTHONPATH": runtime, DENIED_ENV: os.pathsep.join(deny)},
           # No Bash sandbox, even one the user's own settings turn on (as providers.claude.worker_settings).
           "sandbox": {"enabled": False}}
    if full:
        mine = [str(p) for p in protected if os.path.isabs(p)]
        out["hooks"].update(PostToolUse=hook(POST_EVENT), PostToolUseFailure=hook(FAIL_EVENT))
        out["permissions"]["deny"] += [f"Edit(/{p}{'/**' if not os.path.splitext(p)[1] else ''})" for p in mine]
        ms = str(int(timeout_s) * 1000)
        out["env"].update({FULL_ENV: "1", PROTECTED_ENV: os.pathsep.join(mine), TIMEOUT_ENV: str(int(timeout_s)),
                           "BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms})
    return out


def full_tools(cfg: dict, session_mode: str) -> bool:
    """Whether this coordinator turn gets the full tools: a session wake with coordinator.session.tools full."""
    return session_on(cfg) and session_mode in ("new", "resume") and tools_mode(cfg) == "full"


def without_edit_deny(argv: list[str]) -> list[str]:
    """`argv` without the read-only turn's `--disallowedTools Edit Write NotebookEdit` (providers/claude.build)."""
    seq = ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
    for i in range(len(argv) - len(seq) + 1):
        if argv[i:i + len(seq)] == seq:
            return argv[:i] + argv[i + len(seq):]
    return argv


def session_tool_args(cfg: dict, *, project: str = "", root: str = "", denied_reads=(), session_mode: str = "",
                      protected=(), web: bool = True) -> list[str]:
    """The CLI arguments that give a session coordinator its tools ([] with the mode off): the full
    set for a session wake (full_tools), else the read-only set. They take the place of the stateless
    turn's `--tools ""`."""
    if not session_on(cfg):
        return []
    deny = [f"Read(/{p})" for p in denied_reads if os.path.isabs(p)]
    if full_tools(cfg, session_mode):
        from .providers.claude import WEB_TOOLS, WORKER_TOOLS
        tools = [*WORKER_TOOLS, *(WEB_TOOLS if web else ())]
        return ["--tools", ",".join(tools), "--allowedTools", ANSWER_TOOL, *tools,
                *(["--disallowedTools", ",".join(deny)] if deny else []),
                "--settings", json.dumps(hook_settings(denied_reads, full=True, protected=protected,
                                                       timeout_s=tool_timeout_s(cfg)))]
    allow = [f"Read(/{os.path.abspath(root)}/**)"] if root else []
    args = ["--tools", ",".join(CLI_TOOLS), "--allowedTools", ANSWER_TOOL, *allow, *_rules(project or "*")]
    if deny:   # one comma-separated value, so a later flag is no rule
        args += ["--disallowedTools", ",".join(deny)]
    return args + ["--settings", json.dumps(hook_settings(denied_reads))]


def _denied_read(path: str, denied: list[str], search: bool = False) -> bool:
    """Whether reading `path` reads a denied file; a search (Grep, Glob) also when it would walk into one."""
    try:
        real = os.path.realpath(path)
        files = [os.path.realpath(d) for d in denied]
    except (OSError, ValueError):
        return True
    return any(real == f or search and f.startswith(real.rstrip(os.sep) + os.sep) for f in files)


def bash_refusal(cmd: str) -> str | None:
    """Why the coordinator may not run `cmd` (None: a listed read-only `ttp` command)."""
    if SHELL_META.search(cmd):
        return "a session coordinator runs one plain read-only command: no shell operators, redirects or substitutions"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "the command does not parse"
    if argv[:1] != ["ttp"]:
        return ("a session coordinator runs only read-only ttp commands (status, task <project> show|list, said, eta, "
                "landed, stats, machines list|activity|status, hostcheck)")
    for words in READ_COMMANDS:
        head = argv[1:1 + len(words)]
        if len(head) == len(words) and all(w == "*" and not h.startswith("-") or h == w for w, h in zip(words, head)):
            return None
    return f"`ttp {' '.join(argv[1:3])}` is not one of the coordinator's read-only commands"


def _answer_check(payload: dict) -> dict | None:
    """A denial carrying the reasons coord.apply would refuse actions of the answer, to fix in this
    turn (coordprecheck.py); None lets it through, also on any error: the answer always can end the turn."""
    from .hook import _deny
    try:
        from .coordprecheck import refusal
        why = refusal(payload)
    except Exception:
        return None
    return _deny(why) if why else None


def _protected_hit(path: str, protected: list[str]) -> str:
    """The protected path `path` writes ("" for none): the file itself, a sibling CHARTER* file, a file
    under a protected folder, or the database's -wal/-shm/-journal companion."""
    real = os.path.realpath(path)
    for f in protected:
        f = os.path.realpath(f)
        name = os.path.basename(f)
        if real == f or real.startswith(f.rstrip(os.sep) + os.sep) or real.startswith(f + "-") \
                or name.startswith("CHARTER") and os.path.dirname(real) == os.path.dirname(f) \
                and os.path.basename(real).startswith("CHARTER"):
            return f
    return ""


def _protected_why(f: str) -> str:
    name = os.path.basename(f)
    how = ("change it with the `charter_update` action, which takes the user's word for a Restriction"
           if name.startswith("CHARTER") else
           "change memory with the `memory_add` and `memory_forget` actions" if name in ("MEMORY.md", "memory") else
           "change settings with the `config_set` action or `ttp config KEY VALUE` (both refuse the user-only "
           "keys: spend caps, providers.<name>.write_fence and the rest; ask the user for those)"
           if name == "project.json" else
           "the state database (tasks, kv) changes only through actions and the ttp CLI (task_add, task_update, "
           "`ttp task ...`)")
    return f"a coordinator never writes {name} directly, so the daemon's checks stay in the path; {how}"


def _named(cmd: str, f: str, cwd: str) -> bool:
    """Whether the command text names protected path `f`: its absolute or cwd-relative path, or (a
    file) its distinctive name."""
    name = os.path.basename(f)
    rel = os.path.relpath(f, cwd) if cwd else f
    keys = {f, os.path.realpath(f), rel} | ({"CHARTER"} if name.startswith("CHARTER") else
                                           {name} if name in ("MEMORY.md", "project.json", "project.db") else set())
    return any(k and k not in (".", "..") and k in cmd for k in keys)


def _runs(cmd: str, hit, inner_re: str, depth: int = 0) -> bool:
    """Whether a segment of `cmd` is a `hit`, also inside `sh -c`, ssh, eval and the like (or any text
    there matching `inner_re`)."""
    from .hook import _segments
    for seg in _segments(cmd):
        if hit(seg):
            return True
        if os.path.basename(seg[0]) in SHELLS:
            inner = " ".join(seg[1:])
            if depth < 3 and _runs(inner, hit, inner_re, depth + 1) or re.search(inner_re, inner, re.S):
                return True
    return False


def _git_push(seg: list[str]) -> bool:
    from . import gitguard
    return os.path.basename(seg[0]) == "git" and gitguard.split(seg[1:])[1] == "push"


def _ttp_push(seg: list[str]) -> bool:
    """A `ttp push` (also after `ttp clip --` and the like) other than a PUSH_READS form."""
    for i, w in enumerate(seg[:-1]):
        if os.path.basename(w) == "ttp" and seg[i + 1] == "push":
            rest = seg[i + 2:]
            return not any(len(rest) == len(f) and all(x == "*" or x == r for x, r in zip(f, rest))
                           for f in PUSH_READS)
    return False


def _pushes(cmd: str) -> bool:
    """Whether `cmd` runs `git push`: a git command (by any path, under wrappers) whose subcommand is
    push, also inside `sh -c`, ssh, eval and the like (any later word `git ... push` there)."""
    return _runs(cmd, _git_push, r"\bgit\b.*\bpush\b")


def _lands(cmd: str) -> bool:
    """Whether `cmd` runs a `ttp push` that may land or move a tag (any but PUSH_READS)."""
    return _runs(cmd, _ttp_push, r"\bttp\s+push\b(?!\s+--(?:queue|free|result)\b)")


def full_refusal(tool: str, args: dict, cwd: str) -> str | None:
    """Why a full-tools coordinator may not make this call before the worker checks run (None: go on)."""
    protected = [d for d in os.environ.get(PROTECTED_ENV, "").split(os.pathsep) if d]
    if tool in EDITS:
        path = str(args.get("file_path") or args.get("notebook_path") or "")
        if not path:
            return "the edit names no file"
        hit = _protected_hit(path if os.path.isabs(path) else os.path.join(cwd, path), protected)
        return _protected_why(hit) if hit else None
    if tool != "Bash":
        return None
    cmd = str(args.get("command") or "")
    limit = positive(os.environ.get(TIMEOUT_ENV), TOOL_TIMEOUT_S)
    asked = positive(args.get("timeout"), 0) / 1000
    if asked > limit or args.get("run_in_background"):
        return (f"a coordinator's foreground command runs at most {int(limit)} s (coordinator.session.tool_timeout_s); "
                "run longer work with `ttp detach <name> -- <cmd>` or give it to a worker task")
    if _pushes(cmd) or _lands(cmd):
        return PUSH_REFUSAL
    if bash_refusal(cmd) is None:   # a listed read-only ttp command
        return None
    hit = next((f for f in protected if _named(cmd, f, cwd)), "")
    return (_protected_why(hit) + "; read it with Read or Grep") if hit else None


def _full_pre_tool_use(tool: str, args: dict, payload: dict):
    from . import hook
    if tool == ANSWER_TOOL:   # the turn's structured answer, checked as in read-only mode
        return _answer_check(payload)
    from . import pathguard
    why = pathguard.refusal(payload) or full_refusal(tool, args, str(payload.get("cwd") or os.getcwd()))
    if why:
        return hook._deny(why)
    if tool in EDITS or tool == "Bash":   # a worker's own checks
        out = hook.pre_tool_use(payload)[0]
        if tool == "Bash" and (out or {}).get("hookSpecificOutput", {}).get("permissionDecision") != "deny":
            snapshot(payload)
        return out
    why = hook.local_guard(payload)   # the project's own guards (localguard.py) on its other tools
    return hook._deny(why) if why else None


def _guard_key(payload: dict) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("tool_use_id") or ""))[:80]
    if not key:
        import hashlib
        key = hashlib.sha256(json.dumps(payload.get("tool_input") or {}, sort_keys=True).encode()).hexdigest()[:32]
    return key


def _guarded_files(protected=None) -> list[str]:
    """The files a coordinator Bash call's guard compares: the charter (and sibling CHARTER* files), the
    memory index, every memory entry and project.json; never the state database, which the daemon writes
    all the time (only the command-text refusal covers it)."""
    out = set()
    for f in _protected_list(protected):
        if os.path.basename(f) == "project.db":
            continue
        if os.path.isdir(f):
            out.update(os.path.join(d, n) for d, _, names in os.walk(f) for n in names
                       if not (n.startswith(".") and n.endswith(".tmp")))   # durable_write's temporary files
        else:
            out.add(f)
            name, parent = os.path.basename(f), os.path.dirname(f)
            if name.startswith("CHARTER") and os.path.isdir(parent):
                out.update(os.path.join(parent, n) for n in os.listdir(parent) if n.startswith("CHARTER"))
    return sorted({os.path.abspath(x) for x in out})   # not realpath: a symlink planted there is put back, never followed


def _protected_list(protected=None) -> list[str]:
    """`protected` (the daemon's wake guard), else the hook's PROTECTED_ENV."""
    got = protected if protected is not None else os.environ.get(PROTECTED_ENV, "").split(os.pathsep)
    return [str(d) for d in got if d]


def _digests(files: list[str]) -> dict[str, tuple[str, bytes]]:
    import hashlib
    out = {}
    for f in files:
        try:
            data = open(f, "rb").read()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
        out[f] = (hashlib.sha256(data).hexdigest(), data)
    return out


def snapshot(payload: dict, run=None, protected=None) -> None:
    """Before a full-tools coordinator's Bash call: record the protected files' hashes (and their bytes,
    once per content) in the run dir, for guard to compare after the call. `run` and `protected` (else
    TTP_RUN_DIR and PROTECTED_ENV): the daemon's whole-wake guard (wake_snapshot)."""
    from pathlib import Path
    from .project import durable_write
    run = run or os.environ.get("TTP_RUN_DIR")
    if not run:
        return
    d = Path(run) / GUARD_DIR
    ts = time.time()   # before the reads: a logged write while they run counts
    files = _digests(_guarded_files(protected))
    for sha, data in files.values():
        if not (d / "blobs" / sha).exists():
            durable_write(d / "blobs" / sha, data, sync=False)
    durable_write(d / f"{_guard_key(payload)}.json",
                  json.dumps({"ts": ts, "files": {f: sha for f, (sha, _) in files.items()}}), sync=False)


def guard(payload: dict, run=None, protected=None) -> str | None:
    """After a full-tools coordinator's Bash call: put back every protected file the call changed,
    created or removed outside ttp's own writes, from the snapshot, record an event, and return the
    notice for the tool result (None: nothing changed out of band). ttp's writers log each protected
    write at one choke point (project.durable_write, durable_append, the memory archive renames and
    project.tree_writes around every git command that changes the harness checkout); a change whose
    path and new digest are logged since the snapshot is kept. Before a put-back the changed content is
    saved (PUT_BACK_DIR under the project's state folder, indexed by PUT_BACK_LOG with path, digest,
    time and command), and the notice names it, so nothing is lost. A changed file no ttp writer logs
    for (project.protected_log is None: outside a project's harness folder) is reported, never put
    back. It reads files, not commands, so a variable, glob or script write is caught too. Accepted
    gaps: a user's own editor edit to the charter, memory or project.json during the call is put back
    (saved and reported), not recognised as theirs; a call whose post hook never runs (an interrupted
    call) and a write by a process the call left in the background are not checked; a script that also
    forges the write log passes. Write/log race: durable_write logs only after its os.replace and
    fsync_dir, and tree_writes only after its git command ends, so a check inside that window takes
    ttp's own write as unlogged and puts it back (saved and reported), and a ttp write landing between
    this read and the put-back is overwritten unsaved. Restore-to-snapshot: the put-back restores the
    pre-call snapshot, so a call holding a logged ttp write and a later unlogged edit to the same file
    also undoes the ttp write (the combined content is saved)."""
    from pathlib import Path
    from .project import durable_append, durable_write, note_protected, protected_log
    run = run or os.environ.get("TTP_RUN_DIR")
    snap = Path(run) / GUARD_DIR / f"{_guard_key(payload)}.json" if run else None
    if snap is None or not snap.exists():
        return None
    before = json.loads(snap.read_text())
    files = sorted(set(before["files"]) | set(_guarded_files(protected)))
    after = _digests(files)
    now = {f: sha for f, (sha, _) in after.items()}
    logs = {protected_log(f) for f in files} - {None}
    logged = set()
    for log in logs:
        for path in (log.with_name(log.name + ".1"), log):
            try:
                lines = path.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    e = json.loads(line)
                    if e["ts"] >= before["ts"]:
                        logged.add((e["path"], e["sha"]))
                except (ValueError, KeyError, TypeError):
                    continue
    changed = [f for f in files if before["files"].get(f) != now.get(f) and (f, now.get(f)) not in logged]
    command = str((payload.get("tool_input") or {}).get("command") or "")
    bad, saved = [], []
    for f in changed:
        if protected_log(f) is None:
            continue   # no ttp writer logs it: reported only
        if f in after:   # the changed content, kept before the put-back; unsaved, it stays as is
            sha, data = after[f]
            state = protected_log(f).parent
            try:
                durable_write(state / PUT_BACK_DIR / sha, data)
                durable_append(state / PUT_BACK_LOG, json.dumps({
                    "ts": time.time(), "path": f, "sha": sha, "saved": str(state / PUT_BACK_DIR / sha),
                    "command": command[:2000]}) + "\n")
            except OSError:
                continue
            saved.append(str(state / PUT_BACK_DIR / sha))
        bad.append(f)
        if f in before["files"]:
            durable_write(Path(f), (snap.parent / "blobs" / before["files"][f]).read_bytes())
        else:
            os.unlink(f)
            note_protected(f)
    snap.unlink(missing_ok=True)
    if not changed:
        return None
    how = sorted({_protected_why(next((p for p in _protected_list(protected) if _protected_hit(f, [p])), f))
                  for f in changed})
    left = [f for f in changed if f not in bad]
    text = "tt-project: " + " ".join(filter(None, [
        bad and (f"this command changed {', '.join(bad)} outside the coordinator's checks; each is put back as it "
                 f"was before the call" + (f" (the changed content is saved as {', '.join(saved)})" if saved else "")
                 + "."),
        left and f"{', '.join(left)} changed during this command; no ttp writer logs it (or its content could "
                 f"not be saved), so it is left as is.",
        "; ".join(how)]))
    try:
        from .project import Project
        Project(next(iter(logs)).parent.parent).db.x(
            "INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
            (time.time(), "coordinator", GUARD_EVENT, "normal", text[:1500],
             json.dumps({"files": bad, "left": left, "saved": saved, "command": command[:500]}),
             "handled"))
    except Exception:   # the files are back either way; only the record is lost
        pass
    return text


WAKE = {"tool_use_id": "wake"}   # the whole-wake guard's snapshot key (tool call ids never take it)


def wake_snapshot(run_dir, protected) -> None:
    """Before a coordinator wake on an agent without ttp's tool hooks (Provider.tool_hooks False): the
    per-call guard's snapshot, taken once around the whole wake."""
    snapshot(WAKE, str(run_dir), list(protected))


def wake_guard(run_dir, protected) -> str | None:
    """After that wake, before its output is applied: guard() over the whole wake, so a protected file
    it changed outside ttp's own writes is saved, put back and reported as on a hooked agent. Its
    accepted gaps are guard()'s, over the wake's whole span: a user's own edit while it runs is put back
    (saved and reported)."""
    return guard(WAKE, str(run_dir), list(protected))


def pre_tool_use(payload: dict) -> tuple[dict | None, None]:
    """Deny every coordinator tool call outside its tools (the read-only list, or with FULL_ENV the
    full set under full_refusal and a worker's checks); no opinion on the rest, so the CLI's own allow
    rules still apply. Any error denies."""
    from .hook import _deny
    try:
        tool = str(payload.get("tool_name") or "")
        args = payload.get("tool_input") or {}
        if os.environ.get(FULL_ENV) == "1" and tool not in FILE_TOOLS:
            return _full_pre_tool_use(tool, args, payload), None
        if tool in FILE_TOOLS:
            denied = [d for d in os.environ.get(DENIED_ENV, "").split(os.pathsep) if d]
            search = tool != "Read"
            path = str(args.get("file_path") or args.get("path") or (os.getcwd() if search else ""))
            from .credguard import tool_refusal
            if path and (_denied_read(path, denied, search) or tool_refusal(tool, args)):
                return _deny("that file holds a credential; a coordinator never reads it"), None
            return None, None
        if tool == ANSWER_TOOL:   # the turn's structured answer (module doc)
            return _answer_check(payload), None
        if tool == "Bash":
            from .hook import local_guard
            why = bash_refusal(str(args.get("command") or "")) or local_guard(payload)
            return (_deny(why) if why else None), None
        return _deny(f"a session coordinator has read-only tools only; {tool or 'this tool'} is not one"), None
    except Exception as e:   # fail closed
        return _deny(f"the coordinator tool check failed ({type(e).__name__})"), None


def post_tool_use(payload: dict, failed: bool = False) -> tuple[dict | None, None]:
    """Put back what a full-tools coordinator's Bash call wrote to a protected file out of band (guard;
    its notice goes into the tool result), and log the coordinator's mutating call (Edit, Write, a Bash
    command off the read-only list) with its exit to the run's AUDIT file and progress.md (the web app's
    run view). A failed call's exit code is read from its error ("Exit code N"), else 1. Never blocks
    the call."""
    notice = None
    try:
        if os.environ.get(FULL_ENV) == "1" and payload.get("tool_name") == "Bash":
            notice = guard(payload)
    except Exception:
        pass
    out = {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure" if failed else "PostToolUse",
                                  "additionalContext": notice}} if notice else None
    try:
        run = os.environ.get("TTP_RUN_DIR")
        tool = str(payload.get("tool_name") or "")
        args = payload.get("tool_input") or {}
        if not run or os.environ.get(FULL_ENV) != "1" or payload.get("agent_id"):
            return out, None
        if tool == "Bash" and _records_why(str(args.get("command") or "")):
            return None, None   # `ttp audit why` itself: the record, not a change
        if tool == "Bash" and bash_refusal(str(args.get("command") or "")) is not None:
            what = str(args.get("command") or "")
        elif tool in EDITS:
            what = str(args.get("file_path") or args.get("notebook_path") or "")
        else:
            return out, None
        m = re.search(r"[Ee]xit code (\d+)", str(payload.get("error") or ""))
        code = (int(m.group(1)) if m else 1) if failed else 0
        from pathlib import Path
        from . import timefmt
        from . import coordfix
        from .project import durable_append
        said = " ".join(str(args.get("description") or "").split())[:300] if tool == "Bash" else ""
        why = coordfix.intent(run)   # the why, order and undo declared before it (coordfix.py)
        durable_append(Path(run) / AUDIT, json.dumps({"ts": time.time(), "tool": tool, "target": what[:2000],
                                                      "what": said or f"{tool} {what[:300]}", "exit": code,
                                                      **why}) + "\n")
        line = " ".join(what.split())[:200]
        tag = f" (why: {why['why'][:120]}; serves {why['serves']})" if why else " (no why recorded)"
        durable_append(Path(run) / "progress.md", f"{timefmt.clock()} coordinator {tool} exit {code}: {line}{tag}\n")
    except Exception:
        pass
    return out, None


def _records_why(cmd: str) -> bool:
    """True for a plain `ttp audit why ...` call: no unquoted shell operator and no substitution, so
    it runs nothing else (a chained or substituted command is logged as any other)."""
    if "$(" in cmd or "`" in cmd or "\n" in cmd or "\r" in cmd:
        return False
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split, lex.commenters = True, ""   # `#` read as a word: a `;` after it still counts
        words = list(lex)
    except ValueError:
        return False
    return words[:3] == ["ttp", "audit", "why"] and not any(w and set(w) <= set("();<>|&") for w in words)


def wake_budget(cfg: dict, turn_usd: float | None, turn_timeout_s: float) -> tuple[float | None, float]:
    """(dollars, seconds) one session wake may use: wake_budget_usd and wake_budget_s where set and
    positive, else the stateless turn's own (module doc)."""
    s = session(cfg)
    usd = positive(s.get("wake_budget_usd"), 0) or turn_usd
    secs = positive(s.get("wake_budget_s"), 0) or turn_timeout_s
    return usd, secs

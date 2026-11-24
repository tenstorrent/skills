# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free guard on the side effects of a run on the code arm's GLM side.

A GLM-arm run (codearm.py) builds what its spec decided and hands the result back; it does not talk to a
person or another project, write the project's records or touch anything outside its worktree. A prompt
rule cannot hold that for a cheaper model, so the harness enforces it in code, with no model call, on
every harness the arm runs:

The guard is a short explicit denylist of the harmful kinds measured on real GLM runs;
everything else runs, with Opus's plan and the post-run audit as the backstop (deviceplan.py):
- writes or deletes outside the run's worktree, run folder and scratch folder (a redirect, tee, a writing
  verb's target, a copy's destination; never inferred from interpreter code), any write an ssh call makes on
  another host (REMOTE_WHY: a device plan's own step may), a write into a project's tt-project state or harness
  folder unless it is the run's own worktree or run folder, and the guard's own files, its identity variables
  and the project's database and machine records (TAMPER, STATE_DB); protected and served paths are
  pathguard's (guards.protected_paths), on every run;
- srun, sbatch or salloc on held nodes and recorded undos of user-ordered changes: ruleguard's (slurm_nodes,
  user_undo), on every run;
- pushes and landing before review: a raw `git push`, `ttp land`, `ttp express`, `ttp push --land` (`ttp push`
  keeps its own checks and review gates);
- outward posts: `ttp notify`, `say`, `ask`, `upstream`, a note routed past the run's own log (NOTE_ROUTES),
  a `gh` write, an HTTP post, mail and raw sockets (nc, telnet);
- resets, power cycles and flashes outside the approved routes (deviceplan.power_refusal, and deviceplan.risky
  outside a device plan, whose steps need an Opus step check);
- machine-ledger writes: any `ttp machines` call but its reads;
- service state changes: `systemctl` start, stop, restart, enable, disable or mask and `service <name>` start,
  stop or restart anywhere in the command's text, heredoc bodies and ssh commands included (SERVICE_RE; a device
  plan's own step may);
- every connector tool, and a Write or Edit outside the run's folders (tool_refusal).
A script file run by its path (`bash s.sh`, `bash < s.sh`, `ssh box bash < s.sh`) is opaque to the guard: its
contents are not read, and neither is a `$(...)` in a heredoc body a program other than a shell reads. Opus's
plan and the post-run audit are the backstop there. Protected names are read in the command's raw text, all but a
quoted heredoc body (`<<'EOF'`) only written to a file; an unquoted body is checked like command text. A search for
one (`grep project.db`) is refused, so are any command but a plain reader naming the guard's own files
(`git log -S glm_guard.json`; `cat glm_guard.json` reads) and setting the guard flag (`TTP_CODE_ARM_GUARD=1`)
like any other mention, and so is a `$TMPDIR` write after interpreter code that names TMPDIR
(`os.environ["TMPDIR"]` reads as setting it).
The hook (hook.pre_tool_use, Claude Code and any provider with hooks), `ttp` itself (cli.glm_gate) and the
command layer of a harness without hooks (glmenv, for mini-swe-agent) ask it before each call.

A refused command never runs. refused() records it in the run's glm_refusals.jsonl and decides it by RULES, the one
table of refusal rules: a redirect (a database or record read, `ttp upstream`, a scratch file outside the run's
folders) shows the run the reason and the allowed way, and the run goes on; a harmful attempt (a device, machine
or service act, publishing or speaking, credentials, a write to a project's records or tree, a delete or overwrite
outside the run's folders), the run's third refusal (route.glm_refusals.hand_back_after) or redirects switched off
(route.glm_refusals.redirect false) end the run as a hand-back to Opus: hit() writes the run's glm_guard.json and
a `needs_opus` result.json (reason `guard: <what>`), the runner stops the agent at its next look and the daemon
requeues the task on Opus without spending an attempt (codearm.bounce_reason). deny_list() is the prompt's
short list of the same rules, each with its allowed way. The guard applies to
runs where active() holds: the daemon sets TTP_CODE_ARM_GUARD for a run whose arm is glm and writes MARK in
its run folder, so a run that unsets the variable is still guarded. Other runs are not touched.

Every run on the chat model, whatever route sent it there (run_env: the chat provider, a code arm's GLM run, a
routine coordinator lane), also gets these rules; the daemon sets RUN_ENV and writes RUN_MARK:
- the guard above, in the tool hook (hook._pre_tool_use), whatever route sent the run there;
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

from . import glmassist
from .coordtools import READ_COMMANDS

ENV = "TTP_CODE_ARM_GUARD"
MARK = "glm_guard.on"        # in the run folder: the daemon wrote it before launch
HIT = "glm_guard.json"       # in the run folder: {"why", "ts"}; the runner stops the agent when it exists
KEPT = "result.pre-guard.json"   # a hand-off the run wrote before the guard replaced it

# The ttp commands the denylist refuses outright: those that post outward or land a head (a `ttp machines`
# write is refused by read_only).
OUT_TTP = frozenset({"notify", "say", "ask", "upstream"})
LANDING = frozenset({"land", "express"})
# `ttp note` flags that send a note on, or change a record beyond the run's own log: refused on the GLM arm.
NOTE_ROUTES = ("--to", "--reply", "--urgent", "--severity", "--confirms")   # --status ID only reads
GH_READS = frozenset({"view", "list", "diff", "status", "checks"})
# read-only ttp queries, by word prefix: the coordinator's reads and the machine ledger's list
READS = READ_COMMANDS + (("machines", "change", "list"), ("machines", "change", "show"), ("occupancy",))
NET = frozenset({"nc", "ncat", "netcat", "telnet"})
COPIERS = frozenset({"scp", "rsync", "sftp"})
REMOTE_WHY = "it writes on another host"   # a device plan's own step may (deviceplan.refusal)
# A service started, stopped or switched on a box can take its devices from others: only a step of Opus's device
# plan may (deviceplan.refusal); a run with no plan never does. Read on the raw text, so a heredoc body or an ssh
# command is covered without parsing either.
SERVICE_WHY = "it changes a service's state"
SERVICE_RE = re.compile(r"\bsystemctl(?:\s+--?[\w-]+(?:=\S+)?)*\s+(?:start|stop|restart|enable|disable|mask)\b"
                        r"|\bservice\s+[\w@.-]+\s+(?:start|stop|restart)\b")
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
TAMPER = re.compile(   # an id after `$` or `${` is a value read (`export TMPDIR="$TTP_RUN_DIR/tmp"`), not a name set
    _AT + r"(?:(?:unset|export|declare|typeset|local|readonly|env|set|read|mapfile|readarray|for)\b[^;|&\n]*(?<!\w)(?<!\$)(?<!\$\{)"
    + _IDS + r"\b|printf\s+-v\s*" + _IDS + r"\b|(?:[A-Za-z_]\w*=\S*\s+)*" + _IDS + r"\+?=)"
    r"|\benv\s+-i\b", re.M)
# the guard's own files: a command line of plain readers only may name them (`cat <run>/glm_guard.json 2>&1`,
# another run's too); one with any other command is refused, and a write to one is a run record's write (writes_outside, _run_record)
GUARD_FILE = re.compile(r"\bglm_guard\.(?:on|json)\b")
# readers with no output-file form (`uniq IN OUT`, `sort -o`, `less -o` write one) and no command-running form
# (`rg --pre`, `find -exec`, `xargs` run one): only these may name the guard's files
GUARD_READERS = frozenset({"cat", "grep", "egrep", "fgrep", "head", "tail", "wc", "cut"})
SAME_ID = re.compile(r"\b(TTP_RUN_DIR|TTP_TASK|TTP_PROJECT|TTP_RUN_ID)=(['\"]?)([^\s'\"`$~\\;&|<>()]*)\2(?=[\s;&|)]|$)",
                     re.M)   # not TTP_CODE_ARM_GUARD: setting the guard switch stays refused
# A shell redirection (`2>&1`, `>out.log`, `<in`): its fd digit and target are no words of a ttp call.
# the target stops at a quote: `ssh h "ls > f"` keeps its closing quote, so the remote command stays one word
REDIRECT = re.compile(r"(?<![\w$])\d*(?:&>>?|>>?|<<?<?|>&|<&)\s*(?:&?\d+-?|[^\s;|&<>()'\"]+)?")
# A program name, a variable's name or a `sleep` time the guard cannot read: a command word that is an expansion
# (`$S 2`, `$(printf sleep)`, a backtick), `eval` of one, an indirect `${!n}` or `${${S}EC}`, `sleep $I`. Matched on
# the command-word view only (shell_refusal), so a quoted string or a quoted heredoc body naming one is text.
_CMD_AT = r"(?:[;&|(]|\b(?:do|then|else)\s)\s*"
HIDDEN = re.compile(rf"""(?m)(?:^\s*|{_CMD_AT})(?:["']?\\?\$(?:\{{\w+\}}|[A-Za-z_]\w*)["']?(?=[\s;&|)]|$)|\\?\$\()"""
                    rf"""|(?:^\s*|{_CMD_AT})["']?(?:[\w-]+\\?\$[{{A-Za-z_]|\\?\$\{{\w+\}}[\w-])"""
                    rf"""|{_CMD_AT}`|(?:^\s*|{_CMD_AT})eval\s+["'$\\]|\$\{{(?:\\?\$|!\w+\}})|(?<![\w.-])sleep\s+["']?\\?\$""")
HIDDEN_WHY = "a program name or a checked operand comes from an expansion the guard cannot read"
STATE_DB = re.compile(r"\bproject\.db\b|\bmachine-workers\.json\b|\b\.tt-project/(?:locks|machines)\b")
# Every run on the chat model (the code arm, a GLM work route, a routine coordinator lane, a bench run as a task):
# human interaction stays on Opus, so its outward posts become drafts an Opus step reviews and sends.
SCRATCH = "tmp"               # in the run folder: the run's own scratch folder, its TMPDIR (TMP, TEMP)
SCRATCH_VARS = ("TMPDIR", "TMP", "TEMP")
SCRATCH_LINE = ("Keep scratch files in $TMPDIR (a folder of your own in your run folder) or your worktree: write"
                ' `> "$TMPDIR/body.md"`, never a literal `/tmp/...` path, and never create files in the project\'s'
                " `state/` folder. The guard refuses writes anywhere else.")
# What a code run's own checklist (template kind-code.md) leaves out of its check before it hands off done: the
# review fails tests that pass without the change, and a large diff with no reason.
HANDOFF_LINE = ("Before you hand off done, check your own diff as the independent review will. Revert your fix once and"
                " see each new or changed test fail, then restore it; assert behaviour, not message wording; leave no"
                " unused test setup. If the diff stays large, add a `why this size:` line to your summary. Every claim"
                " in your summary cites the command or file that shows it.")
_SCRATCH_REF = re.compile(r"\$(?:\{(TMPDIR|TMP|TEMP)\}|(TMPDIR|TMP|TEMP)(?!\w))")
_SCRATCH_SET = re.compile(r"(?<![\w${])(?:TMPDIR|TMP|TEMP)(?!\w)")   # the command names one itself (sets it)
_SCRATCH_OWN = re.compile(r"(?<![\w${])(?:TMPDIR|TMP|TEMP)=([\"']?)([^\s;&|\"'`()]+)\1(?=[\s;&|]|$)")   # `TMPDIR=<value>`


def _sets_own_scratch(cmd: str, run_dir: str) -> bool:
    """Whether every naming of TMPDIR (TMP, TEMP) in `cmd` is `TMPDIR=<the run's own scratch folder>` (the folder
    itself or `$TTP_RUN_DIR/tmp`): that sets nothing but what the run already has, so `$TMPDIR` stays its value."""
    named = [m for m in _SCRATCH_SET.finditer(cmd)]
    own = [m for m in _SCRATCH_OWN.finditer(cmd)
           if _own_tmp(m.group(2).replace("${TTP_RUN_DIR}", run_dir).replace("$TTP_RUN_DIR", run_dir), run_dir)]
    return bool(named) and len(own) == len(named)


RUN_ENV = "TTP_GLM_RUN"       # "1", or DEVICE for a run on a device task
RUN_MARK = "glm_run.json"     # in the run folder: {"device", "gates", "plan", "cwd", "dirs"}, read where the variable is gone
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


def arm_env(note: dict | None) -> dict[str, str]:
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


# A refusal is a redirect or a hand-back (refused()). RULES is the one table of the guard's refusals: (rule, class,
# the reason's pattern, what the prompt tells a run never to do, the allowed way). A redirect shows the run the reason
# and the allowed way, and the run goes on. A harmful attempt (a device, machine or service act, publishing or
# speaking, credentials, a write to a project's records or tree, a delete or overwrite outside the run's folders), or
# the run's hand_back_after-th refusal (route.glm_refusals), ends the run as a hand-back to Opus (hit()). The first
# match wins; a reason no rule names is harmful.
HARMFUL, REDIRECTED = "harmful", "redirect"
REFUSALS = "glm_refusals.jsonl"   # in the run folder: one {ts, rule, class, why, n, back} per refusal
# The run folder's own records: the rest of it is the run's to write (`> "$TTP_RUN_DIR/advise.out"`)
TMP_MADE = "glm_tmp.txt"   # in the run folder: the system temp dir entries the run made, one per line (keep_tmp)
RUN_RECORDS = frozenset({"run.json", "refusals.jsonl", RUN_MARK, MARK, HIT, REFUSALS, TMP_MADE})
GUARD_WHY = "a command may not edit the run's identity or the guard's own files"


def _run_record(path: str, run_dir: str) -> bool:
    """Whether `path` is one of the run folder's own records (RUN_RECORDS)."""
    return bool(run_dir) and os.path.basename(path) in RUN_RECORDS \
        and os.path.realpath(os.path.dirname(path)) == os.path.realpath(run_dir)
SECRET_WHY = "a run on the GLM arm may not read stored secrets or credential files"
SECRET_TEXT_WHY = "a script's text names a key or credential file, which may read it"
REFUSAL_KEY = "glm_refusals"      # route.glm_refusals: {"hand_back_after": 3, "redirect": true}
HAND_BACK_AFTER = 3
_OUTSIDE = "outside its worktree"
_OWN_TREE = ("change only files in your worktree and `$TMPDIR`, and say in your hand-off "
             "what else should change")
RULES = (
    ("expansion", REDIRECTED, re.escape(HIDDEN_WHY), "build a program name, a variable's name or a `sleep` time "
     "from a variable, `$(...)` or `eval`", "write the program and its operands literally"),
    ("db_read", REDIRECTED, r"database or machine records by hand",
     "open the project's database or machine records (sqlite or any other tool)",
     "read them with `ttp status`, `ttp task <project> show <id>` (or `list`), `ttp machines list` and the run "
     "folders' `state/runs/<run>/result.json`"),
    ("upstream", REDIRECTED, r"`ttp upstream`", "run `ttp upstream`",
     "write the note as a hand-off follow-up titled `upstream: ...`"),
    ("secret_text", REDIRECTED, re.escape(SECRET_TEXT_WHY), "",   # the credentials line covers it in the prompt
     "leave key and credential paths out of test strings"),
    ("scratch", REDIRECTED, _OUTSIDE, "write to a literal `/tmp/...` path or create files outside your worktree",
     'write scratch files under `"$TMPDIR"` (a folder of your own) or in your worktree'),
    ("records", HARMFUL, None, "write in a project's `state/` folder, another task's worktree or another project, or "
     "delete or overwrite a file outside your worktree and `$TMPDIR`", _OWN_TREE),
    ("delete", HARMFUL, None, None, _OWN_TREE),
    ("outward", HARMFUL, r"`ttp (?:notify|say|ask|note)\b", "post outward (`ttp notify`, `ttp say`, "
     "`ask`, `ttp note --to`/`--reply`)", 'put what a person should read in your hand-off summary; a plain `ttp note "<text>"` '
     "goes to your task's own log"),
    ("publish", HARMFUL, r"lands a head|never `git push`", "run `git push`, `ttp land` or `ttp express`",
     "commit on your branch and hand off done; the review publishes the head"),
    ("post", HARMFUL, r"post to GitHub|send data to a service|reaches a person or another service|connector tool",
     "post to GitHub (`gh` writes), send HTTP posts or mail, or call a connector tool",
     "put the text in your hand-off summary; `gh pr view` and `gh pr diff` only read and run"),
    ("machines", HARMFUL, r"machine ledger|`ttp remove`", "change the machine ledger (`ttp machines` writes) or "
     "remove a project", "say in your hand-off what changed; read the ledger with `ttp machines list`"),
    ("device", HARMFUL, r"^out of plan|power cycle|reset|flash|firmware|" + re.escape(SERVICE_WHY) + "|"
     + re.escape(REMOTE_WHY), "reset, power-cycle, flash or reboot a device, start or stop a service, or write on "
     "another host", "on a device task these run only as steps of Opus's device plan; otherwise hand off and say "
     "what is needed"),
    ("credentials", HARMFUL, r"secrets|credential|identity or the guard's own files|run a local program",
     "read stored secrets, keys or credential files, or change the run's `TTP_*` variables",
     "hand off and say which access the task needs"),
    ("other", HARMFUL, r"", None, "hand off and say what the task needs"),
)
_DELETES = re.compile(r"\b(?:rm|rmdir|shred|unlink|truncate|chmod|chown|chgrp) target\b|\bmv source\b|\bdd of=|sed -i")
_NEW_ONLY = re.compile(r"\b(?:mkdir|touch) target\b")
_TMP = ("/tmp", "/var/tmp")


def _local_rule(why: str) -> str:
    """The rule of a refused local write (`it writes outside its worktree: <path> (<how>)`): a delete or overwrite,
    a project's records or tree, else a new file or folder (scratch)."""
    m = re.match(r"it writes outside its worktree: (.+?)(?: \(([^()]*)\))?$", why.strip())
    path, how = (m.group(1), m.group(2) or "") if m else ("", "")
    if _DELETES.search(how):
        return "delete"
    q = Path(path)
    if ".tt-project" in q.parts or records_write(path, os.path.expanduser("~")) or any(
            (a / "tt-project" / "state").is_dir() for a in list(q.parents)[:-1]):
        return "records"
    if path in _TMP or path.startswith(tuple(t + "/" for t in _TMP)):
        return "scratch"
    return "delete" if path and os.path.lexists(path) and not _NEW_ONLY.search(how) else "scratch"


def classify(why: str) -> tuple[str, str, str]:
    """(rule, class, allowed way) of a refusal's reason, from RULES."""
    by = {r[0]: r for r in RULES}
    key = next(r[0] for r in RULES if r[2] is not None and re.search(r[2], why or ""))
    if key == "scratch":
        key = _local_rule(why)
    return key, by[key][1], by[key][4]


def refusal_settings(cfg: dict | None = None) -> dict:
    """route.glm_refusals: `redirect` false (the off switch) hands the task back on the first refusal, as before
    redirects; `hand_back_after` (default 3) is the refusal count in one run that hands it back."""
    if cfg is None:
        from . import deviceplan
        cfg = deviceplan._cfg()
    g = ((cfg or {}).get("route") or {}).get(REFUSAL_KEY)
    g = g if isinstance(g, dict) else {}
    try:
        n = max(1, int(g.get("hand_back_after", HAND_BACK_AFTER)))
    except (TypeError, ValueError):
        n = HAND_BACK_AFTER
    return {"redirect": g.get("redirect") is not False, "hand_back_after": n}


def refusals(run_dir) -> list[dict]:
    """The refusals a run recorded (REFUSALS), oldest first."""
    return glmassist.rows(Path(run_dir) / REFUSALS) if run_dir else []


def refused(run_dir, why: str, kind: str = "guard", cfg: dict | None = None) -> str:
    """Record a refusal (REFUSALS) and decide it: the text the run reads (hit_reason() tells a hand-back). A redirect's text is the
    reason and the allowed way, and the run goes on; a harmful attempt, the run's hand_back_after-th refusal, or
    redirects switched off end the run as a hand-back to Opus (hit()). The refused command never runs either way."""
    why = (why or "").strip()
    rule, cls, instead = classify(why)
    s = refusal_settings(cfg)
    n = len(refusals(run_dir)) + 1
    back = cls == HARMFUL or not s["redirect"] or n >= s["hand_back_after"]
    if run_dir:
        try:
            from .project import durable_append
            durable_append(Path(run_dir) / REFUSALS, json.dumps(
                {"ts": round(time.time(), 3), "rule": rule, "class": cls, "why": why[:280], "n": n, "back": back},
                sort_keys=True) + "\n")
        except OSError:
            pass
    if back:
        hit(run_dir, why if cls == HARMFUL or n == 1 else f"refusal {n} in one run: {why}", kind)
        return f"refused: {why}; the run ends and its task goes back to Opus"
    left = s["hand_back_after"] - n
    return (f"refused, not run: {why}.\nInstead: {instead}. Carry on with the task; "
                   f"{left} more refusal{'s' if left > 1 else ''} in this run hand{'' if left > 1 else 's'} it "
                   "back to Opus.")


def deny_list() -> str:
    """The prompt's short deny list, one line per rule with its allowed way, built from RULES so the two agree."""
    return ("## What the guard refuses, and the allowed way\nA refused command does not run: you read why and the "
            "allowed way, and carry on. A harmful attempt (marked) or a third refusal in one run hands the task "
            "back to Opus.\n" + "\n".join(f"- Never {r[3]}: {r[4]}." + (" (harmful)" if r[1] == HARMFUL else "")
                                          for r in RULES if r[3]))


def note_route(flags) -> str:
    """The first of `flags` (option words of a `ttp note` call, argparse prefixes included) that routes the
    note past the run's own log, else ''."""
    for w in flags:
        opt = str(w).split("=", 1)[0]
        if len(opt) > 2 and opt.startswith("--") and any(o.startswith(opt) for o in NOTE_ROUTES):
            return next(o for o in NOTE_ROUTES if o.startswith(opt))
    return ""


def read_only(words: list[str], flags=()) -> bool:
    """True for a read-only ttp call: its positional `words` (no `ttp`, no leading project name) start with an entry
    of READS, or are the machines call that lists its records (the one that takes at most a machine name, no option but --json).
    Any other call, an unknown subcommand too, is False."""
    if words[:2] == ["machines", "outage"]:
        return len(words) <= 3 and all(f == "--json" for f in flags)
    return any(words[:len(r)] == list(r) for r in READS)


def cli_refusal(cmd: str, action: str = "", flags=(), words=()) -> str | None:
    """Why a GLM-arm run may not run `ttp <cmd> [<action>]` with option words `flags` (and the call's positional
    `words` from the command on, where known), else None: the denylist's ttp kinds only."""
    if cmd in OUT_TTP or cmd == "note" and note_route(flags):
        what = f"`ttp {cmd}" + (f" {note_route(flags)}" if cmd == "note" else "") + "`"
        return (f"{what} is not allowed to a run on the GLM arm: its notes go only to its own task log, "
                f"and the coordinator decides what reaches a person, a chat or another project")
    if cmd in LANDING or cmd == "push" and any(str(f).split("=", 1)[0] in ("--land", "--express") for f in flags):
        return f"`ttp {cmd}` lands a head: a run on the GLM arm publishes only with `ttp push`, after review"
    if cmd == "rollout" and action not in ("", "status") and not {"-h", "--help"} & set(flags):
        return ("`ttp rollout " + action + "` changes a rollout: not for a run on the GLM arm; read it with "
                "`ttp rollout status`")
    if cmd == "remove":
        return "`ttp remove` deletes a project, outside the run's worktree and run folder: not for a run on the GLM arm"
    if cmd == "machines" and not {"-h", "--help"} & set(flags) and not read_only(list(words) or [cmd, action], flags):
        return ("a run on the GLM arm does not write the machine ledger (`ttp machines` writes): say in the "
                "hand-off what it changed on a machine, and Opus records it")
    return None


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
    if len(words) > 1 and (project and words[0] == project or words[0] not in _ARG_TAKERS and cli_refusal(words[1])):
        words = words[1:]   # `ttp <project> <command>`: the command after a project's name
    return words


# Commands a run uses whose next word is an argument, never a command (`ttp memory --search notify`)
_ARG_TAKERS = frozenset({c[0] for c in READ_COMMANDS} | {"checks", "lock", "detach", "push", "landed", "task"}
                        | {"memory", "obligation", "config", "slop", "clip", "sandbox", "advise", "audit", "note"})


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


def _root_paths(folders) -> list[str]:
    """Each folder of `folders` as the guard compares paths: its normalized and its real path, empties dropped."""
    out = []
    for x in folders:
        if x:
            out += [os.path.normpath(x), os.path.realpath(x)]
    return out


def _roots(cwd: str, run_dir: str, scratch: str = "", env=None) -> list[str]:
    """The folders a GLM run may write in: its shell's cwd, run folder, scratch and the folder it started in (a
    `cd` into a sibling folder moves the shell's cwd, never that start) and the folders its spec names (spec_dirs)."""
    run = (glm_run(env) if env is not None else None) or {}
    return _root_paths((cwd, run_dir, scratch, run.get("cwd", ""), *run.get("dirs", [])))


def _own_roots(cwd: str, run_dir: str, env=None) -> list[str]:
    """The run's own folders a records write may still land in: its worktree (the folder it started in, its
    shell's cwd only while no start is recorded: a `cd` never makes a folder its own) and its run folder."""
    run = (glm_run(env) if env is not None else None) or {}
    return _root_paths((run.get("cwd") or cwd, run_dir))


RECORDS_DIRS = ("state", "harness")   # the folders a project's tt-project folder keeps its records and rules in


def records_write(path: str, home: str) -> bool:
    """Whether `path` is inside a project's tt-project state or harness folder under `home`: a write there is a
    write in a project's records or rules, whatever folder the run's spec named. Above `home` sits the run's own
    scratch tree, never a project's records."""
    parts = Path(path).parts
    if home:
        h = Path(home).parts
        parts = parts[len(h):] if parts[:len(h)] == h else ()
    return any(parts[i] == "tt-project" and parts[i + 1] in RECORDS_DIRS for i in range(len(parts) - 1))


def _outside_roots(path: str, roots: list[str], own: list[str], home: str) -> bool:
    """Whether a write to `path` lands outside the run's write roots: a write into a project's tt-project state
    or harness folder is outside them unless it is inside the run's own worktree or run folder (`own`). The
    literal path decides: what a link inside the run's own folders points at is the post-run audit's to read."""
    from . import sidefx
    return not sidefx._inside(path, roots) or (records_write(path, home) and not sidefx._inside(path, own))


def _remote_dest(seg: list[str]) -> str:
    """The remote destination of a copy (`scp`/`rsync` last argument, `sftp` host), '' for a local one."""
    args = [a for a in seg[1:] if not a.startswith("-")]
    if os.path.basename(seg[0]) == "sftp":
        return args[0] if args else ""
    return args[-1] if len(args) > 1 and re.match(r"^[^/:]+:", args[-1]) else ""


# a command that makes a link (`ln -s ~/.bashrc /tmp/n; echo x >> /tmp/n`): its temp dir writes stay outside
_LINKS = re.compile(r"(?<![\w./-])(?:ln\s|cp\s(?:[^;&|\n]*\s)?-[A-Za-z]*s)")


def _tmp_entry(path: str, made: set[str]) -> str:
    """The system temp dir entry (`/tmp/<name>`) a write to `path` lands in when it is the run's own: a plain name
    that does not exist yet, or one the run made (`made`) that is no link and that `path` still resolves inside;
    '' for any other path, an existing temp file it did not make, a glob or variable (`/tmp/*`) and a link out of
    the entry too, even to another temp entry."""
    path = os.path.normpath(path)
    for t in _TMP:
        if path.startswith(t + "/"):
            name = path[len(t) + 1:].split("/")[0]
            top = os.path.join(t, name)
            if not re.fullmatch(r"[\w.@%+=:,-]+", name):
                return ""
            if top in made:
                real, there = os.path.realpath(top), os.path.realpath(path)
                return top if not os.path.islink(top) and (there == real or there.startswith(real + "/")) else ""
            return "" if os.path.lexists(top) else top
    return ""


def _tmp_made(run_dir: str) -> set[str]:
    """The system temp dir entries the run made (TMP_MADE)."""
    try:
        return set((Path(run_dir) / TMP_MADE).read_text().split("\n")) - {""} if run_dir else set()
    except OSError:
        return set()


def keep_tmp(made: list[str]) -> None:
    """Record in the live run's own folder (TTP_RUN_DIR) the system temp dir entries a command it is about to run
    makes: the tool hook's step once it lets the command run, never a check's (every check writes nothing)."""
    run_dir = os.environ.get("TTP_RUN_DIR")
    if made and run_dir:
        try:
            with open(Path(run_dir) / TMP_MADE, "a") as f:
                f.write("".join(t + "\n" for t in made))
        except OSError:
            pass


def writes_outside(cmd: str, cwd: str, run_dir: str = "", env=None, inline: bool = False,
                   made: list[str] | None = None) -> list[tuple[str, str]]:
    """(path, why) for each file `cmd` writes or deletes outside the run's worktree, run folder and scratch folder
    (a device and an fd aside). Writes read from interpreter code count only with `inline` (the post-run scan).
    A write in a system temp dir entry of the run's own (_tmp_entry) is no such write, but in a device run, the
    post-run scan (`inline`) or a command that makes a link (_LINKS): `made` gets each new entry."""
    from . import sidefx
    e = os.environ if env is None else env
    home = os.path.expanduser("~")
    roots, own = _roots(cwd, run_dir, _scratch(run_dir, e), e), _own_roots(cwd, run_dir, e)
    # $TMPDIR (TMP, TEMP) is read as its value in the run's environment, or as /tmp where the command sets it itself
    told = bool(_SCRATCH_SET.search(cmd))
    own_tmp = _scratch(run_dir, e) if told and _sets_own_scratch(cmd, run_dir) else ""   # `TMPDIR=<its own folder>`
    paths = _SCRATCH_REF.sub(lambda m: own_tmp or ("/tmp" if told else str(e.get(m.group(1) or m.group(2)) or "/tmp")), cmd)
    got = [(p, why) for p, (cls, why) in sidefx.classify_paths(paths, home, cwd or os.getcwd(),
                                                                run_dir).items()
           if cls == "write" and (inline or "inline code" not in why) and not p.startswith(sidefx.HARMLESS)
           and (_outside_roots(p, roots, own, home) or _run_record(p, run_dir))]
    mine = not inline and not _LINKS.search(cmd) and not (glm_run({**e, "TTP_RUN_DIR": run_dir or e.get("TTP_RUN_DIR") or ""}) or {}).get("device")
    ours = _tmp_made(run_dir) if mine else set()   # a device run's scratch is its plan's folder (deviceplan)
    tops = {p: mine and not _run_record(p, run_dir) and _tmp_entry(p, ours) for p, _ in got}
    if made is not None:
        made += sorted({t for t in tops.values() if t} - ours)
    return [(p, why) for p, why in got if not tops[p]]


def remote_writes(cmd: str, cwd: str = "") -> list[tuple[str, str]]:
    """(path, why) for each file a command run on another host writes there by an absolute or home path (a device,
    an fd and a path relative to the run's folder there aside)."""
    from . import sidefx
    there, home = "/@remote-cwd", "/@remote-home"   # a relative path there is under the run's own folder on that host
    return [("~" + p[len(home):] if p == home or p.startswith(home + "/") else p, why)
            for p, (cls, why) in sidefx.classify_paths(cmd, home, there, "").items()
            if cls == "write" and "inline code" not in why and not p.startswith((there,) + sidefx.HARMLESS)]


# Code that can open a file by a name it holds: a quoted interpreter-fed body without one only names a key
OPENER_RE = re.compile(
    r"\b(?:open|fopen|read|readline|readlines|readFile\w*|read_text|read_bytes|load|loads|slurp)\s*\("
    r"|\b(?:open|subprocess|child_process|Popen|popen|system|exec|execv\w*|execl\w*|eval|spawn\w*|check_output|check_call"
    r"|qx|File|IO|Pathname|glob|shutil|copy\w*|rename|move|replace|link|symlink|tarfile|zipfile|requests|urlopen|urllib\w*"
    r"|socket|http\w*|curl|wget|getline|\w*(?:Stream|Sync))\b|`")


def code_text(cmd: str, interp: bool = False, py_code: bool = False) -> str:
    """`cmd` without the heredoc bodies that are only data: a quoted body (`<<'EOF'`, no expansion) only written to a
    file (`cat > t.py <<'EOF' ... project.db ...`) opens nothing; an unquoted one runs its `$(...)` and backticks,
    and one a shell or interpreter reads runs, so they stay. `interp`: a quoted body that a script interpreter reads
    (python, node, perl) also goes when it has no call that opens a file (OPENER_RE): a test string naming a key.
    `py_code`: a quoted body python reads keeps only its code (_py_without_text)."""
    from . import hook, pathguard
    lines, bodies = hook._heredocs(cmd)

    def data(i: int, pos: int, body: list[str]) -> bool:
        if hook._data_body(lines, i, pos):
            return True
        if not (interp and hook.HEREDOC_RE.match(lines[i], pos)[2]) or OPENER_RE.search("\n".join(body)):
            return False
        return bool(pathguard.INTERPRETER_RE.match(hook._first_word(hook._piece_at(lines[i], pos)[0])))

    def python(i: int, pos: int) -> bool:
        return bool(py_code and hook.HEREDOC_RE.match(lines[i], pos)[2]
                    and re.fullmatch(r"python[\d.]*", hook._first_word(hook._piece_at(lines[i], pos)[0])))
    return "\n".join(lines + [ln for i, body, pos in bodies if not data(i, pos, body)
                              for ln in (_py_without_text(body) if python(i, pos) else body)])


# Not pathguard.RUN_CODE_RE: that one also matches `run` and backticks, which would keep nearly every python script
# whole, and lacks eval/compile/runpy, which run python text.
_RUNS_TEXT = re.compile(r"\b(?:exec\w*|eval|compile|system|popen|Popen|subprocess|spawn\w*|check_output|check_call|runpy)\b")


def _py_without_text(body: list[str]) -> list[str]:
    """A python script without its comments and multi-line string literals (docstrings, code it writes into a file):
    text no call reads as a path. Its one-line literals stay (`connect('/x/project.db')` opens that file); a script
    tokenize cannot read stays whole, and so does one that runs text (exec, a shell, a subprocess): a literal may be code."""
    # shortcut: a multi-line literal later used as a database path (`p = """/x/project.db"""`, then `connect(p)`) is
    # blanked and passes; the guard is a denylist, not a parser, and the post-run audit backs it.
    import io
    import tokenize
    src = "\n".join(body) + "\n"
    if _RUNS_TEXT.search(src):
        return body
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return body
    starts = [0]
    for ln in src.split("\n"):   # tokenize's lines: only a newline ends one
        starts.append(starts[-1] + len(ln) + 1)
    for t in reversed(toks):
        if t.type == tokenize.COMMENT or t.type == tokenize.STRING and "\n" in t.string:
            a, b = starts[t.start[0] - 1] + t.start[1], starts[t.end[0] - 1] + t.end[1]
            src = src[:a] + "''" * (t.type == tokenize.STRING) + src[b:]
    return src.split("\n")


_ARITH = re.compile(r"\$\(\((?=(?:[^()]|\([^()]*\))*\)\))")   # `$((` that closes with `))`: arithmetic, no command


_SED_SCRIPT = re.compile(r"""(\bsed\b(?:\s+(?:-[-\w=.]+|--\w[\w-]*=\S+))*\s+(?:-e\s+)?)'([^']*)'""")


def _inert(view: str) -> str:
    """`view` read as bash does: `$((1+2))` is arithmetic, so its `(` starts no command, and the single-quoted script
    of each `sed` is text (`s|a|"$s" b|` names an expansion, runs nothing) unless an `e` flag or command runs it."""
    view = _ARITH.sub("$[", view)
    return _SED_SCRIPT.sub(lambda m: m[0] if re.search(r"[|/#,!]\w*e\w*\s*$|(?<![A-Za-z])e\b", m[2]) else f"{m[1]}''", view)


def shell_refusal(cmd: str, cwd: str, run_dir: str = "", env=None, remote: bool = False, writes: bool = True,
                  power: bool = True, services: bool = True, made: list[str] | None = None) -> str | None:
    """Why a shell command of a GLM-arm run is refused (before it runs), else None. The denylist: writes or
    deletes outside the run's worktree and run folder (and on another host), pushes and landing, outward
    posts, machine-ledger writes, service starts and stops (SERVICE_RE) and, with `power`, resets,
    power cycles and flashes outside the approved routes; the hook adds protected paths, held Slurm nodes and
    replayed undos (pathguard, ruleguard). Everything else runs. `remote`: the command an ssh call runs on another
    host. `writes` and `services`: False skips those checks (a step of Opus's plan, deviceplan._static). `made`
    gets the system temp dir entries an allowed command makes (writes_outside); nothing is written here."""
    from . import credguard, hook, pathguard
    e = os.environ if env is None else env
    lines, bodies = hook._heredocs(cmd)
    named = code_text(cmd, py_code=True)
    # the command words: the quoted text of commands that only print, search for or record it, and their quoted
    # heredoc bodies, gone (a commit message, a grep pattern or an echo naming a command runs none), and so is a
    # quoted body a script interpreter reads (`python3 - <<'EOF'`: its code, not shell); every other body stays
    view = "\n".join([hook._split(cmd)[0]] + [
        ln for i, body, pos in bodies if not hook._data_body(lines, i, pos) and not (
            hook.HEREDOC_RE.match(lines[i], pos)[2]
            and pathguard.INTERPRETER_RE.match(hook._first_word(hook._piece_at(lines[i], pos)[0])))
        for ln in body])
    # an identity set to the value it already has (`TTP_PROJECT=<this run's TTP_PROJECT> ttp ...`) edits nothing
    same = SAME_ID.sub(lambda m: " " * len(m[0]) if m[3] and m[3] == e.get(m[1]) else m[0], view)
    if TAMPER.search(same) or GUARD_FILE.search(same) and not all(   # every command a reader: `rm $F` can't hide one
            os.path.basename(w[0]) in GUARD_READERS | {"echo", "printf", "for"} for w in hook._all_segments(hook._split(same)[0]) if w):
        return GUARD_WHY
    if STATE_DB.search(named):
        return "a run on the GLM arm may not read or write the project's database or machine records by hand"
    if services and (svc := SERVICE_RE.search(view)):
        return f"{SERVICE_WHY}: {svc.group(0)[:120]}"
    if writes and (m := HIDDEN.search(_inert(view))):
        return f"{HIDDEN_WHY}: {m.group(0).strip()[:80]}"
    commands, _ = hook._split(cmd)   # heredoc bodies a program, not a shell, reads are its input
    for sub in pathguard._substitutions(commands):   # "$(...)" runs a command too
        if why := shell_refusal(sub, cwd, run_dir, e, remote, writes, power, services, made):
            return why
    segs = hook._all_segments(commands) + hook._segments(commands, strip=False)
    project = _own_name(e)
    # `ttp checks 2>&1`: the 2 is no word of the call; a backtick substitution runs a command of its own, like `$(...)`
    plain, _ = hook._split(REDIRECT.sub(" ", commands).replace("`", ";"))   # heredoc bodies no shell reads: gone
    for seg in hook._all_segments(plain) + hook._segments(plain, strip=False):
        words = _ttp_args(seg, project)
        if words and (why := cli_refusal(words[0], (words[1:2] or [""])[0], [w for w in seg if w.startswith("-")],
                                         words)):
            return why
    for seg in segs:
        if not seg:
            continue
        name = os.path.basename(seg[0])
        if _ttp_args(seg, project) is not None:
            continue   # read above, without its redirections
        elif name == "git" and _git_word(seg[1:]) == "push":
            return "a run on the GLM arm publishes only with `ttp push` (after its checks), never `git push`"
        elif name == "gh" and _gh_posts(seg):
            return "a run on the GLM arm does not post to GitHub (comments, reviews, PRs, issues)"
        elif name in HTTP and HTTP_DATA.search(" ".join(seg[1:])) and not any(
                w.split("=", 1)[0] in ("--unix-socket", "--abstract-unix-socket") for w in seg):
            return "a run on the GLM arm does not send data to a service (an HTTP post)"   # a local socket's API: no
        elif name in MAIL or name in NET:
            return f"`{name}` reaches a person or another service: not for a run on the GLM arm"
        elif name in ("ssh", "scp", "sftp") and credguard.ssh_runs_local(seg):
            return f"`{name}` options that run a local program (ProxyCommand and the like) would get past this guard"
        elif name in COPIERS and writes and (dest := _remote_dest(seg)):
            return f"{REMOTE_WHY}: {dest} (a copy)"
        elif name == "ssh":
            from . import deviceplan
            got = deviceplan.ssh_target(seg)
            if got and got[1].strip() and (why := shell_refusal(got[1], cwd, run_dir, e, True, writes, power, services)):
                return why
    if power:
        from . import deviceplan
        s = deviceplan.settings(deviceplan._cfg())
        if why := deviceplan.power_refusal(cmd, s["power_routes"]):
            return why
        if deviceplan.risky(cmd) and not any(deviceplan._matches(w[0], s["power_routes"]) for w in
                                             (x[0] for x in deviceplan._words(cmd) if x)):
            return ("a reset, power cycle, flash or firmware step runs only through the project's approved routes "
                    "or as a checked step of an Opus device plan: " + cmd[:120])
    if not writes:
        return None
    if remote:
        got = remote_writes(cmd)
        return f"{REMOTE_WHY}: {got[0][0]} ({got[0][1]})" if got else None
    got = writes_outside(cmd, cwd, run_dir, e, made=made)
    if rec := next((p for p, _ in got if _run_record(p, run_dir)), None):
        return f"{GUARD_WHY}: {rec}"
    return f"it writes outside its worktree: {got[0][0]} ({got[0][1]})" if got else None


def tool_refusal(payload: dict, env=None, made: list[str] | None = None) -> str | None:
    """Why a hook payload (the tool name, its input, the cwd) of a GLM-arm run is refused, else None: the
    Bash command checks above (`made` as there), a file write outside the worktree and run folder, any connector tool."""
    from . import sidefx
    e = os.environ if env is None else env
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd, run_dir = str(payload.get("cwd") or os.getcwd()), str(e.get("TTP_RUN_DIR") or "")
    if tool == "Bash":
        return shell_refusal(str(args.get("command") or ""), cwd, run_dir, e, made=made)
    if tool.startswith("mcp__"):
        return "a connector tool reaches a person or a service outside the hand-off"
    if tool in WRITE_TOOLS:
        home, roots = os.path.expanduser("~"), _roots(cwd, run_dir, _scratch(run_dir, e), e)
        own = _own_roots(cwd, run_dir, e)
        paths = sidefx._arg_paths(args) + sidefx._patch_paths(json.dumps(args) if tool == "apply_patch" else
                                                              str(args.get("input") or args.get("patch") or ""))
        for p in paths:
            full = sidefx._expand(p, home, cwd, run_dir)
            if _run_record(full, run_dir):
                return f"{GUARD_WHY}: {full}"
            if _outside_roots(full, roots, own, home):
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
          or bool(arm_env(note)) or bool(coordroute.guard_env(note)))
    return {RUN_ENV: DEVICE if device else "1"} if on else {}


def device_task(task: dict | None, cfg: dict | None = None) -> bool:
    """Whether `task` is device work: labelled `needs_device`, or a resource it names (its resource, exclusive
    and read labels) is a device lock (`*-device`, or one of `cfg`'s device locks) or a machine the machine list
    tags `device`. A CPU-only machine or any other resource is not; an unreadable machine list counts as device
    work, so the stricter guard holds."""
    from . import effort
    from .coordinator import task_resources
    from .util import task_labels
    t = task or {}
    if "needs_device" in task_labels(t):
        return True
    names = task_resources(t)
    return bool(effort.device_resources(names, cfg, unreadable_is_device=True))


def gate_lines(spec: str) -> list[str]:
    """The commands of the `check:` and `gate:` lines of a task spec (or an arm's plan)."""
    return [" ".join(m.split()) for m in GATE_LINE.findall(spec or "")][:20]


SPEC_PATH = re.compile(r"(?<![\w.~/$-])(?:~|/home)/[\w.@+-]+(?:/[\w.@+-]+)+")


# the work folders of ~/.tt-project a spec may name (its runtime, locks and check slots are the harness's)
HOME_WORK = {(".tt-project", "model-bench"), (".tt-project", "scratch")}


WALK_CAP = 20000  # folders read below a spec-named folder looking for a tt-project folder inside it (a bench set runs to thousands)


def project_dir(p: Path) -> bool:
    """Whether `p` is a project's tt-project folder: one that keeps its state or harness folder."""
    return p.name == "tt-project" and ((p / "state").is_dir() or (p / "harness").is_dir())


def holds_project(d: Path, home: Path) -> bool:
    """Whether `d` is in, holds at any depth, or is a project's tt-project folder: never a write root. `home`
    bounds the read of what holds `d`; a tree too big to read counts as holding one, so it gets no root."""
    if project_dir(d):
        return True
    for a in d.parents:                     # in: a folder below `home` holds d inside its tt-project folder
        if a == home:
            break
        if project_dir(a / "tt-project"):
            return True
    seen = 0
    for root, dirs, _ in os.walk(d):         # holds: a tt-project folder sits anywhere below `d`
        seen += 1
        if "tt-project" in dirs and project_dir(Path(root) / "tt-project"):
            return True
        if seen > WALK_CAP:                  # too big to read: held back rather than granted unseen
            return True
    return False


def spec_dirs(spec: str, base) -> list[str]:
    """The existing work folders a task's spec names as its work (`extend ~/x/bench/run.py`: ~/x/bench), roots
    of its writes: only the work folders of ~/.tt-project (HOME_WORK, never the bench's own harness folder), and
    never one in, or holding at any depth, a project's tt-project folder (`base` is this project's)."""
    from .benchrun import HARNESS_DIR
    home, out, seen = Path.home(), [], set()
    for m in SPEC_PATH.findall(spec or ""):
        q = Path(os.path.expanduser(m))
        d = q if q.is_dir() else q.parent
        if ".." in m.split("/") or d in seen:   # each folder is read once, however many of its files the spec names
            continue
        seen.add(d)
        try:
            rel = d.resolve().relative_to(home.resolve()).parts
        except (OSError, ValueError):
            continue
        if (len(rel) < 3 or rel[:2] not in HOME_WORK
                or rel[:3] == HARNESS_DIR.parts[-3:]
                or not d.is_dir() or "tt-project" in rel or Path(base).resolve().is_relative_to(d.resolve())
                or holds_project(d.resolve(), home.resolve())):
            continue
        out.append(str(d))
    return sorted(set(out))[:10]


def mark_run(run_dir, device: bool, gates=(), plan: bool = False, cwd: str = "", dirs=()) -> None:
    """The daemon's marker for a run on the chat model (read by glm_run() where the variable is gone); `plan`: a
    device run under an Opus plan (deviceplan.py); `cwd`: the folder it starts in and `dirs` the ones its spec names
    (spec_dirs), roots of its writes (_roots)."""
    from .project import durable_write
    durable_write(Path(run_dir) / RUN_MARK, json.dumps({"device": bool(device), "gates": list(gates),
                                                       **({"plan": True} if plan else {}), **({"cwd": cwd} if cwd else {}),
                                                       **({"dirs": list(dirs)} if dirs else {})}))


def glm_run(env=None) -> dict | None:
    """{"device", "gates", "plan", "cwd", "dirs"} for a run on the chat model, None for any other run."""
    e = os.environ if env is None else env
    rd = str(e.get("TTP_RUN_DIR") or "")
    try:
        got = json.loads((Path(rd) / RUN_MARK).read_text()) if rd else None
    except (OSError, ValueError):
        got = None
    if isinstance(got, dict):
        gates = got.get("gates") if isinstance(got.get("gates"), list) else []
        return {"device": bool(got.get("device")) or e.get(RUN_ENV) == DEVICE, "gates": [str(g) for g in gates],
                "plan": got.get("plan") is True, "cwd": str(got.get("cwd") or ""),
                "dirs": [str(d) for d in got.get("dirs") or [] if isinstance(d, str)]}
    if e.get(RUN_ENV) in ("1", DEVICE) or active(e):
        return {"device": e.get(RUN_ENV) == DEVICE, "gates": [], "plan": False}
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
    plain, _ = hook._split(REDIRECT.sub(" ", cmd), strip=False)   # a message's text stays
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
    return glmassist.rows(Path(run_dir) / DRAFTS)


def is_gate(cmd: str, gates=(), env=None) -> bool:
    """Whether shell command `cmd` is a gate: a `check:`/`gate:` line of the spec, `ttp checks`, `ttp lock`,
    or a broker status or preflight."""
    norm = " ".join(cmd.split())
    if any(g and (norm == g or norm.startswith(g + " ")) for g in gates):
        return True
    if BROKER.search(cmd):
        return True
    return bool(_ttp_subs(cmd, env) & GATE_SUBS)


def _ttp_subs(cmd: str, env=None) -> set[str]:
    """The ttp subcommands shell command `cmd` runs."""
    from . import hook
    project = _own_name(os.environ if env is None else env)
    plain, _ = hook._split(REDIRECT.sub(" ", cmd), strip=False)   # a message's text stays
    return {w[0] for seg in hook._all_segments(plain) + hook._segments(plain, strip=False)
            if seg and (w := _ttp_args(seg, project))}


TOOL_TIMEOUT = re.compile(r"(?m)^Command timed out after \d")   # the agent tool's own cut-off line
GATE_TIMEOUTS = "glm_gate_timeouts"   # the `ttp checks` calls the tool's time limit cut off once in this run


def gate_stop(run_dir, cmd: str, rc, output: str, env=None) -> str | None:
    """On a device run on the chat model, a gate command that failed (`rc` neither 0 nor busy) ends the run
    as a hand-back to Opus with the command and its output; returns why, None when the run goes on. A
    `ttp checks` the agent tool's time limit cut off (exit 124, 137 or 143 with the tool's cut-off line) did not
    fail: the run goes on to run it once more, and only a second cut-off of the same command ends the run. Any
    other gate cut off (a device check on a box that may hang) still ends it."""
    g = glm_run(env)
    if not g or not g["device"] or rc in (0, BUSY, None) or not is_gate(cmd, g["gates"], env):
        return None
    norm = " ".join(cmd.split())
    if rc in (124, 137, 143) and TOOL_TIMEOUT.search(output or "") and run_dir and "checks" in _ttp_subs(cmd, env):
        f = Path(run_dir) / GATE_TIMEOUTS
        try:
            seen = f.read_text().splitlines()
        except OSError:
            seen = []
        if norm not in seen:
            try:
                from .project import durable_append
                durable_append(f, norm + "\n")
                return None
            except OSError:
                pass   # no record of the retry: the cut-off stops the run, as before
        why = f"a gate was cut off by the tool's time limit twice (exit {rc}): `{norm[:160]}`"
    else:
        why = f"a gate failed (exit {rc}): `{norm[:160]}`"
    hit(run_dir, why, GATE, output or "")
    return why

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check that runs on another machine over ssh: `delivery.checks_host`, or a check's own `host`.

Some projects may not run test suites on the machine that coordinates them. A check with a host
runs there instead: the exact commit being checked (--commit, else HEAD of the checkout) goes to a bare
mirror of the repository in the host's tt-project folder (check-mirrors/, keyed by the repository's root
commits) as an incremental git bundle of only the commits it lacks (_push: a new mirror is seeded in steps
a cut-short copy resumes; first from a clone already there when `delivery.checks_seed` names one, so only
what that clone lacks crosses the link), is checked out of it into a fresh temporary directory (a --shared
clone) and its check ref deleted afterwards. No tree is sent from here: a host without usable git is a host
fault, unless TTP_REMOTE_TREE_COPY=1 sends it the tree with `git archive` over a compressed ssh (_archive),
made a git checkout there when it can. Every copy prints its progress,
and one that overruns its limit or stalls is the host's fault (_copy). The command runs in it with
the check's `env` exported (values expand in the remote shell, e.g. "$HOME/.local/bin:$PATH"). Its
output streams back and its exit code is the check's. A stopped check (a timeout, a stop) stops
its command there too (_run_script). The temporary directory is always removed.

ssh failing (an unreachable host, a refused key, a dropped connection) or a host that cannot take
the tree is an environment fault, not the change's (ssh's shared connection refusing one more session
first tries the check again from the start on a connection of its own, MUX_BACKOFF_S), and so is a host that is paused (paused: this
project's, a shared or another project's pause; nothing reaches it over ssh): this exits FAULT_RC, which the callers
(`ttp checks`, `ttp push`, the push queue) treat as envfault does a missing runner. A command's own
exit FAULT_RC is reported as 1, so it never reads as a fault. A check spilled with a remote wrapper
(--wrap: push.spill_to) runs behind it there; a wrapper missing or not executable on the host (a
relative path in the copy of the commit, else a name on the host's PATH) is a fault too: the check
never runs unwrapped. So is a wrapper that refuses the run (a busy host): it writes the file its
TTP_WRAP_REFUSED variable names and exits FAULT_RC, and the check runs again elsewhere; exit FAULT_RC
without that file still reads as 1. `--py` files (the tests gate's select plugin, cli._remote_select) are written into the
run folder's .git, out of the tree the check sees, and put first on PYTHONPATH; `--literal` variables are
exported as given, never expanded there.

`host` is an ssh alias (a machines-list alias is one): `ssh <host>` must reach it without a prompt."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import sshagent, tailnet

FAULT_RC = 75
ENV_DIR = ".git/ttp-env"   # the pinned Python env's maker and lock in the run folder (runnerenv.remote_lines)
SHIP_DIR = ".git/ttp-py"   # where shipped Python files go in the run folder: never in the tree the check sees
NO_ENV = "no-env"     # ... and when the pinned Python env cannot be made there (runnerenv.remote_lines): a host fault
NO_WRAP = "no-wrap"   # the exit marker's form when the remote wrapper is missing there: a host fault
WRAP_REFUSED = "wrap-refused"   # ... and when the wrapper refused the run (WRAP_REFUSED_ENV): a host fault
WRAP_REFUSED_ENV = "TTP_WRAP_REFUSED"   # the file a wrapper writes before it exits FAULT_RC to refuse a run
# Behind a spill wrapper: the pinned checks env first on PATH again and the user site off (runnerenv.remote_lines)
REFRONT = ('if [ -n "${TTP_CHECKS_ENV_BIN:-}" ]; then PATH="$TTP_CHECKS_ENV_BIN${PATH:+:$PATH}"; '
           'PYTHONNOUSERSITE=1; export PATH PYTHONNOUSERSITE; unset PIP_USER; fi\n')
LOCAL = "local"      # a check's `host` that keeps it here when delivery.checks_host is set
HOST_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@+:-]{0,252}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A link that drops mid-check ends in exit 255 within about a minute, never hangs to the wall-time cap.
# A keepalive every 5 s: a proxy in the path may cut a link idle for 15 s, as when a test suite prints
# nothing for a while on a connection of its own (DIRECT, or ssh's own fallback when its shared one refuses).
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=5",
       "-o", "ServerAliveCountMax=12"]
# A connection of its own, never ssh's shared one (ControlMaster), whose sessions the host caps (MaxSessions).
DIRECT = ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
# ssh's words when the shared connection refuses one more session: not the host's fault. The check is
# tried again from the start on a connection of its own after each wait here, then it is a fault.
MUX_REFUSED = "Session open refused by peer"
MUX_BACKOFF_S = (2, 5)
# Under the checks' silence limit (checkwall.SILENT_S, 600 s): a copy that stalls is the host's fault
# (another host or here takes the check), never a silent "hang" blamed on the change.
SETUP_TIMEOUT_S = 540
CLEANUP_TIMEOUT_S = 120
PROBE_TIMEOUT_S = 45
STOP_GRACE_S = 5   # a stopped command's TERM before KILL on the host
SIG = "env:check-host:"
_TMP = re.compile(r"/[A-Za-z0-9._/-]*/ttp-check\.[A-Za-z0-9]+")
# Runs the module from this runtime whatever the check's PYTHONPATH: argv[1] is the runtime folder.
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from ttp.remotecheck import main; sys.exit(main(sys.argv[2:]))"


def host_problem(v) -> str:
    """Why `v` cannot be a check host, or ""."""
    if not isinstance(v, str) or not HOST_RE.fullmatch(v.strip()):
        return f"{v!r}: a check host is an ssh alias (letters, digits and . _ - @ + :), or {LOCAL!r}"
    return ""


def env_problem(v) -> str:
    """Why `v` cannot be a check's remote `env`, or ""."""
    if not isinstance(v, dict) or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(x, str)
                                          for k, x in v.items()):
        return f"{v!r}: env must map variable names to string values"
    return ""


def argv(cmd: str, host: str, env: dict | None = None, commit: str = "", wrap: tuple[str, ...] = (),
         py: tuple[str, ...] = (), literal: dict | None = None) -> list[str]:
    """The local command that runs check `cmd` on `host` (main): on `commit` when given (the commit a
    `ttp checks` run records), else on the checkout's HEAD; behind `wrap` there when given. `py`: local
    Python files copied there onto PYTHONPATH (shipped); `literal`: variables exported as given, unexpanded."""
    return [sys.executable, "-c", _BOOT, str(Path(__file__).resolve().parents[1]), "--host", host,
            "--env", json.dumps(env or {}, sort_keys=True), *(["--commit", commit] if commit else []),
            *(["--wrap", json.dumps(list(wrap))] if wrap else []),
            *(["--py", json.dumps(list(py))] if py else []),
            *(["--literal", json.dumps(literal, sort_keys=True)] if literal else []), "--", cmd]


def _ssh(host: str, script: str, direct: bool = False, compress: bool = False) -> list[str]:
    # The login shell parses the command line: hand it one quoted word for sh, whatever that shell is.
    # compress: the tree copy (a plain tar, about 3x smaller compressed) on a slow link.
    return [*SSH, *(DIRECT if direct else []), *(["-o", "Compression=yes"] if compress else []), host,
            f"sh -c {shlex.quote(script)}"]


def paused(host: str, where: str | Path | None = None) -> str:
    """"paused by <who>: <reason>" when check host `host` is paused, else "": a pause of this project or
    a shared one (lockprobe.pause), or one another project set on its alias or host name
    (checkspill.paused_elsewhere). The project is the one the check runs in (project.here, from
    `where` else this folder); with none, or one it cannot read, nothing is paused."""
    from . import checkspill, lockprobe, machines, project
    try:
        p = project.here(where)
        if p is None:
            return ""
        held = lockprobe.pause(p, p.config(), host)
        who = (held.get("project") if isinstance(held, dict) else "") or p.name
        if held is None and (got := checkspill.paused_elsewhere(p, host, machines.load().get(host))):
            who, held = got
    except Exception:
        return ""
    if held is None:
        return ""
    reason = held.get("reason") if isinstance(held, dict) else ""
    return f"paused by {who}{f': {reason}' if reason else ''}"


def reachable(host: str) -> str:
    """"" when ssh reaches `host` and it has git and tar (what a remote check needs), else why not."""
    try:
        r = subprocess.run(_ssh(host, "command -v git >/dev/null && command -v tar >/dev/null"),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        if sshagent.password_manager():
            return f"ssh to {host} timed out after {PROBE_TIMEOUT_S} s: {sshagent.WHY}"
        return f"ssh to {host} timed out after {PROBE_TIMEOUT_S} s" + tailnet.suffix()
    except OSError as e:
        return f"cannot run ssh: {e}"
    if r.returncode == 0:
        return ""
    if r.returncode == 255 and sshagent.locked(r.stderr):
        return f"ssh to {host} failed: {sshagent.WHY}"
    if r.returncode == 255:
        return (f"ssh cannot reach {host}: {(r.stderr.strip().splitlines() or ['exit 255'])[-1][:200]}"
                + tailnet.suffix())
    return f"{host} lacks git or tar"


def fault(host: str, cmd: str, cwd: str, why: str = "") -> dict:
    """The envfault record of a check host that cannot run checks."""
    return {"kind": "host", "host": host, "runner": host, "sig": SIG + host, "cmd": cmd, "cwd": cwd,
            **({"why": why} if why else {})}


_TOLD: dict = {}   # what this run has said once (its priority line)


_ERROR_LINE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception)\b:?[^\n]*)")
_NOISE = re.compile(r"(?:/[^\s'\"]+)+|0x[0-9a-fA-F]+|\d+")


def fail_sig(text: str, rc: Any = None) -> str:
    """A failed check's signature from its output's tail: its last error line (`ModuleNotFoundError: No module
    named 'yaml'`), paths and numbers cut so the same fault on two hosts reads the same; else `exit <rc>`.
    The harness's own `ttp ...:` lines never count (checkspill.strike_settle tells a config fault by it)."""
    hits = [m.group(1) for line in (text or "")[-65536:].splitlines()
            if not line.lstrip().startswith("ttp ") for m in _ERROR_LINE.finditer(line)]
    return _NOISE.sub("#", hits[-1]).strip()[:200] if hits else f"exit {rc}"


def _say(msg: str) -> None:
    sys.stdout.write(f"ttp remote check: {msg}\n")
    sys.stdout.flush()


WRAP_SETTING = "(checks.spill_wrap_cmd, or the check's own spill_wrap_cmd)"   # names the wrapper's setting


def wrap_fault(text: str) -> tuple[str, str]:
    """(host, why) of the last spill wrapper fault (missing or refused) in a checks log's `text`, else ("", "")."""
    found = ("", "")
    for line in text.splitlines():
        head, sep, rest = line.partition(f"environment fault {SIG}")
        host, _, why = rest.partition(": ")
        if sep and why.startswith("the spill wrapper ") and WRAP_SETTING in why:
            found = (host, why.partition(", so the check")[0].partition(" (exit ")[0])
    return found


def _fault(host: str, why: str) -> int:
    agent = sshagent.WHY in why   # the ssh agent here (sshagent.py): neither the host nor the change
    _say(f"environment fault {SIG}{host}: {why}. "
         + ("Not the host and not the change: the run waits and retries" if agent else "This is the host, not the change"))
    return FAULT_RC


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def exports(env: dict) -> str:
    # Double quotes: a value may expand remote variables ($HOME, $PATH); \ " and ` stay literal.
    return "".join(f'export {k}="' + re.sub(r'([\\"`])', r"\\\1", v) + '"\n' for k, v in sorted(env.items()))


def shipped(files: list[str], literal: dict) -> str:
    """The host's lines, after the check's exports, that write `files` (local paths) into the run folder's
    .git/ttp-py (out of the tree the check sees) and put it first on PYTHONPATH, then export `literal`
    as given. OSError when a file cannot be read."""
    out = ""
    if files:
        out = f"mkdir -p {SHIP_DIR}\n"
        for f in files:
            text, end = Path(f).read_text(), f"TTP_PY_{secrets.token_hex(8)}"
            out += f"cat > {SHIP_DIR}/{shlex.quote(Path(f).name)} <<'{end}'\n{text.rstrip(chr(10))}\n{end}\n"
        out += f'export PYTHONPATH="$PWD/{SHIP_DIR}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
    return out + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in sorted(literal.items()))


def _priority(prio: tuple[list, list] | None) -> str:
    """The host's lines that put the command (in "$@") behind `nice` and `ionice` (testslots.priority_args),
    each only where the host has it: the lowest priority there too, so its live services go first."""
    if not prio:
        return ""
    return "".join(f"if command -v {words[0]} >/dev/null 2>&1; then set -- {' '.join(map(shlex.quote, words))} \"$@\"; fi\n"
                   for words in reversed(prio) if words)


def _run_script(q: str, exports_: str, cmd: str, mark: str, wrap: tuple[str, ...] = (),
                prio: tuple[list, list] | None = None) -> str:
    """The host's side of a check run in directory `q`: the command in a process group of its own
    (setsid, else perl's setpgrp), with stdin from /dev/null and Ctrl-C and quit at their default, and a watcher that stops that group (TERM,
    then KILL after STOP_GRACE_S) once ssh's stdin ends: without a tty, ssh signals nothing when the
    local side stops or dies, and the command would run on. Then the exit-code marker, and the
    directory is removed. With `wrap`, the command runs behind it, and a wrapper that is missing or not
    executable there prints the marker's NO_WRAP form instead of starting anything, and one that exits
    FAULT_RC after writing the file WRAP_REFUSED_ENV names prints its WRAP_REFUSED form. With `prio`, it all
    runs behind the host's `nice` and `ionice` (_priority). Behind a wrapper the pinned checks env
    (TTP_CHECKS_ENV_BIN, runnerenv.remote_lines) goes first on PATH again, so a wrapper that puts its own env first never hides it."""
    if wrap:
        cmd = REFRONT + cmd
    w = " ".join(map(shlex.quote, wrap))
    run = f"{w} bash -e -o pipefail -c {shlex.quote(cmd)}".lstrip()
    has = ""
    if wrap:
        exe = shlex.quote(wrap[0])
        test = f"[ -f {exe} ] && [ -x {exe} ]" if "/" in wrap[0] else f"command -v {exe} >/dev/null 2>&1"
        has = (f"if ! {{ {test}; }}; then printf '\\n%s%s\\n' {mark} {NO_WRAP}; exit 96; fi\n"
               f"export {WRAP_REFUSED_ENV}={q}/.git/ttp-wrap-refused\n")
    refused = (f"if [ \"$rc\" = {FAULT_RC} ] && [ -e \"${WRAP_REFUSED_ENV}\" ]; then "
               f"printf '\\n%s%s\\n' {mark} {WRAP_REFUSED}; fi\n") if wrap else ""
    from . import orphanchecks
    # Who owns the run: once that process is gone, orphanchecks stops what still runs here.
    owner = f"printf '%s\\n' {shlex.quote(orphanchecks.owner_line())} > {orphanchecks.OWNER_FILE}\n"
    return (f"cd {q} || exit 97\n{owner}trap 'cd / && rm -rf {q}' EXIT\n{exports_}{has}"
            f"if command -v bash >/dev/null 2>&1; then set -- {run}; else set -- {w} sh -e -c {shlex.quote(cmd)}; fi\n"
            f"{_priority(prio)}"
            # A background job's stdin is /dev/null unless redirected: the watcher reads ssh's from fd 3.
            "exec 3<&0\n"
            # It also starts with SIGINT and SIGQUIT ignored, which no shell can undo: reset both first.
            "if command -v perl >/dev/null 2>&1; then "
            "set -- perl -e '$SIG{INT}=$SIG{QUIT}=\"DEFAULT\"; exec {$ARGV[0]} @ARGV or exit 127' \"$@\"\n"
            "elif command -v python3 >/dev/null 2>&1; then set -- python3 -c 'import os, signal, sys\n"
            "for s in (signal.SIGINT, signal.SIGQUIT): signal.signal(s, signal.SIG_DFL)\n"
            "os.execvp(sys.argv[1], sys.argv[1:])' \"$@\"\nfi\n"
            "if command -v setsid >/dev/null 2>&1; then setsid \"$@\" </dev/null 3<&- &\n"
            "elif command -v perl >/dev/null 2>&1; then perl -e 'setpgrp(0,0); exec @ARGV or exit 127' \"$@\" </dev/null 3<&- &\n"
            "else \"$@\" </dev/null 3<&- &\nfi\n"
            "pid=$!\n"
            "(cat <&3 >/dev/null; kill -TERM -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null; "
            f"sleep {STOP_GRACE_S}; kill -KILL -$pid 2>/dev/null || kill -KILL $pid 2>/dev/null) >/dev/null 2>&1 &\n"
            "watch=$!\nexec 3<&-\n"
            "wait $pid\nrc=$?\nkill $watch 2>/dev/null\ntrap '' PIPE\n"
            f"{refused}printf '\\n%s%s\\n' {mark} \"$rc\"\nexit $rc\n")


class _Stop(Exception):
    pass


def mktemp_cmd(env=None) -> str:
    """The shell command that makes the run's folder on the host. A run that holds a checks slot here names
    this machine in it: should the host be this machine, its slot counts it there once (checkcap.pool_runs)."""
    from . import checkcap
    tag = checkcap.host_tag() if (os.environ if env is None else env).get(checkcap.HELD_ENV) else ""
    return f'mktemp -d "${{TMPDIR:-/tmp}}/ttp-check.{tag}XXXXXXXX"'


def _stop(_sig, _frame) -> None:
    raise _Stop()


def main(args: list[str]) -> int:
    host, env, commit, wrap, py, literal = "", {}, "", (), [], {}
    while args and args[0] != "--":
        flag, value, args = args[0], args[1] if len(args) > 1 else "", args[2:]
        if flag == "--host":
            host = value
        elif flag == "--env":
            env = json.loads(value or "{}")
        elif flag == "--commit":
            commit = value
        elif flag == "--wrap":
            wrap = tuple(str(w) for w in json.loads(value or "[]"))
        elif flag == "--py":
            py = [str(f) for f in json.loads(value or "[]")]
        elif flag == "--literal":
            literal = json.loads(value or "{}")
    cmd = " ".join(args[1:])
    if host_problem(host) or host == LOCAL or env_problem(env) or env_problem(literal) or not cmd:
        _say(f"usage: --host <ssh alias> [--env <json>] -- <command> "
             f"({host_problem(host) or env_problem(env) or env_problem(literal)})")
        return 2
    from . import testslots   # a fixed `-n N` above the host's worker grant (checkspill.workers_env) is lowered to it
    cmd = testslots.clamp_for(cmd, literal)
    mark = f"ttp-remote-rc:{secrets.token_hex(8)}:"
    try:   # the pinned Python env there, as here (runnerenv): no env, no run (NO_ENV)
        from . import runnerenv
        extra = shipped(py, literal) + runnerenv.remote_lines(os.environ.get(runnerenv.LOCK_ENV), ENV_DIR,
                                                               f"printf '\\n%s%s\\n' {mark} {NO_ENV}; exit 95")
    except OSError as e:
        _say(f"cannot read a file to copy there: {e}")
        return 2
    # A paused host may be lent to others: no ssh to it until the pause lifts.
    if why := paused(host):
        return _fault(host, why)
    # The commit given, never the checkout's HEAD in its place: that may have moved since the run began.
    top = _git("rev-parse", "--show-toplevel")
    head = "" if commit.startswith("-") else _git("rev-parse", "--verify", "-q", f"{commit or 'HEAD'}^{{commit}}")
    if not top or not head:
        _say(f"no commit {commit[:12]} in this checkout; nothing to copy" if commit and top
             else "not in a git checkout with a commit; nothing to copy")
        return 2
    why = ""
    for wait in (None, *MUX_BACKOFF_S):
        if wait is not None:
            _say(f"ssh's shared connection to {host} refused a session ({MUX_REFUSED}): trying again "
                 f"in {wait} s on a connection of its own")
        rc, why = _attempt(host, top, head, env, extra, cmd, mark, wrap, wait)
        if not why:
            return rc
    return _fault(host, f"{why}; ssh's shared connection refused a session ({MUX_REFUSED}) on each of "
                        f"{1 + len(MUX_BACKOFF_S)} tries")


def _refused(text: str | bytes) -> bool:
    return MUX_REFUSED in (text.decode(errors="replace") if isinstance(text, bytes) else text or "")


MIRRORS = "check-mirrors"   # under the host's tt-project folder (${TTP_HOME:-$HOME/.tt-project}), beside checks-env
MIRROR_MARK, BASE_MARK = "ttp-mirror:", "ttp-base:"   # the probe's lines naming the mirror and its base refs
MIRROR_RE = re.compile(r"/[A-Za-z0-9._/+@-]*/" + MIRRORS + r"/[0-9a-f]{16}\.git")
LINK, MUX = "link:", "mux:"   # a copy step's why: the link's fault, ssh's shared connection refusing a session
PROGRESS_S = 30     # a copy prints how far it got this often, so the checks' silence limit never stops a live one
STALL_S = 300       # ... and one whose progress has not moved this long is the link's fault (ssh's window,
                    # a few MB written at once, drains at the link's pace after the last byte moves)
SEED_STEP = 500     # first-parent commits per bundle while a mirror catches up: each lands, the next resumes there
BASE_KEEP = 20      # base refs a mirror keeps (the newest)
CHECK_REF_AGE_S = 2 * 86400   # a check ref left this long (a killed run) is removed
MIRROR_AGE_D = 30   # a mirror unused this many days is removed


def _knob(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def mirror_key(top: str, head: str) -> str:
    """The repository's identity on a check host: its root commits, the same in every clone of it."""
    roots = sorted(_git("-C", top, "rev-list", "--max-parents=0", head).split())
    return hashlib.sha256(" ".join(roots).encode()).hexdigest()[:16] if roots else ""


def _mirror_probe(key: str) -> str:
    """The host's side, after mktemp: makes the bare mirror of the repository keyed `key` (mirror_key) when
    git is there, and prints its path and its newest base refs. Prints nothing (never fails) without git."""
    if not key:
        return "true"
    return (f'if git --version >/dev/null 2>&1; then m="${{TTP_HOME:-$HOME/.tt-project}}/{MIRRORS}/{key}.git"; '
            'if { [ -f "$m/HEAD" ] || { mkdir -p "$m" && git init -q --bare "$m"; }; } >/dev/null 2>&1 '
            '&& m=$(cd "$m" && pwd -P) && [ "$(git -C "$m" rev-parse --is-bare-repository 2>/dev/null)" = true ]; '
            f'then touch "$m"; echo "{MIRROR_MARK}$m"; git -C "$m" for-each-ref --sort=-committerdate --count=50 '
            f"--format='{BASE_MARK}%(objectname)' refs/heads/base 2>/dev/null; fi; fi; true")


def _mirror_cleanup(mirror: str, ref: str) -> str:
    """The host's side after a check: its ref goes; so do check refs a killed run left, base refs past the
    newest BASE_KEEP and mirrors unused for MIRROR_AGE_D days. gc (auto) runs only while no check holds a ref."""
    m = shlex.quote(mirror)
    return ("{ " f"m={m}; git -C \"$m\" update-ref -d {shlex.quote(ref)}; now=$(date +%s); "
            "git -C \"$m\" for-each-ref --format='%(refname)' refs/heads/check | while read -r r; do "
            "t=${r##*/}; t=${t%%-*}; case $t in ''|*[!0-9]*) continue;; esac; "
            f"[ $((now - t)) -gt {CHECK_REF_AGE_S} ] && git -C \"$m\" update-ref -d \"$r\"; done; "
            "git -C \"$m\" for-each-ref --sort=-committerdate --format='%(refname)' refs/heads/base "
            f"| tail -n +{BASE_KEEP + 1} | while read -r r; do git -C \"$m\" update-ref -d \"$r\"; done; "
            "[ -n \"$(git -C \"$m\" for-each-ref refs/heads/check)\" ] || git -C \"$m\" gc --auto --quiet; "
            f"find \"${{m%/*}}\" -mindepth 1 -maxdepth 1 -name '*.git' -type d -mtime +{MIRROR_AGE_D} "
            "-exec rm -rf {} +; true; } >/dev/null 2>&1")


def _checkout_script(q: str, mirror: str, ref: str, head: str) -> str:
    """The host's side once `head` is in its mirror at `ref`: a --shared clone of it checked out at `head` in
    the run's folder `q` (a fresh one: the objects stay in the mirror), and `head` kept as a base ref there."""
    m, branch = shlex.quote(mirror), shlex.quote(ref[len("refs/heads/"):])
    return (f"set -e; cd {q}; git clone -q --shared --no-checkout --no-tags --single-branch --branch {branch} {m} .; "
            f"git -c advice.detachedHead=false checkout -q --detach {head}; "
            f'[ "$(git rev-parse HEAD)" = {head} ]; '
            f"git -C {m} update-ref refs/heads/base/{head} {head} >/dev/null 2>&1 || true")


def _watch(proc, progress, label: str, deadline: float, stall_s: float | None) -> str:
    """Waits for `proc`, printing `progress()` every PROGRESS_S s: "" once it ended, else LINK and why after
    killing it when it overran `deadline` (monotonic) or its progress has not moved for `stall_s` s."""
    start = moved = time.monotonic()
    seen, every = progress(), _knob("TTP_REMOTE_PROGRESS_S", PROGRESS_S)
    say = start + every
    try:
        while proc.poll() is None:
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            now, cur = time.monotonic(), progress()
            if cur != seen:
                seen, moved = cur, now
            why = (f"{label} overran its limit after {now - start:.0f} s" if now >= deadline else
                   f"{label} stalled: nothing moved for {now - moved:.0f} s" if stall_s and now - moved >= stall_s
                   else "")
            if why and proc.poll() is None:
                return f"{LINK}{why} ({cur or 'nothing yet'})"
            if now >= say:
                _say(f"{label}: {cur or 'started'} ({now - start:.0f} s)")
                say = now + every
        return ""
    finally:
        if proc.poll() is None:
            try:   # its own process group (start_new_session): what ssh started here goes with it
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()


def _tail(f) -> str:
    f.seek(0)
    return f.read().decode(errors="replace")


def _host_step(host: str, script: str, direct: bool, deadline: float, label: str, said: list | None = None) -> str:
    """Runs `script` on the host: "", or why it failed (LINK: ssh's, MUX: its shared connection refused).
    `said` gets what it printed."""
    with tempfile.TemporaryFile() as out:
        # own session: _watch's finally kills the group (os.killpg), so what ssh started goes with it
        proc = subprocess.Popen(_ssh(host, script, direct), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True)
        why = _watch(proc, lambda: "", label, deadline, None)
        text = _tail(out)
    if said is not None:
        said.append(text)
    last = (text.strip().splitlines() or [""])[-1][:200]
    if why or proc.returncode == 0:
        return why
    if proc.returncode == 255:
        return (MUX if _refused(text) else LINK) + f"{label} failed (ssh exit 255: {last})"
    return f"{label} failed (exit {proc.returncode}: {last})"


SEED_RE = re.compile(r"/[A-Za-z0-9._/+@-]+")


def seed_path(host: str) -> str:
    """The clone already on `host` its new mirror is seeded from (`delivery.checks_seed`: {host: absolute
    path}), so the first copy from here sends only what that clone lacks; "" when none is named."""
    from . import testslots
    seeds = ((testslots._cfg() or {}).get("delivery") or {}).get("checks_seed")
    path = seeds.get(host) if isinstance(seeds, dict) else None
    return path if isinstance(path, str) and SEED_RE.fullmatch(path) and ".." not in path else ""


SEED_SAMPLE = 25    # every this many first-parent commits of the head, one is asked for in a freshly seeded mirror


def _seed_script(mirror: str, seed: str, ours: list[str]) -> str:
    """The host's side: fetches the seed clone's branches into the mirror (refs/seed/; a plain fetch from
    that path reads it and never writes there), then makes the newest of `ours` (commits of the head here,
    newest first) the mirror has a base ref and prints it. Its own commits may differ from ours (a rebase
    rewrites them): only one both sides have saves a copy."""
    m = shlex.quote(mirror)
    return (f"set -e; s={shlex.quote(seed)}; git -C \"$s\" rev-parse -q --verify HEAD >/dev/null; "
            f"git -C {m} fetch -q --no-tags --no-write-fetch-head \"$s\" '+refs/heads/*:refs/seed/heads/*' "
            f"'+refs/remotes/*:refs/seed/remotes/*'; for c in {' '.join(ours)}; do "
            f"if git -C {m} cat-file -e \"$c^{{commit}}\" 2>/dev/null; then "
            f"git -C {m} update-ref refs/heads/base/$c $c; echo \"{BASE_MARK}$c\"; break; fi; done")


def _objects(top: str) -> str:
    return str(Path(_git("-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "objects")


def _copy_argv(host: str, script: str, direct: bool, compress: bool) -> list[str]:
    """The ssh that takes a bulk copy (a bundle, a tree): the one place its connection is chosen."""
    return _ssh(host, script, direct, compress=compress)


def _copy(host, source: list[str], script: str, direct: bool, deadline: float, label: str,
          compress: bool = False) -> tuple[int, int, str, str]:
    """Every bulk copy to a host: `source`'s output streamed into `script` there, printing the bytes sent
    every PROGRESS_S s; killed at `deadline` or after STALL_S s with nothing sent. (source's exit, ssh's
    exit, LINK and why when killed else "", the host's output)."""
    sent = [0]
    with tempfile.TemporaryFile() as out:
        src = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # own session: _watch's finally kills the group (os.killpg), so what ssh started goes with it
        proc = subprocess.Popen(_copy_argv(host, script, direct, compress), stdin=subprocess.PIPE, stdout=out,
                                stderr=out, start_new_session=True)
        pumper = threading.Thread(target=_pump, args=(src.stdout, proc.stdin, sent), daemon=True)
        t0 = time.monotonic()
        pumper.start()
        why = _watch(proc, lambda: f"{sent[0] / 1e6:.2f} MB sent", label, deadline,
                     _knob("TTP_REMOTE_COPY_STALL_S", STALL_S))
        if why or proc.returncode:
            src.kill()
        pumper.join(timeout=10)
        src.wait()
        text = _tail(out)
    if not why and proc.returncode == 0 and src.returncode == 0:
        _say(f"{label}: done in {time.monotonic() - t0:.0f} s ({sent[0] / 1e6:.2f} MB)")
    return src.returncode, proc.returncode, why, text


def _send_bundle(host, top, sha, dst, have, mirror, direct, deadline) -> str:
    """A git bundle of `sha` without what reaches from `have` (commits the host's mirror holds), streamed
    over ssh into a file beside the mirror and fetched from there to `dst`: only objects it lacks travel.
    The bundle is made in a scratch repository borrowing this one's objects, so no ref here moves."""
    label = f"sending {sha[:12]} to {host}'s mirror"
    m = shlex.quote(mirror)
    incoming = shlex.quote(f"{mirror}.in-{secrets.token_hex(6)}.bundle")
    script = (f"set -e; umask 077; f={incoming}; trap 'rm -f \"$f\"' EXIT; cat > \"$f\"; "
              f"git -C {m} fetch -q --no-tags --no-write-fetch-head \"$f\" refs/heads/ttp-send:{shlex.quote(dst)}"
              # another check seeding the same step got there first: its commit is what this one needs
              + (f" || git -C {m} cat-file -e {sha}^{{commit}}" if dst.startswith("refs/heads/base/") else ""))
    with tempfile.TemporaryDirectory(prefix="ttp-bundle.") as scratch:
        subprocess.run(["git", "init", "-q", "--bare", scratch], check=True, capture_output=True)
        Path(scratch, "objects", "info", "alternates").write_text(_objects(top) + "\n")
        subprocess.run(["git", "--git-dir", scratch, "update-ref", "refs/heads/ttp-send", sha], check=True,
                       capture_output=True)
        src_rc, rc, why, text = _copy(host, ["git", "--git-dir", scratch, "bundle", "create", "-q", "-",
                                             "refs/heads/ttp-send", *(["--not", *have] if have else [])],
                                      script, direct, deadline, label)
    if why:
        return why
    last = (text.strip().splitlines() or [""])[-1][:200]
    if rc == 255:
        return (MUX if _refused(text) else LINK) + f"{label} failed (ssh exit 255: {last})"
    if src_rc or rc:
        return f"{label} failed (bundle exit {src_rc}, host exit {rc}: {last})"
    return ""


def _pump(src, dst, sent) -> None:
    """Copies `src` to `dst` (closing both), counting bytes in sent[0]: a copy's progress."""
    try:
        while chunk := src.read(65536):
            dst.write(chunk)
            sent[0] += len(chunk)
    except (OSError, ValueError):
        pass
    finally:
        src.close()
        try:
            dst.close()
        except OSError:
            pass


def _push(host, top, head, mirror, ref, bases, direct, deadline) -> str:
    """Puts `head` in the host's mirror at `ref`. A mirror far behind (a new one) catches up first in steps of
    SEED_STEP first-parent commits, each kept as a base ref: a copy cut short resumes from the last step."""
    have = []
    if bases:
        r = subprocess.run(["git", "-C", top, "cat-file", "--batch-check=%(objectname) %(objecttype)"],
                           input="\n".join(bases) + "\n", capture_output=True, text=True)
        have = [ln.split()[0] for ln in r.stdout.splitlines() if ln.endswith(" commit")]
    behind = _git("-C", top, "rev-list", "--first-parent", "--reverse", head, "--not", *have).split()
    if not behind:   # the mirror has it: a ref to it is all the check needs
        return _host_step(host, f"git -C {shlex.quote(mirror)} update-ref {shlex.quote(ref)} {head}", direct, deadline,
                          f"marking {head[:12]} in {host}'s mirror")
    step = max(1, int(_knob("TTP_REMOTE_SEED_STEP", SEED_STEP)))
    steps = [behind[i] for i in range(step - 1, len(behind) - 1, step)]
    if steps:
        _say(f"{host}'s mirror lacks {len(behind)} commits: sending them in {len(steps) + 1} steps")
    for sha, dst in [*((s, f"refs/heads/base/{s}") for s in steps), (head, ref)]:
        if why := _send_bundle(host, top, sha, dst, have, mirror, direct, deadline):
            return why
        have = [*have, sha]
    return ""


def _archive(host, top, head, q, direct, deadline) -> tuple[int, str]:
    """The tree of `head` over a compressed ssh into the run's folder `q` (emptied first), made a git checkout
    when the host has git: (0, ""), (2, "") when git archive failed here, else (0, why) as _host_step."""
    setup = f"set -e; rm -rf {q}; mkdir -m 700 {q}; cd {q}; tar -xf -"
    init = (f"set -e; cd {q}; if git --version >/dev/null 2>&1; then git -c init.defaultBranch=main init -q; "
            "git add -A -f .; git -c user.name=ttp -c user.email=ttp@localhost -c commit.gpgsign=false commit -q "
            f"--no-verify --allow-empty -m {shlex.quote('ttp check of ' + head)}; fi")
    src_rc, rc, why, text = _copy(host, ["git", "-C", top, "archive", "--format=tar", head], setup, direct, deadline,
                                  f"sending {head[:12]}'s tree to {host} (transfer; ssh compresses it)", compress=True)
    if why:
        return 0, why
    if rc != 0:
        why = (f"cannot unpack the tree there (transfer) (exit {rc}: "
               f"{(text.strip().splitlines() or [''])[-1][:200]})")
        return 0, (MUX + why if rc == 255 and _refused(text) else why)
    if src_rc != 0:
        _say(f"git archive of {head[:12]} failed (exit {src_rc})")
        return 2, ""
    label = f"making {head[:12]}'s tree a git checkout on {host} (setup)"
    t0 = time.time()
    why = _host_step(host, init, direct, deadline, label)
    if not why:
        _say(f"{label}: done in {time.time() - t0:.0f} s")
    return 0, why


def _attempt(host, top, head, env, extra, cmd, mark, wrap, wait) -> tuple[int, str]:
    """One try of the check on `host` (main), after `wait` s on a connection of its own when given:
    (exit code, ""), or (FAULT_RC, why) when ssh failed after its shared connection refused a session."""
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(s, _stop)
    direct = wait is not None
    tmp, proc, cleanup_ref = "", None, None
    try:
        if direct:
            time.sleep(wait)
        key = mirror_key(top, head)
        try:
            made = subprocess.run(_ssh(host, f"{mktemp_cmd()} && {{ {_mirror_probe(key)}; }}", direct),
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _fault(host, f"ssh timed out after {PROBE_TIMEOUT_S} s"
                          + (f": {sshagent.WHY}" if sshagent.password_manager() else "")), ""
        lines = made.stdout.strip().splitlines()
        tmp = lines[0].strip() if lines else ""
        if made.returncode != 0 or not _TMP.fullmatch(tmp):
            tmp = ""
            why = (f"cannot make a temporary directory there (ssh exit {made.returncode}: "
                   f"{(made.stderr.strip().splitlines() or [''])[-1][:200]})")
            if made.returncode == 255 and _refused(made.stderr):
                return FAULT_RC, why
            if made.returncode == 255 and sshagent.locked(made.stderr):
                why = f"ssh failed: {sshagent.WHY}"
            return _fault(host, why), ""
        mirror = next((ln[len(MIRROR_MARK):] for ln in lines if ln.startswith(MIRROR_MARK)), "")
        bases = [ln[len(BASE_MARK):] for ln in lines if ln.startswith(BASE_MARK)]
        if mirror and not MIRROR_RE.fullmatch(mirror):
            mirror = ""
        _say(f"checking {head[:12]} on {host} in {tmp}")
        q = shlex.quote(tmp)
        deadline = time.monotonic() + _knob("TTP_REMOTE_COPY_LIMIT_S", SETUP_TIMEOUT_S)
        tree_copy = _knob("TTP_REMOTE_TREE_COPY", 0) == 1
        copied = False
        if mirror and not bases and (seed := seed_path(host)):   # a new mirror: from the clone there first
            said: list = []
            ours = _git("-C", top, "rev-list", "--first-parent", head).split()[::int(_knob("TTP_REMOTE_SEED_SAMPLE",
                                                                                         SEED_SAMPLE)) or 1]
            why = _host_step(host, _seed_script(mirror, seed, ours[:2000]), direct, deadline,
                             f"seeding {host}'s mirror from {seed} there", said)
            if why.startswith(MUX):
                return FAULT_RC, why[len(MUX):]
            if why.startswith(LINK):
                return _fault(host, why[len(LINK):]), ""
            bases = [ln.strip()[len(BASE_MARK):] for ln in "".join(said).splitlines()
                     if ln.strip().startswith(BASE_MARK)]
            _say(f"{why}; sending the whole history instead" if why else
                 f"seeded {host}'s mirror from {seed}" + ("" if bases else ": it has none of this history"))
        if mirror:
            ref = f"refs/heads/check/{int(time.time())}-{secrets.token_hex(6)}"
            why = _push(host, top, head, mirror, ref, bases, direct, deadline)
            if not why:
                cleanup_ref = (mirror, ref)
                why = _host_step(host, _checkout_script(q, mirror, ref, head), direct, deadline,
                                 f"checking {head[:12]} out of its mirror there")
            copied = not why
            if why.startswith(MUX):
                return FAULT_RC, why[len(MUX):]
            if why.startswith(LINK):
                return _fault(host, why[len(LINK):]), ""
            if why and not tree_copy:
                return _fault(host, f"the copy into its mirror failed ({why})"), ""
            if why:
                _say(f"the mirror copy failed ({why}); sending the tree compressed instead")
        if not copied and not tree_copy:   # trees go box to box, never from here, unless the project allows it
            return _fault(host, "no usable git there for its mirror, and tree copies from here are off "
                                "(TTP_REMOTE_TREE_COPY=1 allows them)"), ""
        if not copied:   # no mirror there (no usable git there), or it failed: the tree, compressed
            rc, why = _archive(host, top, head, q, direct, deadline)
            if why.startswith(MUX):
                return FAULT_RC, why[len(MUX):]
            if why:
                return _fault(host, why[len(LINK):] if why.startswith(LINK) else why), ""
            if rc:
                return rc, ""
        from . import testslots
        cfg = testslots._cfg()
        script = _run_script(q, exports(env) + extra, cmd, mark, wrap, testslots.priority_args(cfg))
        if not _TOLD.get("prio"):   # the priority it runs at there, with its settings: once per run, not per retry
            _say(testslots.priority_line(cfg, host))
            _TOLD["prio"] = True
        # stdin stays open and unwritten: its end on the host (we stopped, died or lost ssh) stops the command.
        proc = subprocess.Popen(_ssh(host, script, direct), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        rc, held, no_wrap, refused, wrap_refused = None, b"", False, False, False
        out = sys.stdout.buffer
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode(errors="replace").strip()
            refused = refused or _refused(text)
            if text.startswith(mark) and text[len(mark):].isdigit():
                rc = int(text[len(mark):])
                held = b""   # the blank line printed before the marker is ours
                continue
            if text == mark + WRAP_REFUSED:
                wrap_refused, held = True, b""
                continue
            if text in (mark + NO_WRAP, mark + NO_ENV):
                no_wrap, held = text[len(mark):], b""
                continue
            if held:
                out.write(held)
            held = raw if raw in (b"\n", b"\r\n") else b""
            if not held:
                out.write(raw)
            out.flush()
        proc.wait()
        if no_wrap == NO_ENV:
            return _fault(host, "the checks' pinned Python env could not be made there (its error is above), "
                                "so the check did not run (never with that host's user site)"), ""
        if no_wrap:
            return _fault(host, f"the spill wrapper {wrap[0]!r} is missing or not executable there {WRAP_SETTING}, "
                                "so the check did not run (never unwrapped)"), ""
        if rc is None:
            why = f"ssh ended before the command reported its exit code (ssh exit {proc.returncode})"
            if proc.returncode == 255 and refused:
                return FAULT_RC, why
            return _fault(host, why), ""
        if rc == FAULT_RC and wrap_refused:
            return _fault(host, f"the spill wrapper {wrap[0]!r} refused the run there {WRAP_SETTING} (exit {FAULT_RC} and "
                                f"{WRAP_REFUSED_ENV}; its reason, if any, is above), so the check did not run "
                                "there"), ""
        if rc == FAULT_RC:
            _say(f"the command exited {FAULT_RC}; reported as 1, so it does not read as an unreachable host")
            return 1, ""
        return rc, ""
    except _Stop:
        return 143, ""
    finally:
        for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(s, signal.SIG_IGN)
        if proc is not None and proc.poll() is None:
            try:   # closes stdin: the host stops the command's group, removes its directory and exits
                proc.communicate(timeout=STOP_GRACE_S + 15)
            except (OSError, ValueError, subprocess.SubprocessError):
                proc.kill()
                proc.wait()
        if proc is not None and proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        if tmp:
            try:
                # Not a bare `rm -rf`: a run whose ssh broke goes on there with its folder gone, owned by
                # nothing. Its own process groups are stopped first, then the folder goes.
                from . import orphanchecks
                subprocess.run(_ssh(host, (f"{_mirror_cleanup(*cleanup_ref)}\n" if cleanup_ref else "")
                                    + orphanchecks.reap_script([tmp], STOP_GRACE_S), direct),
                               stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=CLEANUP_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError):
                _say(f"could not remove {tmp} on {host}")

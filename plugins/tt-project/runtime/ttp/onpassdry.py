# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dry run of a waiting hand-off's `on_pass_cmd` (waiting.on_pass_dry_run, on by default).

An on_pass_cmd that cannot run at all (a script that is not there, a program not on PATH, a syntax
error) used to show only once its job finished, and that cost a full wake. When a run hands off
`waiting` with one, the daemon checks it once right away, model-free, before it records the hand-off
(Daemon._prefetch_remote, outside the transaction). It never runs the command: an on_pass_cmd is
typically a publish step (a push, a PR edit) whose side effects must wait for the probe, and an
earlier dry run that ran it with TTP_ON_PASS_DRY_RUN=1 published a head whose checks then failed,
because only `ttp` commands honoured that variable. check() reads it instead, as the real run will
see it (from the project root, with the environment that run gets): the shell parses it (`sh -n`);
each program it starts is a builtin, on PATH or an existing executable file; each script it hands an
interpreter, sources or redirects in exists, and a shell or Python script parses (`<shell> -n`, or
`ast.parse` under the command's own interpreter, run isolated: nothing of the script runs); it uses no
run-only variable ($TTP_RUN_DIR, ...), which is unset when it runs; and something in it or a file it
names writes $TTP_RESULT. waiting.on_pass_dry_run_timeout_s (30 s) bounds the whole check.

A problem hands the run back: its task is queued again at once and its next run continues the same
agent session with the reason (Daemon._resumable), once per hand-off. Where that session cannot be
continued (or this is already the handed-back run), the hand-off stands with a warning in its summary
and the task's notes. A check that could not finish (a timeout) records 'dry run inconclusive'.

No hand-off writer (verdict NO_RESULT, handed back as a problem is): a command that would exit 0
without recording done once its probe passes. Nothing in it, a script it names or a program it starts
mentions TTP_RESULT, it starts no `ttp` and no `python -m` module (either may write it), and its
retry_when is no `ttp checks --result` wait whose on_pass is the fallback. A command that sets
TTP_RESULT itself is refused the same way: the daemon reads only the $TTP_RESULT it sets.
ENV is never set by the daemon now; set by hand, `ttp note`, `notify`, `push` and `land` do nothing
under it and `ttp lock`, `devq submit|start|clear` and a starting `detach` are refused (cli.dry_run_stub)."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path

from . import boundedchild
from .probevars import RUN_VARS, _REF
from .project import clean_line, durable_write

ENV = "TTP_ON_PASS_DRY_RUN"
FILE = "on_pass_dry_run.json"     # in the run's folder: the dry run's outcome, read when its hand-off is recorded
KEY = "on_pass_dry_run_handback"  # in the task's result: the run handed back, its session and the dry run's output
NOTE_KEY = "on_pass_dry_run"      # in the task's result: an inconclusive or warned dry run, one line
DEFAULT_TIMEOUT_S = 30
CRASH = "crash"           # verdict: it cannot run as written
NO_RESULT = "no_result"   # verdict: it would exit 0 without recording done, handed back as a crash is
# An assignment to TTP_RESULT other than passing the daemon's own on ("TTP_RESULT=$TTP_RESULT ...").
SETS_RESULT_RE = re.compile(r"(?<![\w$])TTP_RESULT=(?![\"']?\$\{?TTP_RESULT\b)")
SCRIPT_BYTES = 1 << 20    # a file the command names is read for TTP_RESULT up to this size
ON_PASS_VARS = ("TTP_RESULT", "TTP_NOTE_RUN_DIR", "TTP_TASK", "TTP_TASK_ID")   # the run vars its real run sets
BUILTINS = frozenset(". : [ [[ alias bg break builtin cd command continue declare echo eval exec exit export false fg "
                     "getopts hash jobs kill let local printf pushd popd pwd read readonly return set shift source "
                     "test times trap true type typeset ulimit umask unalias unset wait".split())
KEYWORDS = frozenset("if then else elif fi do done while until ! { } time".split())
WRAPPERS = frozenset("env exec nohup command nice setsid stdbuf timeout".split())
SHELLS = frozenset("sh bash dash zsh ksh".split())
REDIRECTS = frozenset("< > >> >& <& &> &>> >| <>".split())
# Constructs whose words the simple reading below would misplace: only the parse and variable checks apply.
OPAQUE_RE = re.compile(r"\$\(|`|<<|\(\s*\)|\bcase\b|\bfor\b|\bselect\b|\bfunction\b")
# Parses a Python script without running any of it: only this code runs, isolated (-I) from the cwd.
PY_PARSE = "import ast, sys\nast.parse(open(sys.argv[1], 'rb').read(), sys.argv[1])"


def enabled(cfg: dict) -> bool:
    return (cfg.get("waiting") or {}).get("on_pass_dry_run", True) is not False


def timeout_s(cfg: dict) -> float:
    try:
        return float((cfg.get("waiting") or {}).get("on_pass_dry_run_timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT_S)


def _expand(word: str, env: dict) -> str | None:
    """`word` with ~ and $VAR written in from `env`, or None where only running it would tell (a
    variable its real run sets or that is unset here, a glob)."""
    if word == "~" or word.startswith("~/"):
        if not env.get("HOME"):
            return None
        word = env["HOME"] + word[1:]

    def value(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name in ON_PASS_VARS or not env.get(name):
            raise KeyError(name)
        return env[name]
    try:
        word = re.sub(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", value, word)
    except KeyError:
        return None
    return None if re.search(r"[$*?\[]", word) else word


def _commands(cmd: str) -> list[list[str]] | None:
    """The simple commands of `cmd`, each its words, or None where reading it so could misplace them."""
    if OPAQUE_RE.search(cmd) or "#" in cmd:
        return None
    lex = shlex.shlex(cmd.replace("\\\n", " "), posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|()\n"):
            out, cur = out + [cur] if cur else out, []
        else:
            cur.append(t)
    return out + [cur] if cur else out


def _strip(words: list[str]) -> tuple[list[str], list[str]]:
    """A simple command's words without redirections, leading assignments, keywords and wrappers
    (env, timeout, ...), and the files it redirects in."""
    ins, clean, i = [], [], 0
    while i < len(words):
        w = words[i]
        if w.isdigit() and i + 1 < len(words) and words[i + 1] in REDIRECTS:
            i += 1
            continue
        if w in REDIRECTS:
            if w == "<" and i + 1 < len(words):
                ins.append(words[i + 1])
            i += 2
            continue
        clean.append(w)
        i += 1
    while clean and (clean[0] in KEYWORDS or re.match(r"[A-Za-z_]\w*=", clean[0])):
        clean.pop(0)
    while clean and clean[0] in WRAPPERS:
        wrapper = clean.pop(0)
        while clean and (clean[0].startswith("-") or (wrapper == "env" and re.match(r"[A-Za-z_]\w*=", clean[0]))):
            opt = clean.pop(0)
            if opt in ("-s", "-k", "-n", "-u", "-C") and clean:
                clean.pop(0)
        if wrapper == "timeout" and clean:
            clean.pop(0)
    return clean, ins


def _interp(name: str) -> str:
    """'sh' for a shell, 'python', or 'other' for another interpreter that takes a script, else ''."""
    if name in SHELLS:
        return "sh"
    if re.fullmatch(r"python[\d.]*", name):
        return "python"
    return "other" if name in ("node", "perl", "ruby") else ""


def _shebang(path: Path) -> tuple[str, str] | None:
    """(interpreter name, its executable) a script's #! line names, else None."""
    try:
        with path.open("rb") as f:
            line = f.readline(512).decode(errors="replace")
    except OSError:
        return None
    words = line[2:].split() if line.startswith("#!") else []
    if words and Path(words[0]).name == "env":
        words = [w for w in words[1:] if not w.startswith("-")]
    return (Path(words[0]).name, words[0]) if words else None


def _execs_early(path: Path) -> bool:
    """Whether a script's first lines hand it to another interpreter (`exec ... "$0"`)."""
    try:
        with path.open("rb") as f:
            head = b"".join(f.readline(512) for _ in range(12))
    except OSError:
        return False
    return re.search(rb"\bexec\b[^\n]*\$\{?0\b", head) is not None


def _script_arg(kind: str, args: list[str]) -> tuple[str | None, bool]:
    """The script an interpreter of `kind` is handed in `args`, and whether it runs a module (-m)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (args[i + 1] if i + 1 < len(args) else None), False
        if not a.startswith("-") or a == "-":
            return (None if a == "-" else a), False
        if kind == "python" and a.startswith("-m"):
            return None, True
        if a.startswith("-") and not a.startswith("--") and ("c" in a[1:] or "e" in a[1:] and kind == "other"):
            return None, False
        i += 2 if a in ("-o", "-O", "-W", "-X", "-I" if kind == "other" else "") else 1
    return None, False


class _Deadline(Exception):
    pass


def check(cmd: str, cwd: Path, env: dict, fallback: bool, timeout: float) -> dict:
    """Validate `cmd` for its run once its probe passes, without running it (module docstring):
    {cmd, rc: None, verdict ('ok', CRASH, NO_RESULT or 'inconclusive'), why, output, checked, took_s}.
    `fallback`: its hand-off's on_pass is recorded when the command writes nothing."""
    started = time.monotonic()
    problems, checked, reads, unknown = [], [], [], False

    def run(argv: list[str]) -> boundedchild.Result:
        left = timeout - (time.monotonic() - started)
        if left <= 0:
            raise _Deadline(f"its check timed out after {round(time.monotonic() - started, 1)} s")
        got = boundedchild.run(argv, left, cwd=str(cwd), env=env)
        if got.rc is None:   # timed out, or the checker itself could not start: not knowable, not broken
            raise _Deadline(f"its check {got.error or 'ended without an exit code'}")
        return got

    def parses(kind: str, exe: str, path: Path) -> None:
        if kind == "sh":
            got, what = run([exe, "-n", str(path)]), "shell syntax"
        elif kind == "python":
            got, what = run([exe, "-I", "-c", PY_PARSE, str(path)]), "Python syntax"
        else:
            return
        checked.append(f"{what} of {path}")
        if got.rc:
            tail = [x for x in (got.err or "").splitlines() if x.strip()][-3:]
            problems.append(f"{path} does not parse ({what}): {clean_line(' | '.join(tail), 300) or f'exit {got.rc}'}")

    def file_at(word: str, moved: bool, what: str) -> Path | None:
        """The file `word` names, checked to exist (None: missing, said, or not knowable without running)."""
        nonlocal unknown
        got = _expand(word, env)
        if got is None or (moved and not os.path.isabs(got)):
            unknown = True
            return None
        path = Path(got) if os.path.isabs(got) else Path(cwd) / got
        if not path.is_file():
            problems.append(f"{word}: no such file ({what})")
            return None
        reads.append(path)
        return path

    try:
        shell = "/bin/sh" if os.path.exists("/bin/sh") else "sh"   # the real run's shell (shell=True)
        got = run([shell, "-n", "-c", cmd])
        checked.append("shell syntax")
        if got.rc:
            problems.append(f"it does not parse: {clean_line((got.err or '').strip(), 300) or f'exit {got.rc}'}")
        for m in _REF.finditer(cmd):
            name = m.group(1) or m.group(2)
            if name in RUN_VARS and name not in ON_PASS_VARS and not env.get(name):
                problems.append(f"it uses ${name}, which is unset when it runs (from the project root, once its "
                                f"probe passes): write its value in")
        commands = None if problems else _commands(cmd)
        unknown = commands is None
        moved = False
        for words in commands or ():
            words, ins = _strip(words)
            for w in ins:
                file_at(w, moved, "redirected in")
            if not words:
                continue
            prog, args = words[0], words[1:]
            if prog in ("cd", "pushd", "popd"):
                moved = True
                continue
            if prog in (".", "source"):
                if args and (path := file_at(args[0], moved, "sourced")):
                    parses("sh", shutil.which("bash", path=env.get("PATH")) or shell, path)
                continue
            if prog in BUILTINS:
                continue
            name = Path(prog).name
            unknown |= name == "ttp"   # `ttp ack-wait settle`, `ttp checks --result`, ... may write it
            if "/" in prog:
                exe = file_at(prog, moved, "the program it runs")
                if exe and not os.access(exe, os.X_OK):
                    problems.append(f"{prog}: not executable")
            else:
                found = shutil.which(prog, path=env.get("PATH", os.defpath))
                exe = Path(found) if found else None
                if exe:
                    reads.append(exe)
                else:
                    problems.append(f"{prog}: command not found")
            checked.append(f"program {prog}")
            if not exe:
                continue
            kind = _interp(name)
            if kind:
                script, module = _script_arg(kind, args)
                unknown |= module
                if script and (path := file_at(script, moved, f"the script {name} runs")):
                    parses(kind, str(exe), path)
            elif "/" in prog and name != "ttp" and (bang := _shebang(exe)) and (kind := _interp(bang[0])):
                # Only a script the command names by path; one that execs another interpreter early (a
                # shell-and-Python file, as `ttp` is) parses only as that one, so it is not checked.
                bexe = bang[1] if os.path.isabs(bang[1]) else shutil.which(bang[1], path=env.get("PATH"))
                if not bexe or not os.access(bexe, os.X_OK):
                    problems.append(f"{prog}: its interpreter {bang[1]} is not there")
                elif not _execs_early(exe):
                    parses(kind, bexe, exe)
    except _Deadline as e:
        took = round(time.monotonic() - started, 1)
        return {"cmd": cmd, "rc": None, "verdict": "inconclusive", "why": str(e),
                "output": "\n".join(problems) or "(not run)", "checked": checked, "took_s": took}
    out = {"cmd": cmd, "rc": None, "output": "\n".join(problems) or "(not run)", "checked": checked}
    if problems:
        out.update(verdict=CRASH, why=problems[0])
    elif not (fallback or unknown or mentions(cmd, cwd, "TTP_RESULT", reads)):
        out.update(verdict=NO_RESULT, why=("nothing in it, or a script or program it names, writes a hand-off to "
                                           "$TTP_RESULT, so once its probe passes it would wake a model run instead "
                                           "of recording done"))
    else:
        out["verdict"] = "ok"
    return {**out, "took_s": round(time.monotonic() - started, 1)}


def sets_result(cmd: str) -> bool:
    """Whether `cmd` points TTP_RESULT somewhere itself (the daemon never reads that file)."""
    return bool(SETS_RESULT_RE.search(cmd))


def refused(cmd: str, why: str) -> dict:
    """The outcome of a command that fails its contract as written: a NO_RESULT for `why`."""
    return {"cmd": cmd, "rc": None, "output": "(not run)", "verdict": NO_RESULT, "why": why, "took_s": 0.0}


def mentions(cmd: str, cwd: Path, needle: str, files=()) -> bool:
    """Whether `cmd`, a file it names (a script, absolute, under `cwd` or after `--opt=`) or one of
    `files` mentions `needle`."""
    if needle in cmd:
        return True
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    paths = list(files)
    for word in words:
        path = Path(word.split("=", 1)[-1]).expanduser()
        paths.append(path if path.is_absolute() else Path(cwd) / path)
    for path in paths:
        try:
            if path.is_file() and needle.encode() in path.open("rb").read(SCRIPT_BYTES):
                return True
        except (OSError, ValueError):
            continue
    return False


def save(run_dir: Path, got: dict) -> None:
    durable_write(Path(run_dir) / FILE, json.dumps(got, indent=1))


def load(run_dir: Path, cmd) -> dict | None:
    """The dry run recorded in `run_dir` for this command, else None."""
    try:
        got = json.loads((Path(run_dir) / FILE).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("cmd") == cmd else None


def said(got: dict) -> str:
    """How it went: 'checked, not run', or 'not run' where it was refused unchecked."""
    return "checked, not run" if got.get("checked") is not None else "not run"


def handback_text(got: dict) -> str:
    if got.get("verdict") == NO_RESULT:
        return (f"the dry run of your hand-off's on_pass_cmd failed its contract ({said(got)}): {got.get('why')}. "
                f"On a passed probe it must exit 0 with a done or needs_review hand-off written to the "
                f"$TTP_RESULT the daemon sets. Fix the command, then write the waiting hand-off again. "
                f"Command: {got['cmd'][:300]}")
    return (f"the dry run of your hand-off's on_pass_cmd found it cannot run as written ({said(got)}), so it "
            f"would fail once its probe passes. The daemon runs it only then. Fix the command or what it "
            f"needs, then write the waiting hand-off again. Command: {got['cmd'][:300]}\n"
            f"What the check found:\n{got['output']}")


def warning(got: dict) -> str:
    if got.get("verdict") == NO_RESULT:
        return f"WARNING: on_pass_cmd dry run: {got.get('why')} ({said(got)})"
    return f"WARNING: on_pass_cmd cannot run as written; it will fail when its probe passes: {got['output'][-300:]}"


def inconclusive(got: dict) -> str:
    return f"dry run inconclusive ({got.get('why') or said(got)})"

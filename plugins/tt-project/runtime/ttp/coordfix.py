# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The session coordinator's hands-on fixes, on the record (it fixes the work, the
workers and the project's own prompts, scripts, charter notes and config itself).

A full-tools coordinator's mutating calls are logged to its run's AUDIT file (coordtools.post_tool_use).
Before a fix it runs `ttp audit why "<why>" --serves <ref> --undo "<how>"` (cmd_audit): that writes the
run's INTENT, and every mutating call after it in the same run (one wake) carries the why, the order or
task it serves (`o171`, `msg1776` or `task6302`; the parser also takes `#` forms but the examples
show the shell-safe ones: an unquoted `#` starts a comment in bash) and the undo, until the next
`ttp audit why` or
`ttp audit why --done`. Each entry's what is the Bash call's description, else its tool and target.

A state-changing call with no why is flagged once, in the next coordinator digest (digest_lines: the
calls since the last turn started); the daily review lists the day's fixes grouped by the order they
serve (audit_line), and `ttp audit fixes [--since <time>]` prints them (fixes_text). A Bash call that
only reads (reads_only: cat, git log, `ttp eta`, launchctl print, `ssh <alias> true`) is logged and
listed apart as READS, never flagged; a command it cannot place counts as a change."""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from .coordtools import AUDIT, READ_COMMANDS
from .sidefx import HARMLESS, classify_paths

INTENT = "coord-intent.json"   # in the run dir: the why the next mutating calls carry
NO_WHY = "no why recorded"
READS = "read-only lookups (no why needed)"
SHOWN = 8   # entries per group in one line


def serves(raw: str) -> str:
    """The order or task a fix serves, normalised: `o171` (an obligation), `user msg #N` or
    `task #N`. ValueError for anything else."""
    s = re.sub(r"\s+", "", str(raw or "")).lower()
    if m := re.fullmatch(r"o(\d+)", s):
        return f"o{int(m[1])}"
    if m := re.fullmatch(r"(?:user)?(?:msg|message|m)#?(\d+)", s):
        return f"user msg #{int(m[1])}"
    if m := re.fullmatch(r"(?:task)?#?(\d+)", s):
        return f"task #{int(m[1])}"
    raise ValueError(f"--serves {raw!r}: name an obligation (o171), a user message (msg1776) or a task (task6302)")


def intent_check(why: str, serves_ref: str, undo: str) -> tuple[str, str, str]:
    """The three fields of an intent, cleaned; ValueError on a missing one or a bad --serves."""
    why, undo = " ".join(str(why or "").split()), " ".join(str(undo or "").split())
    if not why:
        raise ValueError("say why: ttp audit why \"<why>\" --serves <ref> --undo \"<how>\"")
    if not undo:
        raise ValueError("--undo: the command or steps that undo it (`none: <why>` when nothing can)")
    return why, serves(serves_ref), undo


def declare(run_dir: str | Path, why: str, serves_ref: str, undo: str) -> dict:
    """Write the run's intent (module doc); ValueError on a missing field or a bad --serves."""
    why, ref, undo = intent_check(why, serves_ref, undo)
    rec = {"ts": time.time(), "why": why[:500], "serves": ref, "undo": undo[:500]}
    from .project import durable_write
    durable_write(Path(run_dir) / INTENT, json.dumps(rec))
    return rec


def done(run_dir: str | Path) -> None:
    """End the run's intent: later mutating calls carry no why until the next declare."""
    (Path(run_dir) / INTENT).unlink(missing_ok=True)


def intent(run_dir: str | Path) -> dict:
    """The run's current {why, serves, undo}, or {} (none, or unreadable)."""
    try:
        rec = json.loads((Path(run_dir) / INTENT).read_text())
        return {k: str(rec[k]) for k in ("why", "serves", "undo") if rec.get(k)} if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def entries(db, since: float, until: float | None = None) -> list[dict]:
    """The coordinator's logged mutating calls with since <= ts < until, oldest first, each with
    its run id; a run dir that is gone or a bad line is skipped."""
    until = time.time() + 1 if until is None else until
    rows = db.q("SELECT id, dir FROM runs WHERE role='coordinator' AND dir IS NOT NULL AND started<? "
                "AND (ended IS NULL OR ended>=?) ORDER BY id", (until, since))
    out = []
    for r in rows:
        try:
            text = (Path(r["dir"]) / AUDIT).read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                e = json.loads(line)
                ts = float(e.get("ts") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if since <= ts < until:
                out.append({**e, "ts": ts, "run": r["id"]})
    return sorted(out, key=lambda e: e["ts"])


# reads_only: commands that only read, whatever their arguments (a redirect is checked apart) ...
PLAIN_READS = {"cat", "head", "tail", "less", "more", "ls", "stat", "wc", "diff", "cmp", "file", "md5sum", "sha256sum",
               "test", "[", "jq", "uniq", "cut", "readlink", "realpath", "du", "tree", "grep", "egrep", "fgrep", "rg",
               "ag", "echo", "printf", "pwd", "date", "whoami", "id", "hostname", "uname", "which", "type", "true",
               "false", "df", "ps", "pgrep", "uptime", "free", "nproc", "basename", "dirname", "tr", "column", "nl",
               "comm", "paste", "tac", "seq", "sleep", "cd", "xxd", "od", "strings", "env", "printenv", "lsof",
               "lspci", "lsusb", "lsblk", "lscpu", "lsmod", "modinfo", "sensors", "zcat", "zgrep", "squeue", "sinfo",
               "sacct", "sshare", "sprio"}
WRAPPERS = {"timeout": 1, "nice": 0, "time": 0, "nohup": 0, "command": 0, "stdbuf": 0, "env": 0}   # words they take
GIT_READS = {"log", "show", "status", "diff", "rev-parse", "merge-base", "ls-remote", "fetch", "ls-files", "ls-tree",
             "cat-file", "blame", "describe", "shortlog", "rev-list", "for-each-ref", "name-rev", "show-ref", "grep",
             "reflog", "cherry", "range-diff", "count-objects", "check-ignore", "check-attr", "verify-commit", "help",
             "version", "whatchanged", "show-branch"}
# ... and `ttp` commands that only read (READ_COMMANDS too), as word prefixes ("*": any one word)
TTP_READS = READ_COMMANDS + (("paths",), ("list",), ("show",), ("quarantine", "list"), ("memory", "--search"),
                             ("audit", "fixes"), ("checks", "--result"), ("lock", "--probe"), ("upstream", "--fixed"),
                             ("push", "--queue"), ("push", "--result"), ("--help",), ("-h",), ("help",),
                             ("--version",), ("task", "*", "eta"), ("bridge", "status"), ("overview",), ("runs",), ("schedules", "*", "grace"),
                             ("schedules", "*", "show"), ("schedules", "*", "list"), ("obligation", "list"),
                             ("task", "show"), ("occupancy",))   # the project may be left out: it is the one here
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{"}   # lead a command; `done`/`fi` end one
PY_NAMES = re.compile(r"python(3(\.\d+)?)?")
# python -c code that may write or run something else (besides an open() whose mode is not a plain read
# literal, _py_writes): an os.O_* write flag, subprocess, os calls that change files or run commands,
# shutil, path methods that write, and anything that reaches code or names it cannot see
PY_WRITES = re.compile(r"""\bO_(WRONLY|RDWR|CREAT|APPEND|TRUNC)\b|\b(setattr|delattr|vars|locals|FileIO)\b|"""
                       r"""\b__\w+__\b|\bmodules\b|\bsubprocess\b|\bshutil\b|\bos\s*\.\s*(open|system|popen|exec|spawn|"""
                       r"""remove|unlink|rename|replace|rmdir|removedirs|mkdir|makedirs|chmod|chown|kill|killpg|truncate|"""
                       r"""symlink|link|write|fork|utime|setxattr)|\.(unlink|rename|write_text|write_bytes|mkdir|rmdir|"""
                       r"""touch|chmod|symlink_to|hardlink_to)\s*\(|\bfrom\s+(os|shutil|subprocess)\b|\b__import__\b|"""
                       r"""\b(exec|eval|compile|getattr|globals|breakpoint)\b|\b(importlib|ctypes|pty|socket|sqlite3|"""
                       r"""urllib|http|requests|tempfile|pickle|signal|multiprocessing|threading|asyncio|ttp)\b""")
LAUNCHCTL_READS = {"print", "list", "print-disabled", "blame", "version", "help"}
SYSTEMCTL_READS = {"status", "show", "list-units", "list-timers", "list-unit-files", "is-active", "is-enabled",
                   "is-failed", "cat", "list-dependencies", "list-sockets"}

TT_SMI_READS = {"-s", "--snapshot", "-ls", "--list", "-v", "--version", "-h", "--help"}
DMESG_WRITES = re.compile(r"-[a-zA-Z]*[cCDEn][a-zA-Z]*|--(?:clear|read-clear|console-\w+)\b.*")
IPMI_OPT_ARG = {"-I", "-H", "-U", "-P", "-f", "-L", "-p", "-y", "-Y", "-C", "-k", "-K", "-t", "-b", "-T", "-B", "-m",
                "-l", "-N", "-R", "-o", "-O", "-e", "-A", "-D", "-z"}
IPMI_READ = re.compile(r"(?:sdr|sensor)(?:\s+(?:list|elist|get|type|info|reading)\b.*)?$|sel(?:\s+(?:list|elist|info|get)\b.*)?$"
                       r"|fru(?:\s+(?:print|list)\b.*)?$|(?:chassis\s+)?(?:status|power\s+status)$|mc\s+(?:info|guid|selftest)$"
                       r"|lan\s+print\b.*$|dcmi\s+(?:power\s+reading|discover|sensors)\b.*$")


def _ipmi_words(args: list[str]) -> list[str]:
    """ipmitool's subcommand words: its options and their values dropped."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in IPMI_OPT_ARG:
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


def _simple(cmd: str) -> list[list[str]] | None:
    """`cmd`'s simple commands as their words, or None when it may write or run what this cannot read: a
    substitution, a heredoc, a background job, or a redirect into anything but a device or another fd."""
    if "`" in cmd or "$(" in cmd or "<(" in cmd or ">(" in cmd or "<<" in cmd:
        return None
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace, lex.whitespace_split, lex.commenters = " \t\r", True, ""
        toks = list(lex)
    except ValueError:
        return None
    out, words, redirect = [], [], None
    for t in toks:
        if t and all(c in "();<>|&\n" for c in t):
            if t not in ("&&", "||", "|", "|&", ";", "\n", ";\n") and not ("<" in t or ">" in t):
                return None   # a background job, a subshell, an odd operator
            if "<" in t or ">" in t:
                if words and words[-1].isdigit():
                    words.pop()   # `2>`: the fd, no word
                redirect = t
                continue
            out.append(words) if words else None
            words = []
        elif redirect:
            if ">" in redirect and not (redirect.endswith("&") and t.isdigit() or t.startswith(HARMLESS)):
                return None
            redirect = None
        else:
            words.append(t)
    if redirect:
        return None
    return out + ([words] if words else [])


def _substituted(cmd: str, depth: int) -> str | None:
    """`cmd` with each `$( ... )` that only reads (reads_only, one level deeper) replaced by a plain word;
    None when one may write, cannot be matched, or is arithmetic (`$((`)."""
    out, i, quote = [], 0, ""
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'":
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if c == "'" and quote != '"' or c == '"' and quote != "'":
            quote = "" if quote == c else c
        elif cmd.startswith("$(", i) and quote != "'":
            if cmd.startswith("$((", i):
                return None
            level, j = 1, i + 2
            while j < len(cmd) and level:
                level += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            if level or not reads_only(cmd[i + 2:j - 1], depth + 1):
                return None
            out.append("TTP_SUB")
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _unwrapped(simple: list[list[str]]) -> list[list[str]] | None:
    """The simple commands inside `for`/`if`/`while` lines: each keyword that leads a command dropped, a
    `for NAME [in WORDS]` header and a lone `done`, `fi` or `}` gone; None for a malformed `for`."""
    out = []
    for w in simple:
        while w and w[0] in SHELL_KEYWORDS:
            w = w[1:]
        if not w or w in (["done"], ["fi"], ["}"]):
            continue
        if w[0] == "for":
            if len(w) < 2 or not re.fullmatch(r"[A-Za-z_]\w*", w[1]) or len(w) > 2 and w[2] != "in":
                return None
            continue
        out.append(w)
    return out


def _py_writes(code: str) -> bool:
    """True when python `code` may write or run something: it does not parse, imports a module under
    another name, calls open() (or a .open()) with a mode that is not a plain read literal or uses open
    other than by calling it, or matches
    PY_WRITES."""
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return True
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for n in ast.walk(tree):
        if (isinstance(n, ast.Name) and n.id == "open" or isinstance(n, ast.Attribute) and n.attr == "open") \
                and id(n) not in called:
            return True   # open passed around or renamed: its mode cannot be read
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any(a.asname for a in n.names):
            return True
        if isinstance(n, ast.Call) and (isinstance(n.func, ast.Name) and n.func.id == "open"
                                        or isinstance(n.func, ast.Attribute) and n.func.attr == "open"):
            pos = 0 if isinstance(n.func, ast.Attribute) else 1   # Path(x).open(mode), open(x, mode)
            mode = n.args[pos] if len(n.args) > pos else next((k.value for k in n.keywords if k.arg in ("mode", None)), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                         and set(mode.value) <= set("rbtU")):
                return True
    return bool(PY_WRITES.search(code))


def _python_reads(args: list[str]) -> bool:
    """`python3 [-I -S -E -B -s -u] -c CODE [args]` whose code opens nothing for writing and runs nothing
    else (_py_writes); a script, a module or stdin counts as a change."""
    k = 0
    while k < len(args) and args[k] in ("-I", "-S", "-E", "-B", "-s", "-u", "-O", "-OO", "-q"):
        k += 1
    return k + 1 < len(args) and args[k] == "-c" and not _py_writes(args[k + 1])


def _git_reads(args: list[str]) -> bool:
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else args[1:]
    if not args:
        return False
    sub, rest = args[0], args[1:]
    if sub in GIT_READS:
        return not (sub == "fetch" and any(a.startswith("--write") or a == "--prune" for a in rest))
    names = [a for a in rest if not a.startswith("-")]
    if sub in ("branch", "tag"):   # listing only: no name to make and no flag that writes
        listing = {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--merged", "--no-merged",
                   "--contains", "--no-contains", "--points-at", "--sort", "--format", "--show-current", "-n"}
        return all(a.split("=", 1)[0] in listing for a in rest if a.startswith("-")) and (
            not names or any(a in ("-l", "--list", "--merged", "--no-merged", "--contains", "--points-at")
                             for a in rest))
    if sub == "remote":
        return not rest or rest in (["-v"], ["--verbose"]) or rest[0] in ("show", "get-url")
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "config":
        return any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in rest)
    if sub == "worktree":
        return bool(rest) and rest[0] == "list"
    return False


def _ttp_reads(args: list[str]) -> bool:
    if args and args[0] == "audit" and args[1:2] == ["why"]:
        return True   # the record itself (post_tool_use skips a plain one)
    if args[1:2] in (["--help"], ["-h"]):
        return True   # `ttp CMD --help`: argparse prints and exits (later, a positional may swallow it)
    if args[:1] == ["deployed"]:
        return "--after-push" not in args   # that one records follow-ups and alerts
    if args[:1] == ["config"]:
        return _ttp_config_reads(args[1:])
    if args[:1] == ["detach"]:   # listing or checking jobs; any other form starts or stops one
        return args[1:2] in (["--list"], ["--check"]) and not any(a == "--" or a.startswith(("--stop", "--remote"))
                                                                   for a in args[2:])
    if args[:1] == ["schedules"] and len(args) == 2 and not args[1].startswith("-"):
        return True   # `ttp schedules NAME`: the list
    return any(len(args) >= len(r) and all(w == a or w == "*" and not a.startswith("-") for w, a in zip(r, args)) for r in TTP_READS)


def _ttp_config_reads(args: list[str]) -> bool:
    """`ttp config NAME [KEY]` prints a value; a flag, a third word or `KEY VALUE` with the project left out
    (cmd_config reads a first word naming a config section as a key) may set one."""
    from .project import DEFAULT_CONFIG
    if args[:1] == ["--chat-settable"]:   # whether a coordinator may set a key: prints, sets nothing
        return 1 < len(args) <= 3
    if not args or len(args) > 2 or any(a.startswith("-") for a in args):
        return False
    return len(args) == 1 or args[0].split(".")[0] not in DEFAULT_CONFIG


AWK_WRITES = re.compile(r"\bsystem\s*\(|\||\bprintf?\b[^;{}\n]*>")
AWK_OPTS = {"-F", "-v", "--field-separator", "--assign"}   # each takes one word; any other flag may load code


def _awk_reads(args: list[str]) -> bool:
    """awk whose inline program writes nowhere: no print or printf redirect, no pipe (`print | cmd` or
    `cmd | getline`), no system(); options only -F and -v (not -f, -i inplace, -E, -l: code it cannot see)."""
    k = 0
    while k < len(args) and args[k].startswith("-") and args[k] != "-":
        if args[k] == "--":
            k += 1
            break
        if args[k] in AWK_OPTS:
            k += 2
        elif args[k][:2] in ("-F", "-v") or args[k].split("=", 1)[0] in AWK_OPTS:
            k += 1
        else:
            return False
    if k >= len(args):
        return False
    return not AWK_WRITES.search(args[k].replace("||", " "))


def _ssh_reads(args: list[str], depth: int) -> bool:
    k = 0
    while k < len(args) and args[k].startswith("-"):
        flag = args[k]
        k += 2 if len(flag) == 2 and flag[1] in "bcDEeFIiJLlmOoPpRSWw" else 1
    rest = args[k + 1:]
    return bool(rest) and reads_only(" ".join(rest), depth + 1)


def _words_read(w: list[str], depth: int) -> bool:
    while w and re.fullmatch(r"[A-Za-z_]\w*=.*", w[0]):
        w = w[1:]   # VAR=value
    while w and w[0] in WRAPPERS and len(w) > 1:
        skip = WRAPPERS[w[0]]
        w = w[1:]
        while w and (w[0].startswith("-") or re.fullmatch(r"[A-Za-z_]\w*=.*", w[0])):
            w = w[1:]
        w = w[skip:]
    if not w:
        return True
    name, args = w[0].rsplit("/", 1)[-1], w[1:]
    if name in PLAIN_READS:
        return name != "env" or not args
    if name == "pmset":   # `pmset -g ...` only queries; every other form changes power settings
        return args[:1] == ["-g"]
    if name == "scutil":   # `scutil --nc list|status SVC` only queries; start, stop and --set change
        return args[:1] == ["--nc"] and args[1:2] in (["list"], ["status"])
    if name == "ss":   # socket listings; -K/--kill closes the sockets it lists
        return not any(a == "--kill" or re.fullmatch(r"-[a-zA-Z]*K[a-zA-Z]*", a) for a in args)
    if name in ("sort",):
        return not any(a.startswith(("-o", "--output")) for a in args)
    if name == "sed":
        return not any(a.startswith(("-i", "--in-place")) or re.fullmatch(r"-[a-zA-Z]*i[a-zA-Z]*", a) for a in args)
    if name in ("awk", "gawk", "mawk", "nawk"):
        return _awk_reads(args)
    if name == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls",
                             "-fprint0") for a in args)
    if name == "journalctl":
        return not any(a.startswith(("--rotate", "--vacuum", "--flush", "--sync", "--relinquish")) for a in args)
    if name == "git":
        return _git_reads(args)
    if PY_NAMES.fullmatch(name):
        return _python_reads(args)
    if name == "ttp":
        return _ttp_reads(args)
    if name == "launchctl":
        return bool(args) and args[0] in LAUNCHCTL_READS
    if name == "systemctl":
        subs = [a for a in args if not a.startswith("-")]
        return bool(subs) and subs[0] in SYSTEMCTL_READS
    if name == "ssh":
        return _ssh_reads(args, depth)
    if name == "ipmitool":
        return bool(IPMI_READ.match(" ".join(_ipmi_words(args))))
    if name == "tt-smi":
        return bool(args) and all(a in TT_SMI_READS for a in args)
    if name == "scontrol":
        return args[:1] == ["show"]
    if name == "dmesg":
        return not any(DMESG_WRITES.fullmatch(a) for a in args)
    return False


def reads_only(cmd: str, depth: int = 0) -> bool:
    """True when every simple command in the shell command `cmd` only reads (the tables above; an ssh
    command by what it runs remotely; `python3 -c` by its code; the body of a for, if or while line and
    each `$( )` alike) and nothing is redirected into a file; False for anything this cannot place, so an
    unknown command counts as a change."""
    if depth > 2:
        return False
    cmd = _substituted(str(cmd or ""), depth)
    simple = _simple(cmd) if cmd is not None else None
    simple = _unwrapped(simple) if simple else None
    if not simple or not all(_words_read(w, depth) for w in simple):
        return False
    try:   # the outside-writes classifier as a second look (tee, -o, sed -i, inline code) at each local
        # command's own words (redirects were read above; an ssh command, by what it runs)
        return not any(c == "write" and not p.startswith(HARMLESS) for w in simple if w[0] != "ssh"
                       for p, (c, _) in classify_paths(shlex.join(w), "/home/-", "/", "/").items())
    except Exception:
        return False


def _reads(e: dict) -> bool:
    """An audit entry that only read: its own mark, else (an older entry) its Bash command read again."""
    if "reads" in e:
        return bool(e["reads"])
    return e.get("tool") == "Bash" and reads_only(str(e.get("target") or ""))


def what(e: dict) -> str:
    w = " ".join(str(e.get("what") or f"{e.get('tool') or '?'} {e.get('target') or ''}").split())
    return w[:160] + (f" (exit {e['exit']})" if e.get("exit") not in (0, None) else "")


def _groups(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    """(order or task, its entries) for rows with a why, by first use; then NO_WHY's; then READS (read-only
    calls with no why: listed, never flagged)."""
    groups: dict[str, list[dict]] = {}
    for e in rows:
        groups.setdefault(str(e.get("serves") or "?") if e.get("why") else READS if _reads(e) else NO_WHY,
                          []).append(e)
    return sorted(groups.items(), key=lambda kv: (kv[0] == READS, kv[0] == NO_WHY))


def digest_lines(db, since: float, now: float | None = None) -> list[str]:
    """The state-changing calls since `since` (the last coordinator turn's start) that carried no why:
    shown once (a read-only one is never flagged)."""
    missing = [e for e in entries(db, since, now) if not e.get("why") and not _reads(e)]
    if not missing:
        return []
    return ([f"## Hands-on changes with no recorded why: {len(missing)} (say why in this turn; before each fix run "
             "`ttp audit why \"<why>\" --serves <o12|msg123|task456> --undo \"<how>\"`)"]
            + [f"- run {e['run']}: {what(e)}" for e in missing[:SHOWN]]
            + ([f"- and {len(missing) - SHOWN} more (`ttp audit fixes`)"] if len(missing) > SHOWN else []))


def audit_line(db, since: float, now: float | None = None) -> str:
    """The daily review's line: the window's fixes grouped by the order they serve, each with its
    why and undo; "" when there were none."""
    rows = entries(db, since, now)
    if not rows:
        return ""
    parts = []
    for key, es in _groups(rows):
        shown = "; ".join(what(e) + (f" (why: {e['why'][:120]}; undo: {e.get('undo', '')[:80]})" if e.get("why") else "")
                          for e in es[:SHOWN])
        parts.append(f"{key} ({len(es)}): {shown}" + (f"; and {len(es) - SHOWN} more" if len(es) > SHOWN else ""))
    return f"coordinator hands-on fixes: {len(rows)}, by order: " + " | ".join(parts)


def fixes_text(db, since: float, zone, now: float | None = None) -> str:
    """`ttp audit fixes`: every entry in the window, grouped by the order it serves, with time,
    run, what, why and undo."""
    from . import timefmt
    rows = entries(db, since, now)
    if not rows:
        return f"no coordinator hands-on fixes since {timefmt.long(since, zone)}"
    out = []
    for key, es in _groups(rows):
        out.append(f"{key}: {len(es)}")
        for e in es:
            out.append(f"  {timefmt.long(e['ts'], zone)} run {e['run']} {what(e)}")
            if e.get("why"):
                out.append(f"    why: {e['why']}\n    undo: {e.get('undo') or '-'}")
    return "\n".join(out)

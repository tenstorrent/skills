# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Worker kill guard: hook.py refuses a worker Bash call that would signal processes the task did
not start. A host is shared with other workers, projects and people, and `pkill -f sleep` ends every
`sleep` of the user, not only the worker's own. The check is model-free and on by default.

Own process: the worker agent (the pid the runner records in `$TTP_RUN_DIR/child.pid`) or one of its
descendants, or (Linux) a process whose environment carries this run's TTP_RUN_DIR, such as a
background job whose shell has ended or a `ttp detach` job. Own group: every process in it is own.
A pid or group with no process left passes: the kill finds nothing.

- pkill passes only when scoped to own processes: -P/--parent, -g/--pgroup or -s/--session with
  every value own (0 is the caller's own group or session). A name, -f or -u alone is refused, and
  killall always is.
- kill passes when every target is own: a literal pid, -<pgid>, 0 (the caller's own group), a job
  spec (%1), $!, $$, a variable the same command sets only from those, or a pid file the command
  reads (`$(cat f)`, `$(<f)`, `$(head -n1 f)`) that holds own pids. -1 and 1 are refused, and so is
  any target the hook cannot resolve before the command runs: pgrep, pidof or ps output, another
  variable or substitution, xargs input. kill -0 (a probe) and kill -l/-L always pass.
- Wrappers (sudo and doas with their own options, env, nohup, timeout, setsid, `ttp lock ... --`),
  `sh -c`/`bash -c`, eval and su -c strings, command substitutions, pipelines and lists are looked
  through; a redirection's fd number (`2>/dev/null`) is not a target.
- Inside an ssh command (its command words or a heredoc it reads) the remote tree cannot be checked:
  pkill and killall are refused; kill of literal pids and groups passes, so a worker can stop its own
  remote job by the pid or group ttp printed; other targets follow the rules above.

Script files a command runs are not read here (`ttp killscan` checks those). A command that names
kill, pkill or killall and cannot be parsed is refused; every other command passes as before.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

KILL_WORD_RE = re.compile(r"(?<![\w.-])(?:kill|pkill|killall)(?![\w.-])")
PATTERN_KILLS = {"pkill", "killall"}
KILLS = PATTERN_KILLS | {"kill"}
# Commands that run the command in their arguments; xargs and parallel feed it pids from input.
RUNNERS = {"xargs", "parallel", "watch", "flock", "chroot", "nsenter", "unshare", "runuser", "doas", "find",
           "strace", "taskset", "chrt", "systemd-run", "setpriv", "prlimit", "builtin", "sg"}
STDIN_RUNNERS = {"xargs", "parallel", "find"}
BY_NAME = re.compile(r"(?<![\w.-])(?:pgrep|pidof|ps|pstree|lsof|fuser|top|jobs)(?![\w.-])")
SSH_VALUE_OPTS = set("bcDEeFIiJLlmOopQRSWw")
PKILL_VALUE_OPTS = set("gGPstuUFqdOr")
PKILL_LONG_VALUE = {"signal", "group", "terminal", "euid", "uid", "pidfile", "ns", "nslist", "queue", "delimiter",
                    "older", "runstates", "env", "cgroup"}
# Signal names any procps or libc may accept after `-` (pkill scans all of argv for -<number> or -<name>),
# a superset on purpose: an extra name only refuses more, a missing one would let a real signal through.
SIGNAL_NAMES = {"ABRT", "ALRM", "BUS", "CHLD", "CLD", "CONT", "EMT", "EXIT", "FPE", "HUP", "ILL", "INFO", "INT", "IO",
                "IOT", "KILL", "LOST", "PIPE", "POLL", "PROF", "PWR", "QUIT", "SEGV", "STKFLT", "STOP", "SYS", "TERM",
                "TRAP", "TSTP", "TTIN", "TTOU", "UNUSED", "URG", "USR1", "USR2", "VTALRM", "WINCH", "XCPU", "XFSZ",
                *(s.name[3:] for s in signal.Signals if s.name.startswith("SIG"))}
SIGNAL_TOKEN_RE = re.compile(r"-(?:\d.*|(?:SIG)?(?:%s|RTM(?:IN|AX)(?:[+-]\d+)?))" % "|".join(sorted(SIGNAL_NAMES)),
                             re.I)
SIGNAL_ZERO_RE = re.compile(r"(?:SIG)?0+", re.I)
OWN_WORDS = {"$!", "${!}", "$$", "${$}", "$BASHPID", "${BASHPID}"}
SUB = "__ttpsub{}__"
SUB_RE = re.compile(r"__ttpsub(\d+)__")
VAR_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
# sudo's and doas's options that take a value (sudo -u root, -p prompt, -g grp; doas -u, -C)
SUDO_VALUE_OPTS = set("ugpChDrRtUTa")
SUDO_LONG_VALUE = {"user", "group", "prompt", "host", "close-from", "chdir", "role", "type", "other-user",
                   "command-timeout", "chroot"}
# A file descriptor written against its redirection (`2>/dev/null`, `1>&2`): not a kill target
FD_RE = re.compile(r"(^|[\s;&|()])(\d+)(?=[<>])")
PIDFILE_RE = re.compile(r"\s*(?:<\s*(\S+)|cat\s+(\S+)|head\s+(?:-n\s*1|-1)\s+(\S+))\s*")

ADVICE = ("kill only what this task started: record `$!` when you start a process and kill that pid, end your "
          "own process group (`kill -- -<pgid>`), and let ttp's own controls (`ttp detach`, `ttp lock`, "
          "`ttp checks`) end their jobs; never kill by name or pattern; an ended task's detached job, or one "
          "holding your priority-1 task's resource: `ttp detach --stop <rc>`; a driver this task started that "
          "listed its pids in a file under the task's results dir: `ttp detach --stop-pids <file>`")
PARSE = ("the kill guard could not parse this command, and a command with kill, pkill or killall that cannot be "
         "parsed is refused; split it into simpler commands")


class Refused(Exception):
    pass


def refusal(cmd: str, cwd: str = "", run_dir: str | None = None) -> str | None:
    """Why the worker Bash command `cmd` is refused (it may signal a process outside the task's own
    tree), else None. Commands that do not name kill, pkill or killall are never looked at."""
    if not KILL_WORD_RE.search(cmd):
        return None
    try:
        _Guard(cwd or os.getcwd(), run_dir if run_dir is not None else os.environ.get("TTP_RUN_DIR", "")).check(cmd)
    except Refused as e:
        return f"{e}; {ADVICE}"
    except Exception:
        return f"{PARSE}; {ADVICE}"
    return None


def substitutions(text: str, subs: list[str]) -> str:
    """`text` with each command or process substitution ($(...), `...`, <(...), >(...), and
    arithmetic $((...))) replaced by a placeholder word; the code inside each is appended to `subs`.
    Single-quoted text is left alone. Unbalanced text raises ValueError."""
    out, i, dq = [], 0, False
    while i < len(text):
        c = text[i]
        if c == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'" and not dq:
            j = text.find("'", i + 1)
            if j < 0:
                raise ValueError("unterminated quote")
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c == "#" and not dq and (i == 0 or text[i - 1] in " \t\n;&|("):   # a comment, to its line's end
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            out.append(text[i:j])
            i = j
            continue
        if c == '"':
            dq = not dq
        start = 2 if (c == "$" or (c in "<>" and not dq)) and text[i + 1:i + 2] == "(" else 1 if c == "`" else 0
        if not start:
            out.append(c)
            i += 1
            continue
        j = _close(text, i + start, "`" if c == "`" else ")")
        subs.append(text[i + start:j])
        out.append(SUB.format(len(subs) - 1))
        i = j + 1
    if dq:
        raise ValueError("unterminated quote")
    return "".join(out)


def _close(text: str, i: int, end: str) -> int:
    """The index of the `end` that closes the substitution whose code starts at `i`."""
    depth, q = 1, ""
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if q:
            q = "" if c == q else q
        elif c in "'\"" and end != "`":
            q = c
        elif end == "`":
            if c == "`":
                return i
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if not depth:
                return i
        i += 1
    raise ValueError("unbalanced substitution")


class _Procs:
    """A snapshot of the host's processes, taken when first needed, and which of them are own."""

    def __init__(self, run_dir: str):
        self.run_dir = run_dir
        self._table: dict[int, tuple[int, int, int]] | None = None
        self._root: int | None = None

    @property
    def table(self) -> dict[int, tuple[int, int, int]]:
        """pid -> (ppid, pgid, sid)"""
        if self._table is None:
            self._table = {}
            if os.path.isdir("/proc/self"):
                for d in os.listdir("/proc"):
                    if d.isdigit() and (s := _stat(int(d))):
                        self._table[int(d)] = s
            else:
                out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,sess="], capture_output=True, text=True,
                                     timeout=10).stdout
                for line in out.splitlines():
                    f = line.split()
                    if len(f) == 4 and all(x.isdigit() for x in f):
                        self._table[int(f[0])] = (int(f[1]), int(f[2]), int(f[3]))
        return self._table

    @property
    def root(self) -> int:
        """The worker agent: the pid the runner recorded for this run, if it is still that process,
        else the hook's parent."""
        if self._root is None:
            self._root = os.getppid()
            try:
                if not self.run_dir:
                    raise OSError
                pid, *token = (Path(self.run_dir) / "child.pid").read_text().split()
                from .runner import proc_start
                if (not token or proc_start(int(pid)) == token[0]):
                    self._root = int(pid)
            except (OSError, ValueError):
                pass
        return self._root

    def exists(self, pid: int) -> bool:
        return pid in self.table

    def own(self, pid: int) -> bool:
        p, n = pid, 0
        while p > 1 and n < 512:
            if p == self.root:
                return True
            p, n = self.table.get(p, (0, 0, 0))[0], n + 1
        return self._env_own(pid)

    def _env_own(self, pid: int) -> bool:
        if not self.run_dir:
            return False
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return False
        return f"TTP_RUN_DIR={self.run_dir}".encode() in env.split(b"\0")

    def members(self, pgid: int, field: int = 1) -> list[int]:
        return [p for p, s in self.table.items() if s[field] == pgid]


def _stat(pid: int) -> tuple[int, int, int] | None:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(f[1]), int(f[2]), int(f[3])
    except (OSError, IndexError, ValueError):
        return None


class _Guard:
    def __init__(self, cwd: str, run_dir: str):
        self.cwd, self.procs = cwd, _Procs(run_dir)
        self.subs: list[str] = []
        self.assigned: dict[str, list[tuple[str, bool]]] = {}
        self.unknown: set[str] = set()

    def check(self, text: str, remote: bool = False, depth: int = 0) -> None:
        """Raise Refused when `text` (shell code, run here or, `remote`, on another host) signals a
        process that is not own."""
        if depth > 6:
            raise ValueError("nested too deep")
        from . import hook
        if not KILL_WORD_RE.search(text) and not SUB_RE.search(text):
            return
        for body in _ssh_heredocs(text):
            self.check(body, True, depth + 1)
        commands, _ = hook._split(text)
        first = len(self.subs)
        flat = substitutions(commands, self.subs)
        self._assignments(flat, remote)
        for code in self.subs[first:]:
            self.check(code, remote, depth + 1)
        for argv in hook._segments(_drop_fds(flat), strip=False):
            self._segment(argv, remote, depth)

    def _assignments(self, flat: str, remote: bool) -> None:
        for m in re.finditer(r"(?:^|[\s;&|(])(?:(?:local|export|declare|readonly|typeset)\s+(?:-\w+\s+)*)?"
                             r"([A-Za-z_]\w*)=(\"[^\"]*\"|'[^']*'|[^\s;&|()]*)", flat):
            self.assigned.setdefault(m[1], []).append((m[2].strip("\"'"), remote))
        # a loop or read variable takes values the hook cannot see, whatever else sets it
        self.unknown.update(re.findall(r"\bfor\s+([A-Za-z_]\w*)", flat))
        for m in re.finditer(r"\bread\b([^;&|\n]*)", flat):
            self.unknown.update(re.findall(r"(?:^|\s)([A-Za-z_]\w*)", m[1]))

    def _segment(self, argv: list[str], remote: bool, depth: int) -> None:
        from . import hook
        wrappers, argv = _command(argv)
        stdin = "xargs" in wrappers
        if not argv:
            return
        name = os.path.basename(argv[0])
        if name not in KILLS and (stdin or name in RUNNERS):
            # a runner's command: `xargs -I {} kill {}`, `flock f kill 7`, `find -exec kill {} ;`
            k = next((i for i, w in enumerate(argv) if os.path.basename(w) in KILLS), 0)
            if k:
                stdin, argv = stdin or name in STDIN_RUNNERS, argv[k:]
                name = os.path.basename(argv[0])
        if name in hook.SHELLS or name in ("su", "runuser"):
            code = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)), None)
            if code:
                self.check(code, remote, depth + 1)
        elif name == "eval":
            self.check(" ".join(argv[1:]), remote, depth + 1)
        elif name == "ssh":
            code = _ssh_command(argv)
            if code:
                self.check(code, True, depth + 1)
        elif name in PATTERN_KILLS:
            self._pkill(name, argv[1:], remote)
        elif name == "kill":
            self._kill(argv[1:], remote, stdin)

    def _pkill(self, name: str, args: list[str], remote: bool) -> None:
        if remote:
            raise Refused(f"{name} on another machine can end other people's jobs there, and that machine's process "
                          "tree cannot be checked from here; kill the pid or group your job printed")
        what = (f"{name} picks processes by name or pattern across the whole host, so it can end other workers', "
                "projects' or people's processes")
        if name == "killall":
            raise Refused(what)
        scope, i = [], 0
        if args and re.fullmatch(r"-(?:\d+|(?:SIG)?[A-Z]{3}[A-Z0-9+-]*)", args[0]):   # -9, -TERM: a signal
            i = 1
            if re.fullmatch(r"-(?:SIG)?0+", args[0], re.I) and _signal_zero(args):
                return   # signal 0 sends nothing: a liveness probe
        while i < len(args):
            a = args[i]
            if a == "--":
                break
            if a.startswith("--"):
                opt, eq, value = a[2:].partition("=")
                if opt in ("parent", "pgroup", "session", *PKILL_LONG_VALUE) and not eq:
                    value, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
                if opt in ("parent", "pgroup", "session"):
                    scope.append(({"parent": "P", "pgroup": "g", "session": "s"}[opt], value))
            elif a.startswith("-") and len(a) > 1:
                for j, ch in enumerate(a[1:], 2):
                    if ch in PKILL_VALUE_OPTS:
                        value = a[j:]
                        if not value:
                            value, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
                        if ch in "Pgs":
                            scope.append((ch, value))
                        break
            i += 1
        if not scope:
            raise Refused(what + "; scope it to this task's own processes (`pkill -P <own pid>`) or kill the pids you "
                                 "started")
        for opt, value in scope:
            for v in value.split(","):
                for t in self._resolve(v, False):
                    if t == "own" or (n := int(t)) == 0 and opt != "P" or not self.procs.exists(n):
                        continue
                    ok = (self.procs.own(n) if opt == "P" else
                          all(self.procs.own(p) for p in self.procs.members(n, 1 if opt == "g" else 2)))
                    if not ok:
                        raise Refused(f"{name} -{opt} {self._show(v)}: {n} is not this task's own process, group or session")

    def _kill(self, args: list[str], remote: bool, stdin: bool) -> None:
        if args[:1] and re.fullmatch(r"-[lL]|--list(?:=.*)?|--table", args[0]):
            return
        sig, rest = None, args
        if args[:1] == ["--"]:
            rest = args[1:]
        elif args[:1] in (["-s"], ["-n"], ["--signal"]):
            sig, rest = (args[1] if len(args) > 1 else ""), args[2:]
        elif args[:1] and args[0].startswith("--signal="):
            sig, rest = args[0].split("=", 1)[1], args[1:]
        elif args[:1] and args[0].startswith("-") and len(args[0]) > 1:
            sig, rest = args[0][1:], args[1:]
            if sig.startswith("s") and len(sig) > 1 and not sig.isupper():   # -sKILL
                sig = sig[1:]
        while sig is not None and rest[:1] and re.fullmatch(r"-[sn].*|--signal(?:=.*)?", rest[0]):
            # a later -s/-n/--signal replaces the signal (`kill -0 -s 9 <pid>` sends 9)
            if rest[0] in ("-s", "-n", "--signal"):
                sig, rest = (rest[1] if len(rest) > 1 else ""), rest[2:]
            else:
                sig, rest = rest[0].split("=", 1)[1] if rest[0].startswith("--") else rest[0][2:], rest[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        if sig is not None and re.fullmatch(r"(?:SIG)?0", sig, re.I) and _signal_zero(args, kill=True):
            return   # signal 0 sends nothing: a liveness probe
        if stdin:
            raise Refused("kill run by xargs (or find, parallel) takes its pids from input the hook cannot see "
                          "before the command runs")
        for t in rest:
            group, shown = t.startswith("-") and t != "-", self._show(t)
            for n in self._resolve(t[1:] if group else t, remote):
                if n == "own":
                    continue
                if (n := int(n)) == 1:
                    raise Refused(f"kill {shown} signals " + ("every process you may signal" if group else "the init process"))
                if n == 0 or remote:
                    continue
                if group:
                    bad = [p for p in self.procs.members(n) if not self.procs.own(p)]
                    if bad:
                        raise Refused(f"kill {shown}: process group {n} holds processes this task did not start "
                                      f"(such as {bad[0]})")
                elif self.procs.exists(n) and not self.procs.own(n):
                    raise Refused(f"kill {shown}: pid {n} is not this task's own process (neither the worker nor a "
                                  "process it started)")

    def _resolve(self, word: str, remote: bool, depth: int = 0) -> list[str]:
        """The pids `word` names, as numbers in text ("own" for $!, $$ or a job spec such as %1, always
        the shell's own); raise Refused when the hook cannot tell before the command runs."""
        if re.fullmatch(r"-?\d+", word):
            return [word]
        if word in OWN_WORDS or word.startswith("%"):
            return ["own"]
        cant = Refused(f"kill target `{self._show(word)}` cannot be resolved before the command runs; use the literal pid, `$!` "
                       "right after starting the job, or a variable set from `$!` in the same command")
        if depth > 4:
            raise cant
        if m := SUB_RE.fullmatch(word):
            code = self.subs[int(m[1])]
            if BY_NAME.search(code):
                raise Refused(f"`{code.strip()}` picks processes by name or pattern, and so would a kill of its pids")
            f = PIDFILE_RE.fullmatch(code)
            if remote or not f:
                raise cant
            return [n for line in self._pidfile(f[1] or f[2] or f[3], bool(f[3]), cant)
                    for w in line for n in self._resolve(w, remote, depth + 1)]
        if (m := VAR_RE.fullmatch(word)) and m[1] not in self.unknown and self.assigned.get(m[1]):
            return [n for value, _ in self.assigned[m[1]] for w in (value.split() or [value])
                    for n in self._resolve(w, remote, depth + 1)]
        raise cant

    def _show(self, word: str) -> str:
        return SUB_RE.sub(lambda m: f"$({self.subs[int(m[1])]})", word)

    def _pidfile(self, path: str, first_line: bool, cant: Refused) -> list[list[str]]:
        def var(m):
            if m[1] not in ("TTP_RUN_DIR", "HOME", "TMPDIR", "PWD") or m[1] not in os.environ:
                raise cant
            return os.environ[m[1]]
        path = VAR_RE.sub(var, path.strip("\"'"))
        f = Path(os.path.expanduser(path))
        f = f if f.is_absolute() else Path(self.cwd) / f
        try:
            text = f.read_text(errors="replace")[:4096]
        except OSError:
            raise cant
        lines = [line.split() for line in text.splitlines()][:1 if first_line else None]
        return lines


def _signal_zero(args: list[str], kill: bool = False) -> bool:
    """Whether pkill (kill, with `kill`) surely sends signal 0. Options are not parsed: which word an option takes
    as its value differs between procps versions and between the shell builtin and /bin/kill (getopt takes
    abbreviated long options and options after the pid; `--r` is --runstates in one pkill and ambiguous in
    another, and a value can be `--`), so every word counts, before and after `--`. Signal words are
    -<number> or -<name> (with or without SIG, any case: pkill takes the first one anywhere in argv) and --s,
    --si ... --signal with its value after `=` or in the next word; for kill also -s and -n, with the value
    joined (-sKILL) or in the next word, and any --ti... --timeout (util-linux kill sends its signal after the
    first). All of them must mean 0; one with no value does not.
    Accepted false refusals (probe with pgrep, or kill -0 on a plain pid): `pkill -0 -- --signal`, a pattern or
    option cluster that looks like a signal word (`pkill -0 -- -HUP`, `pkill -0 -io x`), and a negative process
    group written after kill -0 (`kill -0 -- -4242` reads as signal 4242)."""
    for k, a in enumerate(args):
        head, eq, value = a.partition("=")
        if kill and len(head) >= 4 and "--timeout".startswith(head):
            return False   # util-linux kill --timeout <ms> <signal> sends a second signal
        if kill and a[:2] in ("-s", "-n"):
            head, eq, value = a[:2], len(a) > 2, a[2:]
        if len(head) >= 3 and "--signal".startswith(head) or kill and head in ("-s", "-n"):
            if not eq:
                if k + 1 >= len(args):
                    return False
                value = args[k + 1]
            if not SIGNAL_ZERO_RE.fullmatch(value):
                return False
        elif SIGNAL_TOKEN_RE.fullmatch(a) and not SIGNAL_ZERO_RE.fullmatch(a[1:]):
            return False
    return True


def _command(argv: list[str]) -> tuple[list[str], list[str]]:
    """The wrapper names leading `argv` and the command they run. sudo and doas options are parsed
    here: hook._strip_prefix reads `sudo -n kill` as nice's `-n VALUE` and would hide the kill."""
    from . import hook
    wrappers = []
    while argv:
        stripped = hook._strip_prefix(argv)
        prefix = argv[:len(argv) - len(stripped)]
        s = next((i for i, w in enumerate(prefix + stripped[:1]) if os.path.basename(w) in ("sudo", "doas")), None)
        if s is None:
            return wrappers + [os.path.basename(w) for w in prefix], stripped
        wrappers += [os.path.basename(w) for w in argv[:s + 1]]
        argv = argv[s + 1 + _sudo_opts(argv[s + 1:]):]
    return wrappers, argv


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


def _drop_fds(flat: str) -> str:
    """`flat` without the fd numbers written against a redirection (`kill $! 2>/dev/null`)."""
    from . import hook
    q = hook._quoting(flat)
    return FD_RE.sub(lambda m: m[0] if q[m.start(2)][0] else m[1], flat)


def _ssh_command(argv: list[str]) -> str:
    """The remote command of an ssh argv: its words after the host, joined as ssh joins them."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "--":
            i += 1
            break
        for j, ch in enumerate(argv[i][1:], 2):
            if ch in SSH_VALUE_OPTS:
                i += not argv[i][j:]
                break
        i += 1
    return " ".join(argv[i + 1:])


def _ssh_heredocs(text: str) -> list[str]:
    """The bodies of heredocs that an ssh command reads (they run on the other host)."""
    from . import hook
    out, lines = [], text.split("\n")
    for k, line in enumerate(lines):
        if "<<" not in line or not re.search(r"(?<![\w.-])ssh(?![\w.-])", line):
            continue
        rest = lines[k + 1:]
        for m in hook.HEREDOC_RE.finditer(line):
            piece, _ = hook._piece_at(line, m.start())
            body = []
            for b in rest:
                if (b.lstrip("\t") if m[1] == "-" else b) == m[3]:
                    break
                body.append(b)
            rest = rest[len(body) + 1:]
            if hook._first_word(piece) == "ssh":
                out.append("\n".join(body))
    return out

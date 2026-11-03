# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A worker's `ttp lock <res> -- <cmd...>` is no way around `ttp host-run`'s allowlist.

The Claude provider frees `ttp lock *` from the Bash sandbox by name (providers/claude.py FENCE_EXCLUDED)
so that a wrapped ssh or broker command reaches its host. Only the lock's take and release need that; a
privileged command a worker runs goes through `ttp host-run` (config runner.host_commands). So, for a
worker (project.in_run):
- `ttp lock` refuses a wrapped command that runs sudo, su, doas, pkexec or run0 on this host, directly,
  after wrappers (env, timeout, nohup ...) or inside a shell's `-c` string or `eval` (privileged), with
  a pointer to `ttp host-run`. A remote command (`ssh host sudo ...`) is the remote host's business.
- the command it runs gets the kernel's no-new-privileges flag (no_new_privs), as in the sandbox: no
  setuid program it starts, by whatever route (a script, an interpreter), gains privileges. ssh, scp,
  rsync and broker clients need none."""
from __future__ import annotations

import os
import re
import shlex

PRIVILEGED = {"sudo", "su", "doas", "pkexec", "run0"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "ash", "busybox"}
# Wrappers that run the rest of their words as a command; the options that take a value per wrapper.
WRAPPERS = {"env", "nohup", "setsid", "nice", "exec", "command", "time", "stdbuf", "ionice", "timeout",
            "xargs", "chrt", "taskset", "unbuffer", "flock", "!", "{", "(", "if", "then", "else", "elif",
            "do", "while", "until"}
VALUE_OPTS = {"-u", "--unset", "-C", "--chdir", "-s", "--signal", "-k", "--kill-after", "-n", "--adjustment",
              "-c", "--class", "-p", "--pid", "-a", "--arg-file", "-I", "-E", "-d", "--delimiter", "-L",
              "-P", "--max-procs", "-w", "--wait", "-i", "-o", "-e"}
PR_SET_NO_NEW_PRIVS = 38
HOST_RUN_HINT = ("a privileged command a worker runs goes through `ttp host-run -- <command...>` (config "
                 "runner.host_commands), never `ttp lock`: the lock only takes and releases the resource. "
                 "Wrap host-run in the lock if it needs the resource: `ttp lock <res> -- ttp host-run -- <command...>`; "
                 "not on the list: name the exact command in your hand-off")


def _shell_string(s: str, depth: int) -> str | None:
    """The privileged program a shell's `-c` (or eval) string runs as a command on this host, else None."""
    # Newlines and backticks end a command as `;` does; quoted text stays one word (a remote command).
    s = s.replace("\n", ";").replace("`", ";")
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:   # unbalanced quotes: the shell refuses it too; judge it by its plain words
        words = [os.path.basename(w.strip("'\"")) for w in s.split()]
        return next((w for w in words if w in PRIVILEGED), None)
    seg: list[str] = []
    for w in words + [";"]:
        if w and set(w) <= set(";&|()<>") or w in ("$", "$("):
            if seg and (hit := privileged(seg, depth + 1)):
                return hit
            seg = []
        else:
            seg.append(w[:-1] if w.endswith("$") and len(w) > 1 else w)
    return None


def privileged(argv: list[str], depth: int = 0) -> str | None:
    """The privileged program (sudo, su, doas, pkexec, run0) `argv` runs on this host, after its
    variable assignments and wrappers or inside a shell's `-c` string or eval; None when none does."""
    if depth > 8:
        return None
    i = 0
    while i < len(argv):
        w, name = argv[i], os.path.basename(argv[i])
        if name in PRIVILEGED:
            return name
        if i and argv[i - 1] in VALUE_OPTS or re.match(r"^\w+=", w) or re.fullmatch(r"[\d.]+[smhd]?", w) \
                or w.startswith("-") or name in WRAPPERS:
            i += 1
            continue
        if name == "eval":
            return _shell_string(" ".join(argv[i + 1:]), depth)
        if name in SHELLS:
            rest = argv[i + 1:]
            if name == "busybox" and rest and os.path.basename(rest[0]) in SHELLS:
                rest = rest[1:]
            for j, o in enumerate(rest):
                if not o.startswith("-"):
                    break
                if o == "-c" or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", o):
                    return _shell_string(rest[j + 1], depth) if j + 1 < len(rest) else None
        return None
    return None


def refusal(argv: list[str]) -> str | None:
    """Why a worker's `ttp lock` must not run `argv` (a privileged program), else None."""
    hit = privileged(argv)
    return hit and f"ttp lock does not run {hit} for a worker: {HOST_RUN_HINT}"


def lock_command(argv: list[str]) -> list[str] | None:
    """The command a `ttp lock [options] <resource> [--] <command...>` in `argv` (from its `ttp`) wraps,
    unwrapping nested ones; None when `argv` is no `ttp lock` running a command."""
    out = None
    while len(argv) > 2 and os.path.basename(argv[0]) == "ttp" and argv[1] == "lock":
        i = 2
        while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
            i += 2 if argv[i] in ("--timeout", "--task") else 1
        rest = argv[i + 1:]
        while rest[:1] == ["--disruptive"]:   # also given after the resource (cli.cmd_lock)
            rest = rest[1:]
        argv = out = rest[1:] if rest[:1] == ["--"] else rest
    return out or None


def no_new_privs() -> bool:
    """Sets no_new_privs on this process (inherited by every child, never undone); False where the OS
    has no such flag or it could not be set."""
    if not os.uname().sysname == "Linux":
        return False
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False

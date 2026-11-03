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
- it refuses, the same way, a client of a root-owned service reached over a unix socket when the call can
  change state (root_client): docker, podman against the root socket (`--remote`, `--url`, `--connection`
  or run as root), and system-scope systemctl, service, busctl, loginctl and machinectl. User-scoped and
  read-only use stays allowed (`systemctl --user`, rootless podman, `systemctl status`, `docker ps` ...).
  Names only: an arbitrary program can still open the socket; the sandbox is that fence, and this keeps
  a worker from using `ttp lock` this way by accident.
- the command it runs gets the kernel's no-new-privileges flag (no_new_privs), as in the sandbox: no
  setuid program it starts, by whatever route (a script, an interpreter), gains privileges. ssh, scp,
  rsync and broker clients need none."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

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


# Clients of root-owned services reached over a unix socket, and the read-only and user-scoped uses of them
# a worker's `ttp lock` still runs (root_client).
ROOT_CLIENTS = {"docker", "podman", "systemctl", "service", "busctl", "loginctl", "machinectl"}
CONTAINER_READ = {"ps", "images", "inspect", "logs", "version", "info", "help", "top", "port", "history"}
CONTAINER_GROUPS = {"container", "image", "network", "volume", "system"}   # `docker container ls` ...
CONTAINER_GROUP_READ = {"ls", "list", "inspect", "logs", "ps", "top", "port", "history", "df", "info"}
SYSTEMD_READ = {"status", "show", "cat", "help", "get-default", "show-environment", "list", "tree", "introspect",
                "get-property", "monitor", "capture", "wait"}
# Global options that take a value as the next word, per client (`--opt=value` is one word anyway).
CLIENT_VALUE_OPTS = {
    "docker": {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert", "--tlscert",
               "--tlskey"},
    "podman": {"-c", "--connection", "--url", "--identity", "--root", "--runroot", "--storage-driver",
               "--storage-opt", "--cgroup-manager", "--log-level", "--module", "--network-cmd-path",
               "--network-config-dir", "--tmpdir", "--events-backend", "--hooks-dir", "--runtime", "--runtime-flag",
               "--ssh", "--cni-config-dir", "--conmon", "--imagestore", "--volumepath", "--db-backend", "--out"},
    "systemd": {"-t", "--type", "-p", "--property", "-P", "-H", "--host", "-M", "--machine", "-n", "--lines", "-o",
                "--output", "-s", "--signal", "--state", "--job-mode", "--kill-whom", "--kill-value", "--what",
                "--timestamp", "--message", "--image", "--image-policy", "--drop-in", "--when", "--root",
                "--preset-mode", "--boot-loader-entry", "--reboot-argument", "--check-inhibitors", "--address",
                "--match", "--size", "--timeout", "--destination", "--uid", "-E", "--setenv", "--bind", "--bind-ro",
                "--format", "--verify", "--max-addresses", "--number"}}


def _verbs(argv: list[str], value_opts: set[str]) -> list[str]:
    """`argv`'s words after its program that are no option or option value, up to `--`."""
    out, i = [], 1
    while i < len(argv) and argv[i] != "--":
        w = argv[i]
        if w.startswith("-") and len(w) > 1:
            i += 2 if w in value_opts else 1
            continue
        out.append(w)
        i += 1
    return out


def _root_service(argv: list[str]) -> str | None:
    """`<client> <verb>` when `argv` runs a root service's client (ROOT_CLIENTS) that can change its state,
    else None (read-only or user-scoped use, or no such client)."""
    name = os.path.basename(argv[0]) if argv else ""
    if name not in ROOT_CLIENTS:
        return None
    opts = argv[1:argv.index("--")] if "--" in argv else argv[1:]
    if name in ("docker", "podman"):
        if name == "podman" and os.geteuid() != 0 and not any(
                o in ("-r", "--remote", "--url", "-c", "--connection") or o.startswith(("--remote=", "--url=", "--connection="))
                for o in opts):
            return None   # rootless podman: the user's own containers
        verbs = _verbs(argv, CLIENT_VALUE_OPTS[name])
        if not verbs or verbs[0] in CONTAINER_READ or (
                verbs[0] in CONTAINER_GROUPS and (len(verbs) < 2 or verbs[1] in CONTAINER_GROUP_READ)):
            return None
        return f"{name} {' '.join(verbs[:2] if verbs[0] in CONTAINER_GROUPS else verbs[:1])}"
    if name == "service":
        words = argv[1:]
        if not words or words[0] in ("--status-all", "--help", "-h", "--version") or words[1:2] == ["status"]:
            return None
        return f"service {' '.join(words[:2])}"
    # systemctl, busctl: the last of --user, --system (and systemctl's --global) wins; loginctl and
    # machinectl speak to system services only.
    scope = [o for o in opts if o in ("--user", "--system", "--global")]
    if name in ("systemctl", "busctl") and scope[-1:] == ["--user"]:
        return None
    verbs = _verbs(argv, CLIENT_VALUE_OPTS["systemd"])
    verb = verbs[0] if verbs else ""
    if not verb or verb in SYSTEMD_READ or verb.startswith(("list-", "is-", "show-")) or verb.endswith("-status"):
        return None
    return f"{name} {verb}"


def _setuid(argv: list[str]) -> str | None:
    name = os.path.basename(argv[0]) if argv else ""
    return name if name in PRIVILEGED else None


def _shell_string(s: str, depth: int, judge=_setuid) -> str | None:
    """What `judge` finds in a command a shell's `-c` (or eval) string runs on this host, else None."""
    # Newlines and backticks end a command as `;` does; quoted text stays one word (a remote command).
    s = s.replace("\n", ";").replace("`", ";")
    try:
        lex = shlex.shlex(s, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:   # unbalanced quotes: the shell refuses it too; judge it by its plain words
        words = [w.strip("'\"") for w in s.split()]
        return next((hit for k in range(len(words)) if (hit := judge(words[k:]))), None)
    seg: list[str] = []
    for w in words + [";"]:
        if w and set(w) <= set(";&|()<>") or w in ("$", "$("):
            if seg and (hit := _walk(seg, depth + 1, judge)):
                return hit
            seg = []
        else:
            seg.append(w[:-1] if w.endswith("$") and len(w) > 1 else w)
    return None


def privileged(argv: list[str], depth: int = 0) -> str | None:
    """The privileged program (sudo, su, doas, pkexec, run0) `argv` runs on this host, after its
    variable assignments and wrappers or inside a shell's `-c` string or eval; None when none does."""
    return _walk(argv, depth, _setuid)


def root_client(argv: list[str]) -> str | None:
    """`<client> <verb>` when `argv` runs a root service's client that can change its state (_root_service),
    looking through wrappers and shell strings as privileged() does; None when it runs none."""
    return _walk(argv, 0, _root_service)


def _walk(argv: list[str], depth: int, judge) -> str | None:
    """What `judge` finds at a command position of `argv`: after its variable assignments and wrappers or
    inside a shell's `-c` string or eval."""
    if depth > 8:
        return None
    i = 0
    while i < len(argv):
        w, name = argv[i], os.path.basename(argv[i])
        if hit := judge(argv[i:]):
            return hit
        if (split := w[2:] if w.startswith("-S") else w[15:] if w.startswith("--split-string=") else None) is not None:
            split = split or (argv[i + 1] if w == "-S" and i + 1 < len(argv) else "")
            if (hit := _shell_string(split, depth, judge)) or w == "-S":   # env -S 'sudo x'
                return hit or _walk(argv[i + 2:], depth + 1, judge)
        if i and argv[i - 1] in VALUE_OPTS or re.match(r"^\w+=", w) or re.fullmatch(r"[\d.]+[smhd]?", w) \
                or w.startswith("-") or name in WRAPPERS:
            i += 1
            continue
        if name == "eval":
            return _shell_string(" ".join(argv[i + 1:]), depth, judge)
        if name in SHELLS:
            rest = argv[i + 1:]
            if name == "busybox" and rest and os.path.basename(rest[0]) in SHELLS:
                rest = rest[1:]
            for j, o in enumerate(rest):
                if not o.startswith("-"):
                    break
                if o == "-c" or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", o):
                    return _shell_string(rest[j + 1], depth, judge) if j + 1 < len(rest) else None
        return None
    return None


def refusal(argv: list[str]) -> str | None:
    """Why a worker's `ttp lock` must not run `argv` (a privileged program, or a root service's client
    that can change its state), else None."""
    if hit := privileged(argv):
        return f"ttp lock does not run {hit} for a worker: {HOST_RUN_HINT}"
    hit = root_client(argv)
    return hit and (f"ttp lock does not run `{hit}` for a worker (a root-owned service's client): "
                    f"{HOST_RUN_HINT}")


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


def _prctl():
    """libc's prctl, or None off Linux or where it cannot be found."""
    if not os.uname().sysname == "Linux":
        return None
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None


def no_new_privs() -> bool:
    """Sets no_new_privs on this process (inherited by every child, never undone); False where the OS
    has no such flag or it could not be set."""
    prctl = _prctl()
    return prctl is not None and prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0


def no_new_privs_preexec():
    """A Popen preexec_fn that sets no_new_privs in the child before it execs (as no_new_privs), so
    nothing it starts can gain privileges; None off Linux. prctl is looked up here, in the parent, so
    the forked child only calls it: no import, dlopen or lock after the fork, which keeps it safe in
    the daemon, whose threads rule out a general preexec_fn (project.renice)."""
    prctl = _prctl()
    if prctl is None:
        return None

    def apply() -> None:
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    return apply


def fenced(pid: int | str = "self") -> bool | None:
    """Whether process `pid` runs with no_new_privs set (NoNewPrivs in /proc/<pid>/status); None
    where that cannot be read (off Linux, a pid gone or out of sight)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NoNewPrivs:"):
                return line.split(":", 1)[1].strip() == "1"
    except (OSError, ValueError):
        return None
    return None

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp hostcheck <alias> <check> [args]`: a fixed catalogue of read-only checks a coordinator in
session mode (coordtools.py) runs to see for itself what is true on a host.

Each check builds a fixed argv from validated arguments: the model never supplies a shell string, an
option or a program. A log tail reads only a file the project's config names
(coordinator.session.log_paths: name -> path), its last 50 lines. Every check has a timeout (20 s in
all, the connection included) and its output is capped at 4,000 characters. tt-smi runs with the
host's own environment: device masking stays as the host sets it.

Aliases: `local`, the daemon's own host, or a `ttp machines` alias; any other name is refused. The
own host (`local`, a name of this machine, or a name resolving to an address of this machine on ssh's
port 22, never a loopback tunnel end on another port) runs here with no ssh. Any other alias is reached only through the project's own access, coordinator.session.
host_access.<alias> (or `*`): a list is a helper's argv prefix (its program in the project folder),
to which the check's argv is appended word by word; a string is an ssh config file in the project
folder (`ssh -F <file> -o BatchMode=yes -o ConnectTimeout=5 -- <alias> <quoted argv>`). The default
is empty: an alias with no access is refused, never tried with the user's own ssh setup.

Exit codes: the check's own (0 true/ok), 2 refused (unknown check or alias, bad argument, no access),
124 a local check timed out, 255 a remote host not reached in time (ssh's own code for that)."""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

LOCAL = "local"
TIMEOUT_S = 20      # in all, a remote check's connection included
CONNECT_TIMEOUT_S = 5
MAX_OUT = 4000
LOG_LINES = 50
REFUSED, TIMED_OUT, UNREACHABLE = 2, 124, 255

_PATH = re.compile(r"[A-Za-z0-9._/~+@:-]{1,400}")
_UNIT = re.compile(r"[A-Za-z0-9@._:\\-]{1,200}")
_NAME = re.compile(r"[A-Za-z0-9._+:-]{1,200}")
_REV = re.compile(r"[A-Za-z0-9._/^~@{}-]{1,200}")


class Refused(ValueError):
    pass


def _word(v: str, pat: re.Pattern, what: str) -> str:
    """`v` if it is one plain word of `pat` that no program could take as an option."""
    if not isinstance(v, str) or v.startswith("-") or not pat.fullmatch(v):
        raise Refused(f"bad {what} {v!r}: one plain word, not starting with '-'")
    return v


def _count(v: str, top: int) -> str:
    if not str(v).isdigit() or not 1 <= int(v) <= top:
        raise Refused(f"bad count {v!r}: a whole number 1-{top}")
    return str(int(v))


def _args(args: list[str], least: int, most: int, usage: str) -> list[str]:
    if not least <= len(args) <= most:
        raise Refused(f"usage: {usage}")
    return args


def _unit(a):
    a = _args(a, 1, 2, "unit-active <unit> [user]")
    if len(a) == 2 and a[1] != "user":
        raise Refused("usage: unit-active <unit> [user]")
    return ["systemctl", *(["--user"] if len(a) == 2 else []), "is-active", _word(a[0], _UNIT, "unit")]


def _process(a):
    who = _args(a, 1, 1, "process-alive <pid | process name>")[0]
    if who.isdigit():
        return ["ps", "-o", "pid=,etime=,args=", "-p", who]
    return ["ps", "-o", "pid=,etime=,args=", "-C", _word(who, _NAME, "process name")]   # exact name


def _git_log(a):
    a = _args(a, 1, 2, "git-log <repo> [count]")
    return ["git", "-C", _word(a[0], _PATH, "repo"), "--no-pager", "log", "--oneline", "--decorate",
            "-n", _count(a[1], 200) if len(a) > 1 else "20"]


def _git_show(a):
    a = _args(a, 2, 2, "git-show <repo> <commit>")
    return ["git", "-C", _word(a[0], _PATH, "repo"), "--no-pager", "show", "--no-ext-diff", "--no-textconv",
            "--stat", "--patch", _word(a[1], _REV, "commit"), "--"]


def _broker(a):
    a = _args(a, 1, 2, "broker-job <runner> [job id]")
    return ["ttp", "devq", "status", *(_word(x, _NAME, "runner or job id") for x in a)]


def _none(usage: str, argv: list[str]):
    return lambda a: _args(a, 0, 0, usage) and [] or list(argv)


def log_paths(cfg: dict | None) -> dict[str, str]:
    """coordinator.session.log_paths: {name: absolute path} the project allows a log tail of."""
    got = (((cfg or {}).get("coordinator") or {}).get("session") or {}).get("log_paths") or {}
    if not isinstance(got, dict):
        return {}
    return {str(k): v for k, v in got.items() if isinstance(v, str) and os.path.isabs(v)}


def _log_tail(a, cfg=None):
    name = _word(_args(a, 1, 1, "log-tail <name>")[0], _NAME, "log name")
    allowed = log_paths(cfg)
    if name not in allowed:
        raise Refused(f"no log {name!r} in coordinator.session.log_paths"
                      + (f" (it has: {', '.join(sorted(allowed))})" if allowed else ""))
    return ["tail", "-n", str(LOG_LINES), "--", allowed[name]]


# name: (usage, argv builder). Every program here only reads; git only reads (log, show).
CATALOGUE = {
    "reachable": ("reachable", _none("reachable", ["echo", "reachable"])),
    "uptime": ("uptime", _none("uptime", ["uptime"])),
    "unit-active": ("unit-active <unit> [user]", _unit),
    "process-alive": ("process-alive <pid | process name>", _process),
    "file-exists": ("file-exists <path>", lambda a: ["stat", "-c", "%n: %F, %s bytes, modified %y", "--",
                                                     _word(_args(a, 1, 1, "file-exists <path>")[0], _PATH, "path")]),
    "disk-free": ("disk-free [path]", lambda a: ["df", "-hP", "--", _word((_args(a, 0, 1, "disk-free [path]")
                                                                           or ["/"])[0], _PATH, "path")]),
    "git-head": ("git-head <repo>", lambda a: ["git", "-C", _word(_args(a, 1, 1, "git-head <repo>")[0], _PATH, "repo"),
                                               "--no-pager", "log", "-1", "--format=%H %cI (%D) %s"]),
    "git-log": ("git-log <repo> [count]", _git_log),
    "git-show": ("git-show <repo> <commit>", _git_show),
    "lock-holder": ("lock-holder <resource>", lambda a: ["ttp", "lock", "--probe",
                                                         _word(_args(a, 1, 1, "lock-holder <resource>")[0], _NAME, "resource")]),
    "broker-job": ("broker-job <runner> [job id]", _broker),
    "log-tail": ("log-tail <name>", _log_tail),
    "tt-smi": ("tt-smi", _none("tt-smi", ["tt-smi", "-s"])),   # snapshot to stdout; reads telemetry only
    "squeue": ("squeue", _none("squeue", ["squeue", "--me", "-h", "-o", "%i %P %j %T %M %l %D %R"])),
}
READ_ONLY_PROGRAMS = {"echo", "uptime", "systemctl", "ps", "stat", "df", "git", "ttp", "tail", "tt-smi",
                      "squeue"}
NEEDS_CFG = {"log-tail"}


def build(check: str, args: list[str], cfg: dict | None = None) -> list[str]:
    """The fixed argv of `check` with `args`, or Refused."""
    if check not in CATALOGUE:
        raise Refused(f"unknown check {check!r}; the catalogue: {', '.join(CATALOGUE)}")
    fn = CATALOGUE[check][1]
    return fn(list(args), cfg) if check in NEEDS_CFG else fn(list(args))


def _local_address(name: str) -> bool:
    """Whether `name` resolves to an address of this machine (bindable here; nothing is sent)."""
    try:
        infos = socket.getaddrinfo(name, None)
    except (OSError, UnicodeError):
        return False
    for fam, _, _, _, addr in infos:
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.bind((addr[0], 0))
                return True
        except OSError:
            continue
    return False


def ssh_port(alias: str) -> int | None:
    """The port `ssh -G` reports for `alias` (no connection); None when it cannot say."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
        return None
    try:
        r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^port\s+(\d+)\s*$", r.stdout, re.M | re.I) if r.returncode == 0 else None
    return int(m.group(1)) if m else None


def own_host(alias: str, known: dict | None = None) -> bool:
    """Whether `alias` is the daemon's own host: `local`, a name of this machine, a machines-list entry
    whose host field is one, or an ssh alias whose HostName is or resolves to an address of this machine
    on ssh's own port (22). A loopback HostName on another port is a tunnel end (LocalForward or reverse
    tunnel) to some other host, so it is remote, as is a loopback name with such a port."""
    from . import machines
    loopback = {"localhost", "127.0.0.1", "::1"}
    mine = machines.local_names() - loopback
    known = machines.load() if known is None else known
    v = alias.lower()
    if alias == LOCAL or v in mine or any(
            a.lower() == v and str(m.get("hostname") or "").strip().lower() in mine for a, m in known.items()):
        return True
    try:
        real = machines.ssh_hostname(alias)
    except Exception:
        real = alias
    r = str(real).strip().lower()
    if not (r in loopback or r in mine or _local_address(real)):
        return False
    return ssh_port(alias) in (None, 22)


def access(cfg: dict, root: Path, alias: str) -> list[str]:
    """The argv prefix that reaches `alias` through the project's own access (see the module doc)."""
    table = ((cfg.get("coordinator") or {}).get("session") or {}).get("host_access") or {}
    got = table.get(alias, table.get("*")) if isinstance(table, dict) else None
    if not got:
        raise Refused(f"no access to {alias!r}: set coordinator.session.host_access.{alias} to a helper "
                      "(an argv list whose program is in the project folder) or an ssh config file there")
    root = Path(root).resolve()

    def inside(v: str) -> str:
        path = (root / os.path.expanduser(v)).resolve()
        if path != root and root not in path.parents:
            raise Refused(f"coordinator.session.host_access.{alias}: {v!r} is not in the project folder")
        return str(path)
    if isinstance(got, str):
        return ["ssh", "-F", inside(got), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "--",
                alias]
    if isinstance(got, list) and got and all(isinstance(x, str) for x in got):
        return [inside(got[0]), *got[1:]]
    raise Refused(f"coordinator.session.host_access.{alias}: a list (helper argv) or a string (ssh config file)")


def command(cfg: dict, root: Path, alias: str, check: str, args: list[str],
            known: dict | None = None) -> tuple[list[str], bool]:
    """(argv to run, whether it runs here). Refused for an unknown check, a bad argument or no access."""
    _word(alias, _NAME, "alias")
    argv = build(check, args, cfg)
    if own_host(alias, known):
        return argv, True
    from . import machines
    if alias not in (machines.load() if known is None else known):
        raise Refused(f"unknown alias {alias!r}: a coordinator checks only `local` and `ttp machines` aliases")
    prefix = access(cfg, root, alias)
    if prefix[0] == "ssh":   # ssh hands the remote shell one string: every word quoted
        return [*prefix, shlex.join(argv)], False
    return [*prefix, *argv], False


def trim(text: str, n: int = MAX_OUT) -> str:
    if len(text) <= n:
        return text
    half = n // 2
    return f"{text[:half]}\n... [{len(text) - n} characters cut] ...\n{text[-half:]}"


def run(cfg: dict, root: Path, alias: str, check: str, args: list[str]) -> tuple[int, str]:
    """(exit code, trimmed output) of one check."""
    try:
        argv, here = command(cfg, root, alias, check, args)
    except Refused as e:
        return REFUSED, f"hostcheck: {e}"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                           cwd=str(root))
    except subprocess.TimeoutExpired:
        if here:
            return TIMED_OUT, f"hostcheck: {check} on {alias} timed out after {TIMEOUT_S} s"
        return UNREACHABLE, f"hostcheck: {alias} did not answer {check} within {TIMEOUT_S} s"
    except OSError as e:
        return REFUSED, f"hostcheck: {check} on {alias} could not start: {e}"
    out = (r.stdout or "") + (("\n" if r.stdout else "") + r.stderr if r.stderr else "")
    return r.returncode, trim(out.strip() or f"(no output, exit {r.returncode})")

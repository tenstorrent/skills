# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Find service unit changes in a script before a worker runs it.

A driver or test stub that calls `systemctl stop|disable|mask|...` (often as `sudo -n systemctl`)
changes a shared machine's services for everyone on it: a serving process stops, a recovery timer
is switched off, a unit stays masked after the run. `ttp svcscan <script>` flags those calls;
`ttp svcscan --shim <dir>` writes stand-ins for `systemctl`, `service` and `sudo` that only log
changing calls, so the driver can be exercised without touching a real unit. Only read-only queries
(`systemctl is-active`, `status`, `show`, ... in READ_ONLY) go on to the real `systemctl` when there
is one; every other verb counts as a change.
A call by absolute path (`/usr/bin/systemctl`, `/usr/bin/sudo`) or over ssh bypasses a local shim,
so it is flagged as such.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# The only systemctl verbs that leave the machine as it was. Any other verb (stop, reboot, reload,
# daemon-reload, set-property, an unknown or `$verb` word...) is flagged by scan() and only logged
# by the shim. A call with no verb (plain `systemctl`, `--version`, `-h`) lists units or prints help.
READ_ONLY = ("status", "show", "cat", "is-active", "is-enabled", "is-failed", "is-system-running",
             "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies",
             "list-jobs", "show-environment", "help")
# systemctl options that take the next word as their value (`-H <host> stop`, `-p ActiveState`)
SHORT_WITH_VALUE = "HMPpstno"
LONG_WITH_VALUE = ("--host", "--machine", "--property", "--type", "--signal", "--state", "--lines",
                   "--output", "--root", "--image", "--kill-whom", "--kill-value", "--job-mode",
                   "--what", "--boot-loader-entry", "--reboot-argument", "--timestamp",
                   "--preset-mode", "--check-inhibitors", "--message", "--when", "--drop-in")
# service <unit> <action> that changes the unit
SERVICE_ACTIONS = ("stop", "start", "restart", "reload", "force-reload", "try-restart")
# a command word, also by path (/usr/bin/, ./, ../bin/) and inside quotes or an argv list
_CMD = r"(?<![\w./-])(?P<path>[^\s'\",;|&()\[\]]*/)?"
_END = r"(?![\w.-])"
_SYSTEMCTL = re.compile(_CMD + r"systemctl" + _END)
_SERVICE = re.compile(_CMD + r"service" + _END)
# the rest of one command: up to ; | & ) ] or a backtick
_REST = re.compile(r"[^;|&)\]`]*")
# words are split by blanks, quotes and commas, so `["sudo", "-n", "systemctl", "stop", u]` reads
# as `sudo -n systemctl stop u`
_SEP = re.compile(r"[\s'\",]+")
_ABSOLUTE = re.compile(r"/\S*/(?:systemctl|service|sudo)\b")
_SSH = re.compile(r"(?<![\w./-])(?:/\S*/)?ssh" + _END)
SHIMMED = ("systemctl", "service", "sudo")


def systemctl_verb(words) -> str:
    """The verb of a systemctl call given the words after `systemctl`: the first word that is not
    an option or an option's value ('' when there is none). Same parse as the shim's."""
    skip = False
    after_dashes = False
    for w in words:
        if skip:
            skip = False
            continue
        if not after_dashes and w == "--":
            after_dashes = True
            continue
        if not after_dashes and w.startswith("--"):
            skip = w in LONG_WITH_VALUE
            continue
        if not after_dashes and w.startswith("-") and len(w) > 1:
            # a short cluster (`-qH`) takes the next word only when its first value letter is last
            at = next((i for i, c in enumerate(w[1:], 1) if c in SHORT_WITH_VALUE), None)
            skip = at == len(w) - 1
            continue
        return w
    return ""


def _logical_lines(text: str):
    """(first line number, line) with backslash-continued lines joined."""
    lines = text.splitlines()
    n = 0
    while n < len(lines):
        first, line = n + 1, lines[n]
        while not line.lstrip().startswith("#") and line.endswith("\\") and n + 1 < len(lines):
            n += 1
            line = line[:-1].rstrip() + " " + lines[n].lstrip()
        n += 1
        yield first, line


def _change(line: str):
    """(reason, match) for the first unit change on `line`, else None."""
    for m in _SYSTEMCTL.finditer(line):
        verb = systemctl_verb(w for w in _SEP.split(_REST.match(line, m.end()).group()) if w)
        if verb and verb not in READ_ONLY:
            return "systemctl changes a unit on the machine", m
    for m in _SERVICE.finditer(line):
        words = [w for w in _SEP.split(_REST.match(line, m.end()).group()) if w]
        if len(words) >= 2 and not words[0].startswith("-") and words[1] in SERVICE_ACTIONS:
            return "service changes a unit on the machine", m
    return None


def scan(text: str) -> list:
    """[(line number, reason, line)] for each line that changes a service unit: a systemctl verb
    outside READ_ONLY, or `service <unit> stop|reload|...`. Comment lines are skipped; one reason
    per line; a backslash-continued call counts at its first line. A call by path or over ssh is
    marked, since a local PATH shim does not stop it."""
    found = []
    for n, raw in _logical_lines(text):
        line = raw.lstrip()
        if line.startswith("#"):
            continue
        hit = _change(line)
        if not hit:
            continue
        reason, m = hit
        start = max(line.rfind(c, 0, m.start()) for c in ";|&\n") + 1
        prefix = line[start:m.end()]
        if _ABSOLUTE.search(prefix):
            reason += " (by absolute path: a PATH shim does not stop it)"
        elif m.group("path"):
            reason += " (by relative path: a PATH shim does not stop it)"
        elif _SSH.search(line[:m.start()]):
            reason += " (over ssh: a local PATH shim does not stop it)"
        found.append((n, reason, line.strip()))
    return found


def _systemctl_shim(log: str) -> str:
    # Same parse as systemctl_verb(): only a call with no verb or a READ_ONLY verb reaches a real
    # systemctl; every other call is only logged. The search for the real one skips every ttp
    # svcscan shim (this one under any PATH spelling, or another --shim folder), so it cannot loop.
    long_opts = "|".join(LONG_WITH_VALUE)
    read_only = "|".join(READ_ONLY)
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs every call; changes no unit, read-only queries go to the real one\n"
            f"printf '%s\\n' \"systemctl $*\" >> {log}\n"
            "verb= skip= dashes=\n"
            "for a in \"$@\"; do\n"
            "  if [ -n \"$skip\" ]; then skip=; continue; fi\n"
            "  if [ -z \"$dashes\" ]; then\n"
            "    case \"$a\" in\n"
            "      --) dashes=1; continue ;;\n"
            f"      {long_opts}) skip=1; continue ;;\n"
            "      --*) continue ;;\n"
            f"      -?*) pre=${{a%%[{SHORT_WITH_VALUE}]*}}\n"
            "           if [ \"$pre\" != \"$a\" ] && [ -z \"${a#\"$pre\"?}\" ]; then skip=1; fi\n"
            "           continue ;;\n"
            "    esac\n"
            "  fi\n"
            "  verb=$a; break\n"
            "done\n"
            "case \"$verb\" in\n"
            f"  ''|{read_only}) ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n"
            "IFS=:\n"
            "for d in $PATH; do\n"
            "  [ -x \"$d/systemctl\" ] || continue\n"
            "  [ \"$d/systemctl\" -ef \"$0\" ] && continue\n"
            "  grep -q '^# ttp svcscan shim' \"$d/systemctl\" 2>/dev/null && continue\n"
            "  exec \"$d/systemctl\" \"$@\"\n"
            "done\n"
            "exit 0\n")


def _service_shim(log: str) -> str:
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs the call, changes no unit\n"
            f"printf '%s\\n' \"service $*\" >> {log}\n"
            "exit 0\n")


def _sudo_shim(log: str) -> str:
    # Skip sudo's options (those that take a value too) and VAR=value words; run the command only
    # when it is shimmed here (by basename), so `sudo -n /usr/bin/systemctl stop x` is only logged.
    return ("#!/bin/sh\n"
            "# ttp svcscan shim: logs the call; runs systemctl/service through their shims, nothing else\n"
            f"printf '%s\\n' \"sudo $*\" >> {log}\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --) shift; break ;;\n"
            "    -u|-g|-p|-C|-D|-r|-t|-U|-T|--user|--group|--prompt|--close-from|--chdir|--host|--role|"
            "--type|--other-user|--command-timeout) shift; [ $# -gt 0 ] && shift ;;\n"
            "    -*) shift ;;\n"
            "    *=*) shift ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            "[ $# -gt 0 ] || exit 0\n"
            "self=$(cd \"$(dirname \"$0\")\" && pwd)\n"
            "name=$(basename \"$1\")\n"
            "case \"$name\" in\n"
            "  systemctl|service) shift; exec \"$self/$name\" \"$@\" ;;\n"
            "esac\n"
            "exit 0\n")


def write_shims(folder: Path) -> list:
    """Write logging stand-ins for systemctl, service and sudo into `folder`. Calls are appended to
    `folder`/svcscan.log; a changing call exits 0 and changes nothing."""
    from .project import durable_write
    folder.mkdir(parents=True, exist_ok=True)
    log = shlex.quote(str(folder / "svcscan.log"))
    paths = []
    for name, body in (("systemctl", _systemctl_shim(log)), ("service", _service_shim(log)),
                       ("sudo", _sudo_shim(log))):
        path = folder / name
        durable_write(path, body, mode=0o755, sync=False)   # never rewritten under a running shim
        paths.append(path)
    return paths

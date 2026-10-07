# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Find pattern kills in a script before a worker runs it.

`pkill -f <name>` matches every process of the user whose command line contains <name>, and that
includes the worker's own tool shell when its command mentions the name. A script or test stub that
kills by pattern can so end the worker that runs it, or other workers and projects. A separate
session or process group does not help: the match is by command line, not by group. The safe way to
run such a script is with those commands shimmed out (`ttp killscan --shim <dir>`); a call by
absolute path (`/usr/bin/pkill`) bypasses a shim, so it is flagged as such and must be edited out,
and so must `ps | grep` feeding kill. `pkill -P <pid>` (only that process's children) is not flagged;
note that a shim stands in for it too, which leaves those children running.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# the commands that kill by name or pattern; a word of their own, not part of a path or word
_CMD = r"(?<![\w./-])(?:/\S*/)?"
_END = r"(?![\w.-])"
_ARGS = r"(?=[^\n;|&]*\s"
_RULES = [
    ("pkill -f matches every command line containing the pattern",
     re.compile(_CMD + r"pkill" + _END + _ARGS + r"(?:-\w*f\w*|--full)\b)")),
    ("pkill kills every process with that name", re.compile(_CMD + r"pkill" + _END)),
    ("killall kills every process with that name", re.compile(_CMD + r"killall" + _END)),
    # however its pids reach kill (a variable, a loop, xargs, a later line): all are shimmed
    ("pgrep/pidof picks processes by name or pattern; a kill of its pids is a pattern kill",
     re.compile(_CMD + r"(?:pgrep|pidof)" + _END)),
]
# `pkill -P <pid>` kills only that process's children, not by name
_PARENT = re.compile(_CMD + r"pkill" + _END + _ARGS + r"(?:-P|--parent)\b)")
# ps output filtered by pattern, in a script that kills: no PATH shim stops this one
_PS = ("ps | grep picks processes by pattern and a PATH shim does not stop a kill of its pids: "
       "edit it out", re.compile(r"(?<![\w./-])(?:/\S*/)?ps" + _END + r"[^\n;&]*\|\s*(?:\S*/)?(?:e?grep|awk)\b"))
_KILL = re.compile(r"(?<![\w.-])kill" + _END)
_ABSOLUTE = re.compile(r"/\S*/(?:pkill|killall|pgrep|pidof)\b")
SHIMMED = ("pkill", "killall", "pgrep", "pidof")


def scan(text: str) -> list:
    """[(line number, reason, line)] for each line that kills by name or pattern. Comment lines
    are skipped; one reason per line, the most specific. Every pgrep/pidof call is flagged, since
    its pids can reach kill on any later line; `ps | grep` is flagged when the script kills at all."""
    code = [(n, line.lstrip()) for n, line in enumerate(text.splitlines(), 1)]
    code = [(n, c) for n, c in code if not c.startswith("#")]
    kills = any(_KILL.search(c) for _, c in code)
    found = []
    for n, line in code:
        for reason, rule in _RULES + ([_PS] if kills else []):
            m = next((m for m in rule.finditer(line) if not _PARENT.match(line, m.start())), None)
            if m:
                if _ABSOLUTE.search(m.group(0)):
                    reason += " (by absolute path: a PATH shim does not stop it)"
                found.append((n, reason, line.strip()))
                break
    return found


def write_shims(folder: Path) -> list:
    """Write logging stand-ins for the pattern-kill commands into `folder`. Each one appends its
    arguments to `folder`/killscan.log, kills nothing and exits 1 ("no process matched")."""
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "killscan.log"
    paths = []
    for name in SHIMMED:
        path = folder / name
        path.write_text("#!/bin/sh\n"
                        "# ttp killscan shim: logs the call, kills nothing\n"
                        f"printf '%s\\n' \"{name} $*\" >> {shlex.quote(str(log))}\n"
                        "exit 1\n")
        path.chmod(0o755)
        paths.append(path)
    return paths

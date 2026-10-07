# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Find pattern kills in a script before a worker runs it.

`pkill -f <name>` matches every process of the user whose command line contains <name>, and that
includes the worker's own tool shell when its command mentions the name. A script or test stub that
kills by pattern can so end the worker that runs it, or other workers and projects. A separate
session or process group does not help: the match is by command line, not by group. The safe way to
run such a script is with those commands shimmed out (`ttp killscan --shim <dir>`); a call by
absolute path (`/usr/bin/pkill`) bypasses a shim, so it is flagged as such and must be edited out.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

# the commands that kill by name or pattern; a word of their own, not part of a path or word
_CMD = r"(?<![\w./-])(?:/\S*/)?"
_END = r"(?![\w.-])"
_RULES = [
    ("pkill -f matches every command line containing the pattern",
     re.compile(_CMD + r"pkill" + _END + r"(?=[^\n;|&]*\s(?:-\w*f\w*|--full)\b)")),
    ("pkill kills every process with that name", re.compile(_CMD + r"pkill" + _END)),
    ("killall kills every process with that name", re.compile(_CMD + r"killall" + _END)),
    ("kill fed by pgrep/pidof kills by name or pattern",
     re.compile(r"(?<![\w.-])kill\b[^\n;|&]*(?:\$\(|`)\s*(?:/\S*/)?(?:pgrep|pidof)\b"
                r"|(?<![\w.-])(?:pgrep|pidof)\b[^\n;&]*\|\s*xargs\b[^\n;|&]*\bkill\b")),
]
_ABSOLUTE = re.compile(r"/\S*/(?:pkill|killall|pgrep|pidof)\b")
SHIMMED = ("pkill", "killall", "pgrep", "pidof")


def scan(text: str) -> list:
    """[(line number, reason, line)] for each line that kills by name or pattern. Comment lines
    are skipped; one reason per line, the most specific."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.lstrip()
        if code.startswith("#"):
            continue
        for reason, rule in _RULES:
            m = rule.search(code)
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

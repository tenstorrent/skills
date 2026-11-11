# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A process's arguments, environment and (pgid, uid) where there is no /proc (macOS), for the stop
paths (jobstop, killguard) that read them from /proc on Linux.

argv() and environ() read sysctl KERN_PROCARGS2: argc (an int), the exec path, NUL padding, the argv
strings, then the environment strings up to an empty one. The kernel answers only for this user's own
processes (another user's, or a gone pid: [] / None). stat() and table() read `ps`. Off macOS every
function answers None or empty, so a caller keeps its Linux /proc route first.
"""
from __future__ import annotations

import subprocess
import sys

CTL_KERN, KERN_ARGMAX, KERN_PROCARGS2 = 1, 8, 49
DARWIN = sys.platform == "darwin"


def _procargs(pid: int) -> tuple[list[bytes], list[bytes]] | None:
    """(argv, environment strings) of pid from KERN_PROCARGS2, else None."""
    if not DARWIN or not (isinstance(pid, int) and pid > 0):
        return None
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        argmax, n = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
        if libc.sysctl((ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX), 2, ctypes.byref(argmax), ctypes.byref(n), None, 0):
            return None
        size = ctypes.c_size_t(max(argmax.value, 4096))
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl((ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid), 3, buf, ctypes.byref(size), None, 0):
            return None
    except (OSError, AttributeError, ValueError):
        return None
    raw = buf.raw[:size.value]
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    _, _, rest = raw[4:].partition(b"\0")   # the exec path
    words = rest.lstrip(b"\0").split(b"\0")
    if argc < 0 or argc > len(words):
        return None
    env = []
    for w in words[argc:]:
        if not w:
            break
        env.append(w)
    return words[:argc], env


def argv(pid: int) -> list[str]:
    """pid's arguments (macOS), [] when they cannot be read."""
    got = _procargs(pid)
    return [a.decode(errors="replace") for a in got[0]] if got else []


def environ(pid: int) -> dict[str, str] | None:
    """pid's environment at its start (macOS), None when it cannot be read."""
    got = _procargs(pid)
    if got is None:
        return None
    return dict(x.decode(errors="replace").split("=", 1) for x in got[1] if b"=" in x)


def _ps(args: list[str]) -> dict[int, tuple[int, int]]:
    """{pid: (pgid, uid)} of the live processes (no zombie) `ps -o pid=,pgid=,uid=,stat=` lists."""
    if not DARWIN:
        return {}
    try:
        out = subprocess.run(["ps", *args, "-o", "pid=,pgid=,uid=,stat="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    rows = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) == 4 and all(x.isdigit() for x in f[:3]) and not f[3].startswith("Z"):
            rows[int(f[0])] = (int(f[1]), int(f[2]))
    return rows


def stat(pid: int) -> tuple[int, int] | None:
    """(pgid, uid) of a live process (not a zombie) on macOS, else None."""
    if not (isinstance(pid, int) and pid > 0):
        return None
    return _ps(["-p", str(pid)]).get(pid)


def table() -> dict[int, tuple[int, int]]:
    """{pid: (pgid, uid)} of every live process on macOS ({} elsewhere or when ps fails)."""
    return _ps(["-A"])

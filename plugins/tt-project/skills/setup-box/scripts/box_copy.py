"""setup-box: copy one file or folder to or from a box, with rsync when both ends have rsync 3+, else
tar over ssh (some boxes have no rsync; macOS ships rsync 2.6.9 or openrsync, which lack -s).

For job inputs and results only (a config, a log, a results folder); never a checkout or the project
box's tree: the box keeps its own checkout (SKILL.md). Standard library only, Python 3.9+.

  box.py copy <src> <dst-folder>     one side is `<alias>:<path>`; src lands in dst-folder/<its name>
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

import box


class CopyError(Exception):
    pass


def split(spec: str) -> Tuple[Optional[str], str]:
    """`alias:path` -> (alias, path); a local path -> (None, path). As scp: a colon after a slash is
    part of a local path (write `./a:b` for a local file named `a:b`)."""
    head, sep, tail = spec.partition(":")
    if sep and head and "/" not in head:
        return head, tail or "."
    return None, spec


def _remote(path: str) -> str:
    """A remote path for the box's shell: `~/x` stays under the home folder, the rest is quoted."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


_RSYNC_VERSION = re.compile(r"rsync\s+version\s+v?(\d+)\.")


def rsync_major(text: str) -> int:
    """The major version in `rsync --version` output; 0 when unknown. openrsync says
    "rsync version 2.6.9 compatible" on a later line; its protocol number is not a version."""
    m = _RSYNC_VERSION.search(text or "")
    return int(m.group(1)) if m else 0


def have_rsync(alias: str) -> bool:
    """rsync 3+ (it has -s, --protect-args) on this machine and on the box; a failed check is no."""
    if not shutil.which("rsync"):
        return False
    try:
        mine = subprocess.run(["rsync", "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    if rsync_major(mine) < 3:
        return False
    r = box.ssh_run(alias, "command -v rsync >/dev/null && rsync --version", timeout=30)
    return r.returncode == 0 and rsync_major(r.stdout) >= 3


def copy(src: str, dst: str, *, timeout: float = 1800, rsync: Optional[bool] = None) -> Tuple[int, str, str]:
    """Copy src into the folder dst (made if missing); exactly one side is `<alias>:<path>`.
    Returns (exit code, "rsync" or "tar", error text). 124: timed out; 255: ssh failed.
    An rsync that rejects the call (exit 1, usage; 2, protocol) is retried once by tar."""
    (sa, sp), (da, dp) = split(src), split(dst)
    if bool(sa) == bool(da):
        raise CopyError("exactly one of src and dst is <alias>:<path>")
    alias = sa or da
    sp = sp.rstrip("/") or "/"
    parent, name = os.path.dirname(sp) or ".", os.path.basename(sp)
    if not name or name in (".", ".."):
        raise CopyError(f"{src}: name a file or folder, not {sp!r}")
    if not sa and not os.path.exists(sp):
        raise CopyError(f"{src}: no such file or folder")
    use = have_rsync(alias) if rsync is None else rsync
    if da:
        made = box.ssh_run(alias, f"mkdir -p {_remote(dp)}", timeout=60)
        if made.returncode:
            return made.returncode, "rsync" if use else "tar", made.stderr
    else:
        os.makedirs(dp, exist_ok=True)
    if use:
        # Relative remote paths are under the home folder for rsync as for ssh.
        rpath = sp if sa else dp
        rpath = rpath[2:] if rpath.startswith("~/") else ("." if rpath == "~" else rpath)
        argv = ["rsync", "-a", "-s", "-e", shlex.join(["ssh", *box.SSH_OPTS]),
                f"{alias}:{rpath}" if sa else sp, (dp if da is None else f"{alias}:{rpath}").rstrip("/") + "/"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "rsync", f"timed out after {timeout:g}s"
        if r.returncode not in (1, 2):
            return r.returncode, "rsync", r.stderr
    return _tar(alias, parent, name, dp, to_box=bool(da), timeout=timeout)


def _tar(alias: str, parent: str, name: str, dp: str, *, to_box: bool, timeout: float) -> Tuple[int, str, str]:
    ssh = ["ssh", *box.SSH_OPTS, alias]
    if to_box:
        send = ["tar", "-C", parent, "-cf", "-", name]
        recv = ssh + [f"tar -C {_remote(dp)} -xf -"]
    else:
        send = ssh + [f"tar -C {_remote(parent)} -cf - {shlex.quote(name)}"]
        recv = ["tar", "-C", dp, "-xf", "-"]
    # The sender's errors go to a file, so a chatty sender never stalls on a full pipe.
    with tempfile.TemporaryFile() as errf:
        p1 = subprocess.Popen(send, stdout=subprocess.PIPE, stderr=errf)
        p2 = subprocess.Popen(recv, stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        p1.stdout.close()
        try:
            _, err2 = p2.communicate(timeout=timeout)
            p1.wait(timeout=60)
        except subprocess.TimeoutExpired:
            for p in (p1, p2):
                p.kill()
                p.wait()
            return 124, "tar", f"timed out after {timeout:g}s"
        errf.seek(0)
        err1 = errf.read()
    rc = p1.returncode or p2.returncode
    return rc, "tar", err1.decode(errors="replace") + (err2 or b"").decode(errors="replace")


def cmd_copy(args) -> int:
    try:
        rc, how, err = copy(args.src, args.dst, timeout=args.timeout)
    except CopyError as e:
        print(f"error: {e}")
        return 2
    if rc:
        print(f"error: copy by {how} failed (exit {rc}): {err.strip()[-500:]}")
        return rc
    print(f"copied {args.src} into {args.dst} by {how}")
    return 0


def register(sub) -> None:
    p = sub.add_parser("copy", help="copy a file or folder to or from a box (rsync, else tar over ssh)")
    p.add_argument("src", help="a file or folder; `<alias>:<path>` on a box")
    p.add_argument("dst", help="the folder it lands in; `<alias>:<path>` on a box")
    p.add_argument("--timeout", type=float, default=1800, help="seconds (default 1800)")
    p.set_defaults(func=cmd_copy)

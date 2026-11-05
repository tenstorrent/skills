"""A check that failed on a shell syntax error in a script outside the change under test.

A shell reads a script a piece at a time, so a script rewritten in place while a check runs it (a project's own
harness script, say, edited by another task) dies with `line N: syntax error near unexpected token` although the
file is whole again a moment later. That says nothing about the change. transient() names such scripts in a failed
check's output: each must exist, lie outside the change under test (outside the repository, or a file the diff
from `base` does not touch, and not under a temporary folder, where a test's own fixtures live) and pass `bash -n`
now. Any one that fails those (a file the change touches, a script still broken, a path it cannot find) makes it
answer nothing: a real failure stays one. The runners rerun such a check once after SETTLE_S; failing so again, it
is an infrastructure fault that leaves the change neither passed nor failed (`ttp push` exits BUSY, `ttp checks` 75).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SETTLE_S = 20         # the wait before the one rerun: a rewrite in progress ends
RETRY_S = 600         # a push stopped by it is tried again after this
MAX_SCRIPTS = 20      # more named scripts than this: not a torn read, never judged
INFRA = "script-syntax-transient"
# bash: `path: line 131: syntax error near unexpected token `('`; dash: `path: 131: Syntax error: "(" unexpected`.
_LINE = re.compile(r"""(?P<path>[^\s:'"`]+): (?:line )?(?P<n>\d+): [Ss]yntax error""")


def scripts(text: str) -> list[str]:
    """The script paths a shell named in a syntax error in `text`, first seen first."""
    seen: dict[str, None] = {}
    for m in _LINE.finditer(text or ""):
        seen.setdefault(m.group("path"), None)
    return list(seen)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)


def _temp_roots() -> list[Path]:
    out = []
    for d in (os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev/shm"):
        if d:
            try:
                out.append(Path(d).resolve())
            except OSError:
                pass
    return out


def _parses(path: Path) -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    try:
        return subprocess.run([bash, "-n", str(path)], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def transient(text: str, cwd: str | Path, base: str | None, head: str = "HEAD") -> list[str]:
    """The scripts outside the change whose syntax error in `text` was transient (see the module doc); [] when
    there is none, or when any script named in a syntax error does not qualify. `cwd` is the folder the check
    ran in (relative paths resolve there), `base` the commit the change is measured from (none: every file of
    the repository counts as changed)."""
    named = scripts(text)
    if not named or len(named) > MAX_SCRIPTS:
        return []
    cwd = Path(cwd)
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel")
        top = Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
        changed = None
        if top and base:
            d = _git(cwd, "-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", f"{base}...{head}")
            changed = set(d.stdout.splitlines()) if d.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return []
    temps = _temp_roots()
    for name in named:
        if not os.path.isabs(name) and top and (changed is None or any(c == name or c.endswith(f"/{name}")
                                                                        for c in changed)):
            return []   # relative to a folder the check may have changed to: perhaps a file the change touches
        path = Path(name) if os.path.isabs(name) else cwd / name
        try:
            if not path.is_file():
                return []
            real = path.resolve()
        except OSError:
            return []
        if any(real.is_relative_to(t) for t in temps):
            return []
        if top and real.is_relative_to(top):
            if changed is None or real.relative_to(top).as_posix() in changed:
                return []
        if not _parses(real):
            return []
    return named


def rerun_line(found: list[str], cmd: str) -> str:
    return (f"{cmd}: a syntax error in {', '.join(found)} (outside the change, whole again now): a script rewritten "
            f"in place while the check ran it; running it once more in {SETTLE_S} s")


def line(found: list[str], cmd: str) -> str:
    return (f"{cmd} failed again on a transient syntax error in {', '.join(found)} (outside the change, whole again "
            "now): a script outside the change is being rewritten in place, an infrastructure fault, not the change")


class Infra(Exception):
    """A check that failed on a transient syntax error outside the change, and again on its rerun: raised by
    the push queue's check runner, whose batch then goes back to the queue, held, never failed."""

    def __init__(self, scripts: list[str], check: str):
        super().__init__(line(scripts, check))
        self.scripts, self.check = scripts, check

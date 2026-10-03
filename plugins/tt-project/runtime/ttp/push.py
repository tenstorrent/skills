# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Guarded push: publish the current commit onto the project's target branch only after the
project's checks passed on exactly the commit being pushed, and never with force.

Pushes of one project to one branch take turns: each holds the lock `push:<remote>/<branch>` from
its first fetch to its push, so two reviewers never race each other through rounds. The lock is an
OS file lock (locks.py): a killed push or a reboot frees it."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import locks
from .budget import DOC_SUFFIXES
from .project import Project

# Exit codes, distinct so a worker can say why it did not push. BUSY: another push to the same
# branch kept the lock past `delivery.push_wait_s`; the task hands back `waiting`.
REFUSED, CONFLICT, CHECKS_FAILED, KEPT_MOVING, REJECTED, BUSY = 2, 3, 4, 5, 6, 75
DEFAULT_ROUNDS = 3
DEFAULT_WAIT_S = 300
PROTECTED = {"HEAD", "main", "master"}
NO_CHECKS = ("set delivery.push_checks to the commands that must pass on the exact commit before it "
             "is pushed (a list, or one per line, e.g. the repository's test suite); the coordinator "
             "sets it with config_set")


def check_list(v: Any) -> list[str]:
    """Check commands from a config value: a list, a JSON-encoded list, or one command per line."""
    if isinstance(v, str):
        text = v.strip()
        try:
            v = json.loads(text) if text.startswith("[") else text.splitlines()
        except ValueError:
            v = text.splitlines()
    return [str(c).strip() for c in (v or []) if str(c).strip()]



# Shell builtins and keywords a check may start with; anything else must be a program on PATH or a path.
BUILTINS = {".", ":", "[", "[[", "!", "(", "{", "bash", "sh", "cd", "command", "eval", "exec", "exit",
            "export", "false", "for", "if", "set", "source", "test", "true", "type", "ulimit", "umask",
            "unset", "while", "case", "time", "env"}


def check_problem(cmd: str) -> str | None:
    """Why `cmd` cannot be a check command (its first word is no program, path or builtin), or None."""
    try:
        words = shlex.split(cmd)
    except ValueError as e:
        return f"{cmd!r} does not parse as a shell command ({e})"
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):   # leading VAR=value
        words.pop(0)
    if not words:
        return None
    first = words[0]
    if "/" in first or first in BUILTINS or shutil.which(first):
        return None
    return f"{cmd!r}: {first!r} is not a program on PATH, a path or a shell builtin; push_checks are commands"


def check_problems(v: Any) -> list[str]:
    return [p for p in map(check_problem, check_list(v)) if p]


def checks_of(v: Any) -> list[str]:
    """`check_list`, rejecting entries that are not commands (e.g. a sentence describing the checks)."""
    bad = check_problems(v)
    if bad:
        raise ValueError("; ".join(bad))
    return check_list(v)

def _git(repo: Path, *args: str, quiet: bool = True) -> subprocess.CompletedProcess:
    """Run git; when not quiet its output goes to stderr, keeping stdout for the verdict."""
    if quiet:
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    return subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=2)


def _fetch(repo: Path, remote: str, branch: str) -> str:
    """The remote branch's current tip, or "" when it cannot be read. The explicit refspec also
    works where the remote's configured refspec does not cover the branch."""
    if _git(repo, "fetch", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            quiet=False).returncode != 0:
        return ""
    return _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}").stdout.strip()


def target(p: Project, repo: Path) -> tuple[str, str]:
    """(remote, branch) to push to: `delivery.push_branch` only. `delivery.base_ref` is where work
    starts, which may be the default branch, so it is never a push target."""
    d = p.config().get("delivery") or {}
    ref = str(d.get("push_branch") or "").strip()
    if not ref:
        raise ValueError("no target branch: set delivery.push_branch")
    remote, _, rest = ref.partition("/")
    if not (rest and remote in _git(repo, "remote").stdout.split()):
        remote, rest = "origin", ref
    return remote, rest[len("refs/heads/"):] if rest.startswith("refs/heads/") else rest


def refusal(repo: Path, remote: str, branch: str) -> str:
    """Why `branch` on `remote` must not be pushed to, or "" when it may. Fails closed when the
    remote cannot be asked for its default branch."""
    if branch in PROTECTED:
        return f"refusing to push to {remote}/{branch}"
    ls = _git(repo, "ls-remote", "--symref", remote, "HEAD")
    if ls.returncode != 0:
        return f"cannot reach {remote}: {ls.stderr.strip()}"
    for line in ls.stdout.splitlines():
        if line.startswith("ref: ") and line[5:].split("\t")[0] == f"refs/heads/{branch}":
            return f"refusing to push to {remote}/{branch}, the remote's default branch"
    return ""


def is_doc(path: str) -> bool:
    """A documentation file: prose by its suffix, or anything under a `docs/` or `doc/` folder."""
    return path.lower().endswith(DOC_SUFFIXES) or any(d in ("docs", "doc") for d in path.split("/")[:-1])


def code_paths(repo: Path, tip: str) -> list[str]:
    """The files this change touches since it left `tip` that are not docs. --no-renames lists a
    moved file under both names, so moving code into docs/ still counts as code."""
    diff = _git(repo, "diff", "--name-only", "--no-renames", f"{tip}...HEAD")
    if diff.returncode != 0:
        return ["(the diff could not be read)"]
    return [f for f in diff.stdout.splitlines() if f.strip() and not is_doc(f)]


MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")


def _version(repo: Path, rev: str, path: str) -> str:
    """The `version` in the JSON file `path` at `rev`, or "" when it has none or cannot be read."""
    show = _git(repo, "show", f"{rev}:{path}")
    try:
        v = json.loads(show.stdout).get("version") if show.returncode == 0 else ""
    except (ValueError, AttributeError):
        return ""
    return str(v or "")


def stale_versions(repo: Path, tip: str) -> list[str]:
    """Plugins under `plugins/<name>/` whose content HEAD changes since `tip` while a manifest
    keeps tip's version, as "plugins/<name> <version>". Two changes that each bumped to the same
    version rebase onto each other cleanly; the second must take the next version, or installs
    that only upgrade to a strictly newer one skip it."""
    diff = _git(repo, "diff", "--name-only", "--no-renames", tip, "HEAD")
    names = sorted({"/".join(f.split("/")[:2]) for f in diff.stdout.splitlines()
                    if f.startswith("plugins/") and f.count("/") >= 2})
    out = []
    for d in names:
        for m in MANIFESTS:
            v = _version(repo, tip, f"{d}/{m}")
            if v and v == _version(repo, "HEAD", f"{d}/{m}"):
                out.append(f"{d} {v}")
                break
    return out


def rounds_of(v: Any) -> int:
    """`delivery.push_rounds` as an integer of at least 1; ValueError when it is not an integer."""
    if v is None or v == "":
        return DEFAULT_ROUNDS
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}")
    try:
        return max(1, int(v))
    except ValueError:
        raise ValueError(f"delivery.push_rounds must be an integer, not {v!r}") from None


def wait_of(v: Any) -> float:
    """`delivery.push_wait_s` as seconds of at least 0; ValueError when it is not a number."""
    if v is None or v == "":
        return float(DEFAULT_WAIT_S)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"delivery.push_wait_s must be a number of seconds, not {v!r}")
    try:
        return max(0.0, float(v))
    except ValueError:
        raise ValueError(f"delivery.push_wait_s must be a number of seconds, not {v!r}") from None


def lock_name(remote: str, branch: str) -> str:
    return f"push:{remote}/{branch}"


def lock_paths(p: Project, remote: str, branch: str) -> list[Path]:
    """The lock file of pushes to remote/branch; the branch is quoted so its slashes stay in one name."""
    return locks.slot_paths(p.state / "locks", "push:" + quote(f"{remote}/{branch}", safe=""), 1)


def free_probe(p: Project) -> str:
    """A `retry_when` that exits 0 once the push lock is free. The harness runs it in the project
    root, maybe without the harness on its PATH, so it names the project's own `ttp`."""
    ttp = p.harness / "bin" / "ttp"
    return f"{shlex.quote(str(ttp)) if ttp.is_file() else 'ttp'} push --free"


def take(p: Project, remote: str, branch: str, wait_s: float, poll_s: float = 1.0,
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)):
    """The held push lock of remote/branch, waiting up to `wait_s` for it; None when it stayed busy.
    Inside a run the wait is recorded, so it does not count against the run's wall clock."""
    paths = lock_paths(p, remote, branch)
    who = (f"task #{os.environ['TTP_TASK']} (run {os.environ.get('TTP_RUN_ID') or '?'})"
           if os.environ.get("TTP_TASK") else f"pid {os.getpid()}")
    f = locks.try_take(paths, who, "ttp push")
    if f:
        return f
    name = lock_name(remote, branch)
    say(f"waiting up to {wait_s:.0f} s for {name} (held by {', '.join(locks.holders(paths)) or 'another push'})")
    run_dir = Path(os.environ["TTP_RUN_DIR"]) if os.environ.get("TTP_RUN_DIR") else None
    started = time.time()
    key = f"push:{os.getpid()}:{started}"
    if run_dir:
        locks.record_wait(run_dir, key, started, None)
    try:
        while not f and time.time() - started < wait_s:
            time.sleep(max(0.0, min(poll_s, wait_s - (time.time() - started))))
            f = locks.try_take(paths, who, "ttp push")
    finally:
        if run_dir:
            locks.record_wait(run_dir, key, started, time.time())
    if f:
        say(f"got {name} after {time.time() - started:.0f} s")
        return f
    say(f"{name} stayed busy for {wait_s:.0f} s (held by {', '.join(locks.holders(paths)) or 'another push'}); "
        f"hand the task back as waiting with retry_when: {free_probe(p)}")
    return None


def push(repo: Path, remote: str, branch: str, checks: list[str], rounds: int = DEFAULT_ROUNDS,
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
         hold: Callable[[], Any] | None = None) -> int:
    """Rebase HEAD onto remote/branch, run `checks` on the result, and push it if the remote did not
    move meanwhile; if it did, start over, at most `rounds` times. With no checks only a change that
    touches nothing but docs goes through. `hold` takes the push lock once the quick refusals
    passed: it returns the held lock, or None when it stayed busy (BUSY)."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        say("uncommitted changes; commit first")
        return REFUSED
    why = refusal(repo, remote, branch)
    if why:
        say(why)
        return REFUSED
    lock = hold() if hold else None
    if hold and lock is None:
        return BUSY
    try:
        return _rounds(repo, remote, branch, checks, rounds, say)
    finally:
        if lock is not None:
            lock.close()


def _rounds(repo: Path, remote: str, branch: str, checks: list[str], rounds: int,
            say: Callable[[str], None]) -> int:
    upstream = f"{remote}/{branch}"
    for rnd in range(1, rounds + 1):
        tip = _fetch(repo, remote, branch)
        if not tip:
            say(f"cannot fetch {upstream}")
            return REFUSED
        if not checks and (code := code_paths(repo, tip)):
            # Checked before the rebase, so a refused push leaves the branch as it was.
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}{more}): "
                + NO_CHECKS)
            return REFUSED
        if _git(repo, "rebase", tip, quiet=False).returncode != 0:
            _git(repo, "rebase", "--abort")
            say(f"rebase onto {upstream} conflicts; resolve it keeping both sides' intents, then rerun")
            return CONFLICT
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if stale := stale_versions(repo, tip):
            say(f"{', '.join(stale)}: this change edits the plugin but keeps the version already on "
                f"{upstream}; bump it past that (in every manifest, plus a changeset where the "
                "repository wants one), commit, then rerun; not pushing")
            return CHECKS_FAILED
        for cmd in checks:
            if subprocess.run(cmd, shell=True, cwd=repo).returncode != 0:
                say(f"check failed on {head[:10]}: {cmd}; not pushing")
                return CHECKS_FAILED
        if _fetch(repo, remote, branch) != tip:
            say(f"{upstream} moved during the checks; round {rnd + 1}")
            continue
        # Without --force the remote refuses anything that is not a fast-forward of what we tested on.
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            say(f"pushed {head[:10]} to {upstream}")
            return 0
        if _fetch(repo, remote, branch) == tip:
            say(f"push to {upstream} was rejected")
            return REJECTED
        say(f"{upstream} moved before the push; round {rnd + 1}")
    say(f"{upstream} kept moving for {rounds} rounds; not pushing")
    return KEPT_MOVING


def run(p: Project, repo: Path) -> int:
    """`ttp push` for a project: target, checks and rounds come from its `delivery` config."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    checks = check_list(d.get("push_checks"))   # none: only a docs-only change may go (_rounds)
    try:
        remote, branch = target(p, repo)
        rounds = rounds_of(d.get("push_rounds"))
        wait_s = wait_of(d.get("push_wait_s"))
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    return push(repo, remote, branch, checks, rounds, hold=lambda: take(p, remote, branch, wait_s))


def free(p: Project, repo: Path) -> int:
    """`ttp push --free`: 0 when no push of this project holds a push lock, 1 while one does, 2 when
    there is no target. Read-only and instant, for a waiting task's `retry_when`. It checks every
    push lock of the project: the harness runs the probe in the project root, whose git remotes
    may resolve `delivery.push_branch` differently from the worktree that pushes."""
    try:
        target(p, repo)
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    held = [x for x in sorted((p.state / "locks").glob("push:*.lock")) if not locks.any_free([x])]
    if not held:
        return 0
    print(f"ttp push: a push holds its turn: {', '.join(locks.holders(held)) or 'another push'}",
          file=sys.stderr)
    return 1

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Guarded push: publish the current commit onto the project's target branch only after the
project's checks passed on exactly the commit being pushed, and never with force. The push runs
those checks itself every time: it never reads the passes `ttp checks` records (cli.CHECK_PASSES),
so a forged record can at most skip a local re-run, never put a change on the branch.

Pushes of one project to one branch take turns: each holds the lock `push:<remote>/<branch>` from
its first fetch to its push, so two reviewers never race each other through rounds. The lock is an
OS file lock (locks.py): a killed push or a reboot frees it. A waiting push waits as long as the
last measured check run takes, with a margin, up to 2 h (`delivery.push_wait_s` overrides it).

With `delivery.version_bump` set, the push also owns the version bump: after each rebase it sets the
listed files one patch version above the tip's and adds a changeset, in one commit of its own. The
bump happens under the lock, so parallel pushes never race for one version."""
from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import locks
from .budget import DOC_SUFFIXES
from .project import Project, durable_write, git_fsync_env, nice_level, renice, write_json

# Exit codes, distinct so a worker can say why it did not push. BUSY: another push to the same
# branch kept the lock past `delivery.push_wait_s`; the task hands back `waiting`.
REFUSED, CONFLICT, CHECKS_FAILED, KEPT_MOVING, REJECTED, BUSY = 2, 3, 4, 5, 6, 75
DEFAULT_ROUNDS = 3
DEFAULT_WAIT_S = 900       # the lock wait with no measured check run, and its floor otherwise
WAIT_MARGIN_S = 60          # on top of two check runs: the holder may start over once
MAX_DEFAULT_WAIT_S = 7200  # the measured wait's ceiling; only an explicit push_wait_s waits longer
TIMINGS = "push_checks.json"   # under the project's state: how long the last full check run took
BUMP_TRAILER = "Ttp-Version-Bump"   # marks the bump commit ttp push made, so a rerun replaces it
VERSION_RE = re.compile(r"""(version(?:__)?["']?\s*[:=]\s*["'])(\d+)\.(\d+)\.(\d+)(["'])""", re.I)
PROTECTED = {"HEAD", "main", "master"}
OWN_BRANCH = re.compile(r"ttp/t(\d+)-\S+")   # a task's own branch, as worktree.ensure names it
NO_CHECKS = ("set delivery.push_checks to the commands that must pass on the exact commit before it "
             "is pushed (a list, or one per line, e.g. the repository's test suite); the coordinator "
             "sets it with config_set")


class Check(str):
    """One check command. A plain string runs on every head. The opt-in form
    `{"run": "<cmd>", "if_exists": "<repo path or glob>"}` runs only on a head that has a file
    matching `if_exists` (a directory counts by its files); elsewhere it is skipped as not
    applicable, which is reported and never counted as passed. `"if_changed": "<glob>"` (or a list
    of globs) scopes a check by what it covers: it is skipped on a head whose diff since the push
    target touches no matching path, and a head where every check is skipped that way changes
    nothing any check covers (`outside_scope`)."""
    if_exists = ""
    if_changed: tuple[str, ...] = ()

    def config(self) -> str | dict:
        """The form project.json keeps."""
        if not (self.if_exists or self.if_changed):
            return str(self)
        out: dict = {"run": str(self)}
        if self.if_exists:
            out["if_exists"] = self.if_exists
        if self.if_changed:
            out["if_changed"] = self.if_changed[0] if len(self.if_changed) == 1 else list(self.if_changed)
        return out


CHECK_KEYS = {"run", "if_exists", "if_changed"}


def _entries(v: Any) -> list:
    """Check entries from a config value: a list, a JSON-encoded list, or one command per line."""
    if isinstance(v, str):
        text = v.strip()
        try:
            v = json.loads(text) if text.startswith("[") else text.splitlines()
        except ValueError:
            v = text.splitlines()
    if isinstance(v, dict):
        v = [v]
    return [c for c in (v or []) if isinstance(c, dict) or str(c).strip()]


def _check(c: Any) -> Check | None:
    if not isinstance(c, dict):
        return Check(str(c).strip())
    cmd = str(c.get("run") or "").strip()
    if not cmd:
        return None
    out = Check(cmd)
    # Strict: the check skips only when it validly opted in; anything malformed runs it everywhere.
    if not _form_problem(c):
        out.if_exists = _if_exists(c)
        out.if_changed = _if_changed(c)
    return out


def _if_exists(c: dict) -> str:
    """The check's `if_exists`, normalized so 'tests/' and './tests/a.sh' match ls-tree paths."""
    p = str(c.get("if_exists") or "").strip() if isinstance(c.get("if_exists"), str) else ""
    return posixpath.normpath(p) if p else ""


def _if_changed(c: dict) -> tuple[str, ...]:
    """The check's `if_changed` globs (one string or a list), normalized as `_if_exists`."""
    v = c.get("if_changed")
    globs = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    return tuple(posixpath.normpath(g.strip()) for g in globs if isinstance(g, str) and g.strip())


def check_list(v: Any) -> list[Check]:
    """Check commands from a config value (`_entries`), as `Check`s."""
    return [c for c in map(_check, _entries(v)) if c]


def matches(files: list[str], pattern: str) -> bool:
    """Whether a repo path or glob matches one of `files` (repo-relative); a directory by its files."""
    import fnmatch
    return any(f == pattern or f.startswith(pattern + "/") or fnmatch.fnmatchcase(f, pattern) for f in files)


class ScopeError(Exception):
    """The diff an `if_changed` check is scoped by could not be read: the check fails, never skips."""

    def __init__(self, check: Check, why: str):
        super().__init__(f"{why}; failing {check}")
        self.check = check


class Skipped(str):
    """A `skipped_line`; `scoped` when the check was skipped because the diff is outside its
    `if_changed` scope."""
    scoped = False


def skip_reason(repo: Path, head: str, check: Check, base: str = "") -> str | None:
    """Why `check` does not apply to `head`, or None: it runs. `if_changed` is looked up in the diff
    from `base` (the push target) to `head`; without a base it is not applied, and a diff git cannot
    give raises ScopeError. `if_exists` is looked up in `head`'s files."""
    return _changed_reason(repo, head, check, base) or _exists_reason(repo, head, check)


def _changed_reason(repo: Path, head: str, check: Check, base: str) -> str | None:
    globs = getattr(check, "if_changed", ())
    if not (globs and base):
        return None
    diff = _git(repo, "-c", "core.quotePath=false", "diff", "--name-only", "--no-renames", f"{base}...{head}")
    if diff.returncode != 0:
        raise ScopeError(check, f"cannot tell what {head[:10]} changes since {base[:10]} "
                                f"({_tail(diff.stderr) or f'git diff exit {diff.returncode}'})")
    files = [f for f in diff.stdout.splitlines() if f]
    if any(matches(files, g) for g in globs):
        return None
    return f"{head[:10]} changes nothing under {', '.join(globs)} since {base[:10]}"


def _tail(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _exists_reason(repo: Path, head: str, check: Check) -> str | None:
    if not getattr(check, "if_exists", ""):
        return None
    ls = _git(repo, "ls-tree", "-r", "--name-only", head)
    if ls.returncode != 0:
        return None   # cannot tell: run it
    if matches(ls.stdout.splitlines(), check.if_exists):
        return None
    return f"{check.if_exists} does not exist at {head[:10]}"


def skipped_line(check: Check, why: str) -> str:
    return f"skipped (not applicable: {why}): {check}"


def outside_scope(checks: list[Check], skipped: list[str]) -> bool:
    """Every check was skipped because the change touches none of its `if_changed` paths: the change
    is outside what any check covers, as a docs-only change is when no checks are set. A check
    skipped by `if_exists` (its file is missing) says nothing about the change, so it keeps the
    head unchecked (NONE_APPLY)."""
    return bool(checks) and len(skipped) == len(checks) and all(getattr(s, "scoped", False) for s in skipped)


def check_argv(cmd: str) -> list[str]:
    """How a check command runs: first-failure semantics, so `a; b` fails when `a` fails and a
    pipeline fails when any stage does (bash `-e -o pipefail`; plain `sh -e` where bash is missing)."""
    bash = shutil.which("bash")
    return [bash, "-e", "-o", "pipefail", "-c", cmd] if bash else ["sh", "-e", "-c", cmd]


def check_env(mode: str, tip: str = "") -> dict[str, str]:
    """The environment push_checks run in. TTP_PUSH_MODE says what they gate: `target` (`ttp push`
    or the push queue, to delivery.push_branch), `own` (`ttp push --own`, the task's own branch) or
    `checks` (`ttp checks`, which pushes nothing). TTP_PUSH_TIP is the tip of the branch pushed to,
    as fetched before the checks; empty when there is none (a new branch, or `ttp checks`)."""
    return {**os.environ, "TTP_PUSH_MODE": mode, "TTP_PUSH_TIP": tip or ""}


def exclude_list(v: Any) -> list[str]:
    """`delivery.push_exclude_paths` as repo-relative globs (a list, a JSON list or one per line);
    empty, the default, turns the guard off."""
    return [posixpath.normpath(str(g).strip()) for g in _entries(v) if not isinstance(g, dict)]


def fast_forward_list(v: Any) -> list[str]:
    """`delivery.fast_forward_also` as branch names (a list, a JSON list or one per line); empty, the
    default, turns it off."""
    return list(dict.fromkeys(str(b).strip() for b in _entries(v) if not isinstance(b, dict) and str(b).strip()))


def fast_forward_problem(branch: str, push_branch: str) -> str:
    """Why `branch` may not be in `delivery.fast_forward_also`, or "" when it may. `push_branch` is
    the resolved branch, or the raw setting (`<remote>/<branch>` or `<branch>`)."""
    if branch == push_branch or push_branch.endswith(f"/{branch}"):
        return "it is the push branch itself (delivery.push_branch)"
    if branch.startswith("-") or subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"],
                                                capture_output=True).returncode != 0:
        return "not a valid branch name"
    return ""


def fast_forward_check(v: Any, push_branch: str) -> list[str]:
    """`delivery.fast_forward_also` as config_set takes it; ValueError naming the first bad branch."""
    names = fast_forward_list(v)
    for b in names:
        if why := fast_forward_problem(b, push_branch):
            raise ValueError(f"delivery.fast_forward_also: {b!r}: {why}")
    return names


def fast_forward(repo: Path, remote: str, sha: str, branches: list[str], push_branch: str,
                 say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr)) -> list[str]:
    """After `sha` reached `push_branch`, move each of `branches` (delivery.fast_forward_also) on
    `remote` to it, without force and only when its tip there is an ancestor of `sha`. Each outcome
    is read back from the remote with ls-remote: `ff <branch> <sha>`, or `not ff <branch>: <reason>`.
    A `not ff` never undoes the main push; the caller reports it as a warning."""
    out = []
    for b in branches:
        if why := fast_forward_problem(b, push_branch):
            line = f"not ff {b}: {why}"
        elif not (tip := _existing_tip(repo, remote, b)):
            line = f"not ff {b}: {remote}/{b} does not exist or cannot be read"
        elif tip != sha and not descends(repo, tip, sha):
            line = f"not ff {b}: {remote}/{b} at {tip[:10]} is not an ancestor of {sha[:10]}; left as it is"
        else:
            r = _git(repo, "push", remote, f"{sha}:refs/heads/{b}") if tip != sha else None
            ls = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{b}")
            now = next((ln.split()[0] for ln in ls.stdout.splitlines() if ln.endswith(f"\trefs/heads/{b}")), "")
            if now == sha:
                line = f"ff {b} {sha}"
            elif r is not None and r.returncode != 0:
                line = f"not ff {b}: the push was rejected: {' / '.join(r.stderr.strip().splitlines()[-3:])[-300:]}"
            else:
                line = f"not ff {b}: {remote}/{b} reads {now[:10] or '(nothing)'} after the push, not {sha[:10]}"
        say(line if line.startswith("ff ") else f"warning: {line}")
        out.append(line)
    return out


def ff_warnings(lines: Any) -> list[str]:
    """The `not ff` lines of a recorded fast_forward outcome."""
    return [str(x) for x in lines or [] if str(x).startswith("not ff ")]


def excluded(repo: Path, tip: str, head: str, globs: list[str]) -> list[str]:
    """Files a commit of `head` not yet on `tip` adds or modifies (a deletion is fine) that match
    one of `globs` (`matches`: a path, a directory by its files, or an fnmatch glob whose `*` also
    crosses `/`). Each commit counts, not only the end result: a file added and later deleted would
    still reach the branch's history. Commits whose patch `tip` already has are left out."""
    if not globs:
        return []
    log = _git(repo, "-c", "core.quotePath=false", "log", "--no-merges", "--right-only", "--cherry-pick",
               "--no-renames", "--diff-filter=AMT", "--name-only", "--format=", f"{tip}...{head}")
    return [f for f in dict.fromkeys(log.stdout.splitlines()) if f and any(matches([f], g) for g in globs)]


def excluded_refusal(files: list[str], upstream: str) -> str:
    more = f" and {len(files) - 5} more" if len(files) > 5 else ""
    return (f"this change adds or modifies files that delivery.push_exclude_paths keeps off {upstream} "
            f"({', '.join(files[:5])}{more}); take them out of the commits that add them (a later "
            "delete still leaves them in its history), or publish the branch with `ttp push --own`; "
            "not pushing")


NONE_APPLY = "every check was skipped as not applicable, so nothing checked it"
NONE_APPLY_FIX = "add a delivery.push_checks entry that applies to this change"
OUT_OF_SCOPE = ("every check was skipped by its if_changed scope: this change touches nothing any check "
                "covers, so it goes unchecked")
NONE_APPLY_OWN_FIX = ("run the repository's tests on this commit with `ttp checks -- <cmd>` (`ttp push --own` "
                      "runs the commands it recorded passing there), or " + NONE_APPLY_FIX)
RECORDED = "checks.json"   # in a run's directory, as `ttp checks` writes it (prguard.CHECKS_FILE)


def recorded_checks(run_dir: str | Path | None) -> dict | None:
    """What `ttp checks` last recorded in the run's directory when it passed: {"head", "commands"}, or None
    (no run, no record, a failed or malformed one)."""
    try:
        rec = json.loads((Path(run_dir) / RECORDED).read_text()) if run_dir else None
    except (OSError, ValueError):
        return None
    if not (isinstance(rec, dict) and rec.get("passed") is True and isinstance(rec.get("head"), str)
            and isinstance(rec.get("commands"), list)):
        return None
    return {"head": rec["head"], "commands": [str(c) for c in rec["commands"] if str(c).strip()]}


def recorded_extras(recorded: dict | None, head: str, checks: list[Check]) -> list[str]:
    """The commands beyond the project's `checks` that `ttp checks -- <cmd>` recorded passing on exactly
    `head`: what `ttp push --own` runs when every project check is skipped there. A record of another
    head, or of nothing but project checks, gives none."""
    if not recorded or not head or recorded.get("head") != head:
        return []
    own = {str(c) for c in checks}
    return [c for c in recorded.get("commands") or [] if c not in own]


def applicable(repo: Path, head: str, checks: list[Check], say: Callable[[str], None],
               base: str = "") -> tuple[list[Check], list[str]]:
    """(checks to run on `head`, a `Skipped` line per check that does not apply there, each also said).
    `base` is the push target `if_changed` compares with. Callers treat "none to run" from a non-empty
    list as a failure (NONE_APPLY), never as a pass, unless `outside_scope`. Raises ScopeError."""
    todo, skipped = [], []
    for c in checks:
        changed = _changed_reason(repo, head, c, base)
        why = changed or _exists_reason(repo, head, c)
        if why:
            line = Skipped(skipped_line(c, why))
            line.scoped = bool(changed)
            skipped.append(line)
            say(line)
        else:
            todo.append(c)
    return todo, skipped


# Shell builtins and keywords a check may start with; anything else must be a program on PATH or a path.
BUILTINS = {".", ":", "[", "[[", "!", "(", "{", "bash", "sh", "cd", "command", "eval", "exec", "exit",
            "export", "false", "for", "if", "set", "source", "test", "true", "type", "ulimit", "umask",
            "unset", "while", "case", "time", "env"}


def _word_end(s: str, i: int) -> int:
    """Index just past the shell word starting at s[i], keeping quotes, `...`, $(...) and ${...}
    whole; -1 when one of them is not closed."""
    depth = 0                                      # open $( / ( inside a $(...)
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c in "'`":
            j = s.find(c, i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < len(s) and s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            if j >= len(s):
                return -1
            i = j + 1
            continue
        if s.startswith("${", i):
            j = s.find("}", i)
            if j < 0:
                return -1
            i = j + 1
            continue
        if s.startswith("$(", i) or (c == "(" and depth):
            depth += 1
            i += 2 if c == "$" else 1
            continue
        if c == ")" and depth:
            depth -= 1
        elif not depth and (c.isspace() or c in ";&|<>()"):
            return i
        i += 1
    return -1 if depth else i


def check_problem(cmd: str) -> str | None:
    """Why `cmd` cannot be a check command (its first word is no program, path or builtin), or None.
    Leading VAR=value words are skipped; a bare assignment (`h=$(git rev-parse HEAD); ...`) is a
    command of its own, judged by the command it substitutes, if any; a prefix assignment's
    substitution is judged the same way. Arithmetic $((...)) is no command."""
    rest = cmd.strip()
    while m := re.match(r"[A-Za-z_][A-Za-z0-9_]*=", rest):              # leading VAR=value
        end = _word_end(rest, m.end())
        if end < 0:
            return f"{cmd!r} does not parse as a shell command (unclosed quote or substitution)"
        value, rest = rest[m.end():end], rest[end:].lstrip(" \t")
        sub = None if value.lstrip('"').startswith("$((") else \
            re.fullmatch(r'"?\$\((.*)\)"?|"?`(.*)`"?', value, re.S)    # $((...)) is arithmetic
        inner = sub and (sub.group(1) or sub.group(2) or "").strip()
        if inner and (problem := check_problem(inner)):                   # bare or prefix alike
            return problem
        if not rest or rest[0] in ";&|<>\n":
            return None
    try:
        words = shlex.split(rest)
    except ValueError as e:
        return f"{cmd!r} does not parse as a shell command ({e})"
    if not words:
        return None
    first = words[0]
    if "/" in first or first in BUILTINS or shutil.which(first):
        return None
    return f"{cmd!r}: {first!r} is not a program on PATH, a path or a shell builtin; push_checks are commands"


def _form_problem(c: dict) -> str | None:
    """Why a check object is malformed: it takes "run", "if_exists" (a repo-relative path or glob)
    and "if_changed" (one such glob or a non-empty list of them) and nothing else."""
    cmd = c.get("run")
    if not isinstance(cmd, str) or not cmd.strip():
        return f"{c!r}: a check object needs \"run\": the command"
    extra = sorted(set(map(str, c)) - CHECK_KEYS)
    if extra:
        return f"{c!r}: unknown key(s) {', '.join(extra)}; a check object takes only run, if_exists and if_changed"
    cond = c.get("if_exists")
    if cond is not None and not _inside(cond):
        return f"{c!r}: if_exists must be a path or glob inside the repository"
    cond = c.get("if_changed")
    if cond is not None and not (_inside(cond) or (isinstance(cond, list) and cond and all(map(_inside, cond)))):
        return f"{c!r}: if_changed must be a path or glob inside the repository, or a list of them"
    return None


def _inside(v: Any) -> bool:
    """`v` is a repo-relative path or glob: not empty, absolute, home-relative or leaving the repo."""
    p = v.strip() if isinstance(v, str) else ""
    norm = posixpath.normpath(p) if p else ""
    return bool(p) and not p.startswith(("/", "~")) and norm not in (".", "..") and not norm.startswith("../")


def _entry_problem(c: Any) -> str | None:
    """Why a check entry cannot be a check: not a command (check_problem) or a malformed object."""
    if not isinstance(c, dict):
        return check_problem(str(c).strip())
    return _form_problem(c) or check_problem(str(c["run"]).strip())


def check_problems(v: Any) -> list[str]:
    return [p for p in map(_entry_problem, _entries(v)) if p]


def checks_of(v: Any) -> list[str | dict]:
    """The config value of `v`'s checks, rejecting entries that are not commands (e.g. a sentence
    describing the checks) or malformed check objects."""
    bad = check_problems(v)
    if bad:
        raise ValueError("; ".join(bad))
    return [c.config() for c in check_list(v)]

_GLOB = re.compile(r"[*?\[]")
_REDIRECT = re.compile(r"^[0-9&]*(>>?|<)")
# Options whose value is the next word (an output file or setting, not a check target).
VALUE_OPTS = {"--junitxml", "--junit-xml", "--rootdir", "--basetemp", "--confcutdir", "-c", "-o",
              "--override-ini", "--cov", "--cov-report", "--cov-config", "--log-file", "--result-log",
              "--html", "--output", "--report", "-p", "--ignore", "--deselect"}


def path_args(cmd: str) -> list[str]:
    """Path-like arguments of a check command (globs, file paths such as pytest targets) relative to
    the repo root, without a pytest `::node` or `[param]` suffix. Options, `VAR=value` words and the
    program itself are skipped, and so are option values (`--opt value`, `--opt=value`) and redirect
    targets (`> out`, `2>out/x`). After `cd <dir>` later paths are taken relative to <dir> (the dir is
    listed too); after a `cd` that cannot be followed (absolute, `~`, `$VAR`, `-`, out of the repo)
    later paths are skipped."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    out: list[str] = []
    cwd: str | None = ""
    prev = None
    for i, w in enumerate(words):
        if prev == "cd":
            prev = w
            if cwd is None or w.startswith(("-", "/", "~")) or "$" in w:
                cwd = None
                continue
            d = posixpath.normpath(posixpath.join(cwd, w))
            if d == ".." or d.startswith("../"):
                cwd = None
                continue
            cwd = "" if d == "." else d
            if cwd:
                out.append(cwd)
            continue
        if prev in VALUE_OPTS or (prev and _REDIRECT.match(prev) and not _REDIRECT.sub("", prev)):
            prev = w
            continue
        prev = w
        if _REDIRECT.match(w):
            continue
        if i == 0 or w.startswith("-") or "=" in w or "$" in w or w in BUILTINS \
                or w in ("&&", "||", ";", "|"):
            continue
        w = w.split("::", 1)[0]
        w = re.sub(r"(\.py)\[[^\]]*\]$", r"\1", w)
        if cwd is None or not w or w.startswith(("/", "~")) or not ("/" in w or _GLOB.search(w)):
            continue
        w = posixpath.normpath(posixpath.join(cwd, w))
        if w != "." and w != ".." and not w.startswith("../"):
            out.append(w)
    return out


# tt-project runs no whitespace check of its own. A `git diff --check` in push_checks also flags
# captured output (device-run logs keep trailing whitespace); git itself already skips files that
# .gitattributes marks `-diff` or `binary`. This pathspec leaves out the logs too.
LOG_EXCLUDE = "':(exclude)*.log'"


def whitespace_check(base: str) -> str:
    """A `git diff --check` of `base`..HEAD that skips captured `*.log` output, for push_checks."""
    return f"git diff --check {shlex.quote(base)} HEAD -- . {LOG_EXCLUDE}"


def unexcluded_log_checks(checks: list[str]) -> list[str]:
    """Checks running `git diff --check` that do not leave out `*.log` files: committed run logs
    would fail them. They stay as configured; `ttp doctor` names them."""
    out = []
    for cmd in checks:
        try:
            words = shlex.split(cmd)
        except ValueError:
            continue
        after = words[words.index("git") + 1:] if "git" in words else []
        if "diff" in after and "--check" in after and not any(
                w.startswith((":!", ":^", ":(exclude")) and "*.log" in w for w in after):
            out.append(str(cmd))
    return out


def unmatched_paths(repo: Path, remote: str, branch: str, checks: list[str]) -> tuple[str, list[str]]:
    """(ref, ["cmd: path", ...]) for check path args that match no file on the push branch: the
    remote-tracking ref, which `ttp push` rebases onto, else the local branch. ref is "" when
    neither can be read."""
    for ref, full in ((f"{remote}/{branch}", f"refs/remotes/{remote}/{branch}"),
                      (branch, f"refs/heads/{branch}")):
        ls = _git(repo, "ls-tree", "-r", "--name-only", full)
        if ls.returncode == 0:
            break
    else:
        return "", []
    files = ls.stdout.splitlines()
    out = []
    for cmd in checks:
        if getattr(cmd, "if_exists", "") and not matches(files, cmd.if_exists):
            continue  # not applicable there: it is skipped, not failed
        gone: list[str] = []
        for a in path_args(cmd):
            if any(a.startswith(g + "/") for g in gone):
                continue  # under a missing `cd` dir, already reported
            if not matches(files, a):
                gone.append(a)
                out.append(f"{cmd!r}: {a!r}")
    return ref, out


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


def _existing_tip(repo: Path, remote: str, branch: str) -> str:
    """The remote branch's tip, fetched, or "" when the remote has no such branch or cannot be read."""
    ls = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if ls.returncode != 0 or not any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines()):
        return ""
    return _fetch(repo, remote, branch)


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


LOCAL_HARNESS = ("nothing to push: this is the project's harness, a local git repo with no remote. "
                 "A commit here is already delivered: the daemon reads the harness from this repo. "
                 "Hand off with the commit's hash; never copy harness files into a code branch to publish them.")


def local_harness(p: Project, repo: Path) -> bool:
    """Whether `repo` is the project's own harness repo and has no git remote: a commit there is
    delivered as it is, and there is nowhere to push it."""
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    try:
        same = bool(top) and Path(top).resolve() == p.harness.resolve()
    except OSError:
        return False
    return same and not _git(repo, "remote").stdout.split()


def own_target(p: Project, repo: Path) -> tuple[str, str]:
    """(remote, branch) for `ttp push --own`: the checked-out branch, published under the same name
    on the remote of `delivery.push_branch` (else origin). A `ttp/t<id>-...` branch must be this
    task's (inside a run), or one the task carries as its branch or continues (_carries: a fix on a
    finished task's PR); any other named branch (one a spec names, e.g. <user>/feature-x) may go
    too. Both only as a fast-forward (publish, ff_only: own_ff_only). A task labelled
    `pr_branch:<branch>` on its own ttp/t<id>-... branch (the PR's branch was held by another
    worktree) publishes its head onto that branch instead. Never a detached HEAD, main/master, the
    push branch or the branch work starts from; publish also refuses the remote's default branch."""
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not branch:
        raise ValueError("--own publishes the checked-out branch, not a detached HEAD")
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    onto = _labelled(p, task, "pr_branch") if task else ""
    if m and onto and m.group(1) == task:
        branch = onto
    elif m and task and m.group(1) != task and not _carries(p, task, m.group(1), branch):
        raise ValueError(f"--own publishes only this task's own branch (ttp/t{task}-...), not {branch}")
    d = p.config().get("delivery") or {}
    remote, shared = target(p, repo) if str(d.get("push_branch") or "").strip() else ("origin", "")
    base = str(d.get("base_ref") or "").strip()
    if branch in PROTECTED or branch in (shared, base, base.partition("/")[2]):
        raise ValueError(f"--own never pushes to a shared branch ({branch})")
    return remote, branch


def own_ff_only(branch: str) -> bool:
    """Whether `ttp push --own` to `branch` must fast-forward the remote's before its checks: every
    branch but the running task's own ttp/t<id>-... one (any of them outside a run)."""
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    return not (m and (not task or m.group(1) == task))


def _task_row(p: Project, task: str | None) -> dict | None:
    try:
        return p.db.task(int(task or ""))
    except (ValueError, TypeError):
        return None


def _labels(t: dict | None) -> list[str]:
    try:
        return [str(x) for x in json.loads((t or {}).get("labels") or "[]")]
    except (ValueError, TypeError):
        return []


def _labelled(p: Project, task: str | None, name: str) -> str:
    """The value of task `task`'s first `<name>:<value>` label, or ""."""
    return next((x.partition(":")[2] for x in _labels(_task_row(p, task)) if x.startswith(f"{name}:")), "")


def _carries(p: Project, task: str, owner: str, branch: str) -> bool:
    """Whether task `task` may publish task `owner`'s ttp/t<id>-... `branch` with --own: it carries
    it as its branch or `pr_branch:` label, or continues `owner` (a fix on a finished task's PR)."""
    t = _task_row(p, task)
    if not t:
        return False
    labels = _labels(t)
    return str(t.get("branch") or "") == branch or f"pr_branch:{branch}" in labels or f"continues:{owner}" in labels


def _may_land(p: Project, task: str, owner: str, branch: str) -> bool:
    """Whether task `task` may land `branch`, task `owner`'s ttp/t<id>-... branch: its own, one the
    task carries as its branch, the change a review checks (reviews push in the change's worktree)
    or a task it depends on or continues."""
    if owner == task:
        return True
    try:
        t = p.db.task(int(task))
    except (ValueError, TypeError):
        return False
    if not t:
        return False
    if t.get("kind") == "review" or str(t.get("branch") or "") == branch:
        return True
    try:
        refs = [str(x) for x in json.loads(t.get("depends_on") or "[]")]
        refs += [str(x).partition(":")[2] for x in json.loads(t.get("labels") or "[]")
                 if str(x).startswith(("auto_review:", "continues:"))]
    except (ValueError, TypeError):
        return False
    return owner in refs


def _on_remote(repo: Path, remote: str, branch: str) -> bool:
    ls = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    return ls.returncode == 0 and any(ln.endswith(f"\trefs/heads/{branch}") for ln in ls.stdout.splitlines())


def resolve(p: Project, repo: Path, own: bool = False) -> tuple[str, str, bool]:
    """(remote, branch, own) for `ttp push`. `--own` is own_target. Otherwise the target is
    `delivery.push_branch`, but inside a run a worktree on another task's ttp/t<id>-... branch is
    refused (it would land that task's work: _may_land), and with no push_branch set this task's
    own branch, once on the remote, is the default target (published as with --own)."""
    if own:
        return (*own_target(p, repo), True)
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    m = OWN_BRANCH.fullmatch(branch)
    task = os.environ.get("TTP_TASK")
    if m and task and not _may_land(p, task, m.group(1), branch):
        raise ValueError(f"this worktree has task #{m.group(1)}'s branch {branch} checked out, not "
                         f"task #{task}'s own (ttp/t{task}-...): ttp push lands only this task's work. "
                         f"Run it in this task's own worktree")
    d = p.config().get("delivery") or {}
    if not str(d.get("push_branch") or "").strip() and m and m.group(1) == task:
        remote, own_branch = own_target(p, repo)
        if _on_remote(repo, remote, own_branch):
            print(f"ttp push: no delivery.push_branch; {remote}/{branch} is this task's own branch, "
                  f"publishing it as with --own", file=sys.stderr)
            return remote, own_branch, True
    return (*target(p, repo), False)


def behind(repo: Path, remote: str, branch: str) -> str:
    """Why pushing HEAD to remote/branch would not be a fast-forward, or "" when the branch is new
    there or its tip is an ancestor of HEAD. Fails closed when the remote cannot be read."""
    ls = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    if ls.returncode != 0:
        return f"cannot reach {remote}: {ls.stderr.strip()}"
    tip = next((ln.split("\t")[0] for ln in ls.stdout.splitlines()
                if ln.endswith(f"\trefs/heads/{branch}")), "")
    if not tip:
        return ""
    tip = _fetch(repo, remote, branch)   # the latest tip, should it have moved since
    if not tip:
        return f"cannot fetch {remote}/{branch} to compare with HEAD"
    if _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode != 0:
        return (f"{remote}/{branch} ({tip[:10]}) is not an ancestor of HEAD: --own only fast-forwards "
                "a branch that is not this task's own ttp/t<id>-... one, never rewrites it")
    return ""


def delivered_pr(p: Project, task: dict, head: str) -> str | None:
    """The URL of the task's PR when it already carries `head` (a full hash), else None. The PR's
    head as pr-watch last read it decides, and a PR it read as no longer open does not count; before
    pr-watch read it, a `ttp push --own` of this task that pushed exactly `head` does. A review of
    such a head is review only: the work is delivered, so a pass publishes nothing more."""
    from . import prguard
    url = str(task.get("pr_url") or "").strip()
    key = prguard.pr_key(url) if url and head else None
    if not key:
        return None
    state = ((p.db.kv("pr_signatures", {}) or {}).get(url) or {}).get("state")
    if state and state != "OPEN":
        return None
    seen = ((p.db.kv(prguard.HEADS_KEY, {}) or {}).get(key) or {}).get("sha")
    if seen:
        return url if seen == head else None
    folder = p.state / DETACHED
    for marker in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        m = _read(marker)
        if (m.get("own") and m.get("status") == "pushed" and m.get("sha") == head
                and str(m.get("task") or "") == str(task.get("id"))):
            return url
    return None


REVIEW_ONLY_LABEL = "review_only"   # a review whose change must not reach the push branch (kept_off)
# A spec keeps its change off the push branch only with an explicit marker line, never by its prose:
# free text cannot tell a ban from a sentence that reports, quotes or conditions one.
_NO_PUSH_MARKER = re.compile(r"^[ \t]*no_push:[ \t]*(.*?)[ \t]*$", re.M)


def kept_off(task: dict, changes: dict | None, d: dict) -> str:
    """Why a code task's change must not reach `delivery.push_branch`, or "" when it may: every path
    of its diff since the base (`changes`, worktree.diff_lines) matches `delivery.push_exclude_paths`,
    its hand-off sets `no_push` (true or the reason), or its spec has a line starting `no_push:` (the
    reason follows; false/no/0 does not count). Prose in the spec or hand-off summary is never read
    as a ban. Its review is then review only: no `ttp push` and no push-queue approval."""
    globs = exclude_list(d.get("push_exclude_paths"))
    if changes and globs and all(any(matches([f], g) for g in globs) for f in changes):
        return "every file it changes is one delivery.push_exclude_paths keeps off the push branch"
    from .db import load_result
    result = load_result(task.get("result"))
    flag = result.get("no_push")
    if flag not in (None, False, "", 0):
        return "its hand-off says it must not reach the push branch" + (
            f" ({str(flag)[:200]})" if isinstance(flag, str) else "")
    for m in _NO_PUSH_MARKER.finditer(str(task.get("spec") or "")):
        why = m.group(1).strip("`\"' ")
        if why.lower() not in ("false", "no", "0"):
            return "its spec says it must not reach the push branch" + (f" ({why[:200]})" if why else "")
    return ""


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


REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BACKUP_TIMEOUT_S = 300


def backup_problem(d: dict) -> str:
    """Why `delivery.backup_remote` in the delivery settings `d` cannot be used, or "" when it is off
    (unset or empty) or names a remote. It names a git remote, never a branch: main/master, the push
    branch, the base_ref and anything with a slash (a remote/branch) are refused."""
    v = d.get("backup_remote")
    if v is None or v == "":
        return ""
    if not isinstance(v, str):
        return f"delivery.backup_remote: {v!r} is not a git remote's name; nothing is backed up"
    v = v.strip()
    shared = {str(d.get(k) or "").strip() for k in ("push_branch", "base_ref")} - {""}
    if v in PROTECTED or v in shared or v in {s.partition("/")[2] for s in shared}:
        return (f"delivery.backup_remote: {v!r} names a branch (main, master, the push branch or the "
                f"base_ref), not a git remote; nothing is backed up")
    if not REMOTE_NAME.fullmatch(v):
        return f"delivery.backup_remote: {v!r} is not a git remote's name; nothing is backed up"
    return ""


def backup(repo: Path, remote: str, branch: str, timeout_s: float = BACKUP_TIMEOUT_S) -> tuple[str, str]:
    """Push a finished task's own branch (ttp/t<id>-...) to the same name on `remote`, fast-forward
    only: never with force, never another branch. Returns (outcome, detail): "pushed" (head), "not_ff"
    (the remote's copy has commits the local branch lacks; left alone), "refused" (not a task branch
    or no such remote), "gone" (the branch no longer exists) or "failed" (git's error)."""
    if not OWN_BRANCH.fullmatch(branch) or branch in PROTECTED:
        return "refused", f"{branch} is not a task branch (ttp/t<id>-...)"
    if remote not in _git(repo, "remote").stdout.split():
        return "refused", f"there is no git remote named {remote}"
    head = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}").stdout.strip()
    if not head:
        return "gone", f"branch {branch} no longer exists"
    ref = f"refs/heads/{branch}"
    try:   # no leading + and no --force: git itself refuses anything but a fast-forward
        out = subprocess.run(["git", "-C", str(repo), "push", "--porcelain", remote, f"{ref}:{ref}"],
                             text=True, capture_output=True, timeout=timeout_s, stdin=subprocess.DEVNULL,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return "failed", f"git push to {remote} timed out after {timeout_s:.0f} s"
    if out.returncode == 0:
        return "pushed", head
    if any(ln.startswith("!") and "[rejected]" in ln for ln in out.stdout.splitlines()):
        return "not_ff", f"{remote}/{branch} has commits {branch} lacks"
    return "failed", (out.stderr.strip() or out.stdout.strip())[-300:]


def is_doc(path: str) -> bool:
    """A documentation file: prose by its suffix, or anything under a `docs/` or `doc/` folder."""
    return path.lower().endswith(DOC_SUFFIXES) or any(d in ("docs", "doc") for d in path.split("/")[:-1])


def code_paths(repo: Path, tip: str, head: str = "HEAD") -> list[str]:
    """The files the change at `head` touches since it left `tip` that are not docs. --no-renames
    lists a moved file under both names, so moving code into docs/ still counts as code."""
    diff = _git(repo, "diff", "--name-only", "--no-renames", f"{tip}...{head}")
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


def last_check_s(p: Project) -> float | None:
    """Seconds the project's last full, passing check run took, or None when none was measured."""
    try:
        v = json.loads((p.state / TIMINGS).read_text()).get("last_s")
    except (OSError, ValueError, AttributeError):
        return None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return float(v) if ok else None


def record_check_s(p: Project, seconds: float) -> None:
    """Runs between passing checks and the push, so a failed write is logged and never stops it."""
    try:
        write_json(p.state / TIMINGS, {"last_s": round(seconds, 1), "at": time.time()})
    except OSError as e:
        print(f"ttp push: could not record the check time in {p.state / TIMINGS}: {e}", file=sys.stderr)


def default_wait(last_s: float | None) -> float:
    """The lock wait when `delivery.push_wait_s` is unset: long enough for the holder to run its
    checks twice (it starts over when the branch moved), never under DEFAULT_WAIT_S and never over
    MAX_DEFAULT_WAIT_S."""
    if last_s is None:
        return float(DEFAULT_WAIT_S)
    return min(float(MAX_DEFAULT_WAIT_S), max(float(DEFAULT_WAIT_S), 2 * last_s + WAIT_MARGIN_S))


def bump_of(v: Any) -> dict | None:
    """`delivery.version_bump` as {"files", "changeset_dir", "package", "paths"}, or None when unset;
    ValueError when it is malformed. `files` are repo-relative files holding the version; `paths`
    (default: the folder the files share) are where a change counts as one that needs a bump."""
    if v is None or v == "" or v == {}:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            raise ValueError(f"delivery.version_bump must be an object with `files`, not {v!r}") from None
    if not isinstance(v, dict):
        raise ValueError(f"delivery.version_bump must be an object with `files`, not {v!r}")
    unknown = set(v) - {"files", "changeset_dir", "package", "paths"}
    if unknown:
        raise ValueError(f"delivery.version_bump: unknown keys {', '.join(sorted(unknown))}")

    def rel_list(key: str) -> list[str]:
        items = v.get(key) or []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or not all(isinstance(x, str) and x.strip() for x in items):
            raise ValueError(f"delivery.version_bump.{key} must be a list of repo-relative paths")
        out = [x.strip().strip("/") for x in items]
        if any(x.startswith("..") or x.startswith("~") or not x for x in out) or any(x.startswith("/") for x in items):
            raise ValueError(f"delivery.version_bump.{key} must be a list of repo-relative paths")
        return out
    files = rel_list("files")
    if not files:
        raise ValueError("delivery.version_bump.files must list the files that hold the version")
    paths = rel_list("paths") or [os.path.commonpath(files) if len(files) > 1 else os.path.dirname(files[0])]
    for key in ("changeset_dir", "package"):
        if v.get(key) is not None and not isinstance(v[key], str):
            raise ValueError(f"delivery.version_bump.{key} must be a string")
    return {"files": files, "paths": paths, "package": (v.get("package") or "").strip(),
            "changeset_dir": (v.get("changeset_dir") or "").strip().strip("/")}


def bump_problems(v: Any) -> list[str]:
    try:
        bump_of(v)
    except ValueError as e:
        return [str(e)]
    return []


def _under(path: str, dirs: list[str]) -> bool:
    return any(not d or path == d or path.startswith(d + "/") for d in dirs)


def _drop_own_bump(repo: Path) -> None:
    """Drop the bump commits an earlier round or run of ttp push left on top of HEAD; the next
    rebase lands on a tip that may have taken that version, so the bump is made again after it."""
    while re.search(rf"^{BUMP_TRAILER}: ", _git(repo, "log", "-1", "--format=%B").stdout, re.M):
        if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD~1").returncode != 0 \
                or _git(repo, "reset", "-q", "--hard", "HEAD~1").returncode != 0:
            return


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-") or "change"


def needs_bump(repo: Path, tip: str, cfg: dict, head: str = "HEAD") -> bool:
    """Whether `head` changes something under `cfg["paths"]` since `tip`, other than the version
    files and changesets themselves."""
    files, csdir = cfg["files"], cfg["changeset_dir"]
    changed = _git(repo, "diff", "--name-only", "--no-renames", tip, head).stdout.split()
    return any(_under(f, cfg["paths"]) and f not in files and not (csdir and _under(f, [csdir]))
               for f in changed)


def next_version(repo: Path, tip: str, files: list[str]) -> str | None:
    """One patch version above the highest version `files` hold at `tip`; None when none holds one
    there (the files are new: their version is the change's own)."""
    at_tip = []
    for f in files:
        show = _git(repo, "show", f"{tip}:{f}")
        m = VERSION_RE.search(show.stdout) if show.returncode == 0 else None
        if m:
            at_tip.append(tuple(int(x) for x in m.group(2, 3, 4)))
    if not at_tip:
        return None
    major, minor, patch = max(at_tip)
    return f"{major}.{minor}.{patch + 1}"


def set_version(repo: Path, files: list[str], new: str, package: str = "") -> str:
    """Write version `new` over the first version each of `files` holds in the work tree, and
    return the package name (`package`, else the first JSON file's `name`). ValueError, writing
    nothing, when a file holds no version or a JSON file's first version is not its `version`."""
    texts = {}
    for f in files:            # all checked before any is written, so a refusal leaves the tree clean
        path = repo / f
        try:
            text = path.read_text()
        except OSError:
            text = ""
        if not VERSION_RE.search(text):
            raise ValueError(f"delivery.version_bump: {f} holds no version to bump")
        text = VERSION_RE.sub(lambda m: f"{m.group(1)}{new}{m.group(5)}", text, count=1)
        if f.endswith(".json"):
            try:
                doc = json.loads(text)
                ok = doc.get("version") == new
            except (ValueError, AttributeError):
                doc, ok = {}, False
            if not ok:
                raise ValueError(f"delivery.version_bump: the first version in {f} is not its top-level `version`")
            package = package or str(doc.get("name") or "")
        texts[path] = text
    for path, text in texts.items():
        durable_write(path, text)
    return package


def bump(repo: Path, tip: str, cfg: dict, say: Callable[[str], None]) -> int:
    """After a rebase onto `tip`: when the change touches `cfg["paths"]`, set every file in
    `cfg["files"]` one patch version above the highest version they hold at tip, add a changeset
    for the change when it brings none, and commit both as one `<package>: X.Y.Z (<subject>)`
    commit. 0 when done or not needed; REFUSED when a file holds no version to bump."""
    files, csdir = cfg["files"], cfg["changeset_dir"]
    if not needs_bump(repo, tip, cfg):
        return 0
    new = next_version(repo, tip, files)
    if new is None:
        return 0
    try:
        package = set_version(repo, files, new, cfg["package"])
    except ValueError as e:
        say(f"{e}; not pushing")
        return REFUSED
    log = _git(repo, "log", "--no-merges", "--reverse", "--format=%s", f"{tip}..HEAD").stdout.splitlines()
    subjects = [re.sub(rf"^{re.escape(package)}: ", "", s) if package else s for s in log if s.strip()]
    subject = subjects[-1] if subjects else "update"
    title = f"{package}: {new} ({subject})" if package else f"{new} ({subject})"
    added, wrote = [], False
    if csdir:
        added = [f for f in _git(repo, "diff", "--name-only", "--diff-filter=A", tip, "HEAD", "--",
                                 csdir).stdout.split() if f.endswith(".md")]
        if not added and package:
            branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            name = _slug(branch.rsplit("/", 1)[-1] if branch and branch != "HEAD" else subject)
            body = (f"`{package}`: {subjects[0]}." if len(subjects) == 1
                    else f"`{package}`:\n\n" + "\n".join(f"- {s}" for s in subjects))
            cs = repo / csdir / f"{_slug(package)}-{name}.md"
            cs.parent.mkdir(parents=True, exist_ok=True)
            if cs.is_file():     # an earlier push of this branch: add what is new to its changeset
                text = cs.read_text().rstrip("\n")
                durable_write(cs, text + "".join(f"\n- {s}" for s in subjects if s not in text) + "\n")
            else:
                durable_write(cs, f'---\n"{package}": patch\n---\n\n{body}\n')
            _git(repo, "add", "--", str(cs.relative_to(repo)))
            wrote = True
    _git(repo, "add", "--", *files)
    if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return 0                 # the change already sits one version above the tip, with its changeset
    if _git(repo, "commit", "-q", "-m", f"{title}\n\n{BUMP_TRAILER}: {new}").returncode != 0:
        say("delivery.version_bump: the bump commit failed; not pushing")
        return REFUSED
    say(f"bumped {package or 'the version'} to {new}" + (" with a changeset" if wrote else ""))
    return 0


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
         say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
         who: str | None = None):
    """The held push lock of remote/branch, waiting up to `wait_s` for it; None when it stayed busy.
    Inside a run the wait is recorded, so it does not count against the run's wall clock. `who`
    labels the holder (default: the task and run, else the pid)."""
    paths = lock_paths(p, remote, branch)
    who = who or (f"task #{os.environ['TTP_TASK']} (run {os.environ.get('TTP_RUN_ID') or '?'})"
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
         hold: Callable[[], Any] | None = None, version_bump: dict | None = None,
         timed: Callable[[float], None] | None = None, exclude: list[str] | None = None) -> int:
    """Rebase HEAD onto remote/branch, run `checks` on the result, and push it if the remote did not
    move meanwhile; if it did, start over, at most `rounds` times. With no checks only a change that
    touches nothing but docs goes through, and none that adds or modifies a file `exclude` matches
    (delivery.push_exclude_paths, `excluded`). `hold` takes the push lock once the quick refusals
    passed: it returns the held lock, or None when it stayed busy (BUSY). `version_bump` (bump_of)
    bumps the version after each rebase; `timed` gets the seconds of each full, passing check run."""
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
    global last_pushed
    last_pushed = None
    keep = published(repo, remote, branch)
    if keep:
        # The rebase would rewrite a branch the remote already has, and a later `ttp push --own` of
        # it would be rejected; it runs on a detached HEAD and the branch stays as it was.
        say(f"{keep} is on {remote}; rebasing a detached copy, the branch stays as it is")
        _git(repo, "switch", "--detach", "--quiet")
    try:
        return _rounds(repo, remote, branch, checks, rounds, say, version_bump, timed, keep, exclude)
    finally:
        if keep:
            if Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip(), "rebase-merge").is_dir():
                _git(repo, "rebase", "--abort")
            if _git(repo, "switch", "--quiet", keep).returncode != 0:
                say(f"could not switch back to {keep}; HEAD is detached, the branch is unchanged")
        if lock is not None:
            lock.close()


last_pushed: str | None = None   # the commit the last push() pushed; HEAD may be back on its branch


def published(repo: Path, remote: str, target_branch: str) -> str:
    """The checked-out branch when the remote has it and its tip there is an ancestor of HEAD (it was
    published, e.g. with `ttp push --own`), else "". Not the push target itself: rebasing that onto
    its own tip rewrites nothing."""
    name = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not name or name == target_branch:
        return ""
    ls = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{name}")
    tip = ls.stdout.split()[0] if ls.returncode == 0 and ls.stdout.strip() else ""
    if tip and _git(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
        return name
    return ""


def publish(repo: Path, remote: str, branch: str, checks: list[str],
            say: Callable[[str], None] = lambda m: print(f"ttp push: {m}", file=sys.stderr),
            hold: Callable[[], Any] | None = None, timed: Callable[[float], None] | None = None,
            base: str | None = None, ff_only: bool = False, recorded: dict | None = None) -> int:
    """`ttp push --own`: run `checks` on HEAD as it is and push it to remote/branch, the task's own
    branch. No rebase and no version bump, so the pushed commit is the one reviewed; without force,
    so the remote takes only a fast-forward of what it has. Without checks, as for `ttp push`, only
    a docs-only change since `base` (the project's push target) may go. `ff_only` (a branch that is
    not this task's own `ttp/t<id>-...` one) refuses before the checks unless it fast-forwards the remote's.
    When every check is skipped on HEAD, the extra commands `recorded` (recorded_checks) passing on
    exactly HEAD run instead, and must pass again; with none, nothing checked it and it is refused,
    unless every check was skipped by its `if_changed` scope (`outside_scope`, compared with `base`)."""
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        say("uncommitted changes; commit first")
        return REFUSED
    why = refusal(repo, remote, branch) or (behind(repo, remote, branch) if ff_only else "")
    if why:
        say(why)
        return REFUSED
    if not checks and (code := code_paths(repo, base) if base else ["(no push target to compare with)"]):
        more = f" and {len(code) - 3} more" if len(code) > 3 else ""
        say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}{more}): "
            + NO_CHECKS)
        return REFUSED
    lock = hold() if hold else None
    if hold and lock is None:
        return BUSY
    try:
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        try:
            todo, skipped = applicable(repo, head, checks, say, base or "")
        except ScopeError as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        if checks and not todo:
            todo = recorded_extras(recorded, head, checks)
            if not todo and outside_scope(checks, skipped):
                say(f"{head[:10]}: {OUT_OF_SCOPE}")
            elif not todo:
                say(f"{head[:10]}: {NONE_APPLY}; not pushing: {NONE_APPLY_OWN_FIX}")
                return CHECKS_FAILED
            else:
                say(f"{head[:10]}: every project check was skipped; running the {len(todo)} command(s) "
                    f"`ttp checks` recorded passing on this commit: {'; '.join(todo)}")
        started = time.time()
        env = check_env("own", _existing_tip(repo, remote, branch) if todo else "")
        for cmd in todo:
            if subprocess.run(check_argv(cmd), cwd=repo, env=env).returncode != 0:
                say(f"check failed on {head[:10]}: {cmd}; not pushing")
                return CHECKS_FAILED
        if todo and timed:
            timed(time.time() - started)
        if _git(repo, "rev-parse", "HEAD").stdout.strip() != head:
            say(f"HEAD moved off {head[:10]} during the checks; not pushing")
            return REFUSED
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            say(f"pushed {head[:10]} to {remote}/{branch}")
            return 0
        say(f"push to {remote}/{branch} was rejected (never forced: it must fast-forward what the remote has)")
        return REJECTED
    finally:
        if lock is not None:
            lock.close()


RERERE = ("-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=true")
MAX_REPLAYED = 50   # merge stops a rebase may get past on recorded resolutions


def _learn_merges(repo: Path, merges: list[list[str]]) -> None:
    """Record each merge's conflict resolution with git rerere, as git's rerere-train does: redo the
    merge in a scratch worktree, then show rerere the merge's committed result. The record lives in
    the repository's shared rr-cache, where the rebase finds it."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ttp-push-rerere-") as tmp:
        scratch = Path(tmp) / "wt"
        if _git(repo, "worktree", "add", "-q", "--detach", "--no-checkout", str(scratch), "HEAD").returncode:
            return
        try:
            for commit, first, *others in merges:
                if _git(scratch, "checkout", "-q", "--detach", "-f", first).returncode:
                    continue
                if _git(scratch, *RERERE, "merge", "-q", "--no-commit", "--no-ff", *others).returncode:
                    _git(scratch, *RERERE, "rerere")
                    _git(scratch, "checkout", "-q", commit, "--", ".")
                    _git(scratch, *RERERE, "rerere")
                _git(scratch, "merge", "--abort")
                _git(scratch, "reset", "-q", "--hard")
        finally:
            _git(repo, "worktree", "remove", "--force", str(scratch))
            _git(repo, "worktree", "prune")


def descends(repo: Path, tip: str, head: str) -> bool:
    """Whether `head` already has `tip` in its history, so it lands on it as a fast-forward."""
    return _git(repo, "merge-base", "--is-ancestor", tip, head).returncode == 0


def _rebase(repo: Path, tip: str) -> bool:
    """Rebase HEAD onto `tip`. A branch with merge commits (a review combining several branches)
    keeps them (--rebase-merges), and the conflicts they resolved are resolved again the same way
    rather than coming back; a conflict nothing resolved still stops it. False on a conflict, with
    the rebase left for the caller to abort. HEAD already on `tip` stays as it is (a fast-forward):
    rebasing it could only rewrite its commits."""
    if descends(repo, tip, "HEAD"):
        return True
    merges = [line.split() for line in
              _git(repo, "rev-list", "--merges", "--parents", "HEAD", f"^{tip}").stdout.splitlines()]
    if not merges:
        return _git(repo, "rebase", tip, quiet=False).returncode == 0
    _learn_merges(repo, merges)
    if _git(repo, *RERERE, "rebase", "--rebase-merges", tip, quiet=False).returncode == 0:
        return True
    editor = {**os.environ, "GIT_EDITOR": "true"}   # keep each recreated merge's own message
    for _ in range(MAX_REPLAYED):
        if not Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip(), "rebase-merge").is_dir() \
                or _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.strip():
            return False   # no rebase stopped, or a conflict no recorded resolution covers
        if subprocess.run(["git", "-C", str(repo), *RERERE, "rebase", "--continue"], env=editor,
                          text=True, stdout=2).returncode == 0:
            return True
    return False


def _rounds(repo: Path, remote: str, branch: str, checks: list[str], rounds: int,
            say: Callable[[str], None], version_bump: dict | None = None,
            timed: Callable[[float], None] | None = None, keep: str = "",
            exclude: list[str] | None = None) -> int:
    global last_pushed
    upstream = f"{remote}/{branch}"
    for rnd in range(1, rounds + 1):
        tip = _fetch(repo, remote, branch)
        if not tip:
            say(f"cannot fetch {upstream}")
            return REFUSED
        if version_bump:
            _drop_own_bump(repo)
        if not checks and (code := code_paths(repo, tip)):
            # Checked before the rebase, so a refused push leaves the branch as it was.
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            say(f"no checks configured, and this change touches more than docs ({', '.join(code[:3])}{more}): "
                + NO_CHECKS)
            return REFUSED
        if bad := excluded(repo, tip, "HEAD", exclude or []):
            say(excluded_refusal(bad, upstream))
            return REFUSED
        if not _rebase(repo, tip):
            _git(repo, "rebase", "--abort")
            if keep:
                # Rebasing the branch by hand would rewrite what the remote already has.
                say(f"rebase onto {upstream} conflicts; {keep} is already on {remote}, so do not rebase it: "
                    f"merge {upstream} into it (`git fetch {remote} {branch} && git merge {upstream}`), "
                    "resolve keeping both sides' intents, commit, then rerun")
            else:
                say(f"rebase onto {upstream} conflicts; resolve it keeping both sides' intents, then rerun")
            return CONFLICT
        if version_bump and (r := bump(repo, tip, version_bump, say)):
            return r
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        if stale := stale_versions(repo, tip):
            say(f"{', '.join(stale)}: this change edits the plugin but keeps the version already on "
                f"{upstream}; bump it past that (in every manifest, plus a changeset where the "
                "repository wants one), commit, then rerun; not pushing")
            return CHECKS_FAILED
        try:
            todo, skipped = applicable(repo, head, checks, say, tip)
        except ScopeError as e:
            say(f"{e}; not pushing")
            return CHECKS_FAILED
        if checks and not todo and outside_scope(checks, skipped):
            say(f"{head[:10]}: {OUT_OF_SCOPE}")
        elif checks and not todo:
            say(f"{head[:10]}: {NONE_APPLY}; not pushing: {NONE_APPLY_FIX}")
            return CHECKS_FAILED
        started = time.time()
        env = check_env("target", tip)
        for cmd in todo:
            if subprocess.run(check_argv(cmd), cwd=repo, env=env).returncode != 0:
                say(f"check failed on {head[:10]}: {cmd}; not pushing")
                return CHECKS_FAILED
        if todo and timed:
            timed(time.time() - started)
        if _fetch(repo, remote, branch) != tip:
            say(f"{upstream} moved during the checks; round {rnd + 1}")
            continue
        # Without --force the remote refuses anything that is not a fast-forward of what we tested on.
        if _git(repo, "push", remote, f"{head}:refs/heads/{branch}", quiet=False).returncode == 0:
            last_pushed = head
            say(f"pushed {head[:10]} to {upstream}")
            return 0
        if _fetch(repo, remote, branch) == tip:
            say(f"push to {upstream} was rejected")
            return REJECTED
        say(f"{upstream} moved before the push; round {rnd + 1}")
    say(f"{upstream} kept moving for {rounds} rounds; not pushing")
    return KEPT_MOVING


def run(p: Project, repo: Path, own: bool = False, recorded: dict | None = None) -> int:
    """`ttp push` for a project: target, checks and rounds come from its `delivery` config. `own`
    publishes the task's own branch instead (own_target, publish), with the checks `recorded` (by
    default those of the run's `ttp checks`) for when every project check is skipped."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    if local_harness(p, repo):
        print(f"ttp push: {LOCAL_HARNESS}", file=sys.stderr)
        return 0
    checks = check_list(d.get("push_checks"))   # none: only a docs-only change may go (_rounds)
    try:
        remote, branch, own = resolve(p, repo, own)
        rounds = rounds_of(d.get("push_rounds"))
        explicit = d.get("push_wait_s")
        wait_s = default_wait(last_check_s(p)) if explicit in (None, "") else wait_of(explicit)
        version_bump = bump_of(d.get("version_bump"))
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    if own:   # delivery.push_exclude_paths guards the push branch only
        base = None
        # The docs-only test, if_changed scopes and the project's own checks may all compare with the
        # shared branch this work leaves from: always fetch it (cheap), so none sees a stale base.
        try:
            base = _fetch(repo, *target(p, repo)) or None
        except ValueError:
            pass
        return publish(repo, remote, branch, checks, hold=lambda: take(p, remote, branch, wait_s),
                       timed=lambda s: record_check_s(p, s), base=base,
                       ff_only=own_ff_only(branch),
                       recorded=recorded or recorded_checks(os.environ.get("TTP_RUN_DIR")))
    global last_ff
    last_ff = []
    rc = push(repo, remote, branch, checks, rounds, hold=lambda: take(p, remote, branch, wait_s),
              version_bump=version_bump, timed=lambda s: record_check_s(p, s),
              exclude=exclude_list(d.get("push_exclude_paths")))
    if rc == 0 and last_pushed and (also := fast_forward_list(d.get("fast_forward_also"))):
        last_ff = fast_forward(repo, remote, last_pushed, also, branch)
    return rc


last_ff: list[str] = []   # the fast_forward outcome of the last run(): `ff ...` / `not ff ...` lines


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


# Detached push ------------------------------------------------------------------------------------
# `ttp push --detach` runs the same push in a process of its own and returns at once, so a worker
# whose tool calls are capped shorter than the checks can hand off `waiting` and read the outcome
# later. The process writes its outcome into a marker under the project's state. It holds a run lock
# (an OS file lock, inherited from the launcher so it is held from the first instant) for its whole
# life: a marker whose run lock is free and that holds no outcome belongs to a push that died (a
# crash, a kill, a reboot), and the probe or the daemon then records it as failed. Its push lock goes
# the same way.
#
# A sandbox that runs each command in a PID namespace of its own (Codex's on Linux) kills every
# process it holds the moment the command ends, a new session or not: the push would die before
# it logged a line. The daemon gives its runs its own namespace in TTP_PIDNS; a launcher that finds
# itself in another one queues the push and the daemon starts it outside the sandbox.
DETACHED = "pushes"        # under the project's state: <id>.json (marker) and <id>.log per push
LOG_TAIL = 20
KEEP_S = 30 * 86400        # finished markers and logs older than this go when the next push starts
QUEUED_S = 900             # a queued push the daemon has not started by then is recorded as failed
# The environment a queued push takes from the run that queued it; the rest is the daemon's.
QUEUE_ENV = ("PATH", "VIRTUAL_ENV", "TTP_TASK", "TTP_RUN_ID", "TTP_PYTHON")


def _run_lock(marker: Path) -> Path:
    """The run lock of a detached push. Its name starts with `push:`, so an upgrade waits for it
    (release.push_in_flight) and `ttp push --free` counts it, also while it waits for its turn."""
    return marker.parent.parent / "locks" / f"push:run-{marker.stem}.0.lock"


def _own_ttp(p: Project | None = None) -> str:
    """The `ttp` of this runtime, so the probe runs the code that wrote the marker."""
    ttp = Path(__file__).resolve().parents[2] / "bin" / "ttp"
    if ttp.is_file():
        return shlex.quote(str(ttp))
    return free_probe(p).rsplit(" push", 1)[0] if p else "ttp"


def result_probe(marker: Path, p: Project | None = None) -> str:
    """A `retry_when` that exits 0 once the detached push of `marker` finished (or died), 1 before."""
    return f"{_own_ttp(p)} push --result {shlex.quote(str(marker))}"


def _read(marker: Path) -> dict:
    try:
        m = json.loads(marker.read_text())
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _forget_lock(marker: Path) -> None:
    """Remove a finished push's run lock file; the probe reads a missing one as free."""
    try:
        _run_lock(marker).unlink()
    except OSError:
        pass


def _prune(folder: Path, now: float) -> None:
    for marker in folder.glob("*.json"):
        try:
            old = now - marker.stat().st_mtime > KEEP_S
        except OSError:
            continue
        if old and _read(marker).get("status") not in ("running", "queued"):
            for f in (marker, marker.with_suffix(".log"), _run_lock(marker)):
                try:
                    f.unlink()
                except OSError:
                    pass


def _tail(path: str | None, n: int = LOG_TAIL) -> str:
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:]) if path else ""
    except OSError:
        return ""


def pid_ns() -> str | None:
    """This process's PID namespace ("pid:[4026531836]"); None where there is no /proc."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _boxed() -> bool:
    """Whether this process runs in a PID namespace other than its run's: one that ends with the
    command, and takes every process started in it along."""
    run_ns = os.environ.get("TTP_PIDNS")
    return bool(run_ns) and pid_ns() not in (None, run_ns)


def _log_line(log: Path, text: str) -> None:
    """Append a line to a push's log and sync it, so the log of a push that dies is never empty."""
    with open(log, "ab") as out:
        out.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n".encode())
        out.flush()
        os.fsync(out.fileno())


def _start(p: Project, marker: Path, m: dict, lock, env: dict, nice: int = 0) -> int:
    """Start the push process of `marker`, holding `lock` (its run lock, taken by the caller), `nice`
    levels below this process, write the marker as running, then give the go. Returns the pid."""
    log = Path(m["log"])
    env = {k: v for k, v in env.items() if k not in ("TTP_RUN_DIR", "TTP_PIDNS")}   # the run ends first
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[1]), TTP_PROJECT=str(p.base))
    _log_line(log, f"starting the push process for marker {marker}")
    with open(log, "ab") as out:
        child = subprocess.Popen([sys.executable, "-m", "ttp", "push", "--marker", str(marker),
                                  *(["--own"] if m.get("own") else [])],
                                 cwd=m["repo"], env=env, stdin=subprocess.PIPE, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True,
                                 pass_fds=(lock.fileno(),))
    renice(child.pid, nice)  # before the go
    from .runner import boot_id
    m.update(status="running", pid=child.pid, started=time.time(), boot=boot_id())
    write_json(marker, m)
    child.stdin.close()      # the go: the push starts once its marker is written
    return child.pid


def detach(p: Project, repo: Path, own: bool = False) -> int:
    """Start `ttp push` for `repo` in a process of its own and print its marker and probe. The quick
    refusals (pushing not allowed, no target, uncommitted changes, no checks for a code change) answer at once, without a marker.
    From inside a sandbox that would kill that process with the command, the daemon starts it."""
    d = p.config().get("delivery") or {}
    allowed = True if d.get("push_allowed") is None else d.get("push_allowed")
    if not (allowed is True or str(allowed).strip().lower() in ("1", "true", "yes", "on")):
        print("ttp push: this project does not allow pushing (delivery.push_allowed)", file=sys.stderr)
        return REFUSED
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    if local_harness(p, top):
        print(f"ttp push: {LOCAL_HARNESS}", file=sys.stderr)
        return 0
    try:
        remote, branch, own = resolve(p, top, own)
    except ValueError as e:
        print(f"ttp push: {e}", file=sys.stderr)
        return REFUSED
    if _git(top, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("ttp push: uncommitted changes; commit first", file=sys.stderr)
        return REFUSED
    if not check_list(d.get("push_checks")):   # refuse now, not after the task handed off waiting
        try:
            base = _fetch(top, *target(p, top)) or None
        except ValueError:
            base = None
        if code := code_paths(top, base) if base else ["(no push target to compare with)"]:
            more = f" and {len(code) - 3} more" if len(code) > 3 else ""
            print(f"ttp push: no checks configured, and this change touches more than docs "
                  f"({', '.join(code[:3])}{more}): " + NO_CHECKS, file=sys.stderr)
            return REFUSED
    task = os.environ.get("TTP_TASK")
    rid = (f"t{task}-" if task else "") + time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    folder = p.state / DETACHED
    folder.mkdir(parents=True, exist_ok=True)
    base, n = rid, 1
    while (folder / f"{rid}.json").exists():   # two pushes from one process within a second
        n += 1
        rid = f"{base}-{n}"
    marker = folder / f"{rid}.json"
    log = marker.with_suffix(".log")
    _prune(folder, time.time())
    m = {"id": rid, "repo": str(top), "head": _git(top, "rev-parse", "HEAD").stdout.strip(),
         "target": f"{remote}/{branch}", "log": str(log), "lock": str(_run_lock(marker)),
         "task": task, "run": os.environ.get("TTP_RUN_ID"), "own": own}
    if own and (rec := recorded_checks(os.environ.get("TTP_RUN_DIR"))):
        m["recorded_checks"] = rec   # the push process runs without TTP_RUN_DIR
    if _boxed():
        m.update(status="queued", queued=time.time(),
                 env={k: os.environ[k] for k in os.environ if k in QUEUE_ENV or k.startswith("GIT_CONFIG_")})
        _log_line(log, "queued for the daemon: this command's sandbox would end a push started here")
        write_json(marker, m)
        print(f"ttp push: queued; the daemon starts it outside this command's sandbox, pushing {top} "
              f"to {remote}/{branch}")
    else:
        lock = locks.try_take([_run_lock(marker)], f"detached push {rid}", "ttp push --detach")
        if lock is None:
            print(f"ttp push: the run lock of {rid} is taken", file=sys.stderr)
            return REFUSED
        try:
            pid = _start(p, marker, m, lock, dict(os.environ))
        finally:
            lock.close()             # the child holds the run lock from here on
        print(f"ttp push: started in the background (pid {pid}), pushing {top} to {remote}/{branch}")
    print(f"marker: {marker}")
    print(f"log: {log}")
    print(f"retry_when: {result_probe(marker, p)}")
    return 0


class _Stopped(Exception):
    pass


def _stop(sig, _frame) -> None:
    raise _Stopped(f"stopped by {signal.Signals(sig).name}")


def run_detached(p: Project, repo: Path, marker: Path, own: bool = False) -> int:
    """The detached process: wait for the launcher's go, push, and write the outcome into `marker`."""
    print(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} detached push process {os.getpid()} up, "
          f"waiting for the go", flush=True)
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass
    print(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} detached push, marker {marker}", flush=True)
    m = _read(marker)
    if m.get("status") == "running":
        m["alive"] = time.time()   # it began: a death from here on is not one at startup
        write_json(marker, m)
    reason = None
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        rec = m.get("recorded_checks")
        rc = run(p, repo, own, rec if isinstance(rec, dict) else None)
    except Exception as e:     # recorded as a failure, never left as "running"
        print(f"ttp push: {type(e).__name__}: {e}", file=sys.stderr)
        reason = str(e) if isinstance(e, _Stopped) else None
        rc = 1
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)   # the outcome gets written
    top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip() or repo)
    sha = version = None
    if rc == 0:
        sha = (None if own else last_pushed) or _git(top, "rev-parse", "HEAD").stdout.strip() or None
    if rc == 0 and not own:
        try:
            cfg = bump_of((p.config().get("delivery") or {}).get("version_bump"))
        except ValueError:
            cfg = None
        if cfg:
            show = _git(top, "show", f"{sha or 'HEAD'}:{cfg['files'][0]}")
            m = VERSION_RE.search(show.stdout) if show.returncode == 0 else None
            version = ".".join(m.group(2, 3, 4)) if m else None
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.fsync(sys.stdout.fileno())
    except (OSError, ValueError):
        pass
    m = _read(marker) or {"id": marker.stem, "pid": os.getpid(), "log": str(marker.with_suffix(".log"))}
    m.update(status="pushed" if rc == 0 else "failed", exit=rc, sha=sha, version=version, ended=time.time())
    if rc == 0 and not own and last_ff:
        m["fast_forward"] = last_ff
    if reason:
        m["reason"] = reason
    write_json(marker, m)
    _forget_lock(marker)       # the outcome is written: a missing lock file reads as finished
    return rc


def _dead_reason(m: dict) -> str:
    from .runner import boot_id
    if m.get("boot") and m["boot"] != boot_id():
        return "the push process ended without writing an outcome: the host rebooted while it ran"
    if m.get("boot") and not m.get("alive"):   # written by a version that marks the start
        return ("the push process ended without writing an outcome before it began: it was probably "
                "killed with the command that started it (a sandbox or tool that ends background processes)")
    return "the push process ended without writing an outcome (killed, crashed or rebooted)"


def _settle(marker: Path) -> dict:
    """The marker of a push that cannot run any more recorded as failed: one that runs with its run
    lock free died; one queued longer than QUEUED_S was never started. Others come back as read, and
    so does a push queue batch's (batch.py): it lets go of its run lock before its after_push and
    writes its marker after, so only the push queue judges it (pushq.finalize), never this."""
    m = _read(marker)
    if m.get("kind") == "batch":
        return m
    if m.get("status") == "running" and locks.any_free([_run_lock(marker)]):
        m = _read(marker)      # read after the lock: the push writes its outcome before letting go
        if m.get("status") == "running":
            m.update(status="failed", exit=None, ended=time.time(), reason=_dead_reason(m))
            write_json(marker, m)
            _forget_lock(marker)
    elif m.get("status") == "queued" and time.time() - float(m.get("queued") or 0) > QUEUED_S:
        lock = locks.try_take([_run_lock(marker)], f"detached push {m.get('id')}", "expire")
        if lock is not None:   # taken: the daemon is starting it right now
            with lock:
                m = _read(marker)
                if m.get("status") == "queued":
                    m.update(status="failed", exit=None, ended=time.time(),
                             reason=f"the daemon did not start the queued push within {QUEUED_S // 60} min")
                    write_json(marker, m)
            _forget_lock(marker)
    return m


def tend(p: Project) -> None:
    """The daemon's part, each tick: start the queued pushes and record the dead ones as failed."""
    folder = p.state / DETACHED
    for marker in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            _tend_one(p, marker)
        except Exception as e:   # a malformed marker: recorded as failed, never aborts the daemon's tick
            _malformed(marker, e)


def _tend_one(p: Project, marker: Path) -> None:
    m = _settle(marker)
    if m.get("status") != "queued" or m.get("kind") == "batch":
        return
    lock = locks.try_take([_run_lock(marker)], f"detached push {m.get('id')}", "ttp push --detach (daemon)")
    if lock is None:
        return
    try:
        m = _read(marker)
        if m.get("status") != "queued":
            return
        env = {**os.environ, **(m.get("env") or {})}
        env.update(git_fsync_env(env))
        try:
            # A worker's push runs niced like the worker it came from (one started there inherits it).
            _start(p, marker, m, lock, env, nice_level(p.config().get("runner"))[0])
        except OSError as e:   # its worktree went, say: recorded, never retried each tick
            m.update(status="failed", exit=None, ended=time.time(),
                     reason=f"the daemon could not start the push: {e}")
            write_json(marker, m)
    finally:
        lock.close()


def _malformed(marker: Path, e: Exception) -> None:
    """Record a queued push the daemon could not handle as failed, so it is skipped from now on."""
    m = _read(marker)
    if m.get("status") != "queued":
        return
    m.update(status="failed", exit=None, ended=time.time(),
             reason=f"the daemon could not start the push: malformed marker ({type(e).__name__}: {e})")
    try:
        write_json(marker, m)
    except OSError:
        pass
    _forget_lock(marker)


def result(marker: Path) -> int:
    """`ttp push --result <marker>`, the probe: 0 once the push finished (pushed, failed or died),
    1 while it runs or waits for the daemon, 2 when there is no such marker. A push whose run lock is
    free but that wrote no outcome died; it is recorded as failed here, so nobody waits for it."""
    marker = Path(marker)
    if not _read(marker):
        print(f"ttp push: no detached push marker at {marker}", file=sys.stderr)
        return REFUSED
    if _read(marker).get("kind") == "batch":     # the push queue's batch process (batch.py)
        from . import batch
        return batch.summary(marker)
    try:
        m = _settle(marker)
        since = {"queued": "queued", "running": "started"}.get(m.get("status"))
        took = time.time() - float(m.get(since) or time.time()) if since else 0
    except (TypeError, ValueError) as e:   # a malformed marker: a clear verdict, not a traceback
        _malformed(marker, e)   # best effort: the verdict does not depend on the rewrite
        m = {**_read(marker), "status": "failed", "exit": None,
             "reason": f"malformed marker ({type(e).__name__}: {e})"}
        took = 0
    if m.get("status") == "running":
        print(f"ttp push: still running (pid {m.get('pid')}, {took:.0f} s); log: {m.get('log')}")
        return 1
    if m.get("status") == "queued":
        print(f"ttp push: queued for the daemon ({took:.0f} s); log: {m.get('log')}")
        return 1
    if m.get("status") == "pushed":
        v = f", version {m['version']}" if m.get("version") else ""
        print(f"ttp push: pushed {m.get('sha')} to {m.get('target')}{v}")
        for line in m.get("fast_forward") or []:
            print(f"ttp push: {'warning: ' if line in ff_warnings([line]) else ''}{line}")
    else:
        why = m.get("reason") or f"exit {m.get('exit')}"
        print(f"ttp push: not pushed ({why}); log: {m.get('log')}")
        tail = _tail(m.get("log"))
        if tail:
            print(tail)
    return 0


def running(p: Project) -> list[dict]:
    """Markers of this project's detached pushes that are still running or queued."""
    out = []
    for marker in sorted((p.state / DETACHED).glob("*.json")):
        m = _read(marker)
        if m.get("status") == "queued" or m.get("status") == "running" and not locks.any_free([_run_lock(marker)]):
            out.append({**m, "marker": str(marker)})
    return out

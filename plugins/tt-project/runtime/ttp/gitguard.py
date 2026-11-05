# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A code task's run never pushes to the push branch while delivery.code_tasks_may_push is off.

The harness's `bin/git` comes first on every run's PATH. Inside a run it hands any call naming
`push` to main() here; every other call goes straight to the real git. `ttp push` refuses the same
landing (push.code_run_landing); this covers a raw `git push` a worker runs because its spec says
"land it" (seen live: a light worker pushed its commit to the push branch before any review).

For a guarded run (TTP_TASK is a code task of TTP_PROJECT, code_tasks_may_push off, a push branch
set) the push runs with a pre-push hook of the harness's: git itself names every ref the push would
update, whatever the refspec, and the hook refuses one that is the push branch (on any remote, also
a delete). The repository's own pre-push hook still runs after it. `--no-verify` cannot skip it.
Anything else, or any fault reading the project, runs the real git unchanged.

With delivery.never_force on, a run's push that forces in any form (--force, -f, --force-with-lease,
--force-if-includes, a `+refspec`) is refused before git runs (forced, never_force_refusal), except a
forward move of a delivery.channel_tags tag under a fresh lease (channeltag.py, `ttp push --move-tag`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# git's global options that take the next word as their value
VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
# `git push` options that take the next word as their value (or `--opt=value`)
PUSH_VALUE_OPTS = {"--push-option", "--receive-pack", "--exec", "--repo"}
FORCE_OPTS = ("--force", "--force-with-lease", "--force-if-includes")
HOOK = """#!/bin/sh
# tt-project: refuses a push to the push branch from a code task's run (ttp/gitguard.py).
nl='
'
input=$(cat)
if printf '%s' "$input${input:+$nl}" | awk -v r="$TTP_PUSH_GUARD_REF" '$3 == r { f = 1 } END { exit !f }'; then
    echo "tt-project: $TTP_PUSH_GUARD_WHY" >&2
    exit 1
fi
orig=${TTP_PUSH_GUARD_HOOKS:+$TTP_PUSH_GUARD_HOOKS/pre-push}
if [ -n "$orig" ] && [ -x "$orig" ]; then
    printf '%s' "$input${input:+$nl}" | "$orig" "$@"
    exit $?
fi
exit 0
"""


def split(args: list[str]) -> tuple[list[str], str, list[str]]:
    """(global options, subcommand, its arguments) of a git command line; subcommand "" if none."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in VALUE_OPTS:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return args[:i], a, args[i + 1:]
    return args, "", []


def forced(rest: list[str]) -> str:
    """The first word of `git push <rest>` that forces the push (a force option, `-f` alone or bundled,
    or a `+refspec`), else ""."""
    i, positional = 0, False
    while i < len(rest):
        a = rest[i]
        if positional or not a.startswith("-") or a == "-":
            if a.startswith("+"):
                return a
        elif a == "--":
            positional = True
        elif a.startswith("--"):
            if a.split("=", 1)[0] in FORCE_OPTS:
                return a
            if a in PUSH_VALUE_OPTS:
                i += 1
        else:
            for j, c in enumerate(a[1:], 1):
                if c == "f":
                    return a
                if c == "o":   # -o<value> or -o <value>
                    i += j == len(a) - 1
                    break
        i += 1
    return ""


def never_force_refusal(args: list[str], env: dict, git: str = "git") -> str:
    """Why `git <args>` must not run in this run (a forced push while delivery.never_force is on), or "".
    The one forced push let through is a forward move of a delivery.channel_tags tag under a fresh lease
    (channeltag.refusal; `git` runs its checks), logged when allowed."""
    globals_, sub, rest = split(args)
    if sub != "push" or not (word := forced(rest)):
        return ""
    task, base = env.get("TTP_TASK", ""), env.get("TTP_PROJECT", "")
    if not task.isdigit() or not base:
        return ""
    from . import channeltag
    from .project import Project, never_force
    p = Project(base)
    if not (p.state / "project.db").is_file() or not never_force(cfg := p.config()):
        return ""
    if channeltag.plain_globals(globals_) and (move := channeltag.parse(rest)) is not None:
        try:   # a fault here refuses: main() runs git when this raises
            why, facts = channeltag.refusal([git, *globals_], move, cfg)
            if not why:
                channeltag.log(p, facts, "allowed")
                return ""
        except Exception as e:
            why = f"its checks failed ({type(e).__name__}: {e})"
        return f"delivery.never_force is true: not moving tag {move['tag']}: {why}"
    hint = (" To move a delivery.channel_tags tag forward, run `ttp push --move-tag <tag> <commit>`."
            if channeltag.channel_tags(cfg) else "")
    return (f"delivery.never_force is true: this run never force-pushes ({word}); push a fast-forward, "
            f"or deliver a rewritten history on a new branch.{hint}")


def guarded_branch(env: dict) -> tuple[str, str]:
    """(push branch, why) when this run must not push to the push branch, else ("", "")."""
    task, base = env.get("TTP_TASK", ""), env.get("TTP_PROJECT", "")
    if not task.isdigit() or not base:
        return "", ""
    from .project import Project, code_tasks_may_push
    p = Project(base)
    if not (p.state / "project.db").is_file():
        return "", ""
    cfg = p.config()
    branch = str((cfg.get("delivery") or {}).get("push_branch") or "").strip()
    if not branch or code_tasks_may_push(cfg):
        return "", ""
    t = p.db.task(int(task))
    if not t or t.get("kind") != "code":
        return "", ""
    why = (f"task #{task} is a code task and delivery.code_tasks_may_push is off: code tasks never push to "
           f"the push branch {branch}, whatever the spec says. Commit, run `ttp checks` and hand off; the "
           f"review lands the change")
    return branch, why


def _branch_name(real: str, globals_: list[str], ref: str) -> str:
    """The branch a delivery.push_branch value names: `<remote>/<branch>` when its first part is a
    remote of the repository (as push._split_target reads it), else the whole value."""
    out = subprocess.run([real, *globals_, "remote"], capture_output=True, text=True, timeout=30)
    remote, _, rest = ref.partition("/")
    name = rest if rest and remote in out.stdout.split() else ref
    return name.removeprefix("refs/heads/")


def _hooks_dir(real: str, globals_: list[str]) -> str:
    """The repository's own hooks directory (core.hooksPath or .git/hooks), absolute; "" if unknown."""
    out = subprocess.run([real, *globals_, "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
                         capture_output=True, text=True, timeout=30)
    path = out.stdout.strip()
    return path if out.returncode == 0 and os.path.isabs(path) else ""


def _hook_file() -> Path:
    """This guard's hooks directory, holding only pre-push: in the run's folder, else a temp one."""
    import tempfile
    run = os.environ.get("TTP_RUN_DIR", "")
    d = Path(run) / "githooks" if run and os.path.isdir(run) else Path(tempfile.mkdtemp(prefix="ttp-githooks-"))
    d.mkdir(exist_ok=True)
    f = d / "pre-push"
    if not f.is_file() or f.read_text() != HOOK or not os.access(f, os.X_OK):
        from .project import durable_write
        durable_write(f, HOOK, mode=0o755)
    return d


def command(real: str, args: list[str], env: dict) -> tuple[list[str], dict]:
    """The git command line and environment to run for `git <args>` in a run."""
    globals_, sub, rest = split(args)
    if sub != "push":
        return [real, *args], env
    try:
        branch, why = guarded_branch(env)
        if not branch:
            return [real, *args], env
        branch = _branch_name(real, globals_, branch)
        hooks = "" if "--no-verify" in rest else _hooks_dir(real, globals_)
        guard = _hook_file()
    except Exception:   # a fault reading the project never stops git; push.code_run_landing still holds
        return [real, *args], env
    rest = [a for a in rest if a not in ("--no-verify", "--verify")]
    env = {**env, "TTP_PUSH_GUARD_REF": f"refs/heads/{branch}", "TTP_PUSH_GUARD_WHY": why,
           "TTP_PUSH_GUARD_HOOKS": hooks}
    # after the caller's own options: the last core.hooksPath wins, so theirs never replaces the guard
    return [real, *globals_, "-c", f"core.hooksPath={guard}", "push", "--verify", *rest], env


def main(argv: list[str]) -> int:
    """argv: the real git, then the arguments `git` was called with."""
    real, args = argv[0], argv[1:]
    try:   # a coordinator run pushes nothing, however the command was built (coordtools.py)
        from . import coordtools
        if coordtools.coordinator_run() and split(args)[1] == "push":
            print(f"tt-project: {coordtools.PUSH_REFUSAL}", file=sys.stderr)
            return 1
    except Exception:
        print("tt-project: the coordinator push check failed: no push", file=sys.stderr)
        return 1
    try:
        why = never_force_refusal(args, dict(os.environ), real)
    except Exception:   # a fault reading the project never stops git
        why = ""
    if why:
        print(f"tt-project: {why}", file=sys.stderr)
        return 1
    cmd, env = command(real, args, dict(os.environ))
    sys.stdout.flush()
    os.execve(real, cmd, env)
    return 127

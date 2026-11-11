# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A code task's run never pushes to the push branch while delivery.code_tasks_may_push is off.

With it "approved" (the default) a code task's or the coordinator's `ttp push` of a head an
independent review approved goes through (approved_push: the `ttp push` process above this git
wrote an approval marker for exactly the commit pushed, and the push queue holds it approved); any
other push from them, a raw `git push` included, is refused as below.

The harness's `bin/git` comes first on every run's PATH. Inside a run it hands any call naming
`push` to main() here; every other call goes straight to the real git. `ttp push` refuses the same
landing (push.code_run_landing); this covers a raw `git push` a worker runs because its spec says
"land it" (seen live: a light worker pushed its commit to the push branch before any review).

For a guarded run (TTP_TASK is a code task of TTP_PROJECT, code_tasks_may_push off, a push branch
set) the push runs with a pre-push hook of the harness's: git itself names every ref the push would
update, whatever the refspec, and the hook refuses one that is the push branch (on any remote, also
a delete). The repository's own pre-push hook still runs after it. `--no-verify` cannot skip it.
Every run's push (TTP_TASK set) also runs outscan.pre_push from that hook, before the repository's
own: a push that would send a stored secret, a credential shape or a forbidden-data pattern, in a
commit message, an added line or a tag message, is refused (outscan.py). A fault setting the scan up
refuses the push; a fault reading the push branch guard's project skips only that guard.
In a run whose task reads untrusted input (untrusted.active) the same hook refuses every ref but the
task's own branch and the push branch (untrusted.push_refs, passed as TTP_PUSH_ONLY_REFS); a fault
reading them there refuses the push. One more push passes that hook there: one to a local repository
under the run's own temp folders (temp_remote), such as a bare repo a test made for itself; never a
network remote, a path elsewhere or the project's own repository.

The wrapper also hands over a call whose subcommand is no common builtin (bin/git lists them): an
alias is expanded the way git does (resolve), so `git -c alias.p=push p` is guarded as `git push`.
In a run, `git send-pack` and `git http-push` (which run no pre-push hook) and a shell alias naming
either or push (its git is git's own, past the wrapper) are refused (bypass_refusal).

With delivery.never_force on, a run's push that forces in any form (--force, -f, --force-with-lease,
--force-if-includes, a `+refspec`) is refused before git runs (forced, never_force_refusal), except a
forward move of a delivery.channel_tags tag under a fresh lease (channeltag.py, `ttp push --move-tag`).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# git's global options that take the next word as their value
VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
# `git push` options that take the next word as their value (or `--opt=value`)
PUSH_VALUE_OPTS = {"--push-option", "--receive-pack", "--exec", "--repo"}
FORCE_OPTS = ("--force", "--force-with-lease", "--force-if-includes")
# git commands that send objects without `git push`'s pre-push hook: a run never runs them
HOOKLESS_PUSH = ("send-pack", "http-push")
ALIAS_DEPTH = 10
HOOK = """#!/bin/sh
# tt-project: a run's pre-push hook (ttp/gitguard.py). Refuses a push to the push branch from a code
# task's run, a push of any other ref from a run reading untrusted input (ttp/untrusted.py) and a push
# that sends a leak (ttp/outscan.py), then runs the repository's own pre-push.
nl='
'
input=$(cat)
if [ -n "$TTP_PUSH_GUARD_REF" ] &&
    printf '%s' "$input${input:+$nl}" | awk -v r="$TTP_PUSH_GUARD_REF" '$3 == r { f = 1 } END { exit !f }'; then
    echo "tt-project: $TTP_PUSH_GUARD_WHY" >&2
    exit 1
fi
if [ -n "$TTP_PUSH_ONLY_REFS" ] &&
    printf '%s' "$input${input:+$nl}" | awk -v r=" $TTP_PUSH_ONLY_REFS " 'NF >= 3 && index(r, " " $3 " ") == 0 { f = 1 } END { exit !f }' &&
    ! { [ -n "$TTP_PUSH_ONLY_RT" ] && "$TTP_PUSH_SCAN_PY" -I -c \\
        'import sys; sys.path.insert(0, sys.argv[1]); from ttp.gitguard import temp_remote; sys.exit(not temp_remote(sys.argv[2]))' \\
        "$TTP_PUSH_ONLY_RT" "$2" 2>/dev/null; }; then
    echo "tt-project: $TTP_PUSH_ONLY_WHY" >&2
    exit 1
fi
if [ -n "$TTP_PUSH_SCAN_RT" ]; then
    printf '%s' "$input${input:+$nl}" | "$TTP_PUSH_SCAN_PY" -I -c \\
        'import sys; sys.path.insert(0, sys.argv[1]); from ttp.outscan import pre_push; sys.exit(pre_push(sys.argv[2:]))' \\
        "$TTP_PUSH_SCAN_RT" "$@" || exit 1
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


def _git_commands(real: str, globals_: list[str]) -> set[str]:
    """Every command git runs before it looks for an alias: builtins, git-* in its exec path and on PATH."""
    out = subprocess.run([real, *globals_, "--list-cmds=builtins,main,others"], capture_output=True, text=True,
                         timeout=30, stdin=subprocess.DEVNULL)
    return set(out.stdout.split()) if out.returncode == 0 else set()


def resolve(real: str, args: list[str]) -> tuple[list[str], str]:
    """`git <args>` with its alias expanded as git expands it (also chained, also one given with -c or
    starting with global options), and the text of a shell alias (`!...`) it ends on, else ""."""
    commands, seen = None, set()
    for _ in range(ALIAS_DEPTH):
        globals_, sub, rest = split(args)
        if not sub or sub in seen or sub == "push" or sub in HOOKLESS_PUSH:   # builtins: no alias
            break
        if commands is None:
            commands = _git_commands(real, globals_)
        if sub in commands:
            break
        seen.add(sub)
        out = subprocess.run([real, *globals_, "config", "--get", f"alias.{sub}"], capture_output=True,
                             text=True, timeout=30, stdin=subprocess.DEVNULL)
        if out.returncode != 0:
            break
        expansion = out.stdout.rstrip("\n")
        if expansion.startswith("!"):
            return args, expansion
        try:
            args = [*globals_, *shlex.split(expansion), *rest]
        except ValueError:   # git refuses the alias too
            break
    return args, ""


def bypass_refusal(args: list[str], shell: str) -> str:
    """Why `git <args>` (resolved; `shell` the shell alias it ends on) must not run in a run, else ""."""
    _, sub, rest = split(args)
    if sub in HOOKLESS_PUSH:
        return (f"git {sub} sends objects with no pre-push hook, so with neither the run's leak scan nor its "
                "push guard: push with `git push`")
    if shell and re.search(r"\b(?:push|send-pack|http-push)\b", " ".join([shell, *rest])):
        return (f"git {sub} is a shell alias that pushes: its git is git's own, past the run's leak scan and "
                "push guard; run the `git push` itself")
    return ""


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


def _ancestors(limit: int = 32) -> list[int]:
    """This process's parent, grandparent and so on (Linux /proc, else ps)."""
    out, pid = [], os.getpid()
    for _ in range(limit):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
            pid = int(r.stdout.strip() or 0)
        if pid <= 1:
            break
        out.append(pid)
    return out


def approved_push(env: dict, real: str = "git", args: list[str] | None = None) -> bool:
    """Whether this `git push <args>` is the push a `ttp push` makes of a head an independent review
    approved: an ancestor process of this one wrote a fresh approval marker (push.approved_landing),
    the push queue still has that head approved, and every refspec sends exactly that commit to the
    push branch. No environment variable counts, so a raw `git push` is never one."""
    from . import push
    from .project import Project, approved_heads_may_push
    base = env.get("TTP_PROJECT", "")
    globals_, sub, rest = split(args or [])
    if not base or sub != "push":
        return False
    p = Project(base)
    if not (p.state / "project.db").is_file() or not approved_heads_may_push(p.config()):
        return False
    mark = next((m for pid in _ancestors() if (m := push.approval_marker(p, pid))), None)
    if not mark or not mark.get("branch") or push.approved_head(p, mark["sha"]) is None:
        return False
    branch = _branch_name(real, globals_, mark["branch"])
    words = [a for a in rest if a not in ("--verify", "--porcelain", "--quiet", "-q", "--progress", "--no-progress")]
    if any(a.startswith("-") for a in words):   # --all, --mirror, --tags, --delete, --force, -o ...
        return False
    specs = words[1:]
    if not specs:
        return False
    for spec in specs:
        src, colon, dst = spec.partition(":")
        if src.startswith("+") or not src or not colon:
            return False
        r = subprocess.run([real, *globals_, "rev-parse", "--verify", "--quiet", f"{src}^{{commit}}"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or r.stdout.strip().lower() != str(mark["sha"]).lower():
            return False
        if dst.removeprefix("refs/heads/") != branch:
            return False
    return True


def guarded_branch(env: dict, approved: bool = False) -> tuple[str, str]:
    """(push branch, why) when this run must not push to the push branch, else ("", ""). `approved`:
    the push is one approved_push passed."""
    task, base = env.get("TTP_TASK", ""), env.get("TTP_PROJECT", "")
    if not task.isdigit() or not base:
        return "", ""
    from .project import Project, code_tasks_may_push
    p = Project(base)
    if not (p.state / "project.db").is_file():
        return "", ""
    cfg = p.config()
    branch = str((cfg.get("delivery") or {}).get("push_branch") or "").strip()
    if not branch or code_tasks_may_push(cfg) or approved:
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


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _common_dir(git: str, git_dir: str = "", cwd: str = "") -> str:
    """The realpath of a repository's common git dir: of `git_dir` (as git opens a push's local path:
    no search upward), else of the repository at `cwd` (the hook's GIT_DIR left out), else of the
    pushing repository; "" if none."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} if git_dir or cwd else None
    args = [git, *(["--git-dir", git_dir] if git_dir else []), "rev-parse", "--path-format=absolute",
            "--git-common-dir"]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env, cwd=cwd or None)
    except (OSError, subprocess.SubprocessError):
        return ""
    path = out.stdout.strip()
    return os.path.realpath(path) if out.returncode == 0 and os.path.isabs(path) else ""


def _temp_roots(env: dict) -> list[str]:
    """The run's own temp folders: the run folder, and the temp folder mktemp uses ($TMPDIR, else /tmp)
    unless it holds the home folder or the project, or lies in the home folder outside the run folder."""
    import pwd
    run = os.path.realpath(env["TTP_RUN_DIR"]) if env.get("TTP_RUN_DIR") else ""
    run = run if run and os.path.isdir(run) and Path(run).parent.name == "runs" else ""
    tmp = os.path.realpath(env.get("TMPDIR") or "/tmp")
    home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    keep = [os.path.realpath(d) for d in (home, env.get("TTP_PROJECT") or "") if d]
    bad = tmp == "/" or any(_under(d, tmp) for d in keep) or (_under(tmp, home) and not (run and _under(tmp, run)))
    return [r for r in (run, "" if bad else tmp) if r]


def temp_remote(url: str, env: dict | None = None, git: str = "") -> bool:
    """A push to `url` (the pre-push hook's location, after insteadOf rewrites, from the pushing repo's
    top) goes to a local repository under the run's own temp folders (_temp_roots), after symlinks
    and `..`: a plain path or a file:// URL, never a network remote (ssh, https, git://, scp-style
    host:path, a `<transport>::` helper), a path elsewhere, or the project's or the pushing repo's own
    repository (also a worktree's git dir, which opens the same common dir). Read by the hook only in a
    run whose push passes TTP_PUSH_ONLY_RT (command: no receive-pack override)."""
    env = os.environ if env is None else env
    git = git or env.get("TTP_PUSH_GUARD_GIT") or "git"
    if not url or "::" in url:
        return False
    if "://" in url:
        if not url.startswith("file://") or not url[7:].startswith("/"):
            return False
        url = url[7:]
    elif ":" in url.split("/", 1)[0]:   # scp-style host:path (git reads a colon before any slash so)
        return False
    roots = _temp_roots(env)
    if not roots:
        return False
    path = os.path.abspath(url)
    target = next((c for c in (_common_dir(git, d) for d in (path, path + ".git", os.path.join(path, ".git"))
                               if os.path.exists(d)) if c), "")
    if not target or not any(_under(target, r) for r in roots):
        return False
    deny = [_common_dir(git)]
    if env.get("TTP_PROJECT") and os.path.isdir(env["TTP_PROJECT"]):
        deny += [_common_dir(git, cwd=env["TTP_PROJECT"])]
    return not any(d and (_under(target, d) or _under(d, target)) for d in deny)


def _receive_pack_override(real: str, globals_: list[str], rest: list[str]) -> bool:
    """The push names its own receive-pack command (an option or a remote's config): a local-looking
    remote then runs any command, so the temp-folder allowance (temp_remote) does not apply."""
    if any(a in ("--receive-pack", "--exec") or a.startswith(("--receive-pack=", "--exec=")) for a in rest):
        return True
    out = subprocess.run([real, *globals_, "config", "--get-regexp", r"^remote\..*\.receivepack$"],
                         capture_output=True, text=True, timeout=30)
    return out.returncode != 1


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
    scan = bool(env.get("TTP_TASK"))
    try:
        branch, why = guarded_branch(env, approved_push(env, real, args))
        branch = branch and _branch_name(real, globals_, branch)
    except Exception:   # a fault reading the project skips this guard; push.code_run_landing still holds
        branch, why = "", ""
    from . import untrusted
    try:   # a run reading untrusted input pushes only its own work branch
        only = untrusted.push_refs(env, lambda ref: _branch_name(real, globals_, ref))
    except Exception as e:   # fails closed in such a run
        if untrusted.active(env):
            raise ValueError(f"git push refused: its untrusted-input guard failed ({type(e).__name__})") from None
        only = []
    if not branch and not scan and not only:
        return [real, *args], env
    try:
        hooks = "" if "--no-verify" in rest else _hooks_dir(real, globals_)
        guard = _hook_file()
    except Exception as e:
        if scan or only:   # the leak scan and the untrusted-input guard fail closed
            raise ValueError(f"git push refused: its leak scan could not be set up ({type(e).__name__}: {e}); "
                             "nothing was sent") from None
        return [real, *args], env
    rest = [a for a in rest if a not in ("--no-verify", "--verify")]
    try:
        tmp_off = bool(only) and _receive_pack_override(real, globals_, rest)
    except Exception:   # the temp-folder allowance fails closed
        tmp_off = True
    env = {**env, "TTP_PUSH_GUARD_REF": f"refs/heads/{branch}" if branch else "", "TTP_PUSH_GUARD_WHY": why,
           "TTP_PUSH_GUARD_HOOKS": hooks, "TTP_PUSH_SCAN_RT": str(Path(__file__).resolve().parents[1]) if scan else "",
           "TTP_PUSH_SCAN_PY": sys.executable, "TTP_PUSH_ONLY_REFS": " ".join(only), "TTP_PUSH_GUARD_GIT": real,
           "TTP_PUSH_ONLY_RT": str(Path(__file__).resolve().parents[1]) if only and not tmp_off else "",
           "TTP_PUSH_ONLY_WHY": untrusted.WHY(label=untrusted.LABEL, what=f"pushes only {' or '.join(only)}")
           + "Commit and hand off; the review lands the change" if only else ""}
    # after the caller's own options: the last core.hooksPath wins, so theirs never replaces the guard
    return [real, *globals_, "-c", f"core.hooksPath={guard}", "push", "--verify", *rest], env


def main(argv: list[str]) -> int:
    """argv: the real git, then the arguments `git` was called with."""
    real, args = argv[0], argv[1:]
    try:
        resolved, shell = resolve(real, args)
    except Exception as e:   # the wrapper hands over only a run's calls: fails closed, as the leak scan does
        print(f"tt-project: git {split(args)[1]} refused: its alias could not be resolved ({type(e).__name__})",
              file=sys.stderr)
        return 1
    if split(resolved)[1] == "push" or bypass_refusal(resolved, shell):
        try:   # a coordinator run pushes only an approved head through `ttp push` (coordtools.py)
            from . import coordtools
            coord = coordtools.coordinator_run() and not (split(resolved)[1] == "push"
                                                          and approved_push(dict(os.environ), real, resolved))
        except Exception:
            print("tt-project: the coordinator push check failed: no push", file=sys.stderr)
            return 1
        if coord:
            print(f"tt-project: {coordtools.PUSH_REFUSAL}", file=sys.stderr)
            return 1
    if os.environ.get("TTP_TASK") and (why := bypass_refusal(resolved, shell)):
        print(f"tt-project: {why}", file=sys.stderr)
        return 1
    if split(resolved)[1] == "push":
        args = resolved
    try:
        why = never_force_refusal(args, dict(os.environ), real)
    except Exception:   # a fault reading the project never stops git
        why = ""
    if why:
        print(f"tt-project: {why}", file=sys.stderr)
        return 1
    try:
        cmd, env = command(real, args, dict(os.environ))
    except ValueError as e:
        print(f"tt-project: {e}", file=sys.stderr)
        return 1
    sys.stdout.flush()
    os.execve(real, cmd, env)
    return 127

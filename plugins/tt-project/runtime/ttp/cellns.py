# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Real isolation for a bench cell's agent (`ttp bench run`, `ttp model-bench suite`): a Linux mount namespace
in which the provider CLI, and every tool command it runs, see only what the cell gives them.

A deny list ("hidden paths", benchrun.hidden_paths) is not isolation: an agent that names a path it was
not told about, or runs `git fetch`, `git clone` or `gh`, can still find the landed answer. Here the agent
starts in a namespace built from nothing (bubblewrap where installed, else util-linux `unshare` with
cellns_init.py) that holds only:
- the cell's own folder: the workspace (a one-commit git repo of the task's base tree, no remote, no other
  ref, no reflog, no alternates), the run folder, a scratch HOME and config folders, the cell's venv;
- read-only system folders (/usr, /etc and the usual /bin, /lib links) and the provider CLI's own install;
- a private /proc, a minimal /dev and an empty /tmp.
The source repository and its worktrees, the harness runtime, the project's state, the bench, prep, results
and cells folders and the real home folder do not exist in it. The network is shared (the endpoint and the
chat bridge stay reachable; the runner and the bridge run outside), but `gh` is a stand-in that logs and
fails, forge tokens, the ssh agent and git credential helpers are unset, and git speaks only to local paths.
The project's CLI hooks are not loaded in a cell (their commands live in the runtime): the namespace is the guard.

An injection cell (benchcells.injection_cell) keeps the same namespace and adds only what its canaries need, at the
same paths: its stand-in tools, its canary folder, its bare canary remote (one `origin`) and its inputs; its
decoy credential lives in the cell's scratch HOME. The source checkout, the project's folders and the real home
stay out of it, as for a replay cell.

selftest() runs inside the same namespace before the agent starts and returns what is wrong; a cell that fails
it is never run or scored. Without a working backend, backend() is None and a cell is "not isolated": it
never runs, is never scored and never ranks. Applies to bench cells only: project workers stay as they are."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc")
INIT = Path(__file__).with_name("cellns_init.py")
PROBE_S = 30
# Environment a cell must not inherit: forge and ssh credentials, git's own overrides, the real config folders.
SCRUB = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST", "GH_CONFIG_DIR",
         "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_DIR",
         "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
         "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "NETRC",
         "OLDPWD", "TMPDIR", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
         "CLAUDE_SECURESTORAGE_CONFIG_DIR")
# Real files and folders a cell's agent must not find (the self-test), under the real home folder.
HOME_SECRETS = (".ssh", ".config/gh", ".gitconfig", ".git-credentials", ".netrc", ".aws", ".claude/plugins",
                ".claude/projects", ".codex/sessions")
GH_STANDIN = ('#!/bin/sh\nprintf "%s\\n" "gh $*" >> "$HOME/.gh-calls"\n'
              'echo "gh: GitHub is not available in a bench cell" >&2\nexit 1\n')
GITCONFIG = ("[user]\n\tname = bench\n\temail = bench@example.invalid\n[credential]\n\thelper =\n"
             "[safe]\n\tdirectory = *\n[commit]\n\tgpgsign = false\n")
SELFTEST = r'''
fail() { echo "FAIL: $*"; }
for p in "$@"; do
  if [ -e "$p" ] || [ -L "$p" ]; then fail "visible inside the cell: $p"; fi
done
r=$(echo $(git remote 2>&1))   # one line, whatever it lists
if [ -n "$EXPECT_REMOTE" ]; then
  [ "$r" = origin ] || fail "git remote is '$r', not only origin"
  u=$(git remote get-url origin 2>&1); [ "$u" = "$EXPECT_REMOTE" ] || fail "origin is '$u', not the cell's canary remote"
else
  [ -z "$r" ] || fail "git remote is not empty: $r"
fi
n=$(git rev-list --all --count 2>&1); [ "$n" = 1 ] || fail "git rev-list --all --count is '$n', not 1"
if [ -n "$EXPECT_GH" ]; then
  g=$(command -v gh 2>&1); [ "$g" = "$EXPECT_GH" ] || fail "gh is '$g', not the cell's stand-in"
elif gh auth status >/dev/null 2>&1; then fail "gh succeeds"; fi
[ -z "$GH_TOKEN$GITHUB_TOKEN$SSH_AUTH_SOCK$GIT_ASKPASS" ] || fail "a forge or ssh credential is in the environment"
[ "$HOME" = "$EXPECT_HOME" ] || fail "HOME is '$HOME', not the cell's"
echo SELFTEST-END
'''


class Unavailable(RuntimeError):
    """No isolation backend works here; a cell must not run without one."""


_BACKEND: dict[str, str | None] = {}
Plan = list  # of tuples: (ro|bind, src, dst), (tmpfs, dst), (symlink, target, dst), (set, name, value), (unset, name), (chdir, dir)


def render(name: str, plan: Plan, root: Path | None = None) -> list[str]:
    """The command prefix that runs a program in a namespace built from `plan`. unshare needs `root`: a folder
    (made by the caller, inside the cell's own) that becomes the new root's mount point."""
    if name == "bwrap":
        args = ["--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--proc", "/proc",
                "--dev", "/dev"]
        for kind, *rest in plan:
            args += {"ro": ["--ro-bind"], "bind": ["--bind"], "tmpfs": ["--tmpfs"], "symlink": ["--symlink"],
                     "set": ["--setenv"], "unset": ["--unsetenv"], "chdir": ["--chdir"]}[kind] + rest
        return [shutil.which("bwrap") or "bwrap", *args, "--"]
    args = [f"--root={root}"]
    for kind, *rest in plan:
        args.append(f"--{kind}={rest[0]}=" + rest[1] if kind == "set" else f"--{kind}={':'.join(rest)}")
    return [shutil.which("unshare") or "unshare", "--user", "--map-current-user", "--keep-caps", "--mount", "--pid",
            "--fork", "--kill-child", sys.executable or "python3", "-I", "-S", str(INIT), *args, "--"]


def _under(p: str, roots) -> bool:
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)


def system_plan() -> Plan:
    """Mounts of the system folders as the host has them: a /bin that links into /usr stays a link."""
    plan: Plan = []
    for d in SYSTEM:
        if os.path.islink(d):
            plan.append(("symlink", os.readlink(d), d))
        elif os.path.isdir(d):
            plan.append(("ro", d, d))
    if os.path.islink("/etc/resolv.conf"):   # a resolver stub the system keeps under /run
        real = os.path.realpath("/etc/resolv.conf")
        if os.path.exists(real) and _under(real, ("/run",)):
            plan.append(("ro", str(Path(real).parent), str(Path(real).parent)))
    return plan


def backend() -> str | None:
    """"bwrap" or "unshare", whichever builds a namespace here (bubblewrap first), else None; probed once.
    TTP_CELL_BACKEND names one (or "none") for tests."""
    forced = os.environ.get("TTP_CELL_BACKEND")
    if forced is not None:
        return forced if forced in ("bwrap", "unshare") and shutil.which(forced) else None
    if "v" not in _BACKEND:
        found = None
        if sys.platform.startswith("linux"):
            with tempfile.TemporaryDirectory(prefix="cellns-") as d:
                for n in ("bwrap", "unshare"):
                    if not shutil.which(n):
                        continue
                    try:
                        r = subprocess.run([*render(n, [*system_plan(), ("tmpfs", "/tmp")], Path(d)), "true"],
                                           capture_output=True, timeout=PROBE_S, stdin=subprocess.DEVNULL,
                                           env={**os.environ, "PATH": "/usr/bin:/bin"})
                    except (OSError, subprocess.SubprocessError):
                        continue
                    if r.returncode == 0:
                        found = n
                        break
        _BACKEND["v"] = found
    return _BACKEND["v"]


def tool_binds(exe: str, avoid: list[Path]) -> list[Path]:
    """The folders (read-only) a provider CLI or interpreter `exe` needs beyond the system ones: its resolved
    install (the file's folder, or a python's prefix) and its shebang interpreter's. A folder that holds a
    hidden path, or is the real home folder or one of its parents, is narrowed to the file itself."""
    out: list[Path] = []
    real0 = os.path.realpath(shutil.which(exe) or exe)
    todo = [real0]
    try:
        with open(real0, "rb") as f:
            head = f.read(200)
        if head.startswith(b"#!"):
            words = head[2:].split(b"\n")[0].decode(errors="replace").split()
            if len(words) > 1 and os.path.basename(words[0]) == "env":
                words = [shutil.which(words[1]) or words[1]]
            if words and words[0].startswith("/"):
                todo.append(os.path.realpath(words[0]))
    except OSError:
        pass
    for real in todo:
        if _under(real, SYSTEM) or not os.path.exists(real):
            continue
        folder = Path(real).parent
        if folder.name == "bin" and Path(real).name.startswith("python"):
            folder = folder.parent
        home = Path.home()
        if folder == home or folder in home.parents or any(Path(a).is_relative_to(folder) for a in avoid):
            folder = Path(real)
        if folder not in out:
            out.append(folder)
    return out


@dataclass
class Cell:
    backend: str
    prefix: list[str]
    home: Path
    path: str = ""
    conf: dict = field(default_factory=dict)   # provider config folder variables (CLAUDE_CONFIG_DIR ...)
    cli: str = ""   # the provider's executable as the cell finds it (its resolved install)
    python: str = ""   # the cell venv's interpreter and, for a dry run, the runtime copy its fake agent imports
    runtime: str = ""
    remote: str = ""   # an injection cell's canary remote (its one allowed `origin`); "" for none
    gh: str = ""   # an injection cell's own gh stand-in (logs to the cell's canary folder); "" for the default one

    def wrap(self, argv: list[str], unset: list[str] = ()) -> list[str]:
        """`argv` run in the namespace, with the inherited variables `unset` also removed (the prefix ends in --)."""
        if not unset:
            return [*self.prefix, *argv]
        more = [x for k in unset for x in (["--unsetenv", k] if self.backend == "bwrap" else [f"--unset={k}"])]
        return [*self.prefix[:-1], *more, self.prefix[-1], *argv]

    def sets(self) -> dict:
        """What the cell sets in its processes' environment (the rest is inherited, less SCRUB)."""
        out = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "GH_CONFIG_DIR": str(self.home / ".config" / "gh"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"), "GIT_ALLOW_PROTOCOL": "file",
               "GIT_TERMINAL_PROMPT": "0", **self.conf}
        if self.path:
            out["PATH"] = self.path
        return out


def build(tmp: Path, ws: Path, hidden: list[Path], *, python: str, cli: str = "", config: dict | None = None,
          creds: list[tuple[str, str]] = (), runtime_copy: Path | None = None, name: str | None = None,
          path_dirs: list[str] = (), standins: dict[str, Path] | None = None, binds: list[tuple[str, str]] = (),
          remote: str = "") -> Cell:
    """The namespace for one cell whose folder is `tmp` (holding `ws`, run, venv, config folders).
    `python`: the interpreter its venv uses; `cli`: the provider's executable; `config`: provider config
    variables (their folders are in `tmp`); `creds`: real login files (path, where in the cell) bound
    read-only; `runtime_copy`: a copy of the runtime a dry run's fake agent imports (never the real one);
    `path_dirs`: folders of `tmp` the cell's PATH starts with. An injection cell also gives `standins` (tool
    name -> its stand-in script, which then replaces the default `gh` one, also over a system copy), `binds`
    (("ro"|"bind", path): folders bound at the same path: its stand-ins' folder, the canary folder its
    stand-ins and hooks write to, its bare canary remote and inputs) and `remote` (that remote's path, the
    one `origin` the self-test accepts). Raises Unavailable without a backend."""
    name = name or backend()
    if not name:
        raise Unavailable("not isolated: neither bwrap (bubblewrap) nor unshare (util-linux) can build a mount "
                          "namespace here (or user namespaces are off), so the cell never runs and is never scored")
    tmp = Path(tmp).resolve()
    home = tmp / "home"
    (home / ".config").mkdir(parents=True, exist_ok=True)
    (tmp / "bin").mkdir(exist_ok=True)
    (home / ".gitconfig").write_text(GITCONFIG)
    gh = tmp / "bin" / "gh"
    if not standins:
        gh.write_text(GH_STANDIN)
        gh.chmod(0o755)
    covers = dict(standins) if standins else {"gh": gh}
    avoid = [Path(h) for h in hidden]
    ro: list[Path] = []
    for exe in (python, cli):
        ro += tool_binds(exe, avoid) if exe else []
    if runtime_copy:
        ro.append(Path(runtime_copy))
    plan: Plan = [*system_plan(), ("tmpfs", "/tmp")]
    plan += [("ro", str(p), str(p)) for p in dict.fromkeys(ro) if not _under(str(p), SYSTEM)]
    plan.append(("bind", str(tmp), str(tmp)))
    plan += [("ro", str(real), str(to)) for real, to in creds if os.path.isfile(real)]   # after its folder's mount
    plan += [(mode, str(p), str(p)) for mode, p in binds]
    for d in dict.fromkeys(os.path.realpath(x) for x in [*os.environ.get("PATH", "").split(os.pathsep),
                                                        "/usr/local/bin", "/usr/bin", "/bin"] if x):
        for tool, standin in covers.items():
            if _under(d, SYSTEM) and os.path.isfile(os.path.join(d, tool)):   # a system copy: the stand-in covers it
                plan.append(("ro", str(standin), os.path.join(d, tool)))
    cell = Cell(name, [], home, conf=dict(config or {}), remote=remote, gh=str((standins or {}).get("gh", "")))
    found = shutil.which(cli) or cli if cli else ""
    real = os.path.realpath(found) if found else ""
    cell.cli = real if real and not _under(real, SYSTEM) else found
    cell.path = os.pathsep.join([*map(str, path_dirs), str(tmp / "bin"), "/usr/local/bin", "/usr/bin", "/bin",
                                 "/usr/sbin", "/sbin"])
    plan += [("unset", v) for v in SCRUB] + [("set", k, v) for k, v in cell.sets().items()] + [("chdir", str(ws))]
    root = tmp / "root"
    if name == "unshare":
        root.mkdir(exist_ok=True)
    cell.prefix = render(name, plan, root)
    return cell


def selftest(cell: Cell, ws: Path, absent: list[Path], timeout_s: float = 60) -> list[str]:
    """What is wrong with `cell`, checked from inside it: a path of `absent` that exists there, a remote,
    more than one commit (an injection cell's one remote is its canary `origin`: any other remote or url is
    wrong), a `gh` that works (an injection cell's is its own stand-in), a forge credential or the real HOME
    in the environment. []
    when the cell is as isolated as it should be."""
    env = {**os.environ, "EXPECT_HOME": str(cell.home), "EXPECT_REMOTE": cell.remote, "EXPECT_GH": cell.gh}
    try:
        r = subprocess.run(cell.wrap(["sh", "-c", SELFTEST, "sh", *map(str, absent)]), capture_output=True, text=True,
                           timeout=timeout_s, stdin=subprocess.DEVNULL, env=env, cwd=str(ws))
    except (OSError, subprocess.SubprocessError) as e:
        return [f"the namespace did not start: {e}"[:300]]
    lines = r.stdout.splitlines()
    if "SELFTEST-END" not in lines:
        return [f"the self-test did not finish (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}"]
    return [x[len("FAIL: "):] for x in lines if x.startswith("FAIL: ")]


def real_paths(runtime: Path, repos: list) -> list[Path]:
    """Paths the self-test requires absent: the runtime, each source repository, and the real home
    folder's credentials and saved sessions."""
    home = Path.home()
    out = [Path(runtime)] + [Path(r) for r in repos if r] + [home / p for p in HOME_SECRETS]
    return list(dict.fromkeys(Path(os.path.abspath(p)) for p in out))

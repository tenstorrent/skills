"""Runs the tt-project tests (tests/test_tt_project_*.py) in forked worker processes, with no plugin.

Those files hold well over a thousand tests that mostly wait on subprocesses (git, the CLI, the
runner), so one process spends most of its time idle. When a session collects at least
MIN_ITEMS of them, the main process forks TTP_TEST_JOBS workers after collection. The default is
half the CPUs, at most 4, so a laptop stays usable; on CI (the CI variable set) it is one per CPU, at
most 8. Off CI the workers run at a lower priority (nice +10, where the system has it). Each worker takes the next test from a shared queue and runs it with pytest's own
runtest protocol; the main process replays every report through pytest's hooks, so the terminal
output, --durations, the summary and the exit status are those of an ordinary run. All other
collected tests form one unit that a single worker runs in order, as before.

Every collected test still runs exactly once. A worker that dies fails the tests it took and did not
finish. A test that closes or replaces one of the worker's own pipe fds (the shared queue or its
report pipe) is named: the worker checks both fds after each test phase and, on a change, writes the
test's nodeid to stderr and to the main process and exits with FD_LOST_EXIT, instead of using the
wrong file and ending with status 0 mid-run. TTP_TEST_JOBS=1 (or -x, --maxfail, --pdb, -s,
--stepwise, xdist) runs everything in-process.

A session that runs tt-project tests also fails if, at its end, a process whose command line names
the session's own temp root (basetemp) is still alive: those are processes a test detached and never
reaped. The guard lists them and stops them first, so a failure leaves nothing behind. Only this
session's basetemp counts, so concurrent sessions on the host never trip it. `reap_tmp_processes`
gives a test the same reaping for its own tmp_path. Each tt-project test that used tmp_path also has,
after its teardown, any process still running there (its working folder in tmp_path, or its command
line naming it; read from /proc, so Linux only) stopped with its group and reported as an error of
that test (_reap_test_processes): a test that fails before its own cleanup leaves nothing running.

Every run ends its summary with one line of wall time and CPU time (user + system, of the main
process and every child it waited for: workers and the processes the tests started), e.g.
`ttp-suite: wall 312.4s cpu 1840.2s workers 4`, so a slower or costlier suite shows in push logs. A
full-suite run (MIN_ITEMS or more tt-project tests) also prints its SLOWEST slowest test calls just above
that line (`ttp-suite: slowest 15 test calls:`, then `ttp-suite:   61.20s <test id>` each), so slow or
wall-clock-bound tests can be ranked from any run's log.

A forked run hands its workers the slowest tests first (_order), by the durations earlier runs on this
host measured (DURATIONS, a per-user file in the temp folder; TTP_TEST_DURATIONS names another, `off`: none,
as for every suite a test starts), so one
long test never starts last and holds the run's end; tests it has no time for run in their usual order
after the known slow ones. Every run of MIN_ITEMS or more tt-project tests updates that file.

A tt-project test still running after STACK_DUMP_S has every thread's stack dumped to the session's
stderr (Python's faulthandler, as pytest's faulthandler_timeout does, scoped to tests/test_tt_project_*.py:
other plugins' tests are untouched). It only dumps and kills nothing. The default sits far above the
slowest test call (about 30 s) and below `ttp checks`' silence limit (checkwall.SILENT_S, 10 min), so a
hung test's stack is in the checks log before that limit ends the run. TTP_TEST_STACK_DUMP_S sets it (0:
off); a faulthandler_timeout set in pytest's own config wins.

Every test ends with the process's signal handlers and signal mask as they were before it
(`_signals_kept`): a handler a test's in-process command installed and never restored (`ttp lock`'s
wait exits 128 + the signal) made a later signal test in the same worker pass or fail by test order.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import selectors
import shlex
import signal
import struct
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

MIN_ITEMS = 50
SLOWEST = 15           # the slowest test calls a full-suite run lists
MAX_AUTO_JOBS = 8      # on CI
MAX_LOCAL_JOBS = 4     # anywhere else: half the CPUs, at most this, and never more than the free CPUs
UNKNOWN_S = 1.0        # the duration _order assumes for a test no earlier run timed
WORKER_NICE = 10
FD_LOST_EXIT = 97      # a worker's own exit when a test closed or replaced one of its pipe fds
_SHARDED = "test_tt_project_"
STACK_DUMP_S = 480.0   # a tt-project test running this long dumps its stacks (faulthandler; kills nothing)
# A suite run inside a tt-project worker (`ttp checks`, a worker's own pytest) inherits the run's
# variables: its task, run folder, project, device and hand-off guards, and the agent CLI's session.
# Tests set what they need, so the session starts without them; the test-slot, port, check-output
# and leak-pattern variables the checks harness passes the suite stay.
_WORKER_ENV_KEEP = ("TTP_TEST_", "TTP_CHECKS_", "TTP_CHECK_OUTPUT_DIR", "TTP_PORT_", "TTP_BUILD_SLOTS_HELD",
                    "TTP_LEAK_PATTERNS_FILE", "TTP_TX_GUARD", "TTP_ALLOW_VOLATILE_BASE", "TTP_DEVICE_NODES")
_WORKER_ENV_CLEAR = ("TTP_", "CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_ENV_FILE", "CLAUDE_PID", "CLAUDE_EFFORT",
                     "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI", "CODEX_MANAGED_BY")


def worker_env_cleared(env) -> dict:
    """`env` without the worker run's variables (above)."""
    return {k: v for k, v in env.items() if not k.startswith(_WORKER_ENV_CLEAR) or k.startswith(_WORKER_ENV_KEEP)}


def _clear_worker_env() -> None:
    for var in set(os.environ) - set(worker_env_cleared(os.environ)):
        os.environ.pop(var, None)


_clear_worker_env()
# tt-project's db.tx() refuses a git network command (ls-remote, fetch, push) started inside a write
# transaction while this is set: every test, and every process it starts, runs under that guard.
os.environ.setdefault("TTP_TX_GUARD", "1")
os.environ.setdefault("TTP_ALLOW_VOLATILE_BASE", "1")   # the suite runs on tmpfs on purpose
os.environ.setdefault("TTP_DEVICE_NODES", "")   # no host device node changes what a test sees; tests set their own


def _git_global_config() -> None:
    """Every test runs with HOME in a temp folder (_temp_home), so git would find neither the account's
    global config nor its identity there, and a test's `git config --global` would write the account's
    own ~/.gitconfig without it. The suite and every process it starts get a global git config of their
    own instead, a session file holding a copy of the account's (read only), with a test identity
    under it where git resolves none (a fresh test host). It copies rather than includes: the runtime's
    git answer cache (gitcache, gitreplay) keeps nothing while a global config has an include."""
    import atexit
    import shutil
    import tempfile
    try:
        # Outside any repository: the checkout running the suite may set an identity of its own.
        ident = subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], capture_output=True, cwd=os.sep,
                               env={k: v for k, v in os.environ.items() if not k.startswith("GIT_DIR")}).returncode == 0
    except OSError:
        return   # no git: the tests that need it say so
    home = Path(os.environ.get("HOME", "/"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    own = [os.environ["GIT_CONFIG_GLOBAL"]] if os.environ.get("GIT_CONFIG_GLOBAL") else \
        [str(f) for f in (xdg / "git" / "config", home / ".gitconfig") if f.is_file()]
    folder = tempfile.mkdtemp(prefix="ttp-test-git-")
    atexit.register(shutil.rmtree, folder, True)
    config = Path(folder) / "gitconfig"
    texts = []
    for f in own:
        try:
            text = Path(f).read_text(errors="replace")
        except OSError:
            continue
        # One with includes of its own stays included by path, so its relative include paths resolve.
        texts.append(f"[include]\n\tpath = {f}\n" if re.search(r"^\s*\[\s*include", text, re.I | re.M)
                     else text.rstrip("\n") + "\n")
    config.write_text(("" if ident else "[user]\n\tname = t\n\temail = test@example.invalid\n")
                      + "".join(texts)
                      # Signing keys live in the account's home folder, which tests never see.
                      + "[commit]\n\tgpgSign = false\n[tag]\n\tgpgSign = false\n"
                      # Test repositories are thrown away: no fsync (git 2.36+), which on a disk
                      # filesystem queues the suite's many small repositories on its metadata locks.
                      + "[core]\n\tfsync = none\n")
    os.environ["GIT_CONFIG_GLOBAL"] = str(config)


_git_global_config()


# No test writes into the account's own home folder: each one runs with HOME, the XDG folders, gh's and the
# agent CLIs' config folders in a fresh temp folder of its own (_temp_home), the session outside tests in
# one of its own (_session_home), and every process they start inherits them. Python's user packages stay importable from the account's own user base. A session
# also fails when, at its end, one of the account's own files below (HOME_WATCHED, under REAL_HOME) or
# an untracked file in the repository the suite runs from is new or changed: what a test wrote there by
# an absolute path, or relative to its working folder. A home folder change that no process of the session
# recorded writing (_WriteRecorder) is another process's on a shared host: a warning, not a failure. The
# credential guards below (credential_marks, folder_marks) judge their changes by the same rule (guarded_changes).
REAL_HOME = os.path.expanduser("~")
_XDG = {"XDG_CONFIG_HOME": ".config", "XDG_DATA_HOME": ".local/share", "XDG_STATE_HOME": ".local/state",
        "XDG_CACHE_HOME": ".cache"}
_AGENT_HOMES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME")


def _leak_patterns_file() -> None:
    """The local leak-pattern file (tests/test_tt_project_plugin.py) lives under the account's own
    config folder, which each test's temp HOME hides: the suite gets it by path, set before any test
    runs, so the leak test runs instead of skipping. Only the path: its text is never read here."""
    if os.environ.get("TTP_LEAK_PATTERNS_FILE"):
        return
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.join(REAL_HOME, ".config")
    path = os.path.abspath(os.path.join(config, "tt-project", "leak-patterns.txt"))
    if os.path.isfile(path):
        os.environ["TTP_LEAK_PATTERNS_FILE"] = path


_leak_patterns_file()


def _user_base() -> str | None:
    import site
    return site.getuserbase() if site.ENABLE_USER_SITE else None


_USER_BASE = _user_base()
_REAL_ENV = dict(os.environ)   # the account's own environment, for the credential snapshot below
HOME_WATCHED = (".bash_profile", ".bash_login", ".bashrc", ".profile", ".zprofile", ".zshrc", ".zshenv",
                ".gitconfig", ".gitmodules", ".config/git/config", ".ssh/config", ".ssh/authorized_keys",
                ".mcp.json", ".ripgreprc", ".idea", ".vscode", ".claude/settings.json",
                ".claude/settings.local.json", ".claude/CLAUDE.md", ".claude/skills", ".claude/agents",
                ".claude/commands", ".claude/hooks", ".codex/config.toml", ".config/systemd/user",
                ".local/share/systemd/user", "Library/LaunchAgents", ".local/bin/ttp")
# The credential stores and the ssh and gh folders the session's credential guards watch: the write
# recorder records the session's writes there too (and the providers' login files, _WriteRecorder.watch).
GUARDED = (".ssh", ".config/gh", ".git-credentials", ".netrc")


def _point_home(home: str, setenv, delenv) -> None:
    """HOME, the XDG folders and gh's config folder under `home`; the agent CLIs' own config folders unset
    (they fall back under HOME); the account's user packages still importable."""
    setenv("HOME", home)
    for var, sub in _XDG.items():
        setenv(var, os.path.join(home, sub))
    setenv("GH_CONFIG_DIR", os.path.join(home, ".config", "gh"))
    setenv("GIT_CONFIG_NOSYSTEM", "1")   # git reads only the temp global config (GIT_CONFIG_GLOBAL)
    for var in _AGENT_HOMES:
        delenv(var)
    if _USER_BASE and not os.environ.get("PYTHONUSERBASE"):
        setenv("PYTHONUSERBASE", _USER_BASE)


def _session_home() -> None:
    """The same for the whole session, before any test module is imported: code that runs at collection,
    in a session fixture or between tests never sees the account's home either."""
    import atexit
    import shutil
    import tempfile
    home = tempfile.mkdtemp(prefix="ttp-test-home-")
    atexit.register(shutil.rmtree, home, True)
    _point_home(home, os.environ.__setitem__, lambda var: os.environ.pop(var, None))


_session_home()


def _checks_python_env() -> None:
    """A stand-in for the checks' pinned Python env (runnerenv), made once per session: its python,
    python3 and pytest run the interpreter running these tests (with its user site, if it loads one), so a
    `ttp checks` in a test never builds one; a check's command runs `python3`, not sys.executable.
    runnerenv's own tests unset TTP_TEST_CHECKS_PYTHON_ENV."""
    import atexit
    import json
    import platform
    import shutil
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ttp-test-pyenv-"))
    atexit.register(shutil.rmtree, str(d), True)
    (d / "bin").mkdir()
    for name, tail in (("python", ""), ("python3", ""), ("pytest", " -m pytest")):
        # with the user site this interpreter itself loads (a venv loads none), as it runs these tests
        (d / "bin" / name).write_text(f'#!/bin/sh\nunset PYTHONNOUSERSITE\nexec {shlex.quote(sys.executable)}{tail} "$@"\n')
        (d / "bin" / name).chmod(0o755)
    (d / "ttp-env.json").write_text(json.dumps({"key": "test", "lock": "test", "lock_sha256": "0" * 64,
                                                "python": platform.python_version(), "pytest": pytest.__version__}))
    os.environ.setdefault("TTP_TEST_CHECKS_PYTHON_ENV", str(d))


_checks_python_env()


# The account's credential files (gh's hosts file, git's and netrc's credential files, ssh private keys,
# every file under the write guard's protected paths (pathguard.paths()), the providers' login files) as the
# plugin's own list names them (credwatch.files), read by a child under the account's real environment and
# compared by stat only (size, mtime, ctime, inode; never contents, as credwatch.changes does): a session
# fails when the session changed one (guarded_changes).
_HERE = Path(__file__).resolve().parent
_RUNTIME = next((d / "plugins" / "tt-project" / "runtime" for d in (_HERE.parent, _HERE)
                 if (d / "plugins" / "tt-project" / "runtime" / "ttp" / "credwatch.py").is_file()), None)

def _load_hoststate():
    """The plugin's hoststate module, loaded by path (the session imports no ttp package of its own)."""
    import importlib.util
    if _RUNTIME is None:
        return None
    spec = importlib.util.spec_from_file_location("_ttp_hoststate", _RUNTIME / "ttp" / "hoststate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _host_guard():
    """Every test, and every process it starts, finds the host-state stand-ins first on PATH (crontab,
    systemctl, loginctl, at, reboot, shutdown, poweroff, halt, ipmitool, tt-smi: hoststate.py), so a
    test's call by name never changes the account's real crontab, systemd units or power state. A run
    under `ttp checks` or `ttp sandbox` keeps their store; else a temp one goes at exit. The session
    also compares the real crontab and user units before and after (pytest_sessionfinish)."""
    import atexit
    import shutil
    hs = _load_hoststate()
    if hs is None:
        return None
    inherited = bool(os.environ.get(hs.MARKER))
    os.environ.update(hs.fake_env(dict(os.environ)))
    if not inherited:
        atexit.register(shutil.rmtree, os.environ[hs.MARKER], True)
    return hs


_HOSTSTATE = _host_guard()
os.environ.setdefault("TTP_NO_HAIKU", "1")   # no test asks Claude Haiku for a decision (cheapdecide)

_CRED_SCRIPT = ("import json, sys\nsys.path.insert(0, sys.argv[1])\nfrom ttp import credwatch\n"
                "before = json.load(sys.stdin)\nnow = credwatch.snapshot()\n"
                "print(json.dumps(now if before is None else credwatch.changes(before, now)))\n")


def credential_marks(before: dict | None = None):
    """credwatch.snapshot() of the account's credential files, or with `before` the changes since it
    ([[path, how], ...]); None when the plugin's runtime is not beside this file or the child fails."""
    if _RUNTIME is None:
        return None
    recorder = getattr(sys.modules.get("_ttp_suite_writes"), "recorder", None)
    if recorder:   # its own child reads under the account's environment: no write of a test
        recorder.quiet += 1
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _CRED_SCRIPT, str(_RUNTIME)], input=json.dumps(before),
                           capture_output=True, text=True, timeout=60, env=_REAL_ENV, cwd=os.sep)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if recorder:
            recorder.quiet -= 1


# The account's ssh folder and gh's config folder, whole: each regular file's size, mtime, ctime and inode
# (stat only: no file there is ever opened or read), so a session fails when a test wrote any file there (another process's change: a warning). ssh and ssh-keygen find
# ~/.ssh from the account's passwd entry, not HOME: a test passes them -f/-F/-o UserKnownHostsFile under its
# temp folder. A child running ssh-keygen without such a -f, or ssh-copy-id, counts as handed the ssh
# folder (_WriteRecorder.spawned). known_hosts files and sockets are left out: the account's own ssh
# sessions change them while a suite runs.
def secret_folders(env=None) -> list[str]:
    env = _REAL_ENV if env is None else env
    home = env.get("HOME") or REAL_HOME
    gh = env.get("GH_CONFIG_DIR") or os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "gh")
    return [os.path.join(home, ".ssh"), gh]


def folder_marks(folders) -> dict[str, tuple]:
    """{path: (size, mtime_ns, ctime_ns, inode)} of each regular file under `folders`, by lstat alone:
    private keys and gh's token are never opened or read."""
    import stat
    marks: dict[str, tuple] = {}
    for top in folders:
        for root, dirs, names in os.walk(top):
            for name in names:
                if name.startswith("known_hosts"):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    marks[path] = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
    return marks


def folder_diff(before: dict, after: dict) -> list[tuple[str, str]]:
    """(path, how) of each created, removed or changed file between two folder_marks."""
    return [(p, "created" if p not in before else "removed" if p not in after else "changed")
            for p in sorted(set(before) | set(after)) if before.get(p) != after.get(p)]


def folder_changes(before: dict, after: dict) -> list[str]:
    """One line per created, removed or changed file between two folder_marks."""
    return [f"  {p}: {how}" for p, how in folder_diff(before, after)]


@pytest.fixture(autouse=True)
def _temp_home(tmp_path_factory, monkeypatch):
    """Points HOME (and the XDG and agent config folders) at a fresh temp folder for this test, and
    fails it when it handed the account's whole home to a child (a HOME-less env the recorder could not
    fill, HOME=<account home>): the session would then count every outside change there as its own."""
    root = tmp_path_factory.getbasetemp() / "homes"
    root.mkdir(mode=0o700, exist_ok=True)
    import tempfile
    home = tempfile.mkdtemp(prefix="home-", dir=root)
    _point_home(home, monkeypatch.setenv, lambda var: monkeypatch.delenv(var, raising=False))
    monkeypatch.setenv("TTP_TEST_NO_TAILSCALE", "1")   # unreachable-host messages never ask this host's Tailscale (tailnet.py)
    if os.environ.get("GIT_CONFIG_GLOBAL"):   # a `git config --global` of its own: tests never share one
        own = Path(home, ".gitconfig")   # a copy, not an include: see _git_global_config
        own.write_bytes(_session_git_config(os.environ["GIT_CONFIG_GLOBAL"]))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(own))
    recorder = getattr(sys.modules.get("_ttp_suite_writes"), "recorder", None)
    bare, fallback = (recorder.bare, recorder.fallback) if recorder else (0, None)
    if recorder:
        recorder.fallback = home
    yield Path(home)
    if recorder:
        recorder.fallback = fallback
    if recorder and recorder.bare > bare:
        pytest.fail(f"a child of this test was handed the account's home {recorder.home} (HOME unset or "
                    "set to it): give it a temp HOME (and patch ttp.project._account where the code falls "
                    "back to passwd's home)", pytrace=False)


_GIT_CONFIG_TEXT: dict[str, bytes] = {}


def _session_git_config(path: str) -> bytes:
    """The session's global git config (read once per path; tests never write it)."""
    if path not in _GIT_CONFIG_TEXT:
        try:
            _GIT_CONFIG_TEXT[path] = Path(path).read_bytes()
        except OSError:
            _GIT_CONFIG_TEXT[path] = f"[include]\n\tpath = {path}\n".encode()
    return _GIT_CONFIG_TEXT[path]


def _content_mark(path: str) -> tuple:
    """A file's size and sha256: the account's own tooling rewrites some watched files with the same
    bytes while a suite runs (a deploy's `ttp setup` rewrites ~/.local/bin/ttp), which is no change. An
    unreadable file: its size and mtime."""
    import hashlib
    try:
        with open(path, "rb") as f:
            digest = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
            return os.fstat(f.fileno()).st_size, digest.hexdigest()
    except OSError:
        st = os.stat(path)
        return st.st_size, st.st_mtime_ns


def home_marks(home: str | Path) -> dict[str, object]:
    """Each HOME_WATCHED path under `home`: a file's content mark (_content_mark), a folder's entry
    names (a frozenset), or None when it is missing."""
    marks: dict[str, object] = {}
    for rel in HOME_WATCHED:
        path = os.path.join(home, rel)
        try:
            marks[rel] = frozenset(os.listdir(path)) if os.path.isdir(path) else _content_mark(path)
        except OSError:
            marks[rel] = None
    return marks


def untracked_files(root: str | Path) -> set[str] | None:
    """The untracked, not ignored files of the git repository whose top folder is `root`; None when
    `root` is not one (a suite run from a temp folder) or git fails."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=30)
        if top.returncode != 0 or os.path.realpath(top.stdout.strip()) != os.path.realpath(root):
            return None
        r = subprocess.run(["git", "--no-optional-locks", "-C", str(root), "status", "--porcelain", "-z",
                            "--untracked-files=all"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {e[3:] for e in r.stdout.split("\0") if e.startswith("?? ")}


def written_outside(before: dict, after: dict, recorded=None) -> tuple[list[str], list[str]]:
    """What changed outside a session's temp folders, from two snapshots of {"home": home_marks,
    "home_path": ..., "repo": untracked_files, "repo_path": ...}: one line per path, as (the session's
    own changes, home folder changes no recorded write of the session explains). `recorded`: the
    session's recorded writes (recorded_writes); None counts every change as the session's."""
    home: list[tuple[str, str]] = []
    for rel, was in before["home"].items():
        now = after["home"].get(rel)
        path = os.path.join(before["home_path"], rel)
        if was == now:
            continue
        if isinstance(was, frozenset) and isinstance(now, frozenset):
            home += [(os.path.join(path, n), "created") for n in sorted(now - was)]
            home += [(os.path.join(path, n), "removed") for n in sorted(was - now)]
        else:
            home.append((path, "created" if was is None else "removed" if now is None else "changed"))
    own = [f"  {p}: {how}" for p, how in home if recorded is None or own_change(p, recorded)]
    other = [f"  {p}: {how}" for p, how in home if recorded is not None and not own_change(p, recorded)]
    if before["repo"] is not None and after["repo"] is not None:   # the run's own checkout: always its own
        own += [f"  {os.path.join(before['repo_path'], f)}: created" for f in sorted(after["repo"] - before["repo"])]
    return own, other


def _outside_snapshot(config) -> dict:
    root = str(config.rootpath)
    return {"home": home_marks(REAL_HOME), "home_path": REAL_HOME, "repo": untracked_files(root), "repo_path": root}


# Other processes on a shared host change the account's own files while a suite runs (an agent CLI
# rewrites its config file when it starts, another project's setup adds or removes a systemd user unit),
# so a change alone does not say a test made it. Each process of the session (the main one, its forked
# workers, xdist's workers) records in one log the watched paths it wrote (a write open, rename, remove,
# mkdir, link, truncate or rmtree there or above) and the ones it handed to a child it started (by its
# command line, working folder or an explicit environment that differs from the session's, or that
# lacks HOME: the child then finds the account's home from its passwd entry). A change to a recorded
# path fails the session; any other is a warning line. A child that finds the account's home by itself
# while the session's temp HOME is set is handed its ssh folder where it runs ssh-keygen (without -f
# outside the home) or ssh-copy-id, by its argv or a shell command's words (ssh_keys_handed); any other
# (plain ssh, a script calling getpwuid) is not seen: its change is a warning.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_CHARS = r"[^\s'\"`;:,=()&|<>]"


class _WriteRecorder:
    """The audit hook: appends each watched path under `home` that this process wrote or handed to a
    child, once per process, to the file `log`. `env` is the session's own environment (a child's
    explicit environment counts only where it differs from it)."""

    SPAWNS = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}
    WRITES = {"os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.symlink": (1,),
              "os.link": (1,), "os.truncate": (0,), "shutil.rmtree": (0,)}

    def __init__(self, home: str, watched, log: str, env: dict):
        self.homes = _homes(home)
        self.home = os.path.normpath(home)
        self.watched = [os.path.join(self.home, rel) for rel in watched]
        self.log, self.env, self.seen, self.quiet = log, dict(env), set(), 0   # quiet: the conftest's own child
        self.bare = 0   # times the whole home was recorded: the per-test guard in _temp_home reads it
        self.fallback = None   # the running test's temp HOME: given where the test unset HOME
        self.found = re.compile("|".join(re.escape(h) for h in self.homes) + rf"(?:/{_PATH_CHARS}*)?(?![^\s'\"`;:,=()&|<>/])")

    def __call__(self, event: str, args: tuple) -> None:
        if self.quiet:
            return
        try:
            if event == "open":
                if isinstance(args[2], int) and args[2] & _WRITE_FLAGS or isinstance(args[1], str) and set(args[1]) & set("wax+"):
                    self.wrote(args[0])
            elif event in self.WRITES:
                for i in self.WRITES[event]:
                    self.wrote(args[i])
            elif event in self.SPAWNS:
                self.spawned(event, args)
        except Exception:   # an audit hook that raises would fail the call it audits
            pass

    def wrote(self, path) -> None:
        if isinstance(path, int) or path is None:
            return
        path = home_path(os.fsdecode(os.fspath(path)), self.home, self.homes)
        if path:
            self.record(path)

    def watch(self, paths) -> None:
        """Records writes to these paths too (absolute; ones outside the home folder are not seen)."""
        for path in paths:
            path = home_path(path, self.home, self.homes)
            if path and path not in self.watched:
                self.watched.append(path)

    def spawned(self, event: str, args: tuple) -> None:
        if event == "subprocess.Popen":
            exe, argv, cwd, env = args
            texts = [exe, cwd] + ([argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or ()))
        elif event == "os.system":
            texts, env = [args[0]], None
        else:
            texts, env = [args[-3]] + list(args[-2] or ()), args[-1]
        if env is not None:
            raw, env = env, {os.fsdecode(k): os.fsdecode(v) for k, v in dict(env).items()}
            if "HOME" not in env and not (event == "subprocess.Popen" and self.give_home(raw)):
                self.record(self.home)
            for key, value in env.items():
                own = set(self.env.get(key, "").split(os.pathsep))
                texts += [part for part in value.split(os.pathsep) if part not in own]
        for text in texts:
            if text is not None:
                for m in self.found.finditer(os.fsdecode(os.fspath(text))):
                    self.wrote(m.group(0))
        if event == "subprocess.Popen":
            words = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else [exe] * (exe is not None) + list(argv or ())
        else:
            words = list(args[-2] or ()) if event != "os.system" else [args[0]]
        if ssh_keys_handed([w for w in words if w is not None],
                           (cwd if event == "subprocess.Popen" else None) or os.getcwd(), self.homes):
            self.record(os.path.join(self.home, ".ssh"))

    def give_home(self, env) -> bool:
        """Puts the session's HOME (a temp folder) into a child's HOME-less env dict, so it does not fall
        back to passwd's: Popen builds the child's env after this audit event (os.exec and os.posix_spawn
        before theirs), or the running test's temp HOME where the test unset HOME. False where it cannot:
        not a dict, or the session's HOME is the account's own."""
        home = os.environ.get("HOME") or self.fallback
        real = os.path.realpath(home) if home else None
        if type(env) is not dict or not real or any(real == h or real.startswith(h + os.sep) for h in self.homes):
            return False
        as_bytes = any(isinstance(k, bytes) for k in env)
        env[b"HOME" if as_bytes else "HOME"] = os.fsencode(home) if as_bytes else home
        return True

    def record(self, path: str) -> None:
        self.bare += path == self.home
        if path in self.seen or not any(path == w or w.startswith(path + os.sep) or path.startswith(w + os.sep)
                                        for w in self.watched):
            return
        self.seen.add(path)
        fd = os.open(self.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (path + "\n").encode())
        finally:
            os.close(fd)


_SSH_KEY_TOOLS = {"ssh-keygen", "ssh-copy-id"}
_SHELL_BREAKS = {";", "&", "&&", "|", "||", "(", ")", ";;"}
_KEYGEN_ARGS = set("CDEFGIJMNOPRVYZabfgjmnrstwz")   # ssh-keygen's options that take a value


def ssh_keys_handed(words, cwd, homes) -> bool:
    """True when a child's `words` (its argv; a word with spaces or shell punctuation, as an `sh -c`
    command or os.system's string, is split as a shell would) run ssh-keygen or ssh-copy-id on the
    account's ssh folder: they find it from passwd, not HOME. ssh-keygen given -f under a folder outside
    `homes` (relative to `cwd`) does not; a -f naming ~, a variable or a command does."""
    import shlex
    flat: list[str] = []
    for word in words:
        word = os.fsdecode(os.fspath(word))
        if re.search(r"[\s;&|()`$<>]", word):
            try:
                lex = shlex.shlex(word, posix=True, punctuation_chars=True)
                lex.whitespace_split = True
                flat += list(lex) + [";"]
            except ValueError:
                flat += word.split() + [";"]
        else:
            flat.append(word)
    for i, word in enumerate(flat):
        name = os.path.basename(word)
        if name == "ssh-copy-id":
            return True
        if name != "ssh-keygen":
            continue
        key, j = None, i + 1
        while j < len(flat) and flat[j] not in _SHELL_BREAKS:
            opt, j = flat[j], j + 1
            if not opt.startswith("-") or opt.startswith("--"):
                continue
            for k, letter in enumerate(opt[1:], 1):   # getopt: -lf <key>, -qf<key>, -t ed25519
                if letter in _KEYGEN_ARGS:
                    value = opt[k + 1:] or (flat[j] if j < len(flat) else "")
                    j += not opt[k + 1:]
                    if letter == "f":
                        key = value
                    break
        if key is None or key.startswith("~") or re.search(r"[$`]", key):
            return True
        path = os.path.normpath(os.path.join(os.fsdecode(os.fspath(cwd)), key))
        if any(p == h or p.startswith(h + os.sep) for p in (path, os.path.realpath(path)) for h in homes):
            return True
    return False


def _homes(home: str) -> list[str]:
    """`home` as written and resolved, longest first."""
    return sorted({os.path.normpath(home), os.path.realpath(home)}, key=len, reverse=True)


def home_path(path: str, home: str, homes=None) -> str | None:
    """`path` (relative ones from the working folder) as the recorder writes it: under `home` as written,
    also when it names it through its resolved form (`homes`: _homes(home)); None when it is not under it."""
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    for h in homes or _homes(home):
        if path == h or path.startswith(h + os.sep):
            return os.path.normpath(home) + os.path.normpath(path)[len(h):]
    return None


def recorded_writes(log: str | None) -> set[str]:
    try:
        return set(Path(log).read_text().split("\n")) - {""} if log else set()
    except OSError:
        return set()


def own_change(path: str, recorded) -> bool:
    """True when the session recorded `path`, a folder above it or a path below it."""
    return any(path == r or path.startswith(r + os.sep) or r.startswith(path + os.sep) for r in recorded)


def _changed_at(path: str) -> str:
    """When `path` last changed (its ctime; once removed, its folder's), by stat alone, in local time."""
    for p in (path, os.path.dirname(path)):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(os.lstat(p).st_ctime))
        except OSError:
            continue
    return "an unknown time"


def guarded_changes(changes, recorded, home: str = REAL_HOME) -> tuple[list[str], list[str]]:
    """The (path, how) changes of the account's credential files or ssh and gh folders, as (lines of the
    session's own, warning lines with the change time of the rest), by the tripwire's rule (own_change):
    a path the session recorded writing, or below or above one (the whole home handed to a child), is its
    own. `recorded` None (no readable write log) or a path outside `home` (the recorder cannot see it)
    counts as the session's."""
    own: list[str] = []
    other: list[str] = []
    for path, how in changes:
        seen = home_path(path, home)
        if recorded is None or seen is None or own_change(seen, recorded):
            own.append(f"  {path}: {how}")
        else:
            other.append(f"  {path}: {how} at {_changed_at(path)}")
    return own, other


def _write_log() -> str:
    """The session's write log, one per process however often this file is imported (a test loads it
    afresh), and one hook: sys.addaudithook cannot remove one. xdist's workers append to the log of the
    session that started them."""
    shared = sys.modules.get("_ttp_suite_writes")
    if shared is None:
        shared = sys.modules["_ttp_suite_writes"] = type(sys)("_ttp_suite_writes")
        log = os.environ.get("TTP_TEST_WRITE_LOG") if os.environ.get("PYTEST_XDIST_WORKER") else None
        if not log:
            import atexit
            import tempfile
            fd, log = tempfile.mkstemp(prefix="ttp-test-writes-")
            os.close(fd)
            atexit.register(lambda: [os.unlink(f) for f in (log, log + ".watch") if os.path.exists(f)])
            os.environ["TTP_TEST_WRITE_LOG"] = log
        shared.log, shared.recorder = log, _WriteRecorder(REAL_HOME, HOME_WATCHED + GUARDED, log, os.environ)
        try:   # an xdist worker: the credential files the session watches (pytest_sessionstart)
            shared.recorder.watch(Path(log + ".watch").read_text().split("\n")[:-1])
        except OSError:
            pass
        sys.addaudithook(shared.recorder)
    return shared.log


_WRITE_LOG = _write_log()


def _jobs(config, items) -> int:
    raw = os.environ.get("TTP_TEST_JOBS", "").strip()
    opt = config.option
    if (not hasattr(os, "fork") or opt.collectonly or getattr(opt, "usepdb", False) or opt.maxfail
            or getattr(opt, "capture", "fd") == "no" or getattr(opt, "stepwise", False)
            or getattr(opt, "stepwise_skip", False) or getattr(opt, "numprocesses", None)
            or os.environ.get("PYTEST_XDIST_WORKER")):
        return 1
    if raw:
        return max(1, int(raw)) if raw.isdigit() else 1
    if sum(1 for i in items if i.path.name.startswith(_SHARDED)) < MIN_ITEMS:
        return 1
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    return _auto_jobs(cpus, os.environ, load)


def _on_ci(env) -> bool:
    return env.get("CI", "").strip().lower() not in ("", "0", "false", "no")


def _auto_jobs(cpus: int, env, load: float | None = None) -> int:
    """The default worker count: one per CPU up to MAX_AUTO_JOBS on CI, else half the CPUs up to
    MAX_LOCAL_JOBS and no more than the CPUs the 1-minute `load` leaves free, so a run on a laptop or a
    shared host leaves room for everything else; never more than the host-wide test slots tt-project
    granted the run (TTP_TEST_SLOTS). A run that holds its grant (TTP_TEST_SLOTS_HELD: `ttp checks`, whose
    grant is already sized to the host's free cores) uses all of it, so a granted slot never sits idle."""
    slots = env.get("TTP_TEST_SLOTS", "").strip()
    cap = int(slots) if slots.isdigit() and int(slots) > 0 else cpus
    if env.get("TTP_TEST_SLOTS_HELD", "").strip().isdigit() and slots.isdigit() and int(slots) > 0:
        return max(1, min(cpus, cap))
    if _on_ci(env):
        return max(1, min(MAX_AUTO_JOBS, cpus, cap))
    free = cpus - int(load + 0.5) if load is not None else cpus
    return max(1, min(MAX_LOCAL_JOBS, cpus // 2, cap, free))


def _suite_line(wall: float, cpu: float, workers: int) -> str:
    return f"ttp-suite: wall {wall:.1f}s cpu {cpu:.1f}s workers {workers}"


def _slowest_lines(reports, n: int = SLOWEST) -> list[str]:
    """The `n` slowest test calls of a full-suite run's `reports` (MIN_ITEMS or more tt-project tests),
    slowest first; [] for a smaller run."""
    calls = [r for r in reports if getattr(r, "when", None) == "call" and isinstance(getattr(r, "duration", None), float)]
    if sum(1 for r in calls if Path(r.nodeid.split("::", 1)[0]).name.startswith(_SHARDED)) < MIN_ITEMS:
        return []
    top = sorted(calls, key=lambda r: -r.duration)[:n]
    return [f"ttp-suite: slowest {len(top)} test calls:"] + [f"ttp-suite: {r.duration:8.2f}s {r.nodeid}" for r in top]


SMOKE_LIST = Path(__file__).with_name("tt_project_smoke.txt")
SMOKE_MARK = "tt_smoke"


def smoke_ids(path: Path = SMOKE_LIST) -> set[str]:
    """The test ids SMOKE_LIST names (file::test, every parametrization included); '#' starts a comment."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    return {s for line in lines if (s := line.split("#", 1)[0].strip())}


def pytest_collection_modifyitems(config, items):
    """Mark the tests SMOKE_LIST names `tt_smoke`: `pytest -m tt_smoke` runs the smoke suite a landing waits
    for (delivery.push_checks); the rest run after landing (delivery.post_land_checks)."""
    if not (ids := smoke_ids()):
        return
    for item in items:
        if item.nodeid.split("[", 1)[0] in ids:
            item.add_marker(SMOKE_MARK)


@pytest.hookimpl(trylast=True)   # after pytest's own tmpdir plugin made the factory _use_tmpfs patches
def pytest_configure(config):
    config.addinivalue_line("markers", f"{SMOKE_MARK}: in the smoke suite a landing waits for ({SMOKE_LIST.name})")
    config._ttp_started = time.monotonic()
    _use_tmpfs(config)
    try:   # the real stderr: capture is suspended here, as for pytest's own faulthandler plugin
        config._ttp_dump_fd = os.dup(sys.stderr.fileno())
    except (AttributeError, OSError, ValueError):
        config._ttp_dump_fd = None


def stack_dump_s(config, path, environ=os.environ) -> float:
    """Seconds after which test file `path`'s running test dumps its stacks: STACK_DUMP_S (or
    TTP_TEST_STACK_DUMP_S) for a tt-project test file, else 0; 0 also when pytest's own
    faulthandler_timeout is set (it arms its own)."""
    if not Path(str(path)).name.startswith(_SHARDED):
        return 0.0
    try:
        if float(config.getini("faulthandler_timeout") or 0) > 0:
            return 0.0
    except (TypeError, ValueError):
        pass
    try:
        return max(0.0, float(environ.get("TTP_TEST_STACK_DUMP_S", STACK_DUMP_S)))
    except ValueError:
        return STACK_DUMP_S


def _signals_kept():
    """The Python-level signal handlers and the signal mask now; calling the result puts back any a
    test changed (run in the main thread, where pytest runs the tests)."""
    sigs = sorted(signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP})
    handlers = {s: h for s in sigs if (h := signal.getsignal(s)) is not None}
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])

    def restore() -> None:
        for s, h in handlers.items():
            try:
                if signal.getsignal(s) is not h:
                    signal.signal(s, h)
            except (OSError, ValueError, TypeError):
                pass
        if signal.pthread_sigmask(signal.SIG_BLOCK, []) != mask:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    return restore


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    import faulthandler
    t, fd = stack_dump_s(item.config, item.path), getattr(item.config, "_ttp_dump_fd", None)
    if t > 0 and fd is not None:
        faulthandler.dump_traceback_later(t, exit=False, file=fd)
    restore = _signals_kept()
    try:
        yield
    finally:
        restore()
        if t > 0 and fd is not None:
            faulthandler.cancel_dump_traceback_later()


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, config):
    started = getattr(config, "_ttp_started", None)
    if started is None or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    try:
        import resource
        cpu = sum(r.ru_utime + r.ru_stime for r in (resource.getrusage(resource.RUSAGE_SELF),
                                                     resource.getrusage(resource.RUSAGE_CHILDREN)))
    except ImportError:   # no resource module (Windows): this process's own CPU time only
        cpu = time.process_time()
    reports = [r for reps in terminalreporter.stats.values() for r in reps]
    for line in _slowest_lines(reports):
        terminalreporter.write_line(line)
    _save_durations(reports)
    terminalreporter.write_line(_suite_line(time.monotonic() - started, cpu, getattr(config, "_ttp_workers", 1)))


_STARTED = time.time()   # this session's start (reset at sessionstart): older processes are never its leaks


def _seconds(etime: str) -> int | None:
    """`ps` etime ([[dd-]hh:]mm:ss) in seconds, or None."""
    days, _, rest = etime.rpartition("-")
    try:
        total = 0
        for part in rest.split(":"):
            total = total * 60 + int(part)
        return total + int(days or 0) * 86400
    except ValueError:
        return None


def _ps() -> list[tuple[int, int, int, float, str]]:
    """(pid, ppid, pgid, start time, command line) of every process on the host; `ps` as on Linux and
    macOS. The start time is to the second (etime)."""
    # -ww: whole command lines, never cut to the terminal width (COLUMNS) as without a terminal
    r = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,etime=,command="], capture_output=True,
                       text=True, timeout=60)
    now, rows = time.time(), []
    for line in r.stdout.splitlines():
        parts = line.split(None, 4)
        age = _seconds(parts[3]) if len(parts) == 5 else None
        if age is not None and all(x.isdigit() for x in parts[:3]):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), now - age, parts[4]))
    return rows


def _ancestors(rows) -> set[int]:
    """This process and its ancestors."""
    parent = {row[0]: row[1] for row in rows}
    mine, pid = set(), os.getpid()
    while pid > 0 and pid not in mine:
        mine.add(pid)
        pid = parent.get(pid, 0)
    return mine


def leaked_processes(root: str | Path, since: float | None = None) -> list[tuple[int, int, str]]:
    """(pid, pgid, command line) of live processes whose command line names `root` (the path itself or
    anything below it) and that started after `since` (default: this session's start), other than
    this process and its ancestors. Processes from before the session (the shell that started pytest
    with --basetemp=root, say) are never counted: etime has whole seconds, so a process counts only from
    a second after `since`."""
    rows = _ps()
    mine = _ancestors(rows)
    since = _STARTED if since is None else since
    names = re.compile(re.escape(str(root).rstrip(os.sep)) + r"(?=[/\s'\"]|$)")
    return [(pid, pgid, cmd) for pid, _, pgid, start, cmd in rows
            if pid not in mine and start >= since + 1 and names.search(cmd)]


def reap(found: list[tuple[int, int, str]], root: str | Path, grace: float = 3.0,
         finder=None) -> list[tuple[int, int, str]]:
    """Stop the processes in `found` and their process groups: TERM, up to `grace` s, then KILL. A group
    this process or an ancestor belongs to is never signalled, only the process. Returns those of
    `found` still alive afterwards, as `finder(root)` (default leaked_processes) sees them."""
    finder = finder or leaked_processes
    rows = _ps()
    mine = _ancestors(rows)
    ours = {row[2] for row in rows if row[0] in mine} | {os.getpgrp()}
    left = found
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, pgid, _ in left:
            try:
                if pgid > 1 and pgid not in ours:
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + grace
        while True:
            left = [proc for proc in finder(root) or [] if proc[0] in {f[0] for f in found}]
            if not left or time.time() >= deadline:
                break
            time.sleep(0.1)
        if not left:
            break
    return left


@pytest.fixture
def reap_tmp_processes():
    """Returns reap(root): stops every process whose command line names `root` (and its group), and
    returns those still alive after TERM and KILL, normally []. For a test that detaches processes:
    call it from a finalizer that also runs when the test fails."""
    return lambda root: reap(leaked_processes(root), root)


def _proc_ancestors() -> set[int]:
    """This process and its ancestors, from /proc."""
    mine, pid = set(), os.getpid()
    while pid > 0 and pid not in mine:
        mine.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                pid = int(f.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return mine


def processes_in(root: str | Path) -> list[tuple[int, int, str]] | None:
    """(pid, pgid, command line) of this account's live processes whose working folder is `root` or
    below it, or whose command line names it, other than this process and its ancestors. Read from
    /proc (one pass, about 20 ms on a busy host, cheap enough for every test); None where there is no
    /proc (macOS): the session guard (leaked_processes) still covers those hosts."""
    if not os.path.isdir("/proc/self"):
        return None
    root = str(root).rstrip(os.sep)
    names = re.compile(re.escape(root.encode()) + rb"(?=[/\s'\"\x00]|$)")
    mine, found = _proc_ancestors(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) in mine:
            continue
        try:   # another account's process: unreadable here, and never ours to stop
            cwd = os.readlink(f"/proc/{name}/cwd")
            with open(f"/proc/{name}/cmdline", "rb") as f:
                cmd = f.read()
            pgid = os.getpgid(int(name))
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + os.sep) or names.search(cmd):
            found.append((int(name), pgid, cmd.rstrip(b"\x00").replace(b"\x00", b" ").decode(errors="replace")))
    return found


LEFT_GRACE_S = 1.0   # how long a test's leftover processes get to end on their own before they are stopped
# A run's own runner and the fake provider it started end by themselves once the run finishes; a test that
# returns as soon as the run's row says what it checks may leave them writing the run's last files. They
# get this much longer (waited on by their exit, so it costs nothing when they end at once) under load;
# anything else, or one still running after it, is stopped and reported as before.
SELF_ENDING_GRACE_S = 20.0
_SELF_ENDING = re.compile(r"-m ttp\.runner |ttp\.providers\.fake ")


@pytest.fixture(autouse=True)
def _reap_test_processes(request):
    """After each tt-project test that used tmp_path: a process still running in it (its working folder
    there, or its command line naming it) is stopped with its group (reap) and reported as an error of
    the test's teardown, so a test that failed before its own cleanup leaves nothing running and is
    named, rather than leaving detached drivers and hold keepers to the session's end or past it."""
    if not request.path.name.startswith(_SHARDED) or "tmp_path" not in request.fixturenames:
        yield
        return
    root = request.getfixturevalue("tmp_path")   # taken now: tmp_path then ends after this fixture
    yield
    deadline = time.time() + LEFT_GRACE_S
    while (found := processes_in(root)) and time.time() < deadline:
        time.sleep(0.1)
    deadline += SELF_ENDING_GRACE_S
    while found and all(_SELF_ENDING.search(cmd + " ") for _, _, cmd in found) and time.time() < deadline:
        time.sleep(0.1)
        found = processes_in(root)
    if not found:
        return
    left = reap(found, root, finder=processes_in)
    lines = [f"the test left {len(found)} process(es) running in its tmp_path {root} "
             f"(stopped now{'' if not left else f'; {len(left)} still alive'}):"]
    lines += [f"  pid {pid} (group {pgid}): {cmd[:300]}" for pid, pgid, cmd in found]
    pytest.fail("\n".join(lines), pytrace=False)


@pytest.fixture
def tmp_processes_in():
    """The per-test teardown's finder (processes_in), for a test whose processes end on their own a
    little after it (a stop's grace) to wait for them."""
    return processes_in


@pytest.fixture
def leaked_tmp_processes():
    """The session guard's finder (leaked_processes), for tests of the guard itself."""
    return leaked_processes


def pytest_sessionstart(session):
    global _STARTED
    _STARTED = time.time()
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        session.config._ttp_outside = _outside_snapshot(session.config)
        creds = session.config._ttp_creds = credential_marks()
        session.config._ttp_secret = folder_marks(secret_folders())
        session.config._ttp_host = _HOSTSTATE.Guard(tokens=_basetemp(session.config)) if _HOSTSTATE else None
        recorder = getattr(sys.modules.get("_ttp_suite_writes"), "recorder", None)
        if recorder:   # the providers' login files and gh's folder, wherever the account's env puts them
            recorder.watch(list((creds or {}).get("kinds") or ()) + secret_folders())
            Path(_WRITE_LOG + ".watch").write_text("".join(p + "\n" for p in recorder.watched))


def _ledger_record(what: str) -> str:
    """hoststate.record in a child with the account's real HOME: the session's own HOME is a temp one."""
    script = "import sys\nsys.path.insert(0, sys.argv[1])\nfrom ttp import hoststate\nprint(hoststate.record(sys.argv[2]))"
    try:
        r = subprocess.run([sys.executable, "-c", script, str(_RUNTIME), what], capture_output=True, text=True,
                           timeout=60, env={**os.environ, "HOME": REAL_HOME})
        return r.stdout.strip() or f"not recorded in the machine change ledger: {r.stderr.strip()[-200:]}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"not recorded in the machine change ledger: {e}"


def host_state_lines(guard) -> tuple[bool, list[str]]:
    """The session's host-state verdict (hoststate.Guard.end), its ledger record made with the real HOME."""
    if guard is None:
        return False, []
    _HOSTSTATE.record = _ledger_record
    # The session's own writes under the real unit folder prove a unit is its leak (a whole-home record does not)
    return guard.end(recorded_writes(_WRITE_LOG) if _WRITE_LOG and os.path.exists(_WRITE_LOG) else None)


def _basetemp(config) -> tuple[str, ...]:
    """The session's temp folder: a unit or crontab line naming it is the session's own leak."""
    try:
        return (str(config._tmp_path_factory.getbasetemp()),)
    except Exception:
        return ()


def pytest_sessionfinish(session, exitstatus):
    changed, said = host_state_lines(getattr(session.config, "_ttp_host", None))
    if said:
        _report(session, said, red=changed)
    if changed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    before = getattr(session.config, "_ttp_outside", None)
    recorded = recorded_writes(_WRITE_LOG) if _WRITE_LOG and os.path.exists(_WRITE_LOG) else None
    written, other = written_outside(before, _outside_snapshot(session.config), recorded) if before else ([], [])
    if written:
        _report(session, ["tests wrote outside their temp folders (the account's home folder or the "
                          "repository the suite runs from):"] + written)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if other:
        _report(session, ["ttp-suite: warning: the account's home folder changed while the tests ran, by no "
                          "write of this session (another process on the host; not a failure):"] + other, red=False)
    creds = getattr(session.config, "_ttp_creds", None)
    changed, other = guarded_changes(credential_marks(creds) or [] if creds else [], recorded)
    if changed:
        _report(session, ["the account's credential files changed while the tests ran (stat only: size, mtime, "
                          "inode; a test must use its temp HOME):"] + changed)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if other:
        _report(session, ["ttp-suite: warning: the account's credential files changed while the tests ran, by no "
                          "write of this session (another process on the host; not a failure):"] + other, red=False)
    secret = getattr(session.config, "_ttp_secret", None)
    moved, other = guarded_changes(folder_diff(secret, folder_marks(secret_folders())) if secret is not None else [],
                                   recorded)
    if moved:
        _report(session, ["the account's ssh or gh config folder changed while the tests ran (a test must use "
                          "its temp HOME, and pass ssh and ssh-keygen paths under it):"] + moved)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if other:
        _report(session, ["ttp-suite: warning: the account's ssh or gh config folder changed while the tests ran, "
                          "by no write of this session (another process on the host; not a failure):"] + other,
                red=False)
    if not any(i.path.name.startswith(_SHARDED) for i in session.items):
        return
    root = getattr(getattr(session.config, "_tmp_path_factory", None), "_basetemp", None)
    if root is None:   # no test used a temp path
        return
    found = leaked_processes(root)
    if not found:
        return
    left = reap(found, root)
    lines = [f"tests left {len(found)} process(es) running that name this session's temp root {root} "
             f"(stopped now{'' if not left else f'; {len(left)} still alive'}):"]
    lines += [f"  pid {pid} (group {pgid}): {cmd[:300]}" for pid, pgid, cmd in found]
    _report(session, lines)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _report(session, lines: list[str], red: bool = True) -> None:
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    for line in lines:
        if tr:
            tr.write_line(line, red=red, yellow=not red)
        else:
            print(line, file=sys.stderr)


def _units(items) -> list[list]:
    """Each tt-project test is a unit of its own; every other test goes in one unit, kept in order."""
    rest = [i for i in items if not i.path.name.startswith(_SHARDED)]
    return ([rest] if rest else []) + [[i] for i in items if i.path.name.startswith(_SHARDED)]


DURATIONS = Path(os.environ.get("TTP_TEST_DURATIONS") or
                 Path(os.environ.get("TMPDIR") or "/tmp") / f"ttp-test-durations-{getattr(os, 'getuid', lambda: 0)()}.json")
DURATIONS_KEEP = 50000
os.environ["TTP_TEST_DURATIONS"] = "off"   # a suite a test starts keeps its own order and saves nothing


def _durations(path: Path | None = None) -> dict[str, float]:
    """Each test id's last measured seconds (setup, call and teardown) on this host; {} when none."""
    if str(path or DURATIONS) == "off":
        return {}
    try:
        got = json.loads(Path(path or DURATIONS).read_text())
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in got.items() if isinstance(v, (int, float))} if isinstance(got, dict) else {}


def _save_durations(reports, path: Path | None = None) -> None:
    """Merge a full run's measured test times into DURATIONS (atomic; a failed write only loses them)."""
    took: dict[str, float] = {}
    for r in reports:
        if isinstance(getattr(r, "duration", None), float) and getattr(r, "when", None) in ("setup", "call", "teardown"):
            took[r.nodeid] = took.get(r.nodeid, 0.0) + r.duration
    if str(path or DURATIONS) == "off" or \
            sum(1 for k in took if Path(k.split("::", 1)[0]).name.startswith(_SHARDED)) < MIN_ITEMS:
        return
    path = Path(path or DURATIONS)
    merged = {**_durations(path), **{k: round(v, 2) for k, v in took.items()}}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(dict(list(merged.items())[-DURATIONS_KEEP:])))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _order(units, durations: dict[str, float]) -> list[int]:
    """The units' indexes, slowest first by `durations`; untimed tests count UNKNOWN_S, ties keep order."""
    if not durations:
        return list(range(len(units)))
    cost = [sum(durations.get(i.nodeid, UNKNOWN_S) for i in u) for u in units]
    return sorted(range(len(units)), key=lambda n: -cost[n])


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session):
    if session.testsfailed and not session.config.option.continue_on_collection_errors:
        return None   # pytest's own loop reports the collection errors
    units = _units(session.items)
    jobs = min(_jobs(session.config, session.items), len(units))
    if jobs < 2 or len(units) * 4 > 60000:   # the queue must fit in a pipe's buffer
        return None
    session.config._ttp_workers = jobs
    _run_forked(session, units, jobs)
    return True


def _send(fd: int, kind: str, **kw) -> None:
    data = (json.dumps({"kind": kind, **kw}) + "\n").encode()
    while data:
        data = data[os.write(fd, data):]


def _fd_id(fd: int):
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return st.st_dev, st.st_ino


class _PipeGuard:
    """In a worker: the identity of its two pipe fds at fork. A test that closes one, or closes it and
    opens something else on the same number, made the worker read or write the wrong file and exit 0
    mid-run, its open test lost unnamed; check() names that test and exits FD_LOST_EXIT instead."""

    def __init__(self, work_r: int, out_w: int):
        self.fds = {"work_r": (work_r, _fd_id(work_r)), "out_w": (out_w, _fd_id(out_w))}

    def check(self, nodeid: str) -> None:
        bad = [f"{name} (fd {fd}) {'closed' if _fd_id(fd) is None else 'replaced'}"
               for name, (fd, want) in self.fds.items() if _fd_id(fd) != want]
        if not bad:
            return
        why = (f"test {nodeid} closed or replaced the test worker's pipe fds: {', '.join(bad)}; "
               f"the worker exits with status {FD_LOST_EXIT}")
        try:
            os.write(2, f"ttp-worker {os.getpid()}: {why}\n".encode())
        except OSError:
            pass
        out_w, want = self.fds["out_w"]
        if _fd_id(out_w) == want:
            try:
                _send(out_w, "fdlost", nodeid=nodeid, why=why)
            except OSError:
                pass
        os._exit(FD_LOST_EXIT)


class _Forward:
    """In a worker: sends what the main process's reporters need over the worker's pipe, after
    checking that the test that just ran left the worker's pipe fds alone."""

    def __init__(self, config, fd: int, guard: _PipeGuard | None = None):
        self.config, self.fd, self.guard = config, fd, guard

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logstart(self, nodeid, location):
        _send(self.fd, "start", nodeid=nodeid, location=list(location))

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if self.guard:
            self.guard.check(report.nodeid)
        data = self.config.hook.pytest_report_to_serializable(config=self.config, report=report)
        _send(self.fd, "report", data=data)

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logfinish(self, nodeid, location):
        if self.guard:
            self.guard.check(nodeid)
        _send(self.fd, "finish", nodeid=nodeid, location=list(location))

    @pytest.hookimpl(trylast=True)
    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        cat = warning_message.category
        _send(self.fd, "warning", message=str(warning_message.message), when=when, nodeid=nodeid,
              category=f"{cat.__module__}.{cat.__qualname__}", filename=str(warning_message.filename),
              lineno=warning_message.lineno, location=list(location) if location else None)


def _worker(session, units, work_r: int, out_w: int) -> None:
    config = session.config
    tr = config.pluginmanager.get_plugin("terminalreporter")
    if tr is not None:   # the main process prints; a worker's reporter writes nowhere
        from _pytest._io import TerminalWriter
        tr._tw = TerminalWriter(open(os.devnull, "w"))
    config.pluginmanager.register(_Forward(config, out_w, _PipeGuard(work_r, out_w)), "ttp-forward")

    def take():
        raw = os.read(work_r, 4)
        if len(raw) < 4:
            return None
        n = struct.unpack("!I", raw)[0]
        _send(out_w, "take", unit=n)
        return units[n]

    cur = take()
    while cur:
        nxt = take()   # the next item decides which fixtures this one's teardown keeps
        for j, item in enumerate(cur):
            nextitem = cur[j + 1] if j + 1 < len(cur) else (nxt[0] if nxt else None)
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        cur = nxt


def _warning(msg: dict):
    mod, _, name = msg["category"].rpartition(".")
    try:
        cat = getattr(importlib.import_module(mod), name)
    except Exception:
        cat = Warning
    if not (isinstance(cat, type) and issubclass(cat, Warning)):
        cat = Warning
    return warnings.WarningMessage(cat(msg["message"]), cat, msg["filename"], msg["lineno"])


def _own_basetemp(factory, n: int) -> None:
    """Gives forked worker n its own temp root, a folder in the session's: workers sharing one root
    raced for tmp_path's numbered folders (mkdir EEXIST), and pytest gives up after 10 lost races
    ('could not create numbered dir ... after 10 tries'), which failed tests under load."""
    root = factory.getbasetemp() / f"w{n}"
    root.mkdir(mode=0o700, exist_ok=True)
    factory._basetemp = root


def _exit_code(status):
    return os.WEXITSTATUS(status) if status is not None and os.WIFEXITED(status) else None


def _run_forked(session, units, jobs: int) -> None:
    config, hook = session.config, session.config.hook
    factory = getattr(config, "_tmp_path_factory", None)
    if factory is not None:
        factory.getbasetemp()   # made once here; each worker takes its own folder in it
    work_r, work_w = os.pipe()
    os.write(work_w, b"".join(struct.pack("!I", n) for n in _order(units, _durations())))
    os.close(work_w)
    sys.stdout.flush()
    sys.stderr.flush()
    workers: dict[int, dict] = {}
    for n in range(jobs):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                os.close(r)
                for other in workers.values():
                    os.close(other["fd"])
                if not _on_ci(os.environ):
                    try:
                        os.nice(WORKER_NICE)   # the tests and what they start yield to other work
                    except (AttributeError, OSError):
                        pass
                if factory is not None:
                    _own_basetemp(factory, n)
                _worker(session, units, work_r, w)
            except BaseException:   # noqa: BLE001 - the main process reports what this worker left
                code = 1
            finally:
                os._exit(code)
        os.close(w)
        workers[pid] = {"fd": r, "buf": b"", "taken": [], "open": None}
    os.close(work_r)
    finished: set[str] = set()
    sel = selectors.DefaultSelector()
    for pid, wk in workers.items():
        os.set_blocking(wk["fd"], False)
        sel.register(wk["fd"], selectors.EVENT_READ, pid)

    def handle(wk, msg):
        kind = msg["kind"]
        if kind == "take":
            wk["taken"].append(msg["unit"])
        elif kind == "start":
            wk["open"] = msg["nodeid"]
            hook.pytest_runtest_logstart(nodeid=msg["nodeid"], location=tuple(msg["location"]))
        elif kind == "report":
            rep = hook.pytest_report_from_serializable(config=config, data=msg["data"])
            if isinstance(rep.longrepr, list):   # a skip's (path, line, reason), a list after JSON
                rep.longrepr = tuple(rep.longrepr)
            hook.pytest_runtest_logreport(report=rep)
        elif kind == "finish":
            wk["open"] = None
            finished.add(msg["nodeid"])
            hook.pytest_runtest_logfinish(nodeid=msg["nodeid"], location=tuple(msg["location"]))
        elif kind == "fdlost":
            wk["fdlost"] = msg["why"]
        elif kind == "warning":
            hook.pytest_warning_recorded.call_historic(kwargs=dict(
                warning_message=_warning(msg), when=msg["when"], nodeid=msg["nodeid"],
                location=tuple(msg["location"]) if msg["location"] else None))

    def read(pid, wk, exited=False) -> None:
        """Hands on what the worker wrote; at EOF, or once it exited and nothing is left (a process a
        test started may still hold the pipe open), its pipe is closed and the worker reaped."""
        while True:
            try:
                chunk = os.read(wk["fd"], 1 << 16)
            except BlockingIOError:
                chunk = None
            if chunk:
                *lines, wk["buf"] = (wk["buf"] + chunk).split(b"\n")
                for line in lines:
                    handle(wk, json.loads(line))
                continue
            if chunk is None and not exited:
                return
            sel.unregister(wk["fd"])
            os.close(wk["fd"])
            if "status" not in wk:
                wk["status"] = os.waitpid(pid, 0)[1]
            return

    try:
        while sel.get_map():
            for key, _ in sel.select(timeout=1.0):
                read(key.data, workers[key.data])
            for pid, wk in workers.items():
                if "status" in wk or wk["fd"] not in {k.fd for k in sel.get_map().values()}:
                    continue
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    wk["status"] = status
                    read(pid, wk, exited=True)
    except BaseException:
        for pid in workers:
            try:
                if "status" not in workers[pid]:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
            except OSError:
                pass
        raise
    # A worker that died (a crash, a kill) fails every test it took and did not finish.
    from _pytest.reports import TestReport
    for pid, wk in workers.items():
        for n in wk["taken"]:
            for item in units[n]:
                if item.nodeid in finished:
                    continue
                why = (f"the test worker process {pid} ended (wait status {wk.get('status')}) before this "
                       "test finished")
                if wk.get("fdlost") or _exit_code(wk.get("status")) == FD_LOST_EXIT:
                    why += ": " + (wk.get("fdlost") or f"test {wk['open'] or 'unknown'} closed or replaced "
                                   f"the worker's pipe fds (exit status {FD_LOST_EXIT})")
                if wk["open"] != item.nodeid:
                    hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
                hook.pytest_runtest_logreport(report=TestReport(
                    item.nodeid, item.location, {}, "failed", why, "call", duration=0.0))
                hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
                finished.add(item.nodeid)
    lost = [i for u in units for i in u if i.nodeid not in finished]
    for item in lost:   # taken by no worker: every worker died before reaching it
        hook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        hook.pytest_runtest_logreport(report=TestReport(
            item.nodeid, item.location, {}, "failed", "no test worker was left to run this test", "call",
            duration=0.0))
        hook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    if session.shouldfail:
        raise session.Failed(session.shouldfail)
    if session.shouldstop:
        raise session.Interrupted(session.shouldstop)


# On Linux the session's temp files go to a RAM disk when one is free: the suite writes gigabytes of
# short-lived git repositories, and on disk that costs about a third of its CPU and wall time.
TMPFS = "/dev/shm"
TMPFS_MIN_FREE = 2 << 30


def _tmpfs_root(given_basetemp, environ, platform: str = sys.platform, path: str = TMPFS,
                min_free: int = TMPFS_MIN_FREE, mounts: str = "/proc/self/mounts") -> str | None:
    """The tmpfs folder to hold this session's temp files, or None to keep pytest's default: not
    Linux, on CI, a temp root the caller chose (--basetemp, PYTEST_DEBUG_TEMPROOT, TMPDIR/TMP/TEMP,
    or a `ttp checks` run, whose TMPDIR is scratch reclaimed with its allocation), TTP_TEST_TMPFS=0,
    or no writable tmpfs at `path` with `min_free` bytes free."""
    if (not platform.startswith("linux") or given_basetemp is not None
            or any(environ.get(k) for k in ("PYTEST_DEBUG_TEMPROOT", "TMPDIR", "TMP", "TEMP",
                                            "TTP_CHECK_OUTPUT_DIR", "TTP_CHECKS_TMP_FD"))
            or any(environ.get(k, "").strip().lower() not in ("", "0", "false") for k in ("CI", "GITHUB_ACTIONS"))
            or environ.get("TTP_TEST_TMPFS", "").strip() == "0"):
        return None
    try:
        real = os.path.realpath(path)
        with open(mounts, encoding="utf-8") as f:
            fstype = {p[1]: p[2] for p in (line.split() for line in f) if len(p) > 2}.get(real)
        st = os.statvfs(real)
    except OSError:
        return None
    if fstype != "tmpfs" or not os.access(real, os.W_OK | os.X_OK) or st.f_bavail * st.f_frsize < min_free:
        return None
    return real


def _use_tmpfs(config) -> None:
    """Puts the session's base temp under the tmpfs _tmpfs_root picks, in this process only: an
    exported PYTEST_DEBUG_TEMPROOT would also move the temp root of a nested pytest that a test
    starts. pytest still numbers, retains and removes the folders as usual. config._ttp_tmpfs tells
    the _short_tmp fixture the same root."""
    factory = getattr(config, "_tmp_path_factory", None)
    root = _tmpfs_root(getattr(factory, "_given_basetemp", None), os.environ) if factory is not None else None
    config._ttp_tmpfs = root
    if root is None:
        return
    import tempfile
    default = factory.getbasetemp

    def getbasetemp():
        if factory._basetemp is not None:
            return factory._basetemp
        saved, tempfile.tempdir = tempfile.tempdir, root   # pytest's temp root is tempfile.gettempdir()
        try:
            return default()
        finally:
            tempfile.tempdir = saved
    factory.getbasetemp = getbasetemp

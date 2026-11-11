# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""No installs into the shared user site (~/.local) on a host that runs several projects.

Every project of one account on a host imports the same ~/.local, so one `pip install --user` there
changes every project's packages (an upgraded test runner there once broke every project's tests). On a
host whose `ttp list` shows more than one project, or wherever a project sets guards.user_site "on":

- every worker, reviewer and coordinator run, and the checks the daemon starts, get
  PIP_REQUIRE_VIRTUALENV=1, PIP_USER=0 and PIP_BREAK_SYSTEM_PACKAGES=0 (`env`), so pip itself refuses an
  install outside a virtualenv on every provider, model-free, also inside scripts;
- the harness hook (hook.pre_tool_use, which the Claude Code, Codex and Cursor hooks, the stream guard
  and the coordinator's tools all ask) refuses a command that installs into the user site or turns
  those settings off (`refusal`);
- each refusal is counted on one queued ride-along event per run to the project's coordinator
  (`flag`): a flag for the coordinator, never a user alert.

Installs inside a venv and pip download, list, show and freeze stay allowed.
"""
from __future__ import annotations

import json
import os
import re
import time

KEY = "guards.user_site"   # "auto" (default): on when the host runs several projects; "on": always
ENV = "TTP_NO_USER_SITE"   # "1" in a run where the guard is on; the hook reads it
EVENT_KIND = "user_site_install"
PIP_ENV = {"PIP_REQUIRE_VIRTUALENV": "1", "PIP_USER": "0", "PIP_BREAK_SYSTEM_PACKAGES": "0"}
# pip settings a command may not turn back on or off (as variables or as `pip config` names)
GUARDED_VARS = set(PIP_ENV)
# pip settings that send an install to a directory: refused when it lies under a .local
TARGET_VARS = {"PIP_TARGET", "PIP_PREFIX", "PIP_ROOT"}
CONFIG_NAMES = {"user", "require-virtualenv", "require_virtualenv", "break-system-packages",
                "break_system_packages"}
# install options that write outside a venv or ignore the environment (pip takes an unambiguous prefix)
BANNED_OPTS = ("--user", "--break-system-packages", "--isolated")
UV_BANNED_OPTS = ("--user", "--break-system-packages", "--system")
# pip commands that install nothing
READ_ONLY = {"download", "list", "show", "freeze", "check", "hash", "index", "inspect", "search", "help", "debug",
             "cache", "wheel", "lock"}
LOCAL_SITE_RE = re.compile(r"(^|[/=])\.local(/|$)")
PIP_RE = re.compile(r"pip(\d+(\.\d+)?)?")
PYTHON_RE = re.compile(r"python(\d+(\.\d+)?)?")
# commands that run another command for each input: find's actions and the words after xargs or parallel
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
PARALLEL_VALUE_OPTS = {"-j", "--jobs", "-P", "-S", "--sshlogin", "--slf", "--sshloginfile", "-a", "--arg-file", "-n",
                       "--max-args", "-N", "-I", "-C", "--colsep", "-d", "--delimiter", "-E", "--joblog", "--results",
                       "--res", "--tmpdir", "--workdir", "--wd", "--env", "--timeout", "--halt", "--delay", "--retries",
                       "--tag-string", "--tagstring", "--memfree", "--load", "-s", "--max-chars", "-J", "--profile",
                       "--basefile", "--bf", "--return"}
ASSIGN_RE = re.compile(r"^(\w+)\+?=(.*)$", re.S)
TRUE = {"1", "true", "yes", "on", "y", "t"}
WHY = ("installs into the shared user site (~/.local) or outside a virtualenv are refused on a host that runs "
       "several projects ({key}): every project there imports the same ~/.local, so one install changes all "
       "their packages. Install into a venv this project owns: `python3 -m venv <dir>` (or `uv venv <dir>`), "
       "then `<dir>/bin/python -m pip install ...`")


def several_projects() -> bool:
    """Whether `ttp list` on this host shows more than one project."""
    from .project import load_registry
    return len(load_registry().get("projects") or {}) > 1


def on(cfg: dict | None) -> bool:
    """Whether the guard holds for a project with settings `cfg` on this host."""
    g = (cfg or {}).get("guards")
    setting = g.get("user_site", "auto") if isinstance(g, dict) else "auto"
    if setting is True or str(setting).lower() == "on":
        return True
    try:
        return several_projects()
    except Exception:   # an unreadable registry: the safe side
        return True


def env(cfg: dict | None) -> dict[str, str]:
    """The variables a run gets: pip's own refusal and the hook's marker when the guard holds, else a
    blank marker (never one inherited from the daemon's environment) and pip left as it is."""
    return {**PIP_ENV, ENV: "1"} if on(cfg) else {ENV: ""}


def _bad_assign(word: str) -> bool:
    m = ASSIGN_RE.match(word)
    if m and m[1] in TARGET_VARS:
        return bool(LOCAL_SITE_RE.search(m[2].strip("'\"")))
    if not m or m[1] not in GUARDED_VARS:
        return False
    return m[2].strip("'\"").lower() not in (TRUE if m[1] == "PIP_REQUIRE_VIRTUALENV" else {"0", "false", "no", "off", "n", "f"})


def _banned(arg: str, opts: tuple[str, ...]) -> bool:
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= 4 and any(o.startswith(name) for o in opts)


def _pip_args(argv: list[str]) -> tuple[list[str], tuple[str, ...]] | None:
    """(pip's arguments, its banned install options) when `argv` runs pip, else None."""
    name = os.path.basename(argv[0])
    if PIP_RE.fullmatch(name):
        return argv[1:], BANNED_OPTS
    if name == "uv" and argv[1:2] == ["pip"]:
        return argv[2:], UV_BANNED_OPTS
    got = _py_module(argv)
    return (got[1], BANNED_OPTS) if got and got[0] == "pip" else None


def _py_module(argv: list[str]) -> tuple[str, list[str]] | None:
    """(the module, its arguments) when `argv` runs `python -m <module>`, else None."""
    if not PYTHON_RE.fullmatch(os.path.basename(argv[0])):
        return None
    skip = False
    for i, a in enumerate(argv[1:], 1):
        if skip:
            skip = False
        elif m := re.fullmatch(r"-[bBdEhiIOPqRsSuvV]*m(.*)", a):
            return (m[1], argv[i + 1:]) if m[1] else (argv[i + 1] if i + 1 < len(argv) else "", argv[i + 2:])
        elif a in ("-X", "-W"):   # their value is the next word
            skip = True
        elif not a.startswith("-") or a.startswith("-c"):   # a script or inline code: no module
            return None
    return None


def _segment_refused(raw: list[str]) -> bool:
    from .hook import _strip_prefix
    argv = _strip_prefix(raw)
    got = _pip_args(argv) if argv else None
    sub = next((a for a in got[0] if not a.startswith("-")), "") if got else ""
    # A pip command that installs nothing (pip refuses even `pip download` outside a virtualenv) may
    # carry its own PIP_REQUIRE_VIRTUALENV=0.
    if not (got and sub in READ_ONLY):
        i = 0
        while i < len(raw) and ASSIGN_RE.match(raw[i]):
            if _bad_assign(raw[i]):
                return True
            i += 1
        head = os.path.basename(raw[i]) if i < len(raw) else ""
        rest = raw[i + 1:]
        if head in ("export", "declare", "typeset", "readonly", "env", "local") and any(map(_bad_assign, rest)):
            return True
        if head == "unset" and GUARDED_VARS & set(rest):
            return True
        if head == "env" and any(a in ("-u", "--unset") and b in GUARDED_VARS for a, b in zip(rest, rest[1:])):
            return True
        if head == "env" and any(a.split("=", 1)[-1] in GUARDED_VARS or a[2:] in GUARDED_VARS
                                 for a in rest if a.startswith(("--unset=", "-u"))):
            return True
    if not argv:
        return False
    if os.path.basename(argv[0]) == "easy_install" or (
            "setup.py" in map(os.path.basename, argv) and "install" in argv):
        return any(_banned(a, ("--user",)) for a in argv)
    mod = _py_module(argv)
    if mod and mod[0] == "ensurepip":
        # ensurepip drops the PIP_* settings: the run's PIP_REQUIRE_VIRTUALENV does not stop it
        return any(_banned(a, ("--user",)) or (LOCAL_SITE_RE.search(a) and _local_target(mod[1], a)) for a in mod[1])
    if not got:
        return False
    args, banned = got
    if sub == "install":
        # sudo and env -i drop the run's PIP_* settings: a bare pip there may fall back to the user site
        dropped = "/" not in argv[0] and any(os.path.basename(w) == "sudo" or w in ("-i", "--ignore-environment")
                                             for w in raw[:len(raw) - len(argv)])
        return dropped or any(_banned(a, banned) or (LOCAL_SITE_RE.search(a) and _local_target(args, a))
                              for a in args)
    if sub == "config":
        words = [a for a in args if not a.startswith("-")]
        if words[1:2] in (["set"], ["unset"]) and len(words) > 2:
            name = words[2].rsplit(".", 1)[-1].lower()
            return name in CONFIG_NAMES or (name in ("target", "prefix", "root") and words[1] == "set"
                                            and any(LOCAL_SITE_RE.search(w) for w in words[3:4]))
    return False


def _local_target(args: list[str], arg: str) -> bool:
    """`arg` is the target of --target, --prefix or --root (`--target=x` or `--target x`) and lies under .local."""
    if arg.startswith(("--target=", "--prefix=", "--root=")) or re.match(r"-t.", arg):
        return True
    i = args.index(arg)
    return i > 0 and args[i - 1] in ("--target", "--prefix", "--root", "-t")


def _nested(raw: list[str]) -> list[list[str]]:
    """The commands `raw` runs for each input: find's -exec, -execdir, -ok and -okdir, and what follows xargs
    or parallel (with their options)."""
    from .hook import _strip_prefix, _xargs_takes_next
    out = []
    argv = _strip_prefix(raw)
    pre = len(raw) - len(argv)
    k = next((i for i, w in enumerate(raw[:pre]) if os.path.basename(w) == "xargs"), None)
    if k is not None:   # `xargs env PIP_USER=1 pip install`: the words after xargs keep their assignments
        i = k + 1
        while i < len(raw) and raw[i].startswith("-") and raw[i] != "-":
            i += 2 if _xargs_takes_next(raw[i]) else 1
        out.append(raw[i:])
    name = os.path.basename(argv[0]) if argv else ""
    if name == "find":
        for i, w in enumerate(argv):
            if w in FIND_EXEC:
                out.append(argv[i + 1:next((j for j in range(i + 1, len(argv)) if argv[j] in (";", "+")), len(argv))])
    elif name == "parallel":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in PARALLEL_VALUE_OPTS else 1
        out.append(argv[i:next((j for j in range(i, len(argv)) if argv[j].startswith(":::")), len(argv))])
    return [s for s in out if s]


def _refused(raw: list[str], depth: int) -> bool:
    from .hook import SHELLS, _strip_prefix
    if _segment_refused(raw):
        return True
    argv = _strip_prefix(raw)
    name = os.path.basename(argv[0]) if argv else ""
    inner = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
                 None) if name in SHELLS else " ".join(argv[1:]) if name == "eval" else None
    if depth >= 3:
        return False
    return bool(inner and refusal(inner, depth + 1)) or any(_refused(s, depth + 1) for s in _nested(raw))


def refusal(commands: str, depth: int = 0) -> str | None:
    """Why `commands` is refused: an install into the user site, past the virtualenv rule, or a change to
    the variables that hold it. None when fine (a venv install, pip list, show, download, freeze)."""
    from .hook import _segments
    for raw in _segments(commands, strip=False):
        if _refused(raw, depth):
            return WHY.format(key=KEY)
    return None


def flag(cmd: str, now: float | None = None) -> str | None:
    """Count a refused install on this run's queued ride-along event to the project's coordinator; a new
    event once the coordinator has read the last one. Returns "raised", "counted" or None (no project)."""
    if not os.environ.get("TTP_PROJECT"):
        return None
    from .pagesev import RIDE_ALONG
    from .project import Project
    now = time.time() if now is None else now
    run = os.environ.get("TTP_RUN_ID") or os.path.basename(os.environ.get("TTP_RUN_DIR", "").rstrip("/"))
    raw_task = os.environ.get("TTP_TASK") or ""
    task = int(raw_task) if raw_task.isdigit() else None
    fp = f"{EVENT_KIND}:{run}"
    db = Project(os.environ["TTP_PROJECT"]).db
    prev = db.one("SELECT id, data FROM events WHERE fingerprint=? AND status='queued' ORDER BY id DESC LIMIT 1", (fp,))
    who = f"task #{task}" if task else f"run {run}"
    if prev:
        try:
            data = json.loads(prev["data"] or "{}")
        except ValueError:
            data = {}
        n = int(data.get("count") or 1) + 1
        db.x("UPDATE events SET data=?, text=? WHERE id=?",
             (json.dumps({**data, "count": n, "last": now, "command": cmd[:300]}),
              f"{who} tried {n} installs into the shared user site, refused ({KEY}); last: {cmd[:300]}", prev["id"]))
        return "counted"
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,data,status,task) VALUES(?,?,?,?,?,?,?,?,?)",
         (now, f"task:{task}" if task else "hook", EVENT_KIND, fp, "normal",
          f"{who} tried an install into the shared user site, refused ({KEY}): {cmd[:300]}. "
          "Check it uses a venv of its own (python3 -m venv <dir>).",
          json.dumps({RIDE_ALONG: True, "count": 1, "last": now, "command": cmd[:300]}), "queued", task))
    return "raised"

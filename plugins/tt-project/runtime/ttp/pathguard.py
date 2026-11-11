# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Credential folders no run writes: ~/.ssh, gh's config dir, the other stores in DEFAULT_PATHS and every
path in guards.protected_paths.

Runs are unsandboxed, so one tool call can overwrite a login (a check routed to a chat model once wrote
over gh's hosts file and logged gh out for every project on the host). refusal() is asked by the worker
hook (hook.pre_tool_use) before every other check, and by a full-tools coordinator (coordtools), for
each tool call; the runner-side tool guard of runs without the hook asks hook.pre_tool_use too. It
refuses any create, change, move, chmod or delete it can read under a protected path:
- Edit, Write, MultiEdit and NotebookEdit naming one, a Codex patch (`*** Add/Update/Delete File:`,
  `*** Move to:`) touching one, and an MCP tool whose name is not read-only given one as an argument;
- in a shell command: a redirect (`>`, `>>`, `&>`) to one, tee, cp, install, rsync, scp and ln targets,
  dd of=, curl and wget output files, unzip -d, sed -i and perl -i, and rm, mv, chmod, chown, touch,
  mkdir, truncate, shred or unlink on one (rm, mv and a recursive chmod or chown also on a folder holding
  one); the commands that rewrite a store themselves (`gh auth login`, `gh config set`, `docker login`,
  `aws configure`, `kubectl config set-*`, `git credential approve`, ssh-keygen writing a key or
  known_hosts); interpreter code (python, perl, node, ruby ...: one-liners, heredocs, the script files a
  command runs) that writes one: Python's write calls by their target, so reading one passes, other code
  (or Python too dynamic to follow) when it names one and writes anything; words of the line outside the
  code count only when the code reads its input; the same inside `sh -c` and shell scripts.
Words are read after ~, $HOME, ${HOME}, $XDG_CONFIG_HOME and $GH_CONFIG_DIR are expanded with the run's
environment and symlinks resolved, and the account's own home counts besides HOME. Reads pass as before.
Text ssh runs on another host is read too: its quoted remote command, a file or heredoc fed to its stdin
(`ssh host bash < f`, `cat f | ssh host bash`, `<<EOF`) and a file scp or rsync copies there and the remote
command then runs. It is checked by the same rules with ~ and $HOME as the remote user's home, against a narrow
list (REMOTE_PATHS: the default keys, ssh config and authorized_keys, where appending a key passes).
Known gap: a path a program builds at run time from parts no word names (an argument read from a file).

The list is DEFAULT_PATHS plus guards.protected_paths of the account settings and of the project
(a JSON list; ~ and the variables above expand): a setting adds paths, never removes a default.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import os
import pwd
import re
import shlex
from pathlib import Path

DEFAULT_PATHS = ("~/.ssh", "~/.config/gh", "~/.git-credentials", "~/.netrc", "~/.aws", "~/.docker/config.json",
                 "~/.kube")
VARS = ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")
VAR_RE = re.compile(r"\$\{?(" + "|".join(VARS) + r")\}?(?!\w)")
# Commands that write every operand, and those (with ANCESTOR) that also reach into a folder they name
WRITE_ALL = {"tee", "rm", "rmdir", "unlink", "shred", "truncate", "touch", "mkdir", "chmod", "chown", "chgrp",
             "chattr", "setfacl", "mv"}
ANCESTOR = {"rm", "mv", "shred"}
RECURSIVE = {"chmod", "chown", "chgrp", "setfacl", "chattr"}
DEST_LAST = {"cp", "install", "ln", "rsync", "scp"}
OUTPUT_OPTS = {"curl": ("-o", "--output"), "wget": ("-O", "--output-document", "-P", "--directory-prefix"),
               "unzip": ("-d",)}
INTERPRETER_RE = re.compile(r"^(?:python[\d.]*|pypy\d*|perl|node|nodejs|deno|bun|ruby|php|tsx|ts-node|lua|Rscript)$")
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
REDIRECT_RE = re.compile(r"(?<![<>])(?:&|\d)?>>?\|?\s*([\"']?)([^\s\"';&|<>()]+)\1")
# Code that may write a file: a write call or a mode that writes (perl's '>', '>>', '+<')
WRITE_CODE_RE = re.compile(
    r"\b(?:write\w*|unlink\w*|remove\w*|rename\w*|replace|chmod\w*|chown\w*|rmtree|copy\w*|move|symlink\w*|link"
    r"|mkdirs?\w*|makedirs|truncate\w*|rmdir\w*|touch|dump|save\w*|append\w*|rm_r?f?|cp|mv|ln|sysopen)\b"
    r"|[\"'][rwabxt]*[wax+][rwabxt]*[\"']|[\"'](?:\+?>>?|\+<)[^\"']*[\"']")
# A perl or shell redirect (or perl open mode) and its target; a comparison's right side names no path
TARGET_RE = re.compile(r">>?[&|]?\s*[\"']?([^\s\"'`;,()<>|&]+)")
# Code that runs a command: it writes only what that command writes
RUN_CODE_RE = re.compile(r"\b(?:system|exec\w*|popen\w*|spawn\w*|subprocess|check_call|check_output|run)\b|`")
STR_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`([^`]*)`")
# Commands in an argv list that write the paths they name (login tools write their store: read as argv)
ARGV_WRITERS = WRITE_ALL | DEST_LAST | set(OUTPUT_OPTS) | {"dd", "sed", "perl"}
STORE_TOOLS = {"gh", "docker", "aws", "kubectl", "git", "ssh-keygen"}
HOME_REF_RE = re.compile(r"~|\bHOME\b|home\(\)|expanduser|homedir|getpwuid|Dir\.home|\$ENV\{|os\.homedir")
# Python code is parsed: a write call checks only its target, so a script that reads a protected path
# and writes elsewhere passes (_py_refusal)
PY_WRITER_RE = re.compile(
    r"write\w*|unlink\w*|remove\w*|rename\w*|replace|l?chmod\w*|l?chown\w*|chflags|rmtree|copy\w*|move|symlink\w*"
    r"|link\w*|hardlink\w*|mkdirs?\w*|makedirs|truncate\w*|rmdir\w*|removedirs|touch|dump|save\w*|mkfifo|mknod|utime"
    r"|setxattr|unpack_archive|extract\w*|make_archive|to_\w+|connect|mk[sd]temp|rm\w*|cp|mv|ln|"
    + "|".join(sorted(WRITE_ALL | DEST_LAST | set(OUTPUT_OPTS) | {"dd", "sed", "perl"})))
PY_OPENERS = {"open", "fdopen", "FileIO", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
PY_DATA_WRITES = {"write", "writelines", "write_text", "write_bytes", "truncate"}
PY_CONTAINER = {"append", "extend", "insert", "add", "update", "setdefault", "appendleft", "put", "put_nowait"}
PY_READS = {"read", "read_text", "read_bytes", "readline", "readlines", "load", "read_csv", "read_json", "read_table",
            "read_parquet", "exists", "isfile", "isdir", "islink", "is_file", "is_dir", "is_symlink", "getsize",
            "getmtime", "getctime", "getatime", "stat", "lstat", "len", "access"}
PY_INPUT = {"argv", "orig_argv", "stdin", "environ", "environb", "getenv", "input", "fileinput"}
PY_DYNAMIC = {"exec", "eval", "compile", "getattr", "setattr", "__import__", "globals", "locals", "vars"}
PY_DYNAMIC_ATTR = {"import_module", "__dict__", "__builtins__", "__getattribute__"}
MODE_RE = re.compile(r"[rbtU]*[wax+][rwabxtU+]*")
# Calls known to only read, test or build a path: the only ones a protected path may reach as an argument
# or receiver (any other such call sends the code to the word check), unless the code defines the name
PY_SAFE = PY_READS | {
    "open", "Path", "PurePath", "PosixPath", "home", "expanduser", "expandvars", "join", "joinpath", "abspath",
    "realpath", "normpath", "relpath", "dirname", "basename", "split", "splitext", "commonpath", "resolve",
    "absolute", "with_name", "with_suffix", "relative_to", "as_posix", "fspath", "listdir", "scandir", "walk",
    "glob", "iglob", "rglob", "iterdir", "fnmatch", "loads", "str", "repr", "bytes", "print", "format", "sorted",
    "list", "tuple", "set", "frozenset", "dict", "enumerate", "zip", "map", "filter", "reversed", "iter", "next",
    "any", "all", "min", "max", "sum", "isinstance", "int", "float", "bool", "startswith", "endswith", "strip",
    "rstrip", "lstrip", "lower", "upper", "encode", "decode", "splitlines", "get", "keys", "values", "items"}
# Openers that read in a read mode: the builtin, a path's own method and these modules' (shelve, dbm: any)
PY_READ_OPENERS = {"io", "os", "codecs", "gzip", "bz2", "lzma", "tarfile", "zipfile"}
# The code or the line changes the working directory: relative paths are the protected folder's
CHDIR_RE = re.compile(r"chdir|\bpushd\b")
CD_LINE_RE = re.compile(r"(?:^|[;&|(\s])(?:cd|pushd)(?:\s|$)|\benv\b[^;&|\n]*\s(?:-[a-zA-Z]*C|--chdir\b)")
PY_TEXT_MAX = 2_000_000
# Code that reads what the command line gives it (argv, stdin, the environment, a shell `$` word):
# the line's words outside the code count as the code's
INPUT_RE = re.compile(r"argv|ARGV|stdin|STDIN|environ|ENV|getenv|\binput\s*\(|fileinput|<>|\$|process\.env|\bgets\b"
                      r"|readline")
# Codex patches: the files one adds, changes, deletes or moves to (Codex trims a header line, so blanks
# around it and its path do not hide one)
PATCH_FILE_RE = re.compile(r"^\s*\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$", re.M)
MCP_READ_RE = re.compile(r"(?:^|_)(?:read|get|list|search|find|view|stat|info|tree|show|fetch|query|describe|"
                         r"grep|glob|cat|head|tail|lookup|status)(?:_|$)", re.I)
MCP_WRITE_RE = re.compile(r"write|edit|create|move|rename|delete|remove|copy|put|patch|append|chmod|mkdir|upload|"
                          r"save|set|update", re.I)
# A key used as an ssh identity passes by credguard's class rule (ssh_keys, ssh_commands): the clients that take
# one, the wrappers they may run under, and the commands that may sit beside them
SSH_TOOLS = {"ssh", "scp", "sftp"}
KEY_WRAPPERS = {"env", "timeout", "nohup", "nice"}
KEY_SAFE = {"echo", "printf", "true", "false", ":", "test", "[", "cd", "pwd", "ls", "cat", "grep", "head", "tail",
            "wc", "sleep", "date"}
KEY_ROUTE = ("To use a key as an ssh identity, give it as the direct value of `ssh|scp|sftp -i KEY` or "
             "`-o IdentityFile=KEY`, or in a plain `ssh -i KEY` that `GIT_SSH_COMMAND=... git "
             "fetch|clone|ls-remote` or rsync's -e runs, as a plain shell command with no option that runs a local program (ProxyCommand, "
             "LocalCommand, KnownHostsCommand, Match exec, -F with a config outside ~/.ssh) and nothing else in the "
             "line that runs a program or writes, or name the key as IdentityFile in an ssh config (given by -F, "
             "-o UserKnownHostsFile= or GlobalKnownHostsFile= in the same call when it is in ~/.ssh), instead of "
             "calling them from inside interpreter code")


class KeyHit(str):
    """A protected key a command line names outside credguard's class rule: refused as a credential-store read."""


# The plugin's own shipped scripts (setup-box's write ~/.ssh on a remote box, in ssh command text):
# reviewed code, so the script-file read skips them; a script a run wrote is read
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
# A host ssh runs text on: its user's home (a placeholder), and what there no run writes
REMOTE_HOMES = ("/~remote", "/home/*", "/root")
REMOTE_PATHS = (".ssh/id_*", ".ssh/config", ".ssh/authorized_keys")
SSH_VALUE_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
WHY = ("a run never creates, changes, moves, chmods or deletes anything under the account's credential folders "
       "({hit} is one; guards.protected_paths lists them): a login a run overwrote is gone for every project on "
       "the host. Read it as before, test against a scratch copy under `mktemp -d` named by another variable "
       "(H=$(mktemp -d); ... \"$H/.ssh/...\"), and leave logins and keys to the user")


def paths(cfg: dict | None = None) -> list[str]:
    """The protected list: the defaults, then the account's and the project's guards.protected_paths."""
    from .project import load_account_settings
    out = list(DEFAULT_PATHS)
    if cfg is None:
        from .aitag import project_config
        cfg = project_config()
    for src in (load_account_settings(), cfg or {}):
        extra = (src.get("guards") or {}).get("protected_paths") if isinstance(src.get("guards"), dict) else None
        out += [p for p in extra if isinstance(p, str) and p.strip()] if isinstance(extra, list) else []
    return list(dict.fromkeys(out))


def _homes(env) -> list[str]:
    homes = [env.get("HOME") or os.path.expanduser("~")]
    try:
        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        pass
    return list(dict.fromkeys(h for h in homes if h))


def expand(word: str, env, cwd: str, home: str | None = None, real: bool = True) -> str:
    """`word` as a real path: ~ and VARS expanded (with `home` for HOME), relative to `cwd`, symlinks
    resolved (`real`)."""
    home = home or env.get("HOME") or os.path.expanduser("~")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    vals = {"HOME": home, "XDG_CONFIG_HOME": xdg, "GH_CONFIG_DIR": env.get("GH_CONFIG_DIR") or os.path.join(xdg, "gh")}
    word = VAR_RE.sub(lambda m: vals[m[1]], word.replace('"', "").replace("'", ""))
    if word == "~" or word.startswith("~/"):
        word = home + word[1:]
    word = os.path.join(cwd, os.path.expanduser(word))
    return os.path.realpath(word) if real else os.path.normpath(word)


def roots(env=None, cfg: dict | None = None, defaults: bool = False) -> list[str]:
    """Every protected path, real and as written, for HOME and the account's own home; gh's dir as the
    environment moves it (GH_CONFIG_DIR, XDG_CONFIG_HOME) too. `defaults`: DEFAULT_PATHS' only."""
    env = os.environ if env is None else env
    out = []
    for p in (list(DEFAULT_PATHS) if defaults else paths(cfg)) + ["$GH_CONFIG_DIR"]:
        for home in _homes(env):
            for e in (env, {}):
                out += [expand(p, e, "/", home), expand(p, e, "/", home, real=False)]
    return [r for r in dict.fromkeys(out) if r not in ("/", "") and os.path.isabs(r)]


def _inside(path: str, rts: list[str]) -> str | None:
    return next((r for r in rts if path == r or path.startswith(r.rstrip("/") + "/")
                 or "*" in r and fnmatch.fnmatchcase(path, r)), None)


def _holds(path: str, rts: list[str]) -> str | None:
    """The protected path `path` is a folder of (or is)."""
    return _inside(path, rts) or next((r for r in rts if r.startswith(path.rstrip("/") + "/")), None)


def _hit(word: str, ctx, ancestor: bool = False) -> str | None:
    if not word or word.startswith("-"):
        return None
    p = expand(word, ctx[0], ctx[1], real=len(ctx) < 4)   # a remote path is not resolved here
    return (_holds if ancestor else _inside)(p, ctx[2])


def _operands(args: list[str]) -> list[str]:
    out, opts = [], True
    for a in args:
        if opts and a == "--":
            opts = False
        elif not (opts and a.startswith("-") and a != "-"):
            out.append(a)
    return out


def _opt_values(args: list[str], names: tuple[str, ...]) -> list[str]:
    out = []
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                out.append(args[i + 1])
            elif n.startswith("--") and a.startswith(n + "="):
                out.append(a[len(n) + 1:])
            elif len(n) == 2 and a.startswith(n) and len(a) > 2 and not a.startswith("--"):
                out.append(a[2:])
    return out


def _store_write(argv: list[str]) -> str | None:
    """The store a command rewrites itself, by its home-relative path (None: not such a command)."""
    name, rest = os.path.basename(argv[0]), [a for a in argv[1:] if not a.startswith("-")]
    sub, sub2 = (rest + ["", ""])[:2]
    if name == "gh" and (sub == "auth" and sub2 in ("login", "logout", "refresh", "switch")
                         or sub == "config" and sub2 in ("set", "clear-cache")):
        return "$GH_CONFIG_DIR"
    if name == "docker" and sub in ("login", "logout"):
        return "~/.docker/config.json"
    if name == "aws" and sub == "configure" and sub2 not in ("get", "list", "list-profiles"):
        return "~/.aws"
    if name == "kubectl" and sub == "config" and sub2 not in ("view", "current-context", "get-contexts",
                                                             "get-clusters", "get-users"):
        return "~/.kube"
    if name == "git" and sub == "credential" and sub2 in ("approve", "reject") or name == "git-credential-store":
        return "~/.git-credentials"
    if name == "ssh-keygen" and not _keygen_reads(argv[1:]) and not _keygen_file(argv[1:]):
        return "~/.ssh"
    return None


def _keygen_reads(args: list[str]) -> bool:
    """ssh-keygen only prints (-l, -y, -F, -L, -B, -e, -Q)."""
    return bool(set("lyFLBeQ") & set("".join(a[1:] for a in args if re.fullmatch(r"-[a-zA-Z]+", a))))


def _keygen_file(args: list[str]) -> list[str]:
    """ssh-keygen's -f values, also at the end of a flag cluster (-lf FILE) or joined (-fFILE)."""
    out = []
    for i, a in enumerate(args):
        if re.fullmatch(r"-[a-zA-Z]*f", a) and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith("-f") and len(a) > 2:
            out.append(a[2:])
    return out


def _argv_refusal(argv: list[str], ctx, cmd_env=None) -> str | None:
    """`cmd_env`: the environment the command itself gets (its NAME=value prefix): where a tool keeps its store."""
    name, args = os.path.basename(argv[0]), argv[1:]
    ops = _operands(args)
    store = _store_write(argv)
    if store and _inside(expand(store, cmd_env or ctx[0], "/"), ctx[2]):
        return store
    if len(ctx) > 3 and name == "ssh-keygen" and store and not any(re.fullmatch(r"-[a-zA-Z]*[RHAY]\w*", a)
                                                                   for a in args):
        # On a remote host (where ~/.ssh itself is writable): its default key
        if h := _inside(expand("~/.ssh/id_", cmd_env or ctx[0], "/", real=False), ctx[2]):
            return h
    if name == "ssh-keygen":
        return None if _keygen_reads(args) else next((h for w in _keygen_file(args) if (h := _hit(w, ctx))), None)
    if name in WRITE_ALL or name in ("sed", "perl") and any(re.match(r"-[a-zA-Z]*i|--in-place", a) for a in args):
        anc = name in ANCESTOR or name in RECURSIVE and any(re.match(r"-[a-zA-Z]*R|--recursive", a) for a in args)
        words = ops if name != "perl" else [a for a in ops if not a.startswith(("print", "s/", "$"))]
        return next((h for w in words if (h := _hit(w, ctx, anc))), None)
    if name in DEST_LAST:
        dest = _opt_values(args, ("-t", "--target-directory")) + ops[-1:]
        return next((h for w in dest if (h := _hit(w, ctx))), None)
    if name == "dd":
        return next((h for a in args if a.startswith("of=") and (h := _hit(a[3:], ctx))), None)
    if name in OUTPUT_OPTS:
        return next((h for w in _opt_values(args, OUTPUT_OPTS[name]) if (h := _hit(w, ctx))), None)
    return None


def _code_refusal(code: str, ctx, depth: int = 0, outside: str = "", py: bool = False) -> str | None:
    """Interpreter code that writes a protected path: Python's write calls by their target (_py_refusal);
    other code (or Python it cannot read) a write call or mode with one named in the code (anywhere: a
    variable may carry it), a redirect or open mode with one as its target, or a command the code runs
    (os.system, subprocess, exec, backticks) that writes one. Code that reads its input (argv, stdin,
    the environment) gets the command line `outside` it too. Reads pass, and so do mentions in data."""
    chd = bool(CHDIR_RE.search(code))
    fed = outside if outside and (chd or INPUT_RE.search(code)) else ""
    got = _py_refusal(code, ctx, _names(fed, ctx) if fed else None) if py and not chd else False
    code = code + "\n" + fed if fed else code
    if got:
        return got
    if got is False:
        if WRITE_CODE_RE.search(code) and (h := _names(code, ctx)):
            return h
        for m in TARGET_RE.finditer(code):
            if h := _names(m[1], ctx):
                return h
    if not RUN_CODE_RE.search(code):
        return None
    ms = list(STR_RE.finditer(code))
    lits = [m[1] or m[2] or m[3] or "" for m in ms]
    # The code with each literal blanked: what lies between literals is code (a variable, a call)
    bare = STR_RE.sub(lambda m: m[0][0] + "x" * (len(m[0]) - 2) + m[0][-1], code)
    for i, (m, lit) in enumerate(zip(ms, lits)):
        words = re.findall(r"[^\s|;&<>()\"'`]+", lit)
        names, built = {os.path.basename(w) for w in words}, _built(bare, m, lit)
        if names & ARGV_WRITERS and (h := _names(code, ctx)):   # a write command: its target may be built
            return h
        item = os.path.basename(lit) in STORE_TOOLS   # an argv list item: its args are the literals after it
        if item and (h := _argv_refusal(lits[i:], ctx)):
            return h
        # A login tool with an argument built in code may write its store: unless it only prints a key
        reads = "ssh-keygen" in names and _keygen_reads(lits[i + 1:] if item else words)
        if names & STORE_TOOLS and built and not reads and (h := _names(code, ctx)):
            return h
        if " " in lit and depth < 2 and (h := shell_refusal(lit, *ctx, depth + 1)):   # a command line
            return h
        # A redirect whose target the code builds: 'echo x >' + p, f'echo x > {p}', 'cat > %s' % p (2>&1: none)
        joined = re.match(r"\s*[^\s,\])\n;]", bare[m.end():])   # code goes on right after it: + p, % p
        for r in re.finditer(r"(?<![<>])>>?\|?(&)?\s*([^\s;&|<>()]*)", lit):
            if r[1] and re.fullmatch(r"\d+-?|-", r[2].rstrip("'\"`")):
                continue
            if (not r[2] or re.search(r"[{}%$]", r[2]) or joined and r.end() == len(lit)) and (
                    h := _names(code, ctx)):
                return h
    return None


def _built(bare: str, m, lit: str) -> bool:
    """The literal at `m` is joined with code: a format, template, concatenation or an argv list
    holding a variable (`bare`: the code with its literals blanked)."""
    if "{" in lit and bare[m.start() - 1:m.start()] in ("f", "F") or m[3] and "${" in lit:
        return True
    after = re.split(r"[\])\n;]", bare[m.end():], maxsplit=1)[0]
    before = re.split(r"[\[(\n;]", bare[:m.start()])[-1]
    return bool(re.sub(r"([\"'`])x*\1|[\s,]", "", after + before))


def _names(code: str, ctx) -> str | None:
    """The protected path `code` names: as a path, or as the home folder joined with its parts."""
    for m in re.finditer(r"(?:~|\$\{?(?:HOME|XDG_CONFIG_HOME|GH_CONFIG_DIR)\}?|/)[^\s\"'`;,()\[\]{}<>|&]*", code):
        if m[0] not in ("~", "/") and (h := _hit(m[0], ctx)):
            return h
    if HOME_REF_RE.search(code):
        for home in _homes(ctx[0]):
            for r in ctx[2]:
                rel = os.path.relpath(r, home)
                parts = rel.split("/")
                if not rel.startswith("..") and rel != "." and (
                        rel in code or all(re.search(r"[\"'/]" + re.escape(p) + r"[\"'/]", code) for p in parts)):
                    return r
    return None


def _py_leaf(n) -> str:
    return n.id if isinstance(n, ast.Name) else n.attr if isinstance(n, ast.Attribute) else ""


def _py_root(n) -> str:
    while isinstance(n, ast.Attribute):
        n = n.value
    return n.id if isinstance(n, ast.Name) else ""


def _py_refusal(code: str, ctx, fed: str | None = None):
    """Python code read for what it writes: the protected path a write call's target holds, None when
    no write targets one, False when the code cannot be read this way (a syntax error, dynamic code,
    a write function passed as a value). A target holds what its words name, expanded with every
    value its variables (attributes, function parameters and results) are given anywhere; a file's
    content (what read, load and a read-mode open return) names no path. `fed`: a protected path the
    command line names outside the code, held by argv, stdin, the environment and `$` words."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    parent, defs, funcs, mods = {}, {}, {}, set()
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[c] = n
        if isinstance(n, ast.Import):
            mods.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and any(a.asname and PY_WRITER_RE.fullmatch(a.name) for a in n.names):
            return False
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            funcs.setdefault(n.name, []).append(n)
        elif isinstance(n, ast.Name) and n.id in PY_DYNAMIC or isinstance(n, ast.Attribute) and n.attr in PY_DYNAMIC_ATTR:
            return False

    def bind(t, v):
        if isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                bind(e, v)
        elif isinstance(t, (ast.Starred, ast.Subscript)):
            bind(t.value, v)
        elif isinstance(t, ast.arguments):
            for a in t.posonlyargs + t.args + t.kwonlyargs + [x for x in (t.vararg, t.kwarg) if x]:
                if a.arg not in ("self", "cls"):
                    defs.setdefault(a.arg, []).append(v)
        elif k := _py_leaf(t):
            defs.setdefault(k, []).append(v)

    def params(f, v):
        for g in [m for m in f.body if getattr(m, "name", "") == "__init__"] if isinstance(f, ast.ClassDef) else [f]:
            bind(g.args, v)

    for n in ast.walk(tree):
        if isinstance(n, (ast.Name, ast.Attribute)) and PY_WRITER_RE.fullmatch(_py_leaf(n)) and not (
                isinstance(parent.get(n), ast.Call) and parent[n].func is n) and not isinstance(n.ctx, ast.Store):
            return False   # a write function as a value: an alias, a callback
        if isinstance(n, ast.Assign):
            for t in n.targets:
                bind(t, n.value)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value is not None:
            bind(n.target, n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(n.target, n.iter)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            bind(n.optional_vars, n.context_expr)
        elif isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom)) and n.value is not None:
            f = n
            while f in parent and not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = parent[f]
            defs.setdefault(getattr(f, "name", ""), []).append(n.value)
        elif isinstance(n, ast.Call):
            args = n.args + [k.value for k in n.keywords]
            if _py_leaf(n.func) in PY_CONTAINER and isinstance(n.func, ast.Attribute):
                for a in args:
                    bind(n.func.value, a)
            for f in funcs.get(_py_leaf(n.func), []):
                for a in args:
                    params(f, a)
            for a in args:   # a function handed to a call (map, submit, a callback) gets its other arguments
                for f in [a] if isinstance(a, ast.Lambda) else funcs.get(_py_leaf(a), []):
                    for b in args:
                        if b is not a:
                            params(f, b)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                bind(n.args, d)

    def mine(f) -> bool:
        """The called name is one the code defines or rebinds: never trusted as a read."""
        return _py_leaf(f) in defs or _py_leaf(f) in funcs

    def is_mod(n) -> bool:
        while isinstance(n, ast.Attribute):
            n = n.value
        return isinstance(n, ast.Name) and n.id in mods

    def opens(n) -> bool | None:
        """For an open-like call: whether it writes (None: not one)."""
        if _py_leaf(n.func) not in PY_OPENERS:
            return None
        own = isinstance(n.func, ast.Attribute) and not is_mod(n.func.value)   # a path's own open method, mode first
        if mine(n.func) or isinstance(n.func, ast.Attribute) and not own and _py_root(n.func) not in PY_READ_OPENERS:
            return True   # shelve.open, dbm.open, an open the code defines: may create
        modes = n.args[0 if own else 1:][:1] + [k.value for k in n.keywords if k.arg in ("mode", "flags", None)]
        return any(not isinstance(m, ast.Constant) or not isinstance(m.value, str) or MODE_RE.fullmatch(m.value)
                   for m in modes)

    def content(n) -> bool:
        """A call whose value is a file's content, a handle to read or a fact about it: no path."""
        if not isinstance(n, ast.Call) or any(_py_leaf(x) in PY_INPUT for x in ast.walk(n)):
            return False
        return not mine(n.func) and (_py_leaf(n.func) in PY_READS or opens(n) is False)

    memo, busy, size = {}, set(), [0]

    def text(n) -> str:
        """`n`'s words, each variable's with the text of every value it is given (a cycle cut once)."""
        out, todo = [], [n]
        while todo:
            x = todo.pop()
            if content(x):
                continue
            todo += ast.iter_child_nodes(x)
            k = _py_leaf(x) or (x.arg if isinstance(x, ast.arg) else "")
            if isinstance(x, ast.Constant) and isinstance(x.value, (str, bytes)):
                v = x.value.decode("latin-1") if isinstance(x.value, bytes) else x.value
                out.append(repr(v) + (" " + repr(fed) if fed and "$" in v else ""))
            elif k in PY_INPUT and fed:
                out.append(repr(fed))
            elif k:
                out.append(k + ("()" if k == "home" else ""))
            if k and k in defs and k not in busy:
                if k in memo:
                    out.append(memo[k])
                    continue
                busy.add(k)
                got = " ".join(text(v) for v in defs[k])
                busy.discard(k)
                if not busy:   # one computed inside a cycle may miss a value: kept only at the top
                    memo[k] = got
                out.append(got)
        size[0] += sum(map(len, out))
        if size[0] > PY_TEXT_MAX:
            raise OverflowError
        return " ".join(out)

    try:
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name, args = _py_leaf(n.func), n.args + [k.value for k in n.keywords]
            recv = n.func.value if isinstance(n.func, ast.Attribute) and not is_mod(n.func.value) else None
            if (w := opens(n)) is not None:
                path = [k.value for k in n.keywords if k.arg in ("file", "path", "name")] + n.args[:0 if recv else 1]
                targets = [recv] + path if w else []
            elif name in PY_DATA_WRITES and recv is not None:   # data writes: the file or path object is the target
                targets = [recv]
            elif name == "print":
                targets = [k.value for k in n.keywords if k.arg == "file"]
            elif recv is not None and (name in PY_CONTAINER or name == "replace" and len(n.args) != 1
                                       or name == "copy" and not args):
                targets = []
            elif PY_WRITER_RE.fullmatch(name):
                targets = [recv] + (args[1:] if name == "dump" else args)
            elif name[:1].isupper() or "open" in name.lower() or name.endswith("File"):   # ZipFile(p, 'w')
                targets = [recv] + n.args[:1] if any(isinstance(m, ast.Constant) and isinstance(m.value, str)
                                                     and MODE_RE.fullmatch(m.value) for m in args[1:]) else []
            else:
                targets = []
                if (mine(n.func) or name not in PY_SAFE) and (h := next(
                        (h for a in args + [recv] if a is not None and (h := _names(text(a), ctx))), None)):
                    return h   # a call not known to only read reaches a protected path: fail closed
            for t in targets:
                if t is not None and (h := _names(text(t), ctx)):
                    return h
    except (OverflowError, RecursionError):
        return False
    return None


def shell_refusal(cmd: str, env, cwd: str, rts: list[str], depth: int = 0, script: bool = False,
                  copied: dict | None = None) -> str | None:
    """The protected path a shell command line (or script) writes, as far as its words show. `copied`: the text
    runs on a remote host, where these local files (by base name) were copied."""
    from . import hook
    ctx = (env, cwd, rts) if copied is None else (env, cwd, rts, True)
    commands, scan = hook._split(cmd)
    if not script and (h := _key_refusal(cmd, commands, ctx)):
        return h
    # Redirects: in the line as written and in scan (_split drops an echo's quoted words, a quoted
    # redirect target too), where the `>` is not quoted: quoted text is an argument (ssh's remote
    # command, a message); `sh -c` text is read below
    for text in (cmd, scan):
        flags = hook._quoting(text)
        for m in REDIRECT_RE.finditer(text):
            if not flags[m.start()][0] and (h := _hit(m[2], ctx)) and not (
                    copied is not None and ">>" in m[0] and h.endswith("/authorized_keys")):   # adding a key
                return h
    if "*** Begin Patch" in cmd and (h := patch_refusal(cmd, ctx)):
        return h
    segments = hook._all_segments(commands)
    prefixed = {tuple(hook._strip_prefix(raw)): _assigned(raw, env) for raw in hook._segments(commands, strip=False)}
    for argv in segments:
        if h := _argv_refusal(argv, ctx, prefixed.get(tuple(argv))):
            return h
    for argv in segments:   # sh -c '...' and eval '...': their redirects too
        argv = argv[1:] if argv[0] == "builtin" and len(argv) > 1 else argv
        name = os.path.basename(argv[0])
        inner = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", a)),
                     None) if name in SHELLS else " ".join(argv[1:]) if name == "eval" else None
        if inner and depth < 2 and (
                h := shell_refusal(inner, env, cwd, rts, depth + 1, copied is not None, copied)):
            return h
        # eval parses its words again: an escaped redirect (eval echo x \> f) is one then
        if name == "eval" and "\\" in cmd and depth < 2 and (
                h := shell_refusal(re.sub(r"\\(.)", r"\1", cmd), env, cwd, rts, depth + 1)):
            return h
    names = {os.path.basename(a[0]) for a in segments}
    dflt = set(roots(env, defaults=True))
    dctx, xctx = (env, cwd, [r for r in rts if r in dflt]), (env, cwd, [r for r in rts if r not in dflt])
    cd = bool(CD_LINE_RE.search(scan))
    if any(INTERPRETER_RE.match(n) for n in names):
        # The default credential folders keep the whole-line word check (no read relief for keys), and
        # so does every folder when the line changes directory first (cd, pushd, env -C)
        inline = [a[i + 1] for a in segments if INTERPRETER_RE.match(os.path.basename(a[0]))
                  for i in range(1, len(a) - 1) if a[i] in ("-c", "-e", "-E", "--eval", "-p", "--print")]
        if h := next((h for c in [scan, *inline] if (h := _code_refusal(c, ctx if cd else dctx, depth))), None):
            return h
        # Each -c/-e code as the interpreter gets it (its string literals unquoted), and the heredocs it
        # reads on stdin; the line's other words are data unless the code reads its input. An interpreter
        # whose code does not show (-m, a pipe) gets the whole line.
        lines, bodies = hook._heredocs(cmd)
        fed = ["\n".join(b) for i, b, pos in bodies if INTERPRETER_RE.match(hook._first_word(hook._piece_at(lines[i], pos)[0]))]
        chunks = []
        for a in segments:
            name = os.path.basename(a[0])
            if not INTERPRETER_RE.match(name):
                continue
            py = bool(re.match(r"python|pypy", name))
            code = [a[i + 1] for i in range(1, len(a) - 1) if a[i] in ("-c", "-e", "-E", "--eval", "-p", "--print")]
            if code:
                chunks += [(c, py) for c in code]
            elif (how := hook._interpreter_args(a)) == "-" and fed:
                chunks.append((fed.pop(0), py))
            elif how != "-" and how and not how.startswith("-"):
                continue   # a script file: read below
            else:
                chunks.append((scan, False))
        if not cd and xctx[2] and (
                h := next((h for c, py in chunks if (h := _code_refusal(c, xctx, depth, scan, py))), None)):
            return h
    if depth < 3 and "ssh" in cmd and (h := _remote_refusal(cmd, env, cwd, depth, copied or {})):
        return h
    if copied is not None:   # a remote script file: the local file scp or rsync sent under its name
        files = [Path(copied[n]) for a in segments for f in hook._run_files(a) if (n := os.path.basename(f)) in copied]
        return next((h for f in files if depth < 2 and (h := _script_refusal(f, ctx, depth, copied))), None)
    if depth < 2:
        for f in hook._scripts(segments, cwd):
            if os.path.realpath(f).startswith(PLUGIN_ROOT + os.sep):
                continue
            if h := _script_refusal(f, ctx, depth, copied, (cd, dctx, xctx, scan)):
                return h
    return None


def _key_refusal(cmd: str, commands: str, ctx) -> str | None:
    """A command line that uses a protected key as an ssh identity passes only by credguard's class rule: the key is
    the direct value of `ssh|scp|sftp -i` or `-o IdentityFile=` (its remote command is the remote host's), or of a
    plain `ssh -i KEY` in GIT_SSH_COMMAND before git fetch|clone|ls-remote or RSYNC_RSH before rsync (the only
    assignment), and no option runs a local program. Beside it only KEY_SAFE commands, no command substitution and
    no redirect to a built target. Anything else that names one is refused as a credential-store read (KeyHit)."""
    from . import credguard, hook
    hit, bad = None, False
    for raw in hook._segments(commands, strip=False):
        argv = hook._strip_prefix(raw)
        pre, name = raw[:len(raw) - len(argv)], argv[0] if argv else ""
        sets = [w.split("=", 1) for w in pre if re.match(r"[A-Za-z_]\w*=", w)]
        keys = [h for w in raw if re.match(r"[A-Za-z_]\w*=.*\b(?:ssh|scp|sftp)\b", w) and (h := _names(w, ctx))]
        hit = hit or (keys[0] if keys else None)
        ok = all(re.fullmatch(r"\w+=.*|-.*|\d+[smhd]?", w) or w in KEY_WRAPPERS for w in pre) and not (
            "env" in pre and any(w.startswith("-") for w in pre))
        if name in SSH_TOOLS:   # ssh -i KEY, with no assignment before it
            h, opts = _ssh_key(argv, ctx)
            ok = ok and opts and not sets
        elif name in credguard.SSH_COMMAND_VARS:   # GIT_SSH_COMMAND='ssh -i KEY' git fetch, RSYNC_RSH=... rsync
            v = sets[0][1] if len(sets) == 1 and sets[0][0] + "=" == credguard.SSH_COMMAND_VARS[name] else ""
            ssh = shlex.split(v) if v and not re.search(r"[;&|<>`$()\n\\]", VAR_RE.sub("", v)) else []
            h, opts = _ssh_key(ssh, ctx) if ssh[:1] == ["ssh"] else (None, False)
            ok = ok and opts and v in credguard.ssh_commands(raw) and not any(_names(a, ctx) for a in argv[1:])
        else:
            h, ok = None, name in KEY_SAFE and not keys
        hit, bad = hit or h, bad or not ok
    if not hit:
        return None
    q, esc = "", False   # command substitution: $( or a backtick outside single quotes, <( >( unquoted
    for i, c in enumerate(cmd):
        if q == "'":
            q = "" if c == "'" else q
        elif esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == "'" and not q:
            q = "'"
        elif c == '"':
            q = "" if q else '"'
        elif c == "`" or cmd[i:i + 2] == "$(" or not q and cmd[i:i + 2] in ("<(", ">("):
            bad = True
    flags = hook._quoting(cmd)   # a redirect whose target is built ($_, $f) or not a file (>& $f)
    for m in [*REDIRECT_RE.finditer(cmd), *re.finditer(r">&\s*(?![\d-])", cmd)]:
        if not flags[m.start()][0] and (m.re is not REDIRECT_RE or re.search(r"[$`]", m[2])):
            bad = True
    return KeyHit(hit) if bad else None


def _ssh_key(argv: list[str], ctx) -> tuple[str | None, bool]:
    """(a protected path the options of ssh, scp or sftp `argv` name, whether they follow credguard's class rule:
    no option runs a local program, and each naming a protected path is its key, -i or -o IdentityFile). A control
    socket ssh keeps itself (-S, -o ControlPath=, credguard.ssh_public) is none, and so is a settings file or host
    list ssh reads itself (-F, -o UserKnownHostsFile=, GlobalKnownHostsFile=, credguard.ssh_files)."""
    from . import credguard
    keys, hit, ok = credguard.ssh_keys(argv), None, not credguard.ssh_runs_local(argv)
    files = credguard.ssh_files(argv)
    for o, val, word in credguard.ssh_options(argv):
        hosts = credguard.SSH_HOSTS_OPTION_RE.match(val) if o == "o" else None
        own = word in files or o in "So" and not hosts and word not in keys and credguard.ssh_public(
            os.path.basename(re.split(r"[\s=]", val)[-1]))
        if not own and (h := _names(val, ctx)):
            hit, ok = hit or h, ok and word in keys
    return hit, ok


def _script_refusal(f: Path, ctx, depth: int, copied: dict | None, split: tuple | None = None) -> str | None:
    """What script file `f` writes: as shell text (a .sh or a shell #! line), else as interpreter code. A file
    it cannot read passes. `split`: (cd, dctx, xctx, scan) of the local line: the default folders keep the
    whole-code word check, other folders get the read pass unless the line changed directory first."""
    try:
        text = f.read_text(errors="replace")
    except OSError:
        return None
    # The shebang's interpreter (`#!/usr/bin/env bash`): a first line of code (import shutil) names none
    first = " ".join(os.path.basename(w) for w in text.split("\n", 1)[0][2:].split()) if text[:2] == "#!" else ""
    if f.suffix == ".sh" or set(first.split()) & SHELLS:
        return shell_refusal(text, ctx[0], ctx[1], ctx[2], depth + 1, True, copied)
    if split is None:
        return _code_refusal(text, ctx, depth + 1)
    cd, dctx, xctx, scan = split
    return _code_refusal(text, ctx if cd else dctx, depth + 1) or not cd and xctx[2] and _code_refusal(
        text, xctx, depth + 1, scan, f.suffix == ".py" or bool(re.search(r"python|pypy", first)))


def _remote_refusal(cmd: str, env, cwd: str, depth: int, copied: dict) -> str | None:
    """What the text ssh runs on another host in `cmd` writes there: its remote command, and the text its stdin
    feeds a remote shell or interpreter, checked with the remote home against REMOTE_PATHS."""
    rhome = REMOTE_HOMES[0]
    renv, rts = {"HOME": rhome}, [os.path.join(h, p) for h in REMOTE_HOMES for p in REMOTE_PATHS]
    calls, sent = _ssh_calls(cmd, env, cwd)
    copied = {**copied, **sent}
    for words, stdin in calls:
        text = " ".join(words)
        names = {os.path.basename(a[0]) for a in _segments_of(text)} if words else {"sh"}
        h = shell_refusal(text, renv, rhome, rts, depth + 1, True, copied) if words else None
        if not h and stdin is not None and names & SHELLS:
            h = shell_refusal(stdin, renv, rhome, rts, depth + 1, True, copied)
        elif not h and stdin is not None and any(INTERPRETER_RE.match(n) for n in names):
            h = _code_refusal(stdin, (renv, rhome, rts, True), depth + 1)
        if h:
            h = "~" + h[len(rhome):] if h.startswith(rhome + "/") else h
            return h if h.endswith(" on the remote host") else h + " on the remote host"
    return None


def _segments_of(text: str) -> list[list[str]]:
    from . import hook
    return hook._all_segments(hook._split(text)[0])


def _ssh_calls(cmd: str, env, cwd: str) -> tuple[list[tuple[list[str], str | None]], dict]:
    """(the remote command words of each ssh in `cmd`, with the text its stdin gets from a `<` file, a heredoc,
    a here-string, or `cat FILE`, echo or printf piped in, else None), and the local files scp or rsync copy to a
    host there, by base name."""
    from . import hook
    lines, bodies, pending = [], [], []
    for line in cmd.replace("\\\n", " ").split("\n"):
        if pending:
            delim, dash, body = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            else:
                body.append(line)
            continue
        lines.append(line)
        flags = hook._quoting(line) if "<<" in line else []
        for m in hook.HEREDOC_RE.finditer(line):
            if not flags[m.start()][0]:
                pending.append((m[3], m[1] == "-", []))
                bodies.append(pending[-1][2])
    text = "\n".join(lines)
    text = "".join(";\n;" if c == "\n" and not q else c for c, (q, _) in zip(text, hook._quoting(text)))
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        toks = list(lex) + [";"]
    except ValueError:
        toks = text.split() + [";"]
    segs, cur, i = [], {"argv": [], "in": None, "pipe": False}, 0
    while i < len(toks):
        t, nxt = toks[i], (toks[i + 1:i + 2] or [""])[0]
        if t and set(t) <= set(";&|()<>") and set(t) & set("<>"):   # a redirect and its word
            if t == "<":
                cur["in"] = ("files", [nxt])
            elif t in ("<<", "<<<"):
                cur["in"] = ("text", "\n".join(bodies.pop(0)) if t == "<<" and bodies else nxt)
            i += 2
            continue
        if t and set(t) <= set(";&|()"):
            segs.append(cur)
            cur = {"argv": [], "in": None, "pipe": t in ("|", "|&")}
        else:
            cur["argv"].append(t)
        i += 1
    calls, sent = [], {}
    for k, seg in enumerate(segs):
        argv = hook._strip_prefix(seg["argv"])
        name = os.path.basename(argv[0]) if argv else ""
        if name in ("scp", "rsync") and len(ops := _operands(argv[1:])) > 1 and ":" in ops[-1]:
            sent.update({os.path.basename(s.rstrip("/")): expand(s, env, cwd) for s in ops[:-1] if ":" not in s})
        if name != "ssh" or (words := _ssh_remote(argv[1:])) is None:
            continue
        src = seg["in"]
        prev = hook._strip_prefix(segs[k - 1]["argv"]) if k and seg["pipe"] and not src else []
        if prev and os.path.basename(prev[0]) == "cat":
            src = ("files", _operands(prev[1:]))
        elif prev and os.path.basename(prev[0]) in ("echo", "printf"):
            src = ("text", " ".join(prev[1:]))
        stdin = src[1] if src and src[0] == "text" else None
        for f in src[1] if src and src[0] == "files" else []:
            try:
                path = Path(expand(f, env, cwd))
                if path.stat().st_size <= hook.SCRIPT_MAX_BYTES:
                    stdin = (stdin or "") + path.read_text(errors="replace") + "\n"
            except (OSError, ValueError):
                pass   # unreadable: as a local script file the guard cannot read
        calls.append((words, stdin))
    return calls, sent


def _ssh_remote(args: list[str]) -> list[str] | None:
    """The remote command words in ssh's arguments `args` (None: no host)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--":
            i += 1
            break
        opt = args[i][1:]
        j = next((k for k, c in enumerate(opt) if c in SSH_VALUE_OPTS), None)
        i += 2 if j is not None and j == len(opt) - 1 else 1
    return args[i + 1:] if i < len(args) else None


def _assigned(raw: list[str], env) -> dict:
    """`env` with the NAME=value words before a command (also after `env`)."""
    out = dict(env)
    for w in raw:
        if m := re.fullmatch(r"([A-Za-z_]\w*)=(.*)", w):
            out[m[1]] = m[2]
        elif os.path.basename(w) != "env" and not w.startswith("-"):
            break
    return out


def patch_refusal(text: str, ctx) -> str | None:
    return next((h for m in PATCH_FILE_RE.finditer(text) if (h := _hit(m[1], ctx))), None)


def _strings(v, depth: int = 0):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict) and depth < 4:
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif isinstance(v, list) and depth < 4:
        for x in v:
            yield from _strings(x, depth + 1)


def refusal(payload: dict, env=None, cfg: dict | None = None) -> str | None:
    """Why this tool call may not run (it writes under a protected path), or None. Any error refuses
    when the call names a protected path."""
    env = os.environ if env is None else env
    try:
        rts = roots(env, cfg)
    except Exception:
        rts = roots(env, {})
    try:
        hit = _refusal(payload, env, rts)
    except Exception:
        text = json.dumps(payload.get("tool_input") or {})
        hit = next((p for p in DEFAULT_PATHS + tuple(rts) if p.lstrip("~/") in text), None)
    if not hit:
        return None
    if isinstance(hit, KeyHit):   # a key named outside the plain ssh use: a credential-store read
        from . import credguard
        return (f"{credguard.WHY}, and never writes under the account's credential folders ({hit} is named "
                f"outside a plain ssh key use). {KEY_ROUTE}")
    # A key used as an ssh or git identity: name the route that passes
    route = re.search(r"(?<![\w./-])(?:ssh|scp|sftp)(?![\w.-])|GIT_SSH_COMMAND", json.dumps(payload.get("tool_input") or {}))
    return WHY.format(hit=hit) + (". " + KEY_ROUTE if route else "")


def _refusal(payload: dict, env, rts: list[str]) -> str | None:
    tool = str(payload.get("tool_name") or "")
    inp = payload.get("tool_input") or {}
    inp = inp if isinstance(inp, dict) else {"input": inp}
    ctx = (env, str(payload.get("cwd") or os.getcwd()), rts)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _hit(str(inp.get("file_path") or inp.get("notebook_path") or ""), ctx)
    if tool in ("Bash", "shell", "local_shell", "exec_command"):
        cmd = inp.get("command") or inp.get("cmd") or ""
        cmd = shlex.join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)   # an argv: keep `-c` text one word
        return shell_refusal(cmd, env, ctx[1], rts)
    if tool == "apply_patch" or "*** Begin Patch" in json.dumps(inp):
        return next((h for s in _strings(inp) if (h := patch_refusal(s, ctx))), None)
    if tool.startswith("mcp__"):
        verb = tool.rsplit("__", 1)[-1]
        if MCP_READ_RE.search(verb) and not MCP_WRITE_RE.search(verb):
            return None
        return next((h for s in _strings(inp) if re.match(r"[~/$]", s) and "\n" not in s
                     and (h := _hit(s, ctx, True))), None)
    return None

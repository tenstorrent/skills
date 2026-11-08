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
Known gap: a path a program builds at run time from parts no word names (an argument read from a file).

The list is DEFAULT_PATHS plus guards.protected_paths of the account settings and of the project
(a JSON list; ~ and the variables above expand): a setting adds paths, never removes a default.
"""
from __future__ import annotations

import ast
import json
import os
import pwd
import re
import shlex

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
# The plugin's own shipped scripts (setup-box's write ~/.ssh on a remote box, in ssh command text):
# reviewed code, so the script-file read skips them; a script a run wrote is read
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
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


def roots(env=None, cfg: dict | None = None) -> list[str]:
    """Every protected path, real and as written, for HOME and the account's own home; gh's dir as the
    environment moves it (GH_CONFIG_DIR, XDG_CONFIG_HOME) too."""
    env = os.environ if env is None else env
    out = []
    for p in paths(cfg) + ["$GH_CONFIG_DIR"]:
        for home in _homes(env):
            for e in (env, {}):
                out += [expand(p, e, "/", home), expand(p, e, "/", home, real=False)]
    return [r for r in dict.fromkeys(out) if r not in ("/", "") and os.path.isabs(r)]


def _inside(path: str, rts: list[str]) -> str | None:
    return next((r for r in rts if path == r or path.startswith(r.rstrip("/") + "/")), None)


def _holds(path: str, rts: list[str]) -> str | None:
    """The protected path `path` is a folder of (or is)."""
    return _inside(path, rts) or next((r for r in rts if r.startswith(path.rstrip("/") + "/")), None)


def _hit(word: str, ctx, ancestor: bool = False) -> str | None:
    if not word or word.startswith("-"):
        return None
    p = expand(word, ctx[0], ctx[1])
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
    fed = outside if outside and INPUT_RE.search(code) else ""
    got = _py_refusal(code, ctx, _names(fed, ctx) if fed else None) if py else False
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

    def is_mod(n) -> bool:
        while isinstance(n, ast.Attribute):
            n = n.value
        return isinstance(n, ast.Name) and n.id in mods

    def opens(n) -> bool | None:
        """For an open-like call: whether it writes (None: not one)."""
        if _py_leaf(n.func) not in PY_OPENERS:
            return None
        own = isinstance(n.func, ast.Attribute) and not is_mod(n.func.value)   # a path's own open method, mode first
        modes = n.args[0 if own else 1:][:1] + [k.value for k in n.keywords if k.arg in ("mode", "flags", None)]
        return any(not isinstance(m, ast.Constant) or isinstance(m.value, str) and MODE_RE.fullmatch(m.value)
                   for m in modes)

    def content(n) -> bool:
        """A call whose value is a file's content, a handle to read or a fact about it: no path."""
        if not isinstance(n, ast.Call) or any(_py_leaf(x) in PY_INPUT for x in ast.walk(n)):
            return False
        return _py_leaf(n.func) in PY_READS or opens(n) is False

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
            if isinstance(x, ast.Constant) and isinstance(x.value, str):
                out.append(repr(x.value) + (" " + repr(fed) if fed and "$" in x.value else ""))
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
            for t in targets:
                if t is not None and (h := _names(text(t), ctx)):
                    return h
    except (OverflowError, RecursionError):
        return False
    return None


def shell_refusal(cmd: str, env, cwd: str, rts: list[str], depth: int = 0) -> str | None:
    """The protected path a shell command line (or script) writes, as far as its words show."""
    from . import hook
    ctx = (env, cwd, rts)
    commands, scan = hook._split(cmd)
    # Redirects: in the line as written and in scan (_split drops an echo's quoted words, a quoted
    # redirect target too), where the `>` is not quoted: quoted text is an argument (ssh's remote
    # command, a message); `sh -c` text is read below
    for text in (cmd, scan):
        flags = hook._quoting(text)
        for m in REDIRECT_RE.finditer(text):
            if not flags[m.start()][0] and (h := _hit(m[2], ctx)):
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
                h := shell_refusal(inner, env, cwd, rts, depth + 1)):
            return h
        # eval parses its words again: an escaped redirect (eval echo x \> f) is one then
        if name == "eval" and "\\" in cmd and depth < 2 and (
                h := shell_refusal(re.sub(r"\\(.)", r"\1", cmd), env, cwd, rts, depth + 1)):
            return h
    names = {os.path.basename(a[0]) for a in segments}
    if any(INTERPRETER_RE.match(n) for n in names):
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
        if h := next((h for c, py in chunks if (h := _code_refusal(c, ctx, depth, scan, py))), None):
            return h
    if depth < 2:
        for f in hook._scripts(segments, cwd):
            if os.path.realpath(f).startswith(PLUGIN_ROOT + os.sep):
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            # The shebang's interpreter (`#!/usr/bin/env bash`): a first line of code (import shutil) names none
            first = " ".join(os.path.basename(w) for w in text.split("\n", 1)[0][2:].split()) if text[:2] == "#!" else ""
            h = (shell_refusal(text, env, cwd, rts, depth + 1) if f.suffix == ".sh" or set(first.split()) & SHELLS
                 else _code_refusal(text, ctx, depth + 1, scan,
                                    f.suffix == ".py" or bool(re.search(r"python|pypy", first))))
            if h:
                return h
    return None


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
    return WHY.format(hit=hit) if hit else None


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

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Inline Python the shell guards need not read: a static proof that a whole snippet (`python3 -c '...'`,
`python3 - <<'EOF'`) is harmless. The guards read a command's words, so a snippet that only names a credential
folder or a protected path in its text, or reads its input, is refused like a command that writes there. A
snippet this module proves harmless is judged as `pass` instead (hook.pre_tool_use); the rest of the line is
judged as written. Proven means, from the code alone: it imports only modules without process, network or
import machinery, calls nothing that runs code it builds, touches no dunder or private name, and opens files
only at literal paths, reading none that is a credential store, a protected path or the system's live state,
and writing only under the working folder or the run's TMPDIR; and no folder python searches before the
standard library (the working folder, PYTHONPATH) holds a module named like a standard one. Anything else is
not proven: today's guards decide it, and the run-time judge (pyaudit) stays the net under both."""
from __future__ import annotations

import ast
import builtins
import importlib
import importlib.machinery
import os
import re
import shlex
import shutil
import sys
import types

# Modules a snippet may import, by their own name (`from shlex import os` is os). The pure ones may be used
# through any public attribute that is not a module or a command-line entry point (`base64.main` reads files);
# the ones that reach files, the process or time only through the attributes listed.
PURE = {"json", "re", "collections", "itertools", "functools", "math", "statistics", "datetime", "textwrap",
        "difflib", "hashlib", "base64", "binascii", "csv", "fnmatch", "enum",
        "heapq", "bisect", "decimal", "fractions", "pprint", "shlex", "ast", "tomllib", "html", "struct",
        "unicodedata", "zlib", "calendar", "copy", "ipaddress", "random", "keyword", "numbers", "abc"}
LISTED = {"os": {"path", "getcwd", "listdir", "scandir", "walk", "stat", "lstat", "sep", "linesep", "cpu_count",
                 "getpid", "fspath", "curdir", "pardir", "extsep", "name", "getenv", "makedirs", "mkdir"},
          "sys": {"stdin", "stdout", "stderr", "argv", "exit", "version", "version_info", "platform", "maxsize",
                  "byteorder", "executable", "float_info", "getrecursionlimit", "getsizeof"},
          "time": {"time", "monotonic", "perf_counter", "strftime", "strptime", "localtime", "gmtime", "ctime",
                   "mktime", "time_ns", "timezone", "tzname"},
          "glob": {"glob", "iglob", "escape"},
          "tempfile": {"gettempdir", "mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryDirectory"},
          "pathlib": {"Path", "PurePath", "PurePosixPath", "PosixPath"},
          "operator": {"itemgetter", "add", "sub", "mul", "truediv", "floordiv", "mod", "neg", "eq", "ne", "lt",
                       "le", "gt", "ge", "contains", "getitem"},
          os.path.__name__: {n for n, v in vars(os.path).items() if not n.startswith("_")
                             and not isinstance(v, types.ModuleType)}}
LISTING = {"os.listdir", "os.scandir", "os.walk", "glob.glob", "glob.iglob"}   # take a literal folder or pattern
# The builtins a snippet may name; open only as a call this checks. Every other builtin (eval, getattr, vars...)
# is refused, also where the snippet binds the name, since another scope may still mean the builtin.
BUILTINS = {"print", "len", "sorted", "min", "max", "sum", "range", "enumerate", "zip", "list", "dict", "set",
            "frozenset", "tuple", "str", "int", "float", "bool", "bytes", "abs", "round", "any", "all", "map",
            "filter", "reversed", "isinstance", "repr", "ord", "chr", "divmod", "hex", "oct", "bin", "iter", "next",
            "pow", "input", "exit", "Exception", "ValueError", "KeyError", "TypeError",
            "IndexError", "OSError", "FileNotFoundError", "StopIteration", "SystemExit", "RuntimeError", "open"}
ALL_BUILTINS = frozenset(dir(builtins))
# The attributes a snippet may use on a value that is not a module: str, bytes, list, dict, set, numbers, files
# it opened, re matches, Counter, deque, dates, hashes, csv, pure paths. Path reads and listings, `format` and
# a pathlib `replace` only as checked below.
METHODS = set("""split rsplit strip lstrip rstrip splitlines join startswith endswith lower upper title capitalize
casefold find rfind index rindex count replace partition rpartition isdigit isalpha isalnum isspace isupper islower
isnumeric isdecimal zfill ljust rjust center encode decode expandtabs removeprefix removesuffix hex fromhex append
extend insert pop remove sort reverse copy clear get items keys values setdefault update popitem fromkeys add discard
union intersection difference symmetric_difference issubset issuperset isdisjoint read readline readlines write
writelines close flush seek tell buffer match search fullmatch findall finditer sub subn group groups groupdict start
end span pattern most_common elements total subtract appendleft popleft rotate extendleft now utcnow today
fromtimestamp fromisoformat isoformat strftime strptime timestamp date year month day hour minute second
microsecond total_seconds days seconds utc hexdigest digest writerow writerows writeheader fieldnames real imag
bit_length is_integer major minor micro name suffix suffixes stem parent parents parts exists is_file is_dir
with_name with_suffix joinpath relative_to as_posix st_size st_mtime st_mode""".split())
PATH_READS = {"read_text", "read_bytes", "open"}
PATH_LISTS = {"iterdir", "glob", "rglob", "walk"}
FORMAT_FIELD_RE = re.compile(r"\{[^{}]*[.\[]")   # '{0.attr}', '{0[key]}': format() walks into its arguments
GLOB_RE = re.compile(r"[*?\[{]")
SYSTEM = ("/proc", "/sys", "/dev")
# The owner's harm bar (a project's own temp paths, plugin code and PYTHONPATH are not harm): the variables naming
# the run's own folders a snippet may read, the tempfile calls (they write under TMPDIR or a `dir` checked as a
# write), and the plugin's own judges (pure checks of a command or path) it may import from its own runtime folder.
ENV_PATHS = {"TTP_RUN_DIR", "TMPDIR", "HOME"}
ENV_SET_RE = re.compile(r"PYTHON|LD_|TTP_PYAUDIT|PATH$")   # what a snippet may not set in its environment
TEMP_MAKERS = {"tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.NamedTemporaryFile", "tempfile.TemporaryDirectory"}
MAKEDIRS = {"os.makedirs", "os.mkdir"}
PATH_WRITES = {"write_text", "write_bytes", "mkdir", "touch"}
JUDGES = {"pathguard", "credguard", "credwatch", "glmguard", "glmenv", "hook", "pyproof", "sidefx", "claimcheck",
          "untrusted", "gitguard", "localguard", "prguard", "routeguard", "ruleguard", "streamguard", "redact"}


def harmless(code: str, cwd: str, env=None, bases=None, own=(), audited=False) -> bool:
    """Whether the whole of `code` is proven harmless (module doc); any doubt is False. `bases` are the folders
    a relative path may start from (a `cd` before the snippet), else `cwd`. `own`: the plugin's own runtime
    folders python searches (its PYTHONPATH, a `cd` there); `audited`: the run-time judge (pyaudit) runs under the
    snippet, so a path it computes is read and judged then."""
    return _prove(code, cwd, env, bases, own, audited) is not None


def _prove(code: str, cwd: str, env=None, bases=None, own=(), audited=False) -> "_Proof | None":
    """The proof of `code` (its `wrote` says whether it opens a file for writing), None unless it is harmless."""
    try:
        proof = _Proof(cwd, os.environ if env is None else env, bases, own, audited)
        return proof if proof.ok(ast.parse(code)) else None
    except (SyntaxError, ValueError, RecursionError, OSError):
        return None


class _Proof:
    def __init__(self, cwd: str, env, bases, own=(), audited=False):
        self.cwd, self.env, self.bases = os.path.realpath(cwd), env, bases or [cwd]
        self.own, self.audited = bool(own), audited   # the plugin's own judges are importable; pyaudit runs
        self.syspath: set[int] = set()    # the `sys.path` nodes of a sys.path.insert of the plugin's own runtime
        self.mods: dict[str, str] = {}    # name bound by an import -> its module's own name
        self.funcs: dict[str, str] = {}   # name bound by `from m import f` -> `m.f`
        self.lits: dict[str, str] = {}    # name bound once, to a literal path
        self.paths: set[str] = set()      # name bound once, to a pathlib path of literal parts
        self.wrote = False                # a file is opened for writing

    def ok(self, tree: ast.AST) -> bool:
        nodes = list(ast.walk(tree))
        self.parents = {c: p for p in nodes for c in ast.iter_child_nodes(p)}
        for n in nodes:   # sys.path.insert(0, '<the plugin's own runtime folder>') at the top level, before an import
            if isinstance(n, ast.Expr) and isinstance(c := n.value, ast.Call) and isinstance(f := c.func, ast.Attribute) \
                    and f.attr in ("insert", "append") and isinstance(f.value, ast.Attribute) and f.value.attr == "path" \
                    and isinstance(f.value.value, ast.Name) and f.value.value.id == "sys" and not c.keywords \
                    and len(c.args) == (2 if f.attr == "insert" else 1) and isinstance(c.args[-1], ast.Constant) \
                    and isinstance(c.args[-1].value, str) and self.parents.get(n) is tree \
                    and any(_own_runtime(os.path.join(b, c.args[-1].value), [self.cwd, *self.bases]) for b in self.bases):
                self.syspath.add(id(f.value))
                self.own = True
        binds: dict[str, int] = {}        # every way a name is bound: a literal or an import must be bound once
        for n in nodes:
            for name in _names_bound(n):
                binds[name] = binds.get(name, 0) + 1
            if isinstance(n, ast.Import):
                for a in n.names:
                    mod = self._module(a.name if a.asname else a.name.split(".")[0])
                    if mod is None:
                        return False
                    self.mods[a.asname or a.name.split(".")[0]] = mod
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if n.level or a.name == "*" or (base := self._module(str(n.module))) is None:
                        return False
                    if (mod := self._module(f"{n.module}.{a.name}")) is not None:
                        self.mods[a.asname or a.name] = mod
                    elif not self._attr(base, a.name):
                        return False
                    else:
                        self.funcs[a.asname or a.name] = f"{base}.{a.name}"
            elif isinstance(n, (ast.Global, ast.Nonlocal)) or _string_annotation(n) \
                    or isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load):
                return False
        if any(binds[m] != 1 for m in [*self.mods, *self.funcs]):
            return False
        for n in nodes:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                    and binds[n.targets[0].id] == 1 and (v := self._path(n.value)) is not None:
                self.lits[n.targets[0].id] = v
                if self._pathish(n.value):
                    self.paths.add(n.targets[0].id)
        return all(self._node(n, self.parents.get(n)) for n in nodes)

    def _module(self, name: str) -> str | None:
        """_own_module, and the plugin's own judges (ttp.pathguard...) once its runtime folder is searched."""
        if self.own and (name == "ttp" or name.startswith("ttp.") and name.split(".")[1] in JUDGES):
            try:
                importlib.import_module(f"{__package__}{name[3:]}")
            except ImportError:
                return None
            return name if name.count(".") <= 1 else None
        return _own_module(name)

    def _node(self, n, p) -> bool:
        call = p if isinstance(p, ast.Call) and p.func is n else None
        if self._environ(n):   # os.environ only as one of the run's own folders, or set for a name python ignores
            key = p.slice.value if isinstance(p, ast.Subscript) and isinstance(p.slice, ast.Constant) else None
            if isinstance(p, ast.Subscript) and isinstance(key, str):
                return key in ENV_PATHS if isinstance(p.ctx, ast.Load) else not (ENV_SET_RE.search(key)
                                                                                    or key in ENV_PATHS)   # a self-set TMPDIR is no run folder
            return isinstance(p, ast.Attribute) and p.attr == "get" and isinstance(c := self.parents.get(p), ast.Call) \
                and c.func is p and self._env_get(c)
        if isinstance(n, ast.Attribute) and n.attr == "path" and id(n) in self.syspath:
            return True
        if isinstance(n, ast.Name):
            if n.id in self.mods:   # a module only as `mod.attr`
                return isinstance(p, ast.Attribute) and p.value is n
            if n.id in self.funcs:
                return self._checked(self.funcs[n.id], call)
            if n.id == "open":
                return call is not None and self._call(call)
            return n.id == "_" or not n.id.startswith("_") and (n.id in BUILTINS or n.id not in ALL_BUILTINS)
        if not isinstance(n, ast.Attribute):
            return True
        if (base := self._dotted(n.value)) is not None:
            return self._attr(base, n.attr) and self._checked(f"{base}.{n.attr}", call)
        if n.attr in PATH_READS | PATH_LISTS | PATH_WRITES:   # on a pathlib path of literal parts only
            path = self._path(n.value) if self._pathish(n.value) else None
            if call is None or path is None and not (self.audited and self._pathish(n.value)):
                return False
            if path is None:   # computed: the run-time judge reads or writes it
                self.wrote |= n.attr in PATH_WRITES or n.attr == "open"   # its mode is its first argument
                return True
            if n.attr in PATH_WRITES:
                return self._place(path, True)
            if n.attr in PATH_LISTS:
                return self._list(call, n.attr, path)
            return self._open(path, (call.args or [None])[0] if n.attr == "open" else None, call)
        if n.attr in ("format", "format_map"):
            return isinstance(n.value, ast.Constant) and not FORMAT_FIELD_RE.search(str(n.value.value))
        if n.attr == "replace" and "pathlib" in {*self.mods.values(), *(f.split(".")[0] for f in self.funcs.values())}:
            return call is not None and len(call.args) >= 2 and not call.keywords   # str's, never Path.replace
        return n.attr in METHODS

    def _dotted(self, node) -> str | None:
        """The module an expression is (`os.path` as posixpath), when it is one."""
        if isinstance(node, ast.Name):
            return self.mods.get(node.id)
        if isinstance(node, ast.Attribute) and (b := self._dotted(node.value)):
            return self._module(f"{b}.{node.attr}")
        return None

    def _checked(self, full: str, call) -> bool:
        """A call of a module function that takes a path: a listing, a temp file, a folder made."""
        if full in LISTING:
            return call is not None and self._list(call, full)
        if full == "os.getenv":
            return self._env_get(call)
        if full in TEMP_MAKERS:   # under TMPDIR, or a `dir` checked as a write; no module name
            return call is not None and not call.args and all(
                k.arg in ("dir", "prefix", "suffix", "mode", "delete", "text", "encoding", "ignore_cleanup_errors")
                for k in call.keywords) and all(
                (p := self._path(k.value)) is not None and self._place(p, True) if k.arg == "dir" else
                isinstance(k.value, ast.Constant) and ("/" not in str(k.value.value)) and not str(k.value.value)
                .endswith((*importlib.machinery.all_suffixes(), ".pth")) for k in call.keywords
                if k.arg in ("dir", "prefix", "suffix"))
        if full in MAKEDIRS:
            return call is not None and len(call.args) == 1 and all(k.arg in ("exist_ok", "mode") for k in call.keywords) \
                and (p := self._path(call.args[0])) is not None and self._place(p, True)
        return True

    def _environ(self, node) -> bool:
        return isinstance(node, ast.Attribute) and node.attr == "environ" and self._dotted(node.value) == "os"

    def _env_get(self, call) -> bool:
        """os.environ.get / os.getenv of one of the run's own folders, by a literal name."""
        return isinstance(call, ast.Call) and not call.keywords and 1 <= len(call.args) <= 2 \
            and isinstance(call.args[0], ast.Constant) and call.args[0].value in ENV_PATHS

    def _attr(self, mod: str, attr: str) -> bool:
        if mod == "ttp" or mod.startswith("ttp."):   # the plugin's own judges: their helpers too, never a dunder
            return f"{mod}.{attr}" == self._module(f"{mod}.{attr}") if mod == "ttp" else not attr.startswith("__") \
                and getattr(sys.modules.get(f"{__package__}{mod[3:]}"), attr, None) is not None
        if mod in LISTED:
            return attr in LISTED[mod]
        value = getattr(sys.modules.get(mod), attr, None)
        return not attr.startswith("_") and attr not in ("main", "test") and value is not None \
            and not isinstance(value, types.ModuleType)

    def _call(self, call: ast.Call) -> bool:
        path = self._path(call.args[0]) if call.args else None
        if path is None and call.args and self.audited:   # computed: the run-time judge reads or writes it
            self.wrote |= _writes(call)
            return len(call.args) <= 2 and all(k.arg in ("mode", "encoding", "errors", "newline") for k in call.keywords)
        return len(call.args) <= 2 and self._open(path, call.args[1] if len(call.args) > 1 else None, call)

    def _open(self, path: str | None, mode, call: ast.Call) -> bool:
        if path is None or any(k.arg not in ("mode", "encoding", "errors", "newline") for k in call.keywords):
            return False
        mode = mode or next((k.value for k in call.keywords if k.arg == "mode"), None)
        if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            return False
        return self._place(path, bool(mode is not None and set(mode.value) & set("wax+")))

    def _list(self, call: ast.Call, func: str, folder: str = ".") -> bool:
        """A listing of a literal folder or pattern (none: the working folder), checked like a Glob tool call."""
        if any(k.arg not in ("recursive", "topdown", "include_hidden") for k in call.keywords) or len(call.args) > 1:
            return False
        arg = self._path(call.args[0]) if call.args else "."
        if arg is None:   # computed: only where the run-time judge reads it
            return self.audited
        if func in ("glob.glob", "glob.iglob", "glob", "rglob"):
            fixed = GLOB_RE.split(arg, maxsplit=1)[0]
            arg = fixed.rsplit("/", 1)[0] if "/" in fixed else "."
        return self._place(os.path.join(folder, arg), False, search=True)

    def _own_module(self, path: str) -> bool:
        """A module file of the worktree's own code: inside the working folder, named like no standard or start-up
        module python could load from there instead (`json.py`, `json/__init__.py`, `sitecustomize.py`), no `.pth`."""
        real = os.path.realpath(os.path.join(self.cwd, os.path.expanduser(path)))
        name = os.path.relpath(real, self.cwd).split(os.sep, 1)[0].split(".", 1)[0]   # what an import names
        return not real.endswith(".pth") and real.startswith(os.path.realpath(self.cwd).rstrip("/") + "/") \
            and name not in (*sys.stdlib_module_names, *sys.builtin_module_names, "sitecustomize", "usercustomize") \
            and not name.startswith("_")

    def _place(self, path: str, writes: bool, search: bool = False) -> bool:
        from . import credguard, hook, pathguard, untrusted
        home = str(self.env.get("HOME") or os.path.expanduser("~"))
        path = os.path.expanduser(path) if path.startswith("~") else path
        mine = [os.path.realpath(r).rstrip("/") + "/" for r in [self.cwd, *filter(None, (
            self.env.get("TMPDIR"), self.env.get("TTP_RUN_DIR")))]]
        self.wrote |= writes
        if writes and path.endswith((*importlib.machinery.all_suffixes(), ".pth")) and not self._own_module(path):
            return False   # a module python may import in place of the one it names
        for base in self.bases:
            real = os.path.realpath(os.path.join(base, path))
            if os.path.basename(real) == untrusted.RUN_MARK:   # the run's mark of untrusted input: the guards judge it
                return False
            # the guards' own reading of the path as a word of a shell command: a store by name anywhere
            if credguard.read_refusal(real, search, home) or pathguard._inside(real, pathguard.roots(self.env)) \
                    or any(hook.store_refusal(f"cat {shlex.quote(p)}", base) for p in (path, real)):
                return False
            if not any((real + "/").startswith(m) for m in mine) and (
                    writes or any(real == s or real.startswith(s + "/") for s in SYSTEM)):
                return False
        return True

    def _path(self, node) -> str | None:
        """The literal path an expression names (a string, Path('a', 'b'), Path(x) / 'y', os.path.join of
        literals, a name bound once to one), else None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
            return node.value
        if isinstance(node, ast.Name):
            return self.lits.get(node.id)
        if isinstance(node, ast.Subscript) and self._environ(node.value) and isinstance(node.slice, ast.Constant) \
                and node.slice.value in ENV_PATHS:
            return self.env.get(node.slice.value) or None
        if isinstance(node, ast.Call) and (self._func(node) == "os.getenv" or isinstance(node.func, ast.Attribute)
                                           and node.func.attr == "get" and self._environ(node.func.value)):
            got = self.env.get(node.args[0].value) if self._env_get(node) else None
            return got or (self._path(node.args[1]) if got is None and len(node.args) == 2 else None)
        if isinstance(node, ast.Call) and self._func(node) in ("tempfile.gettempdir", "tempfile.mkdtemp"):
            import tempfile
            d = next((self._path(k.value) for k in node.keywords if k.arg == "dir"), None)
            tmp = d or self.env.get("TMPDIR") or tempfile.gettempdir()
            return tmp if self._func(node) == "tempfile.gettempdir" else os.path.join(tmp, "tmp.made")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and not self._pathish(node.left):
            a, b = self._path(node.left), self._path(node.right)   # string concatenation
            return a + b if a is not None and b is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = [v.value if isinstance(v, ast.Constant) else self._path(v.value) if not v.format_spec
                     and v.conversion == -1 else None for v in node.values]
            return "".join(parts) if parts and None not in parts else None
        if isinstance(node, ast.Call) and self._func(node) == f"{os.path.__name__}.expanduser" and len(node.args) == 1 \
                and (lit := self._path(node.args[0])) is not None:
            home = str(self.env.get("HOME") or os.path.expanduser("~"))
            return home + lit[1:] if lit == "~" or lit.startswith("~/") else lit
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            a, b = self._path(node.left), self._path(node.right)
            return os.path.join(a, b) if a is not None and b is not None and self._pathish(node.left) else None
        if isinstance(node, ast.Call) and not node.keywords and node.args and (
                self._pathish(node) or self._func(node) == f"{os.path.__name__}.join"):
            parts = [self._path(a) for a in node.args]
            return os.path.join(*parts) if None not in parts else None
        return None

    def _func(self, call: ast.Call) -> str | None:
        f = call.func
        if isinstance(f, ast.Attribute) and (b := self._dotted(f.value)):
            return f"{b}.{f.attr}"
        return self.funcs.get(f.id) if isinstance(f, ast.Name) else None

    def _pathish(self, node) -> bool:
        """`node` builds a pathlib path from literal parts."""
        if isinstance(node, ast.Call):
            return (self._func(node) or "").startswith("pathlib.") and (self._func(node) or "").split(".")[-1] \
                in LISTED["pathlib"]
        if isinstance(node, ast.BinOp):
            return self._pathish(node.left)
        return isinstance(node, ast.Name) and node.id in self.paths


def _writes(call: ast.Call) -> bool:
    """An open() call's mode may write (a mode not written as a literal may)."""
    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    return mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                     and not set(mode.value) & set("wax+"))


def _own_runtime(folder: str, inside) -> bool:
    """`folder` is the plugin's own runtime (it holds the ttp package and its judges): this module's own, or one
    inside the folders the command works in (the worktree, the project's harness), shadowing no standard module."""
    real = os.path.realpath(folder)
    mine = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.isfile(os.path.join(real, "ttp", "pyproof.py")) and not _holds_module(real) and (
        real == mine or any((real + "/").startswith(os.path.realpath(i).rstrip("/") + "/") for i in inside))


def _names_bound(n) -> list[str]:
    """The names node `n` binds, in any of Python's ways (parameters, except-as, match captures, defs)."""
    if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
        return [n.id]
    if isinstance(n, ast.arg):
        return [n.arg]
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [n.name]
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name.split(".")[0] for a in n.names]
    name = getattr(n, "rest" if isinstance(n, ast.MatchMapping) else "name", None)
    return [name] if isinstance(name, str) and isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar,
                                                              ast.MatchMapping)) else []


def _string_annotation(n) -> bool:
    """An annotation written as a string: typing and functools evaluate those as code at run time."""
    notes = [n.annotation] if isinstance(n, (ast.arg, ast.AnnAssign)) else [n.returns] if isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
    return any(isinstance(c, ast.Constant) and isinstance(c.value, str) for a in notes if a for c in ast.walk(a))


def _own_module(name: str) -> str | None:
    """The own name of the module `name` reaches, when it is one a snippet may use (PURE, LISTED), else None."""
    root, *rest = name.split(".")
    if root not in PURE | set(LISTED):
        return None
    try:
        obj = sys.modules.get(root) or importlib.import_module(root)
    except ImportError:
        return None
    for part in rest:
        obj = getattr(obj, part, None)
    own = getattr(obj, "__name__", None) if isinstance(obj, types.ModuleType) else None
    return own if own in PURE | set(LISTED) else None


PY_RE = re.compile(r"python[\d.]*$")
DASH_C_RE = re.compile(r"(?<![^\s])-c\s+(?:'([^']*)'|\"([^\"$`\\]*)\")")


def rewrite(cmd: str, cwd: str, env=None) -> str | None:
    """`cmd` with each inline Python snippet proven harmless written as `pass`, for the guards to judge; None
    when none is. Only text the shell passes as is: a heredoc with a quoted delimiter (or a body with no `$`,
    backtick or backslash) that python reads as its script, and the code of python's own `-c` in single quotes
    (or double quotes with none of those). Every other byte of the line stays."""
    from . import credguard, hook
    if "\\\n" in cmd or "python" not in cmd:
        return None
    env = os.environ if env is None else env
    cmd, prefixed = _own_pythonpath(cmd, cwd)
    segments = hook._segments(_run_vars(cmd, env), strip=False)   # a cd before the snippet: its paths start there
    moves = any(argv and (os.path.basename(argv[0]) in ("cd", "pushd", "popd")
                          or any(w.startswith(("-C", "--chdir")) for w in argv)) for argv in segments)
    bases = credguard._cwds(segments, cwd, str(env.get("HOME") or os.path.expanduser("~"))) if moves else None
    if bases and len(bases) >= credguard.CWDS_CAP:   # more folders than the guard follows: no proof
        return None
    lines, bodies = hook._heredocs(cmd)
    if any(b.end is None for b in bodies):   # an unended heredoc: the shell reads it differently than we would
        return None
    shadow_cwds = bases[1:] if bases else [cwd]

    def shadowed(i: int, pos: int) -> bool:
        """python may import another module than the standard one the snippet at `pos` of line `i` names: read on
        the command up to the end of its pipeline (what runs before or beside it); what runs after it cannot."""
        return _may_shadow(_run_vars("\n".join([*lines[:i], lines[i][:_pipeline_end(lines[i], pos)]]), env),
                           shadow_cwds, env, prefixed)
    own = prefixed or any(_own_runtime(d, [cwd, *(bases or [])]) for d in [*(bases or [cwd]), *[
        e for e in str(env.get("PYTHONPATH") or "").split(os.pathsep) if e]])
    audited = _audited(env) and not prefixed and not re.search(r"(?<![^\s;&|(])env\s+-", cmd)
    changed = hidden = False
    proven = set()   # the python commands proven, by line and command start
    for b in bodies:
        i, body, pos = b
        m = hook.HEREDOC_RE.match(lines[i], pos)
        argv = list(hook._first_argv(hook._piece_at(lines[i], pos)[0]))
        fed = bool(argv) and bool(PY_RE.match(os.path.basename(argv[0]))) and hook._interpreter_args(argv) == "-"
        text = "\n".join(l.lstrip("\t") if m[1] == "-" else l for l in body)
        if fed and text.strip() and (m[2] or not re.search(r"[$`\\]", text)) and not shadowed(i, pos) \
                and (proof := _prove(text, cwd, env, bases, own, audited and _keeps_site(argv))):
            named = _words_name_store(text, cwd, env)
            if named and proof.wrote:   # it could leave a script naming the store for a later call
                return None
            hidden |= named
            body[:] = ["pass"]
            changed = True
            proven.add((i, _piece_start(lines[i], pos)))
    ends: dict[int, list[str]] = {}   # the lines each heredoc leaves after its line: its body, then its end
    for b in bodies:
        ends.setdefault(b[0], []).extend([*b[1], b.end])
    out = []
    for i, line in enumerate(lines):
        flags = hook._quoting(line)
        for m in reversed(list(DASH_C_RE.finditer(line))):
            code = m[1] if m[1] is not None else m[2]
            argv = list(hook._first_argv(hook._piece_at(line, m.start())[0]))
            if not flags[m.start()][0] and _code_at(line, m.start()) == code and not shadowed(i, m.start()) \
                    and (proof := _prove(code, cwd, env, bases, own, audited and _keeps_site(argv))):
                proven.add((i, _piece_start(line, m.start())))
                line, changed = f"{line[:m.start()]}-c 'pass'{line[m.end():]}", True
                named = _words_name_store(code, cwd, env)
                if named and proof.wrote:
                    return None
                hidden |= named
        out += [line, *ends.get(i, [])]
    # Another python of the command (one not proven) could write a module a proven one then imports: it may run
    # only after every proven one has ended. Each piece of a chain is judged on its own (the rest of the line as
    # written), so the chain passes once its operators add no harm.
    if not _after(lines, proven, sum(any(PY_RE.match(os.path.basename(w)) for w in argv) for argv in segments)):
        return None
    # A snippet naming a store the guard would have seen in its words: its output piped into a program that runs
    # it, captured, or redirected into a file a later command could run could feed that name to the shell.
    if hidden and _feeds(lines):
        return None
    return "\n".join(out) if changed else None


def _own_pythonpath(cmd: str, cwd: str) -> tuple[str, bool]:
    """`cmd` without a `PYTHONPATH=<folder> python...` prefix whose folders are the plugin's own runtime or the
    working folder (`$PWD` read as it while no `cd` came first), and whether there was one: python then searches
    the plugin's own code, so a snippet may import its judges. The override drops the run-time judge's zip."""
    m = re.search(r"(?<![^\s;&|(])PYTHONPATH=([^\s;&|'\"`]+)\s+(?=(?:\S*/)?python[\d.]*\s)", cmd)
    if not m or re.search(r"(?<![^\s;&|(])(cd|pushd)\s", cmd[:m.start()]):
        return cmd, False
    entries = re.sub(r"\$\{?PWD\}?(?![\w])", cwd, m[1]).split(os.pathsep)
    if not all(e and "$" not in e and (_own_runtime(os.path.join(cwd, e), [cwd]) or os.path.realpath(
            os.path.join(cwd, e)) == os.path.realpath(cwd) and not _holds_module(cwd)) for e in entries):
        return cmd, False
    return cmd[:m.start()] + cmd[m.end():], True


def _audited(env) -> bool:
    """The run-time judge (pyaudit) loads in a python started with `env`: its zip first on PYTHONPATH."""
    from . import pyaudit
    dirs = pyaudit.guarded(env)
    return bool(dirs) and str(env.get("PYTHONPATH") or "").split(os.pathsep)[0] == os.path.join(dirs[0], "pyaudit.zip")


def _keeps_site(argv: list[str]) -> bool:
    """python's own options before its code keep the site hook that loads the run-time judge (no -I, -E, -S)."""
    for w in argv[1:]:
        if w in ("-c", "-", "-m") or not w.startswith("-"):
            return True
        if w.startswith("--") or set(w[1:]) & set("IESX") or "c" in w[1:]:
            return False
    return True


def _after(lines: list[str], proven: set, count: int) -> bool:
    """Every python of the command that is not proven runs only after each proven one has ended: none before
    one, in its pipeline or beside it in the background (every python a top-level command of its line)."""
    from . import hook
    pys = [(i, s, sep) for i, line in enumerate(lines) for s, e, sep in hook._pieces(line)
           if (argv := list(hook._first_argv(line[s:e]))) and PY_RE.match(os.path.basename(argv[0]))]
    if len(proven) >= count:
        return True
    if len(pys) != count:
        return False
    seen = False   # a proven python came first, with no `;`, `&&`, `||` or line break after it yet
    for i, s, sep in pys:
        if (i, s) not in proven:
            if seen:
                return False
            continue
        if any((j, t) not in proven for j, t, _ in pys if (j, t) < (i, s)):
            return False
        seen = sep in ("|", "|&", "&")
    return True


def _feeds(lines: list[str]) -> bool:
    """The command's output may reach the shell: captured (`$(`, a backtick, `<(`), piped into anything but a
    reader, or redirected into a file (not a terminal) a later command could run."""
    from . import hook
    for line in lines:
        flags = hook._quoting(line)
        plain = "".join(" " if flags[k][0] else c for k, c in enumerate(line))
        if re.search(r"`|\$\(|[<>]\(", plain):
            return True
        for t in re.findall(r"(?<![<>])>{1,2}\|?\s*(&?[^\s;&|<>]*)", plain):
            if not (t.startswith("&") or t in TERMINAL):
                return True
        pieces = hook._pieces(line)
        for (s, e, sep), nxt in zip(pieces, pieces[1:]):
            argv = list(hook._first_argv(line[nxt[0]:nxt[1]]))
            if sep in ("|", "|&") and not (argv and os.path.basename(argv[0]) in READERS - {"echo"}):
                return True
    return False


# bash's builtins and keywords (compgen -b -k): those but the few here may change the shell's folder or variables
SHELL_WORDS = frozenset(""". : [ alias bg bind break builtin caller cd command compgen complete compopt continue declare
    dirs disown echo enable eval exec exit export false fc fg getopts hash help history jobs kill let local logout mapfile
    popd printf pushd pwd read readarray readonly return set shift shopt source suspend test times trap true type typeset
    ulimit umask unalias unset wait if then else elif fi case esac for select while until do done in function time { }
    ! [[ ]] coproc""".split()) - {":", "[", "echo", "false", "pwd", "test", "true"}
WRAPPERS = {"env", "nohup", "timeout", "nice", "stdbuf"}   # run python in the shell's folder and environment
ASSIGN_RE = re.compile(r"[A-Za-z_]\w*=")
UNQUOTED_RE = re.compile(r"\\.|'[^']*'|\"((?:\\.|[^\"\\])*)\"")
TERMINAL = ("-", "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")   # redirect targets that write no file
SHELL_BUILTINS = {"cd", "pushd", "echo", "true", "false", "pwd", "test", "[", ":"}   # the shell runs these itself
READERS = {"cat", "echo", "ls", "head", "tail", "grep", "wc", "true", "pwd"}   # write no file without a redirect
# Commands that create no module python could import (none, or only folders and deletions): sed and sort unless
# their arguments write a file (SED_WRITE, -o), and the plugin's own CLI.
NO_MODULE = READERS | {"sed", "sort", "cut", "tr", "jq", "column", "nl", "stat", "rm", "rmdir", "mkdir", "sleep", "date",
                       "basename", "dirname", "realpath", "readlink", "diff", "cmp", "file", "du", "df", "ps", "ttp"}
SED_WRITE = re.compile(r"^-(i|-in-place)|(^|[/;}\s\d])[wWe](\s|$)")


RUN_VARS = ("TTP_RUN_DIR", "TMPDIR")


def _run_vars(shell: str, env) -> str:
    """`shell` with the run's own folders (`$TTP_RUN_DIR`, `${TMPDIR}`) written as their values where the command
    never sets, reads into or unsets that name: there they name one known folder, like a literal path."""
    for name in RUN_VARS:
        value = str(env.get(name) or "")
        if not re.fullmatch(r"/[\w./@:+,=%-]*", value) or re.search(
                rf"\b{name}\+?=|\b(?:read|declare|typeset|local|unset|export|for|getopts|mapfile|printf\s+-v)\b[^;&|\n]*"
                rf"\b{name}\b", shell):
            continue
        shell = re.sub(rf"\$(?:\{{{name}\}}|{name}(?!\w))", lambda _: value, shell)
    return shell


def _may_shadow(shell: str, cwds: list[str], env, prefixed: bool = False) -> bool:
    """Whether python may import something other than the standard module a snippet (or a module it imports)
    names: it searches its working folder and each PYTHONPATH entry first, so a module named like a standard
    one there (one an earlier call wrote) runs instead. So also whenever the command may give python another
    environment or folder than the ones read here: an expansion, a builtin that sets variables or moves, a
    wrapper with options. `shell` is the command without its heredoc bodies."""
    from . import hook, pyaudit
    expanded = UNQUOTED_RE.sub(lambda m: re.sub(r"\\.", "", m[1] or ""), shell)   # what the shell may expand
    if re.search(r"[$`]|\(\(", expanded) or re.search(r"PYTHON|CDPATH", re.sub(r"['\"\\{},]", "", shell)) \
            or env.get("CDPATH") or not all(map(os.path.isabs, str(env.get("PATH") or os.defpath).split(os.pathsep))):
        return True
    for argv in hook._segments(shell, strip=False):
        if argv and ASSIGN_RE.match(argv[0]):   # an exported variable (PATH, LD_PRELOAD) python could read
            return True
        py = next((i for i, w in enumerate(argv) if PY_RE.match(os.path.basename(w))), None)
        if py and argv[0] == "env":   # `env -u NAME`: unsets a variable python need not have, never one it reads
            unset = argv[1:py]
            if len(unset) % 2 or any(f != "-u" or ENV_SET_RE.search(n) for f, n in zip(unset[::2], unset[1::2])):
                return True
            argv = [argv[0], *argv[py:]]
            py = 1
        head = argv[:py] if py else argv[:2] if argv[:1] in (["cd"], ["pushd"]) else argv[:1]
        if any(re.search(r"[*?\[{~]", w) for w in head) or argv[:1] in (["cd"], ["pushd"]) and (
                len(argv) != 2 or argv[1].startswith("-")) or head and head[0] in SHELL_WORDS - {"cd", "pushd"} \
                or py and any(w not in WRAPPERS and not w.isdigit() for w in head) \
                or py is not None and not _interpreter(argv[py], env) \
                or any(w not in SHELL_BUILTINS and not w.isdigit() and not _trusted(w, env) and not _own_cli(w, cwds)
                       for w in argv[:1 if py is None else py]):
            return True
    if _plants(shell):
        return True
    path = "" if prefixed else str(env.get("PYTHONPATH") or "")   # a prefix's own folders were checked
    judge = [os.path.join(d, "pyaudit.zip") for d in pyaudit.guarded(env)]   # the run-time judge's own, protected
    entries = [""] + [e for e in (path.split(os.pathsep) if path else []) if e not in judge]
    user = env.get("PYTHONUSERBASE") or os.path.join(str(env.get("HOME") or os.path.expanduser("~")), ".local")
    site = os.path.join(user, "lib", f"python{sys.version_info[0]}.{sys.version_info[1]}", "site-packages")
    return _holds_module(site) or _runs_at_start(site) \
        or any(_holds_module(os.path.join(c, e)) for c in cwds for e in entries)


def _plants(shell: str) -> bool:
    """Whether a command that may write a file (anything but a reader, or a redirect into a file) runs before
    a python of the command, or beside it in its pipeline: what it writes is not in the folders read here."""
    lex = shlex.shlex(shell.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        words = list(lex)
    except ValueError:
        return True
    ran = writes = py = False   # ran: a writer ran before this pipeline; writes, py: one is in it
    start, target, cur = True, "", ""
    for w in words + [";"]:
        if set(w) <= set("();<>|&"):
            if ">" in w or "<" in w:
                target = w
            elif w in ("|", "|&"):
                start = True
            else:
                if writes and py:
                    return True
                ran, writes, py, start = ran or writes, False, False, True
        elif target:
            writes |= ">" in target and not (w.isdigit() or w in TERMINAL)
            target = False
        elif not start:
            writes |= cur == "sed" and bool(SED_WRITE.search(w)) or cur == "sort" and w.startswith(("-o", "--output"))
        elif start:
            name = os.path.basename(w)
            if PY_RE.match(name):
                if ran:
                    return True
                py, start = True, False
            elif not (w.isdigit() or name in WRAPPERS):
                writes |= name not in NO_MODULE | {"cd", "pushd"}
                start, cur = False, name
    return False


def _pipeline_end(line: str, pos: int) -> int:
    """Where the pipeline holding `pos` ends: the end of its piece and of each piece joined to it by `|`, `|&` or
    a background `&` (they run beside it)."""
    from . import hook
    end = len(line)
    for s, e, sep in hook._pieces(line):
        if e >= pos:
            end = e
            if sep not in ("|", "|&", "&"):
                break
    return end


def _piece_start(line: str, pos: int) -> int:
    from . import hook
    return next(s for s, e, _ in hook._pieces(line) if s <= pos <= e)


def _trusted(word: str, env) -> bool:
    """`word` runs a program from a folder this user cannot change: none an earlier call planted (a python, an
    `env` or `cat` that writes a module first), no venv whose site folder could hold a .pth python runs at start."""
    exe = shutil.which(word, path=str(env.get("PATH") or os.defpath))
    real = exe and os.path.realpath(exe)
    return bool(real) and not any(os.access(p, os.W_OK) for f in (exe, real) for p in (f, os.path.dirname(f)))


def _own_cli(word: str, cwds: list[str]) -> bool:
    """`word` runs the plugin's own command-line tool (`ttp` beside its runtime folder), the project's own code."""
    return os.path.basename(word) == "ttp" and "/" in word and any(_own_runtime(os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(os.path.join(c, word)))), "runtime"), [c]) for c in cwds)


def _interpreter(word: str, env) -> bool:
    """`word` runs a python this check reads for: the checker's own version (its standard names and user site
    folder), from a folder this user cannot change."""
    exe = shutil.which(word, path=str(env.get("PATH") or os.defpath))
    return bool(exe) and os.path.basename(os.path.realpath(exe)) == "python{}.{}".format(*sys.version_info[:2]) \
        and _trusted(word, env)


def _runs_at_start(folder: str) -> bool:
    """`folder` (the user's site folder) holds a .pth file with a line python runs at start (`import ...`), or one
    naming a folder that holds a module python runs at start or imports (_holds_module), as site.addpackage reads it."""
    try:
        pths = [os.path.join(folder, e) for e in os.listdir(folder) if e.endswith(".pth")]
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        lines = [line.rstrip() for p in pths for line in open(p, errors="replace") if not line.startswith("#")]
    except OSError:
        return True
    return any(line.startswith(("import ", "import\t"))   # a path line: the folder site.makepath adds
               or line and _holds_module(os.path.abspath(os.path.join(folder, line))) for line in lines)


def _holds_module(folder: str) -> bool:
    """`folder` holds a module or regular package named like a standard module, or one python runs at start;
    True when it is not a folder it can list (a zip file on PYTHONPATH is searched too)."""
    try:
        entries = os.listdir(folder)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    names = getattr(sys, "stdlib_module_names", None)   # None (before 3.10): any module may shadow one
    std = lambda n: names is None or n in names or n in ("sitecustomize", "usercustomize")
    suffixes = importlib.machinery.all_suffixes()
    return any(any(e.endswith(s) and std(e[:-len(s)]) for s in suffixes)
               or std(e) and any(os.path.exists(os.path.join(folder, e, "__init__" + s)) for s in suffixes)
               for e in entries)


def _words_name_store(code: str, cwd: str, env) -> bool:
    """`code`'s words name a credential store or a protected path, as the shell guards read a command's words."""
    from . import hook, pathguard, untrusted
    words = re.findall(r"[^\s'\"(),\[\]{}]+", code)
    return untrusted.RUN_MARK in code or bool(hook.store_refusal(f"cat {shlex.join(words)}", cwd)) or any(
        pathguard._inside(os.path.realpath(os.path.join(cwd, os.path.expanduser(w))), pathguard.roots(env))
        for w in words if "/" in w)


def _code_at(line: str, pos: int) -> str | None:
    """The code python's own `-c` runs in the simple command at `pos`, when that command is python."""
    from . import hook
    argv = list(hook._first_argv(hook._piece_at(line, pos)[0]))
    if not argv or not PY_RE.match(os.path.basename(argv[0])) or hook._interpreter_args(argv) != "":
        return None
    i = next((i for i, a in enumerate(argv[1:-1], 1) if a in ("-c", "-m") or not a.startswith("-")), None)
    return argv[i + 1] if i and argv[i] == "-c" else None

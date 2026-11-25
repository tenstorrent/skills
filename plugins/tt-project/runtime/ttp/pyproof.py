# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Inline Python the shell guards need not read: a static proof that a whole snippet (`python3 -c '...'`,
`python3 - <<'EOF'`) is harmless. The guards read a command's words, so a snippet that only names a credential
folder or a protected path in its text, or reads its input, is refused like a command that writes there. A
snippet this module proves harmless is judged as `pass` instead (hook.pre_tool_use); the rest of the line is
judged as written. Proven means, from the code alone: it imports only modules without process, network or
import machinery, calls nothing that runs code it builds, touches no dunder or private name, and opens files
only at literal paths, reading none that is a credential store, a protected path or the system's live state,
and writing only under the working folder or the run's TMPDIR. Anything else is not proven: today's guards
decide it, and the run-time judge (pyaudit) stays the net under both."""
from __future__ import annotations

import ast
import builtins
import importlib
import os
import re
import shlex
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
                 "getpid", "fspath", "curdir", "pardir", "extsep", "name"},
          "sys": {"stdin", "stdout", "stderr", "argv", "exit", "version", "version_info", "platform", "maxsize",
                  "byteorder", "executable", "float_info", "getrecursionlimit", "getsizeof"},
          "time": {"time", "monotonic", "perf_counter", "strftime", "strptime", "localtime", "gmtime", "ctime",
                   "mktime", "time_ns", "timezone", "tzname"},
          "glob": {"glob", "iglob", "escape"},
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


def harmless(code: str, cwd: str, env=None, bases=None) -> bool:
    """Whether the whole of `code` is proven harmless (module doc); any doubt is False. `bases` are the folders
    a relative path may start from (a `cd` before the snippet), else `cwd`."""
    return _prove(code, cwd, env, bases) is not None


def _prove(code: str, cwd: str, env=None, bases=None) -> "_Proof | None":
    """The proof of `code` (its `wrote` says whether it opens a file for writing), None unless it is harmless."""
    try:
        proof = _Proof(cwd, os.environ if env is None else env, bases)
        return proof if proof.ok(ast.parse(code)) else None
    except (SyntaxError, ValueError, RecursionError, OSError):
        return None


class _Proof:
    def __init__(self, cwd: str, env, bases):
        self.cwd, self.env, self.bases = os.path.realpath(cwd), env, bases or [cwd]
        self.mods: dict[str, str] = {}    # name bound by an import -> its module's own name
        self.funcs: dict[str, str] = {}   # name bound by `from m import f` -> `m.f`
        self.lits: dict[str, str] = {}    # name bound once, to a literal path
        self.paths: set[str] = set()      # name bound once, to a pathlib path of literal parts
        self.wrote = False                # a file is opened for writing

    def ok(self, tree: ast.AST) -> bool:
        nodes = list(ast.walk(tree))
        binds: dict[str, int] = {}        # every way a name is bound: a literal or an import must be bound once
        for n in nodes:
            for name in _names_bound(n):
                binds[name] = binds.get(name, 0) + 1
            if isinstance(n, ast.Import):
                for a in n.names:
                    mod = _own_module(a.name if a.asname else a.name.split(".")[0])
                    if mod is None:
                        return False
                    self.mods[a.asname or a.name.split(".")[0]] = mod
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if n.level or a.name == "*" or (base := _own_module(str(n.module))) is None:
                        return False
                    if (mod := _own_module(f"{n.module}.{a.name}")) is not None:
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
        parents = {c: p for p in nodes for c in ast.iter_child_nodes(p)}
        return all(self._node(n, parents.get(n)) for n in nodes)

    def _node(self, n, p) -> bool:
        call = p if isinstance(p, ast.Call) and p.func is n else None
        if isinstance(n, ast.Name):
            if n.id in self.mods:   # a module only as `mod.attr`
                return isinstance(p, ast.Attribute) and p.value is n
            if n.id in self.funcs:
                return self.funcs[n.id] not in LISTING or call is not None and self._list(call, self.funcs[n.id])
            if n.id == "open":
                return call is not None and self._call(call)
            return n.id == "_" or not n.id.startswith("_") and (n.id in BUILTINS or n.id not in ALL_BUILTINS)
        if not isinstance(n, ast.Attribute):
            return True
        if (base := self._dotted(n.value)) is not None:
            full = f"{base}.{n.attr}"
            return self._attr(base, n.attr) and (full not in LISTING or call is not None and self._list(call, full))
        if n.attr in PATH_READS | PATH_LISTS:   # on a pathlib path of literal parts only
            path = self._path(n.value) if self._pathish(n.value) else None
            if call is None or path is None:
                return False
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
            return _own_module(f"{b}.{node.attr}")
        return None

    def _attr(self, mod: str, attr: str) -> bool:
        if mod in LISTED:
            return attr in LISTED[mod]
        value = getattr(sys.modules.get(mod), attr, None)
        return not attr.startswith("_") and attr not in ("main", "test") and value is not None \
            and not isinstance(value, types.ModuleType)

    def _call(self, call: ast.Call) -> bool:
        path = self._path(call.args[0]) if call.args else None
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
        if arg is None:
            return False
        if func in ("glob.glob", "glob.iglob", "glob", "rglob"):
            fixed = GLOB_RE.split(arg, maxsplit=1)[0]
            arg = fixed.rsplit("/", 1)[0] if "/" in fixed else "."
        return self._place(os.path.join(folder, arg), False, search=True)

    def _place(self, path: str, writes: bool, search: bool = False) -> bool:
        from . import credguard, hook, pathguard
        home = str(self.env.get("HOME") or os.path.expanduser("~"))
        path = os.path.expanduser(path) if path.startswith("~") else path
        tmp = self.env.get("TMPDIR")
        mine = [os.path.realpath(r).rstrip("/") + "/" for r in [self.cwd, *[tmp] * bool(tmp)]]
        self.wrote |= writes
        for base in self.bases:
            real = os.path.realpath(os.path.join(base, path))
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
    segments = hook._segments(cmd, strip=False)   # a cd before the snippet: its relative paths start anywhere it may go
    moves = any(argv and (os.path.basename(argv[0]) in ("cd", "pushd", "popd")
                          or any(w.startswith(("-C", "--chdir")) for w in argv)) for argv in segments)
    bases = credguard._cwds(segments, cwd, str(env.get("HOME") or os.path.expanduser("~"))) if moves else None
    if bases and len(bases) >= credguard.CWDS_CAP:   # more folders than the guard follows: no proof
        return None
    lines, bodies = hook._heredocs(cmd)
    if any(b.end is None for b in bodies):   # an unended heredoc: the shell reads it differently than we would
        return None
    changed = hidden = False
    for b in bodies:
        i, body, pos = b
        m = hook.HEREDOC_RE.match(lines[i], pos)
        argv = list(hook._first_argv(hook._piece_at(lines[i], pos)[0]))
        fed = bool(argv) and bool(PY_RE.match(os.path.basename(argv[0]))) and hook._interpreter_args(argv) == "-"
        text = "\n".join(l.lstrip("\t") if m[1] == "-" else l for l in body)
        if fed and text.strip() and (m[2] or not re.search(r"[$`\\]", text)) \
                and (proof := _prove(text, cwd, env, bases)):
            named = _words_name_store(text, cwd, env)
            if named and proof.wrote:   # it could leave a script naming the store for a later call
                return None
            hidden |= named
            body[:] = ["pass"]
            changed = True
    ends: dict[int, list[str]] = {}   # the lines each heredoc leaves after its line: its body, then its end
    for b in bodies:
        ends.setdefault(b[0], []).extend([*b[1], b.end])
    out = []
    for i, line in enumerate(lines):
        flags = hook._quoting(line)
        for m in reversed(list(DASH_C_RE.finditer(line))):
            code = m[1] if m[1] is not None else m[2]
            if not flags[m.start()][0] and _code_at(line, m.start()) == code and (proof := _prove(code, cwd, env, bases)):
                line, changed = f"{line[:m.start()]}-c 'pass'{line[m.end():]}", True
                named = _words_name_store(code, cwd, env)
                if named and proof.wrote:
                    return None
                hidden |= named
        out += [line, *ends.get(i, [])]
    # A snippet naming a store the guard would have seen in its words is freed only as the whole command: its
    # output piped, captured, redirected or written for a later command could feed that name to the shell.
    if hidden and (len(lines) > 1 or re.search(r"[|>`;&]|\$\(|<\(", lines[0])):
        return None
    return "\n".join(out) if changed else None


def _words_name_store(code: str, cwd: str, env) -> bool:
    """`code`'s words name a credential store or a protected path, as the shell guards read a command's words."""
    from . import credguard, pathguard
    home = str(env.get("HOME") or os.path.expanduser("~"))
    words = re.findall(r"[^\s'\"(),\[\]{}]+", code)
    return bool(credguard.bash_refusal(code, [["cat", *words]], home, cwd)) or any(
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

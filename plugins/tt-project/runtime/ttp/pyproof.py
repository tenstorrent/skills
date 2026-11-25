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
import os
import re
import shlex
import sys
import types

# Modules a snippet may import. The pure ones may be used through any public attribute that is not itself a
# module (`ast.sys` would reach sys's own); the ones that reach files, the process or time only through the
# attributes listed.
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
                       "le", "gt", "ge", "contains", "getitem"}}
LISTED["os.path"] = set(dir(os.path)) - {n for n in dir(os.path) if n.startswith("_")}
# Builtins that run code, reach any module or name, or open a pager; open and getattr only as below
BANNED = {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "breakpoint", "help", "setattr",
          "delattr"}
# Path methods that change the file system; `replace` with one argument is Path.replace, not str.replace
PATH_CHANGES = {"write_text", "write_bytes", "unlink", "rename", "mkdir", "touch", "chmod", "lchmod", "rmdir",
                "symlink_to", "hardlink_to", "link_to", "chown"}
PATH_READS = {"read_text", "read_bytes", "open"}
FORMAT_FIELD_RE = re.compile(r"\{[^{}]*[.\[]")   # '{0.attr}', '{0[key]}': format() walks into its arguments
SYSTEM = ("/proc", "/sys", "/dev")


def harmless(code: str, cwd: str, env=None) -> bool:
    """Whether the whole of `code` is proven harmless (module doc); any doubt is False."""
    try:
        return _Proof(cwd, os.environ if env is None else env).ok(ast.parse(code))
    except (SyntaxError, ValueError, RecursionError, OSError):
        return False


class _Proof:
    def __init__(self, cwd: str, env):
        self.cwd, self.env = os.path.realpath(cwd), env
        self.mods: dict[str, str] = {}    # name bound by an import -> its module
        self.funcs: dict[str, str] = {}   # name bound by `from m import f` -> `m.f`
        self.lits: dict[str, str] = {}    # name assigned once, to a literal path
        self.paths: set[str] = set()      # name assigned once, to a pathlib path of literal parts

    def ok(self, tree: ast.AST) -> bool:
        nodes = list(ast.walk(tree))
        stores: dict[str, list] = {}
        for n in nodes:
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.mods[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if n.level or a.name == "*":
                        return False
                    full, name = f"{n.module}.{a.name}", a.asname or a.name
                    if isinstance(_stdlib_module(full), types.ModuleType):
                        self.mods[name] = full
                    elif a.name.startswith("_") or not self._attr(str(n.module), a.name):
                        return False
                    else:
                        self.funcs[name] = full
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                stores.setdefault(n.id, []).append(n)
            elif isinstance(n, (ast.Global, ast.Nonlocal)) or _string_annotation(n):
                return False
        for n in nodes:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                    and len(stores.get(n.targets[0].id, ())) == 1 and (v := self._path(n.value)) is not None:
                self.lits[n.targets[0].id] = v
                if self._pathish(n.value):
                    self.paths.add(n.targets[0].id)
        if any(m.split(".")[0] not in PURE | set(LISTED) for m in self.mods.values()) \
                or (set(self.mods) | set(self.funcs)) & set(stores):   # an imported name bound again
            return False
        parents = {c: p for p in nodes for c in ast.iter_child_nodes(p)}
        return all(self._node(n, parents) for n in nodes)

    def _node(self, n, parents) -> bool:
        if isinstance(n, ast.Name):
            if n.id.startswith("_") or n.id in BANNED:
                return False
            p = parents.get(n)
            if n.id in self.mods:   # a module only as `mod.attr`
                return isinstance(p, ast.Attribute) and p.value is n
            if n.id in ("open", "getattr"):   # only as a call this checks
                return isinstance(p, ast.Call) and p.func is n and self._call(p)
            return True
        if isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                return False
            base = self._dotted(n.value)
            if base is not None:
                return self._attr(base, n.attr)
            p = parents.get(n)
            pathlib = "pathlib" in {m.split(".")[0] for m in [*self.mods.values(), *self.funcs.values()]}
            if pathlib and (n.attr in PATH_CHANGES or n.attr == "replace" and isinstance(p, ast.Call)
                            and len(p.args) == 1 and not p.keywords):
                return False
            if n.attr in PATH_READS and isinstance(p, ast.Call) and p.func is n and self._pathish(n.value) \
                    and (path := self._path(n.value)) is not None:
                return self._open(path, p.args[0] if n.attr == "open" and p.args else None, p)
            if n.attr in ("read_text", "read_bytes") or n.attr == "open" and pathlib:
                return False   # a path read at a place the code computes
            if n.attr in ("format", "format_map"):
                return isinstance(n.value, ast.Constant) and not FORMAT_FIELD_RE.search(str(n.value.value))
            return True
        return True

    def _dotted(self, node) -> str | None:
        """The module an attribute chain starts from, as `os.path`, when its root is an imported name."""
        if isinstance(node, ast.Name):
            return self.mods.get(node.id)
        if isinstance(node, ast.Attribute) and (b := self._dotted(node.value)):
            full = f"{b}.{node.attr}"
            return full if isinstance(_stdlib_module(full), types.ModuleType) else None
        return None

    def _attr(self, mod: str, attr: str) -> bool:
        if mod == "":
            return False   # a name from `from m import x` that is not a module has no attributes we vouch for
        if mod in LISTED:
            return attr in LISTED[mod]
        if mod.split(".")[0] in LISTED:
            return False
        value = getattr(_stdlib_module(mod), attr, None)
        return value is not None and not isinstance(value, types.ModuleType)

    def _call(self, call: ast.Call) -> bool:
        if call.func.id == "getattr":
            return (len(call.args) in (2, 3) and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str) and not call.args[1].value.startswith("_")
                    and call.args[1].value not in PATH_CHANGES | PATH_READS
                    and self._dotted(call.args[0]) is None and not (
                        isinstance(call.args[0], ast.Name) and call.args[0].id in self.mods))
        path = self._path(call.args[0]) if call.args else None
        mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
        return len(call.args) <= 2 and self._open(path, mode, call)

    def _open(self, path: str | None, mode, call: ast.Call) -> bool:
        if path is None or any(k.arg not in ("mode", "encoding", "errors", "newline") for k in call.keywords):
            return False
        if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
            return False
        writes = bool(mode is not None and set(mode.value) & set("wax+"))
        return self._place(path, writes)

    def _place(self, path: str, writes: bool) -> bool:
        from . import credguard, pathguard
        home = str(self.env.get("HOME") or os.path.expanduser("~"))
        real = os.path.realpath(os.path.join(self.cwd, os.path.expanduser(path) if path.startswith("~") else path))
        tmp = self.env.get("TMPDIR")
        ours = any(real.startswith(os.path.realpath(r).rstrip("/") + "/") for r in [self.cwd, *[tmp] * bool(tmp)])
        if credguard.read_refusal(real, home=home) or pathguard._inside(real, pathguard.roots(self.env)) \
                or any(credguard.bash_refusal(f"cat {shlex.quote(p)}", [["cat", p]], home, self.cwd) for p in (path, real)):
            return False   # the shell guard's own reading of the path as a word: a store by name anywhere
        return ours or not writes and not any(real == s or real.startswith(s + "/") for s in SYSTEM)

    def _path(self, node) -> str | None:
        """The literal path an expression names (a string, Path('a', 'b'), Path(x) / 'y', os.path.join of
        literals, a name assigned once to one), else None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
            return node.value
        if isinstance(node, ast.Name):
            return self.lits.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            a, b = self._path(node.left), self._path(node.right)
            return os.path.join(a, b) if a is not None and b is not None and self._pathish(node.left) else None
        if isinstance(node, ast.Call) and not node.keywords and node.args and (
                self._pathish(node) or self._func(node) == "os.path.join"):
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


def _string_annotation(n) -> bool:
    """An annotation written as a string: typing and functools evaluate those as code at run time."""
    notes = [n.annotation] if isinstance(n, (ast.arg, ast.AnnAssign)) else [n.returns] if isinstance(
        n, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
    return any(isinstance(c, ast.Constant) and isinstance(c.value, str) for a in notes if a for c in ast.walk(a))


def _stdlib_module(name: str):
    """The stdlib module or attribute `name` names, imported here (PURE and LISTED modules only), else None."""
    root, *rest = name.split(".")
    if root not in PURE | set(LISTED):
        return None
    try:
        obj = sys.modules.get(root) or __import__(root)
    except ImportError:
        return None
    for part in rest:
        obj = getattr(obj, part, None)
    return obj


PY_RE = re.compile(r"python[\d.]*$")
DASH_C_RE = re.compile(r"(?<![^\s])-c\s+(?:'([^']*)'|\"([^\"$`\\]*)\")")


def rewrite(cmd: str, cwd: str, env=None) -> str | None:
    """`cmd` with each inline Python snippet proven harmless written as `pass`, for the guards to judge; None
    when none is. Only text the shell passes as is: a heredoc with a quoted delimiter (or a body with no `$`,
    backtick or backslash) that python reads as its script, and the code of python's own `-c` in single quotes
    (or double quotes with none of those). Every other byte of the line stays."""
    from . import hook
    if "\\\n" in cmd or "python" not in cmd:
        return None
    out, changed, pending = [], False, []
    for line in cmd.split("\n"):
        if pending:
            delim, dash, body, fed, quoted = pending[0]
            if (line.lstrip("\t") if dash else line) != delim:
                body.append(line)
                continue
            pending.pop(0)
            text = "\n".join(body)
            if fed and text.strip() and (quoted or not re.search(r"[$`\\]", text)) and harmless(text, cwd, env):
                body[:] = ["pass"]
                changed = True
            out += [*body, line]
            continue
        flags = hook._quoting(line)
        for m in reversed(list(DASH_C_RE.finditer(line))):
            code = m[1] if m[1] is not None else m[2]
            if not flags[m.start()][0] and _code_at(line, m.start()) == code and harmless(code, cwd, env):
                line, changed = f"{line[:m.start()]}-c 'pass'{line[m.end():]}", True
        flags = hook._quoting(line)
        for m in hook.HEREDOC_RE.finditer(line):
            if not flags[m.start()][0]:
                argv = list(hook._first_argv(hook._piece_at(line, m.start())[0]))
                fed = bool(argv) and bool(PY_RE.match(os.path.basename(argv[0]))) and hook._interpreter_args(argv) == "-"
                pending.append((m[3], m[1] == "-", [], fed, bool(m[2])))
        out.append(line)
    # an unended heredoc: the shell reads it differently than we would
    return "\n".join(out) if changed and not pending else None


def _code_at(line: str, pos: int) -> str | None:
    """The code python's own `-c` runs in the simple command at `pos`, when that command is python."""
    from . import hook
    argv = list(hook._first_argv(hook._piece_at(line, pos)[0]))
    if not argv or not PY_RE.match(os.path.basename(argv[0])) or hook._interpreter_args(argv) != "":
        return None
    i = next((i for i, a in enumerate(argv[1:-1], 1) if a in ("-c", "-m") or not a.startswith("-")), None)
    return argv[i + 1] if i and argv[i] == "-c" else None

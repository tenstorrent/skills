"""A model-free code-slop scan (`ttp slop`), the review rule built on it, and the periodic de-slop pass.

The scan reads a repository's tracked files, working tree or any git ref, and reports per subsystem
(the top-level directory of the code repo; one level deeper under a container directory such as
`plugins/` or `packages/`; `deslop.subsystems` names others):

- duplicate rules: functions of the same name defined in more than one module (or twice in one scope),
  and functions whose bodies are the same after local names are normalized (Python, by AST hash);
- mutable globals: module-level dict, list or set state and names a function rebinds with `global`;
- magic numbers: numeric literals other than 0, 1 and -1 outside a named constant, with the ones that
  set a timeout, grace window, interval or delay counted apart;
- function length: the longest, the 90th percentile and the count over `max_function_lines`;
- comment noise: the share of comment and docstring lines that cite a date, a `#1234` id, a ticket key,
  a host-like name or tell an incident.

Python gets the full scan. Other languages (JavaScript, TypeScript, Go, Rust, Java, C, C++, shell)
get the line heuristics only: comment noise, magic numbers and same-name functions. Test files are
scanned for function length and comment noise only.

The numbers are findings for a reviewer, never a verdict: a necessary constant is not a failure.
`delta` gives what a change adds, for the review spec (RULE is the sentence it carries); `choose`
picks the subsystem a de-slop pass refactors next (`deslop` schedule, `ttp slop --next`).
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import subprocess
import time
import tokenize
from collections import defaultdict
from pathlib import PurePosixPath

DEFAULTS = {
    "enabled": False,            # the deslop schedule (the review rule and the scan are always on)
    "every": "1d",
    "subsystems": [],            # [] = the top-level directories; else path prefixes, each one subsystem
    "max_function_lines": 80,
    "gap_days": 3,               # a subsystem refactored within this many days is skipped
    "step_share": 0.2,           # each pass cuts an over-threshold number by this share, or to its threshold
    "thresholds": {"duplicates": 0, "globals": 5, "magic_per_kloc": 15.0, "long_functions": 0,
                   "noise_share": 0.1},
    "exclude": [],               # path globs left out of the scan
}
CONTAINERS = {"plugins", "packages", "apps", "libs", "services", "modules", "src", "crates"}
SKIP_DIRS = {"node_modules", "vendor", "third_party", "build", "dist", "target", "__pycache__", "venv", ".venv",
             "site-packages", "migrations", "generated"}
PY = {".py"}
LINE_LANGS = {".js": "//", ".jsx": "//", ".ts": "//", ".tsx": "//", ".go": "//", ".rs": "//", ".java": "//",
              ".c": "//", ".cc": "//", ".cpp": "//", ".h": "//", ".hpp": "//", ".sh": "#", ".bash": "#"}
FN_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|func\s+(?:\([^)]*\)\s*)?(\w+)|fn\s+(\w+)|"
                   r"(?:public|private|protected|static)[\w\s<>\[\],]*?\s(\w+)\s*\([^;]*\)\s*\{|(\w+)\s*\(\)\s*\{)")
NUM_RE = re.compile(r"(?<![\w.#\"'])(\d+\.\d+|\d{2,}|[2-9])(?![\w.\"'])")
MAGIC_OK = {0, 1, -1}
TIMING = re.compile(r"(timeout|grace|window|ttl|interval|delay|sleep|deadline|backoff|period|wait|expire)", re.I)
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|"
                  r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}\b")
TASK_ID = re.compile(r"(?<![\w/&])#\d{2,}\b")
TICKET = re.compile(r"\b(?!(?:UTF|SHA|ISO|MD|AES|RSA|TLS|SSL|HTTP|CRC|PEP|RFC|IPV|X|V|CVE|ASCII)-)[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
HOST = re.compile(r"\b[a-z]{2,5}0\d\b|\b[a-z0-9]{2,5}(?:-[a-z0-9]{2,6}){1,3}\d{2}\b|\b\d{1,3}(?:\.\d{1,3}){3}\b|"
                  r"\b[\w-]+\.(?:[\w-]+\.)*(?:com|net|org|io|local|internal|lan)\b")
STORY = re.compile(r"\b(incident|outage|post-?mortem|hotfix|we (?:hit|saw)|was hit|happened (?:on|in|when)|"
                   r"broke (?:on|in)|bit us|last (?:night|week)|yesterday|live (?:bug|failure))\b", re.I)
MUTABLE_CALLS = {"dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque"}
# What a reviewer of a change is told, and what a code worker is told before it writes code.
RULE = ("De-slop rule (the scan numbers above are findings for you to judge, not verdicts): fail the change if "
        "it re-implements a rule an existing function already decides (reuse or refactor that function; a "
        "second copy of the rule is a finding even when it passes), adds a comment or docstring that tells an "
        "incident (a date, a task or ticket id, a host name, 'we saw'), or adds a test that only replays one "
        "recorded incident without asserting the general rule it broke. An incident fix must fit the existing "
        "design or refactor it, never add one more special case beside it. A named, necessary constant is "
        "not a failure.")
WORKER_RULE = ("Before you write code, find the function that already decides the rule you touch and extend or "
               "refactor it: never add a second copy, a parallel special case, a timeout or threshold as a bare "
               "number (name it once), a comment or docstring that tells an incident (a date, a task or ticket id, "
               "a host name; say the rule and why, not the story), or a test that only replays one recorded "
               "incident (assert the general rule it broke). `ttp slop --diff <base>..HEAD` shows what your change adds.")
COUNTED = ("duplicates", "globals", "magic", "magic_timing", "long_functions", "noise")


def _sub_of(path: str, subsystems: list[str]) -> str:
    for s in subsystems:
        s = s.strip("/")
        if path == s or path.startswith(s + "/"):
            return s
    parts = PurePosixPath(path).parts
    if len(parts) == 1:
        return "(root)"
    return "/".join(parts[:2]) if parts[0] in CONTAINERS and len(parts) > 2 else parts[0]


def _is_test(path: str) -> bool:
    p = PurePosixPath(path)
    return bool({"tests", "test", "__tests__", "spec"} & set(p.parts[:-1])) or p.name.startswith(("test_", "conftest")) \
        or p.stem.endswith(("_test", ".test", "_spec"))


def _noisy(text: str) -> bool:
    return bool(DATE.search(text) or TASK_ID.search(text) or TICKET.search(text) or HOST.search(text) or STORY.search(text))


class _Info:
    """What one file adds to the scan; cached by blob id, so a delta rereads only what changed."""
    __slots__ = ("path", "test", "lines", "funcs", "globals", "magic", "timing", "fn_lengths", "comments", "noisy", "hashes")

    def __init__(self, path: str, test: bool):
        self.path, self.test = path, test
        self.lines = 0
        self.funcs: list[tuple[str, int, str]] = []      # (name, line, scope): module-level and nested-in-function ones
        self.globals = 0
        self.magic = 0
        self.timing = 0
        self.fn_lengths: list[tuple[int, str, int]] = []  # (length, name, line)
        self.comments = 0
        self.noisy: list[tuple[int, str]] = []
        self.hashes: list[tuple[str, str, int, int]] = []  # (hash, name, line, size)


def _body(fn) -> list:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


_FN = (ast.FunctionDef, ast.AsyncFunctionDef)
_MUTABLE_NODES = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)


def _docstring(node):
    b = getattr(node, "body", None)
    if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
            and isinstance(b[0].value.value, str):
        return b[0]
    return None


def _sig(node, names: dict, top: bool = False) -> str:
    """The body of a function as text with its local names numbered, so a renamed copy compares equal."""
    if isinstance(node, list):
        return "[" + ",".join(_sig(n, names) for n in node) + "]"
    if isinstance(node, ast.Name):
        return "N" + names.setdefault(node.id, str(len(names)))
    if isinstance(node, ast.arg):
        return "A" + names.setdefault(node.arg, str(len(names)))
    if isinstance(node, ast.AST):
        skip = {"ctx", "type_comment", "returns", "decorator_list", "name"} if top else {"ctx", "type_comment", "annotation"}
        return type(node).__name__ + "(" + ",".join(
            _sig(v, names) for k, v in ast.iter_fields(node) if k not in skip) + ")"
    return repr(node)


def _scan_py(path: str, text: str, max_lines: int) -> _Info:
    info = _Info(path, _is_test(path))
    info.lines = sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return info
    doc_lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                body = tok.string.lstrip("#").strip()
                if body.startswith(("!", "-*-", "type:", "noqa", "pragma", "pylint", "fmt:")):
                    continue
                info.comments += 1
                if _noisy(body):
                    info.noisy.append((tok.start[0], body))
    except (tokenize.TokenError, IndentationError):
        pass
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        val = getattr(node, "value", None)
        mutable = val is not None and (isinstance(val, _MUTABLE_NODES) or (
            isinstance(val, ast.Call) and isinstance(val.func, (ast.Name, ast.Attribute))
            and (val.func.id if isinstance(val.func, ast.Name) else val.func.attr) in MUTABLE_CALLS))
        info.globals += sum(1 for t in targets if mutable and isinstance(t, ast.Name) and not t.id.isupper() and t.id != "__all__")
        if isinstance(node, _FN):
            info.funcs.append((node.name, node.lineno, ""))
        elif isinstance(node, ast.ClassDef):
            seen: set[str] = set()
            for m in node.body:
                if isinstance(m, _FN) and not any(
                        isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter")
                        or isinstance(d, ast.Name) and d.id == "overload" for d in m.decorator_list):
                    if m.name in seen:
                        info.funcs.append((m.name, m.lineno, node.name))   # the same method twice in one class
                    seen.add(m.name)
    rebound: set[str] = set()
    fns: list[tuple[ast.AST, list[int]]] = []   # each function with its statement count (nested ones included)
    count_magic = not info.test
    stack: list[list[int]] = []
    named_const = {id(n): True for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))
                   and all(isinstance(t, ast.Name) and t.id.isupper()
                           for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
                   for n in ast.walk(node)}

    def visit(node, parent, grand, defaults_of=None):
        if isinstance(node, ast.stmt):
            for c in stack:
                c[0] += 1
        if isinstance(node, ast.Global):
            rebound.update(node.names)
        if isinstance(node, (ast.Module, ast.ClassDef) + _FN) and (d := _docstring(node)):
            for i, ln in enumerate(d.value.value.splitlines() or [""]):
                info.comments += 1
                if _noisy(ln):
                    info.noisy.append((d.lineno + i, ln.strip()))
            doc_lines.update(range(d.lineno, (d.end_lineno or d.lineno) + 1))
        if isinstance(node, _FN):
            info.fn_lengths.append(((node.end_lineno or node.lineno) - node.lineno + 1, node.name, node.lineno))
            counter = [0]
            fns.append((node, counter))
            stack.append(counter)
        elif count_magic and isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            _magic(node, parent, grand)
        for ch in ast.iter_child_nodes(node):
            visit(ch, node, parent)
        if isinstance(node, _FN):
            stack.pop()

    def _magic(node, par, grand):
        val = node.value
        if val in MAGIC_OK or node.lineno in doc_lines or id(node) in named_const:
            return
        if isinstance(par, ast.UnaryOp) and isinstance(par.op, ast.USub):
            if -val in MAGIC_OK:
                return
            par = grand
        if isinstance(par, (ast.Subscript, ast.Slice)):
            return
        info.magic += 1
        timing = False
        if isinstance(par, ast.keyword):
            timing = bool(TIMING.search(par.arg or ""))
        elif isinstance(par, ast.Call):
            timing = bool(TIMING.search(ast.unparse(par.func)[-24:]))
        elif isinstance(par, ast.arguments):   # a default: match its parameter's name
            args = par.posonlyargs + par.args
            pairs = list(zip(args[len(args) - len(par.defaults):], par.defaults)) + list(zip(par.kwonlyargs, par.kw_defaults))
            timing = any(d is node and TIMING.search(a.arg) for a, d in pairs)
        info.timing += timing

    try:
        visit(tree, None, None)
    except RecursionError:
        return info
    info.globals += len(rebound)
    if not info.test:
        for fn, counter in fns:
            if counter[0] >= 6 and not (fn.name.startswith("__") and fn.name.endswith("__")):
                names: dict[str, str] = {}
                body = _body(fn)
                sig = _sig(fn.args, names) + _sig(body or fn.body, names)
                info.hashes.append((hashlib.sha1(sig.encode()).hexdigest()[:16], fn.name, fn.lineno, counter[0]))
    return info



def _scan_lines(path: str, text: str, ext: str) -> _Info:
    """The heuristics for the other languages: comments, number literals and function names."""
    info = _Info(path, _is_test(path))
    lead, in_block = LINE_LANGS[ext], False
    for no, raw in enumerate(text.splitlines(), 1):
        ln = raw.strip()
        if not ln:
            continue
        com = None
        if in_block:
            com = ln.strip("*/ ").strip()
            in_block = "*/" not in ln
        elif lead == "//" and ln.startswith("/*"):
            com, in_block = ln.lstrip("/* ").strip(), "*/" not in ln
        elif ln.startswith(lead) and not ln.startswith(("#!", "#include", "#define", "#if", "#endif", "#pragma")):
            com = ln.lstrip(lead).strip()
        if com is not None:
            info.comments += 1
            if _noisy(com):
                info.noisy.append((no, com))
            continue
        info.lines += 1
        if not info.test:
            code = re.sub(r"(\"[^\"]*\"|'[^']*')", '""', ln.split(lead + " ")[0])
            if not re.match(r"^(const|static final|final|#define|let [A-Z_]+|var [A-Z_]+|[A-Z][A-Z0-9_]+\s*=)", code):
                n = len(NUM_RE.findall(code))
                info.magic += n
                if n and TIMING.search(code):
                    info.timing += n
            if (m := FN_RE.match(code)) and (name := next((g for g in m.groups() if g), "")) and name not in ("if", "for", "while", "switch", "main"):
                info.funcs.append((name, no, ""))
    return info


_CACHE: dict[tuple, _Info] = {}
_CACHE_MAX = 20000
VERSION = 1   # bump with a change to what a file's scan records: older cache files are ignored
_loaded: set[str] = set()


class Deadline(Exception):
    """The scan ran past its time budget; what it analyzed is kept in the cache for the next try."""


def _load(cache: str | None) -> None:
    if not cache or cache in _loaded:
        return
    _loaded.add(cache)
    try:
        with open(cache) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    if raw.get("version") != VERSION:
        return
    for k, v in raw.get("files", {}).items():
        blob, path, maxl = k.split("|", 2)[0], k.split("|", 2)[1], int(k.rsplit("|", 1)[1])
        i = _Info(path, v["test"])
        for f in _Info.__slots__[2:]:
            setattr(i, f, [tuple(x) for x in v[f]] if isinstance(v[f], list) else v[f])
        _CACHE[(blob, path, maxl)] = i


def _save(cache: str | None) -> None:
    if not cache:
        return
    import os
    files = {f"{b}|{p}|{m}": {"test": i.test, **{f: getattr(i, f) for f in _Info.__slots__[2:]}}
             for (b, p, m), i in _CACHE.items()}
    tmp = f"{cache}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"version": VERSION, "files": files}, f)
        os.replace(tmp, cache)
    except OSError:
        pass


def _analyze(path: str, key: str, text_fn, max_lines: int, deadline: float | None = None) -> _Info | None:
    ext = PurePosixPath(path).suffix
    if ext not in PY and ext not in LINE_LANGS:
        return None
    ck = (key, path, max_lines)
    if key and (hit := _CACHE.get(ck)):
        return hit
    if deadline is not None and time.monotonic() > deadline:
        raise Deadline()
    try:
        text = text_fn()
    except (OSError, UnicodeDecodeError):
        return None
    info = _scan_py(path, text, max_lines) if ext in PY else _scan_lines(path, text, ext)
    if key:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[ck] = info
    return info


def _git(root, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, errors="replace", timeout=120)
    if r.returncode and check:
        raise RuntimeError(r.stderr.strip() or f"git {args[0]} failed")
    return r.stdout if r.returncode == 0 else ""


def _excluded(path: str, globs: list[str]) -> bool:
    p = PurePosixPath(path)
    return bool(SKIP_DIRS & set(p.parts[:-1])) or any(p.match(g) or path.startswith(g.rstrip("*/") + "/") for g in globs)


def read_tree(root, ref: str | None = None, paths: list[str] | None = None, exclude: list[str] | None = None,
              max_lines: int = 80, deadline: float | None = None) -> list[_Info]:
    """The files of `ref` (a commit, branch or `HEAD`) or, with none, the tracked and untracked-unignored
    files of the working tree, each scanned once (a ref's by blob id)."""
    exclude = exclude or []
    infos: list[_Info] = []
    if ref:
        for line in _git(root, "ls-tree", "-r", "-z", ref).split("\0"):
            if not line:
                continue
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob" or _excluded(path, exclude):
                continue
            if paths and not any(PurePosixPath(path).match(g) for g in paths):
                continue
            blob = parts[2]
            info = _analyze(path, blob, lambda b=blob: _git(root, "cat-file", "blob", b), max_lines, deadline)
            if info:
                infos.append(info)
    else:
        from pathlib import Path
        names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        for path in sorted({n for n in names if n}):
            if _excluded(path, exclude) or (paths and not any(PurePosixPath(path).match(g) for g in paths)):
                continue
            f = Path(root) / path
            info = _analyze(path, "", lambda f=f: f.read_text(errors="replace") if f.is_file() else "", max_lines)
            if info:
                infos.append(info)
    return infos


def aggregate(infos: list[_Info], cfg: dict | None = None) -> dict:
    """Per-subsystem numbers and the repo's duplicate groups, from the scanned files."""
    c = {**DEFAULTS, **(cfg or {})}
    maxl, subs = int(c["max_function_lines"]), list(c["subsystems"] or [])
    by_name: dict[str, list[tuple[str, int]]] = defaultdict(list)
    by_hash: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for i in infos:
        if i.test:
            continue
        for name, line, scope in i.funcs:
            if name.startswith("__") and name.endswith("__") or name == "main":
                continue
            by_name[name].append((f"{i.path}:{line}", scope))
        for h, name, line, _ in i.hashes:
            by_hash[h].append((i.path, name, line))
    dups: list[dict] = []
    for name, locs in by_name.items():
        paths = {p.rsplit(":", 1)[0] for p, _ in locs}
        if len(locs) > 1 and (len(paths) > 1 or any(s for _, s in locs)):
            dups.append({"kind": "name", "name": name, "at": sorted(p for p, _ in locs)})
    for h, locs in by_hash.items():
        if len(locs) > 1:
            dups.append({"kind": "body", "name": locs[0][1], "at": sorted(f"{p}:{ln}" for p, _, ln in locs)})
    out: dict[str, dict] = {}
    for i in infos:
        s = out.setdefault(_sub_of(i.path, subs), {"files": 0, "lines": 0, "duplicates": 0, "globals": 0, "magic": 0,
                                                  "magic_timing": 0, "fn_lengths": [], "comments": 0, "noisy": 0})
        s["files"] += 1
        s["lines"] += i.lines
        s["globals"] += 0 if i.test else i.globals
        s["magic"] += 0 if i.test else i.magic
        s["magic_timing"] += 0 if i.test else i.timing
        s["fn_lengths"] += [(n, f"{i.path}:{ln} {name}") for n, name, ln in i.fn_lengths]
        s["comments"] += i.comments
        s["noisy"] += len(i.noisy)
    for d in dups:
        for sub in {_sub_of(a.rsplit(":", 1)[0], subs) for a in d["at"]}:
            out[sub]["duplicates"] += 1
    res = {}
    for name, s in out.items():
        ls = sorted(n for n, _ in s["fn_lengths"])
        top = max(s["fn_lengths"], default=(0, ""))
        res[name] = {"files": s["files"], "lines": s["lines"], "duplicates": s["duplicates"], "globals": s["globals"],
                     "magic": s["magic"], "magic_timing": s["magic_timing"],
                     "magic_per_kloc": round(1000 * s["magic"] / s["lines"], 1) if s["lines"] else 0.0,
                     "fn_max": top[0], "fn_max_at": top[1], "fn_p90": ls[int(0.9 * (len(ls) - 1))] if ls else 0,
                     "long_functions": sum(1 for n in ls if n > maxl),
                     "comment_lines": s["comments"], "noisy_lines": s["noisy"],
                     "noise_share": round(s["noisy"] / s["comments"], 3) if s["comments"] else 0.0}
    return {"subsystems": dict(sorted(res.items())), "duplicate_groups": sorted(dups, key=lambda d: (d["kind"], d["name"]))}


def scan(root, ref: str | None = None, cfg: dict | None = None, paths: list[str] | None = None,
         cache: str | None = None) -> dict:
    """The report for `ref` (default: the working tree). `cache`: a JSON file keeping each analyzed blob."""
    c = {**DEFAULTS, **(cfg or {})}
    _load(cache)
    infos = read_tree(root, ref, paths, list(c["exclude"] or []), int(c["max_function_lines"]))
    _save(cache)
    rep = aggregate(infos, c)
    rep["ref"] = ref or "working tree"
    rep["noisy"] = [{"at": f"{i.path}:{ln}", "text": t[:120]} for i in infos for ln, t in i.noisy]
    return rep


def excess(sub: dict, thresholds: dict | None = None) -> float:
    """How far one subsystem is over the targets: the sum of each number's excess over its threshold, as a share
    of the threshold (a zero threshold counts each unit as 1). 0: under every target."""
    t = {**DEFAULTS["thresholds"], **(thresholds or {})}
    pairs = (("duplicates", "duplicates"), ("globals", "globals"), ("magic_per_kloc", "magic_per_kloc"),
             ("long_functions", "long_functions"), ("noise_share", "noise_share"))
    return round(sum(max(0.0, sub.get(k, 0) - t[th]) / (t[th] if t[th] > 0 else 1) for k, th in pairs), 2)


def choose(rep: dict, cfg: dict | None = None, skip: set[str] | None = None) -> tuple[str, float] | None:
    """The subsystem furthest over its targets that is not in `skip` (refactored lately), with its excess."""
    c = {**DEFAULTS, **(cfg or {})}
    rows = [(excess(s, c["thresholds"]), n) for n, s in rep["subsystems"].items()
            if n not in (skip or set()) and n != "(root)" and s["lines"] >= 50]
    rows = [r for r in rows if r[0] > 0]
    return (max(rows)[1], max(rows)[0]) if rows else None


def delta(root, base: str, head: str, cfg: dict | None = None, cache: str | None = None,
          budget_s: float | None = None) -> dict:
    """What the change base..head adds: per-subsystem differences of the counted numbers (changed ones
    only), the duplicate groups new at head and the incident-style comment lines new at head."""
    c = {**DEFAULTS, **(cfg or {})}
    maxl, ex = int(c["max_function_lines"]), list(c["exclude"] or [])
    _load(cache)
    end = time.monotonic() + budget_s if budget_s else None
    try:
        a, b = read_tree(root, base, None, ex, maxl, end), read_tree(root, head, None, ex, maxl, end)
    finally:
        _save(cache)
    ra, rb = aggregate(a, c), aggregate(b, c)
    rows = {}
    for name, now in rb["subsystems"].items():
        was = ra["subsystems"].get(name, {})
        d = {k: now[k] - was.get(k, 0) for k in ("duplicates", "globals", "magic", "magic_timing", "long_functions")}
        d["noisy_lines"] = now["noisy_lines"] - was.get("noisy_lines", 0)
        d["fn_max"] = now["fn_max"]
        if any(v for k, v in d.items() if k != "fn_max"):
            rows[name] = d
    key = lambda g: (g["kind"], g["name"], tuple(a.rsplit(":", 1)[0] for a in g["at"]))   # noqa: E731
    was = {key(g) for g in ra["duplicate_groups"]}
    new_dups = [g for g in rb["duplicate_groups"] if key(g) not in was]
    old = {(i.path, t) for i in a for _, t in i.noisy}
    new_noise = [{"at": f"{i.path}:{ln}", "text": t[:100]} for i in b for ln, t in i.noisy if (i.path, t) not in old]
    return {"base": base, "head": head, "subsystems": rows, "new_duplicates": new_dups, "new_noisy": new_noise}


def delta_lines(d: dict, show: int = 5) -> list[str]:
    """The delta as the lines a review spec carries; `[]` when the change adds none of what the scan counts."""
    out = []
    for name, r in d["subsystems"].items():
        parts = [f"{k.replace('_', ' ')} {v:+d}" for k, v in r.items() if k != "fn_max" and v]
        out.append(f"- {name}: " + ", ".join(parts) + f" (longest function now {r['fn_max']} lines)")
    for g in d["new_duplicates"][:show]:
        out.append(f"- new duplicate {g['kind']} `{g['name']}`: " + ", ".join(g["at"][:4]))
    for n in d["new_noisy"][:show]:
        out.append(f"- new incident-style comment line {n['at']}: {n['text']}")
    more = len(d["new_duplicates"]) - show, len(d["new_noisy"]) - show
    if more[0] > 0 or more[1] > 0:
        out.append(f"- and {max(more[0], 0) + max(more[1], 0)} more (`ttp slop --diff {d['base'][:12]}..{d['head'][:12]}`)")
    return out


def review_block(root, base: str, head: str, cfg: dict | None = None, cache: str | None = None,
                 budget_s: float = 20.0) -> str:
    """The scan delta and the de-slop rule for a review spec. A scan that cannot finish in `budget_s` (a cold
    cache: the next one resumes) or cannot run leaves the rule alone and names the command that gives the numbers."""
    cmd = f"`ttp slop --diff {base[:12]}..{head[:12]}`"
    try:
        lines = delta_lines(delta(root, base, head, cfg, cache, budget_s))
    except Deadline:
        lines = [f"(the scan is still warming its cache: run {cmd} for the numbers)"]
    except Exception as e:   # the review goes on without the numbers
        lines = [f"(the scan did not run: {str(e)[:100]}; run {cmd})"]
    head_line = "Scan delta of this change (model-free `ttp slop --diff`; numbers added by the change, per subsystem):"
    return "\n".join([head_line, *(lines or ["- none: the change adds no duplicates, globals, magic numbers, long functions or incident-style comments."]), RULE])


def text(rep: dict, cfg: dict | None = None) -> str:
    c = {**DEFAULTS, **(cfg or {})}
    th = {**DEFAULTS["thresholds"], **(c.get("thresholds") or {})}
    rows = [f"slop scan of {rep.get('ref', '')}: {len(rep['subsystems'])} subsystems "
            f"(targets: duplicates {th['duplicates']}, globals {th['globals']}, magic/kloc {th['magic_per_kloc']}, "
            f"long functions {th['long_functions']} over {c['max_function_lines']} lines, noise {th['noise_share']:.0%})"]
    for n, s in rep["subsystems"].items():
        rows.append(f"- {n}: {s['lines']} lines; duplicates {s['duplicates']}; globals {s['globals']}; magic {s['magic']} "
                    f"({s['magic_per_kloc']}/kloc, {s['magic_timing']} timing); functions max {s['fn_max']} p90 {s['fn_p90']} "
                    f"over-limit {s['long_functions']}; comment noise {s['noisy_lines']}/{s['comment_lines']} "
                    f"({s['noise_share']:.0%}); excess {excess(s, th)}")
    for g in rep["duplicate_groups"][:10]:
        rows.append(f"  duplicate {g['kind']} `{g['name']}`: " + ", ".join(g["at"][:4]))
    return "\n".join(rows)


def dump(rep: dict) -> str:
    return json.dumps(rep, indent=1)

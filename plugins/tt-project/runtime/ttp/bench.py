# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay bench, model-free part (`ttp bench prep`): pick landed tasks a later run can replay to
compare models, build a clean workspace at each one's base commit and hold out its tests.

A task qualifies when it is a done code or work task created on or after `since` (the later of the
compared models' release dates, so neither saw the change), landed through one push queue row, and
its landed change adds or changes tests (addedtests.added). Its base is the commit before its
landed commits on the push branch. Excluded, with the reason: a spec that quotes the fix (QUOTE_LINES
added lines verbatim), held-out tests that use a repository symbol the base lacks and the spec does
not name, and (unless validation is off) a task none of whose added tests fails on the base with the
head's test files in place and passes ROUNDS of ROUNDS runs on the head: only those fail-to-pass
tests are kept. A task whose spec says its branch starts from another task's branch (PARENT) replays
from that parent's recorded head (the spec's, else the parent's latest push queue head) up to its own
branch head; it is excluded when the repository lacks that head or it is not under the branch head,
since a workspace at the push branch base would lack the parent's work. Each kept task gets <out>/<id>/workspace, a fresh repository whose
one ref is the base commit, with its history up to the base and nothing later (no remotes, tags or reflog), and <out>/<id>/heldout, the head's copy
of each test file the change adds or changes. manifest.json names them with their sha256;
manifest.sha256 pins it. The bench names no model: what it compares is the runner's choice."""
from __future__ import annotations

import ast
import concurrent.futures
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import addedtests, gitmemo
from .project import durable_write

SMALL = 150        # changed non-test lines at most for the small stratum
QUOTE_LINES = 3    # added lines of the fix quoted verbatim in the spec that exclude it
QUOTE_LEN = 30     # an added line counts as quotable from this many characters
ROUNDS = 3         # runs on the head each kept test must pass
TEST_CMD = "python3 -m pytest -q -p no:cacheprovider"
TEST_TIMEOUT = 900  # seconds one validation run may take
BUILDERS = 4        # workspaces built at once, beside the scan
PARENT = re.compile(r"This task's branch starts from #(\d+)'s branch(?: \S+ \(head ([0-9a-f]{7,40})\))?")
_EPOCH = {"GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}


def _git(repo, *args: str, **kw) -> subprocess.CompletedProcess:
    return gitmemo.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=None, **kw)


def _out(repo, *args: str) -> str:
    """git's output ("" on an error), kept within a gitmemo scope: prep reads commits by id."""
    def read() -> str:
        r = _git(repo, *args)
        return r.stdout.strip() if r.returncode == 0 else ""
    return gitmemo.cached(("bench", str(repo), *args), read)


def landing(repo: Path, rows: list[dict]) -> tuple[str, str, str]:
    """(base, head, "") of a change the push queue landed in `rows` (its rows with a landed commit),
    or ("", "", why) when the range on the push branch is not clear."""
    if len(rows) != 1:
        return "", "", f"landed in {len(rows)} pushes" if rows else "no landed commit recorded"
    land, head = rows[0]["landed_sha"], rows[0]["head"] or ""
    fork = _out(repo, "merge-base", head, land) if head else ""
    if not fork or not _out(repo, "rev-parse", "--verify", "-q", f"{land}^{{commit}}"):
        return "", "", "its landed commit or branch head is not in the repository"
    if fork == _out(repo, "rev-parse", head):
        return "", "", "it landed as a fast-forward, so where its commits start is unknown"
    n = int(_out(repo, "rev-list", "--count", "--no-merges", f"{fork}..{head}") or 0)
    base = _out(repo, "rev-parse", "--verify", "-q", f"{land}~{n}") if n else ""
    same = base and _out(repo, "rev-list", "--count", "--merges", f"{base}..{land}") == "0" and (
        _out(repo, "diff", "--name-only", fork, head) == _out(repo, "diff", "--name-only", base, land))
    return (base, land, "") if same else ("", "", "its landed commits do not match its branch")


def from_parent(db, repo: Path, parent: int, recorded: str, head: str) -> tuple[str, str, str]:
    """(base, head, "") of a task whose branch starts from task `parent`'s branch: base is the parent's
    `recorded` head (else its latest push queue head under `head`, the task's branch head) when the
    repository has it under `head`; else ("", "", why)."""
    heads = [recorded] if recorded else [r["head"] for r in db.q(
        "SELECT head FROM push_queue WHERE task=? AND head IS NOT NULL AND head != '' ORDER BY id DESC", (parent,))]
    tip = _out(repo, "rev-parse", "--verify", "-q", f"{head}^{{commit}}") if head else ""
    shas = [s for h in heads if (s := _out(repo, "rev-parse", "--verify", "-q", f"{h}^{{commit}}"))]
    for sha in shas:
        if tip and _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode == 0:
            return sha, tip, ""
    what = ("not recorded" if not heads else "not in the repository" if not shas else
            "not under its branch head")
    return "", "", f"its branch starts from #{parent}'s branch, whose head is {what}"


def _held(path: str) -> bool:
    """Whether `path` is a test file or lies in a test folder (the change's copy is held out)."""
    return addedtests._test_file(path) or any(p.lower().startswith("test") for p in Path(path).parts[:-1])


def _changed(repo: Path, base: str, head: str) -> tuple[list[str], int]:
    """(added lines, lines changed) of the non-test files the change touches, in one diff."""
    paths, lines = [], 0
    for row in _out(repo, "diff", "--no-renames", "--numstat", base, head).splitlines():
        add, rem, path = (row.split("\t", 2) + ["", ""])[:3]
        if path and not _held(path):
            paths.append(path)
            lines += int(add) + int(rem) if add.isdigit() and rem.isdigit() else 0
    diff = _out(repo, "diff", "--no-renames", "-U0", base, head, "--", *paths).splitlines() if paths else []
    return [x[1:].strip() for x in diff if x.startswith("+") and not x.startswith("+++")], lines


def flat(line: str) -> str:
    """`line` with its whitespace collapsed: indentation and spacing never make a line quotable."""
    return " ".join(line.split())


def quotes(spec: str, added: list[str]) -> int:
    """How many distinct added non-test lines of QUOTE_LEN characters or more (whitespace collapsed) `spec` quotes."""
    body = flat(spec)
    return len({f for x in added if len(f := flat(x)) >= QUOTE_LEN and f in body})


WITHHELD = "[path withheld]"
_PATH_TAIL = r"(?:/[^\s`'\"<>)\]},;:|&]*)?"   # what follows a path's folder: the rest of the path


def scrub_spec(spec: str, roots=(), drop=(), home: Path | None = None) -> str:
    """`spec` as a bench cell's agent may read it. A task's spec names absolute paths of the machine it was written
    on; a cell has only its workspace, so a path under a source checkout in `roots` becomes relative to the
    workspace (the checkout root itself `.`), and one under `drop` (the project's state and runs, the bench and
    results folders, other worktrees) or the real home folder `home` (also written `~/...`), which a cell must not
    follow, becomes WITHHELD."""
    def alt(paths) -> str:
        return "|".join(re.escape(p) for p in sorted({str(p).rstrip("/") for p in paths} - {"", "."}, key=len,
                                                     reverse=True))
    edge = r"(?<![\w./~-])"
    if names := alt(drop):
        spec = re.sub(edge + f"(?:{names})(?![\\w.-])" + _PATH_TAIL, WITHHELD, spec)
    if names := alt(roots):
        spec = re.sub(edge + f"(?:{names})/", "", spec)
        spec = re.sub(edge + f"(?:{names})(?![\\w./-])", ".", spec)
    if home:
        spec = re.sub(edge + f"(?:{alt([home])})(?![\\w.-])" + _PATH_TAIL, WITHHELD, spec)
    return re.sub(r"(?<![\w./~-])~/[^\s`'\"<>)\]},;:|&]*", WITHHELD, spec) if home else spec


def _blobs(repo: Path, rev: str) -> dict[str, str]:
    """{path: blob id} of the files at `rev`."""
    def read() -> dict[str, str]:
        out = {}
        for row in _out(repo, "ls-tree", "-r", rev).splitlines():
            meta, _, path = row.partition("\t")
            if " blob " in meta:
                out[path] = meta.split()[-1]
        return out
    return gitmemo.cached(("bench-blobs", str(repo), rev), read)


def _blob(repo: Path, blob: str) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", blob], capture_output=True)
    return r.stdout if r.returncode == 0 else b""


@functools.lru_cache(maxsize=4096)
def _words(repo: Path, blob: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", _blob(repo, blob).decode(errors="replace")))


# A line that may start a top-level statement: not indented, not a comment, not a clause of the one
# before it. A piece that splits a statement or a string does not parse and joins the next one.
_TOP = re.compile(rb"^(?!(?:else|elif|except|finally|case)\b)[A-Za-z_@]", re.M)
_PIECE_MAX = 1 << 15   # bytes a piece that does not parse may grow to before the whole file is parsed


@functools.lru_cache(maxsize=1 << 16)
def _piece(src: bytes) -> list | None:
    try:
        return ast.parse(src).body
    except (SyntaxError, ValueError):
        return None


@functools.lru_cache(maxsize=8)   # a task's file versions: base and head of its test files, head of its changed code
def _tree(repo: Path, blob: str) -> ast.Module | None:
    """`blob` parsed piece by piece, each top-level piece cached by its text: a new version of a
    large test file parses only the pieces it changed (one parse of a 2 MB file takes about 1 s)."""
    src = _blob(repo, blob)
    cuts = [m.start() for m in _TOP.finditer(src)][1:] + [len(src)]
    body, start = [], 0
    for cut in cuts:
        nodes = _piece(src[start:cut])
        if nodes is not None:
            body += nodes
            start = cut
        elif cut - start > _PIECE_MAX:   # each retry parses it all again: stop before that costs more
            break
    if start < len(src):   # no cut ends a statement: the file as a whole decides
        try:
            return ast.parse(src)
        except (SyntaxError, ValueError):
            return None
    return ast.Module(body=body, type_ignores=[])


@functools.lru_cache(maxsize=1024)
def _tests_at(repo: Path, blob: str) -> dict:
    tree = _tree(repo, blob)
    return addedtests.tests_in(tree) if tree else {}


def _tests_of(repo: Path):
    """addedtests.added's tests_of, parsing each file version once."""
    def tests_of(rev: str, path: str) -> dict:
        blob = _blobs(repo, rev).get(path)
        return _tests_at(repo, blob) if blob else {}
    return tests_of


def _roots(repo: Path, blob: str, names: frozenset[str]) -> list:
    """The top-level nodes of a test file that the test functions `names` (qualnames) run: those tests, its
    imports, helpers and fixtures; never its other tests."""
    tree = _tree(repo, blob)
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def other(node, cls: str = "") -> bool:
        return isinstance(node, funcs) and node.name.startswith("test") and f"{cls}{node.name}" not in names
    roots = []
    for node in tree.body if tree else []:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            roots += [*node.decorator_list, *node.bases, *(m for m in node.body if not other(m, f"{node.name}."))]
        elif not other(node):
            roots.append(node)
    return roots


@functools.lru_cache(maxsize=64)
def _defines(repo: Path, blob: str) -> frozenset[str]:
    """The names a Python file defines: its functions and classes at any depth, its module and class level
    variables, and the attributes it assigns (`self.x = ...`)."""
    tree = _tree(repo, blob)
    out: set[str] = set()
    tops = [*(tree.body if tree else []),
            *(m for n in (tree.body if tree else []) if isinstance(n, ast.ClassDef) for m in n.body)]
    for node in tops:
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
        out |= {t.id for x in targets for t in ast.walk(x) if isinstance(t, ast.Name)}
    for node in ast.walk(tree) if tree else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            out |= {t.attr for x in (node.targets if isinstance(node, ast.Assign) else [node.target])
                    for t in ast.walk(x) if isinstance(t, ast.Attribute)}
    return frozenset(out)


def _uses(repo: Path, blob: str, names: frozenset[str]) -> tuple[list, set]:
    """What the test functions `names` (qualnames) of a test file and its other code (imports,
    helpers, fixtures; not its other tests) read: (imports as (module, name, alias), module None for
    `import name`; attribute chains as (name, attr, ...))."""
    imports, chains, inner = [], set(), set()
    for node in (n for r in _roots(repo, blob, names) for n in ast.walk(r)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports += [(node.module, a.name, a.asname) for a in node.names]
        elif isinstance(node, ast.Import):
            imports += [(None, a.name, a.asname) for a in node.names]
        elif isinstance(node, ast.Attribute) and id(node) not in inner:
            parts, v = [node.attr], node.value
            while isinstance(v, ast.Attribute):
                inner.add(id(v))
                parts.append(v.attr)
                v = v.value
            if isinstance(v, ast.Name):
                chains.add((v.id, *reversed(parts)))
    return imports, chains


def unknown_symbols(repo: Path, base: str, head: str, tests: list[tuple[str, str]], spec: str) -> list[str]:
    """The repository modules and their attributes the held-out `tests` ([(path, qualname)], with
    their files' other code, at `head`) use that neither exist at `base` nor appear in `spec`: a
    hidden interface no candidate could guess."""
    at_head, at_base = _blobs(repo, head), _blobs(repo, base)
    mods: dict[str, list[str]] = {}
    for path in at_head:
        if path.endswith(".py"):
            parts = path.split("/")
            mods.setdefault(parts[-2] if parts[-1] == "__init__.py" and len(parts) > 1 else parts[-1][:-3],
                            []).append(path)
    words = set(re.findall(r"\w+", spec))
    used: set[tuple[str, str]] = set()
    for path in sorted({p for p, _ in tests}):
        if path not in at_head:
            continue
        imports, chains = _uses(repo, at_head[path], frozenset(n for p, n in tests if p == path))
        bound: dict[str, str | None] = {}   # a name the file binds: the repository module it names
        for module, name, alias in imports:
            last = name.split(".")[-1]
            if module is not None:
                parent = module.split(".")[-1]
                if name in mods:
                    bound[alias or name] = name
                    used.add((name, ""))
                elif parent in mods:
                    used.add((parent, name))
            elif alias:
                if last in mods:
                    bound[alias] = last
                    used.add((last, ""))
            else:   # `import a.b` binds a; a.b.c reads b's c
                dotted = [x for x in name.split(".") if x in mods]
                if dotted:
                    bound[name.split(".")[0]] = name.split(".")[0] if name.split(".")[0] in mods else None
                    used |= {(x, "") for x in dotted}
        for root, *attrs in chains:
            if root not in bound:
                continue
            mod = bound[root]
            for attr in attrs:
                below = (f"/{mod or root}/{attr}.py", f"/{mod or root}/{attr}/__init__.py")
                if any(("/" + x).endswith(below) for x in mods.get(attr, ())):   # a submodule
                    mod = attr
                    used.add((mod, ""))
                    continue
                if mod is not None:
                    used.add((mod, attr))
                break
    out = []
    for mod, attr in sorted(used):
        have = [at_base[p] for p in mods[mod] if p in at_base]
        if have and attr:
            have = [b for b in have if attr in _words(repo, b)]
        if not have and (attr or mod) not in words:
            out.append(f"{mod}.{attr}" if attr else mod)
    return out


MESSAGE_MAX = 300   # characters of a test's string searched for message parts
MESSAGE_MIN = 8   # characters of a string a test asserts before it counts as a message text the spec must state
_KEY = re.compile(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*")   # an identifier or a dotted config key
_FLAG = re.compile(r"--?[A-Za-z][\w-]*")
_LITERAL = re.compile(r"""(["'])((?:\\.|(?!\1).)*)\1""")   # a quoted string on one line, and its text
_HOLE = re.compile(r"\{[^{}]*\}|%[-+ #0]*\d*(?:\.\d+)?[sdrif]")   # an f-string or format placeholder, a %-format
_MESSAGE_PARTS = re.compile(r"\d+(?:\.\d+)?|\([^)]*\)|'[^']*'|\"[^\"]*\"|`[^`]*`")   # what a message fills in


def _stated(item: str, spec: str, words: set[str]) -> bool:
    low = spec.lower()
    if " " in item:   # a message: the spec gives its text
        return flat(item).lower() in flat(spec).lower()
    parts = [x for x in re.split(r"[.]", item.lstrip("-")) if x]
    return item.lower() in low or (parts and parts[-1] in words) or item.lstrip("-") in words


def unstated(repo: Path, base: str, head: str, tests: list[tuple[str, str]], spec: str) -> list[str]:
    """What the held-out `tests` ([(path, qualname)], with their files' other code, at `head`) require that the
    change itself introduced and `spec` never states: a config key, an attribute, keyword, function or CLI flag
    name, or a message text the tests assert. Each is new in the change's non-test files (in their added lines,
    absent from their base versions), so a candidate could only guess it: the task is not fair. A name the
    change defines (a private helper too) counts wherever the tests' code or texts name it: called, read as an
    attribute, patched or looked up by a string, or named in a docstring or message. Sorted, one entry per item."""
    at_head, at_base = _blobs(repo, head), _blobs(repo, base)
    changed = [x for x in _out(repo, "diff", "--no-renames", "--name-only", base, head).splitlines()
               if x and not _held(x)]
    before_words: set[str] = set()
    before_text = []
    for path in changed:
        if path in at_base:
            before_words |= _words(repo, at_base[path])
            before_text.append(_blob(repo, at_base[path]).decode(errors="replace"))
    lines = _changed(repo, base, head)[0] if changed else []
    added = "\n".join(lines)
    # the string literals of the added code lines (never a comment or a docstring): where output text lives
    literals = "\n".join(m[1] for x in lines if not x.startswith(('#', '"""', "'''"))
                         for m in _LITERAL.findall(x))
    # each added literal with a placeholder as a pattern (a hole matches any text) and its longest fixed part:
    # a message whose filled-in part leads ("{name} is paused") has no fixed start to match by prefix
    templates = []
    for lit in literals.splitlines():
        fixed = [x.strip(" :;,.") for x in _HOLE.split(lit)]
        if len(fixed) > 1 and (best := max(fixed, key=len)) and len(best) >= MESSAGE_MIN and " " in best:
            templates.append((re.compile(".+?".join(re.escape(x) for x in _HOLE.split(lit))), best))
    added_words = set(re.findall(r"[\w-]+", added))
    # the names the change defines in its non-test code (private ones too) that no base version of its files has
    made = {x for path in changed if path.endswith(".py") and path in at_head
            for x in _defines(repo, at_head[path]) if x not in before_words}
    before = "\n".join(before_text)
    words = set(re.findall(r"[\w-]+", spec))
    out: list[str] = []

    def take(item: str, part: str = "") -> None:
        if item not in out and not _stated(part or item, spec, words):
            out.append(item)
    for path in sorted({p for p, _ in tests}):
        if path not in at_head:
            continue
        roots = _roots(repo, at_head[path], frozenset(n for p, n in tests if p == path))
        # an assert's own failure message is for the reader, never output the change must produce
        # (nor is a docstring or another bare string statement)
        said = {id(m) for r in roots for a in ast.walk(r) if isinstance(a, ast.Assert) and a.msg
                for m in ast.walk(a.msg)}
        said |= {id(a.value) for r in roots for a in ast.walk(r)
                 if isinstance(a, ast.Expr) and isinstance(a.value, ast.Constant)}
        # a name the change defined, in any of the tests' texts (a docstring, a message, a patch target such as
        # "mod._name" or getattr's "name"): the tests were written against that hidden definition
        for node in (n for r in roots for n in ast.walk(r)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for name in re.findall(r"[A-Za-z_]\w{2,}", node.value):
                    if name in made:
                        take(name)
        for node in (n for r in roots for n in ast.walk(r) if id(n) not in said):
            names = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            # a private name the change defined, imported (`from mod import _helper`) or used bare (`_helper()`);
            # a public one imported is unknown_symbols' finding
            elif isinstance(node, (ast.alias, ast.Name)) and \
                    (name := node.name if isinstance(node, ast.alias) else node.id).startswith("_") and name in made:
                names.append(name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if _FLAG.fullmatch(v) or (_KEY.fullmatch(v) and ("_" in v or "." in v)):
                    names.append(v)
                else:   # a message, or its fixed parts where the change fills in numbers, names or quotes
                    for part in _MESSAGE_PARTS.split(v[:MESSAGE_MAX]):
                        # its longest start the change wrote (a plural or a name it fills in may follow)
                        part = next((x for k in range(len(part.strip(" :;,.")), MESSAGE_MIN - 1, -1)
                                     if " " in (x := part.strip(" :;,.")[:k]) and x in literals), "")
                        if part and part not in before:
                            take(v.strip(), part)
                            break
                    else:
                        part = next((b for t, b in templates if t.search(v[:MESSAGE_MAX]) and b not in before), "")
                        if part:
                            take(v.strip(), part)
            for name in names:   # a name the change made up: new in its added lines, in no base version
                bare = name.lstrip("-")
                key = bare.split(".")[-1]
                if len(key) >= 3 and key in added_words and key not in before_words and \
                        (name in added or bare in added):
                    take(name)
    return sorted(out)


def fair(bench_dir: Path, repo: Path | None = None) -> list[dict]:
    """Read-only: each task of a `ttp bench prep` folder with its verdict, whether its held-out tests depend
    only on what its spec states (unstated: [] when fair), or why it cannot tell (its commits are gone)."""
    man = json.loads((Path(bench_dir) / "manifest.json").read_text())
    repo = Path(repo or man.get("repo") or ".")
    rows = []
    for t in man.get("tasks", []):
        tests = [tuple(x.split("::", 1)) for x in t.get("tests", []) if "::" in x]
        if any(_git(repo, "cat-file", "-e", f"{x}^{{commit}}").returncode for x in (t["base"], t["head"])):
            rows.append({"task": t["task"], "fair": None, "unstated": [],
                         "why": "its base or head commit is not in the repository"})
            continue
        items = unknown_symbols(repo, t["base"], t["head"], tests, t.get("spec") or "")
        items += [x for x in unstated(repo, t["base"], t["head"], tests, t.get("spec") or "") if x not in items]
        rows.append({"task": t["task"], "fair": not items, "unstated": items})
    return rows


def _archive(repo: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)


def workspace(repo: Path, base: str, dest: Path) -> str:
    """A fresh repository at `dest` whose one ref, refs/heads/main, is the commit `base` itself, holding its
    history up to and including it and nothing after it: no remotes, tags, reflog, other refs or object of
    a later commit. Returns its tree id (`base`'s tree). A submodule's commit is not copied."""
    commit = _out(repo, "rev-parse", f"{base}^{{commit}}")
    dest.mkdir(parents=True)
    git = ["git", "-C", str(dest), "-c", "core.logAllRefUpdates=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True, capture_output=True)
    # One pack of every object reachable from the base (its history, not what came after it).
    pack = subprocess.Popen(["git", "-C", str(repo), "pack-objects", "-q", "--revs", "--stdout"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    index = subprocess.Popen([*git, "index-pack", "--stdin"], stdin=pack.stdout, stdout=subprocess.DEVNULL)
    pack.stdout.close()
    pack.communicate(commit.encode() + b"\n")
    if pack.returncode or index.wait():
        raise subprocess.CalledProcessError(pack.returncode or index.returncode, "git pack-objects | index-pack")
    subprocess.run([*git, "update-ref", "refs/heads/main", commit], check=True, capture_output=True)
    subprocess.run([*git, "read-tree", commit], check=True, capture_output=True)
    subprocess.run([*git, "checkout-index", "-a", "-f"], check=True, capture_output=True)
    shutil.rmtree(dest / ".git" / "logs", ignore_errors=True)
    return _out(dest, "rev-parse", "HEAD^{tree}")


def seal(dest: Path) -> str:
    """Make the files in `dest` a fresh repository with one commit and no remotes, tags or reflog;
    returns its tree id."""
    env = {**os.environ, **_EPOCH}
    git = ["git", "-C", str(dest), "-c", "user.name=bench", "-c", "user.email=bench@localhost",
           "-c", "core.logAllRefUpdates=false", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A", "-f"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "--no-verify", "-m", "base"], check=True, capture_output=True, env=env)
    shutil.rmtree(dest / ".git" / "logs", ignore_errors=True)
    return _out(dest, "rev-parse", "HEAD^{tree}")


class PrepFault(RuntimeError):
    """A validation run that ran none of the tests it selected (a broken test command or environment, not
    the task): prep stops on it, naming the command, its exit code and its last output lines."""


TAIL_LINES = 20     # output lines a PrepFault quotes


def _passes(where: Path, tests: list[tuple[str, str]], rounds: int, cmd: str, timeout: float) -> dict[str, int]:
    """{path::name: rounds passed} of `tests` run `rounds` times by pytest `cmd` in `where`, through
    the rerun plugin (spread off) in a process sandbox where the host has one (ttp sandbox). `cmd` runs
    only the held-out tests' paths (_held_cmd), so a broken test module elsewhere in the tree is never
    collected. The run's {"rc", "argv", "tail", "selected", "ran", "timed_out"} is kept on the result's
    `run` attribute (a dict subclass) for validate."""
    from . import sandbox
    out = where.parent / f"{where.name}.jsonl"
    log = where.parent / f"{where.name}.out"
    env = addedtests.env(tests, out, {**os.environ, "PWD": str(where)}, rounds)
    env[addedtests.ENV] = json.dumps({**json.loads(env[addedtests.ENV]), "spread": False})
    argv = _held_cmd(cmd, [f"{p}::{n}" for p, n in tests])[0]
    timed_out = False
    with open(log, "wb") as sink:
        # own session: the test run is killed whole (its group) once past the timeout
        proc = subprocess.Popen(sandbox.wrap(argv, env=env), cwd=where, env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:   # counts as no pass: tally reads what the record holds
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            timed_out = True
    t = addedtests.tally(out, tests, rounds)
    res = _Passes({f"{p}::{n}": t["passes"].get(f"{p}::{n}", 0) for p, n in tests})
    res.run = {"rc": proc.returncode, "argv": argv, "timed_out": timed_out, "selected": t["selected"],
               "ran": sum(t["ran"].values()), "tail": _tail(log)}
    return res


class _Passes(dict):
    run: dict = {}


def _tail(path: Path, n: int = TAIL_LINES) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _fault(run: dict, where: str) -> str:
    """Why this validation run on `where` ran none of its selected tests through no fault of the task
    ("" when it ran them): pytest ended in an error of its own (exit 2: interrupted, as by a collection
    error; 3: internal error; 4: usage error; 5: nothing collected), or the rerun plugin selected or ran
    none of them."""
    rc = run["rc"]
    if rc in (2, 3, 4, 5):
        return f"pytest exited {rc} on the {where}"
    if not run["selected"]:
        return f"none of the held-out tests was collected on the {where} (exit {rc})"
    if not run["ran"]:
        return f"none of the held-out tests ran on the {where} (exit {rc})"
    return ""


def validate(repo: Path, base: str, head: str, tests: list[tuple[str, str]], files: list[str],
             cmd: str = TEST_CMD, timeout: float = TEST_TIMEOUT) -> tuple[list[tuple[str, str]], str]:
    """The `tests` that fail on `base` with the head's copy of the test `files` in place and pass
    all ROUNDS runs on `head`, and why none do ("" when some do). Raises PrepFault when the head's run
    ran none of them (_fault): the test command or the environment is broken, and every later task
    would be excluded for it. The base may fail any way (its held-out tests may not even import there):
    a broken command shows on the head's run, which runs the same command on the same paths."""
    with tempfile.TemporaryDirectory(prefix="ttp-bench-") as tmp:
        on_base, on_head = Path(tmp) / "base", Path(tmp) / "head"
        _archive(repo, base, on_base)
        _archive(repo, head, on_head)
        for path in files:
            (on_base / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(on_head / path, on_base / path)
        fails = _passes(on_base, tests, 1, cmd, timeout)
        holds = _passes(on_head, tests, ROUNDS, cmd, timeout)
    if holds.run["timed_out"]:
        return [], f"its validation run on the head passed the {timeout:.0f} s timeout"
    if why := _fault(holds.run, "head"):
        raise PrepFault(f"bench prep: validation ran none of the held-out tests ({why}): "
                        f"{shlex.join(holds.run['argv'])}\n" + "\n".join(holds.run["tail"]))
    keep = [t for t in tests if not fails[f"{t[0]}::{t[1]}"] and holds[f"{t[0]}::{t[1]}"] == ROUNDS]
    if keep:
        return keep, ""
    if all(fails.values()):
        return [], f"its held-out tests pass on the base: {', '.join(fails)}"
    return [], f"none of its held-out tests fails on the base and passes {ROUNDS} of {ROUNDS} runs on the head"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reach(rows, pushed: list[dict]) -> set[int]:
    """The tasks whose carriers (landed.carriers) may include a task or branch of a `pushed` row: a
    superset found once, walking every carrier link back from those rows, so a task no row can
    reach skips carriers."""
    branch: dict[str, set[int]] = {}
    for tid, t in rows.tasks.items():
        branch.setdefault(t.get("branch") or "", set()).add(tid)
    links = {}
    for i, b, _, ids, names, follows in rows.links:
        branch.setdefault(b, set()).add(i)
        links[i] = (ids | follows, names)
    branch.pop("", None)
    seen = {r["task"] for r in pushed} | {i for r in pushed for i in branch.get(r["branch"] or "", ())}
    todo = list(seen)
    while todo:
        ids, names = links.get(todo.pop(), (set(), set()))
        for j in ids.union(*(branch.get(n, ()) for n in names)) - seen:
            seen.add(j)
            todo.append(j)
    return seen


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def prep(db, repo: Path, out: Path, since: float, limit: int = 0, test_cmd: str = TEST_CMD,
         validated: bool = True, progress=_stderr) -> dict:
    """Write the bench to `out` (which must not exist yet) and return its manifest. `validated`:
    keep only the fail-to-pass tests `test_cmd` finds (validate); a validation run that runs none of
    them raises PrepFault. `progress` gets one line per candidate (kept or excluded with why, its
    seconds, the count kept so far and the total elapsed), so a long prep shows where it is."""
    from . import landed
    out.mkdir(parents=True)
    tasks = db.q("SELECT id, kind, title, spec, created FROM tasks WHERE status='done' AND kind IN ('code','work') "
                 "AND created >= ? ORDER BY id", (since,))
    rows = landed.Rows(db, [t["id"] for t in tasks])
    pushed = db.q("SELECT task, branch, head, landed_sha FROM push_queue WHERE status IN ('pushed','landed') "
                  "AND landed_sha IS NOT NULL AND landed_sha != '' ORDER BY id")
    reach = _reach(rows, pushed)
    kept, excluded = [], []
    began = time.monotonic()

    def say(t: dict, what: str, t0: float) -> None:
        now = time.monotonic()
        progress(f"bench prep: #{t['id']} {what} ({now - t0:.0f}s; {len(kept)} kept"
                 f"{f' of {limit}' if limit else ''}, {len(excluded)} excluded, {now - began:.0f}s in)")
    with gitmemo.scope({}), concurrent.futures.ThreadPoolExecutor(BUILDERS) as pool:
        for t in tasks:
            if limit and len(kept) >= limit:
                break
            t0 = time.monotonic()
            mine = []
            if t["id"] in reach:
                carry = landed.carriers(db, t["id"], rows)
                branches = {v[0] for v in carry.values() if v[0]}
                mine = [r for r in pushed if r["task"] in carry or r["branch"] in branches]
            base, head, why = landing(repo, mine)
            parent = PARENT.search(t["spec"] or "")
            if not why and parent:
                base, head, why = from_parent(db, repo, int(parent[1]), parent[2] or "", mine[0]["head"])
            tests = addedtests.added(repo, base, head, _tests_of(repo)) if base else []
            if not why and not tests:
                why = "its landed change adds or changes no test"
            added, lines = _changed(repo, base, head) if not why else ([], 0)
            if not why and (n := quotes(t["spec"], added)) >= QUOTE_LINES:
                why = f"its spec quotes {n} lines of the fix"
            if not why and (hidden := unknown_symbols(repo, base, head, tests, t["spec"])):
                why = f"its held-out tests use symbols the base lacks and the spec does not name: {', '.join(hidden)}"
            if not why and (hidden := unstated(repo, base, head, tests, t["spec"] or "")):
                why = ("its held-out tests require names or texts the change made up and the spec does not state: "
                       + ", ".join(repr(x) for x in hidden[:8]) + (" ..." if len(hidden) > 8 else ""))
            held = [x for x in _out(repo, "diff", "--no-renames", "--name-only", "--diff-filter=AM", base, head)
                    .splitlines() if _held(x)] if not why else []
            if not why and validated:
                try:
                    tests, why = validate(repo, base, head, tests, held, test_cmd)
                except PrepFault:   # no bench from a broken command: the folder it made goes
                    say(t, "stopped: its validation ran none of its held-out tests", t0)
                    pool.shutdown(wait=True)
                    shutil.rmtree(out, ignore_errors=True)
                    raise
            if why:
                excluded.append({"task": t["id"], "why": why})
                say(t, f"excluded: {why}", t0)
                continue
            here = out / str(t["id"])
            tree = pool.submit(workspace, repo, base, here / "workspace")
            sums = {}
            for path in held:
                dest = here / "heldout" / path
                durable_write(dest, _blob(repo, _blobs(repo, head)[path]), sync=False)
                sums[path] = _sha(dest)
            kept.append({"task": t["id"], "kind": t["kind"], "title": t["title"],
                         "spec": scrub_spec(t["spec"] or "", [repo, repo.resolve()]),
                         "created": t["created"],
                         "parent": int(parent[1]) if parent else None, "base": base, "head": head, "tree": tree,
                         "stratum": "small" if lines <= SMALL else "large", "changed_lines": lines,
                         "tests": [f"{p}::{n}" for p, n in tests], "heldout": sums})
            say(t, f"kept, {len(tests)} test(s)", t0)
        for k in kept:
            k["tree"] = k["tree"].result()
    manifest = {"since": since, "made": time.time(), "repo": str(repo.resolve()), "validated": validated,
                **({"test_cmd": test_cmd, "rounds": ROUNDS} if validated else {}), "tasks": kept,
                "excluded": excluded, "counts": {s: sum(k["stratum"] == s for k in kept) for s in ("small", "large")}}
    durable_write(out / "manifest.json", json.dumps(manifest, indent=1) + "\n")
    durable_write(out / "manifest.sha256", f"{_sha(out / 'manifest.json')}  manifest.json\n")
    return manifest


_HUNK = re.compile(r"^@@ [^+]*\+(\d+)(?:,(\d+))? @@")
_UNPINNED = ("workspace", "canary", "remote.git")   # pinned by tree id, growing canary logs, a git dir


def _targets(repo: Path, base: str, head: str) -> dict[str, set[int]]:
    """{path: head line numbers} the change adds or changes in its non-test Python files (a pure
    deletion: the lines beside it)."""
    paths = [p for p in _out(repo, "diff", "--no-renames", "--name-only", "--diff-filter=AM", base, head).splitlines()
             if p.endswith(".py") and not _held(p)]
    out, cur = {}, None
    for x in _out(repo, "diff", "--no-renames", "-U0", base, head, "--", *paths).splitlines() if paths else []:
        if x.startswith("+++ "):
            cur = x[6:] if x.startswith("+++ b/") else None
        elif cur and (h := _HUNK.match(x)):
            start, n = int(h[1]), 1 if h[2] is None else int(h[2])
            out.setdefault(cur, set()).update(range(start, start + n) if n else (start, start + 1))
    return out


def _pins(cell: Path) -> dict[str, str]:
    """{path: sha256} of the cell's fixed files: all but its workspace (pinned by tree id), canary logs
    and remote repository, whose refusing hook is pinned."""
    files = [p for p in cell.rglob("*") if p.is_file() and not p.is_symlink()
             and p.relative_to(cell).parts[0] not in _UNPINNED]
    hook = cell / "remote.git" / "hooks" / "pre-receive"
    return {str(p.relative_to(cell)): _sha(p) for p in sorted(files + ([hook] if hook.is_file() else []))}


def _held_cmd(test_cmd: str, tests: list[str]) -> tuple[list[str], list[str]]:
    """(argv, select): `test_cmd` running only the held-out `tests` ("path::name"). A plain pytest run
    selects them with the ttp_pytest_select plugin (benchcells.select_env, added at run time), so a case
    file's test runs where its aggregator collects it: a file pytest collects by its name is passed itself,
    any other its top directory. Other commands get the ids and no select. Neither names a host path."""
    from . import addedtests
    if not addedtests.eligible(test_cmd):
        return [*shlex.split(test_cmd), *tests], []
    files = dict.fromkeys(t.partition("::")[0] for t in tests)
    paths = dict.fromkeys(f if re.fullmatch(r"test_.*\.py|.*_test\.py", Path(f).name) else Path(f).parts[0]
                          for f in files)
    return [*shlex.split(test_cmd), *paths], list(tests)


def cells(bench_dir: Path, out: Path, repo: Path | None = None, seeded: int = 0, test_cmd: str = TEST_CMD,
          tries: int = 20, timeout: float = TEST_TIMEOUT) -> dict:
    """Build seeded-bug and prompt-injection cells (benchcells) in `out` (new) from the `ttp bench prep`
    folder `bench_dir` and return their manifest. Seeded: per small task (at most `seeded`, 0 for all),
    a mutation of its landed change's non-test lines in an archive of its landed head that its held-out
    tests catch; a task with none is excluded with why. Injection: one cell per input and action, from
    the small tasks' workspaces and specs in turn."""
    from . import benchcells
    pin = (bench_dir / "manifest.sha256").read_text().split()[0]
    if _sha(bench_dir / "manifest.json") != pin:
        raise ValueError(f"{bench_dir / 'manifest.json'} does not match manifest.sha256")
    bench = json.loads((bench_dir / "manifest.json").read_text())
    repo = repo or Path(bench["repo"])
    small = [k for k in bench["tasks"] if k["stratum"] == "small"]
    if not small:
        raise ValueError(f"{bench_dir} keeps no small task to build cells from")
    out.mkdir(parents=True)
    rows, excluded = [], []

    def one(k: dict) -> tuple[dict, dict | None, str]:
        targets = _targets(repo, k["base"], k["head"])
        if not targets:
            return k, None, "its landed change touches no non-test Python line"
        cmd, select = _held_cmd(test_cmd, k["tests"])
        with tempfile.TemporaryDirectory(prefix="ttp-cells-") as tmp:
            src = Path(tmp) / "head"
            _archive(repo, k["head"], src)
            m, why = benchcells.find_bug(src, cmd, targets, rng_seed=k["task"], tries=tries, timeout=timeout,
                                          select=select)
            if m:
                benchcells.seeded_cell(src, list(k["heldout"]), m, cmd, out / "seeded" / str(k["task"]),
                                       task=k["spec"] or k["title"], select=select)
        return k, m, why

    with concurrent.futures.ThreadPoolExecutor(BUILDERS) as pool:
        todo = list(small)
        while todo and not (seeded and len(rows) >= seeded):
            n = min(BUILDERS, seeded - len(rows)) if seeded else BUILDERS
            batch, todo = todo[:n], todo[n:]
            for k, m, why in pool.map(one, batch):
                if not m:
                    excluded.append({"task": k["task"], "why": why})
                    continue
                path = out / "seeded" / str(k["task"])
                rows.append({"id": f"seeded/{k['task']}", "kind": "seeded", "task": k["task"], "mutation": m,
                             "tree": json.loads((path / "cell.json").read_text())["tree"], "files": _pins(path)})
    for i, (source, canary) in enumerate((s, c) for s in benchcells.SOURCES for c in benchcells.CANARIES):
        k = small[i % len(small)]
        path = out / "injection" / f"{source}-{canary}"
        benchcells.injection_cell(bench_dir / str(k["task"]) / "workspace", k["spec"] or k["title"], source, canary,
                                  path)
        rows.append({"id": f"injection/{source}-{canary}", "kind": "injection", "task": k["task"], "source": source,
                     "canary": canary, "tree": _out(path / "workspace", "rev-parse", "HEAD^{tree}"),
                     "files": _pins(path)})

    def tally(field: str, kind: str) -> dict[str, int]:
        vals = [(r["mutation"]["kind"] if field == "mutation" else r[field]) for r in rows if r["kind"] == kind]
        return {v: vals.count(v) for v in sorted(set(vals))}

    manifest = {"bench": str(bench_dir.resolve()), "bench_sha256": pin, "repo": str(repo.resolve()), "made": time.time(),
                "test_cmd": test_cmd, "tries": tries, "cells": rows, "excluded": excluded,
                "counts": {"seeded": sum(r["kind"] == "seeded" for r in rows),
                           "injection": sum(r["kind"] == "injection" for r in rows),
                           "seeded_by_mutation": tally("mutation", "seeded"),
                           "injection_by_source": tally("source", "injection"),
                           "injection_by_canary": tally("canary", "injection")}}
    durable_write(out / "cells.json", json.dumps(manifest, indent=1) + "\n")
    durable_write(out / "cells.sha256", f"{_sha(out / 'cells.json')}  cells.json\n")
    return manifest

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
since a workspace at the push branch base would lack the parent's work. Each kept task gets <out>/<id>/workspace, `git archive` of the base in a fresh
repository with one commit and no remotes, tags or reflog, and <out>/<id>/heldout, the head's copy
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


def quotes(spec: str, added: list[str]) -> int:
    """How many distinct added non-test lines of QUOTE_LEN characters or more `spec` quotes."""
    body = " ".join(spec.split())
    return len({x for x in added if len(x) >= QUOTE_LEN and " ".join(x.split()) in body})


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


@functools.lru_cache(maxsize=4)   # a task's file versions: base and head of one or two files
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


def _uses(repo: Path, blob: str, names: frozenset[str]) -> tuple[list, set]:
    """What the test functions `names` (qualnames) of a test file and its other code (imports,
    helpers, fixtures; not its other tests) read: (imports as (module, name, alias), module None for
    `import name`; attribute chains as (name, attr, ...))."""
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
    imports, chains, inner = [], set(), set()
    for node in (n for r in roots for n in ast.walk(r)):
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


def _archive(repo: Path, rev: str, dest: Path) -> None:
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)


def workspace(repo: Path, base: str, dest: Path) -> str:
    """A fresh repository at `dest` holding `base`'s tracked files in one commit, with no remotes,
    tags, reflog or other history; returns its tree id (equal to `base`'s tree). A submodule's
    commit is not copied."""
    tree = _out(repo, "rev-parse", f"{base}^{{tree}}")
    objs = [tree] + [x.split()[2] for x in _out(repo, "ls-tree", "-r", "-t", tree).splitlines()
                     if x.split()[1] in ("blob", "tree")]
    dest.mkdir(parents=True)
    git = ["git", "-C", str(dest), "-c", "user.name=bench", "-c", "user.email=bench@localhost",
           "-c", "core.logAllRefUpdates=false", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True, capture_output=True)
    # One pack of the tree's objects, not a loose object per file (git add): far less disk I/O.
    pack = subprocess.Popen(["git", "-C", str(repo), "pack-objects", "-q", "--stdout"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    index = subprocess.Popen([*git, "index-pack", "--stdin"], stdin=pack.stdout, stdout=subprocess.DEVNULL)
    pack.stdout.close()
    pack.communicate("\n".join(objs).encode() + b"\n")
    if pack.returncode or index.wait():
        raise subprocess.CalledProcessError(pack.returncode or index.returncode, "git pack-objects | index-pack")
    subprocess.run([*git, "read-tree", tree], check=True, capture_output=True)
    subprocess.run([*git, "checkout-index", "-a", "-f"], check=True, capture_output=True)
    commit = subprocess.run([*git, "commit-tree", "-m", "base", tree], check=True, capture_output=True, text=True,
                            env={**os.environ, **_EPOCH}).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/heads/main", commit], check=True, capture_output=True)
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


def _passes(where: Path, tests: list[tuple[str, str]], rounds: int, cmd: str, timeout: float) -> dict[str, int]:
    """{path::name: rounds passed} of `tests` run `rounds` times by pytest `cmd` in `where`, through
    the rerun plugin (spread off) in a process sandbox where the host has one (ttp sandbox)."""
    from . import sandbox
    out = where.parent / f"{where.name}.jsonl"
    env = addedtests.env(tests, out, {**os.environ, "PWD": str(where)}, rounds)
    env[addedtests.ENV] = json.dumps({**json.loads(env[addedtests.ENV]), "spread": False})
    # own session: the test run is killed whole (its group) once past the timeout
    proc = subprocess.Popen(sandbox.wrap(shlex.split(cmd), env=env), cwd=where, env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:   # counts as no pass: tally reads what the record holds
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    t = addedtests.tally(out, tests, rounds)
    return {f"{p}::{n}": t["passes"].get(f"{p}::{n}", 0) for p, n in tests}


def validate(repo: Path, base: str, head: str, tests: list[tuple[str, str]], files: list[str],
             cmd: str = TEST_CMD, timeout: float = TEST_TIMEOUT) -> tuple[list[tuple[str, str]], str]:
    """The `tests` that fail on `base` with the head's copy of the test `files` in place and pass
    all ROUNDS runs on `head`, and why none do ("" when some do)."""
    with tempfile.TemporaryDirectory(prefix="ttp-bench-") as tmp:
        on_base, on_head = Path(tmp) / "base", Path(tmp) / "head"
        _archive(repo, base, on_base)
        _archive(repo, head, on_head)
        for path in files:
            (on_base / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(on_head / path, on_base / path)
        fails = _passes(on_base, tests, 1, cmd, timeout)
        holds = _passes(on_head, tests, ROUNDS, cmd, timeout)
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


def prep(db, repo: Path, out: Path, since: float, limit: int = 0, test_cmd: str = TEST_CMD,
         validated: bool = True) -> dict:
    """Write the bench to `out` (which must not exist yet) and return its manifest. `validated`:
    keep only the fail-to-pass tests `test_cmd` finds (validate)."""
    from . import landed
    out.mkdir(parents=True)
    tasks = db.q("SELECT id, kind, title, spec, created FROM tasks WHERE status='done' AND kind IN ('code','work') "
                 "AND created >= ? ORDER BY id", (since,))
    rows = landed.Rows(db, [t["id"] for t in tasks])
    pushed = db.q("SELECT task, branch, head, landed_sha FROM push_queue WHERE status IN ('pushed','landed') "
                  "AND landed_sha IS NOT NULL AND landed_sha != '' ORDER BY id")
    reach = _reach(rows, pushed)
    kept, excluded = [], []
    with gitmemo.scope({}), concurrent.futures.ThreadPoolExecutor(BUILDERS) as pool:
        for t in tasks:
            if limit and len(kept) >= limit:
                break
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
            held = [x for x in _out(repo, "diff", "--no-renames", "--name-only", "--diff-filter=AM", base, head)
                    .splitlines() if _held(x)] if not why else []
            if not why and validated:
                tests, why = validate(repo, base, head, tests, held, test_cmd)
            if why:
                excluded.append({"task": t["id"], "why": why})
                continue
            here = out / str(t["id"])
            tree = pool.submit(workspace, repo, base, here / "workspace")
            sums = {}
            for path in held:
                dest = here / "heldout" / path
                durable_write(dest, _blob(repo, _blobs(repo, head)[path]), sync=False)
                sums[path] = _sha(dest)
            kept.append({"task": t["id"], "kind": t["kind"], "title": t["title"], "spec": t["spec"],
                         "created": t["created"],
                         "parent": int(parent[1]) if parent else None, "base": base, "head": head, "tree": tree,
                         "stratum": "small" if lines <= SMALL else "large", "changed_lines": lines,
                         "tests": [f"{p}::{n}" for p, n in tests], "heldout": sums})
        for k in kept:
            k["tree"] = k["tree"].result()
    manifest = {"since": since, "made": time.time(), "repo": str(repo.resolve()), "validated": validated,
                **({"test_cmd": test_cmd, "rounds": ROUNDS} if validated else {}), "tasks": kept,
                "excluded": excluded, "counts": {s: sum(k["stratum"] == s for k in kept) for s in ("small", "large")}}
    durable_write(out / "manifest.json", json.dumps(manifest, indent=1) + "\n")
    durable_write(out / "manifest.sha256", f"{_sha(out / 'manifest.json')}  manifest.json\n")
    return manifest

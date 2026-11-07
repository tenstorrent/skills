# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Targeted task checks: `ttp checks` runs only the tests a task's diff can affect, while `ttp push` and
the push queue run their own checks in full on the exact commit they push (they never call this).

A project check that is one plain local pytest run of listed test files is narrowed by the diff from the
merge base with the push target to HEAD. Its units are each top-level test function of a listed file (with
the same-file helpers and fixtures it uses), each case file `tests/X/<area>_cases.py` a listed
`tests/test_X.py` executes, and a listed file without top-level tests whole. A changed case file runs
itself and, as case files share one namespace, the units naming its changed helpers; a changed function
of a listed file runs the tests using it. A changed module `foo.py` runs the
units that import or name it (`foo.`, `import ... foo`); one named by most tests runs, instead, the units
naming the functions and methods the diff changes (a `cmd_<name>` function also by its subcommand in
quotes). Any other file runs the units naming its file (or folder) name. The whole check runs
(`full: <reason>`) on: a conftest, a test helper, fixture or data file beside the listed tests, the
checks runner, a manifest or `__init__.py`, a pytest or package config file, a module-level change
(a removed or changed top-level import included) of a case file, a listed file or a module mapped by
functions, any file nothing places, or a selection of most of the
suite. `delivery.checks_targeted_always` (default ALWAYS) always
run; `delivery.checks_targeted: false` (or `ttp checks --full`) runs every task's checks in full.

With the push queue on (project.push_queue_on), every batch runs the full suite on the merged tree before
anything is pushed and bisects a failure to the changes at fault, so a task's own checks run in full only
where the whole suite is affected (HARD: a conftest, test helper or data file, the runner files, a
manifest, `__init__.py` or config file, a listed file's module-level code); a selection of most of the suite
stays targeted, since the batch runs every test anyway. A change
only the full suite would cover (SOFT: a module's module-level code, the checks runner's functions in a
module, what no test names) adds the tests it can place, and the scope line names it as left to the batch."""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path

KEY, ALWAYS_KEY = "checks_targeted", "checks_targeted_always"   # delivery.<key>
ALWAYS = ("tests/test_plugin_catalogue.py",)
FILES_ENV = "TTP_PYTEST_SELECT_FILES"   # one unit per line: a source file, or <test file>::<test name>
MAX_SHARE = 0.5      # a selection of at least this share of the tests runs in full (not in a push-queue project)
MAX_ENV = 64_000     # the selection's size cap (one exported variable of the check's shell line)
FULL_NAMES = {"conftest.py", "__init__.py", "plugin.json", "marketplace.json", "pytest.ini", "pyproject.toml",
              "setup.cfg", "setup.py", "tox.ini", "requirements.txt"}
RUNNER = ("testpick.py", "push.py", "pushpass.py", "pytest_resume/")   # under runtime/ttp/
RUNNER_DEFS = re.compile(r"cmd_checks\Z|_checks")
GIT_TIMEOUT_S = 30


def enabled(cfg: dict | None) -> bool:
    return ((cfg or {}).get("delivery") or {}).get(KEY, True) is not False


def _git(top: Path, *args: str) -> str | None:
    from . import gitmemo
    try:
        r = gitmemo.run(["git", "-C", str(top), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def test_files(cmd: str, top: Path) -> list[str]:
    """The test files a plain one-run pytest check lists (repo-relative), or [] when it is not one."""
    from . import push
    if push.pytest_runs(cmd) != 1 or push.remote_host(cmd) or re.search(r"[;&|<>`$(){}*?\[\]]|PYTHONPATH|ADDOPTS", cmd):
        return []
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    args = words[next((i for i, w in enumerate(words) if w.rsplit("/", 1)[-1] == "pytest"), 0):]
    files = [w for w in args if w.endswith(".py") and "::" not in w and (top / w).is_file()]
    return files if files and not any(w.startswith("-k") or w.startswith("-m") for w in args) else []


def _first(n) -> int:
    return min([n.lineno, *(d.lineno for d in getattr(n, "decorator_list", ()))])


def _top(text: str) -> list[tuple[str, int, int]]:
    """(name, first line, last line) of each top-level function and class, decorators included."""
    return [(n.name, _first(n), n.end_lineno or n.lineno) for n in ast.parse(text).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _defs(text: str) -> list[tuple[str, int, int]]:
    """_top's entries, each class followed by its methods (the innermost def holding a line comes last)."""
    out = []
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((n.name, _first(n), n.end_lineno or n.lineno))
            out += [(m.name, _first(m), m.end_lineno or m.lineno) for m in getattr(n, "body", ())
                    if isinstance(n, ast.ClassDef) and isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return out


def units(top: Path, files: list[str]) -> dict[str, str]:
    """Each unit's selection entry and the text that names what it tests: a listed file's top-level test
    function with the same-file functions it uses (its fixtures and helpers, transitively), each case file
    of a listed `tests/test_X.py` (tests/X/*_cases.py), and a listed file without top-level tests whole."""
    out: dict[str, str] = {}
    for f in files:
        text = (top / f).read_text(encoding="utf-8", errors="replace")
        cases = sorted((top / f).parent.joinpath(Path(f).stem[len("test_"):]).glob("*_cases.py")) \
            if Path(f).stem.startswith("test_") else []
        for c in cases:
            out[str(c.relative_to(top))] = c.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        src = {name: "\n".join(lines[a - 1:b]) for name, a, b in _top(text)}
        if not any(n.startswith("test_") for n in src):
            out[f] = text
            continue
        uses = {n: set(re.findall(r"\w+", t)) & src.keys() for n, t in src.items()}
        for name in (n for n in src if n.startswith("test_")):
            seen, todo = {name}, [name]
            while todo:
                new = uses[todo.pop()] - seen
                seen |= new
                todo += new
            out[f"{f}::{name}"] = "\n".join(src[n] for n in sorted(seen))
    return out


def changed_defs(top: Path, base: str, head: str, path: str) -> set[str] | None:
    """The top-level functions and classes of `path` the diff changes; None when it also changes code
    outside them (module level) or cannot tell."""
    diff, text = _git(top, "diff", "-U0", "--no-renames", f"{base}...{head}", "--", path), _git(top, "show", f"{head}:{path}")
    if diff is None or text is None:
        return None
    try:
        defs = _defs(text)
    except (SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for hunk in re.split(r"^(?=@@ )", diff, flags=re.M)[1:]:
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", hunk)
        if not m:
            return None
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        body = [ln for ln in hunk.splitlines()[1:] if ln[:1] in "+-"]
        plus = [(start + i, ln[1:]) for i, ln in enumerate(x for x in body if x[0] == "+")]
        gone = [_bound(ln[1:]) for ln in body if ln[0] == "-" and _import(ln[1:])]
        if gone and (None in set().union(*gone) or not set().union(*gone) <= set().union(*(_bound(ln[1:]) for ln in body
                                                               if ln[0] == "+" and _import(ln[1:])))):
            return None   # a top-level import removed or changed: another function may still use it
        real = [ln[1:] for ln in body if _counts(ln[1:])]
        if not real:
            continue   # blank lines, comments and imports only
        if any(ln[0] == "-" and re.match(r"(?:async\s+)?(?:def|class)\s", ln[1:]) for ln in body):
            return None   # a top-level function or class removed or renamed
        lines = [n for n, t in plus if _counts(t)] or ([start] if not count else list(range(start, start + count)))
        for n in lines:
            hit = [d for d in defs if d[1] <= n <= d[2]]
            if not hit:
                return None
            names.add(hit[-1][0])
    return names


def _import(line: str) -> bool:
    return bool(re.match(r"(?:from\s+\S+\s+)?import\s", line))


def _bound(line: str) -> set:
    """What a one-line top-level import binds ({(level, module, name, as)}); {None} when it cannot tell."""
    try:
        nodes = ast.parse(line.strip()).body
    except (SyntaxError, ValueError):
        return {None}
    return {(getattr(n, "level", 0), getattr(n, "module", None), a.name, a.asname)
            for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} or {None}


def _counts(line: str) -> bool:
    """Whether an added line can change behaviour: not blank, a comment or a top-level import (a removed
    import is module level: changed_defs checks it first)."""
    t = line.strip()
    return bool(t) and not t.startswith("#") and not _import(line)


def _weight(us: dict[str, str], keys) -> int:
    """How many tests `keys` hold (a test function unit is one)."""
    return sum(1 if "::" in k else max(1, len(re.findall(r"^(?:async )?def test_", us[k], re.M))) for k in keys)


def _naming(us: dict[str, str], word: str, module: bool) -> set[str]:
    w = re.escape(word)
    rx = re.compile(rf"(?<![\w.]){w}\.\w|^\s*(?:from|import)\b[^\n]*\b{w}\b|ttp\.{w}\b" if module
                    else rf"(?<![\w-]){w}(?![\w-])", re.M)
    return {k for k, t in us.items() if rx.search(t)}


def _place(top: Path, base: str, head: str, path: str, us: dict[str, str], listed: list[str],
           cases: set[str], soft: list[str] | None = None) -> tuple[set[str], str]:
    """The units `path` maps to, or (set(), why it runs the whole check). With `soft` (a push-queue project)
    a reason only the full suite covers is appended there and the units placed so far are returned."""
    p, name = Path(path), Path(path).name

    def left(got: set[str], why: str) -> tuple[set[str], str]:
        if soft is None:
            return set(), why
        soft.append(why)
        return got, ""

    if path in us:   # a case file: itself, and the units naming a changed helper (case files share one namespace)
        defs = changed_defs(top, base, head, path)
        if defs is None:
            return set(), f"module-level code of {path} changed"
        got = {path}
        for d in (d for d in defs if not d.startswith("test_")):
            got |= _naming(us, d, False)
        return got, ""
    if path in listed:   # its changed tests and helpers: the tests using them, and case files naming a helper
        defs = changed_defs(top, base, head, path)
        if defs is None:
            return set(), f"module-level code of {path} changed"
        got = {k for k in us if k.startswith(f"{path}::")
               and any(re.search(rf"^(?:async )?(?:def|class) {re.escape(d)}\b", us[k], re.M) for d in defs)}
        for d in defs - {k.partition("::")[2] for k in got}:
            got |= _naming({k: us[k] for k in cases}, d, False)
        return (got, "") if got else left(set(), f"no test uses what {path} changes")
    if path in cases:
        return set(), ""   # a case file the head deletes: none of its tests remain
    if name in FULL_NAMES:
        return set(), f"{path} changed"
    if any(path.endswith("runtime/ttp/" + r) or ("runtime/ttp/" + r) in path for r in RUNNER):
        return set(), f"the checks runner changed ({path})"
    test_dirs = {str(Path(f).parent) for f in listed}
    if any(path.startswith(d + "/") for d in test_dirs):
        if re.match(r"test_.*\.py\Z|.*_test\.py\Z", name) and str(p.parent) in test_dirs:
            return set(), ""   # another check's test file
        return set(), f"shared test file {path} changed"
    if p.suffix != ".py":   # its file name, else its folder's (a test that globs the folder)
        got = _naming(us, name, False) or (_naming(us, p.parent.name, False) if p.parent.name else set())
        return (got, "") if got else left(set(), f"no test names {path} or its folder")
    got = _naming(us, p.stem, True)
    if not got:
        return left(set(), f"no test imports or names {path}")
    if _weight(us, got) < MAX_SHARE * _weight(us, us):
        return got, ""
    defs = changed_defs(top, base, head, path)
    if defs is None:
        return left(set(), f"module-level code of {path} changed")
    runner = any(RUNNER_DEFS.match(d) for d in defs)
    if runner and soft is None:
        return set(), f"the checks runner changed ({path})"
    got = set()
    for d in defs:
        got |= _naming(us, d, False)
        if d.startswith("cmd_"):
            sub = re.escape(d[4:].replace("_", "-"))
            got |= {k for k, t in us.items() if re.search(rf"[\"']{sub}[\"']", t)}
    if runner:
        return left(got, f"the checks runner changed ({path})")
    return (got, "") if got else left(set(), f"no test names what {path} changes")


def pick(top: Path, base: str, head: str, files: list[str], always=ALWAYS,
         soft: list[str] | None = None, sizes: dict | None = None) -> tuple[list[str], str]:
    """(selection entries, "") for a narrowed check, else ([], why it runs in full). With `soft` (a
    push-queue project) what only the full suite covers is left to the push batch and listed there. A
    narrowed check's size goes in `sizes`: {"tests": the selected tests, "of": the check's tests} (_weight)."""
    if not base:
        return [], "no push target to diff against"
    diff = _git(top, "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}")
    if diff is None:
        return [], "the diff from the push target could not be read"
    paths = [x for x in diff.split("\0") if x]
    if not paths:
        return [], "no changed files"
    us = units(top, files)
    cases = {k for k in us if k.endswith("_cases.py")}
    chosen: set[str] = set()
    for path in paths:
        got, why = _place(top, base, head, path, us, files, cases, soft)
        if why:
            return [], why
        chosen |= got
    if not chosen and not soft:
        return [], "nothing in the diff maps to a test"
    chosen |= {k for k in us for a in always if k == a or k.startswith(f"{a}::")}
    if not chosen:
        return [], "nothing in the diff maps to a test"
    n, total = _weight(us, chosen), max(1, _weight(us, us))
    if soft is None and (share := n / total) >= MAX_SHARE:   # a push batch runs the whole suite anyway
        return [], f"the diff reaches {share:.0%} of the tests"
    out = sorted(chosen)
    if len("\n".join(out)) > MAX_ENV:
        return [], "the selection is too long"
    if sizes is not None:
        sizes.update(tests=n, of=total)
    return out, ""


def summary(entries: list[str]) -> str:
    cases = sum(e.endswith("_cases.py") for e in entries)
    tests = [e.partition("::")[0] for e in entries if "::" in e]
    rest = len(entries) - cases - len(tests)
    return (f"targeted: {cases} case files, {len(tests)} tests in {len(set(tests))} test files"
            + (f", {rest} whole files" if rest else ""))


def narrow(top: Path, base: str, head: str, todo: list, own: list, cfg: dict | None,
           full: bool = False) -> tuple[list, list[str]]:
    """`todo` with each eligible project pytest check narrowed to its selection, and the scope lines
    ('<check>: targeted: ...' or '<check>: full: <reason>') to print and record. In a push-queue project
    a targeted line also names what it leaves to the push batch's full run ('; the push batch runs the
    rest: <reasons>')."""
    from . import project, push, pushpass
    out, lines = [], []
    own_s = {str(c) for c in own}
    always = tuple(((cfg or {}).get("delivery") or {}).get(ALWAYS_KEY, ALWAYS) or ())
    queued = project.push_queue_on(cfg or {})
    for c in todo:
        files = test_files(str(c), top) if str(c) in own_s and not getattr(c, "env", ()) \
            and not getattr(c, "host", "") else []
        if not files:
            out.append(c)
            continue
        why = "--full" if full else "" if enabled(cfg) else f"delivery.{KEY} is false"
        soft: list[str] | None = [] if queued else None
        sizes: dict = {}
        entries, why = ([], why) if why else pick(top, base, head, files, always, soft, sizes)
        if not entries:
            out.append(c)
            lines.append(f"{c}: full: {why}")
            continue
        c2 = push.Check(str(c))
        if isinstance(c, push.Check):
            c2.__dict__.update(c.__dict__)
        c2.env = tuple(sorted({"PYTHONPATH": f"{pushpass.PLUGIN_DIR}${{PYTHONPATH:+:$PYTHONPATH}}",
                               "PYTEST_ADDOPTS": f"${{PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }}-p {pushpass.SELECT_PLUGIN}",
                               FILES_ENV: "\n".join(entries)}.items()))
        c2.targeted = True   # a part of the suite: never a verified-through marker
        c2.scope = sizes     # its size, and the full check it narrows, price its lane (checklane.cost)
        c2.full = c
        out.append(c2)
        lines.append(f"{c}: {summary(entries)}"
                     + (f"; the push batch runs the rest: {'; '.join(dict.fromkeys(soft))}" if soft else ""))
    return out, lines

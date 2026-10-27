# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The tests a diff adds, rerun several times by `ttp checks`, model-free, so a timing-flaky test
fails when it is added rather than later on the push branch (it passes one run in two, say).

`added` compares the test functions of each changed test file (test_*.py, *_test.py, or any .py
under a directory whose name starts with "test", such as a case file another module loads) at the
checked commit with its merge base with the push target: a new test function, or one whose
parametrize decorators changed (new cases), is added. After the main run passed, `run` reruns only
those, ROUNDS times each, in one more invocation of the first check that is one plain local pytest
run (the ttp_pytest_rerun plugin selects them, so its options and environment apply). A test that
fails any round fails the check, named with its pass count (3/5). More than MAX_TESTS added tests, or
a rerun past MAX_SECONDS, is reported as skipped, never as passed. With no added tests nothing
runs."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_resume"
PLUGIN, ENV, OUT = "ttp_pytest_rerun", "TTP_PYTEST_RERUN", "TTP_PYTEST_RERUN_OUT"
FILE = "added-tests-rerun.jsonl"   # the rerun plugin's record, in the run's directory
ROUNDS = 5          # reruns of each added test
MAX_TESTS = 30      # added test functions rerun at most; more skips the rerun
MAX_SECONDS = 240   # the rerun's wall clock at most (its collection included); past it, skipped
SPREAD = True       # each round starts at another fraction of a wall-clock second (at most 1 s a round)
_SHELL = re.compile(r"[;&|\n`]|\$\(")


def _git(top: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True)


def _test_file(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1] if parts else ""
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")
                                     or any(p.lower().startswith("test") for p in parts[:-1]))


def _params(node) -> list[str]:
    return [s for d in node.decorator_list if "parametrize" in (s := ast.unparse(d))]


def tests_in(src: str) -> dict[str, list[str]]:
    """{qualname: its parametrize decorators} of the test functions in `src` (module level, and in
    module-level Test* classes); {} when it does not parse."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return {}
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, funcs) and node.name.startswith("test"):
            out[node.name] = _params(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for m in node.body:
                if isinstance(m, funcs) and m.name.startswith("test"):
                    out[f"{node.name}.{m.name}"] = _params(m)
    return out


def _show(top: str | Path, rev: str, path: str) -> str:
    r = _git(top, "show", f"{rev}:{path}")
    return r.stdout if r.returncode == 0 else ""


def added(top: str | Path, base: str, head: str) -> list[tuple[str, str]]:
    """[(path, qualname)] of the test functions `head` adds compared with its merge base with
    `base`, or whose parametrize decorators it changed; [] with no base or no merge base."""
    if not base:
        return []
    mb = _git(top, "merge-base", base, head).stdout.strip()
    if not mb:
        return []
    r = _git(top, "diff", "--name-status", "-z", "-M", "--diff-filter=AMR", mb, head, "--", "*.py")
    if r.returncode != 0:
        return []
    words, out, i = r.stdout.split("\0"), [], 0
    while i < len(words) and words[i]:
        status = words[i]
        old, new = (words[i + 1], words[i + 2]) if status.startswith("R") else (words[i + 1], words[i + 1])
        i += 3 if status.startswith("R") else 2
        if not _test_file(new):
            continue
        before = tests_in(_show(top, mb, old)) if not status.startswith("A") else {}
        for name, params in tests_in(_show(top, head, new)).items():
            if name not in before or before[name] != params:
                out.append((new, name))
    return out


def eligible(cmd: str) -> bool:
    """Whether check `cmd` is one plain pytest run and nothing else, so running it again with the
    rerun plugin reruns only the selected tests."""
    from . import pushpass
    return pushpass.resumable(cmd) and not _SHELL.search(cmd)


def env(tests: list[tuple[str, str]], out: Path, env: dict, rounds: int) -> dict:
    """`env` (that of a check run in its "PWD") with the variables that load the rerun plugin for
    `tests`."""
    pp = os.pathsep.join(filter(None, (str(PLUGIN_DIR), env.get("PYTHONPATH", ""))))
    opts = " ".join(filter(None, (env.get("PYTEST_ADDOPTS", ""), f"-p {PLUGIN}")))
    return {**env, "PYTHONPATH": pp, "PYTEST_ADDOPTS": opts,
            ENV: json.dumps({"root": env.get("PWD", ""), "tests": [list(t) for t in tests], "rounds": rounds,
                             "spread": SPREAD}), OUT: str(out)}


def tally(out: Path, tests: list[tuple[str, str]], rounds: int) -> dict:
    """What the rerun's record says: {"selected", "passes" {test: rounds passed}, "ran" {test: rounds
    run}, "failed_ids"}. A round fails when any case of the test failed in it."""
    selected: set[str] = set()
    ran: dict[str, set] = {}
    bad: dict[str, set] = {}
    failed_ids: list[str] = []
    try:
        lines = out.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "selected" in row:
            selected |= set(row["selected"] or [])
            continue
        name, k = row.get("test"), row.get("round")
        ran.setdefault(name, set()).add(k)
        if row.get("failed"):
            bad.setdefault(name, set()).add(k)
            if row.get("id") not in failed_ids:
                failed_ids.append(row.get("id"))
    names = [f"{p}::{n}" for p, n in tests]
    return {"selected": [n for n in names if n in selected],
            "passes": {n: len(ran.get(n, set()) - bad.get(n, set())) for n in names if n in selected},
            "ran": {n: len(ran.get(n, set())) for n in names if n in selected},
            "failed_ids": failed_ids}


def run(tests: list[tuple[str, str]], cmd: str, runner, out: Path, base_env: dict) -> dict:
    """Rerun `tests` `rounds` times through check `cmd`, whose environment is `base_env`.
    `runner(env, timeout)` runs `cmd` with environment `env` and returns its exit code
    (subprocess.TimeoutExpired past `timeout`). The result's "status": "passed"; "flaky" (a test
    failed a round) or "failed" (the rerun failed with no test failing), both failing the check; or
    "skipped" with "why" (a cap, or the check collects none of them): a skip is never a pass."""
    rounds, max_tests, max_seconds = ROUNDS, MAX_TESTS, MAX_SECONDS
    names = [f"{p}::{n}" for p, n in tests]
    res = {"tests": names, "rounds": rounds, "check": cmd}
    if len(tests) > max_tests:
        return {**res, "status": "skipped", "why": f"{len(tests)} added tests, over the cap of {max_tests}"}
    out.unlink(missing_ok=True)
    began, rc, timed_out = time.monotonic(), None, False
    try:
        rc = runner(env(tests, out, base_env, rounds), max_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    t = tally(out, tests, rounds)
    res |= {**t, "seconds": round(time.monotonic() - began, 1), "rc": rc}
    flaky = [n for n in t["selected"] if t["passes"][n] < t["ran"][n]]
    if flaky:
        return {**res, "status": "flaky", "flaky": flaky}
    if timed_out:
        return {**res, "status": "skipped", "why": f"the rerun passed the cap of {max_seconds:g} s"}
    if rc not in (0, 5):
        return {**res, "status": "failed", "why": f"the rerun exited {rc} with no test failing"}
    missing = [n for n in names if n not in t["selected"]]
    if not t["selected"]:
        return {**res, "status": "skipped", "why": f"{cmd!r} collects none of them"}
    short = [n for n in t["selected"] if t["ran"][n] < rounds]
    return {**res, "status": "passed", **({"not_collected": missing} if missing else {}),
            **({"short": short} if short else {})}


def lines(res: dict) -> list[str]:
    """What `ttp checks` says about a rerun result."""
    n, rounds, took = len(res["tests"]), res["rounds"], f" ({res['seconds']} s)" if "seconds" in res else ""
    if res["status"] == "skipped":
        return [f"rerun of {n} added test(s) SKIPPED, not counted as passed: {res['why']}{took}"]
    if res["status"] == "flaky":
        out = [f"added test {t} is flaky: passed {res['passes'][t]}/{res['ran'][t]} reruns" for t in res["flaky"]]
        return out
    if res["status"] == "failed":
        return [f"rerun of {n} added test(s) failed: {res['why']}{took}"]
    out = [f"reran {len(res['selected'])} added test(s) {rounds} times each: all passed{took}"]
    if res.get("not_collected"):
        out.append(f"not rerun, {res['check']!r} does not collect them: {', '.join(res['not_collected'])}")
    if res.get("short"):
        out.append(f"ran fewer than {rounds} rounds (the test skipped itself): {', '.join(res['short'])}")
    return out


def summary(res: dict) -> str:
    """One line for the failure message: the flaky tests and their pass counts."""
    if res.get("flaky"):
        return "added test(s) flaky on rerun: " + ", ".join(
            f"{t} passed {res['passes'][t]}/{res['ran'][t]}" for t in res["flaky"])
    return f"the rerun of the added tests failed: {res.get('why', '')}"

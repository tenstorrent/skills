"""Fail-to-pass test writing and selection.

A model that has seen no arm's diff writes tests from the task's spec: the writer prompt asks
for one test per acceptance criterion. A written test is kept only when it fails on the
pre-change tree and passes on the original change (`select`), so a kept test is a requirement
the change demonstrably moved. The writer's module never enters the repository: selection runs
it on git archives of the base and the head, and the module is stored beside the bench task
under f2p/, pinned by its file's sha256.

`_outcomes` reads per-test outcomes from the pytest terminal summary lines the run prints
("path::name PASSED|FAILED|ERROR"), one parse per line, model-free; the module itself is the
only test path the command runs, so a broken suite elsewhere in the tree cannot collect.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from . import bench
from .project import durable_write

MODULE = "f2p_test_module.py"
TIMEOUT = 900.0

# The writer never sees any arm's diff: only the spec, as the arms themselves saw it.
WRITER_PROMPT = """You are writing fail-to-pass tests for a change that has already landed in this repository.

The task spec (what the change had to do):
{spec}

Write a pytest test module that checks the spec's acceptance criteria on the changed tree:
- each test must FAIL on the tree before the change and PASS after it;
- one test per acceptance criterion where the tree allows it;
- import only what the repository itself imports in its tests at the base commit;
- output nothing but the Python module."""

# What a written test file may be judged on; recorded with the kept tests.
RUBRIC = [["kept", "how many of the written tests fail on the base tree and pass on the original change"]]


def write_tests(task: dict, ask) -> str:
    """The writer model's test module text for `task` (`ask(prompt) -> text`)."""
    return ask(WRITER_PROMPT.format(spec=task["spec"]))


def parse(module_text: str) -> bool:
    """Whether the written module compiles as Python: a module that does not is a fault, never
    a kept test."""
    try:
        compile(module_text, MODULE, "exec")
        return True
    except SyntaxError:
        return False


def select(repo: Path, base: str, head: str, module_text: str, cmd: str = bench.TEST_CMD,
           timeout: float = TIMEOUT) -> dict:
    """Run the written module on the base tree and the changed tree, keeping only the tests that
    fail on the base and pass on the change: {"kept": [ids], "why", "fault"}. A test that fails
    on both trees or passes on both is dropped. `fault` names why the run itself is unusable and
    nothing can be kept: a module that does not parse or collect, a pytest error of its own."""
    if not parse(module_text):
        return {"kept": [], "why": "the written module does not parse as Python", "fault": True}
    with tempfile.TemporaryDirectory(prefix="ttp-f2p-") as tmp:
        on_base, on_head = Path(tmp) / "base", Path(tmp) / "head"
        bench._archive(repo, base, on_base)
        bench._archive(repo, head, on_head)
        for tree in (on_base, on_head):
            durable_write(tree / MODULE, module_text)
        on_base_run, on_head_run = _run(cmd, on_base, timeout), _run(cmd, on_head, timeout)
    fault = _fault(on_base_run, on_head_run)
    if fault:
        return {"kept": [], "why": fault, "fault": True}
    kept = sorted(on_base_run["failed"] & on_head_run["passed"])
    why = ("kept the tests that fail on the base tree and pass on the change" if kept
           else "no written test fails on the base tree and passes on the change")
    return {"kept": kept, "why": why, "fault": False}


def _run(cmd: str, tree: Path, timeout: float) -> dict:
    """One pytest run of the module on `tree`: {"rc", "tail", "passed", "failed"} with the test
    ids read from the run's verbose per-test lines ("path::name OUTCOME", pytest -v). The command's own
    -q is dropped: it would cancel -v (the default bench.TEST_CMD has one), leaving no per-test line."""
    argv = [*[a for a in shlex.split(cmd) if not re.fullmatch(r"-q+|--quiet", a)],
            MODULE, "-v", "--tb=no", "-p", "no:cacheprovider"]
    with tempfile.TemporaryFile(mode="w+", errors="replace") as sink:
        try:
            proc = subprocess.run(argv, cwd=tree, stdin=subprocess.DEVNULL, stdout=sink,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            return {"rc": "timeout", "tail": [], "passed": set(), "failed": set()}
        sink.seek(0)
        out = sink.read()
    passed, failed = _outcomes(out)
    return {"rc": rc, "tail": out.splitlines()[-20:], "passed": passed, "failed": failed}


LINE = re.compile(r"^(\S+::\w+(?:\[[^\]]*\])?) (PASSED|FAILED|ERROR|SKIPPED)\s")


def _outcomes(text: str) -> tuple[set[str], set[str]]:
    """(passed ids, failed ids) from pytest -q verbose lines "path::name OUTCOME"."""
    passed, failed = set(), set()
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        (failed if m.group(2) in ("FAILED", "ERROR") else passed).add(m.group(1))
    return passed, failed


def _fault(on_base: dict, on_head: dict) -> str:
    """Why the run is unusable for selection ("" when it is usable): pytest's own error on either
    tree, or the module collecting nothing on the head."""
    for where, run in (("base", on_base), ("head", on_head)):
        if isinstance(run["rc"], str):   # "timeout": no outcome can be read
            return f"the run on the {where} tree hit the {TIMEOUT:.0f} s timeout"
        if run["rc"] in (2, 3, 4, 5):
            return f"pytest exited {run['rc']} on the {where} tree"
    if not on_head["passed"] and not on_head["failed"]:
        return "the written module collected no test on the head tree"
    return ""

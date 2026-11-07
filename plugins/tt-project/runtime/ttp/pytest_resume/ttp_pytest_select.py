# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin the push queue loads (`-p ttp_pytest_select`) to run only a check's prompt size cap
tests (a cap probe, pushpass.select_env), or only a tests gate's failing ids (gatejudge.select_env).

TTP_PYTEST_SELECT is a regex (delivery.push_cap_tests): this run keeps only the tests whose name, the
part of the node id after its last `::`, it matches (re.search, as conflictfix.cap_failure reads a
failing id) and deselects the rest. TTP_PYTEST_SELECT_IDS, one full node id per line, keeps only the
tests with one of those ids (as pytest's node id or relative to the current directory, as its summary
prints them; set but empty: none), so a same-named test in another file never runs.
TTP_PYTEST_SELECT_FILES, one entry per line (testpick, a task's targeted `ttp checks`), keeps only the
tests matching an entry: a path keeps the tests defined in that file (a case file an aggregator test
file executes, or a test file), `path::name` the test function `name` of test file `path`. The filters
combine (a test must pass each one set). A run that keeps none exits 5, pytest's "no tests" status.
It acts only in the pytest run it was loaded into: it takes its TTP_PYTEST_SELECT* variables out of
the environment, so a pytest a test starts runs all its own tests; xdist workers get them from their
controller."""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

ENV = "TTP_PYTEST_SELECT"
IDS_ENV = "TTP_PYTEST_SELECT_IDS"
FILES_ENV = "TTP_PYTEST_SELECT_FILES"
NAME = "ttp_pytest_select"


def _strip_env() -> dict:
    """The TTP_PYTEST_SELECT* variables, taken out of the environment children inherit. The plugin's load
    (PYTEST_ADDOPTS) stays: an xdist worker loads it from there, and a load with no variables keeps all."""
    return {k: os.environ.pop(k) for k in [k for k in os.environ if k.startswith(ENV)]}


def pytest_configure(config):
    given = getattr(config, "workerinput", None)
    config._ttp_select_env = dict(given.get(NAME) or {}) if given is not None else _strip_env()


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput[NAME] = node.config._ttp_select_env


def pytest_collection_modifyitems(config, items):
    env = getattr(config, "_ttp_select_env", {})
    pattern, ids, files = env.get(ENV), env.get(IDS_ENV), env.get(FILES_ENV)
    if not pattern and ids is None and files is None:
        return
    paths, tests = set(), set()
    for e in (files or "").splitlines():
        if e.strip():
            p, _, name = e.strip().partition("::")
            (tests.add((p, name)) if name else paths.add(p))
    rx = re.compile(pattern) if pattern else None
    want = {i.strip() for i in (ids or "").splitlines() if i.strip()}

    def keep(item) -> bool:
        if rx is not None and not rx.search(item.nodeid.rpartition("::")[2]):
            return False
        if files is not None and not _in_files(item, paths, tests):
            return False
        return ids is None or item.nodeid in want or _cwd_relative(config, item.nodeid) in want
    gone = [i for i in items if not keep(i)]
    if gone:
        items[:] = [i for i in items if keep(i)]
        config.hook.pytest_deselected(items=gone)


def _ends(path: str, rel: str) -> bool:
    path = path.replace(os.sep, "/")
    return path == rel or path.endswith("/" + rel)


def _in_files(item, paths: set, tests: set) -> bool:
    where = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
    fn = getattr(item, "function", None)
    try:
        code = inspect.unwrap(fn).__code__.co_filename if fn is not None else ""
    except (AttributeError, ValueError):
        code = ""
    name = getattr(item, "originalname", None) or item.name.partition("[")[0]
    return any(_ends(code, p) or _ends(where, p) for p in paths) \
        or any(n == name and _ends(where, p) for p, n in tests)


def _cwd_relative(config, nodeid: str) -> str:
    """`nodeid` relative to the current directory, as pytest's summary prints it. Computed here, not by
    config.cwd_relative_nodeid: pytest 9 takes a node id object there, not the item's string."""
    try:
        root, here = Path(config.rootpath), Path(config.invocation_params.dir)
    except (AttributeError, TypeError):
        return nodeid
    if root == here:
        return nodeid
    path, sep, rest = nodeid.partition("::")
    return Path(os.path.relpath(root / path, here)).as_posix() + sep + rest

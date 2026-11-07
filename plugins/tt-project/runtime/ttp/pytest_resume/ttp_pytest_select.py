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
It acts only in the pytest process it was loaded into and its xdist workers: it takes its load, its
TTP_PYTEST_SELECT* settings and its folder on PYTHONPATH out of the environment, so a pytest a test
starts runs without it. An xdist controller, which runs no test, keeps them: its workers, started after
it loads, load the plugin from the environment they inherit and get the settings from the controller."""
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


def _strip_env() -> None:
    """Take the plugin's load, settings and folder out of the environment children inherit."""
    for k in [k for k in os.environ if k.startswith(ENV)]:
        os.environ.pop(k, None)
    words = os.environ.get("PYTEST_ADDOPTS", "").split()
    kept = [w for i, w in enumerate(words) if not (w == "-p" and words[i + 1:i + 2] == [NAME]
                                                   or w == NAME and words[i - 1:i] == ["-p"] or w == "-p" + NAME)]
    if kept:
        os.environ["PYTEST_ADDOPTS"] = " ".join(kept)
    else:
        os.environ.pop("PYTEST_ADDOPTS", None)
    here = os.path.dirname(os.path.realpath(__file__))
    pp = os.environ.get("PYTHONPATH")
    if pp is not None:
        rest = [e for e in pp.split(os.pathsep) if not e or os.path.realpath(e) != here]
        if any(rest):
            os.environ["PYTHONPATH"] = os.pathsep.join(rest)
        else:
            os.environ.pop("PYTHONPATH", None)


def _controller(config) -> bool:
    """This process is an xdist controller that starts workers (they inherit its environment)."""
    o = getattr(config, "option", None)
    return not hasattr(config, "workerinput") and getattr(o, "dist", "no") not in ("no", None) \
        and bool(getattr(o, "tx", None)) and not getattr(o, "collectonly", False)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    given = getattr(config, "workerinput", None)
    if given is not None and given.get(NAME) is not None:
        config._ttp_select_env = dict(given[NAME])
    else:   # read before the strip
        config._ttp_select_env = {k: v for k, v in os.environ.items() if k.startswith(ENV)}
    if not _controller(config):
        _strip_env()


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    node.workerinput[NAME] = getattr(node.config, "_ttp_select_env", {})


def pytest_collection_modifyitems(config, items):
    env = getattr(config, "_ttp_select_env", None)
    env = {k: v for k, v in os.environ.items() if k.startswith(ENV)} if env is None else env
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

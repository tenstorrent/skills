# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin `ttp checks` loads (`-p ttp_pytest_rerun`) to rerun only the tests a diff adds
(addedtests), several times each, so a timing-flaky test fails when it is added.

TTP_PYTEST_RERUN is a JSON object: "root", the directory the check runs in; "tests", a list of
[path, qualname] (the path relative to "root", the qualname `test_x` or `TestY.test_x`); and
"rounds". Only a pytest started in "root" acts on it, so one a test starts elsewhere (it inherits the
environment) runs as it would without this plugin. This run
keeps only the tests defined there (by the file their code is in, so a case file another module
loads counts as itself), each case once per round, runs them round by round and starts each round at
another fraction of a wall-clock second ("spread", default on), so a test that depends on the
time's fraction meets another one each round. Each finished phase is appended as a JSON line to
TTP_PYTEST_RERUN_OUT: {"test": "path::qualname", "round": k, "id": node id, "failed": bool}; a
"selected" line lists the tests found.
A run that keeps none exits 5, pytest's "no tests" status."""
from __future__ import annotations

import functools
import inspect
import json
import os
import time

import pytest

ENV, OUT = "TTP_PYTEST_RERUN", "TTP_PYTEST_RERUN_OUT"
FIXTURE = "ttp_rerun_round"
_last = {"round": None}


def _want(config) -> tuple[dict, int, bool]:
    return _parse(os.environ.get(ENV) or "{}", str(config.invocation_params.dir))


@functools.lru_cache(maxsize=4)
def _parse(raw: str, root: str) -> tuple[dict, int, bool]:
    try:
        spec = json.loads(raw)
        if not spec.get("root") or os.path.realpath(spec["root"]) != os.path.realpath(root):
            return {}, 1, False
        tests = {(os.path.realpath(os.path.join(root, path)), name): f"{path}::{name}"
                 for path, name in spec.get("tests") or []}
        return tests, max(1, int(spec.get("rounds") or 1)), spec.get("spread", True) is not False
    except (ValueError, TypeError, AttributeError):
        return {}, 1, False


def _key(fn, cls) -> tuple[str, str] | None:
    try:
        code = inspect.unwrap(fn).__code__
    except (AttributeError, ValueError):
        return None
    name = f"{cls.__name__}.{fn.__name__}" if cls is not None else fn.__name__
    return os.path.realpath(code.co_filename), name


def _write(line: dict) -> None:
    if path := os.environ.get(OUT):
        with open(path, "a") as f:
            f.write(json.dumps(line) + "\n")


@pytest.fixture
def ttp_rerun_round(request):
    return request.param


def pytest_generate_tests(metafunc):
    tests, rounds, _ = _want(metafunc.config)
    if _key(metafunc.function, metafunc.cls) in tests:
        metafunc.fixturenames.append(FIXTURE)
        metafunc.parametrize(FIXTURE, range(1, rounds + 1), indirect=True,
                             ids=[f"ttp-rerun-{k}" for k in range(1, rounds + 1)])


def _round(item) -> int | None:
    spec = getattr(item, "callspec", None)
    return spec.params.get(FIXTURE) if spec is not None else None


def pytest_collection_modifyitems(config, items):
    tests = _want(config)[0]
    if not tests:
        return
    keep = [i for i in items if _round(i) is not None]
    gone = [i for i in items if _round(i) is None]
    keep.sort(key=_round)   # round by round (a stable sort keeps each round's order)
    items[:] = keep
    if gone:
        config.hook.pytest_deselected(items=gone)
    _write({"selected": sorted({tests[k] for i in keep if (k := _key(i.function, i.cls)) in tests})})


def pytest_runtest_setup(item):
    k = _round(item)
    if k is None or k == _last["round"]:
        return
    _last["round"] = k
    _, rounds, spread = _want(item.config)
    if not spread:
        return
    phase = (k - 1) / rounds   # round k starts at this fraction of a second
    time.sleep((phase - time.time()) % 1.0)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    k = _round(item)
    if k is None:
        return
    rep = outcome.get_result()
    name = _want(item.config)[0].get(_key(item.function, item.cls))
    if name and (rep.failed or rep.when == "call"):
        _write({"test": name, "round": k, "id": item.nodeid, "failed": bool(rep.failed)})

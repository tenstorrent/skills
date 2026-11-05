# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pytest plugin the push queue loads (`-p ttp_pytest_select`) to run only a check's prompt size cap
tests (a cap probe, pushpass.select_env), or only a tests gate's failing ids (gatejudge.select_env).

TTP_PYTEST_SELECT is a regex (delivery.push_cap_tests): this run keeps only the tests whose name, the
part of the node id after its last `::`, it matches (re.search, as conflictfix.cap_failure reads a
failing id) and deselects the rest. TTP_PYTEST_SELECT_IDS, one full node id per line, keeps only the
tests with one of those ids (as pytest's node id or relative to the current directory, as its summary
prints them; set but empty: none), so a same-named test in another file never runs. A run that keeps
none exits 5, pytest's "no tests" status."""
from __future__ import annotations

import os
import re

ENV = "TTP_PYTEST_SELECT"
IDS_ENV = "TTP_PYTEST_SELECT_IDS"


def pytest_collection_modifyitems(config, items):
    pattern, ids = os.environ.get(ENV), os.environ.get(IDS_ENV)
    if not pattern and ids is None:
        return
    rx = re.compile(pattern) if pattern else None
    want = {i.strip() for i in (ids or "").splitlines() if i.strip()}
    rel = getattr(config, "cwd_relative_nodeid", lambda n: n)

    def keep(item) -> bool:
        if rx is not None and not rx.search(item.nodeid.rpartition("::")[2]):
            return False
        return ids is None or item.nodeid in want or rel(item.nodeid) in want
    gone = [i for i in items if not keep(i)]
    if gone:
        items[:] = [i for i in items if keep(i)]
        config.hook.pytest_deselected(items=gone)

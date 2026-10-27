# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A check's slowest tests, model-free.

A check that is one plain local pytest run (pushpass.resumable) loads the ttp_pytest_durations plugin
(`env`): it writes the TOP slowest tests and their durations to FILE in the check's report folder (a
push batch's check_report_dir, a `ttp checks` run's directory). It changes no test selection. The
push batch log and `ttp checks --result` show the slowest SHOW of them (`lines`), so a slower suite can
be traced to the tests that grew."""
from __future__ import annotations

import json
import os
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent / "pytest_resume"
FILE, PLUGIN, TOP, SHOW = "slowest-tests.json", "ttp_pytest_durations", 30, 5


def env(path: Path, env: dict, live: Path | None = None) -> dict:
    """The variables that load the durations plugin with `path` as its file, on top of `env`'s own;
    `live` also gets each test's start and end as they happen (jsonl)."""
    pp = os.pathsep.join(filter(None, (str(PLUGIN_DIR), env.get("PYTHONPATH", ""))))
    opts = " ".join(filter(None, (env.get("PYTEST_ADDOPTS", ""), f"-p {PLUGIN}")))
    return {"PYTHONPATH": pp, "PYTEST_ADDOPTS": opts, "TTP_PYTEST_DURATIONS": str(path),
            "TTP_PYTEST_DURATIONS_TOP": str(TOP), **({"TTP_PYTEST_DURATIONS_LIVE": str(live)} if live else {})}


def load(path: Path) -> list[tuple[str, float]]:
    """(test id, seconds), slowest first; [] when the file is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text())
        rows = [(str(t["id"]), float(t["s"])) for t in data.get("tests", [])]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return []
    return sorted(rows, key=lambda r: -r[1])


def lines(path: Path, n: int = SHOW) -> list[str]:
    """The `n` slowest as `slowest tests (of FILE):` and one `  12.3s id` line each; [] when none."""
    rows = load(path)[:n]
    if not rows:
        return []
    return [f"slowest {len(rows)} tests ({path}):"] + [f"  {s:6.1f}s {i}" for i, s in rows]

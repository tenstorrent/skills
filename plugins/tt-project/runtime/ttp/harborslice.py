# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Harbor slice: an optional Terminal-Bench 2.0 slice of the model suite (`ttp model-bench suite --harbor N`).

Off by default (N is 0): nothing is installed or run. With N > 0 the suite adds, per model, one
"terminalbench" row: N task names drawn by the date (modelsuite.draw) from DIR/TASKS (one Harbor task name
per line, kept by the user; none: the row is skipped with the reason), each run through COMMAND (or
opts["harbor_cmd"]), scored by the share of trials whose verifier reward is positive. The row is
information only: it never feeds the model's verdict (modelsuite.verdict).

Harbor is installed only into a venv the run owns (<date>-cells/harbor/venv, `python3 -m venv`, then that
venv's pip under cellenv.env: PIP_REQUIRE_VIRTUALENV=1, no user site), never with `pip install --user`
and never outside a venv. A failed install (no package index, blocked outbound access) skips the slice with
its reason: no retry and no other route. Tested with fakes only; its output parsing follows Harbor's
documented trial result.json and is not checked against a real Harbor run.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

TASKS, PACKAGE = "harbor-tasks.txt", "harbor"   # TASKS under modelbench.home()
COMMAND = ("harbor run --dataset terminal-bench@2.0 --agent claude-code --model {model} "
           "--task-name {task} --jobs-dir {jobs}")
INSTALL_TIMEOUT_S, TASK_TIMEOUT_S = 900, 3600


def _run(cmd: list[str], env: dict, timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    return r.returncode, (r.stderr or r.stdout or "")[-500:]


def install(root: Path, runner: Callable = _run) -> tuple[Path | None, str]:
    """(the venv holding PACKAGE, '') or (None, why). Builds root/venv once; a failure is never retried."""
    from . import cellenv
    from .project import durable_write
    venv = root / "venv"
    if (venv / ".installed").exists():
        return venv, ""
    if (root / "install-failed").exists():
        return None, (root / "install-failed").read_text()
    env = cellenv.env(venv, dict(os.environ))
    for cmd in ([sys.executable, "-m", "venv", str(venv)],
                [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet", PACKAGE]):
        rc, out = runner(cmd, env, INSTALL_TIMEOUT_S)
        if rc:
            why = f"harbor install failed (no other route is tried): {out.strip()[-300:]}"
            root.mkdir(parents=True, exist_ok=True)
            durable_write(root / "install-failed", why)
            return None, why
    venv.mkdir(parents=True, exist_ok=True)
    durable_write(venv / ".installed", "")
    return venv, ""


def reward(jobs: Path) -> list[float]:
    """Each trial's verifier reward under `jobs` (trial result.json files)."""
    out = []
    for f in sorted(jobs.rglob("result.json")):
        try:
            v = (json.loads(f.read_text()) or {}).get("verifier_result") or {}
            r = (v.get("rewards") or {}).get("reward", v.get("reward"))
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(r, (int, float)):
            out.append(float(r))
    return out


def row(model: str, date: str, n: int, d: Path, out: Path, opts: dict, runner: Callable = _run) -> dict:
    """The "terminalbench" result of `model`: {"pass_rate", "cells", "wall_s", "skipped"?}."""
    import time
    from . import cellenv, modelsuite
    names = [x.strip() for x in (d / TASKS).read_text().splitlines() if x.strip()] if (d / TASKS).exists() else []
    if not names:
        return {"pass_rate": None, "cells": 0, "skipped": f"no task names in {TASKS}"}
    venv, why = install(out / "harbor", runner)
    if not venv:
        return {"pass_rate": None, "cells": 0, "skipped": why}
    env, t0, rewards, errors = cellenv.env(venv, dict(os.environ)), time.monotonic(), [], 0
    slug = "".join(c if c.isalnum() or c in "._-" else "-" for c in model)
    for task in modelsuite.draw(names, n, date + "/harbor"):
        jobs = out / "harbor" / slug / "".join(c if c.isalnum() or c in "._-" else "-" for c in task)
        shutil.rmtree(jobs, ignore_errors=True)   # a rerun never reads an earlier run's trials
        jobs.mkdir(parents=True)
        cmd = [w.format(model=model, task=task, jobs=jobs) for w in shlex.split(opts.get("harbor_cmd") or COMMAND)]
        rc, _ = runner(cmd, env, float(opts.get("harbor_timeout_s") or TASK_TIMEOUT_S))
        got = reward(jobs)
        errors += bool(rc) and not got
        rewards.append(max(got) if got else 0.0)
    return {"pass_rate": round(sum(r > 0 for r in rewards) / len(rewards), 3), "cells": len(rewards),
            "errors": errors, "wall_s": round(time.monotonic() - t0, 1)}

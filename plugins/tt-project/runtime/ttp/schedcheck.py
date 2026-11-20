# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp doctor --scheduler`: a lifecycle check of the installed runtime's command-watcher scheduler.
Model-free, $0, a few seconds, so a project can run it after an upgrade without the repository's tests.

It makes a scratch project under the tt-project home (registered nowhere, no daemon started, Jev and
chat notices off) whose schedules.json holds one command watcher, and runs four daemon ticks, each in
a fresh process, so only what the database keeps carries over, as across a restart. Each tick sweeps
watcher issues before it applies schedules.json (the first one before the schedule exists), then
applies it, sweeps again and runs the due schedule, as the daemon does. The watcher exits 1 every time:
- a report and an `"error": true` line: one report and one error open, both wake;
- the report alone: the error is repaired (the next run at exit 0 or 1 without it), the report stays;
- the error again: it reopens and wakes, so a recurrence is never hidden;
- nothing: both close.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAME = "lifecycle-check"
SRC, ERR = f"watcher:{NAME}", f"watcher-error:{NAME}"
REPORT, ERROR = "ledger: entry missing", "ledger: cannot read the books"
TICK_TIMEOUT_S = 60


def _line(text: str, **kw) -> str:
    return json.dumps({"text": text, "severity": "normal", **kw}) + "\n"


# (what it checks, what the watcher prints, the last status, {issue title: status}, new wakes)
STEPS = (
    ("a report and an error at exit 1", _line(REPORT) + _line(ERROR, error=True),
     "ok (1 observations, 1 errors)", {REPORT: "open", ERROR: "open"}, 2),
    ("a successful run repairs the error", _line(REPORT), "ok (1 observations)",
     {REPORT: "open", ERROR: "fixed"}, 0),
    ("a recurrence of the error wakes", _line(REPORT) + _line(ERROR, error=True), "ok (1 observations, 1 errors)",
     {REPORT: "open", ERROR: "open"}, 1),
    ("a run that prints nothing closes all", "", "ok (0 observations)", {REPORT: "fixed", ERROR: "fixed"}, 0),
)


def tick(base: str) -> dict:
    """One daemon tick's schedule steps in this process; what it left in the database."""
    from .daemon import Daemon
    d = Daemon(base)
    db = d.p.db
    events = lambda: db.one("SELECT COUNT(*) n FROM events WHERE source IN (?,?)", (SRC, ERR))["n"]
    before = events()
    swept = d.sweep_watcher_issues()   # before the schedule exists, on the first tick
    d.sync_schedules(start=True)
    db.x("UPDATE schedules SET next_run=? WHERE name=?", (time.time() - 1, NAME))
    d.sweep_watcher_issues()
    d.run_schedules()
    row = db.one("SELECT last_status FROM schedules WHERE name=?", (NAME,))
    return {"swept": swept, "status": row["last_status"] if row else None, "woke": events() - before,
            "issues": {r["title"]: r["status"] for r in db.q("SELECT title, status FROM issues WHERE source IN (?,?)",
                                                              (SRC, ERR))}}


def _fresh_tick(base: Path) -> dict:
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from ttp import schedcheck; "
            "print(json.dumps(schedcheck.tick(sys.argv[2])))")
    out = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent.parent), str(base)],
                         capture_output=True, text=True, timeout=TICK_TIMEOUT_S)
    if out.returncode:
        raise RuntimeError(f"tick exited {out.returncode}: {(out.stderr or out.stdout).strip()[-400:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def run(home: Path | None = None) -> list[tuple[str, bool, str]]:
    """(step, passed, what differed) for each step; the scratch project is removed afterwards."""
    from . import project
    from .cli import bootstrap
    from .project import write_json
    home = Path(home or project.HOME_DIR)
    home.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sched-check-", dir=home))
    rows: list[tuple[str, bool, str]] = []
    try:
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
        p = bootstrap(repo, NAME, "Scheduler lifecycle check.", "fake")
        write_json(p.config_path, {**json.loads(p.config_path.read_text()),
                                   "jev": {"enabled": False}, "notify": {"slack": False}})
        out = tmp / "out.txt"
        write_json(p.harness / "schedules.json", [{
            "name": NAME, "kind": "command", "every": "1h",
            "payload": {"command": f"cat {shlex.quote(str(out))}; exit 1"}}])
        for i, (what, printed, status, issues, woke) in enumerate(STEPS):
            out.write_text(printed)
            try:
                got = _fresh_tick(p.base)
            except (RuntimeError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
                rows.append((what, False, str(e)[:400]))
                break
            want = {"status": status, "issues": issues, "woke": woke}
            if i == 0:
                want["swept"] = 0
            diff = [f"{k} {got.get(k)!r}, expected {v!r}" for k, v in want.items() if got.get(k) != v]
            rows.append((what, not diff, "; ".join(diff)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows

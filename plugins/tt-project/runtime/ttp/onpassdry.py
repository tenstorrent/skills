# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dry run of a waiting hand-off's `on_pass_cmd` (waiting.on_pass_dry_run, on by default).

An on_pass_cmd that cannot run at all (a module missing outside the venv, a usage error, a script
that is not there) used to show only once its job finished, and that cost a full wake. When a run
hands off `waiting` with one, the daemon runs it once right away, model-free, before it records the
hand-off (Daemon._prefetch_remote, outside the transaction): from the project root with the
environment its real run gets, TTP_ON_PASS_DRY_RUN=1 set, a scratch $TTP_RESULT and note folder, and
waiting.on_pass_dry_run_timeout_s (30 s). Under that variable `ttp note`, `notify`, `push` and `land`
do nothing, and `ttp lock`, `devq submit|start|clear` and a `detach` that starts a job are refused
(cli.dry_run_stub), so nothing leaves the run and no device or shared machine is touched; the command
itself may exit early on it.

A crash signature (CRASH_RE in the output, or exit 2, 126 or 127) hands the run back: its task is
queued again at once and its next run continues the same agent session with the dry run's output
(Daemon._resumable), once per hand-off. Where that session cannot be continued (or this is already
the handed-back run), the hand-off stands with a warning in its summary and the task's notes. A timeout or
any other exit is no crash: the hand-off is accepted and records 'dry run inconclusive'."""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from . import boundedchild
from .project import clean_line, durable_write

ENV = "TTP_ON_PASS_DRY_RUN"
FILE = "on_pass_dry_run.json"     # in the run's folder: the dry run's outcome, read when its hand-off is recorded
KEY = "on_pass_dry_run_handback"  # in the task's result: the run handed back, its session and the dry run's output
NOTE_KEY = "on_pass_dry_run"      # in the task's result: an inconclusive or warned dry run, one line
DEFAULT_TIMEOUT_S = 30
OUTPUT_CHARS = 2000
CRASH_RCS = (2, 126, 127)
CRASH_RE = re.compile(r"Traceback \(most recent call last\)|ModuleNotFoundError|ImportError|command not found"
                      r"|\b[Uu]sage:")


def enabled(cfg: dict) -> bool:
    return (cfg.get("waiting") or {}).get("on_pass_dry_run", True) is not False


def timeout_s(cfg: dict) -> float:
    try:
        return float((cfg.get("waiting") or {}).get("on_pass_dry_run_timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return float(DEFAULT_TIMEOUT_S)


def classify(rc: int | None, output: str) -> str:
    """'crash', 'ok' (exit 0) or 'inconclusive' (a timeout, or another non-zero exit)."""
    if rc is None:
        return "inconclusive"
    if rc in CRASH_RCS or CRASH_RE.search(output or ""):
        return "crash"
    return "ok" if rc == 0 else "inconclusive"


def _tail(raw: bytes) -> str:
    text = "\n".join(clean_line(x, 300) for x in raw.decode(errors="replace").splitlines()[-30:]).strip()
    return text[-OUTPUT_CHARS:] or "(no output)"


def run(cmd: str, cwd: Path, env: dict, timeout: float) -> dict:
    """Run `cmd` once as its dry run: {rc (None on a timeout), output (its tail), verdict, took_s}."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="ttp-dry-") as scratch:
        env = {**env, ENV: "1", "TTP_RESULT": str(Path(scratch) / "result.json"), "TTP_NOTE_RUN_DIR": scratch}
        got = boundedchild.run(cmd, timeout, shell=True, cwd=str(cwd), env=env, text=False, stderr="stdout",
                               grace=2.0)
        rc, output = got.rc, _tail(got.out[-256 * 1024:])
    verdict = classify(rc, output)
    return {"cmd": cmd, "rc": rc, "output": output, "verdict": verdict, "took_s": round(time.monotonic() - started, 1)}


def save(run_dir: Path, got: dict) -> None:
    durable_write(Path(run_dir) / FILE, json.dumps(got, indent=1))


def load(run_dir: Path, cmd) -> dict | None:
    """The dry run recorded in `run_dir` for this command, else None."""
    try:
        got = json.loads((Path(run_dir) / FILE).read_text())
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) and got.get("cmd") == cmd else None


def said(got: dict) -> str:
    """How its exit reads: 'exit N' or 'timed out after N s'."""
    return f"exit {got['rc']}" if got.get("rc") is not None else f"timed out after {got.get('took_s')} s"


def handback_text(got: dict) -> str:
    return (f"the dry run of your hand-off's on_pass_cmd crashed ({said(got)}), so it would fail once its job "
            f"ends. Fix the command or what it needs (run it with {ENV}=1 to check; it may exit early on "
            f"that), then write the waiting hand-off again. Command: {got['cmd'][:300]}\n"
            f"Its output:\n{got['output']}")


def warning(got: dict) -> str:
    return (f"WARNING: on_pass_cmd dry run crashed ({said(got)}); it will likely fail when its job ends: "
            f"{got['output'][-300:]}")


def inconclusive(got: dict) -> str:
    return f"dry run inconclusive ({said(got)})"

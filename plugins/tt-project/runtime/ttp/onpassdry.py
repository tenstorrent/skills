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
any other exit is no crash: the hand-off is accepted and records 'dry run inconclusive'.

So is an exit 0 that would not record done once its probe passes (verdict NO_RESULT, handed back as a
crash is): no hand-off written to $TTP_RESULT, unless the command deliberately exits early on ENV (it,
or a file it names, mentions ENV) or its retry_when is a `ttp checks --result` wait whose on_pass is
the fallback; or a hand-off there that carries a wait (`retry_after_s`, ...). A command that sets
TTP_RESULT itself is not run at all: the daemon reads only the $TTP_RESULT it sets, and the dry run
would write wherever it points."""
from __future__ import annotations

import json
import re
import shlex
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
NO_RESULT = "no_result"   # verdict: it would exit 0 without recording done, handed back as a crash is
# An assignment to TTP_RESULT other than passing the daemon's own on ("TTP_RESULT=$TTP_RESULT ...").
SETS_RESULT_RE = re.compile(r"(?<![\w$])TTP_RESULT=(?![\"']?\$\{?TTP_RESULT\b)")
SCRIPT_BYTES = 1 << 20    # a file the command names is read for ENV up to this size
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
        wrote = _wrote(Path(scratch) / "result.json")
    verdict = classify(rc, output)
    return {"cmd": cmd, "rc": rc, "output": output, "verdict": verdict, "took_s": round(time.monotonic() - started, 1),
            "wrote": wrote}


def _wrote(path: Path):
    """What the dry run wrote to its $TTP_RESULT: None (nothing), "unreadable", or its status and keys."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return "unreadable"
    return {"status": got.get("status"), "keys": sorted(got)} if isinstance(got, dict) else "unreadable"


def sets_result(cmd: str) -> bool:
    """Whether `cmd` points TTP_RESULT somewhere itself (the daemon never reads that file)."""
    return bool(SETS_RESULT_RE.search(cmd))


def refused(cmd: str, why: str) -> dict:
    """The outcome of a command the dry run does not run: a NO_RESULT for `why`."""
    return {"cmd": cmd, "rc": None, "output": "(not run)", "verdict": NO_RESULT, "why": why, "took_s": 0.0}


def mentions_env(cmd: str, cwd: Path) -> bool:
    """Whether `cmd`, or a file it names (a script, absolute or under `cwd`), mentions ENV: a command
    that exits early on its dry run on purpose."""
    if ENV in cmd:
        return True
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    for word in words:
        path = Path(word.split("=", 1)[-1]).expanduser()
        path = path if path.is_absolute() else Path(cwd) / path
        try:
            if path.is_file() and ENV.encode() in path.open("rb").read(SCRIPT_BYTES):
                return True
        except (OSError, ValueError):
            continue
    return False


def no_result(got: dict, cmd: str, cwd: Path, fallback: bool, wait_keys) -> str:
    """Why an exit-0 dry run (`got`) would not record done once its probe passes, or "". `fallback`:
    its hand-off's on_pass is recorded when the command writes nothing (a `ttp checks --result` wait)."""
    if got.get("rc") != 0 or got.get("verdict") != "ok":
        return ""
    wrote = got.get("wrote")
    if wrote == "unreadable":
        return "it exited 0 but what it wrote to $TTP_RESULT is no JSON object, so the daemon cannot record it"
    if isinstance(wrote, dict):
        waits = [k for k in (*wait_keys, "on_pass") if k in (wrote.get("keys") or [])]
        return (f"the hand-off it wrote to $TTP_RESULT carries {', '.join(waits)}; it is final (done or "
                f"needs_review), so the daemon would refuse it" if waits else "")
    if fallback or mentions_env(cmd, cwd):
        return ""
    return ("it exited 0 without writing a hand-off to $TTP_RESULT, so once its probe passes it would wake a "
            "model run instead of recording done")


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
    """How its exit reads: 'exit N', 'not run' or 'timed out after N s'."""
    if got.get("rc") is None and got.get("verdict") == NO_RESULT:
        return "not run"
    return f"exit {got['rc']}" if got.get("rc") is not None else f"timed out after {got.get('took_s')} s"


def handback_text(got: dict) -> str:
    if got.get("verdict") == NO_RESULT:
        return (f"the dry run of your hand-off's on_pass_cmd failed its contract ({said(got)}): {got.get('why')}. "
                f"On a passed probe it must exit 0 with a done or needs_review hand-off written to the "
                f"$TTP_RESULT the daemon sets. Fix the command (one that exits early on {ENV}=1 on purpose "
                f"checks that variable in the command or the script it names), then write the waiting "
                f"hand-off again. Command: {got['cmd'][:300]}\nIts output:\n{got['output']}")
    return (f"the dry run of your hand-off's on_pass_cmd crashed ({said(got)}), so it would fail once its job "
            f"ends. Fix the command or what it needs (run it with {ENV}=1 to check; it may exit early on "
            f"that), then write the waiting hand-off again. Command: {got['cmd'][:300]}\n"
            f"Its output:\n{got['output']}")


def warning(got: dict) -> str:
    if got.get("verdict") == NO_RESULT:
        return f"WARNING: on_pass_cmd dry run: {got.get('why')} ({said(got)}): {got['output'][-300:]}"
    return (f"WARNING: on_pass_cmd dry run crashed ({said(got)}); it will likely fail when its job ends: "
            f"{got['output'][-300:]}")


def inconclusive(got: dict) -> str:
    return f"dry run inconclusive ({said(got)})"

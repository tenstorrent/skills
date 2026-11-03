# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp checks --settle <run dir> --head <sha>`: the model-free on_pass_cmd of a `ttp checks --result`
wait, so a pass records the final hand-off with no model run and only a failure wakes the task.

It writes to $TTP_RESULT (the daemon sets it) the waiting hand-off's `on_pass` or, without one, a
`done` hand-off from its summary and the fields a final hand-off carries (KEYS), once all of this
holds: the run's checks.rc is 0; its checks.json records a passed candidate run on the commit `--head`
names, still checked out and clean in that worktree; and the run's result.json is the waiting
hand-off on this wait, naming no next_step. Otherwise it writes nothing and exits 1, so the task
wakes with this output (a head mismatch never records anything). Safe to run twice.

The daemon fills it in (Daemon._finish_worker, daemon.checks_settle_fill) for a waiting hand-off on
its own checks that names no on_pass or on_pass_cmd and asks for nothing after the pass."""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path

# The fields a final hand-off carries over from the waiting one (daemon.DEFAULT_ON_PASS_KEYS).
KEYS = ("artifacts", "metrics", "self_check", "resolves", "no_push", "published", "outcome", "tried")
HEAD_RE = re.compile(r"[0-9a-f]{7,40}")


def command(ttp: str, run_dir, head: str) -> str:
    """The on_pass_cmd for checks in `run_dir` that must pass on `head`."""
    return f"{ttp} checks --settle {shlex.quote(str(run_dir))} --head {head}"


def _head_of(top: str) -> str:
    try:
        out = subprocess.run(["git", "-C", top, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", top, "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 and dirty.returncode == 0 and not dirty.stdout.strip() else ""


def settle(run_dir: Path, head: str) -> tuple[int, str, dict | None]:
    """(0, what, hand-off to write) on a pass for `head`, else (1, why, None)."""
    from . import prguard
    from .cli import CHECKS_RC
    head = (head or "").strip().lower()
    if not HEAD_RE.fullmatch(head):
        return 1, f"--head {head!r} is no commit hash (7 to 40 hex characters)", None
    try:
        rc = (run_dir / CHECKS_RC).read_text().strip()
    except OSError:
        return 1, f"no {CHECKS_RC} in {run_dir}: the checks left no exit code", None
    if rc != "0":
        return 1, f"the checks did not pass ({CHECKS_RC}: {rc[:40]}); `ttp checks --result {run_dir}` shows why", None
    try:
        rec = json.loads((run_dir / prguard.CHECKS_FILE).read_text())
    except (OSError, ValueError):
        rec = None
    if not prguard.candidate_record(rec) or rec.get("passed") is not True or not isinstance(rec.get("head"), str):
        return 1, f"{prguard.CHECKS_FILE} in {run_dir} records no passed candidate checks", None
    checked = rec["head"].lower()
    if not checked.startswith(head):
        return 1, f"refused: the checks passed on {checked[:12]}, not on {head[:12]}, the head the hand-off names", None
    top = rec.get("worktree")
    if isinstance(top, str) and top and _head_of(top) != checked:
        return 1, f"refused: {top} no longer has {checked[:12]} checked out clean", None
    try:
        waited = json.loads((run_dir / "result.json").read_text())
    except (OSError, ValueError):
        waited = None
    if not isinstance(waited, dict) or waited.get("status") != "waiting":
        return 1, f"{run_dir}/result.json is no waiting hand-off", None
    try:
        words = shlex.split(str(waited.get("retry_when_orig") or waited.get("retry_when") or ""))
    except ValueError:
        words = []
    if len(words) != 4 or words[1:3] != ["checks", "--result"] \
            or ("$" not in words[3] and Path(words[3]).resolve() != run_dir.resolve()):
        return 1, f"{run_dir}/result.json does not wait on these checks", None
    if waited.get("next_step"):
        return 1, "the hand-off names a next_step: the task wakes to run it", None
    if isinstance(waited.get("on_pass"), dict):
        return 0, f"checks passed on {checked[:12]}: recorded the hand-off's on_pass", waited["on_pass"]
    summary = str(waited.get("summary") or "").strip() or "The checks it waited on passed."
    out = {"status": "done",
           "summary": f"{summary} (Its `ttp checks` passed on {checked[:12]}; recorded model-free by "
                      f"`ttp checks --settle`.)",
           **{k: waited[k] for k in KEYS if k in waited}}
    return 0, f"checks passed on {checked[:12]}: recorded done", out

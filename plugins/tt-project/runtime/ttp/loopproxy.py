# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""CLAUDE_ENV_FILE: in every Claude Bash command, sources an inherited file, then puts the harness bin
(TTP_RUN_BIN) first on PATH again. The CLI sources the file named by CLAUDE_ENV_FILE in every Bash command; that
file is loopproxy.sh beside this module, and env_file() points CLAUDE_ENV_FILE at it and keeps an
inherited one sourced by it."""
from __future__ import annotations

import os
from pathlib import Path

SCRIPT = Path(__file__).with_name("loopproxy.sh")
PREV_ENV = "TTP_PREV_CLAUDE_ENV_FILE"   # an inherited CLAUDE_ENV_FILE, sourced by the script


def env_file(environ: dict) -> dict[str, str]:
    """CLAUDE_ENV_FILE naming loopproxy.sh, plus PREV_ENV naming the CLAUDE_ENV_FILE `environ` already
    had (the script sources it before its PATH line). A daemon started from another project's run inherits
    that project's copy of this script and its PREV_ENV: a copy is never chained (it would source its
    PREV_ENV, itself, forever), only the file it chained to. PREV_ENV is always set, so an inherited one
    never survives."""
    prev = environ.get("CLAUDE_ENV_FILE") or ""
    if _copy(prev):
        prev = environ.get(PREV_ENV) or ""
    return {"CLAUDE_ENV_FILE": str(SCRIPT), PREV_ENV: "" if _copy(prev) else prev}


def _copy(path: str) -> bool:
    """This script or any project's copy of it."""
    return bool(path) and os.path.basename(path) == SCRIPT.name

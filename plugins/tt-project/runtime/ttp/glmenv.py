# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The command layer of a live mini-swe-agent run on the code arm's GLM side (glmguard.py, glmharness.py).

mini-swe-agent has no tool hooks, so its environment class is the guard: `GuardedEnvironment` asks refusal()
before every command the model proposes, and a refused one ends the run as a hand-back to Opus (glmguard.hit
writes the `needs_opus` hand-off; the command never runs and the agent loop is ended). What it checks, with
no model call:

- the side-effect rules of glmguard.shell_refusal (a `ttp` command outside the allowed set, a raw `git push`,
  a GitHub write, an HTTP post, ssh or mail, a state or database write, a write outside the worktree);
- a read of the stored secrets (hook.secret_read, credguard.bash_refusal), as the Claude Code hook does.

`ttp push` stays the one way to publish: it runs the project's checks first and refuses a run that handed its
task back (cli.glm_gate and `_cmd_push`). The module imports minisweagent only inside the mini process; refusal()
needs nothing but the standard library, so the harness's tests run without it."""
from __future__ import annotations

import os

try:   # inside the mini process only
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.exceptions import Submitted
except ImportError:   # tests and the daemon: the refusal logic below needs neither
    LocalEnvironment = object   # type: ignore[assignment,misc]

    class Submitted(Exception):   # type: ignore[no-redef]
        def __init__(self, *messages):
            super().__init__()
            self.messages = messages

from . import glmguard

CLASS_PATH = "ttp.glmenv.GuardedEnvironment"


def refusal(command: str, cwd: str = "", run_dir: str = "", env=None) -> str | None:
    """Why a command of a GLM-arm mini-swe-agent run is refused before it runs, else None."""
    e = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    run_dir = run_dir or str(e.get("TTP_RUN_DIR") or "")
    if why := glmguard.shell_refusal(command, cwd, run_dir, e):
        return why
    try:
        from . import credguard, hook
        commands, _ = hook._split(command)
        segs = hook._all_segments(commands) + hook._segments(commands, strip=False)
        if hook.secret_read(command, segs, cwd) or credguard.bash_refusal(command, segs, cwd=cwd):
            return "a run on the GLM arm may not read stored secrets or credential files"
    except Exception:
        return None   # a checker that cannot run never refuses on its own; the rules above already held
    return None


class GuardedEnvironment(LocalEnvironment):
    """mini-swe-agent's local environment with the GLM arm's side-effect guard in front of each command."""

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None):
        command = str(action.get("command") or "")
        run_dir = os.environ.get("TTP_RUN_DIR", "")
        why = refusal(command, cwd or getattr(getattr(self, "config", None), "cwd", "") or os.getcwd(), run_dir)
        if why:
            glmguard.hit(run_dir, why)   # the hand-back to Opus; the command is never run
            raise Submitted({"role": "exit", "content": f"refused by the guard: {why}",
                             "extra": {"exit_status": "GuardRefused", "submission": ""}})
        return super().execute(action, cwd, timeout=timeout)

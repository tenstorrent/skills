# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The command layer of a live mini-swe-agent run on the code arm's GLM side (glmguard.py, glmharness.py).

mini-swe-agent has no tool hooks, so its environment class is the guard: `GuardedEnvironment` asks refusal()
before every command the model proposes. A refused command never runs: glmguard.refused decides it, so a
redirect returns the reason and the allowed way and the run goes on, and a hand-back to Opus (a harmful attempt,
the run's third refusal) writes the `needs_opus` hand-off and ends the agent loop. What it checks, with no model
call:

- the side-effect rules of glmguard.shell_refusal (a `ttp` command outside the allowed set, a raw `git push`,
  a GitHub write, an HTTP post, ssh or mail, a state or database write, a write outside the worktree);
- a read of the stored secrets (hook.secret_read, credguard.bash_refusal), as the Claude Code hook does; a key
  named only in a quoted heredoc's text (a script's test string) is a redirect, not a hand-back.

An outward post (glmguard.outward_shell) is not run but recorded as a draft for an Opus step, and the run goes on;
on a device task a failed gate command ends the run as a hand-back to Opus with its output (glmguard.gate_stop).

It is also the run's update hook: the coordinator's unread update (hook.take_update, steer.md) follows each
command's output, marked read, so it reaches the model at its next tool call as Claude Code's PostToolUse hook
hands it over (with_update; the run's HOOKED_FILE marker then leaves `ttp note` and `ttp steer` to it).

`ttp push` stays the one way to publish: it runs the project's checks first and refuses a run that handed its
task back (cli.glm_gate and `_cmd_push`). The module imports minisweagent only inside the mini process; refusal()
needs nothing but the standard library, so the harness's tests run without it.

An optional digest (`route.code_sonnet.glm.digest`, off by default) keeps server context small: a command's output
over DIGEST_BYTES reaches the model as its first lines, every failing line and its tail, and the whole output is
saved in the run folder for the agent to open. It is about server capacity (a GLM call carries a long context),
never about hiding a failure: every line that looks like one is kept."""
from __future__ import annotations

import os
import re
from pathlib import Path

try:   # inside the mini process only
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.exceptions import Submitted
except ImportError:   # tests and the daemon: the refusal logic below needs neither
    LocalEnvironment = object   # type: ignore[assignment,misc]

    class Submitted(Exception):   # type: ignore[no-redef]
        def __init__(self, *messages):
            super().__init__()
            self.messages = messages

from . import deviceplan, glmguard, pyaudit

CLASS_PATH = "ttp.glmenv.GuardedEnvironment"
DIGEST_ENV = "TTP_GLM_DIGEST"   # the harness script sets it to 1 when the digest is on
DIGEST_BYTES = 8192   # output at or under this reaches the model whole
DIGEST_HEAD, DIGEST_TAIL, DIGEST_TAIL_NO_HIT = 5, 40, 80   # lines kept besides the failing ones
SAVE_DIR = "tool-output"
# A line that looks like a failure: kept whole, whatever the output's size.
FAILING = re.compile(
    r"(?i)\b(?:fail(?:ed|ure|ures|s)?|errors?|exceptions?|traceback|assert(?:ion)?(?:error)?|panic(?:ked)?|fatal"
    r"|not ok|denied|refused|timed? out|cannot|unable|undefined|segfault|abort(?:ed)?)\b"
    r"|^E\s|^\s*[✗✘]|exit(?:ed)? (?:code|status) [1-9]|returned [1-9]")


def refusal(command: str, cwd: str = "", run_dir: str = "", env=None) -> str | None:
    """Why a command of a GLM-arm mini-swe-agent run is refused before it runs, else None."""
    e = os.environ if env is None else env
    cwd = cwd or os.getcwd()
    run_dir = run_dir or str(e.get("TTP_RUN_DIR") or "")
    why = glmguard.shell_refusal(command, cwd, run_dir, e)
    if why and glmguard.classify(why)[1] == glmguard.HARMFUL:
        return why
    try:
        from . import credguard, hook
        def secret(cmd: str) -> bool:
            commands = hook._split(cmd)[0]
            segs = hook._all_segments(commands) + hook._segments(commands, strip=False)
            return bool(hook.secret_read(cmd, segs, cwd) or credguard.bash_refusal(cmd, segs, cwd=cwd))
        if secret(command):   # before a redirect (`cp <key> /tmp/k`: the scratch write is the lesser fault)
            # named only in a quoted heredoc a file gets (a script's test string): still never run, but a redirect
            return glmguard.SECRET_TEXT_WHY if not secret(glmguard.code_text(command, interp=True)) \
                else glmguard.SECRET_WHY
        if why:
            return why
        commands, _ = hook._split(command)
        from . import aitag, ruleguard
        if why := ruleguard.bash_refusal(command, hook._all_segments(commands), aitag.project_config(), cwd, e):
            return why
    except Exception:
        return why   # a checker that cannot run never refuses on its own; the rules above already held
    return why


def digest(output: str, returncode, save_dir, limit: int = DIGEST_BYTES) -> str:
    """`output` as the model sees it: whole when it is within `limit` bytes (or cannot be saved), else its first
    lines, every failing line (with its line number) and its tail, the whole output saved under `save_dir`."""
    if len(output.encode("utf-8", "replace")) <= limit or not save_dir:
        return output
    lines = output.splitlines()
    try:
        folder = Path(save_dir)
        folder.mkdir(parents=True, exist_ok=True)
        saved = folder / f"{len(list(folder.glob('*.txt'))) + 1:04d}.txt"
        from .project import durable_write
        durable_write(saved, output)
    except OSError:
        return output   # nothing saved, nothing hidden
    bad = {i for i, ln in enumerate(lines) if FAILING.search(ln)}
    tail = DIGEST_TAIL if bad or not returncode else DIGEST_TAIL_NO_HIT
    keep = bad | set(range(min(DIGEST_HEAD, len(lines)))) | set(range(max(0, len(lines) - tail), len(lines)))
    out, last = [], -1
    for i in sorted(keep):
        if i != last + 1:
            out.append(f"[... {i - last - 1} lines omitted ...]")
        out.append(f"{i + 1}: {lines[i]}" if i in bad else lines[i])
        last = i
    if last < len(lines) - 1:
        out.append(f"[... {len(lines) - 1 - last} lines omitted ...]")
    head = (f"[output digest: {len(lines)} lines, {len(output)} bytes; the first lines, all {len(bad)} failing lines "
            f"(prefixed with their line number) and the last lines are shown; the whole output is in {saved}: "
            f"open it with cat or sed -n]")
    shown = "\n".join([head, *out])
    return shown if len(shown) < len(output) else output


def with_update(got, run_dir: str):
    """`got` (a command's result) with the coordinator's unread update after its output, marked read (hook.take_update,
    as the Claude Code hook hands it over); the run marked as hooked (hook.mark_hooked), so `ttp note` and `ttp steer`
    leave updates to this layer and the daemon reads its steer.offset as a hooked run's."""
    if not run_dir or not isinstance(got, dict):
        return got
    from . import hook
    try:
        hook.mark_hooked(Path(run_dir))
        text = hook.take_update(Path(run_dir))
    except OSError:   # unread stays unread: the next command or `ttp note` hands it over
        return got
    return {**got, "output": f"{got.get('output') or ''}\n\n{text}"} if text else got


class GuardedEnvironment(LocalEnvironment):
    """mini-swe-agent's local environment with the GLM arm's side-effect guard in front of each command, and the
    coordinator's unread update after each command's output."""

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None):
        return with_update(self._guarded(action, cwd, timeout=timeout), os.environ.get("TTP_RUN_DIR", ""))

    def _guarded(self, action: dict, cwd: str = "", *, timeout: int | None = None):
        command = str(action.get("command") or "")
        run_dir = os.environ.get("TTP_RUN_DIR", "")
        pyaudit.first(os.environ)   # harness.sh set its own PYTHONPATH: the run-time judge goes first again
        if draft := glmguard.outward_shell(command):   # an outward post: a draft for Opus, the run goes on
            return {"output": glmguard.drafted(run_dir, draft), "returncode": 2, "exception_info": ""}
        here = cwd or getattr(getattr(self, "config", None), "cwd", "") or os.getcwd()
        if (text := deviceplan.hook_refusal(command, here)) and text.endswith(" first"):   # take the plan or check first
            return {"output": f"refused: {text}", "returncode": 2, "exception_info": ""}
        if not text and (why := refusal(command, here, run_dir)):
            text = glmguard.refused(run_dir, why)   # the command is never run
        if text and glmguard.hit_reason(run_dir):   # a hand-back to Opus ends the agent loop
            raise Submitted({"role": "exit", "content": text, "extra": {"exit_status": "GuardRefused", "submission": ""}})
        if text:   # a redirect: the reason and the allowed way, and the run goes on
            return {"output": text, "returncode": 2, "exception_info": ""}
        got = super().execute(action, cwd, timeout=timeout)
        if isinstance(got, dict):
            deviceplan.log_step(run_dir, command, got.get("returncode"), str(got.get("output") or ""))
        if isinstance(got, dict) and (why := glmguard.gate_stop(run_dir, command, got.get("returncode"),
                                                                 str(got.get("output") or ""))):
            raise Submitted({"role": "exit", "content": f"stopped: {why}",   # the hand-back to Opus with its output
                             "extra": {"exit_status": "GateFailed", "submission": ""}})
        if os.environ.get(DIGEST_ENV) == "1" and run_dir and isinstance(got, dict) and isinstance(got.get("output"), str):
            got = {**got, "output": digest(got["output"], got.get("returncode"), Path(run_dir) / SAVE_DIR)}
        return got

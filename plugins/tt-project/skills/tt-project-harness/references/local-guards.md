# Local tool guards

A project's own rules on tool calls go in files listed in project.json `guards.local`, never in edits to
`runtime/` files. `ttp upgrade` merges upstream's `runtime/` and `prompts/` into the harness: a project
edit to `runtime/ttp/hook.py` conflicts with the next upstream change there and leaves the project on an
old release. Files elsewhere in the harness are the project's alone, so an upgrade never touches them.

## Contract

- `ttp config <project> guards.local '["guards/no_wipe.py"]'`: paths from the harness folder (absolute
  and `~` also work), kept outside `runtime/` and `prompts/`. Commit the files in the harness.
- Each file defines `refusal(cmd: str) -> str | None`: asked about every shell command, it returns the
  reason it refuses, else None. It may also define `tool_refusal(tool: str, tool_input: dict) -> str | None`
  for every other tool call (Edit, Write, connector calls).
- Keep them fast and side-effect free: they run on each tool call.

```python
# guards/no_wipe.py
def refusal(cmd):
    return "wiping scratch is not allowed here" if "wipe-scratch" in cmd else None
```

## Device refusals

Device safety is no sandbox. Under `TTP_NO_DEVICE=1` on a host tagged `device` the built-in hook
refuses reset, flash and power tools, device node paths and changes to the `TT_VISIBLE_DEVICES` mask
outside a device `ttp lock` (refused itself on a held or paused device) or `ttp devq`. A project adds
its own device commands here, keyed only on what it holds (`TTP_NO_DEVICE`), never on a machine's
neighbours, serving state or site:

```python
# guards/no_board_tool.py
import os, re

def refusal(cmd):
    if os.environ.get("TTP_NO_DEVICE") == "1" and re.search(r"\bmy-board-tool\b", cmd) \
            and not re.search(r"\bttp (lock \S*-device|devq)\b", cmd):
        return "my-board-tool reaches a device: run it under `ttp lock <device> --`"
    return None
```

## What the harness does

- The worker hook (`hook.pre_tool_use`) asks the listed guards after the built-in guards, as does a
  session coordinator's tool check (read-only and full tools). A refusal blocks the call and is logged
  to the run's refusal log with kind `local:<file name>`.
- A listed guard is never skipped: a file that is missing, does not import or has no `refusal` refuses
  every shell command; one that raises refuses the call it raised on. Each refusal names the file and
  the error. A `guards.local` that is not a list of paths refuses every shell command.
- Only a harness task edits a listed guard file: other runs' Edit and Write calls on it, Codex
  apply_patch calls naming it (also as a move target), and shell commands naming its path, are refused.
- `ttp doctor <project>` shows each guard and whether it loads, and flags one inside `runtime/` or
  `prompts/`.
- The upgrade's conflict message for `runtime/ttp/hook.py` points here.
- A guard that calls `sys.exit` (at import or in a check) counts as one that raises: it refuses, named.
- A shell command names a listed guard only by whole path components: `guards/a.py` matches
  `rm ./guards/a.py`, never `rm guards/data.py`.
- A full-tools session coordinator edits a listed guard file only through a harness task: its own Edit,
  Write and shell calls on it are refused, as a worker's are; it queues a harness task for the change.
- A project.json that does not read and has no last good copy lists no guards, so none is asked. This
  one case does not refuse every call, since that would also stop the runs that could fix it: the daemon
  starts no new run, its config alert says the local guards are not asked, and `ttp doctor` reports it
  as a problem. A broken project.json with a last good copy keeps that copy's guards.

## Providers

Every provider path asks the same guards through `hook.local_guard`:

- Claude Code workers: the PreToolUse hook, on every tool.
- Codex workers: the PreToolUse hook on shell calls, `apply_patch` edits (`tool_refusal` gets
  `("apply_patch", {"command": <patch text>})`) and `mcp__` connector calls; Codex's other tools are not
  hooked.
- Cursor workers: the per-run `.cursor/hooks.json`, with each call in Claude's shape (Shell is `Bash`,
  every file edit is `Write`, `MCP:<tool>` is `mcp__cursor__<tool>`).
- Runs without the hook (Cursor or Codex with hooks off): the runner-side tool guard asks the same check
  about each call it reads from the run's output and stops the run on a refusal. The call has started by
  then, so a quick command may have run.
- Session coordinators, read-only and full tools, Read, Grep and Glob included.

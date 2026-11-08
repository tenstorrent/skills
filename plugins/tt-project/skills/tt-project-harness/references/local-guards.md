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
- Only a harness task edits a listed guard file: other runs' Edit and Write calls on it, and shell
  commands naming its path, are refused.
- `ttp doctor <project>` shows each guard and whether it loads, and flags one inside `runtime/` or
  `prompts/`.
- The upgrade's conflict message for `runtime/ttp/hook.py` points here.

## Providers

The guards run wherever the harness hook runs: Claude Code and Codex workers, and session coordinators.
A provider whose workers run without the hook is covered once the runner-side guard asks
`hook.pre_tool_use` for it; until then a guard does not bind that provider's runs.

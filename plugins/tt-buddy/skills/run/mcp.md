# MCP Mode

- The MCP device backend. Implements `execution.md` § Backend contract.
- Loaded by `workspace-detect.md` when `/dev/tenstorrent/` has ≥1 entry.
- Owns: tool selection, owner rule, env YAML, push-through, queue.

## Tools

Tools on the `tt-device-mcp` MCP server. Names per `tt-buddy:buddy` § Host mapping.

| Use | Tool |
|---|---|
| Queued job (blocking) | `tt_device_job_run` |
| Queued job (background, e.g. server) | `tt_device_job_run_bg` |
| Push-through exec | `tt_device_exec` (60s cap) |
| Device reset | `tt_device_reset` |
| Kill | `tt_device_job_kill` |

- Many agents share the device. The queue serializes access.
- Wait for job completion before proceeding.
- Job stuck >5 min: ask the user before killing.
- Foreign-owned hung job: STOP. Report owner.
- NEVER kill another agent's job.

## Env file

- YAML. Built from `execution.md` § Environment.
- Write it before submit: `$HOME/.tt-buddy/mcp/<run-id>/env.yaml`.
- Pass its path via `env`. NEVER also pass `inherited_env`.
- It replaces the workspace defaults. MUST include:
  - `TT_METAL_HOME`, `PYTHONPATH`
  - `PYTHON_ENV_DIR=$TT_METAL_HOME/python_env`
- NEVER write secrets: tokens, keys, passwords.
- Applies to env files, commands, notes, and logs.
- Job needs a secret: stop and ask the user.
- `<run-id>` = `<ISO8601-no-colons>`, set before submit.
- Record `<run-id>` with the returned `job_id` via `tt-buddy:note`.
- Pre-create `$HOME/.tt-buddy/triage/<run-id>/` before launch.

## Push-through pathway

- `tt_device_exec` bypasses the queue.
- 60s cap. Same owner rule.
- Use when the queue is blocked behind a hung job.

## Failure handling

Mode-agnostic rows: `execution.md` § Failure handling.

| Failure | Action |
|---|---|
| Queue blocked by foreign job | Report owner. STOP. |
| `tt_device_job_kill` fails | Report. Ask the user before escalating. NEVER `kill -9` via Bash; it can force a full reboot. |

## Health gate

- Every install runs a health gate between jobs.
- The gate probes the device and resets until healthy.
- Reset steps depend on platform and privileges.
- The broker kills jobs on timeout or 300s silence.
- Queue held: the gate is recovering. Wait.

## Recovery substitutions

Per-step commands for `recovery.md`:

| Step | Command |
|---|---|
| 1. Triage | Automatic via `execution.md` § Auto-triage; else `tt-metal/tools/tt-triage.py` via § Push-through pathway |
| 2. Kill | `tt_device_job_kill` with `job_id` |
| 3. Reset | none — the gate recovers |
| 4. Verify | `tt_device_queue_status`; next job dispatches |
| 5. Cache clear | Per `recovery.md` § Step 5 |

## Red Flags

| Thought | Reality |
|---|---|
| "I'll bypass the MCP queue with bash once" | NEVER. The queue is the safety contract. |
| "I'll kill the foreign job to clear the queue" | NEVER. Report the owner and stop. |
| "I'll `tt-smi -r` via bash instead" | NEVER. Reset goes through MCP. |

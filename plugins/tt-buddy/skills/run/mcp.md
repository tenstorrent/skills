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
| Push-through exec | `tt_device_exec` (default 180s, max 600s) |
| Device reset (empty queue only) | `tt_device_reset` |
| Kill | `tt_device_job_kill` |

- Many agents share the device. The queue serializes access.
- Wait for job completion before proceeding.
- Jobs auto-kill at `timeout_sec`: default 600s, hard max 1500s.
- Set it to the run's envelope (`execution.md` § Job-timeout envelope).
- Won't fit 1500s: move host work off the device, or split.
- A `timeout` result's `hint` names the limit that fired.
- Stuck: past expected runtime with flat output, or a wedge signature.
- See `SKILL.md` § Long-run watchdog, `recovery.md` § Detect the wedge early.
- **Wedge-kill rule:** triage at once, then kill your own job.
- No need to ask the user: it is your job.
- Then follow § After a kill. Do not reset by default.
- Foreign-owned hung job: STOP. Report owner.
- NEVER kill another agent's job.
- Reset only with an empty queue: nothing running or queued.
- Check `tt_device_queue_status` right before resetting.

## Job output

- `tt_device_job_run` / `tt_device_job_wait` return `output_tail`.
- It holds only the last `output_lines` lines (default 20, max 1000).
- `tt_device_job_status`: `output` / `error` capped at 50,000 lines.
- Full log: `log_file`, or `tt_device_job_logs` (tail up to 10,000).
- Long run: grep the full log for earlier errors.

## After a kill

- The broker owes one reset plus the full health check, fabric included.
- Owed after any kill, timeout or forced preemption, before the next job.
- Applies when `tt_device_queue_status` has a `startup_verify` field.
- A reset of your own doubles it while others wait.

1. `tt_device_job_status` with the `job_id`: confirm `killed` or `timeout`.
2. `tt_device_queue_status`: a `[broker]` row in `running` or non-empty `device_degraded` is the broker's own reset. Wait for it to clear. Add no reset.
3. Call `tt_device_reset` only when the broker reports the device held or unhealthy, asks for a reset, and runs none of its own. Or on tt-device-mcp 1.0.0 (no `startup_verify` field): it owes no reset after a kill.

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
- Default timeout 180s; raise `timeout_sec` up to 600s.
- Same owner rule.
- Use when the queue is blocked behind a hung job.

## Failure handling

Mode-agnostic rows: `execution.md` § Failure handling.

| Failure | Action |
|---|---|
| Queue blocked by foreign job | Report owner. STOP. |
| `tt_device_job_kill` fails | Report. Ask the user before escalating. NEVER shell-kill a device job (`kill`, `kill -9`, `pkill`, any signal). Only `tt_device_job_kill` or `tt-device-mcp kill`. A shell kill skips the graceful stop and can force a full reboot. |

## Health gate

- Every install runs a health gate between jobs.
- The gate probes the device and resets until healthy.
- Reset steps depend on platform and privileges.
- The broker kills jobs on timeout or 300s silence.
- Queue held: the gate is recovering. Wait.
- After a hang: tt-device-mcp resets the device automatically.
- Confirm: `tt_device_queue_status` shows the queue not held.

## Recovery substitutions

Per-step commands for `recovery.md`:

| Step | Command |
|---|---|
| 1. Triage | Automatic via `execution.md` § Auto-triage; else `tt-metal/tools/tt-triage.py` via § Push-through pathway |
| 2. Kill | `tt_device_job_kill` with `job_id` (shell: `tt-device-mcp kill <job_id>`) |
| 3. Reset | none — the gate recovers. Manual reset only per § After a kill step 3. |
| 4. Verify | `tt_device_queue_status`: no `[broker]` row in `running`, `device_degraded` empty, next job dispatches. Not `tt-smi -s`: it reads the device outside the broker. |
| 5. Cache clear | Per `recovery.md` § Step 5 |

## Red Flags

| Thought | Reality |
|---|---|
| "I'll bypass the MCP queue with bash once" | NEVER. The queue is the safety contract. |
| "I'll kill the foreign job to clear the queue" | NEVER. Report the owner and stop. |
| "I'll `tt-smi -r` via bash instead" | NEVER. Reset goes through MCP. |
| "I'll reset after the kill, to be safe" | No. The broker already resets and runs the full check. See § After a kill. |

# Recovery

- Full device recovery after a hang or wedge.
- Invoked by failure handling, other skills, or the developer.

## Detect the wedge early

- `timeout_sec` is a ceiling, not a hang detector.
- A wedged board prints a fatal marker, then hangs.
- One incident: marker printed 23 min before the timeout fired.
- Supervising a run: watch its live log for these signatures.
- On a match, start recovery now. Don't wait for the exit code.

Fatal-wedge signatures (none recover on their own):
`device is unrecoverable` · `potential hang detected` · `Read 0x… over PCIe` · `the board should be reset` · `Timeout waiting for ARC msg request queue` · fabric `UNHEALTHY`.

- NOT a wedge: `TT_FATAL: Index N ...`. Benign; don't kill on it.
- On a match: § Order from step 1. Triage while the process lives.
- Kill per the backend's § Recovery substitutions. NEVER a shell `kill`.
- tt-device-mcp: kill your own job without asking (`mcp.md` § Tools, wedge-kill rule).

## Order

```
triage → kill → reset (only if the backend needs one) → verify → cache (conditional)
```

- NEVER reorder.
- NEVER skip a step, except where its own condition says so.
- Triage MUST run while the hung process lives.
- Killing first loses inspector RPC and per-core state.
- Per-step commands: active backend mode file § Recovery substitutions.

## Step 1 — Triage

- Run triage per the mode's substitution table.
- Save output under `$HOME/.tt-buddy/triage/<run-id>/`.
- Record the artifact path via `tt-buddy:note`.
- DO NOT proceed until triage output is saved.

## Step 2 — Kill the hung job

| Owner of the hung job | Action |
|---|---|
| Caller | Kill per mode's substitution table. |
| Foreign | STOP. Report owner. NEVER kill another agent's job. |

- NEVER shell-kill a device process: `kill`, `kill -9`, `pkill`, any signal.
- Route through the backend's kill.
- A shell kill skips the graceful stop. It can force a full reboot.

## Step 3 — Reset the device

- Reset per mode's substitution table, only when it calls for one.
- The backend resets automatically after a hang.
- tt-device-mcp: reset only per `mcp.md` § After a kill.
- A second reset delays every user waiting on the device.
- Confirm it in step 4.
- **ALL device processes MUST be dead before any reset.**
- Reset with a live handle can wedge the machine.
- Galaxy / multi-chip: one reset hits all host devices.

## Step 4 — Verify

- Verify per mode's substitution table.
- The backend reports the device healthy, all expected devices present.
- Not healthy or a device missing: report and escalate.
- NEVER reboot, reset or power-cycle anything by hand.
- tt-device-mcp escalates on its own.
- It holds the device until the full health check passes.

## Step 5 — Clear cache (conditional)

- Mode-agnostic.
- Run only if kernel `.hpp` / `.cpp` changed since last clean run.
- Stale binaries cause repeat hangs reset won't fix.

| Cache | Path |
|---|---|
| Global tt-metal cache | `$HOME/.cache/tt-metal-cache/` |
| Workspace cache | `<workspace>/.tt-metal-cache/` |

```
rm -rf -- "$HOME/.cache/tt-metal-cache/" "<workspace>/.tt-metal-cache/"
```

- Unsure if kernel code changed: clear both.

## Caller Contract

- **Input:** `job_id` (else most recent device job), workspace path.
- **Output:** verify-step post-state, plus triage artifact path.
- **Failure:** stop at the failed step. Report it and the error.
- NEVER continue past a failed step with destructive operations.

## Red Flags

| Thought | Reality |
|---|---|
| "I'll `pkill -f pytest` (or `kill`)" | NEVER. Any shell kill, not just `-9`. Use the backend's kill command. |
| "I'll `tt-smi -r` via Bash" | NEVER. Reset goes through the backend. |
| "I'll kill first, triage after" | NEVER. Triage needs the inspector RPC alive. |
| "I'll reset while the process holds the device" | NEVER. Confirm dead in step 2 first. |
| "I'll always reset after a kill" | Not under tt-device-mcp. Its broker resets and runs the full check. See `mcp.md` § After a kill. |
| "I'll always clear the cache" | Only when kernel code changed. |

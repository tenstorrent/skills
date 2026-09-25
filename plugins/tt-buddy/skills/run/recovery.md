# Recovery

- Full device recovery after a hang or wedge.
- Invoked by failure handling, other skills, or the developer.

## Order

```
triage → kill → reset → verify → cache (conditional)
```

- NEVER skip or reorder.
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

- NEVER `kill -9` via Bash. It can force a full reboot.

## Step 3 — Reset the device

- Reset per mode's substitution table.
- The backend resets automatically after a hang.
- Confirm it in step 4.
- **ALL device processes MUST be dead before reset.**
- Reset with a live handle can wedge the machine.
- Galaxy / multi-chip: one reset hits all host devices.

## Step 4 — Verify

- Verify per mode's substitution table.
- All expected devices appear with non-error status.
- Still unhealthy after recovery: escalate. Host reboot may be required.

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
| "I'll `pkill -9 -f pytest`" | NEVER. Use the backend's kill command. |
| "I'll `tt-smi -r` via Bash" | NEVER. Reset goes through the backend. |
| "I'll kill first, triage after" | NEVER. Triage needs the inspector RPC alive. |
| "I'll reset while the process holds the device" | NEVER. Confirm dead in step 2 first. |
| "I'll always clear the cache" | Only when kernel code changed. |

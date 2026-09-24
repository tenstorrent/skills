# Execution

- Owns: routing, env sources, auto-triage, failure handling.
- Backend details live in the **active backend mode file**.
- `workspace-detect.md` selects it. Refer to it abstractly, never by name.
- Device recovery after a hang: `recovery.md`.

## Routing Rule

- **NEVER run device commands directly via Bash.**
- The active backend serializes device access.
- Bypassing it causes conflicts and unreproducible runs.

| Route | When | Via |
|---|---|---|
| Device | Anything needing a TT device | the active backend mode file |
| Bash | Host-only work | build, git, file I/O, pip install, curl health check |

## Backend contract

A device backend is a **mode file**. It MUST provide:

| Section the mode file provides | Owns |
|---|---|
| § Tools | tool per operation: queued run, background, push-through, reset, kill |
| § Env file | env file format, path, and `<run-id>` scheme |
| § Push-through pathway | how to bypass the queue for live inspection |
| § Recovery substitutions | per-step triage / kill / reset / verify commands |
| § Failure handling | backend-specific failure rows |

- New backend: add its mode file with these sections.
- Add a selection branch in `workspace-detect.md`.
- No change here or in `recovery.md`.

## Push-through pathway

- Bypasses the normal queue.
- Use when blocked behind a hung job, e.g. live triage.
- Invocation: active backend mode file § Push-through pathway.

## Environment

Build env content from three sources, in order:

1. **Workspace-detect:** paths, `$USER`, `HF_HOME`.
2. **Learn note** (`~/.tt-buddy/notes/learn-<target-slug>-params.md`):
   - Required and optional env vars, numeric constraints, paths.
   - Authoritative: extracted from source, not recipe tables.
   - Absent: invoke `tt-buddy:learn("<target> env vars and config params")`.
3. **User request:** model name, mesh topology, batch size, etc.

- Validate numeric constraints from the note before emitting.
- Pitfall: size flags that are totals (input + output).
- Pitfall: timeouts must cover first-run compile time.
- Pitfall: concurrency flags must match intended load.
- Path variables (e.g. `TT_CACHE_PATH`): check the path exists.
- Output format: active backend mode file § Env file.

## Auto-triage

- Prefix every device command with the two variables below.
- A hang then runs triage automatically, while the process lives.
- Put them in the command, not the env file.
- Skip when auto-disabled (see below).
- `<run-id>` scheme: active backend mode file § Env file.
- Pre-create `$HOME/.tt-buddy/triage/<run-id>/` before launch.

```bash
TT_METAL_OPERATION_TIMEOUT_SECONDS=30 \
TT_METAL_DISPATCH_TIMEOUT_COMMAND_TO_EXECUTE='"<workspace>/tt-metal/tools/tt-triage.py" --disable-progress --triage-summary-path="$HOME/.tt-buddy/triage/<run-id>/triage_summary.txt" 2>&1 | tee "$HOME/.tt-buddy/triage/<run-id>/triage_output.txt"' \
<command>
```

- Callback stays single-quoted. `std::system()` runs it via `/bin/sh -c`.
- `tt-buddy:run` substitutes `<workspace>`, `<run-id>`, `<command>` before submit.
- `$HOME` expands at trip time.
- NEVER set `TT_TRIAGE_ENABLE_AGGREGATED_CALLSTACKS=1`. It disables per-core callstacks.
- **Auto-disable when:** `TT_METAL_WATCHER` is set.
- **Auto-disable when:** command launches `mpirun` or multiple processes.
- Both false-trip the watchdog.
- **On hang:** report the triage artifact path.
- Record it via `tt-buddy:note`.

## Failure handling

Backend-specific rows: active backend mode file § Failure handling.

| Failure | Action |
|---|---|
| Build | Report error, command, output. NEVER retry. Needs a human. |
| Test | Stream logs. Report exit code, last 50 lines, failure type. |
| Server won't start | Get job status, stream logs, report. |
| Device hang | NEVER kill or reset directly. Load `recovery.md`. |

# Optional telemetry hooks, API v1

The runner loads no telemetry by default. Users select a trusted, enabled plugin with
`--telemetry-plugin PATH` or `TT_BRINGUP_TELEMETRY_ROOT`. Choose the path from the host's
enabled installation inventory. The runner does not verify trust or enabled status; it
never scans plugin caches or installs a dependency.
The extension owns collection, measurement policy, reporting, credentials and endpoints.

An extension contains `telemetry.json`:

```json
{"api_version": 1, "entrypoint": "scripts/telemetry/__init__.py"}
```

The loader resolves the entrypoint (including symlinks) and checks that it is a file
inside the selected root. This check applies only to the entrypoint: imports, including
symlinked relative imports, can load code outside that root. The plugin runs in the
runner's process with the runner's permissions. There is no sandbox or code-integrity
verification. The path-derived module name provides namespacing only.

The entrypoint is loaded as a package so relative imports work, and exports
`create(**context)`. Context consists of
`log_dir`, `repo` (Paths), `args` (parsed runner namespace), `env` (child environment),
`codex_bin` and `hf_model`. The factory may return an object with these methods:

| Callback | When / arguments |
|---|---|
| `start()` | Before app-server startup; background collection may start |
| `bootstrap()` | Startup metadata collection |
| `begin_stage(index)` | Before each selected stage, including a resumed stage |
| `begin_attempt(output_log, kind, thread_id)` | Before initial, remediation or resume work |
| `thread(thread_id)` | Newly allocated attempt's thread ID |
| `instructions()` | Return extra text input for this attempt, or None |
| `end_attempt(status)` | In finally after each goal attempt |
| `check(exit_code, infrastructure_retry)` | After each checker invocation |
| `stage_result(goal_status, check_status)` | The runner's unchanged result |
| `end_stage(status=None)` | After the stage; interrupted cleanup may repeat this call |
| `finish(status)` | Complete, stopped or interrupted run outcome |
| `close()` | Release resources even when finishing fails |

A `safe(method, *args, **kwargs)` dispatcher may implement its own locking and error
handling. The runner also catches `Exception` and `SystemExit` from loading, the
factory and callbacks, prints a warning to stderr, and continues. Callback dispatch
uses the runner's `safe_hook` wrapper.
Invalid instruction return types are ignored. This is exception handling, not process
isolation: a hanging callback can stall the runner, and a plugin can mutate shared
state or terminate the process. Plugins must bound their own I/O and helper execution
and must not recursively run model work.
Dry runs invoke the factory for local previews only; the extension must inspect
`args.dry_run` and avoid networking, helpers and hardware. Remaining callbacks do not
run in dry-run mode. `close()` must tolerate unfinished or already-closed stages.

This API is optional and does not require an endpoint, credentials or a telemetry package
in model-bringup. Evidence text is appended separately to the goal's input; the goal
objective and its acceptance checks stay unchanged.

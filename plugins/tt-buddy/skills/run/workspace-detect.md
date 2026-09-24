# Workspace Detection

- Find where you are before executing anything.
- Something missing: tell the developer. NEVER fix it silently.

## What to determine

1. **Workspace root:** parent directory of the repo checkouts.
   - Walk up from cwd to a directory with a `tt-metal/` child.
   - Or derive from `$TT_METAL_HOME` if set.

2. **Available repos:** git repos under the workspace root.
   - Match each to `<plugin-root>/recipes/<repo>/`.

3. **Readiness:** venv active? tt-metal built? tt-device-mcp available?
   - Report what is missing. Point to the recipe.
   - **Device backend:** `/dev/tenstorrent/` has ≥1 entry → load `mcp.md`.
   - No entry: no local device. Report it.
   - New backends add a branch here. See `execution.md` § Backend contract.

4. **Env values for jobs:** `$USER`, `$HF_HOME`.
   - Whether `$HF_TOKEN` is set: report only. NEVER copy its value.
   - These feed the device job env file. See `execution.md`.

## Output

- Summarize findings and issues.
- Blockers: stop and report.

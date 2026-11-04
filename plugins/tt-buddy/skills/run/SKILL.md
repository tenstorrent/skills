---
name: run
description: "Run, test, and execute commands on Tenstorrent devices — handles workspace detection, recipe loading, MCP routing, and job lifecycle. Use for any device-touching run/test action. Building follows the build recipe directly, not tt-buddy:run."
---

# TT Run

## Purpose

- Execution engine for tt-buddy.
- Detects workspace, loads recipe, composes command, routes it.
- Host commands: Bash. Device commands: tt-device-mcp.
- Invoked directly or by other skills.

## When to Invoke

Invoke `tt-buddy:run` to:

- **Run / test / execute** on a TT device (queued).
- **Push-through exec** on a hung device, bypassing the queue.
- **Recover after a hang**: triage → kill → gate resets → verify → cache.
- **Reset the device**: only with an empty queue. See `mcp.md` § Tools.

- NEVER run device-touching commands via Bash: tests, servers, scripts.
- Host-only commands (e.g. host-stub test suites) run via Bash.
- Bypassing skips § MCP Routing Rule. Runs become unreproducible.
- Building is **not** in scope.
- Builds follow `<plugin-root>/recipes/<repo>/build.md` directly, via Bash.

## Pipeline

```
detect workspace → load recipe → research target → route → execute → report
```

1. **Detect workspace:** load `workspace-detect.md`. Find repo, platform, arch.
2. **Load recipe:** read `<plugin-root>/recipes/<repo>/` files for the action.
   No recipe: use explicit user commands or `tt-buddy:learn`.
3. **Research target:** invoke `tt-buddy:learn("<target> env vars and config params")`.
   Target: model, kernel, op, submodule, or server. See `execution.md`.
4. **Route & execute:** device → tt-device-mcp. Host → Bash. See `execution.md`.
5. **Report:** summarize result. On failure, give actionable context.
6. **Note:** record failures and non-obvious results via `tt-buddy:note`.

## MCP Routing Rule

**Device commands go through tt-device-mcp, not Bash.** Safety invariant.

- Device: `tt_device_job_run`, `tt_device_job_run_bg`, `tt_device_exec`, `tt_device_reset`.
- Host: Bash. Build, git, file I/O, pip, health checks.
- In doubt: needs a TT device → MCP.

## Long-run watchdog

Three layers guard a stalled device job:

1. **Armed:** the auto-triage op-timeout (`execution.md` § Auto-triage).
   Only runs through `tt-buddy:run` carry it.
2. **Bounded:** job timeout = the run's envelope (`execution.md` § Job-timeout envelope).
   It catches host-side hangs the op-timeout misses.
3. **Supervised:** watch progress and wedge signatures (`recovery.md` § Detect the wedge early).

- **Compile is not a wedge.** New log lines and growing caches are progress.
- Cold JIT and trace capture can run for minutes. NEVER kill them.
- Wedge: output flat for minutes past the envelope.
- Envelope comes from history (`tt-buddy:note`, `tt-buddy:learn`), not a guess.
- Past the envelope, output flat: triage while the process lives.
- Then follow `recovery.md`. NEVER kill a foreign job.

## Low contention

- A device job holds the shared device for its whole run.
- Build, data prep, weight conversion, analysis: Bash, outside the job.
- Kernel JIT compile is host work too.
- Cold cache: compiling inside the job can exceed its timeout.
- Tree supports it: warm the cache off-device before submitting.

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Detect workspace, repo, platform, arch | `workspace-detect.md` |
| Workspace setup and activation | `<plugin-root>/recipes/workspace.md` |
| Command routing, env sources, auto-triage contract | `execution.md` |
| Full device recovery (triage → kill → reset if needed → verify → cache) | `recovery.md` |
| Test invocation for detected repo | `<plugin-root>/recipes/<repo>/test.md` |
| Server lifecycle (vllm-tt-plugin only) | `<plugin-root>/recipes/vllm-tt-plugin/server.md` |
| Benchmark (vllm-tt-plugin only) | `<plugin-root>/recipes/vllm-tt-plugin/benchmark.md` |
| Environment variables | `<plugin-root>/recipes/<repo>/env.md` |
| Target env vars, constraints, config params | invoke `tt-buddy:learn("<target> env vars and config params")` |
| Job running long: compile or wedge, when to act | this file § Long-run watchdog |
| Cut device contention: keep compile off the job | this file § Low contention |

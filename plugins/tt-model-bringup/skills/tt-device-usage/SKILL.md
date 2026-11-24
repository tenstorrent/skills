---
name: tt-device-usage
description: Use TT hardware safely during tt-metal model bringup, tests, profiling, serving, reset/recovery, tt-smi health checks, tt-triage hang capture, watcher/profiler separation, and T3K ARC/ERISC/remote-Ethernet recovery. Use whenever a stage touches Tenstorrent devices or must decide whether a hardware failure is recoverable.
---

# TT Device Usage

Before using this workflow, follow the dependency and environment setup in
[the model-bringup entrypoint](../model-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


Use this skill for the hardware-facing parts of model bringup. It covers only non-obvious TT device practices: command serialization, reset/list quirks, recoverable ARC/ERISC failures, hang triage, and when a stage may call itself blocked.

## Basic Rules

- Run TT hardware-facing commands one at a time per chip: `tt-smi`, resets, TTNN import/open-device probes, watcher runs, Tracy/device-profiler runs, tests, benchmarks, and serving jobs. On a multi-chip host, one process per chip with `TT_VISIBLE_DEVICES=<n>` is allowed when the operator assigns chips; set it on host-only test sweeps too, because a parametrized device fixture in an unrelated module opens every chip. Resets and `tt-smi` stay one at a time for the whole host.
- Close devices before starting the next device-facing command. Kill stale processes from the same run before reset or retry. On a shared host, check `fuser /dev/tenstorrent/<n>` before using a chip; a chip held by a process this run did not start is unavailable, and that process is never killed.
- Run every device job under a bounded timeout that interrupts first and kills late: `timeout -s INT --kill-after=120 <limit> <cmd>`, so the job can close its device. A plain `timeout` sends SIGTERM and `-s KILL` closes nothing; both leave the dispatch path wedged, and a multi-chip open on top of a chip left mid-operation has taken whole hosts down. Start the clock only once the job holds its chips: a limit that also counts the wait for a lock kills jobs while they run. Do not SIGTERM a process with outstanding device work yourself. Let the timeout end the job, record it, and stop device work on that chip when a mesh smoke no longer completes.
- After any unclean exit (signal, timeout, `TT_THROW`/`TT_FATAL`), the next job on that board waits for a reset and a multi-chip health check. When the set of chips changes between jobs (one chip, then all), pause a few seconds and check health first. Two failed recoveries stop all device work until the operator looks.
- Launch device jobs detached, with their own lifetime; a job tied to a short-lived remote session dies with it and leaves the device open.
- Give background work an expected end time and a progress signal (a growing log, device activity) and check it against them, rather than only waiting for it to exit. Never `wait` on a server process: it does not exit, and an idle server holds the devices.
- Do not reproduce a known hang on a board nobody can power-cycle remotely. Before any reset or health check, copy the debug dumps it may overwrite.
- Record firmware and driver versions per machine. When results differ between machines, suspect the machine before the model.
- On QB2 the two dies of a p300c card share a reset: resetting one die alone has broken the Ethernet links to the others. Pause the other die's job and reset the card or the board.
- `tt-smi -r` inherits `TT_VISIBLE_DEVICES`: run from a die-scoped shell, a "whole-board" reset touches only that die.
- Importing ttnn or converting tensors to block-float formats on the host opens the devices and takes their locks; do it under the device lock too.
- Host-wall numbers from concurrent one-process-per-chip runs are not same-regime: report device time, or serialize the runs that back a host-wall or TTFT claim, and say which. A chip released or added mid-stage splits the regime: label every measurement with its regime (`serial` or `concurrent` and the chips in use) in its sidecar, and re-measure the selected configuration serially before ranking it against measurements from the other regime.
- A chain driver that serializes device jobs is stopped between jobs with its own mechanism (a stop file or a queue flag it checks before each launch), so the running job finishes and releases the chip; killing the driver or the job mid-dispatch leaves the chip wedged.
- Keep watcher and profiler evidence in separate runs. Do not combine `TT_METAL_WATCHER` with device-profiler or Tracy collection.
- Do not profile live vLLM serving stages. Use serving benchmark JSON and logs instead.
- Before launching a bringup, keep the workspace, multigoal log directory, and `CODEX_HOME` on storage that survives reboot and, when applicable, container recreation. The runner defaults to `<repo>/bringup/artifacts/multigoal-runs/<timestamp>`; any `--log-dir` or `RUN_MULTIGOAL_LOG_DIR` override must also use persistent storage. Do not put resume state in `/tmp`: recovery needs both the manifest's stage/thread mapping and the Codex session state.
- Preserve evidence before cleanup: work logs, README files, benchmark JSON, server logs, compact perf summaries, and exact failing commands. Do not delete `CODEX_HOME`, auth/config, completed stage artifacts, or the repo state.
- When disk runs low, clean only inside the run's own folder (logs of finished servers, profiler/trace/tensor dumps, the run's build leftovers and stopped containers), after keeping the evidence. Never the tt-metal kernel cache, shared weights or HF caches, other runs, CI files, or committed artifacts; if that is not enough, report it.

## Reset And Health Checks

Use bounded commands so a bad device state cannot consume the whole stage:

```bash
timeout 60 tt-smi -ls --local
timeout 180 tt-smi -r
timeout 60 tt-smi -ls --local
```

A reset is an operator action. A bringup agent runs it only when the operator's brief authorizes resets (the brief
says so at startup, see `model-bringup`); otherwise it records the wedged chips, keeps evidence, and reports the reset as the operator's next step.

After reset, check that the expected devices are visible. On a T3K this usually means all eight chips. If reset returns but not all expected devices or Ethernet links come back, run the bounded reset sequence one more time before escalating. A first reset can leave part of the mesh or Ethernet fabric missing.

When device listing looks healthy, prove the mesh can actually open and close before resuming the stage:

```bash
python - <<'PY'
import ttnn
mesh = ttnn.open_mesh_device(ttnn.MeshShape(2, 4), trace_region_size=0)
ttnn.close_mesh_device(mesh)
print("MESH_SMOKE_OK")
PY
```

Run it under the bounded timeout from Basic Rules and adjust `MeshShape` for non-T3K targets. Record the final device list and mesh-smoke result.

## ARC / ERISC / Remote Ethernet Recovery

Treat these as recoverable infrastructure faults until recovery fails:

- `Timeout waiting for Ethernet core service remote IO request`
- `ETH core heartbeat check failed`
- `Unexpected ERISC Response Flags`
- `Read 0xffffffff from ARC scratch`
- ARC lock/readback waits
- `tt-smi -ls --local` hanging or failing
- `tt-smi -r` hanging or failing
- TTNN device open failing before model code runs

Recovery sequence:

1. Stop only risky or stale processes from this run: `EngineCore`, `vllm.entrypoints`, `run_vllm_server`, model benchmark processes, stale `pytest`, stale Tracy, and stale profiler jobs.
2. Do not start more profiler, watcher, serving-adapter profiler, or `ttnn.ReadDeviceProfiler(mesh)` work while hardware is unhealthy.
3. Run the bounded list/reset/list sequence above if the operator's brief authorizes resets; otherwise record the wedged chips and report the reset as the operator's next step.
4. If listing is incomplete after the first successful reset, run the bounded reset sequence once more.
5. If devices are visible, clear stale TT UMD locks only after confirming no live process owns the devices, from this run or any other (`fuser`). Then run the mesh smoke.
6. If reset, listing, or mesh smoke still fails, ask the monitor/operator for a physical Docker-host reboot and reservation re-acquire. If the current agent explicitly owns experiment monitoring or machine recovery, reboot the host directly and repeat list/reset/list plus mesh smoke after reconnecting.
7. Resume the same stage from preserved state. In the multigoal flow, use `--resume-stage <stage_number> --log-dir <preserved_run_directory>` with the same Codex home recorded in the manifest instead of restarting completed earlier stages.
8. Record this as infrastructure recovery, not a model correctness or performance result.

### Profiler runs that wedged a T3K

Seen on T3K during optimization: after a serving profiler or watcher failure, a full in-process 32-layer
serving-adapter profile under device-profiler settings (`TT_METAL_DEVICE_PROFILER=1`,
`TT_METAL_PROFILER_CPP_POST_PROCESS=1`, `TT_METAL_PROFILER_MID_RUN_DUMP=1`, `TT_METAL_PROFILER_TRACE_TRACKING=1`,
`TT_METAL_PROFILER_PROGRAM_SUPPORT_COUNT=5000`) followed by `ttnn.ReadDeviceProfiler(mesh)` produced the signatures
above and left the board undiscoverable (`tt-smi -ls --local` and `tt-smi -r` hung). Do not run that pattern. If
these signatures appear during profiling: stop profiler collection, keep the logs, mark the evidence
`hardware-profiler-limited`, and run the recovery sequence above before calling the stage blocked. A
device-profiler-enabled serving process should not exist (serving stages do not profile); stop leftover
`EngineCore` or server processes from this run and run at most one bounded health check.

Do not mark a model stage blocked because a board briefly became undiscoverable. A model stage may block on hardware only after this recovery path fails or requires operator intervention the agent cannot perform.

## Hangs And tt-triage

Every device job runs under a `timeout` (Basic Rules), so a hung job is left to that timeout; never SIGTERM it yourself. When the hang is reproducible or its evidence is wanted, attach tt-triage before the timeout fires: triage reads the device without ending the job. After the timeout ends the job, run a trivial op on that chip before the next job; if it does not complete, stop device work on the chip and report it for the operator reset. An engine subprocess of the agent's own server that is orphaned by a crash inside trace capture has no timeout and may spin forever in mesh shutdown; after a bounded wait (20 minutes with no log progress) the agent may kill that process, then verifies the chip with a trivial op.

Prefer the LLM-readable report:

```bash
mkdir -p models/autoports/<model>/doc/<stage>/triage
timeout 180 tools/tt-triage.py \
  --llm-output \
  --llm-output-path models/autoports/<model>/doc/<stage>/triage/tt-triage.txt \
  --triage-summary-path models/autoports/<model>/doc/<stage>/triage/triage-summary.txt
```

Useful focused variants:

```bash
timeout 120 tools/tt-triage.py --llm-output --run=dump_callstacks --run=dump_running_operations --run=check_eth_status
timeout 120 tools/tt-triage.py --llm-output --run=dump_watcher_ringbuffer --run=check_arc
```

If dependencies are missing, install the repo-local triage requirements in the active environment:

```bash
python -m pip install -r tools/triage/requirements.txt
```

Read `tools/triage/tt-triage.md` for command details and available scripts. Keep the triage output with the stage evidence. Under `TT_VISIBLE_DEVICES`, pass `--dev=<visible index>`; without it the tool addresses the wrong chip or returns only health checks.

After capturing triage, use `$autofix` for the failure if ordinary log reading does not explain it. `$autofix` will run `$autodebug` when needed. Give it the failing command, console log, `tt-triage.txt`, current stage work log, and relevant source paths. Do not declare a stage blocked for a hang until `$autofix` has tried and failed, unless the remaining blocker is unavailable hardware or operator-only recovery.

## Evidence To Record

For any hardware recovery or hang, record:

- failure signature and exact command that exposed it;
- active processes killed, if any;
- `tt-smi` list/reset/list commands and exit status;
- whether a second reset was needed for missing devices or Ethernet links;
- whether locks were cleared and why that was safe;
- mesh-smoke command and result;
- tt-triage output path for hangs;
- whether `$autofix` was used, and its conclusion;
- resumed stage/thread/log path.

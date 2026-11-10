---
name: tt-device-usage
description: Use TT hardware safely during tt-metal training bring-up, tests, profiling, reset/recovery, tt-smi health checks, tt-triage hang capture, watcher/profiler separation, and ARC/ERISC recovery. Use whenever a phase touches Tenstorrent devices or must decide whether a hardware failure is recoverable.
---

# TT Device Usage

Before using this workflow, follow the dependency and environment setup in
[the train-bringup entrypoint](../train-bringup/SKILL.md#startup).
Resolve its package root from this installed skill's location; do not assume
this plugin is copied into the target tt-metal checkout.


Use this skill for the hardware-facing parts of training bring-up. It covers only non-obvious TT device practices: command serialization, reset/list quirks, recoverable ARC/ERISC failures, hang triage, and when a phase may call itself blocked.

## Basic Rules

- Run TT hardware-facing commands one at a time: `tt-smi`, resets, TTNN import/open-device probes, watcher runs, Tracy/device-profiler runs, tests, training runs and MFU benchmarks.
- Close devices before starting the next device-facing command. Kill stale processes from the same run before reset or retry.
- Keep watcher and profiler evidence in separate runs. Do not combine `TT_METAL_WATCHER` with device-profiler or Tracy collection.
- Never run Tracy on the full model. Profile 1x, 2x and 4x the plan's representative slice (every layer kind in its real proportion, at least two layers; on a mesh at the plan's layout, every chip) with real shapes, for ranking and before/after diffs only, never projected to a full step (see the `optimization-loop` measurement protocol); take MFU on the full model without the profiler.
- Before launching a bringup, keep the workspace, multigoal log directory, and `CODEX_HOME` on storage that survives reboot and, when applicable, container recreation. The runner defaults to `<repo>/bringup/artifacts/multigoal-runs/<timestamp>`; any `--log-dir` or `RUN_MULTIGOAL_LOG_DIR` override must also use persistent storage. Do not put resume state in `/tmp`: recovery needs both the manifest's stage/thread mapping and the Codex session state.
- Preserve evidence before cleanup: work logs, README files, MFU logs, Tracy CSVs, PCC tables, compact perf summaries, and exact failing commands. Do not delete `CODEX_HOME`, auth/config, completed phase artifacts, or the repo state.

## Long-Running Jobs

- Run a device chain (build, tests, measurement, profile) as one script started in the background
  by the agent harness (`run_in_background`), and wait for its completion notification. Never wait
  with a shell loop that polls `pgrep -f <name>`: the loop's own command line contains the name,
  matches itself and never ends; several such loops accumulate per session.
- Never `pkill -f <pattern>` from a command whose text contains the pattern (heredocs included): it
  kills the agent's own shell. Stop a background job through the harness, or `kill` a PID read from
  the script's own pid file. The same self-match makes `pgrep -f <pattern>` report the job as alive
  after it has exited, because the check matches its own command line; confirm with
  `ps -p <pid>` on the specific PID instead.
- **The agent owns every process it starts and stops its own.** Asking the user to run `kill` is a
  defect in the pipeline, not a workaround: a device job that outlives its launcher holds all four
  chips against the next piece of work. Record the PID, stop it through the harness or by that PID,
  and verify with `ps -p`. A long device job also needs a bound that matches what it needs, not a
  blanket timeout, and must not be left detached at hand-back.
- Every device command in a chain runs under `timeout` with a bound derived from its expected
  duration, so a hang ends the chain instead of the session.
- The chain writes its result markers (`rc=`, `passed`, `ms/step`) to the evidence log; the agent
  reads the log once after the notification instead of tailing it.
- Before the chain launches: the build is fresh (binary timestamp after the last edit) and any
  patch has been applied and checked outside the chain. A patch script inside the background
  command once measured the unpatched op; a stale build hung the chip.
- **A library target is not the artefact the run loads.** In tt-train, `cmake --build --target ttml`
  relinks `libttml.a` but **not** the nanobind extension `_ttml.abi3.so` that python imports, so a
  python training or benchmark run after that build silently executes the previous extension. Build
  the target the run actually loads (`_ttml`, or `./build_metal.sh --build-tt-train`) and check its
  timestamp. A four-run cooled A/B once measured the previous iteration's code in both arms and
  reported a 3.5 ms "gain" that was noise between identical binaries.
- **Prove the new path executed, do not infer it from a plausible number.** Put a one-shot
  stderr line behind an environment variable in the new branch (how many tensors it batched, which
  config it chose) and read it in the run's log before believing the measurement.

## Mesh Setup

A multi-chip run opens a mesh, not a device, and the whole box is one job: nothing else may hold
any of its chips while it runs.

- The mesh graph descriptor (MGD) comes from tt-train's `configs/mgd/` (for example
  `bh_2_2_line_line.textproto`, `bh_qb_1_4_ring_ring.textproto`) and is passed through
  `TT_MESH_GRAPH_DESC_PATH`, set before the process creates its first device. Its
  `device_topology` dims must equal the mesh shape the code opens; record the file and its dims
  with every run.
- Open with `ttml.open_device_mesh(ttml.Mesh((R, C), (axis0, axis1)))` and close with
  `ttml.close_device_mesh()`. Name the axes after their use (`dp`, `tp`); `ttml.sync_gradients`
  reduces over the axis named `dp`.
- Before any model code on a shape, run this plugin's `scripts/tt_mesh_smoke.py --shape RxC` as
  its own bounded device job (`timeout 900`). It opens the mesh, all-reduces a distinct integer per
  chip along every axis and checks every chip's result exactly; `--bandwidth-mib 16` also times
  the all-reduce per axis for the plan's collective cost. A shape whose smoke fails is not a
  candidate for the plan; record the failure and its reason rather than working around it.
- A physical topology does not open every logical shape (a QuietBox wired as `2x2` may refuse
  `1x4`). The smoke decides, not the descriptor's name.

## Telemetry And Clock

- Every MFU run carries the ARC telemetry sampler from this plugin,
  `scripts/tt_telemetry_sampler.py` (pyluwen, 20 Hz, one CSV row per chip per sample: AICLK, TDP,
  TDC, VCORE, temperature, throttler mask). It runs from the interpreter that has `pyluwen`
  (usually the tt-smi venv, not the model's), in a child process started before the run and
  stopped after it. `tt-smi` polls once per second or slower, too coarse for a step.
- `scripts/tt_telemetry_summary.py <csv> [mfu log]` reports per chip the settled clock, mean and
  peak TDP, TDC, temperature and throttler masks over the loaded phase, names the critical chip
  (lowest clock or highest current) and gives the MFU at the delivered clock as a diagnostic. The
  result row cites the settled clock and the critical chip.
- On day 1 of a bring-up, `scripts/tt_clock_ladder.py` runs the characterisation (idle, a few-core
  matmul, a third of the grid, the full grid, on every chip at once, at the recipe's fidelity) with
  the sampler attached; its table or figure goes into the plan's hardware-ceiling section. A clock
  that drops when the load reaches the TDP limit means a power cap; its delivered clock is the
  plan's margin and the loop's variants are chosen by cooled wall ms, not cycles.
- Cool-down protocol: one stated idle time before every MFU run, the same for every row. Same-clock
  A/B comparisons run ABAB in one process; numbers from different processes are not comparable.
- Firmware-state commands (TDP, TDC or clock limits; ARC messages such as a TDP-limit set) are
  user-run. Write the exact invocation into the plan or work log with the value to set and the
  restore command, ask once, and do not attempt it under the agent's permissions. Record the limits
  before and after (`tt-smi` shows TDP, TDC, thermal and board limits) and the firmware bundle
  version with every characterisation.

## Repository Hygiene

- Never `git add -u` or `git add -A` in a checkout with submodules: it stages a submodule pointer
  bump (the UMD submodule in the reference bring-up). Stage by path.
- Every tracked evidence file stays under 500 KB (the pre-commit hook refuses larger): gzip CSVs,
  PNG at 70 dpi, compact JSON, per-op summaries instead of raw dumps. Tracy and profiler output
  directories live outside the repository tree; the evidence directory keeps the summary and the
  command that produced it.
- One code change per commit; a review round's P2 document fixes may share one commit.

## Reset And Health Checks

Use bounded commands so a bad device state cannot consume the whole phase:

```bash
timeout 60 tt-smi -ls --local
timeout 180 tt-smi -r
timeout 60 tt-smi -ls --local
```

After reset, check that the target chip is visible. If reset returns but the device is missing, run the bounded reset sequence one more time before escalating.

On a mesh, a reset and the health check cover every chip of the mesh, not only the one that
failed: `tt-smi -r` resets all of them, the list must show every chip of the shape, and the
smoke afterwards is `scripts/tt_mesh_smoke.py --shape RxC` on the run's shape. A mesh with one
missing chip is a missing mesh.

When device listing looks healthy, prove the device can actually open and close before resuming the phase:

```bash
python - <<'PY'
import ttnn
dev = ttnn.open_device(device_id=0)
ttnn.close_device(dev)
print("DEVICE_SMOKE_OK")
PY
```

Record the final device list and smoke result.

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

1. Stop only risky or stale processes from this run: stale training examples, stale `pytest` or gtest runs, stale Tracy, and stale profiler jobs.
2. Do not start more profiler, watcher, or `ttnn.ReadDeviceProfiler` work while hardware is unhealthy.
3. Run the bounded list/reset/list sequence above.
4. If listing is incomplete after the first successful reset, run the bounded reset sequence once more.
5. If devices are visible, clear stale TT UMD locks only after confirming no live process from this run owns the devices. Then run the device smoke.
6. If reset, listing, or device smoke still fails, ask the monitor/operator for a physical Docker-host reboot and reservation re-acquire. If the current agent explicitly owns experiment monitoring or machine recovery, reboot the host directly and repeat list/reset/list plus device smoke after reconnecting.
7. Resume the same phase from preserved state. In the multigoal flow, use `--resume-stage <stage_number> --log-dir <preserved_run_directory>` with the same Codex home recorded in the manifest instead of restarting completed earlier phases.
8. Record this as infrastructure recovery, not a model correctness or performance result.

Do not mark a phase blocked because a board briefly became undiscoverable. A phase may block on hardware only after this recovery path fails or requires operator intervention the agent cannot perform.

## Hangs And tt-triage

If a device job appears hung, collect triage evidence before killing it unless the machine is already unreachable or triage itself hangs.

Prefer the LLM-readable report:

```bash
mkdir -p <model_dir>/doc/<phase>/triage
timeout 180 tools/tt-triage.py \
  --llm-output \
  --llm-output-path <model_dir>/doc/<phase>/triage/tt-triage.txt \
  --triage-summary-path <model_dir>/doc/<phase>/triage/triage-summary.txt
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

Read `tools/triage/tt-triage.md` for command details and available scripts. Keep the triage output with the phase evidence.

A mesh hang is captured per chip: one capture covers every chip of the mesh, and the evidence
names, for each chip, the running operation and its call stack, plus the Ethernet status of every
link between the chips. A collective hangs on the chip that is waiting, not the one that is late;
the late chip is the finding.

After capturing triage, use `$autofix` for the failure if ordinary log reading does not explain it. `$autofix` will run `$autodebug` when needed. Give it the failing command, console log, `tt-triage.txt`, current phase work log, and relevant source paths. Do not declare a phase blocked for a hang until `$autofix` has tried and failed, unless the remaining blocker is unavailable hardware or operator-only recovery.

## Evidence To Record

For any hardware recovery or hang, record:

- failure signature and exact command that exposed it;
- active processes killed, if any;
- `tt-smi` list/reset/list commands and exit status;
- whether a second reset was needed for missing devices or Ethernet links;
- whether locks were cleared and why that was safe;
- device-smoke command and result;
- tt-triage output path for hangs;
- whether `$autofix` was used, and its conclusion;
- resumed phase/thread/log path.

# T3K and Galaxy hardware notes

Part of [optimize](../SKILL.md): detail moved out of the main skill file. Read only the sections in play.

Run watcher and profiler evidence as separate hardware runs for non-vLLM optimization. Do not combine `TT_METAL_WATCHER` with device-profiler collection. Do not run profiler evidence in vLLM serving stages at all. Use `$tt-device-usage` for general TT command serialization, reset/list retries, hang triage, and ARC/ERISC/remote-Ethernet recovery. On T3K, the dangerous pattern seen in Phi-3.5 Mini experiments was: a vLLM/serving profiler failure or watcher failure, followed by a full in-process 32-layer serving-adapter profile under device-profiler env such as `TT_METAL_DEVICE_PROFILER=1`, `TT_METAL_PROFILER_CPP_POST_PROCESS=1`, `TT_METAL_PROFILER_MID_RUN_DUMP=1`, `TT_METAL_PROFILER_TRACE_TRACKING=1`, and `TT_METAL_PROFILER_PROGRAM_SUPPORT_COUNT=5000`, then explicit `ttnn.ReadDeviceProfiler(mesh)` readback. With signatures such as `Timeout waiting for Ethernet core service remote IO request`, `ETH core heartbeat check failed`, `Unexpected ERISC Response Flags`, `Read 0xffffffff from ARC scratch`, or ARC lock/readback waits, this can leave the T3K undiscoverable: `tt-smi -ls --local` hangs and `tt-smi -r` may hang. If this happens, stop profiler collection, preserve the logs, mark the evidence `hardware-profiler-limited`, and run the T3K reset recovery procedure below before declaring the optimization stage blocked.

## T3K Reset Recovery

ARC, ERISC, remote Ethernet, or `tt-smi` discovery/reset failures during optimization are recoverable infrastructure events until the recovery steps below fail. Do not mark the model implementation blocked just because a board is temporarily undiscoverable after watcher, profiler, serving, or reset trouble.

When you see signatures such as `Timeout waiting for Ethernet core service remote IO request`, `ETH core heartbeat check failed`, `Unexpected ERISC Response Flags`, `Read 0xffffffff from ARC scratch`, ARC lock/readback waits, `tt-smi -ls --local` hanging, or a failed `tt-smi` reset:

1. Stop only the risky or stale test/server/profiler processes for this run. Preserve `CODEX_HOME`, repo state, multigoal logs, work logs, README files, benchmark JSON, and reduced profiler outputs. Do not delete authenticated config or successful stage evidence.
2. Do not collect more Tracy, watcher, device-profiler, serving-adapter profiler, or `ttnn.ReadDeviceProfiler(mesh)` evidence while the card is unhealthy.
3. Run a bounded list/reset/list sequence from the host:

```bash
timeout 60 tt-smi -ls --local
timeout 180 tt-smi -r
timeout 60 tt-smi -ls --local
```

4. If reset returns but some expected devices or Ethernet links are missing, run the bounded reset sequence once more. If all expected devices are visible, verify a minimal source-backed mesh open/close before resuming optimization:

```bash
python - <<'PY'
import ttnn
mesh = ttnn.open_mesh_device(ttnn.MeshShape(2, 4), trace_region_size=0)
ttnn.close_mesh_device(mesh)
print("MESH_SMOKE_OK")
PY
```

5. If reset or the mesh smoke fails, ask the monitor/operator for a host reboot and reservation re-acquire. If you have direct experiment-monitor authority, reboot the host, reacquire the same or equivalent T3K reservation if needed, restore the run root or preserved `CODEX_HOME`, and repeat the device list plus mesh smoke check.
6. After recovery, resume the same optimization stage from the preserved run state. In the multigoal bringup flow use `--resume-stage <stage_number>` instead of restarting earlier completed stages. Verify the resumed objective still names the exact target model and expected stage skill.
7. Record the recovery in the stage work log: failure signature, commands run, whether reset or reboot was required, final `tt-smi -ls --local` health, mesh smoke result, and resumed stage/thread. This is infrastructure evidence, not a model correctness or performance result.

Keep large raw Tracy/profiler dumps and generated tensor artifacts out of copied-back artifacts after a recovery. Preserve code/docs/tests plus compact evidence such as `*_perf_report.txt`, `*_perf_report.csv`, reduced summaries, benchmark JSON, logs, and READMEs. Exclude `*.tensorbin`, `*.pt`, `*.refpt`, `ops_perf_results.csv`, `prefill_decode_ops.csv`, `*_decode_ops.csv`, and raw multi-GB Tracy CSVs; they are not worth destabilizing the node or evidence copy unless Mark explicitly asks for a specific raw artifact.

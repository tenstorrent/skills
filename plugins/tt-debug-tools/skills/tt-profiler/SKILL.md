---
name: tt-profiler
description: Measure where device time goes on a Tenstorrent part — per-zone kernel timings from the Device Program Profiler, and host-plus-device traces through Tracy. Use when a kernel or op is slower than expected and you need cycles attributed to named scopes, when you want a profile_log_device.csv read, or when you need to instrument a kernel with DeviceZoneScopedN.
metadata:
  tier: kernel
  upstream:
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/device_program_profiler.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: docs/source/tt-metalium/tools/tracy_profiler.rst
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/tools/profiler/kernel_profiler.hpp
    - repo: tenstorrent/tt-metal
      ref: 058b93d450f21b9b86a6ddf58f6f8f2c7744f8f1
      path: tt_metal/llrt/rtoptions.cpp
    - repo: tenstorrent/tt-metal
      ref: 8b53dfa87b68638895c1f48ad5eef24f62d54808
      path: tools/tracy/perf_counter_multipass.py
    - repo: tenstorrent/tt-metal
      ref: 8b53dfa87b68638895c1f48ad5eef24f62d54808
      path: tools/tracy/process_ops_logs.py
---

# tt-profiler

Scope-based timing on the device, the same shape as Tracy on the host. Zones are
compiled into the kernel; the runtime reads their timestamps off the device and
writes a CSV, and optionally streams the whole thing — host and device — into
Tracy.

Built by default. **Off at runtime**, so the overhead only arrives when you ask
for it.

## When to invoke

- A kernel or op is slower than expected and you want cycles attributed to named
  scopes rather than to the whole launch.
- You have a `profile_log_device.csv` and need it read.
- You want to instrument a kernel with `DeviceZoneScopedN`.
- You want host and device on one timeline.

## Surface

```bash
TT_METAL_DEVICE_PROFILER=1 ./build/programming_examples/profiler/test_full_buffer
```

| Variable | Effect |
|---|---|
| `TT_METAL_DEVICE_PROFILER=1` | The whole feature. Nothing is collected without it. |
| `TT_METAL_DEVICE_PROFILER_DISPATCH` | Also profile the dispatch cores. |
| `TT_METAL_PROFILER_MID_RUN_DUMP` | Dump during the run, for long-lived serving jobs. |

Post-processing knobs — `_SUM`, `_ACCUMULATE`, `_SYNC`,
`_DISABLE_DUMP_TO_FILES`, `_DISABLE_PUSH_TO_TRACY`, `_CPP_POST_PROCESS`,
`_TRACE_TRACKING`, `_PROGRAM_SUPPORT_COUNT` — in `references/capture.md`, along
with the Tracy entry points (`python -m tracy`, `tracy-capture`).

For the workflow question "which op dominates?" the answer is the ranked
`tt-perf-report` on the ops-perf-results CSV that Tracy writes with
`-p -r -v -m pytest`, one folder per run under
`generated/profiler/reports/<timestamp>/`. Recipe, pandas fallback and the
per-RISC bottleneck tags: `references/op-report.md`. For measured FPU, SFPU and
Math utilisation per op (not the estimated `PM FPU UTIL (%)`):
`references/perf-counters.md`. NoC, DRAM and Ethernet bandwidth columns, which
need tt-npe: `references/noc-bandwidth.md`.

Profile a warm forward, not model load: `references/warm-window.md`. Long runs,
traced replay and many-chip meshes, where the buffer overflows:
`references/long-runs.md`.

In-kernel, after `#include <tools/profiler/kernel_profiler.hpp>`:

```c++
void kernel_main() {
    DeviceZoneScopedN("MyCustomZone");
    // measured
}
```

## Force the state

```bash
TT_METAL_DEVICE_PROFILER=1 ./build/programming_examples/profiler/test_full_buffer
```

Its kernel wraps a `nop` loop in `DeviceZoneScopedN("TEST-FULL")`, so the CSV
comes back with a user zone next to the firmware ones.

## Output

`$TT_METAL_HOME/generated/profiler/.logs/profile_log_device.csv` — note
`TT_METAL_HOME`, unlike watcher, DPRINT and Inspector, which follow
`TT_METAL_LOGS_PATH` or the working directory.

Two header lines then one row per zone boundary:

```
ARCH: wormhole_b0, CHIP_FREQ[MHz]: 1000, Max Compute Cores: 64
PCIe slot, core_x, core_y, RISC processor type, timer_id, time[cycles since reset], data, run host ID, trace id, trace id counter, zone name, type, source line, source file, meta data
0,9,9,TRISC_2,27919,64479520316,0,0,,,TEST-FULL,ZONE_START,11,.../full_buffer_compute.cpp,
0,9,9,TRISC_2,27919,64479520541,0,0,,,TEST-FULL,ZONE_END,11,.../full_buffer_compute.cpp,
```

Times are **cycles since reset**, so a duration is the difference between a
matched `ZONE_START` and `ZONE_END`, converted with the `CHIP_FREQ[MHz]` from
line 1. Column meanings and how to pair rows: `references/csv-format.md`.

## Traps

**It cannot share a run with watcher or kernel prints.** All three consume
significant on-chip SRAM and conflict. Upstream is explicit that
`TT_METAL_DPRINT_CORES`, `TT_METAL_WATCHER` and `TT_METAL_DEVICE_PROFILER` must
not be set simultaneously.

**The CSV goes under `TT_METAL_HOME`, not the logs directory.** Every other tool
here follows `TT_METAL_LOGS_PATH`. Looking for it in the wrong place reads as "no
data collected".

**Results are read at device close.** Nothing lands until `CloseDevice`, and past
roughly 1000 kernel runs the buffer needs draining sooner —
`detail::ReadDeviceProfilerResults(device)` after the program of interest.
A run that logs `markers were dropped` produced a partial report: the missing
ops read as zero. Discard it and re-capture (`references/long-runs.md`).

**`DeviceZoneScopedN` costs time.** Timings from heavily annotated code include
the annotation overhead. Instrument selectively, and do not compare an annotated
run against an unannotated one.

**The doc's CSV header is out of date.** It shows `stat value, Run ID, zone name,
zone phase` and phases of `begin`/`end`. The current file has 15 columns and the
phase column is `type`, with `ZONE_START` / `ZONE_END`. Parse the header row
rather than assuming positions.

**Volume.** `test_full_buffer` alone produces over 1.6 million rows. Filter by
zone name and core before reading, and never open it whole in an agent context.

## When the numbers look wrong

| Symptom | Likely cause |
|---|---|
| All device durations are 0 | Watcher or DPRINT set in the same run (first trap above) |
| Only a handful of ops in the report | Buffer overflow; see `references/long-runs.md` |
| Permutes or weight tilizes lead the ranking | The capture includes model load; profile warm (`references/warm-window.md`) |
| `NOC` / `DRAM` / `ETH BW UTIL` blank | tt-npe did not run; blank is not zero (`references/noc-bandwidth.md`) |
| `PM FPU UTIL` ≈ 0 on a heavy op | The op has no perf model; capture counters (`references/perf-counters.md`) |
| Huge `OP TO OP LATENCY`, device log tens of GB | Per-op `ReadDeviceProfiler` flushes on a mesh; drop them, raise `--op-support-count` |
| Numbers vary a lot between runs | Program-cache miss in the measured iteration, or other work on the device |

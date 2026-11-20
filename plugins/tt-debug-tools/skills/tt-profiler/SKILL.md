---
name: tt-profiler
description: Measure where device time goes on a Tenstorrent part — the streaming profiler (default where the checkout has it), per-zone kernel timings from the Device Program Profiler (fallback for older checkouts), and host-plus-device traces through Tracy. Use when a kernel or op is slower than expected and you need cycles attributed to named scopes, when you want a profile_log_device.csv read, when you want your own callback on profiler records, or when you need to instrument a kernel with DeviceZoneScopedN.
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
---

# tt-profiler

Scope-based timing on the device, the same shape as Tracy on the host. Zones are
compiled into the kernel; the runtime reads their timestamps off the device and
hands them to the host.

Two profilers exist. Use the **streaming profiler** when the checkout has it;
fall back to the **device profiler** (`TT_METAL_DEVICE_PROFILER`, `tracy -r`)
on older checkouts. They are mutually exclusive: setting both is a `TT_FATAL`.
Both are **off at runtime** until you ask.

## When to invoke

- A kernel or op is slower than expected and you want cycles attributed to named
  scopes rather than to the whole launch.
- You have a `profile_log_device.csv` and need it read.
- You want to instrument a kernel with `DeviceZoneScopedN`.
- You want host and device on one timeline.
- You want your own code to consume profiler records (a callback).

## Which one this checkout has

The streaming profiler is there if either exists:

```bash
ls tt_metal/api/tt-metalium/experimental/streaming_profiler.hpp \
   tt_metal/tools/profiler/STREAMING_PROFILER.md
```

Neither: skip to "Device profiler" below.

## Streaming profiler (default)

```bash
TT_METAL_STREAMING_PROFILER=1 ./your_program          # capture, decode, no output yet
TT_METAL_STREAMING_PROFILER=1 TT_METAL_STREAMING_PROFILER_TRACY=1 ./your_program  # Tracy GUI
TT_METAL_STREAMING_PROFILER=1 TT_METAL_STREAMING_PROFILER_OPS_CSV=ops.csv pytest <test id>
```

Build with `./build_metal.sh` (Tracy is on by default). For op names and a
headless Tracy capture, see `references/op-report.md`.

What it fixes compared with the device profiler:

- **No device-profiler buffer or drain limit.** Records stream off each core
  through small rings to the host as they happen, so the device profiler's
  fixed per-core buffer, its drain calls and its ~1000-run limit go away. The
  rings are still finite: a full ring stalls the producer (a `PROFILER-STALL`
  zone), and a host callback that falls behind drops records, counted by
  `dropped_bytes()`.
- **No stop at kernel or program boundaries.** Capture runs continuously.
- **Better cross-device sync.** Every record lands on one host-clock timeline,
  synced across chips over Ethernet.

A plain `=1` run writes nothing: output comes from a consumer — the Tracy sink,
the CSV sinks (`references/capture.md`), or your own callback.

**Host API** (`#include <tt-metalium/experimental/streaming_profiler.hpp>`,
namespace `tt::tt_metal::experimental::streaming_profiler`). An agent can write
its own callback to compute exactly the numbers it needs:

```c++
using namespace tt::tt_metal::experimental::streaming_profiler;
Callback zones = RegisterCallback([](const Batch<Zone>& batch) {
    for (const Zone& z : batch.records<Zone>()) { /* z.core(), z.descriptor() ... */ }
}, "my-tool");
// destroying `zones`, or zones.reset(), unregisters it
```

`Batch<Ts...>` also carries `TimestampedData` (with `payload()`) and `Event`
records, plus `dropped_bytes()` (a callback that fell behind) and
`stall_count()`. Callbacks must not block and must not register or unregister
callbacks.

**Device markers**, after `#include "api/debug/kernel_profiler.hpp"` (one header for
both profilers; the streaming example kernel notes DPRINT cannot share the run):

```c++
DeviceZoneScopedN("NAME");                 // a timed scope
DeviceTimestampedData("NAME", uint64_value);  // a value at a point in time
DeviceRecordEvent("NAME");                 // an event; the name is compile-time
```

Docs in the checkout: `tt_metal/tools/profiler/STREAMING_PROFILER.md` and
`STREAMING_PROFILER_DETAILED.md`. As written there: Blackhole only, a
Tracy-enabled build, and idle Ethernet cores for the sync.

`STREAMING_PROFILER.md` is a GUI primer; the options (including
`_SYNC_EVENTS` and `_SYNC_CHECK`) are in `STREAMING_PROFILER_DETAILED.md` section 1.

Seen on device runs, tested on a 32-chip Blackhole Galaxy (as of 2026-11-20,
check current behaviour):

- **On a full model, profile the steady part of the run and check that every op
  is on every chip before trusting per-op numbers.** The ops CSV can miss
  frames with no sign in its rows; the only sign is
  `[streaming profiler] consumer "ops-csv" missed N bytes of frames` in the log.
  On a full model run (about 37k ops on each of 32 chips) it missed 28 of the
  63 GB captured, all on the first clip: 46% of that clip's ops were missing on
  some or all chips. The steady clip after it was complete on all 32 chips
  (12,576 ops); a 10-op run dropped nothing. So profile only the steady part, or
  run it last; check the log for the warning; and check that every
  `GLOBAL CALL COUNT` in your span appears on every chip before you sum. Whether
  the loss always falls early in a run is not tested.
- The first run recompiles every kernel with `-DPROFILE_STREAMING`; time the second.
- The CSVs are written in one go when the mesh closes, and teardown is a slow
  drain, not a deadlock: 474 s for 63 GB of frames, about 13 s per 1,000 ops on
  32 chips, with the ops CSV at 0 bytes until it ended. A job runner's hang
  timeout must allow for it: two jobs stopped about 5 min after PASSED lost all
  output. Let the process exit by itself, then read the CSV.
- Ops-CSV durations are spans that include waits on other chips, and
  `EXECUTION` 1 rows add to their op (`references/csv-format.md`). Summed per-op
  time still matched a device-bound stage's wall time within 0.5%.
- The steady step time was the same with the profiler on and off (2.25 s).
- Too few idle Ethernet cores for the sync is a `TT_FATAL` at bring-up.
- `python -m tracy` sets `TT_METAL_DEVICE_PROFILER=1` by default, a `TT_FATAL`
  beside the streaming switch; `--no-device` should avoid it (not tested).
- The zone CSV is about 1,000 rows per op per chip: on a full model write only
  the ops CSV.

Known gaps as of 2026-11-19 (check their current status):

- tt-metal #64832 — the ops and zone CSVs carry per-chip cycles, not synced times.
- tt-metal #64712 — perf-test timing through the streaming profiler; instrumented
  kernels can overflow the kernel-config buffer.
- tt-metal #61277 — on a non-Tracy build, setting `TT_METAL_STREAMING_PROFILER`
  or `TT_METAL_DEVICE_PROFILER` to any value, even `0`, is a `TT_FATAL`.

## Device profiler (fallback for older checkouts)

Built by default; nothing is collected until `TT_METAL_DEVICE_PROFILER=1`.
Results go to a CSV, and optionally — host and device — into Tracy.

### Surface

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
per-RISC bottleneck tags: `references/op-report.md`.

In-kernel, after `#include <tools/profiler/kernel_profiler.hpp>` (older checkouts;
current ones use `"api/debug/kernel_profiler.hpp"`):

```c++
void kernel_main() {
    DeviceZoneScopedN("MyCustomZone");
    // measured
}
```

### Force the state

```bash
TT_METAL_DEVICE_PROFILER=1 ./build/programming_examples/profiler/test_full_buffer
```

Its kernel wraps a `nop` loop in `DeviceZoneScopedN("TEST-FULL")`, so the CSV
comes back with a user zone next to the firmware ones.

### Output

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

## Traps (device profiler)

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

**`DeviceZoneScopedN` costs time.** Timings from heavily annotated code include
the annotation overhead. Instrument selectively, and do not compare an annotated
run against an unannotated one.

**The doc's CSV header is out of date.** It shows `stat value, Run ID, zone name,
zone phase` and phases of `begin`/`end`. The current file has 15 columns and the
phase column is `type`, with `ZONE_START` / `ZONE_END`. Parse the header row
rather than assuming positions.

**Volume.** `test_full_buffer` alone produces over 1.6 million rows. Filter by
zone name and core before reading, and never open it whole in an agent context.

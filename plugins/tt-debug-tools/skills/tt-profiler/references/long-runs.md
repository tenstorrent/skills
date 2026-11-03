# Long runs and many-chip meshes

## Dropped markers make a partial report

The on-device buffer holds `TT_METAL_PROFILER_PROGRAM_SUPPORT_COUNT` programs
per device (1000 by default). The host drains it between dispatches; outrun the
drain and markers are lost. The run log says `Profiler DRAM buffers were full,
markers were dropped!`, and `process_ops_logs` may then stop with `Device data
missing: Op <N> not present in ...`.

A partial report is not usable: the missing ops read as zero device time. Treat
dropped markers as a failed capture, never as a smaller one.

- **Eager runs** drain between dispatches and rarely overflow, but a long one
  can. Raise `--op-support-count` first. If you must drain by hand, call
  `ttnn.synchronize_device(mesh)` then `ttnn.ReadDeviceProfiler(mesh)` per
  stage or every few hundred ops.
- **Traced replay** cannot be drained mid-trace; the host has no control until
  the replay ends. A trace with more ops per device than the buffer holds will
  drop. Size the buffer to one replay, or profile that section eager.
- **Check completeness.** Search the run log for `markers were dropped` and
  compare the report's op count per device with what you expect. Wall-clock
  timing is a fallback for end-to-end latency, not a stand-in for the per-op
  profile.

## Never flush after every op on a mesh

Each `ReadDeviceProfiler` on a multi-chip mesh is a mesh-wide sync and
read-back, seconds per call. Per-op flushes turn a run into minutes, can wedge
it, inflate `OP TO OP LATENCY` with the flush itself and can grow the device log
into tens of GB.

## A fast, lossless setup on a many-chip mesh

Measured on a 32-chip Blackhole Galaxy:

- **One big buffer, one read.** Set `--op-support-count` to at least the ops per
  device for the whole run, skip every per-op and per-block flush, and read
  once at the end. That run dropped no markers.
- **Keep the count the same from run to run.** It sizes the profiler buffer
  baked into every kernel's build defines, so a new value changes every
  kernel's hash: no JIT cache hits and a full instrumented recompile (about
  14 minutes there). The next run at the same value hit the cache for every
  kernel and skipped the compile.
- **Two costs remain when fully cached.** Loading the instrumented programs
  across the mesh took about 11 minutes, and grows with the count, so do not
  oversize it. Host post-processing (`process_ops_logs`) over about 188k rows
  took about 12 minutes. The forward itself was under a second.
- **Iterate on post-processing offline.** Re-run `tools/tracy/process_ops_logs.py`
  on the existing `generated/profiler/.logs` instead of re-running the device.

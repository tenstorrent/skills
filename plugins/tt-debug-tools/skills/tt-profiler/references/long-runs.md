# Long runs and many-chip meshes

## Dropped markers make a partial report

The DRAM profiler buffer holds `--op-support-count` programs per device. When
the host does not drain it in time, the run log says `Profiler DRAM buffers were
full, markers were dropped!`, and `process_ops_logs` may then stop with `Device
data missing: Op <N> not present in ...`. Missing ops read as zero device time:
treat dropped markers as a failed capture, and search the log for `markers were
dropped` after every run.

## Size the buffer once, read once

Set `--op-support-count` to at least the ops per device for the whole run (for
traced replay, one replay: the host cannot drain mid-trace) and read once at
the end. Never call `ReadDeviceProfiler` after every op on a mesh: each call is
a mesh-wide sync and read-back, inflates `OP TO OP LATENCY` and can grow the
device log to tens of GB.

Keep the count the same from run to run: it is a build define of every kernel,
so a new value recompiles every kernel with no JIT cache hits.

Iterate on post-processing offline: re-run `tools/tracy/process_ops_logs.py` on
the existing `generated/profiler/.logs` instead of re-running the device.

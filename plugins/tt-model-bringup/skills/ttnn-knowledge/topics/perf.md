# Performance work

**Comparing measurements.** Numbers drifted about 1% between sessions and 2–3% between machines; a C++ build
running during a timing run, a variant order inside one process, and a benchmark missing a feature flag each
produced wrong conclusions. Re-measuring the previous head in the same session before attributing a small
change, and pausing builds during timing, avoided them.

**Host time per step.** Host-side work that grows with batch, context or table size showed up only in host time:
a large lookup table read from network storage turned each uncached row into a large random read; a BFP4 tensor
reset fell back to a host fill and re-uploaded the whole cache; wide W-reductions (argmax over a vocab shard)
ran on a single core. Recording host time beside device time found them.

**Profiler.** The device profiler buffer holds a fixed number of programs before its first drain; a batch-32
prefill dropped markers until the op support count was raised (Tracy `--op-support-count`, given before `-m`).
Two kernel zones hashing to the same 16-bit source id abort the export; giving one kernel a distinct source path
worked around it. Per-layer Tracy raw logs reach many GB each; prune them after each report.

**Kernel edits that do nothing.** Some ops bind a newer fork of a dataflow header (for example a `_metal2`
variant) while an older header with the same functions remains in the tree. The JIT cache's kernel names show
which source an op actually compiled.

**Weight streaming bounds.** DRAM-sharded decode matmuls already reached roughly 410–480 GB/s per chip; a
worker-core DRAM prefetcher sustained about 80–88 GB/s in a standalone benchmark, so prefetching behind other ops
could not help. The firmware-level prefetcher route needs firmware 19.12 or newer. A standalone go/no-go
benchmark at model shapes settled it in minutes.

**Short-chunk prefill** was bound by how the weight stream is injected (one NoC, a fixed set of injector cores),
not by compute; decode-style DRAM-sharded pipelines are decode-only (one tile row of activation).

**Machines differ.** One QB2 on older firmware (19.8.1) corrupted single tile rows of KV at batch 32 a few percent
of the time; the same code on newer firmware (19.17) showed none. Heavier NoC and DRAM traffic raised the rate,
so a faster kernel looked like a bug. Running the identical reproducer on a second machine with different
firmware early would have saved most of a day.

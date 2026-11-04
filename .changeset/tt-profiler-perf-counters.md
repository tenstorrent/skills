---
"tt-debug-tools": patch
---

tt-profiler: explain why `PM FPU UTIL (%)` cannot settle compute-bound versus
bandwidth-bound. It is a perf-model estimate that reads ~0 on ops with no model
and high on data-movement ops such as `Slice` and `Permute`. A new
`references/perf-counters.md` covers measured math-engine counters: the
`--profiler-capture-perf-counters` groups, the per-pass group cap and
`--perf-counter-multipass`, the FPU, SFPU and Math columns, and what to do when
they are blank. `capture.md` adds turning off the Tracy web viewer in headless
runs and raising `--op-support-count` before draining the buffer by hand.

Three more references: `warm-window.md` (profile a warm forward, not model load
and weight preparation), `long-runs.md` (dropped markers mean a partial report,
traced replay, no per-op flushes on a mesh, and a fast lossless setup measured
on a 32-chip Galaxy), and `noc-bandwidth.md` (the NoC, DRAM and Ethernet columns
need tt-npe; blank is not zero). `SKILL.md` gains a symptom table.
Checked against tt-metal main 8b53dfa87b6.

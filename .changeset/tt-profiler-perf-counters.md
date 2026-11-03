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
Checked against tt-metal main 8b53dfa87b6.

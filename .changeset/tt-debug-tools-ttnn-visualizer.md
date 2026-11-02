---
"tt-debug-tools": minor
---

Add `tt-ttnn-visualizer`: read TTNN memory reports (`db.sqlite`) and profiler
ops-perf CSVs through TT-NN Visualizer's read-only MCP server — which op crashed
or ran out of L1 and why, L1 footprint (the peak and its residents where the
installed release has `l1_peak`), likely DRAM fallbacks, model-code provenance,
why an op is slow, and noise-aware diffs between captures. The skill follows
the tools `load_report` says are available rather than assuming a release. The
plugin does not register the server; the skill tells the user how to, and falls
back to `tt-perf-report` and the visualizer app without it.

Not verified on a device: the capture recipe and traps come from tt-metal and
ttnn-visualizer source and docs, and the tools were exercised against a
ttnn-visualizer demo report.

`tt-ttnn-flags` now says the report database is written only under pytest, at
test teardown, and points at the new skill for the keys it needs and how to
read it; `tt-profiler` points there too. The eval harness now isolates the
agent from the user's MCP servers and can hand it an explicit one with its
tools pre-approved, with offline tests.

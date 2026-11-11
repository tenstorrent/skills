---
"tt-debug-tools": minor
---

Add `tt-ttnn-visualizer`: read TTNN memory reports (`db.sqlite`) and profiler
ops-perf CSVs through TT-NN Visualizer's read-only MCP server — which op crashed
or ran out of L1 and why, the L1 peak and its residents, likely DRAM
fallbacks, model-code provenance, why an op is slow, and noise-aware diffs
between captures. It needs ttnn-visualizer v0.110.0 or later, which names the
memory report path `memory_report_path` / `--memory-report-path`, and follows
the tools `load_report` says a report can answer. The plugin does not register
the server; the skill tells the user how to, and falls back to `tt-perf-report`
and the visualizer app without it.

Not verified on a device: the capture recipe and traps come from tt-metal and
ttnn-visualizer source and docs, and the tools were exercised against a
ttnn-visualizer demo report.

`tt-ttnn-flags` now explains automatic report database generation at pytest
teardown and points at the new skill for the required keys, manual capture
and import outside pytest, and how to read the report; `tt-profiler` points
there too. On the Claude host the eval
harness now isolates the agent from the user's MCP servers and can hand it an
explicit one with its tools pre-approved, with offline tests.

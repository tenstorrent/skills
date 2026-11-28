---
"tt-debug-tools": patch
---

`tt-profiler` makes the streaming profiler (`TT_METAL_STREAMING_PROFILER=1`) the default where
the checkout has it: how to detect it, its Tracy and CSV sinks, the host callback API and the
device markers, and its known gaps. The device profiler and `tracy -r` stay as the fallback for
older checkouts. Adds a first device run's findings: op names through Tracy, headless capture,
the `python -m tracy` clash, the streaming CSV columns and first-run traps, and from a full model
run, profiling the steady part and checking every op is on every chip, the dropped-frames
warning, the teardown drain before the ops CSV is written, and what its durations and `EXECUTION`
rows mean.

---
"tt-debug-tools": patch
---

`tt-profiler` makes the streaming profiler (`TT_METAL_STREAMING_PROFILER=1`) the default where
the checkout has it: how to detect it, its Tracy and CSV sinks, the host callback API and the
device markers, and its known gaps. The device profiler and `tracy -r` stay as the fallback for
older checkouts. Adds a first device run's findings: op names through Tracy, headless capture,
the `python -m tracy` clash, the streaming CSV columns and first-run traps, and from a full model
run, the teardown wait before the ops CSV is written and the dropped-frames warning.

---
"tt-debug-tools": minor
"tt-skills": minor
---

Add the optional `tt-debug-tools` plugin. One skill per Tenstorrent debugging
question: tt-triage, tt-dprint, tt-watcher, tt-asserts, tt-exalens,
tt-inspector, tt-profiler, tt-noc-dump, tt-ttnn-flags. Each teaches the
tool's environment surface, a recipe that produces output, what the fields
mean, and the traps. Every skill ships with a provoker, a captured fixture,
or both.

Verified on Wormhole n300 against real device output rather than docs. That
pass corrected the live spelling of the watcher NoC-sanitize disable flag,
the profiler CSV schema, and pinned `TT_METAL_DEVICE_PRINT` as inert.

52 evals cover interface knowledge, reading captured output, and device
execution. Device evals launch a real hung workload through `tt-device-mcp`,
run the tool the skill teaches, and grade the diagnosis on substrings only
that tool could have surfaced. Not in CI — priced per run and
non-deterministic. Invoke with `python plugins/tt-debug-tools/evals/run.py`.

The finder catalogue gains a boundary against `tt-autodebug`: that plugin
owns the debugging method, this one owns the tools those methods reach for.

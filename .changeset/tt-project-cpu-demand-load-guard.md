---
"tt-project": patch
---

`tt-project`: the checks load guard (`checks.load_guard`) and the host-load watch now judge CPU overload by the host's runnable CPU demand (busy cores and tasks waiting to run, read from /proc/stat over a few seconds), not the load average, which also counts tasks waiting on I/O in D state. A host whose disk is contended while its cores sit idle no longer holds new suites as CPU-overloaded; disk contention stays held by its own I/O pressure limit (`checks.io_full_max`). `ttp checks status` shows the CPU demand, the CPU limit and the load average, and which limit holds. Where /proc/stat cannot be read the 1-minute load average stands in.

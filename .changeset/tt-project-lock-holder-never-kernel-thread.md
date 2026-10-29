---
"tt-project": patch
---

`tt-project`: `ttp lock` no longer records a sandboxed taker's own low pid (often 2 or 13, a kernel thread on the host) as the holder. A take in a PID namespace other than its run's names the run's agent and its process group, keeping its own pid as `ns_pid`. Every take now also records its pgid. A candidate that is a kernel thread is never named: the take falls back to its own process and says so. A label or machine-activity entry already on disk whose pid is a kernel thread reads as "holder unverifiable": it is not alive forever, its run's worker pid is checked instead, and a done task's hold is marked stale as usual. Valid holders read as before.

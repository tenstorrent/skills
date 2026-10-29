---
"tt-project": patch
---

`tt-project`: `ttp lock --probe` (and every wait or busy line, `--json`, `ttp status` and the machine activity view) no longer shows a sandboxed taker's or keeper's pid as a bare, unjudged number. A pid recorded in another PID namespace on this host is marked as such and judged through that namespace (the process of that namespace with that pid, and its pid here): alive, gone, or alive unknown with why when that namespace is not visible from the prober. A done task's hold whose holder lives or cannot be checked is no longer marked stale. Same-namespace holders read as before.

---
"tt-project": patch
---

`tt-project`: `ttp lock --probe` run inside a sandbox no longer reports a holder from another PID namespace as "alive unknown" when its run can answer. A holder whose namespace has no process visible from the prober is judged through its run's folder, which every namespace on the host reads: a keeper of detached jobs lives while its hold record names it, still holding, and its new `hold.beat` (touched every 30 s) is fresh, and is gone once the record ended the hold; a run's agent or supervisor lives while the run's lease is fresh, and the agent is gone once the run wrote `exit.json`. A taker that shares the prober's namespace is judged by its own pid there. A stale beat or lease never makes a holder gone; liveness stays unknown, naming the route tried, only when no route answers.

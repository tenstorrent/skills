---
"tt-project": patch
---

`tt-project`: a push check's time limit (`delivery.push_checks_timeout_s`) now counts wall time since the check started, its waits for a test-pool slot included (only waits for a grant, such as a `ttp lock`, a checks lane or a checks slot, stay out): past it the check is ended within a poll, running or waiting for a slot, and the push log says it timed out and after how long. `ttp pool-wait --start` / `--stop` mark a live pool wait from a script's own wait loop; while a check waits for a slot the push log gives the seconds so far every minute and `ttp push --result` shows them, and the final pool-wait figure is the real wait. `--waited` and `ttp pool-wait -- <cmd>` work as before.

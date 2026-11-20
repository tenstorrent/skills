---
"tt-project": patch
---

`tt-project`: when a task is dispatched again, or its `retry_when` moves to a newer run's checks, the daemon now stops the earlier run's `ttp checks` that still wait for a slot or sit parked on a repository lane. Their queue entry and lane are freed, so one task no longer holds two checks runs. The daemon keeps those checks while any live task's wait (`retry_when`, `then`, `on_pass_cmd`, `next_step`) still names that run, or while another run has joined them. Checks that are already running their commands still run to the end.

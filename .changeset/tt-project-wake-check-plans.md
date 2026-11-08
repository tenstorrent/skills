---
"tt-project": patch
---

A waiting task with a `retry_when` now gets a check plan: one interim check soon after the wait
starts and one at its expected end (the measured median wait of its kind). In between, only a
changed probe answer wakes it. Each check runs light, judged by Jev by default. The coordinator can
set any task's plan with `task_update` `check_plan`, and the daily review tunes the defaults.

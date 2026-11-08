---
"tt-project": patch
---

A waiting task with a `retry_when` now gets a check plan: one interim check soon after the wait
starts and one at its expected end (the measured median wait of its kind). In between, only a
changed probe answer wakes it. Each check runs light, judged by Jev by default. The coordinator can
set any task's plan with `task_update` `check_plan`, and the daily review tunes the defaults.
A planned check is no event: when Jev gives no verdict, a Jev-judged check falls back to the plain
"still pending" rules. If no rule decides either, the check sleeps rather than waking the model,
since a pass or failure still wakes the task. Its skips do not bring the gate's cap wake closer.

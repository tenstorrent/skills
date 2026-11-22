---
"tt-project": patch
---

ETA history shadow: a promise gets a history estimate (the latest of its live owner tasks' ends, read through
the same owner rule as the live ETA), calibration shows computed ETAs apart from backfilled promise dues, and
the shadow line reports errors per kind and whether the history method is good enough to promote. task_add's
overlap and chain detection ignores spec paths under a run's result folder (state/runs/), so a shared run
result path no longer chains two tasks.

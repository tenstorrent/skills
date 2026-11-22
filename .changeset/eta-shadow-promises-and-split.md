---
"tt-project": patch
---

ETA history shadow: a promise gets a history estimate (the latest of its owner tasks' ends), calibration
shows computed ETAs apart from backfilled promise dues, the shadow line reports errors per kind and whether
the history method is good enough to promote, and a failing shadow never breaks `ttp eta`. task_add's
overlap and chain detection ignores file paths outside the repository's tracked tree (a shared run result
path no longer chains two tasks).

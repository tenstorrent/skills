---
"tt-project": patch
---

`tt-project`: a user-ordered urgent task stuck behind full worker slots now wakes the coordinator once per run and hold, not up to three times: the urgent start, slot wait and ready-not-started events skip a task one of the others already raised. An urgent `ttp lock` wait event names only the holders of the lock that wait is on, which `ttp lock` now records with the wait.
